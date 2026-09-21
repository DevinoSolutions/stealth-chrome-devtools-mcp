"""F-899: nothing may leave an entry stranded in ``in_memory_storage``.

One subject, two halves — a test that leaks into the process-global store, and a
``close_instance`` that strands an entry it has already claimed. Sections 1 and 2
below. The store is the same object in both, which is why both pins live here.

## 1. The reported leak — a test writing the process-global store

Measured on main ``f18ecc5``::

    uv run python -m pytest tests/test_browser_reattach.py \
                            tests/test_tool_failure_visibility.py -q
    2 failed, 102 passed

Both failures were ``assert await call_tool(server, "list_instances") == []``
answering with two ``source: "stored"`` rows — ``i-kept`` and ``i-held``, created
four hundred tests earlier in ``test_browser_reattach.py``. Neither file is at
fault. ``list_instances`` merges the manager's live instances with
``in_memory_storage``'s (``tool_sections/browser_management.py``), and that
storage is a module-level singleton: ONE object for the whole pytest process,
where production gets a fresh one per backend. Two production writers put
entries in it — ``browser_reattach``'s adoption and ``BrowserManager``'s spawn —
and both are reached by hermetic tests that drive the real code with a fake
manager, so ``patched_server(in_memory_storage=FakeStorage())`` does not cover
them: those modules import the singleton directly, by value, at module scope.

The whole lane was green only by accident. ``test_mcp_protocol_surface.py``
sorts between the two files and opens ``fastmcp.Client(server.mcp)`` with NO
``patched_server``, so the real ``app_lifespan`` runs and its shutdown branch
calls ``rt.in_memory_storage.clear_all()`` on the real singleton — an unrelated
file incidentally wiping the leak on its way past. Nothing holds that in place:
drop that file, rename it, or mark it integration, and the two symptom files go
red again in a different pair.

So THAT fix is the harness's, at its one home — ``conftest.py``'s autouse
``_in_memory_storage_hygiene``, a sibling of ``_stealth_logger_hygiene``, which
restores the store's contents after every test. The write it isolates is the
product working correctly: ``adopt`` writes as the last statement of its ``try``,
so no abort path can strand it.

The pin below is the order-independent one the mask could hide. It is
deliberately a two-step NODE PAIR in ONE file — pytest runs a file's tests in
declared order, so step 2 always follows step 1 no matter which other files are
collected, and it needs no sibling file to stay adjacent. Step 1 writes through
the same public call the two production writers use; step 2 asserts both what the
store holds and what ``list_instances`` answers, because the reported defect was
the second one.

## 2. The half that WAS a product defect — a cancelled close

Reviewing the above traced every write and removal, and ``close_instance`` did not
match the rest: it popped ``_instances`` in Phase 1 and dropped the store entry in
Phase 4, six awaits later, inside a ``try`` whose ``except Exception`` a
``CancelledError`` walks straight past. A client disconnecting mid-close therefore
left the manager without the instance and the store with its entry — a
``source: "stored"`` row in ``list_instances``, about a browser already being torn
down, for the life of the backend. The removal is Phase 1's now, under the same
lock as the pop with no ``await`` between them.

Its pin is the third node here and not in ``test_close_instance_offload.py``,
whose autouse fixture replaces ``in_memory_storage.remove_instance`` with a
``MagicMock`` — against that double this defect is invisible.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from fakes import FakeBrowserManager, FakeTab, call_tool
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.in_memory_storage import in_memory_storage
from stealth_chrome_devtools_mcp.embedded.models import BrowserInstance, BrowserState

# The id step 1 leaves behind. Named for this file so a failure in step 2 says
# which test wrote the entry it is complaining about.
LEAKED_ID = "f899-leaked-instance"

# Step 1 ran. Without this, ``-k``, a single-node selection and — the one that
# matters — ``--lf`` after a step-2 failure all rerun step 2 ALONE, where it
# passes about nothing: the store is empty because nobody filled it. A pin that
# goes green when its own premise was skipped is worse than no pin (review S3).
_WROTE_THE_STORE = False


def test_a_test_may_write_the_process_global_store():
    """Step 1 — the write a production writer performs, through its own API.

    ``browser_reattach``'s adoption path and ``BrowserManager``'s spawn both end
    in exactly this call, against exactly this object; neither is patchable from
    ``tool_runtime``, which is why a fixture and not a fake is what isolates it.
    Nothing is cleaned up here on purpose — that is the point of the pin.
    """
    global _WROTE_THE_STORE

    in_memory_storage.store_instance(
        LEAKED_ID,
        {
            "instance_id": LEAKED_ID,
            "state": "active",
            "last_navigated_url": "https://f899.test/page",
            "last_navigated_title": "F-899",
        },
    )
    assert in_memory_storage.get_instance(LEAKED_ID) is not None
    _WROTE_THE_STORE = True


async def test_the_next_test_does_not_inherit_it(patched_server):
    """Step 2 — and the next test sees an empty store, and an empty listing.

    Two assertions because the defect had two faces: the store itself still held
    the entry, and ``list_instances`` reported it to a caller as a ``stored``
    record about a browser that never existed in this test.
    """
    assert _WROTE_THE_STORE, "run the whole file — step 1 is this pin's other half"
    assert LEAKED_ID not in in_memory_storage.list_instances().get("instances", {})

    server = patched_server(
        browser_manager=FakeBrowserManager(tabs={"i1": FakeTab()}),
    )
    assert await call_tool(server, "list_instances") == []


# ---------------------------------------------------------------------------
# 2. the product half — a cancelled close must not strand its entry either
# ---------------------------------------------------------------------------

CLOSED_ID = "f899-cancelled-close"


def _blocking_browser(entered: asyncio.Event) -> SimpleNamespace:
    """A browser whose first Phase-2 CDP send never returns.

    ``close_instance`` reaches it after Phase 1 has already popped the instance,
    which is the window the pin is about. ``tabs`` is empty so the tab loop above
    it is skipped and there is exactly ONE place the coroutine can be suspended.
    """

    async def _never(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    return SimpleNamespace(
        tabs=[],
        connection=SimpleNamespace(closed=False, send=_never, disconnect=_never),
        _process=SimpleNamespace(returncode=0, pid=0),
        _process_pid=0,
        stop=lambda: None,
    )


async def test_a_cancelled_close_does_not_strand_its_store_entry():
    """A client that disconnects mid-``close_instance`` leaves no ghost row.

    The removal used to live in Phase 4, six awaits past the Phase-1 pop and
    inside a ``try`` whose handler is ``except Exception`` — which a
    ``CancelledError`` walks straight past, because it is a ``BaseException``.
    So the manager lost the instance and the store kept its entry, and
    ``list_instances`` reported it as a ``source: "stored"`` record for the life
    of the backend: nothing but lifespan shutdown clears it, and the browser it
    names is already being torn down. Phase 1 drops it under the same lock, with
    no ``await`` in between, so the two cannot be separated.

    The cancellation must still PROPAGATE — this is a client that went away, not
    a close that succeeded, and swallowing it would make the caller's task look
    like it finished.
    """
    manager = BrowserManager()
    entered = asyncio.Event()
    manager._instances[CLOSED_ID] = {
        "browser": _blocking_browser(entered),
        "instance": BrowserInstance(instance_id=CLOSED_ID, state=BrowserState.READY),
    }
    in_memory_storage.store_instance(
        CLOSED_ID, {"instance_id": CLOSED_ID, "state": "active"}
    )

    task = asyncio.create_task(manager.close_instance(CLOSED_ID))
    await asyncio.wait_for(entered.wait(), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert CLOSED_ID not in manager._instances  # Phase 1 did claim it
    assert CLOSED_ID not in in_memory_storage.list_instances().get("instances", {})
