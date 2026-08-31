"""THE one home for the ``server.json`` backend record: its schema, its
read/write/clear I/O, and the canonical definition of the state dir it lives in.

Moved out of ``singleton.py`` (plan_F808 Task 2), which keeps lifecycle only —
probe, lock, spawn, evict, proxy — and re-exports the path names because
``settings.py``, ``logging_setup``, ``process_cleanup``, ``response_handler`` and
several tests reach for ``singleton.STATE_DIR`` / ``singleton._read_server_state``.

The I/O takes the record's path as an argument rather than reading the module
global. That is deliberate, not ceremony: the caller's binding is what selects
the file, so redirecting ``singleton.SERVER_STATE_FILE`` at runtime (which the
hermetic tests do, and which is the only thing keeping a test run from editing
the developer's live ``~/.stealth-mcp`` record) still reaches every read and
write. A function that closed over this module's own global would silently
ignore that redirection.

Corollary: no function here may take a default path. This module's own
``SERVER_STATE_FILE`` names the real file; a default would bind it at def-time
and bypass every caller's redirection. ``test_backend_registry.py`` pins this.

Schema (plan_F808 Task 3). v2 keys the record by **display context**, so one
machine can hold a headless backend and a desktop backend at once and discovery
can pick the one that can actually show a window::

    {"schema": 2, "backends": {"<display_context>": {port, version, pid,
                                                     source_fingerprint,
                                                     display_context}}}

A v1 record — the flat ``{port, version, pid, source_fingerprint}`` every
release up to 2.0.3 wrote — still reads, as ONE backend classified
``UNVERIFIED`` (treated as window-capable). Dropping it instead would evict a
perfectly healthy backend the moment a user upgrades.

**Supersede by port.** Recording a backend drops every other entry claiming the
same port, whatever context it is filed under, before filing this one. The port
is the discriminator because only one process can hold a loopback listener: a
second entry naming that port cannot describe a live backend, so it is by
construction a leftover. Two ways to produce one, both real — a v1 record
(which reads as ``UNVERIFIED``, never the real token the upgraded client
writes), and a context token that changed under a backend that did not (a
Windows session id is reassigned across an RDP reconnect). Without the rule the
leftover survives, sorts first, and is handed to every reader forever; because
its version or fingerprint can no longer match, the reuse gate kills and
respawns the shared backend on every single proxy start. Entries on OTHER ports
are untouched — including ``UNVERIFIED`` ones, which may be live backends on a
platform we genuinely cannot classify.

A leaf module: stdlib plus ``display_context`` (itself a leaf). Never
``singleton``, never ``server``.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import suppress
from pathlib import Path

from stealth_chrome_devtools_mcp.embedded.display_context import HEADLESS, UNVERIFIED

# One recorded backend, and equally the raw record that holds them: both are
# str-keyed JSON objects whose values are deliberately heterogeneous. `object`
# rather than a precise union or a model on purpose - this record is a
# never-raise cache that must keep reading two schemas and tolerate a
# hand-edited file, so every consumer already re-checks the field it wants
# (`isinstance(port, int)`), which a validating model would replace with an
# exception at exactly the moment discovery must not fail.
BackendEntry = dict[str, object]

# THE definition of the state dir. settings.py recomputes this one path (as
# _STATE_DIR_ENV_FILE) because it is a leaf module that may not import the
# package; keep the two in step. Nothing else may fork it.
STATE_DIR = Path.home() / ".stealth-mcp"
PORT_FILE = STATE_DIR / "server.port"
# Records {port, version, pid} per display context for the backends we started,
# so discovery can confirm a running backend is the SAME version before reusing
# it. Without this an upgraded session silently reuses a stale old-version
# backend (issue #14).
SERVER_STATE_FILE = STATE_DIR / "server.json"

SCHEMA_VERSION = 2

# Windows refuses an atomic replace while another process holds the target open
# (see _commit); a few short retries outlast that window.
_COMMIT_ATTEMPTS = 5
_COMMIT_RETRY_SECONDS = 0.02


def read_record(path: Path) -> BackendEntry | None:
    """Return the raw record as written, or None if absent/corrupt.

    The RAW shape — v1 flat or v2 — because ``singleton._read_server_state`` is
    a patch surface several test modules stub with v1-flat dicts. Callers that
    want backends read them through :func:`backends_in` / :func:`first_backend`
    / :func:`backend_on_port`, which normalize both shapes; nothing outside this
    module should branch on ``"schema"`` itself.
    """
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return None
    return state if isinstance(state, dict) else None


def backends_in(state: BackendEntry | None) -> list[BackendEntry]:
    """THE schema reader: a raw record (either version) → its backend entries.

    Every other accessor here is derived from this one, so "how a record is
    laid out" is answered in exactly one place. Each returned entry carries a
    ``display_context``: for v2 it comes from the key (the key is authoritative,
    so a hand-edited file cannot disagree with itself); for a v1 flat record it
    defaults to ``UNVERIFIED``, which :mod:`display_context` treats as
    window-capable. Anything unreadable — missing file, corrupt JSON, a
    non-object top level, a v2 file whose ``backends`` is not a mapping — is no
    backends, never an exception: this record is a cache, and discovery must not
    be able to fail on it.
    """
    if not isinstance(state, dict):
        return []
    if state.get("schema") == SCHEMA_VERSION:
        backends = state.get("backends")
        if not isinstance(backends, dict):
            return []
        return [
            {**entry, "display_context": str(ctx)}
            for ctx, entry in backends.items()
            if isinstance(entry, dict)
        ]
    if isinstance(state.get("port"), int):
        return [{**state, "display_context": state.get("display_context", UNVERIFIED)}]
    return []


def first_backend(state: BackendEntry | None) -> BackendEntry | None:
    """The first recorded backend in a raw record, or None.

    "First" preserves the pre-v2 single-backend behaviour exactly (a v1 record
    reads as one entry). It carries no preference of its own — a caller that
    wants the backend most likely to be usable asks :func:`window_capable_first`.
    """
    return next(iter(backends_in(state)), None)


def backend_on_port(
    state: BackendEntry | None, port: int | None
) -> BackendEntry | None:
    """The recorded backend listening on ``port``, or None.

    For the callers that already hold a port and want THAT backend's fields —
    the pid to terminate, the fingerprint that explains an eviction — rather
    than whichever entry happens to come first.
    """
    return next((e for e in backends_in(state) if e.get("port") == port), None)


def read_backends(path: Path) -> list[BackendEntry]:
    """Every backend recorded in the file at ``path``, in recorded order."""
    return backends_in(read_record(path))


def window_capable_first(path: Path) -> list[BackendEntry]:
    """Recorded backends, those that can show a window before those that cannot.

    A pure ORDERING over every recorded backend, kept whole for callers that
    must name them all — doctor's per-context listing, most-useful first. It is
    NOT discovery's order: :func:`adoption_candidates` is, and it filters this
    list rather than merely reading it, so nothing here decides what a client
    may reuse. Only a PROVEN-invisible context sorts last; ``UNVERIFIED`` stays
    with the capable group, matching ``display_context.can_show_windows``. The
    sort is stable, so recorded order breaks ties.
    """
    return sorted(
        read_backends(path), key=lambda e: e.get("display_context") == HEADLESS
    )


def adoption_candidates(path: Path, own_context: str) -> list[BackendEntry]:
    """Recorded backends in the order discovery should try them (F-808).

    Adoption is deliberately ASYMMETRIC, and the two halves answer different
    questions.

    A client that CANNOT prove it has a desktop — HEADLESS, or UNVERIFIED —
    adopts anything, window-capable entries first. This is THE F-808 fix: an
    SSH or service-session client converging on the desktop backend is exactly
    what makes its headed spawns visible, so for it display context is a
    PREFERENCE, never an equality test.

    A client that CAN prove it has a desktop adopts only its OWN context's
    entry, plus UNVERIFIED ones. Every other proven context is excluded, not
    merely demoted — and that means foreign desktops as well as HEADLESS.
    Identity cannot separate them (a sibling desktop runs the same install, so
    version and fingerprint both match), yet a browser spawned on that backend
    renders on a window station THIS user cannot see: the F-808 headline
    symptom, and one Task 5 cannot guard, because that backend's own
    ``can_show_windows()`` is perfectly True. The plan's invariant is one
    backend per (source fingerprint, display context); adopting a foreign
    desktop's backend simply violates it. Refusing costs one cold start.

    UNVERIFIED is adoptable on BOTH sides. It is what every record written up
    to 2.0.3 reads as, and what an unclassifiable platform reports for itself.
    Refusing it would evict a healthy backend on upgrade; applying the
    capable-client exclusion *because of* it would do the same wherever the
    probe cannot answer. Only a PROVEN verdict moves anything.
    """
    candidates = window_capable_first(path)
    if own_context in (HEADLESS, UNVERIFIED):
        return candidates
    return [
        e for e in candidates if e.get("display_context") in (own_context, UNVERIFIED)
    ]


def port_for_context(path: Path, display_context: str) -> int | None:
    """The port recorded for ONE context, or None when that context has no
    entry (or its entry names nothing usable as a port).

    ``singleton._select_backend_port`` asks with its own context: a spawn
    should land back on the port ITS desktop last used. "Whichever entry comes
    first" is the wrong answer once the record holds one per context — first
    can easily be a sibling backend that is still alive on that port.
    """
    entry = next(
        (e for e in read_backends(path) if e.get("display_context") == display_context),
        None,
    )
    return recorded_int(entry, "port")


def recorded_int(entry: BackendEntry | None, key: str) -> int | None:
    """One integer field off a recorded entry, or None when the entry is
    missing, the key is absent, or the value is not an ``int``.

    THE one home for that narrowing. The record tolerates hand-editing and two
    schema versions, so every field read must re-check its own type; without a
    shared helper each caller spells it slightly differently and the annotation
    drifts away from the truth (``cli._recorded_backend_pid`` claimed
    ``int | None`` while returning whatever the file said). ``port`` and
    ``pid`` are the only two such fields today.
    """
    value = entry.get(key) if entry else None
    return value if isinstance(value, int) else None


def fingerprint_mismatch(entry: BackendEntry | None, current: str | None) -> bool:
    """True only when ``entry``'s recorded source fingerprint and ``current``
    are BOTH known and DIFFER — THE one reading of that field.

    Three states, not two (F-829). A digest compares normally. ``None`` on
    either side means UNREADABLE: the source could not be hashed just now (this
    tree syncs through OneDrive, where a file being synced raises a transient
    OSError), or the backend was recorded while it could not be. Unknown is not
    evidence of a source change, so it never refuses reuse — reading it as one
    is what let a single failed read evict a healthy shared backend. ``""`` —
    an absent field (a pre-M2 record) or a caller that means "no digest" —
    keeps its fail-closed meaning and still counts as a mismatch.
    """
    recorded = (entry or {}).get("source_fingerprint", "")
    if recorded is None or current is None:
        return False
    return not current or recorded != current


def own_or_first_port(path: Path, own_context: str) -> int | None:
    """The port to act on when a caller must pick exactly ONE recorded backend:
    our own context's, else whatever single backend is recorded, else None.

    ``singleton.restart_backend`` is the one caller, and it uses the answer only
    to SEED ``_select_backend_port``. So the own-context half is now
    production-inert: selection re-reads :func:`port_for_context` itself and
    discards the seed whenever our own context has an entry. The half that still
    decides anything is the ``first_backend`` fallback — the seed selection
    actually uses, when our context has no entry — which keeps the
    single-backend and pre-v2 cases landing exactly where they did.

    The own-context branch stays because it is the honest statement of this
    function's contract, not because restart depends on it; it was load-bearing
    before plan_F808 Task 4d reordered restart to terminate the port SELECTION
    returned. Until then restart terminated the port read here directly, and a
    terminate half reading ``first_backend`` would, on a two-context machine,
    have killed a sibling desktop's backend and respawned ours somewhere else.

    "No answer" is ``None`` and only ``None``. A caller that tests the result
    for truthiness reaches its fallback for a recorded ``0`` as well; that is
    harmless (0 names no listener) but it is not what the signature says.
    """
    own = port_for_context(path, own_context)
    if own is not None:
        return own
    return recorded_int(first_backend(read_record(path)), "port")


def port_conflict(path: Path, port: int, own_context: str) -> bool:
    """True iff *port* is claimed by an entry whose display context is PROVEN
    and different from ``own_context``.

    The sibling this protects is a backend we REFUSED to adopt. For a
    PROVEN-CAPABLE client — and only for one — that description is now exact:
    :func:`adoption_candidates` excludes every foreign proven context, so what
    such a client will not adopt is precisely what it will not bind on top of.
    A client that CANNOT prove it has a desktop (HEADLESS or UNVERIFIED) adopts
    anything and yet still yields here; that is the one deliberate mismatch,
    described below. Binding on such a port would be self-defeating:
    :func:`record_backend` supersedes by port, and a spawn records itself at
    Popen time — BEFORE the new backend is ready — so that entry would be
    dropped the instant we start, leaving a live backend on another desktop
    undiscoverable and its sessions orphaned. Picking a different port instead
    costs nothing.

    An UNVERIFIED entry is therefore NEVER a conflict, and the soundness
    argument is what makes that safe rather than merely convenient:
    :func:`adoption_candidates` offers UNVERIFIED entries to EVERY client, so a
    healthy same-identity backend recorded that way was already adopted and we
    never reached port selection at all. Reaching here proves that entry is
    version-stale or dead, and evicting it — then superseding its record by
    port — is exactly the intended upgrade flow. A proven-HEADLESS entry is the
    opposite case: a window-capable client refuses to adopt it *while it may
    still be alive* serving SSH sessions, so "we got here" proves nothing about
    it, and that is precisely the sibling worth stepping around.

    Getting this wrong is not theoretical. Treating UNVERIFIED as a conflict
    diverts the spawn to a random free port, which sends ``_clear_stale_backend``
    at the WRONG port — so the live <= 2.0.3 backend on the target and its
    Chrome processes are never evicted and leak for good, with the record left
    naming both. "Only a PROVEN verdict moves anything" holds here exactly as it
    does for adoption.

    The mismatch promised above, stated as a rule rather than left to fall out
    of the code: a CLIENT that cannot prove it has a desktop — HEADLESS or
    UNVERIFIED alike — still conflicts with a proven entry (it adopts anything,
    but it does not get to bind on top of one). This is deliberate: such a
    client has not earned the right to evict a sibling that may be alive and
    serving, and the rule is about what the CLIENT can prove, not which of the
    two unproven tokens it happens to carry. It has two
    known costs, both accepted. An old backend on that port can linger, because
    nobody claims the port that would supersede its record. And on this one
    path adoption and selection disagree: discovery may adopt the entry on that
    port while selection steps around it. That is harmless in practice —
    discovery runs first and returns, so selection is never reached in the case
    where they differ — but it is a real asymmetry, not an oversight. The
    argument for revisiting: the same soundness reasoning used just above
    applies to an UNVERIFIED client too (it refuses nothing, so arriving at
    selection proves the entry stale), which would make this branch False and
    the two rules identical. It is left conservative because "may still be
    alive" is the more expensive thing to get wrong.
    """
    return any(
        e.get("port") == port
        and e.get("display_context") not in (own_context, UNVERIFIED)
        for e in read_backends(path)
    )


def record_backend(  # noqa: PLR0913  PERMANENT(function interface)
    path: Path,
    *,
    port: int,
    version: str,
    pid: int,
    source_fingerprint: str | None,
    display_context: str,
) -> None:
    """Record one backend's identity under its display context, replacing any
    previous entry for that SAME context and leaving the others untouched.

    Identity is the port, the package version that started it, its pid, and a
    fingerprint of the source it is running: discovery reuses a backend only
    when BOTH the version AND the source fingerprint still match (and it answers
    a live probe); the pid is used to evict a stale one. ``None`` records the
    F-829 sentinel — the source was unreadable at spawn — deliberately, so that
    :func:`fingerprint_mismatch` later reads it as unknown rather than as an
    empty digest that contradicts every future one.

    Also supersedes by port: any OTHER entry claiming this port is a leftover
    (only one process can hold a loopback listener) and is dropped rather than
    left to shadow this one forever. The module docstring carries the full
    argument; entries on other ports, UNVERIFIED included, survive.
    """
    entries: dict[str, BackendEntry] = {}
    for recorded in read_backends(path):
        if recorded.get("port") == port:
            continue
        entries[str(recorded["display_context"])] = recorded
    entries[display_context] = {
        "port": port,
        "version": version,
        "pid": pid,
        "source_fingerprint": source_fingerprint,
        "display_context": display_context,
    }
    _write(path, entries)


def forget_backend(path: Path, display_context: str) -> None:
    """Drop one context's entry, keeping every other context's.

    Forgetting the last one leaves a readable EMPTY record rather than removing
    the file; deleting it is :func:`clear_record`'s job. ``singleton.stop_backend``
    composes the two — forget the stopped backend's own context, then clear only
    once nothing is left recorded — so stopping one backend cannot make another
    display context's live backend undiscoverable.
    """
    entries = {
        str(e["display_context"]): e
        for e in read_backends(path)
        if e["display_context"] != display_context
    }
    _write(path, entries)


def _commit(tmp: Path, path: Path) -> None:
    """Move ``tmp`` onto ``path``, retrying briefly on a Windows sharing refusal.

    ``Path.replace`` is atomic on both platforms, but on Windows it raises
    PermissionError (WinError 5/32) while ANOTHER process has the target open —
    Python's ``open()`` does not pass FILE_SHARE_DELETE, so a concurrent reader
    (a second stdio proxy in ``read_record``) is enough to make the rename lose
    a race it would win on POSIX. The window is microseconds, so a few short
    retries convert a spurious hard failure into a slightly later success.

    Do NOT delete this loop as redundant: on POSIX it costs one iteration and
    never sleeps, and the failure it prevents only ever appears under the
    concurrency a herd of proxy starts produces — never in a single-process test.
    """
    for attempt in range(_COMMIT_ATTEMPTS):
        try:
            tmp.replace(path)
        except PermissionError:
            if attempt == _COMMIT_ATTEMPTS - 1:
                raise
            time.sleep(_COMMIT_RETRY_SECONDS)
        else:
            return


def _write(path: Path, entries: dict[str, BackendEntry]) -> None:
    """Write the v2 record atomically: stage into a sibling temp file, then
    ``Path.replace`` (i.e. ``os.replace``) — so a reader concurrent with a write
    sees the whole old record or the whole new one, never a truncated file, and
    a crash mid-write cannot leave the record unparseable.

    No locking here on purpose. Every production writer already runs under
    singleton's cold-start file lock, so what this module owes is atomicity of
    an individual write, not a merge protocol between racing writers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # pid-suffixed so two processes staging at once cannot collide on the temp
    # name; the same directory so the replace stays within one filesystem.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps({"schema": SCHEMA_VERSION, "backends": entries}))
        _commit(tmp, path)
    except BaseException:
        # ACCEPTED GAP: this cleans up a failed write, but a process killed
        # between write_text and the replace leaves a .tmp that nothing sweeps.
        # Bounded and harmless - one small file per killed writer, in a
        # directory the reader only ever looks up server.json by name in.
        with suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def clear_record(*paths: Path) -> None:
    """Best-effort unlink of the given files; a path that is absent or that the
    OS refuses is skipped, never raised.

    `stop_backend()` calls this with the server.json record and the legacy
    write-only port file, so a stale record can never make a later
    `_find_running_server` believe a stopped backend is still there to reuse —
    but that pair is the example, not the contract.
    """
    for path in paths:
        with suppress(OSError):
            path.unlink(missing_ok=True)
