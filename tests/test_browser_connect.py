"""F-834 stage 2 — a Chrome that opens its DevTools endpoint after nodriver's
fixed 2.75 s window must still be connected to, not killed and relaunched.

Every node here drives nodriver's REAL ``Browser.start`` — its own 0.25 s lead,
its own ``range(5)`` of retries, its own 0.5 s between them and its own
"Failed to connect to browser" — over a fake endpoint. A hand-written double of
that loop would be a copy of the very thing whose constants are the defect, and
would keep passing the day nodriver changed them; the last node here reads those
constants back out of the library source so the premise itself is pinned.

Only the OS is faked: the subprocess, the endpoint, the websocket ``Connection``
and the clock. No Chrome is launched and no socket is opened. The 2.5 s of
``Browser.sleep`` between nodriver's five attempts and the whole 30 s patience
are virtual; nodriver's initial ``await asyncio.sleep(0.25)`` is NOT — it is
reached before any seam of ours exists and costs each node a real quarter
second.

``test_the_same_chrome_is_given_up_on_without_the_patience`` is the sensitivity
control: the identical scenario through nodriver's unwrapped ``HTTPApi.get``,
which is what shipped through 2.1.9.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import urllib.error

import nodriver as uc
import pytest
from nodriver.core.browser import Browser, HTTPApi

from fakes import nodriver_registry
from stealth_chrome_devtools_mcp.embedded import browser_connect, cdp_attach
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager

pytestmark = pytest.mark.asyncio

WS_URL = "ws://127.0.0.1:9222/devtools/browser/f834-stage2"
VERSION = {"webSocketDebuggerUrl": WS_URL, "Browser": "Chrome/152.0.0.0"}

#: Later than nodriver's whole window (2.75 s) and inside ours. Between the
#: worst warm and worst cold ``ms_to_json_version`` the F-870 probe has
#: measured on the macOS/ARM64 cell (2138.7 ms and 5839.1 ms).
OPENS_AT_SECONDS = 4.0


class Clock:
    """Virtual time for both seams: ours (``_now``/``_sleep``) and nodriver's
    ``Browser.sleep``, which is the 0.5 s between its five attempts."""

    def __init__(self) -> None:
        self.t = 0.0
        #: Called after every advance, so a node can make the world change at a
        #: chosen instant (a launcher that dies mid-wait).
        self.on_tick = None

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float = 0.1) -> None:
        self.t += seconds
        if self.on_tick is not None:
            self.on_tick(self.t)
        await asyncio.sleep(0)


class FakeProcess:
    """``asyncio.subprocess.Process`` as ``Browser.start`` reads it."""

    def __init__(self) -> None:
        self.pid = 4242
        self.returncode = None


class FakeConnection:
    """nodriver's ``Connection``, as ``Browser.start`` uses it: a handler map
    and one ``send``. Nothing here opens a websocket."""

    def __init__(self, url, browser=None, **_kwargs) -> None:
        self.url = url
        self.handlers: dict = {}
        self.sent: list = []

    async def send(self, command, **_kwargs):
        self.sent.append(command)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    dial = Clock()
    monkeypatch.setattr(browser_connect, "_now", dial.now)
    monkeypatch.setattr(browser_connect, "_sleep", dial.sleep)
    monkeypatch.setattr(Browser, "sleep", lambda self, t=0.1: dial.sleep(t))
    return dial


@pytest.fixture
def endpoint(monkeypatch, clock):
    """``/json/version``: refused until ``opens_at`` on the virtual clock."""

    state = {"opens_at": OPENS_AT_SECONDS, "attempts": 0}

    async def _request(self, endpoint_name, method="get", data=None):
        state["attempts"] += 1
        if clock.now() < state["opens_at"]:
            raise urllib.error.URLError(
                ConnectionRefusedError(61, "Connection refused")
            )
        return dict(VERSION)

    monkeypatch.setattr(HTTPApi, "_request", _request)
    return state


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    """``Browser.start`` with the OS removed: no Chrome, no websocket."""

    process = FakeProcess()

    async def _spawn(*_args, **_kwargs):
        return process

    async def _no_targets(self):
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr("nodriver.core.browser.Connection", FakeConnection)
    monkeypatch.setattr(Browser, "update_targets", _no_targets)

    def _config():
        return uc.Config(
            headless=True,
            user_data_dir=str(tmp_path / "profile"),
            sandbox=True,
            browser_executable_path=sys.executable,
            browser_args=[],
        )

    # The registry is a module global of nodriver's and an ``atexit`` hook walks
    # it, so it is left exactly as found — ``fakes.nodriver_registry`` is the one
    # home for that, shared with ``test_spawn_leak``.
    with nodriver_registry():
        yield process, _config


def _unwrapped() -> object:
    """nodriver's own ``HTTPApi.get``, whatever this process has installed."""
    return getattr(HTTPApi.get, browser_connect._MARKER, HTTPApi.get)


