"""plan_E2E STEP 2 — interaction E2E against the fixture app (real headless Chrome).

Covers every tool in browser-management (8), tabs (5), element-interaction (12),
and cookies-storage (3). Each test spawns ONE browser (nodriver binds websockets
to the running loop; pytest-asyncio auto mode gives function-scoped loops, so a
shared browser is a cross-loop hazard), walks a chunk of related tools, asserts
the fixture's action log / ground truth, and closes in ``finally``.

Determinism (plan §2.6): no sleep-then-assert (bounded ``wait_for_js`` polling or
tool-native waits only), the fixture animation is paused, screenshots are checked
by PNG magic bytes + nonzero size only, every URL is ``base_url``-relative.
"""

from __future__ import annotations

import json

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    read_actions,
    sandbox_kwargs,
    wait_for_js,
    warmup_once,
)

pytestmark = integration_pytestmark()


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


# ---------------------------------------------------------------------------
# browser-management (8): spawn, list, state, navigate, history, reload, close
# ---------------------------------------------------------------------------


async def test_browser_lifecycle_and_history(fixture_app_server):
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    list_instances = get_fn("list_instances")
    get_instance_state = get_fn("get_instance_state")
    navigate = get_fn("navigate")
    go_back = get_fn("go_back")
    go_forward = get_fn("go_forward")
    reload_page = get_fn("reload_page")
    close = get_fn("close_instance")
    get_page_content = get_fn("get_page_content")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    assert result["state"] == "ready"
    try:
        instances = await list_instances()
        assert any(i["instance_id"] == iid for i in instances)

        state = await get_instance_state(instance_id=iid)
        assert isinstance(state, dict)
        assert iid in json.dumps(state, default=str)

        nav = await navigate(instance_id=iid, url=f"{base}/index.html")
        assert isinstance(nav, dict)
        content = await get_page_content(instance_id=iid)
        assert "fixture-index-page" in json.dumps(content, default=str)

        await navigate(instance_id=iid, url=f"{base}/interact.html")
        assert (
            await wait_for_js(iid, "document.title", "fixture-interact-page")
            == "fixture-interact-page"
        )

        # History: back to index, forward to interact.
        await go_back(instance_id=iid)
        assert (
            await wait_for_js(iid, "document.title", "fixture-index-page")
            == "fixture-index-page"
        )
        await go_forward(instance_id=iid)
        assert (
            await wait_for_js(iid, "document.title", "fixture-interact-page")
            == "fixture-interact-page"
        )

        # Reload keeps us on interact and re-runs app.js (fresh action log).
        await reload_page(instance_id=iid)
        assert (
            await wait_for_js(iid, "document.title", "fixture-interact-page")
            == "fixture-interact-page"
        )
        assert await read_actions(iid) == []
    finally:
        await close(instance_id=iid)


# ---------------------------------------------------------------------------
# element-interaction (12) split across three chunky walks + one characterization
# ---------------------------------------------------------------------------


