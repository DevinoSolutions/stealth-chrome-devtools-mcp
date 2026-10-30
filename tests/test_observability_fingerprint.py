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

import ast
import asyncio
import http.client
import json
import logging
import pathlib
import sys
import threading
import types
import unittest.mock
import urllib.error

import pytest
from nodriver.core.connection import ProtocolException

from stealth_chrome_devtools_mcp import observability, tool_error_fingerprint
from stealth_chrome_devtools_mcp.embedded import (
    browser_health,
    clone_probe,
    element_resolution,
    forensic_errors,
    tool_registry,
)
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
            "heal",
            "TimeoutError at evaluate",
        )
        second = _log_hung(
            shipped_events,
            _hung_message(
                "d0df744d-6ad9-47c0-9352-21c349bf0676", 27273, "16.88", 15003
            ),
            "heal",
            "TimeoutError at evaluate",
        )

        assert first["fingerprint"] == second["fingerprint"]
        assert first["fingerprint"] == [
            "hung-browser",
            "heal",
            "TimeoutError at evaluate",
        ]

    def test_the_full_message_is_still_the_events_message(self, shipped_events):
        event = _log_hung(
            shipped_events,
            _hung_message(
                "7e75ad01-5c3b-4a95-b86a-3c9530336361", 71999, "33.11", 15070
            ),
            "heal",
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
            ("heal", "TimeoutError at closeTarget"),  # a different failed step
            ("heal", "ConnectionError at evaluate"),  # a different error type
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


#: The fixture's functions stand in for a file of the package: their code objects
#: are entered in the CODE map under this module name, the way the map holds the
#: package's own, so the tests of WHICH frame is named (innermost, deepest cause,
#: fallback) can pick the function and the message. The map's own correctness is
#: tested with real package functions, below.
FIXTURE_FRAME = "stealth_chrome_devtools_mcp.dom_handler"

_SITE_SOURCE = """
def click_element(error, cause):
    try:
        raise cause
    except Exception:
        raise error


def select_option(error, cause):
    try:
        raise cause
    except Exception:
        raise error


def raise_it(error):
    raise error
"""


def _sites(release_padding: int = 0) -> dict:
    """The fixture's functions, as the file of a release whose code sits
    ``release_padding`` lines further down: same names, different line numbers.
    The module's ``__name__`` is nonsense on purpose: it is never read."""
    code = compile("\n" * release_padding + _SITE_SOURCE, "dom_handler.py", "exec")
    for each in tool_error_fingerprint._code_objects(code):  # noqa: SLF001  PERMANENT(the fixture enters its code in the map the package's files are in)
        tool_error_fingerprint._code_labels[each] = (  # noqa: SLF001  PERMANENT(same)
            f"{FIXTURE_FRAME}.{each.co_qualname}"
        )
    namespace = {"__name__": "not.the.module.name"}
    exec(code, namespace)  # noqa: S102  PERMANENT(executing the fixture IS the test: a function at other line numbers)
    return namespace


@pytest.fixture(autouse=True)
def _fixture_code_is_ours(monkeypatch):
    tool_error_fingerprint._refresh()  # noqa: SLF001  PERMANENT(the maps are built before the fixture adds to them)
    labels = tool_error_fingerprint._code_labels  # noqa: SLF001  PERMANENT(same)
    before = dict(labels)
    monkeypatch.setitem(
        tool_registry.SECTION_TOOLS, "fixture", ["click_element", "select_option"]
    )
    yield
    labels.clear()
    labels.update(before)


def _ship_tool_error(
    shipped,
    error: ToolError,
    *,
    cause: Exception | None = None,
    site: str | None = "click_element",
    padding: int = 0,
    tool: str | None = "click_element",
) -> dict:
    """Ship a ``ToolError`` the way fastmcp logs one: raised by a function of
    ours (*site*) while *cause* is in flight, and logged by the tool-call logger
    (``tool=None``: by a logger that names no tool; ``site=None``: never raised)."""
    before = len(shipped)
    if site is None:  # never raised: no traceback anywhere, the cause only linked
        error.__cause__ = cause if cause is not None else RuntimeError("x")
        _log(tool, exc_info=error)
    else:
        try:
            _sites(padding)[site](
                error, cause if cause is not None else RuntimeError("x")
            )
        except ToolError:
            _log(tool)
    sentry_sdk.get_client().flush()
    assert len(shipped) == before + 1, "the event was not shipped exactly once"
    return shipped[-1]


def _log(tool: str | None, exc_info: object = True) -> None:
    """What fastmcp does (``logger.exception``), from inside an ``except`` block."""
    if tool is None:
        logging.getLogger("stealth.backend").error("a failure", exc_info=exc_info)
    else:
        logging.getLogger(TOOL_CALL_LOGGER).error(
            "Error calling tool %r", tool, exc_info=exc_info
        )


def _click_gone(selector: str = "#b") -> ToolError:
    return ToolError(
        f"Failed to click element: '{selector}' left the page each time it was "
        "resolved, before any input was sent, so nothing was clicked; query it "
        "again once the page settles"
    )


NO_NODE = ProtocolException({"code": -32000, "message": "No node with given id found"})


class TestToolError:
    def test_one_tool_and_function_is_one_issue_across_releases(self, shipped_events):
        """-EV (2.1.53) and -ET (2.1.50): the same tool, the same cause type, the
        same function at other line numbers. The line number is what split them."""
        release_a = _ship_tool_error(shipped_events, _click_gone(), cause=NO_NODE)
        release_b = _ship_tool_error(
            shipped_events, _click_gone(), cause=NO_NODE, padding=17
        )

        assert release_a["fingerprint"] == release_b["fingerprint"]
        assert release_a["fingerprint"] == [
            "tool-error",
            "click_element",
            "ToolError",
            "ProtocolException",
            "stealth_chrome_devtools_mcp.dom_handler.click_element",
        ]
        # nothing diagnostic is lost: the full title and the cause chain ship
        values = release_a["exception"]["values"]
        assert "'#b' left the page" in values[-1]["value"]
        assert [v["type"] for v in values][:2] == ["ProtocolException", "ToolError"]

    def test_a_different_cause_type_is_another_issue(self, shipped_events):
        """-EW: the same title over an ``ElementDisconnected``, not a
        ``ProtocolException``, is a different root failure and its own issue."""
        protocol = _ship_tool_error(shipped_events, _click_gone(), cause=NO_NODE)
        disconnected = _ship_tool_error(
            shipped_events,
            _click_gone(),
            cause=ElementDisconnected("'#b' is no longer in its document"),
        )

        assert disconnected["fingerprint"][3] == "ElementDisconnected"
        assert disconnected["fingerprint"] != protocol["fingerprint"]

    def test_different_keys_raised_in_one_function_are_one_issue(self, shipped_events):
        """cdp_element_cloner.py:801 wraps a missing ``stylesheets`` and a missing
        ``scripts`` KeyError as ``ToolError(str(e))``. Accepted and deliberate: one
        function, one issue; each event still shows its own key."""
        stylesheets = _ship_tool_error(
            shipped_events,
            ToolError(str(KeyError("stylesheets"))),
            cause=KeyError("stylesheets"),
        )
        scripts = _ship_tool_error(
            shipped_events,
            ToolError(str(KeyError("scripts"))),
            cause=KeyError("scripts"),
        )

        assert stylesheets["fingerprint"] == scripts["fingerprint"]
        assert "'stylesheets'" in stylesheets["exception"]["values"][-1]["value"]
        assert "'scripts'" in scripts["exception"]["values"][-1]["value"]

    def test_the_same_message_from_two_tools_is_two_issues(self, shipped_events):
        click = _ship_tool_error(
            shipped_events, ToolError("No node found"), tool="click_element"
        )
        select = _ship_tool_error(
            shipped_events, ToolError("No node found"), tool="select_option"
        )

        assert click["fingerprint"] != select["fingerprint"]
        assert click["fingerprint"][1] == "click_element"
        assert select["fingerprint"][1] == "select_option"

    def test_an_error_raised_in_two_functions_is_two_issues(self, shipped_events):
        in_click = _ship_tool_error(
            shipped_events, ToolError("No node found"), site="click_element"
        )
        in_select = _ship_tool_error(
            shipped_events, ToolError("No node found"), site="select_option"
        )

        assert in_click["fingerprint"][-1] == f"{FIXTURE_FRAME}.click_element"
        assert in_select["fingerprint"][-1] == f"{FIXTURE_FRAME}.select_option"
        assert in_click["fingerprint"] != in_select["fingerprint"]

    def test_the_error_type_and_the_whole_chain_are_part_of_it(self, shipped_events):
        plain = _ship_tool_error(
            shipped_events, ToolError("Instance gone"), cause=KeyError("a")
        )
        subclass = _ship_tool_error(
            shipped_events, InstanceNotFoundError("Instance gone"), cause=KeyError("a")
        )
        deeper = _ship_tool_error(
            shipped_events,
            ToolError("Instance gone"),
            cause=_chained(ProtocolException({"message": "m"}), KeyError("a")),
        )

        assert plain["fingerprint"][2:4] == ["ToolError", "KeyError"]
        assert subclass["fingerprint"][2] == "InstanceNotFoundError"
        assert deeper["fingerprint"][3] == "ProtocolException>KeyError"

    def test_the_innermost_frame_is_the_deepest_causes_raise_site(self, shipped_events):
        """The ToolError is raised in ``click_element``; its cause was first raised
        in ``raise_it``. The cause's own innermost frame is the one named."""
        cause = _raised_by(_sites()["raise_it"], KeyError("k"))

        event = _ship_tool_error(
            shipped_events, ToolError("wrapped"), cause=cause, site="click_element"
        )

        assert event["fingerprint"][-1] == f"{FIXTURE_FRAME}.raise_it"

    def test_no_frame_of_the_cause_falls_back_to_the_tool_errors_raise_site(
        self, shipped_events
    ):
        """The cause has only the test's own frames (not ours); the ToolError is
        raised by ``raise_it`` while it is in flight."""
        try:
            try:
                raise KeyError("k")  # noqa: TRY301  PERMANENT(the in-flight cause IS the fixture)
            except KeyError:
                _sites()["raise_it"](ToolError("wrapped"))
        except ToolError:
            _log("click_element")
        sentry_sdk.get_client().flush()

        assert shipped_events[-1]["fingerprint"] == [
            "tool-error",
            "click_element",
            "ToolError",
            "KeyError",
            f"{FIXTURE_FRAME}.raise_it",
        ]

    def test_no_traceback_at_all_falls_back_to_the_tool_and_the_types(
        self, shipped_events
    ):
        event = _ship_tool_error(shipped_events, ToolError("never raised"), site=None)

        assert event["fingerprint"] == [
            "tool-error",
            "click_element",
            "ToolError",
            "RuntimeError",
        ]

    def test_a_traceback_with_no_frame_of_ours_falls_back_the_same_way(
        self, shipped_events
    ):
        try:
            try:
                raise KeyError("k")  # noqa: TRY301  PERMANENT(the in-flight cause IS the fixture)
            except KeyError:
                raise ToolError("raised outside the package")  # noqa: B904  PERMANENT(an implicit __context__ is the shape under test)
        except ToolError:
            _log("click_element")
        sentry_sdk.get_client().flush()

        assert shipped_events[-1]["fingerprint"] == [
            "tool-error",
            "click_element",
            "ToolError",
            "KeyError",
        ]

    def test_an_event_that_names_no_tool_says_so(self, shipped_events):
        event = _ship_tool_error(shipped_events, ToolError("x"), tool=None)

        assert event["fingerprint"][:3] == ["tool-error", "-", "ToolError"]


class TestNothingACallerCanSetIsRead:
    """``create_python_binding`` and ``execute_python_in_browser`` run the caller's
    code in this process: a class it defines, a ``__name__`` it assigns and a code
    object it builds are all text of the caller's, and the fingerprint is sent."""

    SECRET = "my password is swordfish"

    def test_a_dynamic_class_named_with_a_secret_is_external(self, shipped_events):
        cause = type(self.SECRET, (Exception,), {})("x")

        event = _ship_tool_error(shipped_events, ToolError("wrapped"), cause=cause)

        assert event["fingerprint"][:4] == [
            "tool-error",
            "click_element",
            "ToolError",
            "<external>",
        ]
        assert "swordfish" not in " ".join(event["fingerprint"])

    @pytest.mark.parametrize(
        "module", ["builtins", ElementDisconnected.__module__, "no.such.module"]
    )
    def test_a_class_that_only_claims_a_trusted_module_is_external(
        self, shipped_events, module
    ):
        """A name that exists in the module, a module that exists: the class still
        is not THE class the interpreter holds under that name."""
        fake = type("ElementDisconnected", (Exception,), {"__module__": module})
        liar = type(self.SECRET, (Exception,), {"__module__": module})

        event = _ship_tool_error(shipped_events, ToolError("x"), cause=fake("x"))
        named = _ship_tool_error(shipped_events, ToolError("x"), cause=liar("x"))

        assert event["fingerprint"][3] == "<external>"
        assert named["fingerprint"][3] == "<external>"
        assert "swordfish" not in " ".join(named["fingerprint"])

    def test_a_tool_error_subclass_of_the_callers_is_external(self, shipped_events):
        mine = type(self.SECRET, (ToolError,), {})

        event = _ship_tool_error(shipped_events, mine("x"), cause=KeyError("k"))

        assert event["fingerprint"][2] == "<external>"
        assert "swordfish" not in " ".join(event["fingerprint"])

    def test_a_builtin_and_a_class_of_the_package_keep_their_names(self):
        assert tool_error_fingerprint.type_name(KeyError) == "KeyError"
        assert tool_error_fingerprint.type_name(ToolError) == "ToolError"
        assert tool_error_fingerprint.type_name(ElementDisconnected) == (
            "ElementDisconnected"
        )
        assert tool_error_fingerprint.type_name(HungBrowserError) == "HungBrowserError"
        assert tool_error_fingerprint.type_name(ProtocolException) == (
            "ProtocolException"
        )
        assert tool_error_fingerprint.type_name(FrozenCloneError) == "FrozenCloneError"
        assert tool_error_fingerprint.type_name(InstanceNotFoundError) == (
            "InstanceNotFoundError"
        )

    @pytest.mark.parametrize("where", ["<string>", "a file outside the package"])
    def test_a_frame_that_only_claims_the_package_name_is_not_ours(
        self, shipped_events, tmp_path_factory, where
    ):
        """The frame's ``__name__`` says it is a module of the package; its code
        object's file says it is not."""
        source = (
            "def boom(error, cause):\n"
            "    try:\n        raise cause\n"
            "    except Exception:\n        raise error\n"
        )
        filename = "<string>"
        if where != "<string>":
            outside = tmp_path_factory.mktemp("outside") / "binding.py"
            outside.write_text(source, encoding="utf-8")
            filename = str(outside)
        namespace = {"__name__": f"stealth_chrome_devtools_mcp.{self.SECRET}"}
        exec(compile(source, filename, "exec"), namespace)  # noqa: S102  PERMANENT(the caller's code IS the fixture)
        before = len(shipped_events)
        try:
            namespace["boom"](ToolError("x"), KeyError("k"))
        except ToolError:
            _log("click_element")
        sentry_sdk.get_client().flush()

        assert len(shipped_events) == before + 1
        assert shipped_events[-1]["fingerprint"] == [
            "tool-error",
            "click_element",
            "ToolError",
            "KeyError",
        ]

    def test_renamed_code_is_not_ours_whatever_its_name_is(self, shipped_events):
        """A code object is ours by what it IS, not by the name it carries."""
        function = _sites()["raise_it"]
        function.__code__ = function.__code__.replace(co_name=self.SECRET)

        event = _ship_tool_error(
            shipped_events,
            ToolError("x"),
            cause=_raised_by(function, KeyError("k")),
            site="select_option",
        )

        assert event["fingerprint"][-1] == f"{FIXTURE_FRAME}.select_option"
        assert "swordfish" not in " ".join(event["fingerprint"])

    @pytest.mark.parametrize(
        ("filename", "name"),
        [
            (tool_error_fingerprint.__file__, "swordfish"),
            (element_resolution.__file__, "swordfish"),
            (element_resolution.__file__, "resolve_element"),
            (observability.__file__, "_scrub_event"),
        ],
        ids=["our-file-secret-name", "other-file", "same-name", "same-name-2"],
    )
    def test_code_compiled_under_a_package_file_name_is_not_ours(
        self, shipped_events, filename, name
    ):
        """Codex round 4, HIGH: ``compile()`` takes any file name, including the
        package's own, and any function name, including one of the package's."""
        source = (
            f"def {name}(error, cause):\n"
            "    try:\n        raise cause\n"
            "    except Exception:\n        raise error\n"
        )
        namespace: dict = {}
        exec(compile(source, filename, "exec"), namespace)  # noqa: S102  PERMANENT(the caller's code IS the fixture)
        before = len(shipped_events)
        try:
            namespace[name](ToolError("x"), KeyError("k"))
        except ToolError:
            _log("click_element")
        sentry_sdk.get_client().flush()

        assert len(shipped_events) == before + 1
        assert shipped_events[-1]["fingerprint"] == [
            "tool-error",
            "click_element",
            "ToolError",
            "KeyError",
        ]

    def test_two_functions_with_one_name_in_one_module_keep_two_names(self):
        """Codex round 4, MED: ``resolve_element.<locals>._do`` and
        ``resolve_by_text.<locals>._do`` share a ``co_name``."""

        class BoomError(KeyError):
            pass

        class Tab:
            async def query_selector(self, selector):
                raise BoomError(selector)

            async def find_element_by_text(self, text, best_match):
                raise BoomError(text)

        async def provoke(resolve, argument):
            try:
                await resolve(Tab(), argument, timeout=0)
            except BoomError as error:
                return error
            raise AssertionError("did not raise")

        by_selector = asyncio.run(provoke(element_resolution.resolve_element, "#a"))
        by_text = asyncio.run(provoke(element_resolution.resolve_by_text, "a"))
        module = "stealth_chrome_devtools_mcp.embedded.element_resolution"

        assert tool_error_fingerprint.frame_name(by_selector) == (
            f"{module}.resolve_element.<locals>._do"
        )
        assert tool_error_fingerprint.frame_name(by_text) == (
            f"{module}.resolve_by_text.<locals>._do"
        )

    def test_a_module_registered_with_a_package_file_is_not_the_package(
        self, shipped_events, monkeypatch
    ):
        """Codex round 4, HIGH: ``sys.modules`` is a dict anyone writes to, and a
        module's ``__file__`` is an attribute anyone sets."""
        fake = types.ModuleType("stealth_chrome_devtools_mcp.pretend")
        fake.__file__ = element_resolution.__file__
        secret_error = type(self.SECRET, (Exception,), {"__module__": fake.__name__})
        setattr(fake, self.SECRET.replace(" ", "_"), secret_error)
        fake.Pretend = type("Pretend", (Exception,), {"__module__": fake.__name__})
        monkeypatch.setitem(sys.modules, fake.__name__, fake)

        event = _ship_tool_error(
            shipped_events, ToolError("x"), cause=secret_error("k")
        )

        assert tool_error_fingerprint.type_name(fake.Pretend) == "<external>"
        assert event["fingerprint"][3] == "<external>"
        assert "swordfish" not in " ".join(event["fingerprint"])

    @pytest.mark.parametrize("on", ["instance", "subclass", "class attribute"])
    def test_a_declaration_on_a_tool_error_is_not_read(self, shipped_events, on):
        """Codex round 4, HIGH: a ``sentry_fingerprint`` is an attribute any code
        can set, on an instance, or on a class of its own."""
        if on == "instance":
            error = ToolError("x")
            error.sentry_fingerprint = (self.SECRET,)
        else:
            mine = type("Mine", (ToolError,), {"sentry_fingerprint": (self.SECRET,)})
            error = mine("x")
            if on == "subclass":
                error.sentry_fingerprint = (self.SECRET,)

        event = _ship_tool_error(shipped_events, error, cause=KeyError("k"))

        assert event["fingerprint"][:2] == ["tool-error", "click_element"]
        assert event["fingerprint"][3] == "KeyError"
        assert "swordfish" not in " ".join(event["fingerprint"])

    @pytest.mark.parametrize(
        "declaration",
        [
            ("my password is swordfish",),
            ("hung-browser", "my password is swordfish"),
            ("hung-browser", "heal swordfish at evaluate extra"),
            ("hung-browser", "heal TimeoutError at swordfish"),
            ("swordfish", "heal"),
            ("hung-browser", "swordfish"),
            ("hung-browser", "heal C:\\Users\\alice123\\x.log for bob@example.com"),
        ],
    )
    def test_a_declaration_outside_the_vocabulary_is_ignored(
        self, shipped_events, declaration
    ):
        """Even our own class carries nothing but the closed vocabulary."""
        error = HungBrowserError("i-1: failed")
        error.sentry_fingerprint = declaration
        before = len(shipped_events)
        debug_logger.log_error("browser_health", "probe", error)
        sentry_sdk.get_client().flush()

        assert len(shipped_events) == before + 1
        assert "fingerprint" not in shipped_events[-1]

    def test_a_caller_subclass_of_our_forensic_error_declares_nothing(
        self, shipped_events
    ):
        mine = type(self.SECRET, (HungBrowserError,), {})

        before = len(shipped_events)
        debug_logger.log_error("browser_health", "probe", mine("i-1", "heal"))
        sentry_sdk.get_client().flush()

        assert len(shipped_events) == before + 1
        assert "fingerprint" not in shipped_events[-1]

    def test_an_exception_type_we_do_not_define_reads_external_in_a_cause(
        self, shipped_events
    ):
        """A type that is neither builtin, ours nor in a library we handle reads
        ``<external>``; one that is (``JSONDecodeError``) keeps its name."""
        event = _log_hung(shipped_events, "i-1: x", "heal Pretend at evaluate")
        known = _log_hung(shipped_events, "i-2: x", "heal JSONDecodeError at evaluate")
        other = _log_hung(shipped_events, "i-3: x", "relaunch-failed swordfish")

        assert event["fingerprint"] == ["hung-browser", "heal <external> at evaluate"]
        assert known["fingerprint"] == [
            "hung-browser",
            "heal JSONDecodeError at evaluate",
        ]
        assert other["fingerprint"] == ["hung-browser", "relaunch-failed <external>"]

    def test_two_websockets_handshake_failures_are_two_freeze_issues(
        self, shipped_events
    ):
        """Review of e03fd55, MED: ``browser_health._describe`` writes
        ``type(error).__name__``, and a handshake class no list named read
        ``<external>`` for every one of them."""
        status = _log_hung(shipped_events, "i-1: x", "heal InvalidStatus at connect")
        uri = _log_hung(shipped_events, "i-2: x", "heal InvalidURI at connect")

        assert status["fingerprint"] == [
            "hung-browser",
            "heal InvalidStatus at connect",
        ]
        assert uri["fingerprint"] == ["hung-browser", "heal InvalidURI at connect"]

    def test_three_spawn_failures_in_one_function_are_three_issues(
        self, shipped_events
    ):
        """Review of e03fd55, MED: ``browser_connect._patient`` reaches a ``ToolError``
        with a ``URLError``, a ``RemoteDisconnected`` or a ``JSONDecodeError`` under
        it; all three read ``<external>`` and were one issue."""
        causes = (
            urllib.error.URLError("refused"),
            http.client.RemoteDisconnected("closed"),
            json.JSONDecodeError("bad", "{", 0),
        )

        events = [
            _ship_tool_error(shipped_events, ToolError("x"), cause=cause)
            for cause in causes
        ]

        assert [event["fingerprint"][3] for event in events] == [
            "urllib.URLError",
            "http.RemoteDisconnected",
            "json.JSONDecodeError",
        ]
        assert len({tuple(event["fingerprint"]) for event in events}) == 3

    def test_protocol_exception_keeps_its_name_in_a_chain_and_a_freeze(
        self, shipped_events
    ):
        chain = _ship_tool_error(shipped_events, ToolError("x"), cause=NO_NODE)
        freeze = _log_hung(
            shipped_events, "i-1: x", "heal ProtocolException at evaluate"
        )

        assert chain["fingerprint"][3] == "ProtocolException"
        assert freeze["fingerprint"] == [
            "hung-browser",
            "heal ProtocolException at evaluate",
        ]

    @pytest.mark.parametrize("how", ["lookalike", "claims the module", "subclass"])
    def test_a_class_that_only_looks_like_a_handled_one_is_external(
        self, shipped_events, how
    ):
        nodriver_module = ProtocolException.__module__
        if how == "subclass":
            fake = type("ProtocolException", (ProtocolException,), {})
        else:
            claimed = {} if how == "lookalike" else {"__module__": nodriver_module}
            fake = type("ProtocolException", (Exception,), claimed)

        event = _ship_tool_error(
            shipped_events, ToolError("x"), cause=fake({"message": "m"})
        )

        assert tool_error_fingerprint.type_name(fake) == "<external>"
        assert event["fingerprint"][3] == "<external>"

    def test_the_declarations_of_both_forensic_classes_keep_their_names(self):
        assert tool_error_fingerprint.declared(
            FrozenCloneError("c-1", "probe TimeoutError at evaluate")
        ) == ["frozen-clone", "probe TimeoutError at evaluate"]
        assert tool_error_fingerprint.declared(
            HungBrowserError("i-1", "relaunch-probe-fails no instance")
        ) == ["hung-browser", "relaunch-probe-fails no instance"]
        assert tool_error_fingerprint.declared(HungBrowserError("i-1")) == [
            "hung-browser"
        ]

    def test_a_tool_name_that_is_not_registered_is_not_read(self, shipped_events):
        event = _ship_tool_error(shipped_events, ToolError("x"), tool="swordfish_tool")

        assert event["fingerprint"][1] == "<tool>"
        assert "swordfish" not in " ".join(event["fingerprint"])


def _chained(outer: BaseException, inner: BaseException) -> BaseException:
    """*outer* raised while *inner* is in flight: ``outer.__context__ is inner``."""
    try:
        try:
            raise inner  # noqa: TRY301  PERMANENT(the in-flight cause IS the fixture)
        except BaseException:
            raise outer  # noqa: B904  PERMANENT(an implicit __context__ is the shape under test)
    except BaseException as caught:  # noqa: BLE001  PERMANENT(handing the chain back)
        return caught


def _raised_by(function, error: BaseException) -> BaseException:
    """*error* after ``function(error)`` raised it, with the traceback that gives."""
    try:
        function(error)
    except BaseException as caught:  # noqa: BLE001  PERMANENT(handing the raised error back)
        return caught
    raise AssertionError("did not raise")


#: (what the CALLER or the machine put in a message, every fragment that must NOT
#: travel in the fingerprint). None of it is shaped like a credential, which is
#: the point: no redaction rule could catch it, so none of it may be read.
LEAKS = {
    "text the caller typed": (
        "Failed to click element: find 'my password is swordfish'",
        ("swordfish", "password", "find"),
    ),
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
        ("eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", "dBjftJeZ4CVPm"),
    ),
    "url with a token query": (
        "Navigation to https://api.example.com/v1/items?token=s3cr3tT0ken99 failed",
        ("s3cr3tT0ken99", "s3cr3t", "token=", "api.example.com"),
    ),
    "windows user path": (
        "Failed to read C:\\Users\\alice123\\AppData\\Local\\Temp\\x.log",
        ("alice123", "alice", "AppData"),
    ),
    "posix user path": (
        "Failed to read /home/alice123/.stealth-mcp/logs/x.log",
        ("alice123", "alice", "stealth-mcp"),
    ),
}


