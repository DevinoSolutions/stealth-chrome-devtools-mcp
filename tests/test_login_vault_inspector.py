"""No browser inspector can read the vault's login tab (T-19366, re-review N1/N2/N5).

The re-review found the typed password read end to end through Chrome's OWN
DevTools frontend, with tools only and no WebSocket: ``navigate("chrome://inspect")``,
a click on "inspect", and ``execute_script`` in the frontend tab it opened, which
sends raw CDP through ``InspectorFrontendHost.sendMessageToBackend``. The debug-port
guard is irrelevant to that route, so the invariant is stated where the target
appears: **no target an agent can address is ever a DevTools frontend or a page that
opens one**, and a login refuses to type while one exists.

Real Chrome, tools and page script only on the agent's side. The password is never
printed: a reading is compared with it inside the test.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import threading
import urllib.parse

import pytest
from nodriver import cdp

import test_login_flow as flow
from e2e_helpers import get_fn
from stealth_chrome_devtools_mcp.embedded import (
    debug_port_guard,
    login_flow,
    login_seal,
    tab_open,
)
from stealth_chrome_devtools_mcp.embedded import tool_runtime as runtime
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from vault_helpers import unlocked_vault

pytestmark = flow.pytestmark
site, vault, _warmup = flow.site, flow.vault, flow._warmup
RIGHT, _browser, _entry, _run = flow.RIGHT, flow._browser, flow._entry, flow._run

#: Where the password would be read from inside a DevTools frontend: the global
#: ``InspectorFrontendHost`` sends raw CDP to the page it inspects, and the replies
#: come back through ``DevToolsAPI.dispatchMessage`` (the re-review's script).
READ_THROUGH_FRONTEND = r"""(async () => {
  if (!window.__hook) { window.__hook = {msgs: {}, n: 9000};
    const o = DevToolsAPI.dispatchMessage;
    DevToolsAPI.dispatchMessage = function(m) { try { const j = typeof m === 'string' ? JSON.parse(m) : m; if (j && j.id >= 9000) window.__hook.msgs[j.id] = j; } catch (e) {} return o.apply(this, arguments); }; }
  const send = (method, params, sessionId) => new Promise(res => { const id = ++window.__hook.n; const m = {id, method, params: params || {}}; if (sessionId) m.sessionId = sessionId;
    InspectorFrontendHost.sendMessageToBackend(JSON.stringify(m));
    const t = setInterval(() => { if (window.__hook.msgs[id]) { clearInterval(t); res(window.__hook.msgs[id]); } }, 20);
    setTimeout(() => { clearInterval(t); res({timeout: true}); }, 3000); });
  const tg = await send('Target.getTargets');
  const infos = (tg.result && tg.result.targetInfos) || [];
  const page = infos.find(i => i.type === 'page' && /^http/.test(i.url));
  if (!page) return JSON.stringify({});
  const at = await send('Target.attachToTarget', {targetId: page.targetId, flatten: true});
  const sid = at.result && at.result.sessionId;
  if (!sid) return JSON.stringify({attached: false});
  const r = await send('Runtime.evaluate', {expression: "(document.querySelector('input[type=password]')||{}).value||''", returnByValue: true}, sid);
  return JSON.stringify({attached: true, value: r.result && r.result.result && r.result.result.value});
})()"""

CLICK_INSPECT = (
    "(() => { let n = 0; document.querySelectorAll('.action').forEach(a => "
    "{ if (/^inspect$/i.test(a.innerText.trim())) { a.click(); n++; } }); return n; })()"
)
INSPECTOR_URL = "chrome://inspect/#pages"
FRONTEND = "devtools://devtools/bundled/devtools_app.html"


async def _browser_of(iid):
    return (await runtime.browser_manager.get_instance(iid))["browser"]


async def _listed(iid):
    return [
        (t["tab_id"], str(t.get("url", "")))
        for t in await get_fn("list_tabs")(instance_id=iid)
    ]


def _is_inspector(url):
    return debug_port_guard.inspector_url(url)


async def _inspectors_in_chrome(browser):
    infos = await tab_open.send(browser, cdp.target.get_targets)
    return [(str(i.target_id), str(i.url)) for i in infos if _is_inspector(i.url)]


async def _open_frontend_privileged(browser):
    """A DevTools window the way a person's F12 (or an earlier agent step through
    ``execute_cdp_command``) leaves one: made over the test's own connection."""
    target = await tab_open.send(
        browser, cdp.target.create_target, f"{FRONTEND}?ws=127.0.0.1:1/devtools/page/x"
    )
    for _ in range(100):
        if any(t == str(target) for t, _u in await _inspectors_in_chrome(browser)):
            return str(target)
        await asyncio.sleep(0.05)
    raise AssertionError("the DevTools window was not created")


