"""T-10284 / T-10299 in a REAL Chrome: the isolated-world reads, run as shipped.

``tests/test_element_left_the_page.py`` and ``tests/test_page_natives.py`` pin
everything the product does with a read's answer, against a double. A double
cannot show what the read itself does, and the claims the fix rests on are claims
about Chrome and V8:

* a node the page removed but still references answers ``no``, and a node in the
  document answers ``yes``; a ``<form>`` with a control named ``isConnected`` does
  not shadow the read;
* the reads run in an ISOLATED WORLD (``page_natives.run``), so nothing a page
  does in its own world steers them: a global ``Node`` of its own, a custom
  element with its own ``isConnected`` / ``id`` / ``click()``, a ``setPrototypeOf``
  (even to a plain object, cutting the node off from the DOM interfaces), a member
  added to ``HTMLElement.prototype`` or to ``EventTarget.prototype``, replaced
  built-ins, a polluted ``Object.prototype`` (accessors, and ``get`` / ``value``
  inherited by every property descriptor) and a ``getComputedStyle`` that throws
  ``true`` or ``false``;
* the read finds the node's own frame (an iframe, a closed shadow root inside it),
  clicks land with the page's listeners firing, and a custom element's own
  ``click()`` is not the one that runs;
* a read gives the page no user activation, a script that throws is a failed read
  whatever it threw, a navigation between the commands of a read is a departure,
  and every remote object a read resolved is released.

The nodes are held by the page (``window.__held``) and wrapped in real nodriver
``Element`` objects, so the scripts run through ``page_natives.run`` exactly as the
tools run them, against ``DOM.resolveNode`` of a node the document no longer
holds.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from nodriver import cdp
from nodriver.core import element as nodriver_element

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    runtime,
    sandbox_kwargs,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded import (
    click_target,
    element_resolution,
    page_natives,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

pytestmark = integration_pytestmark()

YES = click_target.CONNECTED_YES
NO = click_target.CONNECTED_NO

#: The page: four held nodes, a click counter on the one that is clicked, and an
#: oracle that the form really does shadow the member (a script reading
#: ``form.isConnected`` would get the ``<input>``).
_SETUP = """(() => {
  const mk = (tag) => document.body.appendChild(document.createElement(tag));
  const held = {};
  held.connected = mk('div');
  held.removed = mk('div');
  held.removed.remove();
  held.form = mk('form');
  const control = held.form.appendChild(document.createElement('input'));
  control.name = 'isConnected';
  held.removedForm = mk('form');
  const other = held.removedForm.appendChild(document.createElement('input'));
  other.name = 'isConnected';
  held.removedForm.remove();
  window.__held = held;
  window.__clicks = 0;
  held.connected.addEventListener('click', () => { window.__clicks++; });
  return true;
})()"""


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def _element(tab, name: str, tree=None):
    """A real nodriver ``Element`` for the node the page holds as ``__held.<name>``."""
    remote, _ = await tab.send(
        cdp.runtime.evaluate(f"window.__held.{name}", return_by_value=False)
    )
    node = await tab.send(cdp.dom.describe_node(object_id=remote.object_id))
    return nodriver_element.create(node, tab, tree=tree)


async def _answers(held) -> dict[str, object]:
    """Every shipped script that asks, run on the held node as the tools run it."""
    run = page_natives.run
    aim = json.loads(await run(held, click_target.AIM_JS))
    return {
        "connected": await run(held, click_target.CONNECTED_JS),
        "aim": aim["connected"],
        "visible": await run(held, element_resolution.VISIBLE_JS),
        "synthetic": await run(held, click_target.SYNTHETIC_JS, user_gesture=True),
    }


async def _spawned(fixture_app_server, page="interactions.html"):
    iid = (await get_fn("spawn_browser")(headless=True, **sandbox_kwargs()))[
        "instance_id"
    ]
    await navigate_and_settle(iid, f"{fixture_app_server}/{page}")
    return iid, await runtime.browser_manager.get_tab(iid)


async def test_the_connectivity_read_in_real_chrome(fixture_app_server, tmp_empty_root):
    close = get_fn("close_instance")
    iid, tab = await _spawned(fixture_app_server)
    try:
        assert await eval_js(iid, _SETUP) is True
        await tab.send(cdp.dom.get_document())

        # The oracle for the form case: reading the MEMBER off the form yields
        # the control, which a script that did so would call connected.
        shadow = await eval_js(
            iid, "window.__held.removedForm.isConnected instanceof HTMLInputElement"
        )
        assert shadow is True, "this Chrome does not shadow the member: no premise"

        live = await _answers(await _element(tab, "connected"))
        gone = await _answers(await _element(tab, "removed"))
        form_live = await _answers(await _element(tab, "form"))
        form_gone = await _answers(await _element(tab, "removedForm"))

        assert live == {
            "connected": YES,
            "aim": YES,
            "visible": True,
            "synthetic": click_target.SYNTHETIC_CLICKED,
        }, live
        for removed in (gone, form_gone):
            assert removed == {
                "connected": NO,
                "aim": NO,
                "visible": NO,
                "synthetic": click_target.SYNTHETIC_DETACHED,
            }, removed
        assert form_live["connected"] == YES and form_live["aim"] == YES, form_live
        assert await eval_js(iid, "window.__clicks") == 1, "only the live node clicked"

        # -- Python takes only those two tokens ------------------------------
        assert click_target.connectivity(live["connected"], "x") is True
        assert click_target.connectivity(gone["connected"], "x") is False

        # -- the page-world read the isolated world replaces, on this Chrome --
        # A getter replaced with one that does ``throw true``: through
        # ``Element.apply`` the thrown ``true`` reaches Python as a plain
        # ``True`` -- indistinguishable from a read that said "connected".
        await eval_js(
            iid,
            "Object.defineProperty(Node.prototype, 'isConnected', "
            "{configurable: true, get() { throw true; }}) && true",
        )
        removed = await _element(tab, "removed")
        unguarded = await removed.apply(
            "(elem) => Object.getOwnPropertyDescriptor(Node.prototype, "
            "'isConnected').get.call(elem) === true"
        )
        assert unguarded is True, (
            "premise: a thrown true must reach Python as a plain True",
            unguarded,
        )
        # ... and the shipped read does not run in that world at all.
        for name, expected in (
            ("connected", live),
            ("removed", gone),
            ("form", form_live),
            ("removedForm", form_gone),
        ):
            again = await _answers(await _element(tab, name))
            # (the live node's synthetic click is repeated, so compare the reads)
            assert {k: v for k, v in again.items() if k != "synthetic"} == {
                k: v for k, v in expected.items() if k != "synthetic"
            }, (name, again)
    finally:
        await close(instance_id=iid)


#: Pages that steer a read in the page's world. Each builds ``window.__held.live``
#: (in the document, ``#steer-live``, a 160x40 box with a ``<span>`` filling it) and
#: ``window.__held.gone`` (the same, removed but still held), and counts clicks in
#: ``window.__clicks``. ``(top_level, setup, premise)``: ``top_level`` is run as a
#: top-level script (a ``class Node {}`` there replaces the global); ``premise`` is
#: evaluated after the setup and must be true, so a Chrome that stopped honouring
#: the steering cannot pass silently.
_STEER_NODE = """
  const build = (make) => {
    const mk = (id) => {
      const el = make();
      el.setAttribute('id', id);
      el.style.cssText = 'display:block;position:fixed;left:20px;top:20px;' +
        'width:160px;height:40px;z-index:99999;background:#ccc';
      el.innerHTML = '<span style="display:block;height:40px">hit</span>';
      return el;
    };
    const live = document.body.appendChild(mk('steer-live'));
    const gone = document.body.appendChild(mk('steer-gone'));
    gone.remove();
    window.__clicks = 0;
    window.__overridden = false;
    live.addEventListener('click', () => { window.__clicks++; });
    window.__held = {live, gone};
  };
