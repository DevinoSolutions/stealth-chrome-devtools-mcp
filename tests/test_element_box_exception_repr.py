"""F-912: nodriver's "could not find position" exception carries the whole repr.

``nodriver/core/element.py``:498-499 raises a bare
``Exception("could not find position for %s " % self)`` when
``DOM.getContentQuads`` answers with an empty list, and ``Element.__repr__``
renders the tag, **every attribute as ``name="value"`` and the element's whole
recursive descendant TEXT**. F-907 shaped that rendering for LOG RECORDS and
could not reach this one: an exception is not a ``LogRecord``, and the factory
inside ``Logger.makeRecord`` sits upstream of sinks, not of raises.

Unlike F-907's three box-model WARNINGs — unreachable in nodriver 0.47 because
``Position.center`` is always truthy — **this branch is reachable**, measured
against Chrome 153 on a real page:

===========================  ======================  ======================
shape                        ``getContentQuads``     ``get_position()``
===========================  ======================  ======================
ordinary box                 1 quad                  ``Position``
``visibility:hidden``        1 quad                  ``Position``
zero-size box                1 quad                  ``Position``
empty inline                 1 quad                  ``Position``
``content-visibility:hidden``  1 quad                ``Position``
``display:none``             ``[]``                  **raises, with repr**
``<option>`` in a ``select`` ``[]``                  **raises, with repr**
===========================  ======================  ======================

and measured through our own handlers on that page::

    get_element_state('#pwhidden') RAISED ToolError
      'Failed to get element state: could not find position for <input
       id="pwhidden" type="password" value="SECRET-VALUE-MARKER"
       style="display:none"></input> '
    [DEBUG] dom_handler.click_element: could not find position for <input
       id="pwhidden" type="password" value="SECRET-VALUE-MARKER" ...>

These pins build a REAL ``nodriver.core.element.Element`` from nodriver's own
constructors and drive nodriver's REAL ``get_position``, on F-907's argument: a
hand-written double renders whatever ``__repr__`` we gave it and so could only
ever measure the test against itself. The only thing faked is the TAB, whose
``send`` answers the two CDP commands ``get_position`` issues — because what is
being reproduced is Chrome answering ``[]``.

The premise nodes are the ones that matter for the life of this fix: the day a
nodriver release re-types or rewords that raise, they go RED here rather than
the guard silently ceasing to fire in production.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest
import sentry_sdk
import sentry_sdk.utils
from nodriver import cdp
from nodriver.core.connection import ProtocolException
from nodriver.core.element import Element

import logging_state
from fakes import cdp_command_name
from stealth_chrome_devtools_mcp.embedded import element_box, logging_setup
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.element_box import ElementBoxError
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

#: One marker per half of ``__repr__``, so a failure names WHICH half escaped.
ATTR_VALUE_MARK = "F912_ATTR_VALUE"
TOKEN_VALUE_MARK = "F912_TOKEN_VALUE"
TEXT_CONTENT_MARK = "F912_TEXT_CONTENT"

SECRETS = {
    "attribute value": ATTR_VALUE_MARK,
    "data-* token value": TOKEN_VALUE_MARK,
    "element text content": TEXT_CONTENT_MARK,
}

#: What the replacement must still SAY, so a "fix" that silences it fails.
KEPT_SHAPE = ("input", "type", "value", "data-session-token")


# --------------------------------------------------------------------------
# Real nodriver objects, from nodriver's own constructors
# --------------------------------------------------------------------------
def make_element(tab: Any) -> Element:
    """An ``<input type=password>`` with a value, a token and child text.

    The dict is the shape ``DOM.describeNode`` answers with and ``from_json``
    is nodriver's own parser for it, so every field this element exposes is one
    nodriver itself would have built.
    """
    node = cdp.dom.Node.from_json(
        {
            "nodeId": 42,
            "backendNodeId": 4242,
            "nodeType": 1,
            "nodeName": "INPUT",
            "localName": "input",
            "nodeValue": "",
            "childNodeCount": 1,
            "attributes": [
                "type",
                "password",
                "value",
                ATTR_VALUE_MARK,
                "data-session-token",
                TOKEN_VALUE_MARK,
            ],
            "children": [
                {
                    "nodeId": 43,
                    "backendNodeId": 4343,
                    "nodeType": 3,
                    "nodeName": "#text",
                    "localName": "",
                    "nodeValue": TEXT_CONTENT_MARK,
                    "childNodeCount": 0,
                }
            ],
        }
    )
    # ``tree=node`` so ``Element.parent`` answers None instead of raising its
    # "no tree set" RuntimeError; ``get_position`` reads ``self.parent`` before
    # anything else, and a raise there would measure the harness, not nodriver.
    return Element(node, tab=tab, tree=node)


class QuadlessTab:
    """The one thing that is faked: Chrome answering ``getContentQuads``.

    Keyed on the generator's own ``gi_code.co_name`` (``fakes.cdp_command_name``)
    so the double never has to know nodriver's wire format — it answers the two
    commands ``get_position`` issues and asserts on anything else.
    """

    def __init__(self, quads: Any = (), *, quads_error: Exception | None = None):
        self.quads = quads
        self.quads_error = quads_error
        self.commands: list[str] = []

    async def send(self, cdp_obj: Any, *_args: Any, **_kwargs: Any) -> Any:
        name = cdp_command_name(cdp_obj)
        self.commands.append(name)
        if name == "resolve_node":
            return cdp.runtime.RemoteObject(
                type_="object", object_id=cdp.runtime.RemoteObjectId("OBJ-F912")
            )
        if name == "get_content_quads":
            if self.quads_error is not None:
                raise self.quads_error
            return self.quads
        raise AssertionError(f"unexpected CDP command in get_position: {name}")

    async def mouse_click(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a boxless element must never reach a mouse click")

    async def flash_point(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def nodrivers_own_get_position():
    """nodriver's ``get_position``, guard or no guard.

    The wrapper keeps the original on its marker, so the premise nodes can drive
    the UNGUARDED raise in a process where ``install()`` has already run — which
    it has, because importing ``tool_runtime`` anywhere in the session installs
    it.
    """
    return getattr(Element.get_position, element_box._MARKER, Element.get_position)


def raise_from_nodriver(element: Element) -> Exception:
    """Whatever nodriver's own ``get_position`` raises for empty quads."""
    with pytest.raises(BaseException) as caught:  # noqa: PT011,B017 - PERMANENT(F-912 — the exception's exact TYPE is what the premise measures, so the pin may not narrow what it catches)
        asyncio.run(nodrivers_own_get_position()(element))
    return caught.value


