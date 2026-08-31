"""Pins for F-812: a top-level ``return`` in execute_script is not an error.

``return document.title;`` is how an agent writes a script, and it was the #1
error by volume on the wire (Sentry STEALTH-CHROME-DEVTOOLS-MCP-P, 205 events):
a bare CDP ``Runtime.evaluate`` is an EXPRESSION evaluator, so Chrome answers
with ``SyntaxError: Illegal return statement`` and ``tool_errors._require_js_value``
turns that into a ``ToolError`` (correctly — F-795 — but for a script that has
nothing wrong with it).

The fix is a retry keyed on that ONE error, at the one eval home
(``DOMHandler.execute_script``): evaluate as written, and only if Chrome names an
illegal return, evaluate once more as a function body. What these tests protect
is the *narrowness* of it — a script that fails for any other reason keeps the
old error and is never evaluated twice, so nothing that already worked acquires a
second execution or a changed meaning.

Since F-832 the eval seam is a raw ``Runtime.evaluate`` (``return_by_value``)
rather than nodriver's ``Tab.evaluate``, so the attempts are counted off
``tab.cdp_frames`` and the canned answers are ``(result, exceptionDetails)``
pairs. The retry's *behaviour* — one error, one extra eval, never a wrapper on
the happy path — is unchanged, which is what this module exists to hold still.

Hermetic: a ``FakeTab`` and real ``nodriver`` CDP records, no browser.
"""

import pytest

from fakes import FakeBrowserManager, FakeTab, call_tool, js_result, js_threw
from stealth_chrome_devtools_mcp.embedded import server
from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

# The wrapper the retry evaluates is the only expression containing "=>", which
# is what lets a FakeTab's evaluate_map answer the two attempts differently.
WRAPPED = "=>"

ILLEGAL_RETURN = js_threw("SyntaxError: Illegal return statement")


def _expressions(tab: FakeTab) -> list[str]:
    """The JS of every ``Runtime.evaluate`` the tab was sent, in order."""
    return [frame["params"]["expression"] for frame in tab.cdp_frames]


@pytest.mark.asyncio
async def test_top_level_return_is_retried_as_a_function_body_and_succeeds():
    # First attempt (bare) is the illegal-return SyntaxError; the retry carries
    # the wrapper, so the fake answers it with the script's real value.
    tab = FakeTab(
        evaluate_result=ILLEGAL_RETURN,
        evaluate_map={WRAPPED: js_result("Fake Page", type_="string")},
    )

    result = await DOMHandler.execute_script(tab, "return document.title;")

    assert result == "Fake Page"
    assert len(tab.cdp_frames) == 2
    assert _expressions(tab)[0] == "return document.title;", "first try is verbatim"
    assert "return document.title;" in _expressions(tab)[1]


@pytest.mark.asyncio
async def test_a_working_script_is_evaluated_exactly_once():
    """The retry is dead weight on the happy path — it must never fire there."""
    tab = FakeTab(evaluate_result=js_result(42, type_="number"))

    assert await DOMHandler.execute_script(tab, "6 * 7") == 42
    assert len(tab.cdp_frames) == 1


@pytest.mark.asyncio
async def test_a_script_failing_for_another_reason_keeps_its_error_and_one_eval():
    """No double execution for ordinary failures: a script with a side effect
    that throws afterwards must not have that side effect applied twice."""
    tab = FakeTab(evaluate_result=js_threw("ReferenceError: nope is not defined"))

    with pytest.raises(ToolError) as raised:
        await DOMHandler.execute_script(tab, "nope.x = 1; nope.boom()")

    assert str(raised.value) == (
        "Script raised an exception: ReferenceError: nope is not defined"
    )
    assert len(tab.cdp_frames) == 1


@pytest.mark.asyncio
async def test_a_retry_that_fails_reports_the_scripts_own_error_not_ours():
    """``return nope.x`` is BOTH an illegal return (attempt 1) and a
    ReferenceError (attempt 2). The caller must be told about their undefined
    variable — the illegal-return complaint is an artifact of how we evaluated
    it, and repeating it would send them fixing the wrong thing."""
    tab = FakeTab(
        evaluate_result=ILLEGAL_RETURN,
        evaluate_map={WRAPPED: js_threw("ReferenceError: nope is not defined")},
    )

    with pytest.raises(ToolError) as raised:
        await DOMHandler.execute_script(tab, "return nope.x;")

    message = str(raised.value)
    assert "ReferenceError: nope is not defined" in message
    assert "Illegal return" not in message
    # The wrapper is visible in the JS stack, so the message admits to it.
    assert "wrapper" in message and "top-level 'return'" in message


@pytest.mark.asyncio
async def test_the_args_path_is_untouched():
    """A script WITH args was already wrapped in a function before this fix, so
    its top-level return was always legal — it must not gain a second eval."""
    tab = FakeTab(evaluate_result=js_result("ok", type_="string"))

    assert await DOMHandler.execute_script(tab, "return 1;", args=[7]) == "ok"
    assert len(tab.cdp_frames) == 1
    assert _expressions(tab)[0] == "(function() { return 1; })(7)"


@pytest.mark.asyncio
async def test_declarations_still_reach_the_page_unwrapped():
    """The reason for retrying instead of always wrapping: a script that
    declares a global is evaluated as written, so the declaration lands on the
    page instead of becoming a local of a wrapper that is thrown away."""
    tab = FakeTab(evaluate_result=js_result(None, type_="undefined"))

    await DOMHandler.execute_script(tab, "var installed = 1; function f() {}")

    assert _expressions(tab) == ["var installed = 1; function f() {}"]


@pytest.mark.asyncio
async def test_the_tool_returns_the_value_of_a_top_level_return(monkeypatch):
    """End of the path the Sentry issue was reported from: the MCP tool answers
    with the script's value instead of raising."""
    tab = FakeTab(
        evaluate_result=ILLEGAL_RETURN,
        evaluate_map={WRAPPED: js_result("Fake Page", type_="string")},
    )
    monkeypatch.setattr(server, "browser_manager", FakeBrowserManager(tabs={"i1": tab}))

    result = await call_tool(
        server, "execute_script", instance_id="i1", script="return document.title;"
    )

    assert result == {"success": True, "result": "Fake Page", "error": None}
