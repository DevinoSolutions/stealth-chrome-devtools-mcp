"""Behavioral tests for platform_utils — stealth arg filtering + arg merging.

filter_stealth_args is a core product guarantee: user-supplied Chrome flags that
would unmask automation must be stripped (by prefix), while benign flags pass
through. merge_browser_args must still add the sandbox flags a root/container
environment needs to launch at all, even though those same flags are otherwise
stealth-blocked. Sandbox detection is monkeypatched so the tests are
deterministic across CI environments.
"""

import subprocess

import pytest

from stealth_chrome_devtools_mcp.embedded import platform_utils
from stealth_chrome_devtools_mcp.embedded.platform_utils import (
    build_reduced_user_agent,
    filter_stealth_args,
    get_platform_info,
    get_required_sandbox_args,
    merge_browser_args,
    resolve_browser_major_version,
)


@pytest.fixture(autouse=True)
def _no_default_user_agent(monkeypatch):
    """Neutralize the F-770 masked-UA default for the pre-existing arg tests.

    ``merge_browser_args`` now also supplies a default ``--user-agent=`` derived
    from the resolved browser, which would make these assertions depend on
    whether the machine running them happens to have Chrome installed. The
    F-770 behaviour has its own tests below, which opt back in explicitly.
    """
    monkeypatch.setattr(platform_utils, "check_browser_executable", lambda: None)


class TestFilterStealthArgs:
    def test_known_automation_flags_are_stripped(self):
        clean, warnings = filter_stealth_args(
            ["--enable-automation", "--headless", "--window-size=1920,1080"]
        )
        assert clean == ["--window-size=1920,1080"]
        assert len(warnings) == 2
        assert all("stripped:" in w for w in warnings)

    def test_prefix_match_catches_variants(self):
        # startswith matching: these share a blocked prefix.
        clean, _ = filter_stealth_args(
            ["--disable-gpu-sandbox", "--remote-debugging-port=9222"]
        )
        assert clean == []

    def test_benign_flags_pass_through_without_warnings(self):
        clean, warnings = filter_stealth_args(["--lang=en-US", "--window-position=0,0"])
        assert clean == ["--lang=en-US", "--window-position=0,0"]
        assert warnings == []

    def test_matching_is_case_insensitive(self):
        clean, warnings = filter_stealth_args(["--HEADLESS"])
        assert clean == [] and len(warnings) == 1

    def test_empty_input(self):
        assert filter_stealth_args([]) == ([], [])


class TestRequiredSandboxArgs:
    def test_none_needed_on_normal_host(self, monkeypatch):
        monkeypatch.setattr(platform_utils, "is_running_as_root", lambda: False)
        monkeypatch.setattr(platform_utils, "is_running_in_container", lambda: False)
        assert get_required_sandbox_args() == []

    def test_root_requires_no_sandbox_deduped(self, monkeypatch):
        monkeypatch.setattr(platform_utils, "is_running_as_root", lambda: True)
        monkeypatch.setattr(platform_utils, "is_running_in_container", lambda: False)
        args = get_required_sandbox_args()
        assert "--no-sandbox" in args
        assert len(args) == len(set(args)), "required args must be de-duplicated"

    def test_container_adds_shm_and_gpu_flags(self, monkeypatch):
        monkeypatch.setattr(platform_utils, "is_running_as_root", lambda: False)
        monkeypatch.setattr(platform_utils, "is_running_in_container", lambda: True)
        args = get_required_sandbox_args()
        assert "--disable-dev-shm-usage" in args and "--no-sandbox" in args


class TestMergeBrowserArgs:
    def test_strips_stealth_flags_but_keeps_benign(self, monkeypatch):
        monkeypatch.setattr(platform_utils, "is_running_as_root", lambda: False)
        monkeypatch.setattr(platform_utils, "is_running_in_container", lambda: False)
        combined, warnings = merge_browser_args(["--headless", "--lang=en-US"])
        assert combined == ["--lang=en-US"]
        assert len(warnings) == 1

    def test_root_sandbox_flags_added_even_though_stealth_blocked(self, monkeypatch):
        # --no-sandbox is stealth-blocked, but a root env needs it to launch, so
        # merge must add it back after filtering. This is the override that keeps
        # the browser runnable at all in CI/containers.
        monkeypatch.setattr(platform_utils, "is_running_as_root", lambda: True)
        monkeypatch.setattr(platform_utils, "is_running_in_container", lambda: False)
        combined, _ = merge_browser_args(["--foo"])
        assert "--foo" in combined
        assert "--no-sandbox" in combined
        assert combined.count("--no-sandbox") == 1


class TestPlatformInfo:
    def test_reports_expected_keys(self):
        info = get_platform_info()
        for key in ("system", "is_root", "is_container", "required_sandbox_args"):
            assert key in info


