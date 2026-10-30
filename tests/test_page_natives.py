"""``page_natives.run``: THE one send of a read, pinned against a double (T-10284, T-10299).

What the isolated world does in V8 is shown in a real Chrome by
``tests/test_e2e_connectivity_read.py``. What a double CAN pin is what the product
sends and what it does with each reply:

* the commands, in order -- the page's frames, the node's world, the resolution in
  THAT world, the call with the caller's user-gesture flag, the release of the
  call's object group -- and the release on success AND on every failure;
* ``exceptionDetails`` is a failed read whatever ``result`` holds (a thrown
  ``true`` / ``false`` has a perfectly good-looking value);
* a transport failure is classified in one place: Chrome's own "the node or its
  context is gone" replies are a departure (raised as they came, for
  ``detached_element``), anything else is the read's ``ToolError``;
* the frame search: a node of another frame is tried in the next frame's world,
  whatever "not here" sounds like (a script's FOREIGN, a null object, Chrome's
  "does not belong"), and a node no frame holds is a departure -- never the first
  frame's failure;
* a failed or cancelled read leaves its object group on the tab for the next
  read to release (bounded, one wait), never spawns a task, never swallows a
  cancellation and leaves nothing behind when the loop is shut down;
* the visibility reader and ``wait_for_element`` / ``query_elements`` never turn a
  failed read into "visible".
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import time

import pytest
from nodriver.core.connection import ProtocolException

from fakes import FakeClickTarget, FakeTab, left_the_document
from fakes_elements import PageThrew
from stealth_chrome_devtools_mcp.embedded import (
    click_target,
    element_resolution,
    page_natives,
)
from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

pytestmark = pytest.mark.asyncio

SCRIPT = "(elem) => 1"
CONTEXT_LOST = "Cannot find context with specified id"
UNRESOLVABLE = (
    "Either objectId or executionContextId or uniqueContextId must be specified"
)


@pytest.fixture
def backend_log(caplog):
    """Every record on ``stealth.backend``, whatever its level, once each."""
    backend = logging.getLogger("stealth.backend")
    backend.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="stealth.backend")
    try:
        yield lambda: list(dict.fromkeys(caplog.records))
    finally:
        backend.removeHandler(caplog.handler)


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch):
    monkeypatch.setattr(element_resolution, "_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(element_resolution, "_DEFAULT_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(element_resolution, "_POLL_SECONDS", 0.0)


class _Node(FakeClickTarget):
    """A node whose page answers *answer*, or raises *failure*."""

    def __init__(self, answer=1, failure=None, **kwargs) -> None:
        super().__init__(tag="li", element_id="n", text="n", **kwargs)
        self._answer, self._failure = answer, failure

    async def apply(self, js_function, *args, **kwargs):
        self.apply_calls.append(js_function)
        if self._failure is not None:
            raise self._failure
        return self._answer


def _left(node):
    """The object groups the failed reads of *node*'s tab left, oldest first."""
    return [group for _, group in getattr(node.tab, page_natives._LEFTOVERS, [])]


class _TabOf:
    def __init__(self, *elements) -> None:
        self._elements = list(elements)

    async def query_selector_all(self, selector, *args, **kwargs):
        return list(self._elements)

    async def query_selector(self, selector, *args, **kwargs):
        return self._elements[0] if self._elements else None


# ---------------------------------------------------------------------------
# the commands
# ---------------------------------------------------------------------------


async def test_a_read_runs_in_the_isolated_world_of_the_nodes_frame():
    node = _Node(answer="x")

    assert await page_natives.run(node, SCRIPT) == "x"

    assert node.cdp_methods == [
        "Page.getFrameTree",
        "Page.createIsolatedWorld",
        "DOM.resolveNode",
        "Runtime.callFunctionOn",
        "Runtime.releaseObjectGroup",
    ]
    assert node.worlds == [("MAIN", page_natives.WORLD_NAME)]
    # resolved IN the world it was just given, never in the page's main world
    assert node.resolved_contexts == [100]
    assert page_natives.WORLD_NAME == "stealth-mcp-reads"


