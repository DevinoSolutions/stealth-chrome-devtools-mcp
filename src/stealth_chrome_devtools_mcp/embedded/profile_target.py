"""THE one home for "the profile this spawn asked for is already OPEN" —
whose cookie jar comes over, or a refusal by name (F-914, F-915).

**The rule is the owner's, it has exactly two outcomes, and it is not to be
re-litigated.** A browser THIS backend drives can be asked for its cookies over
CDP (``cookie_handoff``), so the spawn goes ahead on a directory of its own and
is seeded from the holder's live jar; anything else — another backend's Chrome,
or the human's own — is REFUSED, because there is no connection of ours to read
that jar through and a file copy of a profile Chrome is writing to carries no
cookies at all (``profile_copy.copy_file`` skips a locked file, and nothing can
enumerate the gap afterwards). "Substitute but report it" was offered and
explicitly not chosen.

**What the substitution cost, measured.** 2.1.12 walked a held NAMED session to
``<name>-N`` and seeded that walk from the SHARED seed, and answered a held
shared session with a fresh clone of the same seed — both reported success,
both logged out, and ``profile_role`` was the only tell. On the owner's machine
that is 17 walked directories, one at ``-22``, i.e. one session name
substituted at least twenty-two times; and the seed those copies came from is
refreshed only while the shared session is CLOSED
(``clone_storage._refresh_master_snapshot_if_safe`` answers ``seed_error:
"default-in-use"`` otherwise), so on a machine whose shared browser stays open
it is frozen at the last clean close — measured at five and a half hours
behind. "The master profile was erased / I have to set up the credentials
again" is that, from the user's chair.

**It is the TARGET-side twin of ``profile_source.seed_source``**, deliberately
reaching the same verdicts through the same witness: a running profile is
usable exactly when we drive it. What differs is only which directory is being
asked about — the one a spawn is about to OPEN rather than the one it copies
FROM — which is why this is its own module rather than a fifth question in that
one, whose whole surface is ``seed_from``. It is a sibling of ``profile_lock``
too, and not a part of it: that module answers *is this directory held, and by
whom*, which is a fact; this one answers *what do we do about it*, which is a
policy, and the two sat welded together in ``clone_storage`` until F-914 needed
the policy in two branches at once.

**The two witnesses are NOT asked in ``profile_source``'s ``driven and held``
order**, and the saving that order buys does not exist here: both call sites
already hold the ``Hold``, because "is it held" is what sends a spawn down this
path at all. The psutil walk is paid either way.

**The refusal names the SESSION and the HOLDER and no PATH** — F-869/F-877's
discipline, because a profile path names the operating user and this message
reaches the client, the durable log and Sentry at once. The name is data rather
than two messages, on ``profile_seed.require_name``'s precedent: the shared
session and a named one differ in one word.

**What a walk MEANS changed with it.** F-871 made the walk to ``<name>-N``
REPORTED (``requested_user_data_dir`` / ``walked_to`` / ``walk_reason``) and
left it unconditional; since F-915 the only walk that survives is one taken
because we drive the holder, so the new directory is copied FROM that holder
with its jar handed over. Every sentence about a walk — the resolver's, the
tool's ``warning``, the CLI's line — has to be true of THAT walk and not of the
seed copy that no longer happens.

**Which is why the walk must land on a directory that does not EXIST yet**, and
why ``clone_storage._next_available_explicit_dir`` grew a ``fresh`` flag that
this path passes and nothing else does (F-914 review). That function skipped a
candidate that was BUSY — a live browser or an in-flight reservation — and
existence was never consulted, while ``resolve_profile_selection`` gates the
copy AND the ``LIVE_SEED_KEY`` stamp on the target not existing. So a CLOSED
``<name>-2`` that an earlier walk left behind was handed back verbatim: nothing
copied, nothing handed over, ``seeded_via`` never set, and the caller given
whatever that directory last held — a STALE THIRD identity, neither the
holder's jar nor a fresh copy of anything, under the tool's own warning saying
its cookies came across. That is this finding's substitution with one extra
step, and it is the SHIPPED state that reaches it: 17 leftover directories on
the owner's machine, one at ``-22``, are exactly what a first walk lands on.
The flag is not the default because for every other caller an existing
directory is precisely what reopening a named session MEANS; and it carries the
attempt token into the timestamp rung so the ladder cannot run out onto an
existing directory at the -99 boundary. A walk that could not land fresh would
have to REFUSE — returning an existing directory on this path is the one thing
it may not do.

A leaf: ``profile_lock`` for the type of the answer it is handed,
``profile_seed`` for the one naming rule, ``tool_errors`` for the convention.
The directories arrive as a ``profile_seed.Roots`` and the "do we drive it"
witness as a callable, so this module never learns where a session root is and
``clone_storage`` stays the only thing that knows.
"""

