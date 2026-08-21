"""plan_RELEASE W4 (G-D) — deterministic offline stealth-invariant probe.

The product's headline promise is "undetectable by anti-bot systems" (G-D), yet
before this module that claim was asserted **nowhere**. This is the acceptance
suite that makes a regression which reintroduces ``navigator.webdriver``, a CDP
leak global, or a fingerprint tell fail the release gate.

Two tiers (stealth is the one place determinism and realism genuinely conflict):

* **Offline / deterministic (GATING)** — marker ``stealth`` (and ``integration``,
  so the unit lane ``-m "not integration"`` excludes it). The release gate lane
  selects ``-m "stealth and not online"``. It drives real headless Chrome against
  the passive local fixture ``fixture_app/stealth_probe.html`` (served by the ONE
  fixture mechanism, :func:`release_gate_harness.serve_fixture_app`), releases the
  armed probe only after an ordered CDP prerequisite transcript, validates the
  probe's closed schema, and runs a versioned predicate table. A deliberately
  non-stealth **vanilla control** (the same product spawn path with the stealth
  arg-filter neutralized — the single intentional treatment) must FAIL the probes,
  proving the predicates have teeth.

* **Online / informational (NON-GATING, opt-in)** — markers ``stealth`` +
  ``online`` + ``integration``. Excluded from every default run and the release
  gate (``not online``). Drives CreepJS and bot.incolumitas, asserts only the hard
  invariants, logs the rest, and tolerates network flakiness by design.

Design honesty (release-claim integrity): a known gap is pinned as a strict
``xfail`` with a finding id, never hidden by weakening a probe — an xfailed
invariant does NOT satisfy a release claim.

* **F-770 — CLOSED by RELEASE-FIX-D.** The headless User-Agent advertised
  ``HeadlessChrome`` on every vector that reaches a site. ``src/`` now supplies a
  masked default ``--user-agent`` from ``platform_utils.merge_browser_args``, so
  the signal moved into the GATING table and the xfail was deleted rather than
  relaxed. The four measured vectors are documented at :data:`F770_VECTORS`.
* **F-774 — OPEN, opened by that fix.** A ``--user-agent`` override makes Chrome
  blank the high-entropy UA client hints; the low-entropy ``sec-ch-ua*`` headers
  on the wire stay correct. Recorded in :data:`XFAIL_SIGNALS` with the measured
  before/after, not papered over.

Both browsers spawn through the project's own ``spawn_browser`` tool; the ordered
transcript uses the project's own nodriver ``tab.send(uc.cdp.*)`` CDP seam; the
fixture and env-isolation reuse W1's homes.
"""

from __future__ import annotations

import json
import os
import platform as _platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import nodriver as uc
import pytest

