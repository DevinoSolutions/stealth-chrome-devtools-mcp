"""What every "is this pid there, and is it the same process" reader answers (T-19642).

There were eleven separate readers of that one question and they disagreed. This
file pins what each one DID answer, in one table, before any of them moved, so
that folding them into ``zombie`` can only change an answer on purpose: the
expectation table below is the contract, and a row that changes in the same
commit as a caller migrates is a behaviour change that commit must justify.

Each scenario is a ``World``: what ``psutil`` answers (or raises) for ONE pid,
built from psutil's own exception types and constants, so every reader sees the
same machine. POSIX's states (zombies, the Linux exec window, a tearing-down
process) are scripted rather than made, because the suite also runs on Windows,
where none of them can exist.
"""

from __future__ import annotations

import errno
from dataclasses import dataclass
from typing import Any

import psutil
import pytest

from stealth_chrome_devtools_mcp.embedded import (
    backend_supervisor,
    browser_cmdline,
    client_presence,
    desktop_launch,
    devtools_port,
    process_exit,
    profile_lock,
    zombie,
)
from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup

PID = 4242
READABLE = "/usr/lib/chromium/chromium"
GONE = FileNotFoundError(errno.ENOENT, "no such file")
UNREADABLE = PermissionError(errno.EACCES, "denied")
NOT_THERE = ProcessLookupError(errno.ESRCH, "no such process")
NOT_OURS = PermissionError(errno.EPERM, "not permitted")


#: Every ``os.kill(pid, 0)`` the last ``install`` let a reader make.
KERNEL_ASKED: list[tuple[int, int]] = []


@dataclass(frozen=True)
class World:
    """What the machine says about the one pid under test.

    A field holding an exception INSTANCE is raised when that read is made.
    """

    listed: Any = True  # psutil.pid_exists
    ctor: BaseException | None = None  # raised by psutil.Process(pid)
    status: Any = psutil.STATUS_RUNNING
    cmdline: Any = ("chrome", "--remote-debugging-port=9222")
    running: Any = True  # Process.is_running()
    created: Any = 1000.0  # Process.create_time()
    exe: Any = READABLE  # Path.readlink(): a target, or an exception to raise
    linux: bool = True  # whether the exec-window witness exists on this platform
    kernel: BaseException | None = NOT_THERE  # os.kill(pid, 0): None = delivered


class _Process:
    """``psutil.Process`` as a World scripts it. Reads answer or raise."""

    def __init__(self, world: World, pid: Any) -> None:
        if world.ctor is not None:
            raise world.ctor
        self._world = world
        self.pid = pid
        self.killed: list[str] = []

    def _read(self, field: str) -> Any:
        value = getattr(self._world, field)
        if isinstance(value, BaseException):
            raise value
        return value

    def status(self) -> str:
        return self._read("status")

    def cmdline(self) -> list[str]:
        return list(self._read("cmdline"))

    def is_running(self) -> bool:
        return self._read("running")

    def create_time(self) -> float:
        return self._read("created")

    def children(self, recursive: bool = False) -> list[Any]:
        return []

    def kill(self) -> None:
        self.killed.append("kill")


def install(monkeypatch: pytest.MonkeyPatch, world: World) -> list[_Process]:
    """Make ``psutil`` and the kernel answer *world*; returns every Process built.

    The ONE place that knows where each reader keeps its seam, so a caller that
    moves house moves one line here and no expectation below.
    """
    built: list[_Process] = []

    def pid_exists(pid: Any) -> bool:
        if isinstance(world.listed, BaseException):
            raise world.listed
        return bool(world.listed)

    def process(pid: Any = None) -> _Process:
        built.append(_Process(world, pid))
        return built[-1]

    def readlink(_self: Any) -> str:
        if isinstance(world.exe, BaseException):
            raise world.exe
        return world.exe

    def kernel(pid: int, sig: int) -> None:
        KERNEL_ASKED.append((pid, sig))
        if world.kernel is not None:
            raise world.kernel

    KERNEL_ASKED.clear()

    monkeypatch.setattr(zombie, "ZOMBIES_EXIST", True)
    monkeypatch.setattr(psutil, "pid_exists", pid_exists)
    monkeypatch.setattr(psutil, "Process", process)
    monkeypatch.setattr(browser_cmdline, "_EXE_WITNESS", world.linux)
    monkeypatch.setattr(browser_cmdline.Path, "readlink", readlink)
    monkeypatch.setattr(profile_lock, "_SIGNAL_ASKS", True)
    monkeypatch.setattr(profile_lock, "_signal", kernel)
    return built


