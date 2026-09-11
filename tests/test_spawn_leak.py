"""F-860: a spawn that fails AFTER Chrome launched but BEFORE nodriver handed a
``Browser`` back must not leak that Chrome.

nodriver's ``Browser.start()`` spawns Chrome, polls ``/json/version`` and raises
on no answer without killing what it spawned. ``spawn_browser``'s failure
handler only ever stopped a ``Browser`` it HELD, and ``kill_browser_process``
returns early for an instance that was never tracked — tracking happens in
``_apply_post_launch``, i.e. after a successful launch. So a connect failure
left a live, untracked Chrome on the attempt's profile directory; on ``master``
that pins every later spawn to clones with nothing in ``list_instances`` to
explain why.

The seam under test is the orchestrator: ``_launch_browser`` is replaced by a
double that "launches" a Chromium-family process into a fake psutil table and
then raises, exactly as nodriver does. The reap itself walks the REAL
``process_cleanup`` primitives over that table — ``psutil.process_iter``,
``pid_exists`` and ``Process`` are the only things faked. No test spawns Chrome,
and no test touches the real ~/.stealth-mcp: ``pid_file`` is under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import time

import psutil
import pytest

from stealth_chrome_devtools_mcp.embedded import spawn_contention, spawn_exhaustion
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup

INNER_FAILURE = "--- Failed to connect to browser ---"


class FakeChrome:
    """A ``psutil.Process`` double for one Chromium-family process."""

    def __init__(self, pid, user_data_dir, create_time, *, stubborn=False):
        self.pid = pid
        self.info = {"pid": pid, "name": "chrome.exe"}
        self._cmdline = ["chrome.exe", f"--user-data-dir={user_data_dir}", "--headless"]
        self._create_time = create_time
        self.stubborn = stubborn
        self.alive = True
        self.terminate_calls = 0

    def _check(self):
        if not self.alive:
            raise psutil.NoSuchProcess(self.pid)

    def name(self):
        self._check()
        return self.info["name"]

    def cmdline(self):
        self._check()
        return list(self._cmdline)

    def create_time(self):
        self._check()
        return self._create_time

    def terminate(self):
        self._check()
        self.terminate_calls += 1
        if not self.stubborn:
            self.alive = False

    kill = terminate

    def wait(self, timeout=None):
        if self.alive:
            raise psutil.TimeoutExpired(timeout, pid=self.pid)


class ProcessTable:
    """The OS, as far as ``process_cleanup``'s psutil reads are concerned."""

    def __init__(self, monkeypatch):
        self.procs: dict[int, FakeChrome] = {}
        self.iter_calls = 0
        monkeypatch.setattr(psutil, "process_iter", self._iter)
        monkeypatch.setattr(psutil, "pid_exists", self._exists)
        monkeypatch.setattr(psutil, "Process", self._process)

    def _iter(self, attrs=None):
        self.iter_calls += 1
        return [proc for proc in self.procs.values() if proc.alive]

    def _exists(self, pid):
        return pid in self.procs and self.procs[pid].alive

    def _process(self, pid):
        proc = self.procs.get(pid)
        if proc is None or not proc.alive:
            raise psutil.NoSuchProcess(pid)
        return proc

    def launch(self, pid, user_data_dir, *, create_time=None, stubborn=False):
        proc = FakeChrome(
            pid,
            user_data_dir,
            time.time() if create_time is None else create_time,
            stubborn=stubborn,
        )
        self.procs[pid] = proc
        return proc

    def alive_on(self, user_data_dir):
        marker = f"--user-data-dir={user_data_dir}"
        return sorted(
            proc.pid
            for proc in self.procs.values()
            if proc.alive and marker in proc._cmdline
        )


@pytest.fixture
def table(monkeypatch):
    return ProcessTable(monkeypatch)


@pytest.fixture
def pid_file(tmp_path):
    return tmp_path / "browser_pids.json"


def doomed_manager(monkeypatch, pid_file, on_launch, error=None):
    """A BrowserManager whose launch phase runs *on_launch* and then raises."""

    async def failing_launch(self, options, browser_executable, launch_args):
        on_launch(options)
        raise error if error is not None else RuntimeError(INNER_FAILURE)

    monkeypatch.setattr(
        BrowserManager,
        "_resolve_launch_args",
        lambda self, options, proxy, platform_info: ([], "/fake/chrome", []),
    )
    monkeypatch.setattr(BrowserManager, "_launch_browser", failing_launch)
    monkeypatch.setattr(process_cleanup, "pid_file", pid_file)
    monkeypatch.setattr(spawn_exhaustion, "exhaustion_hint", lambda path: None)
    monkeypatch.setattr(spawn_contention, "contention_hint", lambda n: None)
    return BrowserManager()