@pytest.fixture
def guarded():
    """``install()`` applied, and the class handed back exactly as found."""
    before = Element.get_position
    element_box.install()
    try:
        yield
    finally:
        Element.get_position = before


@pytest.fixture
def unguarded():
    """nodriver's own method on the class, whatever this session installed."""
    before = Element.get_position
    Element.get_position = nodrivers_own_get_position()
    try:
        yield
    finally:
        Element.get_position = before


# --------------------------------------------------------------------------
# Premises — the nodriver facts this whole fix rests on
# --------------------------------------------------------------------------
class TestPremise:
    def test_get_position_raises_a_bare_exception_carrying_the_whole_repr(self):
        """The leak itself, driven through nodriver's own code.

        If this goes green on its own, nodriver fixed it and this module can be
        deleted.
        """
        element = make_element(QuadlessTab(quads=[]))
        exc = raise_from_nodriver(element)

        assert type(exc) is Exception, (
            "the guard keys on the EXACT builtin; a nodriver release that raises "
            f"a subclass here silently stops being guarded (got {type(exc)!r})"
        )
        text = str(exc)
        assert "could not find position for" in text
        leaked = {name for name, mark in SECRETS.items() if mark in text}
        assert leaked == set(SECRETS), (
            "the premise is that ALL THREE halves of __repr__ ride in this "
            f"message; only {sorted(leaked)} did"
        )

    def test_the_raise_still_interpolates_self(self):
        """Read out of nodriver's installed source, never assumed.

        Keyed on the AST and not on a line number, because line numbers drift
        between releases while "the only raise in ``get_position`` renders
        ``self``" is the fact the guard depends on.
        """
        source = Path(inspect.getsourcefile(Element)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        func = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_position"
        )
        raises = [n for n in ast.walk(func) if isinstance(n, ast.Raise)]
        assert len(raises) == 1, (
            f"get_position has {len(raises)} raise sites now; the guard's "
            "argument is that it has exactly one"
        )
        rendered = ast.unparse(raises[0])
        assert "Exception(" in rendered, rendered
        assert "% self" in rendered, (
            f"get_position no longer renders self at line {raises[0].lineno}: "
            f"{rendered!r} — re-measure F-912 before keeping the guard"
        )

    def test_empty_quads_is_the_branch_and_an_error_is_not(self):
        """MEASURED on Chrome 153: a boxless element answers ``[]``.

        This is the whole difference from F-907's severity: its three WARNINGs
        cannot fire, this raise runs for ``display:none`` and for an
        ``<option>``. A Chrome that answered an ERROR instead would make the
        leak unreachable, so the empty-list premise is pinned rather than
        assumed.
        """
        tab = QuadlessTab(quads=[])
        exc = raise_from_nodriver(make_element(tab))
        assert type(exc) is Exception
        assert tab.commands[-1] == "get_content_quads"

    def test_element_repr_still_renders_values_and_text(self):
        rendered = str(make_element(QuadlessTab()))
        for name, mark in SECRETS.items():
            assert mark in rendered, f"__repr__ no longer renders the {name}"


