"""Hand a browser launch to the user's desktop (Windows Task Scheduler).

THE one home for "this backend cannot show a window itself, but the OS can put
the process where a window IS visible". Nothing else in the tree may create a
scheduled task or attach to a browser it did not spawn.

**The F-808 amendment (F-810, human ruling 2026-08-02).** F-808 established that
Chrome inherits its parent's window station, so a headed spawn from a service /
SSH / session-0 backend is a ghost, and ruled that the tool must never *pick* a
session — the machine that finding was made on had an active console session
(2) that was NOT the session holding the user's desktop (1), so every "find the
interactive session" heuristic is wrong somewhere. F-810 amends that ruling in
mechanism, not in spirit: we still never pick a session and
``display_context.py`` stays purely observational. We delegate *process
creation* to Task Scheduler with a "run only when the user is logged on" task,
and **Windows itself** places the process in the logged-on user's interactive
session. The window is then visible by construction rather than by our guess.
The same backend attaches over CDP, so there is exactly ONE backend, the
instance lives where every other instance lives, and all 94 tools work unchanged.

When delegation is impossible (non-win32, nobody logged on at the console) or
fails, we raise: ``server.spawn_browser``'s F-808 refusal is the fallback, which
is exactly the situation a loud error is correct for.

Two mechanism facts this module is built on, both verified against the installed
nodriver: ``uc.Config(host=..., port=...)`` makes ``Browser.start`` take the
``connect_existing`` path (no subprocess, ``_process``/``_process_pid`` stay
``None``), and on that path ``browser_args``/``user_data_dir`` in the config are
IGNORED — which is why they must ride on the launcher command line instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import psutil

from stealth_chrome_devtools_mcp.embedded import (
    backend_registry,
    display_context,
    proxy_forwarder,
)
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

if TYPE_CHECKING:
    from nodriver import Browser

# ``nodriver`` (~200 ms) and ``requests`` (~78 ms) are imported inside the two
# functions that use them, not here. Both are on the DELEGATION path, while this
# module's ``_schtasks`` / ``_read_pid`` / ``_cleanup`` seams and its ``/TR``
# budget (``TR_MAX_CHARS`` / ``TOKEN_CHARS`` / ``tr_overflow``, F-879) are also
# the one home ``backend_launch`` reaches for on every backend cold start — and
# the stdio proxy that does that never touches nodriver otherwise. Paying a
# second of import for a browser it will not launch is a cost the proxy's
# startup cannot justify.

# Subdirectory of the state dir holding one launcher script + pid file per
# in-flight delegation. Emptied in a finally, so it must never accumulate.
LAUNCH_DIR_NAME = "desktop-launch"
TASK_PREFIX = "stealth-mcp-launch-"
# How long Chrome has to come up on the user's desktop and open its DevTools
# port. Generous: a cold profile on a busy desktop is slower than a warm one.
PORT_READY_TIMEOUT = 20.0
POLL_INTERVAL = 0.25
SCHTASKS_TIMEOUT = 15
DEVTOOLS_PROBE_TIMEOUT = 2
# The longest ``/TR`` schtasks STORES, and the one home for that number: every
# caller of ``_schtasks`` in the tree composes a ``/TR`` against it (F-879).
# Measured, not documented — this module used to repeat the documented "~261",
# which is wrong by eight characters. On Windows 11 10.0.26200 (2026-09-14) a
# 255-character command came back from ``/Create`` with exit 0 and was stored as
# its first 253 characters, so the task ran against a truncated path, failed with
# Last Result 2 (ERROR_FILE_NOT_FOUND) and logged nowhere. There is nothing in
# the return value to branch on, so the length has to be checked BEFORE the call.
TR_MAX_CHARS = 253
# Length of a per-attempt token, shared for the same reason the cap is: it names
# the task and the scratch files, and the scratch path is most of what ``/TR``
# has to fit. 48 bits per attempt is ample for a name that lives for one launch,
# and 12 characters instead of 32 is 20 more characters of headroom (F-867).
TOKEN_CHARS = 12
# How far apart two readings of one process's start time may be and still be
# the same process. psutil reports it deterministically, so this only absorbs
# float representation — it is NOT slack for "probably the same pid".
PID_IDENTITY_TOLERANCE = 0.5
_HTTP_OK = 200
# WTSGetActiveConsoleSessionId: 0 is the isolated services session (never
# composited onto a screen since Vista), 0xFFFFFFFF means no session is attached.
_NO_CONSOLE_SESSION = (0, 0xFFFFFFFF)


def _active_console_session_id() -> int | None:
    """The Windows session id currently attached to the physical console, or
    ``None`` if the probe is unavailable or refuses.

    Seam: every test fakes this. Note this is NOT session *selection* — we read
    one OS fact to decide whether delegation is on offer at all, and the OS, not
    us, decides where the delegated process lands.
    """
    import ctypes

    try:
        return int(ctypes.windll.kernel32.WTSGetActiveConsoleSessionId())
    except Exception:  # noqa: BLE001  PERMANENT(probe must never raise)
        debug_logger.log_warning(
            "desktop_launch",
            "_active_console_session_id",
            "Console-session probe raised; treating delegation as unavailable",
        )
        return None


def available() -> bool:
    """True when this process can hand a launch to a logged-on user's desktop.

    Never raises: the caller is a spawn guard whose job is to explain a failure,
    so it must not be able to fail for an unrelated reason.
    """
    if sys.platform != "win32":
        return False
    try:
        session = _active_console_session_id()
    except Exception:  # noqa: BLE001  PERMANENT(probe must never raise)
        # Belt and braces: the seam already swallows, but a future edit (or a
        # test double) must not be able to turn a spawn guard into a crash.
        debug_logger.log_warning(
            "desktop_launch",
            "available",
            "Console-session seam raised; treating delegation as unavailable",
        )
        return False
    return session is not None and session not in _NO_CONSOLE_SESSION


def can_deliver_headed_window() -> bool:
    """True when a headed spawn from THIS backend will end up visible — either
    because our own context can show windows (F-808) or because the OS can place
    the launch on a logged-on desktop for us (F-810)."""
    return display_context.can_show_windows() or available()


def should_delegate(headless: bool) -> bool:
    """True when a headed spawn must be delegated rather than started here.

    A ``headless=True`` spawn is invisible on purpose and is never delegated.
    """
    return not headless and not display_context.can_show_windows() and available()


def pid_shim(browser: Browser) -> SimpleNamespace | None:
    """A stand-in process object for an ATTACHED browser, or ``None``.

    ``process_cleanup.track_browser_process`` reads only ``.pid`` off the object
    before going pid-based via psutil, and an attached browser has no
    ``_process`` at all — without this shim a delegated browser would never be
    tracked, which is an orphan-reaping hole.
    """
    pid = getattr(browser, "_process_pid", None)
    return SimpleNamespace(pid=pid) if pid else None


def _launch_dir() -> Path:
    """The scratch dir for launcher scripts. Read through ``backend_registry``
    at call time so a test can redirect ``STATE_DIR`` to tmp_path."""
    return backend_registry.STATE_DIR / LAUNCH_DIR_NAME


def _system_binary(name: str) -> str:
    """Absolute path to a Windows system binary, falling back to the bare name.

    A PATH-resolved ``schtasks`` would let an attacker-controlled PATH decide
    what we run (``display_context.py`` sets the same precedent with
    ``/bin/launchctl``).
    """
    import ctypes

    try:
        buffer = ctypes.create_unicode_buffer(260)
        if ctypes.windll.kernel32.GetSystemDirectoryW(buffer, 260):
            candidate = Path(buffer.value) / name
            if candidate.exists():
                return str(candidate)
    except Exception:  # noqa: BLE001  PERMANENT(probe must never raise)
        debug_logger.log_warning(
            "desktop_launch",
            "_system_binary",
            f"System-directory probe failed; falling back to PATH for {name}",
        )
    return name


def _schtasks(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``schtasks.exe``. Seam: no test may create a real scheduled task."""
    return subprocess.run(  # noqa: S603  PERMANENT(fixed argv, no shell, absolute exe)
        [_system_binary("schtasks.exe"), *args],
        capture_output=True,
        text=True,
        timeout=SCHTASKS_TIMEOUT,
        check=False,
    )


