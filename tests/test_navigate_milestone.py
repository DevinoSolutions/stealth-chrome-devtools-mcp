"""F-881 — ``navigate(wait_until=...)`` answers AFTER the milestone it names.

Two Windows full gates failed the same way on unrelated PRs:
``navigate(url="data:text/html,<title>Alpha</title>…")`` returned ``title: ""``.
Measured (finding §2): the tool's ``load`` wait was ``tab.wait(LoadEventFired)``,
which nodriver 0.47 reads as a *duration* and skips (0.02 ms), and ``tab.get``
before it returned at commit — so ``document.title`` was read while the parser
was still running. A 0.5 s dead sleep inside ``tab.get`` (the ``Page`` domain was
never enabled, so its wait saw no event) is what made it pass everywhere else.

The fake here models what Chrome actually sends (``FakeTab``'s navigation
model, built from the finding's §2d): ``Page.navigate`` answers
``(frameId, loaderId, errorText)``, the new document's ``init`` /
``DOMContentLoaded`` / ``load`` lifecycle events land either side of that
response, and a ``title_at_load`` page reads ``""`` until its ``load`` — the
CI shape, reproduced without Chrome.

Drives the REAL ``BrowserManager.navigate`` with its instance bookkeeping
stubbed, exactly as the F-824 pins do, so the wait under test is the product's.
"""

from __future__ import annotations

import asyncio

import pytest
from nodriver import cdp

from fakes import FakeTab
from stealth_chrome_devtools_mcp.embedded import navigation_milestone
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

URL = "https://fake.test/target"


@pytest.fixture
def manager(monkeypatch):
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(BrowserManager, "touch_instance", noop)
    monkeypatch.setattr(BrowserManager, "update_instance_state", noop)
    return BrowserManager()


def _with_tab(monkeypatch, tab) -> list[str]:
    """Bind *tab* as the navigation tab; the returned list records every
    stale-tab recovery (``_replace_main_tab`` reason) the manager asked for."""
    replacements: list[str] = []

    async def get_navigation_tab(self, instance_id):
        return tab

    async def replace_main_tab(self, instance_id, reason, close_existing=True):
        replacements.append(reason)
        return tab

    monkeypatch.setattr(BrowserManager, "get_navigation_tab", get_navigation_tab)
    monkeypatch.setattr(BrowserManager, "_replace_main_tab", replace_main_tab)
    return replacements


def _navigate_frames(tab: FakeTab) -> list[str]:
    return [
        f["params"]["url"] for f in tab.cdp_frames if f["method"] == "Page.navigate"
    ]


# ---------------------------------------------------------------------------
# The CI failure, without Chrome
# ---------------------------------------------------------------------------


async def test_navigate_reports_the_title_the_page_has_at_load(monkeypatch, manager):
    """THE pin: ``load`` lands after ``Page.navigate`` answered; the tool must
    answer after it, not at commit. RED at 3311be9: ``assert '' == 'Alpha'``."""
    tab = FakeTab(lifecycle="after", title_at_load="Alpha")
    _with_tab(monkeypatch, tab)

    result = await manager.navigate(instance_id="iid-1", url=URL, timeout=500)

    assert result["title"] == "Alpha"
    assert result["url"] == URL
    assert _navigate_frames(tab) == [URL]


async def test_a_load_that_preceded_the_navigate_response_still_counts(
    monkeypatch, manager
):
    """Measured: ``DOMContentLoaded`` arrived 0.3 ms BEFORE the response and
    ``load`` 0.3 ms after; either order is real. A wait armed only after the
    response would miss the first shape and spend the whole budget."""
    tab = FakeTab(lifecycle="before", title_at_load="Alpha")
    _with_tab(monkeypatch, tab)

    result = await manager.navigate(instance_id="iid-1", url=URL, timeout=300)

    assert result["title"] == "Alpha"


async def test_domcontentloaded_returns_on_that_event_without_waiting_for_load(
    monkeypatch, manager
):
    """A page whose subresources never finish still reaches DCL; the parser
    has set ``<title>`` by then. RED at 3311be9: ``assert '' == 'Alpha'``."""
    tab = FakeTab(
        lifecycle="after", last_milestone="DOMContentLoaded", title_at_dcl="Alpha"
    )
    _with_tab(monkeypatch, tab)

    result = await manager.navigate(
        instance_id="iid-1", url=URL, wait_until="domcontentloaded", timeout=300
    )

    assert result["title"] == "Alpha"


