"""plan_E2E STEP 7 (E2E-7) — "hard DOM" characterization against the fixture app.

The STEP 2/3 suites cover every tool against mainstream elements; this file adds
the odd corners the user asked for: shadow roots (open + closed), iframes
(child-document, srcdoc, sandboxed/opaque-origin), contenteditable, a
``<select multiple>``, inline SVG, ``<canvas>``, and ``<details>``. All against
``hard_dom.html``.

These are CHARACTERIZATION tests: where a tool cannot reach an odd corner (CSS
selectors don't pierce shadow roots; extraction walks light DOM only;
``select_option`` has no multi-value path) we PIN the current behavior with a
comment naming the suspected src root cause, so an intended fix surfaces as a
deliberate test update rather than a silent regression. No src is touched here.

Determinism (plan §2.6): ONE spawn per test closed in ``finally`` (nodriver binds
websockets to the function-scoped loop, so a shared browser is a cross-loop
hazard), every navigation through ``navigate_and_settle``, bounded ``wait_for_js``
polling only (no sleep-then-assert), every URL ``base_url``-relative.
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


@pytest.mark.characterization
async def test_shadow_dom_characterization(fixture_app_server):
    """Shadow DOM (open + closed) under the DOM/extraction tools.

    Pins:
      * query_elements is ``document.querySelectorAll`` under the hood, which by
        spec does NOT pierce shadow boundaries, so a shadow-internal selector
        returns [] while the light-DOM host resolves normally.
      * execute_script is the working escape hatch: ``host.shadowRoot`` reaches an
        OPEN root (clicking its button logs through the page), while a CLOSED host
        exposes ``shadowRoot === null``.
      * extract_element_structure walks the light DOM only (extract_structure.js
        uses ``document.querySelector`` + ``.children``; cdp_element_cloner.py),
        so open-shadow content (the inner button and its pinned color) is ABSENT
        from the structure output — a real limitation, pinned here.
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    query = get_fn("query_elements")
    click = get_fn("click_element")
    extract_structure = get_fn("extract_element_structure")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/hard_dom.html")

        # Light-DOM host resolves (bounded-poll past the stale-document race).
        hosts = await _query_at_least(iid, "#shadow-open-host", 1)
        assert isinstance(hosts, list) and len(hosts) == 1

        # A shadow-internal selector does NOT pierce: querySelectorAll cannot see
        # #shadow-open-btn (it lives inside the open root), so the result is [].
        assert await query(instance_id=iid, selector="#shadow-open-btn") == []

        # The host itself is a normal light-DOM element and is clickable.
        assert await click(instance_id=iid, selector="#shadow-open-host")

        # Escape hatch: reach INTO the open root and click its button. The click
        # + the indexOf run in the SAME JS turn, so a non-negative index proves
        # the page-level listener fired synchronously (attributable to this call).
        idx = await eval_js(
            iid,
            "(function(){var h=document.getElementById('shadow-open-host');"
            "h.shadowRoot.querySelector('button').click();"
            "return window.__actions.indexOf('click:shadow-open-btn');})()",
        )
        assert idx >= 0
        assert "click:shadow-open-btn" in await read_actions(iid)

        # Closed root: host.shadowRoot is null from page JS (mode:"closed").
        assert (
            await eval_js(
                iid,
                "document.getElementById('shadow-closed-host').shadowRoot === null",
            )
            is True
        )

        # Extraction walks light DOM only: the host id is present, but the
        # shadow-internal button and its pinned color are absent (the finding).
        structure = await extract_structure(
            instance_id=iid, selector="#shadow-open-host", include_children=True
        )
        blob = json.dumps(structure, default=str)
        assert "shadow-open-host" in blob
        assert "shadow-open-btn" not in blob
        assert "rgb(9, 99, 199)" not in blob
    finally:
        await close(instance_id=iid)


