"""plan_RELEASE W16 (MQ-155…162) — stateful/PWA and internationalized shapes.

W7 covered how a page is *structured*; W10 covered how it *breaks*. This module
covers the two things a modern site does that neither touches: it keeps state
that outlives the document (workers, service workers, CacheStorage, IndexedDB,
storage, cookies), and it carries text that is not ASCII.

Four disciplines make a node here evidence rather than a demo.

*Every oracle is computed twice.* The page or worker computes a value in
JavaScript; ``fixture_routes`` computes the same value in Python; the node
asserts they agree. FNV-1a/32 over UTF-16 code units is the shared transform
because it reproduces exactly in both languages. A fixture that reported only
what it had just stored would pass whatever it produced.

*Worker lifecycle is observed from the SERVER.* A shared worker's last client
is, by definition, gone by the time the worker has no clients — so its
zero-client teardown is reported to the fixture server and read back from the
ledger. The same holds for service-worker install/activate. A page-side
assertion could not see any of it.

*Offline is proved by absence, not by a flag.* The cached read is credited only
when the fixture ledger shows the network was never touched, and the offline
read is taken after the fixture server has actually been shut down — not after
an emulated-offline toggle. W10 already established that
``Network.emulateNetworkConditions`` wedges the connection it is issued on
(F-788); nothing here re-tries it.

*Text is compared as code points.* Never as rendered pixels, never as a locale
decision, and never with normalization applied on the way in or out: the NFC
and NFD strings are canonically equivalent and deliberately NOT equal, so a
round trip that "helpfully" normalized would be caught rather than excused.

What the shapes actually found. Seven of the eight steps hold. One does not:
``reload_page`` reloads a page **out from under its own service worker** — the
reloaded document is uncontrolled, so a PWA that works on first load behaves as
if it had no service worker after a reload (F-800). Root cause is in `src/`:
the tool accepts an ``ignore_cache`` argument and never passes it, so nodriver's
``ignore_cache=True`` default makes every reload a hard reload. `src/` edits are
a plan_RELEASE non-goal (§1.2), so it is characterization-pinned and routed,
and MQ-157 is `planned` because "controlled reload" is one of its named halves.

MQ binding. Only the ids whose whole contract holds are bound:

===========  ================================================================
MQ           node
===========  ================================================================
``MQ-155``   ``test_dedicated_worker_answers_in_order_then_closes``
``MQ-156``   ``test_shared_worker_is_shared_across_tabs_then_reports_zero``
             ``+ test_a_separate_profile_gets_its_own_shared_worker_state``
``MQ-158``   ``test_cache_bytes_and_offline_reads_match_the_byte_oracle``
``MQ-159``   ``test_indexed_db_index_and_transaction_results_match_the_oracle``
``MQ-160``   ``test_storage_and_cookies_survive_one_profile_and_no_other``
``MQ-161``   ``test_unicode_round_trips_through_text_attributes_and_inputs``
``MQ-162``   ``test_the_synthesized_composition_sequence_is_exact``
===========  ================================================================

The ids are bound to runtime evidence by the ``--mq`` flags on the
``integration`` cell in ``.github/workflows/release-gate.yml``. MQ-157 is
deliberately absent from that list.

Scope honesty, stated once so no node has to imply it:

* These nodes drive the in-process ``.fn`` seam against real Chrome, which is
  what puts them in the ``integration`` lane. The stdio wire path is W1's
  separate claim.
* Nothing here enumerates browser *targets*. ``execute_cdp_command`` is a raw
  **Runtime**-domain escape hatch by design and documentation, so "no worker
  remains" is asserted as the observable contract — no later message is
  delivered and the handle is terminated — and NOT as a claim about renderer
  threads or worker targets.
* Cookies are read through ``document.cookie``, not through ``get_cookies``:
  that tool does not settle on this seam (F-777), and W1's
  ``tests/test_e2e_transport_cookies.py`` owns its real-transport claim.
* Native OS IME candidate/selection UI is not automatable on a headless runner
  and is NOT tested here. MQ-162 covers the DOM composition sequence the tool
  can synthesize, and says so; a synthetic ``CompositionEvent`` is never
  evidence about a real IME.
* ``type_text``'s missing ``keydown``/``keyup`` half is already pinned by
  ``tests/test_e2e_interaction_fidelity.py::test_keyboard_fidelity_and_enter_submit``.
  MQ-162 cites that pin instead of re-measuring it.

Profile discipline. Every node spawns with an explicit throwaway
``user_data_dir`` and deletes it afterwards. No node ever runs against the
ambient/default profile: a service worker, a cache and an IndexedDB database
all persist in whatever profile they land in, and a test that seeded them into
a real user's profile would leave them there.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import time
import uuid
from pathlib import Path

import pytest
import requests

import fixture_routes as fr
from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    sandbox_kwargs,
    warmup_once,
)
from release_gate_harness import serve_fixture_app
from stealth_chrome_devtools_mcp.embedded import clone_storage

pytestmark = integration_pytestmark()

HTTP_TIMEOUT = 10
SETTLE = 25.0  # how long an async page op (cache/IDB/SW) may take to land
POLL = 0.15
PROFILE_REMOVAL_TIMEOUT = 20.0  # Windows may hold a just-closed profile briefly
PROFILE_RELEASE_TIMEOUT = 30.0  # how long Chrome may keep its singleton lock


# ── Fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


class _Instances:
    """Spawns throwaway-profile instances and guarantees their removal.

    A named profile is the whole point — MQ-160 asks what survives a browser
    restart, which only a named profile can answer — but a named profile is
    also NOT auto-cleaned by the product, so every one this module creates is
    tracked here and deleted at teardown even if the node fails.
    """

    def __init__(self):
        self.instance_ids: list[str] = []
        self.profile_dirs: dict[str, str] = {}

    async def spawn(self, profile: str) -> str:
        result = await get_fn("spawn_browser")(
            headless=True, user_data_dir=profile, **sandbox_kwargs()
        )
        instance_id = result["instance_id"]
        self.instance_ids.append(instance_id)
        directory = (result.get("spawn_diagnostics") or {}).get("user_data_dir")
        assert directory, f"spawn reported no profile directory: {result}"
        self.profile_dirs[profile] = directory
        return instance_id

    async def close(self, instance_id: str) -> bool:
        closed = await get_fn("close_instance")(instance_id=instance_id)
        with contextlib.suppress(ValueError):
            self.instance_ids.remove(instance_id)
        return closed

    async def await_profile_free(self, profile: str) -> bool:
        """Block until the product itself considers *profile* not in use.

        This is a correctness barrier, not politeness. ``spawn_browser`` on a
        named profile that is still held resolves to a NUMBERED VARIANT
        (``…-2``) freshly cloned from master rather than failing — which is a
        reasonable product behaviour and a silent disaster for a restart test:
        the respawn would read an empty profile and the node would report that
        nothing persisted. Waiting on the product's own predicate is the only
        barrier that means the same thing the product's decision means; a sleep
        would just be a guess with better odds.
        """
        directory = Path(self.profile_dirs[profile])
        deadline = time.monotonic() + PROFILE_RELEASE_TIMEOUT
        while (
            clone_storage._profile_has_running_browser(directory)
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.25)
        return not clone_storage._profile_has_running_browser(directory)

    async def remove_profile(self, profile: str) -> bool:
        """Delete one profile tree, tolerating a Windows lock that outlives the
        process for a moment. Returns whether the tree is really gone."""
        directory = Path(self.profile_dirs[profile])
        deadline = time.monotonic() + PROFILE_REMOVAL_TIMEOUT
        while directory.exists() and time.monotonic() < deadline:
            shutil.rmtree(directory, ignore_errors=True)
            if directory.exists():
                await asyncio.sleep(0.5)
        return not directory.exists()


@pytest.fixture()
async def browsers():
    """The instance/profile ledger. The net, not the assertion: a node that
    cares about clean teardown asserts it itself."""
    manager = _Instances()
    try:
        yield manager
    finally:
        for instance_id in list(manager.instance_ids):
            with contextlib.suppress(Exception):
                await get_fn("close_instance")(instance_id=instance_id)
        for profile in manager.profile_dirs:
            with contextlib.suppress(Exception):
                await manager.await_profile_free(profile)
                await manager.remove_profile(profile)


def _profile(name: str) -> str:
    """A profile name unique to this node AND run, so two nodes (or two runs on
    one machine) can never collide on a persistent named profile."""
    return f"w16-{name}-{uuid.uuid4().hex[:8]}"


# ── Page-side polling (the product evaluates a SYNCHRONOUS expression) ──────
async def _settled(instance_id: str, expression: str, timeout: float = SETTLE):
    """Poll *expression* until it stops reporting "not finished yet".

    ``execute_script`` evaluates a synchronous expression, so a page cannot
    hand back an awaited value. Every async fixture op therefore parks its
    result in a global that starts as ``null``, and this is the one bounded
    poll that waits for it. On timeout the last value is returned so the
    caller's assertion shows what was actually there.
    """
    deadline = time.monotonic() + timeout
    value = await eval_js(instance_id, expression)
    while value in (None, "null") and time.monotonic() < deadline:
        await asyncio.sleep(POLL)
        value = await eval_js(instance_id, expression)
    return value


async def _settled_json(instance_id: str, expression: str, timeout: float = SETTLE):
    raw = await _settled(instance_id, expression, timeout)
    assert isinstance(raw, str) and raw not in ("", "null"), (expression, raw)
    return json.loads(raw)


async def _points(instance_id: str, expression: str) -> list[int]:
    """The exact code points of a page-side string — the one i18n currency."""
    return json.loads(await eval_js(instance_id, f"window.w16Points({expression})"))


async def _tabs_at(instance_id: str, suffix: str) -> list[dict]:
    tabs = await get_fn("list_tabs")(instance_id=instance_id)
    return [tab for tab in tabs if tab["url"].endswith(suffix)]


async def _ledger(base_url: str) -> dict:
    response = await asyncio.to_thread(
        requests.get, f"{base_url}/e2e/ledger", timeout=HTTP_TIMEOUT
    )
    return response.json()


async def _reset_ledger(base_url: str) -> None:
    await asyncio.to_thread(requests.get, f"{base_url}/e2e/reset", timeout=HTTP_TIMEOUT)


async def _await_ledger(base_url: str, key: str, count: int, timeout: float = SETTLE):
    """Wait until the SERVER has recorded *count* entries under *key*.

    The worker milestones arrive out of band — the browser is not asked, the
    fixture is told — so this is the only synchronization point that can
    observe them without asking the page to vouch for itself.
    """
    deadline = time.monotonic() + timeout
    entries = (await _ledger(base_url)).get(key, [])
    while len(entries) < count and time.monotonic() < deadline:
        await asyncio.sleep(POLL)
        entries = (await _ledger(base_url)).get(key, [])
    return entries


async def _register_service_worker(instance_id: str) -> dict:
    await eval_js(instance_id, "window.w16Register()")
    report = await _settled_json(
        instance_id,
        "(() => {const r = JSON.parse(window.w16Report()); "
        "return (r.state === 'ready' || r.state === 'error') "
        "? window.w16Report() : null;})()",
        timeout=45.0,
    )
    assert report["state"] == "ready", report
    return report


# ── MQ-155: the dedicated worker ────────────────────────────────────────────
async def test_dedicated_worker_answers_in_order_then_closes(
    fixture_app_server, browsers
):
    """MQ-155. Three ids in, three PREDICTED replies out in order, one close
    sentinel, then silence.

    The replies are predicted, not echoed: ``fr.w16_worker_replies()`` computes
    the hash of every id in Python before the browser runs, so a worker that
    replied with the id it was handed — or with nothing at all — cannot pass.
    The late message is the termination proof: it is posted AFTER the worker
    called ``self.close()``, and the log must still be exactly four entries.
    """
    base = fixture_app_server
    instance_id = await browsers.spawn(_profile("dedicated"))
    await navigate_and_settle(instance_id, f"{base}/workers/dedicated_host.html")

    assert await eval_js(instance_id, "window.w16Send([1,2,3])") == 3
    log = await _settled_json(
        instance_id,
        "(() => {const s = JSON.parse(window.w16Log()); "
        "return s.log.length >= 3 ? window.w16Log() : null;})()",
    )
    assert log["errors"] == []
    assert log["log"] == fr.w16_worker_replies()

    assert await eval_js(instance_id, "window.w16Close()") is True
    log = await _settled_json(
        instance_id,
        "(() => {const s = JSON.parse(window.w16Log()); "
        "return s.log.length >= 4 ? window.w16Log() : null;})()",
    )
    assert log["log"][3] == {
        "sentinel": fr.W16_WORKER_CLOSED,
        "version": fr.W16_WORKER_VERSION,
    }

    # A closed worker's queue is dead: this message can never be answered.
    assert await eval_js(instance_id, "window.w16Late()") is True
    assert await eval_js(instance_id, "window.w16Terminate()") is True
    await asyncio.sleep(1.0)
    final = json.loads(await eval_js(instance_id, "window.w16Log()"))
    assert final["terminated"] is True
    assert final["errors"] == []
    assert len(final["log"]) == 4, final["log"]
    assert not any(
        entry.get("id") == fr.W16_WORKER_LATE_ID for entry in final["log"]
    ), final["log"]

    assert await browsers.close(instance_id) is True


# ── MQ-156: the shared worker ───────────────────────────────────────────────
async def test_shared_worker_is_shared_across_tabs_then_reports_zero(
    fixture_app_server, browsers
):
    """MQ-156. Two tabs, two fixed port ids, ONE counter, controlled bye order,
    and an exact zero-client teardown sentinel read back from the server.

    The counter is what proves sharing: tab two's tick returns 1 and tab one's
    tick returns 2, which is only possible if both ports address one worker.
    The teardown sentinel is read from the fixture ledger rather than from a
    page, because by the time the worker has no clients there is no page left
    to report to.
    """
    base = fixture_app_server
    await _reset_ledger(base)
    instance_id = await browsers.spawn(_profile("shared"))
    await navigate_and_settle(instance_id, f"{base}/workers/shared_host.html")
    first = await _settled_json(
        instance_id,
        "(() => {const s = JSON.parse(window.w16Log()); "
        "return s.portId ? window.w16Log() : null;})()",
    )
    assert first["portId"] == 1
    assert first["version"] == fr.W16_SHARED_VERSION

    await get_fn("new_tab")(
        instance_id=instance_id, url=f"{base}/workers/shared_host.html"
    )
    hosts = []
    deadline = time.monotonic() + SETTLE
    while len(hosts) < 2 and time.monotonic() < deadline:
        await asyncio.sleep(POLL)
        hosts = await _tabs_at(instance_id, "/workers/shared_host.html")
    assert len(hosts) == 2, hosts
    tab_one, tab_two = hosts[0]["tab_id"], hosts[1]["tab_id"]
    switch = get_fn("switch_tab")

    await switch(instance_id=instance_id, tab_id=tab_two)
    second = await _settled_json(
        instance_id,
        "(() => {const s = JSON.parse(window.w16Log()); "
        "return s.portId ? window.w16Log() : null;})()",
    )
    assert second["portId"] == 2

    await eval_js(instance_id, "window.w16Tick()")
    ticked = await _settled_json(
        instance_id,
        "(() => {const s = JSON.parse(window.w16Log()); "
        "const t = s.log.filter((m) => m.counter); "
        "return t.length ? JSON.stringify(t) : null;})()",
    )
    assert ticked == [{"portId": 2, "counter": 1}]

    await switch(instance_id=instance_id, tab_id=tab_one)
    await eval_js(instance_id, "window.w16Tick()")
    ticked = await _settled_json(
        instance_id,
        "(() => {const s = JSON.parse(window.w16Log()); "
        "const t = s.log.filter((m) => m.counter); "
        "return t.length ? JSON.stringify(t) : null;})()",
    )
    assert ticked == [{"portId": 1, "counter": 2}], (
        "the second port did not observe the first port's increment, so the "
        "worker is not shared"
    )

    # Controlled close order: port 2 leaves first and the worker must NOT
    # report teardown while port 1 is still connected.
    await switch(instance_id=instance_id, tab_id=tab_two)
    await eval_js(instance_id, "window.w16Bye('w16-shared-token')")
    farewell = await _settled_json(
        instance_id,
        "(() => {const s = JSON.parse(window.w16Log()); "
        "const b = s.log.filter((m) => m.bye); "
        "return b.length ? JSON.stringify(b) : null;})()",
    )
    assert farewell == [{"portId": 2, "bye": True, "clients": 1}]
    assert (await _ledger(base))["w16_shared"] == []

    assert await get_fn("close_tab")(instance_id=instance_id, tab_id=tab_two) is True
    await switch(instance_id=instance_id, tab_id=tab_one)
    await eval_js(instance_id, "window.w16Bye('w16-shared-token')")
    reports = await _await_ledger(base, "w16_shared", 1)
    assert reports == [
        {
            "sentinel": fr.W16_SHARED_ZERO_CLIENTS,
            "counter": 2,
            "token": "w16-shared-token",
        }
    ]

    assert await browsers.close(instance_id) is True


async def test_a_separate_profile_gets_its_own_shared_worker_state(
    fixture_app_server, browsers
):
    """MQ-156, the isolation half. Two LIVE instances on separate profiles.

    Both are running at once and both address the same shared-worker URL on the
    same origin, so the only thing that can keep their state apart is the
    profile boundary. Sequencing them instead would prove nothing: the first
    worker would already be gone.
    """
    base = fixture_app_server
    await _reset_ledger(base)
    first_id = await browsers.spawn(_profile("shared-a"))
    second_id = await browsers.spawn(_profile("shared-b"))

    await navigate_and_settle(first_id, f"{base}/workers/shared_host.html")
    await _settled(first_id, "JSON.parse(window.w16Log()).portId")
    await eval_js(first_id, "window.w16Tick()")
    await eval_js(first_id, "window.w16Tick()")
    first = await _settled_json(
        first_id,
        "(() => {const s = JSON.parse(window.w16Log()); "
        "const t = s.log.filter((m) => m.counter); "
        "return t.length >= 2 ? JSON.stringify(t) : null;})()",
    )
    assert [entry["counter"] for entry in first] == [1, 2]

    await navigate_and_settle(second_id, f"{base}/workers/shared_host.html")
    second_hello = await _settled_json(
        second_id,
        "(() => {const s = JSON.parse(window.w16Log()); "
        "return s.portId ? window.w16Log() : null;})()",
    )
    assert second_hello["portId"] == 1, (
        "a second profile continued the first profile's port numbering, so the "
        "shared worker is not profile-scoped"
    )
    await eval_js(second_id, "window.w16Tick()")
    second = await _settled_json(
        second_id,
        "(() => {const s = JSON.parse(window.w16Log()); "
        "const t = s.log.filter((m) => m.counter); "
        "return t.length ? JSON.stringify(t) : null;})()",
    )
    assert second == [{"portId": 1, "counter": 1}]

    assert await browsers.close(first_id) is True
    assert await browsers.close(second_id) is True


# ── MQ-157: the service-worker lifecycle ────────────────────────────────────
async def test_service_worker_installs_activates_controls_and_unregisters(
    browsers,
):
    """MQ-157's satisfiable halves: first load, exact install/activate sentinels
    read from the SERVER, a controlling worker, then unregister + cache delete.

    The reload half is NOT here — it is F-800's pin below, and it is why MQ-157
    is `planned`. The server is this node's own short-lived origin so the
    service worker it registers can never outlive the node.
    """
    with serve_fixture_app() as base:
        instance_id = await browsers.spawn(_profile("pwa-life"))
        await _reset_ledger(base)
        await navigate_and_settle(instance_id, f"{base}/pwa/app.html")
        report = await _register_service_worker(instance_id)
        assert report["scope"] == f"{base}{fr.W16_SW_SCOPE}"
        assert report["controllerAtLoad"] == "uncontrolled"  # first load, pre-claim
        assert report["controller"] == "controlled"  # clients.claim() took effect

        assert await _await_ledger(base, "w16_sw", 2) == [
            {
                "phase": "install",
                "sentinel": fr.W16_SW_INSTALLED,
                "version": fr.W16_SW_VERSION,
            },
            {
                "phase": "activate",
                "sentinel": fr.W16_SW_ACTIVATED,
                "version": fr.W16_SW_VERSION,
            },
        ]
        # Install populated the cache from the network exactly once.
        assert (await _ledger(base))["w16_asset"] == [fr.W16_SW_ASSET_PATH]

        await eval_js(instance_id, "window.w16Cleanup()")
        assert await _settled_json(instance_id, "window.w16CleanupReport()") == {
            "registrations": 0,
            "caches": 0,
        }
        assert await eval_js(instance_id, "window.w16Controller()") == "controlled", (
            "unregister must not retroactively uncontrol the live document"
        )
        assert await browsers.close(instance_id) is True


@pytest.mark.characterization
async def test_reload_page_leaves_the_service_worker_page_uncontrolled(browsers):
    """PINS F-800: ``reload_page`` reloads a page out from under its own
    service worker.

    Same page, same active registration, two tools, two different outcomes:
    after ``navigate`` to the identical URL the document is controlled; after
    ``reload_page`` it is not. The cause is in `src/` — ``reload_page`` declares
    an ``ignore_cache`` parameter and calls ``tab.reload()`` without it, and
    nodriver's default is ``ignore_cache=True``, so every reload is a hard
    reload and Chrome bypasses the service worker for the main resource.

    Asserted in the direction that makes the FIX go red: when F-800 closes,
    ``controllerAtLoad`` becomes ``controlled`` and this node fails, forcing
    MQ-157 to be promoted deliberately.
    """
    with serve_fixture_app() as base:
        instance_id = await browsers.spawn(_profile("pwa-reload"))
        await _reset_ledger(base)
        await navigate_and_settle(instance_id, f"{base}/pwa/app.html")
        await _register_service_worker(instance_id)

        # Control: a fresh navigation to the same URL IS controlled at load.
        await navigate_and_settle(instance_id, f"{base}/pwa/app.html")
        navigated = await _settled_json(instance_id, "window.w16Report()")
        assert navigated["controllerAtLoad"] == "controlled", navigated

        assert await get_fn("reload_page")(instance_id=instance_id) is True
        reloaded = await _settled_json(instance_id, "window.w16Report()")
        assert reloaded["controllerAtLoad"] == "uncontrolled", (
            "F-800 is fixed: reload_page now yields a controlled document. "
            "Promote MQ-157 and bind its --mq id."
        )
        assert reloaded["controller"] == "uncontrolled", reloaded

        # The registration itself survived — only the DOCUMENT lost its worker.
        await eval_js(
            instance_id,
            "(() => {window.__w16regs = null;"
            "navigator.serviceWorker.getRegistrations().then((regs) => {"
            "window.__w16regs = JSON.stringify(regs.map((r) => ("
            "{scope: r.scope, state: r.active ? r.active.state : null})));});"
            "return true;})()",
        )
        assert await _settled_json(instance_id, "window.__w16regs") == [
            {"scope": f"{base}{fr.W16_SW_SCOPE}", "state": "activated"}
        ]

        await eval_js(instance_id, "window.w16Cleanup()")
        await _settled_json(instance_id, "window.w16CleanupReport()")
        assert await browsers.close(instance_id) is True


# ── MQ-158: CacheStorage bytes and the offline oracle ───────────────────────
async def test_cache_bytes_and_offline_reads_match_the_byte_oracle(browsers):
    """MQ-158. Counts, keys and per-entry hashes computed in Python, then a
    cached read proved by the ABSENCE of a network hit, then a real offline
    read after the fixture server is shut down.

    Two independent caches are involved on purpose: one seeded directly by the
    page (no network at all) and one populated by the service worker from the
    network. The offline half is taken after ``serve_fixture_app`` has exited,
    so "offline" is the socket being gone rather than a flag anyone set.
    """
    expected_state_cache = fr.w16_cache_oracle(fr.W16_STATE_CACHE, fr.W16_CACHE_ENTRIES)
    instance_id = None
    with serve_fixture_app() as base:
        instance_id = await browsers.spawn(_profile("cache"))
        await _reset_ledger(base)

        await navigate_and_settle(instance_id, f"{base}/state/store.html")
        await eval_js(instance_id, "window.w16SeedCache()")
        assert await _settled_json(instance_id, "window.w16State('cache')") == {
            "seeded": len(fr.W16_CACHE_ENTRIES)
        }
        await eval_js(instance_id, "window.w16ReadCache()")
        assert (
            await _settled_json(instance_id, "window.w16State('cache')")
            == expected_state_cache
        )
        assert (await _ledger(base))["w16_asset"] == [], (
            "a page-seeded cache must not have touched the network"
        )

        await navigate_and_settle(instance_id, f"{base}/pwa/app.html")
        await _register_service_worker(instance_id)
        assert (await _ledger(base))["w16_asset"] == [fr.W16_SW_ASSET_PATH]

        await eval_js(instance_id, "window.w16CacheReport()")
        report = await _settled_json(
            instance_id,
            "(() => {const r = JSON.parse(window.w16Report()); "
            "return r.cacheReport ? JSON.stringify(r.cacheReport) : null;})()",
        )
        assert report["names"] == sorted([fr.W16_SW_CACHE, fr.W16_STATE_CACHE])
        assert report["caches"][fr.W16_SW_CACHE] == {
            "count": 1,
            "items": [
                {
                    "path": fr.W16_SW_ASSET_PATH,
                    "status": 200,
                    "length": len(fr.W16_SW_ASSET_BODY),
                    "hash": fr.w16_hash(fr.W16_SW_ASSET_BODY),
                }
            ],
        }
        assert report["caches"][fr.W16_STATE_CACHE] == {
            "count": expected_state_cache["count"],
            "items": expected_state_cache["items"],
        }

        # ONLINE, but served from cache: the ledger must not grow.
        await _reset_ledger(base)
        await eval_js(instance_id, f"window.w16Fetch('{fr.W16_SW_ASSET_PATH}')")
        assert await _settled_json(
            instance_id, f"window.w16Fetched('{fr.W16_SW_ASSET_PATH}')"
        ) == {"status": 200, "body": fr.W16_SW_ASSET_BODY}
        assert (await _ledger(base))["w16_asset"] == [], (
            "the cached read reached the network, so nothing about offline "
            "behaviour can be concluded from it"
        )

    # The origin no longer exists from here on.
    await eval_js(instance_id, f"window.w16Fetch('{fr.W16_SW_ASSET_PATH}')")
    assert await _settled_json(
        instance_id, f"window.w16Fetched('{fr.W16_SW_ASSET_PATH}')"
    ) == {"status": 200, "body": fr.W16_SW_ASSET_BODY}

    await eval_js(instance_id, f"window.w16Fetch('{fr.W16_SW_UNCACHED_PATH}')")
    assert await _settled_json(
        instance_id, f"window.w16Fetched('{fr.W16_SW_UNCACHED_PATH}')"
    ) == {"status": 200, "body": fr.W16_SW_OFFLINE_BODY}, (
        "an uncached in-scope request did not fall back to the deterministic "
        "offline response"
    )

    await eval_js(instance_id, "window.w16Cleanup()")
    assert await _settled_json(instance_id, "window.w16CleanupReport()") == {
        "registrations": 0,
        "caches": 0,
    }
    assert await browsers.close(instance_id) is True


# ── MQ-159: IndexedDB ───────────────────────────────────────────────────────
async def test_indexed_db_index_and_transaction_results_match_the_oracle(
    fixture_app_server, browsers
):
    """MQ-159. Index cursor results, primary keys, a payload hash, and a rolled
    back transaction — all predicted in Python before the browser ran.

    The aborted transaction is the half that makes this a transaction test
    rather than a storage test: a record is written and the transaction is then
    aborted, so a store that committed it anyway would be caught.
    """
    base = fixture_app_server
    instance_id = await browsers.spawn(_profile("idb"))
    await navigate_and_settle(instance_id, f"{base}/state/store.html")

    await eval_js(instance_id, "window.w16SeedIdb()")
    assert await _settled_json(instance_id, "window.w16State('idb')") == {
        "seeded": len(fr.W16_IDB_RECORDS),
        "aborted": "yes",
    }

    await eval_js(instance_id, "window.w16QueryIdb()")
    observed = await _settled_json(instance_id, "window.w16State('idb')")
    assert observed["groups"] == {
        group: fr.w16_idb_group(group) for group in ("g1", "g2", "g3")
    }
    assert observed["keys"] == [record[0] for record in fr.W16_IDB_RECORDS]
    assert observed["payloadHash"] == fr.w16_hash(
        "|".join(record[3] for record in fr.W16_IDB_RECORDS)
    )
    assert observed["abortedRecordAbsent"] is True, (
        "the aborted transaction's record is readable, so the abort did not roll back"
    )

    await eval_js(instance_id, "window.w16ClearState()")
    assert (await _settled_json(instance_id, "window.w16State('cleared')"))[
        "caches"
    ] == 0
    assert await browsers.close(instance_id) is True


# ── MQ-160: storage, cookies, persistence, isolation, cleanup ───────────────
async def test_storage_and_cookies_survive_one_profile_and_no_other(
    fixture_app_server, browsers
):
    """MQ-160. The documented same-profile lifecycle, stated as what does AND
    does not survive, plus isolation and real cleanup.

    Local storage, IndexedDB, CacheStorage and a max-age cookie are expected to
    survive a browser restart on the same named profile; ``sessionStorage`` and
    a session cookie are expected NOT to. Asserting only the survivors would
    make the node pass on a browser that persisted everything, which is a
    different (and wrong) product.
    """
    base = fixture_app_server
    kept = _profile("persist")
    other = _profile("isolated")

    first_id = await browsers.spawn(kept)
    await navigate_and_settle(first_id, f"{base}/state/store.html")
    assert await eval_js(first_id, "window.w16SeedStorage()") is True
    await eval_js(first_id, "window.w16SeedIdb()")
    await _settled_json(first_id, "window.w16State('idb')")
    await eval_js(first_id, "window.w16SeedCache()")
    await _settled_json(first_id, "window.w16State('cache')")
    seeded = await _settled_json(first_id, "window.w16ReadStorage()")
    assert seeded["local"] == fr.W16_LOCAL_VALUE
    assert seeded["session"] == fr.W16_SESSION_VALUE
    assert (
        f"{fr.W16_COOKIE_PERSISTENT}={fr.W16_COOKIE_PERSISTENT_VALUE}"
        in (seeded["cookie"])
    )
    assert f"{fr.W16_COOKIE_SESSION}={fr.W16_COOKIE_SESSION_VALUE}" in seeded["cookie"]
    seeded_dir = browsers.profile_dirs[kept]
    assert await browsers.close(first_id) is True

    # Same named profile, brand-new browser process. The barrier is not
    # politeness: a named profile that is still held resolves to a numbered
    # variant cloned fresh from master, and this node would then report that
    # nothing persisted when in truth it had read the wrong profile.
    assert await browsers.await_profile_free(kept) is True
    second_id = await browsers.spawn(kept)
    assert browsers.profile_dirs[kept] == seeded_dir, (
        "the respawn resolved a DIFFERENT profile directory, so any persistence "
        "result below would be about the wrong profile"
    )
    await navigate_and_settle(second_id, f"{base}/state/store.html")
    restored = await _settled_json(second_id, "window.w16ReadStorage()")
    assert restored["local"] == fr.W16_LOCAL_VALUE
    assert restored["session"] is None, "sessionStorage must not survive a restart"
    assert restored["cookie"] == (
        f"{fr.W16_COOKIE_PERSISTENT}={fr.W16_COOKIE_PERSISTENT_VALUE}"
    ), "exactly the max-age cookie survives; the session cookie must not"

    await eval_js(second_id, "window.w16QueryIdb()")
    survived = await _settled_json(second_id, "window.w16State('idb')")
    assert survived["keys"] == [record[0] for record in fr.W16_IDB_RECORDS]
    await eval_js(second_id, "window.w16ReadCache()")
    assert await _settled_json(second_id, "window.w16State('cache')") == (
        fr.w16_cache_oracle(fr.W16_STATE_CACHE, fr.W16_CACHE_ENTRIES)
    )
    assert await browsers.close(second_id) is True

    # A different profile, same origin, same page: nothing carries over.
    third_id = await browsers.spawn(other)
    await navigate_and_settle(third_id, f"{base}/state/store.html")
    isolated = await _settled_json(third_id, "window.w16ReadStorage()")
    assert isolated == {
        "local": None,
        "session": None,
        "cookie": "",
        "localCount": 0,
        "dbs": None,
    }
    await eval_js(third_id, "window.w16QueryIdb()")
    assert (await _settled_json(third_id, "window.w16State('idb')"))["keys"] == []
    await eval_js(third_id, "window.w16ReadCache()")
    assert (await _settled_json(third_id, "window.w16State('cache')"))["count"] == 0
    assert await browsers.close(third_id) is True

    # Cleanup removes ALL fixture state — asserted, not hoped for.
    assert await browsers.await_profile_free(kept) is True
    assert await browsers.await_profile_free(other) is True
    assert await browsers.remove_profile(kept) is True
    assert await browsers.remove_profile(other) is True
    assert not Path(browsers.profile_dirs[kept]).exists()
    assert not Path(browsers.profile_dirs[other]).exists()


# ── MQ-161: the internationalized round trip ────────────────────────────────
async def test_unicode_round_trips_through_text_attributes_and_inputs(
    fixture_app_server, browsers
):
    """MQ-161. Exact code points through DOM text, an attribute, a pasted value,
    a typed value, and the page's action log.

    The NFC/NFD pair is the load-bearing case: the two are canonically
    equivalent and deliberately unequal, so any layer that normalized on the way
    through would collapse them and fail here rather than pass quietly. Nothing
    below looks at rendering, glyph order, or bidi layout — only code points.
    """
    base = fixture_app_server
    instance_id = await browsers.spawn(_profile("i18n"))
    await navigate_and_settle(instance_id, f"{base}/i18n/text.html")

    assert fr.code_points(fr.I18N_NFC) != fr.code_points(fr.I18N_NFD)

    for key, value in fr.I18N_STRINGS.items():
        expected = fr.code_points(value)
        assert (
            await _points(instance_id, f"window.w16Text('text-{key}')") == expected
        ), f"DOM text round trip failed for {key}"
        assert (
            await _points(instance_id, f"window.w16Attr('text-{key}')") == expected
        ), f"attribute round trip failed for {key}"
        assert await eval_js(instance_id, f"window.w16Dir('text-{key}')") == "auto"

    paste_text = get_fn("paste_text")
    for key, value in fr.I18N_STRINGS.items():
        assert await paste_text(
            instance_id=instance_id, selector=f"#input-{key}", text=value
        )
        assert await _points(
            instance_id, f"window.w16Value('input-{key}')"
        ) == fr.code_points(value), f"pasted value round trip failed for {key}"

    type_text = get_fn("type_text")
    for key in fr.I18N_TYPED_KEYS:
        value = fr.I18N_STRINGS[key]
        await eval_js(instance_id, f"document.getElementById('input-{key}').value = ''")
        assert await type_text(
            instance_id=instance_id,
            selector=f"#input-{key}",
            text=value,
            delay_ms=0,
        )
        assert await _points(
            instance_id, f"window.w16Value('input-{key}')"
        ) == fr.code_points(value), f"typed value round trip failed for {key}"

    # The action log is the page's own independent witness: it saw the same
    # final strings the value reads back, so the value was not set behind the
    # page's back.
    actions = json.loads(await eval_js(instance_id, "window.w16Actions()"))
    for key, value in fr.I18N_STRINGS.items():
        assert f"input-{key}={value}" in actions, key
    assert await browsers.close(instance_id) is True


# ── MQ-162: composition, and the honest limit around it ─────────────────────
async def test_the_synthesized_composition_sequence_is_exact(
    fixture_app_server, browsers
):
    """MQ-162. The DOM composition sequence the tool CAN synthesize, exactly.

    What this proves: driven through ``execute_script``, a full
    compositionstart → compositionupdate* → compositionend sequence with its
    interleaved ``insertCompositionText`` input events is delivered in order,
    with exact ``data`` on every event, and leaves the exact committed value.

    What it does NOT prove, stated here because the difference is the whole
    point: these are synthetic ``CompositionEvent``s. A native OS IME —
    candidate window, conversion, selection — is not automatable on a headless
    runner, no product tool synthesizes one, and nothing here should be read as
    evidence about one. The contrast node below records what the real input
    tools actually emit, which is not a composition at all.
    """
    base = fixture_app_server
    instance_id = await browsers.spawn(_profile("compose"))
    await navigate_and_settle(instance_id, f"{base}/i18n/composition.html")
    assert await eval_js(instance_id, "window.w16CompReset()") is True

    script = (
        "(() => {"
        "const el = document.getElementById('composer'); el.focus();"
        f"const steps = {json.dumps(list(fr.COMPOSITION_STEPS))};"
        f"const final = {json.dumps(fr.COMPOSITION_FINAL)};"
        "el.dispatchEvent(new CompositionEvent('compositionstart',"
        "{data: '', bubbles: true}));"
        "for (const step of steps) { el.value = step;"
        "el.dispatchEvent(new CompositionEvent('compositionupdate',"
        "{data: step, bubbles: true}));"
        "el.dispatchEvent(new InputEvent('input', {data: step, bubbles: true,"
        "inputType: 'insertCompositionText', isComposing: true})); }"
        "el.value = final;"
        "el.dispatchEvent(new CompositionEvent('compositionend',"
        "{data: final, bubbles: true}));"
        "el.dispatchEvent(new InputEvent('input', {data: final, bubbles: true,"
        "inputType: 'insertCompositionText'}));"
        "return 'composed';})()"
    )
    assert await eval_js(instance_id, script) == "composed"

    expected = ["compositionstart:"]
    for step in fr.COMPOSITION_STEPS:
        expected.extend([f"compositionupdate:{step}", f"input:{step}"])
    expected.extend(
        [
            f"compositionend:{fr.COMPOSITION_FINAL}",
            f"input:{fr.COMPOSITION_FINAL}",
        ]
    )
    assert json.loads(await eval_js(instance_id, "window.w16CompEvents()")) == expected

    detail = json.loads(await eval_js(instance_id, "window.w16CompDetail()"))
    assert [entry["value"] for entry in detail if entry["type"] == "input"] == [
        *fr.COMPOSITION_STEPS,
        fr.COMPOSITION_FINAL,
    ]
    assert all(
        entry["inputType"] == "insertCompositionText"
        for entry in detail
        if entry["type"] == "input"
    ), detail
    assert await _points(instance_id, "window.w16CompValue()") == fr.code_points(
        fr.COMPOSITION_FINAL
    )

    assert await browsers.close(instance_id) is True


async def test_the_real_input_tools_emit_no_composition_at_all(
    fixture_app_server, browsers
):
    """MQ-162's honesty control — current support, never acceptance.

    ``type_text`` and ``paste_text`` are the two tools that put text into a
    field, and NEITHER produces a composition. This is what makes the node
    above a statement about ``execute_script`` rather than a claim that the
    product speaks IME. It also fixes the shape of what they DO emit, so the
    two tools cannot silently swap behaviours.

    The missing ``keydown``/``keyup`` half of ``type_text`` is already pinned by
    ``tests/test_e2e_interaction_fidelity.py::test_keyboard_fidelity_and_enter_submit``
    and is not re-measured here.
    """
    base = fixture_app_server
    instance_id = await browsers.spawn(_profile("no-ime"))
    await navigate_and_settle(instance_id, f"{base}/i18n/composition.html")

    await eval_js(instance_id, "window.w16CompReset()")
    assert await get_fn("type_text")(
        instance_id=instance_id, selector="#composer", text="ab", delay_ms=0
    )
    typed = json.loads(await eval_js(instance_id, "window.w16CompEvents()"))
    assert typed == ["beforeinput:a", "input:a", "beforeinput:b", "input:b"]

    await eval_js(instance_id, "window.w16CompReset()")
    assert await get_fn("paste_text")(
        instance_id=instance_id, selector="#composer", text="ab"
    )
    pasted = json.loads(await eval_js(instance_id, "window.w16CompEvents()"))
    assert pasted == ["beforeinput:ab", "input:ab"]

    for observed in (typed, pasted):
        assert not any(entry.startswith("composition") for entry in observed), observed

    assert await browsers.close(instance_id) is True