async def test_chrome_launched_by_a_failed_connect_is_killed(
    monkeypatch, table, pid_file, tmp_path
):
    profile = str(tmp_path / "master")
    manager = doomed_manager(
        monkeypatch, pid_file, lambda options: table.launch(4242, options.user_data_dir)
    )

    with pytest.raises(Exception, match="Failed to connect"):
        await manager.spawn_browser(BrowserOptions(user_data_dir=profile))

    assert table.alive_on(profile) == []
    assert table.procs[4242].terminate_calls == 1
    # Nothing was ever tracked, so nothing is recorded: the registry stays
    # empty rather than gaining a ghost entry for a browser that never served.
    assert not pid_file.exists()


async def test_the_original_error_is_still_what_the_caller_sees(
    monkeypatch, table, pid_file, tmp_path
):
    profile = str(tmp_path / "clone")
    manager = doomed_manager(
        monkeypatch, pid_file, lambda options: table.launch(5151, options.user_data_dir)
    )

    with pytest.raises(Exception) as err:
        await manager.spawn_browser(BrowserOptions(user_data_dir=profile))

    # The reap is silent on success: no artifact appended to the message.
    assert str(err.value) == INNER_FAILURE


async def test_cancellation_mid_launch_reaps_the_launched_chrome(
    monkeypatch, table, pid_file, tmp_path
):
    profile = str(tmp_path / "master")
    manager = doomed_manager(
        monkeypatch,
        pid_file,
        lambda options: table.launch(7777, options.user_data_dir),
        error=asyncio.CancelledError(),
    )

    with pytest.raises(asyncio.CancelledError):
        await manager.spawn_browser(BrowserOptions(user_data_dir=profile))

    assert table.alive_on(profile) == []


async def test_a_browser_that_held_the_profile_before_the_attempt_is_spared(
    monkeypatch, table, pid_file, tmp_path
):
    """An explicit ``user_data_dir`` may be a profile a REAL Chrome already holds
    — Chrome's singleton is then why our launch failed. That process is not ours
    and predates the attempt, so it must survive the reap."""
    profile = str(tmp_path / "explicit")
    table.launch(1111, profile, create_time=time.time() - 3600)
    manager = doomed_manager(monkeypatch, pid_file, lambda options: None)

    with pytest.raises(Exception, match="Failed to connect"):
        await manager.spawn_browser(BrowserOptions(user_data_dir=profile))

    assert table.alive_on(profile) == [1111]
    assert table.procs[1111].terminate_calls == 0


async def test_a_browser_on_another_profile_is_spared(
    monkeypatch, table, pid_file, tmp_path
):
    profile = str(tmp_path / "master")
    other = table.launch(2222, str(tmp_path / "sibling-clone"))
    manager = doomed_manager(
        monkeypatch, pid_file, lambda options: table.launch(3333, options.user_data_dir)
    )

    with pytest.raises(Exception, match="Failed to connect"):
        await manager.spawn_browser(BrowserOptions(user_data_dir=profile))

    assert table.alive_on(profile) == []
    assert other.alive and other.terminate_calls == 0


async def test_no_profile_directory_means_no_process_scan(monkeypatch, table, pid_file):
    """With no ``user_data_dir`` there is nothing to match a process against, so
    the reap does not walk the process table at all."""
    manager = doomed_manager(monkeypatch, pid_file, lambda options: None)

    with pytest.raises(Exception, match="Failed to connect"):
        await manager.spawn_browser(BrowserOptions())

    assert table.iter_calls == 0


async def test_a_chrome_that_refuses_to_die_does_not_mask_the_launch_error(
    monkeypatch, table, pid_file, tmp_path
):
    profile = str(tmp_path / "master")
    manager = doomed_manager(
        monkeypatch,
        pid_file,
        lambda options: table.launch(9999, options.user_data_dir, stubborn=True),
    )

    with pytest.raises(Exception) as err:
        await manager.spawn_browser(BrowserOptions(user_data_dir=profile))

    # Best effort: terminate then kill were both tried, and the caller still
    # gets the launch failure, never a cleanup failure in its place.
    assert table.procs[9999].terminate_calls == 2
    assert str(err.value) == INNER_FAILURE
