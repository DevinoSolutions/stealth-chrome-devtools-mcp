"""F-967 — a clone spawn must not freeze the backend's event loop.

The incident (2026-10-14, backend 38760 on 2.1.32): about ten chats called
``spawn_browser()`` at once. ``clone_storage.resolve_profile_selection`` is
``async``, but its clone branch ran the whole ~100 MB / 666-file profile copy,
the seed freshen and the process-table walks SYNCHRONOUSLY on the event loop, so
every spawn froze the loop for the full copy and N of them froze it N times over.
Nothing was served in that time: the log went silent for ~106 s, every proxy's
probe timed out, ``mcp-session`` traffic stopped, and the proxies' watchdogs
condemned a backend that was working flat out.

The pins here are about the one property that closes it: **while profile
selection is blocked on disk or on the process table, the loop keeps turning.**
The seams are production code patched at the module attribute —
``profile_copy.copy_delta`` (the copy), ``clone_storage._profile_hold`` and
``_profile_has_running_browser`` (the walks) — so what is measured is the real
``resolve_profile_selection`` and not a model of it.

The instrument is a rendezvous, not a stopwatch (F-997). The stand-in for "the
disk is slow" blocks until the event loop has turned ``TURNS_REQUIRED`` times
while it waited. Off the loop that takes a few milliseconds; ON the loop the
turns cannot happen, so the stand-in gives up after ``RENDEZVOUS_GRACE`` and the
pin fails. The verdict is "did the loop turn while the work was blocked", which a
slow runner cannot change: the first version asserted ``max gap < 0.2 s`` and a
loaded macOS cell measured 0.28 s and 0.30 s on code that never blocked the loop.

Every wait is wrapped in ``asyncio.wait_for`` with a named budget: a regression
here must FAIL, never hang the suite (no global pytest timeout exists).

Hermetic: ``tmp_session_root``'s directories only; the process table is never
walked for real.
"""

import asyncio
import contextlib
import importlib
import threading
import time
from pathlib import Path

import pytest

