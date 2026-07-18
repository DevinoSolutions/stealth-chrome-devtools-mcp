"""Browser instance management with nodriver."""

import asyncio
import contextlib
import os
import time
import uuid
from collections.abc import Coroutine
from datetime import datetime, timezone
from typing import Any

import nodriver as uc
import psutil
from nodriver import Browser, Tab

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.dynamic_hook_system import dynamic_hook_system
from stealth_chrome_devtools_mcp.embedded.in_memory_storage import in_memory_storage
from stealth_chrome_devtools_mcp.embedded.models import (
    BrowserInstance,
    BrowserOptions,
    BrowserState,
    PageState,
)
from stealth_chrome_devtools_mcp.embedded.platform_utils import (
    check_browser_executable,
    get_platform_info,
    merge_browser_args,
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

    def __init__(self):
        self._instances: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._spawn_diagnostics: dict[str, dict[str, Any]] = {}
        self._proxy_forwarders: dict[str, AuthenticatedProxyForwarder] = {}
        self._idle_timeout_seconds_default = get_settings().browser_idle_timeout
        self._idle_reaper_interval_seconds = get_settings().browser_idle_reaper_interval
        self._idle_reaper_task: asyncio.Task | None = None
        # Strong refs to fire-and-forget background tasks so the event loop can't
        # garbage-collect them mid-run; the done-callback discards each entry and
        # surfaces any failure instead of letting it vanish (RUF006).
        self._background_tasks: set[asyncio.Task] = set()

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

    @staticmethod
    def _browser_process_is_alive(browser: Browser) -> bool:
        process = getattr(browser, "_process", None)
        if process is not None:
            poll = getattr(process, "poll", None)
            if callable(poll):
                try:
                    return poll() is None
                except OSError:
                    pass  # process handle invalid or already closed
            return getattr(process, "returncode", None) is None

        pid = getattr(browser, "_process_pid", None)
        if pid:
            try:
                proc = psutil.Process(int(pid))
                return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
            except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
                return False

        return True

    def _discard_instance_unlocked(
        self, instance_id: str, data: dict, reason: str
    ) -> None:
        instance = data.get("instance")
        if instance is not None:
            instance.state = BrowserState.CLOSED
        self._instances.pop(instance_id, None)
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

    def _blocking_teardown(self, instance_id: str, browser: Browser) -> object | None:  # noqa: C901,PLR0912  DEBT(F-702)
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

        if (
            hasattr(browser, "_process")
            and browser._process
            and browser._process.returncode is None
        ):
            for attempt in range(self._KILL_RETRIES):
                try:
                    browser._process.terminate()
                    debug_logger.log_info(
                        "browser_manager",
                        "terminate_process",
                        f"terminated browser with pid "
                        f"{browser._process.pid} successfully on attempt "
                        f"{attempt + 1}",
                    )
                    break
                except Exception:
                    try:
                        browser._process.kill()
                        debug_logger.log_info(
                            "browser_manager",
                            "kill_process",
                            f"killed browser with pid "
                            f"{browser._process.pid} successfully on "
                            f"attempt {attempt + 1}",
                        )
                        break
                    except Exception:
                        try:
                            if (
                                hasattr(browser, "_process_pid")
                                and browser._process_pid
                            ):
                                os.kill(browser._process_pid, 15)
                                debug_logger.log_info(
                                    "browser_manager",
                                    "kill_process",
                                    f"killed browser with pid "
                                    f"{browser._process_pid} using signal 15 "
                                    f"successfully on attempt {attempt + 1}",
                                )
                                break
                        except (PermissionError, ProcessLookupError) as e:
                            debug_logger.log_info(
                                "browser_manager",
                                "kill_process",
                                f"browser already stopped or no "
                                f"permission to kill: {e}",
                            )
                            break
                        except Exception as e:
                            if attempt == self._KILL_RETRIES - 1:
                                debug_logger.log_error(
                                    "browser_manager", "kill_process", e
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
        """
        Resolve the effective idle timeout for a browser instance.

        Args:
            override (Optional[int]): Optional per-instance override.

        Returns:
            int: Effective idle timeout in seconds. Zero disables reaping.
        """
        if self._idle_timeout_seconds_default == 0:
            return 0
        if override is None:
            return self._idle_timeout_seconds_default
        return max(int(override), 0)

    async def touch_instance(self, instance_id: str) -> bool:
        """
        Update the last-activity timestamp for a browser instance.

        Args:
            instance_id (str): Browser instance id.

        Returns:
            bool: True if the instance exists and was touched.
        """
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
                    debug_logger.log_error(
                        "browser_manager",
                        "idle_reaper",
                        error,
                    )
        except asyncio.CancelledError:
            debug_logger.log_info(
                "browser_manager",
                "idle_reaper",
                "Idle reaper task cancelled",
            )
            raise

    async def start_idle_reaper(self) -> None:
        """
        Start the background idle reaper task when globally enabled.

        Returns:
            None
        """
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
        """
        Stop the background idle reaper task if it is running.

        Returns:
            None
        """
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
        # Detect the best available browser executable (Chrome, Chromium, or Edge)
        browser_executable = check_browser_executable()
        if not browser_executable:
            raise Exception(
                "No compatible browser found (Chrome, Chromium, or Microsoft Edge)"
            )

        # Identify browser type for logging
        browser_type = "Unknown"
        if (
            "edge" in browser_executable.lower()
            or "msedge" in browser_executable.lower()
        ):
            browser_type = "Microsoft Edge"
        elif "chromium" in browser_executable.lower():
            browser_type = "Chromium"
        elif "chrome" in browser_executable.lower():
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
        caller_args = merge_proxy_server_arg(
            caller_args,
            launch_proxy_server,
        )
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

        return launch_args, browser_executable, stealth_warnings

    async def _launch_browser(
        self,
        options: BrowserOptions,
        browser_executable: str,
        launch_args: list[str],
    ) -> Browser:
        """Build the ``uc.Config`` and start the browser, returning the live
        ``Browser``.

        Kept minimal — only the fallible ``uc.start`` await lives here — so the
        orchestrator captures the browser handle immediately and can tear it down
        if any later phase raises."""
        config = uc.Config(
            headless=options.headless,
            user_data_dir=options.user_data_dir,
            sandbox=options.sandbox,
            browser_executable_path=browser_executable,
            browser_args=launch_args,
        )

        return await uc.start(config=config)

    async def _apply_post_launch(  # noqa: PLR0913  PERMANENT(function interface)
        self,
        browser: Browser,
        tab: Tab,
        options: BrowserOptions,
        instance_id: str,
        actual_user_data_dir: str | None,
        uses_custom_data_dir: bool,
    ) -> str | None:
        """Register the process for cleanup and apply the per-instance CDP
        overrides (extra headers, viewport, timezone).

        Returns the applied IANA timezone id (or ``None``). Runs after the browser
        is orchestrator-owned, so a failure here still routes through the spawn
        cleanup path."""
        if hasattr(browser, "_process") and browser._process:
            process_cleanup.track_browser_process(
                instance_id,
                browser._process,
                user_data_dir=actual_user_data_dir,
                uses_custom_data_dir=uses_custom_data_dir,
                auto_clone=options.auto_clone,
            )
        else:
            debug_logger.log_warning(
                "browser_manager",
                "spawn_browser",
                f"Browser {instance_id} has no process to track",
            )

        if options.extra_headers:
            await tab.send(
                uc.cdp.network.set_extra_http_headers(headers=options.extra_headers)
            )

        await tab.set_window_size(
            left=0,
            top=0,
            width=options.viewport_width,
            height=options.viewport_height,
        )
        debug_logger.log_info(
            "browser_manager",
            "spawn_browser",
            f"Set viewport to {options.viewport_width}x{options.viewport_height}",
        )

        return await self._apply_timezone_override(
            tab=tab,
            timezone_id=options.timezone_id,
        )

    async def spawn_browser(self, options: BrowserOptions) -> BrowserInstance:  # noqa: C901,PLR0912,PLR0915  DEBT(F-702)
        """
        Spawn a new browser instance with given options.

        Orchestrates the spawn pipeline (``_build_instance`` → ``_resolve_proxy``
        → ``_resolve_launch_args`` → ``_launch_browser`` → ``_apply_post_launch``)
        under one try/except that owns the cancel/error cleanup. ``browser`` and
        ``proxy_forwarder`` are held as orchestrator locals so a failure at any
        phase tears down whatever was already created.

        Args:
            options (BrowserOptions): Options for browser configuration.

        Returns:
            BrowserInstance: The spawned browser instance.
        """
        instance_id = str(uuid.uuid4())
        instance = self._build_instance(instance_id, options)

        browser: Browser | None = None
        proxy_forwarder: AuthenticatedProxyForwarder | None = None
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

            browser = await self._launch_browser(
                options, browser_executable, launch_args
            )
            tab = browser.main_tab
            config_obj = getattr(browser, "config", None)
            actual_user_data_dir = getattr(
                config_obj, "user_data_dir", options.user_data_dir
            )
            uses_custom_data_dir = getattr(
                config_obj,
                "uses_custom_data_dir",
                bool(options.user_data_dir),
            )

            applied_timezone_id = await self._apply_post_launch(
                browser,
                tab,
                options,
                instance_id,
                actual_user_data_dir,
                uses_custom_data_dir,
            )

            await self._setup_dynamic_hooks(tab, instance_id)

            await asyncio.sleep(0.2)
            if not self._browser_process_is_alive(browser):
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

            instance.state = BrowserState.READY
            instance.update_activity()

            instance.current_url = getattr(tab, "url", "") or instance.current_url
            instance.update_activity()
            in_memory_storage.store_instance(
                instance_id, instance.model_dump(mode="json")
            )

        except asyncio.CancelledError:
            if browser is not None:
                try:
                    await self._stop_browser(browser)
                except (OSError, RuntimeError, ConnectionError) as stop_err:
                    debug_logger.log_warning(
                        "browser_manager",
                        "spawn_browser",
                        f"browser.stop() failed during cancel cleanup "
                        f"for {instance_id}: {stop_err}",
                    )
            if proxy_forwarder is not None:
                try:
                    await proxy_forwarder.close()
                except (OSError, ConnectionError) as proxy_err:
                    debug_logger.log_warning(
                        "browser_manager",
                        "spawn_browser",
                        f"Proxy close failed during cancel cleanup "
                        f"for {instance_id}: {proxy_err}",
                    )
            try:
                process_cleanup.kill_browser_process(instance_id)
                process_cleanup.finalize_browser_process(instance_id)
                process_cleanup.cleanup_deferred_profiles()
            except (OSError, psutil.Error, ProcessLookupError) as proc_err:
                debug_logger.log_warning(
                    "browser_manager",
                    "spawn_browser",
                    f"Process cleanup failed during cancel "
                    f"for {instance_id}: {proc_err}",
                )
            async with self._lock:
                self._instances.pop(instance_id, None)
                self._spawn_diagnostics.pop(instance_id, None)
                self._proxy_forwarders.pop(instance_id, None)
            instance.state = BrowserState.CLOSED
            raise
        except Exception as e:
            if browser is not None:
                try:
                    await self._stop_browser(browser)
                except (OSError, RuntimeError, ConnectionError) as stop_err:
                    debug_logger.log_warning(
                        "browser_manager",
                        "spawn_browser",
                        f"browser.stop() failed during error cleanup "
                        f"for {instance_id}: {stop_err}",
                    )
            if proxy_forwarder is not None:
                try:
                    await proxy_forwarder.close()
                except (OSError, ConnectionError) as proxy_err:
                    debug_logger.log_warning(
                        "browser_manager",
                        "spawn_browser",
                        f"Proxy close failed during error cleanup "
                        f"for {instance_id}: {proxy_err}",
                    )
            try:
                process_cleanup.kill_browser_process(instance_id)
            except (OSError, psutil.Error, ProcessLookupError) as proc_err:
                debug_logger.log_warning(
                    "browser_manager",
                    "spawn_browser",
                    f"Process kill failed during error cleanup "
                    f"for {instance_id}: {proc_err}",
                )
            instance.state = BrowserState.ERROR
            raise Exception(f"Failed to spawn browser: {e!s}")  # noqa: B904  plan_M4ph1

        return instance

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
        """
        Get browser instance by ID.

        Args:
            instance_id (str): The ID of the browser instance.

        Returns:
            Optional[dict]: The browser instance data if found, else None.
        """
        async with self._lock:
            data = self._instances.get(instance_id)
            if data and not self._browser_process_is_alive(data["browser"]):
                self._discard_instance_unlocked(
                    instance_id, data, "browser process is not running"
                )
                return None
            return data

    async def list_instances(self) -> list[BrowserInstance]:
        """
        List all browser instances.

        Returns:
            List[BrowserInstance]: List of all browser instances.
        """
        async with self._lock:
            for instance_id, data in list(self._instances.items()):
                if not self._browser_process_is_alive(data["browser"]):
                    self._discard_instance_unlocked(
                        instance_id,
                        data,
                        "browser process is not running",
                    )
            return [data["instance"] for data in self._instances.values()]

    async def close_instance(self, instance_id: str) -> bool:  # noqa: C901,PLR0912,PLR0915  DEBT(F-702)
        """
        Close and remove a browser instance.

        Four-phase teardown that keeps the event loop responsive:
        Phase 1 (claim) — pop shared state under lock.
        Phase 2 (graceful CDP) — close tabs/connection on the loop (bounded).
        Phase 3 (blocking kill) — synchronous kill in a worker thread.
        Phase 4 (finalize) — bookkeeping, no lock needed.
        """
        # -- Phase 1: claim (under lock, O(microseconds)) --------------------
        async with self._lock:
            if instance_id not in self._instances:
                return False
            data = self._instances.pop(instance_id)
            self._spawn_diagnostics.pop(instance_id, None)
            proxy_forwarder = self._proxy_forwarders.pop(instance_id, None)

        browser = data["browser"]
        instance = data["instance"]
        instance.state = BrowserState.CLOSED

        try:
            # -- Phase 2: graceful CDP teardown (on loop, bounded) ------------
            try:
                if hasattr(browser, "tabs") and browser.tabs:
                    for tab in browser.tabs[:]:
                        try:
                            await tab.close()
                        except Exception as tab_err:
                            debug_logger.log_warning(
                                "browser_manager",
                                "close_instance",
                                f"Failed to close tab for {instance_id}: {tab_err}",
                            )
            except Exception as tabs_err:
                debug_logger.log_warning(
                    "browser_manager",
                    "close_instance",
                    f"Failed to close tabs for {instance_id}: {tabs_err}",
                )

            try:
                import nodriver.cdp.browser as cdp_browser

                if (
                    getattr(browser, "connection", None)
                    and not browser.connection.closed
                ):
                    await asyncio.wait_for(
                        browser.connection.send(cdp_browser.close()),
                        timeout=2.0,
                    )
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

            # -- Phase 4: finalize bookkeeping --------------------------------
            with contextlib.suppress(KeyError):
                in_memory_storage.remove_instance(instance_id)

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
        """Return whether a navigation error should trigger one stale-tab
        recovery attempt."""
        if isinstance(error, asyncio.TimeoutError):
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
        """
        Replace the tracked main tab for an instance with a fresh about:blank tab.

        Args:
            instance_id (str): Browser instance id.
            reason (str): Diagnostic reason for replacement.
            close_existing (bool): Whether to close the previously tracked tab.

        Returns:
            Optional[Tab]: The fresh tab, or None if the instance was missing.
        """
        data = await self.get_instance(instance_id)
        if not data:
            return None

        browser = data["browser"]
        previous_tab = data.get("tab")
        new_tab = await browser.get("about:blank", new_tab=True)
        await new_tab

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
        """
        Get a healthy tab for navigation, recovering from stale tracked tabs
        when needed.

        Args:
            instance_id (str): Browser instance id.

        Returns:
            Optional[Tab]: A valid navigation tab, or None if the instance
            does not exist.
        """
        data = await self.get_instance(instance_id)
        if not data:
            return None

        browser = data["browser"]
        tracked_tab = data.get("tab")
        navigation_count = data.get("navigation_count", 0)

        if (
            self.NAVIGATION_RECYCLE_THRESHOLD > 0
            and navigation_count >= self.NAVIGATION_RECYCLE_THRESHOLD
        ):
            return await self._replace_main_tab(
                instance_id,
                reason=(
                    f"navigation recycle threshold "
                    f"{self.NAVIGATION_RECYCLE_THRESHOLD} reached"
                ),
            )

        try:
            await browser.update_targets()
            tracked_target_id = self._get_tab_target_id(tracked_tab)
            if tracked_target_id:
                for candidate_tab in browser.tabs:
                    if self._get_tab_target_id(candidate_tab) == tracked_target_id:
                        await candidate_tab
                        return candidate_tab

            if browser.tabs:
                fallback_tab = browser.tabs[0]
                await fallback_tab
                async with self._lock:
                    if instance_id in self._instances:
                        self._instances[instance_id]["tab"] = fallback_tab
                return fallback_tab
        except Exception as error:
            debug_logger.log_warning(
                "browser_manager",
                "get_navigation_tab",
                f"Tab health check failed for {instance_id}: {error}",
            )

        return await self._replace_main_tab(
            instance_id,
            reason="tracked tab missing or invalid",
            close_existing=False,
        )

    @staticmethod
    async def _wait_for_navigation_condition(
        tab: Tab,
        wait_until: str,
        timeout_seconds: float,
    ) -> None:
        """
        Wait for a navigation milestone within the remaining timeout budget.

        Args:
            tab (Tab): Browser tab.
            wait_until (str): Desired wait condition.
            timeout_seconds (float): Remaining timeout budget in seconds.
        """
        if timeout_seconds <= 0:
            raise TimeoutError("Navigation wait budget exhausted")

        if wait_until == "domcontentloaded":
            await asyncio.wait_for(
                tab.wait(uc.cdp.page.DomContentEventFired),
                timeout=timeout_seconds,
            )
            return

        if wait_until == "networkidle":
            await asyncio.sleep(min(timeout_seconds, 2.0))
            return

        await asyncio.wait_for(
            tab.wait(uc.cdp.page.LoadEventFired),
            timeout=timeout_seconds,
        )

    async def navigate(
        self,
        instance_id: str,
        url: str,
        wait_until: str = "load",
        timeout: int = 30000,  # noqa: ASYNC109  plan_M7
        referrer: str | None = None,
    ) -> dict[str, Any]:
        """
        Navigate with timeout enforcement and one automatic tab-recovery retry.

        Args:
            instance_id (str): Browser instance id.
            url (str): Target URL.
            wait_until (str): Wait condition after navigation.
            timeout (int): Timeout in milliseconds.
            referrer (Optional[str]): Optional referrer header.

        Returns:
            Dict[str, Any]: Navigation result payload.
        """
        timeout_seconds = max(timeout, 1) / 1000
        last_error: Exception | None = None

        for attempt in range(2):
            await self.touch_instance(instance_id)
            if attempt == 0:
                tab = await self.get_navigation_tab(instance_id)
            else:
                tab = await self._replace_main_tab(
                    instance_id,
                    reason=(
                        f"recovering after navigation failure: "
                        f"{type(last_error).__name__ if last_error else 'unknown'}"
                    ),
                )

            if not tab:
                raise Exception(f"Instance not found: {instance_id}")

            start_time = time.monotonic()

            try:
                if referrer:
                    await tab.send(
                        uc.cdp.network.set_extra_http_headers(
                            headers={"Referer": referrer}
                        )
                    )

                await asyncio.wait_for(tab.get(url), timeout=timeout_seconds)

                elapsed = time.monotonic() - start_time
                await self._wait_for_navigation_condition(
                    tab,
                    wait_until,
                    timeout_seconds - elapsed,
                )

                elapsed = time.monotonic() - start_time
                remaining = timeout_seconds - elapsed
                if remaining <= 0:
                    raise TimeoutError("Navigation result budget exhausted")  # noqa: TRY301  plan_M4ph1

                final_url = await asyncio.wait_for(
                    tab.evaluate("window.location.href"),
                    timeout=remaining,
                )
                title = await asyncio.wait_for(
                    tab.evaluate("document.title"),
                    timeout=remaining,
                )

                await self.update_instance_state(instance_id, final_url, title)

                async with self._lock:
                    if instance_id in self._instances:
                        self._instances[instance_id]["tab"] = tab
                        self._instances[instance_id]["navigation_count"] = (
                            self._instances[instance_id].get("navigation_count", 0) + 1
                        )

                return {
                    "url": final_url,
                    "title": title,
                    "success": True,
                }
            except Exception as error:
                last_error = error
                debug_logger.log_warning(
                    "browser_manager",
                    "navigate",
                    f"Navigation attempt {attempt + 1} failed for "
                    f"{instance_id}: {error}",
                    {"url": url, "attempt": attempt + 1},
                )
                if attempt == 1 or not self._is_recoverable_navigation_error(error):
                    if isinstance(error, asyncio.TimeoutError):
                        raise Exception(
                            f"Navigation to {url} timed out after {timeout}ms"
                        ) from error
                    raise
        return None

    async def get_tab(
        self,
        instance_id: str,
        touch_activity: bool = False,
    ) -> Tab | None:
        """Get the main tab for a browser instance (pure read).

        Does NOT refresh the idle timer. Pass ``touch_activity=True``
        or call ``touch_instance`` explicitly to record activity.
        """
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
        """Get the browser object for an instance (pure read).

        Does NOT refresh the idle timer. Pass ``touch_activity=True``
        or call ``touch_instance`` explicitly to record activity.
        """
        data = await self.get_instance(instance_id)
        if data:
            if touch_activity:
                await self.touch_instance(instance_id)
            return data["browser"]
        return None

    async def list_tabs(self, instance_id: str) -> list[dict[str, str]]:
        """
        List all tabs for a browser instance.

        Args:
            instance_id (str): The ID of the browser instance.

        Returns:
            List[Dict[str, str]]: List of tab information dictionaries.
        """
        browser = await self.get_browser(instance_id)
        if not browser:
            return []

        await browser.update_targets()

        tabs = []
        for tab in browser.tabs:
            await tab
            tabs.append(
                {
                    "tab_id": str(tab.target.target_id),
                    "url": getattr(tab, "url", "") or "",
                    "title": getattr(tab.target, "title", "") or "Untitled",
                    "type": getattr(tab.target, "type_", "page"),
                }
            )

        return tabs

    async def switch_to_tab(self, instance_id: str, tab_id: str) -> bool:
        """
        Switch to a specific tab by bringing it to front.

        Args:
            instance_id (str): The ID of the browser instance.
            tab_id (str): The target ID of the tab to switch to.

        Returns:
            bool: True if switched successfully, False otherwise.
        """
        browser = await self.get_browser(instance_id)
        if not browser:
            return False

        await browser.update_targets()

        target_tab = None
        for tab in browser.tabs:
            if str(tab.target.target_id) == tab_id:
                target_tab = tab
                break

        if not target_tab:
            return False

        try:
            await target_tab.bring_to_front()
            async with self._lock:
                if instance_id in self._instances:
                    self._instances[instance_id]["tab"] = target_tab

            return True
        except Exception as e:
            debug_logger.log_warning("browser_manager", "switch_to_tab", str(e))
            return False

    async def get_active_tab(self, instance_id: str) -> Tab | None:
        """
        Get the currently active tab.

        Args:
            instance_id (str): The ID of the browser instance.

        Returns:
            Optional[Tab]: The active tab if found, else None.
        """
        return await self.get_tab(instance_id)

    async def close_tab(self, instance_id: str, tab_id: str) -> bool:
        """
        Close a specific tab.

        Args:
            instance_id (str): The ID of the browser instance.
            tab_id (str): The target ID of the tab to close.

        Returns:
            bool: True if closed successfully, False otherwise.
        """
        browser = await self.get_browser(instance_id)
        if not browser:
            return False

        target_tab = None
        for tab in browser.tabs:
            if str(tab.target.target_id) == tab_id:
                target_tab = tab
                break

        if not target_tab:
            return False

        try:
            await target_tab.close()
            return True
        except Exception as e:
            debug_logger.log_warning("browser_manager", "close_tab", str(e))
            return False

    async def update_instance_state(
        self, instance_id: str, url: str | None = None, title: str | None = None
    ):
        """
        Update instance state after navigation or action.

        Args:
            instance_id (str): The ID of the browser instance.
            url (str, optional): The current URL to update.
            title (str, optional): The title to update.
        """
        async with self._lock:
            if instance_id in self._instances:
                instance = self._instances[instance_id]["instance"]
                if url:
                    instance.current_url = url
                if title:
                    instance.title = title
        await self.touch_instance(instance_id)

    async def get_page_state(self, instance_id: str) -> PageState | None:
        """
        Get complete page state for an instance.

        Args:
            instance_id (str): The ID of the browser instance.

        Returns:
            Optional[PageState]: The page state if available, else None.
        """
        tab = await self.get_tab(instance_id)
        if not tab:
            return None

        try:
            url = await tab.evaluate("window.location.href")
            title = await tab.evaluate("document.title")
            ready_state = await tab.evaluate("document.readyState")

            cookies = await tab.send(uc.cdp.network.get_cookies())

            local_storage = {}
            session_storage = {}

            try:
                local_storage_keys = await tab.evaluate("Object.keys(localStorage)")
                for key in local_storage_keys:
                    value = await tab.evaluate(f"localStorage.getItem('{key}')")
                    local_storage[key] = value

                session_storage_keys = await tab.evaluate("Object.keys(sessionStorage)")
                for key in session_storage_keys:
                    value = await tab.evaluate(f"sessionStorage.getItem('{key}')")
                    session_storage[key] = value
            except (RuntimeError, ConnectionError) as e:
                debug_logger.log_warning(
                    "browser_manager",
                    "get_page_state",
                    f"Storage access failed (connection issue) for {instance_id}: {e}",
                )
            except Exception as e:
                # Pages may block storage access (cross-origin, opaque origins,
                # security policies)
                debug_logger.log_info(
                    "browser_manager",
                    "get_page_state",
                    f"Storage access unavailable for {instance_id}: {e}",
                )

            viewport = await tab.evaluate("""
                ({
                    width: window.innerWidth,
                    height: window.innerHeight,
                    devicePixelRatio: window.devicePixelRatio
                })
            """)

            return PageState(
                instance_id=instance_id,
                url=url,
                title=title,
                ready_state=ready_state,
                cookies=cookies.get("cookies", []),
                local_storage=local_storage,
                session_storage=session_storage,
                viewport=viewport,
            )

        except Exception as e:
            raise Exception(f"Failed to get page state: {e!s}")  # noqa: B904  plan_M4ph1

    async def cleanup_inactive(self, timeout_seconds: int | None = None) -> int:
        """
        Clean up inactive browser instances.

        Args:
            timeout_seconds (Optional[int]): Override timeout in seconds for
            all instances. Uses per-instance values when None.

        Returns:
            int: Number of instances selected for idle cleanup.
        """
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
        """
        Close all browser instances.

        Closes all currently managed browser instances.
        """
        instance_ids = list(self._instances.keys())
        for instance_id in instance_ids:
            await self.close_instance(instance_id)
