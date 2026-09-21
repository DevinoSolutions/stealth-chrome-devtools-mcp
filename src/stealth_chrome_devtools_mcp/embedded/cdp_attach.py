"""THE one home for entering a browser that is ALREADY RUNNING, over CDP.

One door, and it has two consumers that arrived at it from opposite directions:
``desktop_launch.launch_and_attach``, which hands a headed launch to the logged-on
desktop and then has to connect to what the Task Scheduler started (F-810), and
``browser_reattach``, which connects to a browser whose backend has died (F-888).
Both needed the same three lines, so they are here once rather than twice — a
second spelling of "how do you get into a running Chrome" is a second spawn path
waiting to happen, and the one in ``desktop_launch`` had already drifted into
carrying a bare ``127.0.0.1`` literal of its own.

**The gate is nodriver's, not ours.** Setting BOTH ``host`` and ``port`` on a
``Config`` is what makes ``uc.start`` connect instead of spawn — ``browser.py``
lines 371-375: with neither set it assigns ``127.0.0.1`` and a free port and
launches Chrome; with both set it takes ``connect_existing`` and never reaches
``create_subprocess_exec``. A pin reads ``desktop_launch``'s source and fails if
``config.host =`` or ``uc.start(`` comes back into it.

**The address, three witnesses in order of trust** (:func:`endpoint`, moved here
from ``browser_reattach`` by F-916):

* the port the RECORD carries (``cdp_port``, written at track time since 2.1.10);
* ``--remote-debugging-port=`` on that pid's command line, which is what nodriver
  passed it (``Config.__call__`` appends the flag from ``config.port``);
* ``<user_data_dir>/DevToolsActivePort``, whose first line is the port Chrome
  actually bound.

The last two exist for entries — and processes — that name no port themselves:
2.1.8/2.1.9 recorded none at all, and those are precisely the browsers carrying
today's stranded logins. The recorded port leads because a record WE wrote is
about THIS instance. The command line comes next because it is definitionally
the live process's, while ``DevToolsActivePort`` is a file that outlives the
browser that wrote it — and measured on the stranded Seller Central Chrome
(pid 115652, ``--remote-debugging-port=9223``) the file was **absent** while the
browser ran, so a file-first ladder would have found nothing. The file still
earns its rung: ``--remote-debugging-port=0`` gives a command line
:func:`browser_pid_registry.valid_port` rejects as "not bound yet", and the file
is where Chrome wrote the port it resolved that to. It lives beside the door
rather than beside the classifier because a door nobody can address is not a
door, and both of ``browser_reattach``'s entry points ask for it.

``CDP_HOST`` is a constant rather than an argument because both launch paths
already fix ``127.0.0.1``: nodriver hard-codes it when it spawns, and the
delegated path spelled the same literal. A browser reachable on some other
interface is not a browser either path produced.

A leaf: ``nodriver``, imported LAZILY inside each function for
``desktop_launch``'s measured reason — the stdio proxy never imports
``browser_manager``, so a module-level nodriver import lands in every proxy's cold
start to serve a path the proxy never takes.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.embedded import browser_cmdline, browser_pid_registry
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nodriver import Browser, Config

# Both launch paths give Chrome this and only this (nodriver ``browser.py:374``
# on the spawn path, the literal ``desktop_launch.launch_and_attach`` used to
# carry on the delegated one), so the endpoint is a port and the host is a fact.
CDP_HOST = "127.0.0.1"

# Chrome writes the port it actually bound here, first line, inside the profile
# it was launched on. The second line is the browser websocket path, which we do
# not use — nodriver builds its own from host and port.
DEVTOOLS_PORT_FILE = "DevToolsActivePort"

# Strong references to the closers `_close_late` starts; see there.
_late_closers: set[asyncio.Task] = set()


def config_for(
    user_data_dir: str | None,
    port: int,
    *,
    headless: bool = False,
    browser_executable_path: str | None = None,
    browser_args: list[str] | None = None,
) -> Config:
    """A nodriver ``Config`` that will CONNECT to a running browser, not spawn one.

    ``user_data_dir`` is carried even though the attach path IGNORES it (as it
    ignores ``browser_args``, which is why a delegated launch puts Chrome's own
    argv on the launcher command line instead), because
    ``browser.config.user_data_dir`` is what the spawn pipeline reads back to
    decide profile cleanup — it must name the directory the browser actually
    launched with, or a persistent profile would read as a nodriver temp dir and
    be deleted.
    """
    import nodriver as uc

    config = uc.Config(
        user_data_dir=user_data_dir,
        headless=headless,
        browser_executable_path=browser_executable_path,
        browser_args=browser_args or [],
    )
    config.host = CDP_HOST
    config.port = port
    return config


async def attach(config: Config, pid: int | None = None) -> Browser:
    """Open the door: connect to the browser *config* points at.

    ``_process_pid`` is stamped when the caller knows it, because nodriver leaves
    it ``None`` on the attach path while teardown's ``os.kill(_process_pid, 15)``
    fallback and ``BrowserManager._browser_process_is_alive`` both read it.
    """
    import nodriver as uc

    browser = await uc.start(config=config)
    if pid is not None:
        browser._process_pid = pid
    return browser


async def attach_reclaiming(config: Config, pid: int) -> Browser:
    """:func:`attach`, but a cancellation cannot strand the connection it opens.

    Cancelling an ``await`` never un-opens a websocket nodriver has already
    connected — the same sentence :mod:`cdp_transport` is built on. A plain
    ``wait_for`` around the door therefore leaves, on timeout, a live connection
    and a live listener task belonging to a ``Browser`` nobody holds a reference
    to any more. So the attach runs as its own TASK behind a shield: the
    cancellation lands on the shield, the task keeps going, and whatever it
    eventually produces is closed by the callback below.
    """
    task = asyncio.ensure_future(attach(config, pid))
    try:
        return await asyncio.shield(task)
    except BaseException:
        if task.done():
            _close_late(task)
        else:
            task.add_done_callback(_close_late)
        raise


def _close_late(task: asyncio.Task) -> None:
    """Close a browser whose attach finished after its caller had given up."""
    if task.cancelled() or task.exception() is not None:
        return
    # The caller is gone and there is nobody left to await this, so the closer is
    # held by the module until it finishes — a bare `ensure_future` is only weakly
    # referenced by the loop and may be collected mid-flight, which is the leak
    # this function exists to close.
    with contextlib.suppress(RuntimeError):
        closer = asyncio.ensure_future(close(task.result()))
        _late_closers.add(closer)
        closer.add_done_callback(_late_closers.discard)


async def close(browser: Browser) -> None:
    """Drop our CDP connections to *browser*, LEAVING THE BROWSER RUNNING.

    The connections, never the process. Every caller here reached a browser it
    did not launch, so an attach that failed or was abandoned must leave that
    Chrome exactly as it found it — deciding it should die is the caller's, and
    on the re-attach path only startup recovery is allowed to make it. That rules
    out nodriver's own ``Browser.stop()``, which disconnects AND terminates.

    ``Connection.disconnect`` and nothing else: it cancels the listener task and
    closes the websocket, which is exactly the half of ``stop()`` we want. There
    is **no ``aclose``** on a nodriver ``Connection`` (checked against 0.47) —
    and because ``Connection.__getattr__`` delegates every unknown attribute to
    ``self.target``, calling one raises ``AttributeError`` rather than failing
    loudly, so a guarded ``aclose()`` is a cleanup that silently does nothing.

    Every TAB is a ``Connection`` too, so a tab we touched on the way to failing
    holds its own websocket and its own listener task; closing the browser
    connection alone leaves those behind.
    """
    closed = 0
    for connection in (getattr(browser, "connection", None), *_tabs(browser)):
        if connection is None:
            continue
        with contextlib.suppress(Exception):
            await connection.disconnect()
            closed += 1
    debug_logger.log_info(
        "cdp_attach",
        "close",
        f"Dropped {closed} CDP connection(s); the browser itself is untouched",
    )


def _tabs(browser: Browser) -> tuple:
    """``browser.tabs``, tolerating a half-built or already-torn-down ``Browser``.

    Reading it is itself fallible — it filters ``self.targets``, which a browser
    whose attach failed part way may not have — and a cleanup path must not fail
    on the state of the thing it is cleaning up.
    """
    try:
        return tuple(browser.tabs or ())
    except Exception as error:  # noqa: BLE001  PERMANENT(see the docstring; the browser connection below is still closed)
        debug_logger.log_debug(
            "cdp_attach",
            "close",
            f"Could not list tabs while closing ({type(error).__name__}); "
            f"closing the browser connection only",
        )
        return ()


def endpoint(entry: browser_pid_registry.Entry) -> int | None:
    """The CDP port of the browser *entry* describes, or None.

    Three witnesses, most trusted first (see the module docstring). Each one is
    re-checked as an int in range rather than trusted: the record tolerates a
    hand edit, a command line is whatever the process says it is, and
    ``DevToolsActivePort`` survives the browser that wrote it.
    """
    recorded = browser_pid_registry.recorded_port(entry)
    if recorded is not None:
        return recorded

    profile_dir = entry.get("user_data_dir")
    expect = profile_dir if isinstance(profile_dir, str) and profile_dir else None

    pid = entry.get("pid")
    if isinstance(pid, int):
        from_cmdline = browser_cmdline.debug_port(pid, expect)
        if from_cmdline is not None:
            return from_cmdline

    if expect is not None:
        return _port_from_profile(Path(expect))
    return None


def _port_from_profile(profile_dir: Path) -> int | None:
    """Chrome's own ``DevToolsActivePort``, first line, or None."""
    try:
        first = (profile_dir / DEVTOOLS_PORT_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = first.splitlines()
    return browser_pid_registry.valid_port(lines[0] if lines else "")