# --------------------------------------------------------------------- tools


REFUSED = [
    INSPECTOR_URL,
    "chrome://inspect",
    "CHROME://INSPECT/#devices",
    "about:inspect",
    "chrome://serviceworker-internals",
    f"{FRONTEND}?ws=127.0.0.1:1/devtools/page/x",
    "chrome-devtools://devtools/bundled/inspector.html",
    # re-review F2: Chrome decodes the host, so the up-front refusal must too
    "chrome://%69nspect/#pages",
    "chrome://%65xtensions/",
    "chrome://serviceworker%2Dinternals/",
    "chrome://inspect:80/#pages",
    "chrome://user@inspect/#pages",
    # fourth review F2: UTS 46 drops and maps more than NFKC does
    "chrome://i\u00adnspect/",
    "chrome://inspect\u3002/",
    "chrome://inspect%E3%80%82",
]


async def test_the_tools_refuse_to_load_an_inspector(site, tmp_path):
    async with _browser(tmp_path) as iid:
        before = await _listed(iid)
        for url in REFUSED:
            for tool in ("navigate", "new_tab"):
                with pytest.raises(ToolError):
                    await get_fn(tool)(instance_id=iid, url=url)
        assert await _listed(iid) == before


# ------------------------------------------------ the re-review's sequence (N1)


async def test_the_reviewers_inspect_sequence_does_not_read_the_password(
    site, tmp_path, monkeypatch
):
    """chrome://inspect, click "inspect" on every row, then ``execute_script`` in
    each frontend tab that ``list_tabs`` shows, while a slowed login types. On the
    code the re-review ran, ``navigate`` succeeds, four frontends are listed and the
    password is read in full. Now nothing is listed and nothing is read."""
    monkeypatch.setattr(login_flow, "KEY_DELAY_S", 0.2)
    unlocked_vault({"work": _entry(site)})
    async with _browser(tmp_path) as iid:
        browser = await _browser_of(iid)
        login = asyncio.create_task(_run(iid))
        for _ in range(200):
            await asyncio.sleep(0.05)
            if login_seal.sealing(iid):
                break
        refused, clicked = 0, 0
        for navigate in (
            lambda: get_fn("navigate")(instance_id=iid, url=INSPECTOR_URL),
            lambda: get_fn("new_tab")(instance_id=iid, url=INSPECTOR_URL),
            # the other doors to the same page: the CDP tool and history
            lambda: get_fn("execute_cdp_command")(
                instance_id=iid, command="Page.navigate", params={"url": INSPECTOR_URL}
            ),
            lambda: get_fn("go_back")(instance_id=iid),
        ):
            try:
                await navigate()
            except ToolError:
                refused += 1
                continue
            except Exception:  # noqa: BLE001  PERMANENT(a door that fails some other way is not a refusal)
                continue
            # the page loaded: do what the reviewer did, click "inspect" on every row
            await asyncio.sleep(1.5)
            for tab_id, url in await _listed(iid):
                if _is_inspector(url):
                    with contextlib.suppress(Exception):
                        clicked += int(
                            (
                                await get_fn("execute_script")(
                                    instance_id=iid, tab_id=tab_id, script=CLICK_INSPECT
                                )
                            ).get("result")
                            or 0
                        )
        shown, read = set(), []
        while not login.done():
            for tab_id, url in await _listed(iid):
                if _is_inspector(url):
                    shown.add(tab_id)
                    try:
                        answer = await get_fn("execute_script")(
                            instance_id=iid, tab_id=tab_id, script=READ_THROUGH_FRONTEND
                        )
                        read.append(str(answer.get("result")))
                    except Exception as error:  # noqa: BLE001  PERMANENT(a refusal is the wanted answer)
                        read.append(type(error).__name__)
            await asyncio.sleep(0.05)
        result = await login
        assert result["outcome"] == "logged_in", result
        assert refused == 4  # the tools and the CDP door are all shut during a login
        assert clicked == 0
        assert shown == set(), shown
        assert not [r for r in read if RIGHT[:3] in r]
        assert await _inspectors_in_chrome(browser) == []


# ------------------------------------------- however it got there (N1 b, c)


