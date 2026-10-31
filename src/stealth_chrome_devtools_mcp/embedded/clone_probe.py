"""T-1665: the probe and the first-failure capture for DISPOSABLE clones.

**Why.** On 2026-10-24 three disposable clones stayed frozen for 17 h while the same
backend spawned and closed others and the fleet browser answered a fresh
``Browser.getVersion`` in 1.7 s. ``browser_health`` probes persistent profiles only
(``BrowserHealth._persistent``), so the clones had no probe, no capture and no heal.
Two causes were left standing: Chrome is hung, or THIS backend's own CDP connection
(a dead nodriver listener, an abandoned transaction) or a stuck
``Fetch.requestPaused`` handler is. One failing clone settles which, if the evidence
is taken at the first miss.

**Capture only.** A clone that misses a probe is never healed, closed or relaunched
here: it is the caller's browser. ``browser_health`` hands :meth:`CloneProbe.check`
the live instances that are not persistent, and each is probed and judged by THE one
judgement (``BrowserHealth._judged``, injected: the direct probe, then F-1007's idle
canary and browser-process answer). A miss on a STARVED host is a WARNING that
consumes nothing, within ``scheduling_lag.ExcuseBudget``'s ceiling like the persistent
path. The round shares ONE idle canary (it is the host's scheduling lag, not a clone's)
and runs at most :data:`PROBES_AT_ONCE` clones at a time, so N clones cost one canary
process and four websockets, and our own probes do not load the host the canary reads.
On the FIRST other miss of an incident, one ERROR line records, counts and states only:

* ``fresh_cdp`` / ``own_cdp``: ``Browser.getVersion`` over a NEW connection and over
  this backend's own. Fresh answers and own does not: the backend's connection is the
  cause. Neither answers: Chrome is hung. Both answer: the probe's own steps (``steps``)
  say where it stopped.
* ``listener``: the browser connection's listener task (``running``, ``ended`` — the
  socket closed, ``cancelled``, ``died(<type>)``) with its socket, and how many of
  the page connections' listeners are running.
* ``paused_fetch``: ``DynamicHookSystem.paused_in_flight`` — ``Fetch.requestPaused``
  handlers started and not finished, i.e. requests this backend is holding.

The record is kept in :attr:`CloneProbe.records` until the clone answers again or is
gone, and is the ERROR line's text. A probe that passes ends the incident
(``answers probes again after N min`` at INFO). **T-11617:** an incident that goes on
is not silent: the record also counts the consecutive failed rounds, and from the
:data:`ROUNDS_BEFORE_REPEAT`-th one logs ``still unresponsive since HH:MM:SS`` at
ERROR, again every :data:`REPEAT_SECONDS` while it lasts, with no new capture and no
extra CDP call. A round the starvation excuse covers advances nothing. Every failure
the judgement does not excuse counts, not only a dead browser process: the judgement
already is the starvation test, and the page-level hang the owner sees is one too (the
first miss's record says which). :meth:`CloneProbe.open_incidents` is what
``list_instances`` shows. The whole round is bounded by
:data:`ROUND_SECONDS`, so frozen clones cannot hold up the next persistent probe; a
clone the round never reached is probed FIRST in the next one (then the least recently
started), so a fleet larger than one round covers is rotated, not starved; one it cut
off mid-probe keeps its place in that order. No URL, title or cookie is ever read.
"""

import asyncio
import contextlib
import dataclasses
import itertools
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol

from nodriver import Browser
from nodriver.core.connection import Connection

from stealth_chrome_devtools_mcp.embedded import freeze_capture, scheduling_lag
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.dynamic_hook_system import dynamic_hook_system
from stealth_chrome_devtools_mcp.embedded.forensic_errors import FrozenCloneError

_COMPONENT = "clone_probe"

#: How long each fresh-connection and own-connection ``Browser.getVersion`` gets.
ANSWER_SECONDS = freeze_capture.FRESH_ANSWER_TIMEOUT_SECONDS

#: Clones probed at once: websockets open, and the load our own probes put on the host.
PROBES_AT_ONCE = 4

#: The bound on one whole round: the canary's start (up to 5 s) and three waves of
#: :data:`PROBES_AT_ONCE` frozen clones at their worst (a 15 s probe, then a 5 s
#: capture), so twelve frozen clones fit in one round; more are rotated (see
#: :meth:`CloneProbe.check`). Healthy ones take milliseconds.
ROUND_SECONDS = 70.0

#: Consecutive failed rounds, the first included, before an open incident says it is
#: still going: one more than the two a persistent browser gets before its heal, so a
#: blip is never reported twice, and ~4 min at the 120 s round.
ROUNDS_BEFORE_REPEAT = 3

