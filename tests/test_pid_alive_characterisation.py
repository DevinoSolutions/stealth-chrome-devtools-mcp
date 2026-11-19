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

import asyncio
import errno
import os
import subprocess
import sys
import time
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
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

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
    monkeypatch.setattr(zombie.Path, "readlink", readlink)
    monkeypatch.setattr(zombie, "_EXE_WITNESS", world.linux)
    monkeypatch.setattr(zombie, "_SIGNAL_ASKS", True)
    monkeypatch.setattr(zombie, "_signal", kernel)
    return built


# ---------------------------------------------------------------------------
# The readers, as adapters: each takes a pid and answers a bool (or raises).
# ---------------------------------------------------------------------------


class _Browser:
    def __init__(self, pid: Any) -> None:
        self._process = None
        self._process_pid = pid


READERS = {
    "profile_lock's reader (now zombie.alive, held)": lambda pid: zombie.alive(
        pid, unknown=True
    ),
    "tree member's reader (now zombie.alive, held)": lambda pid: zombie.alive(
        pid, unknown=True
    ),
    "devtools_port's reader (now zombie.alive, dead if unknown)": lambda pid: (
        zombie.alive(pid, unknown=False)
    ),
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
    # psutil does not list the pid (``/proc`` mounted ``hidepid=2``). CHANGED in
    # the browser_cmdline commit: the tree member's reader now asks it too.
    "not listed by psutil, kernel says it exists": (
        World(listed=False, ctor=psutil.NoSuchProcess(PID), kernel=NOT_OURS),
        (True, True, True, True),
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
        (True, True, True, True),
    ),
    "listed, status refused (AccessDenied)": (
        World(status=psutil.AccessDenied(PID)),
        (True, True, False, False),
    ),
    "pid_exists itself refused (AccessDenied)": (
        World(listed=psutil.AccessDenied(PID)),
        (True, True, False, False),
    ),
    # DIVERGENCE 3: ``_still_running`` let an OverflowError out; the lock
    # owner's reader answers alive. CHANGED in the browser_cmdline commit: one
    # function now, and it answers alive.
    "pid_exists raises OverflowError": (
        World(listed=OverflowError("too large")),
        (True, True, False, False),
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
        (False, False, False, False),
    ),
    "tearing down: empty argv, ESRCH on the executable": (
        World(cmdline=(), exe=NOT_THERE),
        (False, False, False, False),
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


def test_a_negative_pid_reads_dead_everywhere_and_the_kernel_is_not_asked(
    monkeypatch,
):
    """``os.kill(-1, 0)`` signals EVERY process the user may signal, so on POSIX a
    kernel probe of -1 answers "exists". The lock owner's reader used to reach it
    (its one caller guarded ``pid > 0``, which is all that kept this latent); the
    one home answers a pid that names no process without asking anything."""
    install(
        monkeypatch,
        World(listed=False, ctor=ValueError("pid must be positive"), kernel=None),
    )

    assert zombie.alive(-1, unknown=True) is False
    assert zombie.alive(-1, unknown=False) is False
    assert KERNEL_ASKED == []
    assert process_exit.browser_is_alive(_Browser(-1)) is False


@pytest.mark.parametrize(
    ("world", "held"),
    [
        pytest.param(World(), True, id="a live owner"),
        pytest.param(
            World(ctor=psutil.AccessDenied(PID)), True, id="an owner we cannot open"
        ),
        pytest.param(
            World(status=psutil.AccessDenied(PID)),
            True,
            id="an owner whose status is refused",
        ),
        pytest.param(
            World(listed=OverflowError("big")), True, id="a pid psutil cannot take"
        ),
        pytest.param(
            World(listed=False, ctor=psutil.NoSuchProcess(PID), kernel=NOT_OURS),
            True,
            id="an owner only the kernel can see",
        ),
        pytest.param(
            World(listed=False, ctor=psutil.NoSuchProcess(PID)), False, id="a dead one"
        ),
        pytest.param(
            World(status=psutil.STATUS_ZOMBIE, cmdline=ZOMBIE_ARGV),
            False,
            id="a zombie owner",
        ),
        pytest.param(
            World(cmdline=(), exe=GONE), False, id="an owner tearing down (Linux)"
        ),
        pytest.param(
            World(cmdline=(), exe=READABLE), True, id="an owner mid-exec (Linux)"
        ),
    ],
)
def test_a_profile_is_held_by_its_lock_owner_unless_that_owner_is_shown_dead(
    tmp_path, monkeypatch, world, held
):
    """The lock owner's direction, through the caller that spends the answer: an
    owner we cannot ask about holds the profile (two browsers on one profile is
    the cost of the other answer)."""
    install(monkeypatch, world)
    monkeypatch.setattr(profile_lock.socket, "gethostname", lambda: "host")
    # A plain file, not a symlink: ``install`` scripts ``Path.readlink`` for the
    # executable link, and a symlink lock would be read through it.
    (tmp_path / profile_lock.LOCK_NAME).write_text(f"host-{PID}", encoding="utf-8")

    assert (profile_lock.profile_hold(tmp_path, lambda _dir: set()) is not None) is held


@pytest.mark.parametrize(
    ("world", "unreadable"),
    [
        pytest.param(
            World(status=psutil.AccessDenied(PID), cmdline=psutil.AccessDenied(PID)),
            (PID,),
            id="refused its status and its argv: held, not ruled out",
        ),
        pytest.param(
            World(cmdline=psutil.AccessDenied(PID)),
            (PID,),
            id="refused its argv only",
        ),
        pytest.param(
            World(listed=False, ctor=psutil.NoSuchProcess(PID)),
            (),
            id="gone between the scan and the read: an established negative",
        ),
        pytest.param(
            World(status=psutil.STATUS_ZOMBIE, cmdline=psutil.ZombieProcess(PID)),
            (),
            id="a zombie: an established negative",
        ),
    ],
)
def test_a_tree_member_we_could_not_read_is_unreadable_not_absent(
    monkeypatch, world, unreadable
):
    """``browser_members``' two negatives, through the caller that spends them: a
    pid whose argv came back empty is UNREADABLE (the profile stays held) unless
    it is shown to have gone."""
    install(monkeypatch, world)

    members = browser_cmdline.browser_members({PID}, "/profile")

    assert members.browsers == ()
    assert members.unreadable == unreadable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("world", "patient"),
    [
        pytest.param(World(), True, id="a launcher that is running gets the patience"),
        pytest.param(
            World(ctor=psutil.AccessDenied(PID)),
            False,
            id="a launcher that cannot be read gets none",
        ),
        pytest.param(
            World(status=psutil.STATUS_ZOMBIE, cmdline=ZOMBIE_ARGV),
            False,
            id="a launcher that has exited gets none",
        ),
    ],
)
async def test_the_identity_wait_goes_on_only_while_the_launcher_is_shown_running(
    monkeypatch, world, patient
):
    """The devtools_port direction (``unknown=False``), through the caller that
    spends it: patience is for a launcher we can SEE running; an unreadable one is
    not a reason to hold a spawn for ``IDENTITY_PATIENCE_SECONDS``."""
    install(monkeypatch, world)
    monkeypatch.setattr(devtools_port, "IDENTITY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(devtools_port, "IDENTITY_POLL_SECONDS", 0.01)
    monkeypatch.setattr(devtools_port, "IDENTITY_PATIENCE_SECONDS", 0.6)

    class _Silent:
        websocket_url = None

        async def send(self, _command):
            await asyncio.Event().wait()

        async def aclose(self):
            pass

    started = time.monotonic()
    with pytest.raises(ToolError, match="did not say which process it is"):
        await devtools_port.confirm_ours(_Silent(), PID)
    waited = time.monotonic() - started

    assert (waited >= 0.5) is patient, waited


def test_pid_none_is_not_this_process():
    """``psutil.Process(None)`` is the CALLING process, so a missing pid read as
    "alive" in the one reader that built a Process from it directly (CHANGED in
    the devtools_port commit: it is dead now)."""
    assert zombie.alive(None, unknown=False) is False


@pytest.mark.parametrize("pid", [0, None])
def test_a_browser_with_no_pid_is_assumed_up(pid):
    assert process_exit.browser_is_alive(_Browser(pid)) is True


def test_a_browser_whose_recorded_pid_is_not_a_number_is_not_running():
    assert process_exit.browser_is_alive(_Browser("not a pid")) is False


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


# ===========================================================================
# THE ONE HOME (T-19642): ``zombie.alive`` / ``same_process`` / ``create_time``
# ===========================================================================
#
# Same worlds, one function. Each row is (unknown=True, unknown=False): what the
# two directions a caller may ask for answer. Compare with ``ALIVE_TABLE`` above:
# the rows that differ from the readers they replace are argued in the commit
# that migrates the caller each one moves.

NEW_ALIVE = {
    "a live process": (World(), (True, True)),
    "dead: not listed, kernel agrees": (
        World(listed=False, ctor=psutil.NoSuchProcess(PID)),
        (False, False),
    ),
    "not listed by psutil, kernel says it exists": (
        World(listed=False, ctor=psutil.NoSuchProcess(PID), kernel=NOT_OURS),
        (True, True),
    ),
    "a zombie": (
        World(status=psutil.STATUS_ZOMBIE, cmdline=ZOMBIE_ARGV),
        (False, False),
    ),
    "a zombie that raises ZombieProcess when asked its status": (
        World(status=psutil.ZombieProcess(PID)),
        (False, False),
    ),
    "listed, but psutil.Process() is AccessDenied": (
        World(ctor=psutil.AccessDenied(PID)),
        (True, False),
    ),
    "listed, status refused (AccessDenied)": (
        World(status=psutil.AccessDenied(PID)),
        (True, False),
    ),
    # The command-line read can only convict: not reading it is "not shown gone".
    "listed, argv refused (AccessDenied)": (
        World(cmdline=psutil.AccessDenied(PID)),
        (True, True),
    ),
    "pid_exists itself refused (AccessDenied)": (
        World(listed=psutil.AccessDenied(PID)),
        (True, False),
    ),
    "pid_exists raises OverflowError": (
        World(listed=OverflowError("too large")),
        (True, False),
    ),
    "exec window: empty argv, executable readable": (
        World(cmdline=(), exe=READABLE),
        (True, True),
    ),
    "empty argv, executable unreadable for permission": (
        World(cmdline=(), exe=UNREADABLE),
        (True, True),
    ),
    "empty argv, executable read fails another way": (
        World(cmdline=(), exe=OSError(errno.EIO, "io")),
        (True, True),
    ),
    "tearing down: empty argv, executable gone": (
        World(cmdline=(), exe=GONE),
        (False, False),
    ),
    "tearing down: empty argv, ESRCH on the executable": (
        World(cmdline=(), exe=NOT_THERE),
        (False, False),
    ),
    "empty argv, executable gone, but not Linux": (
        World(cmdline=(), exe=GONE, linux=False),
        (True, True),
    ),
    "an argv outranks an unreadable executable": (World(exe=GONE), (True, True)),
}


@pytest.mark.parametrize("name", NEW_ALIVE)
def test_alive_answers_each_world_in_both_directions(monkeypatch, name):
    world, (when_unknown_is_alive, when_unknown_is_dead) = NEW_ALIVE[name]
    install(monkeypatch, world)

    assert zombie.alive(PID, unknown=True) is when_unknown_is_alive, name
    assert zombie.alive(PID, unknown=False) is when_unknown_is_dead, name


@pytest.mark.parametrize("pid", [0, -1, None, True, False, "4242", 4242.0])
def test_a_pid_that_names_no_process_is_dead_and_nothing_is_asked(monkeypatch, pid):
    """``Process(None)`` is this process, ``os.kill(-1, 0)`` answers for everyone
    and ``True`` is pid 1: none of them reaches psutil or the kernel."""
    install(monkeypatch, World(kernel=None))

    for unknown in (True, False):
        assert zombie.alive(pid, unknown=unknown) is False
        assert zombie.same_process(pid, T, unknown=unknown) is False
    assert zombie.create_time(pid) is None
    assert KERNEL_ASKED == []


def test_a_pid_too_large_for_a_c_long_is_unknown_not_a_crash():
    for unknown in (True, False):
        assert zombie.alive(2**40, unknown=unknown) is unknown


def test_this_very_process_is_alive_and_the_same_process_as_itself():
    me = os.getpid()
    started = zombie.create_time(me)
    assert isinstance(started, float)
    assert zombie.alive(me, unknown=True) is True
    assert zombie.alive(me, unknown=False) is True
    assert zombie.same_process(me, started, unknown=False) is True
    assert zombie.same_process(me, started + 5.0, unknown=False) is False
    assert zombie.listed(me) is True


def test_a_reaped_child_is_dead_to_every_question():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=60)
    if zombie.listed(child.pid):
        pytest.skip("the pid was recycled in the instant after the child was reaped")

    assert zombie.alive(child.pid, unknown=True) is False
    assert zombie.alive(child.pid, unknown=False) is False
    assert zombie.listed(child.pid) is False
    assert zombie.create_time(child.pid) is None
    assert zombie.same_process(child.pid, 1.0, unknown=True) is False


IDENTITY = {
    "same pid, same start": (World(created=T), T, 0.0, (True, True)),
    "an exact reading is not 'close enough'": (
        World(created=T + 1e-6),
        T,
        0.0,
        (False, False),
    ),
    "start time 0.5 s apart, tolerance 0.5 (inclusive)": (
        World(created=T + 0.5),
        T,
        0.5,
        (True, True),
    ),
    "start time 0.51 s apart, tolerance 0.5": (
        World(created=T + 0.51),
        T,
        0.5,
        (False, False),
    ),
    "start time 2 s apart (a reused pid), tolerance 1.0": (
        World(created=T + 2.0),
        T,
        1.0,
        (False, False),
    ),
    "no start time on record, the pid is readable": (
        World(created=T),
        None,
        0.0,
        (True, True),
    ),
    "the pid is gone": (World(ctor=psutil.NoSuchProcess(PID)), T, 0.0, (False, False)),
    "the pid is a zombie that cannot be opened": (
        World(ctor=psutil.ZombieProcess(PID)),
        T,
        0.0,
        (False, False),
    ),
    "the pid is AccessDenied": (
        World(ctor=psutil.AccessDenied(PID)),
        T,
        0.0,
        (True, False),
    ),
    "start time read refused (AccessDenied)": (
        World(created=psutil.AccessDenied(PID)),
        T,
        0.0,
        (True, False),
    ),
    "start time read fails with an OSError": (
        World(created=OSError(errno.EIO, "io")),
        T,
        0.0,
        (True, False),
    ),
    "a zombie whose start time is still readable": (
        World(status=psutil.STATUS_ZOMBIE, created=T),
        T,
        0.0,
        (True, True),
    ),
}


@pytest.mark.parametrize("name", IDENTITY)
def test_same_process_answers_each_world_in_both_directions(monkeypatch, name):
    world, recorded, tolerance, (when_true, when_false) = IDENTITY[name]
    install(monkeypatch, world)

    assert (
        zombie.same_process(PID, recorded, tolerance=tolerance, unknown=True)
        is when_true
    )
    assert (
        zombie.same_process(PID, recorded, tolerance=tolerance, unknown=False)
        is when_false
    )


def test_create_time_is_a_value_or_none_never_an_exception(monkeypatch):
    install(monkeypatch, World(created=T))
    assert zombie.create_time(PID) == T
    for refused in (
        psutil.AccessDenied(PID),
        psutil.NoSuchProcess(PID),
        psutil.ZombieProcess(PID),
        psutil.Error("odd"),
        OSError(errno.EIO, "io"),
    ):
        install(monkeypatch, World(created=refused))
        assert zombie.create_time(PID) is None, repr(refused)


@pytest.mark.parametrize(
    ("world", "listed"),
    [
        pytest.param(World(), True, id="live"),
        pytest.param(
            World(status=psutil.STATUS_ZOMBIE, cmdline=ZOMBIE_ARGV),
            True,
            id="a zombie is listed",
        ),
        pytest.param(World(cmdline=(), exe=GONE), True, id="tearing down is listed"),
        pytest.param(World(listed=False), False, id="not in the table"),
    ],
)
def test_listed_is_the_process_table_and_nothing_else(monkeypatch, world, listed):
    install(monkeypatch, world)
    assert zombie.listed(PID) is listed


def test_listed_raises_what_psutil_raises(monkeypatch):
    install(monkeypatch, World(listed=psutil.AccessDenied(PID)))
    with pytest.raises(psutil.AccessDenied):
        zombie.listed(PID)


@pytest.mark.parametrize(
    ("running", "status", "answer"),
    [
        pytest.param(True, psutil.STATUS_RUNNING, True, id="running"),
        pytest.param(False, psutil.STATUS_RUNNING, False, id="exited or recycled"),
        pytest.param(True, psutil.STATUS_ZOMBIE, False, id="a zombie"),
    ],
)
def test_running_is_the_held_process_form(monkeypatch, running, status, answer):
    install(monkeypatch, World(running=running, status=status))
    assert zombie.running(psutil.Process(PID)) is answer
