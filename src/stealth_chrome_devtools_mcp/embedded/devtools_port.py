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
    from collections.abc import Awaitable

    from nodriver import Browser
    from nodriver.core.connection import Connection

__all__ = [
    "DROP_TIMEOUT_SECONDS",
    "IDENTITY_PATIENCE_SECONDS",
    "IDENTITY_POLL_SECONDS",
    "IDENTITY_TIMEOUT_SECONDS",
    "confirm_ours",
    "owned_pids",
]

#: How long the browser gets to say which process it is before its launcher is
#: looked at. A healthy Chrome answers in milliseconds; this is a floor, not a wait.
IDENTITY_TIMEOUT_SECONDS = 10.0

#: How long the browser gets in all while the process WE launched is still running
#: (T-5899). A healthy Chrome on a starved CI runner (macOS arm64) did not answer
#: inside the floor, and refusing it failed the spawn of our own browser. Waiting
#: longer cannot attach a stranger: the pid in the answer is the whole proof, and a
#: stranger is refused the moment it answers (or cannot). Inside the launch phase.
IDENTITY_PATIENCE_SECONDS = 60.0

#: How often the launcher is looked at once the floor has passed.
IDENTITY_POLL_SECONDS = 0.5

#: How long dropping a refused connection may take. The drop runs on the cancel
#: path too, so it must not be able to hold a cancellation up for ever.
DROP_TIMEOUT_SECONDS = 5.0

_BROWSER_PROCESS = "browser"


def owned_pids(launched_pid: int) -> set[int]:
    """*launched_pid* and every process below it: what a launch of ours can be.

    A wrapper script that forks the real binary leaves the browser a descendant
    of the pid we hold. A launcher that has already exited, or one psutil may not
    read, has no descendants to find, so only its own pid is returned (logged:
    a browser that IS a descendant is then refused as foreign).
    """
    pids = {launched_pid}
    try:
        pids.update(child.pid for child in psutil.Process(launched_pid).children(True))
    except psutil.Error as error:
        debug_logger.log_warning(
            "devtools_port",
            "owned_pids",
            f"could not list the processes below pid {launched_pid} "
            f"({type(error).__name__}); only that pid counts as ours",
        )
    return pids


def _refusal(browser_pid: int, launched_pid: int) -> ToolError:
    return ToolError(
        f"F-1009: the browser answering this spawn's DevTools port is process "
        f"{browser_pid}, not the Chrome this spawn launched (pid {launched_pid}). "
        "Another process holds that port, or the browser runs in a separate pid "
        "namespace (e.g. flatpak); this spawn disconnected from it without "
        "sending it anything that closes it, and did not register it."
    )


def _unverified(seconds: float) -> ToolError:
    return ToolError(
        f"F-1009: the browser on this spawn's DevTools port did not say which "
        f"process it is within {seconds:.0f} s, so it could not be told apart from "
        "another process's browser on that port. The connection was dropped "
        "without closing the browser."
    )


def _unidentifiable() -> ToolError:
    return ToolError(
        "F-1009: the browser on this spawn's DevTools port cannot identify its "
        "process (it has no SystemInfo.getProcessInfo, which real Chrome always "
        "answers), so it cannot be told apart from another program's browser on "
        "that port, such as an Electron or CEF app. The connection was dropped "
        "without closing the browser."
    )


async def _disconnect(connection: Connection) -> None:
    """Close OUR end of one websocket. Never ``Browser.close``: that is a command.

    Shielded and bounded because it also runs while the spawn is being cancelled,
    when a second cancellation must not abandon a half-open socket.
    """
    with contextlib.suppress(Exception):
        await asyncio.wait_for(
            asyncio.shield(connection.disconnect()), DROP_TIMEOUT_SECONDS
        )


async def _drop(browser: Browser) -> None:
    await _disconnect(browser.connection)


def _running(pid: int) -> bool:
    """Is *pid* a live process? One pid, not a scan; unreadable counts as not."""
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


async def _ask_afresh(websocket_url: str) -> list:
    """The same query over a THROWAWAY connection to the same browser endpoint.

    Never ``browser.connection``: nodriver sends ``Target.setDiscoverTargets`` once, in
    ``Browser.start``, and Chrome scopes it to that websocket, so dropping and
    reopening the launch's own connection would silence every later target event
    (``tab_open`` documents the failure). This connection enables nothing and is
    closed whatever ends the request, cancellation included.
    """
    from nodriver import cdp
    from nodriver.core.connection import Connection

    probe = Connection(websocket_url)
    try:
        return await probe.send(cdp.system_info.get_process_info())
    finally:
        await _disconnect(probe)


