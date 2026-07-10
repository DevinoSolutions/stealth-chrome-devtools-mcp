"""plan_E2E STEP 8 (E2E-8) -- interaction FIDELITY + COMPLETENESS characterization.

The STEP 2/3/7 suites prove the tools RETURN success; this file proves they behave
like a REAL user (or pins exactly where they do not). Two thrusts, against
``interactions.html``:

* FIDELITY -- does ``click_element`` dispatch a REAL trusted input (the full
  pointer/mouse chain, ``isTrusted === true``, honoring hit-testing and
  scroll-into-view) or a synthetic ``element.click()``? Same question for
  ``type_text`` (real per-key keydown/keyup vs a value-set / char-only insert).
* COMPLETENESS -- drive every interaction type the suite has not yet, and for the
  ones no tool can reach (double-click, right-click, drag-and-drop, CSS
  ``:hover``, native JS dialogs) PIN the gap in an explicit census.

These are CHARACTERIZATION tests: each asserts the ACTUAL observed behavior with a
comment naming the suspected src root cause (``file:line``), so an intended change
flips the test deliberately rather than silently. No src is touched here.

FIDELITY VERDICTS pinned below (evidence in each test's docstring):
  * ``click_element`` -> REAL trusted coordinate input. Primary path
    ``element.mouse_click()`` (dom_handler.py:239) -> nodriver ``tab.mouse_click``
    (core/tab.py) dispatches CDP ``Input.dispatchMouseEvent`` mousePressed/
    mouseReleased at the element center => ``isTrusted`` true, full
    pointerdown/mousedown/mouseup/click chain, HIT-TESTED (an overlay wins), and
    AUTO scroll-into-view (dom_handler.py:235). ``element.click()`` (synthetic,
    untrusted) is only a fallback on error (dom_handler.py:242).
  * ``type_text`` -> CHAR-event input, NOT the full key lifecycle. Per char
    ``element.send_keys`` (dom_handler.py:400) -> nodriver
    ``cdp.input_.dispatch_key_event("char")`` (core/element.py) => keypress+input
    fire but keydown/keyup do NOT; so native Enter implicit-submit (which needs a
    trusted keydown) never triggers (the parse_newlines path dispatches a SYNTHETIC
    KeyboardEvent, dom_handler.py:354-396, isTrusted false).

Determinism (plan §2.6): ONE spawn per test closed in ``finally`` (nodriver binds
websockets to the function-scoped loop, so a shared browser is a cross-loop
hazard), every navigation through ``navigate_and_settle``, bounded ``wait_for_js``
polling only (no sleep-then-assert), every URL ``base_url``-relative. NEVER
``get_cookies`` (real-Chrome hang) and NEVER a native alert/confirm/prompt/
beforeunload (same wedge class -- see the census test).
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    read_actions,
    sandbox_kwargs,
    server_mod,
    wait_for_js,
    warmup_once,
)

pytestmark = integration_pytestmark()


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def _query_at_least(iid, selector, min_count, timeout=10.0, **kwargs):
    """Bounded-poll query_elements until it returns a list with len >= min_count.

    The first DOM-path call after a navigation can hit nodriver's stale
    document-node race (query_elements swallows the ProtocolException into []);
    ``navigate_and_settle`` already settles ``body``, but a fresh-context first
    query is re-polled here for safety. Returns the last result either way."""
    query = get_fn("query_elements")
    deadline = time.monotonic() + timeout
    result = await query(instance_id=iid, selector=selector, **kwargs)
    while (
        not (isinstance(result, list) and len(result) >= min_count)
        and time.monotonic() < deadline
    ):
        await asyncio.sleep(0.25)
        result = await query(instance_id=iid, selector=selector, **kwargs)
    return result


async def _wait_action(iid, entry, timeout=5.0):
    """Bounded-poll the in-page action log until ``entry`` appears; True if seen.

    A deterministic barrier for asynchronously-logged events (dialog close,
    popover toggle, invalid, submit) -- deadline + interval, per plan §2.6."""
    js = f"window.__actions.indexOf({json.dumps(entry)}) >= 0"
    return await wait_for_js(iid, js, True, timeout=timeout)


# ---------------------------------------------------------------------------
# THRUST A -- fidelity: real trusted input vs synthetic shortcut
# ---------------------------------------------------------------------------


@pytest.mark.characterization
async def test_click_fidelity_is_trusted_input(fixture_app_server):
    """PINS: click_element dispatches a REAL, TRUSTED, full-chain pointer/mouse click.

    #fidelity-btn logs every event of the click as ``ev:fidelity:<type>:<trust>``.
    A real coordinate click (element.mouse_click -> CDP Input.dispatchMouseEvent,
    dom_handler.py:239) produces the full pointerdown->mousedown->mouseup->click
    chain, all ``isTrusted === true``. A synthetic element.click()
    (dom_handler.py:242, the error-only fallback) would instead produce a LONE
    ``ev:fidelity:click:untrusted``. We assert the trusted chain is present and NO
    untrusted click appears, so a regression to the synthetic path flips this test.
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    click = get_fn("click_element")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")
        await _query_at_least(iid, "#fidelity-btn", 1)

        assert await click(instance_id=iid, selector="#fidelity-btn")
        assert await _wait_action(iid, "ev:fidelity:click:trusted")
        actions = await read_actions(iid)

        # The click is TRUSTED (real input), not synthetic.
        assert "ev:fidelity:click:trusted" in actions
        assert "ev:fidelity:click:untrusted" not in actions

        # The FULL mouse chain fired (a synthetic el.click() emits only `click`).
        assert "ev:fidelity:mousedown:trusted" in actions
        assert "ev:fidelity:mouseup:trusted" in actions
        # Pointer events are generated from the same trusted hardware input.
        assert "ev:fidelity:pointerdown:trusted" in actions
        assert "ev:fidelity:pointerup:trusted" in actions

        # No event in the whole chain was untrusted.
        assert not any(a.endswith(":untrusted") for a in actions), actions
    finally:
        await close(instance_id=iid)


