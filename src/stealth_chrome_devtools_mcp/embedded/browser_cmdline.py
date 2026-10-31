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
compared. It states FACTS and decides nothing: every judgement is
``browser_reattach``'s and ``profile_lock``'s, its two consumers since F-931
(``command_line_is_gone`` is the last of them, and it is a fact about the
command line too).
"""

from __future__ import annotations

import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Collection, Iterator

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


def named_processes() -> Iterator[tuple[psutil.Process, str]]:
    """Every live process with its name, skipping any psutil cannot read.

    THE one walk of the process table. ``psutil.process_iter(["name"])`` reads
    the name INSIDE the generator, so an exception from that read ends the
    generator and no ``try`` around the loop body can see it: on macOS
    ``name()`` falls back to ``cmdline()`` for a truncated name and psutil 7.2.2
    raised ``SystemError`` ("proc_cmdline returned a result with an exception
    set"), which failed the whole scan and, through ``profile_lock``, the spawn
    (T-11618). Here the generator reads nothing; the name is read per process in
    a ``try`` that skips that one process and goes on.

    Still the cheap field only, one read per process: ``cmdline`` for the whole
    table is one PEB read per process on Windows, so a caller reads it lazily
    from the process this yields, for the few browser-named ones. ``psutil.Error``
    covers a process that vanished or is not ours, ``OSError`` a failed system
    call, ``SystemError`` the macOS binding's error with an exception set.
    """
    for process in psutil.process_iter():
        try:
            name = process.name() or ""
        except (psutil.Error, OSError, SystemError):
            continue
        yield process, name


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

    A profile is held by a whole process TREE, and until F-931 the witness that
    named a holder did not say which member it had named:
    ``profile_lock.profile_hold`` answered from ``process_cleanup``'s cmdline
    scan, which is a SET, so the pid it reported was whichever member iterated
    first. Measured on Chrome 153, one real spawn, eleven processes on one
    profile: the browser (no ``--type``), six renderers, two utilities, a
    gpu-process and a crashpad-handler. Only the browser and the renderers carry
    ``--remote-debugging-port`` at all, so in the run where the witness named a
    ``utility`` child the endpoint ladder found nothing and the adoption
    declined — silently, because "no port" and "nothing holds this directory"
    were the same answer. In the run where it happened to name the browser,
    everything worked. That was one bug with a coin flip in front of it.

    **Since F-931 ``profile_hold`` asks this module too**, so its pid is already
    the browser and the two witnesses cannot disagree. This function is not
    thereby redundant: it answers a DIFFERENT question — which member is the
    browser and whether there is EXACTLY ONE — and an ambiguous tree is no
    answer to adopt from while it is still plainly held.

    Adopting a child would be worse than declining even when its argv did carry
    the port: the pid is stamped onto ``Browser._process_pid``, so
    ``process_exit.browser_is_alive`` would discard the instance the
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
    return browser_members(pids, expect_dir).sole


@dataclass(frozen=True)
class Members:
    """Which of a profile's live processes are BROWSERS, and whether every one
    of the others could be read (F-931).

    Two facts because a caller needs both, and collapsing them is the defect
    this exists to prevent: an empty ``browsers`` with an empty ``unreadable``
    is the ESTABLISHED statement "the browser has gone and only its children
    are left", while an empty one with pids in ``unreadable`` says only that we
    could not tell — ``reap_guard``'s distinction, at a different witness.

    ``unreadable`` carries the PIDS and not a bool (F-931 M1) because the
    caller has to be able to NAME one: ``profile_lock`` composes the sentence
    F-914's refusal quotes verbatim, and reporting ``min(pids)`` over the whole
    scan named whichever member sorted first — routinely one we had read
    perfectly well, which is the ``min(pids)`` defect F-931 removes from the
    browser branch, re-made in the branch beside it.
    """

    browsers: tuple[int, ...]
    unreadable: tuple[int, ...]

    @property
    def sole(self) -> int | None:
        """The ONE browser here, or None when none qualifies **and when more
        than one does** — :func:`browser_process`'s rule, which is argued there
        and spelled once."""
        return self.browsers[0] if len(self.browsers) == 1 else None


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
    unreadable: list[int] = []
    for pid in pids or ():
        if not isinstance(pid, int):
            continue
        cmdline = arguments(pid)
        if not cmdline:
            # `arguments` collapses "gone" and "refused" into an empty list, so
            # the pid itself is what tells them apart.
            if _still_running(pid):
                unreadable.append(pid)
            continue
        if flag_value(cmdline, TYPE_FLAG) is not None:
            continue
        on_disk = flag_value(cmdline, "--user-data-dir")
        if browser_pid_registry.normalize_path(
            on_disk
        ) != browser_pid_registry.normalize_path(expect_dir):
            continue
        found.append(pid)
    return Members(tuple(found), tuple(unreadable))


#: Whether an EMPTY command line can be told from a live browser, which is a
#: Linux-only measurement (F-998): see :func:`command_line_is_gone`. False
#: elsewhere, and there only a zombie counts as gone.
_EXE_WITNESS = sys.platform.startswith("linux")


def _exe_is_gone(pid: int) -> bool:
    """Whether ``/proc/<pid>/exe`` is definitely gone: ``FileNotFoundError`` (or
    its ``ESRCH`` twin) and nothing else. A readable link, ``PermissionError`` or
    any other error is "not shown gone", which the caller reads as HELD."""
    try:
        Path(f"{getattr(psutil, 'PROCFS_PATH', '/proc')}/{pid}/exe").readlink()
    except (FileNotFoundError, ProcessLookupError):
        return True
    except OSError:
        return False
    return False


def command_line_is_gone(pid: int) -> bool:
    """Whether *pid* is still LISTED but can no longer be a running browser
    (F-998). A browser that has been killed spends a moment in one of two states,
    and ``psutil.pid_exists`` is True for both:

    * a ZOMBIE, which has exited and waits for its parent's ``wait()``. This
      backend is that parent, and collects it only when its child watcher gets to
      run. Its ``status`` says so, on every platform;
    * TEARING DOWN, which has already released its address space — the first thing
      a dying process does — and so reads an EMPTY command line, with no error,
      while the kernel finishes with its threads and files. **Linux only, and only
      with a second fact:** an empty command line is NOT enough, because a LIVE
      browser has one too. ``/usr/bin/chromium`` and Google's launcher are
      ``#!/bin/sh`` scripts that ``exec`` the real binary under the same pid, and
      across that ``execve`` the pid reads an empty argv while running, before
      ``SingletonLock`` exists (measured on Chromium 155, 40 launches under CPU
      contention: the browser pid read empty while alive in 11 of them, and the
      production ``profile_hold`` showed the profile free in 2 of 11,194 live polls
      with the first version of this function, against 0 of 11,033 before F-998).
      What separates the two is the executable link: ``/proc/<pid>/exe`` stayed
      readable (and ``maps`` non-empty) on 81 of 81 live mid-exec samples, and was
      unreadable on every sample of a dying browser, because the dying process has
      no address space left to name an executable from. So an empty command line
      is gone only when ``/proc/<pid>/exe`` is also ``FileNotFoundError``;
      ``PermissionError``, a readable link or any other outcome is HELD.

    Off Linux only the zombie half applies. The exec-window measurement is
    Linux-only (Chrome on macOS is a direct binary, which was not measured, and
    Windows has no zombies and never asks ``profile_lock._pid_alive`` about a
    BROWSER: Chrome writes no ``SingletonLock`` there, and a killed process is
    simply no longer listed. Since F-1008 our own launch marker reaches it
    there, but for a BACKEND's pid, whose argv no exec window empties), so
    nothing licenses reading an empty argv as dead there.

    Known limit, not fixable here: on Linux the leader of a still-running
    multithreaded process reads ``Z`` once its own thread has exited (a main
    thread that calls ``pthread_exit``), which this reads as gone. Chrome does not
    do that. Holding a zombie that has more than one thread was tried and fails:
    a SIGKILLed Chrome keeps ``num_threads`` above 1 while its other threads
    finish, and 12 of 20 runs of the fleet journey walked to ``fleet-2`` again.

    Neither state can hold a profile or answer Chrome's singleton socket. The ONE
    witness of the two "is this pid still there" questions below and
    ``profile_lock._pid_alive``. (``process_exit._has_exited`` asks a related
    question of a process the backend is WAITING on and cannot answer this one:
    it does not see a teardown.)

    A pid that vanished between the listing and this read is gone, which is True;
    one we cannot ask about (``AccessDenied``) stays HELD, never called dead."""
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return True
        return _EXE_WITNESS and not process.cmdline() and _exe_is_gone(pid)
    except psutil.NoSuchProcess:  # incl. ZombieProcess: reaped or reaping
        return True
    except (psutil.Error, OSError, ValueError):
        return False


def _still_running(pid: int) -> bool:
    """Whether *pid* is still there. A pid we cannot ask about counts as
    running, which resolves toward HELD — ``profile_lock._pid_alive``'s
    direction, for the same reason. A pid whose command line has gone is not still
    there (F-998)."""
    try:
        return psutil.pid_exists(pid) and not command_line_is_gone(pid)
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