"""
_PLAIN = _STEER_NODE + "build(() => document.createElement('div')); return true;"
_NODE_BROKEN = (
    "(() => { try { return Object.getOwnPropertyDescriptor("
    "Node.prototype, 'isConnected') === undefined; } catch (e) { return true; } })()"
)
_STEERED_PAGES = {
    # the page replaces the ``Node`` global (a linked-list demo)
    "declares-class-node": ("class Node {}", _PLAIN, _NODE_BROKEN),
    "declares-function-node": ("function Node() {}", _PLAIN, _NODE_BROKEN),
    "declares-var-node": ("var Node = 1;", _PLAIN, _NODE_BROKEN),
    # a custom element with its own isConnected, id (LWC reads its VM there, so
    # it throws for any other node) and click()
    "custom-element": (
        "",
        _STEER_NODE
        + """
  class SteerX extends HTMLElement {
    get isConnected() { return true; }
    get id() { throw new Error('vm'); }
    click() { window.__overridden = true; }
  }
  customElements.define('x-steer', SteerX);
  build(() => document.createElement('x-steer'));
  return true;""",
        "window.__held.gone.isConnected === true",
    ),
    # an element given a prototype that defines isConnected (and a throwing id)
    "set-prototype-of": (
        "",
        _STEER_NODE
        + """
  const proto = Object.create(HTMLElement.prototype, {
    isConnected: {configurable: true, get() { return true; }},
    id: {configurable: true, get() { throw new Error('vm'); }},
  });
  build(() => Object.setPrototypeOf(document.createElement('div'), proto));
  return true;""",
        "window.__held.gone.isConnected === true",
    ),
    # a node whose chain no longer reaches Node.prototype at all (the residual
    # the page-world walk could only report as the page's own answer)
    "plain-object-chain": (
        "",
        _PLAIN.replace(
            "return true;",
            """for (const k of ['live', 'gone']) {
    Object.setPrototypeOf(window.__held[k], {get isConnected() { return true; }});
  }
  return true;""",
        ),
        "window.__held.gone.isConnected === true",
    ),
    # a page ADDING isConnected on HTMLElement.prototype
    "added-on-html-element": (
        "",
        "Object.defineProperty(HTMLElement.prototype, 'isConnected',"
        " {configurable: true, get() { return true; }});\n" + _PLAIN,
        "window.__held.gone.isConnected === true",
    ),
    # Codex round 8, finding 1: a member added FARTHER from the node than the
    # browser's own beats it under a farthest-owner walk
    "added-on-event-target": (
        "",
        "Object.defineProperty(EventTarget.prototype, 'isConnected',"
        " {configurable: true, get() { return true; }});\n" + _PLAIN,
        "Object.prototype.hasOwnProperty.call(EventTarget.prototype, 'isConnected')",
    ),
    # pollution of Object.prototype, the chain's last prototype
    "object-prototype": (
        "",
        """for (const name of ['isConnected', 'tagName', 'nodeName', 'id', 'classList']) {
    Object.defineProperty(Object.prototype, name,
      {configurable: true, get() { return true; }});
  }\n"""
        + _PLAIN,
        "Object.prototype.hasOwnProperty('isConnected')",
    ),
    # Codex round 8, finding 2: every data descriptor inherits ``get``
    "descriptor-fields": (
        "",
        "Object.prototype.get = () => undefined;\n"
        "Object.prototype.set = () => undefined;\n" + _PLAIN,
        "Object.getOwnPropertyDescriptor(Element.prototype, 'getClientRects')"
        ".get !== undefined",
    ),
    # replaced built-ins: the ORIGINAL member is gone from the page's world
    "replaced-originals": (
        "",
        "Element.prototype.getClientRects = function () { return []; };\n"
        "Element.prototype.matches = function () { return true; };\n"
        "Document.prototype.elementFromPoint = function () { return null; };\n"
        "Object.defineProperty(Node.prototype, 'contains', "
        "{configurable: true, value() { return false; }});\n" + _PLAIN,
        "document.body.getClientRects().length === 0",
    ),
    # Codex round 8, finding 3: getComputedStyle throws a primitive
    "get-computed-style-throws-true": (
        "",
        "window.getComputedStyle = () => { throw true; };\n" + _PLAIN,
        "(() => { try { getComputedStyle(document.body); } catch (e) "
        "{ return e === true; } return false; })()",
    ),
    "get-computed-style-throws-false": (
        "",
        "window.getComputedStyle = () => { throw false; };\n" + _PLAIN,
        "(() => { try { getComputedStyle(document.body); } catch (e) "
        "{ return e === false; } return false; })()",
    ),
}


@pytest.mark.parametrize("page", list(_STEERED_PAGES))
async def test_a_page_that_steers_the_page_world_changes_nothing(
    fixture_app_server, tmp_empty_root, page
):
    """The same assertions on every page: the isolated world's answers are the
    browser's, whatever the page did to its own prototypes and globals, and the
    tools that run the reads answer as they do on any page."""
    close = get_fn("close_instance")
    query_elements = get_fn("query_elements")
    get_element_state = get_fn("get_element_state")
    click_element = get_fn("click_element")
    wait_for_element = get_fn("wait_for_element")
    top_level, setup, premise = _STEERED_PAGES[page]

    iid, tab = await _spawned(fixture_app_server)
    try:
        if top_level:
            await tab.send(cdp.runtime.evaluate(top_level))
        assert await eval_js(iid, f"(() => {{{setup}}})()") is True
        assert await eval_js(iid, premise) is True, "premise: the page steers"
        await tab.send(cdp.dom.get_document())

        live = await _element(tab, "live")
        gone = await _element(tab, "gone")
        run = page_natives.run
        aim = json.loads(await run(live, click_target.AIM_JS))
        assert aim["connected"] == YES and aim["rendered"], aim
        assert aim["hit"]["tag"] == "span" and aim["hit_is_target"], aim
        assert aim["visibility"] == "visible", aim
        assert await run(live, click_target.CONNECTED_JS) == YES
        assert await run(live, element_resolution.VISIBLE_JS) is True

        assert await run(gone, click_target.CONNECTED_JS) == NO
        assert json.loads(await run(gone, click_target.AIM_JS))["connected"] == NO
        assert await run(gone, element_resolution.VISIBLE_JS) == NO
        assert await run(gone, click_target.SYNTHETIC_JS, user_gesture=True) == (
            click_target.SYNTHETIC_DETACHED
        )
        assert not await element_resolution.refresh_element(tab, gone)
        assert await element_resolution.refresh_element(tab, live)
        assert await eval_js(iid, "window.__clicks") == 0, "nothing was clicked yet"

        # The synthetic click on the live node runs the browser's own click():
        # a custom element's override of click() does not run, and the page's
        # listener hears the click.
        assert await run(live, click_target.SYNTHETIC_JS, user_gesture=True) == (
            click_target.SYNTHETIC_CLICKED
        )
        assert await eval_js(iid, "window.__clicks") == 1
        assert await eval_js(iid, "window.__overridden") is False

        # The tools, on the live page: each answers as it does on any page.
        listed = await query_elements(instance_id=iid, selector="#steer-live")
        assert len(listed) == 1, listed
        assert await query_elements(
            instance_id=iid, selector="#steer-live", visible_only=True
        )
        state = await get_element_state(instance_id=iid, selector="#steer-live")
        assert state["is_visible"] is True
        assert await wait_for_element(
            instance_id=iid, selector="#steer-live", timeout=3000, visible=True
        )
        clicked = await click_element(instance_id=iid, selector="#steer-live")
        assert clicked["dispatch"] == "coordinate", clicked
        assert await eval_js(iid, "window.__clicks") == 2
        assert await eval_js(iid, "window.__overridden") is False
    finally:
        await close(instance_id=iid)


async def test_a_hidden_node_is_not_visible_on_a_page_whose_style_read_throws(
    fixture_app_server, tmp_empty_root
):
    """Codex round 8, finding 3: with ``getComputedStyle`` replaced by a function
    that throws ``true``, a page-world visibility read answered ``true`` for a
    ``display:none`` element (a thrown ``true`` is a value) and listed it, and
    satisfied ``wait_for_element(visible=True)``. The isolated world reads the
    browser's own."""
    close = get_fn("close_instance")
    iid, tab = await _spawned(fixture_app_server)
    try:
        await eval_js(
            iid,
            "(() => { const h = document.body.appendChild("
            "document.createElement('div')); h.id = 'hid'; "
            "h.style.display = 'none'; h.textContent = 'x'; "
            "window.getComputedStyle = () => { throw true; }; return true; })()",
        )
        await tab.send(cdp.dom.get_document())
        listed = await get_fn("query_elements")(
            instance_id=iid, selector="#hid", visible_only=True
        )
        assert listed == []
        assert not await get_fn("wait_for_element")(
            instance_id=iid, selector="#hid", timeout=1500, visible=True
        )
    finally:
        await close(instance_id=iid)