# ---------------------------------------------------------------------------
# The readers, as adapters: each takes a pid and answers a bool (or raises).
# ---------------------------------------------------------------------------


class _Browser:
    def __init__(self, pid: Any) -> None:
        self._process = None
        self._process_pid = pid


READERS = {
    "profile_lock._pid_alive": profile_lock._pid_alive,
    "browser_cmdline._still_running": browser_cmdline._still_running,
    "devtools_port._running": devtools_port._running,
    "process_exit.browser_is_alive": lambda pid: process_exit.browser_is_alive(
        _Browser(pid)
    ),
}
ORDER = tuple(READERS)

RAISES = "raises"
ZOMBIE_ARGV: tuple[str, ...] = ()

#: (world, the four readers' answers in ORDER). A bool, or an exception TYPE the
#: reader let escape. Rows marked DIVERGENCE are where today's readers disagree.
ALIVE_TABLE = {
    "a live process": (World(), (True, True, True, True)),
    "dead: not listed, kernel agrees": (
        World(listed=False, ctor=psutil.NoSuchProcess(PID)),
        (False, False, False, False),
    ),
    # DIVERGENCE 1 (F-1008): only the lock owner's reader asks the kernel when
    # psutil does not list the pid (``/proc`` mounted ``hidepid=2``).
    "not listed by psutil, kernel says it exists": (
        World(listed=False, ctor=psutil.NoSuchProcess(PID), kernel=NOT_OURS),
        (True, False, False, False),
    ),
    "a zombie": (
        World(status=psutil.STATUS_ZOMBIE, cmdline=ZOMBIE_ARGV),
        (False, False, False, False),
    ),
    "a zombie that raises ZombieProcess when asked its status": (
        World(status=psutil.ZombieProcess(PID)),
        (False, False, False, False),
    ),
    # DIVERGENCE 2: what "I could not ask" means. The lock owner's and the tree
    # member's readers say ALIVE (a profile is then not shown free); the other
    # two say DEAD.
    "listed, but psutil.Process() is AccessDenied": (
        World(ctor=psutil.AccessDenied(PID)),
        (True, True, False, False),
    ),
    "listed, argv refused (AccessDenied)": (
        World(cmdline=psutil.AccessDenied(PID)),
        (True, True, True, True),
    ),
    "listed, is_running refused (AccessDenied)": (
        World(running=psutil.AccessDenied(PID)),
        (True, True, False, False),
    ),
    "listed, status refused (AccessDenied)": (
        World(status=psutil.AccessDenied(PID)),
        (True, True, False, False),
    ),
    "pid_exists itself refused (AccessDenied)": (
        World(listed=psutil.AccessDenied(PID)),
        (True, True, True, True),
    ),
    # DIVERGENCE 3: ``_still_running`` lets an OverflowError out; the lock
    # owner's reader answers alive.
    "pid_exists raises OverflowError": (
        World(listed=OverflowError("too large")),
        (True, OverflowError, True, True),
    ),
    # The exec-window trap: a live Chrome reads an EMPTY argv while its launcher
    # execs, and must never be read as dead.
    "exec window: empty argv, executable readable": (
        World(cmdline=(), exe=READABLE),
        (True, True, True, True),
    ),
    "empty argv, executable unreadable for permission": (
        World(cmdline=(), exe=UNREADABLE),
        (True, True, True, True),
    ),
    "empty argv, executable read fails another way": (
        World(cmdline=(), exe=OSError(errno.EIO, "io")),
        (True, True, True, True),
    ),
    # DIVERGENCE 4 (F-998): only the two witness readers see a teardown.
    "tearing down: empty argv, executable gone": (
        World(cmdline=(), exe=GONE),
        (False, False, True, True),
    ),
    "tearing down: empty argv, ESRCH on the executable": (
        World(cmdline=(), exe=NOT_THERE),
        (False, False, True, True),
    ),
    "empty argv, executable gone, but not Linux": (
        World(cmdline=(), exe=GONE, linux=False),
        (True, True, True, True),
    ),
    "an argv outranks an unreadable executable": (
        World(exe=GONE),
        (True, True, True, True),
    ),
}


