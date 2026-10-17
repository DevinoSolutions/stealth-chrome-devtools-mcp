"""TEMPORARY F-999 diagnostic (removed before merge): repeat MQ-118's CORS step on
a fresh instance and report, per iteration, what the page, Chrome's network
layer (raw CDP Network events) and the fixture server (accepts + request lines)
each saw."""

from __future__ import annotations

import json
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

pytestmark = integration_pytestmark()

ITERATIONS = 25  # instances per test; 4 tests per arm x 25 x 2 runs = 200 runs per arm
RUNS_PER_INSTANCE = 2
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
SERVER: list[tuple] = []  # (t, kind, server_port, detail)
OTHER: list[tuple] = []  # (t, "EVT"/"SEND", name, {}) every other CDP event / command
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
                if url and "/cors/" in url:
                    _cors_ids.add(rid)
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
        OTHER.append(
            (time.monotonic(), "GSEND", f"{method} session={str(session_id)[:8]}", {})
        )
        return await guard_send(self, method, params, session_id)

    async def traced_guard_handle(self, message):
        OTHER.append(
            (
                time.monotonic(),
                "GEVT",
                f"{message.get('method') or ('reply id=' + str(message.get('id')))} "
                f"session={str(message.get('sessionId'))[:8]}"
                f"{' ERROR' if 'error' in message else ''}",
                {},
            )
        )
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
        if r[1] in ("EVT", "SEND", "GSEND", "GEVT"):
            out.append(f"    +{r[0] - t0:.3f}s {r[1]} {r[2]}")
        elif len(r) == 4 and isinstance(r[3], dict):
            out.append(
                f"    +{r[0] - t0:.3f}s NET {r[1]} {r[2]} {json.dumps(r[3], sort_keys=True)}"
            )
        elif r[2] == port_b:
            out.append(f"    +{r[0] - t0:.3f}s SRV {r[1]} {r[3]}")
    return out


async def _loop(fixture_origin_pair, label):
    origin_a, origin_b = fixture_origin_pair
    port_b = int(origin_b.rsplit(":", 1)[1])
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    report: list[str] = []
    bad = 0
    for i in range(ITERATIONS):
        iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
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
                done = await wait_for_js(iid, "window.__cors.done", True, timeout=15.0)
                took = time.monotonic() - t0
                cors = json.loads(await eval_js(iid, "JSON.stringify(window.__cors)"))
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
                if not ok:
                    bad += 1
                pagelog = await eval_js(iid, "JSON.stringify(window.__f)")
                lines = [
                    f"{label} guards={len(google_rotation_guard._LIVE)} iter={i} run={j} ok={ok} pagelog={pagelog} done={done} took={took:.3f}s "
                    f"cors={json.dumps(cors, sort_keys=True)} "
                    f"cors_methods={led['cors_methods']} resources={res}"
                ]
                if not ok:
                    lines += _fmt(
                        t0, sorted(NET + SERVER + OTHER, key=lambda r: r[0]), port_b
                    )
                report.append("\n".join(lines))
        finally:
            await close(instance_id=iid)
    summary = "\n".join(report)
    print(summary)
    assert bad == 0, f"F999-DIAG[{label}] {bad} bad of {len(report)}\n{summary}"


@pytest.mark.parametrize("chunk", range(4))
async def test_f999_cors_loop_guard_armed(fixture_origin_pair, chunk):
    await _loop(fixture_origin_pair, "GUARD")


@pytest.mark.parametrize("chunk", range(4))
async def test_f999_cors_loop_guard_disabled(fixture_origin_pair, monkeypatch, chunk):
    monkeypatch.setattr(google_rotation_guard, "enabled", lambda: False)
    await _loop(fixture_origin_pair, "NOGUARD")