async def _start(config_factory) -> Browser:
    browser = Browser(config_factory())
    await browser.start()
    return browser


async def test_a_chrome_that_answers_after_nodrivers_window_is_connected_to(
    clock, endpoint, launcher
):
    """The defect, closed: 4.0 s is past nodriver's 2.75 s and inside ours, so
    the spawn gets the browser it launched instead of a failed attempt."""
    browser_connect.install()
    _process, config_factory = launcher

    browser = await _start(config_factory)

    assert browser.info.webSocketDebuggerUrl == WS_URL
    assert browser.connection.url == WS_URL
    # Answered on nodriver's FIRST attempt — the patience is spent inside it,
    # which is why the four remaining ones never run.
    assert clock.now() == pytest.approx(
        OPENS_AT_SECONDS, abs=browser_connect.POLL_SECONDS
    )


async def test_the_same_chrome_is_given_up_on_without_the_patience(
    monkeypatch, clock, endpoint, launcher
):
    """The sensitivity control, and the shape four of the last five macOS gate
    runs failed on: nodriver alone stops asking 1.25 s before Chrome answers."""
    monkeypatch.setattr(HTTPApi, "get", _unwrapped())
    _process, config_factory = launcher

    with pytest.raises(Exception, match="Failed to connect to browser"):
        await _start(config_factory)

    # Five attempts, 0.5 s apart, and the 0.25 s lead is real time rather than
    # clock time — so the window it gave up after is 2.5 s of virtual sleeping.
    assert endpoint["attempts"] == 5
    assert clock.now() == pytest.approx(2.5)


async def test_an_endpoint_that_never_opens_costs_the_patience_and_no_more(
    clock, endpoint, launcher
):
    """The ceiling is real: a Chrome that never answers still fails, bounded by
    ``CONNECT_PATIENCE_SECONDS`` plus nodriver's own four remaining sleeps."""
    browser_connect.install()
    endpoint["opens_at"] = float("inf")
    _process, config_factory = launcher

    with pytest.raises(Exception, match="Failed to connect to browser"):
        await _start(config_factory)

    assert clock.now() >= browser_connect.CONNECT_PATIENCE_SECONDS
    assert clock.now() == pytest.approx(
        browser_connect.CONNECT_PATIENCE_SECONDS + 2.5,
        abs=browser_connect.POLL_SECONDS,
    )


async def test_a_launcher_that_already_exited_is_not_waited_for(
    clock, endpoint, launcher
):
    """The one case this could have made slower is instead faster than 2.1.9:
    a Chrome that died at startup ends the wait at the first refusal, where
    nodriver polls a port nobody will open for its whole window."""
    browser_connect.install()
    endpoint["opens_at"] = float("inf")
    process, config_factory = launcher
    process.returncode = 1

    with pytest.raises(Exception, match="Failed to connect to browser"):
        await _start(config_factory)

    # Only nodriver's own five attempts and four-and-a-bit sleeps; none of the
    # patience was spent.
    assert clock.now() < browser_connect.CONNECT_PATIENCE_SECONDS
    assert endpoint["attempts"] == 5


async def test_a_launcher_that_exits_mid_wait_ends_the_wait_there(
    clock, endpoint, launcher
):
    """The shape production actually sees — Chrome starts, then dies while we
    are still asking. The ``returncode`` is re-read every pass, so the wait ends
    at the next refusal rather than at the ceiling."""
    browser_connect.install()
    endpoint["opens_at"] = float("inf")
    process, config_factory = launcher
    died_at = 1.0

    def die_at(now: float) -> None:
        if now >= died_at:
            process.returncode = 1

    clock.on_tick = die_at

    with pytest.raises(Exception, match="Failed to connect to browser"):
        await _start(config_factory)

    # The patience stopped at the death, not at 30 s; what is left on the clock
    # is nodriver's own four sleeps after it.
    assert clock.now() < died_at + 2.5 + browser_connect.POLL_SECONDS


