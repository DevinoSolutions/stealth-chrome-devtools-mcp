"""THE one home for a profile's SEED — the marker that records it, the files
that witness a login in it, and the seeds' own names (F-892, F-894, F-895).

One subject, three questions that were previously answered in three places or
not at all:

**Which files witness a login?** ``LOGIN_WITNESSES``. This existed as a literal
tuple inside ``clone_storage._snapshot_needs_refresh`` and it named
``Default/Cookies`` — a path Chrome has not written since version 96, when the
cookie jar moved to ``Default/Network/Cookies``. MEASURED on 2026-09-20, on both
the live master profile and its snapshot: ``Default/Cookies`` is **ABSENT** and
``Default/Network/Cookies`` is 524,288 B. So the one witness that catches a pure
**cookie** login — Google SSO, Amazon Seller Central, any SPA that never offers
to save a password — never fired, and the ``pre-clone-stale`` refresh depended
entirely on ``Login Data`` (written only when Chrome saves a password) or
``Web Data`` (autofill). The legacy path is KEPT beside the current one rather
than replaced: a profile carried over from a pre-96 Chrome still has it, the
stat costs nothing, and an absent file is simply skipped.

**Where did this profile come from, and has that source moved on?**
``provenance``. The marker already carried ``source`` / ``source_kind`` /
``created_at``; F-895 adds ``seeded_from`` (the seed's NAME, the word a user can
say) and ``seeded_at``, and reports ``seed_changed_since`` by asking
``LOGIN_WITNESSES`` — the SAME list, never a second one — whether the seed has
been written to since. A marker carrying neither new key is a legacy marker and
reads as ``seeded_from: "unknown"`` with ``seed_changed_since: None``: unknown
provenance is never reported as fresh, and ``created_at`` is deliberately NOT
substituted for ``seeded_at``, because a timestamp that says when the DIRECTORY
was made is not a claim about which seed it was made from.

**Which directories are the product's own seeds?** ``RESERVED_NAMES`` /
``reserved_reason``. A bare relative ``user_data_dir`` is anchored under the
clone root, so ``user_data_dir="master"`` resolved to ``<root>/sessions/master``
and NOT ``<root>/master`` — MEASURED: that directory exists, 0.46 GB, marker
``explicit-master-snapshot``, created 2026-09-11. Someone asked for the shared
profile by its documented name and silently got a nine-day-old clone of its
seed. The seed PATH is refused for a second reason as well: a browser driven
on it writes into the seed every later session copies from, which is how
F-893's precondition arose.

**And which word does a caller say?** ``DEFAULT_SESSION`` / ``profile_request``
(F-896). F-894 reserved ``default`` as a refusal, explicitly ahead of the
vocabulary that would use it; this is that vocabulary, so the word now MEANS
the shared profile rather than refusing. It stays reserved in the sense that
matters — it names one directory, and any spelling that would create a second
one under it is refused — which is what keeps F-894's trap closed rather than
re-opening it under a friendlier word. ``profile_request`` is the one reading
of the two spellings a caller may use, so the alias ``user_data_dir`` resolves
TO ``session`` rather than running beside it.

A leaf: stdlib plus ``tool_errors``, whose whole contract is to import nothing
from ``embedded``. The master and snapshot directories, the profile and the
``inside`` predicate all arrive as ARGUMENTS, so this module never learns where
a session root is and ``clone_storage`` stays the one home for that.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import NamedTuple

from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

MARKER_NAME = ".stealth_chrome_devtools_mcp_clone.json"


class Roots(NamedTuple):
    """The four directories a profile request is decided against.

    One tuple rather than four parameters because they always travel together
    and mean nothing apart: which root a relative name anchors under, which
    root keeps it inside, which directory IS the shared session, and which is
    that session's seed. ``clone_storage`` owns WHERE they are — it is the one
    thing this module is never told — and passes them in.
    """

    session: Path
    clones: Path
    shared: Path
    seed: Path


# F-892: the profile-relative files whose mtime moves when a login lands. THE
# one spelling of each — ``tests/test_profile_seed_truth.py`` fails if any of
# them is written out a second time anywhere under the package.
# ``Default/Network/Cookies`` is Chrome >= 96's cookie jar and leads because it
# is the one that exists; ``Default/Cookies`` is the pre-96 location and is kept
# for a profile carried over from one.
LOGIN_WITNESSES: tuple[str, ...] = (
    "Default/Network/Cookies",
    "Default/Cookies",
    "Default/Login Data",
    "Default/Web Data",
)

# What a marker that predates F-895 says about its seed: nothing at all.
UNKNOWN_SEED = "unknown"

# The MECHANISM's own directory names. A caller may not hand one of these to
# ``session`` or ``user_data_dir`` (F-894): anchoring them under the clone root
# hands back a different profile under the name the caller used.
RESERVED_NAMES = frozenset({"master", "master-snapshot"})

# The shared session — the one a spawn that names none lands on, and the one
# every new session is copied from. F-894 reserved this word as a refusal,
# deliberately ahead of the vocabulary that would use it; F-896 is that
# vocabulary, so the word now MEANS the shared profile instead of refusing.
# It is still reserved in the sense that matters: it names exactly one
# directory and a caller may not create a session of their own called it.
DEFAULT_SESSION = "default"

# The words above, as a set no SPELLING may fold onto. Windows strips a
# trailing dot or space from a path component, so `default.` is created as
# `default` — a second way to the one directory the word names, through the one
# door the `default` refusal exists to close. `fold` is applied on every
# platform: a name that means one directory here and another there is not a
# name, and a rule that fires on one host only is the flavour mistake F-894
# already paid for once.
FOLDED_NAMES = RESERVED_NAMES | {DEFAULT_SESSION}

_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def stamp_now() -> str:
    """The marker's timestamp format: UTC, second precision, ``Z`` suffix."""
    return datetime.now(UTC).strftime(_STAMP_FORMAT)


