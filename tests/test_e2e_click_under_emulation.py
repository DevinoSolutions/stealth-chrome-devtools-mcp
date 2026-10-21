"""F-1017 in a REAL Chrome: ``click_element`` lands where it aimed under a
device-metrics override, and the page's own event target says so.

``Input.dispatchMouseEvent`` takes widget coordinates. Under
``Emulation.setDeviceMetricsOverride`` carrying a ``scale`` (what DevTools' device
mode sets to fit the emulated screen into the window) the page therefore sees
``x / scale``, while ``getClientRects``, ``getContentQuads`` and
``elementFromPoint`` stay in CSS pixels. A click aimed at CSS (100, 290) arrived at
(80, 232) at ``scale: 1.25`` and hit ``<body>``; the tool's own hit-test, taken at
the unscaled point, still named the button, so the miss was silent (T-5907, seen on
a 360 px walk of unotes.net).

Every assertion is read from the page's capturing ``mousedown`` listener
(``emulated_click.html``): the REAL event target and the REAL ``clientX/Y``, never
the tool's own record. Each wait is bounded; no pytest-wide timeout exists.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    sandbox_kwargs,
    wait_for_js,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded import click_target, tab_binding
from stealth_chrome_devtools_mcp.embedded import tool_runtime as runtime

pytestmark = integration_pytestmark()

#: The button's centre in CSS pixels: (60 + 80/2, 260 + 60/2).
CENTRE = (100.0, 290.0)

#: Bound on waiting for the page to report the press it was sent.
HIT_BUDGET_S = 5.0


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def _hits(iid: str) -> list[dict]:
    """What the page's own capturing listener recorded, within a bounded wait."""
    deadline = asyncio.get_running_loop().time() + HIT_BUDGET_S
    while True:
        seen = json.loads(await eval_js(iid, "JSON.stringify(window.__hits)"))
        if seen or asyncio.get_running_loop().time() > deadline:
            return seen
        await asyncio.sleep(0.1)


