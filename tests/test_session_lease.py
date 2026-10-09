"""F-952 -- the advisory session lock.

Pinned: a refused acquire names the holder and the expiry; a lease expires on
its own; only the holder releases; the same owner renewing is not a conflict;
out-of-range numbers are refused rather than clamped; a bounded wait sees a
release. F-956: waiters are served in the order they asked, and status shows
the line. And the tool surface: three tools in one section, raising ``ToolError``
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

        assert session_lease.status("fleet") == {
            "session": "fleet",
            "locked": False,
            "queue_length": 0,
            "waiting": [],
        }
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


#: Every wait in the queue tests is bounded; a hang pin that can hang is no pin.
STEP = 5.0


async def _until(predicate, what: str) -> None:
    async def poll():
        while not predicate():
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(poll(), STEP)
    except TimeoutError:
        raise AssertionError(f"never happened within {STEP}s: {what}") from None


def _line(session: str = "fleet") -> list[str]:
    return [w["owner"] for w in session_lease.status(session).get("waiting", [])]


async def _enqueue(order: list[str], owner: str, wait: int = 30, session="fleet"):
    """Start *owner* waiting and let it get into the line. The 0.1 s stagger is
    deliberate: it puts each waiter on a different phase of the old 0.25 s poll,
    so a hand-off that is not first-come-first-served cannot pass by luck."""

    async def go():
        got = await session_lease.acquire(session, owner, 60, wait)
        order.append(owner)
        return got

    task = asyncio.create_task(go())
    await asyncio.sleep(0.1)
    return task


class TestTheLineIsFirstComeFirstServed:
    async def test_waiters_get_the_lock_in_the_order_they_asked(self):
        await session_lease.acquire("fleet", "x", 60, 0)
        order: list[str] = []
        tasks = [await _enqueue(order, name) for name in ("a", "b", "c")]

        for done, holder in enumerate(("x", "a", "b"), start=1):
            session_lease.release("fleet", holder)
            await _until(lambda n=done: len(order) >= n, f"hand-off after {holder}")
        await asyncio.wait_for(asyncio.gather(*tasks), STEP)

        assert order == ["a", "b", "c"]

    async def test_a_newcomer_with_no_wait_does_not_jump_the_line(self):
        await session_lease.acquire("fleet", "x", 60, 0)
        order: list[str] = []
        first = await _enqueue(order, "a")
        session_lease.release("fleet", "x")  # free now, but "a" has not run yet

        with pytest.raises(ToolError, match="free but 1 other"):
            await session_lease.acquire("fleet", "jumper", 60, 0)

        await asyncio.wait_for(first, STEP)
        assert order == ["a"]

    async def test_a_refused_newcomer_is_told_the_holder_and_how_many_are_ahead(self):
        await session_lease.acquire("fleet", "x", 60, 0)
        order: list[str] = []
        await _enqueue(order, "a")
        await _enqueue(order, "b")

        with pytest.raises(ToolError) as refused:
            await session_lease.acquire("fleet", "c", 60, 0)

        assert "'x'" in str(refused.value)
        assert "position 3" in str(refused.value)
        assert "2 waiting ahead" in str(refused.value)

    async def test_an_expired_lease_goes_to_the_head_not_a_later_poller(self):
        await session_lease.acquire("fleet", "x", 1, 0)  # nobody will release it
        order: list[str] = []
        first = await _enqueue(order, "a")
        second = await _enqueue(order, "b")

        got = await asyncio.wait_for(first, STEP)

        assert got["holder"] == "a"
        assert order == ["a"]
        assert not second.done()
        session_lease.release("fleet", "a")
        await asyncio.wait_for(second, STEP)
        assert order == ["a", "b"]

    async def test_a_lease_that_just_expired_is_not_up_for_grabs_to_a_newcomer(
        self, clock
    ):
        await session_lease.acquire("fleet", "x", 60, 0)
        order: list[str] = []
        await _enqueue(order, "a")
        clock(61)  # expired, and the head has not been scheduled yet

        with pytest.raises(ToolError, match="free but 1 other"):
            await session_lease.acquire("fleet", "jumper", 60, 0)

        assert _line() == ["a"]

    async def test_a_waiter_that_times_out_leaves_and_the_next_gets_the_lock(self):
        await session_lease.acquire("fleet", "x", 60, 0)
        order: list[str] = []
        quitter = await _enqueue(order, "a", wait=1)
        patient = await _enqueue(order, "b", wait=30)

        with pytest.raises(ToolError, match="position 1"):
            await asyncio.wait_for(quitter, STEP)

        assert _line() == ["b"]
        session_lease.release("fleet", "x")
        got = await asyncio.wait_for(patient, STEP)
        assert got["holder"] == "b"

    async def test_the_next_waiter_re_arms_on_the_new_holders_expiry(self):
        """The head takes the lock and never releases: the waiter behind it must
        get the lock at that lease's expiry, not at its own (much later) deadline."""
        await session_lease.acquire("fleet", "x", 1, 0)
        taken: dict[str, float] = {}

        async def wait_for_lock(owner: str):
            got = await session_lease.acquire("fleet", owner, 1, 10)
            taken[owner] = time.monotonic()
            return got

        first = asyncio.create_task(wait_for_lock("a"))
        await asyncio.sleep(0.1)
        second = asyncio.create_task(wait_for_lock("b"))
        await asyncio.sleep(0.1)
        session_lease.release("fleet", "x")  # "a" takes a 1 s lease, never releases

        await asyncio.wait_for(first, STEP)
        await asyncio.wait_for(second, STEP)

        lag = taken["b"] - taken["a"]
        assert lag < 3, f"b got the lock {lag:.1f}s after a took it (a's lease is 1s)"

    async def test_a_cancelled_waiter_leaves_the_line(self):
        await session_lease.acquire("fleet", "x", 60, 0)
        order: list[str] = []
        gone = await _enqueue(order, "a")
        stays = await _enqueue(order, "b")

        gone.cancel()
        await asyncio.gather(gone, return_exceptions=True)

        assert _line() == ["b"]
        session_lease.release("fleet", "x")
        got = await asyncio.wait_for(stays, STEP)
        assert got["holder"] == "b"

    async def test_a_cancelled_head_does_not_strand_a_free_lock(self):
        await session_lease.acquire("fleet", "x", 60, 0)
        order: list[str] = []
        head = await _enqueue(order, "a")
        second = await _enqueue(order, "b")
        session_lease.release("fleet", "x")
        head.cancel()  # woken, but cancelled before it takes the lock
        await asyncio.gather(head, return_exceptions=True)

        got = await asyncio.wait_for(second, STEP)

        assert got["holder"] == "b"

    async def test_the_same_owner_cannot_queue_twice(self):
        await session_lease.acquire("fleet", "x", 60, 0)
        order: list[str] = []
        await _enqueue(order, "a")
        await _enqueue(order, "b")

        with pytest.raises(ToolError, match=r"already waiting.*position 2"):
            await session_lease.acquire("fleet", "b", 60, 5)
        assert _line() == ["a", "b"]

    async def test_the_holder_renews_at_once_even_with_a_queue(self):
        await session_lease.acquire("fleet", "x", 60, 0)
        order: list[str] = []
        await _enqueue(order, "a")

        got = await asyncio.wait_for(session_lease.acquire("fleet", "x", 120, 0), STEP)

        assert got["holder"] == "x"
        assert got["expires_in_seconds"] == 120
        assert _line() == ["a"]

    async def test_status_reports_positions_and_your_position(self):
        await session_lease.acquire("fleet", "x", 60, 0)
        order: list[str] = []
        await _enqueue(order, "a")
        await _enqueue(order, "b")

        status = session_lease.status("fleet")

        assert status["queue_length"] == 2
        assert [(w["owner"], w["position"]) for w in status["waiting"]] == [
            ("a", 1),
            ("b", 2),
        ]
        assert all(w["waiting_seconds"] >= 0 for w in status["waiting"])
        assert "your_position" not in status
        assert session_lease.status("fleet", "x")["your_position"] == 0
        assert session_lease.status("fleet", "a")["your_position"] == 1
        assert session_lease.status("fleet", "b")["your_position"] == 2
        assert session_lease.status("fleet", "nobody")["your_position"] is None

    async def test_reset_leaves_no_waiter_stuck(self):
        await session_lease.acquire("fleet", "x", 60, 0)
        order: list[str] = []
        tasks = [await _enqueue(order, name) for name in ("a", "b")]

        session_lease.reset()

        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), STEP
        )
        assert all(isinstance(r, ToolError) for r in results)
        assert session_lease.status("fleet")["queue_length"] == 0


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

    async def test_the_status_tool_takes_an_owner_and_shows_the_line(
        self, call_tool, patched_server
    ):
        srv = patched_server()
        await call_tool(srv, "acquire_session_lock", owner="agent-a")
        waiter = asyncio.create_task(
            call_tool(srv, "acquire_session_lock", owner="agent-b", wait_seconds=30)
        )
        await _until(lambda: len(_line()) == 1, "agent-b in line")

        status = await call_tool(srv, "get_session_lock_status", owner="agent-b")

        assert status["your_position"] == 1
        assert status["waiting"][0]["owner"] == "agent-b"
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)

    async def test_the_session_is_a_name_not_a_path(self, call_tool, patched_server):
        srv = patched_server()

        with pytest.raises(ToolError, match="NAME"):
            await call_tool(srv, "get_session_lock_status", session="/tmp/fleet")