async def _activation(tab) -> dict[str, bool]:
    """``navigator.userActivation`` as the page sees it, read WITHOUT a gesture."""
    raw, _ = await tab.send(
        cdp.runtime.evaluate(
            "JSON.stringify({been: navigator.userActivation.hasBeenActive, "
            "active: navigator.userActivation.isActive})",
            return_by_value=True,
            user_gesture=False,
        )
    )
    return json.loads(raw.value)


async def test_the_read_only_scripts_give_the_page_no_user_activation(
    fixture_app_server, tmp_empty_root
):
    """T-10284 review round 2, finding 5, MEASURED (headless Chrome, this tree).
    ``Element.apply`` sends ``Runtime.callFunctionOn`` with ``userGesture=True``,
    and ONE connectivity read of ONE node through it gives the page transient
    user activation (``navigator.userActivation.hasBeenActive`` turns true): that
    is the control below. The connectivity, visibility and aim reads are sent by
    ``page_natives.run`` (``userGesture`` false), and neither they nor the tools
    that run them (``query_elements``, ``get_element_state``) activate the page.
    The click path sends ``userGesture`` true, as 2.1.53 did."""
    close = get_fn("close_instance")
    spawn = get_fn("spawn_browser")
    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        tab = await runtime.browser_manager.get_tab(iid)
        # nodriver's own navigation, not ``navigate_and_settle``: its settle step
        # runs ``query_elements``, which is one of the reads under test.
        await tab.get(f"{fixture_app_server}/interactions.html")
        await tab.sleep(1.0)
        setup, _ = await tab.send(
            cdp.runtime.evaluate(_SETUP, return_by_value=True, user_gesture=False)
        )
        assert setup.value is True
        await tab.send(cdp.dom.get_document())
        inactive = {"been": False, "active": False}
        assert await _activation(tab) == inactive, "premise: a fresh page"

        live = await _element(tab, "connected")
        removed = await _element(tab, "removed")
        for script in (
            click_target.CONNECTED_JS,
            element_resolution.VISIBLE_JS,
            click_target.AIM_JS,
        ):
            await page_natives.run(live, script)
            await page_natives.run(removed, script)
        assert await _activation(tab) == inactive, "page_natives.run"

        assert await get_fn("query_elements")(
            instance_id=iid, selector="#fidelity-btn", visible_only=False
        )
        assert await get_fn("query_elements")(instance_id=iid, selector="#fidelity-btn")
        state = await get_fn("get_element_state")(
            instance_id=iid, selector="#fidelity-btn"
        )
        assert state["is_visible"]
        assert await _activation(tab) == inactive, "query_elements, get_element_state"

        # The control: the same read through ``Element.apply`` DOES activate it.
        assert (
            await live.apply(
                click_target.CONNECTED_JS.replace(
                    "native(Node, 'isConnected')",
                    "Object.getOwnPropertyDescriptor(Node.prototype, 'isConnected').get",
                )
            )
            == YES
        )
        assert await _activation(tab) == {"been": True, "active": True}

        # The click path keeps what 2.1.53 sent.
        await page_natives.run(live, click_target.SYNTHETIC_JS, user_gesture=True)
        assert await _activation(tab) == {"been": True, "active": True}
    finally:
        await close(instance_id=iid)