async def test_interaction_controls_and_log(fixture_app_server):
    """query_elements, click_element, select_option, execute_script — verified by
    both the in-page action log and the resulting live DOM property."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    navigate = get_fn("navigate")
    query = get_fn("query_elements")
    click = get_fn("click_element")
    select = get_fn("select_option")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate(instance_id=iid, url=f"{base}/interact.html")

        # query_elements: exact ground-truth counts.
        singles = await query(instance_id=iid, selector="#btn-counter")
        assert isinstance(singles, list) and len(singles) == 1
        radios = await query(
            instance_id=iid, selector="input[name='flavor']", visible_only=False
        )
        assert len(radios) == 3

        # Counter button: click -> increments text + logs click:btn-counter.
        assert await click(instance_id=iid, selector="#btn-counter")
        assert "click:btn-counter" in await read_actions(iid)
        assert (
            await eval_js(iid, "document.getElementById('counter-value').textContent")
            == "1"
        )

        # Select: change to beta -> logs detail + live value updates.
        assert await select(instance_id=iid, selector="#select-single", value="beta")
        assert "change:select-single:beta" in await read_actions(iid)
        assert await eval_js(iid, "document.getElementById('select-single').value") == (
            "beta"
        )

        # Checkbox: click toggles checked + logs change:check-me:on.
        assert await click(instance_id=iid, selector="#check-me")
        assert "change:check-me:on" in await read_actions(iid)
        assert await eval_js(iid, "document.getElementById('check-me').checked") is True

        # Radio: click chocolate -> logs change:flavor:chocolate.
        assert await click(
            instance_id=iid, selector="input[name='flavor'][value='chocolate']"
        )
        assert "change:flavor:chocolate" in await read_actions(iid)
    finally:
        await close(instance_id=iid)


async def test_text_input_scroll_and_wait(fixture_app_server):
    """type_text, paste_text, scroll_page, wait_for_element, click_element."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    navigate = get_fn("navigate")
    type_text = get_fn("type_text")
    paste_text = get_fn("paste_text")
    scroll_page = get_fn("scroll_page")
    click = get_fn("click_element")
    wait_for_element = get_fn("wait_for_element")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate(instance_id=iid, url=f"{base}/interact.html")

        # type_text -> the live .value property reflects exactly what was typed.
        assert await type_text(instance_id=iid, selector="#text-input", text="hello")
        assert (
            await eval_js(iid, "document.getElementById('text-input').value") == "hello"
        )

        # paste_text -> value set directly.
        assert await paste_text(
            instance_id=iid, selector="#textarea-input", text="pasted-text"
        )
        assert (
            await eval_js(iid, "document.getElementById('textarea-input').value")
            == "pasted-text"
        )

        # scroll_page to the bottom (instant, not smooth) -> scrollY advances.
        assert await scroll_page(instance_id=iid, direction="bottom", smooth=False)
        assert await eval_js(iid, "window.scrollY") > 0

        # Bounded reveal: click -> 200ms setTimeout -> #delayed-el visible. The
        # 5s tool timeout dwarfs the 200ms delay so scheduling jitter can't flake.
        assert await click(instance_id=iid, selector="#reveal-btn")
        assert (
            await wait_for_element(
                instance_id=iid, selector="#delayed-el", timeout=5000, visible=True
            )
            is True
        )
    finally:
        await close(instance_id=iid)


async def test_upload_screenshot_and_content(fixture_app_server, tmp_path):
    """upload_file (single + multi), take_screenshot (PNG bytes), get_page_content."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    navigate = get_fn("navigate")
    upload = get_fn("upload_file")
    screenshot = get_fn("take_screenshot")
    get_page_content = get_fn("get_page_content")
    close = get_fn("close_instance")

    f1 = tmp_path / "alpha.txt"
    f1.write_text("alpha", encoding="utf-8")
    f2 = tmp_path / "beta.txt"
    f2.write_text("beta", encoding="utf-8")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate(instance_id=iid, url=f"{base}/interact.html")

        single = await upload(
            instance_id=iid, selector="#single-file", file_paths=str(f1)
        )
        assert single["count"] == 1
        assert (
            await eval_js(iid, "document.getElementById('single-file').files[0].name")
            == "alpha.txt"
        )

        multi = await upload(
            instance_id=iid, selector="#multi-file", file_paths=[str(f1), str(f2)]
        )
        assert multi["count"] == 2
        assert (
            await eval_js(iid, "document.getElementById('multi-file').files.length")
            == 2
        )

        # Screenshot: assert valid image magic bytes + nonzero size only (no
        # pixel asserts). NOTE/FINDING: take_screenshot(file_path=...) delegates
        # to nodriver save_screenshot, which defaults to JPEG and ignores the
        # tool's format="png" default — so a .png path receives JPEG bytes. We
        # accept either magic so the test proves "a screenshot was written"
        # without coupling to the (buggy) format; the finding is reported.
        shot = tmp_path / "shot.png"
        await screenshot(instance_id=iid, file_path=str(shot))
        raw = shot.read_bytes()
        assert raw[:8] == b"\x89PNG\r\n\x1a\n" or raw[:3] == b"\xff\xd8\xff"
        assert len(raw) > 0

        content = await get_page_content(instance_id=iid)
        assert "fixture-interact-page" in json.dumps(content, default=str)
    finally:
        await close(instance_id=iid)


@pytest.mark.characterization
async def test_get_element_state_pins_current_shape(fixture_app_server):
    """PINS CURRENT get_element_state behavior (two NEW findings).

    (1) get_element_state RAISES on a display:none element: it calls nodriver
        ``element.get_position()``, which has no layout box to report and throws
        "could not find position". A *visible* element returns a dict.
    (2) The returned ``value`` is read from the HTML *attribute*
        (``element.attrs``), not the live DOM property, so typing into an input
        does not change the reported value.
    Both pinned so an intended fix (route: M4-Ph1 / M5b) surfaces as a failing
    test to update deliberately.
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    navigate = get_fn("navigate")
    type_text = get_fn("type_text")
    get_element_state = get_fn("get_element_state")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate(instance_id=iid, url=f"{base}/interact.html")

        # A visible element returns the current dict shape (stable fields).
        card = await get_element_state(instance_id=iid, selector="#select-single")
        assert card["tag_name"].lower() == "select"
        assert card["id"] == "select-single"
        assert isinstance(card["attributes"], dict)

        # Finding 1: a display:none element (#delayed-el, class="hidden") makes
        # get_position fail, so the tool raises instead of returning a state dict.
        with pytest.raises(Exception, match=r"position|element state"):
            await get_element_state(instance_id=iid, selector="#delayed-el")

        # Finding 2: value comes from the attribute, so typed text is NOT
        # reflected even though the live DOM property clearly changed.
        await type_text(instance_id=iid, selector="#text-input", text="typed-value")
        state = await get_element_state(instance_id=iid, selector="#text-input")
        assert state["value"] in (None, "")  # attribute empty; live value ignored
        assert (
            await eval_js(iid, "document.getElementById('text-input').value")
            == "typed-value"
        )
    finally:
        await close(instance_id=iid)


