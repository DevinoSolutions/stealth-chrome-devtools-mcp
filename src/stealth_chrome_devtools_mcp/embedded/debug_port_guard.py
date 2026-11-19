"""THE one home for keeping a page out of Chrome's own DevTools (T-19366).

Two doors lead from a page to every tab of the browser, the sealed login tab
included, and this module is where both are shut.

**Door 1: the debug port.** Chrome is launched with ``--remote-debugging-port=N``.
A page that can speak to that port can attach to ANY tab and read what the vault
typed. The port is no secret from an agent (``chrome://version`` prints the command
line), so two things are made true here:

1. *Chrome must not accept a foreign page's WebSocket.* nodriver launches with
   ``--remote-allow-origins=*``, which accepts ANY Origin. :func:`install` takes
   that flag out of nodriver's defaults for every launch (and ``platform_utils``
   strips every spelling of it from caller ``browser_args``). Without it Chrome
   answers 403 to a handshake whose Origin is not the endpoint's own, and nodriver,
   whose client sends no Origin at all, connects as before.
2. *No page may BE the endpoint's own origin*, the one Origin Chrome still accepts
   (``http://127.0.0.1:N``). Requests to the debug port are refused in two places.
   :func:`refuse_urls` turns a tool call whose URL names it into a ``ToolError`` (a
   clear answer, not the guarantee). :func:`protect` is the guarantee: a second CDP
   connection to the browser auto-attaches, recursively and with
   ``waitForDebuggerOnStart``, to every target that can make requests (a page, a
   cross-site iframe which is its own process, a dedicated, shared or service
   worker), puts a narrow ``Fetch`` interception on it that fails every request to
   ``*://*:N/*`` (any host, so no spelling of loopback gets past it), and only then
   lets it run. The session belongs to this module's own connection, which no tool
   can reach. Measured on Chrome 15x: ``Fetch`` does not see a WebSocket handshake
   (Chrome's Origin check, 1, does that work), ``Fetch.enable`` is refused on a
   dedicated worker (best effort there: the worker's own page and the Origin check
   stand for it), and a frame nested at the endpoint's own origin still COMMITS
   despite the interception. The belt for that last case: a document seen at the
   endpoint's origin is left paused (its script never runs) and the page that holds
   it is sent to ``about:blank``. The login's own tab and its popups get the
   interception but are not watched for children (``login_seal.owns``): a paused
   popup of a watched page holds its opener inside ``window.open`` until the
   login's popup handling has closed it, and the key press that opened it never
   returned (measured). Those pages are the site's, which the vault types into
   anyway.

**Door 2: a DevTools frontend.** ``navigate("chrome://inspect")`` plus a click on
"inspect" makes Chrome open a native DevTools window (a ``devtools://`` page) on
every target, the sealed tab included. Its page script reaches
``InspectorFrontendHost.sendMessageToBackend``, which carries raw CDP: no WebSocket,
no Origin, no port. The invariant stated here: **no target an agent can address is
ever a DevTools frontend or a page that opens one** (:func:`inspector_url`).

* *Refused up front* (:func:`refusal`): ``navigate``, ``new_tab`` and the other URL
  tools refuse ``devtools:``, ``chrome-devtools:`` and the ``chrome://`` pages that
  open an inspector (:data:`INSPECTOR_PAGES`, picked from ``chrome://chrome-urls``).
* *Hidden whatever the route* (``login_seal.bar``): a target whose URL is an
  inspector is barred from ``list_tabs`` and every tool the moment Chrome announces
  it (``targetCreated`` or ``targetInfoChanged``: so a redirect, an in-page
  navigation, ``execute_cdp_command`` and ``go_back`` are covered, the committed URL
  being what is judged).
* *Dealt with*: a page that navigated to ``chrome://inspect`` is sent back to
  ``about:blank``; a ``devtools://`` window is closed while a login is running or
  opening, and otherwise only hidden (a person's own F12 window is not destroyed by
  an agent that never logs in).
* *Checked before typing* (:func:`verify_clean`): the login refuses to start, and
  refuses before EVERY field it types, unless the browser's guard is running and no
  inspector exists; one that will not close refuses the login. A guard whose
  connection dropped while the browser lives fails closed the same way.
* *Read the way Chrome reads a URL* (:func:`_decoded`, :func:`_inspector_host`):
  case, a trailing dot, a port, userinfo, ``about:``, ``view-source:`` and a
  percent-encoded or full-width host (``chrome://%69nspect/#pages``) are all
  ``inspect``. Judging the text as typed was the hole the third review found.
* *Fail closed when the guard ends.* A bar is lifted only by Chrome's own
  ``targetDestroyed``, which only a live guard hears, so a guard whose socket drops
  leaves its bars in place; and a login in flight runs under :func:`watch`, which
  cancels it (its ``finally`` clears what it typed) and raises ``GUARD_DOWN``.

**The class rule while a login runs.** The inspector list is a denylist of pages
that open a debugger; it cannot be complete for a Chrome that has not shipped yet.
So for the duration of a login EVERY ``chrome:``, ``chrome-untrusted:``,
``devtools:``, ``chrome-devtools:``, ``chrome-extension:`` and ``chrome-search:``
target is barred (:func:`internal_url`, ``login_seal.mark_internal``) and the URL
tools refuse to load one (:func:`refusal`); ``about:blank`` and the new-tab page
still work. Outside a login only the narrow refusal applies, so ordinary
``chrome://`` use is untouched. A login closes every DevTools window open in its
browser, counted by :func:`closed_windows` and reported by ``login``.

**Vault mode (T-19366, ``vault_mode``).** While a login vault exists the guard also
keeps two things true of EVERY browser it protects, spawned or re-attached (both go
through :func:`protect`, before anyone is handed the browser), and re-asserts them
every :data:`KEEP_SECONDS` so a vault created after the browser was started, or a
download folder an agent set before it existed, is taken back within that time:

* *no page of a browser reads a local file*: a target at a ``file:`` URL (or
  ``view-source:file:``, case, tabs and a drive path spelling included) is barred
  from the tools and blanked exactly like an inspector (:func:`barred_url`), by
  whichever route it got there (``navigate``, a redirect, ``go_back``);
* *downloads go to one folder under generated names*: ``Browser.setDownloadBehavior``
  is sent to the browser with :func:`vault_mode.download_pin` (``allowAndName``,
  ``~/Downloads/stealth-mcp-downloads``, or ``deny``). No tool can change it back:
  ``execute_cdp_command`` is refused in the mode. When the mode ends the guard sends
  ``default``.

**What is NOT covered**: a process running as the same OS user, which can open the
loopback port directly (``RUNBOOK`` "Local trust boundary"); a browser started by an
older backend with ``--remote-allow-origins=*`` (covered by 2 only); an inspector
that is open while no login runs and is never closed (it is hidden, not destroyed);
and any ``chrome://`` page that a later Chrome adds with a debugger action.
Chrome does not say how many clients are attached to a target, so nothing here can
tell that another client attached.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import re
import unicodedata
import urllib.parse
from typing import TYPE_CHECKING, cast

from nodriver import cdp

from stealth_chrome_devtools_mcp.embedded import login_seal, tab_open, vault_mode
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine

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
#: The running guard of each protected port (what ``protected_ports`` stands on).
_GUARDS: dict[int, _Guard] = {}
_TASKS: set[asyncio.Task[None]] = set()
#: DevTools windows this module has closed (by id), per browser (debug port), for
#: the answer of ``login``: another browser's windows are not this login's.
_CLOSED: dict[int, set[str]] = {}
_COUNTER = itertools.count(1)

#: How often a guard re-asserts vault mode (the download pin, no ``file:`` page).
KEEP_SECONDS = 5.0
CONNECT_BUDGET_SECONDS = 15.0
COMMAND_BUDGET_SECONDS = 10.0
REFUSAL = (
    "that address is this browser's own DevTools port, which no page may load: "
    "it would let a page attach to every tab"
)
INSPECTOR_REFUSAL = (
    "that address opens a browser inspector (DevTools), which would let a page "
    "read every tab; it is refused"
)
GUARD_DOWN = (
    "this browser's DevTools guard is not running, so the browser cannot be shown "
    "free of inspectors; vault logins are refused in it"
)
INSPECTOR_OPEN = (
    "a browser inspector (a DevTools window) is open in this browser and could not "
    "be closed; nothing was typed"
)

#: Schemes whose pages ARE a DevTools frontend (``devtools://devtools/bundled/...``).
INSPECTOR_SCHEMES = frozenset({"devtools", "chrome-devtools"})
#: ``chrome://`` pages that open an inspector or carry a debugger channel, picked
#: from ``chrome://chrome-urls`` of Chrome 158 (about 95 pages; the rest show
#: information or settings and have no debugger action):
#:
#: * ``inspect``: lists every target (tab, worker, extension) with an "inspect"
#:   link that opens DevTools on it, and its page script can call ``chrome.send``;
#: * ``serviceworker-internals``: an "Inspect" link per service worker;
#: * ``extensions``: "Inspect views" for an extension in developer mode.
#:
#: Chrome reads ``about:inspect`` and ``about://inspect`` as ``chrome://inspect``.
INSPECTOR_PAGES = frozenset({"inspect", "serviceworker-internals", "extensions"})
#: While a login runs, EVERY target of these schemes is hidden from the tools and no
#: URL tool may load one (:func:`internal_url`): a class rule, not a list of pages,
#: because every ``chrome://`` WebUI page can ``chrome.send`` to its browser-side
#: handlers and "has no debugger action" would have to be re-proved per Chrome
#: release. Outside a login only :data:`INSPECTOR_PAGES` is refused.
INTERNAL_SCHEMES = frozenset(
    {
        "chrome",
        "chrome-untrusted",
        "devtools",
        "chrome-devtools",
        "chrome-extension",
        "chrome-search",
    }
)
#: The new-tab page stays usable during a login (it is what a fresh tab shows).
NEW_TAB_HOSTS = frozenset(
    {"newtab", "new-tab-page", "new-tab-page-third-party", "local-ntp"}
)
INTERNAL_REFUSAL = (
    "a vault login is running in this browser: chrome:// and other browser-internal "
    "pages cannot be opened until it finishes"
)
#: What a URL parser drops before it reads a scheme: C0 controls and space at both
#: ends, tab and newlines anywhere (the WHATWG URL rules Chrome follows).
_URL_EDGE = "".join(chr(code) for code in range(33))
_URL_INNER = dict.fromkeys((9, 10, 13))
_DECODE_ROUNDS = 4
#: UTS 46 (what Chrome applies to a host) maps these to a full stop; NFKC maps only
#: the full-width one, so the ideographic and half-width ones are listed.
_DOTS = {0x3002: ".", 0xFF61: ".", 0xFF0E: ".", 0x2024: "."}
#: Code points UTS 46 DROPS from a host and NFKC keeps (soft hyphen, zero-width and
#: format characters are category Cf; these are the others: the combining grapheme
#: joiner, Hangul fillers, Mongolian free variation selectors, variation selectors).
_IGNORED = frozenset(
    {0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x3164, 0xFFA0}
    | set(range(0x180B, 0x180E))
    | set(range(0xFE00, 0xFE10))
    | set(range(0xE0100, 0xE01F0))
)
#: Types of target that can make requests, each given the port interception.
GUARDED_TYPES = frozenset(
    {"page", "iframe", "worker", "service_worker", "shared_worker"}
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


def _mapped(text: str) -> str:
    """UTS 46's mapping of a host, as far as it matters here: ignorable code points
    (a soft hyphen inside ``inspect``) dropped, every full-stop form a full stop,
    then NFKC (full-width letters). Applied twice: a mapping can expose another."""
    for _ in range(2):
        text = "".join(
            char
            for char in text
            if ord(char) not in _IGNORED and unicodedata.category(char) != "Cf"
        )
        text = unicodedata.normalize("NFKC", text).translate(_DOTS)
    return text


def _decoded(text: str) -> str:
    """``text`` percent-decoded as far as it goes and mapped the way Chrome maps a
    host (:func:`_mapped`), which is what its canonicalisation of a ``chrome:`` URL
    amounts to: ``%69nspect``, ``i<soft hyphen>nspect``, a full-width spelling and
    ``inspect<ideographic full stop>`` are all ``inspect``. Decoding to a fixed
    point reads MORE spellings as the same page than Chrome does: the safe side."""
    for _ in range(_DECODE_ROUNDS):
        decoded = urllib.parse.unquote(text)
        if decoded == text:
            break
        text = decoded
    return _mapped(text)


def _inspector_host(url: str) -> tuple[str, str]:
    """``(scheme, host)`` of ``url`` as Chrome reads it, both lower case; the host
    of ``chrome:inspect`` and ``about:inspect`` is read from the path. Userinfo and
    a port are not part of the host, a trailing dot and the case are dropped."""
    text = url.translate(_URL_INNER).strip(_URL_EDGE).replace("\\", "/")
    parts = urllib.parse.urlsplit(text)
    scheme = parts.scheme.lower()
    host = parts.hostname or parts.path.lstrip("/").split("/")[0].split(":")[0]
    return scheme, _decoded(host).lower().rstrip(".")


def is_frontend(url: object) -> bool:
    """Whether ``url`` is a DevTools frontend page (``devtools://``)."""
    if not isinstance(url, str):
        return False
    try:
        return _inspector_host(url)[0] in INSPECTOR_SCHEMES
    except ValueError:
        return False


def inspector_url(url: object) -> bool:
    """Whether a page at ``url`` is a browser inspector: a DevTools frontend or a
    ``chrome://`` page that opens one (:data:`INSPECTOR_PAGES`). The committed URL
    of a target is what is asked, so a redirect or an in-page navigation to one
    counts; so does any spelling Chrome accepts (case, ``about:``, a tab or
    newline inside the URL, ``view-source:``)."""
    if not isinstance(url, str):
        return False
    try:
        scheme, host = _inspector_host(url)
        if scheme == "view-source":
            return inspector_url(url.strip(_URL_EDGE)[len("view-source:") :])
    except ValueError:
        return False
    if scheme in INSPECTOR_SCHEMES:
        return True
    return scheme in ("chrome", "about") and host in INSPECTOR_PAGES


_DRIVE_PATH = re.compile(r"[A-Za-z]:[\\/]")


def file_url(url: object) -> bool:
    """Whether ``url`` reads a local file: the ``file`` scheme as Chrome reads it
    (case, a tab or newline inside, leading control characters, ``view-source:``), or
    a Windows drive or UNC path written where a URL goes. A URL this parser cannot
    read but that starts with the scheme counts too (the safe error)."""
    if not isinstance(url, str):
        return False
    text = url.translate(_URL_INNER).strip(_URL_EDGE)
    if _DRIVE_PATH.match(text) or text.startswith("\\\\"):
        return True
    try:
        scheme, _host = _inspector_host(url)
    except ValueError:
        return text.lower().startswith(("file:", "view-source:file:"))
    if scheme == "view-source":
        return file_url(text[len("view-source:") :])
    return scheme == "file"


def barred_url(url: object) -> bool:
    """Whether a target at ``url`` is kept from every tool: an inspector, always, and
    a ``file:`` page while vault mode is on."""
    return inspector_url(url) or (file_url(url) and vault_mode.active())


def internal_url(url: object) -> bool:
    """Whether ``url`` is a browser-internal page of the class
    :data:`INTERNAL_SCHEMES` (the new-tab page excepted). ``view-source:`` is read
    through; ``about:`` pages are not of the class (``about:blank`` is one)."""
    if not isinstance(url, str):
        return False
    try:
        scheme, host = _inspector_host(url)
        if scheme == "view-source":
            return internal_url(url.strip(_URL_EDGE)[len("view-source:") :])
    except ValueError:
        return False
    if scheme not in INTERNAL_SCHEMES:
        return False
    return not (scheme != "chrome-extension" and host in NEW_TAB_HOSTS)


def refusal(url: object, instance_id: object = None) -> str | None:
    """Why ``url`` may not be loaded, or ``None``.

    Refused when it is a browser inspector (:func:`inspector_url`), when it is a
    ``file:`` URL while vault mode is on (:func:`file_url`), when a login is
    running in ``instance_id`` (any instance when the call names none) and it is a
    browser-internal page (:func:`internal_url`), or when its
    port is the debug port of any browser of this backend, WHATEVER the host:
    ``127.0.0.1``, ``localhost``, ``localhost.``, ``[::1]``, ``0.0.0.0``,
    ``2130706433``, ``0x7f.1`` and ``0177.0.0.1`` all name the same listener, so
    the port, not the spelling, is what is checked."""
    if inspector_url(url):
        return INSPECTOR_REFUSAL
    if file_url(url) and vault_mode.active():
        return vault_mode.file_url_refusal()
    instance = instance_id if isinstance(instance_id, str) else None
    if login_seal.login_in_flight(instance) and internal_url(url):
        return INTERNAL_REFUSAL
    port = url_port(url)
    return REFUSAL if port is not None and port in _PORTS else None


def refuse_urls(tool: str, arguments: dict[str, object]) -> None:
    """Raise ``ToolError`` when a URL-loading tool is asked for the debug port or
    for an inspector."""
    name = URL_ARGUMENTS.get(tool)
    if name is None:
        return
    reason = refusal(arguments.get(name), arguments.get("instance_id"))
    if reason is not None:
        raise ToolError(reason)


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
    _GUARDS[int(port)] = guard
    task = asyncio.create_task(guard.run(), name=f"debug-port-guard-{port}")
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def _obj(value: object) -> Json:
    """``value`` when it is a JSON object, else an empty one."""
    return cast("Json", value) if isinstance(value, dict) else {}


class _Guard:
    """A tiny CDP client of its own: one browser-level websocket, a session per
    target that can make requests, and nothing else. Its own client (not
    nodriver's) because it must read the ``sessionId`` of every event, which
    nodriver does not hand to a handler."""

    def __init__(self, url: str, port: int) -> None:
        self.url = url
        self.port = port
        self.socket: ClientConnection | None = None
        self.reader: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[Json]] = {}
        self.guarded: set[str] = set()
        self.tasks: set[asyncio.Task[None]] = set()
        #: target id -> our session on it; session -> its parent session.
        self.sessions: dict[str, str] = {}
        self.parents: dict[str, str] = {}
        #: Targets first seen at an ordinary URL, and the inspectors seen so far.
        self.clean: set[str] = set()
        self.inspectors: set[str] = set()
        #: Resolved when this guard ends (:func:`watch` waits on it).
        self.done: asyncio.Future[None] | None = None
        #: Whether this browser's downloads are pinned by vault mode, and the task
        #: that re-asserts the pin and the ``file:`` bar every KEEP_SECONDS.
        self.pinned = False
        self.keeper: asyncio.Task[None] | None = None

    async def start(self) -> None:
        import websockets

        self.socket = await websockets.connect(
            self.url, max_size=None, ping_interval=None, open_timeout=10
        )
        self.done = asyncio.get_running_loop().create_future()
        self.reader = asyncio.create_task(self._read())
        await self.command("Target.setDiscoverTargets", {"discover": True})
        await self._auto_attach(None)
        found = await self.command("Target.getTargets")
        for info in cast("list[Json]", found.get("targetInfos", [])):
            self._judge(info)
            if info.get("type") in GUARDED_TYPES:
                with contextlib.suppress(Exception):
                    await self.command(
                        "Target.attachToTarget",
                        {"targetId": info["targetId"], "flatten": True},
                    )
        # The attach events are handled in tasks: wait until every target that was
        # already open has its interception before the browser is handed on.
        await self.idle()
        # Vault mode before the browser is handed on: a pin that cannot be made
        # fails the guard, and with it the spawn or the re-attach.
        await self.keep()
        self.keeper = asyncio.create_task(self._keep_loop())

    async def keep(self) -> None:
        """Make vault mode true of this browser, or lift what it set (one pass).

        Outside the mode this is one ``stat`` and, when a pin was set before, one
        ``Browser.setDownloadBehavior`` back to ``default``. Inside it: the pin, then
        every open target is judged again, so a ``file:`` page that was already open
        when the vault appeared is barred and blanked, not left readable."""
        pin = await asyncio.to_thread(vault_mode.download_pin)
        if pin is None:
            if self.pinned:
                await self.command(
                    "Browser.setDownloadBehavior", {"behavior": "default"}
                )
                self.pinned = False
            return
        await self.command("Browser.setDownloadBehavior", pin)
        self.pinned = True
        found = await self.command("Target.getTargets")
        for info in cast("list[Json]", found.get("targetInfos", [])):
            self._judge(info)

    async def _keep_loop(self) -> None:
        while True:
            await asyncio.sleep(KEEP_SECONDS)
            try:
                await self.keep()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001  PERMANENT(a failed pass is tried again; the guard ends with its socket)
                _logger.warning("vault mode could not be kept for %s", self.port)

    async def idle(self) -> None:
        """Return once no handler task is running. Not ``gather`` over
        ``self.tasks``: a task that is done but whose done-callback has not run is
        still in the set, ``gather`` of done tasks never yields, and the loop then
        starves the very callbacks that would empty the set (measured: a spawn that
        hung for minutes)."""
        while True:
            running = [task for task in self.tasks if not task.done()]
            if not running:
                return
            await asyncio.wait(running)

    async def _auto_attach(self, session: str | None) -> None:
        """Attach to every target ``session`` (the browser when ``None``) starts
        from now on, paused until this guard has put its interception on it, so
        a cross-site iframe or a worker is guarded like the page that made it."""
        await self.command(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
            session,
        )

    async def stop(self) -> None:
        if self.keeper is not None:
            self.keeper.cancel()
        if self.done is not None and not self.done.done():
            self.done.set_result(None)
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
            if _GUARDS.get(self.port) is self:
                del _GUARDS[self.port]
            # Nothing is un-barred here: a guard that is gone is exactly when a
            # DevTools window must stay out of reach (it is un-barred when Chrome
            # says the target is destroyed, which only a live guard hears).
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
            params = _obj(message.get("params"))
            if method == "Target.attachedToTarget":
                parent = message.get("sessionId")
                self._spawn(
                    self._attached(params, parent if isinstance(parent, str) else None)
                )
            elif method == "Fetch.requestPaused":
                self._spawn(self._block(message))
            elif method in ("Target.targetCreated", "Target.targetInfoChanged"):
                self._judge(_obj(params.get("targetInfo")))
            elif method == "Target.targetDestroyed":
                self._forget(str(params.get("targetId", "")))

    def _spawn(self, work: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(work)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _forget(self, target: str) -> None:
        self.clean.discard(target)
        self.guarded.discard(target)
        session = self.sessions.pop(target, None)
        if session is not None:
            self.parents.pop(session, None)
        login_seal.mark_internal(target, False)
        if target in self.inspectors:
            self.inspectors.discard(target)
            login_seal.unbar(target)

    def _judge(self, info: Json) -> None:
        """A target was created or changed address. An inspector (a DevTools
        window, ``chrome://inspect``) is barred from the tools at once, from the
        event itself; then it is dealt with:

        * a ``devtools://`` page Chrome made on its own is closed while a login
          is running or opening, and otherwise only hidden (a person's own F12
          window outlives an agent that never logs in);
        * a page that NAVIGATED to an inspector (an agent tab sent to
          ``chrome://inspect``) is sent back to ``about:blank`` at once: it may be
          the tab a tool without a ``tab_id`` acts on."""
        target = str(info.get("targetId", ""))
        if not target:
            return
        if not barred_url(info.get("url")):
            self.clean.add(target)
            login_seal.mark_internal(target, internal_url(info.get("url")))
            if target in self.inspectors:  # it was blanked: no longer one
                self.inspectors.discard(target)
                login_seal.unbar(target)
            if info.get("type") in GUARDED_TYPES and (
                url_port(info.get("url")) == self.port
            ):
                self._spawn(self._leave_endpoint(target))
            return
        first_sight = target not in self.clean
        login_seal.bar(target)
        self.inspectors.add(target)
        if is_frontend(info.get("url")) and first_sight:
            if login_seal.login_in_flight_on(self.port):
                self._spawn(self._deal(target, close=True))
            return
        self._spawn(self._deal(target, close=False))

    async def _deal(self, target: str, *, close: bool) -> None:
        """Blank ``target`` (or close it): the page is gone from the agent's reach
        either way."""
        session = self.sessions.get(target)
        if not close and session is not None:
            with contextlib.suppress(Exception):
                await self.command("Page.navigate", {"url": "about:blank"}, session)
                return
        with contextlib.suppress(Exception):
            await self.command("Target.closeTarget", {"targetId": target})
            if close:
                _CLOSED.setdefault(self.port, set()).add(target)

    async def _leave_endpoint(self, target: str) -> None:
        """A document of ``target`` committed at the endpoint's own origin (the one
        Origin Chrome still accepts): send the page that holds it to
        ``about:blank``."""
        session = self.sessions.get(target)
        while session is not None and session in self.parents:
            session = self.parents[session]
        if session is None:
            return
        _logger.warning("a page reached the DevTools port; blanking it")
        with contextlib.suppress(Exception):
            await self.command("Page.navigate", {"url": "about:blank"}, session)

    async def _attached(self, params: Json, parent: str | None = None) -> None:
        """A target arrived. A target that can make requests gets the interception
        BEFORE it may run, and is watched for the children it starts in turn (a
        cross-site iframe is its own process and its own target); a page that
        cannot be given it is closed rather than left unguarded."""
        session = cast("str", params["sessionId"])
        info = _obj(params.get("targetInfo"))
        target = str(info.get("targetId", ""))
        kind = info.get("type")
        self.sessions[target] = session
        if parent is not None:
            self.parents[session] = parent
        self._judge(info)
        if kind in GUARDED_TYPES and url_port(info.get("url")) == self.port:
            # A frame at the endpoint's own origin (``_judge`` above has sent its
            # page to about:blank): it is left PAUSED, never resumed, so none of
            # its script runs before that ends it.
            return
        guarded = kind in GUARDED_TYPES and target not in self.guarded
        # The interception and the watch for children are SENT together (Chrome
        # answers them in order), then the target is released: two round trips, as
        # before the watch for children existed. A third, one after another, lost
        # a race (measured): a paused popup whose opener is blocked inside
        # ``window.open`` (a key handler) is closed by the login's own popup
        # handling within milliseconds, and the opener's key press never returned.
        if guarded:
            self.guarded.add(target)
            guards = [
                self.command("Fetch.enable", {"patterns": patterns(self.port)}, session)
            ]
            # The login's own tab, and a popup it opens, are not watched for
            # children: a paused popup of a page that has this watch holds the
            # opener inside ``window.open`` (a key handler) until the login's own
            # popup handling has closed it, and the key press never returned
            # (measured). Those pages are the SITE's, which the vault types into
            # anyway; an agent's pages keep the watch.
            watch = kind in ("page", "iframe", "worker") and not (
                kind == "page" and login_seal.owns(target, info.get("openerId"))
            )
            if watch:
                guards.append(self._auto_attach(session))
            answers = await asyncio.gather(*guards, return_exceptions=True)
            if any(isinstance(answer, Exception) for answer in answers):
                if kind == "page":
                    _logger.warning("a page could not be guarded; closing it")
                    with contextlib.suppress(Exception):
                        await self.command("Target.closeTarget", {"targetId": target})
                    return
                if kind == "iframe":
                    _logger.warning("a frame could not be guarded; blanking its page")
                    self._spawn(self._leave_endpoint(target))
                    return
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


# --------------------------------------------------------- before a login types

_CLEAN_ATTEMPTS = 3
_CLEAN_RETRY_S = 0.2


async def verify_clean(browser: Browser) -> None:
    """Raise ``ToolError`` unless ``browser`` is guarded and holds no inspector.

    Called before the login opens its tab and before EVERY field it types, so a
    DevTools window that was already open (a headed person's F12, one an agent made
    earlier through ``execute_cdp_command``) is closed before anything is typed,
    and a browser whose guard has ended (its connection dropped while the browser
    lives) refuses instead of carrying on unguarded. A window that stays open
    after three tries refuses the login."""
    port = getattr(getattr(browser, "config", None), "port", None)
    if not port or int(port) not in _PORTS:
        raise ToolError(GUARD_DOWN)
    for attempt in range(_CLEAN_ATTEMPTS):
        infos = await tab_open.send(browser, cdp.target.get_targets)
        bad = [i for i in infos if inspector_url(i.url)]
        if not bad:
            return
        for info in bad:
            login_seal.bar(str(info.target_id))
            with contextlib.suppress(Exception):
                await tab_open.send(browser, cdp.target.close_target, info.target_id)
                if is_frontend(info.url):
                    _CLOSED.setdefault(int(port), set()).add(str(info.target_id))
        if attempt < _CLEAN_ATTEMPTS - 1:
            await asyncio.sleep(_CLEAN_RETRY_S)
    raise ToolError(INSPECTOR_OPEN)


def closed_windows(browser: Browser) -> int:
    """How many DevTools windows this process has closed so far in ``browser``
    (its debug port): a login reports the difference between its end and its start,
    and never another browser's windows."""
    port = getattr(getattr(browser, "config", None), "port", None)
    return len(_CLOSED.get(int(port), ())) if port else 0


@contextlib.asynccontextmanager
async def watch(browser: Browser) -> AsyncIterator[None]:
    """Run a login under the guard of its browser. When the guard ends (its
    connection dropped while the browser lives) the surrounding task is cancelled
    and the block raises ``ToolError`` (:data:`GUARD_DOWN`), so the login's own
    cleanup clears the fields it typed and closes its tab. Raises at once when
    there is no guard."""
    port = getattr(getattr(browser, "config", None), "port", None)
    guard = _GUARDS.get(int(port)) if port else None
    task = asyncio.current_task()
    if guard is None or guard.done is None or task is None:
        raise ToolError(GUARD_DOWN)
    outside = task.cancelling()
    fired = False

    def ended(_future: asyncio.Future[None]) -> None:
        nonlocal fired
        fired = True
        task.cancel()

    guard.done.add_done_callback(ended)
    try:
        yield
    except asyncio.CancelledError:
        if fired and task.uncancel() <= outside:
            raise ToolError(GUARD_DOWN) from None
        raise
    finally:
        guard.done.remove_done_callback(ended)
