"""F-899: no test may leave an entry in the process-global ``in_memory_storage``.

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

So the fix is the harness's, at its one home — ``conftest.py``'s autouse
``_in_memory_storage_hygiene``, a sibling of ``_stealth_logger_hygiene``, which
restores the store's contents after every test. The product is unchanged: a
backend process gets one store, writes it on spawn/adopt, clears it on
``close_instance`` and on lifespan shutdown, and that is symmetric.

This file is the order-independent pin the mask could hide. It is deliberately a
two-step NODE PAIR in ONE file — pytest runs a file's tests in declared order, so
step 2 always follows step 1 no matter which other files are collected, and it
needs no sibling file to stay adjacent. Step 1 writes through the same public
call the two production writers use; step 2 asserts both what the store holds and
what ``list_instances`` answers, because the reported defect was the second one.
"""

from __future__ import annotations

from fakes import FakeBrowserManager, FakeTab, call_tool
from stealth_chrome_devtools_mcp.embedded.in_memory_storage import in_memory_storage

# The id step 1 leaves behind. Named for this file so a failure in step 2 says
# which test wrote the entry it is complaining about.
LEAKED_ID = "f899-leaked-instance"


def test_a_test_may_write_the_process_global_store():
    """Step 1 — the write a production writer performs, through its own API.

    ``browser_reattach``'s adoption path and ``BrowserManager``'s spawn both end
    in exactly this call, against exactly this object; neither is patchable from
    ``tool_runtime``, which is why a fixture and not a fake is what isolates it.
    Nothing is cleaned up here on purpose — that is the point of the pin.
    """
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


async def test_the_next_test_does_not_inherit_it(patched_server):
    """Step 2 — and the next test sees an empty store, and an empty listing.

    Two assertions because the defect had two faces: the store itself still held
    the entry, and ``list_instances`` reported it to a caller as a ``stored``
    record about a browser that never existed in this test.
    """
    assert LEAKED_ID not in in_memory_storage.list_instances().get("instances", {})

    server = patched_server(
        browser_manager=FakeBrowserManager(tabs={"i1": FakeTab()}),
    )
    assert await call_tool(server, "list_instances") == []
