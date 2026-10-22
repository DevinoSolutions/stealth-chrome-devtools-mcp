"""The tools/call helpers and the foundation, warm-up, cookie and canonical journeys.

Split out of :mod:`release_gate_harness` (T-6148); that module re-exports every name
here, so callers keep importing from ``release_gate_harness``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.metadata
import inspect
import json
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import fixture_routes
from release_gate_harness_consts import (
    CALL_TIMEOUT,
    CLOSE_TIMEOUT,
    LIST_TIMEOUT,
    NAV_TIMEOUT,
    REGISTRY_TOOL_COUNT,
    SERVER_NAME,
    SETTLE_TIMEOUT,
    SPAWN_TIMEOUT,
    WARMUP_ATTEMPTS,
    WARMUP_BACKOFF_SECONDS,
    WARMUP_TIMEOUT,
)
from release_gate_harness_env import _chrome_process_snapshot

if TYPE_CHECKING:
    from fastmcp import Client


# ---------------------------------------------------------------------------
# tools/call helpers.
# ---------------------------------------------------------------------------
async def _call(
    client: Client,
    name: str,
    args: dict[str, Any],
    timeout: float,
    *,
    raise_on_error: bool = True,
    allow_fail: bool = False,
) -> Any:
    """Call a tool through the real protocol, bounded, returning the plain
    wire-shape result (raw ``structuredContent`` JSON, not fastmcp's pydantic
    ``Root`` reconstruction — the release gate asserts what a user's client
    actually receives)."""
    coro = client.call_tool(name, args, raise_on_error=raise_on_error)
    try:
        result = await asyncio.wait_for(coro, timeout)
    except Exception:
        if allow_fail:
            return None
        raise
    return _wire_shape(result)


def _wire_shape(result: Any) -> Any:
    """The plain wire-shape payload of one ``CallToolResult``.

    Factored out of :func:`_call` so the soak's own bounded caller returns the
    SAME shape without a second unwrapping rule.
    """
    structured = result.structured_content
    if structured is not None:
        # FastMCP wraps non-object returns as {"result": X} on the wire.
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    return result.data


async def _eval(client: Client, iid: str, expression: str) -> Any:
    """Evaluate JS via ``execute_script`` and return its result (assert success)."""
    result = await _call(
        client,
        "execute_script",
        {"instance_id": iid, "script": expression},
        CALL_TIMEOUT,
    )
    assert isinstance(result, dict) and result.get("success") is True, result
    return result.get("result")


async def _settle_dom(client: Client, iid: str, expect_app: bool = False) -> None:
    """Bounded-poll ``query_elements('body')`` until the document is queryable.

    After navigation nodriver's cached document node is transiently stale, so the
    first DOM-node-path call can fail; ``query_elements`` is the safe probe (it
    returns ``[]`` rather than raising). Mirrors ``navigate_and_settle`` in
    ``e2e_helpers`` but over the real transport. ``expect_app`` is for a caller that
    navigated to a page loading ``app.js`` (``interact.html``).
    """
    deadline = time.monotonic() + SETTLE_TIMEOUT
    while True:
        body = await _call(
            client,
            "query_elements",
            {"instance_id": iid, "selector": "body"},
            CALL_TIMEOUT,
            raise_on_error=False,
            allow_fail=True,
        )
        if (isinstance(body, list) and body) or time.monotonic() >= deadline:
            break  # best-effort; the ready wait below fails loudly if it matters
        await asyncio.sleep(0.25)

    async def probe(expression: str) -> Any:
        result = await _call(
            client,
            "execute_script",
            {"instance_id": iid, "script": expression},
            CALL_TIMEOUT,
            raise_on_error=False,
            allow_fail=True,
        )
        return result.get("result") if isinstance(result, dict) else None

    # A queryable body is not a wired page: app.js (end of <body>) may not have
    # attached its listeners yet, and an interaction before that is silently lost.
    await fixture_routes.wait_fixture_ready(probe, expect_app)


def _decode_screenshot(shot: Any) -> bytes:
    if isinstance(shot, str):
        return base64.b64decode(shot)
    if isinstance(shot, dict) and shot.get("file_path"):
        return Path(shot["file_path"]).read_bytes()
    raise AssertionError(f"unexpected take_screenshot result: {type(shot).__name__}")


# ---------------------------------------------------------------------------
# Foundation proof + schema/handshake + canonical journey.
# ---------------------------------------------------------------------------
async def _foundation_proof(client: Client, record: dict[str, Any]) -> None:
    """Protocol + server identity + 94-tool registry + a real tool call."""
    init = client.initialize_result
    assert init is not None, "no initialize result (handshake did not complete)"
    server_info = init.serverInfo
    assert server_info.name == SERVER_NAME, server_info.name
    installed = importlib.metadata.version(SERVER_NAME)
    assert server_info.version == installed, (server_info.version, installed)
    assert init.protocolVersion, "empty protocolVersion"
    record["server"] = {
        "name": server_info.name,
        "version": server_info.version,
        "protocol_version": init.protocolVersion,
    }

    tools = await asyncio.wait_for(client.list_tools(), LIST_TIMEOUT)
    names = [t.name for t in tools]
    assert len(names) == REGISTRY_TOOL_COUNT, (
        f"{len(names)} tools != {REGISTRY_TOOL_COUNT}"
    )
    assert len(set(names)) == len(names), "duplicate tool names in tools/list"
    for tool in tools:
        schema = tool.inputSchema
        assert isinstance(schema, dict) and schema.get("type") == "object", tool.name
    record["tool_count"] = len(names)

    instances = await _call(client, "list_instances", {}, CALL_TIMEOUT)
    assert isinstance(instances, list), instances
    record["list_instances_initial_count"] = len(instances)


async def _representative_parity(client: Client, record: dict[str, Any]) -> None:
    """One tools/call result is shape-equivalent to the in-process ``.fn`` seam.

    Uses ``list_cdp_commands`` (deterministic, browser-free): the protocol result
    and the ``get_fn`` seam (``e2e_helpers``) must carry the same set of names,
    without requiring byte-identical incidental formatting.
    """
    try:
        from e2e_helpers import get_fn
    except Exception:  # pragma: no cover - only when the src seam is unavailable
        record["representative_parity"] = {
            "tool": "list_cdp_commands",
            "seam_available": False,
        }
        return
    seam = get_fn("list_cdp_commands")()
    if inspect.isawaitable(seam):
        seam = await seam
    proto = await _call(client, "list_cdp_commands", {}, CALL_TIMEOUT)
    assert isinstance(proto, list) and isinstance(seam, list), (type(proto), type(seam))
    assert {str(x) for x in proto} == {str(x) for x in seam}, (
        "protocol vs seam mismatch"
    )
    record["representative_parity"] = {
        "tool": "list_cdp_commands",
        "protocol_len": len(proto),
        "seam_len": len(seam),
        "equal": True,
        "seam_available": True,
    }


def _headless_spawn_kwargs(**extra: Any) -> dict[str, Any]:
    """``{'headless': True}`` plus this environment's sandbox policy.

    No ``--use-mock-keychain`` here, deliberately: an earlier round passed it to
    rule out macOS keychain blocking, and the backend log showed the product's
    own stealth filter removing it ("Stripped 1 detectable arg(s):
    --use-mock-keychain stripped: Playwright default"). It never reached Chrome,
    so it tested nothing — keeping it would be an inert flag that also trips a
    stealth warning on every spawn.
    """
    kwargs: dict[str, Any] = {"headless": True, **extra}
    with contextlib.suppress(Exception):
        from e2e_helpers import sandbox_kwargs

        kwargs.update(sandbox_kwargs())
    return kwargs


async def _cold_start_warmup(
    client: Client, base_url: str, log_dir: Path, record: dict[str, Any]
) -> None:
    """Absorb the first-Chrome-launch cost BEFORE the measured journey.

    The backend's first spawn into a fresh browser-session root pays for both a
    Chrome cold start and the master-profile bootstrap that clone-on-spawn
    needs. On the macOS CI image that cold path is slow enough to exceed
    nodriver's internal connect patience (spawn: "Failed to connect to
    browser") or the product's CDP deadline on the first navigate — while the
    54 in-process integration tests pass on the same image, every one of them
    behind :func:`e2e_helpers.warmup_once` ("the first Chrome launch on CI is
    slow / flaky"). This is that same warmup at the wire level, for the same
    reason and with the same best-effort contract — not a second warmup
    convention: the journey drives a SEPARATE backend process, so it cannot
    share the in-process one.

    It warms BOTH cold paths, because they are separately expensive: the launch
    (Chrome binary + master-profile bootstrap) and the first page LOAD (network
    service, renderer, font cache). A spawn-only warmup left the journey's first
    navigate still cold — which is exactly where macOS kept failing.

    Bounded retry, because the cold launch is not merely slow but intermittently
    FAILS ("Failed to connect to browser" — seen on the Linux runner defeating
    even the in-process warmup). Retrying warmup costs nothing and asserts
    nothing.

    Best-effort by construction: the outcome is recorded and never fatal. It
    proves nothing and is asserted on by nobody — the canonical journey below
    remains the sole evidence and is still asserted in full.
    """
    warmup: dict[str, Any] = {"attempted": True, "ok": False, "attempts": 0}
    record["cold_start_warmup"] = warmup
    for attempt in range(1, WARMUP_ATTEMPTS + 1):
        warmup["attempts"] = attempt
        if attempt > 1:
            # BACK OFF before retrying. "Failed to connect to browser" is a fast
            # connect failure, not a timeout — a bigger WARMUP_TIMEOUT cannot fix
            # it, and an immediate retry meets exactly the resource contention
            # that just failed. Observed defeating both attempts on Linux/X64
            # runners simultaneously across two PRs, taking `transport` and
            # `install-smoke (wheel)` down with it. Same shape as the backoff
            # e2e_helpers.warmup_once already uses for the in-process warmup.
            await asyncio.sleep(WARMUP_BACKOFF_SECONDS * attempt)
        iid: str | None = None
        try:
            # Chrome's OWN log, into log_dir so the failure dump picks it up with
            # the backend logs. Neither flag is in the product's stealth-arg
            # blocklist, so unlike --use-mock-keychain these actually reach
            # Chrome. Warmup instance only: the canonical journey below stays a
            # clean default spawn.
            spawn = await _call(
                client,
                "spawn_browser",
                _headless_spawn_kwargs(
                    user_data_dir="release-gate-warmup",
                    browser_args=[
                        "--enable-logging",
                        f"--log-file={log_dir / 'chrome-warmup.log'}",
                    ],
                ),
                WARMUP_TIMEOUT,
            )
            iid = spawn.get("instance_id") if isinstance(spawn, dict) else None
            if not iid:
                continue
            # Diagnostic probe (recorded, never asserted): "about:blank" needs no
            # NETWORK request, so Chrome's Fetch interception — which the product
            # enables catch-all on every spawn even with zero hooks — has nothing
            # to pause. A real URL does. If blank succeeds where http hangs, the
            # stall is the paused-request path, not the tab, the CDP session, or
            # reachability. Cheap, and it turns the next red into an answer.
            # "refused" targets a port nothing listens on: the OS answers with
            # an immediate ECONNREFUSED, so Chrome renders an error page fast.
            # It separates the two remaining causes, which the fixture URL alone
            # cannot: if a REFUSED connection also hangs, the request never
            # reached the network stack at all (the paused-Fetch path — a
            # product bug); if it errors fast while the fixture port hangs, the
            # network stack is fine and something is dropping that connection
            # (a runner/environment problem).
            for label, url in (
                ("about_blank", "about:blank"),
                ("refused", "http://127.0.0.1:1/"),
                ("http", f"{base_url}/index.html"),
            ):
                started = time.monotonic()
                try:
                    await _call(
                        client,
                        "navigate",
                        {"instance_id": iid, "url": url},
                        NAV_TIMEOUT,
                    )
                    outcome = "ok"
                except BaseException as exc:  # noqa: BLE001  PERMANENT(probe records the failure shape; the journey below is what actually gates)
                    outcome = f"{type(exc).__name__}: {exc}"
                warmup.setdefault("nav_probe", {})[label] = {
                    "outcome": outcome,
                    "seconds": round(time.monotonic() - started, 1),
                }
                if outcome != "ok" and "chrome_processes" not in warmup:
                    # Snapshot WHILE the navigation is stalled and Chrome is
                    # still alive — after teardown the evidence is gone.
                    warmup["chrome_processes"] = _chrome_process_snapshot()
            warmup["ok"] = True
            warmup.pop("error", None)
            return
        except BaseException as exc:  # noqa: BLE001  PERMANENT(warmup is best-effort: record the reason, never fail the gate on it)
            warmup["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if iid:
                with contextlib.suppress(Exception):
                    await _call(
                        client, "close_instance", {"instance_id": iid}, CLOSE_TIMEOUT
                    )


async def _cookie_round_trip(
    client: Client, iid: str, page_url: str, journey: dict[str, Any]
) -> None:
    """plan_RELEASE W5 — the real success path for ``get_cookies``.

    W5's ``get_cookies`` hard block (§2.5) requires a real Chrome + real
    transport test that **sets a cookie, retrieves it, and asserts its value**;
    a schema check, a mock, a missing-instance error, or a characterization
    explicitly cannot satisfy it. This does the full round trip through
    ``tools/call`` only:

    ``set_cookie`` → ``get_cookies(urls=[page_url])`` → **assert the value** →
    ``get_cookies()`` (the no-argument default, a different CDP call) →
    ``document.cookie`` cross-check → ``clear_cookies`` → assert it is gone.

    The value is unique per run, so a stale cookie from an earlier run in a
    reused profile cannot forge the assertion, and the ``document.cookie``
    cross-check proves the cookie really reached the browser rather than
    ``get_cookies`` merely echoing what ``set_cookie`` was handed.

    ``get_cookies`` is declared ``-> list[dict[str, Any]]`` but returns nodriver
    ``cdp.network.Cookie`` **dataclasses**. pydantic serializes those to correct
    JSON objects, so ``structuredContent`` — what ``_call`` returns, and what a
    user's client receives — is a list of cookie dicts. Asserting through
    fastmcp's ``result.data`` instead would see an opaque ``[Root()]``; that is
    a property of the reconstruction, not a product defect.
    """
    name = "release_gate_cookie"
    value = f"w5-{uuid.uuid4().hex}"
    cookies: dict[str, Any] = {"name": name, "value": value}
    journey["cookies"] = cookies

    assert await _call(
        client,
        "set_cookie",
        {"instance_id": iid, "name": name, "value": value, "url": page_url},
        CALL_TIMEOUT,
    ), "set_cookie did not report success"

    # Retrieval #1 — scoped to the page URL (CDP Network.getCookies).
    scoped = await _call(
        client, "get_cookies", {"instance_id": iid, "urls": [page_url]}, CALL_TIMEOUT
    )
    assert isinstance(scoped, list) and scoped, (
        f"get_cookies(urls=…) returned {scoped!r}"
    )
    assert all(isinstance(c, dict) for c in scoped), (
        f"get_cookies did not serialize to dicts: {[type(c).__name__ for c in scoped]}"
    )
    scoped_hit = next((c for c in scoped if c.get("name") == name), None)
    assert scoped_hit is not None, f"{name!r} absent from get_cookies(urls=…): {scoped}"
    # THE assertion W5's hard block turns on.
    assert scoped_hit.get("value") == value, (scoped_hit.get("value"), value)
    cookies["scoped_value"] = scoped_hit["value"]
    cookies["scoped_count"] = len(scoped)
    cookies["field_names"] = sorted(scoped_hit)

    # Retrieval #2 — the no-argument default (CDP Network.getAllCookies).
    every = await _call(client, "get_cookies", {"instance_id": iid}, CALL_TIMEOUT)
    assert isinstance(every, list), f"get_cookies() returned {every!r}"
    all_hit = next(
        (c for c in every if isinstance(c, dict) and c.get("name") == name), None
    )
    assert all_hit is not None, f"{name!r} absent from get_cookies(): {every}"
    assert all_hit.get("value") == value, (all_hit.get("value"), value)
    cookies["all_value"] = all_hit["value"]

    # Ground truth: the cookie is really in the browser, not just in our reply.
    document_cookie = await _eval(client, iid, "document.cookie")
    assert f"{name}={value}" in str(document_cookie), document_cookie
    cookies["document_cookie_confirms"] = True

    # clear_cookies is part of the same real path — prove removal by re-reading,
    # not by trusting its return value.
    assert await _call(
        client, "clear_cookies", {"instance_id": iid, "url": page_url}, CALL_TIMEOUT
    ), "clear_cookies did not report success"
    after = await _call(
        client, "get_cookies", {"instance_id": iid, "urls": [page_url]}, CALL_TIMEOUT
    )
    assert isinstance(after, list), after
    assert not any(isinstance(c, dict) and c.get("name") == name for c in after), (
        f"{name!r} survived clear_cookies: {after}"
    )
    cookies["cleared"] = True


async def _cookie_journey(
    client: Client, base_url: str, record: dict[str, Any]
) -> None:
    """W5's segment: spawn → navigate → cookie round trip → close.

    Deliberately minimal — everything here exists to make the cookie assertions
    meaningful, so a failure reads as a cookie failure rather than as "the
    journey" failing. A real page (not ``about:blank``) is required because
    cookies need a real http:// origin, and the fixture app is that origin.
    """
    spawn = await _call(
        client, "spawn_browser", _headless_spawn_kwargs(), SPAWN_TIMEOUT
    )
    assert isinstance(spawn, dict) and spawn.get("instance_id"), spawn
    iid = spawn["instance_id"]
    journey: dict[str, Any] = {"instance_id": iid}
    record["journey"] = journey
    try:
        url = f"{base_url}/interact.html"
        await _call(client, "navigate", {"instance_id": iid, "url": url}, NAV_TIMEOUT)
        journey["navigated_url"] = url
        await _settle_dom(client, iid, expect_app=True)

        await _cookie_round_trip(client, iid, url, journey)
    finally:
        await _call(
            client,
            "close_instance",
            {"instance_id": iid},
            CLOSE_TIMEOUT,
            raise_on_error=False,
            allow_fail=True,
        )


async def _canonical_journey(
    client: Client, base_url: str, record: dict[str, Any]
) -> None:
    """spawn → navigate → tabs → interact → assert oracle+ground truth →
    structural extraction → PNG screenshot → close, all via ``tools/call``."""
    spawn = await _call(
        client, "spawn_browser", _headless_spawn_kwargs(), SPAWN_TIMEOUT
    )
    assert isinstance(spawn, dict) and spawn.get("instance_id"), spawn
    iid = spawn["instance_id"]
    journey: dict[str, Any] = {"instance_id": iid, "spawn_state": spawn.get("state")}
    record["journey"] = journey
    try:
        url = f"{base_url}/interact.html"
        await _call(client, "navigate", {"instance_id": iid, "url": url}, NAV_TIMEOUT)
        journey["navigated_url"] = url
        await _settle_dom(client, iid, expect_app=True)

        tabs = await _call(client, "list_tabs", {"instance_id": iid}, CALL_TIMEOUT)
        assert isinstance(tabs, list) and tabs and all("tab_id" in t for t in tabs), (
            tabs
        )
        journey["tabs_count"] = len(tabs)
        active = await _call(
            client, "get_active_tab", {"instance_id": iid}, CALL_TIMEOUT
        )
        assert isinstance(active, dict) and active.get("tab_id"), active

        assert await _call(
            client,
            "click_element",
            {"instance_id": iid, "selector": "#btn-counter"},
            CALL_TIMEOUT,
        )
        assert await _call(
            client,
            "type_text",
            {"instance_id": iid, "selector": "#text-input", "text": "hello"},
            CALL_TIMEOUT,
        )
        assert await _call(
            client,
            "select_option",
            {"instance_id": iid, "selector": "#select-single", "value": "beta"},
            CALL_TIMEOUT,
        )

        # Fixture action oracle (proof the RIGHT action fired) via execute_script.
        actions = json.loads(
            await _eval(client, iid, "JSON.stringify(window.__actions)")
        )
        assert "click:btn-counter" in actions, actions
        assert "change:select-single:beta" in actions, actions
        journey["actions"] = actions

        # Live-DOM ground truth.
        counter = await _eval(
            client, iid, "document.getElementById('counter-value').textContent"
        )
        assert counter == "1", counter
        journey["counter_value"] = counter
        text_value = await _eval(
            client, iid, "document.getElementById('text-input').value"
        )
        assert text_value == "hello", text_value
        journey["text_input_value"] = text_value
        select_value = await _eval(
            client, iid, "document.getElementById('select-single').value"
        )
        assert select_value == "beta", select_value
        journey["select_value"] = select_value
        title = await _eval(client, iid, "document.title")
        assert title == "fixture-interact-page", title
        journey["title"] = title

        content = await _call(
            client, "get_page_content", {"instance_id": iid}, CALL_TIMEOUT
        )
        assert "fixture-interact-page" in json.dumps(content, default=str)

        # One structural extraction.
        structure = await _call(
            client,
            "extract_element_structure",
            {"instance_id": iid, "selector": "#select-single"},
            CALL_TIMEOUT,
        )
        assert isinstance(structure, dict), structure
        journey["structure_tag"] = str(
            structure.get("tag_name") or structure.get("tagName") or ""
        ).lower()

        # PNG-magic screenshot (format='png' re-encodes through PIL to PNG).
        shot = await _call(
            client,
            "take_screenshot",
            {"instance_id": iid, "format": "png"},
            CALL_TIMEOUT,
        )
        png = _decode_screenshot(shot)
        assert png[:8] == b"\x89PNG\r\n\x1a\n", png[:8]
        journey["screenshot_bytes"] = len(png)
        journey["screenshot_format"] = "png"
    finally:
        await _call(
            client,
            "close_instance",
            {"instance_id": iid},
            CLOSE_TIMEOUT,
            raise_on_error=False,
            allow_fail=True,
        )
