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
the live instances that are not persistent, and each is probed with THE one probe
(``BrowserHealth._failure_of``, injected). On the FIRST miss of an incident, one ERROR
line records, counts and states only:

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
(``answers probes again`` at INFO). No URL, title or cookie is ever read.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping

from nodriver import Browser
from nodriver.core.connection import Connection

from stealth_chrome_devtools_mcp.embedded import freeze_capture
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.dynamic_hook_system import dynamic_hook_system

_COMPONENT = "clone_probe"

#: How long each fresh-connection and own-connection ``Browser.getVersion`` gets.
ANSWER_SECONDS = freeze_capture.FRESH_ANSWER_TIMEOUT_SECONDS


class FrozenCloneError(Exception):
    """A disposable clone that missed its probe. Only ever handed to
    ``debug_logger.log_error`` (never raised), so the ERROR line and its Sentry
    event carry the record in the message."""


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
    socket = getattr(browser, "socket", None)
    code = getattr(socket, "close_code", None)
    wire = "none" if socket is None else "closed" if code is not None else "open"
    tabs = list(getattr(browser, "tabs", None) or [])
    running = sum(
        1
        for tab in tabs
        if _task_state(getattr(tab, "_listener_task", None)) == "running"
    )
    state = _task_state(getattr(browser, "_listener_task", None))
    return f"browser={state} socket={wire} tabs={running}/{len(tabs)}"


class CloneProbe:
    """Probes disposable clones and records the first failure of each incident."""

    def __init__(
        self,
        get_instance: Callable[
            [str], Awaitable[Mapping[str, Browser] | None]
        ],  # only "browser" is read
        failure_of: Callable[[Browser, list[str]], Awaitable[str | None]],
        fresh_check: Callable[[str | None], Awaitable[None]],
        own_check: Callable[[Browser], Awaitable[None]],
        *,
        paused: Callable[[str], int] = dynamic_hook_system.paused_in_flight,
    ) -> None:
        self._get_instance = get_instance
        self._failure_of = failure_of
        self._fresh_check = fresh_check
        self._own_check = own_check
        self._paused = paused
        #: instance id -> the record taken at its current incident's first miss.
        self.records: dict[str, str] = {}

    async def check(self, instance_ids: list[str]) -> None:
        """One round over *instance_ids* (the live, non-persistent instances); the
        ones that are no longer among them are forgotten. One clone's probe raising
        costs that clone its round and nobody else theirs."""
        for gone in set(self.records) - set(instance_ids):
            del self.records[gone]
        for instance_id in instance_ids:
            try:
                await self._check(instance_id)
            except Exception as error:  # noqa: BLE001  PERMANENT(T-1665: evidence is best effort; a clone's probe may not end the round nor skip the others)
                debug_logger.log_warning(
                    _COMPONENT, "check", f"{instance_id}: {type(error).__name__}"
                )

    async def _check(self, instance_id: str) -> None:
        data = await self._get_instance(instance_id)
        if data is None:
            return
        browser = data["browser"]
        steps = freeze_capture.TimedSteps()
        failure = await self._failure_of(browser, steps)
        steps.finish()
        if failure is None:
            if self.records.pop(instance_id, None) is not None:
                debug_logger.log_info(
                    _COMPONENT, "probe", f"{instance_id} answers probes again"
                )
            return
        if instance_id in self.records:
            return  # the incident is already on record
        if await self._get_instance(instance_id) is not data:
            return  # closed while we probed: no verdict on a browser that is gone
        record = await self._capture(instance_id, browser, steps)
        self.records[instance_id] = record
        debug_logger.log_error(
            _COMPONENT,
            "probe",
            FrozenCloneError(
                f"{instance_id}: clone probe failed ({failure}); {record}"
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