class TestTheFingerprintIsBuiltFromNoMessage:
    """A ``ToolError`` can carry what the caller typed or what the machine holds,
    and no redaction rule can tell either from prose. The fingerprint reads no
    message at all."""

    @pytest.mark.parametrize("case", sorted(LEAKS))
    def test_no_fragment_of_the_message_is_transmitted(self, shipped_events, case):
        message, fragments = LEAKS[case]

        event = _ship_tool_error(
            shipped_events, ToolError(message), cause=KeyError("k")
        )

        sent = " ".join(event["fingerprint"])
        assert event["fingerprint"][:4] == [
            "tool-error",
            "click_element",
            "ToolError",
            "KeyError",
        ]
        for fragment in fragments:
            assert fragment not in sent, (case, fragment, event["fingerprint"])

    def test_paths_with_spaces_under_two_install_dirs_are_one_issue(
        self, shipped_events
    ):
        """cdp_element_cloner.py:715-758 names the extract script by path."""
        first = _ship_tool_error(
            shipped_events,
            ToolError(
                "Failed to extract assets: 'C:\\Deploy Alpha\\pkg\\js\\extract_assets.js'"
            ),
            cause=FileNotFoundError("x"),
        )
        second = _ship_tool_error(
            shipped_events,
            ToolError(
                "Failed to extract assets: "
                "'D:\\Program Files\\Beta Install\\pkg\\js\\extract_assets.js'"
            ),
            cause=FileNotFoundError("x"),
        )

        assert first["fingerprint"] == second["fingerprint"]
        assert "Deploy" not in " ".join(first["fingerprint"])