async def test_every_script_is_wrapped_for_the_frame_check():
    seen = []

    class _Spy(_Node):
        async def apply(self, js_function, *args, **kwargs):
            seen.append(js_function)
            return await super().apply(js_function, *args, **kwargs)

    await page_natives.run(_Spy(), SCRIPT)
    assert seen == [SCRIPT]  # the double hands the inner script on
    wrapped = page_natives.WRAPPER_JS.replace("__SCRIPT__", SCRIPT)
    assert page_natives.NATIVE_JS in wrapped
    assert f'return "{page_natives.FOREIGN}"' in wrapped
    assert wrapped.index("ownerDocument") < wrapped.index(SCRIPT)


@pytest.mark.parametrize("gesture", [False, True])
async def test_the_callers_user_gesture_flag_is_sent(gesture):
    node = _Node()
    await page_natives.run(node, SCRIPT, user_gesture=gesture)
    assert node.read_gestures == [gesture]


async def test_the_default_is_a_read_without_a_user_gesture():
    node = _Node()
    await page_natives.run(node, SCRIPT)
    assert node.read_gestures == [False]


async def test_the_click_path_sends_what_2_1_53_sent():
    """``Element.apply`` sent ``userGesture`` true for the synthetic click and
    for the probe's arm and read; the reads that only ask send false."""
    node = _Node(answer=click_target.SYNTHETIC_CLICKED)
    await click_target.synthetic_click(node, "x", click_target.InputSent())
    assert node.read_gestures == [True]

    probe = _Node(answer=json.dumps([]))
    await click_target._arm(probe, ["mousedown"])
    await click_target._read(probe)
    assert probe.read_gestures == [True, True]


# ---------------------------------------------------------------------------
# the release (finding 5)
# ---------------------------------------------------------------------------


async def test_the_object_group_is_released_after_a_successful_read():
    node = _Node()
    await page_natives.run(node, SCRIPT)
    assert node.cdp_methods[-1] == "Runtime.releaseObjectGroup"
    assert node.open_groups == []


@pytest.mark.parametrize(
    "thrown",
    [PageThrew(True), PageThrew(False), PageThrew(error=True)],
    ids=["throw-true", "throw-false", "throw-error"],
)
async def test_the_object_group_of_a_script_that_threw_is_released_at_once(thrown):
    """The call answered (with ``exceptionDetails``): the read is over, so its group
    is released inline like a successful read's, then the failure is raised."""
    node = _Node(failure=thrown)

    with pytest.raises(page_natives.ReadError):
        await page_natives.run(node, SCRIPT)

    assert node.cdp_methods[-1] == "Runtime.releaseObjectGroup"
    assert node.open_groups == [] and _left(node) == []


@pytest.mark.parametrize(
    "failure",
    [
        ProtocolException({"message": "Some unrelated CDP failure"}),
        left_the_document(),
        ConnectionError("socket closed"),
    ],
    ids=["protocol", "departed", "socket"],
)
async def test_the_object_group_of_a_failed_command_is_left_for_the_next_read(failure):
    """A failed read releases nothing and raises at once; the group is recorded on
    the tab and the next read on that tab releases it."""
    node = _Node(failure=failure)

    with pytest.raises(Exception):  # noqa: B017, PT011  PERMANENT(every failure class is asserted by its own test below)
        await page_natives.run(node, SCRIPT)

    assert "Runtime.releaseObjectGroup" not in node.cdp_methods
    left = list(_left(node))
    assert len(left) == 1 and node.open_groups == left

    node._failure = None  # the page recovers
    assert await page_natives.run(node, SCRIPT) == 1

    assert node.open_groups == [] and _left(node) == []
    assert node.cdp_methods.count("Runtime.releaseObjectGroup") == 2


async def test_each_read_has_a_group_of_its_own():
    groups = []

    class _Spy(_Node):
        async def apply(self, js_function, *args, **kwargs):
            groups.append(tuple(self.open_groups))
            return await super().apply(js_function, *args, **kwargs)

    node = _Spy()
    await page_natives.run(node, SCRIPT)
    await page_natives.run(node, SCRIPT)
    assert len(groups) == 2 and groups[0] != groups[1], groups
    assert all(len(g) == 1 for g in groups)


async def test_a_release_that_fails_does_not_replace_the_reads_answer(backend_log):
    node = _Node(answer="x")
    node.release_error = ConnectionError("gone")

    assert await page_natives.run(node, SCRIPT) == "x"

    assert any("could not release" in r.getMessage() for r in backend_log())


async def test_a_release_that_fails_does_not_mask_the_reads_failure():
    node = _Node(failure=PageThrew(True))
    node.release_error = ConnectionError("gone")

    with pytest.raises(page_natives.ReadError, match="threw"):
        await page_natives.run(node, SCRIPT)


