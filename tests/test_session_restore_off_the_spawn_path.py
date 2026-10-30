"""T-9479: a named spawn does not wait on the ``chrome://settings`` exchange.

The incident (v2.1.49's release gate, Windows integration,
``test_a_fleet_of_six_browsers_answers_truthfully_about_every_page``): four Chromes
cold-starting on 4 vCPUs, and ``login_persistence``'s first stage
(``Target.createTarget``) starved for 17 s inside the spawn's post-launch phase. It
was cut off and the pref was left unset, so that named session would have lost its
session cookies at its close.

Pinned, each with a double that NEVER answers rather than one that raises (a raising
double is the failure the product already handled):

* a profile whose file says ``1`` opens no settings tab, but only once Chrome has had
  time to contradict the file (a profile whose MAC Chrome rejects keeps saying ``1``
  for about 10 s while the live pref is 5), and a close that comes sooner runs the
  exchange, which reads the live value; the file is read from the place Chrome keeps
  it (``Secure Preferences`` where it tracks prefs, ``Preferences`` elsewhere);
* a named spawn returns while the exchange is still pending; a clone and an unnamed
  spawn start none;
* a close waits for a pending exchange BEFORE it ends the connection, as long as the
  exchange itself may take, and cancels it then; a close that is itself cancelled
  while it waits still ends the browser;
* the exchange's own tab is never the tab a caller's closed tab falls back to.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from fakes import (
    FakeAttachedTab,
    FakeBrowser,
    FakeDiscoveredTarget,
    FakeTab,
    fake_instance,
    fake_target,
)
from stealth_chrome_devtools_mcp.embedded import (
    browser_connect,
    browser_manager,
    login_persistence,
    process_exit,
    tab_open,
    window_sizing,
)
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import (
    BrowserInstance,
    BrowserOptions,
    BrowserState,
)
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup
from stealth_chrome_devtools_mcp.settings import get_settings

IID = "iid-9479"
#: A spawn that waited on the exchange would sit for ``PREF_TIMEOUT_SECONDS``; one
#: that does not returns in a few turns of the loop. Generous for a loaded runner,
#: and far short of the exchange's own bound, so the test can tell them apart.
SPAWN_BOUND = 5.0
#: What the file-trust window and the exchange's own bound are shrunk to.
SHORT = 0.2


async def _never(*_args, **_kwargs):
    await asyncio.Event().wait()


def _write_pref(profile, file_name: str, value: object) -> None:
    default = profile / "Default"
    default.mkdir(parents=True, exist_ok=True)
    (default / file_name).write_text(
        json.dumps({"session": {"restore_on_startup": value}}), encoding="utf-8"
    )


class _SettingsTab(FakeTab):
    """The settings page: answers its pref reads and writes."""

    def __init__(self, pref: object) -> None:
        super().__init__(url="about:blank", target_id="T-settings")
        self.pref = pref
        self.closed = False

    async def evaluate(self, script: str, await_promise: bool = False):
        if script == login_persistence._READY:
            return True
        if "setPref" in script:
            self.pref = login_persistence.RESTORE_CONTINUE
            return True
        return self.pref

    async def get(self, url: str):
        return None

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def opt_out(monkeypatch):
    def _set() -> None:
        monkeypatch.setenv("STEALTH_MCP_NO_PERSIST_SESSION_COOKIES", "1")
        get_settings.cache_clear()

    yield _set
    monkeypatch.undo()
    get_settings.cache_clear()


def _named(profile) -> BrowserOptions:
    return BrowserOptions(user_data_dir=str(profile), auto_clone=False)


def _state(browser) -> login_persistence._Pending | None:
    state = getattr(browser, login_persistence._STATE_ATTR, None)
    return state if isinstance(state, login_persistence._Pending) else None


async def _stop(browser) -> None:
    state = _state(browser)
    if state is not None and not state.task.done():
        state.task.cancel()
        await asyncio.wait({state.task}, timeout=1.0)


class TestTheFileIsBelievedOnlyOnceChromeHadTimeToContradictIt:
    @pytest.fixture(autouse=True)
    def short_window(self, monkeypatch):
        monkeypatch.setattr(login_persistence, "FILE_SETTLE_SECONDS", SHORT)

    @pytest.fixture
    def no_tab(self, monkeypatch):
        async def refuse(*_args, **_kwargs):
            raise AssertionError("a settings tab was opened for a trusted file")

        monkeypatch.setattr(tab_open, "open_tab", refuse)

    @pytest.mark.parametrize("file_name", ["Secure Preferences", "Preferences"])
    async def test_a_file_that_still_says_one_after_the_window_is_believed(
        self, tmp_path, no_tab, file_name
    ):
        _write_pref(tmp_path, file_name, 1)
        browser = FakeBrowser()
        login_persistence.start_session_restore(browser, _named(tmp_path))
        task = _state(browser).task
        assert not task.done(), "the file was believed before Chrome had its say"
        assert await asyncio.wait_for(task, SPAWN_BOUND) is True
        assert browser.opened == []

    async def test_a_key_chrome_removed_meanwhile_means_the_exchange(self, tmp_path):
        """The reviewer's case: a copy whose MAC Chrome rejects says 1 until Chrome
        rewrites the file, then the key is gone, and the live pref was 5 all along."""
        _write_pref(tmp_path, "Secure Preferences", 1)
        tab = _SettingsTab(pref=5)
        browser = FakeBrowser(opened_tab=tab)
        login_persistence.start_session_restore(browser, _named(tmp_path))
        await asyncio.sleep(SHORT / 4)
        (tmp_path / "Default" / "Secure Preferences").write_text("{}")
        assert await asyncio.wait_for(_state(browser).task, SPAWN_BOUND) is True
        assert browser.opened == ["about:blank"]
        assert tab.pref == login_persistence.RESTORE_CONTINUE
        assert tab.closed

    async def test_a_close_before_the_window_runs_the_exchange_at_once(
        self, tmp_path, monkeypatch
    ):
        """The file is not evidence yet, and the close must not wait the window out."""
        monkeypatch.setattr(login_persistence, "FILE_SETTLE_SECONDS", 60.0)
        _write_pref(tmp_path, "Secure Preferences", 1)
        tab = _SettingsTab(pref=5)
        browser = FakeBrowser(opened_tab=tab)
        login_persistence.start_session_restore(browser, _named(tmp_path))
        await asyncio.sleep(0)
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(
            login_persistence.settle_session_restore(browser), SPAWN_BOUND
        )
        assert asyncio.get_running_loop().time() - started < 30.0
        assert browser.opened == ["about:blank"]
        assert tab.pref == login_persistence.RESTORE_CONTINUE

    @pytest.mark.parametrize(
        "write",
        [
            pytest.param(lambda d: None, id="no-file"),
            pytest.param(
                lambda d: _write_pref(d, "Secure Preferences", 5), id="other-value"
            ),
            pytest.param(
                lambda d: (
                    (d / "Default").mkdir(),
                    (d / "Default" / "Secure Preferences").write_text("{not json"),
                ),
                id="corrupt",
            ),
            pytest.param(
                lambda d: (
                    (d / "Default").mkdir(),
                    (d / "Default" / "Secure Preferences").write_text("[]"),
                ),
                id="wrong-shape",
            ),
        ],
    )
    async def test_anything_else_is_the_exchange(self, tmp_path, write):
        write(tmp_path)
        tab = _SettingsTab(pref=5)
        browser = FakeBrowser(opened_tab=tab)
        login_persistence.start_session_restore(browser, _named(tmp_path))
        assert await asyncio.wait_for(_state(browser).task, SPAWN_BOUND) is True
        assert browser.opened == ["about:blank"]
        assert tab.pref == login_persistence.RESTORE_CONTINUE
        assert tab.closed

    async def test_the_opt_out_touches_nothing_and_waits_for_nothing(
        self, tmp_path, opt_out, no_tab
    ):
        opt_out()
        _write_pref(tmp_path, "Secure Preferences", 1)
        browser = FakeBrowser()
        login_persistence.start_session_restore(browser, _named(tmp_path))
        assert await asyncio.wait_for(_state(browser).task, SPAWN_BOUND) is False
        assert browser.opened == []

    def test_the_opt_out_reads_the_file_chrome_really_writes(self, tmp_path, opt_out):
        """``remove_saved_tabs`` asked ``Preferences`` only, which on Windows never
        holds the key, so the opt-out never cleaned a profile whose pref was 1."""
        opt_out()
        (tmp_path / "Default" / "Sessions").mkdir(parents=True)
        _write_pref(tmp_path, "Secure Preferences", 1)
        assert login_persistence.remove_saved_tabs(str(tmp_path)) == ["Sessions"]


@pytest.fixture
def launched(monkeypatch) -> FakeBrowser:
    """A browser for the REAL ``spawn_browser``; only what needs a real Chrome is
    stubbed (the shape of ``test_popup_arming``'s fixture)."""
    monkeypatch.setattr(process_cleanup, "track_browser_process", lambda *a, **kw: None)

    async def no_measure(tab, options):
        return {"actual": None}

    async def no_reconcile(tab, executable):
        return None

    async def no_hooks(self, tab, instance_id):
        return True

    main = FakeTab(target_id="T-main")
    browser = FakeBrowser(main_tab=main)

    async def launch(self, *args, **kwargs):
        return browser

    async def first_page(_browser):
        return main

    monkeypatch.setattr(window_sizing, "apply_and_measure", no_measure)
    monkeypatch.setattr(
        browser_manager, "reconcile_launched_browser_version", no_reconcile
    )
    monkeypatch.setattr(
        BrowserManager, "_resolve_launch_args", lambda *a, **k: ([], "chrome", [])
    )
    monkeypatch.setattr(BrowserManager, "_launch_browser", launch)
    monkeypatch.setattr(BrowserManager, "_setup_dynamic_hooks", no_hooks)
    monkeypatch.setattr(browser_connect, "first_page", first_page)
    monkeypatch.setattr(process_exit, "browser_is_alive", lambda _browser: True)
    return browser


class TestANamedSpawnDoesNotWaitOnTheExchange:
    async def test_it_returns_while_the_settings_tab_never_opens(
        self, launched, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(tab_open, "open_tab", _never)
        try:
            await asyncio.wait_for(
                BrowserManager().spawn_browser(_named(tmp_path), instance_id=IID),
                SPAWN_BOUND,
            )
            state = _state(launched)
            assert state is not None, "the exchange was not started at all"
            await asyncio.sleep(0)
            assert not state.task.done(), (
                "it is still waiting on a tab that never opens"
            )
        finally:
            await _stop(launched)

    @pytest.mark.parametrize(
        "options",
        [
            BrowserOptions(auto_clone=True, user_data_dir="clone"),
            BrowserOptions(),
        ],
        ids=["auto-clone", "unnamed"],
    )
    async def test_a_clone_and_an_unnamed_spawn_start_none(
        self, launched, monkeypatch, tmp_path, options
    ):
        monkeypatch.setattr(tab_open, "open_tab", _never)
        if options.user_data_dir:
            options = options.model_copy(update={"user_data_dir": str(tmp_path)})
        await asyncio.wait_for(
            BrowserManager().spawn_browser(options, instance_id=IID), SPAWN_BOUND
        )
        assert _state(launched) is None
        assert launched.opened == []

    async def test_a_named_profile_that_says_one_opens_nothing(
        self, launched, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(login_persistence, "FILE_SETTLE_SECONDS", SHORT)
        _write_pref(tmp_path, "Secure Preferences", 1)
        monkeypatch.setattr(tab_open, "open_tab", _never)
        await asyncio.wait_for(
            BrowserManager().spawn_browser(_named(tmp_path), instance_id=IID),
            SPAWN_BOUND,
        )
        assert await asyncio.wait_for(_state(launched).task, SPAWN_BOUND) is True
        assert launched.opened == []


class _Closing:
    """A browser the REAL ``close_instance`` can end, recording when its
    ``Browser.close`` goes out so a test can order it against the exchange."""

    def __init__(self, log: list[str], returncode: int | None = 0) -> None:
        self.tabs: list = []
        # 0: already gone, so the close does not wait on a pid that is not there.
        self._process = SimpleNamespace(
            returncode=returncode, pid=99999, terminate=MagicMock(), kill=MagicMock()
        )
        self._process_pid = 99999
        self.log = log

    async def send(self, _command):
        self.log.append("browser.close sent")

    async def aclose(self) -> None:
        self.log.append("aclose")

    def stop(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_real_process_work(monkeypatch):
    from stealth_chrome_devtools_mcp.embedded.in_memory_storage import (
        in_memory_storage,
    )

    monkeypatch.setattr(process_cleanup, "kill_browser_process", MagicMock())
    monkeypatch.setattr(process_cleanup, "finalize_browser_process", MagicMock())
    monkeypatch.setattr(process_cleanup, "cleanup_deferred_profiles", MagicMock())
    monkeypatch.setattr(in_memory_storage, "remove_instance", MagicMock())


def _seed(manager: BrowserManager, browser, instance_id: str = IID) -> None:
    manager._instances[instance_id] = {
        "browser": browser,
        "instance": BrowserInstance(instance_id=instance_id, state=BrowserState.READY),
    }
    manager._spawn_diagnostics[instance_id] = {}


def _exchange(browser: _Closing, log: list[str], finish: asyncio.Event) -> asyncio.Task:
    async def run() -> bool:
        try:
            await finish.wait()
            log.append("exchange done")
            return True
        except asyncio.CancelledError:
            log.append("exchange cancelled")
            raise

    task = asyncio.get_running_loop().create_task(run())
    setattr(
        browser,
        login_persistence._STATE_ATTR,
        login_persistence._Pending(task, asyncio.Event()),
    )
    return task


class TestACloseWaitsForAPendingExchange:
    @pytest.fixture
    def short_bound(self, monkeypatch):
        monkeypatch.setattr(login_persistence, "PREF_TIMEOUT_SECONDS", SHORT)
        monkeypatch.setattr(login_persistence, "_CANCEL_GRACE_SECONDS", SHORT / 2)

    async def test_the_exchange_finishes_before_browser_close_is_sent(self):
        log: list[str] = []
        browser = _Closing(log)
        manager = BrowserManager()
        _seed(manager, browser)
        finish = asyncio.Event()
        _exchange(browser, log, finish)

        closing = asyncio.create_task(manager.close_instance(IID))
        await asyncio.sleep(0.05)
        assert not closing.done(), "the close did not wait for the pending exchange"
        assert getattr(browser, login_persistence._STATE_ATTR).closing.is_set(), (
            "the exchange was not told the close came"
        )
        assert log == [], "the browser was touched while its pref was being set"
        assert not tab_open.is_closed(browser), "the link was cut under the exchange"
        finish.set()
        assert await asyncio.wait_for(closing, SPAWN_BOUND) is True
        assert log[0] == "exchange done"
        assert "browser.close sent" in log

    async def test_a_hung_exchange_is_cancelled_and_the_close_goes_on(
        self, short_bound
    ):
        log: list[str] = []
        browser = _Closing(log)
        manager = BrowserManager()
        _seed(manager, browser)
        task = _exchange(browser, log, asyncio.Event())

        assert await asyncio.wait_for(manager.close_instance(IID), SPAWN_BOUND) is True
        assert task.cancelled()
        assert log[0] == "exchange cancelled"
        assert "browser.close sent" in log

    async def test_shutdown_cancels_a_pending_exchange(self, short_bound):
        """``close_all`` is what the stdio server's shutdown calls."""
        log: list[str] = []
        browser = _Closing(log)
        manager = BrowserManager()
        _seed(manager, browser)
        task = _exchange(browser, log, asyncio.Event())

        await asyncio.wait_for(manager.close_all(), SPAWN_BOUND)
        assert task.cancelled()
        assert manager._instances == {}

    async def test_it_waits_as_long_as_the_exchange_may_take_and_no_longer(
        self, monkeypatch
    ):
        """Not a separate, shorter number: v2.1.49's starvation was 17 s."""
        waited: list[float | None] = []
        real_wait = asyncio.wait

        async def spy(fs, *, timeout=None, **kwargs):
            waited.append(timeout)
            return await real_wait(fs, timeout=timeout, **kwargs)

        monkeypatch.setattr(login_persistence.asyncio, "wait", spy)
        log: list[str] = []
        browser = _Closing(log)
        finish = asyncio.Event()
        task = _exchange(browser, log, finish)
        settling = asyncio.create_task(
            login_persistence.settle_session_restore(browser)
        )
        await asyncio.sleep(0.02)
        finish.set()
        await asyncio.wait_for(settling, SPAWN_BOUND)
        assert task.done()
        assert waited[0] == (
            login_persistence.PREF_TIMEOUT_SECONDS
            + login_persistence._CANCEL_GRACE_SECONDS
        )
        assert waited[0] >= 17.0

    async def test_a_close_cancelled_while_it_waits_still_ends_the_browser(self):
        log: list[str] = []
        browser = _Closing(log, returncode=None)
        manager = BrowserManager()
        _seed(manager, browser)
        task = _exchange(browser, log, asyncio.Event())

        closing = asyncio.create_task(manager.close_instance(IID))
        await asyncio.sleep(0.05)
        assert not closing.done()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await asyncio.sleep(0)
        assert task.cancelled(), "the exchange outlived its browser"
        assert tab_open.is_closed(browser), (
            "the connection was left open for a lingerer"
        )
        assert browser._process.terminate.called, "the claimed browser was orphaned"

    async def test_a_cancelled_exchange_still_closes_its_settings_tab(
        self, monkeypatch
    ):
        """The tab is closed on the held-back share, not on whatever the deadline
        has left: a cancelled close must not wait a minute for it."""
        tab = _SettingsTab(pref=5)
        browser = FakeBrowser(opened_tab=tab)

        async def stuck(self, script: str, await_promise: bool = False):
            await asyncio.Event().wait()

        tab.evaluate = stuck.__get__(tab)
        granted: list[float] = []
        real_timeout = asyncio.timeout

        def spy(delay):
            granted.append(delay)
            return real_timeout(delay)

        monkeypatch.setattr(login_persistence.asyncio, "timeout", spy)
        task = asyncio.create_task(login_persistence.ensure_session_restore(browser))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if browser.opened:
                break
        task.cancel()
        await asyncio.wait({task}, timeout=SPAWN_BOUND)
        assert task.cancelled()
        assert tab.closed
        assert granted[-1] == pytest.approx(
            login_persistence.PREF_TIMEOUT_SECONDS * login_persistence.TAB_CLOSE_SHARE
        )

    async def test_with_nothing_pending_it_is_a_no_op(self):
        await login_persistence.settle_session_restore(_Closing([]))
        await login_persistence.settle_session_restore(MagicMock())


TRACKED = {"target_id": "T-main", "url": "https://fake.test/a", "title": "a"}
SETTINGS = {"target_id": "T-settings", "url": "chrome://settings/", "title": "s"}


class TestTheExchangesTabIsNeverWhereAClosedTabFallsBackTo:
    @pytest.fixture
    def manager_and_browser(self):
        browser = FakeBrowser(
            tabs=[
                FakeDiscoveredTarget(fake_target(**TRACKED)),
                FakeDiscoveredTarget(fake_target(**SETTINGS)),
            ]
        )
        manager = BrowserManager()
        manager._instances[IID] = {
            "browser": browser,
            "tab": FakeAttachedTab(fake_target(**TRACKED)),
            "instance": fake_instance(IID),
            "navigation_count": 0,
        }
        return manager, browser

    async def test_closing_the_main_tab_leaves_no_tab_not_the_settings_one(
        self, manager_and_browser
    ):
        manager, browser = manager_and_browser
        tab_open.mark_internal(browser, browser.tabs[1])

        assert await manager.close_tab(IID, TRACKED["target_id"]) is True

        assert await manager.get_active_tab(IID) is None

    async def test_a_page_tab_still_wins_over_it(self, manager_and_browser):
        manager, browser = manager_and_browser
        page = FakeDiscoveredTarget(
            fake_target(target_id="T-page", url="https://fake.test/p", title="p")
        )
        browser.tabs.insert(1, page)
        tab_open.mark_internal(browser, browser.tabs[2])

        assert await manager.close_tab(IID, TRACKED["target_id"]) is True

        assert manager._get_tab_target_id(await manager.get_active_tab(IID)) == "T-page"

    async def test_without_the_mark_it_would_have(self, manager_and_browser):
        """The mark is what decides, not the URL."""
        manager, browser = manager_and_browser

        assert await manager.close_tab(IID, TRACKED["target_id"]) is True

        active = await manager.get_active_tab(IID)
        assert manager._get_tab_target_id(active) == SETTINGS["target_id"]

    async def test_the_exchange_marks_the_tab_it_opens(self):
        tab = _SettingsTab(pref=1)
        browser = FakeBrowser(opened_tab=tab)
        await login_persistence.ensure_session_restore(browser)
        assert str(tab.target.target_id) in tab_open._state(browser).internal


async def test_the_exchange_opens_its_tab_behind_the_callers_page():
    """A tab opened in front covers the caller's page, and a headless page stops
    reporting its window while it is covered: ``window.outerHeight`` fell from the
    requested 600 to the viewport's 513 on Linux CI (``test_window_sizing``, run
    40552975223), which the exchange's tab caused for as long as it was in front.
    The exchange is the backend's own business and asks for its tab behind."""
    tab = _SettingsTab(pref=1)
    browser = FakeBrowser(opened_tab=tab)

    await login_persistence.ensure_session_restore(browser)

    assert browser.cdp_frames[0]["method"] == "Target.createTarget"
    assert browser.cdp_frames[0]["params"]["background"] is True