async def test_load_is_waited_for_and_a_page_that_never_loads_times_out(
    monkeypatch, manager
):
    """The same page under the default ``load``: it must NOT be reported as
    navigated. RED at 3311be9: ``DID NOT RAISE`` (success with ``title ""``).

    And it is NOT retried: Chrome accepted the navigation (``Page.navigate``
    answered), so the page is there and merely never reaches ``load`` — a
    replaced tab would discard it and spend a second full budget. One
    ``Page.navigate``, no ``_replace_main_tab``."""
    tab = FakeTab(
        lifecycle="after", last_milestone="DOMContentLoaded", title_at_dcl="Alpha"
    )
    replacements = _with_tab(monkeypatch, tab)

    with pytest.raises(ToolError, match="timed out"):
        await manager.navigate(instance_id="iid-1", url=URL, timeout=150)

    assert _navigate_frames(tab) == [URL]
    assert replacements == []


class _UnansweringTab(FakeTab):
    """A tab whose ``Page.navigate`` never answers — the hang-before-headers
    shape, where Chrome has not accepted anything and the tab may be stale."""

    async def send(self, cdp_obj, *args, **kwargs):
        if getattr(getattr(cdp_obj, "gi_code", None), "co_name", None) == "navigate":
            self.cdp_frames.append(next(cdp_obj))
            cdp_obj.close()
            await asyncio.get_running_loop().create_future()
        return await super().send(cdp_obj, *args, **kwargs)


async def test_a_navigation_chrome_never_accepted_keeps_its_one_recovery_retry(
    monkeypatch, manager
):
    """The other side of the line: no ``Page.navigate`` answer means the tab may
    be stale (F-824), so the one-shot recovery on a fresh tab stays — two
    attempts, then the pinned timeout."""
    tab = _UnansweringTab()
    replacements = _with_tab(monkeypatch, tab)

    with pytest.raises(ToolError, match="timed out"):
        await manager.navigate(instance_id="iid-1", url=URL, timeout=100)

    assert _navigate_frames(tab) == [URL, URL]
    assert len(replacements) == 1


async def test_the_milestone_is_keyed_on_the_responses_loader_id(monkeypatch, manager):
    """An older document's ``load`` (a page still finishing when the new
    navigation was sent) must not end the wait for the new one."""
    tab = FakeTab(lifecycle="never", stale_load_for="L-old")
    _with_tab(monkeypatch, tab)

    with pytest.raises(ToolError, match="timed out"):
        await manager.navigate(instance_id="iid-1", url=URL, timeout=150)


async def test_a_same_document_navigation_returns_at_the_response(monkeypatch, manager):
    """``Page.navigate`` answers ``loaderId: null`` for a fragment move and no
    lifecycle event ever fires (measured); there is nothing to wait for."""
    tab = FakeTab(url=URL, lifecycle="never", title_at_load="Alpha")
    _with_tab(monkeypatch, tab)

    result = await manager.navigate(instance_id="iid-1", url=URL + "#deep", timeout=300)

    assert result["url"] == URL + "#deep"
    assert result["title"] == "Alpha"


async def test_an_unknown_wait_until_raises_naming_the_accepted_values(
    monkeypatch, manager
):
    """It used to mean ``load`` silently — which then meant nothing at all.

    Checked BEFORE the retry loop: a typo costs no CDP send (no referrer header,
    no ``Page.navigate``) and no stale-tab recovery."""
    tab = FakeTab(lifecycle="before", title_at_load="Alpha")
    replacements = _with_tab(monkeypatch, tab)

    with pytest.raises(ToolError, match=r"load.*domcontentloaded.*networkidle"):
        await manager.navigate(
            instance_id="iid-1",
            url=URL,
            wait_until="commit",
            referrer="https://r.test/",
        )

    assert tab.send_calls == []
    assert tab.cdp_frames == []
    assert replacements == []


# ---------------------------------------------------------------------------
# Hygiene: the listener is transient and its removal survives nodriver
# ---------------------------------------------------------------------------


async def test_the_lifecycle_listener_is_removed_after_the_navigation(
    monkeypatch, manager
):
    tab = FakeTab(lifecycle="after", title_at_load="Alpha")
    _with_tab(monkeypatch, tab)

    await manager.navigate(instance_id="iid-1", url=URL, timeout=300)

    assert tab.handlers == []


