"""A tool that FAILED must not report success — F-802 (`navigate`) and F-795
(`execute_script`), against real headless Chrome.

Both defects had the same shape: the operation failed *at the browser* while
every Python-side step around it succeeded, so the tool assembled a payload
whose ``success`` said it worked. Neither is reachable from a hermetic fake —
the lie is produced by Chrome (an error-page commit) and by nodriver
(``Tab.evaluate`` RETURNING the CDP ``ExceptionDetails`` instead of raising) —
so every node here drives a real instance.

Three properties make these nodes evidence rather than smoke:

*The failure is guaranteed, not hoped for.* The unresolvable host uses the
``.invalid`` TLD, which RFC 6761 §6.4 reserves precisely so it can never
resolve; nothing here depends on a real domain staying broken or on the machine
being offline.

*The truthful half is asserted too.* An HTTP error status, a redirect, a
``data:`` URL and ``about:blank`` are NOT navigation failures, and a guard that
turned any of them into an error would be a worse defect than the one it fixed.
:func:`test_a_loaded_page_is_a_success_even_when_the_server_said_no` pins that
half in the same file as the fix.

*No wedge.* Each failure node then drives the SAME instance successfully. A
truthful error that leaves the instance unusable would only have moved the
defect (the shape of F-788/F-794), so recovery is part of what is asserted.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    sandbox_kwargs,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

pytestmark = integration_pytestmark()

# The harness bound. Never a product deadline: if THIS fires, the tool did not
# answer at all and the node fails by name instead of hanging the suite.
OUTER_BOUND = 90.0
# The product deadline, strictly inside OUTER_BOUND (navigate retries once).
NAV_TIMEOUT_MS = 20_000

# RFC 6761 §6.4 reserves `.invalid`: it is guaranteed never to resolve, so this
# is a DNS failure by specification rather than by luck.
UNRESOLVABLE_URL = "https://this-host-does-not-exist.invalid/"
DATA_URL = "data:text/html,<h1 id='t'>truthful-data-url</h1>"


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


@pytest.fixture()
async def instance():
    """One headless instance per node; the ``finally`` is the leak net."""
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        yield iid
    finally:
        with contextlib.suppress(Exception):
            await close(instance_id=iid)


async def _bounded(coro, what: str):
    """Await *coro* under the harness bound so a wedge fails by name."""
    try:
        return await asyncio.wait_for(coro, OUTER_BOUND)
    except TimeoutError as exc:
        raise AssertionError(
            f"{what} did not answer inside the {OUTER_BOUND}s outer bound"
        ) from exc


# ═══════════════════════════════════════════════════════════════════════════
# F-802 — navigate
# ═══════════════════════════════════════════════════════════════════════════
async def test_navigate_to_an_unresolvable_host_raises_instead_of_reporting_success(
    instance, fixture_app_server
):
    """F-802: a host that cannot resolve is a FAILED navigation.

    Chrome commits ``chrome-error://chromewebdata/`` and every Python-side step
    (the tab, the state update, the title read) still succeeds — which is
    exactly how the tool used to answer ``{"url": "chrome-error://chromewebdata/",
    "success": true}``. The error names both URLs, because "which navigation,
    and what did it land on" is what a caller needs to act.
    """
    navigate = get_fn("navigate")

    with pytest.raises(ToolError) as raised:
        await _bounded(
            navigate(
                instance_id=instance, url=UNRESOLVABLE_URL, timeout=NAV_TIMEOUT_MS
            ),
            "navigate to an unresolvable host",
        )

    message = str(raised.value)
    assert UNRESOLVABLE_URL in message, message
    assert "chrome-error://" in message, message
    assert "failed" in message, message

    # No wedge: the SAME instance still navigates and still runs script.
    result = await navigate_and_settle(instance, f"{fixture_app_server}/index.html")
    assert result["success"] is True, result
    assert (
        await eval_js(instance, "document.getElementById('sentinel').textContent")
        == "fixture-index-page"
    )


async def test_a_loaded_page_is_a_success_even_when_the_server_said_no(
    instance, fixture_app_server
):
    """The truthful half of F-802, so the fix cannot over-reach.

    A 503, a redirect whose final URL differs from the requested one, a
    ``data:`` URL and ``about:blank`` all LOAD. None of them is a Chrome-level
    navigation failure, and a guard that raised on any of them would break more
    than it fixed.
    """
    navigate = get_fn("navigate")

    served = await _bounded(
        navigate(
            instance_id=instance,
            url=f"{fixture_app_server}/status/503",
            timeout=NAV_TIMEOUT_MS,
        ),
        "navigate to a 503",
    )
    assert served["success"] is True, served
    assert served["url"].endswith("/status/503"), served

    redirected = await _bounded(
        navigate(
            instance_id=instance,
            url=f"{fixture_app_server}/redirect/start",
            timeout=NAV_TIMEOUT_MS,
        ),
        "navigate through a redirect",
    )
    assert redirected["success"] is True, redirected
    assert redirected["url"].endswith("/redirect/final"), (
        f"a redirect's final URL differing from the requested one is normal: "
        f"{redirected}"
    )

    for url in (DATA_URL, "about:blank"):
        loaded = await _bounded(
            navigate(instance_id=instance, url=url, timeout=NAV_TIMEOUT_MS),
            f"navigate to {url}",
        )
        assert loaded["success"] is True, loaded
        assert not loaded["url"].startswith("chrome-error://"), loaded


# ═══════════════════════════════════════════════════════════════════════════
# F-795 — execute_script
# ═══════════════════════════════════════════════════════════════════════════
async def test_execute_script_raises_when_the_script_throws(
    instance, fixture_app_server
):
    """F-795: a script that raises is a failure, whatever raised it.

    Both classes are driven: a SyntaxError the evaluator raises before the
    script runs and a runtime ``throw`` from inside a script that parsed fine.
    Both used to come back as ``success: true`` with the exception record
    standing in for the value.

    The SyntaxError specimen is genuinely malformed JS rather than the original
    top-level ``return`` (which is what found the defect) because that one is no
    longer a failure at all: F-812 retries it as a function body, asserted at
    the end of this node. F-795's invariant is untouched — only the example.
    """
    execute = get_fn("execute_script")
    await navigate_and_settle(instance, f"{fixture_app_server}/index.html")

    with pytest.raises(ToolError) as syntax_error:
        await _bounded(
            execute(instance_id=instance, script="] f795-not-js ["),
            "execute_script with malformed JS",
        )
    assert "SyntaxError" in str(syntax_error.value)

    with pytest.raises(ToolError) as runtime_error:
        await _bounded(
            execute(
                instance_id=instance,
                script="(function () { throw new Error('f795-boom'); })()",
            ),
            "execute_script with a runtime throw",
        )
    assert "f795-boom" in str(runtime_error.value)

    # No wedge: a valid script still runs on the same tab, and still reports the
    # success envelope its KEEP contract promises (DESIGN §9).
    ok = await _bounded(
        execute(instance_id=instance, script="1 + 1"),
        "execute_script after a thrown script",
    )
    assert ok == {"success": True, "result": 2, "error": None}, ok

    # F-812: the script an agent actually writes — a top-level ``return`` — is
    # a VALUE now, not the SyntaxError this node used to demonstrate F-795 with.
    returned = await _bounded(
        execute(instance_id=instance, script="return 'top-level-return';"),
        "execute_script with a top-level return",
    )
    assert returned == {
        "success": True,
        "result": "top-level-return",
        "error": None,
    }, returned
    assert (
        await eval_js(instance, "document.getElementById('sentinel').textContent")
        == "fixture-index-page"
    )