def tr_overflow(command: str) -> int | None:
    """*command*'s length when schtasks would store it TRUNCATED, else ``None``.

    THE one home for the comparison, not just for the number (F-879). Both
    ``/TR`` composers in the tree — this module's launcher script and
    ``backend_launch``'s pythonw intermediary — ask here, so neither can drift on
    the cap, on the boundary (``TR_MAX_CHARS`` is what schtasks STORES, so a
    command of exactly that length arrives whole) or on forgetting to ask.

    Returns the length rather than a bool because every caller reports it: a cap
    alone tells an operator nothing about how far over their machine is.
    """
    return len(command) if len(command) > TR_MAX_CHARS else None


# The one spelling of what the scheduler is told to run. A format string rather
# than an f-string inline so ``_tr_command`` and its pin measure the same shape.
_TR_TEMPLATE = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script}"'


def _tr_command(script: Path) -> str:
    """The ``/TR`` for the one-shot task, or ``ToolError`` if it cannot fit.

    THE one composition site, and it cannot hand back a command schtasks would
    truncate — the refusal is the whole point, because a truncated command is
    accepted with exit 0 and then fails at run time where nothing is watching.

    ``powershell.exe`` is left to PATH deliberately, unlike ``_system_binary``'s
    treatment of ``schtasks.exe``: the task runs as the logged-on user, whose
    PATH we are not the ones setting, and an absolute system path would spend
    about 30 of the 253 characters on a name Windows resolves anyway.
    """
    command = _TR_TEMPLATE.format(script=script)
    over = tr_overflow(command)
    if over is not None:
        raise ToolError(
            f"F-879: the desktop-launch task command is {over} characters, past "
            f"the {TR_MAX_CHARS} schtasks stores — it would be truncated with no "
            "error at all, and the task would then fail at run time with nothing "
            f"logged. It is the state dir that is long: {_launch_dir()}. That "
            "path is this account's home plus one fixed subdirectory, so a home "
            "on a deep or redirected profile path is what spends the budget."
        )
    return command