async def test_a_concurrent_wait_deleting_the_listener_does_not_fail_the_navigation(
    monkeypatch, manager
):
    """nodriver's ``remove_handler`` is ``del self.handlers[evt]``; an
    overlapping ``Tab.wait()`` leaves the key gone and the next removal raises
    ``KeyError(<event class>)`` (F-824's race). It must not surface here."""
    tab = FakeTab(lifecycle="before", title_at_load="Alpha")
    _with_tab(monkeypatch, tab)

    def already_deleted(event_type, handler=None):
        raise KeyError(cdp.page.LifecycleEvent)

    tab.remove_handler = already_deleted

    result = await manager.navigate(instance_id="iid-1", url=URL, timeout=300)

    assert result["title"] == "Alpha"


async def test_networkidle_still_sleeps_f787s_fixed_window_after_commit(
    monkeypatch, manager
):
    """F-787 stays OPEN and unchanged in effect: ``networkidle`` is commit plus
    a fixed sleep, not a quiescence wait, so it still answers before ``load``.
    The sleep is observed, not endured."""
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def observed_sleep(delay, *args, **kwargs):
        slept.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", observed_sleep)
    tab = FakeTab(lifecycle="after", last_milestone="init", title_at_load="Alpha")
    _with_tab(monkeypatch, tab)

    result = await manager.navigate(
        instance_id="iid-1", url=URL, wait_until="networkidle", timeout=5000
    )

    assert result["success"] is True
    assert result["title"] == ""  # F-787: answered before load
    assert 2.0 in slept


# ---------------------------------------------------------------------------
# F-882 — the document our loader committed can be REPLACED before it loads
# ---------------------------------------------------------------------------
# Measured live on 2.1.8 (Chrome 152): three of ten ordinary sites answered
# ``Navigation ... timed out after 30000ms`` while the browser sat on a fully
# loaded page — a signed-out Gmail, YouTube, and Reddit's ``js_challenge``. Each
# had replaced the document Chrome committed for OUR loaderId with a new one
# under a NEW loaderId, so the ``load`` we were keyed on never fired. The fake
# below reproduces that stream (``FakeTab``'s supersession model) plus the two
# things that must NOT be mistaken for it: a subframe's loader, and the
# lifecycle replay ``Page.setLifecycleEventsEnabled`` sends for the page being
# LEFT.

LANDING = "https://fake.test/landing"


async def test_a_document_replaced_before_its_load_answers_at_the_replacements(
    monkeypatch, manager
):
    """THE F-882 pin: the head-script ``location.replace`` / js-challenge shape —
    our document commits and is destroyed before ``load``. The answer must be the
    document the tab is actually showing. RED at 6ca0ae9: ``ToolError: Navigation
    to https://fake.test/target timed out after 500ms``."""
    tab = FakeTab(
        lifecycle="after",
        supersede_after="init",
        supersede_url=LANDING,
        title_at_load="Alpha",
        title_after_supersede="Landing",
    )
    replacements = _with_tab(monkeypatch, tab)

    result = await manager.navigate(instance_id="iid-1", url=URL, timeout=500)

    assert result == {"url": LANDING, "title": "Landing", "success": True}
    # One navigation, no stale-tab recovery: Chrome accepted ours and the page
    # moved itself, which a fresh tab would not have fixed.
    assert _navigate_frames(tab) == [URL]
    assert replacements == []


async def test_the_whole_chain_is_followed_not_just_the_first_replacement(
    monkeypatch, manager
):
    """A challenge that bounces twice is the same shape twice. Only the LAST
    document's milestone may end the wait. RED at 6ca0ae9: timed out."""
    tab = FakeTab(
        lifecycle="after",
        supersede_after="init",
        supersede_count=3,
        supersede_url=LANDING,
        title_at_load="Alpha",
        title_after_supersede="Landing",
    )
    _with_tab(monkeypatch, tab)

    result = await manager.navigate(instance_id="iid-1", url=URL, timeout=800)

    assert result["title"] == "Landing"
    assert result["url"] == LANDING