#: How often an incident that goes on says so again: often enough that a hang of hours
#: is a handful of lines, rare enough that the Sentry issue is not a stream.
REPEAT_SECONDS = 1800.0

#: What :func:`listener_state` says when the attribute it reads is not there.
_NO_LISTENER_ATTRIBUTE = "unknown(no _listener_task)"


class InstanceSource(Protocol):
    """What a round needs of the manager (only ``browser`` is read)."""

    async def get_instance(self, instance_id: str) -> Mapping[str, Browser] | None: ...


def _task_state(task: asyncio.Task[object] | None) -> str:
    """``running`` / ``ended`` / ``cancelled`` / ``died(<type>)`` / ``none``."""
    if task is None:
        return "none"
    if not task.done():
        return "running"
    if task.cancelled():
        return "cancelled"
    error = task.exception()
    return "ended" if error is None else f"died({type(error).__name__})"


def listener_state(browser: Connection) -> str:
    """The browser connection's listener (``Connection._listener_task``, nodriver
    0.50: it ENDS, without an error, when the socket closes) and its socket, plus
    how many page connections still have a running listener."""
    if not hasattr(browser, "_listener_task"):
        return _NO_LISTENER_ATTRIBUTE  # a nodriver rename, not a dead listener
    socket = getattr(browser, "socket", None)
    code = getattr(socket, "close_code", None)
    wire = "none" if socket is None else "closed" if code is not None else "open"
    tabs = list(getattr(browser, "tabs", None) or [])
    running = sum(
        1
        for tab in tabs
        if _task_state(getattr(tab, "_listener_task", None)) == "running"
    )
    state = _task_state(browser._listener_task)
    return f"browser={state} socket={wire} tabs={running}/{len(tabs)}"


@dataclasses.dataclass
class Incident:
    """One clone's open incident: what the first miss captured, and how it goes on."""

    record: str  # the first miss's capture: the ERROR line's text
    since: datetime  # wall clock of the first miss (aware), for display
    started: float  # ``clock()`` of the first miss, for how long
    rounds: int = 1  # consecutive failed rounds the excuse did not cover
    said: float | None = None  # ``clock()`` of the last "still unresponsive" line

    def minutes(self, now: float) -> str:
        return f"{(now - self.started) / 60:.0f} min"


