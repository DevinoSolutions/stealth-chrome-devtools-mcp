"""F-887 — the five named classes of event this product is EXPECTED to produce.

Live triage of the project's Sentry on 2026-09-18 (release 2.1.8, last 7 days)
found the issue list was almost entirely the product working as designed, in six
shapes that the step-0 filter ``observability._is_expected_tool_failure`` was
written for but could not see:

===========================================  ======  ==================
shape                                        events  class
===========================================  ======  ==================
``ClientDisconnect`` under ``request.body``    6 200  client-disconnect
``Received exception from stream: `` (bare)    6 500  client-disconnect
``ToolError`` <- ``TimeoutError``               140+  error-convention
``ValidationError`` for ``spawn_browser``        466  caller-input
``ConnectionResetError`` [WinError 10054]        231  proactor-teardown
``ConnectionRefusedError`` (nodriver task)       151  nodriver-dead-browser
===========================================  ======  ==================

Every one of them had already been answered: the disconnected client is gone,
the budget's ``ToolError`` reached the caller, FastMCP answered the caller with
the validation message, and the two teardown races are a library's own.

This file pins BOTH directions for each class, because a filter that only proves
it drops things is a filter nobody can trust with a crash. The negatives are the
point of the exercise: a ``ToolError`` over an ``AttributeError``, a bare
``TimeoutError``, a bare ``CancelledError``, nodriver's ``ProtocolException``,
F-883's ``InvalidStateError`` (fixed in 2.1.9 — if it comes back we WANT to see
it), a ``ConnectionResetError`` from anywhere but the proactor callback, the
``Received exception from stream:`` sibling that carries a real tail, and
``capture_lifecycle``'s proxy messages.

Events are built by the SDK itself — ``event_from_exception`` for the exception
shape and the real ``LoggingIntegration`` handler driven against a live
``sentry_sdk.Client`` for the ``logger``/``logentry`` shape. A hand-shaped double
would encode whatever payload we assumed, and the payload shape IS what the
serialized path decides on. The one thing asserted about the fixtures themselves
is that they really carry what the rule reads (:class:`TestTheFixturesAreTheSdks`).
"""

from __future__ import annotations

import contextlib
import logging
import sys

import pytest

from stealth_chrome_devtools_mcp import observability
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

sentry_sdk = pytest.importorskip("sentry_sdk")
event_from_exception = pytest.importorskip("sentry_sdk.utils").event_from_exception
EventHandler = pytest.importorskip("sentry_sdk.integrations.logging").EventHandler

# --- the real production loggers, verbatim from the triage ------------------
STREAMABLE_HTTP_LOGGER = "mcp.server.streamable_http"
LOWLEVEL_LOGGER = "mcp.server.lowlevel.server"
TOOL_MANAGER_LOGGER = "FastMCP.fastmcp.tools.tool_manager"
ASYNCIO_LOGGER = "asyncio"

#: `mcp/server/lowlevel/server.py:707` (mcp 1.27.1):
#: ``logger.error(f"Received exception from stream: {message}")``. A
#: ``ClientDisconnect`` formats to the empty string, so the tail is empty —
#: which is the WHOLE match, never a prefix: the sibling issue whose tail reads
#: "Received response with an unknown request ID" is a real protocol fault.
STREAM_GONE = "Received exception from stream: "
STREAM_FAULT = (
    "Received exception from stream: Received response with an unknown "
    "request ID: 41. Method not found"
)

#: CPython 3.13 `asyncio/proactor_events.py:154`: `_call_connection_lost`'s
#: ``finally`` calls ``self._sock.shutdown(socket.SHUT_RDWR)`` — the line its own
#: XXX comment calls a cure for ERROR_NETNAME_DELETED. When the peer already
#: reset, that shutdown raises out of a ``call_soon`` callback and the loop's
#: exception handler logs it on ``asyncio``.
PROACTOR_MESSAGE = (
    "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)"
)

#: nodriver's own unawaited background target refresh, after its Chrome died.
NODRIVER_TASK_MESSAGE = (
    "Task exception was never retrieved\n"
    "future: <Task finished name='Task-83' "
    "coro=<Browser.update_targets() done, defined at "
    r"C:\src\.venv\Lib\site-packages\nodriver\core\browser.py:561> "
    "exception=ConnectionRefusedError(1225, ...)>"
)

