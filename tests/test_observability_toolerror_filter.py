"""Which failures are worth a Sentry event, and which are the product working.

Live triage of the project's Sentry (2026-08-04) found the top issues were all
EXPECTED tool failures: `handled: yes`, `mechanism: logging`, arriving through
`FastMCP.fastmcp.tools.tool_manager` and `stealth.backend`. The largest was 205
events of "Script raised an exception: ... Illegal return statement" — an agent
handing `execute_script` a bad script, and `tool_errors._require_js_value`
correctly refusing it. That is CLAUDE.md convention 2 doing its job, and it was
drowning the events that are not.

Why the filter is by TYPE and not by logger name
------------------------------------------------
The obvious fix — `ignore_logger("FastMCP.fastmcp.tools.tool_manager")` — is the
wrong one, and this file pins why. `tool_manager` calls `logger.exception` for
*every* raising tool before re-raising (`tool_manager.py:224`/`:229`), so a real
production bug (the `AttributeError` in `navigate`) reaches Sentry through the
exact same logger as the noise. Silencing the logger silences both.

The rule, stated once: **an event is dropped only when every exception in its
chain is one of ours.** A `ToolError` raised while handling an `AttributeError`
still ships — the real bug is in there, and this filter must never be the reason
nobody saw it. Both directions are asserted here, because a filter that only
proves it drops things is a filter nobody can trust with a crash.

Everything here is a pure `event -> event | None` call. Nothing initializes
Sentry, opens a socket, or touches `~/.stealth-mcp` (`conftest.py` sets
`STEALTH_MCP_NO_ERROR_REPORTING=1` for the whole suite besides). Events are
built with the SDK's **own** `event_from_exception`, never hand-written: a
hand-shaped double would encode whatever payload shape we assumed, and the
payload shape IS what the name-matching path depends on.
"""

from __future__ import annotations

import logging
import sys

import pytest

from stealth_chrome_devtools_mcp import observability
from stealth_chrome_devtools_mcp.embedded.tool_errors import (
    InstanceNotFoundError,
    ToolError,
)

event_from_exception = pytest.importorskip("sentry_sdk.utils").event_from_exception

#: The message from the largest real issue, kept verbatim so the fixture is the
#: production event and not a paraphrase of it.
ILLEGAL_RETURN = "Script raised an exception: SyntaxError: Illegal return statement"

#: The logger the noise AND the real bug both arrive on. Load-bearing: see above.
TOOL_MANAGER_LOGGER = "FastMCP.fastmcp.tools.tool_manager"

#: A planted home path, so every "this event survived" assertion can also prove
#: the survivor was still scrubbed. Never a real username.
USER = "jdoe"


# ---------------------------------------------------------------------------
# Fixtures — built by the SDK, the way the logging integration builds them
# ---------------------------------------------------------------------------
def _raise(exc: BaseException) -> tuple[type, BaseException, object]:
    """Raise and catch ``exc`` so it carries a real traceback and chain."""
    try:
        raise exc  # noqa: TRY301  PERMANENT(raising HERE is the fixture: an exception built but never raised has no traceback and no chain, which is precisely what the SDK serializes)
    except BaseException:  # noqa: BLE001  PERMANENT(the fixture must catch whatever it was handed)
        return sys.exc_info()  # type: ignore[return-value]


def _logging_event(exc: BaseException, logger: str = TOOL_MANAGER_LOGGER):
    """The (event, hint) pair `LoggingIntegration._emit` produces for ``exc``.

    Mirrors `sentry_sdk/integrations/logging.py:275-303` exactly: build from
    `event_from_exception(record.exc_info, ...)`, then attach the record as
    ``hint["log_record"]``. That is the shape every issue in this triage had.
    """
    exc_info = _raise(exc)
    event, hint = event_from_exception(
        exc_info, mechanism={"type": "logging", "handled": True}
    )
    record = logging.LogRecord(
        name=logger,
        level=logging.ERROR,
        pathname=f"/home/{USER}/app/tool_manager.py",
        lineno=224,
        msg="Error calling tool 'execute_script'",
        args=(),
        exc_info=exc_info,
    )
    hint["log_record"] = record
    event["server_name"] = "DESKTOP-ABC123"
    event["logentry"] = {"message": f"cwd was /home/{USER}"}
    return event, hint


def _payload_only(exc: BaseException, logger: str = TOOL_MANAGER_LOGGER):
    """The same event with the hint stripped — the name-matching path.

    `hint["exc_info"]` is present in production today. This proves the decision
    does not DEPEND on it: an SDK that stops filling it in, or a transport that
    replays a serialized event, must reach the same answer.
    """
    event, _ = _logging_event(exc, logger)
    return event, {}


def _tool_error_from_attribute_error() -> tuple[type, BaseException, object]:
    """exc_info for `raise ToolError(...) from AttributeError(...)`."""
    try:
        try:
            raise AttributeError(  # noqa: TRY301  PERMANENT(the real chain IS the fixture; see _raise)
                "'NoneType' object has no attribute 'send'"
            )
        except AttributeError as cause:
            raise ToolError(ILLEGAL_RETURN) from cause
    except ToolError:
        return sys.exc_info()  # type: ignore[return-value]


