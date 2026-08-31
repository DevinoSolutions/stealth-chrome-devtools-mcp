"""Pins for F-832 (GitHub issue #17): execute_script returns plain JSON values.

``execute_script`` used to go through ``nodriver``'s ``Tab.evaluate``, which asks
Chrome for a **deep-serialized** result (``SerializationOptions(serialization=
"deep", max_depth=10)``) and then reads it back with two truthiness tests::

    if remote_object.value:                      # returnByValue path
    if remote_object.deep_serialized_value:      # the default path

Two defects fall out of that, and this module pins the fix for both.

1. **The envelope.** A deep-serialized object is a BiDi-shaped graph of
   ``{"type": ..., "value": ...}`` nodes, not the script's JSON value, and it is
   capped at depth 10 — so a caller asking for an object got a CDP envelope to
   unwrap (issue #17) or a truncated one, and anything Chrome declined to
   serialize came back as a bare ``RemoteObject`` husk.
2. **The falsy trap.** ``if remote_object.value:`` cannot tell "no value" from a
   value that is *legitimately falsy*. ``0``, ``""``, ``false`` and ``null`` all
   failed that test, so the read fell through to ``return remote_object`` and the
   husk reached the caller in place of the number/string/boolean they asked for.

The fix evaluates through a raw ``Runtime.evaluate`` with ``return_by_value=True``
and reads the answer with an explicit **None-vs-absent** check instead of a
truthiness one. The chosen mapping (documented in
``audit/stage2/finding_F832_execute_script_shallow_serialization.md``):

===========================  ==========================================
Chrome's ``RemoteObject``    ``execute_script`` returns
===========================  ==========================================
``type="undefined"``         ``None`` (Python has one nullish)
``subtype="null"``           ``None``
``value`` present            that value, **verbatim**, falsy or not
no ``value``, but an
``unserializableValue``      that token as a string (``"Infinity"``, …)
nothing serializable         the ``description`` string, never the husk
===========================  ==========================================

``undefined`` and ``null`` therefore agree at the boundary but are reached by two
*distinct* branches, and neither is reached by the accidental "the value was
falsy so there must not be one" fallthrough that was the bug.

Hermetic: a ``FakeTab`` answering the real ``Runtime.evaluate`` command with
records built from ``nodriver``'s own ``RemoteObject`` / ``ExceptionDetails``
constructors. No browser.
"""

import pytest

from fakes import FakeBrowserManager, FakeTab, call_tool, js_result, js_threw
from stealth_chrome_devtools_mcp.embedded import server
from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

# The F-812 retry wrapper is the only expression containing "=>", which is what
# lets one tab answer the two attempts differently.
WRAPPED = "=>"

ILLEGAL_RETURN = js_threw("SyntaxError: Illegal return statement")


# ---------------------------------------------------------------------------
# (a) deep values — the issue #17 report
# ---------------------------------------------------------------------------

NESTED = {
    "user": {"id": 7, "name": "ada", "tags": ["a", "b"]},
    "rows": [{"k": 1}, {"k": 2}, {"k": [3, {"deep": True}]}],
    "meta": {"page": {"of": {"of": {"of": {"leaf": "bottom"}}}}},
}


@pytest.mark.asyncio
async def test_a_nested_object_comes_back_whole():
    """The reported defect: an object arrived as a CDP envelope, not its JSON."""
    tab = FakeTab(evaluate_result=js_result(NESTED))

    assert await DOMHandler.execute_script(tab, "({...})") == NESTED


@pytest.mark.asyncio
async def test_an_array_of_objects_comes_back_whole():
    rows = [{"i": i, "sub": {"x": [i, i + 1]}} for i in range(5)]
    tab = FakeTab(evaluate_result=js_result(rows))

    assert await DOMHandler.execute_script(tab, "rows") == rows


@pytest.mark.asyncio
async def test_the_evaluate_command_asks_chrome_for_the_value_itself():
    """``returnByValue`` is what makes the answer plain JSON.

    Also pinned: no ``serializationOptions``. CDP documents it as *overriding*
    ``returnByValue``, so sending both would silently reinstate the deep
    envelope this fix exists to remove.
    """
    tab = FakeTab(evaluate_result=js_result(1))

    await DOMHandler.execute_script(tab, "1")

    assert tab.send_calls == ["evaluate"]
    params = tab.cdp_frames[0]["params"]
    assert tab.cdp_frames[0]["method"] == "Runtime.evaluate"
    assert params["returnByValue"] is True
    assert "serializationOptions" not in params
    # nodriver's own evaluate sent both; dropping either would regress a page
    # whose CSP blocks unsafe-eval, or a click handler gated on user activation.
    assert params["allowUnsafeEvalBlockedByCSP"] is True
    assert params["userGesture"] is True


# ---------------------------------------------------------------------------
# (b) the falsy trap — the reason the fix reads None-vs-absent, not truthiness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param(js_result(0, type_="number"), 0, id="zero"),
        pytest.param(js_result("", type_="string"), "", id="empty-string"),
        pytest.param(js_result(False, type_="boolean"), False, id="false"),
        pytest.param(js_result(None, subtype="null"), None, id="null"),
    ],
)
@pytest.mark.asyncio
async def test_a_legitimately_falsy_value_survives(answer, expected):
    """``0`` / ``""`` / ``false`` / ``null`` are answers, not the absence of one.

    Under the old truthiness read every one of these fell through to the husk.
    """
    tab = FakeTab(evaluate_result=answer)

    result = await DOMHandler.execute_script(tab, "the_falsy_thing")

    assert result == expected
    assert type(result) is type(expected), "the JSON type is part of the answer"


