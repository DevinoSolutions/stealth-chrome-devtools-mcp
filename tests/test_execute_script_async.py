"""Pins for F-883: ``execute_script`` awaits, and says so truthfully.

Measured through the MCP against 2.1.8 (Chrome 152, nodriver 0.47), which is
what these pins hold shut:

* ``const v = await new Promise(r => setTimeout(() => r(42), 100)); return v;``
  → ``ToolError: SyntaxError: await is only valid in async functions``. The
  tool's own docstring forbade every blocking wait and then rejected the only
  non-blocking one there is.
* ``return fetch(u).then(r => r.text());`` → ``{"success": true, "result": {}}``.
  ``returnByValue`` serialized the *Promise object*, which has no own enumerable
  properties, so the answer was indistinguishable from a script that genuinely
  returned ``{}``.
* ``return Promise.reject(new Error('boom'));`` → the same ``{}``, also
  ``success: true``. A failure reported as a success is the worst of the three.

Three mechanisms answer all of it, and this module pins each one at the seam
rather than at the value, because a ``FakeTab`` cannot resolve a Promise — only
Chrome can, and ``tests/test_e2e_execute_script_async.py`` is where the resolved
VALUES are measured:

1. ``await_promise=True`` rides on THE one ``Runtime.evaluate``;
2. the wrapper the F-812 retry evaluates is an ``async`` function, and so is the
   one the ``args`` path has always used;
3. the retry fires for Chrome's top-level-``await`` complaint as well as its
   illegal-``return`` one — and for nothing else, which is the half of F-812
   that must not loosen.

Hermetic: a ``FakeTab`` and real ``nodriver`` CDP records, no browser.
"""

import pytest

from fakes import (
    FakeBrowserManager,
    FakeTab,
    call_tool,
    js_promise,
    js_result,
    js_threw,
)
from stealth_chrome_devtools_mcp.embedded import server
from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

# The wrapper the retry evaluates is the only expression containing "=>", which
# is what lets a FakeTab's evaluate_map answer the two attempts differently.
WRAPPED = "=>"

ILLEGAL_RETURN = js_threw("SyntaxError: Illegal return statement")
TOP_LEVEL_AWAIT = js_threw(
    "SyntaxError: await is only valid in async functions and the top level "
    "bodies of modules"
)


def _expressions(tab: FakeTab) -> list[str]:
    """The JS of every ``Runtime.evaluate`` the tab was sent, in order."""
    return [frame["params"]["expression"] for frame in tab.cdp_frames]


# ---------------------------------------------------------------------------
# (1) the send — awaitPromise is what makes a Promise a value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_one_evaluate_asks_chrome_to_await_the_promise():
    """Without ``awaitPromise`` Chrome answers with the Promise OBJECT, and
    ``returnByValue`` serializes that to ``{}`` — the reported defect."""
    tab = FakeTab(evaluate_result=js_result(1, type_="number"))

    await DOMHandler.execute_script(tab, "Promise.resolve(1)")

    params = tab.cdp_frames[0]["params"]
    assert params["awaitPromise"] is True
    assert params["returnByValue"] is True


@pytest.mark.asyncio
async def test_the_retry_send_asks_for_it_too():
    """Both sends, or a script with a top-level ``return`` keeps the defect."""
    tab = FakeTab(
        evaluate_result=ILLEGAL_RETURN,
        evaluate_map={WRAPPED: js_result("ok", type_="string")},
    )

    # No "=>" in the source itself, or the map would answer the FIRST attempt.
    await DOMHandler.execute_script(tab, "return fetch('/x').then(readText);")

    assert [f["params"]["awaitPromise"] for f in tab.cdp_frames] == [True, True]


@pytest.mark.asyncio
async def test_the_csp_and_user_gesture_flags_are_not_lost_to_the_change():
    """F-832 carried both over from nodriver's own call; F-883 must not drop
    them while adding a third flag beside them."""
    tab = FakeTab(evaluate_result=js_result(1, type_="number"))

    await DOMHandler.execute_script(tab, "1")

    params = tab.cdp_frames[0]["params"]
    assert params["userGesture"] is True
    assert params["allowUnsafeEvalBlockedByCSP"] is True
    # returnByValue's override twin must still be absent (F-832).
    assert "serializationOptions" not in params