def _ps_quote(value: str) -> str:
    """Single-quote a PowerShell literal. Paths here routinely contain spaces."""
    return "'" + str(value).replace("'", "''") + "'"


def _launcher_script(executable: str, args: list[str], pid_file: Path) -> str:
    """The PowerShell the scheduled task runs on the user's desktop.

    ``schtasks /Create`` stores only ``TR_MAX_CHARS`` of ``/TR`` (253, measured —
    see that constant), so the real command line cannot live there: the task runs
    this file, and the file carries the args. That is what makes a pathological
    profile path or a proxy's worth of switches cost ``/TR`` nothing at all.
    ``-PassThru`` gives us the pid, which is the only thing we need back.

    **Two quoting layers, both load-bearing.** ``subprocess.list2cmdline`` builds
    the Windows command line by the MS C-runtime rules Chrome's own argv parser
    uses (quote anything with whitespace, escape embedded quotes and the
    backslash runs before them); ``_ps_quote`` then turns that whole string into
    ONE PowerShell literal. Passing a LIST to ``-ArgumentList`` would skip the
    first layer entirely: PowerShell joins array elements with spaces and does
    NOT re-quote them, so ``--user-data-dir=C:/A B/prof`` arrives at Chrome as
    two arguments, and a caller-supplied ``--user-agent=`` becomes an injection
    channel into the command line.
    """
    command_line = subprocess.list2cmdline(args)
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"$p = Start-Process -FilePath {_ps_quote(executable)} "
        f"-ArgumentList {_ps_quote(command_line)} -PassThru\n"
        f"Set-Content -LiteralPath {_ps_quote(str(pid_file))} -Value $p.Id\n"
    )


def _devtools_ready(port: int) -> bool:
    """True once Chrome answers on its DevTools port. Blocking; call in a thread."""
    import requests

    try:
        response = requests.get(
            f"http://127.0.0.1:{port}/json/version", timeout=DEVTOOLS_PROBE_TIMEOUT
        )
    except requests.RequestException:
        return False
    return response.status_code == _HTTP_OK


def _read_pid(pid_file: Path) -> int | None:
    """The pid the launcher recorded, or ``None`` until it has written one."""
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _process_create_time(pid: int) -> float | None:
    """The process's start time, or ``None`` if there is no such process.

    Doubles as the liveness probe — a pid with no start time is a pid with no
    process — and as the identity stamp: pid alone is not an identity, because
    the OS recycles the number. Seam; blocking, so call it in a thread.
    """
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


class _Delegated:
    """What the poll learned about the launched browser, readable even when the
    poll RAISES.

    A plain return value cannot carry this: the timeout and cancellation paths
    leave ``_run_task`` by exception, and both of them leave a live Chrome on
    the user's desktop that only we know about. So the pid travels out-of-band,
    with the identity stamp that pins it to that exact process.
    """

    __slots__ = ("create_time", "pid")

    def __init__(self) -> None:
        self.pid: int | None = None
        self.create_time: float | None = None


