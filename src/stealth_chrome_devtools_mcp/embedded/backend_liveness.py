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

The vocabulary is one closed set, shared with ``cli._probe_recorded_backend``
(which adds the single word this cannot reach, "no port recorded", for an entry
naming nothing usable as a port): ``down`` | ``wedged`` | ``responsive``, plus
``none`` for "no adoptable entry names a port at all".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.embedded import backend_registry

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def probe_port(
    port: int,
    *,
    is_healthy: Callable[[int], bool],
    http_ready: Callable[[int], bool],
) -> str:
    """THE liveness ladder for ONE port — socket, then a real MCP `initialize`:
    "down" | "wedged" | "responsive" (F-301's third state, which a bare socket
    check cannot see). Read-only. THE one home for those four lines (F-868),
    with three readers: the candidate walk below, `restart_backend`'s report of
    the port it spawned on, and doctor's `cli._probe_recorded_backend`, which
    adds only the one word this cannot reach ("no port recorded") and was a
    verbatim copy of this ladder until now.
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