from e2e_helpers import (
    CAN_RUN,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    sandbox_kwargs,
    server_mod,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded.platform_utils import (
    reset_browser_version_memo as _reset_browser_version_memo,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# Real-Chrome tier: mark the whole module ``stealth`` + ``integration`` (+ skip
# when Chrome/the server is unavailable, via the shared e2e guard). The gate lane
# is ``-m "stealth and not online"``; the unit lane ``-m "not integration"`` skips.
_integration = integration_pytestmark()
if not isinstance(_integration, list):
    _integration = [_integration]
pytestmark = [pytest.mark.stealth, *_integration]


# ===========================================================================
# Versioned signal specification (the ONE reviewed predicate table).
#
# Each row names a signal, the raw observation key(s) it reads, the predicate
# (True == stealthy), the per-OS platform allowance, and a ``forbidden`` overlay
# that introduces the forbidden observation for that collector family — the
# deterministic sensitivity control that proves the predicate can detect a leak.
#
# "Truthy", "looks normal", and silently accepting unavailable values are NOT
# predicates. A Chrome-major change that invalidates a platform allowance requires
# a reviewed edit to this table.
# ===========================================================================

# Required members of a real ``window.chrome`` (present without an extension).
_CHROME_REQUIRED_MEMBERS = frozenset({"app", "csi", "loadTimes"})

# Per-OS ``navigator.platform`` allowance. os.uname-family -> accepted values.
_PLATFORM_ALLOWANCE = {
    "Windows": {"Win32", "Win64"},
    "Linux": {"Linux x86_64", "Linux aarch64", "Linux armv8l", "Linux"},
    "Darwin": {"MacIntel", "MacARM", "macOS"},
}

_NATIVE_SUFFIX = "{ [native code] }"


def _os_family() -> str:
    return _platform.system()


@dataclass(frozen=True)
class Signal:
    """One reviewed stealth-signal row."""

    name: str
    obs_keys: tuple[str, ...]
    predicate: Callable[[dict, str], bool]
    forbidden: dict  # overlay onto a good baseline -> predicate must return False
    description: str
    platforms: tuple[str, ...] = ("*",)
    finding_id: str | None = None  # set on xfail rows

    def applies(self, os_family: str) -> bool:
        return self.platforms == ("*",) or os_family in self.platforms


# ── Predicates (pure; operate on the raw observation dict) ──────────────────
def _p_webdriver_absent(obs: dict, _os: str) -> bool:
    # navigator.webdriver must be exactly False (not True, not missing-as-true).
    return obs.get("webdriver") is False


def _p_window_chrome(obs: dict, _os: str) -> bool:
    if obs.get("window_chrome_present") is not True:
        return False
    members = set(obs.get("window_chrome_members") or [])
    return members >= _CHROME_REQUIRED_MEMBERS


def _p_plugins_present(obs: dict, _os: str) -> bool:
    return isinstance(obs.get("plugins_count"), int) and obs["plugins_count"] >= 1


def _p_languages_present(obs: dict, _os: str) -> bool:
    langs = obs.get("languages")
    lang = obs.get("language")
    return bool(isinstance(langs, list) and langs and isinstance(lang, str) and lang)


def _p_leak_globals_absent(obs: dict, _os: str) -> bool:
    return (
        obs.get("automation_globals_window") == []
        and obs.get("automation_globals_document") == []
    )


def _p_fn_tostring_native(obs: dict, _os: str) -> bool:
    alert = obs.get("fn_tostring_alert") or ""
    meta = obs.get("fn_tostring_meta") or ""
    return alert.rstrip().endswith(_NATIVE_SUFFIX) and "[native code]" in meta


def _p_notification_consistent(obs: dict, _os: str) -> bool:
    perm = obs.get("notification_permission")
    query = obs.get("permissions_query_notifications")
    if perm not in {"default", "granted", "denied"}:
        return False
    # The classic headless leak: Notification.permission == 'denied' while the
    # Permissions API reports 'prompt'. Consistent browsers never show that pair.
    return not (perm == "denied" and query == "prompt")


def _p_platform_present(obs: dict, os_family: str) -> bool:
    plat = obs.get("platform")
    if not (isinstance(plat, str) and plat):
        return False
    allowed = _PLATFORM_ALLOWANCE.get(os_family)
    return plat in allowed if allowed else True


def _p_ua_client_hints(obs: dict, _os: str) -> bool:
    if obs.get("ua_client_hints_present") is not True:
        return False
    brands = obs.get("ua_client_hints_brands") or []
    if not brands:
        return False
    return not any("headlesschrome" in str(b).lower() for b in brands)


def _p_outer_dimensions(obs: dict, _os: str) -> bool:
    return (
        isinstance(obs.get("outer_width"), int)
        and isinstance(obs.get("outer_height"), int)
        and obs["outer_width"] > 0
        and obs["outer_height"] > 0
    )


def _p_ua_no_headless(obs: dict, _os: str) -> bool:
    return "headlesschrome" not in str(obs.get("user_agent") or "").lower()


def _p_http_ua_no_headless(obs: dict, _os: str) -> bool:
    # F-770 V2 — the header the fixture server actually READ off the wire. A
    # page-level override can satisfy _p_ua_no_headless while this still leaks.
    header = obs.get("http_user_agent")
    if not isinstance(header, str) or not header or header.startswith("ERROR:"):
        return False
    return "headlesschrome" not in header.lower()


def _p_ua_matches_http_header(obs: dict, _os: str) -> bool:
    # Coherence: a page UA that disagrees with the wire UA is a mismatch no real
    # browser produces — a sharper tell than the honest headless token.
    page = obs.get("user_agent")
    header = obs.get("http_user_agent")
    return bool(page) and isinstance(header, str) and header == page


def _ua_major(user_agent: object) -> str | None:
    m = re.search(r"(?:Headless)?Chrome/(\d+)\.", str(user_agent or ""))
    return m.group(1) if m else None


def _p_ua_client_hints_high_entropy_populated(obs: dict, _os: str) -> bool:
    # F-774: a real Chrome fills every high-entropy hint. Chrome blanks them
    # whenever a --user-agent override is active, which is how the F-770 mask
    # works — see XFAIL_SIGNALS.
    high_entropy = obs.get("ua_client_hints_high_entropy")
    if not isinstance(high_entropy, dict):
        return False
    return all(
        bool(high_entropy.get(key))
        for key in ("architecture", "bitness", "uaFullVersion", "fullVersionList")
    )


def _p_ua_major_matches_client_hints(obs: dict, _os: str) -> bool:
    # Coherence: the UA's major version must be one the UA client hints agree
    # with. Masking the token while claiming a version sec-ch-ua contradicts
    # would replace one tell with a worse one.
    major = _ua_major(obs.get("user_agent"))
    high_entropy = obs.get("ua_client_hints_high_entropy")
    if not major or not isinstance(high_entropy, dict):
        return False
    majors = {
        str(brand.get("version", "")).split(".")[0]
        for brand in (high_entropy.get("brands") or [])
        if isinstance(brand, dict)
    }
    majors.discard("")
    return bool(majors) and major in majors


# ── Gating table: the product MUST pass every applicable row ────────────────
GATE_SIGNALS: tuple[Signal, ...] = (
    Signal(
        "webdriver_absent",
        ("webdriver",),
        _p_webdriver_absent,
        {"webdriver": True},
        "navigator.webdriver is exactly False (not the automation-controlled true).",
    ),
    Signal(
        "window_chrome_members",
        ("window_chrome_present", "window_chrome_members"),
        _p_window_chrome,
        {"window_chrome_present": False, "window_chrome_members": []},
        "window.chrome present with required members {app, csi, loadTimes}.",
    ),
    Signal(
        "plugins_present",
        ("plugins_count",),
        _p_plugins_present,
        {"plugins_count": 0},
        "navigator.plugins is non-empty (empty is a classic headless tell).",
    ),
    Signal(
        "languages_present",
        ("languages", "language"),
        _p_languages_present,
        {"languages": [], "language": ""},
        "navigator.languages / navigator.language are populated.",
    ),
    Signal(
        "cdp_leak_globals_absent",
        ("automation_globals_window", "automation_globals_document"),
        _p_leak_globals_absent,
        {"automation_globals_window": ["cdc_adoQpoasnfa76pfcZLmcfl_Array"]},
        "No cdc_/$cdc/selenium/webdriver/phantom globals on window or document.",
    ),
    Signal(
        "function_tostring_native",
        ("fn_tostring_alert", "fn_tostring_meta"),
        _p_fn_tostring_native,
        {"fn_tostring_alert": "function alert() { return sneaky(); }"},
        "Native builtins serialize as native code; Function.prototype.toString "
        "is itself native (unpatched).",
    ),
    Signal(
        "notification_permission_consistent",
        ("notification_permission", "permissions_query_notifications"),
        _p_notification_consistent,
        {
            "notification_permission": "denied",
            "permissions_query_notifications": "prompt",
        },
        "Notification.permission is not the denied/prompt mismatch vs Permissions API.",
    ),
    Signal(
        "platform_present",
        ("platform",),
        _p_platform_present,
        {"platform": ""},
        "navigator.platform matches the reviewed per-OS allowance.",
    ),
    Signal(
        "ua_client_hints_present",
        ("ua_client_hints_present", "ua_client_hints_brands"),
        _p_ua_client_hints,
        {"ua_client_hints_present": False, "ua_client_hints_brands": []},
        "userAgentData present with brands, and no brand reveals HeadlessChrome.",
    ),
    Signal(
        "outer_dimensions_nonzero",
        ("outer_width", "outer_height"),
        _p_outer_dimensions,
        {"outer_width": 0, "outer_height": 0},
        "window.outerWidth/outerHeight are non-zero (0x0 is a headless tell).",
    ),
    # ── F-770, closed by RELEASE-FIX-D. These four rows were a strict xfail until
    # the product supplied a masked default --user-agent; they are GATING now, so
    # a regression that reintroduces the headless token fails the release gate.
    # An xfailed invariant never satisfied the stealth claim; these do.
    Signal(
        "ua_no_headless_token",
        ("user_agent",),
        _p_ua_no_headless,
        {"user_agent": "Mozilla/5.0 ... HeadlessChrome/150.0.0.0 Safari/537.36"},
        "navigator.userAgent does not advertise HeadlessChrome (F-770 V1).",
    ),
    Signal(
        "http_ua_no_headless_token",
        ("http_user_agent",),
        _p_http_ua_no_headless,
        {"http_user_agent": "Mozilla/5.0 ... HeadlessChrome/150.0.0.0 Safari/537.36"},
        "The User-Agent header the SERVER received does not advertise "
        "HeadlessChrome (F-770 V2 — the vector a bot check reads first).",
    ),
    Signal(
        "ua_matches_http_header",
        ("user_agent", "http_user_agent"),
        _p_ua_matches_http_header,
        {"http_user_agent": "Mozilla/5.0 ... HeadlessChrome/150.0.0.0 Safari/537.36"},
        "The page User-Agent and the wire User-Agent are the same string "
        "(a page-only override that leaves the header leaking is a worse tell).",
    ),
    Signal(
        "ua_major_matches_client_hints",
        ("user_agent", "ua_client_hints_high_entropy"),
        _p_ua_major_matches_client_hints,
        {
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
            )
        },
        "The masked User-Agent's major version is one the UA client hints agree "
        "with (masking coherently, not swapping one tell for a worse one).",
    ),
)

# ── xfail table: a KNOWN product stealth gap, pinned honestly (never weakened) ─
# F-774 (opened by RELEASE-FIX-D, measured not assumed): whenever a --user-agent
# override is active — which is exactly how the F-770 mask works — Chrome BLANKS
# the high-entropy UA client hints it cannot derive from the override string.
# Measured on Chrome 150, headless, with and without the mask:
#
#   architecture     "x86"              -> ""
#   bitness          "64"               -> ""
#   platformVersion  "19.0.0"           -> ""
#   uaFullVersion    "150.0.7871.186"   -> ""
#   fullVersionList  [3 brands]         -> []
#   brands / mobile / platform          UNCHANGED and still correct
#
# So the LOW-entropy hints — the `sec-ch-ua`, `sec-ch-ua-mobile` and
# `sec-ch-ua-platform` headers Chrome actually puts on the wire by default —
# stay coherent with the masked UA; only the JS-only high-entropy set, which a
# site must ask for explicitly, comes back empty. That is a strictly smaller and
# strictly more expensive tell than the `HeadlessChrome` token it replaces (one
# server-side substring test, before any JavaScript runs), but it IS a new tell
# and is recorded here rather than papered over. Fixing it needs
# Emulation.setUserAgentOverride's userAgentMetadata, which is per-target and
# therefore a different mechanism with its own hazards — deliberately out of
# FIX-D's scope. Strict xfail: if it is ever fixed, or Chrome changes, this
# XPASSes and turns the suite red, forcing the review.
XFAIL_SIGNALS: tuple[Signal, ...] = (
    Signal(
        "ua_client_hints_high_entropy_populated",
        ("ua_client_hints_high_entropy",),
        _p_ua_client_hints_high_entropy_populated,
        {
            "ua_client_hints_high_entropy": {
                "brands": [{"brand": "Google Chrome", "version": "150"}],
                "architecture": "",
                "bitness": "",
                "uaFullVersion": "",
                "fullVersionList": [],
            }
        },
        "getHighEntropyValues() returns populated architecture/bitness/"
        "uaFullVersion/fullVersionList, as an unmasked Chrome does.",
        finding_id="F-774",
    ),
)

# The exact ordered CDP prerequisite transcript (spec §2.4). A pristine page that
# never saw this real CDP activity must not be able to pass a leak check.
CDP_PREREQUISITE_METHODS: tuple[str, ...] = (
    "Runtime.enable",
    "Page.enable",
    "Network.enable",
    "DOM.getDocument",
    "Runtime.evaluate",  # nonce sentinel
    "Page.captureScreenshot",
)

RESULT_SCHEMA_VERSION = 1
_RESULT_SCHEMA_KEYS = frozenset(
    {"schema_version", "started_ms", "finished_ms", "complete", "observations"}
)


# ===========================================================================
# Closed-schema validation (with teeth — see the deterministic negative test).
# ===========================================================================
def validate_result_schema(obj: object) -> None:
    """Raise AssertionError on an absent, extra, duplicate, or unserializable field.

    The probe publishes exactly one closed object; this is the validator the gate
    runs against the real result and the deterministic negative controls exercise.
    """
    assert isinstance(obj, dict), f"result must be an object, got {type(obj).__name__}"
    keys = set(obj)
    missing = _RESULT_SCHEMA_KEYS - keys
    extra = keys - _RESULT_SCHEMA_KEYS
    assert not missing, f"absent schema field(s): {sorted(missing)}"
    assert not extra, f"extra schema field(s): {sorted(extra)}"
    assert obj["schema_version"] == RESULT_SCHEMA_VERSION, obj["schema_version"]
    assert obj["complete"] is True, obj["complete"]
    assert isinstance(obj["started_ms"], (int, float)), obj["started_ms"]
    assert isinstance(obj["finished_ms"], (int, float)), obj["finished_ms"]
    assert obj["finished_ms"] >= obj["started_ms"], (
        obj["started_ms"],
        obj["finished_ms"],
    )
    assert isinstance(obj["observations"], dict) and obj["observations"], "observations"
    # Unserializable -> raises TypeError, surfacing as a failure here.
    json.dumps(obj)


# ===========================================================================
# Real-browser collection (spawn -> armed-check -> ordered CDP transcript ->
# release -> read result). Runs inside its own asyncio.run in a sync module-scoped
# fixture, so it returns plain data and never shares a live browser across the
# function-scoped event loops pytest-asyncio hands each test (the cross-loop
# nodriver hazard the e2e suite documents).
# ===========================================================================
_NONCE = "stealth-nonce-4d1f"


async def _run_cdp_transcript(tab) -> list[dict]:
    """Execute the exact ordered prerequisite CDP sequence via the project's own
    nodriver ``tab.send(uc.cdp.*)`` seam; return an ordered method/result log."""
    transcript: list[dict] = []

    async def step(method: str, coro, checker=None):
        entry = {"method": method}
        try:
            res = await tab.send(coro)
            entry["ok"] = True if checker is None else bool(checker(res))
        except Exception as exc:  # record, never swallow silently
            entry["ok"] = False
            entry["error"] = str(exc)
        transcript.append(entry)

    await step("Runtime.enable", uc.cdp.runtime.enable())
    await step("Page.enable", uc.cdp.page.enable())
    await step("Network.enable", uc.cdp.network.enable())
    await step("DOM.getDocument", uc.cdp.dom.get_document())
    await step(
        "Runtime.evaluate",
        uc.cdp.runtime.evaluate(expression=f"'{_NONCE}'", return_by_value=True),
        checker=lambda r: bool(r and r[0] and r[0].value == _NONCE),
    )
    await step(
        "Page.captureScreenshot",
        uc.cdp.page.capture_screenshot(),
        checker=lambda r: bool(r),
    )
    return transcript


async def _collect_probe(base_url: str, *, control: bool) -> dict:
    """Spawn (product or neutralized-filter control), release the armed probe after
    the ordered transcript, and return a plain-data record."""
    from stealth_chrome_devtools_mcp.embedded import browser_manager as _bm_mod
    from stealth_chrome_devtools_mcp.embedded.platform_utils import (
        check_browser_executable,
    )

    # The gate lane (``-m "stealth and not online"``) selects THIS module alone, so
    # unlike the full integration lane no earlier E2E module has already paid for
    # Chrome's cold start. nodriver gives the debug port only a few seconds, and a
    # first launch on a cold Linux/macOS runner overruns it ("Failed to connect to
    # browser"). Reuse the shared idempotent warmup rather than growing a timeout.
    await warmup_once()

    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    bm = server_mod.browser_manager

    patched_orig = None
    if control:
        # THE single intentional treatment: neutralize the stealth arg-filter so
        # --enable-automation survives to Chrome. Everything else is the identical
        # product spawn path (BrowserManager.spawn_browser -> uc.start). Restored
        # in finally — never leaks into the product run.
        patched_orig = _bm_mod.merge_browser_args
        _bm_mod.merge_browser_args = lambda args: (list(args or []), [])

    iid = None
    try:
        browser_args = ["--enable-automation"] if control else []
        spawned = await spawn(
            headless=True,
            viewport_width=1280,
            viewport_height=800,
            browser_args=browser_args,
            **sandbox_kwargs(),
        )
        iid = spawned["instance_id"]
        url = f"{base_url}/stealth_probe.html"
        await navigate_and_settle(iid, url)
        tab = await bm.get_tab(iid)

        # Armed: the probe must NOT have collected before we release it.
        armed = await tab.send(
            uc.cdp.runtime.evaluate(
                expression="typeof window.__STEALTH_PROBE_RESULT__",
                return_by_value=True,
            )
        )
        armed_before_start = bool(armed and armed[0] and armed[0].value == "undefined")

        transcript = await _run_cdp_transcript(tab)

        # One final Runtime.evaluate calls the armed probe's start function.
        await tab.send(
            uc.cdp.runtime.evaluate(
                expression="window.__STEALTH_PROBE_START__()",
                return_by_value=True,
                await_promise=True,
            )
        )
        raw = await tab.send(
            uc.cdp.runtime.evaluate(
                expression="JSON.stringify(window.__STEALTH_PROBE_RESULT__)",
                return_by_value=True,
            )
        )
        result = json.loads(raw[0].value)
        evc = await tab.send(
            uc.cdp.runtime.evaluate(
                expression="window.__STEALTH_PROBE_EVENT_COUNT__", return_by_value=True
            )
        )
        event_count = evc[0].value if (evc and evc[0]) else None

        # Process-flag evidence: the actual launched Chrome command line + the
        # exact binary identity (W2's resolver).
        browser = await bm.get_browser(iid)
        cmdline: list[str] = []
        pid = None
        proc = getattr(browser, "_process", None)
        if proc is not None:
            pid = getattr(proc, "pid", None)
            try:
                import psutil

                cmdline = psutil.Process(pid).cmdline()
            except Exception:  # best-effort evidence, no secrets kept
                cmdline = []
        binary = check_browser_executable()

        # F-770 vector V4 -- the CDP-level User-Agent (Browser.getVersion). Not a
        # page observation, so it is collected here alongside the process evidence.
        version = await tab.send(uc.cdp.browser.get_version())
        cdp_user_agent = version[3] if version and len(version) > 3 else None

        return {
            "result": result,
            "observations": result.get("observations", {}),
            "transcript": transcript,
            "armed_before_start": armed_before_start,
            "event_count": event_count,
            "cmdline": cmdline,
            "binary": binary,
            "cdp_user_agent": cdp_user_agent,
            "headless": True,
        }
    finally:
        if iid is not None:
            try:
                await close(instance_id=iid)
            except Exception:  # teardown best-effort
                pass
        if patched_orig is not None:
            _bm_mod.merge_browser_args = patched_orig


def _collect_sync(base_url: str, *, control: bool) -> dict:
    import asyncio

    return asyncio.run(_collect_probe(base_url, control=control))


@pytest.fixture(scope="module")
def product_probe(fixture_app_server) -> dict:
    if not CAN_RUN:
        pytest.skip("Chrome not available or server failed to load")
    return _collect_sync(fixture_app_server, control=False)


@pytest.fixture(scope="module")
def control_probe(fixture_app_server) -> dict:
    if not CAN_RUN:
        pytest.skip("Chrome not available or server failed to load")
    return _collect_sync(fixture_app_server, control=True)


# ── F-770 coverage facts: a LATER tab, and an explicit caller User-Agent ──────
# The masked default is a launch flag, so it is process-wide by construction —
# but "by construction" is exactly the reasoning FIX-C's re-arm defect punished,
# so both properties are pinned against a real browser instead of argued.
_EXPLICIT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36 fix-d-explicit"
)