# ---------------------------------------------------------------------------
# a failed read is a failed read (finding 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "thrown",
    [PageThrew(True), PageThrew(False), PageThrew(None), PageThrew(error=True)],
    ids=["true", "false", "null", "error"],
)
async def test_a_script_that_threw_is_a_failed_read_whatever_it_threw(thrown):
    with pytest.raises(page_natives.ReadError, match="the page-side read threw"):
        await page_natives.run(_Node(failure=thrown), SCRIPT)


async def test_a_failed_read_is_a_tool_error():
    assert issubclass(page_natives.ReadError, ToolError)


# ---------------------------------------------------------------------------
# a transport failure is classified in one place (finding 4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "No node with given id found",
        "Node with given id does not belong to the document",
        CONTEXT_LOST,
    ],
)
async def test_chromes_own_departure_replies_propagate_as_they_came(message):
    error = ProtocolException({"message": message, "code": -32000})
    with pytest.raises(ProtocolException) as caught:
        await page_natives.run(_Node(failure=error), SCRIPT)
    assert caught.value is error
    assert page_natives.departed(error)
    assert element_resolution.detached_element(error) is not None


@pytest.mark.parametrize(
    "failure",
    [
        ProtocolException({"message": "Some unrelated CDP failure"}),
        ConnectionError("socket closed"),
        TimeoutError("slow"),
        RuntimeError("boom"),
    ],
    ids=["protocol", "socket", "timeout", "runtime"],
)
async def test_every_other_failure_is_the_reads_tool_error(failure):
    with pytest.raises(page_natives.ReadError) as caught:
        await page_natives.run(_Node(failure=failure), SCRIPT)
    assert caught.value.__cause__ is failure
    assert not page_natives.departed(failure)
    assert element_resolution.detached_element(failure) is None


async def test_a_cancellation_is_not_a_failed_read():
    import asyncio

    with pytest.raises(asyncio.CancelledError):
        await page_natives.run(_Node(failure=asyncio.CancelledError()), SCRIPT)


# ---------------------------------------------------------------------------
# the frame search
# ---------------------------------------------------------------------------


async def test_a_node_of_another_frame_is_read_in_that_frames_world():
    node = _Node(answer="in-the-child")
    node.frame_ids = ("MAIN", "CHILD")
    node.frame_index = 1

    assert await page_natives.run(node, SCRIPT) == "in-the-child"

    assert [frame for frame, _ in node.worlds] == ["MAIN", "CHILD"]
    assert node.resolved_contexts == [100, 101]
    assert node.open_groups == []
    # the page ran once, in the frame that holds the node
    assert node.apply_calls == [SCRIPT]


async def test_the_main_frame_is_tried_first_and_alone_for_its_own_nodes():
    node = _Node()
    node.frame_ids = ("MAIN", "CHILD", "OTHER")
    await page_natives.run(node, SCRIPT)
    assert [frame for frame, _ in node.worlds] == ["MAIN"]


async def test_a_node_no_frame_holds_is_a_departure():
    node = _Node()
    node.frame_ids = ("MAIN", "CHILD")
    node.frame_index = 7

    with pytest.raises(page_natives.FrameGone) as caught:
        await page_natives.run(node, SCRIPT)

    assert page_natives.departed(caught.value)
    assert element_resolution.detached_element(caught.value) is not None
    assert node.open_groups == []


async def test_the_foreign_answer_is_the_products_never_a_pages():
    assert page_natives.FOREIGN.startswith("stealth.")


# ---------------------------------------------------------------------------
# the frame search classifies per frame, never per first failure (HIGH)
# ---------------------------------------------------------------------------

#: The order a reviewer's page lists its frames in: a same-site frame of another
#: origin, a ``data:`` frame, a same-origin ``srcdoc`` frame.
PAGE = ("MAIN", "OTHER-ORIGIN", "DATA", "SRCDOC")