#: The same asyncio complaint about a task of OURS. It must still ship: the
#: message is the only thing that tells the two apart.
OUR_TASK_MESSAGE = (
    "Task exception was never retrieved\n"
    "future: <Task finished name='Task-12' "
    "coro=<BrowserManager._reap_idle() done, defined at "
    r"C:\src\stealth_chrome_devtools_mcp\embedded\browser_manager.py:88> "
    "exception=ConnectionRefusedError(1225, ...)>"
)


# ---------------------------------------------------------------------------
# Fixtures — the SDK's own event shapes
# ---------------------------------------------------------------------------
def _raise(exc: BaseException):
    """Raise and catch ``exc`` so it carries a real traceback and chain."""
    try:
        raise exc  # noqa: TRY301  PERMANENT(raising HERE is the fixture: an exception built but never raised has no traceback and no chain, which is precisely what the SDK serializes)
    except BaseException:  # noqa: BLE001  PERMANENT(the fixture must catch whatever it was handed)
        return sys.exc_info()


class _NoTransport(sentry_sdk.transport.Transport):
    """A transport that cannot reach the network. Belt and braces: the
    ``before_send`` below already returns ``None``, so nothing is queued."""

    def capture_envelope(self, envelope) -> None:
        return None


@contextlib.contextmanager
def _sdk_client():
    """A live client whose ``before_send`` hands back the event it BUILT.

    ``before_send`` runs after the SDK has serialized the event
    (``client.py`` :880 then :896, sdk 2.64.0), so what lands in ``seen`` is
    exactly the plain-JSON shape production's ``before_send`` is handed. The
    client is scoped to this block, so nothing leaks into the rest of the suite.
    """
    seen = []
    client = sentry_sdk.Client(
        dsn="https://public@example.invalid/1",
        before_send=lambda event, hint: seen.append((event, hint)) or None,
        transport=_NoTransport(),
        default_integrations=False,
    )
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(client)
        yield seen


def logged(logger_name: str, message: str, exc=None, pathname="/app/x.py"):
    """The (event, hint) pair the REAL ``LoggingIntegration`` builds for a record.

    ``EventHandler`` is the class ``LoggingIntegration(event_level=ERROR)``
    installs, so driving it directly produces the ``logger`` key, the
    ``logentry`` dict and the ``hint["log_record"]`` exactly as production gets
    them — including the absence of ``exception`` for a message-only record.
    """
    exc_info = _raise(exc) if exc is not None else None
    record = logging.LogRecord(
        name=logger_name,
        level=logging.ERROR,
        pathname=pathname,
        lineno=1,
        msg=message,
        args=(),
        exc_info=exc_info,
    )
    with _sdk_client() as seen:
        EventHandler(level=logging.ERROR).emit(record)
    assert seen, f"the SDK built no event for {logger_name}"
    return seen[-1]


def payload_only(pair):
    """The same event with the hint stripped — the serialized-only path."""
    return pair[0], {}


def cdp_budget_chain():
    """``ToolError`` <- ``TimeoutError`` <- ``CancelledError``, from the real wrapper.

    Produced by ``tool_runtime._with_cdp_timeout``'s own ``asyncio.wait_for``,
    not by hand: the ``CancelledError`` under the ``TimeoutError`` is
    ``wait_for``'s mechanism and nothing else puts it there.
    """
    import asyncio

    from stealth_chrome_devtools_mcp.embedded import tool_runtime

    async def never() -> None:
        await asyncio.sleep(30)

    async def run():
        try:
            await tool_runtime._with_cdp_timeout(
                never(), timeout=0.01, instance_id="i7"
            )
        except BaseException:
            return sys.exc_info()
        return None

    return asyncio.run(run())


def navigate_budget_chain():
    """``raise ToolError(...) from error`` over ``wait_for``'s ``TimeoutError``.

    ``browser_manager.navigate``'s shape (``browser_manager.py``:1224), which
    differs from the CDP wrapper's only in using ``from`` rather than implicit
    context — the chain Sentry serializes is the same three links.
    """
    import asyncio

    async def never() -> None:
        await asyncio.sleep(30)

    async def run():
        try:
            try:
                await asyncio.wait_for(never(), timeout=0.01)
            except TimeoutError as error:
                raise ToolError(
                    "Navigation to https://example.invalid/ timed out after 30000ms "
                    "(accepted, committed)"
                ) from error
        except BaseException:
            return sys.exc_info()
        return None

    return asyncio.run(run())


