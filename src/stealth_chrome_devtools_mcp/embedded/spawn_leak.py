"""THE one home for "this spawn failed before nodriver handed a ``Browser``
back — WHICH process did it launch, and reap that one" (F-860, F-919).

nodriver's ``Browser.start()`` spawns Chrome, polls ``/json/version`` and, on no
answer, raises without killing what it spawned; the websocket handshake that
follows can time out the same way. Either raise happens INSIDE
``BrowserManager._launch_browser``, so the orchestrator holds no ``Browser`` to
stop, and ``process_cleanup.kill_browser_process`` returns early because the
instance is tracked only in ``_apply_post_launch`` — after a successful launch.
The Chrome that nodriver started therefore outlived the failure, untracked and
invisible to ``list_instances``; on the shared profile it also made every later
caller clone, with nothing to explain why, until a backend restart reaped it.

**What identifies the leaked process is the pid nodriver launched it with**, and
that is a change of ANSWER, not of degree (F-919). F-860 had only the attempt's
``--user-data-dir`` and fenced on a start TIME: kill what is on this directory
and started at or after the attempt began, less a second of clock slack. Its
safety argument was that "a real Chrome already holding an explicit profile
predates the attempt and is spared" — and concurrency falsifies it, because
F-834 measured that concurrent unnamed spawns all select the SHARED profile, by
design. Two of them stamp their launches **9.9-86.4 ms apart** (measured; the
finding's §1), so when the loser fails BECAUSE the winner holds Chrome's profile
singleton — the exact case the paragraph above promises to spare — the winner's
Chrome sits at worst 11x inside the loser's one-second window, and was
terminated. On the shared profile that browser is the operator's logged-in
Chrome, and re-entering those logins is manual work no agent can do.

Shrinking the window is not the fix, and the arithmetic says so on the
constant's own premise: it existed to absorb the kernel's rounding of a process
start time, which its comment put at 10 ms on Linux and ~16 ms on Windows, and
the smallest separation two concurrent spawns were measured at is 9.9 ms. No
value both absorbs the rounding and spares the sibling, because the two
quantities are the same size. A guess with a smaller blast radius is a guess.

So the fence is an IDENTITY now. :class:`Attempt` is the handle the orchestrator
holds across the fallible launch: ``_launch_browser`` stamps the ``uc.Config``
OBJECT it built onto it before awaiting ``uc.start``, and :func:`launched_pid`
reads the pid back off the ``Browser`` nodriver registered for that exact
object. Identity (``is``), never a match on any field — two concurrent spawns on
one directory build configs equal in every field and distinct as objects, which
is the whole point.

Three things then make that pid safe to kill, and each is somebody else's
answer, reused rather than re-spelled:

* ``process_exit.browser_pid`` decides whether the pid is the BROWSER — a pid
  carrying ``--type=`` is one of its children — and refuses a process whose
  ``returncode`` is already set. A stored ``(pid, create_time)`` pair — the
  obvious alternative — is NOT ruled out here by being unobtainable; that claim
  was made and refuted (F-919 §4). It is ruled out on WORTH. It would differ
  from this guard in one band only: the two loop iterations below, and then
  only if a recycled pid had landed on a Chromium-family process carrying our
  own ``--user-data-dir``, which the second witness already excludes. Against
  that it costs a second responsibility inside a seam whose one job is how long
  a launch may take, a blind spot for every failure preceding the first
  ``/json/version``, and a second recycled-pid rule beside this one. What is
  not arguable either way: reading a create_time at REAP time is circular, because
  it says what the pid is NOW, not what it was.

  What the ``returncode`` refusal buys is a pid the OS has not yet freed, and
  the two platforms buy it differently. On **Windows** it is unconditional and
  has nothing to do with ``returncode``: ``subprocess`` keeps the PROCESS
  handle for the life of the ``Popen`` (``subprocess.py``:1575; only the THREAD
  handle is closed, :1577), and Windows will not reuse a pid while a handle to
  it is open. On **POSIX the guard is OPEN for two loop iterations, and open
  deterministically** — not as a race. The kernel frees the pid at
  ``os.waitpid`` on the watcher thread (``asyncio/unix_events.py``:1443), and
  ``returncode`` lands two ``call_soon_threadsafe`` hops later (:1461 -> :231 ->
  ``base_subprocess.py``:237). ``base_events._run_once`` drains exactly
  ``ntodo = len(self._ready)`` entries (:2033-2034), so a callback queued during
  a step cannot run before the next iteration; and the reap has no suspension
  point in front of it (``browser_manager``:583-586 reaches it synchronously).
  So a ``waitpid`` landing inside that band leaves ``returncode`` provably None
  at the reap, and there a stored pair would have done BETTER. What stands in
  the band is the second witness below: the freed pid would have to be recycled,
  within it, onto a Chromium-family process on OUR ``--user-data-dir``. Linux
  and macOS allocate pids sequentially and wrap the whole space before reissuing
  one, so that is not a reachable event — which is why the band is tolerable,
  not a reason it does not exist.
* ``process_cleanup._get_browser_pids_for_profile`` is the second witness: the
  pid must still be a Chromium-family process on the directory we launched it
  on, or it is not the thing we came for.
* ``process_cleanup._kill_process_by_pid`` is the escalating kill, unchanged.

**An answer that cannot be established resolves toward NOT killing**, uniformly
with ``profile_lock._browser_pids`` and with what F-886 and F-888 both chose,
and it is what the deleted ``_started_after`` got right and is remembered for.
The cost is named rather than implied: a Chrome that genuinely leaked but whose
launch we cannot name — a ``Browser`` missing from nodriver's registry, a handle
already collected, a delegated launch — is left RUNNING, and only the next
backend start's orphan reap ends it. That is the direction, because the other
one is the incident: a process killed on a guess is somebody's logins.

**It never raises**: it runs inside a failure handler, and a cleanup failure
that replaces the launch failure the caller needs to see is strictly worse
than a leak that is logged.

A leaf in the sense that matters — it imports no orchestrator, and the
``ProcessCleanup`` arrives as an argument. ``process_exit`` is itself a leaf,
and the ``nodriver`` import costs no cold start: this module is reached only
through ``browser_manager``, which imports nodriver already, and the stdio proxy
imports neither.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nodriver.core.util import get_registered_instances

from stealth_chrome_devtools_mcp.embedded import process_exit
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

if TYPE_CHECKING:
    from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup


@dataclass
class Attempt:
    """One spawn's handle on the Chrome it is about to launch (F-919).

    Orchestrator-owned and passed INTO ``_launch_browser``, never returned from
    it: the moment it is needed is the moment that call raised, and anything
    returned from there goes with the exception.

    ``config`` is the ``uc.Config`` object ``_launch_browser`` built, or None
    when the launch never reached nodriver — a delegated headed launch, which
    kills its own Chrome on failure, or a raise before Chrome could exist.
    """

    config: object | None = None


def launched_pid(attempt: Attempt) -> int | None:
    """The pid of the browser *attempt* launched, or None when we cannot say.

    nodriver sets ``Browser._process`` and adds the ``Browser`` to its registry
    BEFORE it starts polling ``/json/version`` (``core/browser.py``:397-412),
    so the object behind a connect failure is still findable from here — the
    same invariant ``browser_connect._launched_process`` depends on and
    documents, read through a different handle to answer a different question.
    Two short walks over that registry and no shared helper, deliberately: two
    occurrences is not yet a home (``browser_pid_registry._write``'s precedent).

    ``Browser.__init__`` stores the caller's config verbatim (``self.config =
    config``), so ``is`` on that object is an exact answer where every
    field comparison is ambiguous between concurrent siblings.
    """
    if attempt.config is None:
        return None
    for browser in tuple(get_registered_instances()):
        if getattr(browser, "config", None) is attempt.config:
            return process_exit.browser_pid(
                getattr(browser, "_process", None),
                getattr(browser, "_process_pid", None),
            )
    return None


def reap_launched_browsers(
    cleanup: ProcessCleanup,
    user_data_dir: str | None,
    attempt: Attempt,
    instance_id: str,
) -> list[int]:
    """Kill the browser THIS attempt launched on *user_data_dir*, and return the
    pids that were reaped — at most one, and never a sibling's. ``None`` for the
    directory means nothing can be matched, so nothing is walked or killed; a
    launch we cannot name is left alone for the same reason.
    """
    if not user_data_dir:
        return []
    launched = launched_pid(attempt)
    if launched is not None:
        try:
            on_profile = cleanup._get_browser_pids_for_profile(user_data_dir)
        except Exception as error:  # noqa: BLE001  PERMANENT(a reap inside a failure handler must not raise)
            debug_logger.log_warning(
                "spawn_leak",
                "reap",
                f"Could not scan for browsers left by failed spawn "
                f"{instance_id}: {error}",
            )
            return []
        if launched in on_profile:
            return _kill(cleanup, launched, user_data_dir, instance_id)
    debug_logger.log_info(
        "spawn_leak",
        "reap",
        f"Failed spawn {instance_id} has no browser of its own running on "
        f"{user_data_dir}; nothing was reaped (F-919)",
    )
    return []


def _kill(
    cleanup: ProcessCleanup, pid: int, user_data_dir: str, instance_id: str
) -> list[int]:
    """End the one process this attempt launched. Never raises."""
    debug_logger.log_warning(
        "spawn_leak",
        "reap",
        f"Failed spawn {instance_id} left browser pid {pid} running on "
        f"{user_data_dir}; killing it (F-860)",
    )
    try:
        if cleanup._kill_process_by_pid(pid, instance_id):
            return [pid]
    except Exception as error:  # noqa: BLE001  PERMANENT(a reap inside a failure handler must not raise)
        debug_logger.log_warning(
            "spawn_leak",
            "reap",
            f"Could not kill browser pid {pid} left by failed spawn "
            f"{instance_id}: {error}",
        )
    return []
