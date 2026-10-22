"""Pins for T-5655 / F-1023: a backend a cold start spawned for a proxy that never came.

The defect, reproduced 2026-10-21 in an isolated HOME: ``python -m
stealth_chrome_devtools_mcp`` cold-started a backend and was closed before the backend
finished booting (it took 51 s on a swapping machine). The proxy was gone, nothing ever
connected, and the backend sat listening for as long as the machine stayed up (~140 MB).
Nothing in the tree ended it: the browser idle reaper has no browser to reap, and the
liveness machinery only judges backends someone is talking to.

The fix is the ``--exit-if-unadopted`` flag the cold start passes, and
``session_hygiene.exit_if_unadopted``, which asks two questions after a grace: did any
request ever reach ``/mcp``, and does it host a browser. These pins are hermetic; the
real-process check (a real backend, an isolated HOME) is recorded in the finding.
"""

import runpy
import sys

import anyio
import anyio.lowlevel
import httpx
import pytest
from fastmcp import FastMCP

from stealth_chrome_devtools_mcp import observability
from stealth_chrome_devtools_mcp.embedded import (
    browser_reattach,
    logging_setup,
    session_hygiene,
    singleton,
)
from stealth_chrome_devtools_mcp.embedded import (
    server as embedded_server,
)


@pytest.fixture
def manager(monkeypatch):
    """A fresh manager as the ONLY registered one, with a grace of a few ms."""
    monkeypatch.setattr(session_hygiene, "_managers", [])
    monkeypatch.setattr(session_hygiene, "UNADOPTED_AFTER_SECONDS", 0.01)
    return session_hygiene.HygienicSessionManager(app=object())  # type: ignore[arg-type]


async def _no_browsers() -> bool:
    return False


async def _one_browser() -> bool:
    return True


async def test_a_backend_nobody_came_for_stops_itself(manager):
    stops = []
    assert await session_hygiene.exit_if_unadopted(
        _no_browsers, lambda: stops.append(1)
    )
    assert stops == [1]


async def test_a_backend_that_was_ever_reached_is_left_alone(monkeypatch, manager):
    """However quiet it later goes: an adopted backend's lifetime is its clients'."""

    async def sdk_handles_it(self, scope, receive, send):
        return None

    monkeypatch.setattr(
        session_hygiene.FastMCPStreamableHTTPSessionManager,
        "handle_request",
        sdk_handles_it,
    )
    assert manager.ever_served is False
    await manager.handle_request(
        {"type": "http", "method": "POST", "headers": []}, None, None
    )
    assert manager.ever_served is True
    stops = []
    assert not await session_hygiene.exit_if_unadopted(
        _no_browsers, lambda: stops.append(1)
    )
    assert stops == []


async def test_a_backend_hosting_a_browser_is_left_alone(manager):
    stops = []
    assert not await session_hygiene.exit_if_unadopted(
        _one_browser, lambda: stops.append(1)
    )
    assert stops == []


async def test_an_unanswerable_browser_question_keeps_the_backend(manager):
    async def broken() -> bool:
        raise RuntimeError("no answer")

    stops = []
    assert not await session_hygiene.exit_if_unadopted(broken, lambda: stops.append(1))
    assert stops == []


async def test_without_a_manager_nothing_is_stopped(monkeypatch):
    monkeypatch.setattr(session_hygiene, "_managers", [])
    monkeypatch.setattr(session_hygiene, "UNADOPTED_AFTER_SECONDS", 0.01)
    stops = []
    assert not await session_hygiene.exit_if_unadopted(
        _no_browsers, lambda: stops.append(1)
    )
    assert stops == []


async def test_the_grace_is_waited_before_anything_is_decided(monkeypatch, manager):
    monkeypatch.setattr(session_hygiene, "UNADOPTED_AFTER_SECONDS", 0.3)
    stops = []
    with anyio.move_on_after(0.1):
        await session_hygiene.exit_if_unadopted(_no_browsers, lambda: stops.append(1))
    assert stops == []


def test_the_grace_is_the_one_abandonment_window():
    """One number for "gone, not thinking"; a second constant would drift."""
    assert (
        session_hygiene.UNADOPTED_AFTER_SECONDS
        == session_hygiene.ABANDONED_AFTER_SECONDS
    )


def test_the_cold_start_argv_asks_for_the_exit():
    assert "--exit-if-unadopted" in singleton._server_process_cmd(4321)


def test_the_flag_is_off_for_a_backend_an_operator_started():
    parser = embedded_server.build_arg_parser()
    assert parser.parse_args(["--transport", "http"]).exit_if_unadopted is False
    assert (
        parser.parse_args(
            ["--transport", "http", "--exit-if-unadopted"]
        ).exit_if_unadopted
        is True
    )


# ---------------------------------------------------------------------------
# PR 269 review (F-1023): the check/stop race, a re-attach in flight, the CLI stamp,
# and what does NOT count as a client.
# ---------------------------------------------------------------------------


async def _a_request_arrives(manager, monkeypatch):
    """One REAL ``handle_request`` call: the SDK part is stubbed, ours is not."""

    async def sdk_handles_it(self, scope, receive, send):
        return None

    monkeypatch.setattr(
        session_hygiene.FastMCPStreamableHTTPSessionManager,
        "handle_request",
        sdk_handles_it,
    )
    await manager.handle_request(
        {"type": "http", "method": "POST", "headers": []}, None, None
    )