async def test_domcontentloaded_is_followed_across_a_replacement_too(
    monkeypatch, manager
):
    """The milestone the caller named, on the document that exists. RED at
    6ca0ae9: timed out."""
    tab = FakeTab(
        lifecycle="after",
        supersede_after="init",
        supersede_last_milestone="DOMContentLoaded",
        supersede_url=LANDING,
        title_at_dcl="Landing",
    )
    _with_tab(monkeypatch, tab)

    result = await manager.navigate(
        instance_id="iid-1", url=URL, wait_until="domcontentloaded", timeout=500
    )

    assert result == {"url": LANDING, "title": "Landing", "success": True}


async def test_a_page_that_keeps_replacing_itself_still_times_out_and_says_so(
    monkeypatch, manager
):
    """Following the chain is not waiting forever: a document that never reaches
    the milestone is still the caller's budget, and the message now carries WHY
    — ``accepted, committed, superseded by 1 later document(s)`` — where it used
    to end at an empty ``TimeoutError`` (F-882 §4)."""
    tab = FakeTab(
        lifecycle="after",
        supersede_after="init",
        supersede_last_milestone="DOMContentLoaded",
    )
    _with_tab(monkeypatch, tab)

    with pytest.raises(ToolError, match=r"superseded by 1 later document\(s\)"):
        await manager.navigate(instance_id="iid-1", url=URL, timeout=200)


async def test_the_failed_attempt_warning_carries_the_reason_not_an_empty_colon(
    monkeypatch, manager
):
    """The shipped line was ``Navigation attempt 1 failed for <id>: `` — a
    ``TimeoutError`` stringifies to nothing, so the durable log said only that
    something had failed. RED at 6ca0ae9: the message ends at the colon."""
    lines: list[str] = []
    monkeypatch.setattr(
        debug_logger,
        "log_warning",
        lambda component, method, message, context=None, error=None: lines.append(
            message
        ),
    )
    tab = FakeTab(
        lifecycle="after",
        supersede_after="init",
        supersede_last_milestone="DOMContentLoaded",
    )
    _with_tab(monkeypatch, tab)

    with pytest.raises(ToolError):
        await manager.navigate(instance_id="iid-1", url=URL, timeout=200)

    assert lines, "the failed attempt was not logged at all"
    assert "TimeoutError" in lines[0]
    assert "accepted, committed, superseded by 1 later document(s)" in lines[0]


async def test_a_download_is_named_after_the_grace_never_after_the_whole_budget(
    monkeypatch, manager
):
    """A download (``Content-Disposition: attachment``) answers ``Page.navigate``
    with ``net::ERR_ABORTED`` in ~9-13 ms, commits nothing, fires nothing and
    leaves the tab where it was (measured, Chrome 152). RED at 6ca0ae9: the wait
    sat for the WHOLE budget and then reported a timeout about a navigation that
    was over before it started. It is not retried either — a second attempt
    would trigger the download twice."""
    tab = FakeTab(navigate_error="net::ERR_ABORTED", url=LANDING)
    replacements = _with_tab(monkeypatch, tab)

    started = asyncio.get_running_loop().time()
    with pytest.raises(ToolError, match=r"aborted by Chrome \(net::ERR_ABORTED\)"):
        await manager.navigate(instance_id="iid-1", url=URL, timeout=8000)
    spent = asyncio.get_running_loop().time() - started

    assert spent < navigation_milestone.ABORTED_GRACE_SECONDS + 0.5, (
        f"the abort was waited on for {spent:.2f}s"
    )
    assert _navigate_frames(tab) == [URL]
    assert replacements == []
    assert tab.url == LANDING  # the tab did not move, and the message says so


async def test_an_abort_whose_page_took_our_place_is_followed_not_called_a_download(
    monkeypatch, manager
):
    """The OTHER meaning of ``net::ERR_ABORTED`` (measured, 6/6 runs): the
    displayed page navigated itself away while our navigation was still pending,
    so ours is cancelled and the page's own document commits 11.6-14.1 ms later
    — or, in two of six runs, BEFORE the abort response arrived. Both orders are
    followed; reporting either as a download would be a lie about a tab that
    moved. RED at 6ca0ae9: timed out after the whole budget."""
    tab = FakeTab(
        lifecycle="after",
        navigate_error="net::ERR_ABORTED",
        supersede_after="aborted",
        supersede_url=LANDING,
        title_at_load="Alpha",
        title_after_supersede="Landing",
    )
    _with_tab(monkeypatch, tab)

    result = await manager.navigate(instance_id="iid-1", url=URL, timeout=4000)

    assert result == {"url": LANDING, "title": "Landing", "success": True}