# ===========================================================================
# Dropped: the error convention working as designed
# ===========================================================================
class TestExpectedFailuresAreDropped:
    @pytest.mark.parametrize(
        "exc",
        [
            ToolError(ILLEGAL_RETURN),
            ToolError("Invalid index value: abc. Must be a number."),
            InstanceNotFoundError("Instance not found: i7"),
        ],
        ids=["illegal-return", "validation", "instance-not-found"],
    )
    def test_a_raised_tool_error_never_reaches_sentry(self, exc):
        """The live-object path: `hint["exc_info"]`, matched by isinstance."""
        event, hint = _logging_event(exc)

        assert observability._scrub_event(event, hint) is None

    @pytest.mark.parametrize(
        "exc",
        [ToolError(ILLEGAL_RETURN), InstanceNotFoundError("Instance not found: i7")],
        ids=["tool-error", "instance-not-found"],
    )
    def test_the_serialized_payload_alone_is_enough_to_recognise_it(self, exc):
        """The name+module path, with no live exception to inspect."""
        event, hint = _payload_only(exc)

        assert hint == {}
        assert observability._scrub_event(event, hint) is None

    def test_the_record_is_consulted_when_the_hint_lost_its_exc_info(self):
        """`hint["log_record"].exc_info` is the documented second shape."""
        event, hint = _logging_event(ToolError(ILLEGAL_RETURN))
        del hint["exc_info"]

        assert hint["log_record"].exc_info is not None
        assert observability._scrub_event(event, hint) is None

    def test_a_subclass_raised_anywhere_is_covered_by_the_convention(self):
        """isinstance, not an allowlist: a new subclass needs no edit here."""

        class SelectorNotFoundError(ToolError):
            """A subclass declared outside `tool_errors`, as a future one may be."""

        event, hint = _logging_event(SelectorNotFoundError("no such selector"))

        assert observability._scrub_event(event, hint) is None

    def test_the_logger_it_arrived_on_does_not_change_the_answer(self):
        """`stealth.backend` and `tool_manager` ship the same noise."""
        event, hint = _logging_event(ToolError(ILLEGAL_RETURN), "stealth.backend")

        assert observability._scrub_event(event, hint) is None


# ===========================================================================
# Kept: everything that might be a real bug
# ===========================================================================
class TestRealCrashesStillShip:
    @pytest.mark.parametrize(
        "exc",
        [
            AttributeError("'NoneType' object has no attribute 'send'"),
            ValueError("Unknown CDP command: Page.nope"),
            TypeError("expected str, got int"),
            ConnectionError("connection reset by peer"),
            KeyError("instance_id"),
        ],
    )
    def test_an_unexpected_exception_survives_the_same_logging_path(self, exc):
        """THE regression this filter must never cause.

        The `AttributeError` in `navigate` — a real production bug — arrived
        through `FastMCP.fastmcp.tools.tool_manager`, the very logger the noise
        arrives on. Anything that decided by logger name would have lost it.
        """
        event, hint = _logging_event(exc)

        assert observability._scrub_event(event, hint) is not None

    @pytest.mark.parametrize(
        "exc",
        [AttributeError("'NoneType' object has no attribute 'send'"), ValueError("x")],
    )
    def test_it_survives_the_payload_only_path_too(self, exc):
        event, hint = _payload_only(exc)

        assert observability._scrub_event(event, hint) is not None

    def test_a_tool_error_wrapping_a_real_bug_still_ships(self):
        """A chain is dropped only if EVERY link is ours; here one is not.

        This is the failure mode that would be invisible: the product converts
        an internal `AttributeError` into a `ToolError` on the way out, and a
        filter that judged only the outermost type would bury the crash under
        the convention.
        """
        exc_info = _tool_error_from_attribute_error()
        event, hint = event_from_exception(
            exc_info, mechanism={"type": "logging", "handled": True}
        )

        # Both links really are in the payload — otherwise this passes vacuously.
        assert [v["type"] for v in event["exception"]["values"]] == [
            "AttributeError",
            "ToolError",
        ]
        assert observability._scrub_event(event, hint) is not None

    def test_the_same_chain_survives_the_payload_only_path(self):
        exc_info = _tool_error_from_attribute_error()
        event, _ = event_from_exception(exc_info)

        assert observability._scrub_event(event, {}) is not None

    def test_fastmcps_own_tool_error_is_a_different_class_and_still_ships(self):
        """A name collision that matters: `fastmcp.exceptions.ToolError`.

        `tool_manager` wraps every non-fastmcp failure as
        `raise ToolError(f"Error calling tool {key!r}") from e` — so a class
        named exactly `ToolError` is what a genuine crash looks like on its way
        out. Matching the bare name would drop precisely the wrong events; the
        module is checked for this reason.
        """
        fastmcp_error = pytest.importorskip("fastmcp.exceptions").ToolError
        assert fastmcp_error is not ToolError

        event, hint = _logging_event(fastmcp_error("Error calling tool 'navigate'"))
        assert event["exception"]["values"][-1]["type"] == "ToolError"

        assert observability._scrub_event(event, hint) is not None
        assert (
            observability._scrub_event(*_payload_only(fastmcp_error("x"))) is not None
        )

    def test_an_event_with_no_exception_at_all_is_not_an_expected_failure(self):
        """A bare `logger.error(...)` has no exception values; it is not noise
        we recognised, so it ships. "Nothing to classify" is never "expected"."""
        out = observability._scrub_event({"logentry": {"message": "backend wedged"}})

        assert out is not None


