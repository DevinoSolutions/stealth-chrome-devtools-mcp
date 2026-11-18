"""THE one home for keeping a page out of Chrome's own DevTools endpoint (T-19366).

Chrome is launched with ``--remote-debugging-port=N``. A page that can speak to
that port can attach to ANY tab of the browser, including the backend's sealed
login tab, and read what the vault typed: the whole of ``login_seal`` is bypassed
by a WebSocket from page script. The port is no secret from an agent
(``chrome://version`` prints the command line), so two things have to be true, and
this module is where both are made so:

1. **Chrome must not accept a foreign page's WebSocket.** nodriver launches with
   ``--remote-allow-origins=*``, which accepts ANY Origin. :func:`install` takes
   that flag out of nodriver's defaults for every launch (and
   ``platform_utils`` strips it from caller ``browser_args``). Without it Chrome
   answers 403 to a handshake whose Origin is not the endpoint's own, and
   nodriver, whose client sends no Origin at all, connects as before (measured:
   the whole spawn path works without the flag, see
   ``tests/test_debug_port_guard.py``).
2. **No page may BE the endpoint's own origin**, the one Origin Chrome still
   accepts (``http://127.0.0.1:N``): a tab navigated to
   ``http://127.0.0.1:N/json/version`` can open the socket. So requests to the
   debug port are refused in two places. :func:`refuse_urls` turns a tool call
   whose URL names it into a ``ToolError`` (a clear answer, not the guarantee).
   :func:`protect` is the guarantee: a second CDP connection to the browser
   auto-attaches to every new page BEFORE it can run (``waitForDebuggerOnStart``),
   puts a narrow ``Fetch`` interception on that session that fails every request
   to ``*://*:N/*`` (any host, so a spelling of loopback cannot get past it), and
   only then lets the page run. It covers navigations, redirects, iframes,
   ``window.open`` popups, ``fetch`` and XHR, for every page, internal or not. The
   session belongs to this module's own connection, which no tool can reach, so a
   page's own ``execute_cdp_command`` session cannot undo it.

**What blocks a WebSocket handshake.** Measured on Chrome 15x with
``tests/test_debug_port_guard.py``: neither ``Network.setBlockedURLs`` nor a
``Fetch`` pattern sees a WebSocket handshake. So the WebSocket itself is not
blocked; it is refused by Chrome's Origin check (1), and (2) denies the one
origin that check lets through.

**What is NOT covered**: a process running as the same OS user, which can open the
loopback port directly (``RUNBOOK`` "Local trust boundary"). A browser started
by an older backend, with ``--remote-allow-origins=*``, is covered by (2) only.
Chrome does not say how many clients are attached to a target (``attached`` is
true for our own session), so nothing here can tell that another client
attached.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import urllib.parse
from typing import TYPE_CHECKING, cast

from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from nodriver import Browser
    from websockets.asyncio.client import ClientConnection

#: A CDP message or result: JSON objects, read by key.
Json = dict[str, object]

_logger = logging.getLogger("stealth.debug_port_guard")

#: nodriver's default launch argument that makes Chrome accept any Origin.
ALLOW_ORIGINS_FLAG = "--remote-allow-origins=*"
_MARKER = "_debug_port_guard_original"

#: Tools that load a URL, and the argument that carries it.
URL_ARGUMENTS = {
    "navigate": "url",
    "new_tab": "url",
    "create_simple_dynamic_hook": "target_url",
}
_WEB_SCHEMES = {"http": 80, "https": 443, "ws": 80, "wss": 443}
#: Every port of a browser this process has protected (an instance's debug port).
_PORTS: set[int] = set()
_TASKS: set[asyncio.Task[None]] = set()
_COUNTER = itertools.count(1)

CONNECT_BUDGET_SECONDS = 15.0
COMMAND_BUDGET_SECONDS = 10.0
REFUSAL = (
    "that address is this browser's own DevTools port, which no page may load: "
    "it would let a page attach to every tab"
)


def install() -> None:
    """Make every nodriver launch without ``--remote-allow-origins=*``. Idempotent.

    The one place the launch defaults are changed: both ``uc.Config`` call sites
    (``browser_manager`` and ``cdp_attach.config_for``, which the delegated
    desktop launch derives its argv from) construct the same class."""
    from nodriver.core import config as nodriver_config

    config = nodriver_config.Config
    if hasattr(config.__init__, _MARKER):
        return
    original = cast("Callable[..., None]", config.__init__)

    def init(self: object, *args: object, **kwargs: object) -> None:
        original(self, *args, **kwargs)
        defaults = cast("list[str]", getattr(self, "_default_browser_args"))  # noqa: B009  PERMANENT(a private nodriver list, read by name)
        kept = [arg for arg in defaults if arg != ALLOW_ORIGINS_FLAG]
        vars(self)["_default_browser_args"] = kept

    setattr(init, _MARKER, original)
    config.__init__ = init  # ty: ignore[invalid-assignment]


def url_port(url: object) -> int | None:
    """The port a web URL would connect to, or ``None`` for anything else (not a
    string, not http/https/ws/wss, or not parseable). Chrome's own reading: the
    scheme's default port when none is written, leading zeros allowed."""
    if not isinstance(url, str):
        return None
    try:
        parts = urllib.parse.urlsplit(url.strip())
        scheme = parts.scheme.lower()
        if scheme not in _WEB_SCHEMES:
            return None
        return parts.port or _WEB_SCHEMES[scheme]
    except ValueError:
        return None


