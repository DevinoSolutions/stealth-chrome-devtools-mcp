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

import logging
import time
from typing import TYPE_CHECKING, NamedTuple

from stealth_chrome_devtools_mcp.embedded import backend_registry

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_logger = logging.getLogger("stealth.proxy")

#: The one word the down/wedged/responsive ladder cannot reach: an entry whose
#: ``port`` is not an int (the record tolerates hand-editing and two schema
#: versions). There is nothing to probe, so there is no liveness claim to make.
NO_PORT = "no port recorded"

#: How often the backend stamps its own entry, and how old a stamp may be and
#: still be evidence. The two are stated together because their RELATION is the
#: decision: 30 / 3 is TEN consecutive missed loop iterations. Sized against the
#: incident rather than against taste — a backend answering ``initialize`` in
#: 227 ms is stamping, and a backend starved badly enough to miss ten
#: consecutive iterations while its socket still answers is a state nobody has
#: observed. A reader cannot change one without seeing the other.
HEARTBEAT_INTERVAL_SECONDS = 3.0
HEARTBEAT_STALE_SECONDS = 30.0


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


def self_report(path: Path, port: int) -> float | None:
    """How long ago the backend on ``port`` last said its own event loop was
    turning, or ``None`` when it has not said so recently enough to be evidence
    (F-889 (b)).

    **The second witness, and the only one a starved prober cannot fake.** Every
    other liveness answer in this tree is a claim by the PROBER: a timeout says
    "it did not answer in N seconds", which is a fact about the backend only
    while we were awake to hear an answer. On 2026-09-18 the machine had 2.4 GB
    free of 125.7 and 114 stdio proxies paged out to ~0 MB; their 2 s probes
    timed out, the watchdog condemned, and the backend they condemned answered
    an ``initialize`` in 227 ms throughout. This function is how that proxy can
    tell "the backend is dead" from "I was not scheduled": no HTTP, no socket,
    no thread — one small local JSON read.

    It answers the AGE rather than a bool so the reporting line can say how
    fresh the evidence was; callers test ``is not None``, never truthiness,
    because a stamp written this instant is ``0.0``.

    ``None`` — i.e. no evidence — for every one of: no entry on that port, no
    stamp (a 2.1.9 backend, which is what makes a mixed fleet degrade to
    today's behaviour instead of misreading silence as death), a hand-edited
    non-numeric stamp, a ``heartbeat_pid`` that disagrees with the entry's own
    ``pid`` (the stamp is a leftover from a predecessor on that port, not a
    claim by the process recorded there), and a stamp further than
    :data:`HEARTBEAT_STALE_SECONDS` from now IN EITHER DIRECTION — a clock that
    jumped forward is not a backend that is alive.

    ``time.time()`` and not ``time.monotonic()`` deliberately: the writer and
    the reader are DIFFERENT PROCESSES, and monotonic clocks are not comparable
    across them. The cost is that a wall-clock step changes an age, which the
    symmetric bound above turns into "no evidence" — falling back to the
    confirmation phase, i.e. to exactly today's behaviour.
    """
    entry = backend_registry.backend_on_port(backend_registry.read_record(path), port)
    if entry is None:
        return None
    at = entry.get(backend_registry.HEARTBEAT_AT)
    if isinstance(at, bool) or not isinstance(at, int | float):
        return None
    if entry.get(backend_registry.HEARTBEAT_PID) != entry.get("pid"):
        return None
    age = time.time() - float(at)
    return age if abs(age) <= HEARTBEAT_STALE_SECONDS else None


def stamp(path: Path, port: int, pid: int) -> bool:
    """Write one heartbeat; never raise. True iff the record was updated.

    The write is ``backend_registry.stamp_heartbeat``'s — the record's schema
    and its read-merge-write protocol are that module's and always were, and a
    stamp that could resurrect a forgotten entry is the failure it guards.
    """
    try:
        return backend_registry.stamp_heartbeat(
            path, port=port, pid=pid, at=time.time()
        )
    except Exception:  # noqa: BLE001  PERMANENT(a liveness stamp must not kill the backend)
        _logger.debug("could not stamp the backend heartbeat", exc_info=True)
        return False


async def beat(
    path: Path, port: int, pid: int, *, interval: float | None = None
) -> None:
    """Stamp ``path`` every ``interval`` seconds, forever, from the caller's
    event loop.

    **It must run on the EVENT LOOP, and that is the entire design.** The
    failure the watchdog exists for (F-501) is a backend whose dispatch loop is
    dead while its socket stays open. A heartbeat on an OS thread, or in a
    signal handler, would keep stamping straight through exactly that failure
    and would defend the one backend that deserves condemning. Driven from the
    loop it cannot: the same wedge that stops answering ``initialize`` stops
    this coroutine being resumed, the stamp ages past
    :data:`HEARTBEAT_STALE_SECONDS`, and the strikes plus the confirmation
    condemn it as they always did. **The heartbeat can only ever say "I am
    scheduled and my loop is turning"** — which is precisely the fact a starved
    prober cannot establish and has no other way to obtain.

    The WRITE itself goes to a worker thread: it is an ``os.replace`` of a
    sub-kilobyte file, but ``_commit``'s Windows sharing retry can sleep up to
    0.1 s, and the event loop this exists to demonstrate is turning must not be
    the thing blocked to demonstrate it. The loop is still the thing being
    measured — it has to schedule the hand-off and be resumed afterwards.
    """
    import anyio

    every = HEARTBEAT_INTERVAL_SECONDS if interval is None else interval
    while True:
        await anyio.to_thread.run_sync(stamp, path, port, pid)
        await anyio.sleep(every)


_BEATING = False


def start_beating(path: Path, port: int, pid: int) -> bool:
    """Start :func:`beat` on the running loop, once per process; True iff this
    call is the one that started it.

    Idempotent on ``session_hygiene.install()``'s precedent and for the same
    reason — ``server.py`` is executed three times under runpy — and a no-op
    with no running loop, so a caller outside an event loop gets no heartbeat
    rather than an exception. Never raises: a backend that cannot stamp is a
    backend without a second witness, which degrades to 2.1.9's behaviour, and
    that is strictly better than a backend that will not serve.
    """
    global _BEATING  # noqa: PLW0603  PERMANENT(once-per-process guard; the suite patches this name)
    if _BEATING:
        return False
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _BEATING = True
    # Held so the task is not garbage-collected mid-flight; it outlives the
    # process's serving life by construction, so nothing ever cancels it.
    loop.create_task(beat(path, port, pid))  # noqa: RUF006  PERMANENT(process-lifetime task)
    _logger.info(
        "backend heartbeat started on port %d every %.1fs",
        port,
        HEARTBEAT_INTERVAL_SECONDS,
    )
    return True


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
