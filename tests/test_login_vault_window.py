"""No tool can reach the login's tab, or its popup, at any moment (T-19366).

The review's popup script showed two things: the popup of a sealed tab is closed
(and, after the fix, sealed before it is closed), and, found by running it again
with the ids printed, that an agent polling ``list_tabs`` also SAW THE LOGIN'S OWN
TAB in the instant between Chrome creating it and the flow sealing it, and ran a
script in it. This is that agent: it polls ``list_tabs`` every few milliseconds
for the whole login, runs ``execute_script`` in every tab id it was not given, and
must never get a tab or a result back.

Only tools are used by the agent, so the test is RED against a tree without the
seal's creation window and popup handling.
"""

from __future__ import annotations

import asyncio

import test_login_flow as flow
from e2e_helpers import get_fn
from vault_helpers import unlocked_vault

pytestmark = flow.pytestmark
site, vault, _warmup = flow.site, flow.vault, flow._warmup
_browser, _entry, _run, RIGHT = flow._browser, flow._entry, flow._run, flow.RIGHT

POPUP_LOGIN = """<html><body><form method=post action="{action}">
<input name=username type=text autocomplete=username>
<input name=password type=password autocomplete=current-password
 onkeydown="if(!window.__o){{window.__o=1;window.open('/popup','_blank')}}">
<button>go</button></form>{note}</body></html>"""


async def _agent(iid, base, seen, stop):
    """Every tab the agent is shown that it was not given, and what a script run in
    it answered."""
    list_tabs, execute = get_fn("list_tabs"), get_fn("execute_script")

    async def probe(tab_id):
        try:
            seen["executed"].append(
                await execute(instance_id=iid, tab_id=tab_id, script="location.href")
            )
        except Exception as error:  # noqa: BLE001  PERMANENT(a refusal is the wanted answer)
            seen["refused"].append(type(error).__name__)

    tasks = []
    while not stop.is_set():
        try:
            tabs = await list_tabs(instance_id=iid)
        except Exception:  # noqa: BLE001  PERMANENT(a refused or racing listing is not a sighting)
            await asyncio.sleep(0.01)
            continue
        for tab in tabs:
            if tab["tab_id"] not in base and tab["tab_id"] not in seen["listed"]:
                seen["listed"].append(tab["tab_id"])
                tasks.append(asyncio.create_task(probe(tab["tab_id"])))
        # a tool that names an id it was never given, as a model might guess one
        await asyncio.sleep(0.005)
    await asyncio.gather(*tasks)


async def _watched_login(site, tmp_path, monkeypatch, login_form):
    if login_form is not None:
        monkeypatch.setattr(flow, "LOGIN_FORM", login_form)
    unlocked_vault({"work": _entry(site)})
    async with _browser(tmp_path) as iid:
        base = [t["tab_id"] for t in await get_fn("list_tabs")(instance_id=iid)]
        seen = {"listed": [], "executed": [], "refused": []}
        stop = asyncio.Event()
        agent = asyncio.create_task(_agent(iid, base, seen, stop))
        try:
            result = await _run(iid)
            await asyncio.sleep(1.0)
        finally:
            stop.set()
            await agent
        after = [t["tab_id"] for t in await get_fn("list_tabs")(instance_id=iid)]
    return result, seen, base, after


async def test_the_agent_never_sees_or_runs_anything_in_the_login_tab(
    site, tmp_path, monkeypatch
):
    result, seen, base, after = await _watched_login(site, tmp_path, monkeypatch, None)
    assert result["outcome"] == "logged_in", result
    assert seen["listed"] == [], seen
    assert seen["executed"] == [], seen
    assert set(after) <= set(base)


async def test_nor_the_popup_its_page_opens(site, tmp_path, monkeypatch):
    result, seen, base, after = await _watched_login(
        site, tmp_path, monkeypatch, POPUP_LOGIN
    )
    assert result["outcome"] == "logged_in", result
    assert seen["listed"] == [], seen
    assert seen["executed"] == [], seen
    assert set(after) <= set(base), (after, base)
