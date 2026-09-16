"""F-882 — ``navigate`` answers about the document the tab is ACTUALLY showing.

Measured live on 2.1.8 (Chrome 152 headed, Windows 11): three of ten ordinary
sites answered ``Navigation to <url> timed out after 30000ms`` while the browser
sat on a fully loaded page — a signed-out ``mail.google.com``, ``youtube.com``
and ``reddit.com`` (whose landing URL carried ``?solution=…&js_challenge=1``).
Each had REPLACED the document Chrome committed for the tool's own ``loaderId``
before that document reached ``load``, so the event the wait was keyed on never
fired and the whole budget burned.

Every shape below is served locally by ``fixture_routes``' ``nav_*`` routes: the
live sites that showed the defect change under us and a test may never reach the
network, so each is reproduced exactly and deterministically instead. The
oracles are independent of the tool under test — the page's own sentinel read
through a SECOND tool (``execute_script``), and the fixture server's ledger of
which documents the browser actually fetched, read over plain HTTP from this
process. A tool that silently did nothing cannot agree with either.

Every node passes a small ``timeout`` (8 s), so a regression costs eight seconds
and names itself rather than costing the default thirty. Nothing here sleeps as
an oracle: the only durations asserted on are the fixture's own declared delays,
and the two nodes that must wait for a fetch the tool's milestone does not cover
poll the ledger for it rather than sleeping a fixed amount (``_await_fetched``).

===========================  ==============================================
shape                        node
===========================  ==============================================
(a) head-script replace      ``test_a_document_replaced_before_load_...``
(b) meta refresh 0           ``test_a_meta_refresh_answers_about_a_real_...``
(c) self-reload once         ``test_a_page_that_reloads_itself_once_...``
(d) JS challenge             ``test_a_js_challenge_lands_on_the_solved_...``
(e) 302 -> 302 -> slow load  ``test_a_redirect_chain_waits_for_the_final_...``
(f) download                 ``test_a_download_is_answered_at_once_...``
(g) pre-commit pre-emption   ``test_a_pending_navigation_the_page_pre_...``
(h) same-origin iframe       ``test_a_subframes_navigation_never_answers_...``
===========================  ==============================================
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

import fixture_routes as fr
from e2e_helpers import (
    eval_js,
    fixture_ledger,
    get_fn,
    integration_pytestmark,
    reset_fixture_ledger,
    sandbox_kwargs,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

pytestmark = integration_pytestmark()

#: Small enough that a regression reads as a fast, named failure rather than the
#: default 30 s; large enough to be ~5x the slowest fixture delay below.
NAV_TIMEOUT_MS = 8000
#: A node that answers must answer well inside its own budget. Not an oracle for
#: anything the page does — only for "this did not consume the whole timeout".
PROMPT_SECONDS = 6.0
#: (e)'s slow subresource. 1.6 s is comfortably above scheduler noise and still
#: a fifth of the node's budget.
SLOW_ASSET_MS = 1600
#: (g): a DOCUMENT the server answers slowly, and the moment the displayed page
#: tries to leave. The 1.5 s margin between them is what makes which navigation
#: wins a property of the shape rather than of the runner's load.
SLOW_DOC_MS = 2500
PREEMPT_MS = 1000
#: How long a fetch the tool's milestone does not cover may still be in flight
#: (see ``_await_fetched``). Generous on purpose: it is spent only when the
#: ledger does not match yet, and a node that spends it all fails anyway.
LEDGER_SETTLE_SECONDS = 5.0
LEDGER_POLL_SECONDS = 0.05


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


@pytest.fixture()
async def instance():
    """One headless instance per node, always closed (no fleet, no leak)."""
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        yield iid
    finally:
        await close(instance_id=iid)


async def _navigate(iid: str, url: str) -> tuple[dict, float]:
    """The tool under test, plus how long it took to answer."""
    navigate = get_fn("navigate")
    started = time.monotonic()
    result = await navigate(instance_id=iid, url=url, timeout=NAV_TIMEOUT_MS)
    return result, time.monotonic() - started


async def _sentinel(iid: str) -> str:
    """The page's own marker, read through a DIFFERENT tool than the one under
    test — so "the tab is showing this document" is not ``navigate``'s word."""
    return await eval_js(iid, "document.getElementById('sentinel').textContent")


async def _live(iid: str) -> tuple[str, str, str]:
    return (
        await eval_js(iid, "location.href"),
        await eval_js(iid, "document.title"),
        await eval_js(iid, "document.readyState"),
    )


def _fetched(ledger: dict) -> list[str]:
    """The document paths the BROWSER asked the server for, in order."""
    return [entry for entry in ledger["nav_paths"] if not entry.startswith("cookie=")]