class _LateAbortTab(FakeTab):
    """A ``Page.navigate`` whose abort takes a while to arrive — the real shape.
    The abort IS the page navigating away, which happens a second or more into a
    pending navigation (measured: the response landed at 1006 ms)."""

    async def send(self, cdp_obj, *args, **kwargs):
        if getattr(getattr(cdp_obj, "gi_code", None), "co_name", None) == "navigate":
            await asyncio.sleep(navigation_milestone.ABORTED_GRACE_SECONDS + 0.2)
        return await super().send(cdp_obj, *args, **kwargs)


async def test_the_abort_grace_starts_at_the_response_not_at_the_call(
    monkeypatch, manager
):
    """Caught by the real-Chrome node before it could ship: a grace anchored at
    the start of the attempt is ALREADY SPENT when the abort arrives — the tool
    reported every pre-empted navigation as a download, ~1 s into its own
    budget, while the tab was on the page that pre-empted it."""
    tab = _LateAbortTab(
        lifecycle="after",
        navigate_error="net::ERR_ABORTED",
        supersede_after="aborted",
        supersede_url=LANDING,
        title_at_load="Alpha",
        title_after_supersede="Landing",
    )
    _with_tab(monkeypatch, tab)

    result = await manager.navigate(instance_id="iid-1", url=URL, timeout=8000)

    assert result == {"url": LANDING, "title": "Landing", "success": True}


async def test_the_abort_grace_cannot_outlive_the_callers_own_budget(
    monkeypatch, manager
):
    """The grace is clipped to what is left of ``timeout``, so a caller who
    asked for 200 ms still gets the NAMED answer rather than a bare
    cancellation from the enclosing ``wait_for``."""
    tab = FakeTab(navigate_error="net::ERR_ABORTED", url=LANDING)
    _with_tab(monkeypatch, tab)

    with pytest.raises(ToolError, match=r"aborted by Chrome"):
        await manager.navigate(instance_id="iid-1", url=URL, timeout=200)


async def test_a_subframes_loader_is_never_this_navigations(monkeypatch, manager):
    """Measured: a same-origin iframe that replaces itself produces two loaders
    under the SUBFRAME's frameId while the main frame commits once. Those
    documents load; ours must still be the one waited for, so a main frame that
    never reaches ``load`` still times out."""
    tab = FakeTab(
        lifecycle="after",
        supersede_after="DOMContentLoaded",
        supersede_last_milestone="DOMContentLoaded",
        iframe_loader=True,
    )
    _with_tab(monkeypatch, tab)

    with pytest.raises(ToolError, match="timed out"):
        await manager.navigate(instance_id="iid-1", url=URL, timeout=200)


async def test_the_enable_time_replay_is_never_read_as_this_navigation(
    monkeypatch, manager
):
    """``Page.setLifecycleEventsEnabled(true)`` re-sends the CURRENT document's
    whole lifecycle — ``commit``/``DOMContentLoaded``/``load`` — under the loader
    of the page being LEFT, and the tool sends it immediately before
    ``Page.navigate``. A wait that took any ``load`` on its frame would answer
    with the previous page, instantly and always."""
    tab = FakeTab(lifecycle="never", replay_loader="L-previous", title_at_load="Alpha")
    _with_tab(monkeypatch, tab)

    with pytest.raises(ToolError, match="timed out"):
        await manager.navigate(instance_id="iid-1", url=URL, timeout=200)


async def test_a_document_that_loads_before_its_replacement_answers_at_its_own(
    monkeypatch, manager
):
    """The Amazon / ``meta refresh`` shape, and the deliberate residual (§6):
    when OUR document reaches ``load`` first, that is what the tab was showing at
    that instant and it is what we answer. Waiting past it would be a quiescence
    wait, which ``navigate`` does not promise and cannot bound.

    The replacement is HELD, not raced: how many loop turns fall between our
    ``load`` landing and the tool's landing read is the host's (``wait_for``
    wraps its awaitable in a Task on some interpreters — green on 3.13, red on
    the three CI lanes). Held, the page cannot move before the read, and a rule
    that waited for the replacement times out instead of passing by luck."""
    tab = FakeTab(
        lifecycle="after",
        supersede_after="load",
        supersede_held=True,
        supersede_url=LANDING,
        title_at_load="Alpha",
        title_after_supersede="Landing",
    )
    _with_tab(monkeypatch, tab)

    result = await manager.navigate(instance_id="iid-1", url=URL, timeout=500)

    assert result["title"] == "Alpha"
    assert result["url"] == URL
    # ... and the replacement WAS real: released, it is where the page goes.
    tab.deliver_supersession()
    assert tab.url == LANDING


