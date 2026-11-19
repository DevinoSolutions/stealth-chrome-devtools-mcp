"""A page cannot use Chrome's DevTools endpoint to read the vault's login tab (T-19366).

The reviewer's sequence, with real tools in a real Chrome: an agent reads the debug
port from ``chrome://version``, navigates a tab to ``http://127.0.0.1:PORT/json/version``,
opens a WebSocket to the browser endpoint from page script, attaches to the sealed
login tab and polls its password field. Every step that used to work must now fail,
and the password must not be obtained.

Only tools and page script are used here, never a module of the product, so this
file also runs against a tree that has no guard (it must be RED there).
"""

from __future__ import annotations

import asyncio
import json
import urllib.request

import pytest

import test_login_flow as flow
from e2e_helpers import eval_js, get_fn
from stealth_chrome_devtools_mcp.embedded import tool_runtime as runtime
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from vault_helpers import unlocked_vault

# The flow test's fixtures (a local login site, the vault in a temp dir, a warm
# Chrome) and helpers, under the names pytest finds them by.
pytestmark = flow.pytestmark
site, vault, _warmup = flow.site, flow.vault, flow._warmup
RIGHT, _browser, _entry, _run = flow.RIGHT, flow._browser, flow._entry, flow._run

WATCH = r"""(async () => {
const url = %(url)s;
window.__leak = {values: [], log: [], opened: false};
const ws = new WebSocket(url);
let id = 0; const pend = {};
const send = (method, params = {}, sessionId) => new Promise((res) => {
  const m = {id: ++id, method, params}; if (sessionId) m.sessionId = sessionId;
  pend[m.id] = res; ws.send(JSON.stringify(m));
});
ws.onmessage = async (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pend[msg.id]) { pend[msg.id](msg); delete pend[msg.id]; return; }
  if (msg.method === 'Target.targetCreated' && msg.params.targetInfo.type === 'page') {
    const att = await send('Target.attachToTarget', {targetId: msg.params.targetInfo.targetId, flatten: true});
    const sid = att.result && att.result.sessionId;
    window.__leak.log.push('attached ' + (sid ? 'yes' : 'no'));
    if (!sid) return;
    for (let i = 0; i < 2000; i++) {
      const r = await send('Runtime.evaluate', {expression: "(document.querySelector('input[type=password]')||{}).value || ''", returnByValue: true}, sid);
      const v = r.result && r.result.result && r.result.result.value;
      if (v) window.__leak.values.push(v);
      await new Promise(r => setTimeout(r, 10));
    }
  }
};
return await new Promise((res) => {
  ws.onopen = () => { window.__leak.opened = true; send('Target.setDiscoverTargets', {discover: true}); res('OPEN'); };
  ws.onerror = () => res('ERROR');
  ws.onclose = () => res('CLOSED');
  setTimeout(() => res('TIMEOUT'), 8000);
});
})()"""


async def _debug_port(iid):
    """The port the reviewer read from ``chrome://version``. With a vault that page is
    out of reach of the tools (sixth audit, N6; ``test_login_vault_inspector`` pins
    it), and these tests need the number whether or not a vault exists, so it is read
    from the browser object."""
    browser = (await runtime.browser_manager.get_instance(iid))["browser"]
    return str(browser.config.port)


def _browser_ws_url(port):
    """What an attacker with the endpoint's id would have, read out of band."""
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/json/version", timeout=5
    ) as r:
        return json.load(r)["webSocketDebuggerUrl"]


async def _on_agent_page(iid, site):
    await get_fn("navigate")(instance_id=iid, url=site.url("/home"))


