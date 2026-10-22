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

import anyio
import pytest

from stealth_chrome_devtools_mcp.embedded import (
    server as embedded_server,
)
from stealth_chrome_devtools_mcp.embedded import session_hygiene, singleton


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