# ---------------------------------------------------------------------------
# cookies-storage (3): set via tool + via page button, read, clear
# ---------------------------------------------------------------------------


async def test_cookies_lifecycle(fixture_app_server):
    """set_cookie + clear_cookies, verified via the live ``document.cookie``.

    get_cookies is deliberately NOT called here — it hangs (the CDP
    Network.getCookies / deprecated getAllCookies never returns in the installed
    nodriver) and, worse, poisons the tab's CDP connection so every subsequent
    call times out. It is E2E_EXEMPT with that finding; cookies are read via
    document.cookie (non-httpOnly) instead, which is exact ground truth.
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    navigate = get_fn("navigate")
    click = get_fn("click_element")
    set_cookie = get_fn("set_cookie")
    clear_cookies = get_fn("clear_cookies")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate(instance_id=iid, url=f"{base}/cookies.html")

        # Page button sets client_cookie=from-js; the tool sets tool_cookie.
        assert await click(instance_id=iid, selector="#set-client-cookie-btn")
        assert (
            await set_cookie(
                instance_id=iid, name="tool_cookie", value="tool-val", url=base
            )
            is True
        )
        cookie_str = await eval_js(iid, "document.cookie")
        assert "client_cookie=from-js" in cookie_str
        assert "tool_cookie=tool-val" in cookie_str

        # Clear resets browser-wide state (also this test's own cleanup).
        assert await clear_cookies(instance_id=iid) is True
        assert "tool_cookie" not in await eval_js(iid, "document.cookie")
    finally:
        await close(instance_id=iid)


# ---------------------------------------------------------------------------
# tabs (5): list, open, active, switch, close
# ---------------------------------------------------------------------------


async def test_tabs_lifecycle(fixture_app_server):
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    navigate = get_fn("navigate")
    list_tabs = get_fn("list_tabs")
    new_tab = get_fn("new_tab")
    get_active_tab = get_fn("get_active_tab")
    switch_tab = get_fn("switch_tab")
    close_tab = get_fn("close_tab")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate(instance_id=iid, url=f"{base}/index.html")

        before = await list_tabs(instance_id=iid)
        assert isinstance(before, list) and len(before) >= 1
        assert all("tab_id" in t for t in before)
        original_ids = {t["tab_id"] for t in before}

        opened = await new_tab(instance_id=iid, url=f"{base}/interact.html")
        assert "tab_id" in opened
        new_id = opened["tab_id"]

        after = await list_tabs(instance_id=iid)
        after_ids = {t["tab_id"] for t in after}
        assert new_id in after_ids
        assert len(after_ids) >= len(original_ids) + 1

        active = await get_active_tab(instance_id=iid)
        assert isinstance(active, dict)
        assert active.get("tab_id") in after_ids

        # Switch back to an original tab, then close the tab we opened.
        an_original = next(iter(original_ids))
        assert await switch_tab(instance_id=iid, tab_id=an_original) is True
        assert await close_tab(instance_id=iid, tab_id=new_id) is True

        remaining = {t["tab_id"] for t in await list_tabs(instance_id=iid)}
        assert new_id not in remaining
    finally:
        await close(instance_id=iid)
