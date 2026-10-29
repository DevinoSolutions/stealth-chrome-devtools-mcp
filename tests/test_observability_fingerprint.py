"""One defect is one Sentry issue: stable fingerprints (T-10034, -ES and -EV/-EW/-ET).

Sentry groups an event by its stack frames, and by its message when there is no
stack. Measured on the project's Sentry on 2026-10-29:

* ``HungBrowserError`` (logged at ERROR by ``browser_health``, never raised, so no
  stack) embeds the instance id, uptime, process counts, free RAM, paths and step
  timings in its message. Seven freezes were seven issues (-ES, -EJ, -EF, -EE,
  -DY, -DQ, -DB), so the freeze rate could not be read off the issue list.
* one ``ToolError`` title, "Failed to click element: '#b' left the page each time
  it was resolved ...", was three issues (-EV 15 events, -EW 5, -ET 1): a
  ``ProtocolException`` underneath in two of them and an ``ElementDisconnected``
  in the third, and a different source line in each release's frames.

``observability._scrub_event`` now gives such an event a fingerprint that names
the KIND and its stable cause. These tests go through the real path -- the real
``debug_logger.log_error`` / ``logger.exception`` -> ``LoggingIntegration`` ->
``before_send`` -> transport, on a real client -- never a hand-built event dict,
which cannot fail the way the SDK's own serialization can.
"""

from __future__ import annotations

import logging

import pytest
from nodriver.core.connection import ProtocolException

from stealth_chrome_devtools_mcp import observability
from stealth_chrome_devtools_mcp.embedded.browser_health import HungBrowserError
from stealth_chrome_devtools_mcp.embedded.clone_probe import FrozenCloneError
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.element_resolution import ElementDisconnected
from stealth_chrome_devtools_mcp.embedded.tool_errors import (
    InstanceNotFoundError,
    ToolError,
)

sentry_sdk = pytest.importorskip("sentry_sdk")
LoggingIntegration = pytest.importorskip(
    "sentry_sdk.integrations.logging"
).LoggingIntegration
Transport = pytest.importorskip("sentry_sdk.transport").Transport