@pytest.mark.characterization
async def test_iframe_characterization(fixture_app_server):
    """iframes (child-document, srcdoc, sandboxed opaque-origin) under the tools.

    Pins:
      * query_elements on the top document cannot see into any child frame, so a
        child selector returns [].
      * get_page_content returns the TOP document only (dom_handler.py:708-709):
        ``body.innerText`` excludes every frame's text, and the serialized html
        carries the inline srcdoc attribute (so srcdoc text is present) but not
        the externally-fetched child document's text.
      * execute_script reaches same-origin frames (child-doc + srcdoc) via
        ``contentDocument`` and can drive them; the sandboxed frame
        (allow-scripts WITHOUT allow-same-origin => opaque origin) exposes
        ``contentDocument`` as null to the parent (guarded probe: a thrown
        SecurityError is returned as a value, never a tool failure).
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    query = get_fn("query_elements")
    get_page_content = get_fn("get_page_content")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/hard_dom.html")

        # Top-document query cannot cross the frame boundary.
        assert await query(instance_id=iid, selector="#child-sentinel") == []

        # Same-origin child-doc frame: wait for load, then read its text through
        # contentDocument (guarded so a pre-load access returns a value).
        child_text = await wait_for_js(
            iid,
            "(function(){try{return document.getElementById('iframe-src')"
            ".contentDocument.getElementById('child-sentinel').textContent;}"
            "catch(e){return 'ERR:'+e.name;}})()",
            "CHILD-SENTINEL-TEXT",
        )
        assert child_text == "CHILD-SENTINEL-TEXT"

        # Drive the child button through contentWindow and read the CHILD's own
        # window.__actions back (the child owns a separate action log).
        child_actions = await eval_js(
            iid,
            "(function(){var w=document.getElementById('iframe-src').contentWindow;"
            "w.document.getElementById('child-btn').click();"
            "return (w.__actions||[]).join(',');})()",
        )
        assert "click:child-btn" in child_actions

        # srcdoc frame: also same-origin, reachable via contentDocument.
        srcdoc_text = await wait_for_js(
            iid,
            "(function(){try{return document.getElementById('iframe-srcdoc')"
            ".contentDocument.getElementById('srcdoc-sentinel').textContent;}"
            "catch(e){return 'ERR:'+e.name;}})()",
            "SRCDOC-SENTINEL-TEXT",
        )
        assert srcdoc_text == "SRCDOC-SENTINEL-TEXT"

        # Sandboxed frame: opaque origin => the parent sees contentDocument null.
        sandbox_probe = await eval_js(
            iid,
            "(function(){try{"
            "var f=document.getElementById('iframe-sandbox');"
            "if(f.contentDocument===null){return 'contentDocument-null';}"
            "var p=f.contentDocument.getElementById('sandbox-sentinel');"
            "return 'accessible:'+(p?p.textContent:'no-sentinel');"
            "}catch(e){return 'ERR:'+e.name;}})()",
        )
        assert sandbox_probe == "contentDocument-null"

        # get_page_content is the top document only. srcdoc text rides along in
        # the serialized html attribute; the external child-doc text does not.
        content = await get_page_content(instance_id=iid)
        blob = json.dumps(content, default=str)
        assert "SRCDOC-SENTINEL-TEXT" in blob
        assert "CHILD-SENTINEL-TEXT" not in blob
        # body.innerText excludes ALL frame text (even the inline srcdoc's).
        assert "SRCDOC-SENTINEL-TEXT" not in content["text"]
    finally:
        await close(instance_id=iid)


@pytest.mark.characterization
async def test_contenteditable_and_multiselect(fixture_app_server):
    """contenteditable + ``<select multiple>`` under the interaction tools.

    Pins:
      * type_text's clear_first sets ``elem.value = ''`` (dom_handler.py:347),
        a no-op on a contenteditable div, so the pinned initial text is NOT
        cleared; the typed characters DO land (send_keys drives the focused
        editable) and each fires input:editable.
      * paste_text uses CDP ``Input.insertText`` (dom_handler.py:476), which
        inserts into the editable's CONTENT (unlike setting .value), so pasted
        text lands.
      * select_option sets ``select.value = <one value>`` (dom_handler.py:519) —
        which selects exactly ONE option even on a ``<select multiple>`` (there is
        no multi-value path) — then dispatches change, so the log records the
        single value only.
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    click = get_fn("click_element")
    type_text = get_fn("type_text")
    paste_text = get_fn("paste_text")
    select = get_fn("select_option")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/hard_dom.html")

        # contenteditable: click to focus, then type.
        assert await click(instance_id=iid, selector="#editable")
        assert await type_text(instance_id=iid, selector="#editable", text="TYPED")

        inner_after_type = await eval_js(
            iid, "document.getElementById('editable').innerText"
        )
        # clear_first did NOT clear the div (value-based clear is a no-op here).
        assert "EDITABLE-INITIAL-TEXT" in inner_after_type
        # Typed characters landed, and input:editable was logged.
        assert "TYPED" in inner_after_type
        assert "input:editable" in await read_actions(iid)

        # paste_text inserts via Input.insertText -> content updates.
        assert await paste_text(instance_id=iid, selector="#editable", text="PASTED")
        inner_after_paste = await eval_js(
            iid, "document.getElementById('editable').innerText"
        )
        assert "PASTED" in inner_after_paste

        # select_option on a <select multiple>: single-value only.
        assert await select(instance_id=iid, selector="#select-multi", value="green")
        selected = await eval_js(
            iid,
            "Array.from(document.getElementById('select-multi').selectedOptions)"
            ".map(function(o){return o.value;}).join(',')",
        )
        assert selected == "green"
        assert "change:select-multi:green" in await read_actions(iid)
    finally:
        await close(instance_id=iid)


