"""Real-Chrome proof for F-888: a login survives the backend that opened it.

The hermetic pins in ``test_browser_reattach.py`` prove the RULE — who may be
adopted, which witness names the port, what the reaper spares. They cannot prove
the thing the incident was actually about, because every one of them patches the
CDP door: that a second `uc.start` against a port the first backend's Chrome is
listening on **connects to that same renderer** instead of launching a new one,
and that the page state a human built is still there afterwards.

So these nodes use no double at all. Each spawns a real Chrome on a real named
profile, writes a value into the live page, then simulates the backend dying the
only way that is honest here — dropping the manager's handle on the browser
WITHOUT killing the process, exactly as a `TerminateProcess`d backend leaves it.
Every assertion about the page is a claim about the RENDERER and not about the
profile directory: a re-spawn onto the same `user_data_dir` would pass a cookie
check and fail all three.

The three are the three shapes this finding has:

1. **The record path.** A recorded browser, adopted by a fresh manager at startup
   under its ORIGINAL instance id.
2. **The holder path with an EMPTY record** — the real stranded Chrome's shape,
   where the port must come off the live process's own command line and a plain
   ``spawn_browser(user_data_dir=…)`` has to reach it.
3. **Two managers coexisting**, the second CONSTRUCTED BEFORE the first drops its
   handle, which is what F-886 made the ordinary case: no restart happens at all,
   and the take-over still has to work — including the cross-process claim, read
   back out of the record, and its refusal of a second take-over.

The owner in node 1's record is a pid that is not a backend of ours, which is what
the adoption rule needs to see. It is this process's own pid answered through a
patched witness rather than a fabricated dead pid, because a fabricated one can
be RECYCLED onto a live process between the write and the read, and the flake
that produces would look exactly like a real adoption refusal.
"""

import asyncio
import contextlib
import json
import os
from unittest.mock import patch

import psutil
import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    instance_entry,
    integration_pytestmark,
    navigate_and_settle,
    sandbox_kwargs,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded import (
    browser_cmdline,
    browser_pid_registry,
    browser_reattach,
    tool_runtime,
)
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup

pytestmark = integration_pytestmark()

# Long enough for a cold `uc.start` against a live port on a loaded machine, and
# short enough that a genuine refusal is reported rather than waited out. The
# product's own bound is ATTACH_BUDGET_SECONDS (15 s); this is the test's outer
# guard so a hang names itself instead of hitting the suite timeout.
_ADOPT_DEADLINE = 45.0


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


def _cleanup_on(pid_file) -> ProcessCleanup:
    """A ProcessCleanup whose record is the test's file, built without __init__.

    The same shape the hermetic pins use, and for the same reason: the adoption
    pass WRITES (it re-stamps ownership), and nothing here may reach the
    developer's live ``~/.stealth-mcp/browser_pids.json``.
    """
    cleanup = ProcessCleanup.__new__(ProcessCleanup)
    cleanup.pid_file = pid_file
    cleanup.tracked_pids = set()
    cleanup.browser_processes = {}
    cleanup.orphan_profile_max_age_seconds = 0
    cleanup._init_time = 0.0
    return cleanup


def _browsers_on(profile) -> bool:
    """Is any Chromium process still running on *profile*?"""
    target = str(profile).lower()
    for proc in psutil.process_iter(["name", "cmdline"]):
        with contextlib.suppress(Exception):
            if "chrome" not in (proc.info["name"] or "").lower():
                continue
            if any(target in (arg or "").lower() for arg in proc.info["cmdline"] or ()):
                return True
    return False


