"""A refusal of the CALLER's own input is not a Sentry issue (T-9185).

``expected_events``' ``error-convention`` drops a ``ToolError`` whose chain is
ours and budget links only. That left one leak: a refusal the caller caused,
raised while the caller's own failure was in flight -- a hook whose code raises
``NameError`` when it loads, a ``json.loads`` error on ``extraction_options`` --
carries that foreign exception as its ``__context__``/``__cause__``, so the chain
is "not all ours" and the event shipped (STEALTH-CHROME-DEVTOOLS-MCP-EC).

``tool_errors.CallerError`` is the one subclass that says "this is the caller's
input, whatever raised underneath", and ``expected_events``' ``caller-error``
class drops it by type. These tests pin both directions, with events built by the
SDK's own objects: the pure ``before_send`` on ``event_from_exception`` output,
and the whole logging path (``logger.exception`` -> ``LoggingIntegration`` ->
``before_send`` -> transport) on a real client.
"""

from __future__ import annotations

import logging
import sys

import pytest

from fakes import FakeBrowserManager, FakeTab
from stealth_chrome_devtools_mcp import observability
from stealth_chrome_devtools_mcp.embedded import tool_errors
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

sentry_sdk = pytest.importorskip("sentry_sdk")
event_from_exception = pytest.importorskip("sentry_sdk.utils").event_from_exception
LoggingIntegration = pytest.importorskip(
    "sentry_sdk.integrations.logging"
).LoggingIntegration
Transport = pytest.importorskip("sentry_sdk.transport").Transport

#: The logger fastmcp reports a failed tool call on, as ``expected_events`` names it.
TOOL_CALL_LOGGER = "fastmcp.server.server"


@pytest.fixture
def caller_error():
    """The class under test, read off the module so a missing one fails each test."""
    return tool_errors.CallerError


def _raised(factory):
    """exc_info of the exception ``factory()`` raises while a ``NameError`` is in
    flight -- the shape ``dynamic_hook_model`` gives a hook whose code does not load."""
    try:
        try:
            raise NameError("name 'undefined_name' is not defined")  # noqa: TRY301  PERMANENT(the in-flight cause IS the fixture)
        except NameError:
            raise factory()  # noqa: B904  PERMANENT(an implicit __context__ is the shape under test)
    except ToolError:
        return sys.exc_info()


def _event(exc_info):
    """The (event, hint) pair ``LoggingIntegration`` builds for a logged exception."""
    event, hint = event_from_exception(
        exc_info, mechanism={"type": "logging", "handled": True}
    )
    event["logger"] = TOOL_CALL_LOGGER
    hint["log_record"] = logging.LogRecord(
        TOOL_CALL_LOGGER,
        logging.ERROR,
        __file__,
        1,
        "Error calling tool 'x'",
        (),
        exc_info,
    )
    return event, hint


class TestTheFilter:
    def test_a_caller_error_over_a_foreign_cause_is_dropped(self, caller_error):
        event, hint = _event(_raised(lambda: caller_error("hook code does not load")))

        assert observability._scrub_event(event, hint) is None

    def test_the_serialized_payload_alone_is_enough(self, caller_error):
        event, _ = _event(_raised(lambda: caller_error("hook code does not load")))

        assert observability._scrub_event(event, {}) is None

    def test_a_bare_caller_error_is_dropped(self, caller_error):
        try:
            raise caller_error("Invalid index value: abc. Must be a number.")  # noqa: TRY301  PERMANENT(a raised exception is the fixture)
        except ToolError:
            event, hint = _event(sys.exc_info())

        assert observability._scrub_event(event, hint) is None

    def test_a_product_tool_error_over_the_same_foreign_cause_still_ships(self):
        """The mirror of the drop: ours, same chain, but not declared the caller's."""
        event, hint = _event(_raised(lambda: ToolError("Failed to execute script")))

        shipped = observability._scrub_event(event, hint)

        assert shipped is not None
        assert shipped["exception"]["values"][-1]["type"] == "ToolError"

    def test_the_product_error_is_not_dropped_on_the_payload_path_either(self):
        event, _ = _event(_raised(lambda: ToolError("Failed to execute script")))

        assert observability._scrub_event(event, {}) is not None

    def test_a_caller_error_is_still_a_tool_error(self, caller_error):
        """The caller gets the same error text over MCP; nothing else changes."""
        assert issubclass(caller_error, ToolError)
        assert str(caller_error("same text")) == str(ToolError("same text"))

    def test_a_lookalike_outside_our_package_is_not_dropped(self):
        """Name AND module: a third party's ``CallerError`` is not ours."""
        lookalike = type("CallerError", (Exception,), {"__module__": "somelib.errors"})
        try:
            raise lookalike("boom")  # noqa: TRY301  PERMANENT(a raised exception is the fixture)
        except Exception:  # noqa: BLE001  PERMANENT(the fixture catches what it raised)
            event, hint = _event(sys.exc_info())

        assert observability._scrub_event(event, hint) is not None


class _Capture(Transport):
    """A transport that keeps each event envelope's event instead of sending it."""

    def __init__(self, sink: list[dict]) -> None:
        super().__init__()
        self._sink = sink

    def capture_envelope(self, envelope) -> None:
        self._sink.extend(
            event for item in envelope.items if (event := item.get_event()) is not None
        )