@pytest.mark.characterization
async def test_click_respects_occlusion_and_offscreen(fixture_app_server):
    """PINS: click_element is HIT-TESTED (coordinate click) and AUTO-scrolls.

    * Occlusion: #covered-btn is fully covered by #overlay-trap (pointer-events:
      auto, higher z-index). A trusted coordinate click at the button's center
      lands on the OVERLAY (``click:overlay-trap``), NOT the button -- proving the
      click is a real hit-tested pointer event, not a synthetic element.click()
      that would bypass occlusion. (element.mouse_click, dom_handler.py:239.)
    * pointer-events control: #overlay-pen covers #pen-covered-btn but is
      pointer-events:none, so the real click passes THROUGH to the button
      (``click:pen-covered-btn``) -- hit-testing honors pointer-events.
    * Auto-scroll: #offscreen-btn sits below a tall spacer and is clicked with NO
      manual scroll; the click still registers because click_element calls
      ``element.scroll_into_view()`` first (dom_handler.py:235), and scrollY moves.
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    click = get_fn("click_element")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")
        await _query_at_least(iid, "#covered-btn", 1)

        # Occlusion: the overlay (topmost at the coordinate) receives the click.
        assert await click(instance_id=iid, selector="#covered-btn")
        assert await _wait_action(iid, "click:overlay-trap")
        actions = await read_actions(iid)
        assert "click:overlay-trap" in actions
        assert "click:covered-btn" not in actions  # occlusion honored

        # pointer-events:none overlay -> the real click passes through.
        assert await click(instance_id=iid, selector="#pen-covered-btn")
        assert await _wait_action(iid, "click:pen-covered-btn")
        actions = await read_actions(iid)
        assert "click:pen-covered-btn" in actions
        assert "click:overlay-pen" not in actions

        # Offscreen: no manual scroll; the tool scrolls it into view and clicks.
        assert await eval_js(iid, "window.scrollY") == 0
        assert await click(instance_id=iid, selector="#offscreen-btn")
        assert await _wait_action(iid, "click:offscreen-btn")
        assert await eval_js(iid, "window.scrollY") > 0  # auto-scrolled
    finally:
        await close(instance_id=iid)


@pytest.mark.characterization
async def test_keyboard_fidelity_and_enter_submit(fixture_app_server):
    """PINS: type_text emits CHAR events (keypress+input), NOT keydown/keyup, and
    its Enter is synthetic (no native implicit form submit).

    #key-probe logs ``key:<down|up|press>:<key>:<trust>`` and ``input:key-probe:
    <value>``. type_text drives ``element.send_keys`` per char (dom_handler.py:400)
    -> nodriver ``cdp.input_.dispatch_key_event("char")``, which COMMITS the
    character: keypress + input fire (trusted) but keydown/keyup do NOT. So the
    typed value lands, yet the keydown/keyup half of the lifecycle is absent -- a
    partial-fidelity FINDING (tool-limitation).

    Enter: #enter-form has a single field and NO submit button, so only a TRUSTED
    Enter keydown triggers native implicit submission. type_text's parse_newlines
    path dispatches a SYNTHETIC ``KeyboardEvent('keydown',{key:'Enter'})`` via
    element.apply (dom_handler.py:381-396, isTrusted false), so ``submit:enter-form``
    NEVER fires -- pinned as a tool-gap (no trusted-Enter / key-press tool exists).
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    type_text = get_fn("type_text")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")
        await _query_at_least(iid, "#key-probe", 1)

        assert await type_text(instance_id=iid, selector="#key-probe", text="ab")
        assert await eval_js(iid, "document.getElementById('key-probe').value") == "ab"
        actions = await read_actions(iid)

        # input events fired and carried the growing value (real text input).
        assert any(a.startswith("input:key-probe:") for a in actions), actions
        # Whatever key phase fires is TRUSTED (CDP char is a trusted event).
        assert not any(
            a.startswith("key:") and a.endswith(":untrusted") for a in actions
        ), actions
        # FINDING: no keydown / keyup -- send_keys uses dispatch_key_event("char"),
        # which fires keypress+input only, not the keydown/keyup lifecycle.
        assert not any(a.startswith("key:down:") for a in actions), actions
        assert not any(a.startswith("key:up:") for a in actions), actions

        # Enter via parse_newlines: types the text, but the synthetic Enter does
        # NOT trigger native implicit submission of the button-less form.
        assert await type_text(
            instance_id=iid,
            selector="#enter-input",
            text="hello\n",
            parse_newlines=True,
        )
        assert "hello" in await eval_js(
            iid, "document.getElementById('enter-input').value"
        )
        # Positive control: the value landed (type_text ran) -- so the absent
        # submit is a real gap, not a missed dispatch.
        assert "submit:enter-form" not in await read_actions(iid)
    finally:
        await close(instance_id=iid)


