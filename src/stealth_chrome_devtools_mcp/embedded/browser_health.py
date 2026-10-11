"""THE one home for "is a long-lived named-session browser still answering, and if
not, bring it back" (F-963).

**The incident.** Two named-session browsers, ``default`` (master) and ``fleet``,
froze at ~26.5 h of uptime, launched by one backend at one time. Chrome's HTTP
endpoints still answered (``/json``, ``/json/new``) but ``Runtime.evaluate``
never returned in ANY tab, a fresh ``about:blank`` one opened over a direct CDP
websocket included. ``Browser.close`` replied ok and the process stayed up. The
Chrome-side mechanism is not identified (a JS dialog, a busy page and the
renderer-process limit each hang only their own tab), so this module is a
self-heal plus forensics: the next freeze is both healed and explained.

**The probe does not go through nodriver.** A fresh ``websockets`` client opens a
background ``about:blank`` target, evaluates ``1+1`` in it and closes it — the
exact request that hung. Going through this backend's own nodriver connection
would let a wedged listener HERE fake a Chrome freeze, or hide one. The same
check over the backend's own connection (``Browser.getVersion``) runs only when
the direct probe passed, and a failure there is reported as its own warning —
"backend connection wedged" — and does not relaunch anything in v1.

**Scope: persistent profiles only** — ``browser_pid_registry.on_persistent_profile``,
the one predicate for "a login lives in this profile". A disposable clone is
skipped: it is cheap to respawn and nothing is lost with it.

**The heal** holds the F-961 per-directory gate for its whole length (a concurrent
``spawn_browser(session=…)`` waits and is then handed the relaunched browser),
closes through the EXISTING close path (injected: the ``close_instance`` tool,
which owns exit-wait, kill escalation and the ``default`` seed refresh), and
relaunches the same directory, headless and viewport under the SAME instance id
through ``BrowserManager.spawn_browser(instance_id=…)``. Callers and MASTER's
shared-browser.json keep that id. Rotation-guard arming is the spawn path's own
(``fleet_session.guards_rotation`` decides, ``spawn_browser`` arms). A heal is
attempted at most once per :data:`HEAL_COOLDOWN_SECONDS` per instance, so a
browser that cannot recover does not loop. A relaunch that fails leaves the
session closed, which is exactly the state ``spawn_browser(session=…)`` relaunches
from (F-961).

**Forensics are counts and shapes only** — never a URL, title or cookie. They are
written once on the first failed probe (WARNING) and again when the heal fires
(ERROR, which reaches Sentry through ``debug_logger``).

The manager, the close path and the interception arming arrive as arguments —
this module imports no ``server`` and no ``tool_sections``. Every external effect
is a keyword seam, so the unit lane runs the loop with no Chrome.
"""

import asyncio
import contextlib
import itertools
import json
import re
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone

import nodriver as uc
import psutil
import websockets.asyncio.client
from nodriver import Browser, Tab
from websockets.asyncio.client import ClientConnection

from stealth_chrome_devtools_mcp.embedded import (
    browser_cmdline,
    browser_pid_registry,
    directory_gate,
    fleet_session,
    process_exit,
    spawn_exhaustion,
    tab_binding,
)
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions
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
Closer = Callable[[str], Awaitable[object]]
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
        finally:
            if target_id is not None:
                steps.append("closeTarget")
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        wire.call("Target.closeTarget", {"targetId": target_id}),
                        _CLOSE_TARGET_TIMEOUT_SECONDS,
                    )


async def own_connection_answers(browser: Browser) -> None:
    """``Browser.getVersion`` over THIS backend's nodriver connection."""
    await browser.connection.send(uc.cdp.browser.get_version())


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
) -> str:
    """One line of counts and shapes about a browser that stopped answering."""

    async def targets() -> str:
        if not websocket_url:
            return "unavailable (no endpoint)"
        return describe_targets(await fetch_targets(websocket_url))

    parts = await asyncio.gather(
        targets(),
        asyncio.to_thread(process_tree, pid),
        asyncio.to_thread(machine_facts),
        return_exceptions=True,
    )
    named = ("targets", "tree", "machine")
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