def tool_manager_event(exc_info, logger_name=TOOL_MANAGER_LOGGER, tool="navigate"):
    """An exception event on a tool logger, the way ``_emit`` assembles one."""
    event, hint = event_from_exception(
        exc_info, mechanism={"type": "logging", "handled": True}
    )
    event["logger"] = logger_name
    event["logentry"] = {
        "message": f"Error calling tool '{tool}'",
        "formatted": f"Error calling tool '{tool}'",
        "params": [],
    }
    hint["log_record"] = logging.LogRecord(
        name=logger_name,
        level=logging.ERROR,
        pathname="/app/tool_manager.py",
        lineno=224,
        msg=f"Error calling tool '{tool}'",
        args=(),
        exc_info=exc_info,
    )
    return event, hint


def client_disconnect():
    return pytest.importorskip("starlette.requests").ClientDisconnect


def validation_error_info():
    """The exc_info FastMCP's ``type_adapter.validate_python`` raises for
    ``spawn_browser(window_width='1440')`` — the real pydantic class."""
    pydantic = pytest.importorskip("pydantic")

    class Spawn(pydantic.BaseModel):
        viewport_width: int = 1280
        viewport_height: int = 720

        model_config = pydantic.ConfigDict(extra="forbid")

    try:
        Spawn.model_validate({"window_width": "1440", "window_height": "900"})
    except Exception:
        return sys.exc_info()
    pytest.fail("expected a pydantic ValidationError")
    return None


def dropped(pair) -> bool:
    return observability._scrub_event(*pair) is None


# ===========================================================================
# The fixtures really are the SDK's — otherwise every test below is vacuous
# ===========================================================================
class TestTheFixturesAreTheSdks:
    def test_a_logging_record_becomes_logger_plus_logentry(self):
        event, hint = logged(ASYNCIO_LOGGER, PROACTOR_MESSAGE, ConnectionResetError())

        assert event["logger"] == ASYNCIO_LOGGER
        assert event["logentry"]["formatted"] == PROACTOR_MESSAGE
        assert event["exception"]["values"][-1]["type"] == "ConnectionResetError"
        assert "log_record" in hint

    def test_a_message_only_record_carries_no_exception_values(self):
        event, hint = logged(LOWLEVEL_LOGGER, STREAM_GONE)

        assert "exception" not in event
        assert event["logentry"]["formatted"] == STREAM_GONE
        assert "exc_info" not in hint

    def test_the_budget_chain_really_has_all_three_links(self):
        event, _ = tool_manager_event(cdp_budget_chain())

        assert [v["type"] for v in event["exception"]["values"]] == [
            "CancelledError",
            "TimeoutError",
            "ToolError",
        ]
        modules = {v["type"]: v.get("module") for v in event["exception"]["values"]}
        assert modules["CancelledError"] == "asyncio.exceptions"
        assert modules["TimeoutError"] is None  # the SDK omits `builtins`
        assert modules["ToolError"].startswith("stealth_chrome_devtools_mcp")

    def test_a_client_disconnect_formats_to_the_empty_string(self):
        """Which is why the stream message's tail is empty. It is not a typo."""
        assert str(client_disconnect()()) == ""


# ===========================================================================
# (a) error-convention: our ToolError over the budget links it was raised on
# ===========================================================================
class TestTheBudgetIsTheProductAnswering:
    def test_a_cdp_timeout_chain_is_dropped(self):
        assert dropped(tool_manager_event(cdp_budget_chain()))

    def test_a_cdp_timeout_chain_is_dropped_from_the_payload_alone(self):
        assert dropped(payload_only(tool_manager_event(cdp_budget_chain())))

    def test_a_navigation_timeout_chain_is_dropped(self):
        assert dropped(tool_manager_event(navigate_budget_chain()))

    def test_a_navigation_timeout_chain_is_dropped_from_the_payload_alone(self):
        assert dropped(payload_only(tool_manager_event(navigate_budget_chain())))

    def test_a_bare_timeout_with_no_tool_error_over_it_still_ships(self):
        """A budget link is tolerated BESIDE ours, never on its own: a
        ``TimeoutError`` nobody converted is a place the convention is missing."""
        assert not dropped(tool_manager_event(_raise(TimeoutError())))
        assert not dropped(payload_only(tool_manager_event(_raise(TimeoutError()))))

    def test_a_bare_cancellation_still_ships(self):
        import asyncio

        info = _raise(asyncio.CancelledError())
        assert not dropped(tool_manager_event(info))
        assert not dropped(payload_only(tool_manager_event(info)))

    def test_a_tool_error_over_an_attribute_error_still_ships(self):
        """The historical ``navigate`` bug. Widening to the budget links must
        not have widened to anything else."""

        def chain():
            try:
                try:
                    raise AttributeError(  # noqa: TRY301  PERMANENT(the real chain IS the fixture; see _raise)
                        "'NoneType' object has no attribute 'send'"
                    )
                except AttributeError as cause:
                    raise ToolError("Navigation failed") from cause
            except ToolError:
                return sys.exc_info()

        assert not dropped(tool_manager_event(chain()))
        assert not dropped(payload_only(tool_manager_event(chain())))

    def test_a_failed_spawn_over_a_plain_exception_still_ships(self):
        """``ToolError: Failed to spawn browser: ...`` <- nodriver's plain
        ``Exception("Failed to connect to browser")``. Not a budget link, so
        the chain is not the convention answering — it is a spawn that broke."""

        def chain():
            try:
                try:
                    raise Exception(  # noqa: TRY002,TRY301  PERMANENT(nodriver really raises a bare Exception here; the fixture must be that class, not a tidier one)
                        "Failed to connect to browser"
                    )
                except Exception as cause:
                    raise ToolError("Failed to spawn browser: ...") from cause
            except ToolError:
                return sys.exc_info()

        assert not dropped(tool_manager_event(chain()))
        assert not dropped(payload_only(tool_manager_event(chain())))


