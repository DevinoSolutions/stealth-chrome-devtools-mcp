"""Where a window launched by THIS process can be seen (F-808).

The module is deliberately observational: it reports our own context and never
picks a session. These pins hold the classification table AND the fail-toward-
capable rule, which is the part a well-meaning "make it stricter" edit breaks.

Expected values are written as bare string literals rather than dc.HEADLESS /
dc.UNVERIFIED on purpose: Task 3 persists these tokens to server.json, so they
are wire values. Asserting against the constant would let a rename silently
change the wire format while every test stayed green.

`monkeypatch.setattr(sys, "platform", ...)` patches the sys module process-wide
for the test's duration; monkeypatch restores it. That is intended.
"""

import subprocess
import sys

import pytest

from stealth_chrome_devtools_mcp.embedded import display_context as dc


@pytest.fixture(autouse=True)
def _clear_macos_cache():
    """_macos_context is lru_cached, so one test's launchctl double would
    otherwise answer for every test after it."""
    dc._macos_context.cache_clear()
    yield
    dc._macos_context.cache_clear()


def _launchctl_returning(stdout: str):
    """A subprocess.run double built from CompletedProcess itself, so it cannot
    drift from the real shape the code reads."""

    def _run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["/bin/launchctl", "managername"], returncode=0, stdout=stdout
        )

    return _run


def test_windows_session_zero_is_headless(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(dc, "_windows_session_id", lambda: 0)
    assert dc.display_context() == "headless"
    assert dc.can_show_windows() is False


def test_windows_interactive_session_is_named_by_id(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(dc, "_windows_session_id", lambda: 2)
    assert dc.display_context() == "win-session-2"
    assert dc.can_show_windows() is True


def test_linux_prefers_wayland_then_x11_then_headless(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    assert dc.display_context() == "wayland-wayland-0"
    monkeypatch.delenv("WAYLAND_DISPLAY")
    assert dc.display_context() == "x11-:0"
    monkeypatch.delenv("DISPLAY")
    assert dc.display_context() == "headless"


def test_unknown_platform_never_claims_headless(monkeypatch):
    """Fail toward 'interactive' on platforms we cannot classify: a wrong
    'headless' would BLOCK headed browsing that works today, which is a worse
    regression than the ghost window we are fixing."""
    monkeypatch.setattr(sys, "platform", "sunos5")
    assert dc.display_context() == "unverified"
    assert dc.can_show_windows() is True


def test_windows_probe_failure_is_unverified_not_headless(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(dc, "_windows_session_id", lambda: None)
    assert dc.display_context() == "unverified"
    assert dc.can_show_windows() is True


def test_macos_aqua_names_the_gui_session(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", _launchctl_returning("Aqua\n"))
    monkeypatch.setattr(dc.os, "getuid", lambda: 501, raising=False)
    assert dc.display_context() == "aqua-501"
    assert dc.can_show_windows() is True


def test_macos_background_domain_is_headless(monkeypatch):
    """An SSH session's launchd domain owns no window server connection."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", _launchctl_returning("Background\n"))
    assert dc.display_context() == "headless"
    assert dc.can_show_windows() is False


def test_macos_empty_manager_name_is_unverified(monkeypatch):
    """No manager name means launchctl told us nothing - that is 'unknown',
    not 'proven invisible'."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", _launchctl_returning("  \n"))
    assert dc.display_context() == "unverified"
    assert dc.can_show_windows() is True


def test_macos_launchctl_failure_is_unverified(monkeypatch):
    """Both failure shapes: launchctl missing (OSError) and launchctl hanging
    (TimeoutExpired). Neither may downgrade macOS to headless."""
    monkeypatch.setattr(sys, "platform", "darwin")

    def _raise_oserror(*args, **kwargs):
        raise OSError("no such file: /bin/launchctl")

    monkeypatch.setattr(subprocess, "run", _raise_oserror)
    assert dc.display_context() == "unverified"

    dc._macos_context.cache_clear()

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="/bin/launchctl", timeout=5)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    assert dc.display_context() == "unverified"


def test_real_probe_returns_a_nonempty_token():
    """No mocks: exercises the real ctypes probe on Windows, the real launchctl
    on macOS, and the real environment reads on Linux. Catches a probe that
    raises or returns None on a platform no unit test simulates faithfully."""
    context = dc.display_context()
    assert isinstance(context, str)
    assert context
    assert dc.can_show_windows() is (context != "headless")