@pytest.mark.parametrize("other_worlds", ["object", "null", "not-belong"])
@pytest.mark.parametrize("index", [1, 2, 3], ids=["other-origin", "data", "srcdoc"])
async def test_a_node_of_any_frame_is_found_whatever_the_frames_before_it_say(
    other_worlds, index
):
    """RED on 3f9cc1d: the main frame's world answers a null object for a node of
    an other-origin or ``data:`` frame, the next call had no ``objectId`` and
    failed -32602, and the search ended there as the node's departure."""
    node = _Node(answer="in-the-frame")
    node.frame_ids, node.frame_index, node.other_worlds = PAGE, index, other_worlds

    assert await page_natives.run(node, SCRIPT) == "in-the-frame"

    assert [frame for frame, _ in node.worlds] == list(PAGE[: index + 1])
    assert node.apply_calls == [SCRIPT]  # the page ran once, where the node is
    # a script runs where the world held an object: with "object" (a world shared
    # with a same-origin frame) once per frame before it, FOREIGN each time
    assert node.cdp_methods.count("Runtime.callFunctionOn") == (
        index + 1 if other_worlds == "object" else 1
    )
    assert node.open_groups == []


async def test_a_null_object_is_never_followed_by_a_call_without_an_object():
    node = _Node()
    node.frame_ids, node.frame_index, node.other_worlds = PAGE, 2, "null"

    await page_natives.run(node, SCRIPT)

    # one call, in the node's own frame; the fake raises for a call with no object
    assert node.cdp_methods.count("Runtime.callFunctionOn") == 1
    assert node.read_gestures == [False]


async def test_a_call_without_an_object_is_a_failed_read_not_a_departure():
    """The reply of 3f9cc1d's departure list: only our own bug can produce it."""

    error = ProtocolException({"message": UNRESOLVABLE, "code": -32602})
    assert not page_natives.departed(error)
    assert element_resolution.detached_element(error) is None
    with pytest.raises(page_natives.ReadError) as caught:
        await page_natives.run(_Node(failure=error), SCRIPT)
    assert caught.value.__cause__ is error


@pytest.mark.parametrize("other_worlds", ["null", "not-belong"])
async def test_a_node_no_frame_holds_is_a_departure_whatever_each_frame_says(
    other_worlds,
):
    node = _Node()
    node.frame_ids, node.frame_index, node.other_worlds = PAGE, 9, other_worlds

    with pytest.raises(page_natives.FrameGone) as caught:
        await page_natives.run(node, SCRIPT)

    assert [frame for frame, _ in node.worlds] == list(PAGE)  # every frame asked
    assert element_resolution.detached_element(caught.value) is not None
    assert node.apply_calls == []


async def test_chromes_node_gone_reply_ends_the_search_at_once():
    node = _Node()
    node.frame_ids = PAGE
    node.resolve_error = left_the_document()

    with pytest.raises(ProtocolException) as caught:
        await page_natives.run(node, SCRIPT)

    assert page_natives.departed(caught.value)
    assert [frame for frame, _ in node.worlds] == ["MAIN"]


async def test_a_sibling_frame_that_left_between_the_tree_and_its_world_is_skipped():
    """RED on 3f9cc1d: its ``No frame for given id`` ended the search as the held
    node's departure."""
    node = _Node(answer="in-the-last")
    node.frame_ids, node.frame_index = ("MAIN", "GONE", "LAST"), 2
    node.other_worlds = "null"
    node.gone_frames = frozenset({"GONE"})

    assert await page_natives.run(node, SCRIPT) == "in-the-last"


async def test_a_context_that_left_between_the_world_and_the_resolution_is_skipped():
    """A frame that navigates between ``createIsolatedWorld`` and ``resolveNode``
    says so in its own words; it is that frame's news, not the node's."""

    node = _Node(answer="elsewhere")
    node.frame_ids, node.frame_index = ("MAIN", "OTHER"), 1
    node.other_worlds = "not-belong"

    assert await page_natives.run(node, SCRIPT) == "elsewhere"


async def test_the_frame_that_answered_is_tried_first_next_time():
    node = _Node()
    node.frame_ids, node.frame_index, node.other_worlds = PAGE, 3, "null"

    await page_natives.run(node, SCRIPT)
    first = list(node.cdp_methods)
    node.worlds.clear()
    node.cdp_methods.clear()
    await page_natives.run(node, SCRIPT)

    assert "Page.getFrameTree" in first
    assert [frame for frame, _ in node.worlds] == ["SRCDOC"]
    assert node.cdp_methods == [
        "Page.createIsolatedWorld",
        "DOM.resolveNode",
        "Runtime.callFunctionOn",
        "Runtime.releaseObjectGroup",
    ]