async def _await_fetched(origin: str, expected: list[str]) -> list[str]:
    """The fetch ledger once it MATCHES ``expected``, or as it stands at the
    deadline — for the two nodes whose last fetch the tool's own milestone does
    NOT guarantee.

    (b) and (c) both schedule their second document AT ``load``, which is the
    very milestone ``navigate`` returns on, so both accept an answer about the
    FIRST document — and at that instant the server legitimately has not been
    asked for the second yet. Reading the ledger once then asserts that the
    refresh happened within one round trip of the answer, which is not what the
    node claims and not something either page promises. Measured: CI run
    35157444236 (macOS/ARM64 integration, PR #126) failed (b) with
    ``Right contains one more item: '/nav/landing?from=meta-refresh'``.

    It cannot mask a wrong answer. The sequence is returned and asserted on
    unchanged, so a ledger that is the WRONG order, is short at the deadline, or
    grows a FURTHER entry fails exactly as before; the only thing that changes
    is how long we are willing to wait for a fetch that is still in flight. The
    poll interval is a poll interval and not an oracle — nothing here asserts on
    it, and a match on the first read costs one HTTP round trip.

    The other five ledger assertions read ONCE, deliberately. (a), (d), (e) and
    (g) each assert that the tool answered ABOUT the final document, which it
    cannot have done without that document being served first; (f)'s abort needs
    the response headers Chrome read to decide it was a download; (h)'s subframe
    blocks its parent's ``load``. In each the single read is itself a claim
    about the product — the milestone covered those fetches — and a poll would
    quietly give that claim away.
    """
    deadline = time.monotonic() + LEDGER_SETTLE_SECONDS
    while True:
        fetched = _fetched(await fixture_ledger(origin))
        if fetched == expected or time.monotonic() >= deadline:
            return fetched
        await asyncio.sleep(LEDGER_POLL_SECONDS)


# ═══════════════════════════════════════════════════════════════════════════
# (a) a head script replaces the document before it can reach `load`
# ═══════════════════════════════════════════════════════════════════════════
async def test_a_document_replaced_before_load_answers_about_the_replacement(
    instance, fixture_origin_pair
):
    """THE F-882 node. RED at 6ca0ae9: ``ToolError: Navigation to
    <origin>/nav/head-replace timed out after 8000ms`` — while the browser was
    sitting on a fully loaded landing page."""
    origin_a, _ = fixture_origin_pair
    await reset_fixture_ledger(origin_a)

    result, spent = await _navigate(instance, f"{origin_a}/nav/head-replace")

    assert result["success"] is True
    assert spent < PROMPT_SECONDS, f"answered only after {spent:.1f}s"
    assert result["url"].endswith("/nav/landing?from=head-replace")
    assert result["title"] == fr.NAV_LANDING_TITLE
    # Independent oracle 1: the page itself, read through execute_script.
    assert await _sentinel(instance) == fr.NAV_LANDING_SENTINEL
    live_url, live_title, ready = await _live(instance)
    assert (live_url, live_title, ready) == (result["url"], result["title"], "complete")
    # Independent oracle 2: the server saw BOTH documents fetched, in order.
    assert _fetched(await fixture_ledger(origin_a)) == [
        "/nav/head-replace",
        "/nav/landing?from=head-replace",
    ]


# ═══════════════════════════════════════════════════════════════════════════
# (b) `<meta http-equiv="refresh" content="0;url=…">`
# ═══════════════════════════════════════════════════════════════════════════
async def test_a_meta_refresh_answers_about_a_real_document_either_side_of_it(
    instance, fixture_origin_pair
):
    """Measured: a ``meta refresh`` document DOES reach ``load`` (22.8 ms) and
    only then schedules its replacement (23.5 ms), so which of the two documents
    is current when the wait ends is a genuine race on a loaded runner. Both
    answers are truthful; a MIXED answer — one document's url with the other's
    title — is not, and is exactly what this node caught: two ``tab.evaluate``
    round trips straddled the refresh and reported
    ``{'url': '…/nav/meta-refresh', 'title': 'Nav Landing'}``, a record no
    document ever had. The landing is ONE round trip now. The server-side ledger
    proves the refresh really happened either way."""
    origin_a, _ = fixture_origin_pair
    await reset_fixture_ledger(origin_a)

    result, spent = await _navigate(instance, f"{origin_a}/nav/meta-refresh")

    assert result["success"] is True
    assert spent < PROMPT_SECONDS
    assert (result["url"].endswith("/nav/meta-refresh"), result["title"]) in (
        (True, ""),
        (False, fr.NAV_LANDING_TITLE),
    ), result
    # The refresh really happened. Awaited, not read once: the arm above that
    # answers about the FIRST document is truthful at an instant when the
    # landing has not been asked for yet (``_await_fetched``).
    expected = ["/nav/meta-refresh", "/nav/landing?from=meta-refresh"]
    assert await _await_fetched(origin_a, expected) == expected


