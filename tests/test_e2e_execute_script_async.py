"""F-883 E2E — ``execute_script``'s async contract, against real Chrome.

The hermetic tier (``tests/test_execute_script_async.py``) pins the MECHANISM:
``awaitPromise`` on the one send, an ``async`` wrapper on both wrap paths, and a
retry keyed on exactly two compile complaints. It cannot pin the ANSWER, because
resolving a Promise is Chrome's job and a ``FakeTab`` can only hand back what a
test already wrote down. That is what this module measures.

Every node runs against ``/es_async.html`` from ``tests/fixture_routes.py`` — a
deliberately inert local page whose only contribution is three Promises the PAGE
owns (``esDelayed`` / ``esRejects`` / ``esNever``) and one nested literal
(``esNested``). A value that arrives from ``esDelayed`` is therefore proof the
tool waited for a resolution that did not exist when the script was sent, not
proof that a literal survived a round trip.

Determinism (plan §2.6): no fixed sleep is an oracle. The un-settling node reads
a CLOCK only to bound the answer it already has — the tool's own ``timeout_ms``
is the thing under test, so the wall time is asserted as a window around it
rather than waited out.
"""

from __future__ import annotations

import time

import pytest

from e2e_helpers import (
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    sandbox_kwargs,
    warmup_once,
)
from fixture_routes import ES_JSON_PAYLOAD, ES_REJECT_REASON, ES_VALUE_TOKEN
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

pytestmark = integration_pytestmark()


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def _on_the_page(base: str):
    """Spawn a browser sitting on ``/es_async.html``; yields ``(iid, execute)``."""
    spawn = get_fn("spawn_browser")
    spawned = await spawn(headless=True, **sandbox_kwargs())
    iid = spawned["instance_id"]
    await navigate_and_settle(iid, f"{base}/es_async.html")
    return iid, get_fn("execute_script")


async def _close(iid: str) -> None:
    await get_fn("close_instance")(instance_id=iid)


async def test_top_level_await_runs_instead_of_being_a_syntax_error(
    fixture_app_server,
):
    """2.1.8: ``ToolError: SyntaxError: await is only valid in async functions``.

    The value is produced by a ``setTimeout`` the page schedules, so it does not
    exist at the moment the script is sent — an answer carrying it is the whole
    proof."""
    iid, execute = await _on_the_page(fixture_app_server)
    try:
        answer = await execute(
            instance_id=iid,
            script=(
                "const v = await window.esDelayed(80, 'awaited-late');"
                " return {awaited: v, sentinel: document.title};"
            ),
        )
    finally:
        await _close(iid)

    assert answer == {
        "success": True,
        "result": {"awaited": "awaited-late", "sentinel": "es-async"},
        "error": None,
    }


async def test_a_returned_promise_answers_with_the_value_it_resolves_to(
    fixture_app_server,
):
    """2.1.8: ``{"success": true, "result": {}}`` — the Promise object, whose
    own enumerable properties are none, serialized by ``returnByValue``."""
    iid, execute = await _on_the_page(fixture_app_server)
    try:
        answer = await execute(
            instance_id=iid,
            script="return fetch('/es_value').then(r => r.json());",
        )
    finally:
        await _close(iid)

    assert answer["success"] is True
    assert answer["result"] == ES_JSON_PAYLOAD
    assert answer["result"]["k"] == ES_VALUE_TOKEN


async def test_a_rejected_promise_raises_carrying_its_reason(fixture_app_server):
    """2.1.8: the same ``{}``, with ``success: true`` — a failure reported as a
    success, which is the worst shape of the three."""
    iid, execute = await _on_the_page(fixture_app_server)
    try:
        with pytest.raises(ToolError) as raised:
            await execute(
                instance_id=iid,
                script=f"return window.esRejects('{ES_REJECT_REASON}');",
            )
    finally:
        await _close(iid)

    assert ES_REJECT_REASON in str(raised.value)


async def test_a_promise_that_never_settles_is_killed_at_timeout_ms(
    fixture_app_server,
):
    """The bound is the tool's own (``_clamp_timeout`` + ``_with_cdp_timeout``),
    not a second deadline inside the eval seam — so a Promise that never settles
    costs exactly what a blocking script costs, and the tab survives it."""
    iid, execute = await _on_the_page(fixture_app_server)
    try:
        started = time.monotonic()
        with pytest.raises(ToolError) as raised:
            await execute(
                instance_id=iid,
                script="return window.esNever();",
                timeout_ms=1500,
            )
        elapsed = time.monotonic() - started

        assert "timed out" in str(raised.value).lower()
        # A window around the bound, never a sleep: too fast means the timeout
        # did not come from timeout_ms, too slow means it did not bound anything.
        assert 1.0 <= elapsed <= 8.0, elapsed

        # The connection is still usable: the abandoned Promise is the PAGE's,
        # and nothing was left holding the renderer.
        after = await execute(instance_id=iid, script="return 6 * 7;")
        assert after == {"success": True, "result": 42, "error": None}
    finally:
        await _close(iid)


async def test_a_synchronous_throw_still_raises_with_its_own_message(
    fixture_app_server,
):
    """Unchanged by F-883, and pinned here because ``awaitPromise`` turns every
    failure into the same ``exceptionDetails`` shape — a sync throw must not
    start reading as a rejection or lose its text."""
    iid, execute = await _on_the_page(fixture_app_server)
    try:
        with pytest.raises(ToolError) as raised:
            await execute(instance_id=iid, script="throw new Error('sync-boom');")
    finally:
        await _close(iid)

    assert "sync-boom" in str(raised.value)


async def test_a_nested_object_and_array_survive_the_await_path(fixture_app_server):
    """F-832's answer must be byte-identical through the async wrapper: falsy
    leaves included, and no BiDi ``RemoteValue`` node anywhere in it."""
    iid, execute = await _on_the_page(fixture_app_server)
    try:
        direct = await execute(instance_id=iid, script="return window.esNested();")
        awaited = await execute(
            instance_id=iid,
            script="return await Promise.resolve(window.esNested());",
        )
    finally:
        await _close(iid)

    expected = {
        "user": {"id": 7, "tags": ["a", "b"]},
        "rows": [{"k": 1}, {"k": [3, {"deep": True}]}],
        "flags": {"zero": 0, "empty": "", "no": False, "nil": None},
    }
    assert direct["result"] == expected
    assert awaited["result"] == expected, "the await path must not change the shape"


async def test_args_still_arrive_and_may_now_be_awaited(fixture_app_server):
    """The ``args`` wrapper became ``async`` in the same change, so the two call
    shapes support the same JavaScript rather than one of them."""
    iid, execute = await _on_the_page(fixture_app_server)
    try:
        plain = await execute(
            instance_id=iid,
            script="return arguments[0] + arguments[1];",
            args=[3, 4],
        )
        awaited = await execute(
            instance_id=iid,
            script="return await window.esDelayed(40, arguments[0] * 2);",
            args=[21],
        )
    finally:
        await _close(iid)

    assert plain == {"success": True, "result": 7, "error": None}
    assert awaited == {"success": True, "result": 42, "error": None}