async def test_the_tools_refuse_to_load_the_endpoint(site, tmp_path):
    async with _browser(tmp_path) as iid:
        port = await _debug_port(iid)
        for spelling in (
            f"http://127.0.0.1:{port}/json/version",
            f"http://localhost:{port}/json/version",
            f"http://[::1]:{port}/json/version",
            f"http://2130706433:{port}/json/version",
        ):
            with pytest.raises(ToolError):
                await get_fn("navigate")(instance_id=iid, url=spelling)
            with pytest.raises(ToolError):
                await get_fn("new_tab")(instance_id=iid, url=spelling)
        tabs = await get_fn("list_tabs")(instance_id=iid)
        assert not [t for t in tabs if port in str(t.get("url", ""))], tabs


async def test_a_page_cannot_load_the_endpoint_by_itself(site, tmp_path):
    async with _browser(tmp_path) as iid:
        port = await _debug_port(iid)
        await _on_agent_page(iid, site)
        endpoint = f"http://127.0.0.1:{port}/json/version"
        # fetch and XHR
        fetched = await eval_js(
            iid, f"fetch('{endpoint}').then(r => 'LOADED ' + r.status, e => 'BLOCKED')"
        )
        assert fetched == "BLOCKED"
        # a navigation the page starts itself
        await eval_js(iid, f"setTimeout(() => {{ location.href = '{endpoint}'; }}, 0)")
        await asyncio.sleep(1.5)
        assert await eval_js(iid, "location.protocol") != "http:" or port not in str(
            await eval_js(iid, "location.href")
        )
        assert "webSocketDebuggerUrl" not in str(
            await eval_js(iid, "document.documentElement.outerHTML")
        )
        # an iframe and a popup
        await _on_agent_page(iid, site)
        await eval_js(
            iid,
            f"(() => {{ const f = document.createElement('iframe'); f.id = 'dbg'; "
            f"f.src = '{endpoint}'; document.body.appendChild(f); "
            f"window.open('{endpoint}'); return 1; }})()",
        )
        await asyncio.sleep(1.5)
        frame = await eval_js(
            iid,
            "(() => { try { return document.getElementById('dbg').contentDocument"
            ".documentElement.outerHTML; } catch (e) { return 'OPAQUE'; } })()",
        )
        assert "webSocketDebuggerUrl" not in str(frame)
        for tab in await get_fn("list_tabs")(instance_id=iid):
            if port in str(tab.get("url", "")):
                body = await eval_js_on(
                    iid, tab["tab_id"], "document.documentElement.outerHTML"
                )
                assert "webSocketDebuggerUrl" not in str(body), tab


async def eval_js_on(iid, tab_id, expression):
    result = await get_fn("execute_script")(
        instance_id=iid, script=expression, tab_id=tab_id
    )
    return result


async def test_the_reviewers_sequence_does_not_obtain_the_password(site, tmp_path):
    unlocked_vault({"work": _entry(site)})
    async with _browser(tmp_path) as iid:
        port = await _debug_port(iid)
        # 1. the endpoint's own origin: the tools refuse, and a page cannot get there
        with pytest.raises(ToolError):
            await get_fn("navigate")(
                instance_id=iid, url=f"http://127.0.0.1:{port}/json/version"
            )
        await _on_agent_page(iid, site)
        await eval_js(
            iid,
            f"setTimeout(() => {{ location.href = 'http://127.0.0.1:{port}/json/version'; }}, 0)",
        )
        await asyncio.sleep(1.5)
        # 2. from wherever the agent is, a WebSocket to the browser endpoint (its id
        #    handed to the attacker for free) must not open.
        await _on_agent_page(iid, site)
        url = _browser_ws_url(port)
        state = await get_fn("execute_script")(
            instance_id=iid, script=WATCH % {"url": json.dumps(url)}
        )
        assert "OPEN" not in str(state), state
        # 3. the login runs; nothing was armed, nothing can read the password field
        result = await _run(iid)
        assert result["outcome"] == "logged_in", result
        await asyncio.sleep(1.0)
        leak = await get_fn("execute_script")(
            instance_id=iid, script="JSON.stringify(window.__leak || {})"
        )
        assert RIGHT not in str(leak)
        assert "attached yes" not in str(leak)
