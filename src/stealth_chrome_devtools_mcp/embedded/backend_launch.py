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
   stays inside the client's outer job. So the rung is accepted only when the
   new process is in NO job at all; otherwise the backend is discarded, at an
   age where it has done nothing yet, and the next rung runs. If the caller was
   in no job to begin with, the flag is a no-op and the check passes trivially —
   which is exactly why trying it first is free.
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
   * *Env and command hand-off.* A scheduled task inherits nothing. The ENTIRE
     child env dict — exactly as the caller built it, nothing hand-picked — plus
     the argv and the boot-log path travel in a JSON spec beside the launcher, in
     the user-private state dir, deleted in a ``finally``. ``/TR`` truncates near
     261 characters, so it carries only two paths (F-810's precedent) and the
     launcher addresses its spec, pid and error files off its own path.
   * *The boot log and the pid.* A ``Popen`` stdout handle cannot cross the
     scheduler, so the launcher re-opens the rolled boot log itself and hands the
     backend's stdout AND stderr to it — F-303's property (an import-time crash
     lands in ``backend-boot.log``) is preserved. The launcher then writes the
     backend's real pid through a temp file and ``os.replace``, so the poller can
     never read half a number, and so the pid ``singleton`` records is the
     interpreter that serves (F-866), never the launcher's.
3. ``plain`` — today's detached spawn, unchanged. A runner with no scheduler, no
   console session, or a refusing service account still starts a backend; the
   WARNING that precedes it names F-867, so the log says which rung served.

A leaf: it imports ``backend_registry`` for the state dir and reaches
``desktop_launch`` lazily for the ONE ``schtasks`` seam and the ONE pid-file
reader, so neither gets a second home. It never raises for a rung's own failure —
only a genuine ``Popen`` error reaches the caller.
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
# schtasks truncates /TR around this many characters.
TR_MAX_CHARS = 261

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
            close_fds=True,
            creationflags=(
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
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
    flags = _DETACHED_FLAGS | _CREATE_BREAKAWAY_FROM_JOB
    try:
        breakaway = _popen(cmd, env, boot_log, creationflags=flags)
    except OSError as error:
        if getattr(error, "winerror", None) != _ACCESS_DENIED:
            raise
        _logger.info(
            "F-867: this client's job refuses CREATE_BREAKAWAY_FROM_JOB; "
            "spawning the backend through Task Scheduler instead"
        )
    else:
        if not _proven_in_a_job(breakaway):
            _logger.info(
                "backend spawned via the breakaway rung (pid %s)", breakaway.pid
            )
            return Launched(breakaway.pid, "breakaway")
        # A nested job chain: the flag freed the innermost job (a console-script
        # launcher's, say) and the client's still holds this backend. Discard it
        # — it is microseconds old and has bound nothing — and drop a rung.
        _logger.info(
            "F-867: CREATE_BREAKAWAY_FROM_JOB left the backend (pid %s) inside a "
            "job; discarding it and spawning through Task Scheduler",
            breakaway.pid,
        )
        with suppress(OSError):
            breakaway.kill()

    pid = _scheduler_spawn(cmd, env, boot_log)
    if pid is not None:
        _logger.info("backend spawned via the scheduler rung (pid %s)", pid)
        return Launched(pid, "scheduler")

    plain = _popen(cmd, env, boot_log, creationflags=_DETACHED_FLAGS)
    _logger.info(
        "backend spawned via the plain rung (pid %s): it is inside this client's "
        "job and dies when this session ends (F-867)",
        plain.pid,
    )
    return Launched(plain.pid, "plain")


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
        if not ctypes.windll.kernel32.IsProcessInJob(
            wintypes.HANDLE(handle), None, ctypes.byref(member)
        ):
            return False
    except Exception:  # noqa: BLE001  PERMANENT(a probe may never raise)
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


def _spec(cmd: list[str], env: dict[str, str], boot_log: Path | None) -> str:
    """The launcher's whole input: argv, the ENTIRE child env, the boot log."""
    return json.dumps(
        {
            "cmd": cmd,
            "env": env,
            "boot_log": None if boot_log is None else str(boot_log),
        }
    )


def _scheduler_spawn(
    cmd: list[str], env: dict[str, str], boot_log: Path | None
) -> int | None:
    """Create the backend through a one-shot scheduled task, or ``None`` to fall
    through to the plain spawn.

    Never raises: every failure here is a rung, not an error, and each one logs a
    WARNING naming F-867. The scratch files go first in the cleanup, so a task
    that has not read its spec yet fails instead of starting a second backend.
    """
    if not _same_session_as_console():
        return None
    interpreter = _intermediary_interpreter(cmd[0])
    if interpreter is None:
        _logger.warning(
            "F-867: no job-free pythonw.exe beside %s (a venv redirector does not "
            "count — it would rebuild F-866's job); using the plain spawn",
            cmd[0],
        )
        return None

    token = uuid.uuid4().hex
    task_name = f"{TASK_PREFIX}{token}"
    launch_dir = _launch_dir()
    base = launch_dir / token
    script = base.with_suffix(".py")
    suffixes = (".py", ".json", ".pid", ".err", ".tmp")
    scratch = [base.with_suffix(suffix) for suffix in suffixes]
    command = f'"{interpreter}" "{script}"'
    if len(command) > TR_MAX_CHARS:
        _logger.warning(
            "F-867: the scheduled-task command is %s characters, past the %s "
            "schtasks truncates at; using the plain spawn",
            len(command),
            TR_MAX_CHARS,
        )
        return None

    try:
        launch_dir.mkdir(parents=True, exist_ok=True)
        script.write_text(_LAUNCHER_SCRIPT, encoding="utf-8")
        spec_file = base.with_suffix(".json")
        spec_file.write_text(_spec(cmd, env, boot_log), encoding="utf-8")
        return _run_task(
            task_name, command, base.with_suffix(".pid"), base.with_suffix(".err")
        )
    except (OSError, subprocess.SubprocessError):
        _logger.warning(
            "F-867: the scheduled-task spawn failed; using the plain spawn",
            exc_info=True,
        )
        return None
    finally:
        _cleanup(task_name, scratch)


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


def _cleanup(task_name: str, scratch: list[Path]) -> None:
    """Delete the task and every scratch file. Never raises — it runs in a
    ``finally`` whose caller may already be handling the real failure."""
    from stealth_chrome_devtools_mcp.embedded import desktop_launch

    for path in scratch:
        with suppress(OSError):
            path.unlink(missing_ok=True)
    with suppress(OSError, subprocess.SubprocessError):
        desktop_launch._schtasks(["/Delete", "/F", "/TN", task_name])
