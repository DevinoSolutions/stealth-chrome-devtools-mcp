"""F-860: a spawn that fails AFTER Chrome launched but BEFORE nodriver handed a
``Browser`` back must not leak that Chrome. F-919: and it must reap the process
IT launched, never whichever browser on that directory started around the same
time.

nodriver's ``Browser.start()`` spawns Chrome, polls ``/json/version`` and raises
on no answer without killing what it spawned. ``spawn_browser``'s failure
handler only ever stopped a ``Browser`` it HELD, and ``kill_browser_process``
returns early for an instance that was never tracked — tracking happens in
``_apply_post_launch``, i.e. after a successful launch. So a connect failure
left a live, untracked Chrome on the attempt's profile directory; on the shared
profile that pins every later spawn to clones with nothing in ``list_instances``
to explain why.

F-860 fenced that reap on a start TIME — everything on the directory that
started within a second of the attempt — and F-919 is what that window costs
under concurrency: concurrent unnamed spawns all select the shared profile by
design (F-834), they stamp their launches 9.9-86.4 ms apart (measured), and the
loser's teardown therefore reaped the WINNER, in exactly the profile-singleton
case the reap promised to spare. ``test_the_sibling_that_won_the_race_survives``
is the pin for that; the fence is the launched PID now.

The seam under test is the orchestrator: ``_launch_browser`` is replaced by a
double that "launches" a Chromium-family process into a fake psutil table,
registers it with nodriver exactly as a failed ``Browser.start`` leaves it, and
then raises. The reap itself walks the REAL ``process_cleanup`` primitives over
that table — ``psutil.process_iter``, ``pid_exists`` and ``Process`` are the
only things faked, and ``fakes.LaunchedBrowser`` is pinned against the REAL
``Browser.start`` at ``test_a_real_failed_nodriver_start_is_resolved_to_its_pid``
rather than trusted. No test spawns Chrome, and no test touches the real
~/.stealth-mcp: ``pid_file`` is under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import sys
import time
import urllib.error

import psutil
import pytest
from nodriver.core.browser import Browser, HTTPApi

from fakes import LaunchedBrowser, nodriver_registry
from stealth_chrome_devtools_mcp.embedded import (
    browser_connect,
    spawn_contention,
    spawn_exhaustion,
    spawn_leak,
)
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

    def __init__(self, monkeypatch, registry):
        self.procs: dict[int, FakeChrome] = {}
        self.iter_calls = 0
        self._registry = registry
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

    def launch(
        self, pid, user_data_dir, *, attempt=None, create_time=None, stubborn=False
    ):
        """Start a Chrome, and — when an *attempt* owns it — register it with
        nodriver the way ``Browser.start`` does before it polls.

        ``attempt=None`` is a browser this backend did not launch: a real Chrome
        already on the directory, or a sibling of another kind.

        The stamped config is a bare sentinel rather than a ``uc.Config``:
        ``launched_pid`` compares it by IDENTITY and never reads a field of it,
        so building the real thing here would add a Chrome-executable lookup and
        prove nothing. The node that needs a real one builds it through the real
        ``_launch_browser``.
        """
        proc = FakeChrome(
            pid,
            user_data_dir,
            time.time() if create_time is None else create_time,
            stubborn=stubborn,
        )
        self.procs[pid] = proc
        if attempt is not None:
            attempt.config = object()
            self._registry.add(LaunchedBrowser(attempt.config, pid))
        return proc

    def alive_on(self, user_data_dir):
        marker = f"--user-data-dir={user_data_dir}"
        return sorted(
            proc.pid
            for proc in self.procs.values()
            if proc.alive and marker in proc._cmdline
        )


@pytest.fixture
def registry():
    with nodriver_registry() as live:
        yield live


@pytest.fixture
def table(monkeypatch, registry):
    return ProcessTable(monkeypatch, registry)


@pytest.fixture
def pid_file(tmp_path):
    return tmp_path / "browser_pids.json"


def doomed_manager(monkeypatch, pid_file, on_launch, error=None):
    """A BrowserManager whose launch phase runs *on_launch* and then raises."""

    async def failing_launch(self, options, browser_executable, launch_args, attempt):
        on_launch(options, attempt)
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
        monkeypatch,
        pid_file,
        lambda options, attempt: table.launch(
            4242, options.user_data_dir, attempt=attempt
        ),
    )

    with pytest.raises(Exception, match="Failed to connect"):
        await manager.spawn_browser(BrowserOptions(user_data_dir=profile))

    assert table.alive_on(profile) == []
    assert table.procs[4242].terminate_calls == 1
    # Nothing was ever tracked, so nothing is recorded: the registry stays
    # empty rather than gaining a ghost entry for a browser that never served.
    assert not pid_file.exists()


async def test_the_sibling_that_won_the_race_survives(
    monkeypatch, table, pid_file, tmp_path
):
    """THE F-919 pin. Two concurrent spawns onto ONE directory — which is what
    the shared profile gets, by design (F-834) — where one wins the profile
    singleton and the other fails because of it. The loser must reap its own
    Chrome and leave the winner's alone.

    The winner is held INSIDE its launch so both attempts are genuinely in
    flight when the loser's teardown runs; their launches land 9.9-86.4 ms
    apart in reality, and the shipped one-second window covered every one of
    them. Nothing here mentions a time: the loser is fenced on the pid it
    launched, so the separation is not a parameter of the answer any more.
    """
    profile = str(tmp_path / "master")
    winner_launched = asyncio.Event()
    release_winner = asyncio.Event()
    pids = iter((5001, 5002))

    async def launch(self, options, browser_executable, launch_args, attempt):
        pid = next(pids)
        table.launch(pid, options.user_data_dir, attempt=attempt)
        if pid == 5001:  # the winner: still coming up when the loser gives up
            winner_launched.set()
            await release_winner.wait()
            raise asyncio.CancelledError
        raise RuntimeError(INNER_FAILURE)

    monkeypatch.setattr(
        BrowserManager,
        "_resolve_launch_args",
        lambda self, options, proxy, platform_info: ([], "/fake/chrome", []),
    )
    monkeypatch.setattr(BrowserManager, "_launch_browser", launch)
    monkeypatch.setattr(process_cleanup, "pid_file", pid_file)
    monkeypatch.setattr(spawn_exhaustion, "exhaustion_hint", lambda path: None)
    monkeypatch.setattr(spawn_contention, "contention_hint", lambda n: None)
    manager = BrowserManager()

    winner = asyncio.create_task(
        manager.spawn_browser(BrowserOptions(user_data_dir=profile))
    )
    await winner_launched.wait()

    with pytest.raises(Exception, match="Failed to connect"):
        await manager.spawn_browser(BrowserOptions(user_data_dir=profile))

    # The loser reaped ITS Chrome and nothing else. Before F-919 both were on
    # the directory inside the loser's one-second window and both died.
    assert table.alive_on(profile) == [5001]
    assert table.procs[5001].terminate_calls == 0
    assert table.procs[5002].terminate_calls == 1

    release_winner.set()
    with pytest.raises(asyncio.CancelledError):
        await winner
    # And the winner's own teardown still reaps the winner's own Chrome.
    assert table.alive_on(profile) == []


async def test_a_real_failed_nodriver_start_is_resolved_to_its_pid(
    monkeypatch, registry, tmp_path
):
    """``fakes.LaunchedBrowser`` against the article it doubles.

    Every other node here reads a pid back out of a hand-written stand-in, so
    one node drives nodriver's REAL ``Browser.start`` — its own ``range(5)``,
    its own "Failed to connect to browser" — over a faked subprocess and a
    refusing endpoint, and asserts ``launched_pid`` finds the same pid through
    the config object ``_launch_browser`` stamped. If nodriver stopped storing
    the caller's config verbatim, or stopped registering before it polls, this
    is what would say so; the stand-in alone would keep passing.
    """
    process = type("P", (), {"pid": 7331, "returncode": None})()

    async def _spawn(*_args, **_kwargs):
        return process

    async def _refuse(self, endpoint_name, method="get", data=None):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    now = {"t": 0.0}

    async def _tick(seconds=0.1):
        now["t"] += seconds
        await asyncio.sleep(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(HTTPApi, "_request", _refuse)
    monkeypatch.setattr(Browser, "sleep", lambda self, t=0.1: _tick(t))
    # Our own 30 s patience on a virtual clock, so the node costs nodriver's
    # one real 0.25 s lead and nothing else (test_browser_connect's pattern).
    monkeypatch.setattr(browser_connect, "_now", lambda: now["t"])
    monkeypatch.setattr(browser_connect, "_sleep", _tick)
    browser_connect.install()

    attempt = spawn_leak.Attempt()
    manager = BrowserManager()
    with pytest.raises(Exception, match="Failed to connect to browser"):
        await manager._launch_browser(
            BrowserOptions(headless=True, user_data_dir=str(tmp_path / "profile")),
            sys.executable,
            [],
            attempt,
        )

    assert attempt.config is not None
    assert spawn_leak.launched_pid(attempt) == 7331
    # And it is OUR launch that was found, not just "some registered browser":
    # a second registered browser with a different config answers nothing.
    assert spawn_leak.launched_pid(spawn_leak.Attempt(config=object())) is None
    assert len(registry) >= 1


async def test_the_original_error_is_still_what_the_caller_sees(
    monkeypatch, table, pid_file, tmp_path
):
    profile = str(tmp_path / "clone")
    manager = doomed_manager(
        monkeypatch,
        pid_file,
        lambda options, attempt: table.launch(
            5151, options.user_data_dir, attempt=attempt
        ),
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
        lambda options, attempt: table.launch(
            7777, options.user_data_dir, attempt=attempt
        ),
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
    and must survive the reap. It is spared now because no attempt of ours ever
    launched it, not because of when it started: the ``create_time`` here is the
    same instant the attempt runs, which is exactly what the F-860 window could
    not tell from a leak."""
    profile = str(tmp_path / "explicit")
    table.launch(1111, profile)
    manager = doomed_manager(monkeypatch, pid_file, lambda options, attempt: None)

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
        monkeypatch,
        pid_file,
        lambda options, attempt: table.launch(
            3333, options.user_data_dir, attempt=attempt
        ),
    )

    with pytest.raises(Exception, match="Failed to connect"):
        await manager.spawn_browser(BrowserOptions(user_data_dir=profile))

    assert table.alive_on(profile) == []
    assert other.alive and other.terminate_calls == 0


