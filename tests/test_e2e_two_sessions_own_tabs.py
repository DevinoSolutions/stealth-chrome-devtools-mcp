"""F-962 -- two Claude sessions drive two tabs of ONE browser at once, over the real wire.

The field failure: chats sharing the ``fleet`` browser had their page tools act
on whichever tab the instance last switched to, so BioFlow's and uprank's
``execute_script`` read another chat's GCP console and MinIO login tabs, and a
click or a keystroke would have landed there too. This node is that situation
with nothing in-process: one isolated backend, two independent stdio proxies
(two ``initialize``s, two callers), real headless Chrome, one shared browser.

What each session is owed, asserted from its own answers:

* ``spawn_browser`` hands each session a ``tab_id`` of its own -- the second
  gets ``already_running: true`` AND a different tab;
* with both sessions calling at the same moment -- reads, ``switch_tab`` to
  their own tab (the call that used to move everyone), and ``type_text`` into
  the same field of the same page -- every read lands in the caller's tab and
  every keystroke in the caller's field;
* ``tab_id`` reaches a named tab on request, and does not rebind;
* a session whose tab was closed is told so, and is not quietly moved into the
  other session's tab.

Marked ``integration`` + ``transport`` like every node that drives the installed
launcher (macOS runs ``integration and not transport``, F-773).
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from e2e_helpers import CAN_RUN
from release_gate_harness import (
    CALL_TIMEOUT,
    CLOSE_TIMEOUT,
    INIT_TIMEOUT,
    NAV_TIMEOUT,
    SPAWN_TIMEOUT,
    _call,
    _cold_start_warmup,
    _headless_spawn_kwargs,
    _settle_dom,
    gate_work_dir,
    gate_workspace,
    resolve_launcher,
    serve_fixture_app,
    workspace_backend_logs,
    workspace_proxy_warnings,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.transport,
    pytest.mark.timeout(300),
]

if not CAN_RUN:
    pytestmark.append(pytest.mark.skip("Chrome not available or server failed to load"))

JOURNEY_BUDGET = 270.0
#: Rounds of simultaneous calls from both sessions; each round also switches
#: both sessions to their own tab, which is what moved everyone before F-962.
ROUNDS = 6
_WHO_JS = "new URLSearchParams(location.search).get('who')"
_FIELD_JS = "document.querySelector('#text-input').value"


def _client(launcher: str, space: dict[str, Any]) -> Client:
    """One Claude session's proxy: its own process, the workspace's backend."""
    return Client(
        StdioTransport(
            command=launcher,
            args=["--singleton-port", str(space["port"])],
            env=space["env"],
            keep_alive=False,
        ),
        init_timeout=INIT_TIMEOUT,
    )


async def _eval(client: Client, iid: str, script: str, **extra: Any) -> Any:
    result = await _call(
        client,
        "execute_script",
        {"instance_id": iid, "script": script, **extra},
        CALL_TIMEOUT,
    )
    assert isinstance(result, dict) and result.get("success") is True, result
    return result.get("result")


async def _refusal(client: Client, name: str, args: dict[str, Any]) -> str:
    """The error text of a call that must fail as a tool error."""
    result = await asyncio.wait_for(
        client.call_tool(name, args, raise_on_error=False), CALL_TIMEOUT
    )
    assert result.is_error, result
    return " ".join(getattr(part, "text", "") for part in result.content)


async def _journey(launcher: str, space: dict[str, Any], base_url: str) -> None:
    kwargs = _headless_spawn_kwargs(session="fleet")
    page = f"{base_url}/interact.html"

    async with _client(launcher, space) as first:
        await _cold_start_warmup(first, base_url, space["log_dir"], {})
        opened = await _call(first, "spawn_browser", kwargs, SPAWN_TIMEOUT)
        iid = opened["instance_id"]
        mine = opened.get("tab_id")
        assert mine, opened
        try:
            async with _client(launcher, space) as second:
                again = await _call(second, "spawn_browser", kwargs, SPAWN_TIMEOUT)
                assert again["already_running"] is True, again
                assert again["instance_id"] == iid, again
                theirs = again.get("tab_id")
                assert theirs and theirs != mine, (opened, again)

                # Both sessions navigate at the same moment, each without tab_id.
                await asyncio.gather(
                    _call(
                        first,
                        "navigate",
                        {"instance_id": iid, "url": f"{page}?who=first"},
                        NAV_TIMEOUT,
                    ),
                    _call(
                        second,
                        "navigate",
                        {"instance_id": iid, "url": f"{page}?who=second"},
                        NAV_TIMEOUT,
                    ),
                )
                await _settle_dom(first, iid)
                await _settle_dom(second, iid)

                for round_no in range(ROUNDS):
                    calls = [_eval(first, iid, _WHO_JS), _eval(second, iid, _WHO_JS)]
                    # The call that used to move EVERY session: each switches
                    # the browser to its own tab while the other is reading.
                    switch = first if round_no % 2 else second
                    target = mine if switch is first else theirs
                    calls.append(
                        _call(
                            switch,
                            "switch_tab",
                            {"instance_id": iid, "tab_id": target},
                            CALL_TIMEOUT,
                        )
                    )
                    seen = await asyncio.gather(*calls)
                    assert seen[:2] == ["first", "second"], (round_no, seen)

                # Keystrokes, simultaneously, into the same field of the same page.
                await asyncio.gather(
                    _call(
                        first,
                        "type_text",
                        {
                            "instance_id": iid,
                            "selector": "#text-input",
                            "text": "typed-by-first",
                        },
                        CALL_TIMEOUT,
                    ),
                    _call(
                        second,
                        "type_text",
                        {
                            "instance_id": iid,
                            "selector": "#text-input",
                            "text": "typed-by-second",
                        },
                        CALL_TIMEOUT,
                    ),
                )
                assert await _eval(first, iid, _FIELD_JS) == "typed-by-first"
                assert await _eval(second, iid, _FIELD_JS) == "typed-by-second"

                # tab_id reaches the named tab, and the default does not move.
                assert await _eval(first, iid, _WHO_JS, tab_id=theirs) == "second"
                assert await _eval(first, iid, _WHO_JS) == "first"
                active = await _call(
                    second, "get_active_tab", {"instance_id": iid}, CALL_TIMEOUT
                )
                assert active["tab_id"] == theirs, active

                # The second session closes its tab: it is told, and is NOT
                # quietly moved into the first session's tab.
                closed = await _call(
                    second,
                    "close_tab",
                    {"instance_id": iid, "tab_id": theirs},
                    CALL_TIMEOUT,
                )
                assert closed is True, closed
                said = await _refusal(
                    second, "execute_script", {"instance_id": iid, "script": _WHO_JS}
                )
                assert "new_tab" in said, said
                assert await _eval(first, iid, _WHO_JS) == "first"
        finally:
            await _call(
                first,
                "close_instance",
                {"instance_id": iid},
                CLOSE_TIMEOUT,
                raise_on_error=False,
                allow_fail=True,
            )


async def test_two_claude_sessions_drive_their_own_tabs_at_once(tmp_path):
    launcher = str(resolve_launcher())
    work_dir = gate_work_dir(tmp_path)  # RUNNER_TEMP on CI (see helper docstring)
    try:
        with gate_workspace(work_dir) as space, serve_fixture_app() as base_url:
            try:
                await asyncio.wait_for(
                    _journey(launcher, space, base_url), JOURNEY_BUDGET
                )
            except BaseException as exc:
                raise AssertionError(
                    f"{type(exc).__name__}: {exc}\n"
                    f"--- proxy warnings ---\n"
                    f"{workspace_proxy_warnings(space)[-3000:]}\n"
                    f"--- backend logs ---\n{workspace_backend_logs(space)[-3000:]}"
                ) from exc
        assert not space["leftover_children"], space["leftover_children"]
    finally:
        if work_dir != tmp_path:  # pytest cleans its own; this one is ours
            shutil.rmtree(work_dir, ignore_errors=True)
