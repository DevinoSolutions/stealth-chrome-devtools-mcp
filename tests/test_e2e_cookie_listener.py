"""F-902 in a REAL Chrome: a cookie in a reply must not kill the tab.

The hermetic half (``tests/test_cdp_transport.py``) models the two lines of
``Connection._listener`` and pins them against a cookie captured off Chrome
153's wire. This half exists because the hermetic half CANNOT be the evidence:
the whole finding is a claim about what a live Chrome sends, and
``tests/test_e2e_transport_cookies.py`` — which has driven a real cookie round
trip since W5 — stayed green for as long as the Chrome under it still sent
``Network.Cookie.sameParty``. A fixture is only ever as current as the browser
it was taken from; this file asks the browser.

What makes each node evidence is the SECOND call. ``get_cookies`` raising would
be a visible failure anyone would notice; what F-902 actually did was leave the
connection dead behind an answer that never came, so every node here proves the
tab still works AFTERWARDS. Before the fix, on Chrome 153.0.8010.50, the first
call never returned and the follow-up ``execute_script`` failed with "The
browser may have crashed or the connection dropped" — about a browser that was
fine.

All three shipped doors onto a ``Network.Cookie`` are covered, because they are
three different CDP commands and only the first was reported:
``get_cookies`` (``Network.getAllCookies`` / ``Network.getCookies``),
``get_instance_state`` (``browser_manager.get_page_state``) and
``clear_cookies(url=...)``.
"""

from __future__ import annotations

import asyncio

import pytest

from e2e_helpers import (
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    sandbox_kwargs,
    warmup_once,
)

pytestmark = integration_pytestmark()

#: Synthetic, and deliberately distinctive: the PII node greps for these exact
#: strings, so they must not be able to occur by coincidence.
_COOKIE_NAME = "f902_probe"
_COOKIE_VALUE = "synthetic-value"

#: A trivial round trip. Its job is only to prove the CDP connection still
#: resolves futures; before the fix this raised a ``ToolError`` naming a crash.
_STILL_ALIVE = "1 + 1"


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def _instance_on_a_page_with_a_cookie(base: str) -> tuple[str, callable]:
    """Spawn, land on a real http:// origin and put ONE cookie on it.

    A real origin is required: ``about:blank`` cannot hold a cookie, so a node
    that skipped the navigation would pass on an empty jar — which is exactly
    the case that never reproduced the defect.
    """
    spawn = get_fn("spawn_browser")
    set_cookie = get_fn("set_cookie")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    await navigate_and_settle(iid, f"{base}/interactions.html")
    assert await set_cookie(instance_id=iid, name=_COOKIE_NAME, value=_COOKIE_VALUE)
    return iid, get_fn("close_instance")


async def _still_usable(iid: str) -> None:
    """The assertion every node in this file turns on."""
    execute_script = get_fn("execute_script")
    answer = await execute_script(instance_id=iid, script=_STILL_ALIVE)
    assert answer["success"] is True, f"the tab is wedged: {answer}"
    assert answer["result"] == 2


async def test_get_cookies_answers_and_leaves_the_tab_usable(
    fixture_app_server, tmp_empty_root
):
    """The reported shape. Before the fix the first call never returned."""
    base = fixture_app_server
    iid, close = await _instance_on_a_page_with_a_cookie(base)
    try:
        cookies = await asyncio.wait_for(get_fn("get_cookies")(instance_id=iid), 30)

        names = [getattr(c, "name", None) or c.get("name") for c in cookies]
        assert _COOKIE_NAME in names, f"the cookie we set is missing: {names}"

        await _still_usable(iid)
    finally:
        await close(instance_id=iid)


async def test_get_cookies_for_one_url_answers_too(fixture_app_server, tmp_empty_root):
    """``urls=`` takes ``Network.getCookies``; the bare call takes
    ``Network.getAllCookies``. Two commands, two generated parsers, and the
    product picks between them on whether the caller passed a url — so one node
    leaves the other path unmeasured."""
    base = fixture_app_server
    iid, close = await _instance_on_a_page_with_a_cookie(base)
    try:
        cookies = await asyncio.wait_for(
            get_fn("get_cookies")(instance_id=iid, urls=[f"{base}/interactions.html"]),
            30,
        )

        names = [getattr(c, "name", None) or c.get("name") for c in cookies]
        assert _COOKIE_NAME in names

        await _still_usable(iid)
    finally:
        await close(instance_id=iid)