# --------------------------------------------------------------------------
# The behavioural pin — no page content reaches any sink
# --------------------------------------------------------------------------
class TestNoPageContentInTheException:
    def test_the_guard_replaces_the_message(self, guarded):
        element = make_element(QuadlessTab(quads=[]))
        with pytest.raises(ElementBoxError) as caught:
            asyncio.run(element.get_position())

        text = str(caught.value)
        leaked = {name for name, mark in SECRETS.items() if mark in text}
        assert leaked == set(), f"page content still in the exception: {sorted(leaked)}"

    def test_the_chain_carries_nothing_either(self, guarded):
        """``__context__``/``__cause__`` are a sink of their own.

        Every traceback formatter prints "During handling of the above
        exception…" and sentry-sdk's chain walk follows ``__context__``
        regardless of ``__suppress_context__`` — so the guard raises OUTSIDE the
        handler and the chain must be EMPTY, not merely suppressed.
        """
        element = make_element(QuadlessTab(quads=[]))
        with pytest.raises(ElementBoxError) as caught:
            asyncio.run(element.get_position())

        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None, (
            "nodriver's message is reachable through __context__; build the "
            "replacement outside the except block"
        )

    def test_the_traceback_carries_nothing(self, guarded):
        """What a sink that formats ``exc_info`` would actually render."""
        import traceback

        element = make_element(QuadlessTab(quads=[]))
        try:
            asyncio.run(element.get_position())
        except ElementBoxError as exc:
            rendered = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
        leaked = {name for name, mark in SECRETS.items() if mark in rendered}
        assert leaked == set(), f"page content in the traceback: {sorted(leaked)}"

    def test_the_client_facing_tool_error(self, guarded, monkeypatch):
        """``get_element_state``'s ``ToolError`` — the leg that reaches the caller.

        Measured unfixed::

            'Failed to get element state: could not find position for <input
             id="pwhidden" type="password" value="SECRET-VALUE-MARKER" ...>'
        """
        from stealth_chrome_devtools_mcp.embedded import dom_handler as dom_handler_mod

        element = make_element(QuadlessTab(quads=[]))

        async def _resolve(_tab, _selector, **_kwargs):
            return element

        async def _refresh(_tab, _element):
            return None

        monkeypatch.setattr(dom_handler_mod, "resolve_element", _resolve)
        monkeypatch.setattr(dom_handler_mod, "refresh_element", _refresh)

        with pytest.raises(ToolError) as caught:
            asyncio.run(
                dom_handler_mod.DOMHandler.get_element_state(object(), "#pwhidden")
            )

        text = str(caught.value)
        leaked = {name for name, mark in SECRETS.items() if mark in text}
        assert leaked == set(), f"the client is still told {sorted(leaked)}"
        assert "no layout box" in text, text

    def test_the_debug_ring_entry(self, guarded):
        """``log_tool_failure`` is how a failed tool call reaches the ring."""
        element = make_element(QuadlessTab(quads=[]))
        try:
            asyncio.run(element.get_position())
        except ElementBoxError as exc:
            failure = ToolError(f"Failed to get element state: {exc!s}")

        debug_logger.log_tool_failure("get_element_state", failure)
        blob = json.dumps(debug_logger.get_debug_view(), default=str)
        leaked = {name for name, mark in SECRETS.items() if mark in blob}
        assert leaked == set(), f"the debug ring still holds {sorted(leaked)}"

    def test_the_backend_log_line_click_element_writes(self, guarded, tmp_path):
        """``dom_handler.click_element``'s ``log_debug(..., str(e))``.

        A real handler on the real logger ``debug_logger`` writes to, at DEBUG,
        because that is the level the line is written at and the level a
        ``--debug`` backend runs its file handler at.
        """
        element = make_element(QuadlessTab(quads=[]))
        try:
            asyncio.run(element.mouse_click())
        except ElementBoxError as exc:
            relayed = str(exc)
        else:  # pragma: no cover - the raise is the point
            pytest.fail("mouse_click swallowed the boxless element")

        log_file = tmp_path / "backend.log"
        before = logging_state.snapshot()
        logging_state.reset()
        handler = logging.FileHandler(log_file, encoding="utf-8")
        logger = logging.getLogger("stealth.backend")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            debug_logger.log_debug("dom_handler", "click_element", relayed)
        finally:
            logger.removeHandler(handler)
            handler.close()
            logging_state.restore(before)

        written = log_file.read_text(encoding="utf-8")
        leaked = {name for name, mark in SECRETS.items() if mark in written}
        assert leaked == set(), f"the backend log still holds {sorted(leaked)}"

    def test_the_sentry_event(self, guarded):
        """What ``observability._scrub_event`` would let ship.

        Built by the SDK's own ``event_from_exception`` and scrubbed by the
        real ``before_send``, so the pin measures the serialised shape rather
        than a hand-made dict. The exception is taken NAKED rather than as a
        ``ToolError``: ``expected_events`` DROPS the convention class, so
        wrapping it would measure that drop instead of this redaction, and the
        naked shape is what any future non-``ToolError`` relay produces.
        """
        from stealth_chrome_devtools_mcp import observability

        element = make_element(QuadlessTab(quads=[]))
        try:
            asyncio.run(element.get_position())
        except ElementBoxError:
            exc_info = sys.exc_info()

        # A real (DSN-less, so transport-less) client, so the serializer reads
        # the SDK's own option set rather than a hand-made dict that a version
        # bump can outgrow. ``include_local_variables`` is production's value,
        # set in ``observability.sentry_init`` and pinned by the node below:
        # with locals ON the element is a LOCAL of the raising frame and
        # travels whatever the message says.
        options = sentry_sdk.Client(include_local_variables=False).options
        event, hint = sentry_sdk.utils.event_from_exception(
            exc_info,
            client_options=options,
            mechanism={"type": "logging", "handled": True},
        )
        shipped = observability._scrub_event(event, hint)

        assert shipped is not None, (
            "the event was dropped; this pin measures its CONTENT, so a drop "
            "would make it green for the wrong reason"
        )
        blob = json.dumps(shipped, default=str)
        leaked = {name for name, mark in SECRETS.items() if mark in blob}
        assert leaked == set(), f"Sentry would still ship {sorted(leaked)}"

    def test_the_shipped_sentry_shape_is_the_tool_error(self, guarded, monkeypatch):
        """The leg that actually ships, assembled the way ``_emit`` does.

        MEASURED: this event is **not** dropped. ``expected_events``'
        ``error-convention`` requires the outermost link to be ours AND
        tolerates only ``TimeoutError``/``CancelledError`` behind it — and
        ``get_element_state`` raises its ``ToolError`` from inside an
        ``except``, so the chain is ``[ToolError, <the library's>]`` and the
        rule refuses. So the client, the ring and **Sentry** all had it.
        """
        from stealth_chrome_devtools_mcp import observability
        from stealth_chrome_devtools_mcp.embedded import dom_handler as dom_handler_mod

        logger_name = "FastMCP.fastmcp.tools.tool_manager"
        element = make_element(QuadlessTab(quads=[]))

        async def _resolve(_tab, _selector, **_kwargs):
            return element

        async def _refresh(_tab, _element):
            return None

        monkeypatch.setattr(dom_handler_mod, "resolve_element", _resolve)
        monkeypatch.setattr(dom_handler_mod, "refresh_element", _refresh)

        try:
            asyncio.run(
                dom_handler_mod.DOMHandler.get_element_state(object(), "#pwhidden")
            )
        except ToolError:
            exc_info = sys.exc_info()

        options = sentry_sdk.Client(include_local_variables=False).options
        event, hint = sentry_sdk.utils.event_from_exception(
            exc_info,
            client_options=options,
            mechanism={"type": "logging", "handled": True},
        )
        event["logger"] = logger_name
        event["logentry"] = {
            "message": "Error calling tool 'get_element_state'",
            "formatted": "Error calling tool 'get_element_state'",
            "params": [],
        }
        hint["log_record"] = logging.LogRecord(
            name=logger_name,
            level=logging.ERROR,
            pathname="/app/tool_manager.py",
            lineno=224,
            msg="Error calling tool 'get_element_state'",
            args=(),
            exc_info=exc_info,
        )
        shipped = observability._scrub_event(event, hint)

        assert shipped is not None, (
            "this event is measured to SHIP; a drop would make the pin green "
            "for the wrong reason — re-measure before changing it"
        )
        blob = json.dumps(shipped, default=str)
        leaked = {name for name, mark in SECRETS.items() if mark in blob}
        assert leaked == set(), f"Sentry would still ship {sorted(leaked)}"

    def test_the_sentry_leg_depends_on_include_local_variables(self):
        """The element is a LOCAL of the raising frame, in both versions.

        ``cdp_transport``'s F-902 node makes the same link for the same reason:
        this module's PII argument rests on a setting another module owns, so
        flipping it must fail a pin in the file that depends on it.
        """
        from stealth_chrome_devtools_mcp import observability

        source = Path(observability.__file__).read_text(encoding="utf-8")
        assert "include_local_variables=False" in source.replace(" ", ""), (
            "element_box's message is shape-only, but Sentry serialising frame "
            "locals would carry the element out regardless"
        )