def moment(stamp: object) -> float | None:
    """A marker stamp as epoch seconds, or None when it is not one."""
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.strptime(stamp, _STAMP_FORMAT).replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


def newest_login_write(profile: Path) -> float | None:
    """The newest mtime among ``LOGIN_WITNESSES`` in *profile*, or None when the
    profile has none of them (a directory that is not a Chrome profile, or one
    that has never been logged in to)."""
    newest: float | None = None
    for rel in LOGIN_WITNESSES:
        witness = profile / rel
        try:
            if not witness.exists():
                continue
            written = witness.stat().st_mtime
        except OSError:
            continue
        if newest is None or written > newest:
            newest = written
    return newest


def changed_since(profile: Path, stamp: object) -> bool | None:
    """Has *profile* taken a login write since *stamp*? None when unknowable.

    ``int(...)`` on the mtime because the stamp is truncated to the second: a
    file copied within the same second as the stamp is not evidence of a later
    login, and reporting one would mark every fresh clone as already stale.
    """
    at = moment(stamp)
    if at is None:
        return None
    written = newest_login_write(profile)
    if written is None:
        return None
    return int(written) > at


def needs_refresh(profile: Path, seed: Path) -> bool:
    """Has *profile* taken a login write since *seed* was last copied from it?

    The witnesses are ``LOGIN_WITNESSES`` and the "when" is the seed MARKER's
    mtime — the marker is written at the end of a copy, so it dates the copy
    itself. Stat-only, so it is safe to ask before one. A seed that does not
    exist needs no refresh (creating it is another path's job); one with no
    marker always does, because nothing dates it. It lives here rather than in
    ``clone_storage`` because every term in it is this module's: the list, the
    marker and what counts as a login. The caller binds it to its own two
    directories (F-892, moved F-896)."""
    if not seed.exists():
        return False
    marker = seed / MARKER_NAME
    if not marker.exists():
        return True
    try:
        written = newest_login_write(profile)
        return written is not None and written > marker.stat().st_mtime
    except OSError:
        return False