async def test_a_remembered_frame_that_no_longer_holds_the_node_falls_back():
    node = _Node()
    node.frame_ids, node.frame_index, node.other_worlds = PAGE, 3, "null"
    await page_natives.run(node, SCRIPT)

    node.frame_index = 0  # the node is now in the main frame (re-resolved)
    node.worlds.clear()
    assert await page_natives.run(node, SCRIPT) == 1
    assert [frame for frame, _ in node.worlds] == ["SRCDOC", "MAIN"]

    node.frame_index = 9  # and then nowhere
    with pytest.raises(page_natives.FrameGone):
        await page_natives.run(node, SCRIPT)


# ---------------------------------------------------------------------------
# the release never holds the caller, never swallows a cancellation, never
# outlives the loop (MED; LOW of the review of f7734fa)
# ---------------------------------------------------------------------------


class _Hangs(_Node):
    """A page that is blocked: the script never returns (a ``confirm()``).

    ``hangs`` is switched off for the read that follows (the page unblocked).
    """

    hangs = True

    async def apply(self, js_function, *args, **kwargs):
        if self.hangs:
            self.apply_calls.append(js_function)
            await asyncio.Event().wait()
        return await super().apply(js_function, *args, **kwargs)


@pytest.fixture
def short_releases(monkeypatch):
    monkeypatch.setattr(page_natives, "RELEASE_WAIT_S", 0.05)


async def _ends_within(coro, seconds):
    """The task of *coro* once it has ended, failing the test if it takes longer.

    Bounded on purpose: the defect these tests pin is a hang, and a hang that is
    awaited unbounded would hang the lane (no global pytest timeout).
    """
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=seconds)
    if not done:
        task.cancel()
        pytest.fail(f"still running after {seconds} s")
    return task


async def _cancelled_mid_read(node):
    """Start a read on *node*, let it block in the page, cancel it (``wait_for``)."""
    read = asyncio.ensure_future(page_natives.run(node, SCRIPT))
    await asyncio.sleep(0.02)
    read.cancel()
    await _ends_within(asyncio.wait({read}), 0.2)
    return read


async def test_a_release_that_never_answers_does_not_hold_a_finished_read(
    short_releases, backend_log
):
    node = _Node(answer="x")
    node.release_hangs = True

    task = await _ends_within(page_natives.run(node, SCRIPT), 0.15)

    assert task.result() == "x"
    assert any("was not released" in r.getMessage() for r in backend_log())


async def test_a_cancelled_read_releases_nothing_spawns_nothing_and_stays_cancelled():
    node = _Hangs()
    node.release_hangs = True  # a release sent now would hang: none is sent
    before = asyncio.all_tasks()

    read = await _cancelled_mid_read(node)

    assert read.cancelled()  # a CancelledError, not something it was turned into
    assert asyncio.all_tasks() - {read} == before - {read}
    assert "Runtime.releaseObjectGroup" not in node.cdp_methods
    assert _left(node) == node.open_groups and len(node.open_groups) == 1


async def test_the_next_read_releases_the_group_a_cancelled_read_left():
    node = _Hangs()
    await _cancelled_mid_read(node)
    node.hangs = False

    assert await page_natives.run(node, SCRIPT) == 1

    assert node.open_groups == [] and _left(node) == []
    # the leftover, then the read's own
    assert node.cdp_methods.count("Runtime.releaseObjectGroup") == 2


async def test_a_leftover_release_that_hangs_does_not_hold_the_next_read(
    monkeypatch, backend_log
):
    """Bounded: the next read answers within its budget plus ONE release wait,
    however many groups are waiting, and the stuck ones are dropped, not retried."""
    node = _Hangs()
    reads = [asyncio.ensure_future(page_natives.run(node, SCRIPT)) for _ in range(3)]
    await asyncio.sleep(0.02)
    for read in reads:
        read.cancel()
    await _ends_within(asyncio.wait(reads), 0.2)
    assert len(_left(node)) == 3
    node.hangs = False
    node.release_hangs = True

    monkeypatch.setattr(page_natives, "RELEASE_WAIT_S", 0.2)  # 3 in turn: 0.6 s
    started = time.monotonic()

    task = await _ends_within(page_natives.run(node, SCRIPT), 1.0)

    assert task.result() == 1
    assert time.monotonic() - started < 0.45  # one wait, not one per group
    assert _left(node) == []  # dropped, not kept to be retried forever
    assert any("was not released" in r.getMessage() for r in backend_log())