from collections.abc import Callable
from pathlib import Path

from stealth_chrome_devtools_mcp.embedded import profile_lock, profile_seed
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError


def hand_over_or_refuse(
    requested: Path,
    hold: profile_lock.Hold,
    roots: profile_seed.Roots,
    *,
    driven: Callable[[Path], bool],
) -> Path:
    """The LIVE directory whose jar is handed over, or raise.

    *requested* is the directory the caller asked for and *hold* is what
    ``profile_lock`` says holds it. The module docstring carries the rule, the
    measurement behind it and the reason the message says what it says.
    """
    if driven(requested):
        return requested
    name = profile_seed.seed_name(requested, roots.shared, roots.seed)
    raise ToolError(
        f"the {name!r} session is open in a browser this backend does not "
        f"drive ({hold.reason}), so its cookies cannot be handed over: there "
        "is no CDP connection of ours to read them through, and copying a "
        "profile Chrome is writing to carries no cookies at all — the jar is "
        "held open and skipped, and nothing can say afterwards what was lost. "
        "Nothing was created. Close that browser (`stealthy close "
        "<instance>`) and spawn again to get that session, or pass "
        "session=<a free name> (--session NAME from the CLI) for a NEW "
        f"session of your own — {_fresh_session_holds(name)}."
    )


def _fresh_session_holds(name: str) -> str:
    """What the escape hatch in the refusal above actually hands back.

    The one clause that cannot be shared, as DATA rather than a second message
    (``profile_seed.require_name``'s precedent): every new session is a copy of
    the SEED, and the seed is the shared session's own copyable form —
    ``profile_seed.seed_name`` answers ``default`` for the shared directory and
    for the snapshot alike. So for a NAMED holder the copy carries none of that
    holder's logins, which is worth saying; for the SHARED one it carries
    exactly those logins, as of the last time that browser was closed, because
    the seed is refreshed only while it is closed
    (``clone_storage._refresh_master_snapshot_if_safe``). The shipped sentence
    said "holding none of X's logins" in both branches, so in the machine state
    that is normal for the owner — their own Chrome on the shared session — it
    told them the one remaining way forward loses the logins it in fact keeps.
    """
    if name == profile_seed.DEFAULT_SESSION:
        return (
            "a fresh copy of the seed, which is that session in copyable "
            "form, so its logins come too, as of the last time that browser "
            "was closed"
        )
    return f"a fresh copy of the seed, holding none of the logins in {name!r}"


def still_driven_source(
    recorded: object,
    driven: Callable[[Path], bool],
) -> Path | None:
    """The hand-off a PREVIOUS spawn attempt was making, carried onto the retry.

    Carried at all because the alternative is this finding one spawn failure
    later: the retry copies from the seed again and nothing asks for the jar
    that made the first attempt's session correct.

    **The ``driven`` re-ask is fail-closed insurance and nothing production can
    reach — this docstring claimed otherwise and was wrong.** It is one
    ``cookie_handoff.Driven`` per spawn: an immutable snapshot taken once in
    ``spawn_browser`` and handed unchanged to the first selection and to every
    fallback, and ``clone_storage.LIVE_SEED_KEY`` is only ever stamped on a
    path that same object has already said yes about. So the re-ask answers
    True for every value it can be handed, and a source that CLOSED in the
    meantime is not what it drops.

    Which means the fact with a lifetime is settled where it is actually
    observed, one whole browser launch later:
    ``tool_sections.browser_management._seed_cookies_over_cdp`` re-derives a
    FRESH snapshot at the moment of use, and a source that stopped being ours
    becomes ``seeded_via: "copy"`` plus ``cookie_handoff_error`` rather than a
    failed spawn — deliberate, and pinned in ``tests/test_cookie_handoff.py``
    (``TestEveryReasonAHandOffCanReport``). Re-deciding it here would be a
    second answer to that question against a staler witness.

    *recorded* is whatever the previous selection carried under
    ``clone_storage.LIVE_SEED_KEY`` — that key stays there, with the selection
    dict it belongs to, so this module never learns that dict's shape. A
    previous attempt with no hand-off carries none onto the retry, which is the
    branch that does the work.
    """
    if not isinstance(recorded, str) or not recorded:
        return None
    source = Path(recorded)
    return source if driven(source) else None
