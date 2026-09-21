"""The ``browser-management`` tools. See ``tool_sections/__init__.py`` for the contract.

plan_SERVERSPLIT slice 10 — the second-largest section and the one that owns the
BROWSER's own lifecycle: ``spawn_browser`` (the plan's largest single tool),
``close_instance``, the three history verbs and ``navigate``.

Two things make this section different from the nine before it, and both are
reasons the mechanism had to be proven elsewhere first:

* ``spawn_browser`` carries the F-808/F-810 headed-visibility guard, which runs
  BEFORE the ``try`` and outside it, so a spawn nobody could ever see refuses
  without first cloning a profile dir onto disk. It reads
  ``rt.display_context.display_context()`` for the refusal message and calls
  ``desktop_launch.can_deliver_headed_window()`` through a function-local import
  — both carried verbatim, including the local import, which is what keeps
  ``desktop_launch`` off this module's import graph.
* ``spawn_browser`` and ``close_instance`` are the two ends of the on-disk
  profile/clone lifecycle, so six of this module's calls land in
  ``rt.clone_storage`` — the disk subsystem, resolved at call time like every
  other singleton, which is what keeps
  ``tests/test_clone_storage.py``'s "patch it THERE, not on server" pin true of a
  body that no longer lives in ``server.py``.

Bodies moved verbatim: the only edits are the dropped registration decorator
(contract rule 2 — registration is driven from ``server.py``'s binding loop, once
per execution of that module body) and the rewrite of the singleton/knob reads to
``rt.<name>``, resolved at CALL time against the one patchable home (contract
rule 3). Docstrings and signatures are byte-identical — FastMCP surfaces them and
``tests/goldens/tool_surface.json`` is a HARD golden for this migration — and so
is ``get_instance_state``'s ``# F-164 non-CDP`` marker comment, which
``tests/test_cdp_timeout.py`` follows into this file through
``tests/source_scan.py``.
"""

import asyncio
from typing import TYPE_CHECKING, Any

