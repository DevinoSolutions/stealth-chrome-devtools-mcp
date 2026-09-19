"""THE one home for "which build is THIS process running" — the two facts the
reuse gate compares a recorded backend against.

Extracted from ``singleton`` (F-886 review), which keeps the two thin wrappers
that know WHICH package and WHICH tree to ask about. The pairing is the point:
``backend_registry.fingerprint_mismatch`` is the one READER of a recorded
digest, and this is the one PRODUCER of the current one, so "did the source
change" is answered by exactly two functions that were written to face each
other. A third spelling of either half is how a healthy shared backend comes to
be evicted on every proxy start.

Both answers are deliberately degradable and neither raises. An unresolvable
version is ``"0.0.0"``, which matches no real release, so the gate refuses
reuse and cold-starts — the safe direction. An unreadable digest is ``None``,
and that is NOT the same as a changed one: ``fingerprint_mismatch`` reads
``None`` as UNKNOWN and never as evidence (F-829 — this used to return ``""``,
which the gate could not tell from a real mismatch, so one OneDrive sync lock
evicted the backend every session on the machine was sharing).

A leaf: stdlib only, and the package name and source root arrive as ARGUMENTS
so ``singleton.SOURCE_ROOT`` stays the one patchable binding the suite
redirects. A function that closed over its own module global would ignore that
redirection and hash the developer's real tree.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# One stream: which build a proxy decided it was running is part of that
# proxy's story, so these lines go to the log ``configure_logging("proxy")``
# already owns — the same logger ``singleton`` uses, from which they moved.
_logger = logging.getLogger("stealth.proxy")

# F-829: outlast a transient read failure rather than reporting one as a source
# change. Three attempts at 50 ms covers the OneDrive sync lock that produced
# the finding without adding a perceptible cost to a cold start that succeeds
# on its first pass, which every ordinary one does.
ATTEMPTS = 3
RETRY_SECONDS = 0.05

# The unresolvable-version answer. Named rather than spelled inline because the
# thing that makes it safe is that it matches NO real release: the gate sees a
# version mismatch and cold-starts, instead of adopting a backend whose build
# we could not establish.
UNKNOWN_VERSION = "0.0.0"


def version(package: str) -> str:
    """The installed distribution version of *package*, or ``UNKNOWN_VERSION``.

    DEBUG, not WARNING: a source checkout run without an install resolves
    nothing here on every single call, and the consequence — a cold start — is
    already visible in the lines around it.
    """
    try:
        from importlib.metadata import version as _version

        return _version(package)
    except Exception:  # noqa: BLE001  PERMANENT(a build-identity read may never raise)
        _logger.debug("could not resolve installed package version", exc_info=True)
        return UNKNOWN_VERSION


def _segments(value: object) -> tuple[int, ...] | None:
    """``"2.1.9"`` -> ``(2, 1, 9)``; anything else -> ``None``.

    Deliberately strict and deliberately NOT ``packaging.version``: that is not
    a declared dependency of this package, and our own versions are plain
    ``X.Y.Z``. Anything that does not parse as such — a pre-release suffix, a
    local version, a hand-edited record's integer, ``None`` — is UNCOMPARABLE,
    which :func:`newer` resolves to "not newer", i.e. onto today's behaviour.
    """
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def newer(recorded: object, ours: str) -> bool:
    """True iff ``recorded`` is a version of ours STRICTLY NEWER than ``ours``.

    F-889 (d). A mixed-version fleet — an in-place upgrade, a ``uvx @latest``
    session beside a ``uv tool`` install — had one eviction loop left after
    F-886: two identities on one desktop each read the other as stale and each
    evicted the other on every proxy start. F-886 stops that only when the loser
    owns live browsers, and a fleet MID-UPGRADE is exactly the population where
    neither does yet. Adopting the newer one breaks the symmetry in the
    direction a fleet should converge in: the older proxy is a byte bridge over
    MCP-on-HTTP and has no tool knowledge of its own to be wrong about.

    Two guards, both failing CLOSED onto today's cold start. Anything
    unparseable is not newer (see :func:`_segments`). And
    :data:`UNKNOWN_VERSION` is never newer and never older ON EITHER SIDE — a
    process that could not resolve its own build has no claim to make about
    someone else's, and a RECORD that could not resolve its own is exactly the
    one the gate exists to refuse.

    Unequal segment counts are zero-extended, so ``2.2`` and ``2.2.0`` are the
    same build and neither is newer than the other.
    """
    if UNKNOWN_VERSION in (recorded, ours):
        return False
    theirs, mine = _segments(recorded), _segments(ours)
    if theirs is None or mine is None:
        return False
    width = max(len(theirs), len(mine))
    pad = (0,) * width
    return (theirs + pad)[:width] > (mine + pad)[:width]


def source_fingerprint(root: Path) -> str | None:
    """SHA-256 over every ``*.py`` under *root*, or ``None`` if it cannot be
    read after ``ATTEMPTS`` passes.

    COMPLETE (every module the backend can import), STABLE (identical bytes ->
    identical digest, immune to mtime and git checkout quirks), CHEAP (~1 MB
    read and hash per cold-start discovery). This is what makes an in-place
    source edit visible at all: on an editable install the package version is
    frozen, so the version key alone can never see one (F-206/F-120/F-504).

    The relative path is hashed alongside each file's bytes, so moving a module
    changes the digest even when no byte of any file does. ``__pycache__`` is
    skipped: it is generated, it differs between interpreters that share source,
    and hashing it would make two identical trees disagree.
    """
    err: OSError | None = None
    for _ in range(ATTEMPTS):
        digest = hashlib.sha256()
        try:
            for path in sorted(root.rglob("*.py")):
                if "__pycache__" not in path.parts:
                    digest.update(path.relative_to(root).as_posix().encode())
                    digest.update(b"\0" + path.read_bytes() + b"\0")
            return digest.hexdigest()
        except OSError as exc:
            err = exc
            time.sleep(RETRY_SECONDS)
    _logger.warning("source fingerprint unreadable: %s", err)
    return None
