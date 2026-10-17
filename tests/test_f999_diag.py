"""TEMPORARY F-999 diagnostic (removed before merge): repeat MQ-118's CORS step on
a fresh instance and report, per iteration, what the page, Chrome's network
layer (raw CDP Network events) and the fixture server (accepts + request lines)
each saw."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

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

pytestmark = integration_pytestmark()

ITERATIONS = 30
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
            if method and method not in _NET_METHODS:
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

    nd_conn.Connection.send = traced_send

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


def _netlog_report(path: str, needle: str = "/cors/echo") -> list[str]:
    """The net-stack story of every request whose URL contains ``needle``."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"    NETLOG unreadable: {exc!r}"]
    try:
        doc = json.loads(raw)
    except ValueError:
        try:
            doc = json.loads(raw.rstrip().rstrip(",") + "]}")
        except ValueError as exc:
            return [f"    NETLOG unparseable ({len(raw)} bytes): {exc!r}"]
    const = doc.get("constants", {})
    types = {v: k for k, v in const.get("logEventTypes", {}).items()}
    stypes = {v: k for k, v in const.get("logSourceType", {}).items()}
    phases = {v: k for k, v in const.get("logEventPhase", {}).items()}
    by_src: dict[int, list[dict]] = {}
    for ev in doc.get("events", []):
        by_src.setdefault(ev["source"]["id"], []).append(ev)
    roots = [
        sid
        for sid, evs in by_src.items()
        if any(needle in json.dumps(e.get("params", {})) for e in evs)
        and stypes.get(evs[0]["source"]["type"]) == "URL_REQUEST"
    ]
    out = [
        f"    NETLOG {len(by_src)} sources, {len(roots)} request(s) matching {needle}"
    ]
    for sid in roots:
        seen, queue, lines = set(), [sid], []
        while queue and len(seen) < 6:
            cur = queue.pop(0)
            if cur in seen:
                continue
            seen.add(cur)
            evs = by_src.get(cur, [])
            t0 = int(evs[0]["time"]) if evs else 0
            for e in evs[:80]:
                par = json.dumps(e.get("params", {}), sort_keys=True)
                dep = e.get("params", {}).get("source_dependency", {}).get("id")
                if dep is not None:
                    queue.append(dep)
                lines.append(
                    f"      src{cur}[{stypes.get(e['source']['type'])}] +{int(e['time']) - t0}ms "
                    f"{types.get(e['type'], e['type'])} {phases.get(e['phase'], e['phase'])} {par[:220]}"
                )
        out.append(f"    request source {sid}:")
        out.extend(lines)
    return out


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
        if r[1] in ("EVT", "SEND"):
            out.append(f"    +{r[0] - t0:.3f}s {r[1]} {r[2]}")
        elif len(r) == 4 and isinstance(r[3], dict):
            out.append(
                f"    +{r[0] - t0:.3f}s NET {r[1]} {r[2]} {json.dumps(r[3], sort_keys=True)}"
            )
        elif r[2] == port_b:
            out.append(f"    +{r[0] - t0:.3f}s SRV {r[1]} {r[3]}")
    return out


async def test_f999_cors_loop(fixture_origin_pair):
    origin_a, origin_b = fixture_origin_pair
    port_b = int(origin_b.rsplit(":", 1)[1])
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    report: list[str] = []
    bad = 0
    for i in range(ITERATIONS):
        netlog = os.path.join(
            tempfile.gettempdir(), f"f999-netlog-{os.getpid()}-{i}.json"
        )
        iid = (
            await spawn(
                headless=True,
                browser_args=[
                    f"--log-net-log={netlog}",
                    "--net-log-capture-mode=Everything",
                ],
                **sandbox_kwargs(),
            )
        )["instance_id"]
        bad_before = bad
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
                    f"iter={i} run={j} ok={ok} pagelog={pagelog} done={done} took={took:.3f}s "
                    f"cors={json.dumps(cors, sort_keys=True)} "
                    f"cors_methods={led['cors_methods']} resources={res}"
                ]
                if not ok or (i == 0 and j == 0):
                    lines += _fmt(
                        t0, sorted(NET + SERVER + OTHER, key=lambda r: r[0]), port_b
                    )
                report.append("\n".join(lines))
        finally:
            await close(instance_id=iid)
            if bad > bad_before:
                report.append(
                    f"iter={i} NETLOG" + chr(10) + chr(10).join(_netlog_report(netlog))
                )
            Path(netlog).unlink(missing_ok=True)
    summary = "\n".join(report)
    print(summary)
    assert bad == 0, f"F999-DIAG {bad} bad of {len(report)}\n{summary}"
