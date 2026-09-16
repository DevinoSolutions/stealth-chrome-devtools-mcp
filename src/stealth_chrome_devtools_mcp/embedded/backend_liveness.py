"""THE one home for "is the backend on this port alive, and which recorded
backend would THIS client be served by".

Extracted from ``singleton`` by F-868, which left that file at 999 of its 1000
LOC — the same gate that forced ``backend_watchdog`` out, and the same answer:
this is a self-contained pair that takes every collaborator through an
argument, so it belongs beside the module that wires it, not inside it.

A leaf. The two liveness primitives arrive as PARAMETERS (``is_healthy`` /
``http_ready``), exactly as ``backend_watchdog`` takes its probes, so nothing
here imports ``singleton``; the record arrives as a path, so nothing here
decides WHICH record either. ``backend_registry`` — itself a leaf — is the only
import, because the adoption ORDER is its policy and is merely consumed here.

``singleton`` keeps thin wrappers (``_probe_port`` / ``_probe_backend_status``)
that bind OUR probes, OUR record path and OUR display context to these two.
That is deliberate and not ceremony: the suite patches
``singleton._server_is_healthy``, ``singleton._backend_http_ready`` and
``singleton._probe_port`` by name, and a wrapper resolving those module globals
at CALL time is what keeps every existing ``monkeypatch.setattr(singleton, …)``
reaching this code. A caller that imported these names directly would bind them
at import time and silently stop seeing such a patch.

The vocabulary is one closed set and it is ENTIRELY here: ``down`` | ``wedged``
| ``responsive``, plus ``none`` for "no adoptable entry names a port at all"
and :data:`NO_PORT` for an entry naming nothing usable as a port. That last word
used to live in ``cli._probe_recorded_backend``, an adapter whose whole content
was "the ladder, plus one word"; F-880 moved the word here and deleted the
adapter, because :func:`survey` answers per entry and a second function that
only re-says the vocabulary is the second way this codebase's fourth convention
forbids.

F-880 adds the other half of the same subject — "and is this recorded backend
DEAD, i.e. is its record residue": :func:`survey`, :func:`dead_entries` and
:func:`forget_dead`. Deciding death is a liveness sentence and belongs here; the
WRITE that follows is ``backend_registry.forget_entries``', because the record's
schema and its read-merge-write protocol are that module's and always were.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from stealth_chrome_devtools_mcp.embedded import backend_registry

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

#: The one word the down/wedged/responsive ladder cannot reach: an entry whose
#: ``port`` is not an int (the record tolerates hand-editing and two schema
#: versions). There is nothing to probe, so there is no liveness claim to make.
NO_PORT = "no port recorded"


def probe_port(
    port: int,
    *,
    is_healthy: Callable[[int], bool],
    http_ready: Callable[[int], bool],
) -> str:
    """THE liveness ladder for ONE port — socket, then a real MCP `initialize`:
    "down" | "wedged" | "responsive" (F-301's third state, which a bare socket
    check cannot see). Read-only. THE one home for those four lines (F-868),
    with three readers: the candidate walk below, :func:`_survey_one` (so
    doctor's per-entry line and the deadness rule read the same ladder), and
    `restart_backend`'s report of the port it spawned on. Doctor used to reach
    it through `cli._probe_recorded_backend`, an adapter that added only the one
    word this cannot reach — :data:`NO_PORT` now, here, and the adapter deleted
    (F-880). It was a verbatim copy of this ladder before F-868.
    """
    if not is_healthy(port):
        return "down"
    return "responsive" if http_ready(port) else "wedged"


def probe_recorded(
    path: Path, own_context: str, *, probe: Callable[[int], str]
) -> tuple[str, int | None]:
    """Report the state of the backend THIS process would be served by, for
    display (CLI status/doctor) and for `stop`: `probe`'s verdict and the
    port it was reached on, or ("none", None) when no adoptable entry names a
    port. What this adds over that ladder is WHICH port to ask about.

    Candidates come in ADOPTION order (F-868) — `adoption_candidates`, the one
    home `_find_running_server` already walks — never "whichever entry the
    record lists first", which under one-entry-per-display-context is routinely
    a dead sibling's: that is how `status` came to report "not running" beside
    a backend serving 56 proxies, and `stop` to aim at the dead record. The
    first candidate that ANSWERS wins, else the most informative verdict —
    wedged over down: a wedged backend holds a port and will be evicted, a down
    record names nothing running. The ORDER itself is not decided here.

    ``probe`` is a parameter rather than :func:`probe_port` called directly so
    that ``singleton._probe_port`` — the binding the suite patches — stays the
    thing this walk actually asks.
    """
    best: tuple[str, int | None] = ("none", None)
    for entry in backend_registry.adoption_candidates(path, own_context):
        port = backend_registry.recorded_int(entry, "port")
        if port is None:
            continue
        verdict = probe(port)
        if verdict == "responsive":
            return verdict, port
        if best[0] == "none" or (best[0] == "down" and verdict == "wedged"):
            best = (verdict, port)
    return best


class Surveyed(NamedTuple):
    """One recorded entry, its liveness verdict, and whether it is RESIDUE."""

    entry: backend_registry.BackendEntry
    verdict: str
    dead: bool


def survey(
    entries: list[backend_registry.BackendEntry],
    *,
    probe: Callable[[int], str],
    pid_is_ours: Callable[[object], bool],
) -> list[Surveyed]:
    """THE one probe pass over a list of recorded entries, and THE one rule for
    "this entry describes nothing that exists" (F-880). One probe per entry.

    Takes ENTRIES rather than a path so a single pass can serve two callers that
    order the record two different ways without either of them re-deciding
    anything: doctor hands it ``window_capable_first``'s order, the prune hands
    it ``read_backends``. Neither consults ``adoption_candidates``, and that is
    the point — adoption decides what a client may REUSE, and a backend that
    does not exist is not a thing any display context has a claim on.

    **Dead needs BOTH witnesses.** ``down`` (no listener on the port) AND
    ``pid_is_ours`` False (the recorded pid is not a running backend of ours).
    Neither alone will do, and the tree already knows why:

    - A backend is recorded at Popen time, BEFORE it binds — ``port_conflict``'s
      docstring says so — so a sibling is ``down`` for the whole of its cold
      start while its process is alive and about to serve. The socket alone
      would race every cold start on the machine. This is the same
      discrimination ``_same_identity_backend_ready`` makes in the same words:
      "no socket and no live process: dead, not busy".
    - The pid alone answers the wrong question (F-868 §6: "a backend whose pid
      is gone is not necessarily a backend whose PORT is free"). Requiring
      ``down`` as well means the port has just been OBSERVED to hold no
      listener at all, so there is no squatter the record would help step
      around.

    ``wedged`` is therefore never dead: it holds its port, it will be evicted
    and respawned, and its record is how ``_terminate_backend`` finds the pid to
    kill. An entry with no usable port is reported as :data:`NO_PORT` and is
    never dead either — we have no evidence about it, and inventing one is how
    a hand-edited entry would silently disappear.
    """
    return [_survey_one(entry, probe, pid_is_ours) for entry in entries]


def _survey_one(
    entry: backend_registry.BackendEntry,
    probe: Callable[[int], str],
    pid_is_ours: Callable[[object], bool],
) -> Surveyed:
    port = backend_registry.recorded_int(entry, "port")
    if port is None:
        return Surveyed(entry, NO_PORT, False)
    verdict = probe(port)
    dead = verdict == "down" and not pid_is_ours(entry.get("pid"))
    return Surveyed(entry, verdict, dead)


def dead_entries(surveyed: list[Surveyed]) -> list[backend_registry.BackendEntry]:
    """THE one reduction of a survey to the entries that are residue."""
    return [item.entry for item in surveyed if item.dead]


def forget_dead(
    path: Path,
    *,
    probe: Callable[[int], str],
    pid_is_ours: Callable[[object], bool],
) -> list[str]:
    """Survey the whole record and forget every entry that is dead; return the
    display contexts forgotten, in recorded order (F-880).

    The one composition, so no caller writes "survey, filter, forget" a second
    time, and the one WRITER of this decision: ``cli``'s ``cleanup --apply``.
    ``doctor`` deliberately does not call it — it reports from :func:`survey`
    and never writes, because a read-only verb that edits state is a trap
    regardless of how good the edit is.

    It re-surveys rather than taking a caller's list, so the probe that decides
    and the write that acts are one operation over one record read; the merge
    that protects a concurrent ``record_backend`` is
    ``backend_registry.forget_entries``'.
    """
    surveyed = survey(
        backend_registry.read_backends(path), probe=probe, pid_is_ours=pid_is_ours
    )
    return backend_registry.forget_entries(path, dead_entries(surveyed))
