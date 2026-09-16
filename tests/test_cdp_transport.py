"""F-883 B1 / F-788 / F-794 — a cancelled caller must not cancel its CDP reply.

The defect is nodriver's, and it is three lines of ``core/connection.py``:
``send()`` registers a ``Transaction`` in ``Connection.mapper`` and then
``await tx`` with no ``finally``; ``_listener`` later does
``tx = self.mapper.pop(id); tx(**message)`` in an ``else:`` branch with no
``try``. Cancel the coroutine that owns the await and the future is cancelled
while still registered, so the late answer's ``set_result`` raises
``InvalidStateError`` INSIDE the listener task and ends it — the one task that
resolves every future and dispatches every event on that connection.

Every node here is built out of nodriver's OWN classes: a real ``Transaction``
around a real CDP command generator, answered through the real
``Transaction.__call__`` by the two lines the listener runs. A hand-written
double would have to encode the bug in order to prove it, and this repo has
already been bitten by a double that encoded the wrong policy and hid the defect
it was written to catch.

The two halves this mechanism serves are pinned where they are visible:
``tests/test_e2e_execute_script_async.py`` (a Promise settling after
``timeout_ms`` leaves the instance usable, real Chrome) and
``tests/test_wire_semantics.py`` (a cancelled request stops the body, real wire
frames). The last node here is the sensitivity control — without the patch, the
same cancellation kills the listener's own code path.
"""

import asyncio

import pytest
from nodriver import cdp
from nodriver.core.connection import Transaction

from stealth_chrome_devtools_mcp.embedded import cdp_transport

pytestmark = pytest.mark.asyncio

_REPLY = {"result": {"type": "number", "value": 2}}


def _transaction() -> Transaction:
    """A real nodriver ``Transaction``, around a real CDP command generator."""
    tx = Transaction(cdp.runtime.evaluate(expression="1 + 1"))
    tx.id = 1
    return tx


def _listener_delivers(mapper: dict, message: dict) -> None:
    """The two lines ``Connection._listener`` runs for a command reply.

    Unguarded on purpose, shape for shape: what is being proved is that they
    cannot raise, not that someone caught it when they did.
    """
    tx: Transaction = mapper.pop(message["id"])
    tx(**message)


async def _send(mapper: dict) -> object:
    """``Connection.send``'s last three lines: register, then await."""
    tx = _transaction()
    mapper[tx.id] = tx
    return await tx


async def test_the_patch_is_on_the_class_every_send_resolves():
    """A dunder is looked up on the TYPE, which is why one assignment covers
    nodriver's own sends (``Tab.evaluate``, ``Element.apply``,
    ``update_targets``) as well as ours — none of those pass through a seam of
    ours."""
    cdp_transport.install()
    assert cdp_transport.installed()
    assert Transaction.__await__.__doc__
    assert "without being able to cancel it" in Transaction.__await__.__doc__


async def test_install_is_idempotent_because_server_py_is_executed_three_times():
    """``embedded/server.py`` is executed three times under runpy. A second wrap
    would nest a shield in a shield — harmless today, and exactly the kind of
    silent accumulation plan_SERVERSPLIT §7 R4 found as 282 = 3 x 94."""
    cdp_transport.install()
    once = Transaction.__await__
    cdp_transport.install()
    cdp_transport.install()
    assert Transaction.__await__ is once


async def test_a_cancelled_caller_leaves_the_reply_deliverable():
    """The whole finding in one node: cancel the caller, then let the answer
    arrive exactly the way the listener delivers it."""
    cdp_transport.install()
    mapper: dict[int, Transaction] = {}
    entered = asyncio.Event()

    async def caller():
        entered.set()
        await _send(mapper)

    task = asyncio.ensure_future(caller())
    await entered.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    (tx,) = mapper.values()
    assert not tx.cancelled(), (
        "the Transaction is still registered in mapper — cancelling it is what "
        "kills the listener when Chrome answers"
    )
    _listener_delivers(mapper, {"id": tx.id, "result": _REPLY})
    assert tx.done() and not tx.cancelled()


async def test_the_caller_stops_at_the_send_it_was_cancelled_on():
    """The other half of the contract, and the reason the protection is not at
    ``_with_cdp_timeout``: the ``CancelledError`` still arrives AT the await, so
    the next line never runs. A cancelled ``navigate`` must not navigate."""
    cdp_transport.install()
    mapper: dict[int, Transaction] = {}
    reached_next_line = False
    entered = asyncio.Event()

    async def body():
        nonlocal reached_next_line
        entered.set()
        await _send(mapper)
        reached_next_line = True

    task = asyncio.ensure_future(body())
    await entered.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert not reached_next_line, "a cancelled body must stop, not run on"


async def test_a_timed_out_caller_leaves_the_reply_deliverable_too():
    """The product's own bound reaches the same mechanism: ``_with_cdp_timeout``
    cancels the operation, and the send survives it."""
    cdp_transport.install()
    mapper: dict[int, Transaction] = {}

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_send(mapper), timeout=0.2)

    (tx,) = mapper.values()
    assert not tx.cancelled()
    _listener_delivers(mapper, {"id": tx.id, "result": _REPLY})
    assert tx.result() is not None


async def test_an_answer_that_arrives_in_time_is_returned_unchanged():
    """Every CDP command in the product goes through here, so the happy path is
    the one that must not change: the parsed value, not the shield."""
    cdp_transport.install()
    mapper: dict[int, Transaction] = {}

    async def answer_soon():
        await asyncio.sleep(0.05)
        (tx,) = mapper.values()
        _listener_delivers(mapper, {"id": tx.id, "result": _REPLY})

    answering = asyncio.ensure_future(answer_soon())  # noqa: RUF006 - FALSE-POSITIVE(the task is awaited two lines down)
    remote, exception_details = await _send(mapper)
    await answering
    # The PARSED cdp object, exactly as an unshielded await produced it:
    # ``Runtime.evaluate`` answers ``(RemoteObject, exceptionDetails)``.
    assert remote.value == 2
    assert exception_details is None


async def test_a_protocol_error_still_reaches_the_caller():
    """``Transaction.__call__`` answers an error by ``set_exception``; the
    shield must pass it through, or a failed command would read as a hang."""
    cdp_transport.install()
    mapper: dict[int, Transaction] = {}

    async def fail_soon():
        await asyncio.sleep(0.05)
        (tx,) = mapper.values()
        _listener_delivers(
            mapper, {"id": tx.id, "error": {"code": -32000, "message": "nope"}}
        )

    failing = asyncio.ensure_future(fail_soon())  # noqa: RUF006 - FALSE-POSITIVE(the task is awaited two lines down)
    with pytest.raises(Exception, match="nope"):
        await _send(mapper)
    await failing


async def test_without_the_patch_the_listener_dies_on_the_same_cancellation():
    """The sensitivity control. Restores nodriver's own ``__await__`` for the
    length of the node, so a green run above means the patch did it — not that
    the mechanism was never there to break."""
    cdp_transport.install()
    patched = Transaction.__await__
    original = patched.__stealth_cdp_transport__
    Transaction.__await__ = original
    try:
        mapper: dict[int, Transaction] = {}
        task = asyncio.ensure_future(_send(mapper))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        (tx,) = mapper.values()
        assert tx.cancelled()
        with pytest.raises(asyncio.InvalidStateError):
            _listener_delivers(mapper, {"id": tx.id, "result": _REPLY})
    finally:
        Transaction.__await__ = patched