#: An iframe holding a button and a closed shadow root with a button, a closed
#: shadow root in the main document, and a custom element that overrides click()
#: and isConnected.
_FRAMES_SETUP = """(() => {
  window.__clicks = {inner: 0, shadow: 0, framed_shadow: 0, custom: 0};
  window.__overridden = false;
  const frame = document.body.appendChild(document.createElement('iframe'));
  frame.id = 'framed';
  frame.style.cssText = 'position:fixed;left:300px;top:120px;width:300px;height:160px;border:0';
  frame.srcdoc = '<body style="margin:0"><button id="ib" style="width:120px;height:40px">in</button>' +
    '<div id="fhost" style="width:120px;height:40px"></div></body>';
  const host = document.body.appendChild(document.createElement('div'));
  host.style.cssText = 'position:fixed;left:20px;top:200px;width:120px;height:40px';
  const root = host.attachShadow({mode: 'closed'});
  root.innerHTML = '<button id="sb" style="width:120px;height:40px">sh</button>';
  root.firstChild.addEventListener('click', () => { window.__clicks.shadow++; });
  class ClickX extends HTMLElement {
    get isConnected() { return true; }
    click() { window.__overridden = true; }
  }
  customElements.define('x-click', ClickX);
  const custom = document.body.appendChild(new ClickX());
  custom.style.cssText = 'display:block;position:fixed;left:20px;top:300px;width:120px;height:40px';
  custom.textContent = 'cx';
  custom.addEventListener('click', () => { window.__clicks.custom++; });
  window.__held = {shadow: root.firstChild, custom: custom, frame: frame};
  return new Promise((resolve) => frame.addEventListener('load', () => {
    const doc = frame.contentDocument;
    doc.getElementById('ib').addEventListener('click', () => { window.__clicks.inner++; });
    const inner = doc.getElementById('fhost').attachShadow({mode: 'closed'});
    inner.innerHTML = '<button id="fsb" style="width:120px;height:40px">fs</button>';
    inner.firstChild.addEventListener('click', () => { window.__clicks.framed_shadow++; });
    window.__held.inner = doc.getElementById('ib');
    window.__held.framedShadow = inner.firstChild;
    resolve(true);
  }));
})()"""