async def test_no_profile_directory_means_no_process_scan(monkeypatch, table, pid_file):
    """With no ``user_data_dir`` there is nothing to match a process against, so
    the reap does not walk the process table at all."""
    manager = doomed_manager(monkeypatch, pid_file, lambda options, attempt: None)

    with pytest.raises(Exception, match="Failed to connect"):
        await manager.spawn_browser(BrowserOptions())

    assert table.iter_calls == 0


async def test_a_launch_we_cannot_name_is_left_alone(
    monkeypatch, table, pid_file, tmp_path, caplog
):
    """The cost of an identity fence, pinned rather than implied (F-919 §6).

    A launch that never reached nodriver — the delegated headed path, or a raise
    before Chrome could exist — leaves nothing this teardown can name. A Chrome
    on that directory is then left RUNNING even if it genuinely leaked, because
    the alternative is killing a process on a guess, which is the defect. It
    costs no process-table walk either, and it says so in the log.
    """
    profile = str(tmp_path / "master")
    stranger = table.launch(8080, profile)
    manager = doomed_manager(monkeypatch, pid_file, lambda options, attempt: None)

    with caplog.at_level("INFO"), pytest.raises(Exception, match="Failed to connect"):
        await manager.spawn_browser(BrowserOptions(user_data_dir=profile))

    assert stranger.alive and stranger.terminate_calls == 0
    assert table.iter_calls == 0


