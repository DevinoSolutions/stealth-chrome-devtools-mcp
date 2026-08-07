"""RED-first pins for F-783 / the dom_handler raise-typing sweep (2.0.8).

Convention 2 says a tool reports an expected failure by RAISING
``tool_errors.ToolError``. Two families escaped it and shipped to Sentry as if
they were crashes, because ``observability._is_expected_tool_failure`` drops an
event only when EVERY exception in the chain is one of ours:

* ``dom_handler``'s interaction surface raised bare ``Exception`` — the live
  evidence was ``Element not found: {selector}`` re-wrapped as ``Failed to type
  text: ...`` (issue STEALTH-CHROME-DEVTOOLS-MCP-2H);
* ``server._with_cdp_timeout`` raised bare ``Exception``, and because the
  instance UUID is inside the message, Sentry split one failure mode into seven
  issues (the F-783 residual the audit deferred as C5).

These pin the TYPE, not just the message: ``pytest.raises(Exception)`` passes
for a ``ToolError`` too, so every assertion here is on the exact class.
"""

import asyncio
from typing import ClassVar

import pytest

from stealth_chrome_devtools_mcp import observability
from stealth_chrome_devtools_mcp.embedded import dom_handler as dom_handler_mod
from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler
from stealth_chrome_devtools_mcp.embedded.server import _with_cdp_timeout
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

SELECTOR = "#no-such-element"


@pytest.fixture()
def no_element(monkeypatch):
    """``resolve_element`` finds nothing — the one seam every interaction uses.

    Patched on ``dom_handler``'s own namespace because that is where the name is
    bound (the absolute-import convention means the module holds its own
    reference, so patching ``element_resolution`` would not be seen here).
    """

    async def _none(*args, **kwargs):
        return None

    monkeypatch.setattr(dom_handler_mod, "resolve_element", _none)


@pytest.fixture()
def any_element(monkeypatch):
    """``resolve_element`` succeeds, so a test can reach an argument guard."""

    class _Element:
        tag_name = "select"
        attrs: ClassVar[dict[str, str]] = {}

        async def send_keys(self, _text):
            return None

    async def _found(*args, **kwargs):
        return _Element()

    monkeypatch.setattr(dom_handler_mod, "resolve_element", _found)


class _RaisingTab:
    """A tab whose every CDP call fails with a plain transport-shaped error."""

    def __init__(self, error=None):
        self._error = error or RuntimeError("connection lost")

    async def evaluate(self, *args, **kwargs):
        raise self._error

    async def get_content(self, *args, **kwargs):
        raise self._error

    async def send(self, *args, **kwargs):
        raise self._error


# ---------------------------------------------------------------------------
# The two families named in the Sentry evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_type_text_missing_element_raises_tool_error(no_element):
    """The exact shipped issue: type_text on a selector that resolves to nothing.

    The message chain is preserved — the outer re-wrap still names the operation
    and the inner failure still names the selector, so a caller can still act.
    """
    with pytest.raises(ToolError) as caught:
        await DOMHandler.type_text(_RaisingTab(), SELECTOR, "hello")

    assert type(caught.value) is ToolError
    assert str(caught.value).startswith("Failed to type text: ")
    assert f"Element not found: {SELECTOR}" in str(caught.value)


@pytest.mark.asyncio
async def test_cdp_timeout_raises_tool_error():
    """F-783/C5: the timeout path joins the one error convention.

    Typing it drops the whole family from Sentry, which also dissolves the
    per-UUID issue split (the instance id is inside the message).
    """

    async def never():
        await asyncio.sleep(30)

    with pytest.raises(ToolError) as caught:
        await _with_cdp_timeout(never(), timeout=0.01, instance_id="dead-1")

    assert type(caught.value) is ToolError
    assert str(caught.value).startswith("CDP operation timed out after 0s")
    assert "(instance dead-1)" in str(caught.value)