async def _read_tab_ua(tab) -> str:
    raw = await tab.send(
        uc.cdp.runtime.evaluate(expression="navigator.userAgent", return_by_value=True)
    )
    return raw[0].value if (raw and raw[0]) else ""


async def _read_tab_http_ua(tab, base_url: str) -> str:
    raw = await tab.send(
        uc.cdp.runtime.evaluate(
            expression=(
                f"fetch('{base_url}/api/echo', {{method: 'POST', body: 'tab-probe'}})"
                ".then(r => r.json()).then(j => j.headers['user-agent'])"
            ),
            return_by_value=True,
            await_promise=True,
        )
    )
    return raw[0].value if (raw and raw[0]) else ""


async def _read_browser_product(tab) -> str:
    """The running browser's own version string (``Browser.getVersion``'s
    ``product``) — NOT the User-Agent, and not rewritten by ``--user-agent=``.

    That last property is what makes it the authoritative reading of which
    browser actually launched, and therefore the yardstick F-806 is measured
    against. It is measured, not assumed: see
    :func:`test_f806_a_stale_version_memo_is_repaired_by_the_launched_browser`,
    which reads it out of a browser launched with a deliberately wrong
    ``--user-agent=`` and requires it to still report the real version.
    """
    version = await tab.send(uc.cdp.browser.get_version())
    return version[1] if (version and len(version) > 1) else ""


