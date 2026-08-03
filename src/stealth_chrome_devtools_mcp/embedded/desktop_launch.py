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

import nodriver as uc
import psutil
import requests
from nodriver import Browser

from stealth_chrome_devtools_mcp.embedded import (
    backend_registry,
    display_context,
    proxy_forwarder,
)
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

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


def _ps_quote(value: str) -> str:
    """Single-quote a PowerShell literal. Paths here routinely contain spaces."""
    return "'" + str(value).replace("'", "''") + "'"


def _launcher_script(executable: str, args: list[str], pid_file: Path) -> str:
    """The PowerShell the scheduled task runs on the user's desktop.

    ``schtasks /Create /TR`` truncates around 261 characters, so the real command
    line cannot live there — the task runs this file, and the file carries the
    args. ``-PassThru`` gives us the pid, which is the only thing we need back.

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


def _pid_alive(pid: int) -> bool:
    """Seam: is the delegated process still running? Blocking; call in a thread."""
    return psutil.pid_exists(pid)


def _kill_delegated(pid: int) -> None:
    """Best-effort kill of a delegated Chrome we could not attach to.

    Nothing else can reap it: it was never handed to ``process_cleanup``, so it
    is in no registry and belongs to no instance. Never raises — the caller is
    already unwinding the real error.
    """
    try:
        process = psutil.Process(pid)
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


def _cleanup(task_name: str, script: Path, pid_file: Path) -> None:
    """Delete the task and the scratch files. Never raises — it runs in a
    ``finally`` whose caller may already be raising the real error."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        _schtasks(["/Delete", "/F", "/TN", task_name])
    for path in (script, pid_file):
        with contextlib.suppress(OSError):
            path.unlink()


async def _run_task(task_name: str, script: Path, port: int, pid_file: Path) -> int:
    """Create + run the one-shot task, then wait for Chrome. Returns its pid."""
    command = f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script}"'
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
            if not await asyncio.to_thread(_pid_alive, pid):
                raise ToolError(
                    f"F-810: the delegated browser (pid {pid}) exited before it "
                    f"opened DevTools port {port} — it most likely handed off to "
                    "an already-running Chrome instead of starting its own."
                )
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
    token = uuid.uuid4().hex
    task_name = f"{TASK_PREFIX}{token}"
    launch_dir = _launch_dir()
    launch_dir.mkdir(parents=True, exist_ok=True)
    script = launch_dir / f"{token}.ps1"
    pid_file = launch_dir / f"{token}.pid"
    script.write_text(
        _launcher_script(browser_executable, args, pid_file), encoding="utf-8"
    )
    pid: int | None = None
    attached = False
    try:
        pid = await _run_task(task_name, script, port, pid_file)
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
        await asyncio.to_thread(_cleanup, task_name, script, pid_file)
        if not attached and pid is not None:
            _kill_delegated(pid)
