"""THE one home for "is a long-lived named-session browser still answering, and if
not, bring it back" (F-963).

**The incident.** ``default`` and ``fleet`` froze together at ~26.5 h: no tab answered
``Runtime.evaluate`` and ``Browser.close`` replied ok while the process stayed up. The
mechanism is not identified: this module is a self-heal plus forensics.

**The probe does not go through nodriver.** A fresh ``websockets`` client opens a
background ``about:blank`` target, evaluates ``1+1`` in it and closes it — the exact
request that hung. This backend's own connection could let a wedged listener HERE fake
or hide a freeze; ``Browser.getVersion`` over it runs only after a passed direct probe,
and a failure there is its own warning and relaunches nothing.

**Scope:** ``browser_pid_registry.on_persistent_profile`` is probed and healed;
**T-1665:** a clone is probed too, by :mod:`clone_probe`, capture only.

**The heal** holds the F-961 per-directory gate throughout (a concurrent
``spawn_browser(session=…)`` waits and is handed the relaunched browser), closes
through the EXISTING close path (injected: ``close_instance``, which owns exit-wait,
kill escalation and the ``default`` seed refresh) and relaunches the same directory
under the SAME instance id (``BrowserManager.spawn_browser(instance_id=…)``), at most
once per :data:`HEAL_COOLDOWN_SECONDS`. A failed relaunch leaves the session closed,
which ``spawn_browser(session=…)`` relaunches from (F-961). **T-4686:** a browser the
close path left running past :data:`EXIT_WAIT_SECONDS` is ended by :mod:`forced_end`
if its identity is verified, else the heal is refused.

**F-964: a browser that EXITED is relaunched too, for the two shared sessions only.**
``BrowserManager.take_exited`` records each browser that died on its own (every
``close_instance`` pops its entry first, so a deliberate close is told apart), and the
loop relaunches the ones whose directory is ``profile_seed.DEFAULT_SESSION``'s or
``fleet_session.FLEET_SESSION``'s through the SAME tail the heal uses
(:meth:`BrowserHealth._launch_again`: spawn under the same id, re-arm, probe). Any
other named session and every clone is left alone. Not relaunched: a directory a live
browser already holds, a second exit inside :data:`HEAL_COOLDOWN_SECONDS` (a crash
loop is an ERROR), and anything while the backend is stopping.

**F-1007: a starved browser is not a frozen one.** A timeout is not counted when the
browser still answers a fresh ``Browser.getVersion`` AND an IDLE canary run DURING the
probe was starved (``scheduling_lag.ExcuseBudget`` bounds it); it still starts F-993.

**F-993: the frozen state is captured BEFORE the heal can kill it.** The first failed
probe of an incident starts :class:`freeze_capture.Incidents`' capture as a TASK (CPU
delta, process facts, a fresh ``Browser.getVersion``, on Windows a bounded minidump).
The first-failure WARNING says ``evidence@probe1(capturing)``, the record is logged when
it finishes, and the heal's ERROR line carries it (waited for at most
:data:`freeze_capture.HEAL_WAITS_FOR_CAPTURE_SECONDS`). It never raises into the loop;
a pid is opened only once its create time matches the registry. Counts and shapes only.
"""

import asyncio
import contextlib
import itertools
import json
import re
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import datetime, timezone

import nodriver as uc
import psutil
import websockets.asyncio.client
from nodriver import Browser, Tab
from websockets.asyncio.client import ClientConnection

from stealth_chrome_devtools_mcp.embedded import (
    browser_cmdline,
    browser_pid_registry,
    chrome_log,
    clone_probe,
    directory_gate,
    forced_end,
    freeze_capture,
    process_exit,
    scheduling_lag,
    shared_session,
    spawn_exhaustion,
    tab_binding,
)
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.models import BrowserInstance, BrowserOptions
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup

#: Seconds between two rounds of probes. A module constant on purpose: an unknown
#: ``STEALTH_MCP_*`` key crashes ``get_settings()``, and the house rule is
#: universal defaults over knobs.
PROBE_INTERVAL_SECONDS = 120

#: The whole probe, connect to close. Generous for a loaded machine, short enough
#: that a frozen browser is named within one interval.
PROBE_TIMEOUT_SECONDS = 15

#: Consecutive failed probes before the heal. One failure is a WARNING: a browser
#: that was busy for a moment must not be killed for it.
FAILURES_BEFORE_HEAL = 2

#: The minimum gap between two heals of one instance.
HEAL_COOLDOWN_SECONDS = 900

#: How long the heal waits for the old Chrome to be gone after the close path
#: returns. ``close_instance`` offloads its kill, so it answers while the
#: process is still dying, and nodriver drops its process handle by then, so
#: ``process_exit.browser_is_alive`` cannot be the witness (measured on real
#: Chrome): the pid taken BEFORE the close is.
EXIT_WAIT_SECONDS = 15