async def test_an_attach_does_not_get_the_launch_ceiling(clock, endpoint, tmp_path):
    """`connect_existing` must NOT inherit a ceiling sized for a process that is
    still starting. Driven through `cdp_attach`, which is THE one home for that
    door and F-888's `browser_reattach` door with it — so this follows the real
    config rather than a hand-built one. An attach targets an endpoint that is
    already open (measured: 0.78 ms median for a live one), so a stale recorded
    port has to stay as cheap to reject as it was in 2.1.9."""
    browser_connect.install()
    endpoint["opens_at"] = float("inf")
    config = cdp_attach.config_for(str(tmp_path / "profile"), 9222, headless=True)
    with nodriver_registry(), pytest.raises(Exception, match="Failed to connect"):
        await cdp_attach.attach(config)

    # nodriver's own five attempts and 2.5 s, not our 30 s: nothing was launched
    # here, so there is no ``_process`` to be patient on behalf of.
    assert endpoint["attempts"] == 5
    assert clock.now() == pytest.approx(2.5)


async def test_a_server_that_answered_is_not_an_endpoint_that_is_still_opening(
    monkeypatch, clock, endpoint, launcher
):
    """A squatter on the port answering 500 is a fact, not a wait. Retrying it
    to the ceiling would turn 2.1.9's fast failure into a 30 s one."""
    browser_connect.install()
    _process, config_factory = launcher

    async def _answers_500(self, endpoint_name, method="get", data=None):
        endpoint["attempts"] += 1
        raise urllib.error.HTTPError(
            "http://127.0.0.1/json/version", 500, "no", {}, None
        )

    monkeypatch.setattr(HTTPApi, "_request", _answers_500)

    with pytest.raises(Exception, match="Failed to connect to browser"):
        await _start(config_factory)

    assert endpoint["attempts"] == 5
    assert clock.now() == pytest.approx(2.5)


async def test_only_the_version_endpoint_is_given_the_patience(clock, endpoint):
    """Keyed on the endpoint, not on the class, so a future nodriver that grows
    a second ``HTTPApi.get`` caller does not inherit this silently."""
    browser_connect.install()
    endpoint["opens_at"] = float("inf")

    with pytest.raises(urllib.error.URLError):
        await HTTPApi(("127.0.0.1", 9222)).get("list")

    assert endpoint["attempts"] == 1
    assert clock.now() == 0.0


async def test_install_is_idempotent():
    """``embedded/server.py`` is executed three times under runpy, and the
    launch path calls this on every spawn."""
    browser_connect.install()
    first = HTTPApi.get
    browser_connect.install()

    assert HTTPApi.get is first
    assert browser_connect.installed()


async def test_the_launch_path_installs_the_patience_before_it_launches(
    monkeypatch, tmp_path
):
    """Wired at ``_launch_browser``, ahead of the F-810 delegation branch, so
    every launch in the tree is covered — including the delegated one, which
    attaches through the same ``Browser.start``."""
    from stealth_chrome_devtools_mcp.embedded import browser_manager as bm
    from stealth_chrome_devtools_mcp.embedded import spawn_leak
    from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions

    calls: list[str] = []
    monkeypatch.setattr(browser_connect, "install", lambda: calls.append("install"))
    monkeypatch.setattr(bm.desktop_launch, "should_delegate", lambda headless: True)

    async def _attach(executable, args, user_data_dir):
        calls.append("launch")
        return object(), 1

    monkeypatch.setattr(bm.desktop_launch, "launch_and_attach", _attach)

    attempt = spawn_leak.Attempt()
    await BrowserManager()._launch_browser(
        BrowserOptions(user_data_dir=str(tmp_path)), "/fake/chrome", [], attempt
    )

    assert calls == ["install", "launch"]
    # The DELEGATED launch leaves the attempt unstamped: its own cleanup is
    # best-effort, so this fence reaches nothing there (F-919 §6, F-924).
    assert attempt.config is None


async def test_the_window_this_extends_is_still_the_one_in_nodrivers_source():
    """The premise, pinned against the library rather than asserted: 0.25 s plus
    five attempts 0.5 s apart, and ONE caller of ``HTTPApi.get`` for the patience
    to reach. If nodriver changes any of this, fail here — where the finding's
    measurements are — and not in a fleet run."""
    source = inspect.getsource(Browser.start)

    assert "await asyncio.sleep(0.25)" in source
    assert "for _ in range(5):" in source
    assert 'await self._http.get("version")' in source
    assert "await self.sleep(0.5)" in source
    assert "Failed to connect to browser" in source

    browser_source = inspect.getsource(sys.modules[Browser.__module__])
    assert browser_source.count('_http.get("') == 1
