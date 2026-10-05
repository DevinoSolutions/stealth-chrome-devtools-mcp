"""F-937 — a login survives a browser close.

Two causes, one module (``embedded/login_persistence.py``): session cookies are
dropped unless ``session.restore_on_startup`` is 1, and Google's Device Bound
Session Credentials sign the account out at every relaunch. These tests pin the
pieces that are decidable without Chrome — the merged last
``--disable-features`` switch, the pref setter against a mocked CDP surface, the
saved-tabs removal and its exclusion from profile copies, and the opt-outs. The
behavior against a real Chrome is ``tests/test_e2e_login_persistence.py``.
"""

import asyncio
import json

import nodriver as uc
import pytest

from stealth_chrome_devtools_mcp.embedded import login_persistence, profile_copy
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions
from stealth_chrome_devtools_mcp.settings import get_settings


@pytest.fixture
def opt_out(monkeypatch):
    def _set(name: str) -> None:
        monkeypatch.setenv(name, "1")
        get_settings.cache_clear()

    yield _set
    monkeypatch.undo()
    get_settings.cache_clear()


def _disable_features(args: list[str]) -> list[str]:
    return [a for a in args if a.lower().startswith("--disable-features=")]


def _tokens(arg: str) -> list[str]:
    return arg.split("=", 1)[1].split(",")


class TestDisableFeaturesMerge:
    def test_one_switch_with_nodriver_defaults_and_every_dbsc_feature(self):
        merged = login_persistence.disable_dbsc(["--lang=en-US"])
        (switch,) = _disable_features(merged)
        assert _tokens(switch) == [
            *login_persistence.NODRIVER_DISABLED_FEATURES,
            *login_persistence.DBSC_FEATURES,
        ]
        assert merged[0] == "--lang=en-US"
        assert merged[-1] == switch, "the last switch is the one Chrome keeps"

    def test_a_caller_switch_is_merged_not_duplicated(self):
        merged = login_persistence.disable_dbsc(
            ["--disable-features=Translate,IsolateOrigins", "--lang=en-US"]
        )
        (switch,) = _disable_features(merged)
        tokens = _tokens(switch)
        assert tokens[:3] == ["IsolateOrigins", "site-per-process", "Translate"]
        assert len(tokens) == len(set(tokens)), "a feature is named once"
        assert set(login_persistence.DBSC_FEATURES) <= set(tokens)
        assert "--lang=en-US" in merged

    def test_several_caller_switches_collapse_into_one(self):
        merged = login_persistence.disable_dbsc(
            ["--disable-features=A", "--Disable-Features=B,C"]
        )
        (switch,) = _disable_features(merged)
        assert {"A", "B", "C"} <= set(_tokens(switch))

    def test_the_nodriver_defaults_are_the_ones_nodriver_emits(self):
        """The merged switch replaces nodriver's, so it must be a superset."""
        emitted = [
            a
            for a in uc.Config(user_data_dir="x")()
            if a.startswith("--disable-features=")
        ]
        assert [_tokens(a) for a in emitted] == [
            list(login_persistence.NODRIVER_DISABLED_FEATURES)
        ]

    def test_nodriver_keeps_the_merged_switch_last(self):
        config = uc.Config(
            user_data_dir="x",
            browser_args=login_persistence.disable_dbsc(["about:blank"]),
        )
        switches = _disable_features(config())
        assert len(switches) == 2, "nodriver adds its own ahead of ours"
        assert set(login_persistence.DBSC_FEATURES) <= set(_tokens(switches[-1]))
        assert set(_tokens(switches[0])) <= set(_tokens(switches[-1]))

    def test_opt_out_leaves_the_args_alone(self, opt_out):
        opt_out("STEALTH_MCP_NO_DISABLE_DBSC")
        args = ["--disable-features=Translate", "about:blank"]
        assert login_persistence.disable_dbsc(args) == args

    def test_the_spawn_path_applies_it(self, monkeypatch):
        monkeypatch.setattr(
            "stealth_chrome_devtools_mcp.embedded.browser_manager."
            "check_browser_executable",
            lambda: "/usr/bin/chromium",
        )
        manager = BrowserManager()
        launch_args, _exe, _warn = manager._resolve_launch_args(
            BrowserOptions(browser_args=["--disable-features=Translate"]),
            None,
            {"system": "Linux", "is_root": False, "is_container": False},
        )
        (switch,) = _disable_features(launch_args)
        assert {"Translate", *login_persistence.DBSC_FEATURES} <= set(_tokens(switch))