async def test_a_devtools_window_that_was_already_open_is_hidden_then_closed(
    site, tmp_path
):
    """A person's F12 (or one an agent made earlier over ``execute_cdp_command``).
    Outside a login it is only HIDDEN: not listed, not addressable. A login closes
    it before it types anything."""
    unlocked_vault({"work": _entry(site)})
    async with _browser(tmp_path) as iid:
        browser = await _browser_of(iid)
        window = await _open_frontend_privileged(browser)
        await asyncio.sleep(0.5)
        assert all(not _is_inspector(url) for _t, url in await _listed(iid))
        assert window not in [t for t, _u in await _listed(iid)]
        with pytest.raises(ToolError) as caught:
            await get_fn("execute_script")(
                instance_id=iid, tab_id=window, script="location.href"
            )
        # F5: no login is running, so the refusal must not say one is
        assert str(caught.value) == login_seal.INSPECTOR_REFUSAL
        assert window in [t for t, _u in await _inspectors_in_chrome(browser)]
        result = await _run(iid)
        assert result["outcome"] == "logged_in", result
        assert result["closed_devtools_windows"] == 1, result  # F5: it says so
        assert await _inspectors_in_chrome(browser) == []


async def test_a_window_that_cannot_be_closed_refuses_the_login(
    site, tmp_path, monkeypatch
):
    unlocked_vault({"work": _entry(site)})
    monkeypatch.setattr(debug_port_guard, "_CLEAN_RETRY_S", 0.0)
    async with _browser(tmp_path) as iid:
        browser = await _browser_of(iid)
        await _open_frontend_privileged(browser)

        # a close that closes nothing, in both places that close a window: the
        # guard's own event handler and the check before a field is typed
        async def no_deal(_self, _target, *, close):
            return None

        monkeypatch.setattr(debug_port_guard._Guard, "_deal", no_deal)
        monkeypatch.setattr(
            debug_port_guard.cdp.target,
            "close_target",
            lambda _id: cdp.browser.get_version(),
        )
        with pytest.raises(ToolError) as caught:
            await _run(iid)
        assert str(caught.value) == debug_port_guard.INSPECTOR_OPEN
        assert not login_seal.sealing(iid)
    assert site.posts == []  # nothing was typed, nothing was sent


async def test_a_tab_sent_to_chrome_inspect_over_cdp_is_blanked(site, tmp_path):
    """Before a login runs the CDP tool is allowed, and it can navigate the agent's
    OWN tab (the one a tool with no ``tab_id`` acts on) to ``chrome://inspect``. The
    page never stays: it is sent back to ``about:blank`` from the browser's event."""
    async with _browser(tmp_path) as iid:
        await get_fn("execute_cdp_command")(
            instance_id=iid, command="Page.navigate", params={"url": INSPECTOR_URL}
        )
        now = {}
        for _ in range(100):
            await asyncio.sleep(0.05)
            try:
                now = await get_fn("execute_script")(
                    instance_id=iid, script="location.href"
                )
            except Exception:  # noqa: BLE001  PERMANENT(F7: the guard is navigating the page away under this very script; that is the thing being waited for)
                continue
            if "inspect" not in str(now.get("result")):
                break
        assert str(now.get("result")) == "about:blank", now
        browser = await _browser_of(iid)
        assert await _inspectors_in_chrome(browser) == []


# ------------------------------------- the class rule while a login runs (F3)