# ===========================================================================
# (b) client-disconnect: the client went away mid-request
# ===========================================================================
class TestTheClientWentAway:
    def test_a_client_disconnect_under_a_post_is_dropped(self):
        pair = logged(
            STREAMABLE_HTTP_LOGGER, "Error handling POST request", client_disconnect()()
        )

        assert dropped(pair)

    def test_it_is_dropped_from_the_payload_alone(self):
        pair = logged(
            STREAMABLE_HTTP_LOGGER, "Error handling POST request", client_disconnect()()
        )

        assert dropped(payload_only(pair))

    def test_the_session_loops_message_only_form_is_dropped(self):
        """No exception values at all: the SDK formatted ``ClientDisconnect()``
        into the f-string and it is empty. Logger + exact message is all there is."""
        assert dropped(logged(LOWLEVEL_LOGGER, STREAM_GONE))

    def test_the_same_message_with_a_real_tail_still_ships(self):
        """THE reason the match is equality and not a prefix."""
        assert not dropped(logged(LOWLEVEL_LOGGER, STREAM_FAULT))

    def test_the_empty_tail_on_another_logger_still_ships(self):
        assert not dropped(logged("stealth.backend", STREAM_GONE))

    def test_a_disconnect_wrapping_a_real_bug_still_ships(self):
        def chain():
            try:
                try:
                    raise AttributeError(  # noqa: TRY301  PERMANENT(the real chain IS the fixture; see _raise)
                        "'NoneType' object has no attribute 'scope'"
                    )
                except AttributeError as cause:
                    disconnect = client_disconnect()()
                    raise disconnect from cause
            except BaseException:  # noqa: BLE001  PERMANENT(the fixture catches what it raised)
                return sys.exc_info()

        assert not dropped(tool_manager_event(chain(), STREAMABLE_HTTP_LOGGER))


# ===========================================================================
# (c) proactor-teardown: CPython's own Windows shutdown race
# ===========================================================================
class TestTheProactorTeardownRace:
    def test_the_reset_in_the_connection_lost_callback_is_dropped(self):
        pair = logged(
            ASYNCIO_LOGGER,
            PROACTOR_MESSAGE,
            ConnectionResetError(10054, "forcibly closed by the remote host"),
        )

        assert dropped(pair)
        assert dropped(payload_only(pair))

    def test_a_reset_from_anywhere_else_still_ships(self):
        """The callback is the whole claim. A reset our own code saw is a fact
        about this product's sockets and is not CPython's teardown race."""
        pair = logged(
            ASYNCIO_LOGGER,
            "Exception in callback BrowserManager._on_close()",
            ConnectionResetError(10054, "forcibly closed by the remote host"),
        )

        assert not dropped(pair)

    def test_the_callback_on_another_logger_still_ships(self):
        pair = logged(
            "stealth.backend",
            PROACTOR_MESSAGE,
            ConnectionResetError(10054, "forcibly closed"),
        )

        assert not dropped(pair)

    def test_a_different_exception_in_that_callback_still_ships(self):
        pair = logged(ASYNCIO_LOGGER, PROACTOR_MESSAGE, OSError("bad file descriptor"))

        assert not dropped(pair)


