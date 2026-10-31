"""Process-level doubles and helpers: nodriver registry, fake Chrome, process table, singleton artefacts.

Split out of :mod:`fakes` (T-5431); that module re-exports every name here, so
tests keep writing ``from fakes import ...``.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import psutil
import pytest
from nodriver.core.util import get_registered_instances

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# nodriver's process-wide Browser registry
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def nodriver_registry() -> Any:
    """nodriver's ``__registered__instances__`` set, restored on the way out.

    It is a MODULE GLOBAL of nodriver's and an ``atexit`` hook walks it, so a
    test that adds to it and does not clean up poisons the interpreter's exit
    and every later reader. Yields the live set; discards exactly what the
    block added and leaves what it found — which is the discipline
    ``test_browser_connect.py`` had written out by hand at two of its nodes.
    """
    known = set(get_registered_instances())
    try:
        yield get_registered_instances()
    finally:
        for browser in tuple(get_registered_instances()):
            if browser not in known:
                get_registered_instances().discard(browser)


class LaunchedBrowser:
    """nodriver's ``Browser`` as a FAILED launch leaves it (F-919).

    ``Browser.start`` sets ``_process``/``_process_pid`` and registers the
    object BEFORE it polls ``/json/version``, so a connect failure leaves
    exactly this shape behind: the caller's ``config`` stored verbatim, a live
    ``asyncio`` process handle whose ``returncode`` is still None, and the pid.
    Those three attributes are all ``spawn_leak.launched_pid`` reads.

    A double of a library object, so it is pinned against the article rather
    than trusted: ``test_spawn_leak`` drives the REAL ``Browser.start`` over a
    faked subprocess at one node and asserts the same pid comes back, which is
    what would catch this class drifting from nodriver.
    """

    def __init__(self, config: Any, pid: int) -> None:
        self.config = config
        self._process = SimpleNamespace(pid=pid, returncode=None)
        self._process_pid = pid


class FakeChrome:
    """A ``psutil.Process`` double for one Chromium-family process (F-919)."""

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

    def children(self, recursive=False):
        # F-1009: ``devtools_port.owned_pids`` asks who is below the launched pid.
        return []

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


# ---------------------------------------------------------------------------
# Chrome's process-singleton artefacts (F-871)
# ---------------------------------------------------------------------------
# ONE home for writing what Chrome writes into a profile directory, because a
# test that hand-rolls a `SingletonLock` will hand-roll a WRONG one: Chrome's
# lock is a SYMLINK whose target is the string `<hostname>-<pid>` -- it is a
# claim about a pid, not a file that exists -- and `embedded/profile_lock.py`
# reads it the way Chromium's `ParseProcessSingletonLock` does.


def write_singleton(
    profile_dir: Path, content: str, name: str = "SingletonLock"
) -> Path:
    """Write one singleton artefact as a symlink, or as a plain file where the
    platform refuses one (creating a symlink is privileged on Windows).
    ``profile_lock`` reads both forms, so a test means the same thing on either.
    """
    path = profile_dir / name
    try:
        path.symlink_to(content)
    except (OSError, NotImplementedError):
        path.write_text(content, encoding="utf-8")
    return path


def held_profile(profile_dir: Path) -> Path:
    """Make *profile_dir* look like one a LIVE browser is holding, by naming a
    pid that is certainly running: this test's own."""
    return write_singleton(profile_dir, f"{socket.gethostname()}-{os.getpid()}")


def fail_a_rename(
    monkeypatch: Any, step: str, error: OSError, times: int | None = None
) -> list[str]:
    """Make ONE of ``replace_tree``'s two renames raise *error*: ``"displace"``
    (target -> ``*.stealth-previous``) or ``"publish"`` (staging -> target).

    ``times`` is how many calls fail before the real rename runs again, None for
    every one. Returns the names of the failed calls, so a test can count the
    attempts. It raises ``AssertionError`` past 10 so an unbounded retry loop
    fails the test instead of hanging the lane. The rollback's rename (previous
    -> target) matches neither step and keeps working for real."""
    from pathlib import Path

    from stealth_chrome_devtools_mcp.embedded import profile_copy

    attempts: list[str] = []
    real_replace = Path.replace

    def _replace(self, dest):
        if step == "publish":
            hit = profile_copy.STAGING_SUFFIX in self.name
        else:
            hit = Path(dest).name.endswith(profile_copy.PREVIOUS_SUFFIX)
        if hit and (times is None or len(attempts) < times):
            attempts.append(self.name)
            if len(attempts) > 10:
                raise AssertionError("the rename is retried without a bound")
            raise error
        return real_replace(self, dest)

    monkeypatch.setattr(Path, "replace", _replace)
    return attempts


