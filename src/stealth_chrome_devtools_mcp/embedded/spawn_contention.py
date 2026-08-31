"""THE one home for "did this spawn fail because it was racing sibling spawns,
and what should the caller do about it" (F-834).

nodriver's connect failure ends in *"Possibly because you are running as root?
In that case you need to pass no_sandbox=True"*. Under concurrent spawns that
advice is a **red herring** — the sandbox is already off and root is not
involved — and it cost two independent diagnosing agents real time before the
actual cause (N spawns funnelling into one profile directory) was found. A
failed spawn that overlapped siblings therefore says so, and says so *about* the
advice, so the next reader does not chase it again.

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
        "in this backend when this one failed. Concurrent spawns contend for the "
        "same Chrome profile — only one process may hold a user-data-dir — and "
        "that is a known cause of this exact connect failure (F-834). Any "
        "'running as root / pass no_sandbox=True' advice in the message above "
        "comes from nodriver and does NOT apply here: the sandbox setting is "
        "unrelated to this failure.\n"
        "Serialize the spawns (one spawn_browser at a time, await each before "
        "starting the next), or retry this one once the others have settled."
    )