async def _released(profile, *, budget: float = 30.0) -> None:
    """Wait for a torn-down node's Chrome to actually EXIT before the next runs.

    ``close_instance`` offloads its teardown, so it returns while the process
    tree is still dying — and each node here deliberately leaves a browser
    RUNNING mid-test, so without this barrier three real Chromes launch over the
    top of three dying ones. That is how this file failed as a FILE on a loaded
    machine while every node passed alone: nodriver's connect deadline is a fixed
    ≈2.75 s and it loses that race, which surfaces as "Failed to connect to
    browser" and a retry onto a DIFFERENT directory — i.e. as an adoption that
    was never attempted. Bounded, and deliberately silent on expiry: a browser
    that outlives the budget is the next node's capacity problem to report, not
    a failure of the node that just passed.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while loop.time() < deadline and _browsers_on(profile):
        await asyncio.sleep(0.2)


async def test_a_live_page_survives_its_backend_and_is_re_attached(
    fixture_app_server, tmp_empty_root, tmp_path
):
    """The incident, end to end, with the ending F-888 gives it."""
    manager = tool_runtime.browser_manager
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")

    profile = tmp_path / "seller-central"
    iid = (await spawn(headless=True, user_data_dir=str(profile), **sandbox_kwargs()))[
        "instance_id"
    ]

    adopted_here = False
    try:
        await navigate_and_settle(iid, f"{fixture_app_server}/interactions.html")
        # In-page state, not on-disk state: this is the half a re-spawn loses.
        await eval_js(iid, "window.__f888 = 'logged-in'")
        assert await eval_js(iid, "window.__f888") == "logged-in"

        entry = manager._instances[iid]
        browser = entry["browser"]
        port = browser.config.port
        chrome_pid = browser._process_pid
        assert isinstance(port, int) and port > 0, (
            "nodriver must expose the port it assigned; without it there is "
            "nothing for a later backend to record"
        )

        # The backend dies. `TerminateProcess` runs no handler, so Chrome is
        # left running and the record is the only thing that still names it.
        record = tmp_path / "browser_pids.json"
        record.write_text(
            json.dumps(
                {
                    "browser_processes": {
                        iid: {
                            "pid": chrome_pid,
                            # The REAL start time, read here rather than left
                            # None: adoption requires both halves of a pid's
                            # identity (F-888 review M4), and a node built on the
                            # weakest identity path would be evidence for a rule
                            # the shipped one does not have.
                            "create_time": psutil.Process(chrome_pid).create_time(),
                            "user_data_dir": str(profile),
                            "uses_custom_data_dir": True,
                            "auto_clone": False,
                            "cdp_port": port,
                            "timestamp": 0,
                            "owner_pid": os.getpid(),
                            "owner_create_time": None,
                        }
                    },
                    "timestamp": 0,
                }
            )
        )
        # Forget the browser WITHOUT closing it — the manager's handle is gone,
        # the process is not. Everything after this point reaches that Chrome
        # only through the record.
        manager._instances.pop(iid)
        manager._spawn_diagnostics.pop(iid, None)

        cleanup = _cleanup_on(record)
        with patch.object(
            browser_reattach.browser_pid_registry,
            "is_reapable",
            return_value=True,
        ):
            adopted = await asyncio.wait_for(
                browser_reattach.run(manager, cleanup), timeout=_ADOPT_DEADLINE
            )

        assert adopted == [iid], (
            f"expected the recorded instance to be adopted, got {adopted!r}"
        )
        adopted_here = True
        assert manager._spawn_diagnostics[iid]["reattached"] is True
        assert manager._spawn_diagnostics[iid]["cdp_port"] == port

        # The claim: the SAME renderer, not a fresh Chrome on the same profile.
        assert await eval_js(iid, "window.__f888") == "logged-in"
        # And it is a first-class instance afterwards, addressable by the id the
        # client held before the restart.
        row = instance_entry(await get_fn("list_instances")(), iid)
        assert row["partial"] is False, row.get("detail_error")
        # The LIVE url (F-874), not the cached `last_navigated_*` pair — which
        # for an adopted instance was never written by anything.
        assert row["current_url"].endswith("/interactions.html")

        # The record now names US as the owner, which is what makes the pass
        # idempotent: a second run finds nothing to adopt.
        again = await asyncio.wait_for(
            browser_reattach.run(manager, cleanup), timeout=_ADOPT_DEADLINE
        )
        assert again == []
    finally:
        with contextlib.suppress(Exception):
            await close(instance_id=iid)
        if not adopted_here:
            # The adoption never happened, so nothing owns that Chrome and
            # `close_instance` could not have reached it. Kill it by pid rather
            # than leaving a real browser behind on the runner.
            with contextlib.suppress(Exception):
                psutil.Process(chrome_pid).kill()
        await _released(profile)

    # The profile directory outlives the browser, which is guarantee (a).
    assert profile.exists()


async def test_spawn_re_attaches_to_a_holder_with_no_record_entry(
    fixture_app_server, tmp_empty_root, tmp_path
):
    """The REAL stranded login's shape, reproduced with real Chrome.

    Measured on the machine: the Seller Central Chrome (pid 115652, port 9223)
    is alive, its owner backend is gone, and it has **no entry in
    browser_pids.json at all** — the successor backend rewrote the record
    without it. ``browser_reattach.run`` walks entries, so it would walk past
    this browser forever. The only thing that still names it is the directory,
    which is what a caller passes to ``spawn_browser(user_data_dir=…)``.

    So the record is emptied here rather than seeded, and the port is recovered
    from the holder's real command line through the real ladder. What this
    proves is the whole point: asking to spawn onto that profile REACHES the
    running browser instead of walking to a sibling directory and opening a
    logged-out one.
    """
    manager = tool_runtime.browser_manager
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")

    profile = tmp_path / "seller-central"
    first = (
        await spawn(headless=True, user_data_dir=str(profile), **sandbox_kwargs())
    )["instance_id"]

    second = None
    chrome_pid = None
    try:
        await navigate_and_settle(first, f"{fixture_app_server}/interactions.html")
        await eval_js(first, "window.__f888_held = 'seller-central'")
        chrome_pid = manager._instances[first]["browser"]._process_pid

        # The backend died and the successor pruned the record. Drop the handle,
        # leave Chrome running, and let the tool see an EMPTY record.
        manager._instances.pop(first)
        manager._spawn_diagnostics.pop(first, None)

        # The record is redirected to an ABSENT tmp file rather than stubbed
        # empty at one read: the claim taken before the door reads AND WRITES it,
        # so a stub over `_load_tracked_pids` alone would leave the claim reading
        # the developer's live `~/.stealth-mcp` record — where this test's own
        # first spawn is recorded under a LIVE owner, which is a correct refusal
        # about the wrong record. Redirecting the path gives the incident's real
        # shape, no entry at all, and keeps every write inside tmp_path.
        with patch.object(
            tool_runtime.process_cleanup,
            "pid_file",
            tmp_path / "browser_pids.json",
        ):
            result = await spawn(
                headless=True, user_data_dir=str(profile), **sandbox_kwargs()
            )
        second = result["instance_id"]

        diagnostics = result["spawn_diagnostics"]
        assert diagnostics.get("reattached") is True, (
            f"expected a re-attach, got {diagnostics!r}"
        )
        # The BROWSER process on that profile, so an operator can see which
        # browser answered — the one with no `--type`, picked by
        # `browser_cmdline.browser_process` out of the whole holding tree.
        # `profile_lock` names an arbitrary member of that tree and this must not
        # be it: a `utility` child has no debugging port (the adoption declines),
        # and a `renderer` has one (the adoption succeeds onto a process the
        # manager will discard the moment it recycles).
        holder = diagnostics["reattached_pid"]
        assert isinstance(holder, int)
        assert "chrome" in psutil.Process(holder).name().lower()
        assert (
            browser_cmdline.flag_value(browser_cmdline.arguments(holder), "--type")
            is None
        ), "the adopted pid must be the browser process, not one of its children"
        # Ignored rather than refused: this spawn passed headless=True, which a
        # running browser cannot be given.
        assert "headless" in diagnostics["ignored_spawn_args"]
        assert "user_agent" in diagnostics["not_restored"]
        # MEASURED, not the model's 1920x1080 default — this Chrome is headless
        # and was never sized by us.
        assert diagnostics["window_size"]["measured"] is True
        # Nothing recorded an id for it, so a fresh one is minted — but it names
        # the SAME renderer, which is the claim that matters.
        assert await eval_js(second, "window.__f888_held") == "seller-central"
        # And it did NOT walk: F-871's sibling directory was never created.
        assert not (tmp_path / "seller-central-2").exists()
    finally:
        with contextlib.suppress(Exception):
            await close(instance_id=second or first)
        if second is None and chrome_pid is not None:
            with contextlib.suppress(Exception):
                psutil.Process(chrome_pid).kill()
        await _released(profile)


async def test_a_second_manager_built_first_takes_over_the_live_browser(
    fixture_app_server, tmp_empty_root, tmp_path
):
    """Two managers coexisting, which is the shape F-886 made ordinary.

    Since two backends can run side by side, the failure this finding is about
    does NOT usually look like a restart: backend B is already up and running
    when backend A dies, so B never re-runs its startup pass and A's browser is
    simply unreachable. So the second manager here is **constructed before the
    first drops its handle** — no restart is simulated at all — and the take-over
    happens through the decision a spawn makes, against a REAL Chrome.

    It also proves the cross-process claim end to end: the record starts empty
    and ends naming this process as the owner of that pid, which is what makes a
    third backend refuse the same browser.
    """
    manager = tool_runtime.browser_manager
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")

    profile = tmp_path / "coexisting"
    first = (
        await spawn(headless=True, user_data_dir=str(profile), **sandbox_kwargs())
    )["instance_id"]

    second_manager = BrowserManager()  # ALIVE while the first still holds it
    record = tmp_path / "browser_pids.json"
    cleanup = _cleanup_on(record)
    adopted = None
    chrome_pid = None
    try:
        await navigate_and_settle(first, f"{fixture_app_server}/interactions.html")
        await eval_js(first, "window.__f888_coexist = 'held'")
        chrome_pid = manager._instances[first]["browser"]._process_pid

        # The first backend dies where it stands: the handle goes, the process
        # does not, and nothing is written anywhere.
        manager._instances.pop(first)
        manager._spawn_diagnostics.pop(first, None)

        held = await asyncio.wait_for(
            browser_reattach.adopt_held_profile(
                second_manager, cleanup, str(profile), ignored_args=["headless"]
            ),
            timeout=_ADOPT_DEADLINE,
        )
        assert held.instance_id is not None, f"declined: {held.declined}"
        adopted = held.instance_id

        # The SAME renderer, reached by the manager that was already running.
        tab = await second_manager.get_tab(adopted)
        assert await tab.evaluate("window.__f888_coexist") == "held", (
            "the second manager attached to a different browser"
        )

        # The claim landed: the record now names THIS process as the owner of the
        # BROWSER process on that profile, which is what a third backend reads
        # and refuses — see the note in the node above for why it has to be that
        # process and not whichever member of the tree the witness named.
        entries = browser_pid_registry.read_entries(record)
        assert len(entries) == 1
        recorded = next(iter(entries.values()))
        assert recorded["owner_pid"] == os.getpid()
        assert (
            recorded["pid"]
            == (second_manager._spawn_diagnostics[adopted]["reattached_pid"])
        )
        assert "chrome" in psutil.Process(recorded["pid"]).name().lower()

        # And a second take-over of the same live browser is refused — the
        # two-concurrent-spawns answer, against the record the first one really
        # wrote. ONE witness is injected and it has to be: "is this owner a live
        # BACKEND of ours" is False for a pytest process by construction, so
        # without it the claim would find its own stamp reapable and the refusal
        # could never fire here. Everything else is real, including the entry.
        third = _cleanup_on(record)
        with patch.object(third, "_owner_backend_alive", return_value=True):
            again = await asyncio.wait_for(
                browser_reattach.adopt_held_profile(
                    BrowserManager(), third, str(profile)
                ),
                timeout=_ADOPT_DEADLINE,
            )
        assert again.instance_id is None
        assert "already owns the browser" in (again.declined or "")
    finally:
        with contextlib.suppress(Exception):
            if adopted is not None:
                await second_manager.close_instance(adopted)
            else:
                await close(instance_id=first)
        if adopted is None and chrome_pid is not None:
            with contextlib.suppress(Exception):
                psutil.Process(chrome_pid).kill()
        await _released(profile)