# --------------------------------------------------------------------------
# Redacting is not silencing
# --------------------------------------------------------------------------
class TestTheLineStillSaysSomething:
    def test_it_still_names_the_element(self, guarded):
        element = make_element(QuadlessTab(quads=[]))
        with pytest.raises(ElementBoxError) as caught:
            asyncio.run(element.get_position())

        text = str(caught.value)
        for kept in KEPT_SHAPE:
            assert kept in text, (
                f"{kept!r} is the page's VOCABULARY, not its content — a fix "
                "that silences the line rather than shaping it fails here"
            )

    def test_the_type_names_the_condition(self, guarded):
        """``query_elements`` logs ``type(e).__name__`` and nothing else."""
        element = make_element(QuadlessTab(quads=[]))
        with pytest.raises(ElementBoxError) as caught:
            asyncio.run(element.get_position())
        assert type(caught.value).__name__ == "ElementBoxError"

    def test_it_uses_f907s_shaper_and_not_a_second_one(self):
        """One rendering rule, one home (convention 4).

        The module must reach ``logging_setup._shape`` and must not build a
        shape of its own — pinned by driving the bounds through it, so a
        re-spelled renderer that forgot ``SHAPE_MAX_ATTRS`` fails here.
        """
        source = Path(element_box.__file__).read_text(encoding="utf-8")
        assert (
            "from stealth_chrome_devtools_mcp.embedded.logging_setup import _shape"
            in source
        )
        assert "attrs=[" not in source.split('"""', 2)[-1], (
            "element_box renders its own shape; there is one shaper and it is "
            "logging_setup._shape"
        )

    def test_the_bounds_are_f907s(self, guarded):
        """An element with more attributes than the cap is clamped."""
        node = cdp.dom.Node.from_json(
            {
                "nodeId": 7,
                "backendNodeId": 77,
                "nodeType": 1,
                "nodeName": "DIV",
                "localName": "div",
                "nodeValue": "",
                "childNodeCount": 0,
                "attributes": [
                    item
                    for index in range(logging_setup.SHAPE_MAX_ATTRS + 5)
                    for item in (f"data-attr-{index}", ATTR_VALUE_MARK)
                ],
            }
        )
        element = Element(node, tab=QuadlessTab(quads=[]), tree=node)
        with pytest.raises(ElementBoxError) as caught:
            asyncio.run(element.get_position())
        text = str(caught.value)
        assert logging_setup.SHAPE_OVERFLOW in text
        assert ATTR_VALUE_MARK not in text


