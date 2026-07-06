"""Behavioral tests for platform_utils — stealth arg filtering + arg merging.

filter_stealth_args is a core product guarantee: user-supplied Chrome flags that
would unmask automation must be stripped (by prefix), while benign flags pass
through. merge_browser_args must still add the sandbox flags a root/container
environment needs to launch at all, even though those same flags are otherwise
stealth-blocked. Sandbox detection is monkeypatched so the tests are
deterministic across CI environments.
"""

import platform_utils
from platform_utils import (
    filter_stealth_args,
    get_platform_info,
    get_required_sandbox_args,
    merge_browser_args,
)


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