def refusal(url: object) -> str | None:
    """Why ``url`` may not be loaded, or ``None``.

    Refused when its port is the debug port of any browser of this backend,
    WHATEVER the host: ``127.0.0.1``, ``localhost``, ``localhost.``, ``[::1]``,
    ``0.0.0.0``, ``2130706433``, ``0x7f.1`` and ``0177.0.0.1`` all name the same
    listener, so the port, not the spelling, is what is checked."""
    port = url_port(url)
    return REFUSAL if port is not None and port in _PORTS else None


def refuse_urls(tool: str, arguments: dict[str, object]) -> None:
    """Raise ``ToolError`` when a URL-loading tool is asked for the debug port."""
    name = URL_ARGUMENTS.get(tool)
    if name is not None and _PORTS and refusal(arguments.get(name)) is not None:
        raise ToolError(REFUSAL)


def patterns(port: int) -> list[Json]:
    """The ``Fetch.enable`` patterns that match every request to ``port``."""
    return [{"urlPattern": f"*://*:{port}/*"}, {"urlPattern": f"*://*:{port}"}]


def protected_ports() -> frozenset[int]:
    return frozenset(_PORTS)


async def protect(browser: Browser) -> None:
    """Put the guard on ``browser``: every page, existing and future. Raises
    ``ToolError`` when it cannot be put there (a browser that could not be
    guarded must not be handed to an agent)."""
    port = getattr(getattr(browser, "config", None), "port", None)
    url = getattr(browser, "websocket_url", None) or getattr(
        getattr(browser, "info", None), "webSocketDebuggerUrl", None
    )
    if not port or not url:
        raise ToolError(
            "the browser's DevTools address is unknown, so its debug port cannot "
            "be guarded"
        )
    _PORTS.add(int(port))
    guard = _Guard(str(url), int(port))
    try:
        async with asyncio.timeout(CONNECT_BUDGET_SECONDS):
            await guard.start()
    except Exception as error:  # noqa: BLE001  PERMANENT(any failure to guard is reported as one refusal, the class only)
        _PORTS.discard(int(port))
        await guard.stop()
        raise ToolError(
            f"the browser's debug port could not be guarded ({type(error).__name__})"
        ) from None
    task = asyncio.create_task(guard.run(), name=f"debug-port-guard-{port}")
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def _obj(value: object) -> Json:
    """``value`` when it is a JSON object, else an empty one."""
    return cast("Json", value) if isinstance(value, dict) else {}