# ---------------------------------------------------------------------------
# (2) the wrapper — async, on both paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_retry_wrapper_is_an_async_function():
    """A non-async wrapper is what made a top-level ``await`` a SyntaxError even
    after F-812's retry had already decided to wrap the source."""
    tab = FakeTab(
        evaluate_result=ILLEGAL_RETURN,
        evaluate_map={WRAPPED: js_result("v", type_="string")},
    )

    await DOMHandler.execute_script(tab, "return document.title;")

    assert _expressions(tab)[1].startswith("(async () => {")


@pytest.mark.asyncio
async def test_the_args_wrapper_is_an_async_function():
    """The ``args`` path wraps immediately (its body is already a function
    body), so ``await`` has to be legal there by the same means — otherwise the
    tool supports async on one of its two call shapes."""
    tab = FakeTab(evaluate_result=js_result("ok", type_="string"))

    assert await DOMHandler.execute_script(tab, "return 1;", args=[7]) == "ok"

    assert _expressions(tab) == ["(async function() { return 1; })(7)"]


@pytest.mark.asyncio
async def test_a_declaration_still_reaches_the_page_unwrapped():
    """The reason this is a retry and not an unconditional async wrapper: a
    top-level ``var``/``function`` must still land on the PAGE, not become a
    local of a wrapper that is thrown away."""
    tab = FakeTab(evaluate_result=js_result(None, type_="undefined"))

    await DOMHandler.execute_script(tab, "var installed = 1; function f() {}")

    assert _expressions(tab) == ["var installed = 1; function f() {}"]


# ---------------------------------------------------------------------------
# (3) the trigger — two complaints, and not one more
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_top_level_await_is_retried_as_an_async_function_body():
    tab = FakeTab(
        evaluate_result=TOP_LEVEL_AWAIT,
        evaluate_map={WRAPPED: js_result({"awaited": 42})},
    )

    result = await DOMHandler.execute_script(
        tab, "const v = await Promise.resolve(42); return {awaited: v};"
    )

    assert result == {"awaited": 42}
    assert len(tab.cdp_frames) == 2
    assert _expressions(tab)[0] == (
        "const v = await Promise.resolve(42); return {awaited: v};"
    ), "first try is verbatim"


@pytest.mark.asyncio
async def test_the_retry_message_names_which_complaint_sent_it_round():
    """A caller told "top-level 'return'" about an ``await`` goes fixing the
    wrong line, so the reason is read off the complaint rather than assumed."""
    tab = FakeTab(
        evaluate_result=TOP_LEVEL_AWAIT,
        evaluate_map={WRAPPED: js_threw("ReferenceError: nope is not defined")},
    )

    with pytest.raises(ToolError) as raised:
        await DOMHandler.execute_script(tab, "await nope();")

    message = str(raised.value)
    assert "ReferenceError: nope is not defined" in message
    assert "top-level 'await'" in message
    assert "top-level 'return'" not in message


@pytest.mark.asyncio
async def test_an_illegal_return_still_names_the_return():
    tab = FakeTab(
        evaluate_result=ILLEGAL_RETURN,
        evaluate_map={WRAPPED: js_threw("ReferenceError: nope is not defined")},
    )

    with pytest.raises(ToolError) as raised:
        await DOMHandler.execute_script(tab, "return nope.x;")

    assert "top-level 'return'" in str(raised.value)
    assert "top-level 'await'" not in str(raised.value)


@pytest.mark.asyncio
async def test_any_other_failure_is_still_evaluated_exactly_once():
    """F-812's narrowness, unchanged: a script with a side effect that throws
    afterwards must not have that side effect applied twice."""
    tab = FakeTab(evaluate_result=js_threw("TypeError: x is not a function"))

    with pytest.raises(ToolError):
        await DOMHandler.execute_script(tab, "side.effect(); x()")

    assert len(tab.cdp_frames) == 1


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("SyntaxError: Illegal return statement", "top-level 'return'"),
        (
            "SyntaxError: await is only valid in async functions and the top "
            "level bodies of modules",
            "top-level 'await'",
        ),
        ("ReferenceError: nope is not defined", None),
        ("SyntaxError: Unexpected token '}'", None),
        ("", None),
    ],
)
def test_the_complaint_table_is_the_whole_trigger(message, expected):
    # Imported inside the test on purpose: the module is F-883's own, so a run
    # against the unfixed tree must RED on the assertions rather than on a
    # collection-time ImportError that says nothing about behaviour.
    from stealth_chrome_devtools_mcp.embedded import script_evaluation

    assert script_evaluation._function_body_reason(message) == expected