class _Asks:
    """The identity requests of one wait, in the order they were sent."""

    def __init__(self) -> None:
        self.requests: list[asyncio.Future] = []
        self._reported: set[asyncio.Future] = set()

    def add(self, query: Awaitable[list]) -> None:
        self.requests.append(asyncio.ensure_future(query))

    def pending(self) -> list[asyncio.Future]:
        return [r for r in self.requests if not r.done()]

    def answers(self) -> list[list]:
        """What the browser said so far. A request that FAILED is logged once and
        never preferred over one that answered; a ``ProtocolException`` (the browser
        says it cannot) is raised only while nothing has answered."""
        from nodriver.core.connection import ProtocolException

        failed: list[tuple[asyncio.Future, BaseException]] = []
        answers: list[list] = []
        for request in self.requests:
            if not request.done() or request.cancelled():
                continue
            error = request.exception()
            if error is None:
                answers.append(request.result())
            else:
                failed.append((request, error))
        if answers:
            return answers
        for request, error in failed:
            if isinstance(error, ProtocolException):
                raise error
            if request not in self._reported:
                self._reported.add(request)
                debug_logger.log_warning(
                    "devtools_port",
                    "confirm_ours",
                    f"an identity query failed ({type(error).__name__}); "
                    "it is asked again at the next interval",
                )
        return []

    async def end(self) -> None:
        """Cancel what is still running and wait for it (a throwaway connection closes
        in its own ``finally``); retrieve every failure, so none is left unretrieved."""
        pending = self.pending()
        for request in pending:
            request.cancel()
        if pending:
            await asyncio.wait(pending)
        for request in self.requests:
            if request.done() and not request.cancelled():
                request.exception()


async def _answer(browser: Browser, launched_pid: int) -> list[list]:
    """Every answer the browser gave to its own process-list query, waiting while
    OUR launch is still running.

    The read-only query is asked again once per ``IDENTITY_TIMEOUT_SECONDS``, each
    time over a throwaway connection (T-7014): nodriver's ``send`` can lose a request
    without raising (the fire-and-forget websocket send fails, the listener dies, a
    close ends the listener without failing the pending futures), and one request
    waited on for the whole patience then refuses a healthy browser. The requests are
    waited on (``asyncio.wait`` never cancels them) and cancelled only when the wait
    is over, because an abandoned CDP transaction can take the listener with it. The
    floor is always given; past it the wait goes on, up to
    ``IDENTITY_PATIENCE_SECONDS``, only while the launched pid is alive. A launcher
    that is gone, or one that cannot be read, gets no more.
    """
    from nodriver import cdp

    url = getattr(browser.connection, "websocket_url", None)
    clock = asyncio.get_running_loop().time
    started = clock()
    asks = _Asks()
    asks.add(browser.connection.send(cdp.system_info.get_process_info()))
    next_ask = started + IDENTITY_TIMEOUT_SECONDS
    try:
        while not (answers := asks.answers()):
            waited = clock() - started
            if waited >= IDENTITY_TIMEOUT_SECONDS and not (
                waited < IDENTITY_PATIENCE_SECONDS and _running(launched_pid)
            ):
                raise _unverified(waited)
            if clock() >= next_ask:
                next_ask += IDENTITY_TIMEOUT_SECONDS
                if url:
                    asks.add(_ask_afresh(url))
            pause = max(0.0, min(IDENTITY_POLL_SECONDS, next_ask - clock()))
            if asks.pending():
                await asyncio.wait(
                    asks.pending(), timeout=pause, return_when=asyncio.FIRST_COMPLETED
                )
            else:
                await asyncio.sleep(pause)
        waited = clock() - started
        if waited > IDENTITY_TIMEOUT_SECONDS:
            debug_logger.log_warning(
                "devtools_port",
                "confirm_ours",
                f"the browser took {waited:.1f} s to say which process it is "
                f"(floor {IDENTITY_TIMEOUT_SECONDS:g} s); "
                f"pid {launched_pid} was running",
            )
        return answers
    finally:
        await asks.end()


async def _ask(browser: Browser, launched_pid: int) -> None:
    from nodriver.core.connection import ProtocolException

    try:
        answers = await _answer(browser, launched_pid)
    except ProtocolException:
        raise _unidentifiable() from None
    named = False
    for processes in answers:
        browser_pids = [p.id_ for p in processes or () if p.type_ == _BROWSER_PROCESS]
        if not browser_pids:
            continue
        named = True
        if not set(browser_pids) & owned_pids(launched_pid):
            raise _refusal(browser_pids[0], launched_pid)
    if not named:
        debug_logger.log_warning(
            "devtools_port",
            "confirm_ours",
            "the browser's process list has no `browser` process; identity unchecked",
        )


async def confirm_ours(browser: Browser, launched_pid: int | None) -> None:
    """Raise ``ToolError`` unless *browser*'s own process is the one we launched.

    *launched_pid* ``None`` (nodriver left it unset) means nothing to compare
    against: logged and allowed, as before F-1009. A browser that cannot answer
    the query is NOT allowed (real Chrome always does; an Electron or CEF app on
    the port does not). Whatever ends the wait, an error or a cancellation, the
    connection to the browser is dropped before it propagates.
    """
    if launched_pid is None:
        debug_logger.log_warning(
            "devtools_port",
            "confirm_ours",
            "no launched pid to compare the DevTools endpoint's process against",
        )
        return
    try:
        await _ask(browser, launched_pid)
    except BaseException:
        await _drop(browser)
        raise
