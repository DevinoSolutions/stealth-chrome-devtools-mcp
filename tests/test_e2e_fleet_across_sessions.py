"""F-958 -- the shared ``fleet`` browser survives ACROSS Claude sessions, over the real wire.

The owner's shared signed-in browser is the named session ``fleet`` (F-952).
Another Claude Code session is a separate ``claude`` process with its own stdio
proxy and its own MCP session, talking to the SAME backend. This node is that
situation, with nothing in-process: one isolated backend, two independent
stdio clients of it (``initialize`` twice, two ``mcp-session-id``s), real
headless Chrome, and the fixture app's server-set HttpOnly cookie standing in
for the login (``GET /api/login``; page script cannot read it, so the fixture's
echo of the ``Cookie`` header is the oracle, as in ``test_e2e_fleet_session``).

What a second session is owed, each asserted from the second client's answer:

* ``spawn_browser(session="fleet")`` returns ``already_running: true``, the
  SAME ``instance_id`` and the SAME ``user_data_dir`` -- not a ``fleet-2``, not
  a clone -- with no walk fields and no ``seed_warning``;
* it is not told "Named session created" about a directory it did not create;
* the login is still there when it reads the page, and still there for the
  first session after the second one has gone.

``test_e2e_fleet_session`` proves the same decisions at the ``.fn`` seam inside
one process; it cannot show that a second proxy reaches the same backend, which
is the thing the owner relies on. The refusal of a DIFFERENT backend asking for
a held fleet is pinned hermetically in ``test_fleet_session`` (it needs a second
backend identity, which a gate cell does not have).

Marked ``integration`` + ``transport`` like every node that drives the installed
launcher, so macOS (F-773: navigation under the detached backend hangs there)
runs ``integration and not transport`` and makes no claim about this one.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from e2e_helpers import CAN_RUN
from release_gate_harness import (
    CLOSE_TIMEOUT,
    INIT_TIMEOUT,
    LOGIN_COOKIE,
    LOGIN_COOKIE_VALUE,
    NAV_TIMEOUT,
    SPAWN_TIMEOUT,
    _backend_pids_from_state,
    _call,
    _cold_start_warmup,
    _eval,
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
    # Both gate cells run at --timeout=300; the journey below is bounded by its
    # own budget inside that, so a hang fails by name instead of by the plugin.
    pytest.mark.timeout(300),
]

if not CAN_RUN:
    pytestmark.append(pytest.mark.skip("Chrome not available or server failed to load"))

JOURNEY_BUDGET = 270.0
#: Fields a walk to ``<name>-N`` adds to ``profile_selection`` (F-871).
WALK_FIELDS = ("requested_user_data_dir", "walked_to", "walk_reason")

_COOKIE_HEADER_JS = """
(async () => {
  const reply = await fetch('/api/echo', {method: 'POST', body: 'f958'});
  const answer = await reply.json();
  return answer.headers.cookie || '';
})()
"""


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


def _same_dir(a: str, b: Path) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(
        str(b.resolve())
    )


async def _cookie_header(client: Client, iid: str, page: str) -> str:
    await _call(client, "navigate", {"instance_id": iid, "url": page}, NAV_TIMEOUT)
    await _settle_dom(client, iid)
    return str(await _eval(client, iid, _COOKIE_HEADER_JS))


async def _journey(launcher: str, space: dict[str, Any], base_url: str) -> None:
    kwargs = _headless_spawn_kwargs(session="fleet")
    page = f"{base_url}/index.html"
    sessions = space["session_root"] / "sessions"
    fleet_dir = sessions / "fleet"

    async with _client(launcher, space) as first:
        await _cold_start_warmup(first, base_url, space["log_dir"], {})
        opened = await _call(first, "spawn_browser", kwargs, SPAWN_TIMEOUT)
        iid = opened["instance_id"]
        try:
            created = opened["spawn_diagnostics"]["profile_selection"]
            assert _same_dir(created["user_data_dir"], fleet_dir), created
            assert "already_running" not in opened, opened
            # The control: the creating call IS the one that created it.
            assert created["warning"].startswith("Named session created"), created

            await _call(
                first,
                "navigate",
                {"instance_id": iid, "url": f"{base_url}/api/login"},
                NAV_TIMEOUT,
            )
            signed_in = f"{LOGIN_COOKIE}={LOGIN_COOKIE_VALUE}"
            assert signed_in in await _cookie_header(first, iid, page), "the control"
            backends = _backend_pids_from_state(space["home_dir"])
            assert len(backends) == 1, backends

            async with _client(launcher, space) as second:
                again = await _call(second, "spawn_browser", kwargs, SPAWN_TIMEOUT)

                assert again["already_running"] is True, again
                assert again["instance_id"] == iid, again
                assert "seed_warning" not in again, again
                assert "headless_mismatch" not in again, again
                selection = again["spawn_diagnostics"]["profile_selection"]
                assert selection["user_data_dir"] == created["user_data_dir"]
                assert not [f for f in WALK_FIELDS if f in selection], selection
                assert not (sessions / "fleet-2").exists()
                warning = selection.get("warning", "")
                assert not warning.startswith("Named session created"), warning

                # Another session's view of the page: the login is still there.
                assert signed_in in await _cookie_header(second, iid, page)
                assert _backend_pids_from_state(space["home_dir"]) == backends

            # And the second session going away takes nothing with it.
            assert signed_in in await _cookie_header(first, iid, page)
        finally:
            await _call(
                first,
                "close_instance",
                {"instance_id": iid},
                CLOSE_TIMEOUT,
                raise_on_error=False,
                allow_fail=True,
            )


async def test_a_second_claude_session_gets_the_same_signed_in_fleet(tmp_path):
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
