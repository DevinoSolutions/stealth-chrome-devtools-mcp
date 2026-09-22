"""THE one home for "where is the CDP endpoint of the browser this RECORD
entry describes" (F-888, extracted by F-916).

The address, never the door. Going THROUGH a door is ``cdp_attach``'s — it owns
the nodriver gate, the reclaiming attach and the close — and deciding WHETHER to
knock is ``browser_reattach``'s. This module answers only the question in
between, and it is a question about a RECORD ENTRY rather than about a
websocket, which is why it is not folded into either of them: its three
witnesses are a recorded field, a live process's argv and a file inside a
profile directory, and none of those is something the door knows about.

**Three witnesses, in order of trust:**

* the port this record CARRIES (``cdp_port``, written at track time since
  2.1.10);
* ``--remote-debugging-port=`` on that pid's command line, which is what
  nodriver passed it (``Config.__call__`` appends the flag from
  ``config.port``) — read through ``browser_cmdline.debug_port``, which also
  JOINS the port to the profile, so a RECYCLED pid on a stranger's chrome.exe
  cannot donate its debugging port;
* ``<user_data_dir>/DevToolsActivePort``, whose first line is the port Chrome
  actually bound.

**Why that order.** The recorded port leads because a record WE wrote is about
THIS instance. The command line comes next because it is definitionally the live
process's, while ``DevToolsActivePort`` is a file that OUTLIVES the browser that
wrote it — and measured on the real stranded Seller Central Chrome (pid 115652,
``--remote-debugging-port=9223``) the file was **absent** while the browser ran,
so a file-first ladder would have found nothing. The file still earns its rung:
``--remote-debugging-port=0`` gives a command line
:func:`browser_pid_registry.valid_port` rejects as "not bound yet", and the file
is where Chrome wrote the port it resolved that to.

The last two rungs are not belt-and-braces. 2.1.8/2.1.9 recorded no port at all,
and those are precisely the browsers carrying today's stranded logins — the
population F-888 exists for and the one F-916 measured being reaped.

Each witness is re-checked as an int in range rather than trusted: the record
tolerates a hand edit, a command line is whatever the process says it is, and
the file survives its writer.

A leaf: ``browser_pid_registry`` (what a usable port is) + ``browser_cmdline``
(what a live process's argv says) + stdlib. It imports neither consumer, decides
nothing about adoption, and never raises — an endpoint that cannot be recovered
is ``None``, and what THAT means is the caller's judgement (for
``browser_reattach._adoptable_entry`` since F-916 it means UNDECIDED: spare the
browser, do not adopt it).
"""

from __future__ import annotations

from pathlib import Path

from stealth_chrome_devtools_mcp.embedded import browser_cmdline, browser_pid_registry

# Chrome writes the port it actually bound here, first line, inside the profile
# it was launched on. The second line is the browser websocket path, which we do
# not use — nodriver builds its own from host and port.
DEVTOOLS_PORT_FILE = "DevToolsActivePort"


def endpoint(entry: browser_pid_registry.Entry) -> int | None:
    """The CDP port of the browser *entry* describes, or None.

    Three witnesses, most trusted first — the module docstring carries the
    order and the measurement behind it.
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