async def test_a_request_arriving_during_the_browser_question_aborts_the_exit(
    monkeypatch, manager
):
    """The race: ever_served is read, ``has_browsers`` suspends on the manager lock, a
    proxy connects, and the stop used to fire on the stale answer."""

    async def request_lands_while_listing() -> bool:
        await anyio.lowlevel.checkpoint()  # the suspension the lock gives
        await _a_request_arrives(manager, monkeypatch)
        return False  # no browser, as it was when the question started

    stops = []
    assert not await session_hygiene.exit_if_unadopted(
        request_lands_while_listing, lambda: stops.append(1)
    )
    assert stops == []


async def test_a_request_still_in_flight_counts_as_served(monkeypatch, manager):
    """ever_served is set on ENTRY, so a handshake that has begun but not finished
    already protects the backend."""
    gate = anyio.Event()

    async def sdk_blocks(self, scope, receive, send):
        await gate.wait()

    monkeypatch.setattr(
        session_hygiene.FastMCPStreamableHTTPSessionManager,
        "handle_request",
        sdk_blocks,
    )
    stops = []
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            manager.handle_request,
            {"type": "http", "method": "POST", "headers": []},
            None,
            None,
        )
        await anyio.lowlevel.checkpoint()
        assert not await session_hygiene.exit_if_unadopted(
            _no_browsers, lambda: stops.append(1)
        )
        gate.set()
    assert stops == []


async def test_a_reattach_pass_in_progress_blocks_the_exit_until_it_ends(
    monkeypatch, manager
):
    """Its browsers are not in list_instances until adopted, so no browser yet does not
    mean none coming: the decision waits for the pass, then asks again."""
    monkeypatch.setattr(session_hygiene, "REATTACH_POLL_SECONDS", 0.01)
    busy = [True]
    stops = []
    task_result = []

    async def decide():
        task_result.append(
            await session_hygiene.exit_if_unadopted(
                _no_browsers, lambda: stops.append(1), reattaching=lambda: busy[0]
            )
        )

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(decide)
        await anyio.sleep(0.15)  # well past the grace, pass still running
        assert stops == [] and task_result == []
        busy[0] = False
    assert stops == [1] and task_result == [True]


async def test_a_pass_that_adopted_a_browser_keeps_the_backend(monkeypatch, manager):
    monkeypatch.setattr(session_hygiene, "REATTACH_POLL_SECONDS", 0.01)
    busy = [True]
    adopted = []

    async def has_browsers() -> bool:
        return bool(adopted)

    async def pass_ends_having_adopted():
        await anyio.sleep(0.05)
        adopted.append("fleet")
        busy[0] = False

    stops = []
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(pass_ends_having_adopted)
        assert not await session_hygiene.exit_if_unadopted(
            has_browsers, lambda: stops.append(1), reattaching=lambda: busy[0]
        )
    assert stops == []


async def test_a_pass_that_starts_during_the_browser_question_aborts_the_exit(manager):
    busy = [False]

    async def pass_starts_while_listing() -> bool:
        busy[0] = True
        return False

    stops = []
    assert not await session_hygiene.exit_if_unadopted(
        pass_starts_while_listing, lambda: stops.append(1), reattaching=lambda: busy[0]
    )
    assert stops == []


async def test_the_reattach_lock_is_what_in_progress_reports():
    assert browser_reattach.in_progress() is False
    async with browser_reattach._pass_lock:
        assert browser_reattach.in_progress() is True
    assert browser_reattach.in_progress() is False


async def test_the_health_route_does_not_count_as_adoption(manager):
    """The heartbeat polls /_stealth/health constantly; only /mcp is a client."""
    server = FastMCP("probe")
    session_hygiene.install(server)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.http_app()), base_url="http://test"
    ) as client:
        response = await client.get(session_hygiene.HEALTH_PATH)
    assert response.status_code in (200, 503)  # the route answered
    assert session_hygiene.active_manager().ever_served is False


def _run_main_block(monkeypatch, *flags):
    """Execute server.py as ``__main__`` (what ``python -m`` does) without serving."""
    monkeypatch.setattr(
        logging_setup, "bootstrap_backend_process_logging", lambda: None
    )
    monkeypatch.setattr(observability, "sentry_init", lambda: None)
    served = []
    monkeypatch.setattr(
        FastMCP, "run", lambda self, *args, **kwargs: served.append(kwargs)
    )
    monkeypatch.setattr(
        sys, "argv", ["server", "--transport", "http", "--port", "51999", *flags]
    )
    namespace = runpy.run_module(
        "stealth_chrome_devtools_mcp.embedded.server", run_name="__main__"
    )
    assert served, "the main block reached mcp.run"
    return namespace


@pytest.mark.filterwarnings("ignore:.*found in sys.modules:RuntimeWarning")
def test_the_main_block_stamps_the_flag_the_lifespan_reads(monkeypatch):
    """Without this stamp the flag parses and then does nothing: the lifespan reads a
    module global, and the other tests set that global directly."""
    assert _run_main_block(monkeypatch)["_EXIT_IF_UNADOPTED"] is False
    assert (
        _run_main_block(monkeypatch, "--exit-if-unadopted")["_EXIT_IF_UNADOPTED"]
        is True
    )
