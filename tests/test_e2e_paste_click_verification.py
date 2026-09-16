"""F-876 in a REAL Chrome: the paste is checked, and the click says where it went.

The hermetic half (``tests/test_paste_click_verification.py``) pins the frames
and the record shape. This half pins what Chrome DOES, because every claim F-876
rests on is a claim about Blink and about CSS hit-testing:

* a ``readonly``/``range``/``date``/``color`` control and a non-editable
  ``<div>`` each take ``Input.insertText`` and move nothing — the shape
  ``paste_text`` answered ``True`` for;
* an overlay above the target receives the coordinate click instead of it, and
  ``document.elementFromPoint`` at the click point says so;
* ``pointer-events:none`` / zero-size / ``visibility:hidden`` send the click to
  ``<body>``, and ``display:none`` has no box at all, so the tool falls back to
  the synthetic, untrusted ``el.click()``.

Runs against ``interactions.html``, whose F-876 block adds those targets and
logs ``click:<id>:<trust>`` for each. Its own session root is a ``tmp_path``
(``tmp_empty_root``), so no run of this file can touch a real profile.
"""

from __future__ import annotations

import json

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    read_actions,
    sandbox_kwargs,
    wait_for_js,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

pytestmark = integration_pytestmark()


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def _wait_action(iid, entry, timeout=5.0):
    js = f"window.__actions.indexOf({json.dumps(entry)}) >= 0"
    return await wait_for_js(iid, js, True, timeout=timeout)


# ---------------------------------------------------------------------------
# paste_text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "selector,text",
    [
        ("#readonly-input", "INJECT"),
        ("#range-input", "80"),
        ("#color-input", "#123456"),
        ("#plain-div", "INJECT"),
    ],
)
async def test_a_control_that_refuses_the_paste_raises(
    fixture_app_server, tmp_empty_root, selector, text
):
    """The shapes that refuse the insert on EVERY build measured (finding §2a).

    Each takes the ``Input.insertText`` and leaves its text exactly where it was.
    ``paste_text`` used to answer ``True`` for all of them.

    ``<input type="date">`` refused the insert on the local Chrome 152 build and
    is in the finding's matrix, but it is deliberately NOT parametrized here:
    PR #110's gate measured all three CI cells ACCEPTING digits into a date
    field, so a pin asserting a refusal would be pinning one build's behaviour.
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    paste_text = get_fn("paste_text")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")

        with pytest.raises(ToolError) as caught:
            await paste_text(instance_id=iid, selector=selector, text=text)

        message = str(caught.value)
        assert selector in message
        assert text not in message  # shape and count only, never the content
    finally:
        await close(instance_id=iid)


@pytest.mark.parametrize(
    "selector,text,read",
    [
        ("#number-input", "42", "value"),
        ("#key-probe", "usb c hub", "value"),
        ("#enter-input", "héllo 日本語", "value"),
        ("#editable-div", "pasted-into-contenteditable", "textContent"),
    ],
)
async def test_a_control_that_accepts_the_paste_still_succeeds(
    fixture_app_server, tmp_empty_root, selector, text, read
):
    """The guard against over-correcting, contenteditable included."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    paste_text = get_fn("paste_text")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")

        assert await paste_text(instance_id=iid, selector=selector, text=text)
        assert (
            await eval_js(iid, f"document.querySelector({selector!r}).{read}") == text
        )
    finally:
        await close(instance_id=iid)


# ---------------------------------------------------------------------------
# click_element
# ---------------------------------------------------------------------------


async def test_the_record_names_the_overlay_that_ate_the_click(
    fixture_app_server, tmp_empty_root
):
    """#covered-btn is fully covered by #overlay-trap. The page already proved
    the overlay receives the click; what is new is that the TOOL says so."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    click = get_fn("click_element")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")

        record = await click(instance_id=iid, selector="#covered-btn")

        assert await _wait_action(iid, "click:overlay-trap")
        assert record["dispatch"] == "coordinate"
        assert record["hit"]["id"] == "overlay-trap"
        assert record["hit_is_target"] is False
        assert record["reason"] == "covered"
        assert record["target"]["id"] == "covered-btn"
    finally:
        await close(instance_id=iid)


async def test_a_click_that_reaches_its_target_says_so(
    fixture_app_server, tmp_empty_root
):
    """The positive control: #pen-covered-btn's overlay is pointer-events:none,
    so the real click passes through and the hit IS the target."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    click = get_fn("click_element")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")

        record = await click(instance_id=iid, selector="#pen-covered-btn")

        assert await _wait_action(iid, "click:pen-covered-btn")
        assert record["dispatch"] == "coordinate"
        assert record["hit_is_target"] is True
        assert record["reason"] is None
    finally:
        await close(instance_id=iid)


@pytest.mark.parametrize(
    "selector,expected",
    [
        ("#disabled-btn", "disabled"),
        ("#pe-none-btn", "pointer-events-none"),
        ("#zero-size-btn", "zero-size"),
        ("#vis-hidden-btn", "not-visible"),
        ("#offviewport-btn", "off-viewport"),
    ],
)
async def test_a_click_the_target_cannot_receive_is_named(
    fixture_app_server, tmp_empty_root, selector, expected
):
    """Four shapes that each take a real trusted coordinate click and leave the
    target with nothing (measured, Chrome 152). The tool answered ``True`` for
    every one of them, indistinguishably from a click that worked."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    click = get_fn("click_element")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")

        record = await click(instance_id=iid, selector=selector)

        assert record["dispatch"] == "coordinate"
        assert record["reason"] == expected
        # the target really did receive nothing
        assert not [
            a for a in await read_actions(iid) if a.startswith(f"click:{selector[1:]}")
        ]
    finally:
        await close(instance_id=iid)


async def test_a_target_with_no_box_is_reported_as_a_synthetic_click(
    fixture_app_server, tmp_empty_root
):
    """``display:none``: ``Element.mouse_click`` raises (no content quads) and
    the tool falls back to the in-page ``el.click()``. The page logs it as
    UNTRUSTED, and the record now says ``synthetic`` instead of ``True``."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    click = get_fn("click_element")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")

        record = await click(instance_id=iid, selector="#display-none-btn")

        assert await _wait_action(iid, "click:display-none-btn:untrusted")
        assert record["dispatch"] == "synthetic"
        assert record["reason"] == "not-rendered"
        assert record["point"] is None
    finally:
        await close(instance_id=iid)


async def test_the_click_point_is_the_point_chrome_was_clicked_at(
    fixture_app_server, tmp_empty_root
):
    """The record's point must be the one ``Element.mouse_click`` used, which is
    the centre of ``getClientRects()[0]`` — measured byte-equal to nodriver's
    ``Position(quads[0]).center``, while the BOUNDING box's centre is 28.5 px off
    for an element wrapped over several line boxes."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    click = get_fn("click_element")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")

        record = await click(instance_id=iid, selector="#fidelity-btn")
        expected = json.loads(
            await eval_js(
                iid,
                "(() => { const r = document.querySelector('#fidelity-btn')"
                ".getClientRects()[0];"
                " return JSON.stringify([r.left + r.width/2, r.top + r.height/2]);"
                " })()",
            )
        )

        assert record["point"]["x"] == pytest.approx(expected[0])
        assert record["point"]["y"] == pytest.approx(expected[1])
    finally:
        await close(instance_id=iid)
