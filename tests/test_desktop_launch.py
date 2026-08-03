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

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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


@pytest.fixture()
def delegation(monkeypatch, tmp_path):
    """Hermetic delegation plumbing: tmp state dir, fixed port, faked scheduler,
    faked DevTools probe, faked ``uc.start``. Returns the handles a test asserts on."""
    monkeypatch.setattr(backend_registry, "STATE_DIR", tmp_path)
    monkeypatch.setattr(proxy_forwarder, "_free_port", lambda: 9333)
    monkeypatch.setattr(desktop_launch, "_devtools_ready", lambda port: True)
    monkeypatch.setattr(desktop_launch, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(desktop_launch, "PORT_READY_TIMEOUT", 1.0)

    started: list[object] = []

    async def fake_start(config):
        started.append(config)
        return SimpleNamespace(_process=None, _process_pid=None)

    monkeypatch.setattr(desktop_launch.uc, "start", fake_start)

    def _install(schtasks: FakeSchtasks) -> FakeSchtasks:
        monkeypatch.setattr(desktop_launch, "_schtasks", schtasks)
        return schtasks

    return SimpleNamespace(install=_install, started=started, state_dir=tmp_path)


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


async def test_the_launcher_script_carries_the_args_the_config_would_have(delegation):
    """``browser_args``/``user_data_dir`` are IGNORED by nodriver on attach, so the
    ONLY place they can take effect is the launcher command line."""
    schtasks = delegation.install(FakeSchtasks())
    await desktop_launch.launch_and_attach(
        "C:/Program Files/Google/Chrome/chrome.exe",
        ["--window-size=800,600"],
        "C:/profiles/clone one",
    )
    text = schtasks.script_text
    assert "--window-size=800,600" in text
    assert "--remote-debugging-port=9333" in text
    assert "--user-data-dir=C:/profiles/clone one" in text
    # Every value single-quoted: paths here routinely contain spaces.
    assert "'C:/Program Files/Google/Chrome/chrome.exe'" in text
    assert "'--user-data-dir=C:/profiles/clone one'" in text
    assert "-PassThru" in text


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