async def test_get_instance_state_is_not_degraded_by_a_cookie(
    fixture_app_server, tmp_empty_root
):
    """The widest door: ``get_page_state`` reads cookies, so the tool an agent
    calls to find out whether anything is WRONG both degraded (F-869's
    ``partial``) and killed the tab it was asked about."""
    base = fixture_app_server
    iid, close = await _instance_on_a_page_with_a_cookie(base)
    try:
        state = await asyncio.wait_for(
            get_fn("get_instance_state")(instance_id=iid), 30
        )

        assert state.get("partial") is not True, (
            f"degraded on a page with a cookie: {state.get('detail_error')}"
        )
        assert state.get("cookies"), "a page with a cookie reported none"

        await _still_usable(iid)
    finally:
        await close(instance_id=iid)


async def test_clear_cookies_for_one_url_answers_and_clears(
    fixture_app_server, tmp_empty_root
):
    """``clear_cookies(url=...)`` READS the jar first (to name each cookie for
    ``deleteCookies``), so it is a cookie-parsing door even though nothing about
    deleting a cookie needs one. The bare ``clear_cookies()`` is
    ``clearBrowserCookies`` and was never affected."""
    base = fixture_app_server
    url = f"{base}/interactions.html"
    iid, close = await _instance_on_a_page_with_a_cookie(base)
    try:
        assert await asyncio.wait_for(
            get_fn("clear_cookies")(instance_id=iid, url=url), 30
        )

        # Proved by RE-READING, never by the return value — which is a constant.
        remaining = await asyncio.wait_for(get_fn("get_cookies")(instance_id=iid), 30)
        names = [getattr(c, "name", None) or c.get("name") for c in remaining]
        assert _COOKIE_NAME not in names

        await _still_usable(iid)
    finally:
        await close(instance_id=iid)


#: Sentry's ``LoggingIntegration`` turns records at INFO and above into
#: breadcrumbs and ERROR into events; the durable file handler is INFO by
#: default (``settings.log_level``). So INFO is the line above which a record
#: LEAVES this machine, and the threshold this node measures at.
_SHIPPING_LEVEL = 20


async def test_no_cookie_name_or_value_reaches_a_shipped_log_line(
    fixture_app_server, tmp_empty_root, caplog
):
    """PII, against the real browser rather than a fixture.

    nodriver's own re-raise interpolates ``response['result']`` — the whole
    reply — into its message, and before the fix that escaped as an unretrieved
    task exception, which is the asyncio handler, the durable log and Sentry at
    once. MEASURED: the string carried the cookie's name AND value.

    Two surfaces, because they have two different thresholds:

    * anything OURS, at any level — ``stealth.*`` is the product's own voice
      and the only logger it attaches a file handler to;
    * anything at all at INFO or above — the line above which a record becomes
      a Sentry breadcrumb or a durable log line.

    Deliberately NOT "no logger anywhere at any level": ``nodriver.core
    .connection``'s ``_listener`` DEBUG-logs every raw reply verbatim, cookies
    included (``connection.py``:445). That is real, pre-existing and untouched
    by F-902 — and it is out of this node's scope because it cannot reach
    anything the product writes: ``configure_logging`` attaches its handler to
    ``stealth.{role}`` with ``propagate = False``, so a ``nodriver.*`` record
    propagates to the ROOT logger and lands in the product's file never. It is
    named in the finding's residuals rather than silently excluded here.
    """
    base = fixture_app_server
    iid, close = await _instance_on_a_page_with_a_cookie(base)
    try:
        with caplog.at_level(0):
            await asyncio.wait_for(get_fn("get_cookies")(instance_id=iid), 30)
            await asyncio.wait_for(get_fn("get_instance_state")(instance_id=iid), 30)

            records = list(caplog.records)

        ours = [r for r in records if r.name.startswith("stealth.")]
        assert ours, "no product log records at all — the node proves nothing"

        for secret in (_COOKIE_NAME, _COOKIE_VALUE):
            for record in ours:
                assert secret not in record.getMessage(), (
                    f"{secret!r} reached {record.name} at {record.levelname}"
                )
            for record in records:
                if record.levelno >= _SHIPPING_LEVEL:
                    assert secret not in record.getMessage(), (
                        f"{secret!r} would ship from {record.name} "
                        f"at {record.levelname}"
                    )
    finally:
        await close(instance_id=iid)
