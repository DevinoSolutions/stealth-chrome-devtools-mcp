"""F-1009 — THE one home for "is the browser behind this DevTools connection the
Chrome THIS launch started": the process behind the endpoint is asked who it is,
and a stranger is refused before anything is built on it.

**The defect.** nodriver picks a port by binding and releasing a socket
(``util.free_port``) and only then launches Chrome with
``--remote-debugging-port=<that port>``; the delegated launch (F-810) did the
same with ``proxy_forwarder._free_port`` across a window of SECONDS. Anything
that binds the port in the gap — most often a sibling Chrome a concurrent spawn
is starting — wins it. Our Chrome then cannot open its endpoint, the sibling's
answers ``/json/version``, nodriver cannot tell the difference, and the spawn
registers the SIBLING's browser as its own. ``close_instance`` on that instance
kills the sibling: on the shared ``fleet`` or ``default`` session that is the
owner's logins.

**What does not work, measured.** Not choosing the port (``=0``): CI's
offline-stealth gate then reported ``navigator.webdriver == true`` on Linux,
Windows and macOS, so the argv keeps the concrete port. Trusting
``DevToolsActivePort`` in our profile: Chrome does not write it for a concrete
port (the same fact ``cdp_endpoint`` documents — the file was ABSENT on a
running browser started with ``--remote-debugging-port=9223``), so waiting for it
failed every real spawn on macOS and Windows.

**What does.** The one thing a squatter cannot fake is which process it is.
:func:`confirm_ours` asks the connected browser ``SystemInfo.getProcessInfo`` —
read-only, answered by the browser process itself — and requires the pid of its
``browser`` process to be the pid we launched or one of its descendants (a
wrapper script that forks the real binary is the descendant case). Anything else
is a ``ToolError`` naming both pids, the connection is dropped WITHOUT sending
``Browser.close`` (a websocket close is not a command), and nothing is
registered, so ``close_instance`` has nothing to close and ``spawn_leak`` kills
only the pid we launched.

A leaf: stdlib, ``psutil`` and ``tool_errors``; ``nodriver`` is imported inside
the one function that needs it because ``desktop_launch`` imports this module
and must not pay for a browser driver on the proxy's path (F-867).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import psutil

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

if TYPE_CHECKING:
    from nodriver import Browser

__all__ = ["IDENTITY_TIMEOUT_SECONDS", "confirm_ours", "owned_pids"]

#: How long the browser gets to say which process it is. A healthy Chrome answers
#: in milliseconds; this is a ceiling inside the launch phase's budget, not a wait.
IDENTITY_TIMEOUT_SECONDS = 10.0

_BROWSER_PROCESS = "browser"


def owned_pids(launched_pid: int) -> set[int]:
    """*launched_pid* and every process below it: what a launch of ours can be.

    A wrapper script that forks the real binary leaves the browser a descendant
    of the pid we hold. A launcher that has already exited has no descendants to
    find, so only its own pid is returned.
    """
    pids = {launched_pid}
    with contextlib.suppress(psutil.Error):
        pids.update(child.pid for child in psutil.Process(launched_pid).children(True))
    return pids


def _refusal(browser_pid: int, launched_pid: int) -> ToolError:
    return ToolError(
        f"F-1009: the browser answering this spawn's DevTools port is process "
        f"{browser_pid}, not the Chrome this spawn launched (pid {launched_pid}). "
        "Another process holds that port; this spawn disconnected from it "
        "without sending it anything that closes it, and did not register it."
    )


def _unverified(seconds: float) -> ToolError:
    return ToolError(
        f"F-1009: the browser on this spawn's DevTools port did not say which "
        f"process it is within {seconds:g} s, so it could not be told apart from "
        "another process's browser on that port. The connection was dropped "
        "without closing the browser."
    )


async def confirm_ours(browser: Browser, launched_pid: int | None) -> None:
    """Raise ``ToolError`` unless *browser*'s own process is the one we launched.

    *launched_pid* ``None`` (nodriver left it unset) means nothing to compare
    against: logged and allowed, as before F-1009. A browser that does not offer
    the query (a ``ProtocolException``) or reports no ``browser`` process is
    likewise unverifiable rather than foreign.
    """
    if launched_pid is None:
        debug_logger.log_warning(
            "devtools_port",
            "confirm_ours",
            "no launched pid to compare the DevTools endpoint's process against",
        )
        return
    from nodriver import cdp
    from nodriver.core.connection import ProtocolException

    try:
        processes = await asyncio.wait_for(
            browser.connection.send(cdp.system_info.get_process_info()),
            IDENTITY_TIMEOUT_SECONDS,
        )
    except ProtocolException:
        debug_logger.log_warning(
            "devtools_port",
            "confirm_ours",
            "the browser does not offer SystemInfo.getProcessInfo; identity unchecked",
        )
        return
    except TimeoutError:
        await browser.connection.disconnect()
        raise _unverified(IDENTITY_TIMEOUT_SECONDS) from None
    browser_pids = [p.id_ for p in processes or () if p.type_ == _BROWSER_PROCESS]
    if not browser_pids or set(browser_pids) & owned_pids(launched_pid):
        return
    await browser.connection.disconnect()
    raise _refusal(browser_pids[0], launched_pid)
