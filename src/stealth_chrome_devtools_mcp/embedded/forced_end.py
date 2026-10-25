"""THE one home for the heal's last rung: end a browser the close path left running,
but only one whose identity is established (T-4686).

``browser_health.heal`` closes a hung browser through the close path (exit-wait, then
the terminate ladder) and waits for the pid it took BEFORE the close to go. On
2026-10-21 (backend 46612) the browser outlived all of that and the heal refused to
relaunch onto a profile it still held, so the session stayed frozen until a human
killed it. This is the rung after that wait.

**Identity, all of it, or nothing dies.** The pid is the one the heal took, and it
must also be the pid the registry recorded for this instance, with the recorded
start time (a start time that was never recorded is a refusal here, although
``process_cleanup._fallback_pid_identity_ok``, THE tolerance, reads it as "nothing to
contradict"); its own argv must name this instance's ``--user-data-dir`` and
``--remote-debugging-port`` and carry no ``--type`` (a child is not the browser).
Anything unreadable, missing or different is a refusal that names which, and the
browser is left running: a login in a profile we cannot prove is ours is worth more
than a relaunch (``reap_guard``'s rule, applied to this path).

Nothing here is a second way to identify or end a process: the argv facts are
``browser_cmdline``'s, the path comparison ``browser_pid_registry.normalize_path``'s,
the start-time rule ``process_cleanup``'s, and the kill is its ladder
(``_kill_process_by_pid``: terminate, then kill, never by image name, and refusing a
pid whose name is not a browser's).

Scope is the heal's: a persistent-profile browser the loop already closed and was
about to relaunch under the same id. ``default`` / ``fleet`` get the same relaunch as
before; this only removes the process that stood in its way.
"""

import asyncio
from collections.abc import Awaitable, Callable

from stealth_chrome_devtools_mcp.embedded import browser_cmdline, browser_pid_registry
from stealth_chrome_devtools_mcp.embedded.browser_pid_registry import Entry
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup
from stealth_chrome_devtools_mcp.embedded.process_exit import Exited

_COMPONENT = "forced_end"


def _record_problem(pid: int, entry: Entry, user_data_dir: str | None) -> str | None:
    """What the registry entry and the process's start time cannot establish."""
    created = entry.get("create_time")
    if entry.get("pid") != pid:
        return f"pid {pid} is not the pid recorded for this instance"
    if not isinstance(created, float):
        return f"no start time is recorded for pid {pid}"
    if browser_pid_registry.recorded_port(entry) is None or not user_data_dir:
        return "its profile directory or DevTools port is not recorded"
    if not process_cleanup._fallback_pid_identity_ok(pid, created):
        return f"pid {pid} is gone or its start time differs from the recorded one"
    return None


def _argv_problem(
    pid: int, cmdline: list[str], user_data_dir: str, port: int | None
) -> str | None:
    """What the process's own command line says against it being the browser."""
    if not cmdline:
        return f"the command line of pid {pid} could not be read"
    if browser_cmdline.flag_value(cmdline, browser_cmdline.TYPE_FLAG) is not None:
        return f"pid {pid} is a browser child process, not the browser"
    if browser_pid_registry.normalize_path(
        browser_cmdline.flag_value(cmdline, "--user-data-dir")
    ) != browser_pid_registry.normalize_path(user_data_dir):
        return f"pid {pid} is not on this instance's profile directory"
    on_argv = browser_cmdline.flag_value(cmdline, browser_cmdline.DEBUG_PORT_FLAG)
    if browser_pid_registry.valid_port(on_argv) != port:
        return f"pid {pid} is not on this instance's DevTools port"
    return None


def refusal(
    pid: int,
    entry: Entry,
    *,
    user_data_dir: str | None,
    arguments: Callable[[int], list[str]] = browser_cmdline.arguments,
) -> str | None:
    """Why *pid* is not established as the browser recorded in *entry* on
    *user_data_dir*, or ``None`` when every part of its identity agrees."""
    problem = _record_problem(pid, entry, user_data_dir)
    if problem is not None or not user_data_dir:
        return problem
    return _argv_problem(
        pid, arguments(pid), user_data_dir, browser_pid_registry.recorded_port(entry)
    )


async def run(  # noqa: PLR0913  PERMANENT(T-4686: one keyword per external effect, so the rung runs hermetically; same shape as BrowserHealth.__init__)
    instance_id: str,
    pid: int,
    entry: Entry,
    *,
    user_data_dir: str | None,
    wait_exit: Callable[[int, float], Awaitable[Exited]],
    timeout: float,  # noqa: ASYNC109  PERMANENT(T-4686: the wait_exit seam's own bound, not an await deadline)
    arguments: Callable[[int], list[str]] = browser_cmdline.arguments,
    kill: Callable[[int, str], bool] | None = None,
) -> str | None:
    """Wait up to *timeout* for *pid* to be gone (the close path's own kill is
    offloaded and still dying), then end it when :func:`refusal` finds nothing
    against it. ``None`` means the browser is gone; otherwise the reason it was
    not, worded to follow "the close path left the browser running and it ".
    *entry* is the registry entry taken BEFORE the close, which untracks it. *kill*
    is the kill ladder (a thread: it waits)."""
    if (await wait_exit(pid, timeout)).exited:
        return None
    problem = refusal(pid, entry, user_data_dir=user_data_dir, arguments=arguments)
    if problem is not None:
        return f"was NOT force-ended ({problem})"
    ended = await asyncio.to_thread(
        kill or process_cleanup._kill_process_by_pid, pid, instance_id
    )
    if not ended:
        return f"could not be force-ended (the kill ladder left pid {pid} running)"
    debug_logger.log_warning(
        _COMPONENT,
        "run",
        f"{instance_id}: force-ended pid {pid}, which the close path left running "
        "(identity verified: pid, start time, profile directory, DevTools port)",
    )
    return None
