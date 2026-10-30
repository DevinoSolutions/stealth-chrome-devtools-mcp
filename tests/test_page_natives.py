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
  and a node no frame holds is a departure;
* the visibility reader and ``wait_for_element`` / ``query_elements`` never turn a
  failed read into "visible".
"""

from __future__ import annotations

import json
import logging

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
    "failure",
    [
        PageThrew(True),
        PageThrew(False),
        PageThrew(error=True),
        ProtocolException({"message": "Some unrelated CDP failure"}),
        left_the_document(),
        ConnectionError("socket closed"),
    ],
    ids=["throw-true", "throw-false", "throw-error", "protocol", "departed", "socket"],
)
async def test_the_object_group_is_released_after_a_failed_read(failure):
    """The leak was per row per listing, on the failing path as much as the other:
    the release is in a ``finally``."""
    node = _Node(failure=failure)

    with pytest.raises(Exception):  # noqa: B017, PT011  PERMANENT(every failure class is asserted by its own test below)
        await page_natives.run(node, SCRIPT)

    assert node.cdp_methods[-1] == "Runtime.releaseObjectGroup"
    assert node.open_groups == []


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
        UNRESOLVABLE,
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
# the readers never turn a failed read into "visible"
# ---------------------------------------------------------------------------


async def test_a_departure_during_the_visibility_read_is_not_visible():
    for message in (CONTEXT_LOST, UNRESOLVABLE):
        node = _Node(failure=ProtocolException({"message": message}))
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