# --------------------------------------------------------------------------
# Everything that is NOT the bare Exception passes through untouched
# --------------------------------------------------------------------------
class TestPassesEverythingElseThrough:
    def test_a_protocol_exception_keeps_chromes_own_words(self, guarded):
        """Chrome's diagnostic is the one thing an operator can act on."""
        chrome = ProtocolException(
            {"message": "Could not compute content quads.", "code": -32000}
        )
        element = make_element(QuadlessTab(quads_error=chrome))
        with pytest.raises(ProtocolException) as caught:
            asyncio.run(element.get_position())
        assert caught.value is chrome
        assert "Could not compute content quads" in str(caught.value)

    def test_a_subclass_of_exception_is_not_shaped(self, guarded):
        boom = RuntimeError("a diagnostic of somebody else's")
        element = make_element(QuadlessTab(quads_error=boom))
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(element.get_position())
        assert caught.value is boom

    def test_a_base_exception_passes(self, guarded):
        """A cancelled CDP budget must still cancel."""
        element = make_element(QuadlessTab(quads_error=asyncio.CancelledError()))
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(element.get_position())

    def test_a_successful_read_is_unchanged(self, guarded):
        quads = [cdp.dom.Quad([0.0, 0.0, 10.0, 0.0, 10.0, 8.0, 0.0, 8.0])]
        element = make_element(QuadlessTab(quads=quads))
        position = asyncio.run(element.get_position())
        assert position.center == (5.0, 4.0)

    def test_the_arguments_still_reach_nodriver(self, guarded):
        """``abs=True`` is nodriver's own parameter; the wrapper forwards it."""
        signature = inspect.signature(nodrivers_own_get_position())
        assert list(signature.parameters) == ["self", "abs"], (
            "nodriver changed get_position's signature; the wrapper forwards "
            "*args/**kwargs, so this is a notice rather than a break"
        )