async def _framed_page(fixture_app_server):
    iid, tab = await _spawned(fixture_app_server)
    # ``eval_js`` awaits a Promise result.
    assert await eval_js(iid, _FRAMES_SETUP) is True
    await tab.send(cdp.dom.get_document())
    return iid, tab


async def test_the_read_finds_the_nodes_own_frame_and_shadow_roots(
    fixture_app_server, tmp_empty_root
):
    """An element of an iframe is read in the iframe's world (the main frame's
    wrapper answers ``FOREIGN``), the shadow roots are climbed through ``host``
    whatever their mode, and a click on each one lands with the page's own
    listener firing: the aim, the input probe and the press all run in the node's
    own frame."""
    close = get_fn("close_instance")
    iid, tab = await _framed_page(fixture_app_server)
    try:
        tree = await tab.send(cdp.dom.get_document(-1, True))
        for name, counter in (
            ("inner", "inner"),
            ("shadow", "shadow"),
            ("framedShadow", "framed_shadow"),
        ):
            held = await _element(tab, name, tree)
            aim = await click_target.aim(held, name)
            assert aim["connected"] == YES and aim["rendered"], (name, aim)
            assert aim["hit_is_target"] is True, (
                name,
                aim["hit"],
                aim["point"],
                aim["target"],
            )
            assert aim["visibility"] == "visible", (name, aim)
            ledger = click_target.InputSent()
            await click_target.click(tab, held, name, aim, ledger)
            assert ledger.sent is True
            counted = json.loads(await eval_js(iid, "JSON.stringify(window.__clicks)"))
            assert counted[counter] == 1, (name, counted)

        # a read of the framed node, frame by frame
        framed = await _element(tab, "inner")
        frames = await tab.send(cdp.page.get_frame_tree())
        assert len(frames.child_frames) == 1
        assert (await page_natives.run(framed, click_target.CONNECTED_JS)) == YES
        assert (await page_natives.run(framed, element_resolution.VISIBLE_JS)) is True
    finally:
        await close(instance_id=iid)