#: How long the browser process gets to answer for a timeout to be put down to
#: starvation.
BROWSER_ANSWER_SECONDS = 5.0

#: The bound on the forensics a failure writes. They are best effort.
FORENSICS_TIMEOUT_SECONDS = 10

_OPEN_TIMEOUT_SECONDS = 5.0
_EXPECTED_SUM = 2  # what `1+1` evaluates to
_CLOSE_TARGET_TIMEOUT_SECONDS = 3.0
_COMPONENT = "browser_health"

#: A ``--type=`` value Chrome defines (renderer, gpu-process, crashpad-handler…).
#: Anything else is counted as "other" rather than echoed into a log line.
_PROCESS_TYPE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")

Probe = Callable[[str, list[str]], Awaitable[None]]
Closer = Callable[[str, str | None], Awaitable[object]]
Armer = Callable[[Tab, str, list[str] | None], Awaitable[object]]


class HungBrowserError(Exception):
    """A persistent-profile browser that stopped answering a probe. Only ever
    handed to ``debug_logger.log_error`` (never raised), so the ERROR line and
    its Sentry event carry the forensics in the message."""


class ProbeError(Exception):
    """The browser answered, and the answer says it is not healthy."""


# ---------------------------------------------------------------------------
# The direct probe: a fresh websocket to the browser, nothing of nodriver's.
# ---------------------------------------------------------------------------


class _Wire:
    """Request/reply over one browser-level CDP websocket."""

    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws
        self._ids = itertools.count(1)

    async def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
        session_id: str | None = None,
    ) -> dict[str, object]:
        message: dict[str, object] = {"id": next(self._ids), "method": method}
        if params:
            message["params"] = params
        if session_id:
            message["sessionId"] = session_id
        await self._ws.send(json.dumps(message))
        while True:
            reply = json.loads(await self._ws.recv())
            if reply.get("id") != message["id"]:
                continue  # an event, or the answer to something else
            if "error" in reply:
                raise ProbeError(f"{method} refused: {reply['error'].get('code')}")
            return reply.get("result") or {}


@contextlib.asynccontextmanager
async def _connected(websocket_url: str) -> AsyncIterator[_Wire]:
    """A fresh client connection, with ``RotationGuard.start``'s options."""
    ws = await websockets.asyncio.client.connect(
        websocket_url,
        max_size=None,
        ping_interval=None,
        open_timeout=_OPEN_TIMEOUT_SECONDS,
        proxy=None,  # a system proxy must never sit between us and loopback
    )
    try:
        yield _Wire(ws)
    finally:
        with contextlib.suppress(Exception):
            await ws.close()


def _evaluated_value(reply: dict[str, object]) -> object:
    """``result.value`` of a ``Runtime.evaluate`` reply, or ``None``."""
    result = reply.get("result")
    return result.get("value") if isinstance(result, dict) else None


async def probe_browser(websocket_url: str, steps: list[str]) -> None:
    """Open a background ``about:blank`` target, evaluate ``1+1`` in it, close it.

    Raises when any step fails. *steps* collects the name of each step as it
    starts, so a caller that times the whole thing out still knows where it was.
    """
    async with _connected(websocket_url) as wire:
        target_id: str | None = None
        try:
            steps.append("createTarget")
            created = await wire.call(
                "Target.createTarget", {"url": "about:blank", "background": True}
            )
            target_id = str(created["targetId"])
            steps.append("attachToTarget")
            attached = await wire.call(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True}
            )
            steps.append("evaluate")
            evaluated = await wire.call(
                "Runtime.evaluate",
                {"expression": "1+1", "returnByValue": True},
                str(attached["sessionId"]),
            )
            if _evaluated_value(evaluated) != _EXPECTED_SUM:
                raise ProbeError(
                    f"Runtime.evaluate(1+1) did not answer {_EXPECTED_SUM}"
                )
            steps.append("closeTarget")
        finally:
            if target_id is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        wire.call("Target.closeTarget", {"targetId": target_id}),
                        _CLOSE_TARGET_TIMEOUT_SECONDS,
                    )


async def fresh_browser_version(websocket_url: str | None) -> None:
    """``Browser.getVersion`` over a NEW connection (F-993): does the browser process
    still answer anyone, whatever its renderers do."""
    if not websocket_url:
        raise ConnectionError("no endpoint")
    async with _connected(websocket_url) as wire:
        await wire.call("Browser.getVersion")


async def own_connection_answers(browser: Browser) -> None:
    """``Browser.getVersion`` over THIS backend's connection (the ``Browser``, 0.50)."""
    await browser.send(uc.cdp.browser.get_version())


# ---------------------------------------------------------------------------
# Forensics: counts and shapes, never a URL, a title or a cookie.
# ---------------------------------------------------------------------------


