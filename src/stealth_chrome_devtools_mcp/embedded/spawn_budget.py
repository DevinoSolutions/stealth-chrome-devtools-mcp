"""THE one home for "how long may ONE phase of a browser spawn go unanswered
before the attempt is called failed" (F-969): :func:`bounded`, and the two
named budgets it is handed.

**The defect.** ``BrowserManager.spawn_browser`` awaited nodriver's
``Browser.start`` and the post-launch CDP exchanges with no bound. Both end in a
reply from a Chrome that is, by then, running: a ``Browser.start`` that has
connected sends ``Target.setDiscoverTargets`` and ``Target.getTargets`` and
awaits each reply, and nodriver gives a ``Transaction`` no deadline. A Chrome
that stalls after it has launched therefore hung the tool call for as long as
the caller would wait, and left that Chrome running beside it. On 2026-10-14 the
v2.1.33 publish gate's macOS cell sat in exactly that state for 300 s until
pytest-timeout killed the process: the event loop idle in ``select``, both
default-executor threads parked, one live child process
(``audit/stage2/finding_F969_spawn_phases_have_no_bound.md`` §1).

**What this owns** is the deadline and nothing else. A phase that overruns it
raises a :class:`~stealth_chrome_devtools_mcp.embedded.tool_errors.ToolError`
that names the phase and the budget, and the failure then travels the path every
other spawn failure already travels: ``spawn_browser``'s ``except Exception``
tears down what exists (``spawn_leak`` reaps the Chrome ``Browser.start``
launched, the held ``Browser`` is stopped, ``kill_browser_process`` ends the
tracked one) and the tool body's ``_SPAWN_ATTEMPTS`` loop retries onto a fresh
selection. There is deliberately no teardown here: a second way to reap a
spawn's Chrome is the defect ``spawn_leak`` exists to prevent.

**Why a ceiling this high.** A budget that fires on a slow but healthy spawn
turns a working browser into a failure, which is worse than the hang it
replaces, so both numbers sit several times above the slowest spawn ever
measured (§3 of the finding gives the census). They are ceilings and not waits:
a Chrome that answers in 2 s costs 2 s.

**It is a decorator on the three phases** (``_launch_browser``,
``_apply_post_launch``, ``_setup_dynamic_hooks``) rather than a wrapper at their
call site, so the bound travels with the method: ``spawn_browser`` and a heal's
relaunch (F-963) reach the same three and neither can forget it. The budget is
named by its constant and read when the phase RUNS, so a test patches the one
module attribute.

**Why a whole phase and not each ``send``.** ``_apply_post_launch`` is six CDP
exchanges and every one of them is bounded by the phase's budget, which is no
looser than bounding each: a phase that would have taken longer than its budget
by summing six healthy sends is not a healthy phase. Cancelling a send is safe
because ``cdp_transport`` protects the connection's listener from exactly that
(F-883 B1), which is also why this can use the plain ``asyncio.timeout``.

A leaf: asyncio and ``tool_errors`` only.
"""

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

_P = ParamSpec("_P")
_T = TypeVar("_T")

#: How long ``_launch_browser`` (nodriver's ``Browser.start``: the process, the
#: ``/json/version`` wait, the websocket, target discovery) may take.
LAUNCH_BUDGET_SECONDS = 120.0

#: How long each post-launch phase (``_apply_post_launch``, the dynamic-hook
#: setup) may take. Every exchange in them is a local round trip to a Chrome that
#: already answered ``/json/version``.
POST_LAUNCH_BUDGET_SECONDS = 60.0


def phase(
    stage: str, budget_name: str
) -> Callable[[Callable[_P, Awaitable[_T]]], Callable[_P, Awaitable[_T]]]:
    """Bound a spawn phase by the module constant named *budget_name*: past it the
    phase is cancelled and a ``ToolError`` names *stage* and the budget.

    A ``TimeoutError`` the phase raises ITSELF (a bound of its own) passes
    through untouched: only an expiry of this deadline is rewritten.
    """

    def decorate(fn: Callable[_P, Awaitable[_T]]) -> Callable[_P, Awaitable[_T]]:
        @functools.wraps(fn)
        async def bounded(*args: _P.args, **kwargs: _P.kwargs) -> _T:
            budget: float = globals()[budget_name]
            try:
                async with asyncio.timeout(budget) as scope:
                    return await fn(*args, **kwargs)
            except TimeoutError:
                if not scope.expired():
                    raise
                raise ToolError(
                    f"Browser spawn stalled in the {stage} stage: no answer within "
                    f"{budget:g} s. The Chrome it launched is stopped; spawn again."
                ) from None

        return bounded

    return decorate