#: The logger fastmcp reports a failed tool call on.
TOOL_CALL_LOGGER = "fastmcp.server.server"


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
    """A real client with the product's ``before_send``, on its own scope.

    The transport sees each event AFTER ``before_send``, so the list is exactly
    what would leave the machine. ``Client`` on an isolation scope, never
    ``sentry_sdk.init``: the global client is left alone for other files.
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


def _log_hung(shipped, message: str, *cause: str) -> dict:
    """Ship ONE ``HungBrowserError`` the way ``browser_health`` does, return its event."""
    before = len(shipped)
    debug_logger.log_error("browser_health", "probe", HungBrowserError(message, *cause))
    sentry_sdk.get_client().flush()
    assert len(shipped) == before + 1
    return shipped[-1]


def _hung_message(instance_id: str, uptime: int, free_gb: str, evaluate_ms: int) -> str:
    """The shape of the -ES message, with the per-occurrence values as arguments."""
    return (
        f"{instance_id}: probe 2 failed (TimeoutError at evaluate); "
        f"uptime={uptime}s targets(page=2 other=2) tree(browser=1 renderer=2 "
        f"rss=0.30GB) machine(chromium_processes=60 free_ram={free_gb}GB); "
        f"chrome_log=C:\\Users\\jdoe\\.stealth-mcp\\logs\\chrome-fleet-{instance_id[:8]}.log; "
        f"steps(connect=677ms createTarget=7ms evaluate={evaluate_ms}ms); healing it"
    )


class TestHungBrowserError:
    def test_two_freezes_with_different_values_share_one_fingerprint(
        self, shipped_events
    ):
        first = _log_hung(
            shipped_events,
            _hung_message(
                "7e75ad01-5c3b-4a95-b86a-3c9530336361", 71999, "33.11", 15070
            ),
            "probe-heal",
            "TimeoutError at evaluate",
        )
        second = _log_hung(
            shipped_events,
            _hung_message(
                "d0df744d-6ad9-47c0-9352-21c349bf0676", 27273, "16.88", 15003
            ),
            "probe-heal",
            "TimeoutError at evaluate",
        )

        assert first["fingerprint"] == second["fingerprint"]
        assert first["fingerprint"] == [
            "hung-browser",
            "probe-heal",
            "TimeoutError at evaluate",
        ]

    def test_the_full_message_is_still_the_events_message(self, shipped_events):
        event = _log_hung(
            shipped_events,
            _hung_message(
                "7e75ad01-5c3b-4a95-b86a-3c9530336361", 71999, "33.11", 15070
            ),
            "probe-heal",
            "TimeoutError at evaluate",
        )

        text = event["exception"]["values"][-1]["value"]
        assert "7e75ad01-5c3b-4a95-b86a-3c9530336361" in text
        assert "uptime=71999s" in text and "free_ram=33.11GB" in text
        assert "evaluate=15070ms" in text
        assert event["exception"]["values"][-1]["type"] == "HungBrowserError"
        # the home segment is still scrubbed out of what travels
        assert "jdoe" not in text and "C:\\Users\\~\\.stealth-mcp" in text

    @pytest.mark.parametrize(
        "other",
        [
            ("probe-heal", "TimeoutError at closeTarget"),  # a different failed step
            ("probe-heal", "ConnectionError at evaluate"),  # a different error type
            ("starvation-ceiling", "TimeoutError at evaluate"),  # a different site
        ],
    )
    def test_a_different_step_type_or_site_is_a_different_issue(
        self, shipped_events, other
    ):
        message = _hung_message(
            "7e75ad01-5c3b-4a95-b86a-3c9530336361", 71999, "33.11", 15070
        )
        base = _log_hung(shipped_events, message, "heal TimeoutError at evaluate")
        changed = _log_hung(shipped_events, message, *other)

        assert changed["fingerprint"] != base["fingerprint"]

    def test_a_site_that_names_no_cause_still_groups_by_kind(self, shipped_events):
        """The default is ONE issue per kind, never one per message."""
        first = _log_hung(shipped_events, "i-1: something at 12s")
        second = _log_hung(shipped_events, "i-2: something at 99s")

        assert first["fingerprint"] == second["fingerprint"] == ["hung-browser"]

    def test_a_frozen_clone_is_grouped_the_same_way(self, shipped_events):
        for instance, ms in (("c-1", 40), ("c-2", 77)):
            debug_logger.log_error(
                "clone_probe",
                "probe",
                FrozenCloneError(
                    f"{instance}: clone probe failed (TimeoutError at evaluate); "
                    f"fresh_cdp(Browser.getVersion): answered in {ms}ms",
                    "probe",
                    "TimeoutError at evaluate",
                ),
            )
        sentry_sdk.get_client().flush()

        assert [e["fingerprint"] for e in shipped_events] == [
            ["frozen-clone", "probe", "TimeoutError at evaluate"]
        ] * 2


def _log_tool_error(
    shipped,
    under: Exception,
    error: ToolError,
    tool: str | None = "click_element",
) -> dict:
    """Ship a ``ToolError`` raised while *under* was in flight, as fastmcp logs it
    (``tool=None``: through a logger that names no tool)."""
    before = len(shipped)
    try:
        try:
            raise under  # noqa: TRY301  PERMANENT(the in-flight cause IS the fixture)
        except Exception:
            raise error  # noqa: B904  PERMANENT(an implicit __context__ is the shape under test)
    except ToolError:
        if tool is None:
            logging.getLogger("stealth.backend").exception("a failure")
        else:
            logging.getLogger(TOOL_CALL_LOGGER).exception("Error calling tool %r", tool)
    sentry_sdk.get_client().flush()
    assert len(shipped) == before + 1
    return shipped[-1]


def _click_gone(selector: str) -> ToolError:
    return ToolError(
        f"Failed to click element: '{selector}' left the page each time it was "
        "resolved, before any input was sent, so nothing was clicked; query it "
        "again once the page settles"
    )


NO_NODE = ProtocolException({"code": -32000, "message": "No node with given id found"})

#: The EV/EW/ET title, templated: the selector is a quoted name, so it STAYS.
CLICK_GONE_TEMPLATE = (
    "Failed to click element: '#b' left the page each time it was "
    "resolved, before any input was sent, so nothing was clicked; query "
    "it again once the page settles"
)


class TestToolError:
    def test_one_title_over_different_causes_is_one_issue(self, shipped_events):
        """-EV (a ProtocolException), -EW (an ElementDisconnected), -ET (another run)."""
        under_protocol = _log_tool_error(shipped_events, NO_NODE, _click_gone("#b"))
        under_disconnected = _log_tool_error(
            shipped_events,
            ElementDisconnected("'#b' is no longer in its document"),
            _click_gone("#b"),
        )
        another_run = _log_tool_error(shipped_events, NO_NODE, _click_gone("#b"))

        fingerprints = {
            tuple(e["fingerprint"])
            for e in (under_protocol, under_disconnected, another_run)
        }
        assert fingerprints == {
            ("tool-error", "click_element", "ToolError", CLICK_GONE_TEMPLATE)
        }
        # nothing diagnostic is lost: the full title and the cause chain ship
        values = under_disconnected["exception"]["values"]
        assert "'#b' left the page" in values[-1]["value"]
        assert [v["type"] for v in values][:2] == ["ElementDisconnected", "ToolError"]

    def test_a_quoted_name_is_a_failure_cause_not_a_value(self, shipped_events):
        """cdp_element_cloner.py:801 wraps a missing ``stylesheets`` and a missing
        ``scripts`` KeyError as ``ToolError(str(e))``: two failures, two issues."""
        stylesheets = _log_tool_error(
            shipped_events,
            KeyError("stylesheets"),
            ToolError(str(KeyError("stylesheets"))),
            tool="extract_related_files",
        )
        scripts = _log_tool_error(
            shipped_events,
            KeyError("scripts"),
            ToolError(str(KeyError("scripts"))),
            tool="extract_related_files",
        )

        assert stylesheets["fingerprint"] == [
            "tool-error",
            "extract_related_files",
            "ToolError",
            "'stylesheets'",
        ]
        assert scripts["fingerprint"] != stylesheets["fingerprint"]

    def test_the_same_tool_and_template_with_different_ids_is_one_issue(
        self, shipped_events
    ):
        def failed(pid: int, instance: str, path: str) -> ToolError:
            return ToolError(
                f"Failed to spawn browser: pid {pid} for {instance} could not open "
                f"{path} on port {pid + 1}"
            )

        first = _log_tool_error(
            shipped_events,
            RuntimeError("x"),
            failed(
                105244,
                "dd340276-97f4-4bbf-bbbf-46b14a24fa0a",
                "C:\\stealth-mcp-browser-sessions\\sessions\\a-1\\ActorSafetyLists",
            ),
            tool="spawn_browser",
        )
        second = _log_tool_error(
            shipped_events,
            RuntimeError("x"),
            failed(
                7,
                "0a1b2c3d-1111-2222-3333-444455556666",
                "/tmp/x/y/ActorSafetyLists",
            ),
            tool="spawn_browser",
        )

        assert first["fingerprint"] == second["fingerprint"]
        assert first["fingerprint"] == [
            "tool-error",
            "spawn_browser",
            "ToolError",
            "Failed to spawn browser: pid <n> for <id> could not open <path> "
            "on port <n>",
        ]

    def test_the_same_message_from_two_tools_is_two_issues(self, shipped_events):
        click = _log_tool_error(
            shipped_events,
            RuntimeError("x"),
            ToolError("No node found"),
            tool="click_element",
        )
        select = _log_tool_error(
            shipped_events,
            RuntimeError("x"),
            ToolError("No node found"),
            tool="select_option",
        )

        assert click["fingerprint"] != select["fingerprint"]
        assert click["fingerprint"][1:] == [
            "click_element",
            "ToolError",
            "No node found",
        ]

    def test_an_event_that_names_no_tool_falls_back_to_type_and_template(
        self, shipped_events
    ):
        event = _log_tool_error(
            shipped_events, RuntimeError("x"), ToolError("Gone after 5s"), tool=None
        )

        assert event["fingerprint"] == ["tool-error", "ToolError", "Gone after <n>s"]

    def test_a_different_message_is_a_different_issue(self, shipped_events):
        gone = _log_tool_error(shipped_events, NO_NODE, _click_gone("#b"))
        dom = _log_tool_error(
            shipped_events,
            ProtocolException({"code": -32000, "message": "DOM Error while querying"}),
            ToolError(
                "Failed to click element: DOM Error while querying [code: -32000]"
            ),
        )

        assert gone["fingerprint"] != dom["fingerprint"]

    def test_the_error_type_is_part_of_the_fingerprint(self, shipped_events):
        plain = _log_tool_error(
            shipped_events, RuntimeError("x"), ToolError("Instance gone")
        )
        subclass = _log_tool_error(
            shipped_events, RuntimeError("x"), InstanceNotFoundError("Instance gone")
        )

        assert plain["fingerprint"][2] == "ToolError"
        assert subclass["fingerprint"][2] == "InstanceNotFoundError"


#: (the secret as it appears in a message, every fragment that must NOT travel).
SECRETS = {
    "email with digits": (
        "Failed to type text: alice123@example.com was refused",
        ("alice123", "alice", "example.com"),
    ),
    "bearer token with digits": (
        "Request refused with Bearer abc123SecretSuffix attached",
        ("abc123SecretSuffix", "abc123", "abc", "SecretSuffix", "Suffix"),
    ),
    "jwt with digits": (
        "Session rejected: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
        (
            "eyJhbGciOiJIUzI1NiJ9",
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
            "dBjftJeZ4CVPmB92K27uhbUJU1p1r",
            "1234567890",
        ),
    ),
    "url with a token query": (
        "Navigation to https://api.example.com/v1/items?token=s3cr3tT0ken99&page=2 "
        "failed",
        ("s3cr3tT0ken99", "s3cr3t", "token=", "page=2"),
    ),
    "windows user path": (
        "Failed to read C:\\Users\\alice123\\AppData\\Local\\Temp\\x.log",
        ("alice123", "alice"),
    ),
    "posix user path": (
        "Failed to read /home/alice123/.stealth-mcp/logs/x.log",
        ("alice123", "alice"),
    ),
}


class TestTheFingerprintCarriesNoSecret:
    """The message is redacted BEFORE it is templated: templating first turns
    ``alice123@example.com`` into ``alice<n>@example.com`` (no longer an address)
    and ``Bearer abc123SecretSuffix`` into ``Bearer abc<n>SecretSuffix`` (whose
    redaction then removes only ``abc``)."""

    @pytest.mark.parametrize("case", sorted(SECRETS))
    def test_no_fragment_of_the_secret_is_transmitted(self, shipped_events, case):
        message, fragments = SECRETS[case]

        event = _log_tool_error(shipped_events, RuntimeError("x"), ToolError(message))

        sent = " ".join(event["fingerprint"])
        assert sent.startswith("tool-error click_element ToolError ")
        for fragment in fragments:
            assert fragment not in sent, (case, fragment, event["fingerprint"])
        # the event itself is held to the same rule, as before this change
        assert fragments[0] not in repr(event["exception"])

    def test_a_declared_fingerprint_is_scrubbed_too(self, shipped_events):
        event = _log_hung(
            shipped_events,
            "i-1: failed",
            "heal read C:\\Users\\alice123\\x.log for bob@example.com",
        )

        sent = " ".join(event["fingerprint"])
        assert "alice123" not in sent and "bob" not in sent


class TestEverythingElseIsUnchanged:
    def test_an_event_with_no_declared_cause_gets_no_fingerprint(self, shipped_events):
        try:
            raise AttributeError("'NoneType' object has no attribute 'send'")  # noqa: TRY301  PERMANENT(the raised error IS the fixture)
        except AttributeError:
            logging.getLogger("stealth.backend").exception("send failed")
        logging.getLogger("stealth.backend").error("a plain message")
        sentry_sdk.get_client().flush()

        assert len(shipped_events) == 2
        assert all("fingerprint" not in event for event in shipped_events)

    def test_a_fingerprint_the_scope_already_set_is_kept(self, shipped_events):
        with sentry_sdk.new_scope() as scope:
            scope.fingerprint = ["chosen-upstream"]
            _log_hung(shipped_events, "i-1: x", "probe-heal", "TimeoutError at connect")

        assert shipped_events[-1]["fingerprint"] == ["chosen-upstream"]

    def test_the_hook_changes_nothing_but_the_fingerprint(self):
        """The same event with and without a declared cause differs in ONE key."""
        error = HungBrowserError(
            "i-1: probe failed", "probe-heal", "TimeoutError at connect"
        )
        event = {"level": "error", "message": "m", "logger": "stealth.backend"}
        shipped = observability._scrub_event(
            dict(event), {"exc_info": (type(error), error, None)}
        )
        plain = observability._scrub_event(dict(event), {})

        assert plain == event
        assert shipped == {
            **event,
            "fingerprint": ["hung-browser", "probe-heal", "TimeoutError at connect"],
        }


class TestTemplate:
    @pytest.mark.parametrize(
        ("text", "template"),
        [
            ("'#b' left the page", "'#b' left the page"),
            ("missing 'stylesheets'", "missing 'stylesheets'"),
            (
                "'NoneType' has no attribute 'send'",
                "'NoneType' has no attribute 'send'",
            ),
            ("after 35s at port 9222", "after <n>s at port <n>"),
            ("see https://a.example/p?q=1 now", "see <url> now"),
            ("opened 'https://a.example/x' ok", "opened '<url>' ok"),
            ("log C:\\a\\b\\c.log gone", "log <path> gone"),
            ("log '/tmp/x/y.log' gone", "log '<path>' gone"),
            (
                "id 7e75ad01-5c3b-4a95-b86a-3c9530336361 and 3f9a1c2b7d8e4a51",
                "id <id> and <id>",
            ),
            ("Chrome/Chromium and/or Edge", "Chrome/Chromium and/or Edge"),
            ("no values here", "no values here"),
        ],
    )
    def test_only_per_occurrence_values_are_replaced(self, text, template):
        assert observability._message_template(text) == template