async def test_a_failed_leftover_release_is_logged_and_never_the_reads_result(
    backend_log,
):
    node = _Hangs()
    await _cancelled_mid_read(node)
    node.hangs = False
    node.release_error = ConnectionError("gone")

    assert await page_natives.run(node, SCRIPT) == 1

    assert any("could not release" in r.getMessage() for r in backend_log())
    assert _left(node) == []


@pytest.mark.parametrize(
    "settle", [0, 0.02], ids=["before-the-releases-start", "releases-in-flight"]
)
async def test_a_read_cancelled_while_releasing_leftovers_puts_them_back(settle):
    """The review of 8c19f72: a read cancelled after it took the leftovers off the
    tab, but before their releases finished, forgot them, so no later read could
    release them and they lived until a navigation."""
    node = _Hangs()
    await _cancelled_mid_read(node)
    waiting = list(_left(node))
    assert len(waiting) == 1
    node.hangs = False
    node.release_hangs = True

    read = asyncio.ensure_future(page_natives.run(node, SCRIPT))
    await asyncio.sleep(settle)
    read.cancel()
    await _ends_within(asyncio.wait([read]), 0.2)

    assert read.cancelled()
    assert _left(node) == waiting


async def test_groups_put_back_go_ahead_of_newer_ones_so_the_cap_drops_the_oldest(
    backend_log,
):
    """The review of 03f018c: put back after a group a concurrent read left
    meanwhile, the full list made the cap evict that NEWER group."""
    node = _Hangs()
    cap = page_natives.MAX_LEFTOVERS
    old = [f"old-{n}" for n in range(cap)]
    for group in old:
        page_natives._leave(node.tab, group)
    node.hangs = False
    node.release_hangs = True

    read = asyncio.ensure_future(page_natives.run(node, SCRIPT))
    await asyncio.sleep(0.02)  # all `cap` releases in flight
    page_natives._leave(node.tab, "newer")  # another read failed meanwhile
    read.cancel()
    await _ends_within(asyncio.wait([read]), 0.2)

    assert read.cancelled()
    assert _left(node) == [*old[1:], "newer"]
    dropped = [
        r.getMessage() for r in backend_log() if "dropped unreleased" in r.getMessage()
    ]
    assert len(dropped) == 1 and "old-0" in dropped[0]


async def test_a_read_cancelled_during_its_own_release_leaves_the_group():
    """The review of 3b9e890: the read had its answer, then was cancelled while
    its own release waited (behind another send on the tab); the group was
    neither released nor recorded, so no later read could release it."""
    node = _Hangs()
    node.hangs = False
    node.release_hangs = True

    read = asyncio.ensure_future(page_natives.run(node, SCRIPT))
    await asyncio.sleep(0.02)  # answered; its release in flight
    assert "Runtime.callFunctionOn" in node.cdp_methods
    read.cancel()
    await _ends_within(asyncio.wait([read]), 0.2)

    assert read.cancelled()
    assert _left(node) == node.open_groups and len(node.open_groups) == 1


async def test_overlapping_releasers_cancelled_in_turn_still_drop_the_oldest(
    backend_log,
):
    """The review of 941766f: read 1 takes the `cap` old groups, a newer one is
    left, read 2 takes it; read 1 cancelled puts the old ones back, then read 2
    cancelled put the newer one AHEAD of them and the cap evicted it."""
    node = _Hangs()
    cap = page_natives.MAX_LEFTOVERS
    old = [f"old-{n}" for n in range(cap)]
    for group in old:
        page_natives._leave(node.tab, group)
    node.hangs = False
    node.release_hangs = True

    first = asyncio.ensure_future(page_natives.run(node, SCRIPT))
    await asyncio.sleep(0.02)  # the old groups' releases in flight
    page_natives._leave(node.tab, "newer")
    second = asyncio.ensure_future(page_natives.run(node, SCRIPT))
    await asyncio.sleep(0.02)  # the newer group's release in flight
    first.cancel()
    await _ends_within(asyncio.wait([first]), 0.2)
    second.cancel()
    await _ends_within(asyncio.wait([second]), 0.2)

    assert first.cancelled() and second.cancelled()
    assert _left(node) == [*old[1:], "newer"]
    dropped = [
        r.getMessage() for r in backend_log() if "dropped unreleased" in r.getMessage()
    ]
    assert len(dropped) == 1 and "old-0" in dropped[0]