class TestOneKeyPerDirectory:
    """The lease is keyed by the DIRECTORY a session means, not its spelling."""

    async def test_the_default_session_is_one_key_in_the_tools_and_the_marker(
        self, call_tool, patched_server, tmp_session_root
    ):
        from stealth_chrome_devtools_mcp.embedded import fleet_session

        srv = patched_server()
        await call_tool(srv, "acquire_session_lock", owner="agent-a", session="default")

        marker = fleet_session.reuse_answer(True, str(tmp_session_root["master"]))

        assert marker["session_lock"]["session"] == "default"
        assert marker["session_lock"]["locked"] is True
        assert marker["session_lock"]["holder"] == "agent-a"

    async def test_two_spellings_of_one_name_are_one_lock(
        self, call_tool, patched_server, tmp_session_root
    ):
        srv = patched_server()
        await call_tool(srv, "acquire_session_lock", owner="agent-a", session="fleet")

        status = await call_tool(srv, "get_session_lock_status", session="Fleet")

        assert status["locked"] is True
        assert status["holder"] == "agent-a"
        with pytest.raises(ToolError, match="agent-a"):
            await call_tool(srv, "acquire_session_lock", owner="b", session="FLEET")

    async def test_a_directory_that_merely_ends_in_the_name_is_not_the_session(
        self, call_tool, patched_server, tmp_session_root, tmp_path
    ):
        from stealth_chrome_devtools_mcp.embedded import fleet_session

        srv = patched_server()
        await call_tool(srv, "acquire_session_lock", owner="agent-a", session="fleet")

        elsewhere = fleet_session.reuse_answer(True, str(tmp_path / "other" / "fleet"))

        assert elsewhere["session_lock"]["locked"] is False
        assert elsewhere["session_lock"]["session"] != "fleet"


class TestTheBounds:
    async def test_an_owner_label_has_a_length_cap(self):
        with pytest.raises(ToolError, match="128"):
            await session_lease.acquire("fleet", "x" * 129, 60, 0)

    async def test_a_session_key_has_a_length_cap(self):
        with pytest.raises(ToolError, match="at most"):
            await session_lease.acquire("k" * 256, "agent-a", 60, 0)

    async def test_an_acquire_sweeps_leases_nobody_asks_about_again(self, clock):
        await session_lease.acquire("old-session", "agent-a", 5, 0)
        clock(10)

        await session_lease.acquire("fleet", "agent-b", 60, 0)

        assert "old-session" not in session_lease._leases

    async def test_a_wait_longer_than_a_client_would_stay_is_refused(self):
        with pytest.raises(ToolError, match="0-60"):
            await session_lease.acquire("fleet", "agent-a", 60, 61)