async def test_a_custom_element_keeps_its_own_click_out_of_the_synthetic_click(
    fixture_app_server, tmp_empty_root
):
    """``ClickX`` overrides ``click()`` and ``isConnected``. The synthetic click
    runs the browser's own ``HTMLElement.prototype.click``: the page's listener
    hears it, the override does not run, and the user-gesture flag is the one
    the click path has always sent."""
    close = get_fn("close_instance")
    iid, tab = await _framed_page(fixture_app_server)
    try:
        custom = await _element(tab, "custom")
        ledger = click_target.InputSent()
        assert await click_target.synthetic_click(custom, "x-click", ledger) is True
        counted = json.loads(await eval_js(iid, "JSON.stringify(window.__clicks)"))
        assert counted["custom"] == 1, counted
        assert await eval_js(iid, "window.__overridden") is False
        assert await page_natives.run(custom, click_target.CONNECTED_JS) == YES
        assert await _activation(tab) == {"been": True, "active": True}
    finally:
        await close(instance_id=iid)


async def test_a_node_of_a_frame_that_is_gone_is_a_departure(
    fixture_app_server, tmp_empty_root
):
    """The iframe is removed from the page; the node is still held. No frame of
    the page holds it, which ``detached_element`` classifies as a departure."""
    close = get_fn("close_instance")
    iid, tab = await _framed_page(fixture_app_server)
    try:
        inner = await _element(tab, "inner")
        assert await eval_js(iid, "window.__held.frame.remove() || true") is True
        with pytest.raises(Exception) as caught:  # noqa: PT011  PERMANENT(the departure classification is what is asserted)
            await page_natives.run(inner, click_target.CONNECTED_JS)
        assert element_resolution.detached_element(caught.value) is not None
        assert not await element_resolution.refresh_element(tab, inner)
    finally:
        await close(instance_id=iid)