# ═══════════════════════════════════════════════════════════════════════════
# (c) a page that reloads itself once — the Amazon shape
# ═══════════════════════════════════════════════════════════════════════════
async def test_a_page_that_reloads_itself_once_is_answered_about_honestly(
    instance, fixture_origin_pair
):
    """``amazon.com`` answered ``success: true, title: ""`` while the tab showed
    "Amazon.com. Spend less. Smile more." — its first document has no ``<title>``
    and reloads itself at its own ``load``. The first document IS what the tab
    was showing when the milestone was reached, so the empty title is truthful
    at that instant and stays (F-882 §6); what must hold is that the answer is
    prompt, is not a timeout, and names one of the two real documents."""
    origin_a, _ = fixture_origin_pair
    await reset_fixture_ledger(origin_a)
    token = uuid.uuid4().hex[:8]
    url = f"{origin_a}/nav/self-reload?token={token}"

    result, spent = await _navigate(instance, url)

    assert result["success"] is True
    assert spent < PROMPT_SECONDS
    assert result["url"] == url
    assert result["title"] in ("", fr.NAV_RELOADED_TITLE), result
    # The reload really happened: the server served the same document twice.
    # Awaited for (b)'s reason — this page schedules its reload at `load`, so
    # the `title == ""` arm above answers before the second request is sent.
    expected = [f"/nav/self-reload?token={token}"] * 2
    assert await _await_fetched(origin_a, expected) == expected


# ═══════════════════════════════════════════════════════════════════════════
# (d) a JS challenge: set a cookie, re-navigate to the solved URL
# ═══════════════════════════════════════════════════════════════════════════
async def test_a_js_challenge_lands_on_the_solved_document(
    instance, fixture_origin_pair
):
    """Reddit's shape. RED at 6ca0ae9: timed out after the full budget while the
    tab showed "Reddit - The heart of the internet" on the solved URL."""
    origin_a, _ = fixture_origin_pair
    await reset_fixture_ledger(origin_a)
    token = uuid.uuid4().hex[:8]

    result, spent = await _navigate(
        instance, f"{origin_a}/nav/js-challenge?token={token}"
    )

    assert result["success"] is True
    assert spent < PROMPT_SECONDS
    assert result["url"].endswith(
        f"?token={token}&solution={fr.NAV_SOLUTION}&js_challenge=1"
    )
    assert result["title"] == fr.NAV_SOLVED_TITLE
    assert await _sentinel(instance) == fr.NAV_SOLVED_SENTINEL
    # The challenge really ran in the page: the solved request carried the
    # cookie the interstitial's script set.
    ledger = await fixture_ledger(origin_a)
    cookies = [e for e in ledger["nav_paths"] if e.startswith("cookie=")]
    assert (
        cookies
        and f"{fr.NAV_CHALLENGE_COOKIE}_{token}={fr.NAV_SOLUTION}" in (cookies[0])
    ), cookies


# ═══════════════════════════════════════════════════════════════════════════
# (e) 302 -> 302 -> a document whose `load` a slow subresource holds open
# ═══════════════════════════════════════════════════════════════════════════
async def test_a_redirect_chain_waits_for_the_final_documents_load(
    instance, fixture_origin_pair
):
    """The control for the other direction: following the loader CHAIN must not
    weaken the wait. A 302 chain is ONE loader (measured), and the landing
    document's ``load`` is held open ~1.6 s by an image — so an answer before
    that is an answer about a document that had not loaded."""
    origin_a, _ = fixture_origin_pair
    await reset_fixture_ledger(origin_a)

    result, spent = await _navigate(
        instance, f"{origin_a}/nav/chain-start?ms={SLOW_ASSET_MS}"
    )

    assert result["success"] is True
    assert result["url"].endswith(f"/nav/chain-final?ms={SLOW_ASSET_MS}")
    assert result["title"] == fr.NAV_CHAIN_TITLE
    # The wait really waited: the page cannot have loaded before its subresource.
    assert spent >= SLOW_ASSET_MS / 1000.0, f"answered after only {spent:.2f}s"
    assert spent < PROMPT_SECONDS
    live_url, live_title, ready = await _live(instance)
    assert (live_url, live_title, ready) == (result["url"], result["title"], "complete")
    assert _fetched(await fixture_ledger(origin_a)) == [
        f"/nav/chain-start?ms={SLOW_ASSET_MS}",
        f"/nav/chain-mid?ms={SLOW_ASSET_MS}",
        f"/nav/chain-final?ms={SLOW_ASSET_MS}",
    ]


