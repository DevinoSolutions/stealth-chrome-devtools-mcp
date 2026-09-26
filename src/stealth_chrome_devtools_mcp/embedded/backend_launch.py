"""Spawn the backend where no MCP client's Job Object can reach it (F-867).

THE one home for "how the shared HTTP backend process is created". Nothing else
in the tree may start a backend.

**The defect.** The reference MCP Python SDK
(``mcp/os/win32/utilities.py::create_windows_process``) puts every stdio server
it starts into a Windows Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``
and no ``BREAKAWAY_OK``. Our per-session stdio proxy IS that server. When the
proxy wins the cold-start lock and spawns the backend, the backend is created
inside that job — job membership is inherited by children, and
``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` say nothing about jobs. When that
ONE session ends, ``TerminateJobObject`` or merely the last ``CloseHandle`` kills
every member, and the backend serving every other session dies with no
traceback, no uvicorn shutdown and an empty fault log. POSIX is immune: clients
kill a process GROUP, and the backend gets its own with ``start_new_session``.

**The three rungs, and what each one costs.**

1. ``breakaway`` — ask for ``CREATE_BREAKAWAY_FROM_JOB``. Free, and correct for
   any client whose job carries ``BREAKAWAY_OK``. The SDK's job does not, so the
   call comes back ``ERROR_ACCESS_DENIED`` (winerror 5) and we drop a rung; any
   OTHER ``OSError`` is a real spawn failure and propagates. **A successful call
   is not proof of escape**, which is measured, not assumed: in a NESTED job
   chain the flag leaves the innermost job only, so a proxy started through a
   console-script launcher (whose own job permits breakaway) spawns happily and
   stays inside the client's outer job. If the caller was in no job to begin
   with, the flag is a no-op and the check passes trivially — which is exactly
   why trying it first is free.

   A PARTIAL escape (spawned, still in some job) is never discarded blindly:
   rung 2's viability is decided FIRST, because a backend that got out of one
   job is strictly better than the plain rung's, which got out of none. So:
   rung 2 unavailable → keep the child and return the ``breakaway-partial``
   rung with a WARNING; rung 2 available → discard the child (killed, then
   waited for, bounded) and use the scheduler. And whenever breakaway was
   proven PERMITTED, rung 3 keeps asking for it too — a partial escape is the
   worst case there, not a reason to spawn with fewer flags than we know work.
2. ``scheduler`` — hand the creation to Task Scheduler, the job-free
   intermediary the product already owns (F-810). A one-shot "run only when the
   user is logged on" task runs under the Task Scheduler service: outside every
   caller job and outside the caller's process tree. Four costs, paid
   deliberately:

   * *Session truthfulness.* The OS places such a task in the logged-on console
     session. We take this rung ONLY when the spawner is already in that session
     (``_same_session_as_console``), so the backend lands where it would have
     landed anyway and the display context ``singleton`` records stays true.
     F-808's ruling holds: the tool still never PICKS a session.
   * *No window, ever.* The intermediary is ``pythonw.exe`` — no console, so
     nothing can flash. It must be the pythonw beside the interpreter in ``cmd``,
     which F-866 already resolved to the BASE interpreter; a venv's
     ``Scripts\\pythonw.exe`` is the redirector, and running the launcher under
     it would rebuild F-866's kill-on-close job around the backend. When only a
     redirector is available we drop a rung rather than reintroduce that.
   * *Env, command and cwd hand-off.* A scheduled task inherits nothing, and it
     starts in system32. The ENTIRE child env dict — exactly as the caller built
     it, nothing hand-picked — plus the argv, the boot-log path and the
     spawner's working directory (``clone_storage``'s last-resort clone seed
     reads it) travel in a JSON spec beside the launcher, in the user-private
     state dir, deleted in a ``finally``. ``/TR`` is stored truncated at 253
     characters — measured, and single-homed with the ``schtasks`` seam itself
     as ``desktop_launch.TR_MAX_CHARS`` / ``tr_overflow`` (F-879) — so it
     carries only two paths (F-810's precedent), the token is short
     (``desktop_launch.TOKEN_CHARS``), and the launcher addresses its spec, pid
     and error files off its own path. A spawner killed mid-launch
     leaves its task AND its spec behind, so the next scheduler spawn deletes
     any task whose spec is older than the pid deadline.
   * *The boot log and the pid.* A ``Popen`` stdout handle cannot cross the
     scheduler, so the launcher re-opens the rolled boot log itself and hands the
     backend's stdout AND stderr to it — F-303's property (an import-time crash
     lands in ``backend-boot.log``) is preserved. The launcher then writes the
     backend's real pid through a temp file and ``os.replace``, so the poller can
     never read half a number, and so the pid ``singleton`` records is the
     interpreter that serves (F-866), never the launcher's.
3. ``plain`` — today's detached spawn (plus the breakaway flag when rung 1
   proved it permitted). A runner with no scheduler, no console session, or a
   refusing service account still starts a backend; the WARNING that precedes it
   names F-867, so the log says which rung served.

A leaf: it imports ``backend_registry`` for the state dir and reaches
``desktop_launch`` lazily for the ONE ``schtasks`` seam, the ONE pid-file reader,
the ONE task-teardown and the ONE ``/TR`` length cap, so none of them gets a
second home. That reach used
to cost the proxy a whole second of nodriver import for a browser it never
launches — the stdio branch imports no ``browser_manager`` — so
``desktop_launch`` now imports nodriver and requests inside the two delegation
functions that use them, and reaching its seams costs ~175 ms warm (measured
with ``-X importtime``; it was ~470 ms). It never raises for a rung's own
failure — only a genuine ``Popen`` error reaches the caller.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import IO, NamedTuple

from stealth_chrome_devtools_mcp.embedded import backend_registry

# The proxy's logger, not one of our own: ``configure_logging("proxy")`` installs
# the file handler on that exact name with ``propagate = False``, so a
# ``stealth.backend_launch`` record would reach no handler at all and the rung
# would be invisible in the one log a post-mortem reads.
_logger = logging.getLogger("stealth.proxy")

# Subdirectory of the state dir holding one launcher + spec + pid file per
# in-flight scheduler spawn. Emptied in a finally, so it must never accumulate.
LAUNCH_DIR_NAME = "backend-launch"
TASK_PREFIX = "stealth-mcp-backend-"
# How long the scheduler has to run the task and the launcher has to hand back
# the backend's pid. Generous: task creation, service dispatch and a cold
# interpreter start all fit inside it.
PID_READY_TIMEOUT = 20.0
POLL_INTERVAL = 0.1
# A spec older than this cannot belong to a spawn still waiting for its pid, so
# whatever task it names was orphaned. Twice the deadline, because the age is
# read against a wall clock and the spawn it belongs to may have started late.
STALE_SPEC_SECONDS = 2 * PID_READY_TIMEOUT
# The /TR length cap and the per-attempt token length are NOT here: they are
# facts about the ``schtasks`` seam, whose one home is ``desktop_launch``
# (``TR_MAX_CHARS``, ``TOKEN_CHARS``, ``tr_overflow``), and this module asks
# there at call time exactly as it asks there for ``_schtasks`` itself (F-879).
# A second 253 in this file is a number that can drift from the measured one.
# How long to wait for a discarded partial-breakaway child to actually die.
# Bounded: a spawn must not hang on a teardown that is only tidiness.
DISCARD_WAIT_SECONDS = 5.0
# The one spelling of the line that says which rung served. A post-mortem greps
# it and the F-867 pin parses it, so it is a constant rather than three
# literals that could drift apart.
SPAWN_LOG_PREFIX = "backend spawned via the "

# Spelled out rather than read off ``subprocess``: typeshed exposes
# DETACHED_PROCESS / CREATE_NEW_PROCESS_GROUP only under ``sys.platform ==
# "win32"`` and the type gate also runs on Linux, while
# CREATE_BREAKAWAY_FROM_JOB has no ``subprocess`` name at all. These are Win32
# API values and cannot change.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_DETACHED_FLAGS = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
_ACCESS_DENIED = 5
# One launcher's worth of scratch, named off its token. ``.tmp`` is the pid's
# pre-rename form; it exists only for the instant ``os.replace`` needs.
_SCRATCH_SUFFIXES = (".py", ".json", ".pid", ".err", ".tmp")

# stdlib only: the intermediary is a bare pythonw and must not import the
# package it is about to start. Everything it needs is in the spec beside it.
_LAUNCHER_SCRIPT = '''\
"""F-867 intermediary: create the backend from under Task Scheduler."""

import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

base = Path(__file__).with_suffix("")
try:
    spec = json.loads(base.with_suffix(".json").read_text(encoding="utf-8"))
    boot_log = spec["boot_log"]
    # Append, binary: this is a raw redirect of whatever the backend writes, and
    # F-303 wants an import-time traceback in it even though nothing reads it.
    handle = open(boot_log, "ab") if boot_log else None
    try:
        proc = subprocess.Popen(
            spec["cmd"],
            stdout=handle if handle is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=spec["env"],
            # A scheduled task starts in system32. The backend reads the working
            # directory (clone_storage's last-resort clone seed), so it has to be
            # the spawner's, exactly as it is on every other rung. None means the
            # spawner could not read its own, which is what Popen does anyway.
            cwd=spec["cwd"],
            close_fds=True,
            # F-932: a scheduled task runs at BelowNormal and its children
            # inherit that, so ask for Normal explicitly.
            creationflags=(
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.NORMAL_PRIORITY_CLASS
            ),
        )
    finally:
        if handle is not None:
            handle.close()
    # Atomic: the poller must never read a half-written number.
    pending = base.with_suffix(".tmp")
    pending.write_text(str(proc.pid), encoding="utf-8")
    os.replace(pending, base.with_suffix(".pid"))
except BaseException:
    base.with_suffix(".err").write_text(traceback.format_exc(), encoding="utf-8")
    raise
'''


class Launched(NamedTuple):
    """The backend that was created, and the rung that created it."""

    pid: int
    rung: str


class _SchedulerPlan(NamedTuple):
    """Everything rung 2 needs, decided BEFORE anything is spawned or discarded.

    Viability is a question about this machine — the session, the interpreter,
    the command-line cap — and none of it depends on the attempt, so answering
    it first is what lets rung 1 keep a partial escape when rung 2 could not
    have done better anyway.
    """

    interpreter: Path
    token: str
    command: str


def spawn(cmd: list[str], env: dict[str, str], boot_log: Path | None) -> Launched:
    """Create the backend process; return its pid and the rung that served.

    *boot_log* is the already-rolled ``backend-boot.log`` path — rotation belongs
    to the caller, because once a child inherits the descriptor it pins the file
    for life — or ``None`` when no log dir was writable, in which case output
    goes to ``DEVNULL`` exactly as it did before M3.
    """
    if sys.platform != "win32":
        posix = _popen(cmd, env, boot_log, start_new_session=True)
        return Launched(posix.pid, "posix")
    return _spawn_windows(cmd, env, boot_log)


def _spawn_windows(
    cmd: list[str], env: dict[str, str], boot_log: Path | None
) -> Launched:
    """The three rungs, in order. Only rung 1 can raise, and only for a spawn
    failure that is NOT the client's job refusing to let go."""
    breakaway_flags = _DETACHED_FLAGS | _CREATE_BREAKAWAY_FROM_JOB
    permitted, partial = True, None
    try:
        first = _popen(cmd, env, boot_log, creationflags=breakaway_flags)
    except OSError as error:
        if getattr(error, "winerror", None) != _ACCESS_DENIED:
            raise
        permitted = False
        _logger.info(
            "F-867: this client's job refuses CREATE_BREAKAWAY_FROM_JOB; "
            "spawning the backend through Task Scheduler instead"
        )
    else:
        if not _proven_in_a_job(first):
            _log_rung("breakaway", first.pid)
            return Launched(first.pid, "breakaway")
        # A nested job chain: the flag freed the innermost job (a console-script
        # launcher's, say) and something still holds this backend.
        partial = first

    plan = _scheduler_plan(cmd[0])
    if partial is not None:
        if plan is None:
            # Keeping it beats every remaining option: it is out of one job, and
            # anything we spawn now would be out of none.
            _log_rung(
                "breakaway-partial",
                partial.pid,
                ": it escaped the innermost job only, an enclosing "
                "kill-on-close job would still take it, and no scheduler rung is "
                "available here to do better (F-867)",
                level=logging.WARNING,
            )
            return Launched(partial.pid, "breakaway-partial")
        _logger.info(
            "F-867: CREATE_BREAKAWAY_FROM_JOB left the backend (pid %s) inside a "
            "job; discarding it and spawning through Task Scheduler",
            partial.pid,
        )
        _discard(partial)

    if plan is not None:
        pid = _scheduler_spawn(plan, cmd, env, boot_log)
        if pid is not None:
            _log_rung("scheduler", pid)
            return Launched(pid, "scheduler")

    # Whatever rung 1 proved permitted is still permitted here: asking again
    # cannot be worse than not asking, and on a client whose job allows it this
    # is the difference between escaping one job and escaping none.
    plain_flags = _DETACHED_FLAGS | (_CREATE_BREAKAWAY_FROM_JOB if permitted else 0)
    plain = _popen(cmd, env, boot_log, creationflags=plain_flags)
    _log_rung(
        "plain",
        plain.pid,
        ": it is inside this client's job and dies when this session ends (F-867)",
    )
    return Launched(plain.pid, "plain")