@pytest.mark.asyncio
async def test_a_falsy_value_reaches_the_tool_payload(monkeypatch):
    """End of the path a caller sees: ``success`` true and the value intact."""
    tab = FakeTab(evaluate_result=js_result(0, type_="number"))
    monkeypatch.setattr(server, "browser_manager", FakeBrowserManager(tabs={"i1": tab}))

    result = await call_tool(
        server, "execute_script", instance_id="i1", script="document.scrollTop"
    )

    assert result == {"success": True, "result": 0, "error": None}


# ---------------------------------------------------------------------------
# (c) F-795 — a throwing script still raises, through the ONE guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_throwing_script_still_raises_with_the_js_error():
    tab = FakeTab(evaluate_result=js_threw("TypeError: x.y is not a function"))

    with pytest.raises(ToolError) as raised:
        await DOMHandler.execute_script(tab, "x.y()")

    assert str(raised.value) == (
        "Script raised an exception: TypeError: x.y is not a function"
    )
    assert len(tab.cdp_frames) == 1, "an ordinary throw is never evaluated twice"


@pytest.mark.asyncio
async def test_exception_details_win_even_when_a_result_object_came_back():
    """Chrome answers a throw with BOTH a result object and exceptionDetails.

    Reading the result first would report the exception's own ``RemoteObject`` as
    the script's value and call it a success — the F-795 defect, re-entering
    through the new transport.
    """
    remote_object, details = js_threw("Error: boom")
    tab = FakeTab(evaluate_result=(remote_object, details))

    with pytest.raises(ToolError) as raised:
        await DOMHandler.execute_script(tab, "throw new Error('boom')")

    assert "boom" in str(raised.value)


# ---------------------------------------------------------------------------
# (d) F-812 — the single retry on "Illegal return statement" is untouched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_single_retry_survives_the_new_transport_and_is_by_value_too():
    """``return payload;`` is the most common script an agent writes, so the
    retry — not the first attempt — is the path most values actually take. It
    must still fire exactly once, and it must ask for the value by value.

    The narrowness of the retry (one error, one extra eval, never a wrapper on
    the happy path) is pinned in ``test_execute_script_return_wrap.py``, which is
    F-812's home; this is the F-832 half only.
    """
    tab = FakeTab(
        evaluate_result=ILLEGAL_RETURN, evaluate_map={WRAPPED: js_result(NESTED)}
    )

    assert await DOMHandler.execute_script(tab, "return payload;") == NESTED

    expressions = [f["params"]["expression"] for f in tab.cdp_frames]
    assert len(expressions) == 2, "one retry, not a loop"
    assert expressions[0] == "return payload;", "first try is verbatim"
    assert WRAPPED in expressions[1]
    assert tab.cdp_frames[1]["params"]["returnByValue"] is True


# ---------------------------------------------------------------------------
# (e) undefined vs null vs "nothing serializable"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undefined_is_none():
    """A statement script (``el.click()``) evaluates to ``undefined``."""
    tab = FakeTab(evaluate_result=js_result(None, type_="undefined"))

    assert await DOMHandler.execute_script(tab, "el.click()") is None


@pytest.mark.asyncio
async def test_explicit_null_is_none_too_but_by_its_own_branch():
    """``document.querySelector('#absent')`` is ``null``, not ``undefined``.

    Both map to Python ``None`` — Python has one nullish — but each is reached by
    a named branch. Neither may be reached by "the value was falsy".
    """
    tab = FakeTab(evaluate_result=js_result(None, subtype="null"))

    assert await DOMHandler.execute_script(tab, "document.querySelector('#x')") is None


@pytest.mark.asyncio
async def test_an_unserializable_value_is_reported_as_its_token():
    """``Infinity`` / ``NaN`` / ``-0`` have no JSON form, so CDP sends
    ``unserializableValue`` instead of ``value``. That is a real answer and is
    distinguishable from ``null`` — it must not collapse to ``None``."""
    tab = FakeTab(
        evaluate_result=js_result(None, type_="number", unserializable_value="Infinity")
    )

    assert await DOMHandler.execute_script(tab, "1/0") == "Infinity"


@pytest.mark.asyncio
async def test_a_husk_is_described_never_returned_raw():
    """Defensive: a result Chrome could not send by value (a live DOM node, a
    cyclic object) must still compose into a JSON-serializable payload. The tool
    may not crash and may not hand a ``RemoteObject`` to the transport."""
    tab = FakeTab(
        evaluate_result=js_result(
            None, type_="object", subtype="node", description="div#app"
        )
    )

    assert await DOMHandler.execute_script(tab, "document.body.firstChild") == "div#app"


@pytest.mark.asyncio
async def test_a_result_object_chrome_did_not_send_at_all_is_none():
    """CDP promises a result, but a defensive read must survive its absence
    rather than raise an AttributeError out of the success path."""
    tab = FakeTab(evaluate_result=(None, None))

    assert await DOMHandler.execute_script(tab, "whatever") is None
