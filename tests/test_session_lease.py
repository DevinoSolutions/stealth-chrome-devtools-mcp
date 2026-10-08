"""F-952 -- the advisory session lock.

Pinned: a refused acquire names the holder and the expiry; a lease expires on
its own; only the holder releases; the same owner renewing is not a conflict;
out-of-range numbers are refused rather than clamped; a bounded wait sees a
release. And the tool surface: three tools in one section, raising ``ToolError``
and never returning a ``success: False`` dict.
"""

import asyncio
import time

import pytest

from stealth_chrome_devtools_mcp.embedded import session_lease
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError


@pytest.fixture(autouse=True)
def _no_leases():
    session_lease.reset()
    yield
    session_lease.reset()


@pytest.fixture
def clock(monkeypatch):
    """A controllable monotonic and wall clock."""
    now = {"mono": 1000.0, "wall": 1_700_000_000.0}
    monkeypatch.setattr(session_lease, "_mono", lambda: now["mono"])
    monkeypatch.setattr(session_lease, "_wall", lambda: now["wall"])

    def advance(seconds: float) -> None:
        now["mono"] += seconds
        now["wall"] += seconds

    return advance


class TestTheLease:
    async def test_acquire_reports_holder_and_times(self, clock):
        got = await session_lease.acquire("fleet", "agent-a", 60, 0)

        assert got["acquired"] is True
        assert got["holder"] == "agent-a"
        assert got["expires_at"] == got["acquired_at"] + 60
        assert got["expires_in_seconds"] == 60

    async def test_a_second_owner_is_refused_with_holder_and_expiry(self, clock):
        await session_lease.acquire("fleet", "agent-a", 60, 0)
        clock(10)

        with pytest.raises(ToolError) as refused:
            await session_lease.acquire("fleet", "agent-b", 60, 0)

        text = str(refused.value)
        assert "agent-a" in text
        assert f"{1_700_000_060:.0f}" in text
        assert "50" in text

    async def test_the_lease_expires_by_itself(self, clock):
        await session_lease.acquire("fleet", "agent-a", 60, 0)
        clock(61)

        assert session_lease.status("fleet") == {"session": "fleet", "locked": False}
        got = await session_lease.acquire("fleet", "agent-b", 60, 0)
        assert got["holder"] == "agent-b"

    async def test_the_same_owner_renews(self, clock):
        await session_lease.acquire("fleet", "agent-a", 60, 0)
        clock(50)

        got = await session_lease.acquire("fleet", "agent-a", 60, 0)

        assert got["expires_in_seconds"] == 60

    async def test_status_shows_holder_acquired_and_expiry(self, clock):
        await session_lease.acquire("fleet", "agent-a", 120, 0)
        clock(20)

        status = session_lease.status("fleet")

        assert status["locked"] is True
        assert status["holder"] == "agent-a"
        assert status["acquired_at"] == 1_700_000_000.0
        assert status["expires_at"] == 1_700_000_120.0
        assert status["expires_in_seconds"] == 100

    async def test_sessions_are_locked_independently(self, clock):
        await session_lease.acquire("fleet", "agent-a", 60, 0)

        got = await session_lease.acquire("work", "agent-b", 60, 0)

        assert got["holder"] == "agent-b"

    async def test_only_the_holder_releases(self, clock):
        await session_lease.acquire("fleet", "agent-a", 60, 0)

        with pytest.raises(ToolError, match="agent-a"):
            session_lease.release("fleet", "agent-b")

        assert session_lease.status("fleet")["holder"] == "agent-a"
        released = session_lease.release("fleet", "agent-a")
        assert released == {"session": "fleet", "locked": False, "released": True}
        assert session_lease.status("fleet")["locked"] is False

    async def test_releasing_a_free_session_is_refused(self, clock):
        with pytest.raises(ToolError, match="not locked"):
            session_lease.release("fleet", "agent-a")

    async def test_releasing_an_expired_lease_is_refused(self, clock):
        await session_lease.acquire("fleet", "agent-a", 5, 0)
        clock(6)

        with pytest.raises(ToolError, match="not locked"):
            session_lease.release("fleet", "agent-a")

    @pytest.mark.parametrize("lease", [0, -1, 3601])
    async def test_a_lease_out_of_range_is_refused_not_clamped(self, lease):
        with pytest.raises(ToolError, match="lease_seconds"):
            await session_lease.acquire("fleet", "agent-a", lease, 0)

        assert session_lease.status("fleet")["locked"] is False

    @pytest.mark.parametrize("wait", [-1, 121])
    async def test_a_wait_out_of_range_is_refused(self, wait):
        with pytest.raises(ToolError, match="wait_seconds"):
            await session_lease.acquire("fleet", "agent-a", 60, wait)

    @pytest.mark.parametrize("owner", ["", "   "])
    async def test_an_empty_owner_is_refused(self, owner):
        with pytest.raises(ToolError, match="owner"):
            await session_lease.acquire("fleet", owner, 60, 0)

    async def test_a_bounded_wait_gets_the_lock_when_it_is_released(self):
        await session_lease.acquire("fleet", "agent-a", 60, 0)

        async def release_soon():
            await asyncio.sleep(0.3)
            session_lease.release("fleet", "agent-a")

        releaser = asyncio.create_task(release_soon())
        started = time.monotonic()
        got = await session_lease.acquire("fleet", "agent-b", 60, 5)
        await releaser

        assert got["holder"] == "agent-b"
        assert time.monotonic() - started < 3

    async def test_a_bounded_wait_gives_up_and_names_the_holder(self):
        await session_lease.acquire("fleet", "agent-a", 60, 0)
        started = time.monotonic()

        with pytest.raises(ToolError, match="agent-a"):
            await session_lease.acquire("fleet", "agent-b", 60, 1)

        assert 0.9 <= time.monotonic() - started < 3


class TestTheToolSurface:
    async def test_three_tools_in_the_session_lock_section(self, patched_server):
        from stealth_chrome_devtools_mcp.embedded.tool_registry import SECTION_TOOLS
        from stealth_chrome_devtools_mcp.embedded.tool_sections import session_lock

        patched_server()
        assert set(SECTION_TOOLS["session-lock"]) == {
            "acquire_session_lock",
            "release_session_lock",
            "get_session_lock_status",
        }
        assert session_lock.SECTION == "session-lock"

    async def test_the_round_trip(self, call_tool, patched_server):
        srv = patched_server()

        got = await call_tool(srv, "acquire_session_lock", owner="agent-a")
        assert got["holder"] == "agent-a"
        assert got["session"] == "fleet"
        status = await call_tool(srv, "get_session_lock_status")
        assert status["holder"] == "agent-a"
        with pytest.raises(ToolError, match="agent-a"):
            await call_tool(srv, "acquire_session_lock", owner="agent-b")
        with pytest.raises(ToolError, match="only the holder"):
            await call_tool(srv, "release_session_lock", owner="agent-b")
        released = await call_tool(srv, "release_session_lock", owner="agent-a")
        assert released["released"] is True
        assert (await call_tool(srv, "get_session_lock_status"))["locked"] is False

    async def test_the_session_is_a_name_not_a_path(self, call_tool, patched_server):
        srv = patched_server()

        with pytest.raises(ToolError, match="NAME"):
            await call_tool(srv, "get_session_lock_status", session="/tmp/fleet")