class FakeTab:
    def __init__(self, pref: object, accepts: bool = True) -> None:
        self.pref = pref
        self.accepts = accepts
        self.scripts: list[str] = []
        self.closed = False
        self.navigated: list[str] = []

    async def evaluate(self, script: str, await_promise: bool = False):
        if script == login_persistence._READY:
            return True
        assert await_promise, "both calls are promises"
        self.scripts.append(script)
        if "setPref" in script:
            if self.accepts:
                self.pref = login_persistence.RESTORE_CONTINUE
            return self.accepts
        return self.pref

    async def get(self, url: str):
        self.navigated.append(url)

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, tab: FakeTab) -> None:
        self.tab = tab
        self.opened: list[tuple[str, bool]] = []

    async def get(self, url: str, new_tab: bool = False):
        self.opened.append((url, new_tab))
        return self.tab


class TestSessionRestorePref:
    async def test_sets_the_pref_and_closes_the_settings_tab(self):
        tab = FakeTab(pref=5)
        browser = FakeBrowser(tab)
        assert await login_persistence.ensure_session_restore(browser) is True
        assert browser.opened == [("about:blank", True)]
        assert tab.navigated == ["chrome://settings"]
        assert tab.pref == 1
        assert [("setPref" in s) for s in tab.scripts] == [False, True, False]
        assert "'session.restore_on_startup', 1, ''" in tab.scripts[1]
        assert tab.closed

    async def test_is_idempotent_when_the_pref_is_already_set(self):
        tab = FakeTab(pref=1)
        assert await login_persistence.ensure_session_restore(FakeBrowser(tab)) is True
        assert not any("setPref" in s for s in tab.scripts)
        assert tab.closed

    async def test_reports_false_when_chrome_refuses_the_write(self):
        tab = FakeTab(pref=5, accepts=False)
        assert await login_persistence.ensure_session_restore(FakeBrowser(tab)) is False
        assert tab.closed

    async def test_never_raises_and_still_closes_the_tab(self, monkeypatch):
        monkeypatch.setattr(login_persistence, "PREF_TIMEOUT_SECONDS", 0.2)
        monkeypatch.setattr(login_persistence, "_READY_POLL_SECONDS", 0.01)

        class Exploding(FakeTab):
            async def evaluate(self, script, await_promise=False):
                raise RuntimeError("settingsPrivate is not defined")

        tab = Exploding(pref=5)
        assert await login_persistence.ensure_session_restore(FakeBrowser(tab)) is False
        assert tab.closed

    async def test_a_settings_page_that_will_not_open_is_not_fatal(self):
        class NoTab:
            async def get(self, url, new_tab=False):
                raise ConnectionError("no target")

        assert await login_persistence.ensure_session_restore(NoTab()) is False

    async def test_opt_out_touches_nothing(self, opt_out):
        opt_out("STEALTH_MCP_NO_PERSIST_SESSION_COOKIES")
        browser = FakeBrowser(FakeTab(pref=5))
        assert await login_persistence.ensure_session_restore(browser) is False
        assert browser.opened == []


def _profile(root, *names: str):
    for name in names:
        directory = root / "Default" / name
        directory.mkdir(parents=True)
        (directory / "Session_1").write_text("tabs")
    (root / "Default" / "Cookies").write_text("login")
    return root