async def _collect_ua_facts(base_url: str, *, user_agent: str | None) -> dict:
    """Spawn once, then read the User-Agent from the spawn tab AND from a tab
    created afterwards through the product's own ``new_tab`` tool."""
    await warmup_once()
    # F-806: the warmup performs a real spawn, and that spawn RECONCILES the
    # version memo against the browser it launched. Reading the UA after it would
    # measure the repaired value and never the pre-launch probe's — i.e. the one
    # thing the first spawn of a fresh backend actually ships. Clearing the memo
    # puts the spawn below back in the cold-first-spawn state the warmup absorbs.
    _reset_browser_version_memo()
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    open_tab = get_fn("new_tab")
    bm = server_mod.browser_manager

    explicit = {"user_agent": user_agent} if user_agent else {}
    url = f"{base_url}/stealth_probe.html"
    iid = None
    try:
        spawned = await spawn(headless=True, **explicit, **sandbox_kwargs())
        iid = spawned["instance_id"]
        await navigate_and_settle(iid, url)
        spawn_tab = await bm.get_tab(iid)
        created = await open_tab(instance_id=iid, url=url)
        browser = await bm.get_browser(iid)
        later_tab = next(
            (t for t in browser.tabs if str(t.target.target_id) == created["tab_id"]),
            None,
        )
        assert later_tab is not None, (
            created["tab_id"],
            [str(t.target.target_id) for t in browser.tabs],
        )
        return {
            "spawn_tab_ua": await _read_tab_ua(spawn_tab),
            "new_tab_ua": await _read_tab_ua(later_tab),
            "new_tab_http_ua": await _read_tab_http_ua(later_tab, base_url),
            # F-806: what the browser this UA is supposed to describe really is.
            "browser_product": await _read_browser_product(spawn_tab),
        }
    finally:
        if iid is not None:
            try:
                await close(instance_id=iid)
            except Exception:  # teardown best-effort
                pass


def _collect_ua_facts_sync(base_url: str, *, user_agent: str | None) -> dict:
    import asyncio

    return asyncio.run(_collect_ua_facts(base_url, user_agent=user_agent))


@pytest.fixture(scope="module")
def ua_default_facts(fixture_app_server) -> dict:
    if not CAN_RUN:
        pytest.skip("Chrome not available or server failed to load")
    return _collect_ua_facts_sync(fixture_app_server, user_agent=None)


