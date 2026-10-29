"""T-10284 in a REAL Chrome: the page-side connectivity read, run as shipped.

``tests/test_element_left_the_page.py`` pins everything the product does with the
read's answer, against a double. A double cannot show what the read itself does,
and four claims the fix rests on are claims about Chrome and V8:

* a node the page removed but still references answers ``no``, and a node in the
  document answers ``yes``;
* a ``<form>`` with a control named ``isConnected`` does NOT shadow the read
  (``form.isConnected`` IS that control, so a script that reads the member off
  the node gets a truthy object; the native accessor does not);
* a getter replaced with one that does ``throw true`` is a ``threw`` answer. This
  is the case an unguarded read loses: ``Element.apply`` drops
  ``exceptionDetails`` and hands back the thrown ``true`` as an ordinary
  ``True``, which looks exactly like a read that said "connected";
* a getter that throws an Error, or answers something that is not a boolean, is
  ``threw`` as well, and no script that asks (the aim read, the synthetic click,
  the visibility read) takes it for ``yes`` or ``no``.

The nodes are held by the page (``window.__held``) and wrapped in real nodriver
``Element`` objects, so the scripts run through ``Element.apply`` exactly as the
tools run them, against ``DOM.resolveNode`` of a node the document no longer
holds.
"""

from __future__ import annotations

import json

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
from stealth_chrome_devtools_mcp.embedded import click_target, dom_handler
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

pytestmark = integration_pytestmark()

YES = click_target.CONNECTED_YES
NO = click_target.CONNECTED_NO
THREW = click_target.CONNECTED_THREW

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


async def _element(tab, name: str):
    """A real nodriver ``Element`` for the node the page holds as ``__held.<name>``."""
    remote, _ = await tab.send(
        cdp.runtime.evaluate(f"window.__held.{name}", return_by_value=False)
    )
    node = await tab.send(cdp.dom.describe_node(object_id=remote.object_id))
    return nodriver_element.create(node, tab)


async def _answers(tab, name: str) -> dict[str, object]:
    """Every shipped script that asks, run on ``__held.<name>``."""
    held = await _element(tab, name)
    aim = json.loads(await held.apply(click_target.AIM_JS))
    return {
        "connected": await held.apply(click_target.CONNECTED_JS),
        "aim": aim["connected"],
        "visible": await held.apply(dom_handler.VISIBLE_JS),
        "synthetic": await held.apply(click_target.SYNTHETIC_JS),
    }


async def test_the_connectivity_read_in_real_chrome(fixture_app_server, tmp_empty_root):
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{fixture_app_server}/interactions.html")
        assert await eval_js(iid, _SETUP) is True
        tab = await runtime.browser_manager.get_tab(iid)
        await tab.send(cdp.dom.get_document())

        # The oracle for the form case: reading the MEMBER off the form yields
        # the control, which a script that did so would call connected.
        shadow = await eval_js(
            iid, "window.__held.removedForm.isConnected instanceof HTMLInputElement"
        )
        assert shadow is True, "this Chrome does not shadow the member: no premise"

        # -- the native accessor: the page's own answer ----------------------
        live = await _answers(tab, "connected")
        gone = await _answers(tab, "removed")
        form_live = await _answers(tab, "form")
        form_gone = await _answers(tab, "removedForm")

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
                "visible": False,
                "synthetic": click_target.SYNTHETIC_DETACHED,
            }, removed
        assert form_live["connected"] == YES and form_live["aim"] == YES, form_live
        assert await eval_js(iid, "window.__clicks") == 1, "only the live node clicked"

        # -- Python takes only those two tokens ------------------------------
        assert click_target.connectivity(live["connected"], "x") is True
        assert click_target.connectivity(gone["connected"], "x") is False

        # -- the unguarded read the fix replaces, on this Chrome -------------
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

        # -- the shipped read under a getter that throws `true` ---------------
        for name in ("connected", "removed", "form", "removedForm"):
            thrown = await _answers(tab, name)
            assert thrown == {
                "connected": THREW,
                "aim": THREW,
                "visible": False,
                "synthetic": click_target.SYNTHETIC_THREW,
            }, (name, thrown)
            with pytest.raises(ToolError):
                click_target.connectivity(thrown["connected"], name)
        assert await eval_js(iid, "window.__clicks") == 1, "a threw read never clicks"

        # -- a getter that throws an Error, then one that answers a non-boolean
        for getter in ("throw new Error('x')", "return {}", "return undefined"):
            await eval_js(
                iid,
                "Object.defineProperty(Node.prototype, 'isConnected', "
                f"{{configurable: true, get() {{ {getter}; }}}}) && true",
            )
            for name in ("connected", "removed"):
                answer = await (await _element(tab, name)).apply(
                    click_target.CONNECTED_JS
                )
                assert answer == THREW, (getter, name, answer)
    finally:
        await close(instance_id=iid)
