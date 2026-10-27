"""THE one home for the heal's last rung: end a browser the close path left running,
but only one whose identity is established (T-4686).

``browser_health.heal`` closes a hung browser through the close path (exit-wait, then
the terminate ladder) and waits for the pid it took BEFORE the close to go. On
2026-10-21 (backend 46612) the browser outlived all of that and the heal refused to
relaunch onto a profile it still held, so the session stayed frozen until a human
killed it. This is the rung after that wait.

**Identity, all of it, or nothing dies.** The pid is the one the heal took, and it
must also be the pid the registry recorded for this instance, with the recorded
start time; its own argv must be the BROWSER process (no ``--type``) on this
instance's ``--user-data-dir`` and ``--remote-debugging-port``. Anything unreadable,
missing or different is a refusal that names which, and the browser is left running:
a login in a profile we cannot prove is ours is worth more than a relaunch
(``reap_guard``'s rule, applied to this path). The start time is re-checked inside
the kill's own thread, immediately before the kill.

Nothing here is a second way to identify or end a process: each check is the one
home's call (``freeze_capture.identity_problem`` for pid and start time,
``browser_cmdline.browser_members`` / ``debug_port`` for the argv,
``shared_session.holder_of`` for who holds the profile) and the kill is
``process_cleanup``'s ladder
(``_kill_process_by_pid``: terminate, then kill, never by image name, refusing a pid
whose name is not a browser's). The one difference from ``identity_problem`` is
deliberate and stays here as a thin check: it reads a missing pid or start time as
"nothing to contradict" (right for a capture), and a kill must refuse both.

When the ladder gives up (about 5 s after its terminate, which is all a REAP can spare)
the pid is waited for once more, for :data:`KILL_EXIT_SECONDS`, with the same
``wait_exit`` the heal used before it: Chrome on a host whose process teardown stalls
(2026-10-27, Sentry STEALTH-CHROME-DEVTOOLS-MCP-EF) kept writing its log for ~15 s after
the ladder had been refused, and left on its own. Only a pid still there after that is
reported as left running. The ladder itself is untouched: its other callers (a close, a
reap, a failed spawn) have their own, shorter budgets.

After a kill the profile is asked once more who holds it (a lingering child would),
for a few seconds, before the heal relaunches onto it.

Scope is the heal's: a persistent-profile browser the loop already closed and was
about to relaunch under the same id. ``default`` / ``fleet`` get the same relaunch as
before; this only removes the process that stood in its way.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable

from stealth_chrome_devtools_mcp.embedded import (
    browser_cmdline,
    browser_pid_registry,
    freeze_capture,
    shared_session,
)
from stealth_chrome_devtools_mcp.embedded.browser_pid_registry import Entry
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup
from stealth_chrome_devtools_mcp.embedded.process_exit import Exited

_COMPONENT = "forced_end"

#: How long a browser the kill ladder could not end is waited for before it is called
#: left running: the heal's own ``EXIT_WAIT_SECONDS`` (15 s, ``browser_health``) plus
#: 10 s, since the one measured teardown stall outlasted the ladder by ~15 s. A
#: ceiling, not a wait: a browser that has gone costs one psutil read, off the loop.
KILL_EXIT_SECONDS = 25.0

#: How long a killed browser's profile may still read as held (its children are
#: going) before the heal is refused. A ceiling, not a wait.
SETTLE_SECONDS = 5.0
_POLL_SECONDS = 0.25


def _stamped(entry: Entry) -> Entry:
    """*entry* with its start time as the float ``identity_problem`` compares (or
    ``None``), since that rule reads anything else as "nothing to contradict"."""
    return {
        **entry,
        "create_time": browser_pid_registry.recorded_time(entry, "create_time"),
    }


def refusal(pid: int, entry: Entry, *, user_data_dir: str | None) -> str | None:
    """Why *pid* is not established as the browser recorded in *entry* on
    *user_data_dir*, or ``None`` when every part of its identity agrees."""
    stamped = _stamped(entry)
    port = browser_pid_registry.recorded_port(entry)
    if (
        entry.get("pid") is None
        or stamped["create_time"] is None
        or port is None
        or not user_data_dir
    ):
        return "its pid, start time, DevTools port or profile directory is not recorded"
    if problem := freeze_capture.identity_problem(stamped, pid):
        return problem
    members = browser_cmdline.browser_members([pid], user_data_dir)
    if pid not in members.browsers:
        return (
            f"the command line of pid {pid} could not be read"
            if pid in members.unreadable
            else f"pid {pid} is not a browser process on this instance's profile "
            "directory (a child, another profile, or gone)"
        )
    if browser_cmdline.debug_port(pid, user_data_dir) != port:
        return f"pid {pid} is not on this instance's DevTools port"
    return None


def _end(
    pid: int, instance_id: str, stamped: Entry, kill: Callable[[int, str], bool]
) -> str | bool:
    """The kill, on its thread, with the start time asked again the instant before:
    the reason it did not run, else whether the ladder reports the pid gone."""
    if problem := freeze_capture.identity_problem(stamped, pid):
        return f"was NOT force-ended (changed before the kill: {problem})"
    return kill(pid, instance_id)


async def _held_after(
    user_data_dir: str, holder: Callable[[str], str | None]
) -> str | None:
    """Who still holds *user_data_dir* once :data:`SETTLE_SECONDS` have passed."""
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        held = await asyncio.to_thread(holder, user_data_dir)
        if held is None or time.monotonic() >= deadline:
            return held
        await asyncio.sleep(_POLL_SECONDS)


async def run(  # noqa: PLR0913  PERMANENT(T-4686: one keyword per external effect, so the rung runs hermetically; same shape as BrowserHealth.__init__)
    instance_id: str,
    pid: int,
    entry: Entry,
    *,
    user_data_dir: str | None,
    wait_exit: Callable[[int, float], Awaitable[Exited]],
    timeout: float,  # noqa: ASYNC109  PERMANENT(T-4686: the wait_exit seam's own bound, not an await deadline)
    kill: Callable[[int, str], bool] | None = None,
    holder: Callable[[str], str | None] | None = None,
) -> str | None:
    """Wait up to *timeout* for *pid* to be gone (the close path's own kill is
    offloaded and still dying), then end it when :func:`refusal` finds nothing
    against it. ``None`` means the browser is gone and its profile free; otherwise
    the reason it was not, worded to follow "the close path left the browser running
    and it ". *entry* is the registry entry taken BEFORE the close, which untracks
    it. *kill* is the kill ladder (a thread: it waits)."""
    if (await wait_exit(pid, timeout)).exited:
        return None
    problem = await asyncio.to_thread(refusal, pid, entry, user_data_dir=user_data_dir)
    if problem is not None or not user_data_dir:
        return f"was NOT force-ended ({problem})"
    ended = await asyncio.to_thread(
        _end,
        pid,
        instance_id,
        _stamped(entry),
        kill or process_cleanup._kill_process_by_pid,
    )
    if isinstance(ended, str):
        return ended
    if not ended:
        late = await wait_exit(pid, KILL_EXIT_SECONDS)
        if not late.exited:
            return f"could not be force-ended (the kill ladder left pid {pid} running)"
        debug_logger.log_warning(
            _COMPONENT,
            "run",
            f"{instance_id}: pid {pid} outlasted the kill ladder and was then seen to "
            f"exit, inside a further {KILL_EXIT_SECONDS:.0f}s wait",
        )
    debug_logger.log_warning(
        _COMPONENT,
        "run",
        f"{instance_id}: force-ended pid {pid}, which the close path left running "
        "(identity verified: pid, start time, profile directory, DevTools port)",
    )
    if held := await _held_after(user_data_dir, holder or shared_session.holder_of):
        return f"was force-ended, but {held} still holds its profile"
    return None