@pytest.fixture(scope="module")
def ua_explicit_facts(fixture_app_server) -> dict:
    if not CAN_RUN:
        pytest.skip("Chrome not available or server failed to load")
    return _collect_ua_facts_sync(fixture_app_server, user_agent=_EXPLICIT_UA)


# ===========================================================================
# Deterministic, browser-free sensitivity controls (run everywhere, gate-safe).
# For each collector family: the predicate PASSES a good baseline and FAILS when
# the forbidden observation is introduced — proving the probe has teeth.
# ===========================================================================
def _good_baseline() -> dict:
    os_family = _os_family()
    platform_value = next(iter(_PLATFORM_ALLOWANCE.get(os_family, {"Win32"})))
    return {
        "webdriver": False,
        "webdriver_typeof": "boolean",
        "window_chrome_present": True,
        "window_chrome_members": ["app", "csi", "loadTimes"],
        "plugins_count": 5,
        "mime_types_count": 2,
        "languages": ["en-US", "en"],
        "language": "en-US",
        "notification_permission": "default",
        "permissions_query_notifications": "prompt",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        ),
        "http_user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        ),
        "http_request_headers": {
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
            "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", '
            '"Google Chrome";v="150"',
        },
        "platform": platform_value,
        "ua_client_hints_present": True,
        "ua_client_hints_brands": ["Chromium", "Google Chrome", "Not;A=Brand"],
        "ua_client_hints_high_entropy": {
            "brands": [
                {"brand": "Not;A=Brand", "version": "8"},
                {"brand": "Chromium", "version": "150"},
                {"brand": "Google Chrome", "version": "150"},
            ],
            "fullVersionList": [
                {"brand": "Google Chrome", "version": "150.0.7871.186"}
            ],
            "uaFullVersion": "150.0.7871.186",
            "architecture": "x86",
            "bitness": "64",
            "platform": "Windows",
            "platformVersion": "19.0.0",
        },
        "fn_tostring_alert": "function alert() { [native code] }",
        "fn_tostring_meta": "function toString() { [native code] }",
        "automation_globals_window": [],
        "automation_globals_document": [],
        "outer_width": 1280,
        "outer_height": 800,
    }


def test_signal_table_is_reviewed_and_nonempty():
    """The predicate table exists, is versioned, and has unique signal names.

    Every row is GATING: RELEASE-FIX-D closed F-770, the last xfailed signal, so
    the table no longer carries an xfail tier. A future known gap re-adds one
    deliberately (with a finding id) — it is never created by weakening a row.
    """
    names = [s.name for s in (*GATE_SIGNALS, *XFAIL_SIGNALS)]
    assert len(names) == len(set(names)), "duplicate signal names"
    assert len(GATE_SIGNALS) >= 8, "expected a broad gating table"
    assert {"ua_no_headless_token", "http_ua_no_headless_token"} <= set(names), (
        "F-770's page AND wire User-Agent signals must both gate"
    )
    for s in XFAIL_SIGNALS:
        assert s.finding_id, f"xfail signal {s.name} must carry a finding id"


@pytest.mark.parametrize("sig", GATE_SIGNALS, ids=lambda s: s.name)
def test_gate_predicate_has_teeth(sig: Signal):
    """Each gating predicate PASSES the good baseline and FAILS the forbidden one."""
    os_family = _os_family()
    if not sig.applies(os_family):
        pytest.skip(f"{sig.name} not applicable on {os_family}")
    baseline = _good_baseline()
    assert sig.predicate(baseline, os_family) is True, (
        f"{sig.name} should PASS a stealthy baseline"
    )
    forbidden = {**baseline, **sig.forbidden}
    assert sig.predicate(forbidden, os_family) is False, (
        f"{sig.name} did not detect the forbidden observation {sig.forbidden!r}"
    )


@pytest.mark.parametrize("sig", XFAIL_SIGNALS, ids=lambda s: s.name)
def test_xfail_predicate_has_teeth(sig: Signal):
    """The pinned-gap predicate is still a real predicate: it PASSES a clean sample
    and FAILS the forbidden one (so the xfail below is a genuine gap, not a dead
    assertion)."""
    os_family = _os_family()
    baseline = _good_baseline()
    assert sig.predicate(baseline, os_family) is True
    assert sig.predicate({**baseline, **sig.forbidden}, os_family) is False


def test_result_schema_validator_rejects_bad_shapes():
    """The closed-schema validator fails on absent, extra, wrong-type, and
    unserializable fields (deterministic negative controls)."""
    good = {
        "schema_version": 1,
        "started_ms": 1.0,
        "finished_ms": 2.0,
        "complete": True,
        "observations": {"webdriver": False},
    }
    validate_result_schema(good)  # baseline passes

    with pytest.raises(AssertionError):  # absent field
        validate_result_schema({k: v for k, v in good.items() if k != "complete"})
    with pytest.raises(AssertionError):  # extra field
        validate_result_schema({**good, "surprise": 1})
    with pytest.raises(AssertionError):  # not complete
        validate_result_schema({**good, "complete": False})
    with pytest.raises(AssertionError):  # wrong schema version
        validate_result_schema({**good, "schema_version": 2})
    with pytest.raises(AssertionError):  # finished before started
        validate_result_schema({**good, "started_ms": 5.0, "finished_ms": 1.0})
    with pytest.raises((AssertionError, TypeError)):  # unserializable field
        validate_result_schema({**good, "observations": {"x": {1, 2, 3}}})


# ===========================================================================
# Offline GATING tier — real headless Chrome.
# ===========================================================================
def _chrome_version_from_ua(ua: str) -> str | None:
    m = re.search(r"(?:Headless)?Chrome/([\d.]+)", ua or "")
    return m.group(1) if m else None


# ===========================================================================
# F-770 — the four User-Agent leak vectors (plan_RELEASE_FIX_D D0).
#
# "the UA leaks" is not precise enough to fix: a pre-launch ``--user-agent=``
# flag and a post-launch ``Emulation.setUserAgentOverride`` cover DIFFERENT
# subsets of the surface, so each vector is measured separately on every OS cell
# BEFORE a masking mechanism is chosen. (RELEASE-FIX-C shipped on a strongly
# evidenced but unmeasured hypothesis and was wrong; D0 exists so FIX-D cannot
# repeat that.)
#
#   V1  navigator.userAgent                       page
#   V2  the HTTP ``User-Agent`` request header    wire — what the fixture server
#       the fixture server actually received           READ, not what the page says
#   V3  navigator.userAgentData brands +          page — built from Chrome's own
#       getHighEntropyValues()                         version info
#   V4  Browser.getVersion() -> userAgent         CDP
#
# The table is a MEASUREMENT, not a judgement: :func:`f770_vector_readings`
# records each vector's raw value and whether it contains the ``HeadlessChrome``
# token. The gating verdict lives in :data:`GATE_SIGNALS`.
# ===========================================================================
F770_VECTORS: tuple[str, ...] = (
    "V1_navigator_user_agent",
    "V2_http_request_header",
    "V3_ua_client_hints",
    "V4_cdp_browser_get_version",
)

_HEADLESS_TOKEN = "headlesschrome"


