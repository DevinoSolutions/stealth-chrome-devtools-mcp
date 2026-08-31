"""THE one home for "the backend under this stdio proxy died — heal in place
instead of exiting" (F-838).

Until this module existed, the proxy's only reaction to the liveness watchdog's
CONFIRMED-dead verdict (``singleton._watch_backend_liveness``: F-820's three
strikes plus the identity+readiness confirmation) was to log "backend became
unreachable; tearing down for reconnect" and return. That rested on a premise
that does not hold — MCP clients do not reliably respawn a stdio server
mid-session — so every real backend death became a dead MCP server until the
user reconnected by hand (2026-08-30: an OOM crash at 15:46; a console
CTRL_BREAK at 18:43, on a backend born console-attached via the foreground
``serve --http`` path that never gets the DETACHED spawn flags). F-839 (ignore
SIGBREAK) and F-829 (an unreadable fingerprint is not an edit) each remove one
CAUSE; this is the backstop that must hold for ANY cause, including the ones
nobody has diagnosed yet.

**It heals through the startup path, never beside it.** ``heal_backend`` calls
the ``ensure_running`` it is handed — in production
``singleton.ensure_server_running``, the very function ``run_stdio_proxy`` calls
at boot — so recovery inherits the one identity+readiness reuse gate, the F-808
adoption order and the cold-start lock without restating a line of any of them.
Nothing here decides what a reusable backend is, which port to spawn on, or who
may spawn: those questions already have homes.

**That is also the whole answer to the thundering herd.** When a shared backend
dies, every proxy bridged to it confirms the death inside the same second and
lands here at once. They all ask for the SAME preferred port (the dead one), so
the cold-start lock does what it already does for a 40-session startup herd:
exactly one wins and spawns, the rest find the lock held, return immediately and
adopt what the winner brings up.

**Bounded, then honest.** ``HEAL_ATTEMPTS`` tries, each with a
``HEAL_ATTEMPT_SECONDS`` readiness budget and a fixed backoff between them, is
the entire allowance. A proxy that still has no backend returns to its caller
and the pre-F-838 teardown runs — a backstop that retried forever would just be
a proxy pretending to be alive, which is the failure mode it exists to end.

**In-flight calls are failed, never replayed.** ``PendingCalls`` reports the
requests the dead backend was actually holding as JSON-RPC errors, so the client
sees one failed call instead of an eternal wait — and so a non-idempotent
``tools/call`` is never silently re-run against the replacement.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# One stream: the heal is part of the proxy's story, so it writes to the proxy
# log configure_logging("proxy") already owns (same logger name as singleton).
_logger = logging.getLogger("stealth.proxy")

# Two tries, because the first can legitimately lose a race it should not pay
# for twice: a proxy that wins the cold-start lock while the OS has not yet
# released the dead backend's port, or one that adopts a replacement that dies
# again during its own boot. Beyond that the machine, not the backend, is the
# problem, and the caller's teardown is the honest answer.
HEAL_ATTEMPTS = 2
# Per-attempt readiness budget. Generous against a cold start (STARTUP_TIMEOUT
# is 30s for the socket alone, and a herd's adopters wait behind the winner),
# but far short of the proxy's 120s BACKEND_READY_TIMEOUT: two attempts must
# stay inside the window in which a user would still call this "one slow call".
HEAL_ATTEMPT_SECONDS = 45.0
HEAL_BACKOFF_SECONDS = 1.0
# Flap guard on the OTHER axis. ``heal_backend`` bounds one recovery; this
# bounds recoveries. A replacement that is itself confirmed dead shortly after
# it came up means the machine — not this backend — is the problem, and a proxy
# that kept re-healing it would be the tight retry loop in slow motion. A
# generation that lived at least ``HEAL_STREAK_RESET_SECONDS`` was a real
# working session, so it earns the budget back.
MAX_CONSECUTIVE_HEALS = 3
HEAL_STREAK_RESET_SECONDS = 300.0

# JSON-RPC "Internal error" — the only reserved code that fits "the server this
# call was sent to stopped existing".
_BACKEND_DIED_CODE = -32603

# The proxy's reported vocabulary (F-827). Three of the four disconnect-relevant
# transitions happen here, so their names live here; the fourth (an eviction for
# a genuine source change) is reported from its own site in ``singleton``. Named
# constants rather than inline literals because a report whose message drifts is
# a report nobody can search for.
CONDEMNED_EVENT = "proxy: backend condemned"
HEALED_EVENT = "proxy: backend healed"
TEARDOWN_EVENT = "proxy: teardown after failed heal"


def _report(message: str, *, level: str = "warning", **fields: object) -> None:
    """Ship one lifecycle transition, or don't — but never disturb the caller.

    ``observability.capture_lifecycle`` already promises never to raise; this
    wrapper exists because the promise is not the point. A module whose whole
    job is to be the backstop when everything else has failed cannot have its
    control flow depend on a telemetry call being well-behaved, so the guard is
    local and unconditional. Imported lazily and by module attribute: the leaf
    stays importable without the reporting stack, and a test can spy the one
    seam. This is the only place under ``embedded/`` that talks to Sentry
    besides the backend's own ``sentry_init`` — reporting stays single-homed in
    ``observability``; nothing here decides what an event looks like.
    """
    try:
        from stealth_chrome_devtools_mcp.observability import capture_lifecycle

        capture_lifecycle(message, level=level, **fields)
    except Exception:  # noqa: BLE001  PERMANENT(a backstop must not raise)
        _logger.debug("could not report a lifecycle transition", exc_info=True)


def _teardown(*, port: int, generation: int, reason: str, streak: int) -> None:
    """The one report for the ONE remaining user-visible disconnect.

    ``drive`` gives up on two axes — this recovery failed (``unhealable``) and
    recoveries keep failing (``flapping``) — but the user experiences a single
    thing: the MCP server went away. One event with a ``reason``, not two event
    names, so a search for "how often does stealth still disconnect" is one
    query. ERROR level: after F-838 this is the only path left that ends a
    session, so it is not a warning about something, it is the something.
    """
    _report(
        TEARDOWN_EVENT,
        level="error",
        port=port,
        generation=generation,
        reason=reason,
        consecutive_heals=streak,
    )


class _MessageSink(Protocol):
    """The client-facing half of the proxy: anything we can hand a message to."""

    async def send(self, item: object) -> None: ...


class PendingCalls:
    """The client requests already WRITTEN to a backend that never answered.

    Tracked at the write to the backend, not at the read from the client, and
    that distinction is the contract: messages still buffered when the backend
    died were never seen by it, so forwarding them to the replacement is
    correct rather than a retry, while the ones genuinely in flight are failed
    explicitly. An unanswered id is an unbounded wait for whatever is driving
    the client; replaying a non-idempotent ``tools/call`` would be worse.
    """

    def __init__(self) -> None:
        self._inflight: dict[str | int, str] = {}

    def track(self, inner: object) -> None:
        """Record a request being handed to the backend (ignores notifications,
        which have no id and therefore no answer to owe)."""
        method = getattr(inner, "method", None)
        request_id = getattr(inner, "id", None)
        if method is not None and isinstance(request_id, str | int):
            self._inflight[request_id] = str(method)

    def settle(self, inner: object) -> None:
        """Drop a request the backend has now answered (result or error)."""
        answered = getattr(inner, "id", None)
        if isinstance(answered, str | int):
            self._inflight.pop(answered, None)

    async def fail_all(self, client_write: _MessageSink, port: int) -> None:
        """Answer everything still in flight with a clear error, once."""
        from mcp.shared.message import SessionMessage
        from mcp.types import ErrorData, JSONRPCError, JSONRPCMessage

        inflight, self._inflight = self._inflight, {}
        for request_id, method in inflight.items():
            error = JSONRPCError(
                jsonrpc="2.0",
                id=request_id,
                error=ErrorData(
                    code=_BACKEND_DIED_CODE,
                    message=(
                        f"the backend on port {port} died while '{method}' was "
                        "in flight; the call was NOT retried against its "
                        "replacement — reissue it if it is safe to repeat"
                    ),
                ),
            )
            try:
                await client_write.send(SessionMessage(message=JSONRPCMessage(error)))
            except Exception:  # noqa: BLE001  PERMANENT(a backstop must not raise)
                _logger.debug("could not report an in-flight failure", exc_info=True)


async def heal_backend(
    dead_port: int,
    *,
    ensure_running: Callable[[int], int | None],
    await_ready: Callable[..., Awaitable[bool]],
    url_for: Callable[[int], str],
) -> int | None:
    """Get this proxy a live backend again, or ``None`` when it cannot.

    ``dead_port`` is passed on as the PREFERRED port, which is what makes the
    herd converge: every proxy orphaned by the same death asks for the same
    port, so the cold-start lock's winner spawns there and everyone else adopts
    it. ``ensure_running`` blocks (socket probes, and the reuse gate's
    ``initialize``), so it is driven off-thread — run inline it would freeze the
    stdio pump this whole module exists to keep alive.
    """
    import anyio

    for attempt in range(1, HEAL_ATTEMPTS + 1):
        try:
            port = await anyio.to_thread.run_sync(ensure_running, dead_port)
            if port is not None and await await_ready(
                url_for(port), HEAL_ATTEMPT_SECONDS
            ):
                _logger.warning(
                    "backend healed: re-bridging to port %d (attempt %d/%d)",
                    port,
                    attempt,
                    HEAL_ATTEMPTS,
                )
                return port
            _logger.warning(
                "heal attempt %d/%d produced no ready backend", attempt, HEAL_ATTEMPTS
            )
        except Exception:  # noqa: BLE001  PERMANENT(a backstop must not raise)
            _logger.warning("heal attempt %d/%d failed", attempt, HEAL_ATTEMPTS)
            _logger.debug("heal attempt detail", exc_info=True)
        if attempt < HEAL_ATTEMPTS:
            await anyio.sleep(HEAL_BACKOFF_SECONDS)
    _logger.error("backend unhealable after %d attempts", HEAL_ATTEMPTS)
    return None


async def _one_generation(  # noqa: PLR0913  PERMANENT(function interface)
    *,
    url: str,
    replay_msg: object,
    port: int,
    connect: Callable[..., Awaitable[None]],
    watch: Callable[..., Awaitable[None]],
    pending: PendingCalls,
    client_write: _MessageSink,
) -> bool:
    """Bridge to ONE backend until it ends; True iff it ended CONFIRMED dead.

    ``connect`` sets the ``armed`` event it is handed once the backend has
    genuinely answered an ``initialize``; ``watch`` (the F-820 watchdog) is held
    back until then, so a replacement's own cold start can never be condemned by
    the monitor that was watching its predecessor.
    """
    import anyio

    armed = anyio.Event()
    died = anyio.Event()
    async with anyio.create_task_group() as generation:

        async def bridge() -> None:
            try:
                await connect(url, replay_msg, armed)
            except Exception:  # noqa: BLE001  PERMANENT(a dead backend must not crash the proxy)
                _logger.warning("backend connection lost", exc_info=True)
            finally:
                generation.cancel_scope.cancel()

        async def monitor() -> None:
            await armed.wait()
            live_since = time.monotonic()
            await watch(port)
            # ``watch`` returns for exactly one reason (F-820's strikes plus the
            # identity+readiness confirmation), so this IS the condemnation. The
            # elapsed time dates the verdict against the backend's own logs: how
            # long it had been serving this proxy before the strikes concluded.
            _report(
                CONDEMNED_EVENT,
                port=port,
                strike_seconds=round(time.monotonic() - live_since, 1),
            )
            died.set()
            generation.cancel_scope.cancel()

        generation.start_soon(bridge)
        generation.start_soon(monitor)

    await pending.fail_all(client_write, port)
    return died.is_set()


async def drive(  # noqa: PLR0913  PERMANENT(function interface)
    *,
    port: int,
    url_for: Callable[[int], str],
    connect: Callable[..., Awaitable[None]],
    watch: Callable[..., Awaitable[None]],
    replay: Callable[[], object],
    pending: PendingCalls,
    client_write: _MessageSink,
    ensure_running: Callable[[int], int | None],
    await_ready: Callable[..., Awaitable[bool]],
) -> None:
    """Own the proxy's backend leg across backend GENERATIONS.

    One iteration per backend. A generation that merely ends (the client went
    away, the connection raised, readiness never came) returns at once — that is
    the pre-F-838 behaviour, untouched. Only a CONFIRMED death heals, and only
    while healing works and the deaths are not a flap; ``replay()`` yields the
    client's original ``initialize`` so the next generation opens with a real
    handshake and its own fresh ``mcp-session-id``. Returning means the caller
    should tear down, which is what it always did.

    ``generation`` exists only to be reported (F-827): a heal is far easier to
    read as "the 3rd backend under this proxy" than as a port number, and both
    give-up exits carry it too, so a teardown says how much recovery preceded
    it. Nothing in the loop's decisions reads it.
    """
    current: int = port
    resend: object = None
    streak = 0
    generation = 1
    while True:
        started = time.monotonic()
        confirmed_dead = await _one_generation(
            url=url_for(current),
            replay_msg=resend,
            port=current,
            connect=connect,
            watch=watch,
            pending=pending,
            client_write=client_write,
        )
        if not confirmed_dead:
            return
        if time.monotonic() - started >= HEAL_STREAK_RESET_SECONDS:
            streak = 0  # that generation was a real working session
        streak += 1
        if streak > MAX_CONSECUTIVE_HEALS:
            _logger.error("backend died %d times in a row; giving up", streak)
            _teardown(
                port=current, generation=generation, reason="flapping", streak=streak
            )
            return
        healed = await heal_backend(
            current,
            ensure_running=ensure_running,
            await_ready=await_ready,
            url_for=url_for,
        )
        if healed is None:
            _teardown(
                port=current, generation=generation, reason="unhealable", streak=streak
            )
            return
        generation += 1
        # The recovery the user never saw. Reported at INFO because it is the
        # denominator: a teardown means little without how often healing WORKED.
        _report(
            HEALED_EVENT,
            level="info",
            old_port=current,
            new_port=healed,
            generation=generation,
            consecutive_heals=streak,
        )
        current, resend = healed, replay()