async def test_during_a_login_no_browser_internal_page_loads_is_listed_or_scriptable(
    site, tmp_path, monkeypatch
):
    """Outside a login ``chrome://version`` is ordinary use. While one runs, every
    ``chrome://`` (and ``chrome-extension://``, ``chrome-untrusted://`` ...) page is
    hidden, unaddressable and cannot be loaded; ``about:blank`` and the new-tab page
    still work; afterwards the page is back."""
    monkeypatch.setattr(login_flow, "KEY_DELAY_S", 0.2)
    unlocked_vault({"work": _entry(site)})
    async with _browser(tmp_path) as iid:
        await get_fn("navigate")(instance_id=iid, url="chrome://version/")
        mine = next(t for t, u in await _listed(iid) if u.startswith("chrome://"))
        # navigate acts on the tab it is given: give it one that is not under test
        await get_fn("new_tab")(instance_id=iid, url="about:blank")
        scratch = next(t for t, _u in await _listed(iid) if t != mine)
        login = asyncio.create_task(_run(iid))
        for _ in range(200):
            await asyncio.sleep(0.05)
            if login_seal.sealing(iid):
                break
        listed, refused, loaded = set(), 0, []
        for url in ("chrome://version/", "chrome://history/", "chrome-extension://x/y"):
            for tool in ("navigate", "new_tab"):
                try:
                    where = {"tab_id": scratch} if tool == "navigate" else {}
                    await get_fn(tool)(instance_id=iid, url=url, **where)
                    loaded.append((tool, url))
                except ToolError:
                    refused += 1
        for url in ("about:blank", "chrome://newtab/"):
            await get_fn("navigate")(instance_id=iid, tab_id=scratch, url=url)
            await get_fn("new_tab")(instance_id=iid, url=url)
        reached = []
        while not login.done():
            listed |= {t for t, _u in await _listed(iid)}
            was = login_seal.sealing(iid)
            try:
                await get_fn("execute_script")(
                    instance_id=iid, tab_id=mine, script="location.href"
                )
                # the login may end between the two looks; it may not end later
                # than the call and still have had the page reachable inside it
                reached.append(was and login_seal.sealing(iid))
            except ToolError:
                pass
            await asyncio.sleep(0.1)
        assert (await login)["outcome"] == "logged_in"
        assert loaded == []
        assert refused == 6
        assert not any(reached), "the page was scriptable during the login"
        assert mine not in listed
        # the login is over: the page is shown again
        assert mine in [t for t, _u in await _listed(iid)]
        await get_fn("execute_script")(instance_id=iid, tab_id=mine, script="1")


# ---------------------------------------- the guard dropping, fail closed (F4)


async def test_a_barred_window_stays_barred_when_the_guard_drops(site, tmp_path):
    async with _browser(tmp_path) as iid:
        browser = await _browser_of(iid)
        window = await _open_frontend_privileged(browser)
        await asyncio.sleep(0.5)
        port = int(browser.config.port)
        await debug_port_guard._GUARDS[port].socket.close()
        for _ in range(100):
            if port not in debug_port_guard.protected_ports():
                break
            await asyncio.sleep(0.05)
        assert window not in [t for t, _u in await _listed(iid)]
        with pytest.raises(ToolError):
            await get_fn("execute_script")(
                instance_id=iid, tab_id=window, script="location.href"
            )


async def test_a_login_aborts_when_the_guard_drops_in_the_middle_of_it(
    site, tmp_path, monkeypatch
):
    """The guard's link drops after the password is typed and before Enter: the
    login stops there, clears what it typed, closes its tab, and nothing is sent."""
    unlocked_vault({"work": _entry(site)})
    async with _browser(tmp_path) as iid:
        browser = await _browser_of(iid)
        port = int(browser.config.port)

        async def drop_then_hang(_tab):
            await debug_port_guard._GUARDS[port].socket.close()
            await asyncio.sleep(60)

        monkeypatch.setattr(login_flow.text_entry, "press_enter", drop_then_hang)
        before = {t for t, _u in await _listed(iid)}
        with pytest.raises(ToolError) as caught:
            await asyncio.wait_for(_run(iid), 30)
        assert str(caught.value) == debug_port_guard.GUARD_DOWN
        assert not login_seal.sealing(iid)
        assert {t for t, _u in await _listed(iid)} <= before
        await tab_open.update_targets(browser)
        assert all(
            "login" not in str(getattr(t.target, "url", "")) for t in browser.tabs
        )
    assert site.posts == []  # Enter was never pressed: nothing reached the site


# ------------------------------------------------------------- the guard (N5)


async def test_a_login_in_a_browser_whose_guard_ended_refuses_to_type(site, tmp_path):
    unlocked_vault({"work": _entry(site)})
    async with _browser(tmp_path) as iid:
        browser = await _browser_of(iid)
        port = int(browser.config.port)
        await debug_port_guard._GUARDS[port].socket.close()  # the guard's link drops
        for _ in range(100):
            if port not in debug_port_guard.protected_ports():
                break
            await asyncio.sleep(0.05)
        with pytest.raises(ToolError) as caught:
            await _run(iid)
        assert str(caught.value) == debug_port_guard.GUARD_DOWN
        assert not login_seal.sealing(iid)
    assert site.posts == []