@pytest.mark.characterization
async def test_svg_canvas_details(fixture_app_server):
    """Inline SVG, ``<canvas>``, and ``<details>`` under interaction + state tools.

    Pins:
      * click_element drives an inline SVG child (#svg-circle) and a <canvas>,
        logging click:svg-circle / click:canvas-box.
      * clicking #summary-toggle opens the <details>, whose ASYNCHRONOUS toggle
        event logs toggle:details-box:open (bounded-wait, not sleep-assert).
      * get_element_state reports the HTML ``open`` ATTRIBUTE, not a live property
        (``element.attrs``; dom_handler.py:569) — present once the details opens.
      * extract_element_structure includes inline SVG descendants (#svg-circle).
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    click = get_fn("click_element")
    get_element_state = get_fn("get_element_state")
    extract_structure = get_fn("extract_element_structure")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/hard_dom.html")

        # Inline SVG circle: clickable, logs via its own listener.
        assert await click(instance_id=iid, selector="#svg-circle")
        assert "click:svg-circle" in await read_actions(iid)

        # Canvas element: clickable, logs via its own listener.
        assert await click(instance_id=iid, selector="#canvas-box")
        assert "click:canvas-box" in await read_actions(iid)

        # Clicking the summary opens <details>; toggle fires asynchronously, so
        # bounded-wait for the log entry (deadline + interval, per plan §2.6).
        assert await click(instance_id=iid, selector="#summary-toggle")
        assert (
            await wait_for_js(
                iid,
                "window.__actions.indexOf('toggle:details-box:open') >= 0",
                True,
            )
            is True
        )

        # get_element_state reads attributes: after opening, `open` is present.
        state = await get_element_state(instance_id=iid, selector="#details-box")
        assert state["tag_name"].lower() == "details"
        assert "open" in state["attributes"]

        # Extraction includes inline SVG descendants.
        structure = await extract_structure(
            instance_id=iid, selector="#svg-shape", include_children=True
        )
        blob = json.dumps(structure, default=str)
        assert "svg-shape" in blob
        assert "svg-circle" in blob
    finally:
        await close(instance_id=iid)
