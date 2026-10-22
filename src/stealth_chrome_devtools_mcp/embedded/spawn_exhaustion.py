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

**A CPU-starved launch is a third answer, not a variant of the second (F-1024).**
nodriver gives a launched Chrome a fixed connect deadline, so a Chrome that DID start
and is merely late under CPU load fails exactly like one that never could. Naming
"out of process capacity" for it sent an operator to reap browsers that were not the
problem, while the machine was starved of CPU. :class:`LaunchEvidence` carries the two
facts that tell them apart, both read at the moment of failure and before the teardown
ends the late Chrome: whether the attempt's Chrome was running, and the machine CPU.

A leaf module: ``psutil``, ``debug_logger`` and ``browser_pid_registry`` only.
Never ``process_cleanup``, never ``singleton``, never ``server``.

The module is NOT named ``spawn_diagnostics``: ``browser_manager.spawn_browser``
and ``server.spawn_browser`` each bind a local of that name inside the very
``try`` whose ``except`` is this helper's call site, and a module shadowed there
by an unbound local would raise from inside the error handler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

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

# Machine CPU at which a Chrome that started and was still late is put down to load
# rather than to capacity. Sampled over a short window at the moment of failure, so
# it is "busy NOW", not an average of a quiet minute. A module constant on purpose
# (see above): an unknown STEALTH_MCP_* key crashes get_settings().
_STARVED_CPU_PERCENT = 90.0
_CPU_SAMPLE_SECONDS = 0.2

# The names a Chromium BROWSER tree runs under, measured on the maintainer's box
# (tasklist, 2026-10-21: 243 chrome.exe beside 29 stealth-chrome-devtools-mcp.exe, our
# own stdio proxy, which the old "chrom" substring counted). Exact names, ``.exe``
# stripped and lower-cased: ``chromedriver``, ``ChromeSetup``, ``chrome-devtools-mcp``,
# ``ChromeRemoteDesktopHost`` and every Electron app that happens to say "chrome" are
# not a browser and must not move a process-capacity verdict.
_BROWSER_NAMES = frozenset(
    {
        "chrome",
        "chrome_proxy",
        "chrome_crashpad_handler",
        "chromium",
        "chromium-browser",
        "chromium-browser-sandbox",
        "chrome-sandbox",
    }
)
# macOS spells the family with a bundle name and a role in parentheses.
_BROWSER_PREFIXES = ("google chrome", "chromium helper", "chromium")

_SKIPPABLE = (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess)


def _is_browser_name(name: str) -> bool:
    """Whether a process name is a Chromium browser process (see ``_BROWSER_NAMES``)."""
    lowered = name.lower().removesuffix(".exe")
    return lowered in _BROWSER_NAMES or lowered.startswith(_BROWSER_PREFIXES)


def live_chromium_processes() -> int:
    """Machine-wide count of live Chromium-family BROWSER processes.

    ``process_iter(["name"])`` asks for the name and nothing else: reading
    ``cmdline`` for the whole process table costs one PEB read per process on
    Windows and this question never needs it. The names are matched exactly
    (:func:`_is_browser_name`), covering every platform's spelling — ``chrome.exe`` /
    ``chrome_proxy.exe``, ``chrome`` / ``chromium`` / ``chromium-browser`` /
    ``chrome_crashpad_handler``, ``Google Chrome Helper (Renderer)`` — and nothing
    that merely contains "chrom" (F-1024).

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
        if _is_browser_name(name):
            live += 1
    return live


class LaunchEvidence(NamedTuple):
    """What a failed launch looked like when it failed (F-1024)."""

    chrome_started: bool
    """The attempt's own Chrome was running (it started and was late, or hung)."""
    cpu_percent: float | None
    """Machine CPU over a short window at the failure, or ``None`` if unreadable."""

    def starved(self) -> bool:
        """A Chrome that started on a machine whose CPU is at the ceiling."""
        return (
            self.chrome_started
            and self.cpu_percent is not None
            and self.cpu_percent >= _STARVED_CPU_PERCENT
        )


def observe(chrome_started: bool) -> LaunchEvidence:
    """The evidence for a launch that just failed. Never raises: an unreadable CPU
    is ``None``, which :meth:`LaunchEvidence.starved` reads as "not starved". The
    0.2 s CPU window is spent only when a Chrome started: without one the verdict
    cannot be "starved", so a launch that never began costs the loop nothing."""
    try:
        cpu = (
            psutil.cpu_percent(interval=_CPU_SAMPLE_SECONDS) if chrome_started else None
        )
    except Exception as error:  # noqa: BLE001  PERMANENT(a probe inside a failure handler must not raise)
        debug_logger.log_debug(
            "spawn_exhaustion", "observe", f"CPU not readable: {error}"
        )
        cpu = None
    return LaunchEvidence(chrome_started=chrome_started, cpu_percent=cpu)


def _starved_text(evidence: LaunchEvidence, live: int, tracked: int) -> str:
    """The CPU-starved paragraph, carrying its own leading separator."""
    return (
        "\n\nSpawn diagnostics: this launch was CPU-starved, not capacity-limited "
        "(F-1024). Chrome started, but the machine CPU read "
        f"{evidence.cpu_percent:.0f}% when the launch failed, so the browser "
        "missed its DevTools port deadline under load. "
        f"{live} Chromium-family processes are live and {tracked} browser(s) are "
        "tracked, which is not the cause here.\n"
        "Do not reap browsers for this. Retry once the machine is idle, or spawn "
        "one browser at a time while it is busy."
    )


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


def exhaustion_hint(
    pid_file: Path, evidence: LaunchEvidence | None = None
) -> str | None:
    """The paragraph to append to a failed spawn's error, or ``None``.

    A launch whose Chrome started on a CPU-starved machine (*evidence*) gets the
    starvation paragraph whatever the process count; otherwise the process-capacity
    paragraph, only when the count is really at the limit. Returns ``None``
    otherwise and on any internal failure, so the call site is one line plus an
    ``or ""``. The returned string already starts
    with ``"\\n\\n"``: the call site does no formatting.

    ``pid_file`` is required and never defaults. The caller's binding is what
    selects the record — a defaulted path would bind a module global at def-time
    and silently ignore the tests' redirection, the only thing keeping a test
    run out of the developer's live ``~/.stealth-mcp``.
    """
    try:
        live = live_chromium_processes()
        starved = evidence is not None and evidence.starved()
        if live < _EXHAUSTION_PROCESS_THRESHOLD and not starved:
            return None
        tracked = len(browser_pid_registry.read_entries(pid_file))
    except Exception as error:  # noqa: BLE001  PERMANENT(hint must never raise)
        debug_logger.log_debug(
            "spawn_exhaustion",
            "exhaustion_hint",
            f"Exhaustion probe failed, spawn error left undecorated: {error}",
        )
        return None
    if evidence is not None and starved:
        return _starved_text(evidence, live, tracked)
    return _hint_text(live, tracked)