class TestSavedTabs:
    def test_removes_both_saved_tab_directories_and_keeps_the_login(self, tmp_path):
        profile = _profile(tmp_path, "Sessions", "Sessions_Encrypted", "Local Storage")
        removed = login_persistence.remove_saved_tabs(str(profile))
        assert removed == ["Sessions", "Sessions_Encrypted"]
        assert not (profile / "Default" / "Sessions").exists()
        assert not (profile / "Default" / "Sessions_Encrypted").exists()
        assert (profile / "Default" / "Cookies").read_text() == "login"
        assert (profile / "Default" / "Local Storage").is_dir()

    def test_a_profile_without_them_and_a_missing_profile_are_fine(self, tmp_path):
        assert login_persistence.remove_saved_tabs(str(tmp_path)) == []
        assert login_persistence.remove_saved_tabs(str(tmp_path / "nope")) == []
        assert login_persistence.remove_saved_tabs(None) == []

    def test_opt_out_keeps_them(self, tmp_path, opt_out):
        opt_out("STEALTH_MCP_NO_PERSIST_SESSION_COOKIES")
        profile = _profile(tmp_path, "Sessions")
        assert login_persistence.remove_saved_tabs(str(profile)) == []
        assert (profile / "Default" / "Sessions").is_dir()

    def test_a_copy_does_not_carry_them(self):
        assert profile_copy.ignore_names(
            ["Sessions", "Sessions_Encrypted", "Cookies"]
        ) == {
            "Sessions",
            "Sessions_Encrypted",
        }

    def test_the_copy_walk_leaves_them_behind(self, tmp_path):
        source = _profile(tmp_path / "src", "Sessions")
        target = tmp_path / "dst"
        profile_copy.copy_delta(source, target)
        assert (target / "Default" / "Cookies").read_text() == "login"
        assert not (target / "Default" / "Sessions").exists()

    async def test_the_spawn_path_removes_them_for_a_persistent_profile_only(
        self, tmp_path, monkeypatch
    ):
        class StopHereError(Exception):
            pass

        def stop(_headless):
            raise StopHereError

        seen: list[str | None] = []
        monkeypatch.setattr(
            login_persistence, "remove_saved_tabs", lambda d: seen.append(d) or []
        )
        monkeypatch.setattr(
            "stealth_chrome_devtools_mcp.embedded.browser_manager."
            "desktop_launch.should_delegate",
            stop,
        )
        manager = BrowserManager()
        for options in (
            BrowserOptions(user_data_dir=str(tmp_path)),
            BrowserOptions(user_data_dir=str(tmp_path), auto_clone=True),
        ):
            with pytest.raises(StopHereError):
                await manager._launch_browser(options, "exe", [], object())
        assert seen == [str(tmp_path)]


class TestFinalArgv:
    def test_the_last_disable_features_is_the_one_chrome_keeps_and_loses_nothing(self):
        config = uc.Config(
            user_data_dir="x",
            browser_args=login_persistence.disable_dbsc(
                ["--disable-features=Translate", "about:blank"]
            ),
        )
        switches = _disable_features(config())
        effective = set(_tokens(switches[-1]))
        assert set(login_persistence.DBSC_FEATURES) <= effective
        assert set(login_persistence.NODRIVER_DISABLED_FEATURES) <= effective
        assert "Translate" in effective


class TestSessionRestoreBounded:
    async def test_an_evaluate_that_never_resolves_times_out_and_closes_the_tab(
        self, monkeypatch
    ):
        monkeypatch.setattr(login_persistence, "PREF_TIMEOUT_SECONDS", 0.05)

        class Hanging(FakeTab):
            async def evaluate(self, script, await_promise=False):
                await asyncio.Event().wait()

        tab = Hanging(pref=5)
        assert await login_persistence.ensure_session_restore(FakeBrowser(tab)) is False
        assert tab.closed

    async def test_a_page_that_never_opens_times_out(self, monkeypatch):
        monkeypatch.setattr(login_persistence, "PREF_TIMEOUT_SECONDS", 0.05)

        class NeverOpens:
            async def get(self, url, new_tab=False):
                await asyncio.Event().wait()

        assert await login_persistence.ensure_session_restore(NeverOpens()) is False

    async def test_a_tab_that_never_closes_does_not_hang_the_spawn(self, monkeypatch):
        monkeypatch.setattr(login_persistence, "PREF_TIMEOUT_SECONDS", 0.05)

        class StuckClose(FakeTab):
            async def close(self) -> None:
                await asyncio.Event().wait()

        assert (
            await login_persistence.ensure_session_restore(
                FakeBrowser(StuckClose(pref=1))
            )
            is True
        )