# ═══════════════════════════════════════════════════════════════════════════
# (f) a download: Chrome accepts the navigation and commits nothing
# ═══════════════════════════════════════════════════════════════════════════
async def test_a_download_is_answered_at_once_and_leaves_the_tab_alone(
    instance, fixture_origin_pair
):
    """Measured: ``Page.navigate`` answers in ~13 ms with
    ``errorText='net::ERR_ABORTED'``, nothing commits, nothing fires, and the tab
    keeps showing the page it was on. RED at 6ca0ae9: the wait sat for the whole
    budget and then reported a timeout about a navigation that was over before
    it began."""
    origin_a, _ = fixture_origin_pair
    await reset_fixture_ledger(origin_a)
    before, _ = await _navigate(instance, f"{origin_a}/nav/landing?from=pre-download")

    started = time.monotonic()
    with pytest.raises(ToolError, match=r"aborted by Chrome \(net::ERR_ABORTED\)"):
        await _navigate(instance, f"{origin_a}/nav/download")
    spent = time.monotonic() - started

    assert spent < PROMPT_SECONDS, f"the abort was waited on for {spent:.1f}s"
    # The tab did not move, which is exactly what the message claims.
    live_url, live_title, _ = await _live(instance)
    assert (live_url, live_title) == (before["url"], before["title"])
    assert await _sentinel(instance) == fr.NAV_LANDING_SENTINEL
    assert _fetched(await fixture_ledger(origin_a))[-1] == "/nav/download"


# ═══════════════════════════════════════════════════════════════════════════
# (g) the displayed page tries to leave while our navigation is still pending
# ═══════════════════════════════════════════════════════════════════════════
async def test_a_pending_navigation_the_page_pre_empted_answers_about_the_winner(
    instance, fixture_origin_pair
):
    """Measured (Chrome 152, 6/6 runs): the displayed page navigating itself away
    while our navigation is still pending CANCELS ours — ``Page.navigate``
    answers ``net::ERR_ABORTED``, our loader never commits at all, and the page's
    own document commits 11.6-14.1 ms later (or, in 2 of 6, just before the
    abort response). The answer must be the document that won. RED at 6ca0ae9:
    timed out after the whole budget on a tab showing a finished page."""
    origin_a, _ = fixture_origin_pair
    await reset_fixture_ledger(origin_a)
    await _navigate(instance, f"{origin_a}/nav/preempt?ms={PREEMPT_MS}")

    result, spent = await _navigate(
        instance, f"{origin_a}/nav/slow-doc?ms={SLOW_DOC_MS}"
    )

    assert result["success"] is True
    assert result["url"].endswith("/nav/landing?from=preempt")
    assert result["title"] == fr.NAV_LANDING_TITLE
    assert spent < PROMPT_SECONDS
    assert await _sentinel(instance) == fr.NAV_LANDING_SENTINEL
    live_url, live_title, ready = await _live(instance)
    assert (live_url, live_title, ready) == (result["url"], result["title"], "complete")
    assert "/nav/landing?from=preempt" in _fetched(await fixture_ledger(origin_a))


# ═══════════════════════════════════════════════════════════════════════════
# (h) a same-origin iframe whose document replaces itself
# ═══════════════════════════════════════════════════════════════════════════
async def test_a_subframes_navigation_never_answers_for_the_main_frame(
    instance, fixture_origin_pair
):
    """Measured: the iframe's two documents commit under the SUBFRAME's frameId
    (907517DE…, parent 966836E7…) while the main frame commits once. The answer
    must be the HOST's url and title even though two other documents committed,
    loaded and were replaced inside it while we waited."""
    origin_a, _ = fixture_origin_pair
    await reset_fixture_ledger(origin_a)

    result, spent = await _navigate(instance, f"{origin_a}/nav/iframe-host")

    assert result["success"] is True
    assert spent < PROMPT_SECONDS
    assert result["url"].endswith("/nav/iframe-host")
    assert result["title"] == fr.NAV_IFRAME_HOST_TITLE
    assert await _sentinel(instance) == fr.NAV_IFRAME_HOST_SENTINEL
    # The subframe really did commit twice — the thing the frame filter ignored.
    fetched = _fetched(await fixture_ledger(origin_a))
    assert fetched == [
        "/nav/iframe-host",
        "/nav/head-replace",
        "/nav/landing?from=head-replace",
    ], fetched