async def test_a_tab_whose_reads_keep_failing_keeps_a_bounded_list(backend_log):
    node = _Hangs()
    cap = page_natives.MAX_LEFTOVERS
    reads = [
        asyncio.ensure_future(page_natives.run(node, SCRIPT)) for _ in range(cap + 8)
    ]
    await asyncio.sleep(0.05)  # all blocked in the page together
    assert len(node.open_groups) == cap + 8
    for read in reads:
        read.cancel()
    await _ends_within(asyncio.wait(reads), 1.0)

    kept = _left(node)
    assert len(kept) == cap and len(set(kept)) == cap
    assert set(kept) < set(node.open_groups)
    dropped = [r for r in backend_log() if "dropped unreleased" in r.getMessage()]
    assert len(dropped) == 8


async def test_a_cancellation_during_shutdown_leaves_no_task_and_no_warning():
    """The review of f7734fa: the release was handed to a task created while the
    loop was shutting down, after ``asyncio.run`` had listed the tasks to cancel;
    it never ran its own timeout, the closed loop kept it pending, and collection
    said ``Task was destroyed but it is pending!`` (retaining the Tab each time)."""
    seen: list[str] = []
    holder: list[_Hangs] = []

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _l, ctx: seen.append(ctx["message"]))
        node = _Hangs()
        node.release_hangs = True
        holder.append(node)
        read = asyncio.ensure_future(page_natives.run(node, SCRIPT))
        await asyncio.sleep(0.02)  # the read is blocked in the page; the loop ends
        assert not read.done()

    pending_before = {
        id(t) for t in gc.get_objects() if isinstance(t, asyncio.Task) and not t.done()
    }
    await asyncio.to_thread(asyncio.run, scenario())
    node = holder.pop()
    gc.collect()

    pending_after = [
        t
        for t in gc.get_objects()
        if isinstance(t, asyncio.Task) and not t.done() and id(t) not in pending_before
    ]
    assert pending_after == []
    assert [m for m in seen if "destroyed" in m] == []
    assert _left(node) == node.open_groups and len(node.open_groups) == 1


async def test_the_synthetic_click_on_a_blocked_page_answers_within_its_budget(
    short_releases, monkeypatch
):
    """The caller of 2.1.53's bound: ``INPUT_BUDGET_S``, and the ledger that makes
    the answer "may have reached the page, so it was not repeated", not a crash."""
    monkeypatch.setattr(click_target, "INPUT_BUDGET_S", 0.05)
    node = _Hangs()
    node.release_hangs = True
    ledger = click_target.InputSent()

    task = await _ends_within(
        click_target.synthetic_click(node, "x", ledger),
        0.15,
    )

    assert isinstance(task.exception(), TimeoutError)
    assert ledger.sent is True
    assert len(_left(node)) == 1


# ---------------------------------------------------------------------------
# one connectivity read per listed row (LOW)
# ---------------------------------------------------------------------------


async def test_a_visible_listing_reads_each_row_once():
    node = _Node(answer=True)
    rows = await DOMHandler.query_elements(_TabOf(node), "li", visible_only=True)

    assert len(rows) == 1
    assert node.cdp_methods.count("Runtime.callFunctionOn") == 1


async def test_an_unfiltered_listing_still_reads_connectivity_once_per_row():
    node = _Node(answer=click_target.CONNECTED_YES)
    rows = await DOMHandler.query_elements(_TabOf(node), "li", visible_only=False)

    assert len(rows) == 1
    assert node.cdp_methods.count("Runtime.callFunctionOn") == 1


async def test_a_removed_node_is_dropped_from_a_visible_listing_by_its_one_read():
    node = _Node(answer=click_target.CONNECTED_NO)

    assert await DOMHandler.query_elements(_TabOf(node), "li", visible_only=True) == []
    assert node.cdp_methods.count("Runtime.callFunctionOn") == 1


# ---------------------------------------------------------------------------
# the readers never turn a failed read into "visible"
# ---------------------------------------------------------------------------


async def test_a_departure_during_the_visibility_read_is_not_visible():
    node = _Node(failure=ProtocolException({"message": CONTEXT_LOST}))
    assert await element_resolution.node_is_visible(node, "the node") is False


async def test_an_unrelated_failure_during_the_visibility_read_raises():
    node = _Node(failure=ConnectionError("socket closed"))
    with pytest.raises(ToolError, match="socket closed"):
        await element_resolution.node_is_visible(node, "the node")


