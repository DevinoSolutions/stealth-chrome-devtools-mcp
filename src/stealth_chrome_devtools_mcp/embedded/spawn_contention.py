"""THE one home for "did this spawn fail because it was racing sibling spawns,
and what should the caller do about it" (F-834).

nodriver's connect failure ends in *"Possibly because you are running as root?
In that case you need to pass no_sandbox=True"*. Under concurrent spawns that
advice is a **red herring** — the sandbox is already off and root is not
involved — and it cost two independent diagnosing agents real time before the
actual cause (N spawns funnelling into one profile directory) was found. A
failed spawn that overlapped siblings therefore says so, and says so *about* the
advice, so the next reader does not chase it again.

**It names the count, never the mechanism.** The only fact this module has is an
integer: how many spawns were in flight. Whether they shared a directory is not
knowable here, and since F-834's own layers it is frequently FALSE — concurrent
spawns are handed distinct, reserved clone directories (stage 2 per ATTEMPT,
stage 1 for the loser of the master race), so the shared-profile mechanism the
2026-08-30 incident turned on is one candidate among two rather than the answer.
Measured on the coverage gate's macOS/ARM64 cell (run 35150887345, attempt 2): a
fleet that had ALREADY serialised its one master-taking lead spawn still lost a
follower to ``ConnectionRefusedError``, with all five followers on their own
directories — for that failure the old wording's "contend for the same Chrome
profile" was simply untrue, and the likelier cause was a two-core runner. The
paragraph therefore says what is measured, offers both causes, and lets the one
remedy that serves both stand: serialize, or retry once the others settle.

**A sibling of ``spawn_exhaustion``, deliberately not folded into it.** That
module answers "is this machine out of browser-process capacity" and its own
docstring is explicit that a different question deserves a different predicate
rather than a widened one. Capacity and contention are different questions with
different remedies (reap orphans vs. serialize spawns), so each keeps its own
home; the *pattern* stays single — both render a self-separated paragraph that
``browser_manager``'s one error-composition site concatenates with an ``or ""``.

**It never raises and it never ships to Sentry**: it is pure text over an
integer the caller already holds, and a diagnostic that breaks the error it
decorates is strictly worse than no diagnostic.

A leaf module: it imports nothing from this package.
"""

from __future__ import annotations

# One spawn in flight is not contention. Two is: Chrome's own profile singleton
# lets exactly one process open a given user-data-dir, so the second concurrent
# launch is already the failure mode this hint exists to name. A module constant
# on purpose — an unknown STEALTH_MCP_* key crashes get_settings(), and the house
# rule is universal defaults over config knobs.
_CONTENTION_MIN_IN_FLIGHT = 2


def contention_hint(in_flight: int) -> str | None:
    """The contention paragraph to append to a failed spawn's error, or ``None``.

    *in_flight* is how many ``spawn_browser`` calls this backend had running when
    the failure surfaced. Returns ``None`` below the threshold so the call site
    is one line plus an ``or ""``; the returned string already starts with
    ``"\\n\\n"``, so the site does no formatting.
    """
    if not isinstance(in_flight, int) or in_flight < _CONTENTION_MIN_IN_FLIGHT:
        return None
    return (
        f"\n\nSpawn diagnostics: {in_flight} spawn_browser calls were in flight "
        "in this backend when this one failed. That COUNT is measured; what "
        "these spawns were contending FOR is not, and this hint does not guess. "
        "Two causes fit, and concurrent spawning is a known cause of this exact "
        "connect failure under either (F-834): Chrome's own profile singleton "
        "lets only one process hold a user-data-dir, and N simultaneous Chrome "
        "launches cost CPU, memory and process handles. Any "
        "'running as root / pass no_sandbox=True' advice in the message above "
        "comes from nodriver and does NOT apply here: the sandbox setting is "
        "unrelated to this failure.\n"
        "Serialize the spawns (one spawn_browser at a time, await each before "
        "starting the next), or retry this one once the others have settled."
    )
