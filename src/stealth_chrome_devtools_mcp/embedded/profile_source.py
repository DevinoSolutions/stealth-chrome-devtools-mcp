"""THE one home for ``seed_from`` / ``--from`` — WHICH session a new session is
copied from, and whether there is a new session for that copy to apply to
(F-897).

Its sibling is ``profile_seed`` and the boundary is one question each. That
module answers what a seed IS (the marker, the login witnesses, the seeds' own
names) and what a caller may SAY about a profile (``session``,
``user_data_dir``, where a request LANDS and whether it may). This one answers
a question that only arises at the instant a session is CREATED: where its
contents come from. The two meet at exactly three points — ``require_name``,
so ``seed_from`` and ``session`` share one name rule rather than two that
agree; ``require_allowed``, so a source is anchored and reserved by the same
gate a target is; and ``seed_sentence``/``provenance``, so the refusal for a
session that already exists names its seed in the words ``stealthy profiles``
uses.

**Why it is its own file and not a fifth section of ``profile_seed``.** It was
one, and the merge that brought F-901 in put the combined module at 1112 lines
of a 1000-line budget that ratchets DOWN only. The cut is this question's
surface rather than a raised cap, on ``dom_handler`` → ``text_entry``'s
precedent and ``cli_call`` → ``cli_render``'s: the line between the two is
WHEN the question is asked. ``profile_seed`` is consulted on every spawn;
nothing here runs unless a caller wrote ``seed_from``. That ``profile_seed``'s
own module docstring still describes four questions without naming this one is
the same seam read from the other side.

It is not ``profile_copy`` either, and the difference there is sharper: that
module owns the MECHANICS of copying a profile directory and knows nothing
about sessions, while everything here is policy about which session may be
named. The two are joined by one measured fact — ``profile_copy.copy_file``
answers a file Chrome holds open by skipping it — which is the whole reason
``seed_source`` refuses a live source BY NAME.

A leaf, and it keeps ``profile_seed``'s invariant unchanged rather than
inheriting a weakened one: the four directories arrive as a ``Roots``, the
containment predicate and the "is a browser holding this" witness arrive as
callables, so this module never learns where a session root is and
``clone_storage`` stays the only thing that knows.
"""

import os.path
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from stealth_chrome_devtools_mcp.embedded import profile_seed
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

#: How long an ADVISORY "is this source open" answer may be reused (review N2).
#: The pre-flight gate that asks it is itself asked twice per seeded spawn —
#: the tool's, then the resolver's own — and the witness behind it is a full
#: psutil cmdline walk, which on a machine that has been running a Chrome
#: fleet is hundreds of processes. One seeded spawn walked the table three
#: times for one source where an unseeded named spawn walks it once.
ADVISORY_HOLD_SECONDS = 1.0

_HOLDS: dict[str, tuple[float, bool]] = {}


def forget_advisory_holds() -> None:
    """Drop every memoised advisory answer. For test isolation."""
    _HOLDS.clear()


def advisory(ask: Callable[[Path], bool]) -> Callable[[Path], bool]:
    """*ask*, as a witness that remembers its answers for the advisory window.
    **ADVISORY callers only** — see `advisory_hold`. A binder rather than a
    stored callable because the witness has to be resolved at CALL time: it is
    a module attribute the suite patches, and one captured at import would
    make a patched liveness answer unreachable.
    """
    return lambda profile_dir: advisory_hold(profile_dir, ask)


def advisory_hold(profile_dir: Path, ask: Callable[[Path], bool]) -> bool:
    """*ask* about *profile_dir*, answered from the memo while it is fresher
    than `ADVISORY_HOLD_SECONDS`. **ADVISORY callers only.**

    Reusing an answer is a WEAKER claim than the advisory ask already makes —
    that answer is discarded, and the read that decides is the statement
    before the copy, which takes the witness itself and never comes here.
    Within one spawn a remembered answer can only ever be a NEGATIVE one: a
    positive raises at the first ask, so nothing reaches the second. Across
    spawns a stale negative lands in exactly the check-to-copy window review
    S1 documented and the pre-copy read refuses it; a stale positive refuses a
    source closed less than a second ago, with the right sentence and the
    right remedy. Both are bounded by the window and by nothing else, which is
    why it is a second rather than a minute.

    Unlocked deliberately: `dict` get and set are atomic under the GIL, every
    caller runs on the event loop, and a lost update costs one extra walk —
    the one thing this exists to save, not anything it is trusted for. The key
    is case-folded and nothing more, because the only thing that reaches here
    is `seed_source`'s source, which is `profile_seed.require_allowed`'s
    answer: already absolute, already normalised. A key that resolved would
    also stat, which is the cost this exists to avoid.
    """
    key = os.path.normcase(str(profile_dir))
    now = time.monotonic()
    seen = _HOLDS.get(key)
    if seen is not None and now - seen[0] < ADVISORY_HOLD_SECONDS:
        return seen[1]
    answer = ask(profile_dir)
    _HOLDS[key] = (now, answer)
    return answer