def _leaks_headless(value: object) -> bool:
    """True when the serialized reading contains the ``HeadlessChrome`` token."""
    return _HEADLESS_TOKEN in json.dumps(value, default=str).lower()


def f770_vector_readings(probe: dict) -> dict[str, dict]:
    """Measure the four F-770 UA vectors on one collected probe.

    Returns ``{vector: {"value": <raw reading>, "leaks": bool}}``. Pure — it
    judges nothing beyond "does this reading contain the token".
    """
    obs = probe["observations"]
    readings: dict[str, object] = {
        "V1_navigator_user_agent": obs.get("user_agent"),
        "V2_http_request_header": obs.get("http_user_agent"),
        "V3_ua_client_hints": {
            "brands": obs.get("ua_client_hints_brands"),
            "high_entropy": obs.get("ua_client_hints_high_entropy"),
            "sec_ch_ua_header": (obs.get("http_request_headers") or {}).get("sec-ch-ua")
            if isinstance(obs.get("http_request_headers"), dict)
            else None,
        },
        "V4_cdp_browser_get_version": probe.get("cdp_user_agent"),
    }
    return {
        name: {"value": value, "leaks": _leaks_headless(value)}
        for name, value in readings.items()
    }


def _write_artifact(product: dict, control: dict, predicate_outcomes: dict, path):
    """Write the redacted result artifact: schema version, OS/arch, exact Chrome
    identity, raw observations, predicate outcomes, and control outcomes. No
    secrets and no local profile contents (cmdline/profile paths are NOT included;
    only the binary identity and derived booleans)."""
    obs = product["observations"]
    artifact = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "os": _os_family(),
        "architecture": _platform.machine(),
        "chrome": {
            "binary": product["binary"],
            "version": _chrome_version_from_ua(obs.get("user_agent", "")),
        },
        "headless": product["headless"],
        "observations": obs,
        "predicate_outcomes": predicate_outcomes,
        "control_outcomes": {
            "vanilla_webdriver": control["observations"].get("webdriver"),
            "vanilla_detected": _p_webdriver_absent(
                control["observations"], _os_family()
            )
            is False,
        },
        # F-770 D0: the per-vector UA measurement for BOTH browsers. The control's
        # readings are what an unmasked headless Chrome emits on this exact cell,
        # so the pair is also the differential that proves the fix is real.
        "f770_ua_vectors": {
            "product": f770_vector_readings(product),
            "control": f770_vector_readings(control),
        },
        "cdp_transcript": [e["method"] for e in product["transcript"]],
    }
    json.dumps(artifact)  # must be serializable
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    return artifact