async def test_a_chrome_that_refuses_to_die_does_not_mask_the_launch_error(
    monkeypatch, table, pid_file, tmp_path
):
    profile = str(tmp_path / "master")
    manager = doomed_manager(
        monkeypatch,
        pid_file,
        lambda options, attempt: table.launch(
            9999, options.user_data_dir, attempt=attempt, stubborn=True
        ),
    )

    with pytest.raises(Exception) as err:
        await manager.spawn_browser(BrowserOptions(user_data_dir=profile))

    # Best effort: terminate then kill were both tried, and the caller still
    # gets the launch failure, never a cleanup failure in its place.
    assert table.procs[9999].terminate_calls == 2
    assert str(err.value) == INNER_FAILURE


async def test_a_browser_already_collected_is_not_killed(
    monkeypatch, table, pid_file, tmp_path, registry
):
    """A handle whose ``returncode`` is set is a child asyncio has already
    reaped, so on POSIX its pid is free from that instant and may belong to a
    stranger. ``process_exit.browser_pid`` refuses it, and this is the one
    place a pid could otherwise be recycled out from under the reap."""
    profile = str(tmp_path / "master")

    def launch(options, attempt):
        proc = table.launch(6060, options.user_data_dir, attempt=attempt)
        for browser in registry:
            if getattr(browser, "config", None) is attempt.config:
                browser._process.returncode = 0
        return proc

    manager = doomed_manager(monkeypatch, pid_file, launch)

    with pytest.raises(Exception, match="Failed to connect"):
        await manager.spawn_browser(BrowserOptions(user_data_dir=profile))

    assert table.alive_on(profile) == [6060]
    assert table.procs[6060].terminate_calls == 0
