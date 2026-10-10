"""F-961 -- the shared ``fleet`` browser comes BACK after it is closed, over the real wire.

Several Claude chats share one signed-in browser: each calls
``spawn_browser(session="fleet")`` and works in its own tab. The owner's rule is
that nothing one chat does to that browser may strand the others -- an accidental
``close_instance`` or a closed window must cost them one ``spawn_browser`` call,
not their login. This node is that situation with nothing in-process: one
isolated backend, independent stdio clients of it, real headless Chrome, and the
fixture app's server-set HttpOnly cookie standing in for the login.

Four closures, each asserted from the NEXT chat's answer, in one journey so the
Chrome cold start is paid once:

1. ``close_instance`` by one chat, ``spawn_browser`` by another: a NEW instance
   on the SAME directory, ``already_running`` absent, no ``fleet-2``, the login
   still on disk;
2. the Chrome process killed by PID with no ``close_instance`` (a window closed,
   a crash): the next spawn does not hand back the dead instance, relaunches,
   and the stale lock the death left does not refuse or walk it;
3. a chat still holding the OLD instance id gets an error that says how to get
   back (``spawn_browser(session="fleet")``), not just "not found";
4. two chats asking at once, right after a death, end up with ONE browser.

Marked like ``test_e2e_fleet_across_sessions``: it drives the installed launcher,
so macOS (F-773) runs ``integration and not transport`` and makes no claim here.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

import psutil
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
    pytest.mark.timeout(420),
]

if not CAN_RUN:
    pytestmark.append(pytest.mark.skip("Chrome not available or server failed to load"))

JOURNEY_BUDGET = 390.0
#: Fields a walk to ``<name>-N`` adds to ``profile_selection`` (F-871).
WALK_FIELDS = ("requested_user_data_dir", "walked_to", "walk_reason")
SIGNED_IN = f"{LOGIN_COOKIE}={LOGIN_COOKIE_VALUE}"

_COOKIE_HEADER_JS = """
(async () => {
  const reply = await fetch('/api/echo', {method: 'POST', body: 'f961'});
  const answer = await reply.json();
  return answer.headers.cookie || '';
})()
"""


def _client(launcher: str, space: dict[str, Any]) -> Client:
    """One Claude chat's proxy: its own process, the workspace's backend."""
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


def _browsers_on(directory: Path) -> list[int]:
    """Pids of the Chrome BROWSER processes whose profile is *directory*.

    The test's own witness (not the product's): a command line carrying
    ``--user-data-dir=<directory>`` and no ``--type=``, which Chromium gives
    every child.
    """
    wanted = os.path.normcase(str(directory.resolve()))
    found: list[int] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        argv = proc.info["cmdline"] or []
        if any(arg.startswith("--type=") for arg in argv):
            continue
        for arg in argv:
            if arg.startswith("--user-data-dir="):
                profile = arg.partition("=")[2].strip('"')
                if os.path.normcase(str(Path(profile).resolve())) == wanted:
                    found.append(proc.info["pid"])
    return sorted(found)


async def _until_gone(directory: Path, budget: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + budget
    while _browsers_on(directory):
        assert asyncio.get_running_loop().time() < deadline, _browsers_on(directory)
        await asyncio.sleep(0.25)


async def _cookie_header(client: Client, iid: str, page: str) -> str:
    await _call(client, "navigate", {"instance_id": iid, "url": page}, NAV_TIMEOUT)
    await _settle_dom(client, iid)
    return str(await _eval(client, iid, _COOKIE_HEADER_JS))


def _assert_relaunched(
    answer: dict[str, Any], old_iid: str, fleet_dir: Path, sessions: Path
) -> None:
    """The shape every closure owes the next chat: a fresh browser, same place."""
    assert "already_running" not in answer, answer
    assert answer["instance_id"] != old_iid, answer
    selection = answer["spawn_diagnostics"]["profile_selection"]
    assert _same_dir(selection["user_data_dir"], fleet_dir), selection
    assert not [f for f in WALK_FIELDS if f in selection], selection
    assert "seed_warning" not in answer, answer
    assert not (sessions / "fleet-2").exists()
    assert len(_browsers_on(fleet_dir)) == 1, _browsers_on(fleet_dir)


async def _journey(launcher: str, space: dict[str, Any], base_url: str) -> None:
    kwargs = _headless_spawn_kwargs(session="fleet")
    page = f"{base_url}/index.html"
    sessions = space["session_root"] / "sessions"
    fleet_dir = sessions / "fleet"

    async with (
        _client(launcher, space) as chat_a,
        _client(launcher, space) as chat_b,
    ):
        await _cold_start_warmup(chat_a, base_url, space["log_dir"], {})
        opened = await _call(chat_a, "spawn_browser", kwargs, SPAWN_TIMEOUT)
        first = opened["instance_id"]
        assert _same_dir(
            opened["spawn_diagnostics"]["profile_selection"]["user_data_dir"],
            fleet_dir,
        ), opened
        await _call(
            chat_a,
            "navigate",
            {"instance_id": first, "url": f"{base_url}/api/login"},
            NAV_TIMEOUT,
        )
        assert SIGNED_IN in await _cookie_header(chat_a, first, page), "the control"

        # -- 1. chat A closes it, chat B asks for it ---------------------------
        closed = await _call(
            chat_a, "close_instance", {"instance_id": first}, CLOSE_TIMEOUT
        )
        assert closed.get("closed") is True, closed
        await _until_gone(fleet_dir)

        # -- 3. chat A, still holding the old id, is told how to get back ------
        stale = await chat_a.call_tool(
            "navigate", {"instance_id": first, "url": page}, raise_on_error=False
        )
        assert stale.is_error
        said = " ".join(getattr(c, "text", "") for c in stale.content)
        assert 'spawn_browser(session="fleet")' in said, said

        back = await _call(chat_b, "spawn_browser", kwargs, SPAWN_TIMEOUT)
        _assert_relaunched(back, first, fleet_dir, sessions)
        second = back["instance_id"]
        assert SIGNED_IN in await _cookie_header(chat_b, second, page), (
            "the login did not survive close_instance"
        )

        # -- 2. the Chrome process dies with nobody closing it -----------------
        (victim,) = _browsers_on(fleet_dir)
        psutil.Process(victim).kill()
        await _until_gone(fleet_dir)

        again = await _call(chat_a, "spawn_browser", kwargs, SPAWN_TIMEOUT)
        _assert_relaunched(again, second, fleet_dir, sessions)
        third = again["instance_id"]
        assert victim not in _browsers_on(fleet_dir)
        assert SIGNED_IN in await _cookie_header(chat_a, third, page), (
            "the login did not survive the browser's death"
        )

        # -- 4. two chats ask at once, right after another death ----------------
        (victim,) = _browsers_on(fleet_dir)
        psutil.Process(victim).kill()
        await _until_gone(fleet_dir)

        answers = await asyncio.gather(
            _call(chat_a, "spawn_browser", kwargs, SPAWN_TIMEOUT),
            _call(chat_b, "spawn_browser", kwargs, SPAWN_TIMEOUT),
        )
        assert len({a["instance_id"] for a in answers}) == 1, answers
        assert sorted(bool(a.get("already_running")) for a in answers) == [
            False,
            True,
        ], answers
        assert len(_browsers_on(fleet_dir)) == 1, _browsers_on(fleet_dir)
        assert not (sessions / "fleet-2").exists()
        last = answers[0]["instance_id"]
        assert SIGNED_IN in await _cookie_header(chat_b, last, page)

        await _call(
            chat_a,
            "close_instance",
            {"instance_id": last},
            CLOSE_TIMEOUT,
            raise_on_error=False,
            allow_fail=True,
        )


async def test_the_fleet_comes_back_after_every_way_of_losing_it(tmp_path):
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