async def fetch_target_infos(websocket_url: str) -> list[dict[str, object]]:
    """``Target.getTargets`` over a fresh connection."""
    async with _connected(websocket_url) as wire:
        infos = (await wire.call("Target.getTargets")).get("targetInfos")
        if not isinstance(infos, list):
            return []
        return [
            {str(k): v for k, v in info.items()}
            for info in infos
            if isinstance(info, dict)
        ]


def describe_targets(infos: list[dict[str, object]]) -> str:
    """``page=N other=M`` — the counts only."""
    pages = sum(1 for info in infos if info.get("type") == "page")
    return f"page={pages} other={len(infos) - pages}"


def process_tree(pid: int | None) -> str:
    """The browser's process tree by ``--type`` and its total RSS, or why not."""
    if not isinstance(pid, int):
        return "unavailable (no pid)"
    try:
        root = psutil.Process(pid)
        members = [root, *root.children(recursive=True)]
    except (psutil.Error, OSError):
        return "unavailable (browser pid gone)"
    kinds: Counter[str] = Counter()
    rss = 0
    for proc in members:
        try:
            rss += proc.memory_info().rss
            declared = browser_cmdline.flag_value(
                browser_cmdline.arguments(proc.pid), browser_cmdline.TYPE_FLAG
            )
        except (psutil.Error, OSError):
            continue
        if declared is None:
            kinds["browser"] += 1
        else:
            kinds[declared if _PROCESS_TYPE.fullmatch(declared) else "other"] += 1
    shape = " ".join(f"{kind}={count}" for kind, count in sorted(kinds.items()))
    return f"{shape} rss={rss / 2**30:.2f}GB"


def machine_facts() -> str:
    """Machine-wide Chromium process count and free physical memory."""
    free = psutil.virtual_memory().available
    return (
        f"chromium_processes={spawn_exhaustion.live_chromium_processes()} "
        f"free_ram={free / 2**30:.2f}GB"
    )


async def gather_forensics(
    *,
    uptime_seconds: float,
    websocket_url: str | None,
    pid: int | None,
    fetch_targets: Callable[
        [str], Awaitable[list[dict[str, object]]]
    ] = fetch_target_infos,
    browser_alive: bool = True,
) -> str:
    """One line of counts and shapes about a browser that stopped answering, or
    (``browser_alive=False``, F-964) one that is gone: no targets, no tree."""

    async def targets() -> str:
        if not websocket_url:
            return "unavailable (no endpoint)"
        return describe_targets(await fetch_targets(websocket_url))

    machine = asyncio.to_thread(machine_facts)
    named = ("targets", "tree", "machine") if browser_alive else ("machine",)
    parts = await asyncio.gather(
        *(
            (targets(), asyncio.to_thread(process_tree, pid), machine)
            if browser_alive
            else (machine,)
        ),
        return_exceptions=True,
    )
    shown = [
        f"{name}=unavailable ({type(part).__name__})"
        if isinstance(part, BaseException)
        else f"{name}({part})"
        for name, part in zip(named, parts, strict=True)
    ]
    return f"uptime={uptime_seconds:.0f}s " + " ".join(shown)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def _is_persistent(instance_id: str) -> bool:
    """A named session or the shared ``default`` one: ``on_persistent_profile``,
    read off the same record the reaper reads. No record, no verdict, so skipped."""
    entry = process_cleanup.browser_processes.get(instance_id)
    return bool(entry) and browser_pid_registry.on_persistent_profile(entry)


def _log_note(diagnostics: dict[str, object] | None) -> str:
    """``; chrome_log=<path>`` when the browser's launch asked Chrome for a log
    (F-964): the PATH only, never a line of it."""
    path = (diagnostics or {}).get("chrome_log")
    return f"; chrome_log={path}" if path else ""


def _describe(error: Exception, steps: list[str]) -> str:
    """``TimeoutError at evaluate``: the error's type and the step it stopped at."""
    return f"{type(error).__name__} at {steps[-1] if steps else 'connect'}"