_HEALTH = pathlib.Path(browser_health.__file__)
_CLONE_PROBE = pathlib.Path(clone_probe.__file__)

#: The only expressions a fingerprint cause may interpolate: ``failure`` is
#: ``_describe``'s ``"<ExceptionType> at <step>"`` and the other is a class name.
_PROGRAM_CONTROLLED = {"failure", "type(error).__name__"}


def _forensic_calls(path: pathlib.Path) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"HungBrowserError", "FrozenCloneError"}
    ]


class TestEveryCauseIsProgramControlled:
    """A ``ForensicError`` cause is the fingerprint: it must hold step names and
    exception type names only, never a path, an id or a caller's value."""

    def test_the_call_sites_are_found(self):
        assert len(_forensic_calls(_HEALTH)) == 6
        assert len(_forensic_calls(_CLONE_PROBE)) == 1

    @pytest.mark.parametrize("path", [_HEALTH, _CLONE_PROBE], ids=lambda p: p.name)
    def test_each_cause_is_a_constant_or_a_program_controlled_value(self, path):
        for call in _forensic_calls(path):
            for cause in call.args[1:]:
                if isinstance(cause, ast.Constant):
                    assert isinstance(cause.value, str)
                    continue
                assert isinstance(cause, ast.JoinedStr), ast.unparse(cause)
                for part in cause.values:
                    if isinstance(part, ast.FormattedValue):
                        assert ast.unparse(part.value) in _PROGRAM_CONTROLLED, (
                            path.name,
                            call.lineno,
                            ast.unparse(cause),
                        )

    def test_the_failure_text_is_a_type_name_and_a_step_name(self):
        described = browser_health._describe(
            OSError("C:\\Users\\alice123\\secret.log"), ["createTarget", "evaluate"]
        )

        assert described == "OSError at evaluate"
        assert browser_health._describe(KeyError("k"), []) == "KeyError at connect"

    def test_every_step_the_probe_can_name_is_a_literal(self):
        tree = ast.parse(_HEALTH.read_text(encoding="utf-8"))
        steps = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "steps.append"
        ]

        assert steps
        assert all(
            isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
            for call in steps
        )

    def test_the_vocabulary_is_exactly_the_steps_the_probe_names(self):
        tree = ast.parse(_HEALTH.read_text(encoding="utf-8"))
        named = {
            call.args[0].value
            for call in ast.walk(tree)
            if isinstance(call, ast.Call) and ast.unparse(call.func) == "steps.append"
        }

        # ``connect`` is what ``_describe`` says when no step has been reached
        assert named | {"connect"} == forensic_errors.STEPS

    @pytest.mark.parametrize("path", [_HEALTH, _CLONE_PROBE], ids=lambda p: p.name)
    def test_every_cause_a_call_site_passes_is_in_the_closed_vocabulary(self, path):
        """The call sites and ``forensic_errors``' vocabulary are one list: a
        site whose cause the vocabulary does not know would ship NO fingerprint."""
        stand_in = {
            "failure": "TimeoutError at evaluate",
            "type(error).__name__": "TimeoutError",
        }
        for call in _forensic_calls(path):
            causes = [
                "".join(
                    part.value
                    if isinstance(part, ast.Constant)
                    else stand_in[ast.unparse(part.value)]
                    for part in (
                        cause.values if isinstance(cause, ast.JoinedStr) else [cause]
                    )
                )
                for cause in call.args[1:]
            ]
            error = getattr(forensic_errors, call.func.id)("m", *causes)

            assert tool_error_fingerprint.declared(error) == list(
                error.sentry_fingerprint
            ), (path.name, call.lineno, causes)