async def test_wait_for_element_is_not_satisfied_when_the_node_left_mid_read():
    """RED on 7bdbdab: the visibility catch swallowed the failure, and the wait
    returned True for a selector that had gone."""
    node = _Node(failure=ProtocolException({"message": CONTEXT_LOST}))
    assert (
        await DOMHandler.wait_for_element(_TabOf(node), "li", timeout=100, visible=True)
        is False
    )


async def test_wait_for_element_raises_for_a_transport_failure_of_the_read():
    node = _Node(failure=ConnectionError("socket closed"))
    with pytest.raises(ToolError, match="socket closed"):
        await DOMHandler.wait_for_element(
            _TabOf(node), "li", timeout=5000, visible=True
        )
    assert len(node.apply_calls) == 1


class _AfterTheRefresh(FakeClickTarget):
    """Answers the connectivity read, then fails the visibility read."""

    def __init__(self, failure) -> None:
        super().__init__(tag="li", element_id="n", text="n")
        self._failure = failure

    async def apply(self, js_function, *args, **kwargs):
        if "getComputedStyle" in js_function:
            self.apply_calls.append(js_function)
            raise self._failure
        return await super().apply(js_function, *args, **kwargs)


async def test_query_elements_does_not_list_a_node_whose_visibility_read_failed():
    """RED on 7bdbdab: the catch around the read left ``is_visible = True`` and
    the element was listed."""
    node = _AfterTheRefresh(ConnectionError("socket closed"))
    with pytest.raises(ToolError, match="socket closed"):
        await DOMHandler.query_elements(_TabOf(node), "li", visible_only=True)


async def test_query_elements_drops_a_node_that_left_during_the_visibility_read():
    node = _AfterTheRefresh(ProtocolException({"message": CONTEXT_LOST}))
    assert await DOMHandler.query_elements(_TabOf(node), "li", visible_only=True) == []


async def test_the_connectivity_reader_raises_for_a_transport_failure():
    node = _Node(failure=ConnectionError("socket closed"))
    with pytest.raises(ToolError, match="socket closed"):
        await element_resolution.refresh_element(FakeTab(), node)
    with pytest.raises(ToolError, match="socket closed"):
        await DOMHandler.query_elements(_TabOf(node), "li", visible_only=False)


# ---------------------------------------------------------------------------
# the aim record's exact shape
# ---------------------------------------------------------------------------

_GOOD = {
    "connected": click_target.CONNECTED_YES,
    "rendered": True,
    "rect": {"left": 0, "top": 0, "width": 1, "height": 1},
    "point": {"x": 0, "y": 0},
    "target": {"tag": "b", "id": "", "classes": []},
    "hit": {"tag": "b", "id": "", "classes": []},
    "hit_is_target": True,
    "disabled": False,
    "under_disabled": False,
    "pointer_events": "auto",
    "visibility": "visible",
}


async def test_the_exact_aim_record_is_accepted():
    node = _Node(answer=json.dumps(_GOOD))
    assert await click_target.aim(node, "li") == _GOOD
    nullable = {**_GOOD, "point": None, "hit": None}
    assert await click_target.aim(_Node(answer=json.dumps(nullable)), "li") == nullable


@pytest.mark.parametrize("field", sorted(_GOOD))
async def test_an_aim_record_missing_a_field_is_a_failed_read(field):
    record = {k: v for k, v in _GOOD.items() if k != field}
    with pytest.raises(ToolError, match=f"no {field} field"):
        await click_target.aim(_Node(answer=json.dumps(record)), "li")


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("connected", True),
        ("rendered", "yes"),
        ("rect", []),
        ("target", None),
        ("hit_is_target", 1),
        ("disabled", "false"),
        ("visibility", 3),
    ],
)
async def test_an_aim_record_with_a_field_of_the_wrong_type_is_a_failed_read(
    field, wrong
):
    record = {**_GOOD, field: wrong}
    with pytest.raises(ToolError, match=f"the {field} field"):
        await click_target.aim(_Node(answer=json.dumps(record)), "li")


@pytest.mark.parametrize("answer", [None, True, 3, [], "[]", "not json", '"s"'])
async def test_an_aim_answer_that_is_no_record_is_a_failed_read(answer):
    with pytest.raises(ToolError, match="could not read where a click"):
        await click_target.aim(_Node(answer=answer), "li")