@pytest.mark.asyncio
async def test_cdp_timeout_does_not_convert_cancellation():
    """Cancellation is NOT a tool failure — it must still propagate unconverted.

    ``CancelledError`` is a ``BaseException``; turning it into a ``ToolError``
    would let a cancelled call be reported as a product answer.
    """

    async def cancelled():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError) as caught:
        await _with_cdp_timeout(cancelled(), timeout=5)

    assert not isinstance(caught.value, ToolError)


# ---------------------------------------------------------------------------
# The rest of the dom_handler interaction surface — same defect, same fix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "args", "prefix"),
    [
        ("click_element", (SELECTOR,), "Failed to click element: "),
        ("paste_text", (SELECTOR, "hello"), "Failed to paste text: "),
        ("get_element_state", (SELECTOR,), "Failed to get element state: "),
    ],
)
async def test_missing_element_raises_tool_error(no_element, method, args, prefix):
    with pytest.raises(ToolError) as caught:
        await getattr(DOMHandler, method)(_RaisingTab(), *args)

    assert type(caught.value) is ToolError
    assert str(caught.value).startswith(prefix)
    assert SELECTOR in str(caught.value)


@pytest.mark.asyncio
async def test_select_option_missing_element_raises_tool_error(no_element):
    with pytest.raises(ToolError) as caught:
        await DOMHandler.select_option(_RaisingTab(), SELECTOR, value="a")

    assert type(caught.value) is ToolError
    assert f"Select element not found: {SELECTOR}" in str(caught.value)


@pytest.mark.asyncio
async def test_select_option_without_criteria_raises_tool_error(any_element):
    """An argument guard, not a lookup failure — equally caller-actionable."""
    with pytest.raises(ToolError) as caught:
        await DOMHandler.select_option(_RaisingTab(), SELECTOR)

    assert type(caught.value) is ToolError
    assert "No selection criteria provided" in str(caught.value)


@pytest.mark.asyncio
async def test_upload_file_without_paths_raises_tool_error():
    with pytest.raises(ToolError) as caught:
        await DOMHandler.upload_file(_RaisingTab(), SELECTOR, [])

    assert type(caught.value) is ToolError
    assert "No file paths provided" in str(caught.value)


@pytest.mark.asyncio
async def test_upload_file_missing_path_raises_tool_error(tmp_path):
    absent = tmp_path / "not-here.txt"
    with pytest.raises(ToolError) as caught:
        await DOMHandler.upload_file(_RaisingTab(), SELECTOR, [str(absent)])

    assert type(caught.value) is ToolError
    assert "File not found: " in str(caught.value)


@pytest.mark.asyncio
async def test_upload_file_wrong_element_raises_tool_error(monkeypatch, tmp_path):
    """Pointing the selector at a non-input is the caller's mistake, not a crash."""
    present = tmp_path / "payload.txt"
    present.write_text("x", encoding="utf-8")

    class _Div:
        tag_name = "div"
        attrs: ClassVar[dict[str, str]] = {}

    async def _found(*args, **kwargs):
        return _Div()

    monkeypatch.setattr(dom_handler_mod, "resolve_element", _found)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.upload_file(_RaisingTab(), SELECTOR, [str(present)])

    assert type(caught.value) is ToolError
    assert "not a file input" in str(caught.value)


@pytest.mark.asyncio
async def test_execute_script_transport_failure_raises_tool_error():
    """The CDP call itself failing — distinct from a script that THREW (F-795)."""
    with pytest.raises(ToolError) as caught:
        await DOMHandler.execute_script(_RaisingTab(), "1 + 1")

    assert type(caught.value) is ToolError
    assert str(caught.value).startswith("Failed to execute script: ")


@pytest.mark.asyncio
async def test_function_body_retry_transport_failure_raises_tool_error():
    """The F-812 wrapped retry reports its failure with the same type."""
    with pytest.raises(ToolError) as caught:
        await DOMHandler._evaluate_as_function_body(_RaisingTab(), "return 1")

    assert type(caught.value) is ToolError
    assert str(caught.value).startswith("Failed to execute script: ")