class TestTheMapsAreWarmedOnceOffTheEventPath:
    @pytest.fixture
    def cold(self, monkeypatch):
        """The module as a fresh process has it: nothing built, no warm-up started,
        and ``_build`` a counter that holds until released."""
        gate = {"entered": threading.Event(), "release": threading.Event(), "calls": 0}

        def build():
            gate["calls"] += 1
            gate["entered"].set()
            assert gate["release"].wait(10)

        for name, value in (
            ("_built", False),
            ("_judged", True),
            ("_warmer", None),
            ("_build", build),
            ("_module_names", {}),
            ("_classes_read", set()),
        ):
            monkeypatch.setattr(tool_error_fingerprint, name, value)
        return gate

    def test_the_warm_up_builds_once_and_a_first_use_waits_for_it(self, cold):
        thread = tool_error_fingerprint.warm()
        assert thread is not None
        assert cold["entered"].wait(10)
        answers = []
        user = threading.Thread(
            target=lambda: answers.append(tool_error_fingerprint.type_name(KeyError))
        )
        user.start()
        user.join(0.3)

        assert user.is_alive(), "the first use did not wait for the build"
        assert tool_error_fingerprint.warm() is None  # started once
        cold["release"].set()
        user.join(10)
        thread.join(10)

        assert answers == ["KeyError"]
        assert cold["calls"] == 1
        assert not thread.is_alive()

    def test_a_first_use_with_no_warm_up_builds_it_itself(self, cold):
        cold["release"].set()

        assert tool_error_fingerprint.type_name(KeyError) == "KeyError"
        assert cold["calls"] == 1
        tool_error_fingerprint.type_name(KeyError)
        assert cold["calls"] == 1

    def test_the_backend_starts_the_warm_up_and_nothing_else_does(self, monkeypatch):
        pytest.importorskip("sentry_sdk")
        monkeypatch.delenv("STEALTH_MCP_NO_ERROR_REPORTING", raising=False)
        started = []
        monkeypatch.setattr(tool_error_fingerprint, "warm", lambda: started.append(1))
        with unittest.mock.patch("sentry_sdk.init", lambda **kwargs: None):
            assert observability.sentry_init(auto_enabling_integrations=False) is True
            assert started == []
            assert observability.sentry_init(warm_fingerprints=True) is True

        assert started == [1]

    def test_a_thread_that_cannot_start_does_not_stop_sentry_init(
        self, cold, monkeypatch, caplog
    ):
        """Review of e03fd55, LOW: ``Thread.start`` raises ``RuntimeError("can't start
        new thread")`` when the process is out of threads, and ``sentry_init`` is
        documented never to raise. The first use still builds, as the fallback."""
        pytest.importorskip("sentry_sdk")
        monkeypatch.delenv("STEALTH_MCP_NO_ERROR_REPORTING", raising=False)

        def no_thread(self):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", no_thread)
        with (
            caplog.at_level(logging.DEBUG, logger=tool_error_fingerprint.__name__),
            unittest.mock.patch("sentry_sdk.init", lambda **kwargs: None),
        ):
            assert observability.sentry_init(
                auto_enabling_integrations=False, warm_fingerprints=True
            )
        cold["release"].set()

        assert [record.levelno for record in caplog.records] == [logging.DEBUG]
        assert tool_error_fingerprint._warmer is None  # noqa: SLF001  PERMANENT(no warm-up is running, and one may be asked for again)
        assert cold["calls"] == 0
        assert tool_error_fingerprint.type_name(KeyError) == "KeyError"
        assert cold["calls"] == 1

    def test_no_warm_up_when_reporting_is_off(self, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_NO_ERROR_REPORTING", "true")
        monkeypatch.setattr(
            tool_error_fingerprint,
            "warm",
            lambda: pytest.fail("warmed with reporting off"),
        )

        assert observability.sentry_init(warm_fingerprints=True) is False


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
            _log_hung(shipped_events, "i-1: x", "heal", "TimeoutError at connect")

        assert shipped_events[-1]["fingerprint"] == ["chosen-upstream"]

    def test_the_hook_changes_nothing_but_the_fingerprint(self):
        """The same event with and without a declared cause differs in ONE key."""
        error = HungBrowserError("i-1: probe failed", "heal", "TimeoutError at connect")
        event = {"level": "error", "message": "m", "logger": "stealth.backend"}
        shipped = observability._scrub_event(
            dict(event), {"exc_info": (type(error), error, None)}
        )
        plain = observability._scrub_event(dict(event), {})

        assert plain == event
        assert shipped == {
            **event,
            "fingerprint": ["hung-browser", "heal", "TimeoutError at connect"],
        }
