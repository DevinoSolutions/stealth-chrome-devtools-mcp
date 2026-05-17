"""Unit tests for stealth browser arg filtering.

These tests validate that detectable Chrome flags are stripped
before they reach the browser, preserving stealth.

No browser or network required — pure function tests.
"""

import pytest
from platform_utils import filter_stealth_args, merge_browser_args


# ---------------------------------------------------------------------------
# filter_stealth_args
# ---------------------------------------------------------------------------

class TestFilterStealthArgs:
    """Tests for the stealth arg filter."""

    def test_empty_args(self):
        clean, warnings = filter_stealth_args([])
        assert clean == []
        assert warnings == []

    def test_safe_args_pass_through(self):
        safe = ["--window-position=0,0", "--start-maximized", "--lang=en-US"]
        clean, warnings = filter_stealth_args(safe)
        assert clean == safe
        assert warnings == []

    # ── Automation signals ──

    def test_strips_enable_automation(self):
        clean, warnings = filter_stealth_args(["--enable-automation"])
        assert clean == []
        assert len(warnings) == 1
        assert "navigator.webdriver" in warnings[0]

    def test_strips_test_type(self):
        clean, warnings = filter_stealth_args(["--test-type"])
        assert clean == []
        assert any("test mode" in w for w in warnings)

    def test_strips_remote_debugging_port(self):
        clean, warnings = filter_stealth_args(["--remote-debugging-port=9222"])
        assert clean == []
        assert len(warnings) == 1

    def test_strips_remote_debugging_pipe(self):
        clean, warnings = filter_stealth_args(["--remote-debugging-pipe"])
        assert clean == []

    def test_strips_auto_open_devtools(self):
        clean, warnings = filter_stealth_args(["--auto-open-devtools-for-tabs"])
        assert clean == []

    # ── Fingerprint-altering flags ──

    def test_strips_no_sandbox(self):
        clean, warnings = filter_stealth_args(["--no-sandbox"])
        assert clean == []
        assert any("sandbox" in w for w in warnings)

    def test_strips_disable_gpu(self):
        clean, warnings = filter_stealth_args(["--disable-gpu"])
        assert clean == []
        assert any("WebGL" in w for w in warnings)

    def test_strips_disable_gpu_sandbox(self):
        """--disable-gpu-sandbox starts with --disable-gpu prefix."""
        clean, warnings = filter_stealth_args(["--disable-gpu-sandbox"])
        assert clean == []

    def test_strips_disable_dev_shm(self):
        clean, warnings = filter_stealth_args(["--disable-dev-shm-usage"])
        assert clean == []

    def test_strips_headless_variants(self):
        for flag in ["--headless", "--headless=new", "--headless=old"]:
            clean, warnings = filter_stealth_args([flag])
            assert clean == [], f"{flag} should be stripped"

    def test_strips_disable_webgl(self):
        clean, warnings = filter_stealth_args(["--disable-webgl"])
        assert clean == []

    def test_strips_disable_webgl2(self):
        clean, warnings = filter_stealth_args(["--disable-webgl2"])
        assert clean == []

    def test_strips_single_process(self):
        clean, warnings = filter_stealth_args(["--single-process"])
        assert clean == []

    def test_strips_mute_audio(self):
        clean, warnings = filter_stealth_args(["--mute-audio"])
        assert clean == []

    def test_strips_force_device_scale_factor(self):
        clean, warnings = filter_stealth_args(["--force-device-scale-factor=2"])
        assert clean == []

    def test_strips_disable_extensions(self):
        clean, warnings = filter_stealth_args(["--disable-extensions"])
        assert clean == []

    def test_strips_disable_notifications(self):
        clean, warnings = filter_stealth_args(["--disable-notifications"])
        assert clean == []

    def test_strips_disable_popup_blocking(self):
        clean, warnings = filter_stealth_args(["--disable-popup-blocking"])
        assert clean == []

    # ── Puppeteer / Playwright signature flags ──

    def test_strips_puppeteer_defaults(self):
        puppeteer_flags = [
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-ipc-flooding-protection",
            "--disable-hang-monitor",
            "--disable-prompt-on-repost",
            "--disable-client-side-phishing-detection",
            "--disable-domain-reliability",
            "--metrics-recording-only",
        ]
        clean, warnings = filter_stealth_args(puppeteer_flags)
        assert clean == []
        assert len(warnings) == len(puppeteer_flags)

    def test_strips_playwright_defaults(self):
        playwright_flags = [
            "--password-store=basic",
            "--use-mock-keychain",
            "--export-tagged-pdf",
        ]
        clean, warnings = filter_stealth_args(playwright_flags)
        assert clean == []
        assert len(warnings) == len(playwright_flags)

    def test_strips_automation_convenience_flags(self):
        flags = [
            "--no-first-run",
            "--no-default-browser-check",
            "--safebrowsing-disable-auto-update",
            "--disable-sync",
        ]
        clean, warnings = filter_stealth_args(flags)
        assert clean == []
        assert len(warnings) == len(flags)

    # ── Mixed: safe + blocked ──

    def test_mixed_args_preserves_safe(self):
        args = [
            "--start-maximized",
            "--no-sandbox",
            "--lang=en-US",
            "--enable-automation",
            "--proxy-server=http://proxy:8080",
        ]
        clean, warnings = filter_stealth_args(args)
        assert clean == ["--start-maximized", "--lang=en-US", "--proxy-server=http://proxy:8080"]
        assert len(warnings) == 2

    def test_case_insensitive(self):
        """Flags should be matched case-insensitively."""
        clean, warnings = filter_stealth_args(["--Enable-Automation", "--NO-SANDBOX"])
        assert clean == []
        assert len(warnings) == 2

    # ── Bulk: every blocked flag individually ──

    def test_all_blocked_flags_individually(self):
        """Every flag in the blocklist should be stripped when passed alone."""
        from platform_utils import _stealth_blocked_args
        blocked = _stealth_blocked_args()
        for flag in blocked:
            clean, warnings = filter_stealth_args([flag])
            assert clean == [], f"{flag} should be stripped but wasn't"
            assert len(warnings) == 1, f"{flag} should produce exactly 1 warning"


# ---------------------------------------------------------------------------
# merge_browser_args
# ---------------------------------------------------------------------------

class TestMergeBrowserArgs:
    """Tests for merge_browser_args tuple return."""

    def test_returns_tuple(self):
        result = merge_browser_args([])
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_none_args(self):
        args, warnings = merge_browser_args(None)
        assert isinstance(args, list)
        assert warnings == []

    def test_strips_and_returns_warnings(self):
        args, warnings = merge_browser_args(["--enable-automation", "--lang=en"])
        assert "--lang=en" in args
        assert "--enable-automation" not in args
        assert len(warnings) == 1

    def test_safe_args_no_warnings(self):
        args, warnings = merge_browser_args(["--proxy-server=socks5://localhost:1080"])
        assert "--proxy-server=socks5://localhost:1080" in args
        assert warnings == []
