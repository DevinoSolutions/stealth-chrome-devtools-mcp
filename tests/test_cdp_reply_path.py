"""F-883 B1 / F-788 / F-794 / F-902 on nodriver 0.50 -- a CDP reply cannot kill the listener.

Until nodriver 0.47 this repo patched ``Transaction`` to make that true
(``embedded/cdp_transport.py``: a shield on ``__await__``, a guard on
``__call__``, a ``sameParty`` tolerance on ``Cookie.from_json``). nodriver 0.50
rebuilt the seam those patches wrapped, and the module was deleted with it
(T-8501). What the patches defended is now the library's own design, so what
this file pins is that DESIGN -- over a real ``Connection`` and its real
``_listener`` on a fake websocket, never a model of them:

* ``Transaction`` is a plain record (``id``, ``request``, ``events``,
  ``result``): it has no ``__await__`` and no ``__call__``, and
  ``EventTransaction`` is gone, so there is nothing left to wrap or to cancel;
* ``Connection.send`` owns a future registered in ``_mapper[id]`` and removes it
  in a ``finally``; ``_listener`` only ever ``pop``s it with a default and
  ``set_result``s the RAW message on a future that is not done -- so a caller
  that gave up (F-883) leaves nothing for a late answer to trip on;
* the reply is PARSED in the caller's ``send`` (``cdp_obj.send(...)``), not in
  the listener -- so a reply a generated parser cannot read (F-902) is that one
  command's exception and the listener never sees it;
* Chrome 153's ``Network.Cookie`` has no ``sameParty`` and neither has
  nodriver's ``Cookie``, so the retired-field tolerance has no target.

If a nodriver release moves any of these, a node here fails FIRST, and the
protection that left with ``cdp_transport`` has to come back deliberately. The
last two halves are also pinned where they are visible: ``tests/test_wire_semantics.py``
(a cancelled request stops the body, real wire frames) and
``tests/test_e2e_execute_script_async.py`` / ``tests/test_e2e_cookie_listener.py``
(real Chrome).
"""

import asyncio
import contextlib
import gc
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from nodriver import cdp
from nodriver.core import connection as nodriver_connection
from nodriver.core.connection import Connection

from stealth_chrome_devtools_mcp import observability

pytestmark = pytest.mark.asyncio

_EVALUATE_RESULT = {"result": {"type": "number", "value": 2}}

#: The raw JSON of ONE cookie, copied verbatim out of a
#: ``Network.getAllCookies`` reply captured over a RAW WEBSOCKET to
#: Chrome/153.0.8010.50 on 2026-09-21 (Windows 11 26200), with nodriver nowhere
#: in the path. Name and value are the capture fixture's own synthetic ones.
#:
#: It is written out here rather than imported from the product for
#: ``fakes.TARGET_SWAPPED_ERROR``'s reason: a pin about what CHROME sends must
#: measure against Chrome's bytes, or it compares our idea of the wire to
#: itself. ``sameParty`` is absent because Chrome 153 does not send it -- that
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

#: A generated parser requires ``sourcePort``; Chrome 153 still sends it, so
#: dropping it is a reply the parser genuinely cannot read.
_UNREADABLE_COOKIE = {k: v for k, v in CHROME_153_COOKIE.items() if k != "sourcePort"}

#: Generous for a delivery that is a few local function calls away, and short
#: enough that a REGRESSION reads as a failed node rather than a stalled lane --
#: the defects this file is about make replies never arrive at all.
_BUDGET = 5.0


class _Socket:
    """The two methods of a websocket that ``Connection`` calls, and nothing else."""

    close_code = None

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        return await self.inbox.get()

    def answer(self, request_id: int, result: dict) -> None:
        self.inbox.put_nowait(json.dumps({"id": request_id, "result": result}))

    def fail(self, request_id: int, message: str) -> None:
        error = {"code": -32000, "message": message}
        self.inbox.put_nowait(json.dumps({"id": request_id, "error": error}))


@contextlib.asynccontextmanager
async def _wire() -> AsyncIterator[tuple[Connection, _Socket]]:
    """A real attached ``Connection`` whose real ``_listener`` reads a fake socket."""
    socket = _Socket()
    connection = Connection()
    connection.socket = socket
    connection.session_id = "session"
    connection.target = SimpleNamespace(target_id="target")
    connection._listener_task = asyncio.ensure_future(connection._listener())  # noqa: SLF001  PERMANENT(the real listener over a fake websocket is the thing under test)
    try:
        yield connection, socket
    finally:
        connection._listener_task.cancel()  # noqa: SLF001  PERMANENT(end the real listener)
        with contextlib.suppress(asyncio.CancelledError):
            await connection._listener_task  # noqa: SLF001  PERMANENT(end the real listener)


def _listener_alive(connection: Connection) -> bool:
    return not connection._listener_task.done()  # noqa: SLF001  PERMANENT(is the listener alive is the question every node asks)