def test_product_offline_stealth_gate(product_probe, control_probe, tmp_path):
    """Product passes every gating predicate; schema/transcript/process evidence and
    the vanilla-control sensitivity all validate; a redacted artifact is written."""
    os_family = _os_family()
    product = product_probe
    obs = product["observations"]

    # Closed-schema validation of the real published result.
    validate_result_schema(product["result"])

    # Armed-until-released + exactly one completion publication (no duplicate).
    assert product["armed_before_start"] is True, "probe collected before release"
    assert product["event_count"] == 1, product["event_count"]

    # Ordered CDP prerequisite transcript: exact order, all successful.
    methods = tuple(e["method"] for e in product["transcript"])
    assert methods == CDP_PREREQUISITE_METHODS, methods
    assert all(e.get("ok") for e in product["transcript"]), product["transcript"]

    # Every applicable gating predicate must PASS on the product.
    predicate_outcomes: dict[str, bool] = {}
    failures = []
    for sig in GATE_SIGNALS:
        if not sig.applies(os_family):
            continue
        ok = sig.predicate(obs, os_family)
        predicate_outcomes[sig.name] = ok
        if not ok:
            failures.append((sig.name, {k: obs.get(k) for k in sig.obs_keys}))
    assert not failures, f"product failed gating stealth predicate(s): {failures}"

    # Process-flag evidence: the launched Chrome must not carry automation-revealing
    # flags, and its binary is W2's exact resolved executable.
    cmdline = product["cmdline"]
    assert cmdline, "no Chrome command line captured for process evidence"
    assert cmdline[0] == product["binary"], (cmdline[0], product["binary"])
    for forbidden_flag in ("--enable-automation", "--test-type"):
        assert not any(a == forbidden_flag for a in cmdline), (forbidden_flag, cmdline)

    # F-770 vector V4 — the CDP-level User-Agent. Not a page observation, so it is
    # asserted here rather than through the predicate table. The masked
    # --user-agent= flag is process-wide, so it reaches this value too.
    cdp_ua = product["cdp_user_agent"]
    assert isinstance(cdp_ua, str) and "headlesschrome" not in cdp_ua.lower(), cdp_ua

    # Vanilla-control sensitivity: the deliberately non-stealth spawn (same product
    # path, stealth arg-filter neutralized) MUST be detected. If it is NOT detected,
    # the probe is worthless — fail. Config identity: same binary, same headless
    # mode; the only intentional difference is --enable-automation.
    control = control_probe
    cobs = control["observations"]
    assert _p_webdriver_absent(cobs, os_family) is False, (
        "vanilla control was NOT detected (navigator.webdriver not true) — "
        "the probe has no teeth against a real non-stealth browser"
    )
    assert control["binary"] == product["binary"], "control/product binary identity"
    assert control["headless"] == product["headless"], "control/product headless parity"
    assert any(a == "--enable-automation" for a in control["cmdline"]), (
        "control must carry the intentional automation treatment"
    )
    # Config identity also covers the ordered CDP sequence: the control ran the
    # same armed-then-released prerequisite transcript before collection (§2.4:
    # "for both the MCP browser and vanilla control, execute and assert this exact
    # successful method sequence before release").
    assert control["armed_before_start"] is True, "control probe collected pre-release"
    control_methods = tuple(e["method"] for e in control["transcript"])
    assert control_methods == CDP_PREREQUISITE_METHODS, control_methods
    assert all(e.get("ok") for e in control["transcript"]), control["transcript"]

    # Redacted result artifact (validates on re-read; contains no secrets/profile).
    # NOT a ``STEALTH_MCP_*`` name on purpose: ``settings._reject_unknown_prefixed_env``
    # fails ``get_settings()`` for any unknown key in that namespace, so setting
    # ``STEALTH_MCP_STEALTH_ARTIFACT_DIR`` (this knob's original W4 name) would
    # detonate the whole backend the moment CI exported it. This is a test-only
    # artifact path and deliberately lives outside the product's env namespace.
    artifact_dir = os.environ.get("STEALTH_PROBE_ARTIFACT_DIR")
    out_dir = tmp_path if not artifact_dir else Path(artifact_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = out_dir / "stealth_probe_result_v1.json"
    artifact = _write_artifact(product, control, predicate_outcomes, artifact_path)
    reloaded = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert reloaded["schema_version"] == RESULT_SCHEMA_VERSION
    assert reloaded["control_outcomes"]["vanilla_detected"] is True
    assert reloaded["chrome"]["binary"] == product["binary"]
    # No profile path / secret leaked into the artifact.
    blob = json.dumps(artifact)
    assert "user-data-dir" not in blob and "user_data_dir" not in blob


def _format_vector_table(label: str, readings: dict[str, dict]) -> str:
    rows = [f"[F-770:{label}] {_os_family()}/{_platform.machine()}"]
    for name in F770_VECTORS:
        entry = readings[name]
        verdict = "LEAK" if entry["leaks"] else "clean"
        rows.append(f"  {name:<28} {verdict:<5} {json.dumps(entry['value'])[:220]}")
    return "\n".join(rows)


def test_f770_ua_vectors_are_measured(product_probe, control_probe):
    """F-770 D0 (plan_RELEASE_FIX_D §2) — MEASURE all four UA vectors, judge none.

    This test's contract is that every vector is actually OBSERVABLE on this cell:
    a vector that silently stops being collected would make the F-770 gating
    signals vacuous without turning anything red. It deliberately asserts nothing
    about whether a vector leaks — that verdict belongs to :data:`GATE_SIGNALS`
    (and, before RELEASE-FIX-D, to the strict xfail). The measured table is
    printed and written into the release artifact for every OS cell.
    """
    for label, probe in (("product", product_probe), ("control", control_probe)):
        readings = f770_vector_readings(probe)
        print(_format_vector_table(label, readings))

        v1 = readings["V1_navigator_user_agent"]["value"]
        assert isinstance(v1, str) and "Mozilla/5.0" in v1, v1

        v2 = readings["V2_http_request_header"]["value"]
        assert isinstance(v2, str) and not v2.startswith("ERROR:"), (
            f"{label}: the fixture server never reported a User-Agent header "
            f"(V2 unmeasured, so a wire-level leak could not be detected): {v2!r}"
        )
        assert "Mozilla/5.0" in v2, v2

        v3 = readings["V3_ua_client_hints"]["value"]
        assert v3["brands"], f"{label}: userAgentData.brands unmeasured: {v3!r}"
        assert isinstance(v3["high_entropy"], dict), (
            f"{label}: getHighEntropyValues unmeasured: {v3['high_entropy']!r}"
        )
        # ``brands`` is the part the V3 coherence gate reads and it is populated
        # on both browsers. Whether the rest of the high-entropy set is populated
        # is F-774's subject, not a measurability question — asserting it here
        # would be judging, which this test deliberately does not do.
        assert v3["high_entropy"].get("brands"), v3["high_entropy"]
        assert v3["sec_ch_ua_header"], (
            f"{label}: the fixture server saw no sec-ch-ua header: {v3!r}"
        )

        v4 = readings["V4_cdp_browser_get_version"]["value"]
        assert isinstance(v4, str) and "Mozilla/5.0" in v4, v4


@pytest.mark.xfail(
    strict=True,
    reason="F-774: Chrome blanks the high-entropy UA client hints (architecture, "
    "bitness, platformVersion, uaFullVersion, fullVersionList) whenever a "
    "--user-agent override is active, which is how the F-770 mask works. The "
    "low-entropy sec-ch-ua* headers on the wire stay correct. Recorded honestly, "
    "not papered over: fixing it needs Emulation.setUserAgentOverride's "
    "userAgentMetadata, a per-target mechanism outside RELEASE-FIX-D's scope.",
)
def test_product_ua_client_hints_high_entropy_pinned_gap(product_probe):
    """PINNED GAP (F-774). This assertion is what a fully-coherent masked browser
    WOULD satisfy. It fails today, so the strict xfail passes and records the
    gap — no probe is weakened, and a future fix turns the suite red."""
    obs = product_probe["observations"]
    assert _p_ua_client_hints_high_entropy_populated(obs, _os_family()) is True, (
        obs.get("ua_client_hints_high_entropy")
    )


def test_f770_a_later_tab_is_covered(ua_default_facts):
    """F-770: a tab created AFTER spawn is masked too, page and wire.

    A per-target CDP override would satisfy the spawn tab and leak on the next
    one (the failure mode that made FIX-C's ``create_hook`` re-arm necessary).
    This is what makes the launch-flag mechanism the right one, so it is pinned.
    """
    facts = ua_default_facts
    assert "HeadlessChrome" not in facts["spawn_tab_ua"], facts["spawn_tab_ua"]
    assert "HeadlessChrome" not in facts["new_tab_ua"], facts["new_tab_ua"]
    assert facts["new_tab_ua"] == facts["spawn_tab_ua"], facts
    assert "HeadlessChrome" not in facts["new_tab_http_ua"], facts["new_tab_http_ua"]
    assert facts["new_tab_http_ua"] == facts["new_tab_ua"], facts


def test_f770_explicit_user_agent_still_wins(ua_explicit_facts):
    """F-770: the masked default NEVER overrides a caller-supplied user_agent."""
    facts = ua_explicit_facts
    assert facts["spawn_tab_ua"] == _EXPLICIT_UA, facts["spawn_tab_ua"]
    assert facts["new_tab_ua"] == _EXPLICIT_UA, facts["new_tab_ua"]
    assert facts["new_tab_http_ua"] == _EXPLICIT_UA, facts["new_tab_http_ua"]


def test_f770_product_masks_ua_while_control_still_leaks(product_probe, control_probe):
    """F-770 D2 — the fix is real AND the suite that judges it is still sensitive.

    W4's stealth suite is only worth anything because a deliberately non-stealth
    control still FAILS the probes. The control is built by neutralizing
    ``platform_utils.merge_browser_args``, which is exactly where FIX-D's masked
    default ``--user-agent`` is applied — so the control genuinely does not get
    it, and this differential proves that rather than assuming it:

    * the control's User-Agent STILL advertises HeadlessChrome (so
      ``vanilla_detected`` is not quietly going false, and the whole suite has
      not become vacuous);
    * the product's User-Agent is the control's with exactly the
      ``HeadlessChrome`` token replaced by ``Chrome`` — byte-identical
      otherwise. That equality is the coherence contract: the mask is Chrome's
      own reduced User-Agent for this binary, not a plausible-looking string
      that a platform-token or version mismatch would betray.
    """
    control_ua = control_probe["observations"]["user_agent"]
    product_ua = product_probe["observations"]["user_agent"]

    assert "HeadlessChrome" in control_ua, (
        "the vanilla control no longer leaks the headless token — the masking was "
        f"applied outside the seam the control neutralizes: {control_ua!r}"
    )
    assert control_ua.replace("HeadlessChrome", "Chrome") == product_ua, (
        f"masked UA is not the browser's own UA minus the headless token\n"
        f"  control: {control_ua!r}\n  product: {product_ua!r}"
    )
    # ...and the same differential holds on the wire, not just in the page.
    assert "HeadlessChrome" in control_probe["observations"]["http_user_agent"]
    assert (
        control_probe["observations"]["http_user_agent"].replace(
            "HeadlessChrome", "Chrome"
        )
        == product_probe["observations"]["http_user_agent"]
    )


# ===========================================================================
# F-806 — the masked User-Agent must describe the browser that ACTUALLY launched.
#
# The mask is built from a version probed BEFORE launch. Chrome auto-updates in
# place, so under a long-lived backend the version the mask was built from and
# the version that launches can differ — and then the UA claims Chrome/150 while
# the browser's own ``sec-ch-ua`` says 151. That is a self-contradicting UA:
# strictly a WORSE tell than the headless token the mask exists to remove. It
# turned the 2.0.1 macOS gate red (CI run 30607810053).
#
# The invariant below is what the skew broke, and it is pinned against a real
# browser because both halves of it are real-browser facts: the flag Chrome
# actually launched with, and the version Chrome actually is.
# ===========================================================================
def test_f806_masked_ua_major_is_the_launched_browsers_major(ua_default_facts):
    """F-806: the masked UA's Chrome major == the running browser's own major.

    ``Browser.getVersion().product`` is the browser reporting itself, so this is
    an equality between the mask and its subject — not between two readings of
    the same string.

    The fixture clears the version memo before its spawn, so what is measured
    here is the PRE-LAUNCH PROBE's answer — the first spawn of a fresh backend —
    and not a value the warmup spawn already reconciled. That is the spawn the
    Windows directory-scan probe used to get wrong. It still cannot fail on a
    machine whose Chrome is not mid-update; the teeth are in the node below and
    in ``TestWindowsProbeReadsTheBinaryNotItsNeighbours``.
    """
    facts = ua_default_facts
    ua_major = _ua_major(facts["spawn_tab_ua"])
    product_major = _ua_major(facts["browser_product"])
    print(
        f"[F-806] {_os_family()}/{_platform.machine()} "
        f"masked UA major={ua_major} product={facts['browser_product']!r}"
    )
    assert ua_major, f"no Chrome major in the masked User-Agent: {facts!r}"
    assert product_major, f"Browser.getVersion reported no version: {facts!r}"
    assert ua_major == product_major, (
        "the masked User-Agent describes a different browser than the one "
        "running it (F-806) — a UA that contradicts its own sec-ch-ua is a "
        f"sharper tell than the headless token the mask removes\n"
        f"  masked UA: {facts['spawn_tab_ua']!r} (major {ua_major})\n"
        f"  launched : {facts['browser_product']!r} (major {product_major})"
    )


async def test_f806_a_stale_version_memo_is_repaired_by_the_launched_browser(
    monkeypatch, ua_default_facts
):
    """F-806: reproduce the skew against a live browser, then pin the repair.

    The test above cannot fail on a machine whose Chrome did not update mid-run,
    so on its own it would have no teeth against either defense being deleted.
    This one supplies them by forcing the state an in-place upgrade produces —
    a pre-launch probe answering with a major the browser no longer has — and
    then requiring three things of a real spawn:

    1. the skew is genuinely reachable: a stale probe really does put a wrong
       major on the wire, so the invariant above is guarding something;
    2. ``Browser.getVersion().product`` still reports the REAL version even
       though this browser launched under a wrong ``--user-agent=``. The whole
       reconciliation rests on that, and here it is measured rather than cited;
    3. the post-launch reconciliation wrote the truth back, so the NEXT spawn
       masks correctly — the memo now answers with the launched major even
       though the (still-patched) probe would answer stale.
    """
    from stealth_chrome_devtools_mcp.embedded import platform_utils as _pu

    real_major = _ua_major(ua_default_facts["browser_product"])
    assert real_major, ua_default_facts
    # A stale major is one the machine has genuinely left behind — the shape an
    # in-place upgrade leaves in a path-keyed memo.
    stale_major = str(int(real_major) - 1)

    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    bm = server_mod.browser_manager

    _pu.reset_browser_version_memo()
    monkeypatch.setattr(_pu, "_probe_browser_major_version", lambda _exe: stale_major)
    iid = None
    try:
        spawned = await spawn(headless=True, **sandbox_kwargs())
        iid = spawned["instance_id"]
        tab = await bm.get_tab(iid)
        skewed_ua = await _read_tab_ua(tab)
        launched_product = await _read_browser_product(tab)
        print(
            f"[F-806:forced-stale] masked UA major={_ua_major(skewed_ua)} "
            f"(forced {stale_major}) product={launched_product!r}"
        )

        assert _ua_major(skewed_ua) == stale_major, (
            "a stale version could not be forced onto the wire, so this test is "
            f"not exercising the F-806 mechanism: {skewed_ua!r}"
        )
        assert _ua_major(launched_product) == real_major, (
            "Browser.getVersion().product followed the --user-agent= override, "
            "which would make it useless as the reconciliation's yardstick "
            f"(F-806): {launched_product!r}"
        )
        assert _pu.resolve_browser_major_version(_pu.check_browser_executable()) == (
            real_major
        ), (
            "the post-launch reconciliation did not write the launched browser's "
            "version back, so every later spawn would keep masking as "
            f"{stale_major} while running {real_major} (F-806)"
        )
    finally:
        if iid is not None:
            try:
                await close(instance_id=iid)
            except Exception:  # teardown best-effort
                pass
        # The memo is process-global: leaving a forced value in it would leak
        # into any later test that builds a User-Agent.
        _pu.reset_browser_version_memo()


# ===========================================================================
# Online / informational tier (NON-GATING) — marker ``online``.
# Excluded from every default run and the release gate (``-m "... and not online"``).
# Asserts ONLY hard invariants; logs the rest; tolerates network flakiness.
# ===========================================================================
async def _drive_online(url: str) -> dict:
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    bm = server_mod.browser_manager
    spawned = await spawn(headless=True, **sandbox_kwargs())
    iid = spawned["instance_id"]
    try:
        try:
            await navigate_and_settle(iid, url, timeout=30.0)
        except Exception as exc:
            pytest.skip(f"online detector unreachable ({url}): {exc}")
        tab = await bm.get_tab(iid)
        await tab.send(uc.cdp.runtime.enable())
        raw = await tab.send(
            uc.cdp.runtime.evaluate(
                expression=(
                    "JSON.stringify({webdriver: navigator.webdriver, "
                    "cdc_window: Object.getOwnPropertyNames(window)"
                    ".filter(k=>/^cdc_|^\\$cdc|webdriver|selenium/i.test(k)), "
                    "ua: navigator.userAgent})"
                ),
                return_by_value=True,
                await_promise=True,
            )
        )
        return json.loads(raw[0].value)
    finally:
        try:
            await close(instance_id=iid)
        except Exception:  # teardown best-effort
            pass


@pytest.mark.online
@pytest.mark.parametrize(
    "url",
    [
        "https://abrahamjuliot.github.io/creepjs/",
        "https://bot.incolumitas.com/",
    ],
    ids=["creepjs", "incolumitas"],
)
async def test_online_detector_hard_invariants(url):
    """Non-gating live observation: assert only the hard invariants (webdriver false,
    no CDP-leak globals); log the rest. Never fails on a vendor detector score."""
    if not CAN_RUN:
        pytest.skip("Chrome not available or server failed to load")
    signals = await _drive_online(url)
    # Hard invariants only — the same ones the offline tier gates on.
    assert signals.get("webdriver") is False, f"{url}: webdriver leaked"
    assert signals.get("cdc_window") == [], f"{url}: CDP-leak global(s) present"
    # Everything else is informational; surface it for human review, never gate.
    print(f"[online:{url}] informational: ua={signals.get('ua')!r}")
