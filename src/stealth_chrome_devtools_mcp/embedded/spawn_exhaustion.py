"""THE one home for "is this machine out of browser-process capacity, and what
should the operator do about it" (F-811).

A failed ``spawn_browser`` used to surface nodriver's raw connect failure with
no hint that the machine was drowning in Chromium processes, so the caller —
usually an agent — retried and made the exhaustion worse. This module measures
the two numbers that make that error actionable and renders the remedy the CLI
already ships.

**It decides on the absolute live count, and merely reports the tracked count.**
The tempting signal, "OS-visible minus tracked", points the wrong way: a human
with forty real tabs is also untracked, so a heavy browsing session produces a
large delta with zero orphans — precisely the case that must NOT be told to run
``kill-orphans``. The delta is what tells an operator whether reaping will help
at all, so it is printed; the absolute count is what fires the hint.

**The name matcher here is deliberately narrower than
``process_cleanup._is_browser_process_name``, and the two must not be
"unified".** That predicate answers "am I allowed to kill this pid", where
breadth (``msedge``, ``brave``) is a safety property. This one answers "how much
of what WE spawn is running", and counting a user's Edge windows toward a hint
that says "run kill-orphans" would be a false direction, since we never launch
them. Different question, different predicate — not a second way to do one
thing.

**It never raises, and it never ships to Sentry.** A diagnostic that breaks the
error it decorates is strictly worse than no diagnostic, so every failure
degrades to ``None``; and it logs at ``log_debug`` rather than ``log_error``
because it fires only when the machine is already unhealthy, which is exactly
when adding Sentry volume to the issue we are closing would be perverse.

A leaf module: ``psutil``, ``debug_logger`` and ``browser_pid_registry`` only.
Never ``process_cleanup``, never ``singleton``, never ``server``.

The module is NOT named ``spawn_diagnostics``: ``browser_manager.spawn_browser``
and ``server.spawn_browser`` each bind a local of that name inside the very
``try`` whose ``except`` is this helper's call site, and a module shadowed there
by an unbound local would raise from inside the error handler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import psutil

from stealth_chrome_devtools_mcp.embedded import browser_pid_registry
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

if TYPE_CHECKING:
    from pathlib import Path

# Chrome is process-per-renderer plus a fixed retinue (browser, GPU, network
# utility, storage, crashpad), so one human session with 40 real tabs lands
# around 50-70 and one of our instances costs 5-12. 120 therefore requires
# roughly double a heavy human browsing session, while the observed exhaustion
# event measured 204 — margin on both sides. A module constant on purpose: an
# unknown STEALTH_MCP_* key crashes get_settings(), and the house rule is
# universal defaults over config knobs.
_EXHAUSTION_PROCESS_THRESHOLD = 120

_SKIPPABLE = (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess)


def _live_chromium_processes() -> int:
    """Machine-wide count of live Chromium-family processes.

    ``process_iter(["name"])`` asks for the name and nothing else: reading
    ``cmdline`` for the whole process table costs one PEB read per process on
    Windows and this question never needs it. The single ``"chrom"`` substring
    covers every platform's spelling at once — ``chrome.exe`` /
    ``chrome_proxy.exe``, ``chrome`` / ``chromium`` / ``chromium-browser`` /
    ``chrome_crashpad_handler``, ``Google Chrome Helper (Renderer)``.

    A process that dies mid-walk (``/proc`` entries vanish) or belongs to
    another user (``AccessDenied`` on macOS) is skipped, not fatal: both are
    normal and neither is a reason to lose the count.
    """
    live = 0
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info.get("name") or ""
        except _SKIPPABLE:
            continue
        if "chrom" in name.lower():
            live += 1
    return live


def _hint_text(live: int, tracked: int) -> str:
    """The operator-facing paragraph, carrying its own leading separator."""
    return (
        f"\n\nSpawn diagnostics: {live} Chromium-family processes are live on "
        f"this machine and {tracked} browser(s) are tracked in the shared "
        "record. This machine has most likely run out of process capacity, "
        "which is the usual cause of a failed browser launch (F-811).\n"
        "Reap the tracked browsers whose backend is gone:\n"
        "  stealth-chrome-devtools kill-orphans --force\n"
        "(--force is required because THIS backend is alive; without it the "
        "command refuses.)\n"
        "Then reclaim the profile directories they left behind:\n"
        "  stealth-chrome-devtools cleanup --apply\n"
        "Processes not in the tracked count are not ours to reap — close them "
        "or reboot."
    )


def exhaustion_hint(pid_file: Path) -> str | None:
    """The exhaustion paragraph to append to a failed spawn's error, or ``None``.

    Returns ``None`` below the threshold and on any internal failure, so the
    call site is one line plus an ``or ""``. The returned string already starts
    with ``"\\n\\n"``: the call site does no formatting.

    ``pid_file`` is required and never defaults. The caller's binding is what
    selects the record — a defaulted path would bind a module global at def-time
    and silently ignore the tests' redirection, the only thing keeping a test
    run out of the developer's live ``~/.stealth-mcp``.
    """
    try:
        live = _live_chromium_processes()
        if live < _EXHAUSTION_PROCESS_THRESHOLD:
            return None
        tracked = len(browser_pid_registry.read_entries(pid_file))
    except Exception as error:  # noqa: BLE001  PERMANENT(hint must never raise)
        debug_logger.log_debug(
            "spawn_exhaustion",
            "exhaustion_hint",
            f"Exhaustion probe failed, spawn error left undecorated: {error}",
        )
        return None
    return _hint_text(live, tracked)