class TestSavedTabsGuards:
    def test_a_profile_held_by_a_live_process_keeps_its_saved_tabs(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            login_persistence.profile_lock,
            "profile_hold",
            lambda profile_dir, live_pids: login_persistence.profile_lock.Hold(
                1234, "held"
            ),
        )
        profile = _profile(tmp_path, "Sessions")
        assert login_persistence.remove_saved_tabs(str(profile)) == []
        assert (profile / "Default" / "Sessions").is_dir()

    def test_the_opt_out_still_cleans_a_profile_whose_pref_is_already_one(
        self, tmp_path, opt_out
    ):
        opt_out("STEALTH_MCP_NO_PERSIST_SESSION_COOKIES")
        profile = _profile(tmp_path, "Sessions", "Sessions_Encrypted")
        (profile / "Default" / "Preferences").write_text(
            json.dumps({"session": {"restore_on_startup": 1}})
        )
        assert login_persistence.remove_saved_tabs(str(profile)) == [
            "Sessions",
            "Sessions_Encrypted",
        ]

    def test_the_opt_out_keeps_them_when_the_pref_is_not_one(self, tmp_path, opt_out):
        opt_out("STEALTH_MCP_NO_PERSIST_SESSION_COOKIES")
        profile = _profile(tmp_path, "Sessions")
        (profile / "Default" / "Preferences").write_text(
            json.dumps({"session": {"restore_on_startup": 5}})
        )
        assert login_persistence.remove_saved_tabs(str(profile)) == []
        (profile / "Default" / "Preferences").write_text("not json")
        assert login_persistence.remove_saved_tabs(str(profile)) == []


class TestLateBindings:
    async def test_waits_for_the_settings_bindings_instead_of_failing(
        self, monkeypatch
    ):
        monkeypatch.setattr(login_persistence, "_READY_POLL_SECONDS", 0.01)

        class Late(FakeTab):
            polls = 0

            async def evaluate(self, script, await_promise=False):
                if script == login_persistence._READY:
                    self.polls += 1
                    return self.polls > 3
                return await super().evaluate(script, await_promise)

        tab = Late(pref=5)
        assert await login_persistence.ensure_session_restore(FakeBrowser(tab)) is True
        assert tab.polls == 4
        assert tab.pref == 1


def _signin_switches(args: list[str]) -> list[str]:
    return [a for a in args if a.lower().startswith("--allow-browser-signin")]


class TestBrowserSigninOff:
    def test_the_switch_is_on_by_default(self):
        out = login_persistence.block_browser_signin(["--foo", "about:blank"])
        assert _signin_switches(out) == ["--allow-browser-signin=false"]

    def test_opt_out_leaves_the_args_alone(self, opt_out):
        opt_out("STEALTH_MCP_ALLOW_BROWSER_SIGNIN")
        args = ["--foo"]
        assert login_persistence.block_browser_signin(args) == args

    @pytest.mark.parametrize(
        "given", ["--allow-browser-signin=true", "--allow-browser-signin=false"]
    )
    def test_a_callers_own_switch_wins_and_is_not_repeated(self, given):
        out = login_persistence.block_browser_signin([given, "--foo"])
        assert _signin_switches(out) == [given]

    def test_protect_logins_applies_both_and_the_dbsc_opt_out_is_independent(
        self, opt_out
    ):
        out = login_persistence.protect_logins(["about:blank"])
        assert _signin_switches(out) and _disable_features(out)
        opt_out("STEALTH_MCP_NO_DISABLE_DBSC")
        out = login_persistence.protect_logins(["about:blank"])
        assert _signin_switches(out) and not _disable_features(out)

    def test_the_spawn_path_passes_it_and_keeps_the_start_page_last(self, monkeypatch):
        monkeypatch.setattr(
            "stealth_chrome_devtools_mcp.embedded.browser_manager."
            "check_browser_executable",
            lambda: "/usr/bin/chromium",
        )
        launch_args, _exe, _warn = BrowserManager()._resolve_launch_args(
            BrowserOptions(),
            None,
            {"system": "Linux", "is_root": False, "is_container": False},
        )
        assert _signin_switches(launch_args) == ["--allow-browser-signin=false"]
        assert launch_args[-1] == "about:blank"

    def test_the_gating_features_of_a_saved_bound_session_are_listed(self):
        assert {
            "EnableBoundSessionCredentialsContinuity",
            "EnableChromeRefreshTokenBinding",
        } <= set(login_persistence.DBSC_FEATURES)