# ---------------------------------------------------------------------------
# THRUST B -- completeness: form semantics, rich inputs, top layer, navigation
# ---------------------------------------------------------------------------


@pytest.mark.characterization
async def test_form_semantics(fixture_app_server):
    """PINS: disabled rejects clicks, readonly rejects typed text, constraint
    validation blocks submit while invalid, label click forwards, reset fires.

    * Disabled: click_element on #disabled-btn returns True (the tool dispatches a
      coordinate click regardless) but the browser suppresses events on disabled
      controls, so ``click:disabled-btn`` NEVER logs -- the tool cannot tell you the
      control was inert (FINDING: no disabled-state guard in dom_handler).
    * Label forwarding (positive control): clicking <label for=labeled-check>
      toggles the checkbox -> ``change:labeled-check:on`` (proves the log works).
    * Readonly: type_text into #readonly-input does not land the typed text (char
      events are rejected by the readonly control); note clear_first's programmatic
      ``elem.value=''`` (dom_handler.py:347) is what empties it, not the typing.
    * Constraint validation: clicking submit with #required-input empty fires
      ``invalid:required-input`` and BLOCKS ``submit:validated-form``; after filling
      the field, submit fires -- real browser constraint validation is honored.
    * Reset: clicking the reset button fires ``reset:validated-form``.
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    click = get_fn("click_element")
    type_text = get_fn("type_text")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")
        await _query_at_least(iid, "#disabled-btn", 1)

        # Positive control first: the label click forwards to the checkbox.
        assert await click(instance_id=iid, selector="#label-for-check")
        assert await _wait_action(iid, "change:labeled-check:on")

        # Disabled: the tool reports success, but no click event is dispatched by
        # the browser on a disabled control.
        assert await click(instance_id=iid, selector="#disabled-btn") is True
        assert "click:disabled-btn" not in await read_actions(iid)

        # Readonly: typed text is rejected; the injected string never lands.
        assert await type_text(
            instance_id=iid, selector="#readonly-input", text="INJECT"
        )
        readonly_val = await eval_js(
            iid, "document.getElementById('readonly-input').value"
        )
        assert "INJECT" not in readonly_val

        # Constraint validation: empty required field blocks submit + fires invalid.
        assert await click(instance_id=iid, selector="#validated-submit")
        assert await _wait_action(iid, "invalid:required-input")
        assert "submit:validated-form" not in await read_actions(iid)

        # Fill the field, submit again -> now valid, submit fires.
        assert await type_text(
            instance_id=iid, selector="#required-input", text="filled"
        )
        assert await click(instance_id=iid, selector="#validated-submit")
        assert await _wait_action(iid, "submit:validated-form")

        # Reset fires reset and clears the field.
        assert await click(instance_id=iid, selector="#reset-btn")
        assert await _wait_action(iid, "reset:validated-form")
    finally:
        await close(instance_id=iid)


@pytest.mark.characterization
async def test_rich_input_types(fixture_app_server):
    """PINS which rich <input> types the TOOL surface can drive, and which are gaps.

    For each of range / number / date / color: attempt type_text, then record the
    resulting live .value and change log. execute_script is used as the escape-hatch
    oracle to confirm each element+listener works even where the tool cannot reach.

      * range  (FINDING/tool-gap): type_text cannot move a slider (char events are
        ignored by range) -- value stays 50, no ``input:range-input``; execute_script
        setting the value + dispatching input DOES log, proving the gap is the tool.
      * number (spec-correct): type_text lands digits -> value "42",
        ``input:number-input`` logs.
      * date   (FINDING/tool-gap): type_text cannot fill the segmented date field
        via char events -> value stays "" ; execute_script sets it.
      * color  (FINDING/tool-gap): type_text cannot drive the color control ->
        value stays "#000000" ; execute_script sets it.
    select_option IS exercised here and pinned as a FINDING (see the value+index
    asserts below): a second evaluate-path call on the same document silently
    no-ops yet returns True -- a ``const select`` re-declaration collision
    (dom_handler.py:513-536); route M4-Ph1/M5b.
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    type_text = get_fn("type_text")
    select = get_fn("select_option")
    execute = get_fn("execute_script")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")
        await _query_at_least(iid, "#range-input", 1)

        # range: type_text is a no-op on a slider (char events ignored).
        assert await type_text(instance_id=iid, selector="#range-input", text="80")
        assert await eval_js(iid, "document.getElementById('range-input').value") == (
            "50"
        )
        # Escape hatch proves the element + input listener work.
        await execute(
            instance_id=iid,
            script=(
                "var e=document.getElementById('range-input');e.value=75;"
                "e.dispatchEvent(new Event('input',{bubbles:true}));"
            ),
        )
        assert await _wait_action(iid, "input:range-input:75")

        # number: type_text DOES land digits.
        assert await type_text(instance_id=iid, selector="#number-input", text="42")
        assert await eval_js(iid, "document.getElementById('number-input').value") == (
            "42"
        )
        assert any(a.startswith("input:number-input:") for a in await read_actions(iid))

        # date: type_text cannot fill the segmented field via char events.
        assert await type_text(
            instance_id=iid, selector="#date-input", text="2025-06-15"
        )
        assert (
            await eval_js(iid, "document.getElementById('date-input').value")
            != "2025-06-15"
        )
        await execute(
            instance_id=iid,
            script=(
                "var e=document.getElementById('date-input');e.value='2025-06-15';"
                "e.dispatchEvent(new Event('input',{bubbles:true}));"
            ),
        )
        assert await _wait_action(iid, "input:date-input:2025-06-15")

        # color: type_text cannot drive the color control (stays default black).
        assert await type_text(instance_id=iid, selector="#color-input", text="#123456")
        assert await eval_js(iid, "document.getElementById('color-input').value") == (
            "#000000"
        )
        await execute(
            instance_id=iid,
            script=(
                "var e=document.getElementById('color-input');e.value='#123456';"
                "e.dispatchEvent(new Event('input',{bubbles:true}));"
            ),
        )
        assert await _wait_action(iid, "input:color-input:#123456")

        # select_option baseline + a same-document FINDING. The value path drives
        # the select (synthetic change event, dom_handler.py:513-522).
        assert await select(instance_id=iid, selector="#select-fidelity", value="two")
        assert await _wait_action(iid, "change:select-fidelity:two")
        # FINDING (silent no-op + false success): the value AND index paths both run
        # ``const select = ...`` via tab.evaluate (dom_handler.py:513-536). A SECOND
        # evaluate-path call on the SAME document re-declares ``const select`` -> a
        # SyntaxError nodriver swallows, so the call silently no-ops while STILL
        # returning True. The index call here does NOT change the selection (it
        # stays "two") and logs no new change. Route: M4-Ph1 / M5b.
        assert (
            await select(instance_id=iid, selector="#select-fidelity", index=2) is True
        )
        assert (
            await eval_js(iid, "document.getElementById('select-fidelity').value")
            == "two"
        )
        # The index path itself is sound: on a FRESH document it selects "three",
        # which proves the no-op above is the collision, not a broken index path.
        await navigate_and_settle(iid, f"{base}/interactions.html")
        assert await select(instance_id=iid, selector="#select-fidelity", index=2)
        assert await _wait_action(iid, "change:select-fidelity:three")
    finally:
        await close(instance_id=iid)