@pytest.mark.parametrize("name", ALIVE_TABLE)
def test_every_reader_answers_what_it_always_did(monkeypatch, name):
    world, expected = ALIVE_TABLE[name]
    install(monkeypatch, world)

    for reader, want in zip(ORDER, expected, strict=True):
        if isinstance(want, type):
            with pytest.raises(want):
                READERS[reader](PID)
        else:
            assert READERS[reader](PID) is want, f"{reader}: {name}"


# ---------------------------------------------------------------------------
# Pids that name no process: 0, negative, None.
# ---------------------------------------------------------------------------


def test_a_negative_pid_reads_dead_everywhere_but_the_lock_owners_kernel_probe(
    monkeypatch,
):
    """``os.kill(-1, 0)`` signals EVERY process the user may signal, so on POSIX a
    kernel probe of -1 answers "exists". Only ``profile_lock`` reaches it, and its
    one caller guards ``pid > 0``, which is all that kept this latent."""
    install(monkeypatch, World(listed=False, ctor=ValueError("pid must be positive")))

    assert profile_lock._pid_alive(-1) is False  # the scripted kernel says gone
    assert KERNEL_ASKED == [(-1, 0)]  # but it WAS asked
    assert browser_cmdline._still_running(-1) is False
    assert process_exit.browser_is_alive(_Browser(-1)) is False
    with pytest.raises(ValueError):
        devtools_port._running(-1)


def test_pid_none_is_this_process_to_psutil_and_one_reader_believes_it():
    """``psutil.Process(None)`` is the CALLING process, so a missing pid read as
    "alive" in the one reader that built a Process from it directly."""
    assert devtools_port._running(None) is True  # type: ignore[arg-type]


@pytest.mark.parametrize("pid", [0, None])
def test_a_browser_with_no_pid_is_assumed_up(pid):
    assert process_exit.browser_is_alive(_Browser(pid)) is True


# ---------------------------------------------------------------------------
# "Is it the same process": a (pid, create_time) pair.
# ---------------------------------------------------------------------------

T = 1000.0


IDENTITY_READERS = {
    "ProcessCleanup._fallback_pid_identity_ok": ProcessCleanup._fallback_pid_identity_ok,
    "backend_supervisor._still": backend_supervisor._still,
    "client_presence.present": lambda pid, created: client_presence.present(
        (pid, created)
    ),
}
IDENTITY_ORDER = tuple(IDENTITY_READERS)

IDENTITY_TABLE = {
    "same pid, same start": (World(created=T), T, (True, True, True)),
    "start time 0.5 s apart": (World(created=T + 0.5), T, (True, False, False)),
    "start time 1.0 s apart": (World(created=T + 1.0), T, (False, False, False)),
    "start time 2 s apart (a reused pid)": (
        World(created=T + 2.0),
        T,
        (False, False, False),
    ),
    "no start time recorded": (World(created=T), None, (True, False, False)),
    "the pid is gone": (
        World(ctor=psutil.NoSuchProcess(PID)),
        T,
        (False, False, False),
    ),
    "the pid is a zombie that cannot be opened": (
        World(ctor=psutil.ZombieProcess(PID)),
        T,
        (False, False, False),
    ),
    # DIVERGENCE: "could not ask". Reaping/supervision say no; the client's
    # presence says yes (a false "gone" disconnects a session).
    "the pid is AccessDenied": (
        World(ctor=psutil.AccessDenied(PID)),
        T,
        (False, False, True),
    ),
    "start time read refused (AccessDenied)": (
        World(created=psutil.AccessDenied(PID)),
        T,
        (False, False, True),
    ),
    "start time read fails with an OSError": (
        World(created=OSError(errno.EIO, "io")),
        T,
        (OSError, False, True),
    ),
    "a zombie whose start time is still readable": (
        World(status=psutil.STATUS_ZOMBIE, created=T),
        T,
        (True, True, True),
    ),
    "psutil.Error (not AccessDenied) reading the start time": (
        World(created=psutil.Error("odd")),
        T,
        (psutil.Error, False, True),
    ),
}


