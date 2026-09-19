"""Real-Chrome proof for F-888: a login survives the backend that opened it.

The hermetic pins in ``test_browser_reattach.py`` prove the RULE — who may be
adopted, which witness names the port, what the reaper spares. They cannot prove
the thing the incident was actually about, because every one of them patches the
CDP door: that a second `uc.start` against a port the first backend's Chrome is
listening on **connects to that same renderer** instead of launching a new one,
and that the page state a human built is still there afterwards.

So this node uses no double at all. It spawns a real Chrome on a real named
profile, writes a value into the live page, then simulates the backend dying the
only way that is honest here — dropping the manager's handle on the browser
WITHOUT killing the process, exactly as a `TerminateProcess`d backend leaves it —
and hands a fresh `BrowserManager` the record. What it asserts is that the
adopted instance keeps its id and that ``window.__f888`` is still set, which is a
claim about the RENDERER and not about the profile directory: a re-spawn onto the
same `user_data_dir` would pass a cookie check and fail this one.

The owner in the record is a pid that is not a backend of ours, which is what the
adoption rule needs to see. It is this process's own pid answered through a
patched witness rather than a fabricated dead pid, because a fabricated one can
be RECYCLED onto a live process between the write and the read, and the flake
that produces would look exactly like a real adoption refusal.
"""

import asyncio
import contextlib
import json
import os
from unittest.mock import patch

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
from stealth_chrome_devtools_mcp.embedded import browser_reattach, tool_runtime
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
                            "create_time": None,
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
                import psutil

                psutil.Process(chrome_pid).kill()

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

        with patch.object(
            tool_runtime.process_cleanup, "_load_tracked_pids", return_value={}
        ):
            result = await spawn(
                headless=True, user_data_dir=str(profile), **sandbox_kwargs()
            )
        second = result["instance_id"]

        assert result["spawn_diagnostics"].get("reattached") is True, (
            f"expected a re-attach, got {result['spawn_diagnostics']!r}"
        )
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
                import psutil

                psutil.Process(chrome_pid).kill()
