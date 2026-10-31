"""T-9542: ``paint_hold``'s process-global state is per-test, whoever left it.

``paint_hold._held`` and ``_browserless_caller_owned`` are module globals that live as long as the
process. Any test that drives a tool through ``tab_binding.tab_for_caller`` with ``FakeTab``'s
default target ``T-faketab`` (the login-persistence, browser-manager, spawn, profile and
close suites do) leaves a hold on it, and ``test_paint_hold``'s own cleanup only runs
inside that file. So ``test_a_closed_tab_is_forgotten_and_its_timer_task_ends`` failed
under ``-k 'login_persistence or browser_manager or spawn or profile or close'`` and
passed alone and in the full run. The conftest now resets the state around every test.

The two tests below run in this order (pytest keeps definition order): the first plays
the leaker, the second must see none of it.
"""

from __future__ import annotations

from fakes import FakeTab
from stealth_chrome_devtools_mcp.embedded import paint_hold


async def test_a_test_that_leaves_a_hold_and_a_caller_owned_target_behind():
    await paint_hold.hold(FakeTab())
    paint_hold._browserless_caller_owned.add("T-someone-elses")
    assert paint_hold._held and paint_hold._browserless_caller_owned


async def test_the_next_test_starts_with_no_hold_and_no_caller_owned_target():
    assert paint_hold._held == {}
    assert paint_hold._browserless_caller_owned == set()