# ---------------------------------------------------------------------------
# F-770 — the masked default User-Agent (plan_RELEASE_FIX_D D1).
#
# Headless Chrome advertises `HeadlessChrome/<major>.0.0.0` in its own UA, which
# is the cheapest server-side bot check there is. The product now supplies a
# masked default `--user-agent=` built from Chrome's frozen reduced-UA form. The
# three platform tokens below are the exact strings measured on the three
# qualified runners (D0), so a table typo cannot pass silently: a wrong token
# would make the masked UA disagree with the real OS, which is a WORSE tell than
# the headless token it replaces.
# ---------------------------------------------------------------------------
_REDUCED_UA_CELLS = [
    ("Windows", "Windows NT 10.0; Win64; x64"),
    ("Darwin", "Macintosh; Intel Mac OS X 10_15_7"),
    ("Linux", "X11; Linux x86_64"),
]


@pytest.fixture(autouse=True)
def _clear_version_cache():
    """``resolve_browser_major_version`` is lru_cached (it runs on the spawn
    path); clear it around every test so a fake never bleeds between them."""
    resolve_browser_major_version.cache_clear()
    yield
    resolve_browser_major_version.cache_clear()


class TestReducedUserAgent:
    @pytest.mark.parametrize(("system", "token"), _REDUCED_UA_CELLS)
    def test_masked_ua_matches_the_measured_platform_token(
        self, monkeypatch, system, token
    ):
        monkeypatch.setattr(platform_utils.platform, "system", lambda: system)
        monkeypatch.setattr(
            platform_utils, "resolve_browser_major_version", lambda _exe: "150"
        )
        agent = build_reduced_user_agent("/opt/google/chrome/chrome")
        assert agent == (
            f"Mozilla/5.0 ({token}) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Safari/537.36"
        )
        assert "Headless" not in agent

    def test_edge_keeps_its_own_token(self, monkeypatch):
        # Dropping Edg/ while sec-ch-ua still says "Microsoft Edge" would be a
        # sharper tell than the headless token being masked.
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            platform_utils, "resolve_browser_major_version", lambda _exe: "150"
        )
        agent = build_reduced_user_agent("/usr/bin/microsoft-edge-stable")
        assert agent.endswith("Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0")

    def test_unknown_platform_declines_to_mask(self, monkeypatch):
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "FreeBSD")
        assert build_reduced_user_agent("/usr/local/bin/chrome") is None

    def test_unresolvable_version_declines_to_mask(self, monkeypatch):
        # Masking with a wrong version is worse than not masking: the UA would
        # contradict sec-ch-ua.
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            platform_utils, "resolve_browser_major_version", lambda _exe: None
        )
        assert build_reduced_user_agent("/usr/bin/google-chrome") is None


class TestResolveBrowserMajorVersion:
    def test_windows_reads_the_version_named_sibling_directory(
        self, monkeypatch, tmp_path
    ):
        # Windows must NOT shell out: `chrome.exe --version` hands the flag to a
        # running Chrome instead of printing anything.
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Windows")

        def _explode(*_args, **_kwargs):
            raise AssertionError("must not run a subprocess on Windows")

        monkeypatch.setattr(platform_utils.subprocess, "run", _explode)
        (tmp_path / "150.0.7871.186").mkdir()
        (tmp_path / "149.0.7000.1").mkdir()
        (tmp_path / "SetupMetrics").mkdir()
        assert resolve_browser_major_version(str(tmp_path / "chrome.exe")) == "150"

    def test_posix_parses_the_version_subprocess(self, monkeypatch):
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            platform_utils.subprocess,
            "run",
            lambda *_a, **_k: subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Google Chrome 150.0.7871.186 \n"
            ),
        )
        assert resolve_browser_major_version("/usr/bin/google-chrome") == "150"

    def test_posix_probe_failure_is_not_fatal(self, monkeypatch):
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Linux")

        def _boom(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd="chrome", timeout=10)

        monkeypatch.setattr(platform_utils.subprocess, "run", _boom)
        assert resolve_browser_major_version("/usr/bin/google-chrome") is None


class TestDefaultUserAgentInMergeBrowserArgs:
    def _arm(self, monkeypatch):
        monkeypatch.setattr(platform_utils, "is_running_as_root", lambda: False)
        monkeypatch.setattr(platform_utils, "is_running_in_container", lambda: False)
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            platform_utils, "check_browser_executable", lambda: "/usr/bin/google-chrome"
        )
        monkeypatch.setattr(
            platform_utils, "resolve_browser_major_version", lambda _exe: "150"
        )

    def test_default_spawn_gets_a_masked_user_agent(self, monkeypatch):
        self._arm(monkeypatch)
        combined, _ = merge_browser_args([])
        assert combined == [
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        ]

    def test_explicit_user_agent_always_wins(self, monkeypatch):
        self._arm(monkeypatch)
        combined, _ = merge_browser_args(["--user-agent=CallerChose/1.0"])
        assert combined == ["--user-agent=CallerChose/1.0"]

    def test_no_browser_means_no_masking(self, monkeypatch):
        self._arm(monkeypatch)
        monkeypatch.setattr(platform_utils, "check_browser_executable", lambda: None)
        combined, _ = merge_browser_args(["--lang=en-US"])
        assert combined == ["--lang=en-US"]