class _Interleaved:
    """A tab that runs *hook* before its *nth* command and forwards everything."""

    def __init__(self, tab, nth, hook):
        self._tab, self._nth, self._hook, self._sent = tab, nth, hook, 0

    async def send(self, command):
        self._sent += 1
        if self._sent == self._nth:
            await self._hook()
        return await self._tab.send(command)


@pytest.mark.parametrize("target", ["same-origin", "other-origin"])
async def test_a_navigation_between_the_commands_of_a_read_is_a_departure(
    fixture_app_server, tmp_empty_root, target
):
    """The page navigates after ``DOM.resolveNode`` and before
    ``Runtime.callFunctionOn`` (the 4th of the read's commands: the frame tree,
    the world, the resolution, the call): Chrome answers "Cannot find context with
    specified id" (another origin) or its validation reply for an object id it can
    no longer resolve (the same origin). That is the node leaving, not a failure of the read, and not
    an answer: ``node_is_visible`` says "not visible", so a ``wait_for_element``
    that had resolved the node does not return ``True`` for a selector that is
    gone."""
    close = get_fn("close_instance")
    iid, tab = await _spawned(fixture_app_server)
    try:
        assert await eval_js(iid, _SETUP) is True
        await tab.send(cdp.dom.get_document())
        live = await _element(tab, "connected")
        navigated = []

        async def navigate():
            navigated.append(True)
            url = (
                f"{fixture_app_server}/interactions.html?2"
                if target == "same-origin"
                else "data:text/html;charset=utf-8,<p>elsewhere</p>"
            )
            await tab.send(cdp.page.navigate(url))
            await tab.sleep(1.0)

        racing = SimpleNamespace(
            backend_node_id=live.backend_node_id,
            tab=_Interleaved(tab, 4, navigate),
        )
        with pytest.raises(Exception) as caught:  # noqa: PT011  PERMANENT(the departure classification is what is asserted)
            await page_natives.run(racing, click_target.CONNECTED_JS)
        assert navigated == [True]
        assert element_resolution.detached_element(caught.value) is not None

        # The same race through the visibility reader: not visible, no raise.
        again = SimpleNamespace(backend_node_id=live.backend_node_id, tab=tab)
        assert await element_resolution.node_is_visible(again, "the node") is False
    finally:
        await close(instance_id=iid)


