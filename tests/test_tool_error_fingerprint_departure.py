"""T-11043: every way a click's element leaves the page is ONE Sentry issue.

The fingerprint is built from the chain the REAL ``DOMHandler.click_element``
produces, not from a hand-built one. In production the outer ``ToolError``
(``Failed to click element: ...``) is raised inside an ``except`` with no
``from``, so the inner ``ToolError`` rides along as its ``__context__``: the
chain is ``ToolError > ToolError > <departure>``. Chrome's own reply (-EY) and
the page's ``isConnected`` answer (-EZ) differ in type and in raise site; only
the departure classifier tells them apart from every other failure.

Hermetic: ``FakeTab`` + ``FakeClickTarget`` for the two page replies, and the
real exception classes through a stubbed ``click_target.aim`` for the other two.
The re-run loop, the click's handlers and the fingerprint are unpatched; only
the sleeps are zeroed.
"""

from __future__ import annotations

import asyncio

import pytest
from nodriver.core.connection import ProtocolException

from fakes import FakeClickTarget, FakeTab, left_the_document
from stealth_chrome_devtools_mcp import observability, tool_error_fingerprint
from stealth_chrome_devtools_mcp.embedded import (
    click_target,
    element_resolution,
    page_natives,
    tool_registry,
)
from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

pytestmark = pytest.mark.asyncio

SELECTOR = "#b"
DOM = "stealth_chrome_devtools_mcp.embedded.dom_handler.DOMHandler"


def _expected(causes: str, frame: str = "click_element") -> list[str]:
    return ["tool-error", "click_element", "ToolError", causes, f"{DOM}.{frame}"]


@pytest.fixture(autouse=True)
def _instant(monkeypatch):
    monkeypatch.setattr(element_resolution, "_SETTLE_SECONDS", 0.0)
    real_sleep = asyncio.sleep

    async def no_wait(_seconds, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    monkeypatch.setitem(tool_registry.SECTION_TOOLS, "fixture", ["click_element"])


class _ReplacingTab(FakeTab):
    def __init__(self, *targets: FakeClickTarget) -> None:
        super().__init__(select_result=targets[0])
        self._queue = list(targets)

    async def query_selector(self, selector, *args, **kwargs):
        if self._queue:
            self._select_result = self._queue.pop(0)
        return await super().query_selector(selector, *args, **kwargs)


def _fingerprint(error: BaseException) -> tuple[list[str], list[type]]:
    chain = observability._exception_chain(error)  # noqa: SLF001  PERMANENT(the chain the SDK serializes, which is what the fingerprint is built from)
    event = {"logentry": {"formatted": "Error calling tool 'click_element'"}}
    return tool_error_fingerprint.parts(event, chain), [type(c) for c in chain]


async def _clicked(tab) -> ToolError:
    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, SELECTOR)
    return caught.value


def _aim_raising(error: BaseException, monkeypatch) -> FakeTab:
    async def aim(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(click_target, "aim", aim)
    return _ReplacingTab(FakeClickTarget())


async def test_chrome_replying_no_node_is_the_production_chain():
    """-EY: ``ProtocolException: No node with given id found``."""
    tab = _ReplacingTab(*[FakeClickTarget(detached=True) for _ in range(5)])

    parts, kinds = _fingerprint(await _clicked(tab))

    assert kinds == [ToolError, ToolError, ProtocolException]
    assert parts == _expected("ToolError>ElementDeparted")


async def test_the_page_saying_it_is_disconnected_is_the_same_fingerprint():
    """-EZ: ``ElementDisconnected``, raised by ``dom_handler._aim``."""
    tab = _ReplacingTab(*[FakeClickTarget(disconnected=True) for _ in range(5)])

    parts, kinds = _fingerprint(await _clicked(tab))

    assert [k.__name__ for k in kinds] == [
        "ToolError",
        "ToolError",
        "ElementDisconnected",
    ]
    assert parts == _expected("ToolError>ElementDeparted")


async def test_a_frame_that_is_gone_is_the_same_fingerprint(monkeypatch):
    tab = _aim_raising(page_natives.FrameGone("no frame holds the node"), monkeypatch)

    parts, kinds = _fingerprint(await _clicked(tab))

    assert kinds[-1] is page_natives.FrameGone
    assert parts == _expected("ToolError>ElementDeparted")


async def test_a_context_that_cannot_be_found_is_the_same_fingerprint(monkeypatch):
    tab = _aim_raising(
        left_the_document("Cannot find context with specified id"), monkeypatch
    )

    parts, kinds = _fingerprint(await _clicked(tab))

    assert kinds[-1] is ProtocolException
    assert parts == _expected("ToolError>ElementDeparted")


async def test_a_departure_after_input_is_its_own_issue_at_the_dispatch():
    """Input went out, so it is not re-run: three links, raised in
    ``_dispatch_click``. A different message and a different raise site than the
    before-input one, and it must stay a different issue."""
    tab = _ReplacingTab(FakeClickTarget(detach_on="mouseMoved"), FakeClickTarget())

    parts, kinds = _fingerprint(await _clicked(tab))

    assert kinds[:3] == [ToolError, ToolError, ToolError]
    assert parts == _expected("ToolError>ToolError>ElementDeparted", "_dispatch_click")
    assert parts != _expected("ToolError>ElementDeparted")


async def test_a_refusal_that_is_not_a_departure_keeps_its_own_cause(monkeypatch):
    tab = _aim_raising(
        ProtocolException({"code": -32000, "message": "Not attached"}), monkeypatch
    )

    parts, _ = _fingerprint(await _clicked(tab))

    assert parts[3] == "ProtocolException"


async def test_a_cause_whose_text_cannot_be_read_is_not_a_departure():
    class Unreadable(ProtocolException):
        def __str__(self) -> str:
            raise RuntimeError("no text")

    try:
        try:
            raise Unreadable({"message": "m"})  # noqa: TRY301  PERMANENT(the in-flight cause IS the fixture)
        except Unreadable:
            raise ToolError("wrapped")  # noqa: B904  PERMANENT(an implicit __context__ is the shape under test)
    except ToolError as error:
        parts, _ = _fingerprint(error)

    assert parts[:4] == ["tool-error", "click_element", "ToolError", "<external>"]
