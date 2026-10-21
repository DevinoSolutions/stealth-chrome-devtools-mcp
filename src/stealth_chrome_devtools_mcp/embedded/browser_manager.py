"""Browser instance management with nodriver."""

import asyncio
import contextlib
import json
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from datetime import datetime, timezone
from typing import Any

import nodriver as uc
import psutil
from nodriver import Browser, Tab

from stealth_chrome_devtools_mcp.embedded import (
    browser_connect,
    chrome_log,
    desktop_launch,
    google_rotation_guard,
    login_persistence,
    navigation_milestone,
    page_storage,
    process_exit,
    profile_lock,
    spawn_budget,
    spawn_contention,
    spawn_exhaustion,
    spawn_leak,
    tab_close,
    tab_identity,
    tab_open,
    tool_errors,
    window_sizing,
)
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.dynamic_hook_system import dynamic_hook_system
from stealth_chrome_devtools_mcp.embedded.element_resolution import recoverable_race
from stealth_chrome_devtools_mcp.embedded.in_memory_storage import in_memory_storage
from stealth_chrome_devtools_mcp.embedded.models import (
    BrowserInstance,
    BrowserOptions,
    BrowserState,
    PageState,
)
from stealth_chrome_devtools_mcp.embedded.platform_utils import (
    append_start_page,
    check_browser_executable,
    get_platform_info,
    merge_browser_args,
    reconcile_launched_browser_version,
)
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup
from stealth_chrome_devtools_mcp.embedded.proxy_forwarder import (
    AuthenticatedProxyForwarder,
)
from stealth_chrome_devtools_mcp.embedded.proxy_utils import (
    ProxyConfig,
    ProxyConfigError,
    merge_proxy_server_arg,
    parse_proxy_config,
    redact_launch_arg,
)
from stealth_chrome_devtools_mcp.settings import get_settings