def _kill_delegated(pid: int, create_time: float | None) -> None:
    """Best-effort kill of a delegated Chrome we could not attach to.

    Nothing else can reap it: it was never handed to ``process_cleanup``, so it
    is in no registry and belongs to no instance. Never raises — the caller is
    already unwinding the real error.

    ``create_time`` is the identity check, not a nicety. Seconds pass between
    the poll that last saw this pid and this kill, and Windows reissues pids
    freely; killing a recycled number would take down an unrelated process of
    the user's. Same pid AND same start time is psutil's own identity rule. No
    stamp means no proof, and no proof means no kill.
    """
    if create_time is None:
        debug_logger.log_warning(
            "desktop_launch",
            "_kill_delegated",
            f"No identity stamp for delegated pid {pid}; refusing to kill it",
        )
        return
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - create_time) > PID_IDENTITY_TOLERANCE:
            debug_logger.log_warning(
                "desktop_launch",
                "_kill_delegated",
                f"Pid {pid} was recycled since we launched it; refusing to kill "
                "a process that is not ours",
            )
            return
        for child in process.children(recursive=True):
            with contextlib.suppress(psutil.Error):
                child.kill()
        process.kill()
    except psutil.Error as error:
        debug_logger.log_warning(
            "desktop_launch",
            "_kill_delegated",
            f"Could not kill the unattached delegated browser {pid}: {error}",
        )


def _cleanup(task_name: str, *paths: Path) -> None:
    """Delete the task and every scratch path given. Never raises — it runs in a
    ``finally`` whose caller may already be raising the real error.

    Variadic because ``backend_launch`` (F-867) runs the same round trip with a
    different set of scratch files; one home for "undo a one-shot task" is worth
    more than a signature that names this module's two.
    """
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        _schtasks(["/Delete", "/F", "/TN", task_name])
    for path in paths:
        with contextlib.suppress(OSError):
            path.unlink()


async def _run_task(
    task_name: str, command: str, port: int, pid_file: Path, delegated: _Delegated
) -> int:
    """Create + run the one-shot task, then wait for Chrome. Returns its pid.

    Takes the ``/TR`` already composed (and already length-checked by
    ``_tr_command``) rather than the script path, because the refusal has to
    happen before the launch dir is created, not here.

    Also RECORDS that pid into *delegated* as soon as it is known to belong to a
    live process, because the two failure paths that matter — the readiness
    timeout and a cancellation mid-poll — leave by exception with a Chrome
    already on the user's desktop, and the return value cannot reach the caller
    to have it killed.
    """
    # No /RU or /RP: a task that runs only when the current user is logged on
    # needs no stored credentials and no admin rights.
    created = await asyncio.to_thread(
        _schtasks,
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
        ],
    )
    if created.returncode != 0:
        raise ToolError(
            f"F-810: could not create the desktop-launch task {task_name} "
            f"(schtasks exit {created.returncode}): {created.stderr.strip()}"
        )
    started = await asyncio.to_thread(_schtasks, ["/Run", "/TN", task_name])
    if started.returncode != 0:
        raise ToolError(
            f"F-810: could not run the desktop-launch task {task_name} "
            f"(schtasks exit {started.returncode}): {started.stderr.strip()}"
        )
    deadline = time.monotonic() + PORT_READY_TIMEOUT
    while time.monotonic() < deadline:
        pid = await asyncio.to_thread(_read_pid, pid_file)
        if pid is not None:
            # Liveness first: a Chrome that exited (it handed off to an already
            # running instance, or died on a bad arg) must fail NOW with a
            # precise reason rather than burn the whole deadline in silence.
            # Deliberately does NOT record into `delegated`: the process is gone,
            # and if it handed off, the live Chrome on that desktop is the
            # USER'S — killing it would be us tidying up with their browser.
            create_time = await asyncio.to_thread(_process_create_time, pid)
            if create_time is None:
                raise ToolError(
                    f"F-810: the delegated browser (pid {pid}) exited before it "
                    f"opened DevTools port {port} — it most likely handed off to "
                    "an already-running Chrome instead of starting its own."
                )
            delegated.pid, delegated.create_time = pid, create_time
            if await asyncio.to_thread(_devtools_ready, port):
                return pid
        await asyncio.sleep(POLL_INTERVAL)
    raise ToolError(
        f"F-810: the delegated browser never opened its DevTools port {port} "
        f"within {PORT_READY_TIMEOUT:.0f}s, so there was nothing to attach to."
    )