class _Guard:
    """A tiny CDP client of its own: one browser-level websocket, a session per
    page, and nothing else. Its own client (not nodriver's) because it must read
    the ``sessionId`` of every event, which nodriver does not hand to a handler."""

    def __init__(self, url: str, port: int) -> None:
        self.url = url
        self.port = port
        self.socket: ClientConnection | None = None
        self.reader: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[Json]] = {}
        self.guarded: set[str] = set()
        self.tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        import websockets

        self.socket = await websockets.connect(
            self.url, max_size=None, ping_interval=None, open_timeout=10
        )
        self.reader = asyncio.create_task(self._read())
        await self.command(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
        )
        found = await self.command("Target.getTargets")
        for info in cast("list[Json]", found.get("targetInfos", [])):
            if info.get("type") == "page":
                await self.command(
                    "Target.attachToTarget",
                    {"targetId": info["targetId"], "flatten": True},
                )
        # The attach events are handled in tasks: wait until every page that was
        # already open has its interception before the browser is handed on.
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks))

    async def stop(self) -> None:
        if self.reader is not None:
            self.reader.cancel()
        if self.socket is not None:
            with contextlib.suppress(Exception):
                await self.socket.close()
        for future in self.pending.values():
            if not future.done():
                future.cancel()

    async def run(self) -> None:
        """Live until the browser's socket closes (the browser went away)."""
        try:
            if self.reader is not None:
                await self.reader
        except (asyncio.CancelledError, Exception):  # noqa: BLE001  PERMANENT(the browser closing ends the guard)
            _logger.debug("debug port guard for %s ended", self.port)
        finally:
            _PORTS.discard(self.port)
            await self.stop()

    async def command(
        self,
        method: str,
        params: Json | None = None,
        session: str | None = None,
    ) -> Json:
        number = next(_COUNTER)
        message: Json = {"id": number, "method": method}
        if params:
            message["params"] = params
        if session:
            message["sessionId"] = session
        future: asyncio.Future[Json] = asyncio.get_running_loop().create_future()
        self.pending[number] = future
        try:
            if self.socket is None:
                raise RuntimeError("not connected")
            await self.socket.send(json.dumps(message))
            async with asyncio.timeout(COMMAND_BUDGET_SECONDS):
                reply = await future
        finally:
            self.pending.pop(number, None)
        if "error" in reply:
            raise RuntimeError(f"{method} refused")
        return _obj(reply.get("result"))

    async def _read(self) -> None:
        if self.socket is None:
            return
        async for raw in self.socket:
            message = _obj(json.loads(raw))
            if "id" in message:
                future = self.pending.get(cast("int", message["id"]))
                if future is not None and not future.done():
                    future.set_result(message)
                continue
            method = message.get("method")
            if method == "Target.attachedToTarget":
                self._spawn(self._attached(_obj(message.get("params"))))
            elif method == "Fetch.requestPaused":
                self._spawn(self._block(message))

    def _spawn(self, work: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(work)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _attached(self, params: Json) -> None:
        """A target arrived. A page gets the interception BEFORE it may run; a
        page that cannot be given it is closed rather than left unguarded."""
        session = cast("str", params["sessionId"])
        info = _obj(params.get("targetInfo"))
        if info.get("type") == "page":
            target = str(info.get("targetId", ""))
            if target not in self.guarded:
                self.guarded.add(target)
                try:
                    await self.command(
                        "Fetch.enable", {"patterns": patterns(self.port)}, session
                    )
                except Exception:  # noqa: BLE001  PERMANENT(an unguardable page is closed, never left running unguarded)
                    _logger.warning("a page could not be guarded; closing it")
                    with contextlib.suppress(Exception):
                        await self.command("Target.closeTarget", {"targetId": target})
                    return
            if url_port(info.get("url")) == self.port:
                # A page already AT the endpoint's origin (a browser an older
                # backend started): its document could still open the socket.
                with contextlib.suppress(Exception):
                    await self.command("Page.navigate", {"url": "about:blank"}, session)
        if params.get("waitingForDebugger"):
            with contextlib.suppress(Exception):
                await self.command("Runtime.runIfWaitingForDebugger", None, session)

    async def _block(self, message: Json) -> None:
        with contextlib.suppress(Exception):
            await self.command(
                "Fetch.failRequest",
                {
                    "requestId": _obj(message.get("params"))["requestId"],
                    "errorReason": "BlockedByClient",
                },
                cast("str | None", message.get("sessionId")),
            )