from stealth_chrome_devtools_mcp.embedded import tab_identity
from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.models import (
    BrowserOptions,
)
from stealth_chrome_devtools_mcp.embedded.platform_utils import (
    is_running_as_root,
    is_running_in_container,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import (
    ToolError,
    _require_landing_ok,
    _require_tab,
)
from stealth_chrome_devtools_mcp.settings import get_settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Quoted at the one use site rather than `from __future__ import
    # annotations`: that import would stringify EVERY annotation in this module,
    # including the eight tool signatures FastMCP builds `tool_surface.json`'s
    # HARD golden from.
    from stealth_chrome_devtools_mcp.embedded.models import BrowserInstance

SECTION = "browser-management"

# How many times a spawn drives a profile selection before giving up. Named
# rather than inline because the LAST attempt is now a decision: it must not ask
# for a re-selection, since nothing will ever drive one (F-834 stage 1).
_SPAWN_ATTEMPTS = 3


async def spawn_browser(
    headless: bool = False,
    user_agent: str | None = None,
    viewport_width: int = 1920,
    viewport_height: int = 1080,
    proxy: str | None = None,
    browser_args: list[str] = None,
    timezone_id: str | None = None,
    idle_timeout_seconds: int | None = None,
    block_resources: list[str] = None,
    extra_headers: dict[str, str] = None,
    session: str | None = None,
    user_data_dir: str | None = None,
    sandbox: Any | None = None,
) -> dict[str, Any]:
    """
    Spawn a new browser instance.

    Args:
        headless (bool): Run in headless mode.
        user_agent (Optional[str]): Custom user agent string.
        viewport_width (int): Requested browser WINDOW width in pixels (outer, not
            the CSS viewport). Best-effort: a headed window is clamped to the work
            area of the LAUNCHING context's desktop — the user's monitor only when
            the backend runs on it (F-808), not the caller's screen — so a request
            larger than that desktop lands smaller.
        viewport_height (int): Requested browser WINDOW height in pixels, same
            best-effort clamping as viewport_width.
        proxy (Optional[str]): Proxy server URL.
        browser_args (List[str]): Additional browser launch args.
        timezone_id (Optional[str]): IANA timezone ID applied via CDP timezone override.
        idle_timeout_seconds (Optional[int]): Idle timeout override in seconds for automatic instance cleanup.
        block_resources (List[str]): List of resource types to block (e.g., ['image', 'font', 'stylesheet']).
        extra_headers (Dict[str, str]): Additional HTTP headers.
        session (Optional[str]): The NAME of a persistent browser session — THE
            one documented way to ask for a profile, and the only one to use.
            Leave UNSET for normal use: an unnamed spawn gets a disposable copy
            of the shared ``default`` session and deletes it as soon as the
            browser closes, so you never manage or clean up sessions. Set it
            only when the user has EXPLICITLY asked to keep a login: a named
            session is NOT auto-cleaned and persists on disk indefinitely, so
            treat creating one as a deliberate, space-consuming action, and do
            not invent names. ``session="default"`` opens the SHARED session
            itself — the profile every new session is seeded from and the one a
            human logs in to; it is reserved and is never a session of your own.
            A session is a NAME, not a path: pass ``user_data_dir`` to open a
            directory by path.
            A named session is never deleted by close_instance, by the clone GC,
            by `cleanup --apply` or by `kill-orphans`, and since F-888 its
            BROWSER survives the backend too: a
            backend that stops, restarts, heals or crashes leaves such a browser
            RUNNING, and it is RE-ATTACHED to over CDP rather than replaced, so a
            human's logged-in session is not lost. Two paths reach it and you need
            neither by name: a new backend adopts the browsers it finds recorded
            at its own startup (same instance_id as before), and spawning with a
            session a live browser still holds re-attaches to THAT browser
            instead of walking to a sibling directory. Either way the answer
            carries ``spawn_diagnostics["reattached"]: true`` plus the holder's
            pid, and the page is the one that was already open — not a fresh tab
            on the same cookies. So to recover a logged-in browser whose backend
            died, just spawn with the same session. On that path the
            arguments that describe a LAUNCH cannot apply to a browser already
            running: headless, user_agent, viewport, proxy, browser_args,
            timezone_id and extra_headers are IGNORED rather than refused, and the
            ones you passed are listed in
            ``spawn_diagnostics["ignored_spawn_args"]`` — refusing over a viewport
            would send the spawn to a different directory and lose the login.
            ``block_resources`` IS applied. What the dead backend held and nobody
            can read back off a running browser is named in
            ``spawn_diagnostics["not_restored"]``. The one case that refuses is a
            browser some OTHER LIVE backend still owns (two backends driving one
            Chrome is a defect); you get a normal spawn plus
            ``spawn_diagnostics["reattach_declined"]`` saying so, and the old
            browser is left running and untouched — stop that backend first, see
            RUNBOOK, "Recover a stranded login".
        user_data_dir (Optional[str]): DEPRECATED, and the ONE thing it still
            buys you is an absolute PATH, which ``session`` refuses. For a name
            it resolves to exactly the same profile ``session`` does — it is the
            same argument under the older word, not a second one — so passing
            both with DIFFERENT values is an error rather than a precedence you
            cannot see. Everything said about ``session`` above applies to it.
        sandbox (Optional[Any]): Enable browser sandbox. Accepts bool, string ('true'/'false'), int (1/0), or None for auto-detect.

    Network interception captures request/response metadata by default, but
    response *bodies* are NOT stored unless capture is enabled — via
    set_network_capture_filters(capture_bodies=True) or
    STEALTH_MCP_NETWORK_CAPTURE_BODIES=1 (F-605, off by default). When on, the
    body store is byte-bounded (STEALTH_MCP_NETWORK_BODY_MAX_BYTES per body,
    STEALTH_MCP_NETWORK_BODY_STORE_MAX_BYTES total). get_response_content
    live-refetches a body on demand regardless of this setting.

    Returns:
        Dict[str, Any]: Instance information including instance_id. ``viewport`` is
        the window size Chrome ACTUALLY produced (measured post-launch, F-804), not
        an echo of the request; ``spawn_diagnostics["window_size"]`` carries
        ``requested``/``actual``/``inner_viewport``/``clamped`` so a size the OS
        overrode is visible rather than silent.
    """
    # FIRST of the two pre-flight guards, and outside the try, for three reasons
    # (F-894 review M1 + m9 + its round-3 CI red). `adopt_held_profile` matches
    # the requested directory against live browsers, so an absolute snapshot path
    # with a browser on it — the exact state F-893 is about — was ADOPTED and the
    # resolver, which is where the reservation used to be asked, never saw the
    # request. A caller-input refusal raised inside the try would be re-wrapped by
    # the handler as "Failed to spawn browser: …", which is the wrong label for a
    # request we declined to act on at all. And it sits AHEAD of the F-808 guard
    # below because a refusal about what the CALLER ASKED FOR outranks one about
    # what THIS HOST can do: a reserved path is refused on every machine there is,
    # while "no desktop here" is a fact about this backend, and a caller told the
    # second about a request that fails the first goes looking for a display they
    # do not need. Measured: every headless CI cell answered the F-808 message for
    # a reserved snapshot path, so the reservation was unreachable there. Neither
    # guard has a side effect, so the order decides only which message is sent.
    # The rule has one home; this is the second site that asks it.
    #
    # It is also where the TWO spellings become ONE (F-896): this call reads
    # `session` and `user_data_dir` as a single request and ANSWERS the
    # directory it means, so every line below — the re-attach, the resolver,
    # the diagnostics — sees one value and `session` cannot develop a second
    # path of its own. `session="default"` is the shared profile by the time it
    # reaches the re-attach, which is what lets that re-attach find a browser
    # already open on it.
    user_data_dir = rt.clone_storage.require_allowed_user_data_dir(
        user_data_dir, session
    )

    # Then the HOST-shaped guard, also outside the try so it is not re-wrapped
    # (F-808): a spawn nobody could ever see must not first clone a profile dir
    # onto disk. F-810 demoted it to a FALLBACK: it fires only when delegation is
    # impossible.
    from stealth_chrome_devtools_mcp.embedded import desktop_launch

    if not headless and not desktop_launch.can_deliver_headed_window():
        raise ToolError(
            f"This backend runs in a context that cannot display a window "
            f"({rt.display_context.display_context()}), so a headed browser would launch "
            "invisibly (F-808), and no user is logged on at the desktop for the OS to "
            "launch it there instead (F-810). Start the backend from a desktop session "
            "or pass headless=True; `stealth-chrome-devtools doctor` lists the contexts."
        )

    # Outside the try because the handler READS it: a spawn that fails onto a
    # held directory owes the caller the reason the re-attach was not taken.
    held = rt.browser_reattach.Held()
    try:
        # What the CALLER passed, captured before the resolution below turns an
        # unset `sandbox` into a real bool. `ignored_spawn_args` reports the
        # arguments a caller gave that a running browser cannot be given, and a
        # field whose docstring says "the ones you passed" has to be true of the
        # one argument this handler fills in for them — it named `sandbox` on
        # every single re-attach, which devalues the field for the args that
        # matter (F-888 re-review M-new-3).
        requested_sandbox = sandbox
        sandbox = _resolved_sandbox(sandbox)

        # BEFORE profile selection, because selection is where F-871's walk to
        # <name>-2 happens: a live browser already holding the requested profile
        # is RE-ATTACHED to rather than walked away from (F-888). The browser
        # this exists for has no registry entry at all — its owner backend died
        # and the successor rewrote the record without it — so the directory the
        # caller just named is the only thing that still finds it. Answers an
        # empty `Held` for every other case, including a live sibling backend's
        # browser, and never raises: an adoption that cannot happen costs this
        # spawn nothing but the reason it reports.
        if user_data_dir:
            held = await rt.browser_reattach.adopt_held_profile(
                rt.browser_manager,
                rt.process_cleanup,
                user_data_dir,
                ignored_args=_launch_only_args(
                    headless=headless,
                    user_agent=user_agent,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    proxy=proxy,
                    browser_args=browser_args,
                    timezone_id=timezone_id,
                    extra_headers=extra_headers,
                    sandbox=requested_sandbox,
                ),
            )
            if held.instance_id:
                return await _adopted_instance_record(held.instance_id, block_resources)

        profile_selection = await rt.clone_storage.resolve_profile_selection(
            user_data_dir
        )
        spawn_errors = []

        for spawn_attempt in range(_SPAWN_ATTEMPTS):
            selected_user_data_dir = profile_selection["user_data_dir"]
            options = BrowserOptions(
                headless=headless,
                user_agent=user_agent,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
                proxy=proxy,
                browser_args=browser_args or [],
                timezone_id=timezone_id,
                idle_timeout_seconds=idle_timeout_seconds,
                block_resources=block_resources or [],
                extra_headers=extra_headers or {},
                user_data_dir=selected_user_data_dir,
                sandbox=sandbox,
                auto_clone=(profile_selection.get("profile_role") == "clone"),
            )
            try:
                instance = await rt.browser_manager.spawn_browser(options)
                user_data_dir = selected_user_data_dir
                break
            except Exception as spawn_error:
                spawn_errors.append(f"{type(spawn_error).__name__}: {spawn_error}")
                # This attempt's clone never became a live instance — drop its
                # sweep protection so a failed clone can't stay protected (and thus
                # unreclaimable) for the rest of the process.
                if profile_selection.get("profile_role") == "clone":
                    rt.clone_storage._release_clone_dir(selected_user_data_dir)
                if spawn_attempt == _SPAWN_ATTEMPTS - 1:
                    # The budget is spent and the loop's `else` raises below, so
                    # a re-selection here is one nothing will ever drive: for a
                    # role that clones it copies a whole profile tree and then
                    # leaves it `_protect_clone_dir`-ed for the life of the
                    # process — this handler has already run for it and
                    # `close_instance`, the only other release, never will.
                    continue
                fallback_selection = await rt.clone_storage._fallback_profile_selection(
                    profile_selection, spawn_attempt
                )
                if fallback_selection is None:
                    raise
                profile_selection = fallback_selection
        else:
            raise Exception("; ".join(spawn_errors))

        tab = await rt.browser_manager.get_tab(instance.instance_id)
        if tab:
            await rt.network_interceptor.setup_interception(
                tab, instance.instance_id, block_resources
            )
        spawn_diagnostics = await rt.browser_manager.get_spawn_diagnostics(
            instance.instance_id
        )
        if isinstance(spawn_diagnostics, dict):
            spawn_diagnostics["profile_selection"] = (
                rt.clone_storage._public_profile_selection(profile_selection)
            )
            if held.declined:
                # A live browser held the directory and we spawned anyway: the
                # caller is owed the reason, beside the walk it caused, because
                # the browser they were reaching for is STILL RUNNING and this
                # tool deliberately did not kill it (F-888).
                spawn_diagnostics["reattach_declined"] = held.declined
            if spawn_errors:
                spawn_diagnostics["profile_selection"]["spawn_retries"] = spawn_errors
            if profile_selection.get("profile_role") == "explicit":
                # F-871: when the requested profile was held, the walk to
                # <name>-N is an identity change. It LEADS the field a caller
                # actually reads, rather than sitting quietly beside it in
                # walk_reason — same field set, no second diagnostics home.
                walked = profile_selection.get("walk_reason")
                substitution = (
                    f"NOT the profile you asked for: "
                    f"{profile_selection.get('requested_user_data_dir')} is in use "
                    f"({walked}), so this spawn got "
                    f"{profile_selection.get('walked_to')} — a DIFFERENT profile, "
                    f"either a fresh copy of the default session's seed or one "
                    f"an earlier walk left behind, with none of the cookies or "
                    f"logins the requested one holds. "
                    if walked
                    else ""
                )
                spawn_diagnostics["profile_selection"]["warning"] = substitution + (
                    "Named session created — it is NOT auto-cleaned and persists on disk. "
                    "Only pass session when the user explicitly asks to keep a login; "
                    "otherwise omit it so the profile is copied and auto-deleted."
                )
        return {
            "instance_id": instance.instance_id,
            "state": instance.state,
            "headless": instance.headless,
            "viewport": instance.viewport,
            "spawn_diagnostics": spawn_diagnostics or {},
        }
    except Exception as e:
        # A spawn that failed onto a directory a live browser HOLDS is the one
        # failure where the remedy is not "try again": that browser is still
        # running and still has the login, and why we did not take it over is the
        # only useful thing to say (F-888). Without this the caller gets the bare
        # launch failure and no hint that the thing they asked for exists.
        raise ToolError(
            f"Failed to spawn browser: {e!s}"
            + (
                f" The re-attach was not taken: {held.declined}."
                if held.declined
                else ""
            )
        )


def _launch_only_args(**passed: Any) -> list[str]:
    """The spawn arguments a RUNNING browser cannot be given (F-888).

    Every one of these describes how Chrome is LAUNCHED — its command line, its
    window, the proxy it dials through — and a browser that is already running
    was launched without them. They are REPORTED, never refused: refusing a
    re-attach because the caller also passed a viewport would send the spawn to
    F-871's walk and lose the login the re-attach exists to save.

    Only what differs from the tool's own default is named, because a caller who
    passed nothing asked for nothing. ``block_resources`` is deliberately absent
    — interception IS re-established on the adopted tab — and so are
    ``user_data_dir`` (which is how we found the browser) and
    ``idle_timeout_seconds`` (which the manager applies afterwards, not at
    launch).
    """
    defaults: dict[str, Any] = {
        "headless": False,
        "user_agent": None,
        "viewport_width": 1920,
        "viewport_height": 1080,
        "proxy": None,
        "browser_args": None,
        "timezone_id": None,
        "extra_headers": None,
        "sandbox": None,
    }
    # Compared against the DEFAULT, never tested for truthiness: `sandbox=False`
    # is the one value of that argument a caller would bother to pass, and a
    # truthiness guard dropped exactly it while reporting the resolved `True`
    # nobody asked for. An empty list or dict is "passed nothing" and is the one
    # falsy shape that still reads as unset.
    return sorted(
        name
        for name, value in passed.items()
        if value != defaults.get(name, object()) and value not in ([], {})
    )


def _resolved_sandbox(sandbox: Any | None) -> bool:
    """The caller's ``sandbox`` as the bool the launch needs.

    Unset means "decide for me", and the decision is the one Chrome forces:
    running as root or inside a container, the sandbox cannot be had. Everything
    else is a caller who said something — including the strings an MCP client
    sends for a boolean — and is read literally. Extracted from ``spawn_browser``
    only because the body is at its statement cap; the ladder is unchanged.
    """
    if sandbox is None:
        return not (is_running_as_root() or is_running_in_container())
    if isinstance(sandbox, str):
        return sandbox.lower() in ("true", "1", "yes", "on", "enabled")
    return bool(sandbox)


async def _adopted_instance_record(
    instance_id: str, block_resources: list[str] | None
) -> dict[str, Any]:
    """``spawn_browser``'s answer for a browser it re-attached to (F-888).

    The SAME five keys a spawn returns, because from the caller's side nothing
    else is different: they asked for a browser on that profile and they have
    one. What tells them it was not launched now is
    ``spawn_diagnostics["reattached"]``, set at the adoption site.

    Interception is set up here for the same reason the spawn path does it: an
    adopted tab has none of this backend's handlers on it, so a caller passing
    ``block_resources`` to a spawn that adopted would otherwise be silently
    ignored.
    """
    data = await rt.browser_manager.get_instance(instance_id)
    if not data:
        raise ToolError(
            f"Re-attached instance {instance_id} vanished before it could be reported"
        )
    instance = data["instance"]
    tab = await rt.browser_manager.get_tab(instance_id)
    if tab:
        await rt.network_interceptor.setup_interception(
            tab, instance_id, block_resources
        )
    return {
        "instance_id": instance_id,
        "state": instance.state,
        "headless": instance.headless,
        "viewport": instance.viewport,
        "spawn_diagnostics": await rt.browser_manager.get_spawn_diagnostics(instance_id)
        or {},
    }


async def _live_instance_record(inst: "BrowserInstance") -> dict[str, Any]:
    """One active instance as it IS: the LIVE url and title of its active tab.

    The bound is ONE CDP budget per entry, and it sits on the one call that
    reaches Chrome: ``tab_identity.refreshed``'s ``Target.getTargets`` round
    trip. The two manager lookups in front of it are lock-guarded dict reads
    (``get_active_tab`` is ``get_tab`` under another name; both resolve through
    ``get_instance``), so wrapping them would have claimed a CDP bound over
    something that never speaks CDP and would have charged the entry three
    budgets for one round trip.

    Degraded per entry (F-874). One browser whose devtools websocket has stopped
    answering must cost its OWN row and nothing else — it may not hang the
    listing, and it may not fall back to a cached value under a name that claims
    to be current, which is the defect this whole record exists to close.
    """
    try:
        tab = await rt.browser_manager.get_active_tab(inst.instance_id)
        if tab is None:
            raise ToolError(f"Instance {inst.instance_id} has no active tab.")
        browser = await rt.browser_manager.get_browser(inst.instance_id)
        view = await rt._with_cdp_timeout(
            tab_identity.refreshed(browser, tab), instance_id=inst.instance_id
        )
    except Exception as exc:
        # The caller sees this in `detail_error`; the durable log is what makes a
        # real bug INSIDE tab_identity visible rather than a quiet partial row.
        # Shape only in the message — a url can carry a session token in its
        # query string and this line reaches the log and a Sentry breadcrumb —
        # while `error=` forwards the traceback as `exc_info` (F-869).
        rt.debug_logger.log_warning(
            "browser_management",
            "list_instances",
            f"Live tab read failed for instance {inst.instance_id} "
            f"({type(exc).__name__}); this entry is reported partial.",
            error=exc,
        )
        return {
            "instance_id": inst.instance_id,
            "state": inst.state,
            "source": "active",
            "partial": True,
            "detail_error": f"Could not read the active tab: {type(exc).__name__}: {exc}",
            "last_navigated_url": inst.last_navigated_url,
            "last_navigated_title": inst.last_navigated_title,
        }
    return {
        "instance_id": inst.instance_id,
        "state": inst.state,
        "current_url": view["url"],
        "title": view["title"],
        "source": "active",
        "partial": False,
    }


async def list_instances() -> list[dict[str, Any]]:
    """
    List all active browser instances.

    Returns:
        List[Dict[str, Any]]: One record per instance. An ``active`` record
        carries the LIVE ``current_url``/``title`` of the instance's active tab
        (the same answer ``get_active_tab`` gives) and ``partial: False``; if
        that read failed it carries ``partial: True``, ``detail_error`` and the
        last navigation's values as ``last_navigated_url``/
        ``last_navigated_title`` instead. A ``stored`` record has no live browser
        to read at all, so it only ever carries the ``last_navigated_*`` pair.
    """
    memory_instances = await rt.browser_manager.list_instances()
    storage_instances = rt.in_memory_storage.list_instances()
    # Concurrently: each entry is bounded by ONE CDP budget, for its own
    # Target.getTargets round trip, so N wedged instances served serially would
    # make the caller wait N budgets for one answer.
    result = list(
        await asyncio.gather(*(_live_instance_record(i) for i in memory_instances))
    )
    memory_ids = {inst.instance_id for inst in memory_instances}
    for instance_id, inst_data in storage_instances.get("instances", {}).items():
        if instance_id not in memory_ids:
            result.append(
                {
                    "instance_id": inst_data["instance_id"],
                    "state": inst_data["state"] + " (stored)",
                    "last_navigated_url": inst_data.get("last_navigated_url"),
                    "last_navigated_title": inst_data.get("last_navigated_title"),
                    "source": "stored",
                }
            )
    return result


async def close_instance(instance_id: str) -> bool:
    """
    Close a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        bool: True if closed successfully.
    """
    spawn_diagnostics = await rt.browser_manager.get_spawn_diagnostics(instance_id)
    profile_selection = {}
    if isinstance(spawn_diagnostics, dict):
        profile_selection = spawn_diagnostics.get("profile_selection") or {}
    should_refresh_snapshot = (
        profile_selection.get("profile_role") == rt.profile_seed.DEFAULT_SESSION
    )

    success = await rt.browser_manager.close_instance(instance_id)
    if success:
        await rt.network_interceptor.clear_instance_data(instance_id)
        rt.dynamic_hook_system.remove_instance(instance_id)
        # Instance is gone — lift sweep protection for its disposable clone so the
        # storage cap can reclaim it later if the on-close delete was deferred.
        if profile_selection.get("profile_role") == "clone" and profile_selection.get(
            "user_data_dir"
        ):
            rt.clone_storage._release_clone_dir(profile_selection["user_data_dir"])
        if should_refresh_snapshot:
            await asyncio.to_thread(
                rt.clone_storage._refresh_master_snapshot_if_safe,
                "after-default-close",
            )
    return success


async def get_instance_state(instance_id: str) -> dict[str, Any] | None:
    """
    Get detailed state of a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        Optional[Dict[str, Any]]: Full page state, or a partial record
        (``partial: True``) with ``detail_error`` if collection times out or fails.
    """
    timeout_seconds = get_settings().browser_state_timeout_seconds
    try:
        # F-164 non-CDP: bounds a multi-step page-state aggregation with its own
        # browser_state_timeout_seconds budget; the except paths below return an
        # honest partial record (F-746), not the generic CDP-timeout error that
        # _with_cdp_timeout raises — so this is deliberately not that wrapper.
        state = await asyncio.wait_for(
            rt.browser_manager.get_page_state(instance_id),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        for instance in await rt.browser_manager.list_instances():
            if instance.instance_id == instance_id:
                return {
                    "instance_id": instance.instance_id,
                    "state": instance.state,
                    "last_navigated_url": instance.last_navigated_url,
                    "last_navigated_title": instance.last_navigated_title,
                    "source": "active",
                    "partial": True,
                    "detail_error": f"Timed out after {timeout_seconds:g}s while collecting full page state.",
                }
        return {
            "instance_id": instance_id,
            "state": "unknown",
            "partial": True,
            "detail_error": f"Timed out after {timeout_seconds:g}s while collecting full page state.",
        }
    except Exception as exc:
        for instance in await rt.browser_manager.list_instances():
            if instance.instance_id == instance_id:
                return {
                    "instance_id": instance.instance_id,
                    "state": instance.state,
                    "last_navigated_url": instance.last_navigated_url,
                    "last_navigated_title": instance.last_navigated_title,
                    "source": "active",
                    "partial": True,
                    "detail_error": f"Failed to collect full page state: {type(exc).__name__}: {exc}",
                }
        return {
            "instance_id": instance_id,
            "state": "unknown",
            "partial": True,
            "detail_error": f"Failed to collect full page state: {type(exc).__name__}: {exc}",
        }
    if state:
        result = state.dict()
        result["partial"] = False
        return result
    return None


async def navigate(
    instance_id: str,
    url: str,
    wait_until: str = "load",
    timeout: int = 30000,
    referrer: str | None = None,
) -> dict[str, Any]:
    """
    Navigate to a URL.

    Args:
        instance_id (str): Browser instance ID.
        url (str): URL to navigate to.
        wait_until (str): Wait condition - 'load', 'domcontentloaded', or 'networkidle'.
        timeout (int): Navigation timeout in ms (default 30000, max 60000). Most pages load in under 10s — only increase if you have evidence the page is slow. Values above 60000 are capped.
        referrer (Optional[str]): Referrer URL.

    Returns:
        Dict[str, Any]: Navigation result with final URL and title.

    Raises:
        ToolError: the navigation failed at the browser — Chrome committed an
            error page (unresolvable host, refused connection, TLS failure).
            An HTTP error status (404/500) is a loaded page, not a failure.
    """
    timeout = rt._clamp_timeout(timeout, default=30_000)
    outer_timeout = max(timeout / 1000 + 5, rt.CDP_OPERATION_TIMEOUT)
    result = await rt._with_cdp_timeout(
        rt.browser_manager.navigate(
            instance_id=instance_id,
            url=url,
            wait_until=wait_until,
            timeout=timeout,
            referrer=referrer,
        ),
        timeout=outer_timeout,
        instance_id=instance_id,
    )
    # Bookkeeping completed above, so raising here cannot leave the tab or the
    # state table behind (F-802).
    return await _require_landing_ok(result, url, rt.CDP_OPERATION_TIMEOUT)


async def go_back(instance_id: str) -> bool:
    """
    Navigate back in history.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        bool: True. Landing on a Chrome error page raises instead (F-833).
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    await rt._with_cdp_timeout(tab.back(), instance_id=instance_id)
    return await _require_landing_ok(tab, "the previous page", rt.CDP_OPERATION_TIMEOUT)


async def go_forward(instance_id: str) -> bool:
    """
    Navigate forward in history.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        bool: True. Landing on a Chrome error page raises instead (F-833).
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    await rt._with_cdp_timeout(tab.forward(), instance_id=instance_id)
    return await _require_landing_ok(tab, "the next page", rt.CDP_OPERATION_TIMEOUT)


async def reload_page(instance_id: str, ignore_cache: bool = False) -> bool:
    """
    Reload the current page.

    Args:
        instance_id (str): Browser instance ID.
        ignore_cache (bool): Whether to ignore cache when reloading.

    Returns:
        bool: True. Landing on a Chrome error page raises instead (F-833).
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    await rt._with_cdp_timeout(tab.reload(), instance_id=instance_id)
    return await _require_landing_ok(tab, "the reloaded page", rt.CDP_OPERATION_TIMEOUT)


#: Surface order, which is the order ``server.py``'s binding loop registers them
#: in and therefore the order they appear in ``SECTION_TOOLS["browser-management"]``.
TOOLS = (
    spawn_browser,
    list_instances,
    close_instance,
    get_instance_state,
    navigate,
    go_back,
    go_forward,
    reload_page,
)