def read_marker(profile: Path) -> dict[str, object]:
    """The clone marker in *profile* as a dict — ``{}`` for a directory with no
    marker, an unreadable one, or one that is not a JSON object. Every reader
    here treats an unreadable marker as an absent one: it is the direction that
    keeps a named profile (F-201's ``clone_is_auto`` ruling)."""
    try:
        data = json.loads((profile / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def seed_name(source: Path, master: Path, snapshot: Path) -> str:
    """The seed's NAME rather than its path — what a profile was copied from.

    The shared profile and its copyable form answer with ONE name,
    ``default``, and that is the F-896 rename F-895 said this field existed to
    receive. They are one session to a user: which of the two directories a
    given copy was physically taken from is a mechanism detail — the snapshot
    exists only because a live profile cannot always be copied — and a user
    told ``seeded from master-snapshot`` learns a word they can neither type
    nor act on. What they CAN act on is ``seeded from default``, because
    ``default`` is a session they can open. Staleness does not go with it:
    ``provenance`` reads ``seed_changed_since`` off the marker's recorded
    ``source`` PATH, so the two directories stay distinguishable exactly where
    the distinction is load-bearing. Anything else is its directory name,
    which is what a per-session seed will be (F-897).
    """
    if same_dir(source, snapshot) or same_dir(source, master):
        return DEFAULT_SESSION
    return source.name


def write_marker(
    target: Path, *, source: Path, source_kind: str, seeded_from: str
) -> dict[str, object]:
    """Write *target*'s clone marker and return what was written.

    ``auto_clean`` is the disposability flag ``clone_is_auto`` reads; the
    ``explicit`` source-kind prefix is what a user-named profile carries.
    """
    created = stamp_now()
    marker: dict[str, object] = {
        "source": str(source),
        "source_kind": source_kind,
        "created_at": created,
        # Disposable auto-clones may be reclaimed by the storage-cap sweep;
        # explicit/named profiles (explicit-* source kinds) never are.
        "auto_clean": not str(source_kind).startswith("explicit"),
        # F-895: the seed by NAME, and when this copy was taken from it. Written
        # beside the two legacy keys rather than replacing them, so a 2.1.10
        # reader still finds what it looks for.
        "seeded_from": seeded_from,
        "seeded_at": created,
    }
    (target / MARKER_NAME).write_text(json.dumps(marker, indent=2), encoding="utf-8")
    return marker


def is_auto(profile: Path) -> bool:
    """True only for server-created disposable auto-clones.

    Never true for user-named/explicit profiles (they persist by design) or for
    directories the server did not create (no clone marker). Disposability is
    carried by an explicit ``auto_clean`` flag written at clone time.

    Fail-safe on legacy markers: a marker that predates the ``auto_clean`` flag
    is NEVER treated as disposable. The old source-kind fallback
    (``not source_kind.startswith("explicit")``) misjudged user-named profiles
    cloned from a plain ``master-snapshot`` as auto and let the storage-cap sweep
    permanently delete a logged-in business session — a silent, unrecoverable
    loss. Wrongly keeping a stale auto-clone only costs bounded disk, so the
    ambiguity resolves to "keep".
    """
    return bool(read_marker(profile).get("auto_clean", False))


def is_named(profile: Path) -> bool:
    """True for user-named/explicit profiles (the persistent ones). They are
    never deleted, but they *can* be trimmed of regenerable data when idle."""
    data = read_marker(profile)
    if not data:
        return False
    if "auto_clean" in data:
        return not bool(data["auto_clean"])
    return str(data.get("source_kind", "")).startswith("explicit")


def provenance(profile: Path) -> dict[str, object]:
    """What *profile*'s marker says about its seed, and whether that seed has
    moved on since — the three fields ``spawn_diagnostics.profile_selection``
    and the ``profiles`` CLI verb both report (F-895).

    A profile with no marker at all (the master itself, or a directory a caller
    named by absolute path) is nobody's copy, and reads exactly as a legacy
    marker does: unknown, with no claim either way about staleness.
    """
    data = read_marker(profile)
    seeded_at = data.get("seeded_at")
    source = data.get("source")
    changed = (
        changed_since(Path(source), seeded_at)
        if isinstance(source, str) and source
        else None
    )
    return {
        "seeded_from": data.get("seeded_from") or UNKNOWN_SEED,
        "seeded_at": seeded_at if isinstance(seeded_at, str) else None,
        "seed_changed_since": changed,
    }


def seed_sentence(fields: dict[str, object]) -> str:
    """:func:`provenance`'s three fields as ONE sentence (F-897).

    It exists because two surfaces say this out loud — ``stealthy profiles``
    lists every session's seed, and ``stealthy spawn --from X`` has to show
    that the flag did something — and two phrasings of one marker would drift
    the day a fourth field appears. It is PHRASING only: whether the question
    arises at all is the caller's, because the two callers answer it
    differently and both are right (``cli._seed_line`` prints an unmarked
    SESSION directory as unknown, which is the finding worth reading in a
    listing, while a spawn omits the line rather than claim a seed for the
    shared profile, which is nobody's copy).

    "seed changed since" is printed only when it is True: False is the
    ordinary case and would be noise, and None means the marker could not say
    — reported as an unknown WHEN rather than as a fresh seed, because a
    profile frozen since August reading "up to date" is the silence F-895
    closed.
    """
    seeded_from = fields.get("seeded_from") or UNKNOWN_SEED
    seeded_at = fields.get("seeded_at")
    if not seeded_at:
        return f"seeded from {seeded_from} (when: unknown)"
    sentence = f"seeded from {seeded_from} at {seeded_at}"
    return sentence + (
        "  SEED CHANGED SINCE" if fields.get("seed_changed_since") else ""
    )


def is_default_name(requested: str) -> bool:
    """Is this request the bare word ``default`` — the shared session?

    The BARE form only, and that is what keeps F-894 closed under the new
    vocabulary: ``sessions/default`` is a relative path naming a directory of
    the caller's own making, and answering the shared profile for it would be
    the same silent substitution the finding is about, one separator away.
    That spelling is refused instead, in ``reserved_reason``.
    """
    asked = Path(requested.strip())
    return (
        not asked.is_absolute()
        and len(asked.parts) == 1
        and asked.name.casefold() == DEFAULT_SESSION
    )


def anchor(requested: str, roots: Roots, inside: Callable[[Path, Path], bool]) -> Path:
    """WHERE a ``session`` / ``user_data_dir`` request lands on disk.

    The bare name ``default`` is the shared profile itself (F-896) — checked
    first, because anchoring it would make it ``sessions/default``, a
    different directory under the word the vocabulary now teaches. An absolute
    path is itself. Any other relative one is anchored against the session
    root, and kept there only if that already puts it inside the clone root
    (``"sessions/acme"``); otherwise the clone root is prepended, so a bare
    ``"acme"`` becomes ``sessions/acme`` without ``sessions/sessions/acme``.

    It sits here, one call above ``reserved_reason``, so that function can be
    asked about the directory a request MEANS rather than the string it was
    written as — the two answers differ for every relative path, and the
    reservation is about which directory is being opened. ``inside`` arrives as
    an argument because ``clone_storage._is_relative_to`` is its one home.
    """
    if is_default_name(requested):
        return roots.shared
    asked = Path(requested).expanduser()
    if asked.is_absolute():
        return asked
    anchored = roots.session / asked
    return anchored if inside(anchored, roots.clones) else roots.clones / asked


def is_bare_name(value: str) -> bool:
    """Is this string a NAME rather than something with a path in it?

    THE one home for that half of the question, because two callers ask it for
    opposite purposes and must not answer differently: ``profile_request``
    REFUSES a ``session`` that is not one, and strips whitespace off an alias
    only when it IS one. Both separators are tested literally — a backslash is
    a separator on Windows and a legal filename character on POSIX — and the
    drive through ``PureWindowsPath``, because a drive is a Windows concept and
    ``PurePosixPath("C:foo").drive`` is ``""`` (measured), so reading the
    host's flavour would make the answer differ per platform for one string.
    """
    return "/" not in value and "\\" not in value and not PureWindowsPath(value).drive


def _names_nothing(spelling: str, given: str) -> ToolError:
    """The ONE sentence every field that takes a NAME gets for a value that
    names no profile.

    One function, because two spellings answering an empty request differently
    is the asymmetry this module exists to remove — and they did:
    ``session="   "`` raised while ``user_data_dir="   "`` was ANCHORED, which
    on Windows resolves to the clone root itself (the trailing spaces are
    stripped from the component), so Chrome would have been handed the
    directory that holds every session as its profile.

    It raises rather than falling back to an unnamed spawn: silently turning a
    malformed request into the shared session is a substitution, which is the
    thing this vocabulary exists to stop. What it never covers is the
    exactly-EMPTY string, which is "not given" and is answered long before
    here — see ``profile_request`` and ``seed_request``.

    **Three fields ask it, not two** (F-897): ``session`` and its alias, and
    ``seed_from``, which reaches it through ``require_name``. The sentence is
    IDENTICAL for all three down to its last clause, and that clause is true
    of each — omitting ``session`` opens the shared session, omitting
    ``seed_from`` copies it. A per-field variant would be a second way to say
    one thing, and the field name the sentence opens with is already what
    tells the caller which argument to go and edit.
    """
    return ToolError(
        f"{spelling} must be a name and {given!r} names no profile. Omit it "
        f"entirely to use the {DEFAULT_SESSION!r} session."
    )


def require_name(field: str, value: str, *, path_hint: str) -> str:
    """THE one refusal of a value that has to be a session NAME, and the one
    strip that makes it one (F-896, generalised by F-897).

    Two callers ask it about two different parameters — ``session``, which
    says which profile to OPEN, and ``seed_from``, which says which one to
    COPY — and a name is the same thing for both. The FIELD is data rather
    than a second function, because the two messages must name the parameter
    the caller actually typed: a caller told about ``session`` when they wrote
    ``seed_from`` goes and edits the wrong argument, which is the class of
    mistake this file's neighbours keep closing.

    The names-nothing refusal is ``_names_nothing``'s and is NOT a hint this
    function composes: it is one sentence shared with the alias arm of
    ``profile_request``, which cannot come through here because
    ``user_data_dir`` is also the path door. Only the PATH refusal takes a
    hint, and it has to, because the escape differs — ``session`` has
    ``user_data_dir`` and ``seed_from`` has none, since you cannot seed from a
    directory that is not a session.

    "Has a path in it" is ``is_bare_name``'s, so the three sites that ask can
    never answer differently; the three shapes added here are refusals rather
    than shape — an absolute path with no separator or drive under some
    flavour, a ``~`` this layer does not expand, and a name that is only dots.
    The strip in front of them is also what makes ``" C:foo"`` refusable: the
    drive is read off the STRIPPED name, so one leading space cannot hide it
    (F-896 delta review N, reached here by every field rather than by one).
    """
    name = value.strip()
    if not name:
        raise _names_nothing(field, value)
    if (
        not is_bare_name(name)
        or Path(name).is_absolute()
        or name.startswith("~")
        or set(name) == {"."}
    ):
        raise ToolError(
            f"{field} takes a NAME, not a path, and {value!r} is a path. {path_hint}"
        )
    return name


def profile_request(session: str | None, user_data_dir: str | None) -> str | None:
    """THE one reading of the two spellings a caller may use (F-896).

    ``session`` is the documented one and ``user_data_dir`` the deprecated
    alias, and the alias RESOLVES TO it rather than running beside it: this
    function answers ONE string, so everything downstream — the reserved-name
    gate, the re-attach, the resolver — has a single input and cannot develop
    two opinions about which spelling wins. That is convention 4 applied to a
    parameter rather than to a module.

    It is also the ONE normaliser, and what it normalises is a NAME. Surrounding
    whitespace is stripped off both spellings when the value is a bare name,
    because it was stripped off ``session`` alone and that made ``" default "``
    the shared profile while ``" acme "`` was a directory with literal spaces in
    its name — one function pair with two answers to what a name is (review N5).

    **A path-shaped alias passes through byte-for-byte** (delta review S), and
    the two are told apart by ``is_bare_name`` rather than by trying: whitespace
    is noise around a name and a CHARACTER inside a path. ``user_data_dir`` is
    the path door as well as the deprecated name, so stripping the whole string
    made ``/home/me/work/trailing `` open ``/home/me/work/trailing`` — two
    directories on POSIX — and turned ``" /tmp/x"``, whose parts are
    ``(' ', 'tmp', 'x')`` and which 2.1.11 anchored inside the clone root, into
    a ROOTED string that ``roots.session / asked`` resets to the drive root, so
    the request left the session tree. Both measured.

    A NON-EMPTY value that is empty once stripped is neither — it names no
    profile — and both spellings raise for it through ``_names_nothing``; a
    string that short cannot have held a separator, so that rule can never
    divert a path.

    **The exactly-empty string is NOT GIVEN, through either spelling**, which
    is why both tests here are falsy rather than ``is None``. An MCP client is
    a language model and ``""`` for an optional string is one of the commonest
    shapes it sends; 2.1.11 honours it as "nothing asked for", and refusing it
    would break spawns that work today for a caller who asked for nothing. It
    cannot reach the hazard ``_names_nothing`` exists for either — that needs a
    non-empty string the filesystem folds away — so the two rules do not
    overlap. An empty ``session`` therefore leaves the decision to the alias
    beside it, exactly as an absent one does.

    Two rules, and each exists because its absence is a silence:

    * **Both given with different values is refused.** A precedence would pick
      one and say nothing; the caller who typed two different profiles meant
      one of them and cannot tell which they got. Both given with the SAME
      value is honoured — refusing someone who said one thing twice buys
      nothing.
    * **``session`` takes a NAME and refuses a path.** "A session named
      ``C:\\Users\\me\\profile``" is not a sentence, and the path door stays
      open through the alias and through ``stealthy call``, which the refusal
      says. "Has a path in it" is ``is_bare_name``'s, so this refusal and the
      alias's strip cannot answer it differently; the three shapes it does not
      cover are added here because they are refusals rather than shape — an
      absolute path with no separator or drive under some flavour, a ``~`` this
      layer does not expand, and a name that is only dots.
    """
    if not session:
        if not user_data_dir:
            return None
        bare = user_data_dir.strip()
        if not bare:
            raise _names_nothing("user_data_dir", user_data_dir)
        return bare if is_bare_name(bare) else user_data_dir
    name = require_name(
        "session",
        session,
        path_hint=(
            "Pick a name (letters, digits, dashes), or open a directory by "
            "path with user_data_dir=<path> — `stealthy call spawn_browser "
            "--arg user_data_dir=<path>` from the CLI."
        ),
    )
    if user_data_dir and user_data_dir.strip() != name:
        raise ToolError(
            f"session={session!r} and user_data_dir={user_data_dir!r} name two "
            "different profiles and only one browser is being spawned. Pass "
            "session alone — user_data_dir is the deprecated spelling of the "
            "same argument."
        )
    return name


def require_allowed(
    requested: str, roots: Roots, inside: Callable[[Path, Path], bool]
) -> Path:
    """Raise ``ToolError`` when this ``user_data_dir`` may not be honoured, and
    answer WHERE it lands when it may.

    THE one gate, and it is deliberately callable from TWO places (F-894 review
    M1). Asking it only inside ``resolve_profile_selection`` was not enough:
    ``browser_reattach.adopt_held_profile`` runs in FRONT of profile selection
    (F-888) and matches the requested directory against live browsers, so an
    absolute snapshot path with a browser on it — the exact state F-893 is
    about — was RE-ATTACHED to and the resolver never saw the request. So
    ``spawn_browser`` asks first, ahead of its other pre-flight guard, and the
    resolver asks again because it is public and has its own callers.

    Asking twice is free and correct: it is a path decision plus at most one
    ``Path.resolve`` and one ``exists()`` — the stat ``reserved_reason`` takes
    to decide whether there is an existing directory to name an escape to — so
    two calls are two stats, and one home for the rule is worth more than one
    call. It ANSWERS the anchored path so the resolver does not anchor a second
    time for the same request (review n6); ``anchor`` keeps its one home and is
    now reached once per selection. The SHARED profile passes here — by
    absolute path, and since F-896 by the name ``default`` as well — because
    the resolver, not this gate, is where it becomes the shared ROLE, and it
    must still reach the re-attach in front of it, where being adopted is the
    right outcome.
    """
    resolved = anchor(requested, roots, inside)
    refusal = reserved_reason(requested, resolved, roots)
    if refusal is not None:
        raise ToolError(f"profile request rejected: {refusal}")
    return resolved


def reserved_reason(requested: str, resolved: Path, roots: Roots) -> str | None:
    """Why this profile request may not be honoured, or None (F-894, F-896).

    Five refusals, and one deliberate non-refusal. A reserved NAME is refused
    because anchoring it under the clone root hands back a different profile
    under the name the caller used. The SEED path is refused because a browser
    driven there writes into the seed every later session copies from.
    A drive-qualified path that is not absolute (``C:foo``, what a Windows
    absolute path becomes once a lenient string layer has eaten its backslashes
    — ``\\s`` is nobody's escape) is refused because ``Path.is_absolute()`` is
    False for it, so the resolver treats a drive-qualified path as a bare name
    and anchors it: MEASURED, that is exactly how
    ``sessions/stealth-mcp-browser-sessionssessionsstealth-chrome-devtools-mcp-…``
    came to exist. **The two halves of that test read two different flavours on
    purpose.** A drive is a Windows concept and ``PurePosixPath("C:foo").drive``
    is ``""`` (measured), so reading it through the HOST's flavour made the
    refusal a Windows-only rule and the pin RED on all six POSIX CI cells; the
    drive is therefore read through ``PureWindowsPath``, which on Windows is the
    host flavour and changes nothing there. "Would this be ANCHORED rather than
    opened", though, is a question about the flavour the resolver actually
    anchors with, so it stays plain ``Path`` — and ``anchor`` keeps its one home.
    For ``C:foo`` both flavours answer "not absolute", which is what makes the
    refusal identical on every platform.

    The FOURTH refusal is F-896's and it is what keeps F-894 closed under the
    new word: ``default`` now MEANS the shared profile, but only in its bare
    form, so a RELATIVE spelling that would instead CREATE a directory called
    ``default`` (``sessions/default``) is refused rather than quietly made.
    Without it the finding's exact trap — a documented name that silently opens
    a different profile — would come back one separator away from the word the
    vocabulary teaches. ``./default`` is NOT one of them and is not refused:
    it normalises to the bare name under both flavours and makes nothing.

    **It is gated on the request being RELATIVE, exactly as the reserved-name
    refusal above it is** (review M2). An absolute path whose basename happens
    to be ``default`` — Chrome's own per-profile folder is literally called
    ``Default`` — is a directory the caller already has, outside this tree; it
    creates no second session under the word, 2.1.11 opened it, and the
    refusal's own escape ("pass ``session='default'``") would send them to a
    completely different profile. It is also what makes RUNBOOK's recovery
    paragraph true as written: an existing ``sessions/default`` keeps its
    contents and stays openable by its absolute path, the same escape the
    reserved-name refusal already offers for ``sessions/master``.

    The FIFTH is S1's and it is the fourth one's own door, one character away:
    a name the FILESYSTEM folds onto any of ``FOLDED_NAMES`` is refused, because
    Windows strips a trailing dot or space from a path component and
    ``session="default."`` therefore reached ``sessions/default`` — a second
    directory under the word — while being a different string from every rule
    above (measured; and ``Path.resolve(strict=False)`` does not normalise the
    dot for a path that does not exist yet, so ``same_dir`` cannot catch it
    either). It is asked BEFORE the reserved-name clause so ``master.`` is told
    what it actually is rather than being quoted back a name it did not type.

    The SHARED profile itself is deliberately NOT refused: driving it directly
    is how a human logs in, and the caller who names it (by path, or by
    ``default``) gets the shared ROLE, which the resolver settles.
    ``roots.shared`` is read here only to tell those two apart.
    """
    asked = Path(requested)
    # `.strip()` here is a TEST and never a normalisation — `asked`, and so
    # every answer about where the request LANDS, still reads the string as it
    # arrived. Without it one leading space hid the drive from this rule:
    # `"C:foo"` was refused while `" C:foo"` was anchored as a session name,
    # which is F-894's own shape wearing a space (delta review N).
    if PureWindowsPath(requested.strip()).drive and not asked.is_absolute():
        return (
            f"{requested!r} names a drive but is not an absolute path, so it "
            "would be created as a session name rather than opened. Pass a "
            "path this host reads as absolute, or a bare session name."
        )
    name = asked.name.casefold()
    folded = name.rstrip(". ")
    if not asked.is_absolute() and folded != name and folded in FOLDED_NAMES:
        return (
            f"{name!r} is {folded!r} once the filesystem has had it: Windows "
            "strips a trailing dot or space from a path component, so this "
            f"would reach {folded!r} rather than a session of its own name. "
            "Drop the trailing dot or space, or pick a different name."
        )
    # Past that clause `folded == name` always, since it returns for every
    # spelling where they differ and lands in `FOLDED_NAMES`. The two rules
    # below are spelled on `folded` anyway so the set they test against and the
    # value they test stay one pair; nothing here does a second fold.
    if not asked.is_absolute() and folded in RESERVED_NAMES:
        # F-894 review M3: an operator may ALREADY have a session directory of
        # this name — one exists on the machine the finding was measured on —
        # and "pick another name" is advice for a new session, not for theirs.
        # The escape is named only when there is something to escape to.
        existing = (
            f" The existing directory at {resolved} is still openable by its "
            "absolute path, or rename it to a name that is not reserved."
            if resolved.exists()
            else ""
        )
        return (
            f"{name!r} is a reserved profile name and never means a session of "
            f"that name. Pass session={DEFAULT_SESSION!r} (or no session at "
            "all) for the shared profile every session is seeded from; pick "
            f"another name for a session of your own.{existing}"
        )
    if (
        not asked.is_absolute()
        and folded == DEFAULT_SESSION
        and not same_dir(resolved, roots.shared)
    ):
        return (
            f"{DEFAULT_SESSION!r} is the shared session and names exactly one "
            f"profile, so {requested!r} would create a second one under the "
            f"same word. Pass session={DEFAULT_SESSION!r} on its own to open "
            "it; a session of your own needs a different name."
        )
    if same_dir(resolved, roots.seed):
        return (
            "that path is the shared seed every new session is copied from, and "
            "a browser running on it writes into the seed. Pass "
            f"session={DEFAULT_SESSION!r} to open the shared profile itself."
        )
    return None


def same_dir(left: Path, right: Path) -> bool:
    """Case- and separator-insensitive path equality, without requiring either
    to exist (``Path.samefile`` needs both)."""
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except OSError:
        return False


# ── seed_from: which session a NEW session is copied from (F-897) ────────────


class SeedSource(NamedTuple):
    """The directory a new session is copied FROM, and what that copy is
    called in the marker and in ``clone_source``. One tuple because the two
    are decided together and a caller that could pick them apart would be able
    to record a copy as having come from somewhere it did not."""

    path: Path
    kind: str


def seed_request(seed_from: str | None) -> str | None:
    """THE one reading of a ``seed_from`` / ``--from`` request (F-897).

    A session NAME, through ``require_name`` — the same rule ``session``
    passes, because they name the same kind of thing and a ``seed_from`` that
    accepted a path would be a second way to reach a directory, one the
    resolver could not anchor, reserve or record a name for. There is
    deliberately no path door here at all: what ``--from`` copies has to be a
    session, because the provenance it writes (``seeded_from``) is a word the
    caller can pass back to ``session=``, and an arbitrary directory has no
    such word.

    **The exactly-empty string is NOT GIVEN**, which is why the test is falsy
    rather than ``is None`` — the same decision ``profile_request`` makes for
    ``session``, for the same measured reason (F-896 delta): an MCP client is
    a language model and ``""`` for an optional string is one of the commonest
    shapes it sends, so refusing it would fail a spawn that asked for nothing.
    Here that is not merely harmless but exactly right — ``seed_from=""`` says
    nothing about where to copy from, and the answer for saying nothing is the
    shared session, which is what an unset ``--from`` already means. Anything
    NON-empty that strips to nothing still raises, through the one sentence
    ``session`` and its alias raise (``_names_nothing``, reached via
    ``require_name``): that value cannot have held a separator, so it names a
    session that cannot exist rather than a default that does.
    """
    if not seed_from:
        return None
    return require_name(
        "seed_from",
        seed_from,
        path_hint=(
            "Pick the name of a session to copy — `stealthy profiles` lists "
            "them. There is no path form: a seed has to be a session, because "
            "its name is what the new session records as `seeded_from`."
        ),
    )


def require_new_session(requested: str, target: Path | None, *, shared: bool) -> None:
    """Raise unless this ``seed_from`` has a NEW session of its own to apply to.

    ``seed_from`` says where a session's contents come from at the moment it
    is CREATED, so all three refusals here are one sentence read three ways:
    there has to be a session, it has to be the caller's own, and it must not
    already exist.

    The third is the one with a choice in it, and the choice is deliberate.
    The alternatives were a silent no-op — which tells a caller their login
    came from ``work`` when it came from wherever the directory was seeded
    weeks ago — and a RE-SEED, which overwrites a profile whose whole purpose
    is to hold a login the user typed by hand. Refusing is the only answer
    that is neither a lie nor a loss, and it can afford to be loud because the
    remedy is one word: open the session (which is what a spawn without
    ``seed_from`` does), or pick a name that is free. The refusal NAMES where
    the existing session was actually seeded from, so a caller who expected a
    fresh copy learns what they have instead.
    """
    if target is None:
        raise ToolError(
            f"seed_from={requested!r} says what a NEW session is copied from, "
            "so it needs a session of its own: pass session=<name> beside it. "
            "A spawn that names no session gets the "
            f"{DEFAULT_SESSION!r} session or a disposable copy of it, and "
            "neither is seeded from another session."
        )
    if shared:
        raise ToolError(
            f"seed_from={requested!r} cannot apply to the "
            f"{DEFAULT_SESSION!r} session: {DEFAULT_SESSION!r} is the session "
            "every other one is seeded FROM, and is nobody's copy. Pick a name "
            "for a new session of your own."
        )
    if target.exists():
        raise ToolError(
            f"session {target.name!r} already exists, and seed_from only "
            f"applies when a session is CREATED — it was "
            f"{seed_sentence(provenance(target))}. Pass session="
            f"{target.name!r} on its own to open it with the cookies and "
            f"logins it already holds, or pick a free name for a fresh copy "
            f"of {requested!r}."
        )


def seed_source(
    requested: str | None,
    roots: Roots,
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
    if requested is None or is_default_name(requested):
        if roots.seed.exists():
            return SeedSource(roots.seed, "explicit-default-seed")
        return SeedSource(roots.shared, "explicit-default")
    source = require_allowed(requested, roots, inside)
    if not source.exists():
        raise ToolError(
            f"seed_from={requested!r} is not a session: there is nothing at "
            f"{source}. `stealthy profiles` lists the sessions you have; omit "
            f"seed_from to copy the {DEFAULT_SESSION!r} session."
        )
    if held(source):
        raise ToolError(
            f"seed_from={requested!r} is open in a browser right now. Copying "
            "a profile Chrome is writing to silently drops whatever it has "
            "locked — which is where the logins are — and nothing can say "
            f"afterwards what was lost. Close the {requested!r} session first "
            f"(`stealthy close <instance>`), or seed from "
            f"{DEFAULT_SESSION!r}, which the product keeps a separate "
            "copyable form of."
        )
    return SeedSource(source, "explicit-session")