class _Recording:
    """A tab that records the object ids ``DOM.resolveNode`` handed out."""

    def __init__(self, tab):
        self._tab = tab
        self.object_ids = []

    async def send(self, command):
        result = await self._tab.send(command)
        if isinstance(result, cdp.runtime.RemoteObject) and result.object_id:
            self.object_ids.append(result.object_id)
        return result


async def _alive(tab, object_id) -> bool:
    try:
        await tab.send(
            cdp.runtime.call_function_on("function(){return 1}", object_id=object_id)
        )
    except Exception:  # noqa: BLE001  PERMANENT(any refusal means the handle is gone)
        return False
    return True


async def test_a_read_releases_every_object_it_resolved(
    fixture_app_server, tmp_empty_root
):
    """Codex round 8, finding 5. Every read leaked its resolved ``RemoteObject``
    (no ``releaseObject`` / object group), per row per listing, retaining detached
    DOM trees. Here 4 listings of 6 rows, on success and on a script that throws:
    every handle ``DOM.resolveNode`` handed out is dead afterwards. The control is
    a resolution that is not released, which stays callable."""
    close = get_fn("close_instance")
    iid, tab = await _spawned(fixture_app_server)
    try:
        assert await eval_js(iid, _SETUP) is True
        await tab.send(cdp.dom.get_document())
        live = await _element(tab, "connected")
        removed = await _element(tab, "removed")

        recording = _Recording(tab)
        for _ in range(4):
            for row in range(6):
                node = SimpleNamespace(
                    backend_node_id=(live if row % 2 else removed).backend_node_id,
                    tab=recording,
                )
                await page_natives.run(node, click_target.CONNECTED_JS)
        with pytest.raises(ToolError):
            await page_natives.run(
                SimpleNamespace(backend_node_id=live.backend_node_id, tab=recording),
                "(elem) => { throw true; }",
            )
        assert len(recording.object_ids) == 4 * 6 + 1
        alive = [i for i in recording.object_ids if await _alive(tab, i)]
        assert alive == [], f"{len(alive)} of {len(recording.object_ids)} leaked"

        # the control: an unreleased resolution stays callable
        control = await tab.send(
            cdp.dom.resolve_node(backend_node_id=live.backend_node_id)
        )
        assert await _alive(tab, control.object_id)
    finally:
        await close(instance_id=iid)


@pytest.mark.parametrize(
    ("thrown", "kind"),
    [("true", "boolean"), ("false", "boolean"), ("new Error('x')", "error")],
    ids=["throw-true", "throw-false", "throw-error"],
)
async def test_a_script_that_throws_is_a_failed_read_in_real_chrome(
    fixture_app_server, tmp_empty_root, thrown, kind
):
    """Whatever it throws: Chrome's reply carries a ``result`` (``true`` for a
    thrown ``true``) next to ``exceptionDetails``, and ``run`` raises it."""
    close = get_fn("close_instance")
    iid, tab = await _spawned(fixture_app_server)
    try:
        assert await eval_js(iid, _SETUP) is True
        await tab.send(cdp.dom.get_document())
        live = await _element(tab, "connected")
        with pytest.raises(page_natives.ReadError, match="threw"):
            await page_natives.run(live, f"(elem) => {{ throw {thrown}; }}")
        # and a connected node is still read right after
        assert await page_natives.run(live, click_target.CONNECTED_JS) == YES
        assert kind
    finally:
        await close(instance_id=iid)