async def test_a_guard_that_ends_during_a_login_stops_it_before_the_password(
    site, tmp_path, monkeypatch
):
    """The username is typed (the guard was up), then its link drops: the password
    field's own check refuses, and the password is never typed or sent."""
    unlocked_vault({"work": _entry(site, "/two/login")})
    async with _browser(tmp_path) as iid:
        browser = await _browser_of(iid)
        port = int(browser.config.port)
        real = login_flow._after_username

        async def drop_the_guard(tab, hosts):
            await debug_port_guard._GUARDS[port].socket.close()
            for _ in range(100):
                if port not in debug_port_guard.protected_ports():
                    break
                await asyncio.sleep(0.05)
            return await real(tab, hosts)

        monkeypatch.setattr(login_flow, "_after_username", drop_the_guard)
        with pytest.raises(ToolError) as caught:
            await _run(iid)
        assert str(caught.value) == debug_port_guard.GUARD_DOWN
        assert not login_seal.sealing(iid)
    assert [p for p in site.posts if p.get("password")] == []


# ---------------------------------------- every target that can fetch (N2)


class _Attacker:
    """A cross-site (``localhost`` vs ``127.0.0.1``) page that, from inside its own
    iframe process, fetches the debug port, starts a worker that does, and (when
    ``nest``) nests a frame at the endpoint's own origin."""

    INNER = """<html><body>INNER<script>
const p = new URLSearchParams(location.search).get('p'); const r = {};
fetch('http://127.0.0.1:' + p + '/json/version', {mode: 'no-cors'}).then(
  x => { r.fetch = 'LOADED ' + x.type; }, e => { r.fetch = 'BLOCKED'; });
NEST
const w = new Worker(URL.createObjectURL(new Blob([
  "fetch('http://127.0.0.1:" + p + "/json/version', {mode: 'no-cors'}).then(x => postMessage('LOADED'), e => postMessage('BLOCKED'))"])));
w.onmessage = (m) => { r.worker = m.data; };
setTimeout(() => { parent.postMessage(JSON.stringify(r), '*'); }, 2500);
</script></body></html>"""
    NESTED = (
        "const f = document.createElement('iframe');"
        " f.src = 'http://127.0.0.1:' + p + '/devtools/inspector.html';"
        " document.body.appendChild(f);"
    )

    def __init__(self, *, nest):
        inner = self.INNER.replace("NEST", self.NESTED if nest else "")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                body = inner if path == "/inner" else "<html><body>A</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args):
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()


async def _attack(iid, attacker, browser):
    """Load the attacker's page, which holds the cross-site frame; return the targets
    Chrome lists after it has run and what the frame posted to its parent."""
    port = int(browser.config.port)
    await get_fn("navigate")(instance_id=iid, url=f"http://127.0.0.1:{attacker.port}/a")
    await get_fn("execute_script")(
        instance_id=iid,
        script=(
            "window.__r = ''; addEventListener('message', e => { window.__r = e.data; });"
            "document.body.innerHTML = '<iframe src=\"http://localhost:"
            f"{attacker.port}/inner?p={port}\"></iframe>'; 1"
        ),
    )
    await asyncio.sleep(5)
    infos = await tab_open.send(browser, cdp.target.get_targets)
    try:
        said = await get_fn("execute_script")(instance_id=iid, script="window.__r")
    except ToolError:
        said = {}
    return infos, str(said.get("result") or "")


def _at_the_port(infos, port):
    return [
        (str(i.type_), str(i.url))
        for i in infos
        if f":{port}" in str(i.url) and not str(i.url).startswith("chrome-error")
    ]


async def test_a_cross_site_iframe_cannot_fetch_the_debug_port(site, tmp_path):
    """The re-review's N2: the page-only guard let a cross-site frame (its own
    process) fetch the endpoint. Every target that can make requests is guarded."""
    attacker = _Attacker(nest=False)
    try:
        async with _browser(tmp_path) as iid:
            browser = await _browser_of(iid)
            infos, said = await _attack(iid, attacker, browser)
            assert said, "the frame never reported"
            result = json.loads(said)
            assert result.get("fetch") == "BLOCKED", result
            assert result.get("worker") in (None, "BLOCKED"), result
            assert _at_the_port(infos, int(browser.config.port)) == []
    finally:
        attacker.close()


async def test_a_frame_nested_at_the_endpoint_origin_never_runs(site, tmp_path):
    """The one Origin Chrome accepts is the endpoint's own. A frame that commits
    there (in its own process, nested in a cross-site frame) is held paused so none
    of its script runs, and the page that holds it is sent to ``about:blank``."""
    attacker = _Attacker(nest=True)
    try:
        async with _browser(tmp_path) as iid:
            browser = await _browser_of(iid)
            infos, _said = await _attack(iid, attacker, browser)
            assert _at_the_port(infos, int(browser.config.port)) == []
            now = await get_fn("execute_script")(
                instance_id=iid, script="location.href"
            )
            assert str(now.get("result")) == "about:blank", now
    finally:
        attacker.close()
