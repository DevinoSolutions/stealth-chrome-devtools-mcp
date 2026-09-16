"""F-881 against real Chrome: ``navigate`` waits for the page's OWN ``load``.

The defect was a navigation that answered at COMMIT and then slept: nodriver's
``Tab.get`` is ``Page.navigate`` plus a ``Tab.wait()`` that, with the ``Page``
domain unenabled, is a flat 0.5 s sleep. On every page that loads in under half
a second the two are indistinguishable, which is why the defect survived a green
suite and was found against a live site instead.

What makes the node below red by CONSTRUCTION rather than by timing is the
fixture, not the clock. ``/cov/slow_load.html`` commits immediately and then
holds its ``load`` open on one slow ``<img>``; the two facts the test reads —
``document.title`` and ``document.readyState`` — are set by a ``load`` listener
and by Blink respectively, and by nothing else on the page. A navigation that
returned at commit, or after any fixed sleep shorter than the subresource,
reads ``cov-before-load`` and ``interactive``. It cannot read the other answer
early, however fast the machine is.

The second node is the sensitivity control, and it is what stops the first from
degenerating into "navigate is slow": asking for ``domcontentloaded`` over the
SAME page must come back BEFORE that ``load``, with the pre-load title. If both
nodes passed while the tool simply waited for everything, this one would fail.

Both pages are served by the session fixture app, and the profile root is a
temp dir (``tmp_empty_root``), so nothing here touches a real session root or
the network.
"""

from __future__ import annotations

import time

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    sandbox_kwargs,
    warmup_once,
)
from fixture_routes import COV_TITLE_AFTER_LOAD, COV_TITLE_BEFORE_LOAD

pytestmark = integration_pytestmark()

#: How long the page's ``load`` is held open. Comfortably above any plausible
#: fixed sleep on the commit path (the defect's was 0.5 s) and comfortably
#: inside the node's own budget, so a 2-core runner still decides it.
HELD_MS = 1_800

#: The floor the elapsed time must clear for "it really waited" to mean
#: anything. Deliberately below ``HELD_MS``: the assertion that carries the
#: finding is the TITLE, and this one only rules out a fixture that forgot to
#: hold. A tight equality here would be a clock assertion on a shared runner.
MIN_WAIT_SECONDS = 1.5


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def test_navigate_waits_for_the_load_its_own_subresource_delayed(
    fixture_app_server, tmp_empty_root
):
    """``wait_until='load'`` returns only once the page's real ``load`` fired."""
    spawn = get_fn("spawn_browser")
    navigate = get_fn("navigate")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        started = time.monotonic()
        result = await navigate(
            instance_id=iid,
            url=f"{fixture_app_server}/cov/slow_load.html?ms={HELD_MS}",
            wait_until="load",
        )
        elapsed = time.monotonic() - started

        # The tool's own answer carries the title the LOAD listener set.
        assert result["title"] == COV_TITLE_AFTER_LOAD, (
            f"navigate answered title {result['title']!r} after {elapsed:.2f}s — "
            "the page only sets that title from its load listener, so this is a "
            "navigation that returned before its own load (F-881)"
        )
        assert elapsed >= MIN_WAIT_SECONDS, (
            f"navigate answered in {elapsed:.2f}s but the fixture holds load for "
            f"{HELD_MS}ms — the fixture is not holding, so this node proves nothing"
        )

        # And the page agrees, read back immediately with no settling of any
        # kind: the caller's very next call sees a fully loaded document.
        assert await eval_js(iid, "document.readyState") == "complete"
        assert await eval_js(iid, "window.__covLoaded") is True
        assert await eval_js(iid, "document.title") == COV_TITLE_AFTER_LOAD
    finally:
        await close(instance_id=iid)


async def test_domcontentloaded_comes_back_before_that_same_load(
    fixture_app_server, tmp_empty_root
):
    """The control: the three milestones are still told apart.

    An ``<img>`` blocks ``load`` and not ``DOMContentLoaded``, so asking for
    the earlier milestone over the same held page must answer with the page
    still loading. This is what makes the node above evidence about ``load``
    rather than evidence that ``navigate`` waits for everything.
    """
    spawn = get_fn("spawn_browser")
    navigate = get_fn("navigate")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        started = time.monotonic()
        result = await navigate(
            instance_id=iid,
            url=f"{fixture_app_server}/cov/slow_load.html?ms={HELD_MS}",
            wait_until="domcontentloaded",
        )
        elapsed = time.monotonic() - started

        assert elapsed < MIN_WAIT_SECONDS, (
            f"domcontentloaded took {elapsed:.2f}s over a page whose only delay "
            f"is a load-blocking image — it waited for load instead"
        )
        assert result["title"] == COV_TITLE_BEFORE_LOAD, result["title"]
        assert await eval_js(iid, "window.__covLoaded") is False
    finally:
        await close(instance_id=iid)