async def launch_and_attach(
    browser_executable: str,
    launch_args: list[str],
    user_data_dir: str | None,
) -> tuple[Browser, int]:
    """Launch Chrome on the logged-on user's desktop and attach to it.

    Returns ``(browser, pid)``. The browser is a normal nodriver ``Browser``
    reached over CDP; ``_process_pid`` is stamped on it because nodriver leaves
    it ``None`` on the attach path and teardown's ``os.kill(_process_pid, 15)``
    fallback depends on it.

    Raises ``ToolError`` naming the step that failed. The task and the launcher
    script are always removed, success or failure, and a Chrome that started but
    could not be attached to is killed rather than left as an untracked orphan.
    """
    import nodriver as uc

    # The port is chosen here but bound by Chrome SECONDS later (task create,
    # task run, browser start) — a far wider race window than the normal path's
    # milliseconds. Accepted deliberately: a squatter surfaces as the readiness
    # timeout below, which names the port, and never as a silent attach to some
    # stranger's browser, because we also require OUR launcher's pid.
    port = proxy_forwarder._free_port()
    # Derive the argv from nodriver's OWN config object rather than hand-copying
    # its defaults: the delegated Chrome must receive exactly what the normal
    # path's uc.start would have given it (including --remote-allow-origins=*,
    # without which the CDP websocket handshake is refused), and one home for
    # that list is the only way the two paths cannot drift. Config also
    # synthesizes a temp profile dir when the caller has none, so we never launch
    # against the user's REAL profile — Chrome would hand off to the already
    # running instance and exit, leaving us a dead pid to wait on.
    # (``sandbox`` is not a parameter: ``--no-sandbox`` already rides in
    # ``launch_args`` when the spawn asked for it, added by
    # ``browser_manager._resolve_launch_args`` after the stealth filter.)
    config = uc.Config(
        user_data_dir=user_data_dir,
        headless=False,
        browser_executable_path=browser_executable,
        browser_args=launch_args,
    )
    config.host = "127.0.0.1"
    config.port = port
    args = config()
    token = uuid.uuid4().hex[:TOKEN_CHARS]
    task_name = f"{TASK_PREFIX}{token}"
    launch_dir = _launch_dir()
    script = launch_dir / f"{token}.ps1"
    pid_file = launch_dir / f"{token}.pid"
    # Composed FIRST, because it is the one thing that can be known to be
    # impossible before anything exists: an over-long /TR raises here, with no
    # directory created, no task to delete and no 20s deadline to burn.
    command = _tr_command(script)
    launch_dir.mkdir(parents=True, exist_ok=True)
    script.write_text(
        _launcher_script(browser_executable, args, pid_file), encoding="utf-8"
    )
    delegated = _Delegated()
    attached = False
    try:
        pid = await _run_task(task_name, command, port, pid_file, delegated)
        debug_logger.log_info(
            "desktop_launch",
            "launch_and_attach",
            f"Delegated headed launch landed on the user's desktop as pid {pid}; "
            f"attaching on port {port}",
        )
        # The SAME config: on the attach path nodriver ignores its args and
        # user_data_dir, but ``browser.config.user_data_dir`` is what the spawn
        # pipeline reads back to decide profile cleanup, so it must be the dir
        # the browser actually launched with.
        browser = await uc.start(config=config)
        browser._process_pid = pid
        attached = True
        return browser, pid
    finally:
        # The kill goes FIRST and is synchronous. A cancellation delivered while
        # this ``finally`` is awaiting abandons everything sequenced after that
        # await — measured, not assumed: a second ``cancel()`` landing during
        # the cleanup await skips the remainder of the block. Of the two things
        # that could be stranded here, a visible Chrome that no registry knows
        # about is far worse than a scratch file, so it must not sit behind one.
        if not attached and delegated.pid is not None:
            _kill_delegated(delegated.pid, delegated.create_time)
        # Shielded against the same hazard from the other side: the delete still
        # RUNS to completion if this coroutine is cancelled — we merely stop
        # waiting on it — so a cancelled spawn cannot strand a scheduled task.
        await asyncio.shield(asyncio.to_thread(_cleanup, task_name, script, pid_file))