from stealth_chrome_devtools_mcp.embedded import (
    clone_storage,
    logging_setup,
    profile_copy,
    profile_seed,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

#: How long the stand-in for "copy a 100 MB profile" blocks, per pass. The real
#: copy is two passes, so one copy costs 2x this plus ``RETRY_SETTLE_SECONDS``.
SLOW = 0.25

#: How many loop turns the stand-in for slow work waits to see before it lets go.
TURNS_REQUIRED = 3

#: How long the stand-in waits for those turns before it concludes the loop is
#: blocked by IT. A loop that is merely busy turns long before this; one that
#: cannot turn at all is the defect. It is paid once per failing test.
RENDEZVOUS_GRACE = 10.0

#: Hard stop for any await in this module: a hang is a failure, not a timeout.
WAIT_BUDGET = 60.0


class LoopWitness:
    """Whether the event loop kept turning while slow work was blocked.

    ``block()`` is the stand-in for slow disk or a slow process-table walk: it
    holds its caller until the loop has turned ``TURNS_REQUIRED`` more times. Run
    on a worker thread it returns after a few ms; run on the loop it cannot see a
    single turn, times out, and is counted in ``starved``. After the first
    timeout it returns at once, so a regression costs ``RENDEZVOUS_GRACE`` once.
    """

    def __init__(self) -> None:
        self.blocks = 0
        self.starved = 0
        self._turns = 0
        self._turned = threading.Condition()
        self._stop = False
        self._task: asyncio.Task | None = None

    def block(self) -> None:
        with self._turned:
            self.blocks += 1
            if self.starved:
                return
            wanted = self._turns + TURNS_REQUIRED
            if not self._turned.wait_for(
                lambda: self._turns >= wanted, timeout=RENDEZVOUS_GRACE
            ):
                self.starved += 1

    async def _run(self) -> None:
        while not self._stop:
            await asyncio.sleep(0.005)
            with self._turned:
                self._turns += 1
                self._turned.notify_all()

    async def __aenter__(self) -> "LoopWitness":
        self._task = asyncio.ensure_future(self._run())
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._stop = True
        assert self._task is not None
        await self._task

    def assert_loop_stayed_free(self, what: str) -> None:
        assert self.blocks, (
            f"the slow stand-in never ran: nothing was measured ({what})"
        )
        assert not self.starved, (
            f"the event loop could not turn while {what} was blocked (waited "
            f"{RENDEZVOUS_GRACE:.0f}s for {TURNS_REQUIRED} turns) — it ran on the "
            "loop (F-967)"
        )


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No real process table, and a clean reservation set per test."""
    # `client_roots.session_seed` imports fastmcp at its first call, which on a
    # cold interpreter (OneDrive, antivirus) takes seconds and would be measured
    # as a loop stall in whichever test runs first. The backend has it loaded.
    importlib.import_module("fastmcp.server.dependencies")
    clone_storage._clear_protected_clone_dirs()
    monkeypatch.setattr(clone_storage, "_profile_hold", lambda directory: None)
    monkeypatch.setattr(
        clone_storage, "_profile_has_running_browser", lambda directory: False
    )
    yield
    clone_storage._clear_protected_clone_dirs()


class SlowCopy:
    """``profile_copy.copy_delta`` made slow, and a witness to who ran it.

    Slow is ``delay`` seconds by default; with a ``witness`` it is "until the loop
    has turned" instead (:class:`LoopWitness`), which is what the loop pins use.
    """

    def __init__(
        self, monkeypatch, *, delay: float = SLOW, witness: LoopWitness | None = None
    ) -> None:
        self.delay = delay
        self.witness = witness
        self.started = threading.Event()
        self.finished = threading.Event()
        self.threads: set[int] = set()
        self.protected_at_start: list[bool] = []
        self.correlation: list[str | None] = []
        self._lock = threading.Lock()
        self._running = 0
        self.peak = 0
        self._real = profile_copy.copy_delta
        monkeypatch.setattr(profile_copy, "copy_delta", self)

    def __call__(self, source: Path, target: Path) -> None:
        # ``target`` is the staging copy, built beside the clone it becomes.
        clone = target.with_name(target.name.split(profile_copy.STAGING_SUFFIX)[0])
        with self._lock:
            self._running += 1
            self.peak = max(self.peak, self._running)
            self.threads.add(threading.get_ident())
            self.protected_at_start.append(clone_storage._clone_dir_is_protected(clone))
            self.correlation.append(logging_setup.correlation_id_var.get())
        self.started.set()
        try:
            # Blocking, exactly like the disk is.
            if self.witness is not None:
                self.witness.block()
            else:
                time.sleep(self.delay)
            self._real(source, target)
        finally:
            with self._lock:
                self._running -= 1
            self.finished.set()


async def _resolve_many(count: int) -> list[dict]:
    return await asyncio.wait_for(
        asyncio.gather(
            *(clone_storage.resolve_profile_selection(None) for _ in range(count))
        ),
        WAIT_BUDGET,
    )


async def test_concurrent_clone_spawns_keep_the_loop_turning(
    tmp_session_root, monkeypatch
):
    """THE defect: four concurrent unnamed spawns, each copying slowly. On the
    loop that is four freezes; off it, the loop never notices."""
    async with LoopWitness() as witness:
        copy = SlowCopy(monkeypatch, witness=witness)
        selections = await _resolve_many(4)

    witness.assert_loop_stayed_free("four clones were copied")
    assert witness.blocks >= 4, "every clone's copy should have been measured"
    assert threading.get_ident() not in copy.threads, "the copy ran on the loop"
    # What the offload must not change: four distinct, reserved, marked clones.
    dirs = {s["user_data_dir"] for s in selections}
    assert len(dirs) == 4, f"concurrent spawns collided: {dirs}"
    for selection in selections:
        assert selection["profile_role"] == "clone"
        assert clone_storage._clone_dir_is_protected(selection["user_data_dir"])
        assert (Path(selection["user_data_dir"]) / profile_seed.MARKER_NAME).is_file()


async def test_a_clone_is_reserved_before_its_copy_begins(
    tmp_session_root, monkeypatch
):
    """`_protect_clone_dir` BEFORE the copy: the marker the copy publishes makes
    the clone a sweep target while its browser has not launched yet."""
    copy = SlowCopy(monkeypatch)
    await _resolve_many(3)

    assert copy.protected_at_start, "the copy never ran"
    assert all(copy.protected_at_start), (
        "a copy began on a directory nothing had reserved: the storage sweep "
        f"could reclaim it from under the spawn {copy.protected_at_start}"
    )


async def test_the_offloaded_copy_keeps_the_calls_correlation_id(
    tmp_session_root, monkeypatch
):
    """``to_thread`` copies the context, which is what keeps a log line written
    from the copy joined to its tool call (and what keeps `directory_gate`'s
    per-context holds visible, read-only, to the work its holder started)."""
    copy = SlowCopy(monkeypatch, delay=0.0)
    token = logging_setup.correlation_id_var.set("f967-correlation")
    try:
        await asyncio.wait_for(
            clone_storage.resolve_profile_selection(None), WAIT_BUDGET
        )
    finally:
        logging_setup.correlation_id_var.reset(token)

    assert set(copy.correlation) == {"f967-correlation"}


async def test_a_slow_process_table_walk_does_not_freeze_the_loop(
    tmp_session_root, monkeypatch
):
    """The walks are the other cost: every unnamed spawn asks who holds master,
    and with 80-200 chrome.exe that is a `cmdline()` read per process."""

    async with LoopWitness() as witness:

        def slow_walk(directory):
            witness.block()

        monkeypatch.setattr(clone_storage, "_profile_hold", slow_walk)
        monkeypatch.setattr(
            clone_storage,
            "_profile_has_running_browser",
            lambda directory: bool(slow_walk(directory)),
        )
        await _resolve_many(3)

    witness.assert_loop_stayed_free("a process-table walk")


async def test_a_named_session_copy_keeps_the_loop_turning(
    tmp_session_root, monkeypatch
):
    """The explicit branch copies too: a NEW named session is created from the
    seed, on the loop until F-967."""
    landed = clone_storage.require_allowed_user_data_dir("f967-named")
    async with LoopWitness() as witness:
        SlowCopy(monkeypatch, witness=witness)
        selection = await asyncio.wait_for(
            clone_storage.resolve_profile_selection(landed), WAIT_BUDGET
        )

    assert selection["profile_role"] == "explicit"
    assert (Path(selection["user_data_dir"]) / profile_seed.MARKER_NAME).is_file()
    witness.assert_loop_stayed_free("a named session was copied")


async def test_the_shared_session_open_keeps_the_loop_turning(
    tmp_session_root, monkeypatch
):
    """``session="default"`` freshens the seed from master before it opens it
    (`before-default-open`), a whole-profile copy, and asks who holds master."""
    landed = clone_storage.require_allowed_user_data_dir(None, "default")
    async with LoopWitness() as witness:
        SlowCopy(monkeypatch, witness=witness)
        selection = await asyncio.wait_for(
            clone_storage.resolve_profile_selection(landed), WAIT_BUDGET
        )

    assert selection["profile_role"] == profile_seed.DEFAULT_SESSION
    assert selection["seed_refreshed"] is True, "the freshen did not run"
    witness.assert_loop_stayed_free("the seed was freshened")


async def test_a_retry_selection_keeps_the_loop_turning(tmp_session_root, monkeypatch):
    """F-834's retry asks who holds the directory the failed attempt drove, and a
    clone-role retry re-clones: same cost, same rule, both doors."""
    shared = {
        "user_data_dir": str(clone_storage.master_profile_dir()),
        "profile_role": profile_seed.DEFAULT_SESSION,
    }
    clone = {
        "user_data_dir": str(tmp_session_root["sessions"] / "failed-attempt"),
        "profile_role": "clone",
    }
    async with LoopWitness() as witness:
        SlowCopy(monkeypatch, witness=witness)
        monkeypatch.setattr(
            clone_storage, "_profile_hold", lambda directory: witness.block()
        )
        retried_shared = await asyncio.wait_for(
            clone_storage._fallback_profile_selection(shared, 0), WAIT_BUDGET
        )
        retried_clone = await asyncio.wait_for(
            clone_storage._fallback_profile_selection(clone, 0), WAIT_BUDGET
        )

    assert retried_shared is not None and retried_clone is not None
    assert retried_clone["profile_role"] == "clone"
    witness.assert_loop_stayed_free("a retry selection")
    assert witness.blocks >= 2, "both the hold walk and the re-clone's copy count"


async def test_copies_are_parallel_but_bounded(tmp_session_root, monkeypatch):
    """Off the loop does not mean unbounded: ten spawns must not put ten 100 MB
    copies on one disk, or take ten of the default executor's threads from
    `close_instance`'s teardown. The bound is `MAX_CONCURRENT_COPIES`."""
    from stealth_chrome_devtools_mcp.embedded import profile_offload

    copy = SlowCopy(monkeypatch)
    await _resolve_many(8)

    assert 2 <= copy.peak <= profile_offload.MAX_CONCURRENT_COPIES, (
        f"{copy.peak} copies ran at once (bound {profile_offload.MAX_CONCURRENT_COPIES})"
    )


# ---------------------------------------------------------------------------
# Cancellation and failure: who owns what the thread produced
# ---------------------------------------------------------------------------


def _clone_dirs(root: Path) -> list[Path]:
    return [d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")]


async def test_a_cancelled_spawn_leaves_no_clone_and_no_reservation(
    tmp_session_root, monkeypatch
):
    """A thread cannot be interrupted. A spawn cancelled mid-copy must therefore
    WAIT the copy out and then discard what it built and release the reservation
    — otherwise the thread publishes a complete, marked clone that no spawn owns
    and that `_protect_clone_dir` shields from the sweep for the life of the
    process."""
    copy = SlowCopy(monkeypatch, delay=0.3)
    task = asyncio.ensure_future(clone_storage.resolve_profile_selection(None))
    await asyncio.wait_for(
        asyncio.to_thread(copy.started.wait, WAIT_BUDGET), WAIT_BUDGET
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, WAIT_BUDGET)

    assert copy.finished.is_set(), "the cancelled spawn returned with its copy running"
    assert not clone_storage._PROTECTED_CLONE_DIRS, "a reservation outlived its spawn"
    sessions = tmp_session_root["sessions"]
    assert _clone_dirs(sessions) == [], f"an orphaned clone: {_clone_dirs(sessions)}"
    assert not [p for p in sessions.iterdir() if profile_copy.is_scratch(p.name)]


async def test_a_cancelled_named_spawn_removes_the_session_it_created(
    tmp_session_root, monkeypatch
):
    copy = SlowCopy(monkeypatch, delay=0.3)
    landed = clone_storage.require_allowed_user_data_dir("f967-cancelled")
    task = asyncio.ensure_future(clone_storage.resolve_profile_selection(landed))
    await asyncio.wait_for(
        asyncio.to_thread(copy.started.wait, WAIT_BUDGET), WAIT_BUDGET
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, WAIT_BUDGET)

    sessions = tmp_session_root["sessions"]
    assert _clone_dirs(sessions) == [], "a half-created named session survived"


async def test_a_refusal_in_the_worker_surfaces_unchanged_and_releases_the_clone(
    tmp_session_root, monkeypatch
):
    """The exception crosses the thread boundary as the SAME exception, and the
    directory reserved for the refused spawn is not left reserved."""

    def refuse(override, driven):
        raise ToolError("f967: refused")

    monkeypatch.setattr(clone_storage, "_clone_seed", refuse)
    with pytest.raises(ToolError, match="f967: refused"):
        await asyncio.wait_for(
            clone_storage.resolve_profile_selection(None), WAIT_BUDGET
        )

    assert not clone_storage._PROTECTED_CLONE_DIRS, "a refusal reserved a directory"


async def test_a_spawn_cancelled_while_queued_for_a_slot_releases_its_reservation(
    tmp_session_root, monkeypatch
):
    """Cancel a spawn that is waiting for a copy slot, not running: nothing was
    copied, and the reservation taken for it must still go."""
    from stealth_chrome_devtools_mcp.embedded import profile_offload

    monkeypatch.setattr(profile_offload, "MAX_CONCURRENT_COPIES", 1)
    copy = SlowCopy(monkeypatch, delay=0.4)
    first = asyncio.ensure_future(clone_storage.resolve_profile_selection(None))
    await asyncio.wait_for(
        asyncio.to_thread(copy.started.wait, WAIT_BUDGET), WAIT_BUDGET
    )
    queued = asyncio.ensure_future(clone_storage.resolve_profile_selection(None))
    await asyncio.sleep(0.05)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(queued, WAIT_BUDGET)
    selection = await asyncio.wait_for(first, WAIT_BUDGET)

    owned = {clone_storage._normalize_clone_path(selection["user_data_dir"])}
    assert owned == clone_storage._PROTECTED_CLONE_DIRS, (
        "only the surviving spawn's clone may stay reserved: "
        f"{clone_storage._PROTECTED_CLONE_DIRS}"
    )
    with contextlib.suppress(Exception):
        clone_storage._release_clone_dir(selection["user_data_dir"])