class CloneProbe:
    """Probes disposable clones and records the first failure of each incident."""

    def __init__(  # noqa: PLR0913  PERMANENT(T-1665: one seam per external effect, so the round runs hermetically)
        self,
        manager: InstanceSource,
        judge: Callable[
            [
                Mapping[str, Browser],
                freeze_capture.TimedSteps,
                scheduling_lag.IdleCanary,
            ],
            Awaitable[tuple[str | None, str | None]],
        ],
        fresh_check: Callable[[str | None], Awaitable[None]],
        own_check: Callable[[Browser], Awaitable[None]],
        *,
        paused: Callable[[str], int] = dynamic_hook_system.paused_in_flight,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._manager = manager  # get_instance is read when a round runs, not here
        self._judge = judge  # (failure, starved) of one probe, or (None, None)
        self._fresh_check = fresh_check
        self._own_check = own_check
        self._paused = paused
        self._clock = clock
        self._excuses = scheduling_lag.ExcuseBudget(clock)
        self._slots = asyncio.Semaphore(PROBES_AT_ONCE)
        self._ticks = itertools.count(1)
        self._started: dict[str, int] = {}  # instance id -> tick of its last start
        #: instance id -> its current incident (the record taken at its first miss).
        self.records: dict[str, Incident] = {}

    def open_incidents(self) -> dict[str, dict[str, object]]:
        """What ``list_instances`` adds to the entry of a clone that is failing now."""
        return {
            iid: {
                "unresponsive_since": incident.since.astimezone(UTC).isoformat(
                    timespec="seconds"
                ),
                "failed_rounds": incident.rounds,
            }
            for iid, incident in self.records.items()
        }

    async def check(
        self,
        instance_ids: list[str],
        canary: Callable[
            [], contextlib.AbstractAsyncContextManager[scheduling_lag.IdleCanary]
        ],
    ) -> None:
        """One round over *instance_ids* (the live, non-persistent instances); the
        ones that are no longer among them are forgotten. *canary* is entered ONCE and
        the one canary is handed to every probe. One clone's probe raising costs that
        clone its round and nobody else theirs; the round ends at
        :data:`ROUND_SECONDS` whatever the clones do, and the clones it did not reach
        (``_started`` tick 0) go first next time."""
        known = set(self.records) | self._excuses.tracked() | set(self._started)
        for gone in known - set(instance_ids):
            self.records.pop(gone, None)
            self._started.pop(gone, None)
            self._excuses.reset(gone)
        if not instance_ids:
            return
        order = sorted(instance_ids, key=lambda iid: self._started.get(iid, 0))
        try:
            async with asyncio.timeout(ROUND_SECONDS), canary() as shared:
                await asyncio.gather(*(self._guarded(iid, shared) for iid in order))
        except TimeoutError:
            debug_logger.log_warning(
                _COMPONENT, "check", f"round cut off after {ROUND_SECONDS:.0f}s"
            )

    async def _guarded(
        self, instance_id: str, canary: scheduling_lag.IdleCanary
    ) -> None:
        try:
            async with self._slots:
                self._started[instance_id] = next(self._ticks)
                await self._check(instance_id, canary)
        except Exception as error:  # noqa: BLE001  PERMANENT(T-1665: evidence is best effort; a clone's probe may not end the round nor skip the others)
            debug_logger.log_warning(
                _COMPONENT, "check", f"{instance_id}: {type(error).__name__}"
            )

    async def _check(self, instance_id: str, canary: scheduling_lag.IdleCanary) -> None:
        data = await self._manager.get_instance(instance_id)
        if data is None:
            return
        browser = data["browser"]
        steps = freeze_capture.TimedSteps()
        failure, starved = await self._judge(data, steps, canary)
        if failure is None:
            self._excuses.reset(instance_id)
            if (over := self.records.pop(instance_id, None)) is not None:
                debug_logger.log_info(
                    _COMPONENT,
                    "probe",
                    f"{instance_id} answers probes again after "
                    f"{over.minutes(self._clock())} unresponsive "
                    f"({over.rounds} rounds)",
                )
            return
        if await self._manager.get_instance(instance_id) is not data:
            return  # closed while we probed: no verdict on a browser that is gone
        spent = self._excuses.grant(instance_id) if starved else None
        if (incident := self.records.get(instance_id)) is not None:
            if spent is None:  # a round the excuse covers advances nothing
                self._goes_on(instance_id, incident, failure)
            return  # the incident is already on record: no second capture
        if spent is not None:
            debug_logger.log_warning(
                _COMPONENT,
                "probe",
                f"{instance_id}: clone probe failed ({failure}), the browser process "
                f"answers and the {starved}: starved, not frozen, not captured "
                f"({spent:.0f}s in)",
            )
            return
        record = await self._capture(instance_id, browser, steps)
        self.records[instance_id] = Incident(record, datetime.now(UTC), self._clock())
        debug_logger.log_error(
            _COMPONENT,
            "probe",
            FrozenCloneError(
                f"{instance_id}: clone probe failed ({failure}); {record}",
                f"probe {failure}",
            ),
        )

    def _goes_on(self, instance_id: str, incident: Incident, failure: str) -> None:
        """T-11617: count one more failed round of an open incident and, from the
        :data:`ROUNDS_BEFORE_REPEAT`-th, say so, then every :data:`REPEAT_SECONDS`."""
        incident.rounds += 1
        now = self._clock()
        if incident.rounds < ROUNDS_BEFORE_REPEAT:
            return
        if incident.said is not None and now - incident.said < REPEAT_SECONDS:
            return
        incident.said = now
        since = incident.since.astimezone().strftime("%H:%M:%S")  # the log's own zone
        debug_logger.log_error(
            _COMPONENT,
            "probe",
            FrozenCloneError(
                f"{instance_id} still unresponsive since {since} "
                f"({incident.minutes(now)}, {incident.rounds} rounds); "
                f"last probe failed ({failure})",
                "still-unresponsive",
            ),
        )

    async def _capture(
        self, instance_id: str, browser: Browser, steps: freeze_capture.TimedSteps
    ) -> str:
        """The three facts, never raising. The two ``Browser.getVersion`` checks run
        together so a frozen browser costs :data:`ANSWER_SECONDS`, not twice that."""
        url = getattr(browser, "websocket_url", None)
        fresh, own = await asyncio.gather(
            freeze_capture.fresh_answer(lambda: self._fresh_check(url), ANSWER_SECONDS),
            freeze_capture.fresh_answer(
                lambda: self._own_check(browser), ANSWER_SECONDS
            ),
        )
        facts = {
            "fresh_cdp(Browser.getVersion)": fresh,
            "own_cdp(Browser.getVersion)": own,
            "listener": self._fact(listener_state, browser),
            "paused_fetch": self._fact(
                lambda _: str(self._paused(instance_id)), browser
            ),
        }
        shown = " ".join(f"{name}: {value}" for name, value in facts.items())
        return f"{shown}; steps({freeze_capture.describe_steps(steps)})"

    @staticmethod
    def _fact(read: Callable[[Browser], str], browser: Browser) -> str:
        try:
            return read(browser)
        except Exception as error:  # noqa: BLE001  PERMANENT(T-1665: a fact that cannot be read is named, never an error in the loop)
            return f"unavailable ({type(error).__name__})"
