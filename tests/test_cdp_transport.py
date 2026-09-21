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

import ast
import asyncio
import traceback
from pathlib import Path

import pytest
from nodriver import cdp
from nodriver.core.connection import EventTransaction, Transaction

from stealth_chrome_devtools_mcp import observability
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
    would nest a shield in a shield, a guard in a guard (double reporting) or a
    ``from_json`` wrapper in a wrapper — harmless today, and exactly the kind of
    silent accumulation plan_SERVERSPLIT §7 R4 found as 282 = 3 x 94.

    **All THREE halves, because the marker check is per-half** (F-902 review
    S4): asserting only ``__await__`` would pass for a version that re-wrapped
    the other two on every call. The cookie one is compared through
    ``__func__``, because attribute access on a classmethod builds a NEW bound
    method object each time and ``is`` on those is always False.
    """
    cdp_transport.install()
    once_await = Transaction.__await__
    once_call = Transaction.__call__
    once_from_json = cdp.network.Cookie.from_json.__func__
    cdp_transport.install()
    cdp_transport.install()
    assert Transaction.__await__ is once_await
    assert Transaction.__call__ is once_call
    assert cdp.network.Cookie.from_json.__func__ is once_from_json
    assert cdp_transport.installed()


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


async def test_awaiting_an_already_done_transaction_returns_its_value():
    """``shield`` on a DONE future returns the inner future itself, and the
    inner future here is a ``Transaction`` whose ``__await__`` is this very
    patch — so shielding unconditionally recursed until ``RecursionError``.

    Latent on nodriver 0.47 rather than live: ``send()`` has no yield point
    between ``self.mapper[the_id] = tx`` and ``await tx`` (``create_task`` does
    not suspend), so the listener cannot resolve a Transaction before it is
    awaited. It is pinned anyway, because the module claims in its docstring
    that a done future costs nothing, and an untested claim about a library's
    internals is how a nodriver release turns hang-protection into a crash on
    every CDP command.
    """
    cdp_transport.install()
    tx = _transaction()
    tx.set_result("already-here")
    assert tx.done()
    assert await tx == "already-here"


async def test_awaiting_an_event_transaction_returns_its_event():
    """``EventTransaction`` is constructed COMPLETE (its ``__init__`` ends in
    ``set_result``), so it is the done-future case that a library change would
    reach first. It is dead code in nodriver 0.47 — constructed nowhere, the
    class statement is its only occurrence — which is exactly why the cost of
    getting it wrong is invisible until it is not."""
    cdp_transport.install()
    event = cdp.page.DomContentEventFired(timestamp=1.0)
    ev = EventTransaction(event)
    assert ev.done(), "nodriver constructs this one complete"
    assert type(ev).__await__ is Transaction.__await__, "it inherits the patch"
    assert await ev is event


async def test_a_delivered_reply_can_be_awaited_again():
    """The same shape reached from the product's own path: once the listener
    has delivered, the Transaction is done, and awaiting it a second time must
    answer rather than recurse."""
    cdp_transport.install()
    mapper: dict[int, Transaction] = {}

    async def answer_soon():
        await asyncio.sleep(0.05)
        (tx,) = mapper.values()
        _listener_delivers(mapper, {"id": tx.id, "result": _REPLY})

    answering = asyncio.ensure_future(answer_soon())  # noqa: RUF006 - FALSE-POSITIVE(the task is awaited two lines down)
    tx = _transaction()
    mapper[tx.id] = tx
    first = await tx
    await answering
    assert await tx == first


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


# ---------------------------------------------------------------------------
# F-902 — a reply that cannot be PARSED must fail its own call, not the
# connection. Same discipline as the nodes above: real ``Transaction``, real
# generated CDP command generator, delivered through the two unguarded lines
# ``Connection._listener`` runs.
# ---------------------------------------------------------------------------


#: The raw JSON of ONE cookie, copied verbatim out of a
#: ``Network.getAllCookies`` reply captured over a RAW WEBSOCKET to
#: Chrome/153.0.8010.50 on 2026-09-21 (Windows 11 26200), with nodriver nowhere
#: in the path. Name and value are the capture fixture's own synthetic ones.
#:
#: It is written out here rather than imported from the product for
#: ``fakes.TARGET_SWAPPED_ERROR``'s reason: a pin about what CHROME sends must
#: measure against Chrome's bytes, or it compares our idea of the wire to
#: itself. ``sameParty`` is absent because Chrome 153 does not send it — that
#: absence IS the fixture.
CHROME_153_COOKIE = {
    "domain": "127.0.0.1",
    "expires": -1,
    "httpOnly": False,
    "name": "f902_probe",
    "path": "/",
    "priority": "Medium",
    "secure": False,
    "session": True,
    "size": 25,
    "sourcePort": 36829,
    "sourceScheme": "NonSecure",
    "value": "synthetic-value",
}


#: Generous for a delivery that is three local function calls away, and short
#: enough that a REGRESSION reads as a failed node rather than a stalled lane —
#: the defect this file is about makes replies never arrive at all.
_DELIVERY_BUDGET = 5.0


def _cookie_transaction(tx_id: int = 1) -> Transaction:
    """A real Transaction around the real ``Network.getAllCookies`` generator."""
    tx = Transaction(cdp.network.get_all_cookies())
    tx.id = tx_id
    return tx


async def test_a_chrome_153_cookie_parses_without_sameparty():
    """Half 3. Chrome removed ``Network.Cookie.sameParty``; nodriver 0.47 still
    reads ``json['sameParty']`` unconditionally, so every cookie reply raised."""
    cdp_transport.install()
    cookie = cdp.network.Cookie.from_json(CHROME_153_COOKIE)
    assert cookie.name == "f902_probe"
    assert cookie.value == "synthetic-value"
    assert cookie.source_port == 36829  # the fields Chrome DOES send are intact
    assert cookie.same_party is False  # synthesised; the module names this cost


async def test_a_cookie_reply_resolves_through_the_listeners_own_two_lines():
    """The end-to-end of halves 2+3 at the layer the defect lives in: the
    listener delivers a Chrome 153 cookie reply and the caller gets its value."""
    cdp_transport.install()
    mapper: dict[int, Transaction] = {}

    async def answer():
        await asyncio.sleep(0.05)
        (tx,) = mapper.values()
        _listener_delivers(
            mapper, {"id": tx.id, "result": {"cookies": [CHROME_153_COOKIE]}}
        )

    async def send() -> object:
        tx = _cookie_transaction()
        mapper[tx.id] = tx
        return await tx

    answering = asyncio.ensure_future(answer())  # noqa: RUF006 - FALSE-POSITIVE(awaited three lines down)
    # Bounded: the defect's signature is a reply that NEVER arrives, so an
    # unbounded await would hang the lane instead of failing this node.
    cookies = await asyncio.wait_for(send(), _DELIVERY_BUDGET)
    await answering
    assert [c.name for c in cookies] == ["f902_probe"]


async def test_an_unparseable_reply_fails_its_own_call_and_not_the_listener():
    """Half 2, on a field half 3 does NOT tolerate, so this node cannot pass by
    accident of the retired-field table.

    ``sourcePort`` is required by the generated parser and is not in
    ``_RETIRED_COOKIE_FIELDS`` (Chrome 153 still sends it). The delivery must
    therefore not raise — that is the whole finding — and the caller must be
    told its own command failed.
    """
    cdp_transport.install()
    mapper: dict[int, Transaction] = {}
    unreadable = {k: v for k, v in CHROME_153_COOKIE.items() if k != "sourcePort"}

    async def answer():
        await asyncio.sleep(0.05)
        (tx,) = mapper.values()
        # Unguarded on purpose, shape for shape with ``_listener``: what is
        # being proved is that these two lines CANNOT raise.
        _listener_delivers(mapper, {"id": tx.id, "result": {"cookies": [unreadable]}})

    async def send() -> object:
        tx = _cookie_transaction()
        mapper[tx.id] = tx
        return await tx

    answering = asyncio.ensure_future(answer())  # noqa: RUF006 - FALSE-POSITIVE(awaited below)
    with pytest.raises(cdp_transport.CdpReplyError) as caught:
        await asyncio.wait_for(send(), _DELIVERY_BUDGET)  # bounded: see above
    await answering

    assert "Network.getAllCookies" in str(caught.value)
    assert "sourcePort" in str(caught.value)  # the protocol field, safely named


async def test_a_second_command_still_resolves_after_an_unparseable_reply():
    """The point of half 2 stated as the harm it prevents: one bad reply used to
    end the listener, so every LATER call on that connection hung forever."""
    cdp_transport.install()
    mapper: dict[int, Transaction] = {}
    unreadable = {k: v for k, v in CHROME_153_COOKIE.items() if k != "sourcePort"}

    first = _cookie_transaction(tx_id=1)
    mapper[first.id] = first
    second = _transaction()
    second.id = 2
    mapper[second.id] = second

    # The listener's own two lines, twice, with nothing catching in between.
    _listener_delivers(mapper, {"id": 1, "result": {"cookies": [unreadable]}})
    _listener_delivers(mapper, {"id": 2, "result": _REPLY})

    with pytest.raises(cdp_transport.CdpReplyError):
        await first
    assert await second is not None, "the sibling command must still resolve"


async def test_no_cookie_name_or_value_reaches_the_report(caplog):
    """PII. nodriver's own re-raise interpolates ``response['result']`` — the
    WHOLE reply — into its message, and the measured failure escaped as an
    unretrieved task exception, i.e. the asyncio handler, the durable log and
    Sentry at once. A page's cookie jar is where its sessions live.
    """
    cdp_transport.install()
    mapper: dict[int, Transaction] = {}
    unreadable = {k: v for k, v in CHROME_153_COOKIE.items() if k != "sourcePort"}
    tx = _cookie_transaction()
    mapper[tx.id] = tx

    with caplog.at_level(0):
        _listener_delivers(mapper, {"id": tx.id, "result": {"cookies": [unreadable]}})
        with pytest.raises(cdp_transport.CdpReplyError) as caught:
            await tx

    reported = str(caught.value)
    for secret in ("f902_probe", "synthetic-value", "127.0.0.1"):
        assert secret not in reported, f"{secret!r} leaked into the error message"
        assert secret not in caplog.text, f"{secret!r} leaked into a log line"
    # And nothing smuggles it out through a chained exception either.
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


async def test_the_guarded_frame_is_absent_from_the_delivered_traceback():
    """F-902 review S2, first half. The whole reply is a local named
    ``response`` in the guarded ``__call__``, so what keeps it out of a
    serialised event is that the error is STORED and never RAISED there — a
    stored exception contributes no traceback frame, so there is no frame for a
    serialiser to read locals off.

    MEASURED: the only frame is the awaiting caller's. It is pinned because it
    is a property of construct-and-store that a refactor to ``raise`` here would
    silently end, and because the module's PII paragraph now claims it.
    """
    cdp_transport.install()
    mapper: dict[int, Transaction] = {}
    unreadable = {k: v for k, v in CHROME_153_COOKIE.items() if k != "sourcePort"}
    tx = _cookie_transaction()
    mapper[tx.id] = tx

    _listener_delivers(mapper, {"id": tx.id, "result": {"cookies": [unreadable]}})
    with pytest.raises(cdp_transport.CdpReplyError) as caught:
        await tx

    frames = traceback.extract_tb(caught.value.__traceback__)
    assert frames, "no traceback at all would make this node vacuous"
    assert "__call__" not in [f.name for f in frames], (
        "the guarded frame is in the traceback — the raw reply is a local there"
    )
    # Compared against the module's OWN path, never a filename suffix:
    # "test_cdp_transport.py".endswith("cdp_transport.py") is True, which made
    # the first version of this node fail on its own frame.
    guarded = Path(cdp_transport.__file__).resolve()
    offenders = [f for f in frames if Path(f.filename).resolve() == guarded]
    assert not offenders, (
        "no frame of this module may appear in a delivered reply's traceback — "
        f"the raw reply is a local there: {[(f.lineno, f.name) for f in offenders]}"
    )


def test_the_pii_argument_depends_on_sentry_not_capturing_locals():
    """F-902 review S2, second half.

    ``cdp_transport``'s shape-only rule is defence in depth, and the outermost
    layer belongs to ANOTHER module: if ``sentry_sdk.init`` ever captured frame
    locals, a payload sitting in a local would travel regardless of how careful
    the message is. Nothing linked the two homes, so this node is the link —
    flipping that flag fails a test in the file that depends on it, not only in
    ``tests/test_observability.py`` where it reads as a preference.
    """
    source = Path(observability.__file__).read_text(encoding="utf-8").replace(" ", "")
    assert "include_local_variables=False" in source, (
        "cdp_transport's PII argument assumes Sentry does not serialise frame "
        "locals; see half 2's docstring"
    )


def test_every_generated_parser_indexes_with_a_literal_field_name():
    """F-902 review S3. ``_missing_field``'s regex proves IDENTIFIER-shaped, and
    a cookie name is frequently exactly that shape — so echoing the key is safe
    only under a premise about nodriver: every ``KeyError`` a generated
    ``from_json`` can raise names a LITERAL protocol field, never a
    page-controlled one.

    MEASURED here rather than asserted in prose, so a nodriver release that
    introduces a computed lookup fails this node instead of quietly turning a
    diagnostic into a leak.
    """
    root = Path(cdp.__file__).parent
    offenders: list[str] = []
    parsers = 0

    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef) and node.name == "from_json"):
                continue
            parsers += 1
            for sub in ast.walk(node):
                if isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name):
                    key = sub.slice
                    if not (
                        isinstance(key, ast.Constant) and isinstance(key.value, str)
                    ):
                        offenders.append(f"{path.name}:{sub.lineno}")

    assert parsers > 100, f"only {parsers} parsers found — the scan missed them"
    assert not offenders, (
        "a generated from_json indexes with a NON-literal key, so a KeyError "
        f"could name page data: {offenders[:10]}"
    )


async def test_a_reply_for_a_cancelled_transaction_is_dropped_not_re_raised():
    """``set_exception`` on a done future raises ``InvalidStateError`` — inside
    the listener, which is the shape half 1 exists to stop. Half 2 must not
    reintroduce it by the other door."""
    cdp_transport.install()
    mapper: dict[int, Transaction] = {}
    unreadable = {k: v for k, v in CHROME_153_COOKIE.items() if k != "sourcePort"}
    tx = _cookie_transaction()
    mapper[tx.id] = tx
    tx.cancel()

    _listener_delivers(mapper, {"id": tx.id, "result": {"cookies": [unreadable]}})
    assert tx.cancelled()


async def test_without_the_result_guard_the_listener_dies_on_a_cookie_reply():
    """The sensitivity control for halves 2+3, on the precedent of the node
    below: restore nodriver's own ``__call__`` and its own ``Cookie.from_json``
    for the length of the node, so a green run above means these patches did it.
    """
    cdp_transport.install()
    patched_call = Transaction.__call__
    patched_from_json = cdp.network.Cookie.from_json
    Transaction.__call__ = patched_call.__stealth_cdp_result_guard__
    cdp.network.Cookie.from_json = classmethod(
        patched_from_json.__stealth_cdp_cookie_compat__.__func__
    )
    try:
        mapper: dict[int, Transaction] = {}
        tx = _cookie_transaction()
        mapper[tx.id] = tx
        # A REAL Chrome 153 reply, and the listener's own two lines die on it.
        with pytest.raises(KeyError):
            _listener_delivers(
                mapper, {"id": tx.id, "result": {"cookies": [CHROME_153_COOKIE]}}
            )
    finally:
        Transaction.__call__ = patched_call
        cdp.network.Cookie.from_json = patched_from_json
        tx.cancel()


async def test_without_the_patch_the_listener_dies_on_the_same_cancellation():
    """The sensitivity control. Restores nodriver's own ``__await__`` for the
    length of the node, so a green run above means the patch did it — not that
    the mechanism was never there to break.

    It restores nodriver's own ``__call__`` too, and that is a statement about
    the two halves rather than test bookkeeping: **F-902's result guard also
    absorbs F-883's crash** — a late reply for a cancelled Transaction reaches
    ``set_result``, the ``InvalidStateError`` is caught, and the listener lives.
    Half 1 is still not redundant, and the difference is what the caller gets:
    with the guard alone the reply is DROPPED (the connection survives, the
    answer is lost), while half 1 leaves the Transaction pending and registered,
    which is the state the listener knows how to FINISH. Defence in depth, named
    here so neither half is deleted as redundant to the other.
    """
    cdp_transport.install()
    patched = Transaction.__await__
    patched_call = Transaction.__call__
    Transaction.__await__ = patched.__stealth_cdp_transport__
    Transaction.__call__ = patched_call.__stealth_cdp_result_guard__
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
        Transaction.__call__ = patched_call
