"""F-810: a headed spawn from a headless-context backend is delivered by the OS.

The backend never picks or enters a session (the F-808 spirit is intact): it hands
Chrome's *process creation* to Windows Task Scheduler, which places the process in
the logged-on user's interactive session, and then attaches over CDP. This file
owns three contracts:

* ``available()`` — when delegation is possible at all (win32 + a console session).
* ``launch_and_attach`` — the task create → run → poll → delete → attach sequence,
  including that a failure never leaves a scheduled task or a script behind.
* the tracking hole — an ATTACHED browser has no ``_process``, so without the pid
  shim it would be invisible to orphan reaping.

NOTHING here may create a real scheduled task or touch the real ``~/.stealth-mcp``:
``_schtasks`` is faked and ``backend_registry.STATE_DIR`` is redirected to tmp_path.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from stealth_chrome_devtools_mcp.embedded import (
    backend_registry,
    browser_manager,
    desktop_launch,
    display_context,
    proxy_forwarder,
    tool_errors,
    window_sizing,
)

# ---------------------------------------------------------------------------
# available() — the one question "can this process delegate a launch?"
# ---------------------------------------------------------------------------


def _pretend_win32(monkeypatch, session: int | None) -> None:
    monkeypatch.setattr(desktop_launch.sys, "platform", "win32")
    monkeypatch.setattr(desktop_launch, "_active_console_session_id", lambda: session)


def test_available_true_on_win32_with_a_logged_on_console_session(monkeypatch):
    _pretend_win32(monkeypatch, 1)
    assert desktop_launch.available() is True


@pytest.mark.parametrize("session", [0, 0xFFFFFFFF, None])
def test_available_false_without_a_usable_console_session(monkeypatch, session):
    """Session 0 is the isolated services session and 0xFFFFFFFF means "nobody is
    attached" — neither can show a window, so delegation is not on offer."""
    _pretend_win32(monkeypatch, session)
    assert desktop_launch.available() is False


def test_available_false_off_win32(monkeypatch):
    monkeypatch.setattr(desktop_launch.sys, "platform", "linux")

    def _boom() -> int:
        raise AssertionError("the session probe must not run off win32")

    monkeypatch.setattr(desktop_launch, "_active_console_session_id", _boom)
    assert desktop_launch.available() is False


def test_available_never_raises(monkeypatch):
    """A broken probe must degrade to "no delegation", never to an exception: the
    caller is a spawn guard whose whole job is to explain a failure clearly."""
    monkeypatch.setattr(desktop_launch.sys, "platform", "win32")

    def _boom() -> int:
        raise OSError("probe exploded")

    monkeypatch.setattr(desktop_launch, "_active_console_session_id", _boom)
    assert desktop_launch.available() is False


def test_the_real_probe_never_raises_on_this_machine():
    """The unfaked seam, on whatever platform the runner is: an int or None."""
    session = desktop_launch._active_console_session_id()
    assert session is None or isinstance(session, int)


# ---------------------------------------------------------------------------
# The two derived predicates the guard and the spawn pipeline consult
# ---------------------------------------------------------------------------


def test_can_deliver_headed_window_true_when_the_context_can_show_windows(
    monkeypatch,
):
    monkeypatch.setattr(display_context, "display_context", lambda: "win-session-1")
    monkeypatch.setattr(desktop_launch, "available", lambda: False)
    assert desktop_launch.can_deliver_headed_window() is True


def test_can_deliver_headed_window_true_via_delegation_alone(monkeypatch):
    monkeypatch.setattr(
        display_context, "display_context", lambda: display_context.HEADLESS
    )
    monkeypatch.setattr(desktop_launch, "available", lambda: True)
    assert desktop_launch.can_deliver_headed_window() is True


def test_can_deliver_headed_window_false_when_neither_holds(monkeypatch):
    monkeypatch.setattr(
        display_context, "display_context", lambda: display_context.HEADLESS
    )
    monkeypatch.setattr(desktop_launch, "available", lambda: False)
    assert desktop_launch.can_deliver_headed_window() is False