# ===========================================================================
# (d) nodriver-dead-browser: a library's unawaited task after its Chrome died
# ===========================================================================
class TestNodriversOrphanedRefresh:
    def test_the_refused_connection_from_nodrivers_task_is_dropped(self):
        pair = logged(
            ASYNCIO_LOGGER,
            NODRIVER_TASK_MESSAGE,
            ConnectionRefusedError(1225, "The remote computer refused"),
        )

        assert dropped(pair)
        assert dropped(payload_only(pair))

    def test_an_unawaited_task_of_ours_still_ships(self):
        """Same logger, same complaint, same exception type — and it names OUR
        module, so it is our bug and it must reach us."""
        pair = logged(
            ASYNCIO_LOGGER,
            OUR_TASK_MESSAGE,
            ConnectionRefusedError(1225, "The remote computer refused"),
        )

        assert not dropped(pair)

    def test_f883s_invalid_state_error_still_ships(self):
        """Fixed in 2.1.9 (`cdp_transport`). If nodriver's listener starts dying
        again we want the event, not a filter that learned to expect it."""

        def chain():
            try:
                try:
                    raise StopIteration  # noqa: TRY301  PERMANENT(nodriver's `_listener` really reaches InvalidStateError out of a StopIteration; the chain IS the fixture)
                except StopIteration as cause:
                    import asyncio

                    raise asyncio.InvalidStateError("invalid state") from cause
            except BaseException:  # noqa: BLE001  PERMANENT(the fixture catches what it raised)
                return sys.exc_info()

        pair = logged(ASYNCIO_LOGGER, NODRIVER_TASK_MESSAGE)
        event, hint = pair
        rebuilt, rebuilt_hint = event_from_exception(
            chain(), mechanism={"type": "logging", "handled": True}
        )
        rebuilt["logger"] = event["logger"]
        rebuilt["logentry"] = event["logentry"]

        assert not dropped((rebuilt, rebuilt_hint))

    def test_nodrivers_protocol_exception_still_ships(self):
        protocol_exception = pytest.importorskip(
            "nodriver.core.connection"
        ).ProtocolException
        pair = tool_manager_event(_raise(protocol_exception("-32000 stale node")))

        assert not dropped(pair)
        assert not dropped(payload_only(pair))


# ===========================================================================
# (e) caller-input: the caller sent a parameter the tool does not have
# ===========================================================================
class TestCallerInput:
    def test_a_validation_error_from_the_tool_manager_is_dropped(self):
        pair = tool_manager_event(validation_error_info(), tool="spawn_browser")

        assert dropped(pair)
        assert dropped(payload_only(pair))

    def test_a_validation_error_from_anywhere_else_still_ships(self):
        """Our own code validating its own model is not a caller's typo."""
        pair = tool_manager_event(validation_error_info(), "stealth.backend")

        assert not dropped(pair)

    def test_a_validation_error_wrapping_a_real_bug_still_ships(self):
        def chain():
            try:
                try:
                    raise TypeError(  # noqa: TRY301  PERMANENT(the real chain IS the fixture; see _raise)
                        "expected str, got int"
                    )
                except TypeError as cause:
                    raise ToolError("bad model") from cause
            except ToolError:
                return sys.exc_info()

        assert not dropped(tool_manager_event(chain(), tool="spawn_browser"))


# ===========================================================================
# What F-827 ships deliberately must not become noise
# ===========================================================================
class TestLifecycleReportsStillShip:
    @pytest.mark.parametrize(
        "message",
        [
            "proxy: backend condemned",
            "proxy: backend healed",
            "proxy: giving up, tearing down",
            "proxy: patience extended under starvation",
        ],
    )
    def test_a_proxy_lifecycle_message_survives(self, message):
        """``capture_lifecycle`` builds a MESSAGE event with a ``proxy`` context
        and no exception. Nothing in the taxonomy may reach it."""
        event = {
            "message": message,
            "level": "warning",
            "contexts": {"proxy": {"port": 19222, "attempts": 2}},
        }

        assert observability._scrub_event(event, {}) is not None

    def test_capture_lifecycle_still_reaches_the_sdk(self, monkeypatch):
        monkeypatch.delenv("STEALTH_MCP_NO_ERROR_REPORTING", raising=False)
        sent = []
        monkeypatch.setattr(
            sentry_sdk, "capture_message", lambda m, level="warning": sent.append(m)
        )

        assert (
            observability.capture_lifecycle("proxy: backend condemned", port=1) is True
        )
        assert sent == ["proxy: backend condemned"]