def _registered(connection: Connection) -> int:
    return len(connection._mapper)  # noqa: SLF001  PERMANENT(what send still has registered is the F-883 witness)


async def _until(predicate: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), _BUDGET)


async def _evaluate(connection: Connection, socket: _Socket) -> tuple:
    """One full command: send it, answer it, return the parsed reply."""
    sent = len(socket.sent)
    pending = asyncio.ensure_future(
        connection.send(cdp.runtime.evaluate(expression="1 + 1"))
    )
    await _until(lambda: len(socket.sent) == sent + 1)
    socket.answer(socket.sent[-1]["id"], _EVALUATE_RESULT)
    return await asyncio.wait_for(pending, _BUDGET)


async def test_a_command_is_answered_through_the_real_connection():
    """The happy path every CDP command in the product takes: the PARSED value."""
    async with _wire() as (connection, socket):
        remote, exception_details = await _evaluate(connection, socket)
        assert remote.value == 2
        assert exception_details is None
        assert _registered(connection) == 0


async def test_a_cancelled_caller_leaves_the_listener_alive():
    """F-883 B1 / F-788 / F-794: cancel the caller, then let Chrome answer."""
    async with _wire() as (connection, socket):
        task = asyncio.ensure_future(
            connection.send(cdp.runtime.evaluate(expression="1 + 1"))
        )
        await _until(lambda: len(socket.sent) == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _registered(connection) == 0, "send must not keep a registration"

        socket.answer(socket.sent[0]["id"], _EVALUATE_RESULT)  # the late answer
        await asyncio.sleep(0.05)
        assert _listener_alive(connection), "the late reply killed the listener"

        remote, _ = await _evaluate(connection, socket)
        assert remote.value == 2, "a later command on the same connection must work"


async def test_the_caller_stops_at_the_send_it_was_cancelled_on():
    """The other half of the contract: the ``CancelledError`` arrives AT the
    await, so the next line never runs. A cancelled ``navigate`` must not
    navigate."""
    reached_next_line = False

    async with _wire() as (connection, socket):

        async def body() -> None:
            nonlocal reached_next_line
            await connection.send(cdp.runtime.evaluate(expression="1 + 1"))
            reached_next_line = True

        task = asyncio.ensure_future(body())
        await _until(lambda: len(socket.sent) == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        socket.answer(socket.sent[0]["id"], _EVALUATE_RESULT)
        await asyncio.sleep(0.05)
        assert not reached_next_line, "a cancelled body must stop, not run on"


async def test_a_timed_out_caller_leaves_the_listener_alive_too():
    """The product's own bound (``_with_cdp_timeout``) reaches the same path."""
    async with _wire() as (connection, socket):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                connection.send(cdp.runtime.evaluate(expression="1 + 1")), 0.1
            )
        socket.answer(socket.sent[0]["id"], _EVALUATE_RESULT)
        await asyncio.sleep(0.05)
        assert _listener_alive(connection)
        assert (await _evaluate(connection, socket))[0].value == 2


async def test_a_reply_for_an_id_nobody_is_waiting_on_is_ignored():
    """The listener ``pop``s with a default (0.47 popped bare and a stray id was
    a ``KeyError`` that ended it)."""
    async with _wire() as (connection, socket):
        socket.answer(9999, _EVALUATE_RESULT)
        await asyncio.sleep(0.05)
        assert _listener_alive(connection)
        assert (await _evaluate(connection, socket))[0].value == 2


async def test_a_protocol_error_still_reaches_the_caller():
    """An error reply is raised in the caller's ``send``; it must not read as a hang."""
    async with _wire() as (connection, socket):
        pending = asyncio.ensure_future(
            connection.send(cdp.runtime.evaluate(expression="1 + 1"))
        )
        await _until(lambda: len(socket.sent) == 1)
        socket.fail(socket.sent[0]["id"], "nope")
        with pytest.raises(nodriver_connection.ProtocolException, match="nope"):
            await asyncio.wait_for(pending, _BUDGET)
        assert _listener_alive(connection)


async def test_an_abandoned_reply_that_fails_is_never_logged_as_unretrieved():
    """F-941 (Sentry STEALTH-CHROME-DEVTOOLS-MCP-8W). A caller abandons a
    ``Runtime.evaluate`` (the CDP budget cancelled it), the page then navigates,
    and Chrome answers with an error. Nobody is left to read it, so if the reply
    were kept as a failed future asyncio would log "exception was never
    retrieved" at ERROR -- straight to Sentry. The loop's exception handler is
    the witness, because that is the exact path the 8W events took.
    """
    loop = asyncio.get_running_loop()
    reported: list[str] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda _loop, context: reported.append(context["message"])
    )
    try:
        async with _wire() as (connection, socket):
            task = asyncio.ensure_future(
                connection.send(cdp.runtime.evaluate(expression="1 + 1"))
            )
            await _until(lambda: len(socket.sent) == 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            socket.fail(socket.sent[0]["id"], "Inspected target navigated or closed")
            await asyncio.sleep(0.05)
            del task
            gc.collect()
            await asyncio.sleep(0)
            assert _listener_alive(connection)
    finally:
        loop.set_exception_handler(previous)

    assert not [m for m in reported if "never retrieved" in m], reported


async def test_a_chrome_153_cookie_parses_without_sameparty():
    """nodriver 0.47 read ``json['sameParty']`` unconditionally, so every cookie
    reply from Chrome 153 raised. 0.50's ``Cookie`` has no such field."""
    cookie = cdp.network.Cookie.from_json(CHROME_153_COOKIE)
    assert cookie.name == "f902_probe"
    assert cookie.value == "synthetic-value"
    assert cookie.source_port == 36829
    assert not hasattr(cookie, "same_party"), "the retired field is back"
    assert "sameParty" not in cookie.to_json()


async def test_a_cookie_reply_resolves_through_a_real_connection():
    async with _wire() as (connection, socket):
        pending = asyncio.ensure_future(connection.send(cdp.storage.get_cookies()))
        await _until(lambda: len(socket.sent) == 1)
        socket.answer(socket.sent[0]["id"], {"cookies": [CHROME_153_COOKIE]})
        cookies = await asyncio.wait_for(pending, _BUDGET)
        assert [c.name for c in cookies] == ["f902_probe"]


async def test_an_unparseable_reply_fails_its_own_call_and_not_the_listener():
    """F-902. The reply is parsed in the CALLER's ``send``, so a field a
    generated parser requires and Chrome stopped sending is that one command's
    exception, and every later command on the connection still resolves (the
    0.47 listener died here and every later call hung to its timeout)."""
    async with _wire() as (connection, socket):
        pending = asyncio.ensure_future(connection.send(cdp.storage.get_cookies()))
        await _until(lambda: len(socket.sent) == 1)
        socket.answer(socket.sent[0]["id"], {"cookies": [_UNREADABLE_COOKIE]})
        with pytest.raises(KeyError):
            await asyncio.wait_for(pending, _BUDGET)

        assert _listener_alive(connection)
        assert (await _evaluate(connection, socket))[0].value == 2


async def test_an_unparseable_reply_names_a_field_and_nothing_from_the_reply(caplog):
    """PII. The text nodriver 0.47 raised here interpolated the WHOLE reply -- for
    the measured failure every cookie name and value on the page. 0.50 raises the
    generated parser's own ``KeyError``, whose only argument is the protocol field
    it looked up. Pinned exactly (``args == ("sourcePort",)``) rather than by
    absence, so a release that quotes the reply again fails here.
    """
    async with _wire() as (connection, socket):
        pending = asyncio.ensure_future(connection.send(cdp.storage.get_cookies()))
        await _until(lambda: len(socket.sent) == 1)
        with caplog.at_level(0):
            socket.answer(socket.sent[0]["id"], {"cookies": [_UNREADABLE_COOKIE]})
            with pytest.raises(KeyError) as caught:
                await asyncio.wait_for(pending, _BUDGET)
            await asyncio.sleep(0.05)

    assert caught.value.args == ("sourcePort",)
    assert caught.value.__cause__ is None
    for secret in ("f902_probe", "synthetic-value", "127.0.0.1"):
        assert secret not in repr(caught.value)
        assert secret not in caplog.text, f"{secret!r} leaked into a log line"


async def test_the_pii_argument_depends_on_sentry_not_capturing_locals():
    """The text is key-only, but the whole reply is still a LOCAL
    (``response_message``) of nodriver's ``Connection.send`` frame, which is in
    the traceback of the ``KeyError`` above. What keeps it out of an event is
    ``sentry_sdk.init``'s ``include_local_variables=False`` -- a setting another
    module owns, so flipping it fails a pin in the file that depends on it, not
    only in ``tests/test_observability.py`` where it reads as a preference.
    """
    text = Path(observability.__file__).read_text(encoding="utf-8").replace(" ", "")
    assert "include_local_variables=False" in text, (
        "a parse failure's traceback carries the whole CDP reply as a local; "
        "Sentry must not serialise frame locals"
    )


async def test_the_seams_the_old_patches_wrapped_are_gone_from_nodriver():
    """The canary for the deletion. ``cdp_transport`` wrapped
    ``Transaction.__await__``, ``Transaction.__call__`` and
    ``Cookie.from_json``'s ``sameParty``. If a nodriver release gives
    ``Transaction`` those behaviours back, the reply path is the old one again
    and the protections have to be re-derived, not assumed."""
    transaction = vars(nodriver_connection.Transaction)
    assert "__await__" not in transaction
    assert "__call__" not in transaction
    assert not hasattr(nodriver_connection, "EventTransaction")
    assert "same_party" not in cdp.network.Cookie.__dataclass_fields__