def test_should_delegate_only_for_a_headed_spawn_that_cannot_be_seen(monkeypatch):
    monkeypatch.setattr(
        display_context, "display_context", lambda: display_context.HEADLESS
    )
    monkeypatch.setattr(desktop_launch, "available", lambda: True)
    assert desktop_launch.should_delegate(headless=False) is True
    # A headless spawn is invisible on purpose — never delegate it.
    assert desktop_launch.should_delegate(headless=True) is False


def test_should_delegate_false_on_a_desktop_backend(monkeypatch):
    """The common case: the backend already owns a desktop, so the normal
    ``uc.start`` path must stay untouched."""
    monkeypatch.setattr(display_context, "display_context", lambda: "win-session-1")
    monkeypatch.setattr(desktop_launch, "available", lambda: True)
    assert desktop_launch.should_delegate(headless=False) is False


# ---------------------------------------------------------------------------
# launch_and_attach
# ---------------------------------------------------------------------------


def _split_windows_command_line(line: str) -> list[str]:
    """Split a Windows command line into argv the way Chrome's CRT parser does.

    An INDEPENDENT implementation of the documented MS C-runtime rules (2n
    backslashes + `"` → n backslashes and a quote toggle; 2n+1 → n backslashes
    and a literal quote), so an assertion on it is not just
    ``list2cmdline`` agreeing with itself. Asserting on the PowerShell literal
    instead would pin the serialization and miss the only thing that matters:
    what lands in Chrome's ``argv``.
    """
    argv: list[str] = []
    current: list[str] = []
    quoted = False
    started = False
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\":
            slashes = 0
            while index < len(line) and line[index] == "\\":
                slashes += 1
                index += 1
            if index < len(line) and line[index] == '"':
                current.append("\\" * (slashes // 2))
                if slashes % 2:
                    current.append('"')
                else:
                    quoted = not quoted
                index += 1
            else:
                current.append("\\" * slashes)
            started = True
            continue
        if char == '"':
            quoted = not quoted
            started = True
            index += 1
            continue
        if char in " \t" and not quoted:
            if started:
                argv.append("".join(current))
                current = []
                started = False
            index += 1
            continue
        current.append(char)
        started = True
        index += 1
    if started:
        argv.append("".join(current))
    return argv


def _powershell_literal(text: str, after: str) -> str:
    """The single-quoted PowerShell literal following *after* in *text*."""
    rest = text.split(after, 1)[1]
    assert rest.startswith("'"), rest[:40]
    out: list[str] = []
    index = 1
    while index < len(rest):
        if rest[index] == "'":
            if rest[index + 1 : index + 2] == "'":  # '' is an escaped quote
                out.append("'")
                index += 2
                continue
            return "".join(out)
        out.append(rest[index])
        index += 1
    raise AssertionError("unterminated PowerShell literal")


class FakeSchtasks:
    """Records every schtasks invocation and plays the scheduler's part.

    On ``/Create`` it reads the launcher script named in ``/TR`` (the real
    scheduler's only input) and keeps its text, so a test can assert what would
    actually have run. On ``/Run`` it writes the pid file the launcher writes.
    """

    def __init__(self, pid: int = 4242, *, create_rc: int = 0, run_rc: int = 0):
        self.calls: list[list[str]] = []
        self.script_text: str | None = None
        self.script_path: Path | None = None
        self.pid = pid
        self._create_rc = create_rc
        self._run_rc = run_rc
        self.launched = False

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        verb = args[0]
        rc = 0
        if verb == "/Create":
            command = args[args.index("/TR") + 1]
            self.script_path = Path(command.split('-File "')[1].rstrip('"'))
            self.script_text = self.script_path.read_text(encoding="utf-8")
            rc = self._create_rc
        elif verb == "/Run":
            rc = self._run_rc
            if rc == 0 and self.script_path is not None:
                self.launched = True
                self.script_path.with_suffix(".pid").write_text(
                    f"{self.pid}\n", encoding="utf-8"
                )
        return subprocess.CompletedProcess(
            args=["schtasks", *args],
            returncode=rc,
            stdout="",
            stderr="boom" if rc else "",
        )

    @property
    def verbs(self) -> list[str]:
        return [call[0] for call in self.calls]

    def task_names(self) -> list[str]:
        return [call[call.index("/TN") + 1] for call in self.calls if "/TN" in call]

    @property
    def chrome_argv(self) -> list[str]:
        """What Chrome's ``argv`` would actually contain (argv[1:]).

        Both layers undone in order: the PowerShell literal, then the Windows
        command-line quoting. This is the assertion surface — the raw script
        text is not, because a script that reads plausibly can still hand Chrome
        four arguments where one was meant.
        """
        assert self.script_text is not None
        command_line = _powershell_literal(self.script_text, "-ArgumentList ")
        return _split_windows_command_line(command_line)

    @property
    def chrome_executable(self) -> str:
        assert self.script_text is not None
        return _powershell_literal(self.script_text, "-FilePath ")


@pytest.fixture()
def delegation(monkeypatch, tmp_path):
    """Hermetic delegation plumbing: tmp state dir, fixed port, faked scheduler,
    faked DevTools probe, faked ``uc.start``. Returns the handles a test asserts on."""
    monkeypatch.setattr(backend_registry, "STATE_DIR", tmp_path)
    monkeypatch.setattr(proxy_forwarder, "_free_port", lambda: 9333)
    monkeypatch.setattr(desktop_launch, "_devtools_ready", lambda port: True)
    # The launched pid is a fake number, so liveness is stated, not observed.
    monkeypatch.setattr(desktop_launch, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(desktop_launch, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(desktop_launch, "PORT_READY_TIMEOUT", 1.0)

    started: list[object] = []
    killed: list[int] = []

    async def fake_start(config):
        started.append(config)
        return SimpleNamespace(_process=None, _process_pid=None)

    monkeypatch.setattr(desktop_launch.uc, "start", fake_start)
    monkeypatch.setattr(desktop_launch, "_kill_delegated", killed.append)

    def _install(schtasks: FakeSchtasks) -> FakeSchtasks:
        monkeypatch.setattr(desktop_launch, "_schtasks", schtasks)
        return schtasks

    return SimpleNamespace(
        install=_install, started=started, killed=killed, state_dir=tmp_path
    )


async def test_launch_and_attach_returns_the_launched_pid_and_attaches(delegation):
    schtasks = delegation.install(FakeSchtasks(pid=4242))
    browser, pid = await desktop_launch.launch_and_attach(
        "C:/Program Files/Google/Chrome/chrome.exe",
        ["--window-size=800,600"],
        "C:/profiles/clone one",
    )
    assert pid == 4242
    # The attached browser carries the pid nodriver leaves None, so teardown's
    # os.kill fallback has something to kill.
    assert browser._process_pid == 4242
    assert len(delegation.started) == 1
    config = delegation.started[0]
    assert (config.host, config.port) == ("127.0.0.1", 9333)


async def test_the_launcher_hands_chrome_the_args_the_config_would_have(delegation):
    """``browser_args``/``user_data_dir`` are IGNORED by nodriver on attach, so the
    ONLY place they can take effect is the launcher command line — asserted as
    the argv Chrome receives, not as the text of the script."""
    schtasks = delegation.install(FakeSchtasks())
    await desktop_launch.launch_and_attach(
        "C:/Program Files/Google/Chrome/chrome.exe",
        ["--window-size=800,600"],
        "C:/profiles/clone one",
    )
    argv = schtasks.chrome_argv
    assert "--window-size=800,600" in argv
    assert "--remote-debugging-port=9333" in argv
    assert "--user-data-dir=C:/profiles/clone one" in argv
    assert schtasks.chrome_executable == "C:/Program Files/Google/Chrome/chrome.exe"
    assert "-PassThru" in schtasks.script_text


async def test_a_path_with_spaces_stays_one_chrome_argument(delegation):
    """The regression: ``-ArgumentList`` given a LIST joins elements with spaces
    and does not re-quote them, so this profile path used to reach Chrome as four
    separate arguments — a browser silently launched against the wrong profile.
    A user agent is the same hole pointed the other way: caller-controlled text
    on the command line."""
    schtasks = delegation.install(FakeSchtasks())
    profile = "C:/Users/amind/CUSTOM MCPs & PRODUCTIVITY/prof"
    user_agent = "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) Safari/537"
    await desktop_launch.launch_and_attach(
        "C:/Program Files/Google/Chrome/chrome.exe", [user_agent], profile
    )
    argv = schtasks.chrome_argv
    assert f"--user-data-dir={profile}" in argv
    assert user_agent in argv
    # The sharp edge stated positively: NO fragment of either value became its
    # own argument. "MCPs" alone in argv is exactly the shipped-bug signature.
    assert "MCPs" not in argv
    assert "&" not in argv
    assert "(Windows" not in argv


@pytest.mark.skipif(sys.platform != "win32", reason="CommandLineToArgvW is a Win32 API")
async def test_the_quoting_matches_windows_own_parser(delegation):
    """Cross-check the reference splitter against the OS: ``CommandLineToArgvW``
    is the parser Chrome's own argv comes from."""
    import ctypes
    from ctypes import wintypes

    schtasks = delegation.install(FakeSchtasks())
    profile = 'C:/a b/pro"file\\'
    await desktop_launch.launch_and_attach("chrome.exe", ["--foo=a b"], profile)
    command_line = _powershell_literal(schtasks.script_text, "-ArgumentList ")

    ctypes.windll.shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    count = ctypes.c_int(0)
    # A leading exe token: CommandLineToArgvW parses argv[0] by different rules.
    argv_ptr = ctypes.windll.shell32.CommandLineToArgvW(
        f"chrome.exe {command_line}", ctypes.byref(count)
    )
    try:
        os_argv = [argv_ptr[i] for i in range(count.value)][1:]
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(argv_ptr, wintypes.HLOCAL))

    assert os_argv == schtasks.chrome_argv
    assert f"--user-data-dir={profile}" in os_argv
    assert "--foo=a b" in os_argv


async def test_the_delegated_chrome_gets_nodrivers_own_default_args(delegation):
    """The delegated argv is DERIVED from nodriver's Config, never hand-copied.

    Without ``--remote-allow-origins=*`` the CDP websocket handshake is refused,
    so a delegated browser that came up would still be unusable — and any list we
    maintained ourselves would drift from nodriver's on the next upgrade.
    """
    import nodriver as uc

    schtasks = delegation.install(FakeSchtasks())
    await desktop_launch.launch_and_attach(
        "chrome.exe", ["--window-size=800,600"], "C:/profiles/p"
    )
    argv = schtasks.chrome_argv
    expected = uc.Config(
        user_data_dir="C:/profiles/p",
        headless=False,
        browser_executable_path="chrome.exe",
        browser_args=["--window-size=800,600"],
    )
    expected.host, expected.port = "127.0.0.1", 9333
    assert argv == expected()
    assert "--remote-allow-origins=*" in argv
    assert "--remote-debugging-host=127.0.0.1" in argv


async def test_a_delegated_launch_always_names_a_profile_dir(delegation):
    """With no user_data_dir, Chrome would open the user's DEFAULT profile — and
    if Chrome is already running it hands off and exits, leaving -PassThru a dead
    pid and the poll burning its whole deadline. Config synthesizes a temp dir;
    we use it rather than launching bare."""
    schtasks = delegation.install(FakeSchtasks())
    browser, _pid = await desktop_launch.launch_and_attach("chrome.exe", [], None)
    profile_args = [a for a in schtasks.chrome_argv if a.startswith("--user-data-dir=")]
    assert len(profile_args) == 1
    assert profile_args[0] != "--user-data-dir="
    # The attach config reports the dir the browser REALLY launched with — the
    # spawn pipeline reads it back to decide profile cleanup.
    assert delegation.started[0].user_data_dir == profile_args[0].split("=", 1)[1]


async def test_a_chrome_that_died_fails_fast_with_a_precise_reason(
    delegation, monkeypatch
):
    """A handed-off Chrome exits immediately. Waiting out the full deadline for a
    process that is already gone is 20s of silence for a knowable answer."""
    delegation.install(FakeSchtasks(pid=4242))
    checked: list[int] = []

    def dead(pid: int) -> bool:
        checked.append(pid)
        return False

    monkeypatch.setattr(desktop_launch, "_pid_alive", dead)
    monkeypatch.setattr(desktop_launch, "_devtools_ready", lambda port: False)
    with pytest.raises(tool_errors.ToolError) as err:
        await desktop_launch.launch_and_attach("chrome.exe", [], "C:/p")
    message = str(err.value)
    assert "F-810" in message
    assert "4242" in message
    assert "exited" in message
    assert checked == [4242]
    # Nothing is killed: the process is gone by definition, and if it handed off
    # then the live Chrome on that desktop is the USER'S — killing it would take
    # their browser down to clean up after ourselves.
    assert delegation.killed == []


async def test_a_browser_we_could_not_attach_to_is_killed(delegation, monkeypatch):
    """The orphan hole: the launch succeeded, so a real Chrome is on the user's
    desktop, but nothing recorded it — no instance, no pid registry, no reaper.
    Every exit path after the pid is known must kill it."""
    delegation.install(FakeSchtasks(pid=4242))

    async def exploding_start(config):
        raise RuntimeError("websocket handshake refused")

    monkeypatch.setattr(desktop_launch.uc, "start", exploding_start)
    with pytest.raises(RuntimeError):
        await desktop_launch.launch_and_attach("chrome.exe", [], "C:/p")
    assert delegation.killed == [4242]


async def test_a_successful_attach_kills_nothing(delegation):
    delegation.install(FakeSchtasks(pid=4242))
    await desktop_launch.launch_and_attach("chrome.exe", [], "C:/p")
    assert delegation.killed == []


async def test_a_failure_before_the_pid_is_known_kills_nothing(delegation):
    delegation.install(FakeSchtasks(create_rc=1))
    with pytest.raises(tool_errors.ToolError):
        await desktop_launch.launch_and_attach("chrome.exe", [], "C:/p")
    assert delegation.killed == []


async def test_the_task_is_created_run_and_deleted_under_one_name(delegation):
    schtasks = delegation.install(FakeSchtasks())
    await desktop_launch.launch_and_attach("chrome.exe", [], None)
    assert schtasks.verbs == ["/Create", "/Run", "/Delete"]
    names = set(schtasks.task_names())
    assert len(names) == 1
    assert next(iter(names)).startswith(desktop_launch.TASK_PREFIX)
    # A logged-on-only task needs no admin: passing credentials would.
    create = schtasks.calls[0]
    assert "/RU" not in create and "/RP" not in create


async def test_the_launch_dir_does_not_grow(delegation):
    schtasks = delegation.install(FakeSchtasks())
    await desktop_launch.launch_and_attach("chrome.exe", [], None)
    launch_dir = delegation.state_dir / desktop_launch.LAUNCH_DIR_NAME
    assert list(launch_dir.iterdir()) == []


async def test_a_failed_task_create_raises_and_leaves_nothing_behind(delegation):
    schtasks = delegation.install(FakeSchtasks(create_rc=1))
    with pytest.raises(tool_errors.ToolError) as err:
        await desktop_launch.launch_and_attach("chrome.exe", [], None)
    message = str(err.value)
    assert "F-810" in message
    assert "task" in message.lower()
    assert "/Delete" in schtasks.verbs
    assert list((delegation.state_dir / desktop_launch.LAUNCH_DIR_NAME).iterdir()) == []
    assert delegation.started == []


async def test_a_failed_task_run_raises(delegation):
    schtasks = delegation.install(FakeSchtasks(run_rc=1))
    with pytest.raises(tool_errors.ToolError) as err:
        await desktop_launch.launch_and_attach("chrome.exe", [], None)
    assert "F-810" in str(err.value)
    assert "/Delete" in schtasks.verbs


async def test_a_devtools_port_that_never_answers_raises_after_the_deadline(
    delegation, monkeypatch
):
    monkeypatch.setattr(desktop_launch, "_devtools_ready", lambda port: False)
    schtasks = delegation.install(FakeSchtasks())
    with pytest.raises(tool_errors.ToolError) as err:
        await desktop_launch.launch_and_attach("chrome.exe", [], None)
    message = str(err.value)
    assert "F-810" in message
    assert "9333" in message
    assert "/Delete" in schtasks.verbs
    assert delegation.started == []


# ---------------------------------------------------------------------------
# The unfaked psutil seams
# ---------------------------------------------------------------------------


def test_pid_alive_reads_the_real_process_table():
    assert desktop_launch._pid_alive(os.getpid()) is True


def test_kill_delegated_takes_the_children_too(monkeypatch):
    """Chrome is a process TREE; killing only the root can leave renderers."""
    killed: list[str] = []

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def children(self, recursive=False):
            return [SimpleNamespace(kill=lambda: killed.append("child"))]

        def kill(self):
            killed.append("root")

    monkeypatch.setattr(desktop_launch.psutil, "Process", FakeProcess)
    desktop_launch._kill_delegated(4242)
    assert killed == ["child", "root"]


def test_kill_delegated_never_raises(monkeypatch):
    """It runs while the real error is unwinding; it must not replace it."""

    def boom(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(desktop_launch.psutil, "Process", boom)
    desktop_launch._kill_delegated(4242)  # no raise


# ---------------------------------------------------------------------------
# The tracking hole: an attached browser has no _process
# ---------------------------------------------------------------------------


def test_pid_shim_stands_in_for_an_attached_browsers_process():
    shim = desktop_launch.pid_shim(SimpleNamespace(_process=None, _process_pid=1234))
    assert shim is not None
    assert shim.pid == 1234


def test_pid_shim_is_none_when_there_is_no_pid():
    assert desktop_launch.pid_shim(SimpleNamespace(_process=None)) is None
    assert (
        desktop_launch.pid_shim(SimpleNamespace(_process=None, _process_pid=None))
        is None
    )


async def test_apply_post_launch_tracks_an_attached_browser(monkeypatch):
    """Without this, a delegated browser is untracked — an orphan-reaping hole."""
    tracked: list[tuple[str, object]] = []

    def fake_track(instance_id, process, **kwargs):
        tracked.append((instance_id, process))

    monkeypatch.setattr(
        browser_manager.process_cleanup, "track_browser_process", fake_track
    )

    async def fake_apply_and_measure(tab, options):
        return {
            "requested": {"width": 1, "height": 1},
            "actual": None,
            "inner_viewport": None,
            "measured": False,
            "clamped": None,
        }

    monkeypatch.setattr(window_sizing, "apply_and_measure", fake_apply_and_measure)

    manager = browser_manager.BrowserManager()

    async def fake_timezone(tab, timezone_id):
        return None

    monkeypatch.setattr(manager, "_apply_timezone_override", fake_timezone)

    options = browser_manager.BrowserOptions(headless=False)
    attached = SimpleNamespace(_process=None, _process_pid=1234)
    await manager._apply_post_launch(
        attached, SimpleNamespace(), options, "i-attached", None, False
    )

    assert len(tracked) == 1
    instance_id, process = tracked[0]
    assert instance_id == "i-attached"
    assert process.pid == 1234


def test_desktop_launch_never_imports_server():
    """The one import convention: no module under embedded/ imports ``server``."""
    source = Path(desktop_launch.__file__).read_text(encoding="utf-8")
    assert "import server" not in source
    assert "embedded.server" not in source
    assert sys.modules[desktop_launch.__name__].__name__.endswith("desktop_launch")
