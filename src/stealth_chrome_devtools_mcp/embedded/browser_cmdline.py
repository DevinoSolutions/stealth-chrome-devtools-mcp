"""THE one home for what a RUNNING browser's own command line says about it.

Four facts, and they exist because a browser that outlived the backend which
spawned it (F-888) has nothing else left to describe it: the process that knew
which of its processes was the browser, its port, its headlessness and the proxy
it was dialling through has exited, and the record that process wrote may never
have existed — the stranded Seller Central Chrome (pid 115652) has no entry at
all, because the successor backend rewrote ``browser_pids.json`` without it.

So the argv IS the record for these three, and each one is read the same way:
``psutil`` gives a LIST, never a command string, so quoting is a non-issue, and
both spellings of a flag are accepted because a caller's args reach Chrome
through ``merge_browser_args`` while nodriver appends its own with ``=``.

A leaf: ``psutil``, stdlib ``socket``, and ``browser_pid_registry`` for the two
things it must not re-spell — what a usable port is, and how two paths are
compared. It decides NOTHING: every judgement is ``browser_reattach``'s and
``profile_lock``'s, its two consumers since F-931.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Collection

from stealth_chrome_devtools_mcp.embedded import browser_pid_registry

DEBUG_PORT_FLAG = "--remote-debugging-port"

# Chromium gives every CHILD process a ``--type=`` (renderer, gpu-process,
# utility, crashpad-handler); the browser process alone has none. That is what
# `browser_process` reads, and it is the only structural difference in the argv.
TYPE_FLAG = "--type"

# Only a proxy on one of these is ours to judge: the authenticated forwarder
# always binds loopback, and a remote proxy was never tied to a backend's life.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})

# A loopback connect either answers or refuses at once; this only bounds a
# pathological local firewall, and it is spent once per adoption candidate.
_PROXY_PROBE_SECONDS = 0.5


def arguments(pid: int) -> list[str]:
    """That pid's argv, or empty when it cannot be read."""
    try:
        return list(psutil.Process(pid).cmdline())
    except (psutil.Error, OSError, ValueError):
        return []


def flag_value(cmdline: list[str], flag: str) -> str | None:
    """``--flag=value`` or ``--flag value``, whichever spelling is present."""
    for index, arg in enumerate(cmdline):
        if arg.startswith(f"{flag}="):
            return arg.split("=", 1)[1]
        if arg == flag and index + 1 < len(cmdline):
            return cmdline[index + 1]
    return None


def debug_port(pid: int, expect_dir: str | None = None) -> int | None:
    """``--remote-debugging-port`` off that pid's command line, or None.

    *expect_dir* JOINS the port to the profile (F-888 review M4). Conditions 3
    and 4 of the adoption rule are otherwise independent — one proves *a* Chrome
    holds the pid, the other produces *a* port, and nothing tied them together.
    That gap is reachable: ``_fallback_pid_identity_ok`` answers True when the
    record carries no ``create_time``, so on a machine with hundreds of orphan
    Chromes a RECYCLED pid landing on some other chrome.exe satisfies "alive +
    Chromium-family", and this function would then read a STRANGER's debugging
    port — which adoption would stamp our ownership over and a later
    ``close_instance`` would kill. Comparing the process's own
    ``--user-data-dir`` against the directory we recorded closes it: the same
    process must be on the same profile, or it is not our browser.
    """
    cmdline = arguments(pid)
    if not cmdline:
        return None
    if expect_dir is not None:
        on_disk = flag_value(cmdline, "--user-data-dir")
        if browser_pid_registry.normalize_path(
            on_disk
        ) != browser_pid_registry.normalize_path(expect_dir):
            return None
    return browser_pid_registry.valid_port(flag_value(cmdline, DEBUG_PORT_FLAG))


def browser_process(pids: Collection[int] | None, expect_dir: str) -> int | None:
    """Which of *pids* is the BROWSER process on *expect_dir* — not one of its
    children (F-888 review, measured after the fact).

    A profile is held by a whole process TREE, and the witness that names a
    holder does not say which member it named. ``profile_lock.profile_hold``
    answers from ``process_cleanup``'s cmdline scan, which is a SET, so the pid
    it reports is whichever member iterated first. Measured on Chrome 153, one
    real spawn, eleven processes on one profile: the browser (no ``--type``),
    six renderers, two utilities, a gpu-process and a crashpad-handler. Only the
    browser and the renderers carry ``--remote-debugging-port`` at all, so in the
    run where the witness named a ``utility`` child the endpoint ladder found
    nothing and the adoption declined — silently, because "no port" and "nothing
    holds this directory" were the same answer. In the run where it happened to
    name the browser, everything worked. That is one bug with a coin flip in
    front of it.

    Adopting a child would be worse than declining even when its argv did carry
    the port: the pid is stamped onto ``Browser._process_pid``, so
    ``BrowserManager._browser_process_is_alive`` would discard the instance the
    moment that renderer recycled, and ``close_instance`` would kill a renderer
    and leave the browser running.

    ``--type`` is the whole rule and nothing else is consulted — not the parent
    pid (the browser's own parent is whatever launched it, which on Windows is a
    trampoline that has already exited) and not the port (a renderer has it too).

    Returns None when no member qualifies **and when more than one does**, which
    the caller reports rather than swallows. Chrome's process singleton normally
    guarantees exactly one browser per directory, but the states this whole
    feature operates in are precisely the ones where it does not hold: F-871's
    stale or absent ``SingletonLock``, and a hard-killed Chrome on Windows whose
    ``lockfile`` says nothing about a pid. Taking ``[0]`` of two roots would put
    the same set-ordering coin flip back — SILENTLY, which is the property this
    function exists to remove — so an ambiguous answer is no answer.
    """
    found = browser_members(pids, expect_dir).browsers
    return found[0] if len(found) == 1 else None


