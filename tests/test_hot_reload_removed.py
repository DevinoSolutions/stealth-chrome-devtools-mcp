"""``hot_reload`` and ``reload_status`` are deleted, not repaired — pin that they
stay gone from the tool surface.

``hot_reload`` structurally could not do what its name promised: it reloaded the
bare-named sibling modules (``browser_manager`` and friends) through the stale
``from``-import bindings already captured in ``server.py``, never reloaded
``server.py`` itself (where the tools actually live), destroyed the live browser
sessions belonging to the singletons it rebuilt, and returned a success string
regardless of what happened — F-102 / F-121 / F-610. ``reload_status`` was its
module-status sibling, wholly superseded by ``doctor``. Deleting both (plan_M2
step M2-1) leaves exactly one way for a source edit to reach the running
backend: a fresh process spawn.

Written against HEAD these assertions FAIL (both tools are still registered via
``@section_tool("debugging")``); after the two ``async def``s are removed they
pass. That red→green inversion is the pin.
"""

import server


def test_hot_reload_tool_removed():
    assert "hot_reload" not in server.SECTION_TOOLS["debugging"]
    assert not hasattr(server, "hot_reload")


def test_reload_status_tool_removed():
    assert "reload_status" not in server.SECTION_TOOLS["debugging"]
    assert not hasattr(server, "reload_status")