@pytest.mark.parametrize("name", IDENTITY_TABLE)
def test_every_identity_reader_answers_what_it_always_did(monkeypatch, name):
    world, recorded, expected = IDENTITY_TABLE[name]
    install(monkeypatch, world)

    for reader, want in zip(IDENTITY_ORDER, expected, strict=True):
        call = IDENTITY_READERS[reader]
        if isinstance(want, type):
            with pytest.raises(want):
                call(PID, recorded)
        else:
            assert call(PID, recorded) is want, f"{reader}: {name}"


def test_a_client_that_was_never_identified_is_present():
    assert client_presence.present(None) is True


# ---------------------------------------------------------------------------
# The kill that must not reach a recycled pid.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("world", "recorded", "kills"),
    [
        pytest.param(World(created=T), T, True, id="same process"),
        pytest.param(World(created=T + 0.5), T, True, id="0.5 s apart: still ours"),
        pytest.param(World(created=T + 0.51), T, False, id="0.51 s apart: recycled"),
        pytest.param(World(created=T), None, False, id="no identity stamp: no proof"),
        pytest.param(
            World(ctor=psutil.NoSuchProcess(PID)), T, False, id="already gone"
        ),
        pytest.param(
            World(ctor=psutil.AccessDenied(PID)), T, False, id="cannot be opened"
        ),
    ],
)
def test_a_delegated_chrome_is_killed_only_when_it_is_provably_ours(
    monkeypatch, world, recorded, kills
):
    built = install(monkeypatch, world)

    desktop_launch._kill_delegated(PID, recorded)

    assert any(p.killed for p in built) is kills


# ---------------------------------------------------------------------------
# "When did this pid start": the create_time-or-None reads.
# ---------------------------------------------------------------------------


def test_the_start_time_readers_agree_on_psutil_errors_and_one_lets_oserror_out(
    monkeypatch,
):
    from stealth_chrome_devtools_mcp.embedded import (
        browser_pid_registry,
        browser_reattach,
    )

    install(monkeypatch, World(created=T))
    assert desktop_launch._process_create_time(PID) == T
    assert browser_reattach._create_time(PID) == T

    install(monkeypatch, World(created=psutil.AccessDenied(PID)))
    assert desktop_launch._process_create_time(PID) is None
    assert browser_reattach._create_time(PID) is None

    install(monkeypatch, World(created=OSError(errno.EIO, "io")))
    assert browser_reattach._create_time(PID) is None
    assert browser_pid_registry.owner_identity()[1] is None
    with pytest.raises(OSError):
        desktop_launch._process_create_time(PID)


# ---------------------------------------------------------------------------
# The raw "is the number listed" readers: a zombie IS listed, and they say so.
# ---------------------------------------------------------------------------


def _cleanup(entries: dict[str, dict[str, Any]]) -> ProcessCleanup:
    cleanup = ProcessCleanup.__new__(ProcessCleanup)
    cleanup.browser_processes = entries
    return cleanup


@pytest.mark.parametrize(
    ("world", "claimed"),
    [
        pytest.param(World(), True, id="a live pid claims the profile"),
        pytest.param(
            World(status=psutil.STATUS_ZOMBIE, cmdline=ZOMBIE_ARGV),
            True,
            id="a zombie still claims it: the delete waits",
        ),
        pytest.param(
            World(listed=False, ctor=psutil.NoSuchProcess(PID)), False, id="gone"
        ),
    ],
)
def test_a_deferred_delete_waits_for_every_pid_that_is_still_listed(
    monkeypatch, world, claimed
):
    install(monkeypatch, world)
    cleanup = _cleanup({"other": {"pid": PID, "user_data_dir": "/p"}})
    monkeypatch.setattr(cleanup, "_normalize_path", lambda path: path, raising=False)

    assert cleanup._profile_claimed_by_live_instance("/p", "mine") is claimed


def test_a_recorded_pid_that_is_not_an_int_claims_nothing(monkeypatch):
    install(monkeypatch, World())
    cleanup = _cleanup({"other": {"pid": str(PID), "user_data_dir": "/p"}})
    monkeypatch.setattr(cleanup, "_normalize_path", lambda path: path, raising=False)

    assert cleanup._profile_claimed_by_live_instance("/p", "mine") is False


def test_a_reap_of_an_unlisted_pid_is_already_done(monkeypatch):
    install(monkeypatch, World(listed=False, ctor=psutil.NoSuchProcess(PID)))

    assert _cleanup({})._kill_process_by_pid(PID, "x") is True
