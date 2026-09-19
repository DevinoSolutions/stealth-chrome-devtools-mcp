"""THE one home for "a browser outlived the backend that spawned it — may we
adopt it, where is its CDP endpoint, the door back in, and the pass that walks
through it" (F-888).

Four parts, and they are one module because each exists only to serve the first:
a classification that names nothing to attach to is useless, a door nothing is
allowed through is a second spawn path, and a pass in another file is a second
place the rule gets asked from. They are HERE and not spread across
``process_cleanup`` and ``browser_manager`` for a second reason too: both of
those files sit on a grandfathered LOC cap that ratchets down only, and the
budget gate's remedy is exactly this — extract a leaf rather than grow a god
file.

Both collaborators arrive as ARGUMENTS — the ``ProcessCleanup`` and the
``BrowserManager`` — on ``spawn_leak.reap_launched_browsers``'s precedent, which
takes its ``ProcessCleanup`` the same way and reaches the same private helpers.
That is what keeps the import graph acyclic: ``process_cleanup`` imports THIS
module for its two decisions (which orphans to spare, and the reap a failed
adoption falls back to), so this module may import neither of them.

**The adoption rule** (:func:`adoptable`). A recorded browser may be adopted when
all four hold, asked in cheapest-first order:

1. Its recorded OWNER is not a live backend of ours — exactly
   ``browser_pid_registry.is_reapable``, the one ownership rule, asked here with
   the same injected witness startup recovery uses. Two backends driving one
   Chrome is the harm F-886 exists to prevent, so a browser a LIVE sibling still
   owns is never taken; recovering such a browser needs the operator to stop that
   backend first (RUNBOOK, "recover a stranded login").
2. Its profile is PERSISTENT — ``browser_pid_registry.on_persistent_profile``,
   the same predicate that spares the directory from deletion. A disposable
   auto-clone is never adopted: its whole contract is that it dies with its
   browser, and adopting one would keep a throwaway profile alive forever.
3. The recorded Chrome pid is still that Chrome — pid alive, create_time within
   the recorded tolerance, and a Chromium-family process name. Supplied as
   ``browser_alive`` rather than re-implemented, so the recycled-pid tolerance
   has one home (``process_cleanup``).
4. A CDP endpoint is recoverable for it (:func:`endpoint`). Without a port there
   is nothing to attach to, and a candidate we cannot reach must be classified
   as un-adoptable BEFORE the reaper is told to skip it — otherwise a browser we
   can neither adopt nor reap leaks forever.

**The endpoint ladder** (:func:`endpoint`), three witnesses in order of trust:

* the port this record CARRIES (``cdp_port``, written at track time since 2.1.10);
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
(pid 115652, `--remote-debugging-port=9223`), the file was **absent** while the
browser was running, so a ladder that asked it first would have found nothing to
attach to. The file still earns its rung: a caller that passes
``--remote-debugging-port=0`` gets a command line saying ``0``, which
:func:`browser_pid_registry.valid_port` rejects as "not bound yet", and the file
is where Chrome writes the port it actually resolved that to.

**The door** (:func:`attach_config` + :func:`attach`). Setting BOTH ``host`` and
``port`` on a nodriver ``Config`` is what makes ``uc.start`` connect instead of
spawn — nodriver's own gate, ``browser.py`` lines 371-375: with neither set it
assigns ``127.0.0.1`` and a free port and launches Chrome; with both set it takes
``connect_existing`` and never reaches ``create_subprocess_exec``. That gate is
the only door into a running browser and ``desktop_launch.launch_and_attach`` was
already standing in it, so the two lines that open it moved here and that
function is now this module's SECOND consumer rather than a second door. It
builds the config, needs ``config()`` for the argv it hands the Task Scheduler,
and attaches with the SAME object afterwards — which is why the two halves are
separate functions and not one call.

``CDP_HOST`` is a constant and not an argument because both launch paths already
fix it: nodriver hard-codes ``127.0.0.1`` when it spawns, and the delegated path
spelled the same literal. A browser reachable on some other interface is not a
browser either path produced.

A leaf in the sense that matters: it imports no module that imports it. Both
liveness witnesses arrive as ARGUMENTS on ``backend_liveness``'s pattern, so the
ownership rule stays single-homed in ``browser_pid_registry``. ``nodriver`` is
imported LAZILY inside the door, for ``desktop_launch``'s reason: the
classification half is reached from ``process_cleanup`` on every backend
startup, and a module-level nodriver import would put that cost on a path that
usually has nothing to adopt.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

from stealth_chrome_devtools_mcp.embedded import (
    browser_pid_registry,
    desktop_launch,
    tab_identity,
    tool_errors,
)
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.in_memory_storage import in_memory_storage
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions, BrowserState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from nodriver import Browser, Config

    from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
    from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup

# Both launch paths give Chrome this and only this (nodriver `browser.py:374`
# on the spawn path, the literal in `desktop_launch.launch_and_attach` on the
# delegated one), so the endpoint is a port and the host is a fact.
CDP_HOST = "127.0.0.1"

# Chrome writes the port it actually bound here, first line, inside the profile
# it was launched on. The second line is the browser websocket path, which we do
# not use — nodriver builds its own from host and port.
DEVTOOLS_PORT_FILE = "DevToolsActivePort"

_DEBUG_PORT_FLAG = "--remote-debugging-port"

# One adoption's whole CDP budget: the websocket connect plus nodriver's initial
# target discovery. A browser that has stopped answering must cost this once and
# then be reaped like any other orphan, never hang a backend's startup — which is
# the failure F-856 removed from this exact code path.
ATTACH_BUDGET_SECONDS = 15.0


@dataclass(frozen=True)
class Adoptable:
    """One recorded browser that may be taken over, and what it takes to do it."""

    instance_id: str
    pid: int
    user_data_dir: str
    port: int


def adoptable(
    entries: browser_pid_registry.Entries,
    *,
    owner_alive: Callable[[int, float | None], bool],
    browser_alive: Callable[[int, float | None], bool],
) -> dict[str, Adoptable]:
    """Every entry in *entries* this backend may take over, keyed by instance id.

    ONE pass, shared by both consumers: ``process_cleanup`` skips these entries
    instead of reaping them, and ``browser_manager`` attaches to them. Asking the
    question twice in two places is how the reaper and the adopter would come to
    disagree about one browser — which is a killed login on one side and a leaked
    Chrome on the other.

    Never raises. A candidate that cannot be classified is left OUT, which means
    the reaper treats it exactly as 2.1.9 did: fail toward today's behaviour,
    never toward sparing something we could not reason about.
    """
    found: dict[str, Adoptable] = {}
    for instance_id, entry in entries.items():
        with contextlib.suppress(Exception):
            candidate = _adoptable_entry(
                instance_id, entry, owner_alive=owner_alive, browser_alive=browser_alive
            )
            if candidate is not None:
                found[instance_id] = candidate
    return found


def _adoptable_entry(
    instance_id: str,
    entry: browser_pid_registry.Entry,
    *,
    owner_alive: Callable[[int, float | None], bool],
    browser_alive: Callable[[int, float | None], bool],
) -> Adoptable | None:
    """The four conditions, in the order the module docstring states them."""
    if not browser_pid_registry.is_reapable(entry, owner_alive):
        return None
    if not browser_pid_registry.on_persistent_profile(entry):
        return None

    pid = entry.get("pid")
    profile_dir = entry.get("user_data_dir")
    if not isinstance(pid, int) or not isinstance(profile_dir, str) or not profile_dir:
        return None
    if not browser_alive(pid, browser_pid_registry.recorded_time(entry, "create_time")):
        return None

    port = endpoint(entry)
    if port is None:
        return None
    return Adoptable(
        instance_id=instance_id, pid=pid, user_data_dir=profile_dir, port=port
    )


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

    pid = entry.get("pid")
    if isinstance(pid, int):
        from_cmdline = _port_from_cmdline(pid)
        if from_cmdline is not None:
            return from_cmdline

    profile_dir = entry.get("user_data_dir")
    if isinstance(profile_dir, str) and profile_dir:
        return _port_from_profile(Path(profile_dir))
    return None


def _port_from_profile(profile_dir: Path) -> int | None:
    """Chrome's own ``DevToolsActivePort``, first line, or None."""
    try:
        first = (profile_dir / DEVTOOLS_PORT_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = first.splitlines()
    return browser_pid_registry.valid_port(lines[0] if lines else "")


def _port_from_cmdline(pid: int) -> int | None:
    """``--remote-debugging-port`` off that pid's command line, or None.

    Both spellings, because a caller's args reach Chrome through
    ``merge_browser_args`` and nodriver appends its own with ``=``.
    """
    try:
        cmdline = psutil.Process(pid).cmdline()
    except (psutil.Error, OSError, ValueError):
        return None
    for index, arg in enumerate(cmdline):
        if arg.startswith(f"{_DEBUG_PORT_FLAG}="):
            return browser_pid_registry.valid_port(arg.split("=", 1)[1])
        if arg == _DEBUG_PORT_FLAG and index + 1 < len(cmdline):
            return browser_pid_registry.valid_port(cmdline[index + 1])
    return None


def attach_config(
    user_data_dir: str | None,
    port: int,
    *,
    headless: bool = False,
    browser_executable_path: str | None = None,
    browser_args: list[str] | None = None,
) -> Config:
    """A nodriver ``Config`` that will CONNECT to a running browser, not spawn one.

    Host and port are both set, which is nodriver's own ``connect_existing`` gate
    (``browser.py:371``). ``user_data_dir`` is carried even though the attach path
    ignores it, because ``browser.config.user_data_dir`` is what the spawn
    pipeline reads back to decide profile cleanup — it must name the directory the
    browser actually launched with, or a persistent profile would read as a
    nodriver temp dir and be deleted.
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


def report(action: str, message: str) -> None:
    """One INFO line about an adoption decision, on this module's component name."""
    debug_logger.log_info("browser_reattach", action, message)


# ---------------------------------------------------------------------------
# The seam over a ProcessCleanup: what it may adopt, and the reap that is the
# fallback when it cannot. Functions taking the cleanup rather than methods on
# it, because `process_cleanup` imports this module and cannot be imported back.
# ---------------------------------------------------------------------------


def recorded_browser_alive(
    cleanup: ProcessCleanup, browser_pid: int, browser_create_time: float | None
) -> bool:
    """True when *browser_pid* is still the Chrome its entry recorded.

    The browser-side twin of ``ProcessCleanup._owner_backend_alive``, composed out
    of predicates that already exist rather than a third one: that module's
    create_time tolerance for a recycled pid, plus the Chromium-family name guard
    ``_kill_process_by_pid`` already refuses to kill without. Adoption needs both
    for the reason reaping does — a pid alone says nothing about what holds it,
    and attaching to a stranger's process is worse than failing to adopt our own.
    """
    if not cleanup._fallback_pid_identity_ok(browser_pid, browser_create_time):
        return False
    try:
        return cleanup._is_browser_process_name(psutil.Process(browser_pid).name())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def adoptable_for(
    cleanup: ProcessCleanup, entries: browser_pid_registry.Entries | None = None
) -> dict[str, Adoptable]:
    """Every recorded browser *cleanup*'s backend may take over, keyed by id.

    THE one call both consumers make: startup recovery asks it to decide what to
    SKIP, and the pass below asks it to decide what to ATTACH to. Asking the
    question in two places is how the reaper and the adopter would come to
    disagree about one browser — a killed login on one side, a leaked Chrome on
    the other. *entries* is accepted so recovery can classify the record it has
    already read instead of reading it twice.
    """
    return adoptable(
        cleanup._load_tracked_pids() if entries is None else entries,
        owner_alive=cleanup._owner_backend_alive,
        browser_alive=lambda pid, ctime: recorded_browser_alive(cleanup, pid, ctime),
    )


def reap_recorded(
    cleanup: ProcessCleanup, instance_id: str, metadata: dict[str, object]
) -> bool:
    """Kill one recorded browser and remove its profile if it is disposable.

    THE one home for "reap this entry". Startup recovery calls it per orphan and
    a failed adoption calls it as its fallback, so a browser we could neither
    adopt nor reach still ends exactly where 2.1.9 left it rather than leaking
    because a new code path spared it. Dropping the ENTRY is the caller's,
    because recovery drops a whole pass in one merge-write.

    Returns True when a process was actually killed.
    """
    killed = cleanup._kill_processes_for_metadata(instance_id, metadata, recovery=True)
    cleanup._cleanup_profile_for_metadata(instance_id, metadata)
    return killed


def held_by(
    user_data_dir: str,
    *,
    read_entries: Callable[[], browser_pid_registry.Entries],
    owner_alive: Callable[[int, float | None], bool],
    live_pids: object,
    new_instance_id: str,
) -> Adoptable | None:
    """The live Chrome holding *user_data_dir* that we may take over, or None.

    THE second entry point into the one adoption rule, and the record is NOT its
    input — because the browser this exists for **has no record entry at all**.
    Measured on the stranded Seller Central Chrome (pid 115652, port 9223): its
    owner backend 173824 died, and the SUCCESSOR backend rewrote
    ``browser_pids.json`` without it, so by the time anyone could adopt it there
    was nothing in the record to iterate. :func:`run` walks entries; it would
    walk straight past this browser forever. The only thing that still names it
    is the DIRECTORY it holds, which is exactly what a caller passes to
    ``spawn_browser(user_data_dir=…)``.

    So the witness here is Chrome's own process singleton, through
    ``profile_lock.profile_hold`` — THE one home for "is this profile held by a
    live process, and who holds it" (F-871) — and the record is consulted only to
    REFUSE: if any entry names this pid and its owner is a live backend of ours,
    that browser belongs to a sibling backend and taking it is F-886's harm from
    the other side. An entry with a DEAD owner is not a refusal; it is a gift,
    because it carries the instance id the client used to hold, which we reuse.

    Deliberately NOT gated on :func:`browser_pid_registry.on_persistent_profile`:
    that predicate reads a RECORD, and there is no record here. The caller
    passing an explicit ``user_data_dir`` is the same declaration by hand — a
    disposable auto-clone is one the server chose, and a caller naming it is
    naming a directory they intend to keep.
    """
    from stealth_chrome_devtools_mcp.embedded import profile_lock

    hold = profile_lock.profile_hold(Path(user_data_dir), live_pids)
    if hold is None or not isinstance(hold.pid, int):
        # Nothing holds it, or something does but no witness could name the pid
        # (Windows' bare `lockfile`). Without a pid there is no command line to
        # read and no process to prove alive, so this is not adoptable — the
        # caller spawns, exactly as before.
        return None

    # The record is read ONLY once something is known to hold the directory, and
    # `read_entries` is a callable for exactly that reason: the overwhelmingly
    # common spawn is onto a directory nobody holds, and that one must not pay
    # for a record read it cannot use.
    entries = read_entries()
    recorded_id: str | None = None
    for instance_id, entry in entries.items():
        if entry.get("pid") != hold.pid:
            continue
        # The SAME ownership rule `run` applies, inverted: not reapable means a
        # live backend of ours still owns this browser, and two backends driving
        # one Chrome is F-886's harm. Asked through `is_reapable` rather than
        # re-read here, so the two entry points cannot come to disagree.
        if not browser_pid_registry.is_reapable(entry, owner_alive):
            return None
        recorded_id = instance_id
        break

    entry_for_port: browser_pid_registry.Entry = (
        dict(entries[recorded_id])
        if recorded_id is not None
        else {"pid": hold.pid, "user_data_dir": user_data_dir}
    )
    port = endpoint(entry_for_port)
    if port is None:
        return None
    return Adoptable(
        instance_id=recorded_id or new_instance_id,
        pid=hold.pid,
        user_data_dir=user_data_dir,
        port=port,
    )


async def adopt_held_profile(
    manager: BrowserManager, cleanup: ProcessCleanup, user_data_dir: str
) -> str | None:
    """Re-attach to the live Chrome holding *user_data_dir*; its id, or None.

    The spawn path's one question, asked BEFORE profile selection because the
    alternative answer is F-871's walk to ``<name>-2`` — a different directory,
    a different profile and a logged-out one, which for a human's Seller Central
    session is the loss this finding exists to stop.

    **A failure here never reaps.** That is the one place this differs from
    :func:`run`, and the difference is the caller's intent: `run` is startup
    recovery, where an unreachable orphan must end somewhere, and the fallback is
    the reap 2.1.9 already did. Here a CLIENT asked for a browser, and killing
    the Chrome they were trying to reach because we could not attach to it would
    be this finding's own harm committed by the fix. Every failure returns None
    and the spawn proceeds exactly as it does today.

    Never raises.
    """
    async with _pass_lock:
        try:
            candidate = await asyncio.to_thread(
                held_by,
                user_data_dir,
                read_entries=cleanup._load_tracked_pids,
                owner_alive=cleanup._owner_backend_alive,
                live_pids=getattr(cleanup, "_get_browser_pids_for_profile", None),
                new_instance_id=str(uuid.uuid4()),
            )
        except Exception as exc:  # noqa: BLE001  PERMANENT(a classification failure must cost the spawn nothing but this branch)
            debug_logger.log_warning(
                "browser_reattach",
                "held",
                f"Could not classify the holder of the requested profile "
                f"({type(exc).__name__}); spawning instead.",
                error=exc,
            )
            return None
        if candidate is None:
            return None
        if candidate.instance_id in manager._instances:
            return candidate.instance_id
        try:
            await asyncio.wait_for(
                _adopt_one(manager, cleanup, candidate.instance_id, candidate),
                timeout=ATTACH_BUDGET_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001  PERMANENT(see the docstring: a failed adoption here must never kill the caller's browser)
            debug_logger.log_warning(
                "browser_reattach",
                "held",
                f"A live browser holds the requested profile on port "
                f"{candidate.port} but could not be re-attached to "
                f"({type(exc).__name__}); spawning instead, which will select a "
                f"different directory.",
                error=exc,
            )
            return None
        report(
            "held",
            f"Re-attached to the running browser already holding the requested "
            f"profile rather than spawning beside it; instance "
            f"{candidate.instance_id}",
        )
        return candidate.instance_id


# ---------------------------------------------------------------------------
# The pass: attach to everything `adoptable_for` names, under the manager that
# will hold it afterwards.
# ---------------------------------------------------------------------------

# Module level, not per manager: there is one BrowserManager per backend, and
# what this serializes is two concurrent first MCP sessions both driving the pass
# before the first one's ownership stamp lands.
_pass_lock = asyncio.Lock()


def start(manager: BrowserManager, cleanup: ProcessCleanup) -> asyncio.Task:
    """Schedule the adoption pass off the first-serve path.

    Fire-and-forget for F-856's reason and on the precedent of the sibling line
    beside it in ``app_lifespan`` (``clone_storage.spawn_background_sweep``): the
    work reaches a browser over CDP, a wedged one costs a whole
    ``ATTACH_BUDGET_SECONDS``, and nothing about a backend's readiness depends on
    it. A client sees an adopted instance appear in ``list_instances`` a moment
    after the backend answers, rather than a backend that answers late.
    """
    return manager._run_in_background(run(manager, cleanup), "reattach")


async def run(manager: BrowserManager, cleanup: ProcessCleanup) -> list[str]:
    """Adopt every recorded browser a dead backend of ours left running.

    The other end of ``process_cleanup``'s shutdown hand-over: that one leaves a
    browser on a persistent profile alive and tracked, this one picks it up, so a
    human's login survives a backend restart, a heal, or a crash. Returns the
    instance ids adopted.

    The instance id is the RECORDED one, so a client holding an id from before
    the restart keeps addressing the same browser. What that client does have to
    do is re-initialize its MCP session — the proxy's heal is a new backend and a
    new session — after which ``list_instances`` reports the adopted instance
    like any other, with its LIVE url and title read here through
    ``tab_identity``, never the cached pair (F-874), which for an adopted browser
    would be empty.

    Idempotent, and serialized by ``_pass_lock``, because ``app_lifespan`` drives
    it and FastMCP runs that per MCP session. A successful adoption re-stamps the
    entry's owner to US, so a second session's classification finds nothing.

    Never raises. One browser that cannot be adopted costs its own entry and
    nothing else: it is reaped exactly as 2.1.9's startup recovery would have
    reaped it, with the reason logged at WARNING.
    """
    async with _pass_lock:
        candidates = await asyncio.to_thread(adoptable_for, cleanup)
        adopted: list[str] = []
        failed: set[str] = set()
        for instance_id, candidate in candidates.items():
            if instance_id in manager._instances:
                continue
            try:
                await asyncio.wait_for(
                    _adopt_one(manager, cleanup, instance_id, candidate),
                    timeout=ATTACH_BUDGET_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001  PERMANENT(a startup background pass must never raise; every failure has the one remedy below)
                # Blind on purpose: this pass runs on a background task at
                # startup and MUST never raise. Every failure — a refused
                # connect, a wedged browser, a nodriver change — has the same
                # remedy, which is the reap below, so narrowing the catch would
                # only turn an unforeseen one into an unhandled task exception.
                failed.add(instance_id)
                # Shape only in the message — a profile path names the operating
                # user and this line reaches the durable log and a Sentry
                # breadcrumb — while `error=` forwards the traceback as
                # `exc_info` (F-869).
                debug_logger.log_warning(
                    "browser_reattach",
                    "reattach",
                    f"Could not re-attach instance {instance_id} on port "
                    f"{candidate.port} ({type(exc).__name__}); reaping it as an "
                    f"orphan instead.",
                    error=exc,
                )
                await asyncio.to_thread(
                    reap_recorded,
                    cleanup,
                    instance_id,
                    {
                        "pid": candidate.pid,
                        "user_data_dir": candidate.user_data_dir,
                        # The reap must not delete a persistent profile, and the
                        # guard reads these two keys — the same values the record
                        # carried to be classified adoptable in the first place.
                        "uses_custom_data_dir": True,
                        "auto_clone": False,
                    },
                )
            else:
                adopted.append(instance_id)
        if failed:
            await asyncio.to_thread(cleanup._drop_recorded, failed)
        if adopted:
            report(
                "reattach",
                f"Re-attached {len(adopted)} browser(s) left by a previous "
                f"backend; their instance ids are unchanged",
            )
        return adopted


async def _adopt_one(
    manager: BrowserManager,
    cleanup: ProcessCleanup,
    instance_id: str,
    candidate: Adoptable,
) -> None:
    """Attach to one running browser and register it under its recorded id.

    Raises on any failure, which is what routes the entry to the caller's reap.
    The registration order matches ``spawn_browser``'s deliberately: nothing is
    published into the manager's instances until the tab answers, so a
    half-adopted browser is never visible to a tool body.
    """
    browser = await attach(
        attach_config(candidate.user_data_dir, candidate.port), candidate.pid
    )
    try:
        tab = browser.main_tab
        if tab is None:
            # Inside the try on purpose (so TRY301's "abstract it out" does not
            # apply): a browser we connected to but cannot use still has an open
            # connection, and the handler below is what closes it.
            raise tool_errors.ToolError(  # noqa: TRY301  PERMANENT(it must raise INSIDE the try; the handler below is what closes the connection)
                f"Re-attached browser for {instance_id} has no tab"
            )
        view = await tab_identity.refreshed(browser, tab)
        options = BrowserOptions(
            user_data_dir=candidate.user_data_dir, auto_clone=False
        )
        instance = manager._build_instance(instance_id, options)
        # Ownership moves to US through the ONE write protocol, with the port
        # re-recorded so a THIRD backend can do this again.
        cleanup.track_browser_process(
            instance_id,
            desktop_launch.pid_shim(browser),
            user_data_dir=candidate.user_data_dir,
            uses_custom_data_dir=True,
            auto_clone=False,
            cdp_port=candidate.port,
        )
        diagnostics = {
            "reattached": True,
            "user_data_dir": candidate.user_data_dir,
            "cdp_port": candidate.port,
            # The role the close path reads: "explicit" is what this profile IS,
            # and it is what keeps close_instance from releasing a clone
            # reservation that was never taken or refreshing the master snapshot
            # from a named profile.
            "profile_selection": {
                "user_data_dir": candidate.user_data_dir,
                "profile_role": "explicit",
                "clone_source": None,
            },
        }
        manager._spawn_diagnostics[instance_id] = diagnostics
        async with manager._lock:
            manager._instances[instance_id] = {
                "browser": browser,
                "tab": tab,
                "instance": instance,
                "options": options,
                "navigation_count": 0,
                "idle_timeout_seconds": manager._resolve_idle_timeout_seconds(None),
                "spawn_diagnostics": diagnostics,
                "network_data": [],
            }
        instance.state = BrowserState.READY
        instance.last_navigated_url = view["url"]
        instance.last_navigated_title = view["title"]
        instance.update_activity()
        in_memory_storage.store_instance(instance_id, instance.model_dump(mode="json"))
    except BaseException:
        # The connection, not the browser: an adoption that failed must leave the
        # Chrome exactly as it found it, because the caller's fallback is the
        # thing allowed to decide it dies.
        with contextlib.suppress(Exception):
            connection = getattr(browser, "connection", None)
            if connection is not None:
                await connection.aclose()
        raise