@pytest.mark.characterization
async def test_native_dialog_and_navigation(fixture_app_server):
    """PINS: top-layer elements ARE reachable; real + fragment navigation work.

    * Modal <dialog> (showModal, top layer): open it, then click #dialog-close-btn
      -- a top-layer element -- and the dialog's close event fires
      (``close:modal-dialog``), proving click_element reaches into the top layer.
    * Popover (popovertarget, top layer): clicking #pop-open-btn natively toggles
      the popover -> ``toggle:pop-box:open``.
    * Fragment link: clicking <a href="#anchor-target"> sets location.hash and
      scrolls to the distant target (scrollY moves).
    * Real navigation: clicking <a href="index.html"> performs a same-tab
      navigation; the document actually changes to the index page.
    NOTE: #blank-link (target=_blank) is present and reachable by a plain click, but
    is deliberately NOT driven here -- a click-opened second target adds tab-state
    teardown races already characterized by test_e2e_interaction.test_tabs_lifecycle;
    keeping this test single-tab preserves determinism (plan §2.6).
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    click = get_fn("click_element")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")
        await _query_at_least(iid, "#dialog-open-btn", 1)

        # Top layer -- modal dialog: open, then reach the close button inside it.
        assert await click(instance_id=iid, selector="#dialog-open-btn")
        assert await _wait_action(iid, "click:dialog-open-btn")
        assert (
            await eval_js(iid, "document.getElementById('modal-dialog').open") is True
        )
        assert await click(instance_id=iid, selector="#dialog-close-btn")
        assert await _wait_action(iid, "close:modal-dialog")
        assert (
            await eval_js(iid, "document.getElementById('modal-dialog').open") is False
        )

        # Top layer -- popover: native popovertarget toggles it open.
        assert await click(instance_id=iid, selector="#pop-open-btn")
        assert await _wait_action(iid, "toggle:pop-box:open")

        # Fragment link: hash updates and the page scrolls to the distant target.
        assert await click(instance_id=iid, selector="#anchor-link")
        assert (
            await wait_for_js(iid, "location.hash", "#anchor-target")
            == "#anchor-target"
        )
        assert await eval_js(iid, "window.scrollY") > 0

        # Real navigation: a same-tab nav actually changes the document.
        assert await click(instance_id=iid, selector="#nav-link")
        assert (
            await wait_for_js(iid, "document.title", "fixture-index-page")
            == "fixture-index-page"
        )
    finally:
        await close(instance_id=iid)


@pytest.mark.characterization
async def test_unreachable_interaction_census(fixture_app_server):
    """CENSUS: interaction types with NO dedicated tool, each proven unreachable.

    The honest core of "all types we could even want": we enumerate the gaps rather
    than hide them. For each, we attempt the interaction through the available tool
    surface and assert the target event does NOT fire.

      * double-click: two click_element calls != a dblclick (each is an independent
        click_count=1 dispatch), so ``dblclick:dbl-target`` never fires. (No
        dblclick tool.)
      * right-click / contextmenu: click_element is left-button only, so
        ``contextmenu:ctx-target`` never fires. (No right-click tool.)
      * drag-and-drop: a click on the draggable source is not a drag, so neither
        ``dragstart:drag-src`` nor ``drop:drop-zone`` fires. (No drag tool.)
      * CSS :hover: no dedicated hover tool exists, and a synthetic mouseover does
        NOT trigger :hover; BUT a real click_element leaves the pointer over its
        target and DOES set :hover, so clicking the hover host reveals its
        :hover-gated submenu (a click side effect, not a pure hover).
      * native JS dialogs (alert/confirm/prompt/beforeunload): HAZARDOUS -- they
        block the page thread and this MCP exposes NO dialog-handling tool, so a
        live one would wedge the tab like the known get_cookies hang. They are
        DELIBERATELY never triggered; we assert only that no dialog tool exists.
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    click = get_fn("click_element")
    execute = get_fn("execute_script")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")
        await _query_at_least(iid, "#dbl-target", 1)

        # double-click: two clicks do not coalesce into a dblclick.
        assert await click(instance_id=iid, selector="#dbl-target")
        assert await click(instance_id=iid, selector="#dbl-target")
        # right-click: left-button click cannot express contextmenu.
        assert await click(instance_id=iid, selector="#ctx-target")
        # drag: clicking the draggable source is not a drag gesture.
        assert await click(instance_id=iid, selector="#drag-src")

        # A synchronous barrier: the last click is awaited, so any of the above
        # events would already be logged; assert every census target is absent.
        actions = await read_actions(iid)
        assert "dblclick:dbl-target" not in actions
        assert "contextmenu:ctx-target" not in actions
        assert "dragstart:drag-src" not in actions
        assert "drop:drop-zone" not in actions

        # CSS :hover -- no dedicated hover tool. Two findings, pinned:
        disp = "getComputedStyle(document.getElementById('hover-submenu')).display"
        # (1) GAP: a SYNTHETIC mouseover (the execute_script escape hatch) does NOT
        # trigger :hover, so a pure hover cannot reveal the submenu this way.
        assert await eval_js(iid, disp) == "none"
        await execute(
            instance_id=iid,
            script="document.getElementById('hover-menu').dispatchEvent("
            "new MouseEvent('mouseover', {bubbles: true}))",
        )
        assert await eval_js(iid, disp) == "none"
        # (2) NUANCE (positive fidelity): click_element is a REAL coordinate input,
        # so it leaves the pointer over the target and DOES set :hover -- clicking
        # the hover host reveals its :hover-gated submenu (display:block). So
        # :hover content on the CLICKED element is reachable as a click side effect,
        # even though no hover tool exists.
        assert await click(instance_id=iid, selector="#hover-menu")
        assert await wait_for_js(iid, disp, "block") == "block"

        # native JS dialogs: assert NO dialog-handling tool is exposed (so they are
        # correctly documented as unreachable-and-hazardous, never driven live).
        for tool_name in (
            "handle_dialog",
            "dismiss_dialog",
            "accept_dialog",
            "on_dialog",
        ):
            assert getattr(server_mod, tool_name, None) is None
    finally:
        await close(instance_id=iid)