class SeedSource(NamedTuple):
    """The directory a new session is copied FROM, and what that copy is
    called in the marker and in ``clone_source``. One tuple because the two
    are decided together and a caller that could pick them apart would be able
    to record a copy as having come from somewhere it did not."""

    path: Path
    kind: str


def seed_request(seed_from: str | None) -> str | None:
    """THE one reading of a ``seed_from`` / ``--from`` request (F-897).

    A session NAME, through ``profile_seed.require_name`` — the same rule
    ``session`` passes, because they name the same kind of thing and a
    ``seed_from`` that accepted a path would be a second way to reach a
    directory, one the resolver could not anchor, reserve or record a name
    for. There is deliberately no path door here at all: what ``--from``
    copies has to be a session, because the provenance it writes
    (``seeded_from``) is a word the caller can pass back to ``session=``, and
    an arbitrary directory has no such word.

    **The exactly-empty string is NOT GIVEN**, which is why the test is falsy
    rather than ``is None`` — the same decision ``profile_request`` makes for
    ``session``, for the same measured reason (F-896 delta): an MCP client is
    a language model and ``""`` for an optional string is one of the commonest
    shapes it sends, so refusing it would fail a spawn that asked for nothing.
    Here that is not merely harmless but exactly right — ``seed_from=""`` says
    nothing about where to copy from, and the answer for saying nothing is the
    shared session, which is what an unset ``--from`` already means. Anything
    NON-empty that strips to nothing still raises, through the one sentence
    ``session`` and its alias raise (``profile_seed._names_nothing``, reached
    via ``require_name``): that value cannot have held a separator, so it
    names a session that cannot exist rather than a default that does.
    """
    if not seed_from:
        return None
    return profile_seed.require_name(
        "seed_from",
        seed_from,
        path_hint=(
            "Pick the name of a session to copy — `stealthy profiles` lists "
            "them. There is no path form: a seed has to be a session, because "
            "its name is what the new session records as `seeded_from`."
        ),
    )


def require_new_session(
    requested: str,
    target: Path | None,
    *,
    shared: bool,
    inside_root: bool,
) -> None:
    """Raise unless this ``seed_from`` has a NEW session of its own to apply to.

    ``seed_from`` says where a session's contents come from at the moment it
    is CREATED, so all FOUR refusals here are one sentence read four ways:
    there has to be a session, it has to be the caller's own, it has to be a
    session at all, and it must not already exist.

    The third exists because the answer to it used to be SILENCE (F-897 review
    M1, measured). A ``user_data_dir`` landing outside the clone root is
    opened exactly as it is — the resolver only ever seeds a directory it is
    about to create UNDER the session root — so ``seed_from`` passed every
    gate and was then never used: no copy, no marker, ``seeded_from:
    unknown``, and not one word to the caller. That is this feature's own
    commitment ("``--from`` is never silently dropped") inverted and reached
    through the path door one argument to the left. It is decided HERE rather
    than in the resolver because the rule is this function's — whether there
    is a NEW SESSION for a seed to apply to — and the caller supplies the one
    fact it cannot know, exactly as it does for ``shared``.

    The fourth is the one with a choice in it, and the choice is deliberate.
    The alternatives were a silent no-op — which tells a caller their login
    came from ``work`` when it came from wherever the directory was seeded
    weeks ago — and a RE-SEED, which overwrites a profile whose whole purpose
    is to hold a login the user typed by hand. Refusing is the only answer
    that is neither a lie nor a loss, and it can afford to be loud because the
    remedy is one word: open the session (which is what a spawn without
    ``seed_from`` does), or pick a name that is free. The refusal NAMES where
    the existing session was actually seeded from, so a caller who expected a
    fresh copy learns what they have instead.

    **Every sentence here names BOTH spellings of the remedy** (review N4).
    The CLI prints the backend's message verbatim — one home for the rule —
    so a caller who typed ``stealthy spawn --from work`` and is told to "pass
    ``session=<name>``" goes looking for an argument they never typed. Keying
    the wording on who asked would need a second message home, which is the
    defect this module exists to prevent; naming both costs four words.
    """
    default = profile_seed.DEFAULT_SESSION
    if target is None:
        raise ToolError(
            f"seed_from={requested!r} says what a NEW session is copied from, "
            "so it needs a session of its own: pass session=<name> beside it "
            "(--session NAME from the CLI). A spawn that names no session gets "
            f"the {default!r} session or a disposable copy of it, and "
            "neither is seeded from another session."
        )
    if shared:
        raise ToolError(
            f"seed_from={requested!r} cannot apply to the "
            f"{default!r} session: {default!r} is the session "
            "every other one is seeded FROM, and is nobody's copy. Pick a name "
            "for a new session of your own — session=<name>, or --session NAME "
            "from the CLI."
        )
    if not inside_root:
        raise ToolError(
            f"seed_from={requested!r} copies one SESSION into another, and "
            f"{str(target)!r} is a directory named by path rather than a "
            "session: it is opened exactly as it is, so there is nothing for "
            "seed_from to apply to and it would have been silently ignored. "
            "Pass session=<name> instead (--session NAME from the CLI) — a "
            "session is what carries a name, which is what the copy records."
        )
    if target.exists():
        sentence = profile_seed.seed_sentence(profile_seed.provenance(target))
        raise ToolError(
            f"session {target.name!r} already exists, and seed_from only "
            f"applies when a session is CREATED — it was "
            f"{sentence}. Pass session="
            f"{target.name!r} on its own to open it with the cookies and "
            f"logins it already holds, or pick a free name for a fresh copy "
            f"of {requested!r}. (--session NAME from the CLI.)"
        )