class BrowserManager:
    """Manages multiple browser instances."""

    NAVIGATION_RECYCLE_THRESHOLD = 25
    CLOSE_KILL_TIMEOUT: float = get_settings().close_kill_timeout
    _KILL_RETRIES = 3

    def __init__(
        self, tab_armers: Sequence[Callable[[Tab, str], Awaitable[None]]] = ()
    ):
        self._instances: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._spawn_diagnostics: dict[str, dict[str, Any]] = {}
        self._proxy_forwarders: dict[str, AuthenticatedProxyForwarder] = {}
        self._idle_timeout_seconds_default = get_settings().browser_idle_timeout
        self._idle_reaper_interval_seconds = get_settings().browser_idle_reaper_interval
        self._idle_reaper_task: asyncio.Task | None = None
        # Spawns mid-flight + the PEAK of the current burst. A failing spawn reads
        # the peak: one race's losers fail in sequence, so by the last one the live
        # count is 1 again and only the peak still says "you raced" (F-834).
        self._spawns_in_flight = 0
        self._spawn_peak_in_flight = 0
        # Strong refs to fire-and-forget background tasks so the event loop can't
        # garbage-collect them mid-run; the done-callback discards each entry and
        # surfaces any failure instead of letting it vanish (RUF006).
        self._background_tasks: set[asyncio.Task] = set()
        # F-935: ``armer(tab, instance_id)``: per-tab state kept outside this class.
        self._tab_armers = tuple(tab_armers)
        # F-964: (id, entry) of each browser that died ON ITS OWN, for browser_health.
        self._exited: deque[tuple[str, dict]] = deque(maxlen=16)

    def take_exited(self) -> list[tuple[str, dict]]:
        """Drain the browsers that died unasked (only the discard records one)."""
        exited = list(self._exited)
        self._exited.clear()
        return exited

    async def _arm_tracked_tab(self, instance_id: str, tab: Tab) -> None:
        """Give a tracked tab or popup (F-1012) what the spawn tab got (F-935): its
        overrides and armers. Logged, never raised; the navigation is the caller's."""
        data = await self.get_instance(instance_id)
        try:
            if data and data.get("options") is not None:
                await self._apply_tab_overrides(tab, data["options"])
            for armer in self._tab_armers:
                await armer(tab, instance_id)
        except Exception as error:
            debug_logger.log_warning(
                "browser_manager", "_arm_tracked_tab", f"{instance_id}: {error!r}"
            )

    def _run_in_background(
        self, coro: Coroutine[object, object, object], label: str
    ) -> asyncio.Task:
        """Schedule a fire-and-forget coroutine while holding a strong reference to
        its task (RUF006) and surfacing any exception via the debug logger instead
        of silently dropping it."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _on_done(finished: asyncio.Task) -> None:
            self._background_tasks.discard(finished)
            if not finished.cancelled():
                error = finished.exception()
                if error is not None:
                    debug_logger.log_error("browser_manager", label, error)

        task.add_done_callback(_on_done)
        return task

    @staticmethod
    def _append_user_agent_arg(args: list[str], user_agent: str | None) -> list[str]:
        """Merge a user agent override into launch arguments."""
        if not user_agent:
            return args
        ua_prefix = "--user-agent="
        filtered = [arg for arg in args if not arg.startswith(ua_prefix)]
        filtered.append(f"{ua_prefix}{user_agent}")
        return filtered

    @staticmethod
    def _build_spawn_diagnostics(  # noqa: PLR0913  PERMANENT(function interface)
        *,
        launch_args: list[str],
        proxy_server: str | None,
        launch_proxy_server: str | None,
        timezone_id: str | None,
        idle_timeout_seconds: int,
        sandbox: bool,
        headless: bool,
        user_data_dir: str | None,
    ) -> dict[str, Any]:
        """Build redacted diagnostics for a spawned browser instance."""
        return {
            "effective_browser_args": [redact_launch_arg(arg) for arg in launch_args],
            "proxy_server": proxy_server,
            "launch_proxy_server": launch_proxy_server,
            "timezone_id": timezone_id,
            "idle_timeout_seconds": idle_timeout_seconds,
            "sandbox": sandbox,
            "headless": headless,
            "user_data_dir": user_data_dir,
        }

    @staticmethod
    async def _apply_timezone_override(
        *,
        tab: Tab,
        timezone_id: str | None,
    ) -> str | None:
        """Apply a CDP timezone override to a browser tab."""
        if not timezone_id:
            return None

        trimmed_timezone = timezone_id.strip()
        if not trimmed_timezone:
            return None

        await tab.send(
            uc.cdp.emulation.set_timezone_override(timezone_id=trimmed_timezone)
        )
        return trimmed_timezone

    @staticmethod
    async def _stop_browser(browser: Browser) -> None:
        """Stop a nodriver browser regardless of sync or async stop semantics."""
        stop_result = browser.stop()
        if asyncio.iscoroutine(stop_result):
            await stop_result

    def _discard_instance_unlocked(
        self, instance_id: str, data: dict, reason: str
    ) -> None:
        instance = data.get("instance")
        if instance is not None:
            instance.state = BrowserState.CLOSED
        self._instances.pop(instance_id, None)
        self._exited.append((instance_id, data))
        tool_errors.remember_departed(instance_id, data)
        self._spawn_diagnostics.pop(instance_id, None)
        proxy_forwarder = self._proxy_forwarders.pop(instance_id, None)
        if proxy_forwarder is not None:
            self._run_in_background(
                proxy_forwarder.close(), "discard_instance_proxy_close"
            )
        try:
            process_cleanup.finalize_browser_process(instance_id)
            process_cleanup.cleanup_deferred_profiles()
        except (OSError, psutil.Error, KeyError) as e:
            debug_logger.log_warning(
                "browser_manager",
                "discard_instance",
                f"Process finalize failed for {instance_id}: {e}",
            )
        with contextlib.suppress(KeyError):
            in_memory_storage.remove_instance(instance_id)
        with contextlib.suppress(KeyError):
            dynamic_hook_system.remove_instance(instance_id)
        debug_logger.log_info(
            "browser_manager",
            "discard_instance",
            f"Removed stale browser instance {instance_id}: {reason}",
        )

    async def _close_proxy_forwarder(self, instance_id: str) -> None:
        """Close and forget any authenticated proxy forwarder for an instance."""
        proxy_forwarder = self._proxy_forwarders.pop(instance_id, None)
        if proxy_forwarder is None:
            return
        await proxy_forwarder.close()

    def _blocking_teardown(self, instance_id: str, browser: Browser) -> object | None:
        """Synchronous kill work, run in a worker thread via asyncio.to_thread.

        Returns an awaitable if browser.stop() produced a coroutine (nodriver
        API drift edge), otherwise None.
        """
        try:
            process_cleanup.kill_browser_process(instance_id)
        except Exception as e:
            debug_logger.log_warning(
                "browser_manager",
                "close_instance",
                f"Process cleanup failed for {instance_id}: {e}",
            )

        stop_coro = None
        try:
            result = browser.stop()
            if asyncio.iscoroutine(result):
                stop_coro = result
        except Exception as stop_err:
            debug_logger.log_warning(
                "browser_manager",
                "close_instance",
                f"browser.stop() failed for {instance_id}: {stop_err}",
            )

        process_exit.terminate(
            instance_id,
            getattr(browser, "_process", None),
            getattr(browser, "_process_pid", None),
            self._KILL_RETRIES,
        )

        try:
            if hasattr(browser, "_process"):
                browser._process = None
            if hasattr(browser, "_process_pid"):
                browser._process_pid = None
        except Exception as state_err:
            debug_logger.log_warning(
                "browser_manager",
                "close_instance",
                f"Failed to clear process refs for {instance_id}: {state_err}",
            )

        try:
            process_cleanup.finalize_browser_process(instance_id)
            process_cleanup.cleanup_deferred_profiles()
        except Exception as e:
            debug_logger.log_warning(
                "browser_manager",
                "close_instance",
                f"Post-stop cleanup failed for {instance_id}: {e}",
            )

        return stop_coro

    def _resolve_idle_timeout_seconds(
        self,
        override: int | None,
    ) -> int:
        """Effective idle timeout for an instance in seconds; zero disables reaping."""
        if self._idle_timeout_seconds_default == 0:
            return 0
        if override is None:
            return self._idle_timeout_seconds_default
        return max(int(override), 0)

    async def touch_instance(self, instance_id: str) -> bool:
        """Refresh an instance's last-activity stamp; False if it does not exist."""
        async with self._lock:
            if instance_id not in self._instances:
                return False
            self._instances[instance_id]["instance"].update_activity()
            return True

    async def _run_idle_reaper(self) -> None:
        """Periodically close idle browser instances until cancelled."""
        try:
            while True:
                await asyncio.sleep(self._idle_reaper_interval_seconds)
                try:
                    closed_count = await self.cleanup_inactive()
                    finalized_profiles = process_cleanup.cleanup_deferred_profiles()
                    if closed_count:
                        debug_logger.log_info(
                            "browser_manager",
                            "idle_reaper",
                            f"Closed {closed_count} idle browser instance(s)",
                        )
                    if finalized_profiles:
                        debug_logger.log_info(
                            "browser_manager",
                            "idle_reaper",
                            f"Finalized {finalized_profiles} deferred temp "
                            "profile cleanup entrie(s)",
                        )
                except Exception as error:
                    debug_logger.log_error("browser_manager", "idle_reaper", error)
        except asyncio.CancelledError:
            debug_logger.log_info(
                "browser_manager",
                "idle_reaper",
                "Idle reaper task cancelled",
            )
            raise

    async def start_idle_reaper(self) -> None:
        """Start the background idle reaper task when globally enabled."""
        if self._idle_timeout_seconds_default == 0:
            debug_logger.log_info(
                "browser_manager",
                "start_idle_reaper",
                "Idle reaper disabled by BROWSER_IDLE_TIMEOUT=0",
            )
            return
        if self._idle_reaper_task and not self._idle_reaper_task.done():
            return
        self._idle_reaper_task = asyncio.create_task(self._run_idle_reaper())
        debug_logger.log_info(
            "browser_manager",
            "start_idle_reaper",
            f"Idle reaper started with "
            f"timeout={self._idle_timeout_seconds_default}s "
            f"interval={self._idle_reaper_interval_seconds}s",
        )

    async def stop_idle_reaper(self) -> None:
        """Stop the background idle reaper task if it is running."""
        if not self._idle_reaper_task:
            return
        if self._idle_reaper_task.done():
            self._idle_reaper_task = None
            return
        self._idle_reaper_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._idle_reaper_task
        self._idle_reaper_task = None

    def _build_instance(
        self, instance_id: str, options: BrowserOptions
    ) -> BrowserInstance:
        """Construct the in-memory ``BrowserInstance`` record from spawn options.

        Pure (no I/O); the first pipeline phase so the record exists for the
        orchestrator's cleanup paths even if a later phase raises."""
        return BrowserInstance(
            instance_id=instance_id,
            headless=options.headless,
            user_agent=options.user_agent,
            viewport={
                "width": options.viewport_width,
                "height": options.viewport_height,
            },
        )

    def _resolve_proxy(
        self, options: BrowserOptions
    ) -> tuple[ProxyConfig | None, AuthenticatedProxyForwarder | None, str | None]:
        """Parse the proxy option and, for an authenticated proxy, CREATE (but not
        start) the forwarder.

        Returns ``(proxy_config, proxy_forwarder, launch_proxy_server)``. The
        forwarder is returned un-started with ``launch_proxy_server`` ``None`` for
        the authenticated case: the orchestrator starts it and derives its server
        string, so the forwarder is owned by the caller's try/except (and torn
        down) the instant it exists — mirroring the original
        assign-before-``start`` ordering."""
        if not options.proxy:
            return None, None, None
        try:
            proxy_config = parse_proxy_config(options.proxy)
        except ProxyConfigError as error:
            raise Exception(str(error))  # noqa: B904  plan_M4ph1
        if proxy_config.username is not None:
            return proxy_config, AuthenticatedProxyForwarder(options.proxy), None
        return proxy_config, None, proxy_config.server

    def _resolve_launch_args(
        self,
        options: BrowserOptions,
        launch_proxy_server: str | None,
        platform_info: dict[str, Any],
    ) -> tuple[list[str], str, list[str]]:
        """Detect the browser executable and assemble the stealth-filtered launch
        arguments.

        Returns ``(launch_args, browser_executable, stealth_warnings)``; raises if
        no compatible browser is found. Pure apart from the executable probe.
        ``--no-sandbox`` is re-added after the stealth filter when the sandbox is
        explicitly disabled (a deliberate operator choice, not an accidental
        automation leak)."""
        browser_executable = check_browser_executable()
        if not browser_executable:
            raise Exception(
                "No compatible browser found (Chrome, Chromium, or Microsoft Edge)"
            )

        # Identify browser type for logging
        executable_lower = browser_executable.lower()
        browser_type = "Unknown"
        if "edge" in executable_lower or "msedge" in executable_lower:
            browser_type = "Microsoft Edge"
        elif "chromium" in executable_lower:
            browser_type = "Chromium"
        elif "chrome" in executable_lower:
            browser_type = "Google Chrome"

        debug_logger.log_info(
            "browser_manager",
            "spawn_browser",
            f"Platform: {platform_info['system']} | "
            f"Root: {platform_info['is_root']} | "
            f"Container: {platform_info['is_container']} | "
            f"Sandbox: {options.sandbox} | "
            f"Browser: {browser_type} ({browser_executable})",
        )

        caller_args = list(options.browser_args or [])
        caller_args = self._append_user_agent_arg(caller_args, options.user_agent)
        caller_args = window_sizing.append_size_arg(caller_args, options)
        caller_args = merge_proxy_server_arg(caller_args, launch_proxy_server)
        launch_args, stealth_warnings = merge_browser_args(caller_args)
        if stealth_warnings:
            debug_logger.log_warning(
                "browser_manager",
                "stealth_filter",
                f"Stripped {len(stealth_warnings)} detectable arg(s): "
                + "; ".join(stealth_warnings),
            )

        # When sandbox is explicitly disabled, ensure --no-sandbox is present
        # in launch args (added after stealth filter since this is a deliberate
        # platform/user choice, not an accidental automation leak).
        if options.sandbox is False and "--no-sandbox" not in launch_args:
            launch_args.append("--no-sandbox")
        launch_args = login_persistence.protect_logins(launch_args)
        launch_args = append_start_page(launch_args)

        return launch_args, browser_executable, stealth_warnings

    @spawn_budget.phase("launch", "LAUNCH_BUDGET_SECONDS")
    async def _launch_browser(
        self,
        options: BrowserOptions,
        browser_executable: str,
        launch_args: list[str],
        attempt: spawn_leak.Attempt,
    ) -> Browser:
        """Start the browser and return the live ``Browser`` — nodriver's
        ``Browser.start``, or a headed launch delegated to the user's desktop when
        this backend cannot show windows (F-810), the profile held throughout (F-1008).
        Only the fallible starts live here, so the orchestrator holds what exists and
        can tear it down. ``browser_connect`` goes first (F-834 stage 2), *attempt* is
        stamped BEFORE the fallible await (F-919, F-924), and it is bounded (F-969)."""
        browser_connect.install()
        if not options.auto_clone:
            login_persistence.remove_saved_tabs(options.user_data_dir)
        with profile_lock.launching(options.user_data_dir):
            if desktop_launch.should_delegate(options.headless):
                browser, _pid = await desktop_launch.launch_and_attach(
                    browser_executable, launch_args, options.user_data_dir
                )
                return browser
            attempt.config = config = uc.Config(
                headless=options.headless,
                user_data_dir=options.user_data_dir,
                sandbox=options.sandbox,
                browser_executable_path=browser_executable,
                browser_args=launch_args,
            )
            return await browser_connect.start(config)

    @spawn_budget.phase("post-launch setup", "POST_LAUNCH_BUDGET_SECONDS")
    async def _apply_post_launch(  # noqa: PLR0913  PERMANENT(function interface)
        self,
        browser: Browser,
        tab: Tab,
        options: BrowserOptions,
        instance_id: str,
        actual_user_data_dir: str | None,
        uses_custom_data_dir: bool,
        browser_executable: str,
    ) -> tuple[str | None, window_sizing.WindowSizeMetrics]:
        """Register the process, reconcile the masked User-Agent (F-806), then
        apply the per-instance CDP overrides (headers, window size, timezone).

        Returns ``(applied IANA timezone id or None, window-size metrics)``. Runs
        after the browser is orchestrator-owned, so a failure here still routes
        through the spawn cleanup path."""
        # A DELEGATED browser (F-810) was attached to, not spawned: it has no
        # _process, only a pid. Untracked it would be an orphan-reaping hole.
        process = getattr(browser, "_process", None) or desktop_launch.pid_shim(browser)
        if process:
            process_cleanup.track_browser_process(
                instance_id,
                process,
                user_data_dir=actual_user_data_dir,
                uses_custom_data_dir=uses_custom_data_dir,
                auto_clone=options.auto_clone,
                # Read AFTER uc.start (nodriver assigns it there), the only
                # moment this port exists outside our memory — which is what
                # lets a LATER backend reach this browser at all (F-888).
                cdp_port=getattr(getattr(browser, "config", None), "port", None),
            )
        else:
            debug_logger.log_warning(
                "browser_manager",
                "spawn_browser",
                f"Browser {instance_id} has no process to track",
            )

        if options.user_data_dir and not options.auto_clone:
            await login_persistence.ensure_session_restore(browser)
        if options.guard_rotation and google_rotation_guard.enabled():
            await google_rotation_guard.arm(browser)
        await reconcile_launched_browser_version(tab, browser_executable)
        applied_timezone_id = await self._apply_tab_overrides(tab, options)
        window_metrics = await window_sizing.apply_and_measure(tab, options)
        return applied_timezone_id, window_metrics

    async def _apply_tab_overrides(
        self, tab: Tab, options: BrowserOptions
    ) -> str | None:
        """Per-TARGET overrides (headers, timezone, focus emulation F-1011) of each
        tab (F-935); the applied IANA id or None."""
        await window_sizing.enable_focus_emulation(tab)
        if options.extra_headers:
            headers = uc.cdp.network.Headers(options.extra_headers)
            await tab.send(uc.cdp.network.set_extra_http_headers(headers=headers))
        return await self._apply_timezone_override(
            tab=tab, timezone_id=options.timezone_id
        )

    @staticmethod
    def _warn_spawn_cleanup(
        what: str, phase: str, instance_id: str, error: BaseException
    ) -> None:
        """One line per spawn-teardown warning; six sites differed only in noun."""
        debug_logger.log_warning(
            "browser_manager",
            "spawn_browser",
            f"{what} failed during {phase} cleanup for {instance_id}: {error}",
        )

    async def _teardown_failed_spawn(  # noqa: PLR0913  PERMANENT(function interface)
        self,
        phase: str,
        instance_id: str,
        browser: Browser | None,
        proxy_forwarder: AuthenticatedProxyForwarder | None,
        options: BrowserOptions,
        attempt: spawn_leak.Attempt | None,
    ) -> None:
        """Release what a failed spawn had already created, on cancel or error.

        A ``Browser`` we hold is stopped through nodriver. With NO handle but a
        launch that STARTED, the failure landed inside ``_launch_browser`` —
        after nodriver spawned Chrome and before it handed the object back — and
        what still identifies that process is the pid *attempt* leads to
        (F-860, F-919). ``None`` means the launch was never reached.
        """
        if browser is not None:
            try:
                await self._stop_browser(browser)
            except (OSError, RuntimeError, ConnectionError) as err:
                self._warn_spawn_cleanup("browser.stop()", phase, instance_id, err)
        elif attempt is not None:
            spawn_leak.reap_launched_browsers(
                process_cleanup, options.user_data_dir, attempt, instance_id
            )
        if proxy_forwarder is not None:
            try:
                await proxy_forwarder.close()
            except (OSError, ConnectionError) as err:
                self._warn_spawn_cleanup("Proxy close", phase, instance_id, err)

    async def spawn_browser(  # noqa: PLR0915  DEBT(F-702)
        self, options: BrowserOptions, *, instance_id: str | None = None
    ) -> BrowserInstance:
        """Spawn a browser: ``_build_instance`` → ``_resolve_proxy`` →
        ``_resolve_launch_args`` → ``_launch_browser`` → ``_apply_post_launch``,
        under one try/except owning the cancel/error cleanup (``browser`` and
        ``proxy_forwarder`` are orchestrator locals, so any phase's failure tears
        down what exists). *instance_id* is internal: a heal or relaunch (F-963,
        F-964) reuses the id its callers already hold."""
        instance_id = instance_id or str(uuid.uuid4())
        instance = self._build_instance(instance_id, options)
        self._spawns_in_flight += 1
        self._spawn_peak_in_flight = max(
            self._spawn_peak_in_flight, self._spawns_in_flight
        )

        browser: Browser | None = None
        proxy_forwarder: AuthenticatedProxyForwarder | None = None
        launch_attempt: spawn_leak.Attempt | None = None
        try:
            platform_info = get_platform_info()
            idle_timeout_seconds = self._resolve_idle_timeout_seconds(
                options.idle_timeout_seconds,
            )
            proxy_config, proxy_forwarder, launch_proxy_server = self._resolve_proxy(
                options
            )
            if proxy_forwarder is not None:
                await proxy_forwarder.start()
                launch_proxy_server = proxy_forwarder.proxy_server

            launch_args, browser_executable, stealth_warnings = (
                self._resolve_launch_args(options, launch_proxy_server, platform_info)
            )
            launch_args[:0] = chrome_log.flags(options, instance_id)  # F-964

            launch_attempt = spawn_leak.Attempt()
            browser = await self._launch_browser(
                options, browser_executable, launch_args, launch_attempt
            )
            tab = await browser_connect.first_page(browser)  # F-1003
            config_obj = getattr(browser, "config", None)
            actual_user_data_dir = getattr(
                config_obj, "user_data_dir", options.user_data_dir
            )
            uses_custom_data_dir = getattr(
                config_obj,
                "uses_custom_data_dir",
                bool(options.user_data_dir),
            )

            applied_timezone_id, window_metrics = await self._apply_post_launch(
                browser,
                tab,
                options,
                instance_id,
                actual_user_data_dir,
                uses_custom_data_dir,
                browser_executable,
            )

            await self._setup_dynamic_hooks(tab, instance_id)

            await asyncio.sleep(0.2)
            if not process_exit.browser_is_alive(browser):
                raise Exception("Browser process exited immediately after launch")  # noqa: TRY301  plan_M4ph1

            spawn_diagnostics = self._build_spawn_diagnostics(
                launch_args=launch_args,
                proxy_server=proxy_config.server if proxy_config else None,
                launch_proxy_server=launch_proxy_server,
                timezone_id=applied_timezone_id,
                idle_timeout_seconds=idle_timeout_seconds,
                sandbox=options.sandbox,
                headless=options.headless,
                user_data_dir=actual_user_data_dir,
            )
            spawn_diagnostics["window_size"] = window_metrics
            spawn_diagnostics.update(chrome_log.diagnostics(launch_args))
            instance.viewport = window_metrics["actual"] or instance.viewport
            if stealth_warnings:
                spawn_diagnostics["stealth_args_stripped"] = stealth_warnings
            self._spawn_diagnostics[instance_id] = spawn_diagnostics
            if proxy_forwarder is not None:
                self._proxy_forwarders[instance_id] = proxy_forwarder

            async with self._lock:
                self._instances[instance_id] = {
                    "browser": browser,
                    "tab": tab,
                    "instance": instance,
                    "options": options,
                    "navigation_count": 0,
                    "idle_timeout_seconds": idle_timeout_seconds,
                    "spawn_diagnostics": spawn_diagnostics,
                    "network_data": [],
                }
            tab_open.arm_popups(browser, self._arm_tracked_tab, instance_id)

            instance.state = BrowserState.READY
            instance.last_navigated_url = (
                getattr(tab, "url", "") or instance.last_navigated_url
            )
            instance.update_activity()
            in_memory_storage.store_instance(
                instance_id, instance.model_dump(mode="json")
            )

        except asyncio.CancelledError:
            await self._teardown_failed_spawn(
                "cancel",
                instance_id,
                browser,
                proxy_forwarder,
                options,
                launch_attempt,
            )
            try:
                process_cleanup.kill_browser_process(instance_id)
                process_cleanup.finalize_browser_process(instance_id)
                process_cleanup.cleanup_deferred_profiles()
            except (OSError, psutil.Error, ProcessLookupError) as err:
                self._warn_spawn_cleanup("Process cleanup", "cancel", instance_id, err)
            async with self._lock:
                self._instances.pop(instance_id, None)
                self._spawn_diagnostics.pop(instance_id, None)
                self._proxy_forwarders.pop(instance_id, None)
            instance.state = BrowserState.CLOSED
            raise
        except Exception as e:
            await self._teardown_failed_spawn(
                "error",
                instance_id,
                browser,
                proxy_forwarder,
                options,
                launch_attempt,
            )
            try:
                process_cleanup.kill_browser_process(instance_id)
            except (OSError, psutil.Error, ProcessLookupError) as err:
                self._warn_spawn_cleanup("Process kill", "error", instance_id, err)
            instance.state = BrowserState.ERROR
            # Two independent causes, two homes, one composition site: each hint
            # carries its own "\n\n", so this stays a bare concatenation.
            hint = spawn_exhaustion.exhaustion_hint(process_cleanup.pid_file) or ""
            hint += spawn_contention.contention_hint(self._spawn_peak_in_flight) or ""
            # No "Failed to spawn browser:" prefix here: the spawn_browser tool in
            # server.py wraps this exception with exactly one, and carrying a second
            # copy doubled it in the user-visible ToolError.
            raise Exception(f"{e!s}{hint}")  # noqa: B904  plan_M4ph1
        finally:
            self._spawns_in_flight -= 1
            if self._spawns_in_flight == 0:
                self._spawn_peak_in_flight = 0

        return instance

    @spawn_budget.phase("dynamic-hook setup", "POST_LAUNCH_BUDGET_SECONDS")
    async def _setup_dynamic_hooks(self, tab: Tab, instance_id: str) -> bool:
        """Setup dynamic hook system for browser instance."""
        try:
            dynamic_hook_system.add_instance(instance_id)

            await dynamic_hook_system.setup_interception(tab, instance_id)

            debug_logger.log_info(
                "browser_manager",
                "_setup_dynamic_hooks",
                f"Dynamic hook system setup complete for instance {instance_id}",
            )

            return True

        except Exception as e:
            debug_logger.log_error(
                "browser_manager",
                "_setup_dynamic_hooks",
                f"Failed to setup dynamic hooks for {instance_id}: {e}",
            )
            return False

    async def get_instance(self, instance_id: str) -> dict | None:
        """Instance data by id, or None; a browser whose process died is discarded."""
        async with self._lock:
            data = self._instances.get(instance_id)
            if data and not process_exit.browser_is_alive(data["browser"]):
                self._discard_instance_unlocked(
                    instance_id, data, "browser process is not running"
                )
                return None
            return data

    async def list_instances(self) -> list[BrowserInstance]:
        """All browser instances (a dead browser is discarded)."""
        async with self._lock:
            for instance_id, data in list(self._instances.items()):
                if not process_exit.browser_is_alive(data["browser"]):
                    self._discard_instance_unlocked(
                        instance_id,
                        data,
                        "browser process is not running",
                    )
            return [data["instance"] for data in self._instances.values()]

    async def close_instance(self, instance_id: str) -> bool:  # noqa: C901,PLR0912,PLR0915  DEBT(F-702)
        """Close and remove an instance; four steps keep the event loop responsive:
        Phase 1 (claim) — pop shared state under lock, the in-memory-storage
            entry included (F-899: it must not outlive the pop).
        Phase 2 (graceful CDP) — close tabs/connection on the loop (bounded).
        Phase 2b (grace) — wait, bounded, for Chrome's OWN exit before anything
            kills it: that exit commits the cookie store (F-910, `settle`).
        Phase 3 (blocking kill) — synchronous kill in a worker thread.
        """
        # -- Phase 1: claim (under lock, O(microseconds)) --------------------
        async with self._lock:
            if instance_id not in self._instances:
                return False
            data = self._instances.pop(instance_id)
            tool_errors.remember_departed(instance_id, data)
            self._spawn_diagnostics.pop(instance_id, None)
            proxy_forwarder = self._proxy_forwarders.pop(instance_id, None)
            # F-899: the store cross-checks `_instances`, so no `await` may
            # separate the two — six awaits later it stranded a `stored` row.
            with contextlib.suppress(KeyError):
                in_memory_storage.remove_instance(instance_id)

        browser = data["browser"]
        instance = data["instance"]
        instance.state = BrowserState.CLOSED

        try:
            # -- Phase 2: graceful CDP teardown (on loop, bounded) ------------
            try:
                if hasattr(browser, "tabs") and browser.tabs:
                    for tab in browser.tabs[:]:
                        try:
                            close = uc.cdp.target.close_target(tab.target.target_id)
                            await asyncio.wait_for(browser.connection.send(close), 2.0)
                        except Exception as e:
                            tab_close.log_failure("close_instance", instance_id, e)
            except Exception as tabs_err:
                debug_logger.log_warning(
                    "browser_manager",
                    "close_instance",
                    f"Failed to close tabs for {instance_id}: {tabs_err}",
                )

            try:
                import nodriver.cdp.browser as cdp_browser

                conn = getattr(browser, "connection", None)
                if conn and not conn.closed:
                    await asyncio.wait_for(conn.send(cdp_browser.close()), timeout=2.0)
            except (TimeoutError, Exception) as cdp_err:
                debug_logger.log_info(
                    "browser_manager",
                    "close_instance",
                    f"CDP browser.close() skipped for {instance_id}: {cdp_err}",
                )

            try:
                if getattr(browser, "connection", None):
                    await asyncio.wait_for(browser.connection.disconnect(), timeout=2.0)
                    debug_logger.log_info(
                        "browser_manager",
                        "close_connection",
                        "closed websocket connection",
                    )
            except (TimeoutError, Exception) as e:
                debug_logger.log_info(
                    "browser_manager",
                    "close_connection",
                    f"connection disconnect failed or timed out: {e}",
                )

            try:
                await self._close_proxy_forwarder_ref(proxy_forwarder)
            except Exception as proxy_err:
                debug_logger.log_warning(
                    "browser_manager",
                    "close_instance",
                    f"Proxy forwarder close failed for {instance_id}: {proxy_err}",
                )

            # -- Phase 2b: let Chrome finish leaving (F-910) -------------------
            # The graceful close above is the START of Chrome's shutdown, and
            # that shutdown is what COMMITS the cookie store — terminating
            # mid-flush loses the login the caller just made. The grace, its
            # measurement and what a cancelled close does about it are all
            # `process_exit.settle`'s. Phase 3 keeps its whole budget: this is
            # its own await.
            await process_exit.settle(
                instance_id,
                getattr(browser, "_process", None),
                getattr(browser, "_process_pid", None),
                self._KILL_RETRIES,
            )

            # -- Phase 3: blocking kill (off the loop, real timeout) ----------
            stop_coro = None
            try:
                stop_coro = await asyncio.wait_for(
                    asyncio.to_thread(self._blocking_teardown, instance_id, browser),
                    timeout=self.CLOSE_KILL_TIMEOUT,
                )
            except TimeoutError:
                debug_logger.log_warning(
                    "browser_manager",
                    "close_instance",
                    f"Chrome kill for {instance_id} exceeded "
                    f"{self.CLOSE_KILL_TIMEOUT}s; worker thread continues "
                    f"in background, orphan will be reaped by process_cleanup",
                )
            except Exception as e:
                debug_logger.log_warning(
                    "browser_manager",
                    "close_instance",
                    f"Blocking teardown failed for {instance_id}: {e}",
                )

            if stop_coro is not None:
                try:
                    await asyncio.wait_for(stop_coro, timeout=2.0)
                except (TimeoutError, Exception) as e:
                    debug_logger.log_warning(
                        "browser_manager",
                        "close_instance",
                        f"browser.stop() coroutine failed for {instance_id}: {e}",
                    )

            return True
        except Exception as e:
            debug_logger.log_error("browser_manager", "close_instance", e)
            return False

    @staticmethod
    async def _close_proxy_forwarder_ref(
        proxy_forwarder: AuthenticatedProxyForwarder | None,
    ) -> None:
        """Close a captured proxy forwarder reference (Phase 2 helper)."""
        if proxy_forwarder is not None:
            await proxy_forwarder.close()

    async def get_spawn_diagnostics(self, instance_id: str) -> dict[str, Any] | None:
        """Get spawn diagnostics for an instance."""
        return self._spawn_diagnostics.get(instance_id)

    @staticmethod
    def _get_tab_target_id(tab: Tab | None) -> str | None:
        """Get a stable target id string for a tab when available."""
        if tab is None:
            return None
        target = getattr(tab, "target", None)
        target_id = getattr(target, "target_id", None)
        if target_id is None:
            return None
        return str(target_id)

    @staticmethod
    def _is_recoverable_navigation_error(error: Exception) -> bool:
        """Whether a navigation error earns one stale-tab recovery attempt (the
        nodriver races are classified in element_resolution, not re-listed; F-824)."""
        if isinstance(error, asyncio.TimeoutError) or recoverable_race(error):
            return True
        message = f"{type(error).__name__}: {error}".lower()
        recoverable_markers = (
            "connection dropped",
            "connection closed",
            "connection lost",
            "websocket",
            "target closed",
            "target crashed",
            "session closed",
            "invalid state",
            "not attached",
        )
        return any(marker in message for marker in recoverable_markers)

    async def _replace_main_tab(
        self,
        instance_id: str,
        reason: str,
        close_existing: bool = True,
    ) -> Tab | None:
        """Replace the tracked main tab for an instance with a fresh about:blank
        tab, closing the previous one unless told not to. ``None`` when the
        instance was missing; *reason* is the diagnostic this logs under."""
        data = await self.get_instance(instance_id)
        if not data:
            return None

        browser = data["browser"]
        previous_tab = data.get("tab")
        new_tab = await tab_open.open_tab(browser, "about:blank")
        await new_tab
        await self._arm_tracked_tab(instance_id, new_tab)

        if close_existing and previous_tab:
            previous_target_id = self._get_tab_target_id(previous_tab)
            new_target_id = self._get_tab_target_id(new_tab)
            if previous_target_id and previous_target_id != new_target_id:
                try:
                    await previous_tab.close()
                except (ConnectionError, RuntimeError, OSError) as e:
                    debug_logger.log_warning(
                        "browser_manager",
                        "_replace_main_tab",
                        f"Failed to close previous tab for {instance_id}: {e}",
                    )

        async with self._lock:
            if instance_id in self._instances:
                self._instances[instance_id]["tab"] = new_tab
                self._instances[instance_id]["navigation_count"] = 0

        debug_logger.log_info(
            "browser_manager",
            "_replace_main_tab",
            f"Replaced main tab for {instance_id}: {reason}",
        )
        return new_tab

    async def get_navigation_tab(self, instance_id: str) -> Tab | None:
        """A healthy tab for navigation: the recycle threshold, then
        :meth:`live_tab`. ``None`` when the instance does not exist."""
        data = await self.get_instance(instance_id)
        if not data:
            return None

        threshold = self.NAVIGATION_RECYCLE_THRESHOLD
        if threshold > 0 and data.get("navigation_count", 0) >= threshold:
            return await self._replace_main_tab(
                instance_id,
                reason=f"navigation recycle threshold {threshold} reached",
            )
        return await self.live_tab(instance_id)

    async def live_tab(self, instance_id: str) -> Tab | None:
        """The tracked tab if Chrome still has its target, else a fresh one (F-963).

        THE one stale-tab recovery: a caller may have closed the tab we track,
        and any CDP socket opened on its gone target id is answered HTTP 500.
        Never closes another caller's tab. ``None`` when the instance is gone.

        F-775a: never await a ``browser.tabs`` entry, nor hand one to a caller that
        calls Tab-only methods on it (they are raw Connections after any
        rediscovery). The loop is a presence check on the target id and returns
        the caller's own live Tab; every other path takes _replace_main_tab.
        """
        data = await self.get_instance(instance_id)
        if not data:
            return None

        tracked_tab = data.get("tab")
        try:
            await data["browser"].update_targets()
            tracked_target_id = self._get_tab_target_id(tracked_tab)
            if tracked_target_id:
                for candidate_tab in data["browser"].tabs:
                    if self._get_tab_target_id(candidate_tab) == tracked_target_id:
                        return tracked_tab
        except Exception as error:
            debug_logger.log_warning(
                "browser_manager",
                "live_tab",
                f"Tab health check failed for {instance_id}: {error}",
            )

        return await self._replace_main_tab(
            instance_id,
            reason="tracked tab missing or invalid",
            close_existing=False,
        )

    async def navigate(  # noqa: PLR0913  PERMANENT(navigate's five plus F-962's pinned tab)
        self,
        instance_id: str,
        url: str,
        wait_until: str = "load",
        timeout: int = 30000,  # noqa: ASYNC109  plan_M7
        referrer: str | None = None,
        pinned: Tab | None = None,
    ) -> dict[str, Any]:
        """Navigate (``timeout`` in ms); answer ``navigation_milestone.answer``.

        ``pinned``: a caller's own tab (F-962), navigated as-is — never recycled,
        replaced or made the instance's tab, and a failure is reported, not retried.

        ``referrer`` rides this one ``Page.navigate`` (F-965), never the tab's
        extra headers, so it cannot outlive the navigation or replace the set the
        caller installed.

        One stale-tab recovery retry (F-824) — but only for a failure Chrome
        never accepted; a timeout after ``Page.navigate`` answered is the page's
        own and is reported, not retried (F-881).
        """
        timeout_seconds = max(timeout, 1) / 1000
        last_error: Exception | None = None
        navigation_milestone.require(wait_until)  # a typo costs no CDP send (F-881)

        for attempt in range(2):
            progress = navigation_milestone.Progress()
            await self.touch_instance(instance_id)
            if attempt == 0:
                tab = pinned or await self.get_navigation_tab(instance_id)
            else:
                cause = type(last_error).__name__ if last_error else "unknown"
                reason = f"recovering after navigation failure: {cause}"
                tab = await self._replace_main_tab(instance_id, reason=reason)

            if not tab:
                raise tool_errors.instance_not_found(instance_id)

            start_time = time.monotonic()

            try:
                # The ONE navigation wait (F-881) — never `tab.get` (a 0.5 s
                # sleep) nor `tab.wait(<event class>)` (a no-op).
                await asyncio.wait_for(
                    navigation_milestone.navigate(
                        tab, url, wait_until, timeout_seconds, progress, referrer
                    ),
                    timeout=timeout_seconds,
                )

                remaining = timeout_seconds - (time.monotonic() - start_time)
                if remaining <= 0:
                    raise TimeoutError("Navigation result budget exhausted")  # noqa: TRY301  plan_M4ph1

                # ONE round trip for both (F-882): two straddled a `meta refresh`
                # and answered with one document's url and another's title.
                final_url, title = await asyncio.wait_for(
                    navigation_milestone.landing(tab), timeout=remaining
                )

                await self.update_instance_state(instance_id, final_url, title)

                async with self._lock:
                    if instance_id in self._instances and pinned is None:
                        self._instances[instance_id]["tab"] = tab
                        self._instances[instance_id]["navigation_count"] = (
                            self._instances[instance_id].get("navigation_count", 0) + 1
                        )

                return navigation_milestone.answer(final_url, title, progress)
            except Exception as error:
                last_error = error
                # The reason comes from BOTH witnesses (F-882): a TimeoutError's
                # own message is empty, so the line used to end at the colon.
                debug_logger.log_warning(
                    "browser_manager",
                    "navigate",
                    f"Navigation attempt {attempt + 1} failed for {instance_id}: "
                    f"{type(error).__name__}: {error} [{progress.describe()}]",
                    {"url": url, "attempt": attempt + 1},
                )
                # A timeout after Chrome ACCEPTED the navigation is the page's own
                # (slow, never loading, a download): a replaced tab would discard
                # a page that exists and spend a second budget (F-881).
                pages_own = isinstance(error, TimeoutError) and progress.accepted
                retry = self._is_recoverable_navigation_error(error) and not pages_own
                if attempt == 1 or not retry or pinned is not None:
                    if isinstance(error, asyncio.TimeoutError):
                        raise tool_errors.ToolError(
                            f"Navigation to {url} timed out after {timeout}ms "
                            f"({progress.describe()})"
                        ) from error
                    raise
        return None

    async def get_tab(
        self,
        instance_id: str,
        touch_activity: bool = False,
    ) -> Tab | None:
        """The main tab of an instance (pure read: the idle timer is NOT refreshed
        unless ``touch_activity=True`` or ``touch_instance`` says so)."""
        data = await self.get_instance(instance_id)
        if data:
            if touch_activity:
                await self.touch_instance(instance_id)
            return data["tab"]
        return None

    async def get_browser(
        self,
        instance_id: str,
        touch_activity: bool = False,
    ) -> Browser | None:
        """The browser of an instance (pure read: the idle timer is NOT refreshed
        unless ``touch_activity=True`` or ``touch_instance`` says so)."""
        data = await self.get_instance(instance_id)
        if data:
            if touch_activity:
                await self.touch_instance(instance_id)
            return data["browser"]
        return None

    async def list_tabs(self, instance_id: str) -> list[dict[str, str]]:
        """One ``{tab_id, url, title, type}`` record per tab; ``[]`` on a miss."""
        browser = await self.get_browser(instance_id)
        if not browser:
            return []

        # No per-tab `await` (F-771): update_targets() just refreshed every field
        # below; a rediscovered target is a raw Connection with no __await__ (its
        # __getattr__ still answers .url); and Tab.wait() costs 0.5s per tab.
        # The record itself is `tab_identity`'s, shared with get_active_tab and
        # list_instances so the three cannot disagree (F-874).
        await browser.update_targets()

        return [tab_identity.record(tab) for tab in browser.tabs]

    async def switch_to_tab(self, instance_id: str, tab_id: str) -> bool:
        """Bring tab *tab_id* to front and make it the stored active tab.

        ``False`` if it is unknown or CDP refused.
        """
        browser = await self.get_browser(instance_id)
        if not browser:
            return False

        await browser.update_targets()

        target_tab = tab_open.find(browser, tab_id)
        if not target_tab:
            return False

        try:
            target_id = target_tab.target.target_id
            await browser.connection.send(uc.cdp.target.activate_target(target_id))
            await self._arm_tracked_tab(instance_id, target_tab)
            async with self._lock:
                if instance_id in self._instances:
                    self._instances[instance_id]["tab"] = target_tab

            return True
        except Exception as e:
            debug_logger.log_warning("browser_manager", "switch_to_tab", str(e))
            return False

    async def get_active_tab(self, instance_id: str) -> Tab | None:
        """The instance's stored active tab, or ``None``."""
        return await self.get_tab(instance_id)

    async def _repoint_after_close(
        self, instance_id: str, browser: Browser, closed_id: str
    ) -> None:
        """Re-point the stored active tab when ``close_tab`` destroyed it (F-845).

        Nothing else did, so every later tab-scoped call opened a websocket to
        the dead target and got Chrome's raw "server rejected WebSocket
        connection: HTTP 500". Storing ``None`` when nothing survives lets
        ``tool_errors._require_tab`` raise the honest typed error instead.
        ``browser.tabs`` is page-filtered and read as ``list_tabs`` reads it —
        no per-tab ``await`` (F-771) — and the closed id is excluded explicitly:
        nodriver drops a destroyed target only on ``Target.targetDestroyed``.
        """
        async with self._lock:
            data = self._instances.get(instance_id)
            stored = data.get("tab") if data else None
        if self._get_tab_target_id(stored) != closed_id:
            return
        await browser.update_targets()
        survivor = next(
            (t for t in browser.tabs if self._get_tab_target_id(t) != closed_id),
            None,
        )
        if survivor is not None:
            await self._arm_tracked_tab(instance_id, survivor)
        async with self._lock:
            if instance_id in self._instances:
                self._instances[instance_id]["tab"] = survivor

    async def close_tab(self, instance_id: str, tab_id: str) -> bool:
        """Close tab *tab_id*; ``False`` if unknown or CDP refused (gone is closed)."""
        browser = await self.get_browser(instance_id)
        if not browser:
            return False

        target_tab = tab_open.find(browser, tab_id)
        if not target_tab:
            return False

        try:
            target_id = target_tab.target.target_id
            await browser.connection.send(uc.cdp.target.close_target(target_id))
        except Exception as e:
            if not tab_close.log_failure("close_tab", instance_id, e):  # F-996b
                return False
        # Outside the handler: the close already succeeded (F-775b lying-False).
        await self._repoint_after_close(instance_id, browser, str(target_id))
        return True

    async def update_instance_state(
        self, instance_id: str, url: str | None = None, title: str | None = None
    ):
        """Record what a navigation reported, as the instance's ``last_navigated``
        pair. THE one writer of that cache besides the spawn.

        ``is not None``, not truthiness (F-874): an empty title is what the page
        HAS — Amazon sets its title after load, a bare ``data:text/html``
        document never sets one — and ``if title:`` silently kept the PREVIOUS
        page's instead, which is how a measured instance on a ``data:`` URL still
        reported "Wikipedia, the free encyclopedia". ``None`` alone means this
        caller had nothing to say, which is why it is the default of both
        parameters.
        """
        async with self._lock:
            if instance_id in self._instances:
                instance = self._instances[instance_id]["instance"]
                if url is not None:
                    instance.last_navigated_url = url
                if title is not None:
                    instance.last_navigated_title = title
        await self.touch_instance(instance_id)

    async def get_page_state(
        self, instance_id: str, tab: Tab | None = None
    ) -> PageState | None:
        """The full page state of *tab* (default: the instance's), or ``None``
        when there is none. *tab* is the caller's own (F-962).

        Raises on a collection failure — ``get_instance_state`` is what turns
        that into its ``partial`` record. Every read below takes the shape
        nodriver really answers with; see the ``JSON.stringify`` note (F-844).
        """
        tab = tab or await self.get_tab(instance_id)
        if not tab:
            return None

        try:
            url = await tab.evaluate("window.location.href")
            title = await tab.evaluate("document.title")
            ready_state = await tab.evaluate("document.readyState")

            # nodriver's wrapper already deserializes: a ``list[Cookie]``, never
            # a ``{"cookies": [...]}`` envelope (F-844). PageState wants dicts.
            cookies = await tab.send(uc.cdp.network.get_cookies()) or []

            local_storage: dict[str, str] = {}
            session_storage: dict[str, str] = {}
            try:
                local_storage, session_storage = await page_storage.read(tab)
            except page_storage.StorageBlockedError as blocked:
                # The ONLY condition this message was ever meant for: the PAGE
                # itself refused (opaque origin, ``data:`` URL, storage disabled
                # by policy), so empty dicts are the truth here. Anything else —
                # F-869's TypeError among them — now reaches the handler below
                # and becomes get_instance_state's honest partial record.
                debug_logger.log_info(
                    "browser_manager",
                    "get_page_state",
                    f"Storage access unavailable for {instance_id}: {blocked}",
                )

            # ``JSON.stringify``, not a bare object literal: nodriver always
            # sends deep serialization options, so an object comes back as
            # ``[[key, {type,value}], …]`` — return_by_value cannot undo it.
            viewport = json.loads(
                await tab.evaluate(
                    "JSON.stringify({width:innerWidth,height:innerHeight,"
                    "devicePixelRatio})"
                )
            )

            return PageState(
                instance_id=instance_id,
                url=url,
                title=title,
                ready_state=ready_state,
                cookies=[c.to_json() for c in cookies],
                local_storage=local_storage,
                session_storage=session_storage,
                viewport=viewport,
            )

        except Exception as e:
            # F-869: ONE place a collection failure is recorded, at WARNING with
            # its traceback. It used to be INFO'd as "storage unavailable" and
            # dropped. This reaches the caller (get_instance_state's partial
            # record) and a post-mortem; NOT Sentry — see the finding's §8.
            debug_logger.log_warning(
                "browser_manager",
                "get_page_state",
                f"Page state collection failed for {instance_id}: {e}",
                error=e,
            )
            raise Exception(f"Failed to get page state: {e!s}")  # noqa: B904  plan_M4ph1

    async def cleanup_inactive(self, timeout_seconds: int | None = None) -> int:
        """Close idle instances; the count selected. *timeout_seconds* overrides
        every instance's own idle timeout."""
        now = datetime.now(tz=timezone.utc)

        to_close = []
        async with self._lock:
            for instance_id, data in self._instances.items():
                instance = data["instance"]
                effective_timeout = (
                    timeout_seconds
                    if timeout_seconds is not None
                    else data.get(
                        "idle_timeout_seconds", self._idle_timeout_seconds_default
                    )
                )
                if effective_timeout <= 0:
                    continue
                if (now - instance.last_activity).total_seconds() > effective_timeout:
                    to_close.append(instance_id)

        for instance_id in to_close:
            await self.close_instance(instance_id)

        return len(to_close)

    async def close_all(self):
        """Close every managed browser instance."""
        instance_ids = list(self._instances.keys())
        for instance_id in instance_ids:
            await self.close_instance(instance_id)