@pytest.mark.parametrize("scale", [1.0, 1.25, 0.8])
async def test_click_reaches_the_button_under_a_360px_mobile_override(
    fixture_app_server, tmp_empty_root, scale
):
    """RED before the fix at 1.25 and 0.8: the press landed on ``<body>`` at
    ``centre / scale`` and ``click_element`` still answered ``reason: None``.
    ``1.0`` is the guard that the correction does nothing when there is nothing
    to correct (mobile, ``deviceScaleFactor`` 3, no ``scale``)."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    click = get_fn("click_element")
    cdp = get_fn("execute_cdp_command")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/emulated_click.html")
        override = {
            "width": 360,
            "height": 740,
            "deviceScaleFactor": 3,
            "mobile": True,
        }
        if scale != 1.0:
            override["scale"] = scale
        await cdp(
            instance_id=iid,
            command="Emulation.setDeviceMetricsOverride",
            params=override,
        )
        assert await eval_js(iid, "window.innerWidth") == 360

        record = await click(instance_id=iid, selector="#target")

        hits = await _hits(iid)
        assert hits, "the page received no mousedown at all"
        assert [h["id"] for h in hits] == ["target"], (record, hits)
        assert hits[0]["trusted"] is True
        assert abs(hits[0]["x"] - CENTRE[0]) <= 1.5, hits
        assert abs(hits[0]["y"] - CENTRE[1]) <= 1.5, hits
        assert record["dispatch"] == "coordinate"
        assert record["reason"] is None
    finally:
        await close(instance_id=iid)


async def _emulate(iid: str, scale: float) -> None:
    override = {"width": 360, "height": 740, "deviceScaleFactor": 3, "mobile": True}
    if scale != 1.0:
        override["scale"] = scale
    await get_fn("execute_cdp_command")(
        instance_id=iid, command="Emulation.setDeviceMetricsOverride", params=override
    )
    assert await eval_js(iid, "window.innerWidth") == 360


async def _in_a_page(fixture_app_server, body):
    spawn, close = get_fn("spawn_browser"), get_fn("close_instance")
    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{fixture_app_server}/emulated_click.html")
        await body(iid)
    finally:
        await close(instance_id=iid)


@pytest.mark.parametrize("scale", [1.0, 0.8])
async def test_click_reaches_an_element_at_the_edge_of_a_shrunk_viewport(
    fixture_app_server, tmp_empty_root, scale
):
    """RED before the fix at 0.8: the probe at the aimed point (315, 660) reached the
    page at 393, beyond the 360 px viewport, and was dropped; the old code then
    claimed "no scale" and pressed at the unscaled point."""

    async def body(iid):
        await _emulate(iid, scale)
        record = await get_fn("click_element")(instance_id=iid, selector="#edge")
        hits = await _hits(iid)
        assert [h["id"] for h in hits] == ["edge"], (record, hits)
        assert record["reason"] is None

    await _in_a_page(fixture_app_server, body)


@pytest.mark.parametrize("scale", [1.0, 0.8, 1.25])
async def test_click_reaches_a_button_inside_an_iframe(
    fixture_app_server, tmp_empty_root, scale
):
    """RED before the fix, even at 1.0: the aim was the frame-relative
    ``getClientRects`` point (50, 180) of the TOP viewport, so the press went to the
    page behind the frame and the tool still answered ``reason: null``."""

    async def body(iid):
        ready = (
            "(function(){try{return !!document.getElementById('frame')"
            ".contentDocument.getElementById('ib');}catch(e){return false;}})()"
        )
        assert await wait_for_js(iid, ready, True, timeout=HIT_BUDGET_S)
        await _emulate(iid, scale)
        record = await get_fn("click_element")(
            instance_id=iid, selector="#ib", text_match="Inner Button"
        )
        inner = json.loads(
            await eval_js(
                iid,
                "JSON.stringify(document.getElementById('frame').contentWindow.__hits)",
            )
        )
        assert inner == ["ib"], (record, inner, await _hits(iid))
        assert record["reason"] is None

    await _in_a_page(fixture_app_server, body)


@pytest.mark.parametrize("scale", [1.0, 0.8])
async def test_click_reaches_a_button_inside_a_closed_shadow_root(
    fixture_app_server, tmp_empty_root, scale
):
    """``composedPath`` stops at the host of a closed root, so a verification that
    asks "is the element in the path" calls a CORRECT click a miss."""

    async def body(iid):
        await _emulate(iid, scale)
        record = await get_fn("click_element")(
            instance_id=iid, selector="#sb", text_match="Shadow Button"
        )
        shadow = json.loads(await eval_js(iid, "JSON.stringify(window.__shadow)"))
        assert shadow == ["shadow-button"], (record, shadow, await _hits(iid))

    await _in_a_page(fixture_app_server, body)


async def test_arming_the_probe_twice_leaves_one_listener(
    fixture_app_server, tmp_empty_root
):
    """RED before the fix: a second arm overwrote the handle to the first handler,
    which stayed on ``window`` and logged ``composedPath`` for every later mouse
    event. The probe is also not an own property name of ``window`` while armed."""

    async def body(iid):
        arm = click_target.ARM_PROBE_JS.replace("__KINDS__", '["mousemove"]')
        read = click_target.READ_PROBE_JS
        counted = await eval_js(
            iid,
            f"""(() => {{
                const real = Event.prototype.composedPath;
                let calls = 0;
                Event.prototype.composedPath = function () {{
                    calls += 1;
                    return real.call(this);
                }};
                try {{
                    const arm = ({arm}), read = ({read});
                    arm(document.body);
                    const named = Object.getOwnPropertyNames(window)
                        .filter((n) => n.indexOf('stealth') !== -1);
                    arm(document.body);
                    read(document.body);
                    window.dispatchEvent(new MouseEvent('mousemove'));
                    return JSON.stringify({{calls: calls, named: named}});
                }} finally {{
                    Event.prototype.composedPath = real;
                }}
            }})()""",
        )
        assert json.loads(counted) == {"calls": 0, "named": []}, counted

    await _in_a_page(fixture_app_server, body)


async def test_click_under_mobile_auto_zoom_out_lands_one_to_one(
    fixture_app_server, tmp_empty_root
):
    """The state the uNotes report suspected (T-5907 addendum): mobile=true at 360 over
    a 450 px layout, so Chrome fits it at ``visualViewport.scale`` 0.8, with NO
    ``scale`` in the override. Measured on Chrome 155: a raw
    ``Input.dispatchMouseEvent`` AND the old ``click_element`` land 1:1 here
    (layout and input coordinates agree; only the override's ``scale`` parameter
    moves them), so this is a guard, green on the old code too: the probe must find
    nothing to correct, and must not invent a correction from the 0.8."""
    spawn, close = get_fn("spawn_browser"), get_fn("close_instance")
    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{fixture_app_server}/emulated_click_wide.html")
        await get_fn("execute_cdp_command")(
            instance_id=iid,
            command="Emulation.setDeviceMetricsOverride",
            params={
                "width": 360,
                "height": 740,
                "deviceScaleFactor": 3,
                "mobile": True,
            },
        )
        zoom = json.loads(
            await eval_js(iid, "JSON.stringify([visualViewport.scale, innerWidth])")
        )
        assert abs(zoom[0] - 0.8) < 0.01 and zoom[1] == 450, zoom

        record = await get_fn("click_element")(instance_id=iid, selector="#target")

        hits = await _hits(iid)
        assert [h["id"] for h in hits] == ["target"], (record, hits)
        assert abs(hits[0]["x"] - CENTRE[0]) <= 1.5, hits
        assert abs(hits[0]["y"] - CENTRE[1]) <= 1.5, hits
        assert record["reason"] is None
    finally:
        await close(instance_id=iid)


async def exact_report_sequence(base: str, headless: bool) -> dict:
    """The uNotes report, call for call (T-5907): ``spawn_browser()`` with no
    arguments, ``Emulation.setFocusEmulationEnabled``, then
    ``setDeviceMetricsOverride`` 360x780 / deviceScaleFactor 2 / mobile with NO
    ``scale``, applied BEFORE navigating. A raw ``Input.dispatchMouseEvent`` and
    ``click_element`` are then aimed at the same button, and every CDP command the
    click sends is named. ``headless=False`` is for a one-off local run: the
    report was headed, on a monitor whose native ratio is 2.5."""
    spawn, close = get_fn("spawn_browser"), get_fn("close_instance")
    cdp = get_fn("execute_cdp_command")
    kwargs = {"headless": True} if headless else {}
    iid = (await spawn(**kwargs, **sandbox_kwargs()))["instance_id"]
    try:
        await cdp(
            instance_id=iid,
            command="Emulation.setFocusEmulationEnabled",
            params={"enabled": True},
        )
        await cdp(
            instance_id=iid,
            command="Emulation.setDeviceMetricsOverride",
            params={
                "width": 360,
                "height": 780,
                "deviceScaleFactor": 2,
                "mobile": True,
            },
        )
        await navigate_and_settle(iid, f"{base}/emulated_click.html")
        for kind, buttons in (("mousePressed", 1), ("mouseReleased", 0)):
            await cdp(
                instance_id=iid,
                command="Input.dispatchMouseEvent",
                params={
                    "type": kind,
                    "x": CENTRE[0],
                    "y": CENTRE[1],
                    "button": "left",
                    "buttons": buttons,
                    "clickCount": 1,
                },
            )
        raw = await _hits(iid)
        await eval_js(iid, "window.__hits.length = 0")

        sent: list[str] = []
        tab = await tab_binding.tab_for_caller(runtime.browser_manager, iid)
        real_send = tab.send

        async def recording_send(cdp_obj, *args, **kwargs):
            frame = getattr(cdp_obj, "gi_frame", None)
            module = frame.f_globals.get("__name__", "") if frame else ""
            sent.append(f"{module}.{getattr(cdp_obj, '__name__', '?')}")
            return await real_send(cdp_obj, *args, **kwargs)

        tab.send = recording_send
        try:
            record = await get_fn("click_element")(instance_id=iid, selector="#target")
        finally:
            del tab.send
        clicked = await _hits(iid)
        return {"raw": raw, "clicked": clicked, "record": record, "sent": sent}
    finally:
        await close(instance_id=iid)


async def test_the_reported_sequence_clicks_where_a_raw_dispatch_lands(
    fixture_app_server, tmp_empty_root
):
    """The exact uNotes sequence (no ``scale``): a raw dispatch lands at
    (100, 290) and ``click_element`` must land at the same point, sending no
    ``Emulation`` command of its own."""
    seen = await exact_report_sequence(fixture_app_server, headless=True)

    assert [h["id"] for h in seen["raw"]] == ["target"], seen
    assert [h["id"] for h in seen["clicked"]] == ["target"], seen
    for hit in (seen["raw"][0], seen["clicked"][0]):
        assert abs(hit["x"] - CENTRE[0]) <= 1.5 and abs(hit["y"] - CENTRE[1]) <= 1.5
    assert not [name for name in seen["sent"] if ".emulation." in name], seen
