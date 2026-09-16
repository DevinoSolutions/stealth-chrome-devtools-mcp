"""F-884 in a REAL Chrome: concurrent selector resolution on ONE tab.

The hermetic half (``tests/test_element_resolution.py``) models the race and
pins mutual exclusion. This half pins what Chrome and nodriver 0.47 actually
DO, because every claim F-884 rests on is a claim about them:

* ``DOM.getDocument`` resets THIS CDP session's node-id bindings, so a sibling
  resolution mid-flight loses the id it was about to use;
* nodriver answers that ``ProtocolException`` by sending ``DOM.disable()``
  before re-raising, and that send fails with ``-32000 "DOM agent hasn't been
  enabled"`` once a sibling already disabled the agent -- REPLACING the
  stale-node text the recovery classifies on, which is why retrying could not
  absorb it.

Measured on this file's own fixture before the fix: three concurrent
resolutions failed 5/15, five concurrent failed 15/25, every round. After, 0.

The product overlaps requests even though the default Claude Code client does
not: the stdio proxy does ``start_soon`` per request and the backend runs an
anyio task group, so any client that pipelines reaches this. Driving the tool
functions concurrently here is therefore the product's own shape, not a
synthetic one.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    sandbox_kwargs,
    warmup_once,
)

pytestmark = integration_pytestmark()

# Enough to cross the threshold the finding measured (failures began at three)
# with headroom, and small enough that one wedged browser costs seconds.
_CONCURRENCY = 5


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def test_concurrent_wait_for_element_on_one_tab_all_succeed(
    fixture_app_server, tmp_empty_root
):
    """The reported shape: N concurrent ``wait_for_element`` on one tab.

    Before F-884 the middle callers raised
    ``ProtocolException: DOM agent hasn't been enabled [code: -32000]``.
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    wait_for_element = get_fn("wait_for_element")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")

        results = await asyncio.gather(
            *(
                wait_for_element(instance_id=iid, selector="body", timeout=5000)
                for _ in range(_CONCURRENCY)
            ),
            return_exceptions=True,
        )

        raised = [r for r in results if isinstance(r, BaseException)]
        assert not raised, f"{len(raised)}/{_CONCURRENCY} concurrent waits raised"
    finally:
        await close(instance_id=iid)


async def test_concurrent_query_elements_on_one_tab_all_succeed(
    fixture_app_server, tmp_empty_root
):
    """The multi-match path (``select_all``) races on the same node id.

    ``query_elements`` is the other resolution shape a pipelining client
    overlaps, and it reaches ``resolve_elements`` rather than
    ``resolve_element`` -- a lock on one and not the other would pass the test
    above and still crash here.
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    query_elements = get_fn("query_elements")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")

        results = await asyncio.gather(
            *(
                query_elements(instance_id=iid, selector="div")
                for _ in range(_CONCURRENCY)
            ),
            return_exceptions=True,
        )

        raised = [r for r in results if isinstance(r, BaseException)]
        assert not raised, f"{len(raised)}/{_CONCURRENCY} concurrent queries raised"
        # Every caller must see the SAME page, not a half-reset document: a
        # resolution that silently lost its node id answers an empty list, and
        # "no exception" alone would not notice that.
        counts = {len(r) for r in results}
        assert len(counts) == 1, f"concurrent queries disagreed on the DOM: {counts}"
        assert counts.pop() > 0
    finally:
        await close(instance_id=iid)


async def test_a_waiter_does_not_starve_the_call_that_would_satisfy_it(
    fixture_app_server, tmp_empty_root
):
    """The B1 regression: the cure must not be worse than the crash.

    The first cut of F-884 held the per-tab lock across nodriver's bundled
    wait, so a ``wait_for_element`` blocked the concurrent click that CREATES
    the element it was waiting for — measured ``True @ 1.08 s`` before the fix,
    ``False @ 10.71 s`` after it. A wrong answer, not a slow one, which is why
    this asserts the RESULT and not a duration.

    ``#late-target`` does not exist until ``#make-late`` is clicked, so the only
    way this passes is if the two calls genuinely interleave.
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    wait_for_element = get_fn("wait_for_element")
    click_element = get_fn("click_element")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")
        await eval_js(
            iid,
            "document.getElementById('make-late') || (function(){"
            "var b=document.createElement('button');b.id='make-late';"
            "b.onclick=function(){setTimeout(function(){"
            "var d=document.createElement('div');d.id='late-target';"
            "document.body.appendChild(d);},200);};"
            "document.body.appendChild(b);return 1;})()",
        )

        found, _clicked = await asyncio.gather(
            wait_for_element(instance_id=iid, selector="#late-target", timeout=6000),
            click_element(instance_id=iid, selector="#make-late"),
        )

        assert found is True, "the waiter starved the click that creates its element"
    finally:
        await close(instance_id=iid)


async def test_a_waiter_does_not_delay_a_sibling_query_on_the_same_tab(
    fixture_app_server, tmp_empty_root
):
    """The other half of B1: a waiter must not hold the tab hostage.

    Measured before the wait moved out of the lock: a sibling ``query_elements``
    that answered in 0.25 s took 10.56 s, because the waiter held the document
    lock for nodriver's 10 s default regardless of its own 1 s budget. The
    bound here is deliberately loose (3 s) — it is guarding against a ten-second
    freeze, and a tight one would be a flake on a loaded runner.
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    wait_for_element = get_fn("wait_for_element")
    query_elements = get_fn("query_elements")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")

        async def _sibling():
            started = time.monotonic()
            found = await query_elements(instance_id=iid, selector="body")
            return found, time.monotonic() - started

        _waited, (found, elapsed) = await asyncio.gather(
            wait_for_element(instance_id=iid, selector="#never-there", timeout=1000),
            _sibling(),
        )

        assert found, "the sibling query answered nothing"
        assert elapsed < 3.0, f"a 1 s waiter held the tab for {elapsed:.2f} s"
    finally:
        await close(instance_id=iid)


async def test_mixed_resolution_shapes_do_not_race_each_other(
    fixture_app_server, tmp_empty_root
):
    """The single-, multi- and XPath paths share one document and one lock.

    They are three different nodriver entry points (``select``, ``select_all``,
    ``xpath``) reaching the same per-session node-id table, so the pin that
    matters is that they are mutually exclusive with EACH OTHER and not merely
    with themselves.
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    wait_for_element = get_fn("wait_for_element")
    query_elements = get_fn("query_elements")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")

        calls = []
        for _ in range(_CONCURRENCY):
            calls.append(
                wait_for_element(instance_id=iid, selector="body", timeout=5000)
            )
            calls.append(query_elements(instance_id=iid, selector="div"))
            calls.append(query_elements(instance_id=iid, selector="//div"))

        results = await asyncio.gather(*calls, return_exceptions=True)

        raised = [r for r in results if isinstance(r, BaseException)]
        assert not raised, f"{len(raised)}/{len(calls)} mixed resolutions raised"
    finally:
        await close(instance_id=iid)