@dataclass(frozen=True)
class Members:
    """Which of a profile's live processes are BROWSERS, and whether every one
    of the others could be read (F-931).

    Two facts because a caller needs both, and collapsing them is the defect
    this exists to prevent: an empty ``browsers`` with ``unreadable`` False is
    the ESTABLISHED statement "the browser has gone and only its children are
    left", while an empty one with ``unreadable`` True says only that we could
    not tell — ``reap_guard``'s distinction, at a different witness.
    """

    browsers: tuple[int, ...]
    unreadable: bool


def browser_members(pids: Collection[int] | None, expect_dir: str) -> Members:
    """Which of *pids* are browser processes on *expect_dir*.

    The structural rule is :func:`browser_process`'s and is argued there; what
    this adds is the THIRD outcome that function has no way to report, because
    it answers one pid or None. ``profile_lock.profile_hold`` needs it: an
    argv it could not read is not evidence that a browser is absent, and a
    directory shown free on that evidence is two browsers on one profile.

    A pid missing from the process table contributes NOTHING and is not
    "unreadable" — the scan that produced *pids* and this read are two moments,
    and a process that exited between them is an established negative.
    """
    found: list[int] = []
    unreadable = False
    for pid in pids or ():
        if not isinstance(pid, int):
            continue
        cmdline = arguments(pid)
        if not cmdline:
            # `arguments` collapses "gone" and "refused" into an empty list, so
            # the pid itself is what tells them apart.
            unreadable = unreadable or _still_running(pid)
            continue
        if flag_value(cmdline, TYPE_FLAG) is not None:
            continue
        on_disk = flag_value(cmdline, "--user-data-dir")
        if browser_pid_registry.normalize_path(
            on_disk
        ) != browser_pid_registry.normalize_path(expect_dir):
            continue
        found.append(pid)
    return Members(tuple(found), unreadable)


def _still_running(pid: int) -> bool:
    """Whether *pid* is still there. A pid we cannot ask about counts as
    running, which resolves toward HELD — ``profile_lock._pid_alive``'s
    direction, for the same reason."""
    try:
        return psutil.pid_exists(pid)
    except (psutil.Error, OSError):
        return True


def is_headless(pid: int) -> bool:
    """Whether the browser on *pid* was launched headless (F-888 review M2).

    Measured, not assumed: ``BrowserInstance.headless`` defaults to False and an
    adopted instance used to report that default about a browser this backend
    never launched, so a headless one was listed as headed. Chrome spells it
    ``--headless`` or ``--headless=new``; either is the answer.
    """
    return any(
        arg == "--headless" or arg.startswith("--headless=") for arg in arguments(pid)
    )


def dead_local_proxy(pid: int) -> str | None:
    """``host:port`` of a loopback proxy this browser was launched behind that
    nothing is listening on any more, or None (F-888 review M3).

    An authenticated ``proxy=`` spawn does not put the caller's proxy on Chrome's
    command line: it starts an ``AuthenticatedProxyForwarder`` INSIDE the backend
    and points Chrome at ``127.0.0.1:<forwarder port>``. That object dies with its
    backend while the launch arg lives on, so an adopted browser can come back
    with every navigation failing at a closed local port.

    The test is a CONNECT, not the mere presence of the flag: a caller may
    legitimately run their own local proxy that is still up, and warning about
    that would be a lie. Only loopback is examined — a remote proxy is not ours
    to judge and was never tied to the dead backend's lifetime.
    """
    value = flag_value(arguments(pid), "--proxy-server")
    if not value:
        return None
    endpoint_part = value.rsplit("/", 1)[-1]
    host, _, port_text = endpoint_part.rpartition(":")
    if host.lower() not in _LOOPBACK_HOSTS:
        return None
    port = browser_pid_registry.valid_port(port_text)
    if port is None:
        return None
    with socket.socket() as probe:
        probe.settimeout(_PROXY_PROBE_SECONDS)
        try:
            probe.connect((host, port))
        except OSError:
            return f"{host}:{port}"
    return None
