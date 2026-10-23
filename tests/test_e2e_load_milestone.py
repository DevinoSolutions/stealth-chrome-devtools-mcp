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
degenerating into "navigate is slow": asking for ``domcontentloaded`` over a page
whose ``load`` is blocked must come back BEFORE that ``load``, with the pre-load
title. If both nodes passed while the tool simply waited for everything, this
one would fail.

That node does NOT read the clock (F-1000). It used to assert ``elapsed < 1.5 s``
against a page holding ``load`` for 1.8 s, and on a slow Windows runner a correct
``domcontentloaded`` took 2.20-2.52 s (3 of 45 Windows integration jobs, none of
92 on Linux and macOS) and failed. Now the image that blocks ``load`` is held by
the fault controller (``/fault/slow``) until the TEST releases it, so ``load``
cannot have fired while ``navigate`` is answering, however slow the runner is:
the page reads ``interactive`` and ``__covLoaded`` false at the return, by
construction. A ``domcontentloaded`` that waited for ``load`` never returns while
the image is held, and the bounded wait names that. Releasing the image
afterwards is the control: the SAME page then reaches ``load``.

Both pages are served by the session fixture app, so nothing here reaches the
network. Both spawns are UNNAMED, which means the shared default profile — the
browser-session root is redirected for the whole session at
``tests/conftest.py`` import time, and these nodes deliberately do NOT declare
``tmp_empty_root``: an autouse ``_warmup`` spawns a browser before any
function-scoped root fixture is set up, and ``get_settings`` is ``lru_cache``d,
so a per-test root fixture here would be decorative. The comment in
``conftest.py`` carries the measurement.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
import requests

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    sandbox_kwargs,
    warmup_once,
)
from fixture_routes import (
    COV_GATED_LOAD_PATH,
    COV_TITLE_AFTER_LOAD,
    COV_TITLE_BEFORE_LOAD,
)

pytestmark = integration_pytestmark()

#: How long the page's ``load`` is held open. Comfortably above any plausible
#: fixed sleep on the commit path (the defect's was 0.5 s) and comfortably
#: inside the node's own budget, so a 2-core runner still decides it.
HELD_MS = 1_800

#: The floor the elapsed time must clear for "it really waited" to mean
#: anything. Deliberately below ``HELD_MS``: the assertion that carries the
#: finding is the TITLE, and this one only rules out a fixture that forgot to
#: hold. A slow runner can only make the elapsed time LONGER, so a floor cannot
#: flake the way the old ceiling on the ``domcontentloaded`` node did.
MIN_WAIT_SECONDS = 1.5

#: A hang guard and nothing else. A correct ``domcontentloaded`` returns in
#: well under a second; a regression that waits for ``load`` never returns while
#: the image is held. Strictly below the fixture's own ceiling
#: (``FAULT_CEILING_SECONDS``, 25 s), because past that the controller gives up
#: on the image and ``load`` fires, which would let a regression answer late
#: instead of hanging. No assertion compares anything against this number.
HANG_GUARD_SECONDS = 15.0

#: How long the fixture may take to say the image request arrived, and how long
#: the page may take to reach ``load`` once the test has released the image.
BARRIER_TIMEOUT_SECONDS = 20.0


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def test_navigate_waits_for_the_load_its_own_subresource_delayed(
    fixture_app_server,
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


async def _get(url: str) -> dict:
    """Plain HTTP from this process, off-thread so the loop driving Chrome never
    blocks on it."""
    response = await asyncio.to_thread(requests.get, url, timeout=10)
    return response.json()


async def _until(read, what: str):
    """Poll ``read`` until it answers truthy, under :data:`BARRIER_TIMEOUT_SECONDS`.

    A bounded wait on a FACT (the fixture saw the request; the page reached
    ``load``), never a sleep standing in for one.
    """
    deadline = time.monotonic() + BARRIER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        value = await read()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError(f"{what} did not happen within {BARRIER_TIMEOUT_SECONDS:g}s")


async def _entered(base: str, token: str) -> bool:
    return (await _get(f"{base}/fault/status?token={token}"))["entered"]


async def _page_loaded(iid: str) -> bool:
    return await eval_js(iid, "window.__covLoaded") is True


async def test_domcontentloaded_comes_back_before_that_same_load(
    fixture_app_server,
):
    """The control: the three milestones are still told apart.

    An ``<img>`` blocks ``load`` and not ``DOMContentLoaded``, so asking for
    the earlier milestone over a page whose image is held must answer with the
    page still loading. This is what makes the node above evidence about
    ``load`` rather than evidence that ``navigate`` waits for everything.

    The image is held until THIS test releases it, so "load has not fired" is a
    fact about the server and not a race against how fast the runner is. The
    only clock left is :data:`HANG_GUARD_SECONDS`, which says "never returned".
    """
    spawn = get_fn("spawn_browser")
    navigate = get_fn("navigate")
    close = get_fn("close_instance")

    token = f"f1000-{uuid.uuid4().hex[:8]}"
    armed = await _get(f"{fixture_app_server}/fault/arm?token={token}")
    assert armed["token"] == token and armed["entered"] is False, armed

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        try:
            result = await asyncio.wait_for(
                navigate(
                    instance_id=iid,
                    url=f"{fixture_app_server}{COV_GATED_LOAD_PATH}?token={token}",
                    wait_until="domcontentloaded",
                ),
                HANG_GUARD_SECONDS,
            )
        except TimeoutError:
            raise AssertionError(
                f"domcontentloaded did not return in {HANG_GUARD_SECONDS:g}s over a "
                "page whose only delay is a load-blocking image the test is "
                "holding - it is waiting for load instead"
            ) from None

        # The image has not been released, so ``load`` cannot have fired. These
        # are read before anything else, from the page itself.
        assert result["title"] == COV_TITLE_BEFORE_LOAD, result["title"]
        assert await eval_js(iid, "document.readyState") == "interactive"
        assert await eval_js(iid, "window.__covLoaded") is False

        # And the hold was real: the fixture has the image request and has not
        # answered it, so this node is not "load never had anything to wait on".
        await _until(
            lambda: _entered(fixture_app_server, token),
            "the fixture receiving the held image request",
        )
        held = await _get(f"{fixture_app_server}/fault/status?token={token}")
        assert held["released"] is False and held["exited"] is False, held

        # The control: let the image go and the SAME page reaches its load.
        released = await _get(f"{fixture_app_server}/fault/release?token={token}")
        assert released["released"] is True, released
        await _until(
            lambda: _page_loaded(iid), "the released page reaching its load event"
        )
        assert await eval_js(iid, "document.title") == COV_TITLE_AFTER_LOAD
    finally:
        # Idempotent, and what frees the fixture's parked thread if a failure
        # above never got to the release.
        try:
            await _get(f"{fixture_app_server}/fault/release?token={token}")
        finally:
            await close(instance_id=iid)