@contextlib.contextmanager
def a_zombie():
    """The pid of a REAL zombie (F-998): a child that has exited and that its
    parent -- this process -- has not waited for, which is what a SIGKILLed Chrome
    is until the backend's child watcher reaps it. ``pid_exists`` answers True for
    it, its argv reads as empty and it can hold nothing. Reaped on the way out.
    POSIX only; Windows has no zombies."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        deadline = time.monotonic() + 10
        while psutil.Process(child.pid).status() != psutil.STATUS_ZOMBIE:
            assert time.monotonic() < deadline, "the child never exited"
            time.sleep(0.005)
        yield child.pid
    finally:
        child.wait()


def forbid_real_process_kills(monkeypatch):
    """Make ``psutil`` unable to end a REAL process and show an EMPTY machine to
    anything that enumerates, and return the list of attempts to end one so the
    caller can assert it stayed empty.

    Enumeration is answered, not refused: ``profile_lock``'s read-only scan of the
    process table runs on every launch, and refusing it only proved (CI, PR #256)
    that these tests had been reading the REAL machine. A raise alone is not
    enough for the kills: a handler that catches ``Exception`` would eat it and
    the test would pass having tried. A test that installs ``ProcessTable``
    replaces these names afterwards and is unaffected."""
    attempts: list[str] = []

    def _forbid(name):
        def refuse(*_args, **_kwargs):
            attempts.append(name)
            raise AssertionError(f"a test reached the real psutil.{name}")

        return refuse

    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [])
    monkeypatch.setattr(psutil.Process, "terminate", _forbid("Process.terminate"))
    monkeypatch.setattr(psutil.Process, "kill", _forbid("Process.kill"))
    return attempts


# ---------------------------------------------------------------------------
# Waiting on a spawned child's boot log (T-9675)
# ---------------------------------------------------------------------------

#: The ceiling for a child that never shows its output and never exits. A slow start
#: is NOT what it bounds (a host whose process starts took 9-12 s lost a fixed 10 s
#: window); the wait ends on events, so a crash that was not captured fails the moment
#: the child is gone, not at this deadline.
CHILD_DEADLINE_SECONDS = 120


def _read_log(boot_log: Path) -> str:
    """The log's text, or ``""`` while it is absent. A transient ``PermissionError``
    (an indexer or AV scan holding the file on Windows) is "not readable yet", the way
    F-994's marker poll treats it."""
    for _ in range(40):
        try:
            return boot_log.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""
        except PermissionError:
            time.sleep(0.05)
    return boot_log.read_text(encoding="utf-8", errors="replace")


def _our_child(pid: int, identity: str) -> psutil.Process | None:
    """``pid``'s process if its command line carries *identity*, else ``None``.

    A pid alone is not an identity on Windows: a child that exited before this ran can
    have its pid handed to a stranger, which must be neither waited on nor killed."""
    try:
        child = psutil.Process(pid)
        return child if identity in " ".join(child.cmdline()) else None
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def boot_log_when_settled(
    boot_log: Path,
    pid: int,
    needle: str,
    *,
    identity: str,
    deadline: float = CHILD_DEADLINE_SECONDS,
) -> str:
    """The boot log's text once *needle* is in it or the child *pid* has exited.

    Both are observable events: the text appearing is success, as fast as the child
    is; the child exiting means its output is on disk or never will be, so the final
    read is the answer. *identity* is a string only the child's command line has; a
    pid that is not that child counts as gone. A child that is still running at
    *deadline* is killed before the failure, so a broken redirect leaves nothing behind.
    """
    end = time.monotonic() + deadline
    child = _our_child(pid, identity)
    while True:
        text = _read_log(boot_log)
        if needle in text or child is None:
            return text
        if time.monotonic() > end:
            with contextlib.suppress(psutil.Error):  # it may have exited just now
                child.kill()
            text = _read_log(boot_log)
            if needle in text:
                return text
            pytest.fail(f"boot-crash child {pid} still running, no {needle!r} logged")
        try:
            child.wait(0.05)
            child = None  # gone: read once more, then answer
        except psutil.TimeoutExpired:
            pass


# ---------------------------------------------------------------------------
# psutil's process walk, as psutil does it (T-11618)
# ---------------------------------------------------------------------------


class PsutilProc:
    """A ``psutil.Process`` double whose reads fail the way the real ones do.

    ``name_error`` / ``cmdline_error`` are raised from the read itself, which
    is where macOS raised ``SystemError`` ("proc_cmdline returned a result with
    an exception set"). ``as_dict`` is psutil 7's own: it absorbs
    ``AccessDenied`` and ``ZombieProcess`` into ``ad_value`` and lets anything
    else out, ``NoSuchProcess`` and ``SystemError`` included.
    """

    def __init__(
        self,
        name: str,
        *,
        pid: int = 1,
        cmdline: list[str] | None = None,
        name_error: BaseException | None = None,
        cmdline_error: BaseException | None = None,
    ) -> None:
        self.pid = pid
        self._name = name
        self._cmdline = cmdline if cmdline is not None else [name]
        self._name_error = name_error
        self._cmdline_error = cmdline_error

    def name(self) -> str:
        if self._name_error is not None:
            raise self._name_error
        return self._name

    def cmdline(self) -> list[str]:
        if self._cmdline_error is not None:
            raise self._cmdline_error
        return list(self._cmdline)

    def as_dict(self, attrs: list[str], ad_value: Any = None) -> dict[str, Any]:
        info: dict[str, Any] = {}
        for attr in attrs:
            try:
                value = getattr(self, attr)
                info[attr] = value() if callable(value) else value
            except (psutil.AccessDenied, psutil.ZombieProcess):
                info[attr] = ad_value
        return info


def psutil_process_iter(procs: list[PsutilProc]) -> Any:
    """``psutil.process_iter`` over *procs*, built the way psutil 7 builds it.

    A GENERATOR that reads the requested ``attrs`` while it iterates, so a read
    that raises something ``as_dict`` does not absorb ends the walk for the
    caller, who cannot resume it. That is the property a hand-written list-
    returning double lacks and why one passed over the defect.
    """

    def process_iter(attrs: list[str] | None = None, ad_value: Any = None) -> Any:
        for proc in procs:
            if attrs is not None:
                try:
                    proc.info = proc.as_dict(attrs, ad_value)
                except psutil.NoSuchProcess:
                    continue
            yield proc

    return process_iter
