"""THE one home for "a browser outlived the backend that spawned it — may we
adopt it, where is its CDP endpoint, the door back in, and the pass that walks
through it" (F-888).

Four parts, and they are one module because each exists only to serve the first:
a classification that names nothing to attach to is useless, a door nothing is
allowed through is a second spawn path, and a pass in another file is a second
place the rule gets asked from. What is NOT here is reading a live process's argv
(``browser_cmdline``), the claim's WRITE (``browser_pid_registry``) or the
lifecycle that holds that claim across the attach (``browser_claim``) — three
leaves, each with one question of its own.

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
4. A CDP endpoint is recoverable for it (:func:`cdp_endpoint.endpoint`).
   Without a port there is nothing to attach to.

Conditions 1 and 2 are DECISIONS — a live sibling owns it, or its profile is
disposable — and an entry failing either is the reaper's. Conditions 3 and 4 can
fail because a witness could not be READ, and for an entry already agreed to be
PERSISTENT that is not a decision at all: those answer
:data:`reap_guard.UNDECIDED`, which spares the entry without adopting it
(F-916). :mod:`reap_guard` carries the rule and names what sparing costs.

**The address is not here either.** Which PORT to knock on is
``cdp_endpoint``'s — a three-witness ladder over a RECORD ENTRY, extracted by
F-916 when this file was at its 1000-LOC cap and needed a third answer in it.
It is its own leaf rather than part of the door: ``cdp_attach`` never calls it,
and its witnesses are a recorded field, a live process's argv and a file in a
profile, none of which a websocket knows about.

**The door is not here.** Entering a browser that is already running is a
question ``desktop_launch.launch_and_attach`` had too, so it is ONE leaf with two
consumers: :mod:`cdp_attach`, which owns the nodriver gate (setting BOTH ``host``
and ``port`` on a ``Config`` is what makes ``uc.start`` connect instead of spawn),
the reclaiming attach and the close that drops connections without touching the
process. What this module owns is WHEN to knock and what to do with what answers.

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
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

from stealth_chrome_devtools_mcp.embedded import (
    browser_claim,
    browser_cmdline,
    browser_pid_registry,
    cdp_attach,
    cdp_endpoint,
    desktop_launch,
    reap_guard,
    tab_identity,
    tool_errors,
    window_sizing,
)
from stealth_chrome_devtools_mcp.embedded.browser_claim import Refused
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.in_memory_storage import in_memory_storage
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions, BrowserState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Collection

    from nodriver import Browser

    from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
    from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup

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
    # Chrome was launched pointing at a loopback proxy that nothing is listening
    # on any more — almost always this tool's own authenticated egress forwarder,
    # which lived inside the backend that died. See `_dead_local_proxy`.
    dead_egress: str | None = None


@dataclass(frozen=True)
class Classified:
    """What one pass over the record decided about every entry in it.

    Two sets, because "do not reap this" and "attach to this" are different
    answers and conflating them is how an entry we could not classify would be
    handed to the adopter as though we understood it.

    ``unclassifiable`` has two sources meaning the same thing: an entry that
    made the pass RAISE, and one :data:`reap_guard.UNDECIDED` named because a
    witness could not be read (F-916).
    """

    adoptable: dict[str, Adoptable]
    unclassifiable: set[str]

    @property
    def spare(self) -> set[str]:
        """Every id startup recovery must NOT reap."""
        return set(self.adoptable) | self.unclassifiable


def adoptable(
    entries: browser_pid_registry.Entries,
    *,
    owner_alive: Callable[[int, float | None], bool],
    browser_alive: Callable[[int, float | None], bool],
) -> Classified:
    """Every entry in *entries* this backend may take over, keyed by instance id.

    ONE pass, shared by both consumers: ``process_cleanup`` skips these entries
    instead of reaping them, and ``browser_manager`` attaches to them. Asking the
    question twice in two places is how the reaper and the adopter would come to
    disagree about one browser — which is a killed login on one side and a leaked
    Chrome on the other.

    Never raises, and an entry it cannot reason about resolves toward SPARING it:
    it joins ``unclassifiable``, so the reaper leaves it and the adopter does not
    touch it. Toward a leak, never toward killing what we did not understand —
    the thing being classified may be a human's logged-in Chrome.
    """
    found: dict[str, Adoptable] = {}
    unclassifiable: set[str] = set()
    for instance_id, entry in entries.items():
        try:
            candidate = _adoptable_entry(
                instance_id, entry, owner_alive=owner_alive, browser_alive=browser_alive
            )
        except Exception as exc:  # noqa: BLE001  PERMANENT(one unclassifiable entry must not cost the others, and must never resolve toward killing it)
            # SPARED but NOT adopted, and said out loud. It used to be a bare
            # `contextlib.suppress`, which dropped the entry out of the spared
            # set entirely and let recovery reap a human's logged-in Chrome with
            # nothing written anywhere — and `test_no_silent_excepts` cannot
            # catch that, because it reads `except` blocks, not `suppress`.
            # Shape only in the message (never the
            # profile path, which names the operating user); `error=` carries
            # the traceback (F-869).
            debug_logger.log_warning(
                "browser_reattach",
                "classify",
                f"Could not classify recorded instance {instance_id} "
                f"({type(exc).__name__}); sparing it rather than reaping it, "
                f"and not adopting it either.",
                error=exc,
            )
            unclassifiable.add(instance_id)
            continue
        if isinstance(candidate, Adoptable):
            found[instance_id] = candidate
        elif candidate is not None:
            # UNDECIDED: spared exactly like a raising entry, and for the same
            # reason — we do not know what this is, and it may be a login.
            unclassifiable.add(instance_id)
    return Classified(adoptable=found, unclassifiable=unclassifiable)


def _adoptable_entry(  # noqa: PLR0911  PERMANENT(one early return per condition of the rule; a single composite test would hide which condition refused, and each one carries its own argument)
    instance_id: str,
    entry: browser_pid_registry.Entry,
    *,
    owner_alive: Callable[[int, float | None], bool],
    browser_alive: Callable[[int, float | None], bool],
) -> Adoptable | reap_guard.Undecided | None:
    """The four conditions, in the order the module docstring states them.

    ``None`` is a DECISION: this entry is not one to adopt and the reaper may
    have it. ``UNDECIDED`` is the absence of one, and the reaper may not.
    """
    if not browser_pid_registry.is_reapable(entry, owner_alive):
        return None
    if not browser_pid_registry.on_persistent_profile(entry):
        if browser_pid_registry.persistence_recorded(entry):
            return None
        # The record never SAID, so that False is the reader's default for a
        # pre-2.0.4 shape, not a finding -- measured killing a LIVE Chrome on
        # the shared profile (F-916 §7). Spared only on BOTH halves of the pid's
        # identity, so one whose Chrome is gone still leaves the record.
        create_time = browser_pid_registry.recorded_time(entry, "create_time")
        pid = entry.get("pid")
        if isinstance(pid, int) and create_time is not None:
            return reap_guard.UNDECIDED if browser_alive(pid, create_time) else None
        return None

    # Past here the entry IS persistent — a named profile or the shared one,
    # which is where a human's login lives — so a condition that fails because
    # a witness could not be READ answers UNDECIDED rather than "reap it".
    pid = entry.get("pid")
    profile_dir = entry.get("user_data_dir")
    if not isinstance(pid, int) or not isinstance(profile_dir, str) or not profile_dir:
        return reap_guard.UNDECIDED
    create_time = browser_pid_registry.recorded_time(entry, "create_time")
    if create_time is None:
        # ADOPTION needs BOTH halves of a pid's identity where reaping is content
        # with one (F-888 review M4): the reap's tolerance for a missing
        # create_time would let us take over a stranger's chrome.exe on a
        # recycled pid and kill it at `close_instance`. Left to the holder path,
        # which proves identity by the directory — and since F-916 left ALIVE.
        return reap_guard.UNDECIDED
    if not browser_alive(pid, create_time):
        # The one ESTABLISHED negative, and what lets a dead entry leave the
        # record at all. `recorded_browser_alive` folds a psutil AccessDenied
        # into this False; F-918's guard at the kill catches THAT.
        return None

    port = cdp_endpoint.endpoint(entry)
    if port is None:
        # Alive, ours, persistent — and no door into it. F-916's measured
        # population: 2.1.8/2.1.9 recorded no `cdp_port`, and those are the
        # records holding the stranded logins. Un-adoptable, never reapable.
        return reap_guard.UNDECIDED
    return Adoptable(
        instance_id=instance_id,
        pid=pid,
        user_data_dir=profile_dir,
        port=port,
        dead_egress=browser_cmdline.dead_local_proxy(pid),
    )


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
) -> Classified:
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
    cleanup: ProcessCleanup,
    instance_id: str,
    metadata: dict[str, object],
    protected_pids: frozenset[int] = frozenset(),
) -> bool:
    """Kill one recorded browser and remove its profile if it is disposable.

    THE one home for "reap this entry". Startup recovery calls it per orphan and
    a failed adoption calls it as its fallback, so a browser we could neither
    adopt nor reach still ends exactly where 2.1.9 left it rather than leaking
    because a new code path spared it. Dropping the ENTRY is the caller's,
    because recovery drops a whole pass in one merge-write.

    *protected_pids* is every pid the caller has already decided to spare. The
    kill set is built by scanning this entry's DIRECTORY and two entries may
    legitimately share one, so without it a reap reaches browsers its own caller
    protected a line earlier (F-917).

    Returns True when a process was actually killed.
    """
    killed = cleanup._kill_processes_for_metadata(
        instance_id, metadata, recovery=True, protected_pids=protected_pids
    )
    cleanup._cleanup_profile_for_metadata(instance_id, metadata)
    return killed


def held_by(
    user_data_dir: str,
    *,
    read_entries: Callable[[], browser_pid_registry.Entries],
    owner_alive: Callable[[int, float | None], bool],
    live_pids: Callable[..., Collection[int] | None] | None,
    new_instance_id: str,
) -> Adoptable | None:
    """The live Chrome holding *user_data_dir* that we may take over, or None.

    THE second entry point into the one adoption rule, and the record is NOT its
    input — because the browser this exists for **has no record entry at all**.
    Measured on the stranded Seller Central Chrome (pid 115652, port 9223): its
    owner backend died and the SUCCESSOR rewrote ``browser_pids.json`` without it,
    so :func:`run`, which walks entries, would walk past it forever. The only
    thing that still names it is the DIRECTORY it holds — which since F-896 is what
    ``spawn_browser`` hands us for a ``session`` NAME too, already anchored (§5).

    So the witness is Chrome's own process singleton, through
    ``profile_lock.profile_hold`` (F-871's one home), which answers "is this
    directory held, and by whom" — the BROWSER since F-931, because that module
    asks ``browser_cmdline`` itself; see the call site for why this one still
    asks too. The record is consulted only to REFUSE — an entry
    naming this pid whose owner is a live backend of ours is a sibling's browser,
    and taking it is F-886's harm from the other side — and, when a DEAD owner's
    entry names it, to donate that entry's instance id.

    **Three outcomes, and they are deliberately distinguishable.** An
    ``Adoptable``; ``None``, which means "nothing holds this directory" and is
    the ordinary spawn with nothing to report; and ``Refused``, which means a
    browser IS there and we did not take it — for one of three reasons, each
    named in its message, because ``spawn_diagnostics.reattach_declined`` is the
    only place an operator can learn that the login they were reaching for is
    still running.

    Deliberately NOT gated on :func:`browser_pid_registry.on_persistent_profile`:
    that predicate reads a RECORD, and there is no record here. The caller
    passing an explicit ``user_data_dir`` is the same declaration by hand — a
    disposable auto-clone is one the server chose, and a caller naming it is
    naming a directory they intend to keep.
    """
    from stealth_chrome_devtools_mcp.embedded import profile_lock

    hold = profile_lock.profile_hold(Path(user_data_dir), live_pids)
    if hold is None or not isinstance(hold.pid, int):
        # Nothing holds it, or no witness could name the pid (Windows' bare
        # `lockfile`): with no pid there is no command line to read and no
        # process to prove alive, so the caller spawns, exactly as before.
        return None

    # Not redundant with `profile_hold`'s own F-931 ask: that one is "is
    # anything there", this is "which member is the browser, and is there
    # EXACTLY ONE" (F-888 measured an ambiguous tree deciding on set order).
    # Asked of the set the hold already READ; a lock-derived hold carries none.
    members = hold.members or (
        live_pids(user_data_dir) if callable(live_pids) else (hold.pid,)
    )
    found = browser_cmdline.browser_members(members, user_data_dir)
    holder = found.sole
    if holder is None:
        # The hold's OWN sentence, never a second claim composed here (F-931
        # M2), then WHICH of `sole`'s two Nones this is: with TWO browsers,
        # "none identified" contradicted the hold it had just quoted (M4).
        detail = (
            f"{len(found.browsers)} of its processes are browsers (pids "
            f"{', '.join(map(str, found.browsers))}), so Chrome's process "
            f"singleton did not hold and entering one could drive a profile "
            f"another owns"
            if found.browsers
            else "none of its processes could be identified as the browser "
            "itself, so there was nothing safe to attach to"
        )
        raise Refused(f"{hold.reason}, but {detail}; it was left alone")

    # The record is read ONLY once something is known to hold the directory —
    # hence a callable: the common spawn is onto a directory nobody holds, and
    # must not pay for a record read it cannot use.
    entries = read_entries()
    recorded_id: str | None = None
    for instance_id, entry in entries.items():
        if entry.get("pid") != holder:
            continue
        # The SAME ownership rule `run` applies, inverted: not reapable means a
        # live backend of ours still owns this browser, and two backends driving
        # one Chrome is F-886's harm. Asked through `is_reapable` rather than
        # re-read here, so the two entry points cannot come to disagree.
        #
        # RAISED, not returned as None, and it is the same `Refused` the claim
        # raises one step later: this is the case an operator has to be TOLD
        # about — their browser is still there and the remedy is to stop that
        # backend — while a None here means "an ordinary spawn, nothing to say",
        # and the caller's diagnostics could not tell the two apart.
        if not browser_pid_registry.is_reapable(entry, owner_alive):
            raise Refused(
                f"a live backend of ours already owns the browser holding that "
                f"directory (pid {holder}); two backends driving one Chrome is "
                f"the defect F-886 fixed, so it was left alone. Stop that backend "
                f"first — see RUNBOOK, 'Recover a stranded login'"
            )
        recorded_id = instance_id
        break

    entry_for_port: browser_pid_registry.Entry = (
        dict(entries[recorded_id])
        if recorded_id is not None
        else {"pid": holder, "user_data_dir": user_data_dir}
    )
    port = cdp_endpoint.endpoint(entry_for_port)
    if port is None:
        # A holder was FOUND and we cannot get in: the one outcome that must not
        # read as "an ordinary spawn", and it is left running (this path never
        # reaps). Not what the spawn does NEXT — F-915 appends this text to its
        # refusal, and "a new browser was started instead" contradicted that.
        raise Refused(
            f"a live browser holds that directory (pid {holder}) but no CDP "
            f"endpoint could be recovered for it — no port in the record, none "
            f"on its command line, and no DevToolsActivePort file — so it was "
            f"left alone"
        )
    return Adoptable(
        instance_id=recorded_id or new_instance_id,
        pid=holder,
        user_data_dir=user_data_dir,
        port=port,
        dead_egress=browser_cmdline.dead_local_proxy(holder),
    )


# One lock per requested DIRECTORY, because the directory is what two concurrent
# spawns collide on: without it both read the same live holder, both attach, and
# one Chrome is registered as two instances that then close each other. Keyed on
# the normalized path through the record's own `normalize_path`, so `C:\P` and
# `c:\p\` are one lock.
#
# Deliberately NOT `clone_storage`'s protected-dir set: that is sweep EXEMPTION —
# a membership test with no waiting — and a mutex is not what it offers, so
# reusing it would mean building the exclusion beside it anyway. And deliberately
# per directory rather than the module-wide `_pass_lock` below: an unrelated
# spawn must not wait out a wedged browser's whole ATTACH_BUDGET_SECONDS.
#
# The lock is in-PROCESS only, and is not what makes adoption safe; that is
# `claim`, which two backends and two tasks alike must pass. This only keeps the
# common case cheap by not sending a second task down a path the claim will
# refuse.
_directory_locks: dict[str, asyncio.Lock] = {}


def _directory_lock(user_data_dir: str) -> asyncio.Lock:
    """THE lock two spawns naming one directory serialize on."""
    key = browser_pid_registry.normalize_path(user_data_dir) or user_data_dir
    lock = _directory_locks.get(key)
    if lock is None:
        # No await between the miss and the insert, so this is atomic for the
        # loop; two tasks cannot both create one.
        lock = _directory_locks.setdefault(key, asyncio.Lock())
    return lock


@dataclass(frozen=True)
class Held:
    """What the spawn path's re-attach question answered.

    Two fields, because "it was not taken" is not the same statement as "there
    was nothing to take": a spawn that meets a live browser on the directory the
    caller named owes that caller a reason — since F-915 the refusal carries
    this text, and a silent None would leave that refusal unexplained.
    """

    instance_id: str | None = None
    # Why the re-attach was NOT taken; None when it was, and None when nothing
    # held the directory at all (the ordinary spawn, which has nothing to say).
    declined: str | None = None


async def adopt_held_profile(  # noqa: PLR0911  PERMANENT(each return is a DIFFERENT thing to tell the caller — adopted, already ours, refused by the rule, unreachable, unclassifiable, or an ordinary spawn — and folding them costs exactly the distinction this function exists to report)
    manager: BrowserManager,
    cleanup: ProcessCleanup,
    user_data_dir: str,
    *,
    ignored_args: list[str] | None = None,
) -> Held:
    """Re-attach to the live Chrome holding *user_data_dir*, or say why not.

    The spawn path's one question, asked BEFORE profile selection because that
    is where a held directory is settled: since F-915 a holder we cannot reach
    is REFUSED and one we drive is copied with its jar handed over, and F-871's
    silent walk to a logged-out ``<name>-2`` is what both of those replaced.

    **A failure here never reaps.** That is the one place this differs from
    :func:`run`, and the difference is the caller's intent: `run` is startup
    recovery, where an unreachable orphan must end somewhere, and the fallback is
    the reap 2.1.9 already did. Here a CLIENT asked for a browser, and killing
    the Chrome they were trying to reach because we could not attach to it would
    be this finding's own harm committed by the fix. Every failure returns None
    and the spawn proceeds exactly as it does today.

    *ignored_args* names the spawn arguments the caller passed that describe a
    LAUNCH, which a browser already running cannot be given; they are reported in
    the diagnostics rather than refused, because refusing over a viewport is the
    walk to ``<name>-2`` and the lost login all over again.

    Never raises.
    """
    async with _directory_lock(user_data_dir):
        try:
            candidate = await asyncio.to_thread(
                held_by,
                user_data_dir,
                read_entries=cleanup._load_tracked_pids,
                owner_alive=cleanup._owner_backend_alive,
                live_pids=getattr(cleanup, "_get_browser_pids_for_profile", None),
                new_instance_id=str(uuid.uuid4()),
            )
        except Refused as exc:
            # The rule working, and the ONE case a caller has to be told about:
            # their browser is alive, we did not touch it, and the remedy is to
            # stop the backend that owns it.
            report("held", f"Not adopting the holder of the requested profile: {exc}")
            return Held(declined=str(exc))
        except Exception as exc:  # noqa: BLE001  PERMANENT(a classification failure must cost the spawn nothing but this branch)
            debug_logger.log_warning(
                "browser_reattach",
                "held",
                f"Could not classify the holder of the requested profile "
                f"({type(exc).__name__}); spawning instead.",
                error=exc,
            )
            return Held(
                declined=f"the holder of that directory could not be classified "
                f"({type(exc).__name__})"
            )
        if candidate is None:
            # Nothing holds the directory, or something does and no witness
            # could name a pid at all (Windows' bare `lockfile`). Both are the
            # ordinary spawn with nothing to report; every case where a browser
            # was FOUND and not taken arrives as `Refused` above, so a silent
            # None can no longer stand for one. The F-871 walk and its
            # `walk_reason` are untouched either way.
            return Held()
        async with manager._lock:
            running = candidate.instance_id in manager._instances
        if running:
            return Held(instance_id=candidate.instance_id)
        try:
            adopted_id = await asyncio.wait_for(
                _adopt_one(
                    manager,
                    cleanup,
                    candidate,
                    extra_diagnostics={"ignored_spawn_args": ignored_args or []},
                ),
                timeout=ATTACH_BUDGET_SECONDS,
            )
        except Refused as exc:
            # The rule working, not a failure: a sibling backend claimed this
            # browser between `held_by` and the claim. Reported as the remedy it
            # is, at INFO, because nothing is wrong with this machine.
            report("held", f"Not adopting the holder of the requested profile: {exc}")
            return Held(declined=str(exc))
        except Exception as exc:  # noqa: BLE001  PERMANENT(see the docstring: a failed adoption here must never kill the caller's browser)
            debug_logger.log_warning(
                "browser_reattach",
                "held",
                f"A live browser holds the requested profile on port "
                f"{candidate.port} but could not be re-attached to "
                f"({type(exc).__name__}); it was left running and untouched, "
                f"and since F-915 a held session we cannot reach is REFUSED.",
                error=exc,
            )
            return Held(
                declined=f"a live browser (pid {candidate.pid}) holds that "
                f"directory on CDP port {candidate.port}, but re-attaching to it "
                f"failed ({type(exc).__name__}); it was left running and untouched"
            )
        report(
            "held",
            f"Re-attached to the running browser already holding the requested "
            f"profile rather than spawning beside it; instance {adopted_id}",
        )
        return Held(instance_id=adopted_id)


# ---------------------------------------------------------------------------
# The pass: attach to everything `adoptable_for` names, under the manager that
# will hold it afterwards.
# ---------------------------------------------------------------------------

# The RECORD pass's lock, and it is not about two MCP sessions: `app_lifespan`
# runs this once per process, which the F-856 lifespan guard already enforces —
# measured, not assumed. What it covers is a second caller of `run` (a test, a
# future heal-time trigger) overlapping the first, where the record is re-read
# and every entry would be attached to twice. Module level, not per manager,
# because there is one BrowserManager per backend and the record is shared.
#
# It is NOT the spawn path's lock: that one serializes per DIRECTORY (see
# `_directory_lock`), so a spawn never waits out a whole-record pass.
_pass_lock = asyncio.Lock()


def start(manager: BrowserManager, cleanup: ProcessCleanup) -> asyncio.Task | None:
    """Schedule the adoption pass off the first-serve path.

    Fire-and-forget for F-856's reason and on the precedent of the sibling line
    beside it in ``app_lifespan`` (``clone_storage.spawn_background_sweep``): the
    work reaches a browser over CDP, a wedged one costs a whole
    ``ATTACH_BUDGET_SECONDS``, and nothing about a backend's readiness depends on
    it. A client sees an adopted instance appear in ``list_instances`` a moment
    after the backend answers, rather than a backend that answers late.

    "Nothing about readiness depends on it" has to be true of the SCHEDULING too,
    not just the work: this is called from ``app_lifespan``, so anything that
    escapes here fails the lifespan and the backend never serves at all. A
    failure to schedule therefore costs the adoption and nothing else, and says
    so — the browsers are not lost, the next backend's pass finds them.
    """
    coro = run(manager, cleanup)
    try:
        return manager._run_in_background(coro, "reattach")
    except Exception as exc:  # noqa: BLE001  PERMANENT(scheduling the pass must never be able to fail a backend's startup; see the docstring)
        coro.close()
        debug_logger.log_warning(
            "browser_reattach",
            "start",
            f"Could not schedule the re-attach pass ({type(exc).__name__}); "
            f"browsers a dead backend left running stay running and untracked by "
            f"this backend until one is spawned onto their profile or a later "
            f"backend starts.",
            error=exc,
        )
        return None


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

    Idempotent, and serialized by ``_pass_lock``. ``app_lifespan`` runs it ONCE
    per process (``server.py``'s ``_LIFESPAN_STARTED``), so the lock is not about
    two MCP sessions; it is what keeps a future second caller from opening two
    connections to one Chrome. A successful adoption re-stamps the entry's owner
    to US, so any later classification finds nothing.

    Never raises, and it reaps on ONE class of failure only: evidence about the
    BROWSER — the attach was refused, it reports no tab, it is wedged — logged at
    WARNING and reaped exactly as 2.1.9's startup recovery would have reaped it.
    A ``Refused`` (a sibling backend claimed it first, or the claim could not be
    written at all) is the opposite instruction and is handled above the blanket
    handler: the entry and the browser are left exactly as they are.
    """
    async with _pass_lock:
        # ONE read, threaded into both halves: a second read could disagree
        # about an entry re-recorded between them (`cli.py`'s per-line read).
        entries = await asyncio.to_thread(cleanup._load_tracked_pids)
        classified = await asyncio.to_thread(adoptable_for, cleanup, entries)
        # **`.spare`, never `.adoptable`** (B1): the reap below kills by
        # DIRECTORY and these entries share one, so it must be told every pid
        # this pass decided to keep -- the adoptable ones PLUS everything F-916
        # could not classify. Same `reap_guard.spared_pids` `process_cleanup`
        # asks; two subtraction sites disagreeing is F-917's shape re-made.
        candidate_pids = reap_guard.spared_pids(entries, classified.spare)
        adopted: list[str] = []
        failed: set[str] = set()
        for instance_id, candidate in classified.adoptable.items():
            # Under the manager's own lock, because a spawn on another task may
            # be publishing into `_instances` while this pass reads it, and the
            # answer decides whether we open a second connection to one Chrome.
            async with manager._lock:
                already = instance_id in manager._instances
            if already:
                continue
            try:
                await asyncio.wait_for(
                    _adopt_one(manager, cleanup, candidate),
                    timeout=ATTACH_BUDGET_SECONDS,
                )
            except Refused as exc:
                # THE one outcome that must never reach the reap below, and the
                # two shapes of it are one class on purpose (`Undecided` is a
                # subclass): a sibling backend legitimately claimed this browser
                # first, or the claim could not be written at all. Neither is
                # evidence about the BROWSER.
                #
                # Reaping here was strictly worse than the race it was added to
                # fix. Backends B and C start together and both classify entry E
                # adoptable from their own snapshot; C claims first and adopts;
                # B's claim is refused, and B would then kill every browser on
                # that profile predating its own `_init_time` — which C's adopted
                # browser does — and drop the entry C has just re-stamped. The
                # login dies, C holds a handle to a corpse, and nothing on disk
                # names it. A lost claim is the rule WORKING; it leaves the entry
                # and the browser exactly as they are.
                report("reattach", f"Left to its owner: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001  PERMANENT(a startup background pass must never raise; every failure that is evidence about the BROWSER has the one remedy below)
                # Blind on purpose BELOW the refusal above: this pass runs on a
                # background task at startup and MUST never raise. What is left
                # after `Refused` is evidence about the browser — a refused
                # connect, no tab, a wedged Chrome, a nodriver change — and all
                # of it has the same remedy, the reap, so narrowing further would
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
                    candidate_pids - {candidate.pid},
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


def claim(
    cleanup: ProcessCleanup, candidate: Adoptable
) -> browser_pid_registry.Claimed | None:
    """Take the record's ownership of *candidate*, or None if a sibling has it.

    The one thing that makes adoption safe between PROCESSES, taken BEFORE the
    door; :func:`browser_pid_registry.claim_browser` carries the argument for why
    the decision and the stamp must happen inside one locked read-merge-write.
    Both entry points call it, which is also what answers "two concurrent spawns
    onto one held directory": the second reads a LIVE owner — us — and is refused.
    The id it answers with is authoritative and may not be the one asked for.
    """
    owner_pid, owner_create_time = browser_pid_registry.owner_identity()
    return browser_pid_registry.claim_browser(
        cleanup.pid_file,
        pid=candidate.pid,
        entry=browser_pid_registry.new_entry(
            candidate.pid,
            create_time=_create_time(candidate.pid),
            user_data_dir=candidate.user_data_dir,
            uses_custom_data_dir=True,
            auto_clone=False,
            cdp_port=candidate.port,
        ),
        instance_id=candidate.instance_id,
        owner_pid=owner_pid,
        owner_create_time=owner_create_time,
        owner_alive=cleanup._owner_backend_alive,
    )


def _create_time(pid: int) -> float | None:
    """*pid*'s start time, or None — the second half of a pid's identity."""
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return None


async def _adopt_one(
    manager: BrowserManager,
    cleanup: ProcessCleanup,
    candidate: Adoptable,
    *,
    extra_diagnostics: dict[str, object] | None = None,
) -> str:
    """Attach to one running browser, register it, and answer its instance id.

    Raises on any failure, which is what routes a RECORD-path entry to the
    caller's reap — and on the held-profile path is caught and turned into an
    ordinary spawn instead. The registration order matches ``spawn_browser``'s
    deliberately: nothing is published into the manager's instances until the tab
    answers, so a half-adopted browser is never visible to a tool body.

    The claim comes FIRST, before a single byte reaches Chrome, because the thing
    it protects against is another backend doing all of this at the same time.
    ``browser_claim.held`` is what holds it across the attach and hands it back on
    every path that does not end in ownership — including the cancellation the
    caller's ``ATTACH_BUDGET_SECONDS`` delivers, which is why the claim is taken
    INSIDE that block and not before it. The connection's own teardown is the
    INNER handler and the claim's is the outer one, in that order deliberately:
    the browser must be let go of before the record stops saying it is ours.
    """
    async with browser_claim.held(
        lambda: claim(cleanup, candidate),
        lambda landed: browser_pid_registry.release_claim(cleanup.pid_file, landed),
        pid=candidate.pid,
    ) as claimed:
        instance_id = claimed.instance_id
        return await _attach_one(
            manager,
            cleanup,
            candidate,
            instance_id,
            extra_diagnostics=extra_diagnostics,
        )


async def _attach_one(
    manager: BrowserManager,
    cleanup: ProcessCleanup,
    candidate: Adoptable,
    instance_id: str,
    *,
    extra_diagnostics: dict[str, object] | None = None,
) -> str:
    """Open the connection, publish the instance, and answer its id.

    Split from the claim only so each failure has ONE handler: this one closes
    the CONNECTION and never the browser — an adoption that failed must leave the
    Chrome exactly as it found it, because the caller's fallback is the thing
    allowed to decide it dies. The registration order matches ``spawn_browser``'s
    deliberately: nothing is published into the manager's instances until the tab
    answers, so a half-adopted browser is never visible to a tool body.
    """
    browser: Browser | None = None
    try:
        browser = await cdp_attach.attach_reclaiming(
            cdp_attach.config_for(candidate.user_data_dir, candidate.port),
            candidate.pid,
        )
        try:
            tab = browser.main_tab
        except IndexError as exc:
            # How nodriver actually reports "no targets": `main_tab` is
            # `sorted(self.targets, …)[0]` and raises — it cannot return None,
            # so a None-check here would be pinning a shape the library cannot
            # produce (F-888 review L2). Named rather than re-raised bare, and
            # raised INSIDE the try because the handler below is what closes the
            # connection we just opened.
            raise tool_errors.ToolError(
                f"Re-attached browser for {instance_id} reports no tab"
            ) from exc
        view = await tab_identity.refreshed(browser, tab)
        options = BrowserOptions(
            user_data_dir=candidate.user_data_dir, auto_clone=False
        )
        instance = manager._build_instance(instance_id, options)
        # What `_build_instance` filled in from OPTIONS is a request nobody made
        # here: this browser was launched by a process that is gone. `headless`
        # comes off the holder's own command line and the window is MEASURED (and
        # never re-sized — that would move a human's window); `user_agent` stays
        # None, which is this model's "not known", and is named in `not_restored`.
        instance.headless = browser_cmdline.is_headless(candidate.pid)
        measured = await window_sizing.measure(tab)
        if measured is not None:
            instance.viewport = measured
        # Exactly what a spawn does at this point, and for the same reason the
        # tool body sets interception up: an adopted tab carries none of THIS
        # backend's handlers, so a hook created against the adopted instance
        # would otherwise be registered and never fire.
        await manager._setup_dynamic_hooks(tab, instance_id)
        # Ownership is already ours from the claim; this re-records the entry
        # through the ONE write protocol and adds the in-memory tracking the
        # claim (a record write) could not.
        cleanup.track_browser_process(
            instance_id,
            desktop_launch.pid_shim(browser),
            user_data_dir=candidate.user_data_dir,
            uses_custom_data_dir=True,
            auto_clone=False,
            cdp_port=candidate.port,
        )
        diagnostics: dict[str, object] = {
            "reattached": True,
            "reattached_pid": candidate.pid,
            "user_data_dir": candidate.user_data_dir,
            "cdp_port": candidate.port,
            "window_size": {"actual": measured, "measured": measured is not None},
            # Said out loud rather than fabricated: per-instance state that lived
            # in the backend that died and cannot be read back off a running
            # browser. `block_resources` and dynamic hooks are NOT here — both are
            # re-established above and at the tool body.
            "not_restored": ["extra_headers", "timezone_id", "user_agent", "proxy"],
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
        if candidate.dead_egress:
            # Adopted anyway, and stamped loudly. Refusing would convert a
            # recoverable logged-in browser into a reap on the record path, which
            # is this finding's own harm; the forwarder died with the backend that
            # owned it, so every request through it now fails at the network
            # layer, where the failure is at least visible to the caller.
            diagnostics["dead_egress_proxy"] = candidate.dead_egress
            debug_logger.log_warning(
                "browser_reattach",
                "reattach",
                f"Instance {instance_id} was launched pointing at a local proxy "
                f"({candidate.dead_egress}) that nothing is listening on — almost "
                f"certainly this tool's authenticated forwarder, which died with "
                f"the backend that owned it. Adopting anyway; its page loads will "
                f"fail until it is closed and re-spawned with the same proxy.",
            )
        if extra_diagnostics:
            diagnostics.update(extra_diagnostics)
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
        if browser is not None:
            await cdp_attach.close(browser)
        raise
    return instance_id
