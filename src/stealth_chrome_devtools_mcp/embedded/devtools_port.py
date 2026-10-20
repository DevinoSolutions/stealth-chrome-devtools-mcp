"""F-1009 — THE one home for "which DevTools port is MINE": asking Chrome for
port ``0``, reading the port it chose from ``DevToolsActivePort`` in OUR OWN
profile, and refusing an endpoint that is not the one that file names.

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

**The fix is not a bigger retry; it is to stop choosing a port.** Chrome is
launched with ``--remote-debugging-port=0``, binds a port of its own, and writes
``<port>\n<browser websocket path>`` to ``DevToolsActivePort`` in the
``--user-data-dir`` it was given. That directory is ours (``profile_lock`` holds
it for the launch), so the file is ours — provided it was written by THIS launch,
which is why :func:`clear_stale` removes any earlier one first and why a file
that cannot be removed refuses the launch rather than being trusted.

**Identity is a second, independent check.** The port came from our file, but
what answers there must also be the browser the file describes:
:func:`verify` compares the ``webSocketDebuggerUrl`` ``/json/version`` reports
with the port and the path Chrome wrote. A mismatch is a ``ToolError`` that names
both, and nothing downstream is built on it — no ``Browser`` is handed back, so
there is no instance for ``close_instance`` to close and the foreign browser's
pid is never ours to name.

It is also THE one home for the file itself (``PORT_FILE``, :func:`read`): the
adoption ladder (``cdp_endpoint``) and the live-master door (``browser_cmdline``)
read it through here, because a browser launched with ``=0`` carries no port on
its command line and the file is where Chrome wrote the one it resolved that to.

A leaf: ``browser_pid_registry`` (what a usable port is), ``tool_errors`` and
stdlib. The launch paths — ``browser_connect`` and ``desktop_launch`` — are its
consumers.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import NamedTuple

from stealth_chrome_devtools_mcp.embedded import browser_pid_registry
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

__all__ = [
    "PORT_ARG",
    "PORT_FILE",
    "ActivePort",
    "clear_stale",
    "exited",
    "missing",
    "own_endpoint",
    "read",
    "verify",
    "with_port_zero",
]

PORT_FLAG = "--remote-debugging-port"

# Chrome writes the port it actually bound here, first line, then the browser
# websocket path, inside the profile it was launched on.
PORT_FILE = "DevToolsActivePort"

#: Chrome resolves ``0`` to a port of its own and reports it in the file.
PORT_ARG = f"{PORT_FLAG}=0"


class ActivePort(NamedTuple):
    """What Chrome wrote into ``DevToolsActivePort``: the port it bound and the
    browser websocket path (``None`` when the second line is missing)."""

    port: int
    path: str | None


def read(profile_dir: str | Path) -> ActivePort | None:
    """Chrome's own ``DevToolsActivePort`` in *profile_dir*, or None.

    The file survives its writer, so this is only evidence about a browser the
    caller has other reasons to believe is live; a launch's own wait is
    :func:`own_endpoint`.
    """
    try:
        text = (Path(profile_dir) / PORT_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.splitlines()
    port = browser_pid_registry.valid_port(lines[0] if lines else "")
    if port is None:
        return None
    return ActivePort(port, (lines[1].strip() if len(lines) > 1 else "") or None)


def with_port_zero(args: list[str]) -> list[str]:
    """*args* with every ``--remote-debugging-port`` replaced by ``=0``, once."""
    kept = [arg for arg in args if not arg.startswith(f"{PORT_FLAG}=")]
    return [*kept, PORT_ARG]


def clear_stale(profile_dir: str | Path | None) -> None:
    """Remove a ``DevToolsActivePort`` an earlier Chrome left in *profile_dir*.

    The file outlives the browser that wrote it, so before a launch any file
    there is somebody else's. One that cannot be removed cannot be told from a
    fresh one, and trusting it is the defect — so it refuses the launch.
    """
    if not profile_dir:
        return
    path = Path(profile_dir) / PORT_FILE
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        if not path.exists():
            return
        raise ToolError(
            f"F-1009: {path} is left over from an earlier Chrome and cannot be "
            f"removed ({error}), so the port in it could not be told from the "
            "one this launch opens. Nothing was launched."
        ) from error


def own_endpoint(profile_dir: str | Path | None) -> ActivePort | None:
    """The port and path THIS launch's Chrome wrote into *profile_dir*, or None
    while it has not (a file with no path is half-written and also None)."""
    if not profile_dir:
        return None
    found = read(profile_dir)
    return found if found and found.path else None


def verify(ws_url: str, expected: ActivePort) -> None:
    """Raise unless *ws_url* (``/json/version``'s ``webSocketDebuggerUrl``) is the
    browser *expected* describes."""
    parsed = urllib.parse.urlparse(ws_url)
    if parsed.port == expected.port and parsed.path == expected.path:
        return
    raise ToolError(
        f"F-1009: 127.0.0.1:{expected.port} answered as {ws_url!r}, which is not "
        f"the browser this spawn launched ({PORT_FILE} "
        f"names {expected.path!r}). Another process holds that DevTools "
        "endpoint; this spawn did not attach to it and did not touch it."
    )


def missing(profile_dir: str | Path | None, seconds: float) -> ToolError:
    """The launched Chrome never wrote its port within *seconds*."""
    return ToolError(
        f"F-1009: the launched Chrome did not write "
        f"{PORT_FILE} in {profile_dir} within "
        f"{seconds:g} s, so there is no DevTools port of its own to attach to."
    )


def exited(profile_dir: str | Path | None) -> ToolError:
    """The launched Chrome ended before it wrote its port."""
    return ToolError(
        f"F-1009: the launched Chrome exited before its DevTools "
        f"endpoint opened ({PORT_FILE} in {profile_dir}) - "
        "it most likely handed off to a browser already running on that profile."
    )
