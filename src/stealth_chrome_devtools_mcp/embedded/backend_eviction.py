"""THE one home for taking a backend's port away from it — the DECISION (may
this backend be terminated to make room for ours?) and the ACT (terminate it).

Extracted from ``singleton`` by F-886, which needed the rule and would have put
the file over its 1000-LOC budget. The two halves belong together: the finding
is about a kill that had no rule in front of it, and a rule living in one file
while the kill lives in another is how it came to be missing.

**The defect.** A cold start whose reuse gate said "not mine" ran
``_clear_stale_backend`` -> ``_terminate_backend`` on whatever was on the port,
and the only thing the decision consulted was IDENTITY:
``backend_registry.fingerprint_mismatch`` answers "these two source digests
differ", never "mine is newer" and never "that one is busy". Two clients running
the same released version off different source bytes — a ``uvx @latest`` session
beside a ``uv tool`` install, an editable checkout beside either — therefore each
read the other as stale, and the one that started second killed the one that was
already working.

**What that cost, measured** (2026-09-16, Windows, 2.1.8, the ``S5`` fleet in
``tests/test_e2e_lifecycle_resilience.py``; six runs at the finding, one
instrumented run at 0.25s resolution):

* the incumbent backend died 1.6s after the arriving proxy took the lock;
* its BROWSER outlived it by **4.43s** and was then reaped by the REPLACEMENT's
  orphan recovery (``process_cleanup.recovery: Killed 1 orphaned browser
  processes``) — ``browser_pid_registry`` stamps the backend pid as owner, and
  an owner we ourselves killed a moment ago is indistinguishable from one that
  crashed last week. So the browser did NOT die with its backend; it was reaped
  as an orphan, which is why the fix has to be at the eviction and not at the
  reaper;
* the incumbent's own proxy never learned: both of its death witnesses are
  PORT-scoped and the replacement binds the same port, so its calls answered
  ``{"code": 32600, "message": "Session terminated"}`` (5 of 6 runs) with no
  condemnation, no heal and no teardown in its log.

That is "my browsers randomly closed" and "the MCP server disconnected
mid-session" from one cause, and the cause is this one decision.

**The rule.** A backend is PROTECTED when it is one of ours, it is running, its
recorded identity is not ours (so we would not adopt it), and it still owns at
least one live browser. A protected backend is never terminated and never bound
over: the arriving client spawns its own on a different port and both sessions
keep their browsers. An UNPROTECTED backend — nothing of ours running there, or
running but owning no live browser — is evicted exactly as before, so the
upgrade flow issue #14 introduced (edit source, get a fresh backend) is
untouched in the case that flow actually happens in.

**Why "owns a live browser" and not "is alive".** Protecting every live backend
would mean a machine accumulates one backend per source edit forever, with
nothing in the tree allowed to reclaim one; and the harm the operator reports is
specifically the loss of browsers, which are the only state a backend holds that
cannot be rebuilt by reconnecting. Live browsers are also a LOCAL question —
``browser_pids.json`` answers it with no round trip and no new endpoint — which
is what lets the rule be asked from the stdio proxy's cold-start path, where
there is no backend to ask. The session bricked WITHOUT losing a browser is the
residual, and it is named in the finding's §6.

**The act is deliberately ungated.** :func:`terminate` applies no rule of its
own, because its other two callers are ``singleton.stop_backend`` and
``restart_backend`` — operator verbs, where the operator asking for it IS the
authority the rule otherwise supplies (``stop``'s docstring has always said
terminating every live browser session is the verb's purpose, not a side effect
to guard against). :func:`clear_stale` is the composition that puts the rule in
front of the act, and it is the one a cold start calls.

A leaf: ``browser_pid_registry`` (the record's own home, for its own reader) and
psutil. Every other collaborator arrives as an argument — the backend-identity
check, the pid liveness checks, the reuse gate and the record's own entry are
all ``singleton``'s, and handing them in is what keeps this module out of the
lifecycle import graph, exactly as ``backend_watchdog`` and ``backend_liveness``
do it. It is also what keeps ``singleton``'s thin bindings the patch surface the
suite already targets. Never raises: a record it cannot read is no browsers,
which resolves toward evicting, because refusing to evict on an unreadable
record would brick every cold start on this machine.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import psutil

from stealth_chrome_devtools_mcp.embedded import browser_pid_registry

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from stealth_chrome_devtools_mcp.embedded.backend_registry import BackendEntry

# One stream: a refusal to evict is part of the proxy's story, so it writes to
# the proxy log ``configure_logging("proxy")`` already owns — the same logger
# name ``singleton`` uses, from which this moved. A log line that changed
# streams is a log line nobody finds.
_logger = logging.getLogger("stealth.proxy")

# How long a terminated backend gets to actually go, and then to release its
# listener so a fresh backend can bind. Unchanged values, named on the way out
# of ``singleton``.
TERMINATE_GRACE_SECONDS = 5
PORT_RELEASE_SECONDS = 5
PORT_RELEASE_POLL_SECONDS = 0.1


def pid_on_port(port: int, *, is_ours: Callable[[object], bool]) -> int | None:
    """The pid of OUR backend listening on ``port``, or None.

    A foreign process holding the port is deliberately ignored (never returned
    for termination).
    """
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.Error, OSError):
        return None
    for conn in conns:
        laddr = getattr(conn, "laddr", None)
        if (
            laddr
            and getattr(laddr, "port", None) == port
            and conn.status == psutil.CONN_LISTEN
            and conn.pid
            and is_ours(conn.pid)
        ):
            return conn.pid
    return None


def terminate(
    port: int,
    *,
    pid_on_port: Callable[[int], int | None],
    recorded_pid: object,
    is_ours: Callable[[object], bool],
    is_healthy: Callable[[int], bool],
) -> bool:
    """Terminate OUR backend associated with ``port``, if one is identifiable.

    Resolves the pid by open port first (``pid_on_port`` — the CALLER's binding
    of :func:`pid_on_port`, not this module's, so a patch on
    ``singleton._backend_pid_on_port`` still decides what gets killed), then
    falls back to ``recorded_pid`` (``server.json``'s, handed in by the caller
    that owns the record path) — guarded by ``is_ours`` either way, so a pid
    that is not positively identified as our backend (e.g. a recycled pid now
    running an unrelated process) is never touched. Best-effort and bounded,
    never raises. Returns whether a backend of ours was found and terminated.
    """
    pid = pid_on_port(port)
    if pid is None and is_ours(recorded_pid):
        pid = recorded_pid
    if not isinstance(pid, int):
        return False

    try:
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=TERMINATE_GRACE_SECONDS)
        except psutil.TimeoutExpired:
            proc.kill()
    except (psutil.Error, OSError):
        pass

    # Give the OS a moment to release the port so a fresh backend can bind.
    deadline = time.monotonic() + PORT_RELEASE_SECONDS
    while time.monotonic() < deadline:
        if not is_healthy(port):
            return True
        time.sleep(PORT_RELEASE_POLL_SECONDS)
    return True


def owned_browsers(
    state_dir: Path,
    owner_pid: object,
    *,
    is_running: Callable[[int], bool],
) -> list[int]:
    """The pids of the browsers ``owner_pid`` recorded that are still running.

    Reads the tracking record through ``browser_pid_registry.read_entries`` —
    its one reader, which already tolerates the legacy schema and a hand edit
    and already answers "unreadable" as an empty table — rather than parsing it
    here. Anything that is not an int owns no browsers rather than every
    browser.

    Liveness is the caller's ``is_running`` and is asked of the BROWSER pid
    alone, deliberately not of the ``(pid, create_time)`` pair
    ``browser_pid_registry.is_reapable`` compares. A pid recycled onto an
    unrelated process makes this over-report, i.e. spares a backend that could
    have been evicted — one extra backend on one extra port. The opposite
    mistake closes a browser. That asymmetry is the whole finding, so the cheap
    check is the right one here even though the expensive one is right there.
    """
    if not isinstance(owner_pid, int):
        return []
    entries = browser_pid_registry.read_entries(
        state_dir / browser_pid_registry.RECORD_NAME
    )
    return [
        pid
        for entry in entries.values()
        if entry.get(browser_pid_registry.OWNER_PID) == owner_pid
        and isinstance(pid := entry.get("pid"), int)
        and is_running(pid)
    ]


def protected(
    entry: BackendEntry | None,
    *,
    state_dir: Path,
    identity_matches: Callable[[BackendEntry | None], bool],
    is_ours: Callable[[object], bool],
    is_running: Callable[[int], bool],
) -> list[int]:
    """The live browsers that make ``entry``'s backend UNEVICTABLE — empty when
    it may be terminated and bound over.

    Four conditions, in the order that lets the cheap ones decide first:

    1. there IS a recorded entry (nothing recorded protects nothing);
    2. its identity is NOT ours. A same-identity backend is either adoptable —
       in which case the caller never reached eviction — or wedged, and
       terminating a wedged backend of our own is the recovery both the
       ``restart`` verb and the cold-start lock exist to perform. Protecting it
       would turn a wedge into a permanent one;
    3. its recorded pid is a live backend OF OURS (``is_ours``, the same
       predicate eviction already trusts to never touch the wrong process). A
       record naming a dead or recycled pid describes nothing that can be
       serving, and its browsers — if any survived — are orphans the
       replacement is entitled to reap;
    4. it still owns at least one live browser.

    The verdict is deliberately NOT "is it answering ``initialize``". A wedged
    foreign backend still holds its browsers' processes and its user's tabs, and
    killing it is the harm this module exists to prevent; the caller that wants
    to know whether a backend is USABLE asks ``backend_liveness``, which is that
    question's one home.

    The answer is a list of pids rather than a bool because the refusal is
    LOGGED, and a count of browsers is the only evidence that makes it auditable
    afterwards. ``bool(...)`` at the call site is the predicate.
    """
    if entry is None or identity_matches(entry):
        return []
    pid = entry.get("pid")
    if not is_ours(pid):
        return []
    return owned_browsers(state_dir, pid, is_running=is_running)


def stepping_aside(port: int, spared: list[int]) -> bool:
    """True iff ``spared`` is non-empty — the bind-site half of the rule, with
    the one log line that explains a second backend on this desktop.

    ``singleton._select_backend_port`` folds this into the ``force_new`` it
    hands the port picker, beside the foreign-occupant and sibling-context
    clauses that already live there. Logged at INFO and not WARNING: nothing
    went wrong — a stranger's backend is serving its session, and ours is about
    to serve ours on another port. Without this line the only evidence of why
    two backends exist would be two entries in ``server.json``.
    """
    if not spared:
        return False
    _logger.info(
        "port %d holds another session's backend still serving %d live "
        "browser(s); spawning ours beside it on a fresh port (F-886)",
        port,
        len(spared),
    )
    return True


def clear_stale(
    port: int,
    *,
    reusable: Callable[[], bool],
    protecting: Callable[[], list[int]],
    terminate_backend: Callable[[], object],
) -> list[int]:
    """Free ``port`` for a fresh backend of ours. Returns the live browsers
    that STOPPED it — empty when the port was cleared (or needed no clearing)
    and the caller may go on to spawn.

    Three answers, and only the third terminates anything:

    * a reusable same-identity backend is already there (``reusable``) — there
      is nothing to clear, and the caller's own earlier reuse check is what
      decides whether to spawn (unchanged from before F-886);
    * the occupant is a stranger's backend still owning live browsers
      (``protecting``) — F-886's refusal, logged with the count it spared, and
      the one answer that tells the caller to spawn NOTHING. This is the KILL
      SITE, so the guard belongs here as well as at the bind site
      (``singleton._select_backend_port``), which normally steps around such a
      port before we are ever handed it: the two read the record at different
      moments, and only this one is the moment that does the damage;
    * otherwise a stale, legacy or wedged backend may be squatting the port and
      is evicted under the caller's lock, so our fresh, correctly-versioned
      backend can bind — without which the proxy would fall back to the old
      backend and the upgrade would silently not take effect (issue #14).

    The shape — a list, empty meaning "proceed" — is chosen so that a stub of
    the caller's binding that returns ``None`` (every existing test double of
    ``_clear_stale_backend``) still reads as "proceed": the refusal is the one
    new outcome, so it is the one that has to be non-empty to be seen.
    """
    if reusable():
        return []
    spared = protecting()
    if spared:
        _logger.warning(
            "refusing to evict the backend on port %d: it still owns %d live "
            "browser(s) for another session (F-886)",
            port,
            len(spared),
        )
        return spared
    terminate_backend()
    return []