# --------------------------------------------------------------------------
# Mechanism
# --------------------------------------------------------------------------
class TestMechanism:
    def test_install_is_idempotent(self, guarded):
        element_box.install()
        first = Element.get_position
        element_box.install()
        assert Element.get_position is first
        assert element_box.installed() is True

    def test_the_wrapper_keeps_nodrivers_own(self, guarded):
        original = getattr(Element.get_position, element_box._MARKER)
        assert original is not Element.get_position
        assert original.__qualname__ == "Element.get_position"

    def test_installed_is_false_without_it(self, unguarded):
        assert element_box.installed() is False

    def test_it_covers_mouse_click(self, guarded):
        """``click_element`` never calls ``get_position``; nodriver does.

        This is why the guard is at the raise: the only thing our own call site
        can see is the message, and the element is gone by then.
        """
        element = make_element(QuadlessTab(quads=[]))
        with pytest.raises(ElementBoxError) as caught:
            asyncio.run(element.mouse_click())
        leaked = {name for name, mark in SECRETS.items() if mark in str(caught.value)}
        assert leaked == set()

    def test_tool_runtime_installs_it(self):
        """One call site, in the module loaded once under runpy."""
        from stealth_chrome_devtools_mcp.embedded import tool_runtime

        source = Path(tool_runtime.__file__).read_text(encoding="utf-8")
        assert source.count("element_box.install()") == 1
        assert element_box.installed() is True, (
            "importing tool_runtime must leave the guard in place"
        )

    def test_the_error_is_not_a_tool_error(self):
        """``expected_events`` DROPS ``ToolError`` from Sentry."""
        assert not issubclass(ElementBoxError, ToolError)