@pytest.fixture
def shipped_events():
    """A real client on the real logging path, with ``before_send`` the product's.

    The transport sees each event AFTER ``before_send``, so the list is exactly
    what would have left the machine. The client lives on an isolation scope and
    never becomes the global one (``sentry_sdk.init`` would, and its teardown
    leaves a client behind that other files' sink pins then see).
    """
    shipped: list[dict] = []
    client = sentry_sdk.Client(
        dsn="http://public@localhost/1",
        transport=_Capture(shipped),
        before_send=observability._scrub_event,
        integrations=[LoggingIntegration(event_level=logging.ERROR)],
        default_integrations=False,
        auto_enabling_integrations=False,
    )
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(client)
        yield shipped
        client.flush()
    client.close()


class TestTheWholeLoggingPath:
    """What fastmcp does on a failed call: ``logger.exception("Error calling tool")``."""

    @staticmethod
    def _log_failure(exc_factory):
        logger = logging.getLogger(TOOL_CALL_LOGGER)
        try:
            try:
                raise NameError("name 'undefined_name' is not defined")  # noqa: TRY301  PERMANENT(the in-flight cause IS the fixture)
            except NameError:
                raise exc_factory()  # noqa: B904  PERMANENT(an implicit __context__ is the shape under test)
        except ToolError:
            logger.exception("Error calling tool 'create_dynamic_hook'")
        sentry_sdk.get_client().flush()

    def test_a_caller_error_produces_no_sentry_event(
        self, caller_error, shipped_events
    ):
        self._log_failure(lambda: caller_error("hook code does not load"))

        assert shipped_events == []

    def test_a_product_tool_error_is_delivered_as_an_error(self, shipped_events):
        self._log_failure(lambda: ToolError("Failed to execute script"))

        assert [e["level"] for e in shipped_events] == ["error"]
        assert shipped_events[0]["exception"]["values"][-1]["type"] == "ToolError"


class TestTheConvertedSites:
    """Each refusal below is the caller's own input, raised with a foreign cause in
    flight or as the one place its kind of input becomes an error."""

    def test_a_script_that_throws_is_the_callers(self, caller_error):
        value = type(
            "ExceptionDetails",
            (),
            {
                "exception_id": 1,
                "text": "Uncaught",
                "exception": type(
                    "Remote",
                    (),
                    {"description": "SyntaxError: Illegal return statement"},
                )(),
            },
        )()
        with pytest.raises(caller_error, match="Illegal return statement"):
            tool_errors._require_js_value(value)

    def test_an_unreachable_navigation_target_is_the_callers(self, caller_error):
        with pytest.raises(caller_error, match=r"Navigation to https://nope\.invalid/"):
            tool_errors._require_navigation_ok(
                "https://nope.invalid/",
                {
                    "url": "chrome-error://chromewebdata/",
                    "error_text": "ERR_NAME_NOT_RESOLVED",
                },
            )

    async def test_an_index_that_is_not_a_number_is_the_callers(
        self, caller_error, call_tool, patched_server
    ):
        srv = patched_server(browser_manager=FakeBrowserManager(tabs={"i1": FakeTab()}))
        with pytest.raises(caller_error, match=r"Invalid index value: abc"):
            await call_tool(
                srv, "select_option", instance_id="i1", selector="#s", index="abc"
            )

    async def test_extraction_options_that_are_not_json_are_the_callers(
        self, caller_error, call_tool, patched_server
    ):
        srv = patched_server(browser_manager=FakeBrowserManager(tabs={"i1": FakeTab()}))
        with pytest.raises(caller_error, match=r"Invalid JSON in extraction_options"):
            await call_tool(
                srv,
                "clone_element_complete",
                instance_id="i1",
                selector="#s",
                extraction_options="{not json",
            )

    async def test_file_extraction_options_that_are_not_json_are_the_callers(
        self, caller_error, call_tool, patched_server
    ):
        srv = patched_server(browser_manager=FakeBrowserManager(tabs={"i1": FakeTab()}))
        with pytest.raises(caller_error, match=r"Invalid extraction_options JSON"):
            await call_tool(
                srv,
                "clone_element_to_file",
                instance_id="i1",
                selector="#s",
                extraction_options="{not json",
            )

    def test_a_cdp_param_of_the_wrong_shape_is_the_callers(self, caller_error):
        from nodriver import cdp

        from stealth_chrome_devtools_mcp.embedded import cdp_params

        with pytest.raises(caller_error, match="expects"):
            cdp_params.typed(
                cdp.browser.set_window_bounds, {"window_id": 7, "bounds": "big"}
            )

    def test_a_cdp_call_with_unknown_params_is_the_callers(self, caller_error):
        from nodriver import cdp

        from stealth_chrome_devtools_mcp.embedded import cdp_function_executor

        with pytest.raises(caller_error, match="valid params"):
            cdp_function_executor.build_cdp_call(
                cdp.page.navigate, {"no_such_param": 1}
            )

    def test_a_path_that_cannot_be_resolved_is_the_callers(self, caller_error):
        from stealth_chrome_devtools_mcp.embedded import path_confinement

        with pytest.raises(caller_error):
            path_confinement._real("bad\0path")