class BrowserHealth:
    """Probes every persistent-profile browser and heals a hung one; relaunches
    a shared-session browser that exited."""

    def __init__(  # noqa: PLR0913  PERMANENT(F-963: one keyword seam per external effect, so the loop runs hermetically)
        self,
        manager: BrowserManager,
        *,
        close: Closer,
        arm_interception: Armer,
        probe: Probe = probe_browser,
        own_check: Callable[[Browser], Awaitable[None]] = own_connection_answers,
        browser_answers: Callable[
            [str | None], Awaitable[None]
        ] = fresh_browser_version,
        canary: Callable[
            [], contextlib.AbstractAsyncContextManager[scheduling_lag.IdleCanary]
        ] = scheduling_lag.idle_canary,
        persistent: Callable[[str], bool] = _is_persistent,
        forensics: Callable[..., Awaitable[str]] = gather_forensics,
        heal: Callable[[str], Awaitable[bool]] | None = None,
        browser_pid: Callable[
            [object, int | None], int | None
        ] = process_exit.browser_pid,
        wait_exit: Callable[..., Awaitable[process_exit.Exited]] = (
            process_exit.wait_for_exit_async
        ),
        clock: Callable[[], float] = time.monotonic,
        shared: Callable[[BrowserOptions], bool] = shared_session.is_shared_session,
        holder: Callable[[str], str | None] = shared_session.holder_of,
        capture: Callable[..., Awaitable[str]] = freeze_capture.capture,
        dump_now: Callable[..., Awaitable[str]] = freeze_capture.dump_now,
        mark_healed: Callable[[str], str | None] = freeze_capture.mark_healed,
        hold_exit: Callable[[int], int | None] = process_exit.hold_exit_handle,
        release_exit: Callable[[int], None] = process_exit.release_exit_handle,
        exit_status: Callable[
            [object | None, int | None], str
        ] = process_exit.exit_status,
    ) -> None:
        self._manager = manager
        self._close = close
        self._arm_interception = arm_interception
        self._probe = probe
        self._own_check = own_check
        self._browser_answers = browser_answers
        self._canary = canary
        self._persistent = persistent
        self._forensics = forensics
        self._heal = heal or self.heal
        self._browser_pid = browser_pid
        self._wait_exit = wait_exit
        self._clock = clock
        self._shared = shared
        self._holder = holder
        self._clones = clone_probe.CloneProbe(  # T-1665
            manager.get_instance, self._judged, browser_answers, own_check, clock=clock
        )
        self._incidents = freeze_capture.Incidents(
            capture=capture,
            dump_now=dump_now,
            mark_healed=mark_healed,
            clock=clock,
            log=lambda line: debug_logger.log_warning(_COMPONENT, "capture", line),
        )
        self._hold_exit = hold_exit
        self._release_exit_handle = release_exit
        self._exit_status = exit_status
        self.stopping = False
        self._failures: dict[str, int] = {}
        self._last_heal: dict[str, float] = {}
        self._excuses = scheduling_lag.ExcuseBudget(clock)  # F-1007
        # F-993: per adopted shared-session browser, the (pid, handle) that keeps
        # its exit code readable.
        self._exit_handles: dict[str, tuple[int, int]] = {}

    async def run(self) -> None:
        """Probe every :data:`PROBE_INTERVAL_SECONDS` until cancelled."""
        while True:
            await asyncio.sleep(PROBE_INTERVAL_SECONDS)
            try:
                await self.check_once()
            except Exception as error:  # noqa: BLE001  PERMANENT(F-963: the loop outlives any one round)
                debug_logger.log_error(_COMPONENT, "run", error)

    async def check_once(self) -> None:
        """One round. An exception in one instance's probe costs that instance
        its round and nobody else theirs."""
        if self.stopping:
            return
        # The listing is what discovers a browser that died (it discards it), so
        # the exits are taken after it and the live set is listed again after they
        # are handled: a relaunched browser is probed, and keeps its cooldown.
        await self._manager.list_instances()
        for instance_id, data in self._manager.take_exited():
            try:
                if self._shared(data["options"]):
                    await self.relaunch_exited(
                        instance_id,
                        data["options"],
                        data["instance"],
                        dict(data.get("spawn_diagnostics") or {}),
                        data.get("browser"),
                    )
            except Exception as error:  # noqa: BLE001  PERMANENT(F-964: one instance's relaunch must not end the round, nor skip the probes)
                debug_logger.log_error(_COMPONENT, "relaunch", error)
        listed = [i.instance_id for i in await self._manager.list_instances()]
        scoped = [iid for iid in listed if self._persistent(iid)]
        tracked = (
            set(self._failures)
            | set(self._last_heal)
            | self._excuses.tracked()
            | self._incidents.tracked()
            | set(self._exit_handles)
        )
        for gone in tracked:
            if gone not in scoped:
                self._failures.pop(gone, None)
                self._last_heal.pop(gone, None)
                self._excuses.reset(gone)
                self._incidents.forget(gone)
                self._release_exit(gone)
        for instance_id in scoped:
            try:
                await self._check(instance_id)
            except Exception as error:  # noqa: BLE001  PERMANENT(F-963: a probe that raises must not end the loop, nor skip the other instances)
                debug_logger.log_error(_COMPONENT, "check", error)
        await self._clones.check([i for i in listed if i not in scoped], self._canary)

    async def _probe_error(
        self, browser: Browser, steps: list[str]
    ) -> Exception | None:
        """The error the direct probe failed with, or ``None`` when it passed."""
        try:
            await asyncio.wait_for(
                self._probe(browser.websocket_url, steps), PROBE_TIMEOUT_SECONDS
            )
        except Exception as error:  # noqa: BLE001  PERMANENT(F-963: every way of not answering is a failed probe)
            return error
        return None

    async def _failure_of(self, browser: Browser) -> str | None:
        """Why the direct probe failed, or ``None`` when it passed."""
        steps: list[str] = []
        error = await self._probe_error(browser, steps)
        return None if error is None else _describe(error, steps)

    async def _starved(
        self, data: Mapping[str, object], factor: float | None
    ) -> str | None:
        """F-1007: a counts-only phrase when this failed probe is put down to a
        starved host (*factor*: ``_probed``'s, a TIMEOUT only), else ``None``."""
        if not scheduling_lag.is_starved(factor):
            return None
        url = getattr(data["browser"], "websocket_url", None)
        try:
            await asyncio.wait_for(self._browser_answers(url), BROWSER_ANSWER_SECONDS)
        except Exception as problem:  # noqa: BLE001  PERMANENT(F-1007: every way of not answering keeps the old verdict, the heal)
            debug_logger.log_debug(
                _COMPONENT, "probe", f"not starved: {type(problem).__name__}"
            )
            return None
        return f"idle canary ran {factor:.1f}x slow"

    async def _judged(
        self,
        data: Mapping[str, Browser],
        steps: freeze_capture.TimedSteps,
        canary: scheduling_lag.IdleCanary | None = None,
    ) -> tuple[str | None, str | None]:
        """``(failure, starved)`` of one probe: THE judgement of ``_check`` and, with
        its round's one *canary*, of ``clone_probe`` (T-1665)."""
        error, factor = await self._probed(data["browser"], steps, canary)
        steps.finish()
        if error is None:
            return None, None
        return _describe(error, steps), await self._starved(data, factor)

    async def _probed(
        self,
        browser: Browser,
        steps: list[str],
        shared: scheduling_lag.IdleCanary | None = None,
    ) -> tuple[Exception | None, float | None]:
        """The probe's error (or ``None``) and, for a TIMEOUT only, the IDLE canary's
        factor (a *shared* canary's: over its whole round). Never raises."""
        async with (
            contextlib.nullcontext(shared) if shared else self._canary() as canary
        ):
            error = await self._probe_error(browser, steps)
            if not isinstance(error, TimeoutError):
                return error, None
            try:
                return error, await canary.factor()
            except Exception as problem:  # noqa: BLE001  PERMANENT(F-1007: a canary that cannot be read is no evidence, never an error in the loop)
                debug_logger.log_debug(
                    _COMPONENT, "probe", f"canary unreadable: {type(problem).__name__}"
                )
                return error, None

    def _excused(self, instance_id: str, failure: str, starved: str) -> bool:
        """True while within the excuse ceiling; past it an ERROR and False."""
        spent = self._excuses.grant(instance_id)
        if spent is None:
            debug_logger.log_error(
                _COMPONENT,
                "probe",
                HungBrowserError(
                    f"{instance_id}: probe failed ({failure}) past the starvation "
                    f"ceiling ({starved}); counting it (F-1007)"
                ),
            )
            return False
        debug_logger.log_warning(
            _COMPONENT,
            "probe",
            f"{instance_id}: probe failed ({failure}), the browser process answers "
            f"and the {starved}: starved, not frozen, not counted (F-1007); "
            f"{self._failures.get(instance_id, 0)} failure(s) kept, {spent:.0f}s in",
        )
        return True

    async def _check(self, instance_id: str) -> None:
        data = await self._manager.get_instance(instance_id)
        if data is None:
            return
        self._watch_exit(instance_id, data)
        steps = freeze_capture.TimedSteps()
        failure, starved = await self._judged(data, steps)
        if failure is None:
            self._incidents.end(instance_id)  # the incident, if any, is over
            self._excuses.reset(instance_id)
            if self._failures.pop(instance_id, 0):
                debug_logger.log_info(
                    _COMPONENT, "probe", f"{instance_id} answers probes again"
                )
            await self._check_own_connection(instance_id, data["browser"])
            return
        if await self._manager.get_instance(instance_id) is not data:
            return  # closed while we probed: no verdict on a browser that is gone
        if starved and self._excused(instance_id, failure, starved):
            self._begin_incident(instance_id, data)  # F-993: evidence, never a count
            return
        count = self._failures[instance_id] = self._failures.get(instance_id, 0) + 1
        facts = await self._facts(
            instance_id, data["browser"], data["instance"].created_at
        )
        label = (
            f"{instance_id}: probe {count} failed ({failure}); {facts}"
            f"{_log_note(data.get('spawn_diagnostics'))}"
            f"; steps({freeze_capture.describe_steps(steps)})"
        )
        if count < FAILURES_BEFORE_HEAL:
            debug_logger.log_warning(
                _COMPONENT, "probe", f"{label}; evidence@probe1(capturing)"
            )
            self._begin_incident(instance_id, data)
            return
        self._begin_incident(instance_id, data)  # a count that outlived its incident
        since = self._clock() - self._last_heal.get(instance_id, -HEAL_COOLDOWN_SECONDS)
        if since < HEAL_COOLDOWN_SECONDS:
            debug_logger.log_warning(
                _COMPONENT,
                "probe",
                f"{label}; healed {since:.0f}s ago, not again within "
                f"{HEAL_COOLDOWN_SECONDS}s",
            )
            return
        pid, dump_label, problem = self._incident_target(instance_id, data)
        evidence = await self._incidents.before_heal(
            instance_id, pid=pid, label=dump_label, problem=problem
        )
        debug_logger.log_error(
            _COMPONENT, "probe", HungBrowserError(f"{label}; {evidence}; healing it")
        )
        self._last_heal[instance_id] = self._clock()
        self._failures[instance_id] = 0  # what comes next is a new incident
        self._excuses.reset(instance_id)
        await self._heal(instance_id)

    def _incident_target(
        self, instance_id: str, data: dict[str, object]
    ) -> tuple[int | None, str, str | None]:
        """(pid, dump label, why that pid may not be opened) for *instance_id*.
        Never raises: it stands in front of the heal as well as the capture."""
        key = instance_id[:8]
        try:
            browser = data["browser"]
            entry = process_cleanup.browser_processes.get(instance_id) or {}
            pid = self._browser_pid(
                getattr(browser, "_process", None),
                entry.get("pid") or getattr(browser, "_process_pid", None),
            )
            directory = getattr(data.get("options"), "user_data_dir", None)
            session = chrome_log.session_key(directory) if directory else "browser"
            problem = freeze_capture.identity_problem(entry, pid)
        except Exception as error:  # noqa: BLE001  PERMANENT(F-993: evidence is best effort and must not stand between a hung browser and its heal)
            return None, f"freeze-browser-{key}", f"{type(error).__name__} finding it"
        return pid, f"freeze-{session}-{key}", problem

    def _begin_incident(self, instance_id: str, data: dict[str, object]) -> None:
        """Start the F-993 capture of a browser that just failed its first probe, once
        per incident. Returns at once, so nothing here can delay a heal. Called straight
        AFTER the first-failure line is logged, with no await between, so the record
        (its own line when the task ends) never precedes it, on any interpreter."""
        if self._incidents.has(instance_id):
            return
        try:
            pid, label, problem = self._incident_target(instance_id, data)
            browser = data["browser"]
            self._incidents.begin(
                instance_id,
                pid=pid,
                label=label,
                answers=lambda: fresh_browser_version(
                    getattr(browser, "websocket_url", None)
                ),
                problem=problem,
            )
        except Exception as error:  # noqa: BLE001  PERMANENT(F-993: evidence is best effort and must not stand between a hung browser and its heal)
            debug_logger.log_warning(
                _COMPONENT,
                "capture",
                f"{instance_id}: not started ({type(error).__name__})",
            )

    def _watch_exit(self, instance_id: str, data: dict[str, object]) -> None:
        """Hold a process handle on a shared-session browser this backend did NOT
        launch (a re-attach, F-888), so its exit code survives its exit; replaced
        when the pid under the id changes. A launched browser needs none: nodriver's
        process object keeps the code. Never raises."""
        try:
            browser = data["browser"]
            options = data["options"]
            watched = (
                getattr(browser, "_process", None) is None
                and isinstance(options, BrowserOptions)
                and self._shared(options)
            )
            pid = (
                self._browser_pid(None, getattr(browser, "_process_pid", None))
                if watched
                else None
            )
            held = self._exit_handles.get(instance_id)
            if held is not None and held[0] == pid:
                return
            self._release_exit(instance_id)  # a stale handle goes whatever comes next
            if pid is None:
                return
            entry = process_cleanup.browser_processes.get(instance_id) or {}
            problem = freeze_capture.identity_problem(entry, pid)
            if problem is not None:
                debug_logger.log_warning(
                    _COMPONENT, "exit_handle", f"{instance_id}: not held: {problem}"
                )
                return
            handle = self._hold_exit(pid)
            if handle is not None:
                self._exit_handles[instance_id] = (pid, handle)
        except Exception as error:  # noqa: BLE001  PERMANENT(F-993: a diagnostic handle may not fail a probe round)
            debug_logger.log_warning(
                _COMPONENT, "exit_handle", f"{instance_id}: {type(error).__name__}"
            )

    def _release_exit(self, instance_id: str) -> None:
        held = self._exit_handles.pop(instance_id, None)
        if held is not None:
            with contextlib.suppress(Exception):  # PERMANENT(F-993: best-effort)
                self._release_exit_handle(held[1])

    def release_exit_handles(self) -> None:
        """Give every held process handle back (shutdown). Idempotent."""
        for instance_id in list(self._exit_handles):
            self._release_exit(instance_id)

    def cancel_captures(self) -> None:
        """Stop every capture still running (shutdown)."""
        self._incidents.cancel_all()

    def _read_exit_status(self, instance_id: str, browser: object | None) -> str:
        """``exit_code=...`` for a browser that exited, taking and releasing the
        handle held for it. Never raises."""
        held = self._exit_handles.get(instance_id)
        try:
            return self._exit_status(browser, held[1] if held else None)
        except Exception as error:  # noqa: BLE001  PERMANENT(F-993: a log field may not fail a relaunch)
            return f"exit_code=unavailable ({type(error).__name__})"
        finally:
            self._release_exit(instance_id)

    async def _check_own_connection(self, instance_id: str, browser: Browser) -> None:
        """The direct probe passed: is THIS backend's own connection alive too?"""
        try:
            await asyncio.wait_for(self._own_check(browser), PROBE_TIMEOUT_SECONDS)
        except Exception as error:  # noqa: BLE001  PERMANENT(F-963: the check is a diagnosis; any failure is the finding)
            debug_logger.log_warning(
                _COMPONENT,
                "own_connection",
                f"{instance_id}: backend connection wedged — the browser answers "
                f"a fresh connection but this backend's own did not answer "
                f"Browser.getVersion ({type(error).__name__}); not relaunching",
            )

    async def _facts(
        self, instance_id: str, browser: Browser | None, created: datetime
    ) -> str:
        """The forensics line; bounded, and never raises. *browser* is ``None``
        for one that exited: its tree and targets are gone, and only the rest is
        asked."""
        entry = process_cleanup.browser_processes.get(instance_id) or {}
        try:
            return await asyncio.wait_for(
                self._forensics(
                    uptime_seconds=(
                        datetime.now(tz=timezone.utc) - created
                    ).total_seconds(),
                    websocket_url=getattr(browser, "websocket_url", None),
                    pid=entry.get("pid") or getattr(browser, "_process_pid", None),
                    browser_alive=browser is not None,
                ),
                FORENSICS_TIMEOUT_SECONDS,
            )
        except Exception as error:  # noqa: BLE001  PERMANENT(F-963: forensics are best effort and must not stand between a hung browser and its heal)
            return f"forensics unavailable ({type(error).__name__})"

    async def heal(self, instance_id: str) -> bool:
        """Close the browser and relaunch it under the same id; True when the
        relaunched browser answers a probe."""
        data = await self._manager.get_instance(instance_id)
        if data is None:
            return False
        old_browser, options = data["browser"], data["options"]
        # What close_instance reads later (the `default` seed refresh) lives in
        # the spawn diagnostics; the relaunch writes new ones.
        previous = dict(await self._manager.get_spawn_diagnostics(instance_id) or {})
        relaunch = shared_session.relaunch_options(options, data["instance"], previous)
        old_pid = self._browser_pid(
            getattr(old_browser, "_process", None),
            getattr(old_browser, "_process_pid", None),
        )
        entry = dict(process_cleanup.browser_processes.get(instance_id) or {})
        gate = await directory_gate.hold(options.user_data_dir)
        try:
            # The close path refuses `default` and `fleet` unless told which one
            # (F-966); a heal knows, from the options it is about to relaunch.
            shared = shared_session.shared_session_name(
                options.user_data_dir, auto_clone=options.auto_clone
            )
            await self._close(instance_id, shared)
            if old_pid is not None and (
                left := await forced_end.run(
                    instance_id,
                    old_pid,
                    entry,
                    user_data_dir=options.user_data_dir,
                    wait_exit=self._wait_exit,
                    timeout=EXIT_WAIT_SECONDS,
                    holder=self._holder,
                )
            ):
                debug_logger.log_error(
                    _COMPONENT,
                    "heal",
                    HungBrowserError(
                        f"{instance_id}: the close path left the browser running "
                        f"and it {left}; not relaunching onto a profile it still holds"
                    ),
                )
                return False
            try:
                return await self._launch_again(
                    instance_id,
                    relaunch,
                    previous,
                    "healed (relaunched under the same id)",
                )
            except Exception as error:  # noqa: BLE001  PERMANENT(F-963: a failed relaunch leaves the session closed, which spawn_browser(session=...) relaunches from)
                debug_logger.log_error(_COMPONENT, "heal", error)
                return False
        finally:
            gate.release()

    async def relaunch_exited(
        self,
        instance_id: str,
        options: BrowserOptions,
        instance: BrowserInstance,
        previous: dict[str, object],
        browser: object | None = None,
    ) -> bool:
        """Relaunch a shared-session browser that exited on its own, under the
        same id (F-964); True when the relaunched browser answers a probe. Every
        refusal is logged with its reason. *previous* is its spawn diagnostics."""
        directory = options.user_data_dir
        if not directory:
            return False  # `is_shared_session` never admits one; a seam might
        status = self._read_exit_status(instance_id, browser)
        facts = await self._facts(instance_id, None, instance.created_at)
        label = (
            f"{instance_id}: exited unexpectedly ({facts}; {status})"
            f"{_log_note(previous)}"
        )
        since = self._clock() - self._last_heal.get(instance_id, -HEAL_COOLDOWN_SECONDS)
        if since < HEAL_COOLDOWN_SECONDS:
            debug_logger.log_error(
                _COMPONENT,
                "relaunch",
                HungBrowserError(
                    f"{label}; it was healed or relaunched {since:.0f}s ago, not "
                    f"relaunching again within {HEAL_COOLDOWN_SECONDS}s"
                ),
            )
            return False
        self._last_heal[instance_id] = self._clock()
        self._excuses.reset(instance_id)  # a new browser, a new run
        gate = await directory_gate.hold(directory)
        try:
            holder = await asyncio.to_thread(self._holder, directory)
            if self.stopping or holder is not None:
                debug_logger.log_warning(
                    _COMPONENT,
                    "relaunch",
                    f"{label}; not relaunching: "
                    + (
                        "the backend is stopping"
                        if self.stopping
                        else f"a live browser ({holder}) holds its directory"
                    ),
                )
                return False
            debug_logger.log_warning(_COMPONENT, "relaunch", f"{label}; relaunching")
            try:
                return await self._launch_again(
                    instance_id,
                    shared_session.relaunch_options(options, instance, previous),
                    previous,
                    "relaunched after an unexpected exit (under the same id)",
                )
            except Exception as error:  # noqa: BLE001  PERMANENT(F-964: a failed relaunch is the end of it; spawn_browser(session=...) starts the session from the closed state it leaves)
                debug_logger.log_error(
                    _COMPONENT,
                    "relaunch",
                    HungBrowserError(
                        f"{label}; relaunch failed ({type(error).__name__}: "
                        f"{error}); not trying again"
                    ),
                )
                return False
        finally:
            gate.release()

    async def _launch_again(
        self,
        instance_id: str,
        relaunch: BrowserOptions,
        previous: dict[str, object],
        verb: str,
    ) -> bool:
        """THE tail of a heal and of an exit relaunch, run with the directory gate
        held: spawn under the same id, re-arm what the spawn tool arms, and probe
        the result. Raises the spawn's own error; *verb* words the success line."""
        await self._manager.spawn_browser(relaunch, instance_id=instance_id)
        await self._rearm(instance_id, relaunch, previous)
        return await self._report_healed(instance_id, verb)

    async def _rearm(
        self, instance_id: str, options: BrowserOptions, previous: dict[str, object]
    ) -> None:
        """What the spawn tool does after ``spawn_browser`` returns."""
        diagnostics = await self._manager.get_spawn_diagnostics(instance_id)
        if isinstance(diagnostics, dict) and "profile_selection" in previous:
            diagnostics["profile_selection"] = previous["profile_selection"]
        tab_binding.forget_instance(instance_id)  # every bound target id is gone
        tab = await self._manager.get_tab(instance_id)
        if tab:
            await self._arm_interception(tab, instance_id, options.block_resources)

    async def _report_healed(self, instance_id: str, verb: str) -> bool:
        data = await self._manager.get_instance(instance_id)
        failure = await self._failure_of(data["browser"]) if data else "no instance"
        if failure is not None:
            debug_logger.log_error(
                _COMPONENT,
                "heal",
                HungBrowserError(
                    f"{instance_id}: relaunched but the probe still fails ({failure})"
                ),
            )
            return False
        debug_logger.log_info(
            _COMPONENT,
            "heal",
            f"{instance_id}: {verb}",
        )
        return True


_task: asyncio.Task[None] | None = None
_health: BrowserHealth | None = None


def start(manager: BrowserManager, *, close: Closer, arm_interception: Armer) -> None:
    """Start the loop beside the idle reaper; a no-op while one is running."""
    global _task, _health  # noqa: PLW0603  PERMANENT(F-963: one loop per process, like the reaper's single task)
    if _task is not None and not _task.done():
        return
    health = _health = BrowserHealth(
        manager, close=close, arm_interception=arm_interception
    )
    _task = asyncio.create_task(health.run())
    debug_logger.log_info(
        _COMPONENT,
        "start",
        f"Browser health probe started (every {PROBE_INTERVAL_SECONDS}s, "
        f"heal after {FAILURES_BEFORE_HEAL} failures)",
    )


async def stop() -> None:
    """Stop the loop if it is running."""
    global _task
    task, _task = _task, None
    if _health is not None:
        _health.stopping = True  # F-964: a browser that dies in shutdown stays dead
        _health.release_exit_handles()
        _health.cancel_captures()
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
