"""F-1009 — THE one home for "is the DevTools endpoint on this port MY Chrome's":
reading ``DevToolsActivePort`` in OUR OWN profile and refusing an endpoint that
is not the one that file names.

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

**The fix does not change what Chrome is asked for.** It was first written to
launch with ``--remote-debugging-port=0`` (no port to steal) and CI measured
that real Chrome then reports ``navigator.webdriver == true`` on every OS — a
stealth regression — so the argv keeps the concrete port. What changes is that
nothing answering on it is trusted until THIS launch's Chrome has written
``<port>
<browser websocket path>`` to ``DevToolsActivePort`` in the
``--user-data-dir`` it was given naming that same port (Chrome writes it only
after it bound the port, so a Chrome that lost the race writes nothing). That
directory is ours (``profile_lock`` holds it for the launch), so the file is ours
— provided it was written by THIS launch, which is why :func:`clear_stale`
removes any earlier one first and why a file that cannot be removed refuses the
launch rather than being trusted.

**Identity is a second, independent check.** What answers on that port must also
be the browser the file describes: :func:`verify` compares the
``webSocketDebuggerUrl`` ``/json/version`` reports with the port and the path
Chrome wrote. A mismatch is a ``ToolError`` that names both, and nothing
downstream is built on it — no ``Browser`` is handed back, so there is no
instance for ``close_instance`` to close and the foreign browser's pid is never
ours to name. A launch that loses the port race is therefore a named, retryable
failure instead of an attach to a stranger.

It is also THE one home for the file itself (``PORT_FILE``, :func:`read`): the
adoption ladder (``cdp_endpoint``) reads it through here.

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
    "PORT_FILE",
    "ActivePort",
    "clear_stale",
    "exited",
    "missing",
    "own_endpoint",
    "read",
    "verify",
]

# Chrome writes the port it actually bound here, first line, then the browser
# websocket path, inside the profile it was launched on.
PORT_FILE = "DevToolsActivePort"


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


def own_endpoint(profile_dir: str | Path | None, port: int) -> ActivePort | None:
    """What THIS launch's Chrome wrote into *profile_dir* for *port*, or None.

    None while it has not: no file, a half-written one (no path), or one naming a
    different port than the one this launch asked Chrome for (not this launch's).
    """
    if not profile_dir:
        return None
    found = read(profile_dir)
    return found if found and found.path and found.port == port else None


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


def missing(profile_dir: str | Path | None, port: int, seconds: float) -> ToolError:
    """The launched Chrome never wrote *port* to its file within *seconds*."""
    return ToolError(
        f"F-1009: the launched Chrome did not write {PORT_FILE} naming port "
        f"{port} in {profile_dir} within {seconds:g} s, so it never opened "
        "DevTools on the port it was given (another process may have taken it) "
        "and there is no endpoint of its own to attach to. Nothing that answers "
        "on that port was attached to."
    )


def exited(profile_dir: str | Path | None) -> ToolError:
    """The launched Chrome ended before it wrote its port."""
    return ToolError(
        f"F-1009: the launched Chrome exited before its DevTools "
        f"endpoint opened ({PORT_FILE} in {profile_dir}) - "
        "it most likely handed off to a browser already running on that profile."
    )