# ===========================================================================
# Never raises — a rule that is unsure must ship (#55)
# ===========================================================================
class TestNeverRaises:
    @pytest.mark.parametrize(
        "malformed",
        [
            {"logger": 5},
            {"logger": ASYNCIO_LOGGER, "logentry": "not a dict"},
            {"logger": ASYNCIO_LOGGER, "logentry": {"formatted": 5}},
            {"logger": ASYNCIO_LOGGER, "logentry": {}},
            {"message": None},
            {"exception": {"values": [{"type": "ClientDisconnect"}]}},
            {"exception": {"values": [{"type": 5, "module": 5}]}},
        ],
    )
    def test_a_malformed_event_is_never_a_reason_to_raise(self, malformed):
        observability._scrub_event(malformed, {})

    def test_an_unreadable_exception_value_is_never_expected(self):
        """ "Could not be read" is not "recognised". The default is to send."""
        event = {
            "logger": ASYNCIO_LOGGER,
            "logentry": {"formatted": PROACTOR_MESSAGE},
            "exception": {"values": ["not a dict"]},
        }

        assert observability._scrub_event(event, {}) is not None


# ===========================================================================
# One home — every class is named, and reached through the one before_send
# ===========================================================================
class TestTheClassesAreNamed:
    """Each drop is attributable: the classifier answers WHICH rule recognised
    the event, so a surprise in the Sentry volume can be traced to one rule
    rather than to "the filter"."""

    def _classify(self, pair):
        from stealth_chrome_devtools_mcp import expected_events

        event, hint = pair
        exception = observability._hint_exception(hint)
        chain = observability._exception_chain(exception) if exception else None
        return expected_events.classify(
            event,
            chain=chain,
            error_base=observability._expected_error_base() if chain else None,
        )

    def test_each_shape_is_named_by_its_own_rule(self):
        from stealth_chrome_devtools_mcp import expected_events

        cases = [
            (tool_manager_event(cdp_budget_chain()), expected_events.ERROR_CONVENTION),
            (
                logged(LOWLEVEL_LOGGER, STREAM_GONE),
                expected_events.CLIENT_DISCONNECT,
            ),
            (
                logged(
                    STREAMABLE_HTTP_LOGGER,
                    "Error handling POST request",
                    client_disconnect()(),
                ),
                expected_events.CLIENT_DISCONNECT,
            ),
            (
                logged(ASYNCIO_LOGGER, PROACTOR_MESSAGE, ConnectionResetError(10054)),
                expected_events.PROACTOR_TEARDOWN,
            ),
            (
                logged(
                    ASYNCIO_LOGGER, NODRIVER_TASK_MESSAGE, ConnectionRefusedError(1225)
                ),
                expected_events.NODRIVER_DEAD_BROWSER,
            ),
            (
                tool_manager_event(validation_error_info(), tool="spawn_browser"),
                expected_events.CALLER_INPUT,
            ),
        ]
        for pair, expected in cases:
            assert self._classify(pair) == expected, expected
            assert self._classify(payload_only(pair)) == expected, expected

    def test_a_real_crash_is_named_by_nothing(self):
        assert (
            self._classify(tool_manager_event(_raise(AttributeError("boom")))) is None
        )

    def test_the_new_classes_are_reached_through_the_one_before_send(self, monkeypatch):
        """There is still exactly one hook, and the new rules live inside it."""
        monkeypatch.delenv("STEALTH_MCP_NO_ERROR_REPORTING", raising=False)
        captured = {}
        monkeypatch.setattr("sentry_sdk.init", lambda **kw: captured.update(kw))
        assert observability.sentry_init() is True

        hook = captured["before_send"]
        assert hook is observability._scrub_event
        assert hook(*logged(LOWLEVEL_LOGGER, STREAM_GONE)) is None
        assert hook(*logged(LOWLEVEL_LOGGER, STREAM_FAULT)) is not None


# ===========================================================================
# The survivors are still scrubbed
# ===========================================================================
def test_a_surviving_noise_lookalike_is_still_scrubbed():
    pair = logged(
        ASYNCIO_LOGGER,
        "Exception in callback BrowserManager._on_close() for /home/jdoe/app",
        ConnectionResetError(10054, "forcibly closed"),
    )

    out = observability._scrub_event(*pair)

    assert out is not None
    assert "jdoe" not in str(out)
    assert "server_name" not in out
