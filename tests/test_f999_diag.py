"""TEMPORARY F-999 diagnostic (removed before merge): repeat MQ-118's CORS step on
a fresh instance and report, per iteration, what the page, Chrome's network
layer (raw CDP Network events) and the fixture server (accepts + request lines)
each saw."""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

import release_gate_harness as rgh
from e2e_helpers import (
    eval_js,
    fixture_ledger,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    reset_fixture_ledger,
    sandbox_kwargs,
    wait_for_js,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded import google_rotation_guard

# Round 9 is macOS only: Windows was clean in every round, Linux finished in round 8 (4 bad of 150
# armed runs, too few to settle) and macOS is where the flake is common (5-7% of armed runs).
pytestmark = [
    integration_pytestmark(),
    pytest.mark.skipif(
        sys.platform != "darwin", reason="F-999 diag round 9: macOS only"
    ),
]

ITERATIONS = 24  # instances per test; 18 tests x 24 instances rotate 4 arms: 108 instances x 2 runs = 216 runs per arm
RUNS_PER_INSTANCE = 2
CHUNKS = 18
# NOGUARD: no guard. GUARD: the guard as shipped. NOFETCH: the guard, but Fetch.enable is never
# sent to any target (browser-level + nested auto-attach and the resume are unchanged). CONN: the
# guard's browser-level connection is open and sends NOTHING (round 8; dropped in round 9, it was
# clean on Linux). BLOCKNET: the guard, but each
# session gets Network.enable + Network.setBlockedURLs (no Fetch interception) - also the candidate fix.
ARMS = ("NOGUARD", "GUARD", "NOFETCH", "BLOCKNET")
PROBE_ARMS = ("GUARD", "BLOCKNET")  # the arms that claim to block the rotation endpoint
_REAL_ENABLED = (
    google_rotation_guard.enabled
)  # captured at import, before any arm patches it
ARM = "GUARD"  # the arm of the instance being run; read by the traced guard handler
CRASHES: list[
    tuple
] = []  # (t, text) every Inspector.targetCrashed on the guard connection
ERRS: list[str] = []  # guard-connection replies that carried an error
_NET_METHODS = (
    "Network.requestWillBeSent",
    "Network.requestWillBeSentExtraInfo",
    "Network.responseReceived",
    "Network.responseReceivedExtraInfo",
    "Network.loadingFailed",
    "Network.loadingFinished",
    "Network.requestServedFromCache",
)

NET: list[tuple] = []  # (t, method, requestId, summary)
RAW: list[
    tuple
] = []  # (t, method, requestId, full params JSON) every event of a RotateCookies probe request
_probe_ids: set[str] = set()
SERVER: list[tuple] = []  # (t, kind, server_port, detail)
OTHER: list[tuple] = []  # (t, "EVT"/"SEND", name, {}) every other CDP event / command
GT: list[
    tuple
] = []  # (t, text) every guard-connection command/event since the instance spawn
SESSIONS: dict[str, str] = {}  # guard sessionId -> "type url targetId parent waiting"
_cors_ids: set[str] = set()
_installed = False


def _install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    from nodriver.cdp import util as cdp_util

    parse = cdp_util.parse_json_event

    def spied(message):
        try:
            method = message.get("method", "") if isinstance(message, dict) else ""
            if method == "Page.lifecycleEvent":
                p = message.get("params", {})
                OTHER.append(
                    (
                        time.monotonic(),
                        "EVT",
                        f"Page.lifecycleEvent name={p.get('name')} frame={str(p.get('frameId'))[:8]}"
                        f" loader={str(p.get('loaderId'))[:8]} ts={p.get('timestamp')}",
                        {},
                    )
                )
            elif method and method not in _NET_METHODS:
                OTHER.append((time.monotonic(), "EVT", method, {}))
            if method in _NET_METHODS:
                p = message.get("params", {})
                rid = p.get("requestId")
                url = (p.get("request") or {}).get("url") or (
                    p.get("response") or {}
                ).get("url")
                if url and ("/cors/" in url or "RotateCookies" in url):
                    _cors_ids.add(rid)
                if url and "RotateCookies" in url:
                    _probe_ids.add(rid)
                if rid in _probe_ids:
                    RAW.append(
                        (
                            time.monotonic(),
                            method.split(".")[1],
                            rid,
                            json.dumps(p)[:700],
                        )
                    )
                if rid in _cors_ids:
                    NET.append(
                        (
                            time.monotonic(),
                            method.split(".")[1],
                            rid,
                            {
                                k: v
                                for k, v in {
                                    "url": url,
                                    "type": p.get("type"),
                                    "method": (p.get("request") or {}).get("method"),
                                    "status": (p.get("response") or {}).get("status"),
                                    "errorText": p.get("errorText"),
                                    "blockedReason": p.get("blockedReason"),
                                    "cors": p.get("corsErrorStatus"),
                                    "canceled": p.get("canceled"),
                                    "reused": (p.get("response") or {}).get(
                                        "connectionReused"
                                    ),
                                    "connId": (p.get("response") or {}).get(
                                        "connectionId"
                                    ),
                                    "protocol": (p.get("response") or {}).get(
                                        "protocol"
                                    ),
                                    "fromCache": (p.get("response") or {}).get(
                                        "fromDiskCache"
                                    ),
                                    "blockedCookies": p.get("blockedCookies"),
                                    "chromeTs": p.get("timestamp"),
                                }.items()
                                if v not in (None, [], "")
                            },
                        )
                    )
        except Exception:  # noqa: BLE001  PERMANENT(a temporary diagnostic spy must never break parsing)
            pass
        return parse(message)

    cdp_util.parse_json_event = spied

    from nodriver.core import connection as nd_conn

    send = nd_conn.Connection.send

    async def traced_send(self, cdp_obj, _is_update=False):
        code = getattr(cdp_obj, "gi_code", None)
        if code is not None:
            mod = code.co_filename.replace("\\", "/").rsplit("/", 1)[-1][:-3]
            OTHER.append((time.monotonic(), "SEND", f"{mod}.{code.co_name}", {}))
        return await send(self, cdp_obj, _is_update)

    type.__setattr__(nd_conn.Connection, "send", traced_send)

    guard_send = google_rotation_guard.RotationGuard._send
    guard_handle = google_rotation_guard.RotationGuard._handle

    async def traced_guard_send(self, method, params=None, session_id=None):
        suppressed = ARM == "CONN" or (ARM == "NOFETCH" and method == "Fetch.enable")
        tag = "GSEND-SUPPRESSED" if suppressed else "GSEND"
        OTHER.append(
            (time.monotonic(), tag, f"{method} session={str(session_id)[:8]}", {})
        )
        GT.append((time.monotonic(), f"{tag} {method} session={str(session_id)[:8]}"))
        if ARM == "CONN":
            self._settled.set()  # nothing is outstanding: do not let start() wait out its bound
            return None
        if ARM == "NOFETCH" and method == "Fetch.enable":
            return None
        if ARM == "BLOCKNET" and method == "Fetch.enable":
            await guard_send(self, "Network.enable", None, session_id)
            return await guard_send(
                self,
                "Network.setBlockedURLs",
                {"urls": list(self._patterns)},
                session_id,
            )
        return await guard_send(self, method, params, session_id)

    async def traced_guard_handle(self, message):
        method = message.get("method")
        params = message.get("params") or {}
        sid = str(message.get("sessionId"))[:8]
        label = (
            method or f"reply id={message.get('id')}"
        ) + f" session={sid}{' ERROR' if 'error' in message else ''}"
        if "error" in message:
            ERRS.append(json.dumps(message["error"])[:120])
        if method == "Target.attachedToTarget":
            info = params.get("targetInfo") or {}
            desc = (
                f"type={info.get('type')} url={str(info.get('url'))[:90]!r} "
                f"targetId={str(info.get('targetId'))[:8]} parent={sid} "
                f"waiting={params.get('waitingForDebugger')}"
            )
            SESSIONS[str(params.get("sessionId"))[:8]] = desc
            label += f" -> new session={str(params.get('sessionId'))[:8]} {desc}"
        elif method == "Target.detachedFromTarget":
            label += f" -> detached session={str(params.get('sessionId'))[:8]}"
        elif method == "Inspector.targetCrashed":
            label += f" -> CRASHED {SESSIONS.get(sid, 'UNMAPPED')}"
            CRASHES.append((time.monotonic(), SESSIONS.get(sid, "UNMAPPED")))
        OTHER.append((time.monotonic(), "GEVT", label, {}))
        GT.append((time.monotonic(), f"GEVT {label}"))
        return await guard_handle(self, message)

    google_rotation_guard.RotationGuard._send = traced_guard_send
    google_rotation_guard.RotationGuard._handle = traced_guard_handle

    handle = rgh._FixtureHandler.handle_one_request

    def traced_handle(self):
        t0 = time.monotonic()
        err = None
        try:
            return handle(self)
        except BaseException as exc:  # noqa: BLE001  PERMANENT(temporary diagnostic re-raises)
            err = repr(exc)
            raise
        finally:
            line = (
                (getattr(self, "raw_requestline", b"") or b"").decode("latin-1").strip()
            )
            SERVER.append(
                (
                    t0,
                    "request",
                    self.server.server_address[1],
                    f"{line!r} client={self.client_address[1]} took={time.monotonic() - t0:.3f}s"
                    f" close={self.close_connection} err={err}",
                )
            )

    rgh._FixtureHandler.handle_one_request = traced_handle

    process = rgh._FixtureServer.process_request

    def traced_process(self, request, client_address):
        SERVER.append(
            (
                time.monotonic(),
                "accept",
                self.server_address[1],
                f"client={client_address[1]}",
            )
        )
        return process(self, request, client_address)

    rgh._FixtureServer.process_request = traced_process


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    _install()
    yield


def _fmt(t0, rows, port_b):
    out = []
    for r in rows:
        if r[0] < t0:
            continue
        if r[1] in ("EVT", "SEND", "GSEND", "GSEND-SUPPRESSED", "GEVT"):
            out.append(f"    +{r[0] - t0:.3f}s {r[1]} {r[2]}")
        elif len(r) == 4 and isinstance(r[3], dict):
            out.append(
                f"    +{r[0] - t0:.3f}s NET {r[1]} {r[2]} {json.dumps(r[3], sort_keys=True)}"
            )
        elif r[2] == port_b:
            out.append(f"    +{r[0] - t0:.3f}s SRV {r[1]} {r[3]}")
    return out


def _crash_text(spawn_t, rows):
    return [f"+{t - spawn_t:.3f}s {desc.split(' parent=')[0]}" for t, desc in rows]


async def _loop(fixture_origin_pair, chunk, capfd):
    global ARM
    origin_a, origin_b = fixture_origin_pair
    port_b = int(origin_b.rsplit(":", 1)[1])
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    report: list[str] = []
    stats = {
        a: {
            "runs": 0,
            "bad": 0,
            "crash_runs": 0,
            "bad_crash": 0,
            "reply_err": 0,
            "probe": 0,
            "probe_blocked": 0,
        }
        for a in ARMS
    }
    # A broken arm must fail loudly here, not by design at the end.
    assert google_rotation_guard.enabled is _REAL_ENABLED, "enabled left patched"
    try:
        for i in range(ITERATIONS):
            # Four arms rotate per instance (chunk shifts the start) so no arm owns an
            # early or late stretch of the run.
            label = ARMS[(i + chunk) % len(ARMS)]
            ARM = label
            google_rotation_guard.enabled = (
                (lambda: False) if label == "NOGUARD" else _REAL_ENABLED
            )
            GT.clear()
            SESSIONS.clear()
            CRASHES.clear()
            ERRS.clear()
            spawn_t = time.monotonic()
            iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
            guards = len(google_rotation_guard._LIVE)
            assert guards == (0 if label == "NOGUARD" else 1), (label, guards)
            try:
                await reset_fixture_ledger(origin_a)
                await navigate_and_settle(iid, f"{origin_a}/dynamic_probe.html")
                await eval_js(
                    iid,
                    "(function(){window.__f=[];var t0=performance.now();"
                    "function L(n){return function(){window.__f.push([n,Math.round(performance.now()-t0),"
                    "document.visibilityState,document.readyState])}}"
                    "['pagehide','pageshow','freeze','resume','visibilitychange','beforeunload','unload']"
                    ".forEach(function(n){window.addEventListener(n,L(n))});"
                    "document.addEventListener('visibilitychange',L('doc-vis'));return 1})()",
                )
                await eval_js(iid, "window.__auth.run()")
                await wait_for_js(iid, "window.__auth.done", True, timeout=15.0)
                for j in range(RUNS_PER_INSTANCE):
                    await reset_fixture_ledger(origin_b)
                    await eval_js(iid, "window.__cors.done = false")
                    t0 = time.monotonic()
                    await eval_js(iid, "window.__cors.run()")
                    await wait_for_js(iid, "window.__cors.done", True, timeout=15.0)
                    took = time.monotonic() - t0
                    cors = json.loads(
                        await eval_js(iid, "JSON.stringify(window.__cors)")
                    )
                    res = await eval_js(
                        iid,
                        "JSON.stringify(performance.getEntriesByType('resource')"
                        ".filter(function(e){return e.name.indexOf('/cors/')>=0})"
                        ".map(function(e){return [e.name.split('/cors/')[1], Math.round(e.startTime),"
                        " Math.round(e.duration), e.responseStatus, e.transferSize]}))",
                    )
                    led = await fixture_ledger(origin_b)
                    ok = cors.get("echo_status") == 200 and led["cors_methods"] == [
                        "OPTIONS",
                        "POST",
                        "GET-blocked",
                    ]
                    crashed = bool(CRASHES)
                    st = stats[label]
                    st["runs"] += 1
                    st["bad"] += 0 if ok else 1
                    st["crash_runs"] += 1 if crashed else 0
                    st["bad_crash"] += 1 if (crashed and not ok) else 0
                    lines = [
                        f"RUN {label} guards={len(google_rotation_guard._LIVE)} iter={i} run={j} ok={ok}"
                        f" took={took:.3f}s crashes={_crash_text(spawn_t, CRASHES)}"
                        f" cors_methods={led['cors_methods']} resources={res}"
                    ]
                    if not ok:
                        lines += _fmt(
                            t0, sorted(NET + SERVER + OTHER, key=lambda r: r[0]), port_b
                        )
                        lines.append(
                            f"    GUARD-CONNECTION TRACE since spawn (cors t0 = +{t0 - spawn_t:.3f}s):"
                        )
                        lines += [f"      +{g[0] - spawn_t:.3f}s {g[1]}" for g in GT]
                    report.append("\n".join(lines))
                probe = ""
                if label in PROBE_ARMS:
                    # Does this arm really stop a rotation request? Only the arms that claim to.
                    pt = time.monotonic()
                    await eval_js(
                        iid,
                        "fetch('http://accounts.google.com/RotateCookies',{mode:'no-cors'}).catch(function(){})",
                    )
                    await asyncio.sleep(1.0)
                    rids = {
                        r[2]
                        for r in NET
                        if r[0] >= pt and "RotateCookies" in str(r[3].get("url"))
                    }
                    rows = [r for r in NET if r[0] >= pt and r[2] in rids]
                    raw = [(r[1], r[2], r[3]) for r in RAW if r[0] >= pt]
                    blocked = any(
                        "BLOCKED_BY_CLIENT" in str(r[3].get("errorText")) for r in rows
                    )
                    stats[label]["probe"] += 1
                    stats[label]["probe_blocked"] += 1 if blocked else 0
                    probe = f" probe_blocked={blocked} probe_rows={[(r[1], r[3].get('errorText')) for r in rows]}"
                    probe += f" probe_raw={raw}"
                stats[label]["reply_err"] += len(ERRS)
                await asyncio.sleep(0.5)
                report.append(
                    f"INSTANCE {label} iter={i} crashes_by_end={_crash_text(spawn_t, CRASHES)}"
                    f" reply_errors={ERRS[:3]}{probe}"
                )
            finally:
                await close(instance_id=iid)
    finally:
        google_rotation_guard.enabled = _REAL_ENABLED
        ARM = "GUARD"
    table = "\n".join(f"STATS {a} {json.dumps(stats[a])}" for a in ARMS)
    summary = "\n".join(report)
    # Streamed straight to the job log as the chunk ends: pytest prints a failing test's captured
    # output only at the end of the session, and round 8's macOS job was cancelled before that.
    with capfd.disabled():
        sys.stdout.write(f"\nF999-DIAG chunk={chunk}\n{table}\n{summary}\n")
        sys.stdout.flush()
    # Deliberately fails (the diag must never read as a pass); the data is already in the log.
    pytest.fail(f"F999-DIAG chunk={chunk}\n{table}")


@pytest.mark.parametrize("chunk", range(CHUNKS))
async def test_f999_cors_loop_interleaved(fixture_origin_pair, chunk, capfd):
    await _loop(fixture_origin_pair, chunk, capfd)