# ---------------------------------------------------------------------------
# (4) a rejection is a failure, and its reason is the page's
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_resolved_promise_answers_with_its_value_not_an_empty_object():
    """``js_promise`` answers the way Chrome does: the settlement only when
    ``awaitPromise`` was asked for, and the empty by-value serialization of the
    Promise OBJECT when it was not (``fakes.JsPromise``). So this node fails on
    2.1.8 with the reported ``{}`` rather than on a canned value."""
    tab = FakeTab(evaluate_result=js_promise({"ok": 1}))

    assert await DOMHandler.execute_script(tab, "Promise.resolve({ok: 1})") == {"ok": 1}


@pytest.mark.asyncio
async def test_a_rejection_raises_instead_of_answering_with_an_empty_object():
    """The worst of the three shapes: 2.1.8 answered a REJECTION with
    ``{"success": true, "result": {}}``. ``awaitPromise`` reports it as
    ``exceptionDetails`` — the shape F-795's reader already refuses — so the one
    flag is what turns a silent success into a raise."""
    tab = FakeTab(evaluate_result=js_promise(rejects="Error: boom-reason"))

    with pytest.raises(ToolError) as raised:
        await DOMHandler.execute_script(tab, "Promise.reject(new Error('boom-reason'))")

    assert "boom-reason" in str(raised.value)


@pytest.mark.asyncio
async def test_a_rejection_reason_is_clamped_because_the_page_authored_it():
    """``Promise.reject(new Error(<anything>))`` is unbounded and page-authored,
    and the message reaches the client, the debug ring and Sentry at once."""
    from stealth_chrome_devtools_mcp.embedded.tool_errors import (
        JS_ERROR_CHARS,
        JS_ERROR_TRUNCATION,
    )

    tab = FakeTab(evaluate_result=js_promise(rejects="Error: " + "x" * 5000))

    with pytest.raises(ToolError) as raised:
        await DOMHandler.execute_script(tab, "Promise.reject(new Error('x'))")

    message = str(raised.value)
    assert message.endswith(JS_ERROR_TRUNCATION)
    assert message.count("x") <= JS_ERROR_CHARS


@pytest.mark.asyncio
async def test_a_short_reason_is_not_marked_as_truncated():
    tab = FakeTab(evaluate_result=js_threw("Error: short"))

    with pytest.raises(ToolError) as raised:
        await DOMHandler.execute_script(tab, "throw new Error('short')")

    assert str(raised.value) == "Script raised an exception: Error: short"


# ---------------------------------------------------------------------------
# (5) the tool boundary — what a caller actually receives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_tool_answers_with_the_awaited_value(patched_server):
    # No "=>" in the script's own text, or the map would answer the FIRST
    # attempt and the node would be green without a retry ever happening.
    tab = FakeTab(
        evaluate_result=TOP_LEVEL_AWAIT,
        evaluate_map={WRAPPED: js_result({"fetched": "hello"})},
    )
    patched_server(browser_manager=FakeBrowserManager(tabs={"i1": tab}))

    result = await call_tool(
        server,
        "execute_script",
        instance_id="i1",
        script="const t = await fetch('/x').then(readText); return {fetched: t};",
    )

    assert result == {"success": True, "result": {"fetched": "hello"}, "error": None}


@pytest.mark.asyncio
async def test_the_tool_raises_for_a_rejection_rather_than_reporting_success(
    patched_server,
):
    tab = FakeTab(evaluate_result=js_promise(rejects="Error: es-rejected"))
    patched_server(browser_manager=FakeBrowserManager(tabs={"i1": tab}))

    with pytest.raises(ToolError) as raised:
        await call_tool(
            server,
            "execute_script",
            instance_id="i1",
            script="Promise.reject(new Error('es-rejected'))",
        )

    assert "es-rejected" in str(raised.value)


@pytest.mark.asyncio
async def test_the_docstring_no_longer_forbids_what_the_tool_supports():
    """The docstring was the other half of the defect: it demanded ``await
    fetch(url)`` and the tool rejected it. A doc claim nothing enforces drifts,
    so the claim is pinned to the mechanism that makes it true."""
    from stealth_chrome_devtools_mcp.embedded.tool_sections import element_interaction

    doc = element_interaction.execute_script.__doc__
    assert "Top-level `await` works" in doc
    assert "A Promise that REJECTS raises with its reason" in doc
    assert "A script that never settles is killed at `timeout_ms`" in doc