def _log_rung(
    rung: str, pid: int, detail: str = "", *, level: int = logging.INFO
) -> None:
    """The one line that says which rung served, in the one spelling."""
    _logger.log(level, "%s%s rung (pid %s)%s", SPAWN_LOG_PREFIX, rung, pid, detail)


def _discard(proc: subprocess.Popen[bytes]) -> None:
    """Kill a spawn we are not going to use, and wait (bounded) for it to go.

    Waiting matters: the next rung is about to start a backend on the same port,
    and a discarded one that is still exiting would race it. Never raises — the
    caller is mid-spawn and a teardown must not become the failure.
    """
    with suppress(OSError):
        proc.kill()
    with suppress(OSError, subprocess.SubprocessError):
        proc.wait(timeout=DISCARD_WAIT_SECONDS)


def _popen(
    cmd: list[str],
    env: dict[str, str],
    boot_log: Path | None,
    *,
    creationflags: int = 0,
    start_new_session: bool = False,
) -> subprocess.Popen[bytes]:
    """One ``Popen``, with the boot log opened and closed around it.

    The handle is closed as soon as the child has inherited it: a launcher that
    kept it would pin the file exactly the way the child does. ``Popen`` is
    reached as an attribute at call time, so a test that patches it is seen.
    """
    handle = _open_boot_log(boot_log)
    devnull = subprocess.DEVNULL  # noqa: TID251  PERMANENT(pre-M3 fail-open)
    target = devnull if handle is None else handle
    try:
        return subprocess.Popen(  # noqa: S603  PERMANENT(our own argv, no shell)
            cmd,
            stdout=target,
            stderr=target,
            stdin=devnull,
            env=env,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
    finally:
        if handle is not None:
            handle.close()


def _open_boot_log(boot_log: Path | None) -> IO[bytes] | None:
    """The append handle for *boot_log*, or ``None`` to fall back to DEVNULL.

    Fail-open (plan_M3 §7): a log file that cannot be opened must never stop a
    backend from starting.
    """
    if boot_log is None:
        return None
    try:
        return boot_log.open("ab")
    except OSError:
        _logger.warning(
            "backend-boot.log could not be opened; falling back to DEVNULL",
            exc_info=True,
        )
        return None


def _launch_dir() -> Path:
    """The scratch dir for launcher scripts. Read through ``backend_registry`` at
    call time so a test can redirect ``STATE_DIR`` to tmp_path."""
    return backend_registry.STATE_DIR / LAUNCH_DIR_NAME


def _proven_in_a_job(proc: subprocess.Popen[bytes]) -> bool:
    """True only when the new process is PROVEN to be inside some Job Object.

    ``IsProcessInJob`` with a NULL job is the only membership question askable
    without a handle to the job itself, and it is the right one here: what makes
    the backend safe is being in no job at all, whoever owns them. It is asked
    through the ``Popen``'s OWN handle rather than by reopening the pid — a pid
    is not an identity, and Windows reissues numbers freely.

    An unanswerable query is NOT proof, so it leaves the spawn standing: this
    check exists to catch a partial breakaway, not to invent a reason to discard
    a backend that was very likely fine.
    """
    handle = getattr(proc, "_handle", None)
    if not isinstance(handle, int):
        return False
    import ctypes
    from ctypes import wintypes

    try:
        member = wintypes.BOOL()
        answered = ctypes.windll.kernel32.IsProcessInJob(
            wintypes.HANDLE(handle), None, ctypes.byref(member)
        )
    except Exception:  # noqa: BLE001  PERMANENT(a probe may never raise)
        _logger.warning(
            "F-867: could not ask whether the new backend is in a Job Object, so "
            "the breakaway spawn stands unverified; if this client's job is a "
            "kill-on-close one, the backend will die with this session",
            exc_info=True,
        )
        return False
    if not answered:
        _logger.warning(
            "F-867: IsProcessInJob refused for the new backend, so the breakaway "
            "spawn stands unverified; if this client's job is a kill-on-close "
            "one, the backend will die with this session"
        )
        return False
    return bool(member.value)


def _own_session_id() -> int | None:
    """This process's Windows session id, or ``None`` if the probe refuses."""
    import ctypes
    from ctypes import wintypes

    try:
        kernel = ctypes.windll.kernel32
        session = wintypes.DWORD()
        if not kernel.ProcessIdToSessionId(
            kernel.GetCurrentProcessId(), ctypes.byref(session)
        ):
            return None
    except Exception:  # noqa: BLE001  PERMANENT(a probe may never raise)
        # DEBUG, not WARNING: the ONE caller turns a missing session id into its
        # own WARNING naming both ids and the rung it drops to, so raising the
        # level here would report one fact twice.
        _logger.debug(
            "F-867: the session-id probe refused; the scheduler rung's session "
            "gate will read this process as being in no known session",
            exc_info=True,
        )
        return None
    return int(session.value)


def _same_session_as_console() -> bool:
    """True when a "run when logged on" task would land in OUR OWN session.

    A gate, not a preference: Task Scheduler places such a task in the console
    session, so taking this rung from anywhere else would silently move the
    backend to another desktop and make the display context ``singleton`` records
    a lie. Equality means the OS puts the process exactly where it already was.
    """
    from stealth_chrome_devtools_mcp.embedded import desktop_launch

    own = _own_session_id()
    console = desktop_launch._active_console_session_id()
    if own is None or console is None or console in desktop_launch._NO_CONSOLE_SESSION:
        _logger.warning(
            "F-867: no logged-on console session to spawn the backend into (this "
            "session %s, console session %s); using the plain spawn, which this "
            "client's job can kill",
            own,
            console,
        )
        return False
    if own != console:
        _logger.warning(
            "F-867: this process is in session %s but the console is session %s; "
            "a scheduled task would land the backend on another desktop, so the "
            "plain spawn is used instead",
            own,
            console,
        )
        return False
    return True


def _intermediary_interpreter(interpreter: str) -> Path | None:
    """The console-less interpreter that will run the launcher, or ``None``.

    ``pythonw.exe`` beside the backend's own interpreter: no console means no
    window can flash under the scheduler. It must NOT be a venv's
    ``Scripts\\pythonw.exe`` — that is CPython's redirector, which re-spawns a
    child inside a ``KILL_ON_JOB_CLOSE`` job of its own and would put the backend
    straight back into one (F-866). A venv is recognised by the ``pyvenv.cfg``
    one level above ``Scripts``.
    """
    candidate = Path(interpreter).with_name("pythonw.exe")
    if not candidate.exists():
        return None
    if (candidate.parent.parent / "pyvenv.cfg").exists():
        return None
    return candidate


def _spawner_cwd() -> str | None:
    """The directory the backend should start in, or ``None`` if it cannot be
    read.

    Only the scheduler rung asks: every other rung inherits the spawner's
    working directory for free, and a scheduled task would otherwise start in
    system32 — which ``clone_storage``'s last-resort clone seed would then read.
    Asked HERE and not in ``spawn`` so a deleted working directory cannot fail a
    POSIX or plain spawn that never needed to know it.
    """
    try:
        return str(Path.cwd())
    except OSError:
        _logger.debug(
            "F-867: the working directory could not be read; the scheduled "
            "backend will start wherever the task runs"
        )
        return None


def _spec(
    cmd: list[str], env: dict[str, str], boot_log: Path | None, cwd: str | None
) -> str:
    """The launcher's whole input: argv, the ENTIRE child env, the boot log and
    the working directory a scheduled task would otherwise not have."""
    return json.dumps(
        {
            "cmd": cmd,
            "env": env,
            "boot_log": None if boot_log is None else str(boot_log),
            "cwd": cwd,
        }
    )


def _scheduler_plan(interpreter_of: str) -> _SchedulerPlan | None:
    """Can this machine run the scheduler rung at all, and with what command?

    Answered before anything is spawned or discarded (see ``_spawn_windows``).
    ``None`` means the rung does not exist here; each reason logs a WARNING
    naming F-867, because it is the reason a backend ends up killable.
    """
    from stealth_chrome_devtools_mcp.embedded import desktop_launch

    if not _same_session_as_console():
        return None
    interpreter = _intermediary_interpreter(interpreter_of)
    if interpreter is None:
        _logger.warning(
            "F-867: no job-free pythonw.exe beside %s (a venv redirector does not "
            "count — it would rebuild F-866's job); using the plain spawn",
            interpreter_of,
        )
        return None
    # A short token, not a full uuid hex: it appears in the task name, the five
    # scratch file names and — the one that matters — the /TR command, which has
    # only ``TR_MAX_CHARS`` to fit two absolute paths into. Both numbers are
    # ``desktop_launch``'s, so this rung and the headed hand-off cannot disagree
    # about what schtasks stores (F-879).
    token = uuid.uuid4().hex[: desktop_launch.TOKEN_CHARS]
    command = f'"{interpreter}" "{_launch_dir() / token}.py"'
    over = desktop_launch.tr_overflow(command)
    if over is not None:
        # A rung boundary here, where ``desktop_launch`` raises: that path has no
        # rung to fall to, this one does, and a killable backend beats none.
        _logger.warning(
            "F-867: the scheduled-task command is %s characters, past the %s "
            "schtasks truncates at; using the plain spawn",
            over,
            desktop_launch.TR_MAX_CHARS,
        )
        return None
    return _SchedulerPlan(interpreter, token, command)


def _sweep_orphan_tasks(launch_dir: Path) -> None:
    """Delete backend-launch tasks left behind by a spawner that was killed.

    A one-shot task created between ``/Create`` and ``/Delete`` outlives the
    process that made it, and ``/SC ONCE /ST 00:00`` means it can fire later
    against a stale spec. That spawner leaves its spec behind TOO — the teardown
    deletes the task before the files — so the orphan is recognised by AGE, not
    by absence: a spec older than the pid deadline cannot belong to a spawn that
    is still waiting, while a live sibling's is seconds old and is never touched.

    Reading the directory instead of asking the scheduler is also what keeps
    this free. The normal case is an empty dir and ZERO ``schtasks`` calls, where
    a ``/Query`` cost 0.85-0.89 s on every cold start. Best effort throughout —
    hygiene in front of a spawn may never raise and never delay one.
    """
    from stealth_chrome_devtools_mcp.embedded import desktop_launch

    try:
        specs = sorted(launch_dir.glob("*.json"))
    except OSError:
        _logger.debug("F-867: could not read the launch dir to sweep", exc_info=True)
        return
    now = time.time()
    for spec in specs:
        try:
            age = now - spec.stat().st_mtime
        except OSError:
            continue
        if age < STALE_SPEC_SECONDS:
            continue  # a sibling spawn is still waiting on this one
        token = spec.stem
        _logger.debug(
            "F-867: reaping the orphaned backend-launch task for %s (its spec is "
            "%.0fs old, past the %.0fs pid deadline)",
            token,
            age,
            STALE_SPEC_SECONDS,
        )
        base = launch_dir / token
        desktop_launch._cleanup(
            f"{TASK_PREFIX}{token}",
            *(base.with_suffix(suffix) for suffix in _SCRATCH_SUFFIXES),
        )


def _scheduler_spawn(
    plan: _SchedulerPlan,
    cmd: list[str],
    env: dict[str, str],
    boot_log: Path | None,
) -> int | None:
    """Create the backend through a one-shot scheduled task, or ``None`` to fall
    through to the plain spawn.

    Never raises: every failure here is a rung, not an error, and each one logs a
    WARNING naming F-867.
    """
    task_name = f"{TASK_PREFIX}{plan.token}"
    launch_dir = _launch_dir()
    base = launch_dir / plan.token
    scratch = [base.with_suffix(suffix) for suffix in _SCRATCH_SUFFIXES]

    try:
        launch_dir.mkdir(parents=True, exist_ok=True)
        _sweep_orphan_tasks(launch_dir)
        base.with_suffix(".py").write_text(_LAUNCHER_SCRIPT, encoding="utf-8")
        base.with_suffix(".json").write_text(
            _spec(cmd, env, boot_log, _spawner_cwd()), encoding="utf-8"
        )
        return _run_task(
            task_name, plan.command, base.with_suffix(".pid"), base.with_suffix(".err")
        )
    except (OSError, subprocess.SubprocessError):
        _logger.warning(
            "F-867: the scheduled-task spawn failed; using the plain spawn",
            exc_info=True,
        )
        return None
    finally:
        from stealth_chrome_devtools_mcp.embedded import desktop_launch

        desktop_launch._cleanup(task_name, *scratch)


def _run_task(
    task_name: str, command: str, pid_file: Path, error_file: Path
) -> int | None:
    """Create + run the task, then wait for the launcher's pid hand-back."""
    from stealth_chrome_devtools_mcp.embedded import desktop_launch

    # No /RU or /RP: a task that runs only when the current user is logged on
    # needs no stored credentials and no admin rights (F-810's precedent).
    created = desktop_launch._schtasks(
        [
            "/Create",
            "/F",
            "/TN",
            task_name,
            "/SC",
            "ONCE",
            "/ST",
            "00:00",
            "/TR",
            command,
        ]
    )
    if created.returncode != 0:
        _logger.warning(
            "F-867: could not create the backend-launch task %s (schtasks exit "
            "%s: %s); using the plain spawn",
            task_name,
            created.returncode,
            created.stderr.strip(),
        )
        return None
    started = desktop_launch._schtasks(["/Run", "/TN", task_name])
    if started.returncode != 0:
        _logger.warning(
            "F-867: could not run the backend-launch task %s (schtasks exit %s: "
            "%s); using the plain spawn",
            task_name,
            started.returncode,
            started.stderr.strip(),
        )
        return None

    deadline = time.monotonic() + PID_READY_TIMEOUT
    while time.monotonic() < deadline:
        pid = desktop_launch._read_pid(pid_file)
        if pid is not None:
            return pid
        if error_file.exists():
            _logger.warning(
                "F-867: the backend-launch intermediary failed; using the plain "
                "spawn. Its traceback:\n%s",
                error_file.read_text(encoding="utf-8", errors="replace").strip(),
            )
            return None
        time.sleep(POLL_INTERVAL)
    _logger.warning(
        "F-867: the backend-launch task %s never handed back a pid within %.0fs; "
        "using the plain spawn",
        task_name,
        PID_READY_TIMEOUT,
    )
    return None
