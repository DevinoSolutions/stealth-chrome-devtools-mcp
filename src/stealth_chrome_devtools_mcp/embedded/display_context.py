"""Where a window launched by THIS process can be seen.

THE one home for that question. Chrome inherits its parent's window station, so
whether a headed browser is visible is decided by the process that launches it —
never by the caller and never by the ``headless`` flag (F-808). ``singleton.py``
records this token per backend so discovery can prefer a window-capable one.

Deliberately observational: it reports OUR OWN context and never tries to pick or
enter someone else's session. On the machine F-808 was found on, the active
console session (2) was NOT the session holding the user's desktop (1), so any
"find the interactive session" heuristic is wrong somewhere.

Leaf contract: this module imports no embedded module except ``debug_logger``,
and never ``server``. ``debug_logger`` is the spine every other embedded module
reports through — a bare ``logging.getLogger(__name__)`` record would reach no
log file, because handlers are attached to ``stealth.backend``/``stealth.proxy``.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

# No desktop: a window launched here can never be displayed. PROVEN, not guessed.
HEADLESS = "headless"
# We could not classify this platform. Treated as window-capable on purpose —
# see test_unknown_platform_never_claims_headless.
UNVERIFIED = "unverified"


def _windows_session_id() -> int | None:
    """This process's Windows Terminal Services session id, or None if the
    probe fails. Session 0 is the isolated services session: since Vista its
    desktop is never composited onto a user's screen.
    """
    import ctypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        session = ctypes.c_ulong()
        if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
            # A BOOL-0 refusal is a real answer we cannot read - say so rather
            # than folding it into the same silent None as "no windll at all".
            debug_logger.log_warning(
                "display_context",
                "_windows_session_id",
                f"ProcessIdToSessionId refused (WinError {ctypes.get_last_error()}); "
                "treating display context as unverified",
            )
            return None
        return int(session.value)
    except Exception:  # noqa: BLE001  PERMANENT(probe must never raise)
        # Every remaining failure shape - no WinDLL, a missing symbol, an OS
        # refusal - means the same thing: we do not know. None becomes
        # UNVERIFIED, which is treated as capable, so a broken probe can never
        # block headed browsing.
        debug_logger.log_warning(
            "display_context",
            "_windows_session_id",
            "Windows session probe raised; treating display context as unverified",
        )
        return None


@lru_cache(maxsize=1)
def _macos_context() -> str:
    """macOS GUI access. A process in an SSH session belongs to a Background
    launchd domain and cannot own a window; an "Aqua" manager means it can.
    Any probe failure is UNVERIFIED (capable), never HEADLESS: macOS headed
    browsing works today and must not regress on an unrecognized launchctl.

    Cached: a process cannot change its launchd domain, so the subprocess is
    worth running exactly once. ``display_context`` itself is deliberately NOT
    cached — the Linux branch reads the environment live.
    """
    import subprocess

    try:
        out = subprocess.run(
            # Absolute path on purpose: a PATH-resolved "launchctl" would let an
            # attacker-controlled PATH decide whether we believe we have a desktop.
            ["/bin/launchctl", "managername"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        debug_logger.log_warning(
            "display_context",
            "_macos_context",
            "launchctl managername failed; treating display context as unverified",
        )
        return UNVERIFIED
    name = (out.stdout or "").strip()
    if name == "Aqua":
        # getattr guard: os.getuid does not exist on Windows, and a bare
        # reference trips static analysis there even though this branch is
        # darwin-only (platform_utils.py:354 sets the precedent).
        return f"aqua-{getattr(os, 'getuid', lambda: 'N-A')()}"
    return HEADLESS if name else UNVERIFIED


def display_context() -> str:
    """An opaque token naming the desktop this process can put a window on.

    ``HEADLESS`` means proven-invisible; ``UNVERIFIED`` means unclassifiable and
    is treated as capable. Any other value identifies a specific desktop, so two
    backends on different desktops are distinguishable.
    """
    if sys.platform == "win32":
        session = _windows_session_id()
        if session is None:
            return UNVERIFIED
        return HEADLESS if session == 0 else f"win-session-{session}"
    if sys.platform.startswith("linux"):
        # Read live, NOT via settings.get_settings().display, even though that
        # field exists. Two reasons, both load-bearing:
        #   1. This is an observation of the running process (a peer of
        #      sys.platform and the Win32 session id), not operator config. A
        #      cached value would answer for whoever loaded settings first.
        #   2. get_settings() VALIDATES - an unknown STEALTH_MCP_* key makes it
        #      raise. This function's whole job is to explain why a spawn cannot
        #      be seen, so it must not be able to fail for an unrelated reason.
        wayland = os.environ.get("WAYLAND_DISPLAY")  # noqa: TID251  PERMANENT(runtime fact, not config)
        if wayland:
            return f"wayland-{wayland}"
        x11 = os.environ.get("DISPLAY")  # noqa: TID251  PERMANENT(runtime fact, not config)
        return f"x11-{x11}" if x11 else HEADLESS
    if sys.platform == "darwin":
        return _macos_context()
    return UNVERIFIED


def can_show_windows() -> bool:
    """True unless we PROVED no window launched here could be seen."""
    return display_context() != HEADLESS