@pytest.mark.asyncio
async def test_get_page_content_failure_raises_tool_error():
    with pytest.raises(ToolError) as caught:
        await DOMHandler.get_page_content(_RaisingTab())

    assert type(caught.value) is ToolError
    assert str(caught.value).startswith("Failed to get page content: ")


@pytest.mark.asyncio
async def test_scroll_page_invalid_direction_raises_tool_error():
    with pytest.raises(ToolError) as caught:
        await DOMHandler.scroll_page(_RaisingTab(), direction="sideways")

    assert type(caught.value) is ToolError
    assert "Invalid scroll direction: sideways" in str(caught.value)


# ---------------------------------------------------------------------------
# query_elements: one canonical error, not two nested ones
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_elements_passes_a_resolution_tool_error_through(monkeypatch):
    """A ``ToolError`` from the resolution seam surfaces unwrapped.

    ``element_resolution`` raises its own ``ToolError`` for a recovered-but-
    persistent nodriver handler race, and that error already names the selector.
    The generic wrap would spell the selector a second time and bury the
    resolution layer's diagnosis, so it must pass through identically — asserted
    on object identity, which no re-wrap can fake. The error is injected here
    rather than provoked, so this pins dom_handler's contract without coupling to
    element_resolution's wording.
    """
    canonical = ToolError(f"selector {SELECTOR!r} never resolved: stale node")

    async def _raise(*args, **kwargs):
        raise canonical

    monkeypatch.setattr(dom_handler_mod, "resolve_elements", _raise)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.query_elements(_RaisingTab(), SELECTOR)

    assert caught.value is canonical
    assert str(caught.value).count(SELECTOR) == 1
    assert "Failed to query elements" not in str(caught.value)


@pytest.mark.asyncio
async def test_query_elements_still_wraps_a_foreign_failure(monkeypatch):
    """The guard against over-correcting: a non-ToolError is still wrapped.

    Only errors already in the convention pass through; anything else still gets
    the operation name and the selector it failed on.
    """

    async def _raise(*args, **kwargs):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(dom_handler_mod, "resolve_elements", _raise)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.query_elements(_RaisingTab(), SELECTOR)

    assert type(caught.value) is ToolError
    assert str(caught.value).startswith("Failed to query elements for selector ")
    assert "connection lost" in str(caught.value)


# ---------------------------------------------------------------------------
# Why the type matters: what Sentry does with the result
# ---------------------------------------------------------------------------


def _hint_for(exception):
    return {"exc_info": (type(exception), exception, exception.__traceback__)}


@pytest.mark.asyncio
async def test_the_typed_failure_is_dropped_before_sentry(no_element):
    """The whole point: an expected failure now classifies as expected.

    The chain is ``ToolError("Failed to type text: ...")`` raised while handling
    ``ToolError("Element not found: ...")`` — every link ours, so the filter
    drops it instead of paging a maintainer with the product working correctly.
    """
    with pytest.raises(ToolError) as caught:
        await DOMHandler.type_text(_RaisingTab(), SELECTOR, "hello")

    assert observability._is_expected_tool_failure({}, _hint_for(caught.value))


@pytest.mark.asyncio
async def test_a_real_bug_under_the_wrap_still_ships(monkeypatch):
    """The guard on over-dropping: a genuine bug inside the chain still ships.

    ``resolve_element`` raising ``AttributeError`` is an interpreter-level defect,
    and the re-wrap being a ``ToolError`` must not hide it — the filter keeps any
    chain with a link that is not ours.
    """

    async def _boom(*args, **kwargs):
        raise AttributeError("'NoneType' object has no attribute 'send_keys'")

    monkeypatch.setattr(dom_handler_mod, "resolve_element", _boom)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.type_text(_RaisingTab(), SELECTOR, "hello")

    assert not observability._is_expected_tool_failure({}, _hint_for(caught.value))
