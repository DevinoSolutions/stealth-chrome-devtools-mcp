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
``explicit-master-snapshot``, created 2026-09-11. Someone asked for the master
profile by its documented name and silently got a nine-day-old clone of a
snapshot. ``default`` is reserved with it because the session vocabulary is
about to give the master that name, and a friendlier word for the same trap is
still the trap. The snapshot PATH is refused for a second reason as well: a
browser driven on the snapshot directory writes into the seed every later
session copies from, which is how F-893's precondition arose.

A leaf: stdlib only. The master and snapshot directories, the profile and the
refresh window all arrive as ARGUMENTS, so this module never learns where a
session root is and ``clone_storage`` stays the one home for that.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

MARKER_NAME = ".stealth_chrome_devtools_mcp_clone.json"

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

# The seeds' own names. A caller may not hand one of these to ``user_data_dir``
# (F-894); ``default`` is here ahead of the vocabulary that will use it.
RESERVED_NAMES = frozenset({"master", "master-snapshot", "default"})

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
    """The seed's NAME — the word a user can say about where a profile came
    from. The two the product owns are named; anything else is its directory
    name, which is what a per-session seed will be."""
    if same_dir(source, snapshot):
        return "master-snapshot"
    if same_dir(source, master):
        return "master"
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


def anchor(
    requested: str,
    session_root: Path,
    clone_root: Path,
    inside: Callable[[Path, Path], bool],
) -> Path:
    """WHERE a ``user_data_dir`` request lands on disk.

    An absolute path is itself. A relative one is anchored against the session
    root, and kept there only if that already puts it inside the clone root
    (``"sessions/acme"``); otherwise the clone root is prepended, so a bare
    ``"acme"`` becomes ``sessions/acme`` without ``sessions/sessions/acme``.

    It sits here, one call above ``reserved_reason``, so that function can be
    asked about the directory a request MEANS rather than the string it was
    written as — the two answers differ for every relative path, and the
    reservation is about which directory is being opened. ``inside`` arrives as
    an argument because ``clone_storage._is_relative_to`` is its one home.
    """
    asked = Path(requested).expanduser()
    if asked.is_absolute():
        return asked
    anchored = session_root / asked
    return anchored if inside(anchored, clone_root) else clone_root / asked


def reserved_reason(requested: str, resolved: Path, snapshot: Path) -> str | None:
    """Why this ``user_data_dir`` may not be honoured, or None (F-894).

    Three refusals, and one deliberate non-refusal. A reserved NAME is refused
    because anchoring it under the clone root hands back a different profile
    under the name the caller used. The SNAPSHOT path is refused because a
    browser driven there writes into the seed every later session copies from.
    A drive-qualified path that is not absolute (``C:foo``, what a Windows
    absolute path becomes once a lenient string layer has eaten its backslashes
    — ``\\s`` is nobody's escape) is refused because ``Path.is_absolute()`` is
    False for it, so the resolver treats a drive-qualified path as a bare name
    and anchors it: MEASURED, that is exactly how
    ``sessions/stealth-mcp-browser-sessionssessionsstealth-chrome-devtools-mcp-…``
    came to exist.

    The MASTER path is deliberately NOT refused, and is not even a parameter
    here — driving the master directly is how a human logs in, and the caller
    who names it gets the master ROLE; the resolver settles that before asking
    this question at all.
    """
    asked = Path(requested)
    if asked.drive and not asked.is_absolute():
        return (
            f"{requested!r} names a drive but is not an absolute path, so it "
            "would be created as a session name rather than opened. Pass a "
            "fully qualified path (with separators) or a bare session name."
        )
    name = asked.name.casefold()
    if not asked.is_absolute() and name in RESERVED_NAMES:
        return (
            f"{name!r} is a reserved profile name and never means a session of "
            "that name. Spawn with no user_data_dir to use the shared profile "
            "the sessions are seeded from; pick another name for a session."
        )
    if same_dir(resolved, snapshot):
        return (
            "that path is the shared seed every new session is copied from, and "
            "a browser running on it writes into the seed. Spawn with no "
            "user_data_dir to use the shared profile itself."
        )
    return None


def same_dir(left: Path, right: Path) -> bool:
    """Case- and separator-insensitive path equality, without requiring either
    to exist (``Path.samefile`` needs both)."""
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except OSError:
        return False