# ===========================================================================
# The survivors are still scrubbed — the filter runs BEFORE the scrubber and
# must not have replaced it
# ===========================================================================
class TestSurvivingEventsAreStillScrubbed:
    def test_a_real_crash_keeps_its_diagnostics_and_loses_its_pii(self):
        event, hint = _logging_event(AttributeError("boom"))

        out = observability._scrub_event(event, hint)

        assert out is not None
        assert "server_name" not in out
        assert USER not in str(out)
        assert out["logentry"]["message"] == "cwd was /home/~"
        assert out["exception"]["values"][-1]["type"] == "AttributeError"
        assert out["exception"]["values"][-1]["mechanism"]["handled"] is True

    def test_the_frame_paths_of_a_survivor_are_anonymized(self):
        event, hint = _logging_event(ValueError("boom"))
        frames = event["exception"]["values"][-1]["stacktrace"]["frames"]
        frames[0]["abs_path"] = rf"C:\Users\{USER}\src\server.py"

        out = observability._scrub_event(event, hint)

        assert out is not None
        survivor = out["exception"]["values"][-1]["stacktrace"]["frames"][0]
        assert survivor["abs_path"] == r"C:\Users\~\src\server.py"


# ===========================================================================
# The never-raises contract (#55) — now guarding a DROP decision
# ===========================================================================
class TestNeverRaises:
    @pytest.mark.parametrize(
        "malformed",
        [
            {},
            None,
            "not an event at all",
            ["a", "list"],
            42,
            {"exception": None},
            {"exception": {}},
            {"exception": {"values": None}},
            {"exception": {"values": []}},
            {"exception": {"values": "not a list"}},
            {"exception": {"values": [None]}},
            {"exception": {"values": [{"type": "ToolError"}]}},
            {"exception": {"values": [{"type": None, "module": None}]}},
        ],
    )
    @pytest.mark.parametrize(
        "hint",
        [
            None,
            {},
            {"exc_info": None},
            {"exc_info": (None, None, None)},
            {"exc_info": "not a tuple"},
            {"exc_info": ()},
            {"log_record": None},
            {"log_record": object()},
            "not a dict",
        ],
    )
    def test_a_malformed_event_or_hint_is_never_a_reason_to_raise(
        self, malformed, hint
    ):
        observability._scrub_event(malformed, hint)

    def test_an_unclassifiable_event_is_shipped_not_dropped(self):
        """On doubt, send. A classifier crash must not become a silent drop."""

        class _Hostile(dict):
            def get(self, *args, **kwargs):
                raise RuntimeError("classification exploded")

        out = observability._scrub_event(_Hostile(release="2.0.7"), {})

        assert out is not None

    def test_a_self_referential_exception_chain_terminates(self):
        """A cycle in `__context__` must not spin inside `before_send`."""
        first = ToolError("a")
        second = ToolError("b")
        first.__context__ = second
        second.__context__ = first

        assert observability._is_expected_tool_failure({}, {"exc_info": first}) is True

    def test_a_missing_tool_errors_module_degrades_to_name_matching(self, monkeypatch):
        """If the leaf could not be imported, the payload rule still decides."""
        monkeypatch.setattr(observability, "_expected_error_base", lambda: None)

        event, hint = _logging_event(ToolError(ILLEGAL_RETURN))
        assert observability._scrub_event(event, hint) is None

        event, hint = _logging_event(AttributeError("boom"))
        assert observability._scrub_event(event, hint) is not None


# ===========================================================================
# One home — the drop lives in the one registered hook, not beside it
# ===========================================================================
def test_the_filter_is_reached_through_the_one_registered_before_send(monkeypatch):
    """A second hook would be a second way to change an outgoing event.

    `test_observability.py` pins that `before_send is _scrub_event`; this pins
    the other half — that the drop decision is INSIDE that hook, so there is no
    second event-mutating callback to register, and no ordering between them.
    """
    pytest.importorskip("sentry_sdk")
    monkeypatch.delenv("STEALTH_MCP_NO_ERROR_REPORTING", raising=False)
    captured: dict[str, object] = {}

    monkeypatch.setattr("sentry_sdk.init", lambda **kwargs: captured.update(kwargs))
    assert observability.sentry_init() is True

    hook = captured["before_send"]
    assert hook is observability._scrub_event
    assert "before_send_transaction" not in captured
    assert hook(*_logging_event(ToolError(ILLEGAL_RETURN))) is None
    assert hook(*_logging_event(AttributeError("boom"))) is not None