def seed_source(
    requested: str | None,
    roots: profile_seed.Roots,
    inside: Callable[[Path, Path], bool],
    *,
    held: Callable[[Path], bool],
) -> SeedSource:
    """WHICH directory a new session is copied from, and what that copy is
    called (F-897). ``None`` means the same thing as ``"default"``, so the two
    cannot develop separate behaviour.

    **The shared session is the one source that stays copyable while its own
    browser runs, and that is a fact about the MECHANISM rather than a
    privilege of the word.** The product keeps a separate, closed, copyable
    form of it — the seed — refreshed whenever the shared profile is free
    (F-892/F-893), so a copy taken from it is a copy of a directory nothing is
    writing to. The fallback to the LIVE shared directory when no seed exists
    yet (first run) is 2.1.11's behaviour and is kept deliberately rather than
    widened into the refusal below: it is the path that CREATES the first
    seed, and refusing it would make a fresh installation unable to make its
    first named session. What it costs is named in ``profile_copy``'s
    docstring — a locked file is skipped and nothing can say which.

    **Every other session is refused while it is open, BY NAME.** It has no
    seed, so the copy would be of the live directory itself, and
    ``profile_copy.copy_file`` answers a locked file by skipping it with a
    warning: on Windows that is every file Chrome holds, and everywhere it is
    a WAL-mode cookie jar mid-transaction. The caller would be handed a
    session that looks complete and is missing exactly the logins they wanted,
    with no way for us to enumerate the gap. A refusal naming the session and
    the remedy is worth more than a copy nobody can trust. F-898 is where a
    RUNNING source becomes copyable — a CDP cookie hand-off out of the live
    browser rather than a file copy — and is deliberately not built here.

    Per-session seeds were the other candidate and are deliberately NOT built:
    each costs ~0.47 GB, and each needs its own refresh trigger, its own
    staleness witness and its own in-use rule — a second copy of the lifecycle
    F-892 and F-893 spent two findings getting right for ONE seed, bought to
    avoid a refusal whose remedy is closing a window.
    """
    default = profile_seed.DEFAULT_SESSION
    if requested is None or profile_seed.is_default_name(requested):
        if roots.seed.exists():
            return SeedSource(roots.seed, "explicit-default-seed")
        return SeedSource(roots.shared, "explicit-default")
    source = profile_seed.require_allowed(requested, roots, inside)
    if not source.exists():
        raise ToolError(
            f"seed_from={requested!r} is not a session: there is nothing at "
            f"{source}. `stealthy profiles` lists the sessions you have; omit "
            f"seed_from to copy the {default!r} session."
        )
    if held(source):
        raise ToolError(
            f"seed_from={requested!r} is open in a browser right now. Copying "
            "a profile Chrome is writing to silently drops whatever it has "
            "locked — which is where the logins are — and nothing can say "
            f"afterwards what was lost. Close the {requested!r} session first "
            f"(`stealthy close <instance>`), or seed from "
            f"{default!r}, which the product keeps a separate "
            "copyable form of."
        )
    return SeedSource(source, "explicit-session")