class BrowserHealth:
    """Probes every persistent-profile browser and heals a hung one."""

    def __init__(  # noqa: PLR0913  PERMANENT(F-963: one keyword seam per external effect, so the loop runs hermetically)
        self,
        manager: BrowserManager,
        *,
        close: Closer,
        arm_interception: Armer,
        probe: Probe = probe_browser,
        own_check: Callable[[Browser], Awaitable[None]] = own_connection_answers,
        persistent: Callable[[str], bool] = _is_persistent,
        forensics: Callable[..., Awaitable[str]] = gather_forensics,
        heal: Callable[[str], Awaitable[bool]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._manager = manager
        self._close = close
        self._arm_interception = arm_interception
        self._probe = probe
        self._own_check = own_check
        self._persistent = persistent
        self._forensics = forensics
        self._heal = heal or self.heal
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._last_heal: dict[str, float] = {}

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
        listed = [i.instance_id for i in await self._manager.list_instances()]
        scoped = [iid for iid in listed if self._persistent(iid)]
        for gone in set(self._failures) | set(self._last_heal):
            if gone not in scoped:
                self._failures.pop(gone, None)
                self._last_heal.pop(gone, None)
        for instance_id in scoped:
            try:
                await self._check(instance_id)
            except Exception as error:  # noqa: BLE001  PERMANENT(F-963: a probe that raises must not end the loop, nor skip the other instances)
                debug_logger.log_error(_COMPONENT, "check", error)

    async def _failure_of(self, browser: Browser) -> str | None:
        """Why the direct probe failed, or ``None`` when it passed."""
        steps: list[str] = []
        try:
            await asyncio.wait_for(
                self._probe(browser.websocket_url, steps), PROBE_TIMEOUT_SECONDS
            )
        except Exception as error:  # noqa: BLE001  PERMANENT(F-963: every way of not answering is a failed probe)
            return f"{type(error).__name__} at {steps[-1] if steps else 'connect'}"
        return None

    async def _check(self, instance_id: str) -> None:
        data = await self._manager.get_instance(instance_id)
        if data is None:
            return
        failure = await self._failure_of(data["browser"])
        if failure is None:
            if self._failures.pop(instance_id, 0):
                debug_logger.log_info(
                    _COMPONENT, "probe", f"{instance_id} answers probes again"
                )
            await self._check_own_connection(instance_id, data["browser"])
            return
        if await self._manager.get_instance(instance_id) is not data:
            return  # closed while we probed: no verdict on a browser that is gone
        count = self._failures[instance_id] = self._failures.get(instance_id, 0) + 1
        facts = await self._facts(
            instance_id, data["browser"], data["instance"].created_at
        )
        label = f"{instance_id}: probe {count} failed ({failure}); {facts}"
        if count < FAILURES_BEFORE_HEAL:
            debug_logger.log_warning(_COMPONENT, "probe", label)
            return
        since = self._clock() - self._last_heal.get(instance_id, -HEAL_COOLDOWN_SECONDS)
        if since < HEAL_COOLDOWN_SECONDS:
            debug_logger.log_warning(
                _COMPONENT,
                "probe",
                f"{label}; healed {since:.0f}s ago, not again within "
                f"{HEAL_COOLDOWN_SECONDS}s",
            )
            return
        debug_logger.log_error(
            _COMPONENT, "probe", HungBrowserError(f"{label}; healing it")
        )
        self._last_heal[instance_id] = self._clock()
        self._failures[instance_id] = 0
        await self._heal(instance_id)

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
        self, instance_id: str, browser: Browser, created: datetime
    ) -> str:
        """The forensics line; bounded, and never raises."""
        entry = process_cleanup.browser_processes.get(instance_id) or {}
        try:
            return await asyncio.wait_for(
                self._forensics(
                    uptime_seconds=(
                        datetime.now(tz=timezone.utc) - created
                    ).total_seconds(),
                    websocket_url=getattr(browser, "websocket_url", None),
                    pid=entry.get("pid") or getattr(browser, "_process_pid", None),
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
        instance = data["instance"]
        # What close_instance reads later (the `default` seed refresh) lives in
        # the spawn diagnostics; the relaunch writes new ones.
        previous = dict(await self._manager.get_spawn_diagnostics(instance_id) or {})
        selection = previous.get("profile_selection") or {}
        # The ONE rule for "this relaunch must be guarded" is the spawn path's.
        guard = options.guard_rotation or fleet_session.guards_rotation(
            {
                "profile_role": selection.get("profile_role"),
                "user_data_dir": options.user_data_dir,
            }
        )
        relaunch = options.model_copy(
            update={
                "headless": instance.headless,
                "viewport_width": instance.viewport.get(
                    "width", options.viewport_width
                ),
                "viewport_height": instance.viewport.get(
                    "height", options.viewport_height
                ),
                "guard_rotation": guard,
            }
        )
        gate = await directory_gate.hold(options.user_data_dir)
        try:
            await self._close(instance_id)
            if process_exit.browser_is_alive(old_browser):
                debug_logger.log_error(
                    _COMPONENT,
                    "heal",
                    HungBrowserError(
                        f"{instance_id}: the close path left the browser running; "
                        "not relaunching onto a profile it still holds"
                    ),
                )
                return False
            try:
                await self._manager.spawn_browser(relaunch, instance_id=instance_id)
            except Exception as error:  # noqa: BLE001  PERMANENT(F-963: a failed relaunch leaves the session closed, which spawn_browser(session=…) relaunches from)
                debug_logger.log_error(_COMPONENT, "heal", error)
                return False
            await self._rearm(instance_id, relaunch, previous)
            return await self._report_healed(instance_id)
        finally:
            gate.release()

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

    async def _report_healed(self, instance_id: str) -> bool:
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
            _COMPONENT, "heal", f"{instance_id}: healed (relaunched under the same id)"
        )
        return True


_task: asyncio.Task[None] | None = None


def start(manager: BrowserManager, *, close: Closer, arm_interception: Armer) -> None:
    """Start the loop beside the idle reaper; a no-op while one is running."""
    global _task  # noqa: PLW0603  PERMANENT(F-963: one loop per process, like the reaper's single task)
    if _task is not None and not _task.done():
        return
    health = BrowserHealth(manager, close=close, arm_interception=arm_interception)
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
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
