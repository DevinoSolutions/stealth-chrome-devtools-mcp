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

Design honesty (release-claim integrity): one product predicate — the headless UA
still advertising ``HeadlessChrome`` — does NOT pass. It is pinned as a strict
``xfail`` (F-770), NOT hidden by weakening a probe. An xfailed invariant cannot
satisfy the stealth release claim; see :data:`XFAIL_SIGNALS` and the test docstring.

No ``src/`` edits: both browsers spawn through the project's own
``spawn_browser`` tool; the ordered transcript uses the project's own nodriver
``tab.send(uc.cdp.*)`` CDP seam; the fixture and env-isolation reuse W1's homes.
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
)

# ── xfail table: a KNOWN product stealth gap, pinned honestly (never weakened) ─
# F-770: under headless (the configuration the offline gate runs), the product's
# default User-Agent still advertises "HeadlessChrome". The product ships nodriver
# and does NOT mask the UA token, so this basic bot tell leaks. Pinned as a strict
# xfail — an xfailed invariant does NOT satisfy the stealth release claim.
XFAIL_SIGNALS: tuple[Signal, ...] = (
    Signal(
        "ua_no_headless_token",
        ("user_agent",),
        _p_ua_no_headless,
        {"user_agent": "Mozilla/5.0 ... HeadlessChrome/150.0.0.0 Safari/537.36"},
        "navigator.userAgent does not advertise HeadlessChrome.",
        finding_id="F-770",
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

        return {
            "result": result,
            "observations": result.get("observations", {}),
            "transcript": transcript,
            "armed_before_start": armed_before_start,
            "event_count": event_count,
            "cmdline": cmdline,
            "binary": binary,
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
        "platform": platform_value,
        "ua_client_hints_present": True,
        "ua_client_hints_brands": ["Chromium", "Google Chrome", "Not;A=Brand"],
        "fn_tostring_alert": "function alert() { [native code] }",
        "fn_tostring_meta": "function toString() { [native code] }",
        "automation_globals_window": [],
        "automation_globals_document": [],
        "outer_width": 1280,
        "outer_height": 800,
    }


def test_signal_table_is_reviewed_and_nonempty():
    """The predicate table exists, is versioned, and has unique signal names."""
    names = [s.name for s in (*GATE_SIGNALS, *XFAIL_SIGNALS)]
    assert len(names) == len(set(names)), "duplicate signal names"
    assert len(GATE_SIGNALS) >= 8, "expected a broad gating table"
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
    artifact_dir = os.environ.get("STEALTH_MCP_STEALTH_ARTIFACT_DIR")
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


@pytest.mark.xfail(
    strict=True,
    reason="F-770: headless product UA advertises HeadlessChrome; nodriver's default "
    "stealth does not mask the UA token. Pinned honestly — an xfailed invariant does "
    "NOT satisfy the stealth release claim (the headless UA vector remains detectable).",
)
def test_product_ua_headless_token_pinned_gap(product_probe):
    """PINNED GAP (F-770). The headless product still leaks 'HeadlessChrome' in its
    User-Agent. This assertion is what a fully-stealthy product WOULD satisfy; it
    fails today, so the strict xfail passes and records the gap without weakening
    any probe."""
    obs = product_probe["observations"]
    assert _p_ua_no_headless(obs, _os_family()) is True, obs.get("user_agent")


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