async def test_networkidle_still_keys_to_the_first_commit(monkeypatch, manager):
    """F-787's fixed sleep is keyed to the commit, and the commit is OURS: the
    chain cannot reach past a milestone that is already satisfied when our own
    document lands. The discriminating assertion is the url — a chain that waited
    for the REPLACEMENT's commit would have folded it, and the fake moves
    ``tab.url`` to `LANDING` at exactly that event, so it would answer `LANDING`.

    The replacement is HELD until after the read (see ``supersede_held``): what
    this node pins is the instant the WAIT ended, and a replacement racing the
    landing read across the mocked sleep's yield was the host's schedule, not the
    rule's — green on 3.13, red on the three CI lanes. A rule that keyed to the
    replacement's commit now has nothing to key to and times out."""
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def observed_sleep(delay, *args, **kwargs):
        slept.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", observed_sleep)
    tab = FakeTab(
        lifecycle="after",
        supersede_after="init",
        supersede_held=True,
        supersede_url=LANDING,
        title_at_load="Alpha",
        title_after_supersede="Landing",
    )
    _with_tab(monkeypatch, tab)

    result = await manager.navigate(
        instance_id="iid-1", url=URL, wait_until="networkidle", timeout=5000
    )

    assert result["success"] is True
    assert result["url"] == URL  # ours, not the document that replaced it
    assert 2.0 in slept
    # ... and the replacement WAS real, so the assertion above is a choice the
    # code made rather than a shape that never arose.
    tab.deliver_supersession()
    assert tab.url == LANDING


# ── F-882d: the state between the milestone and the landing read ────────────
async def test_a_commit_after_the_milestone_answers_a_committed_untitled_page():
    """The meta-refresh shape has a THIRD truthful answer, and the E2E oracle
    must name it (F-882d).

    The refresh is scheduled at the first document's ``load`` — the milestone
    the wait returns on — so the replacement can commit in the gap between the
    wait ending and the landing read. ``location.href`` has moved; the new
    document has not parsed its ``<title>`` yet. That pair is ONE document at
    ONE instant, which is all the product ever promised: the read is a single
    round trip (F-882), so it cannot be mixing two.

    Driven through ``navigation_milestone``'s own two calls in the order the
    race produces, with the replacement HELD so the gap is the test's and not
    the host scheduler's. CI hit this for real on 2026-09-16 — run 35175574635
    (release-gate integration, Linux/X64) failed the E2E node with
    ``assert (False, '') in ((True, ''), (False, 'Nav Landing'))`` — while the
    product was right; what was wrong was a set of accepted states with two
    members. The last assertion is why this pin lives here rather than in a
    table of its own: it reads the E2E node's OWN set, so a state the product
    can reach and that oracle does not name fails hermetically, on every lane,
    instead of once in a while on one cell.
    """
    # The E2E node's urls, so its table can be asked about this exact answer.
    # Imported in the body: that module is integration-marked and nothing here
    # should pull its helpers at collection time.
    import test_e2e_navigation_truthfulness as e2e_nav

    origin = "https://fake.test"
    first = f"{origin}/nav/meta-refresh"
    landing = f"{origin}/nav/landing?from=meta-refresh"
    tab = FakeTab(
        url=first,
        lifecycle="after",
        supersede_after="load",
        supersede_held=True,
        supersede_last_milestone="init",  # it COMMITS and gets no further
        supersede_url=landing,
        title_after_supersede="Nav Landing",
        title_at_load="",
    )

    await navigation_milestone.navigate(tab, first, "load", budget_seconds=5.0)
    tab.deliver_supersession()  # the landing commits, still parsing
    answered = await navigation_milestone.landing(tab)

    assert answered == (landing, "")
    assert answered in e2e_nav._meta_refresh_states(origin), answered
