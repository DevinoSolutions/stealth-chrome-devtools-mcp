"""Where a window launched by THIS process can be seen (F-808).

The module is deliberately observational: it reports our own context and never
picks a session. These pins hold the classification table AND the fail-toward-
capable rule, which is the part a well-meaning "make it stricter" edit breaks.
"""

from stealth_chrome_devtools_mcp.embedded import display_context as dc


def test_windows_session_zero_is_headless(monkeypatch):
    monkeypatch.setattr(dc.sys, "platform", "win32")
    monkeypatch.setattr(dc, "_windows_session_id", lambda: 0)
    assert dc.display_context() == dc.HEADLESS
    assert dc.can_show_windows() is False


def test_windows_interactive_session_is_named_by_id(monkeypatch):
    monkeypatch.setattr(dc.sys, "platform", "win32")
    monkeypatch.setattr(dc, "_windows_session_id", lambda: 2)
    assert dc.display_context() == "win-session-2"
    assert dc.can_show_windows() is True


def test_linux_prefers_wayland_then_x11_then_headless(monkeypatch):
    monkeypatch.setattr(dc.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    assert dc.display_context() == "wayland-wayland-0"
    monkeypatch.delenv("WAYLAND_DISPLAY")
    assert dc.display_context() == "x11-:0"
    monkeypatch.delenv("DISPLAY")
    assert dc.display_context() == dc.HEADLESS


def test_unknown_platform_never_claims_headless(monkeypatch):
    """Fail toward 'interactive' on platforms we cannot classify: a wrong
    'headless' would BLOCK headed browsing that works today, which is a worse
    regression than the ghost window we are fixing."""
    monkeypatch.setattr(dc.sys, "platform", "sunos5")
    assert dc.display_context() == "unverified"
    assert dc.can_show_windows() is True


def test_windows_probe_failure_is_unverified_not_headless(monkeypatch):
    monkeypatch.setattr(dc.sys, "platform", "win32")
    monkeypatch.setattr(dc, "_windows_session_id", lambda: None)
    assert dc.display_context() == "unverified"
