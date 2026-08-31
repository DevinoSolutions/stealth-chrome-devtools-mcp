"""Behavioral tests for NetworkInterceptor's query surface (no browser).

The capture path (_on_request/_on_response) needs a live CDP tab and is covered
by integration; here we seed the interceptor's in-memory stores with real
NetworkRequest/NetworkResponse models and exercise the search/list/get + capture
filter logic that the MCP network tools call. These are the filters an agent
relies on to find the one request that matters among thousands.
"""

import base64
import json

import nodriver as uc
import pytest

from fakes import FakeTab
from stealth_chrome_devtools_mcp.embedded.models import NetworkRequest, NetworkResponse
from stealth_chrome_devtools_mcp.embedded.network_interceptor import (
    NetworkInterceptor,
    is_internal_url,
    resource_type_of,
    to_cookie_same_site,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError


def _req(rid, url, method="GET", post_data=None, resource_type="XHR", iid="i1"):
    return NetworkRequest(
        request_id=rid,
        instance_id=iid,
        url=url,
        method=method,
        post_data=post_data,
        resource_type=resource_type,
    )


def _resp(rid, status=200, body=None, content_type="application/json"):
    return NetworkResponse(
        request_id=rid, status=status, content_type=content_type, body=body
    )


def _seed(interceptor, iid, rows):
    """rows: list of (request_id, NetworkRequest, Optional[NetworkResponse])."""
    interceptor._instance_requests[iid] = []
    for rid, req, resp in rows:
        interceptor._requests[rid] = req
        interceptor._instance_requests[iid].append(rid)
        if resp is not None:
            interceptor._responses[rid] = resp


def _fixture():
    ni = NetworkInterceptor()
    _seed(
        ni,
        "i1",
        [
            (
                "r1",
                _req("r1", "https://api.example.com/users", "GET", resource_type="XHR"),
                _resp("r1", 200, body=b'{"users":[1,2]}'),
            ),
            (
                "r2",
                _req(
                    "r2",
                    "https://api.example.com/login",
                    "POST",
                    post_data='{"password":"hunter2"}',
                    resource_type="XHR",
                ),
                _resp("r2", 401, body=b"unauthorized access"),
            ),
            (
                "r3",
                _req(
                    "r3",
                    "https://cdn.example.com/app.js",
                    "GET",
                    resource_type="Script",
                ),
                _resp("r3", 200, body=b"console.log(1)"),
            ),
        ],
    )
    return ni


class _SpyTab:
    """Minimal async CDP tab double for ``_on_response``: counts body-fetch
    sends and either returns a ``(body, base64_encoded)`` tuple or raises."""

    def __init__(self, body=None, raises=None):
        self._body = body
        self._raises = raises
        self.send_count = 0

    async def send(self, cmd):
        self.send_count += 1
        close = getattr(cmd, "close", None)
        if close:
            close()  # close the un-driven CDP command generator (no warning)
        if self._raises is not None:
            raise self._raises
        return self._body


class _FakeResponse:
    def __init__(self, status=200, headers=None, mime_type="application/json"):
        self.status = status
        self.headers = headers or {}
        self.mime_type = mime_type


class _FakeEvent:
    def __init__(self, request_id, response):
        self.request_id = request_id
        self.response = response


class _FakeReqType:
    def __init__(self, value):
        self.value = value


class _FakeRequestObj:
    def __init__(self, url, method="GET", headers=None, post_data=None):
        self.url = url
        self.method = method
        self.headers = headers or {}
        self.post_data = post_data


class _FakeReqEvent:
    """Minimal ``RequestWillBeSent`` double for ``_on_request``.

    F-803: the attribute is ``type_``, matching nodriver's generated CDP
    dataclass (``type`` is a builtin, so the generator suffixes it). The double
    said ``type`` before the fix and so agreed with the *bug*, not with Chrome;
    ``TestResourceTypeCapture`` below drives real ``from_json``-built events so
    a divergence like that cannot hide again.
    """

    def __init__(self, request_id, request, rtype="XHR"):
        self.request_id = request_id
        self.request = request
        self.type_ = _FakeReqType(rtype)


# ── Real CDP event builders (F-803) ────────────────────────────────────────────
# Built through nodriver's own ``from_json`` from realistic CDP payloads, so the
# handlers under test see the exact object shape Chrome produces. No hand-rolled
# attribute names: that is what let resource_type read None for the whole 2.0.0
# release.


def _cdp_request_event(request_id, url, rtype="Document", method="GET"):
    """A real ``uc.cdp.network.RequestWillBeSent``. ``rtype=None`` omits the type."""
    payload = {
        "requestId": request_id,
        "loaderId": "L1",
        "documentURL": url,
        "request": {
            "url": url,
            "method": method,
            "headers": {},
            "initialPriority": "High",
            "referrerPolicy": "no-referrer",
        },
        "timestamp": 1.0,
        "wallTime": 1.0,
        "initiator": {"type": "other"},
        "redirectHasExtraInfo": False,
    }
    if rtype is not None:
        payload["type"] = rtype
    return uc.cdp.network.RequestWillBeSent.from_json(payload)


def _cdp_response_event(request_id, url, rtype="Document", status=200):
    """A real ``uc.cdp.network.ResponseReceived``."""
    return uc.cdp.network.ResponseReceived.from_json(
        {
            "requestId": request_id,
            "loaderId": "L1",
            "timestamp": 1.0,
            "type": rtype,
            "response": {
                "url": url,
                "status": status,
                "statusText": "OK",
                "headers": {},
                "mimeType": "text/html",
                "charset": "utf-8",
                "connectionReused": False,
                "connectionId": 1.0,
                "encodedDataLength": 10.0,
                "securityState": "secure",
            },
            "hasExtraInfo": False,
        }
    )


class TestCaptureFilters:
    async def test_default_filters_empty(self):
        ni = NetworkInterceptor()
        filters = await ni.get_capture_filters("i1")
        assert filters["include"] == []
        assert filters["exclude"] == []
        assert filters["capture_bodies"] is False  # off by default

    async def test_set_and_get_filters(self):
        ni = NetworkInterceptor()
        await ni.set_capture_filters(
            "i1", include_types=["XHR"], exclude_types=["Image"]
        )
        filters = await ni.get_capture_filters("i1")
        assert filters["include"] == ["XHR"]
        assert filters["exclude"] == ["Image"]


class TestSearchRequests:
    async def test_no_filters_returns_all(self):
        result = await _fixture().search_requests("i1")
        assert result["total"] == 3
        assert {r["request_id"] for r in result["results"]} == {"r1", "r2", "r3"}

    async def test_url_pattern_substring_case_insensitive(self):
        result = await _fixture().search_requests("i1", url_pattern="LOGIN")
        assert [r["request_id"] for r in result["results"]] == ["r2"]

    async def test_method_filter(self):
        result = await _fixture().search_requests("i1", method="post")
        assert [r["request_id"] for r in result["results"]] == ["r2"]

    async def test_status_code_filter(self):
        result = await _fixture().search_requests("i1", status_code=401)
        assert [r["request_id"] for r in result["results"]] == ["r2"]

    async def test_payload_contains(self):
        result = await _fixture().search_requests("i1", payload_contains="password")
        assert [r["request_id"] for r in result["results"]] == ["r2"]

    async def test_response_contains_searches_body(self):
        result = await _fixture().search_requests(
            "i1", response_contains="unauthorized"
        )
        assert [r["request_id"] for r in result["results"]] == ["r2"]

    async def test_resource_type_filter(self):
        result = await _fixture().search_requests("i1", resource_type="script")
        assert [r["request_id"] for r in result["results"]] == ["r3"]

    async def test_pagination_reports_has_more(self):
        page1 = await _fixture().search_requests("i1", limit=2, offset=0)
        assert (
            len(page1["results"]) == 2
            and page1["total"] == 3
            and page1["has_more"] is True
        )
        page2 = await _fixture().search_requests("i1", limit=2, offset=2)
        assert len(page2["results"]) == 1 and page2["has_more"] is False

    async def test_unknown_instance_is_empty(self):
        result = await _fixture().search_requests("nope")
        assert result["total"] == 0 and result["results"] == []


class TestListAndGet:
    async def test_list_all_requests(self):
        reqs = await _fixture().list_requests("i1")
        assert [r.request_id for r in reqs] == ["r1", "r2", "r3"]

    async def test_list_filtered_by_resource_type(self):
        reqs = await _fixture().list_requests("i1", filter_type="script")
        assert [r.request_id for r in reqs] == ["r3"]

    async def test_get_request_hit_and_miss(self):
        ni = _fixture()
        assert (await ni.get_request("r1")).url == "https://api.example.com/users"
        assert await ni.get_request("nope") is None

    async def test_get_response_hit_and_miss(self):
        ni = _fixture()
        assert (await ni.get_response("r2")).status == 401
        assert await ni.get_response("nope") is None


class TestBodyStoreByteCaps:
    """M9-1 (F-605): the response-body store is byte-bounded at its single
    write chokepoint ``_store_response``. Caps are resolved from Settings at
    call time; the autouse ``_reset_settings_cache`` fixture makes
    ``monkeypatch.setenv`` visible. 0 on either cap = unbounded.
    """

    def test_over_per_body_cap_drops_body_keeps_metadata(self, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_NETWORK_BODY_MAX_BYTES", "100")
        ni = NetworkInterceptor()
        ni._store_response("r1", _resp("r1", status=200, body=b"x" * 200))
        stored = ni._responses["r1"]
        assert stored.body is None  # over per-body cap -> body dropped
        assert stored.status == 200  # metadata retained
        assert ni._body_bytes == 0  # nothing counted
        assert "r1" not in ni._body_order  # not tracked for eviction

    def test_total_store_cap_evicts_oldest_fifo(self, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_NETWORK_BODY_STORE_MAX_BYTES", "250")
        monkeypatch.setenv("STEALTH_MCP_NETWORK_BODY_MAX_BYTES", "0")  # no per-body cap
        ni = NetworkInterceptor()
        ni._store_response("r1", _resp("r1", body=b"a" * 100))
        ni._store_response("r2", _resp("r2", body=b"b" * 100))
        ni._store_response("r3", _resp("r3", body=b"c" * 100))  # 300 > 250 -> evict
        assert ni._body_bytes <= 250
        assert ni._body_bytes == 200
        assert ni._responses["r1"].body is None  # oldest evicted first (FIFO)
        assert ni._responses["r2"].body == b"b" * 100
        assert ni._responses["r3"].body == b"c" * 100

    def test_overwrite_same_request_id_does_not_double_count(self, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_NETWORK_BODY_MAX_BYTES", "0")
        monkeypatch.setenv("STEALTH_MCP_NETWORK_BODY_STORE_MAX_BYTES", "0")
        ni = NetworkInterceptor()
        ni._store_response("r1", _resp("r1", body=b"x" * 100))
        assert ni._body_bytes == 100
        ni._store_response("r1", _resp("r1", body=b"y" * 30))  # overwrite
        assert ni._body_bytes == 30  # prior 100 subtracted, not summed to 130
        assert ni._responses["r1"].body == b"y" * 30

    def test_cap_zero_is_unbounded(self, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_NETWORK_BODY_MAX_BYTES", "0")
        monkeypatch.setenv("STEALTH_MCP_NETWORK_BODY_STORE_MAX_BYTES", "0")
        ni = NetworkInterceptor()
        for i in range(5):
            ni._store_response(f"r{i}", _resp(f"r{i}", body=b"z" * 1_000_000))
        assert ni._body_bytes == 5_000_000  # nothing dropped or evicted
        assert all(ni._responses[f"r{i}"].body is not None for i in range(5))

    async def test_import_from_json_is_capped(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STEALTH_MCP_NETWORK_BODY_STORE_MAX_BYTES", "250")
        monkeypatch.setenv("STEALTH_MCP_NETWORK_BODY_MAX_BYTES", "0")
        requests, responses = [], []
        for i in range(3):
            rid = f"r{i}"
            requests.append(
                {
                    "request_id": rid,
                    "url": f"https://x/{i}",
                    "method": "GET",
                    "headers": {},
                    "cookies": {},
                    "post_data": None,
                    "resource_type": "XHR",
                    "timestamp": "2026-01-01T00:00:00",
                }
            )
            responses.append(
                {
                    "request_id": rid,
                    "status": 200,
                    "headers": {},
                    "content_type": "application/json",
                    "body": base64.b64encode(b"q" * 100).decode("utf-8"),
                    "timestamp": "2026-01-01T00:00:00",
                }
            )
        fp = tmp_path / "net.json"
        fp.write_text(json.dumps({"requests": requests, "responses": responses}))
        ni = NetworkInterceptor()
        await ni.import_from_json("i1", str(fp))
        assert ni._body_bytes <= 250
        assert ni._responses["r0"].body is None  # oldest imported evicted first
        assert ni._responses["r2"].body is not None

    async def test_clear_instance_data_returns_body_bytes_to_zero(self, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_NETWORK_BODY_MAX_BYTES", "0")
        monkeypatch.setenv("STEALTH_MCP_NETWORK_BODY_STORE_MAX_BYTES", "0")
        ni = NetworkInterceptor()
        ni._instance_requests["i1"] = []
        for i in range(3):
            rid = f"r{i}"
            ni._instance_requests["i1"].append(rid)
            ni._store_response(rid, _resp(rid, body=b"m" * 100))
        assert ni._body_bytes == 300
        await ni.clear_instance_data("i1")
        assert ni._body_bytes == 0
        assert ni._responses == {}


class TestRequestStoreCaps:
    """C4 (A3): the request store is count- and metadata-bounded at its single
    write chokepoint ``_store_request``, mirroring the byte-bounded body store.
    Caps are resolved from Settings at call time (the autouse
    ``_reset_settings_cache`` fixture makes ``monkeypatch.setenv`` visible); 0 on
    either cap = unbounded. Driven through the real ``_on_request`` capture path.
    """

    async def test_retained_request_count_is_capped_fifo(self, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_NETWORK_REQUEST_MAX_COUNT", "3")
        ni = NetworkInterceptor()
        ni._instance_requests["i1"] = []
        for i in range(5):
            await ni._on_request(
                _FakeReqEvent(f"r{i}", _FakeRequestObj(f"https://x/{i}")), "i1"
            )
        assert len(ni._requests) == 3  # retained count capped
        assert "r0" not in ni._requests and "r1" not in ni._requests  # oldest evicted
        assert list(ni._requests) == ["r2", "r3", "r4"]  # FIFO
        assert ni._instance_requests["i1"] == ["r2", "r3", "r4"]  # per-instance pruned

    async def test_oversize_post_data_is_bounded(self, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_NETWORK_POST_DATA_MAX_BYTES", "100")
        ni = NetworkInterceptor()
        ni._instance_requests["i1"] = []
        await ni._on_request(
            _FakeReqEvent(
                "r1",
                _FakeRequestObj("https://x/1", method="POST", post_data="p" * 200),
            ),
            "i1",
        )
        stored = ni._requests["r1"]
        assert stored.post_data is None  # over per-post_data cap -> dropped
        assert stored.url == "https://x/1"  # metadata retained
        assert stored.method == "POST"


class TestCaptureOptIn:
    """M9-2 (F-605): response-body capture is opt-in / off-by-default.
    ``_on_response`` gates the CDP body fetch on the resolved capture flag —
    the per-instance filter if set, else ``network_capture_bodies``."""

    async def test_default_off_stores_metadata_and_skips_body_fetch(self):
        ni = NetworkInterceptor()
        tab = _SpyTab(body=("hello", False))
        await ni._on_response(_FakeEvent("r1", _FakeResponse(status=200)), "i1", tab)
        stored = await ni.get_response("r1")
        assert stored is not None
        assert stored.status == 200  # metadata captured
        assert stored.body is None  # body not captured
        assert tab.send_count == 0  # CDP body fetch never attempted

    async def test_per_instance_enable_fetches_and_stores_body(self):
        ni = NetworkInterceptor()
        await ni.set_capture_filters("i1", capture_bodies=True)
        tab = _SpyTab(body=("hello", False))
        await ni._on_response(_FakeEvent("r1", _FakeResponse()), "i1", tab)
        stored = await ni.get_response("r1")
        assert tab.send_count == 1
        assert stored.body == b"hello"
        assert ni._body_bytes == len(b"hello")

    async def test_global_env_enable_fetches_body(self, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_NETWORK_CAPTURE_BODIES", "1")
        ni = NetworkInterceptor()
        tab = _SpyTab(body=("data", False))
        await ni._on_response(_FakeEvent("r1", _FakeResponse()), "i1", tab)
        assert tab.send_count == 1
        assert (await ni.get_response("r1")).body == b"data"

    async def test_capture_bodies_only_update_preserves_include_exclude(self):
        ni = NetworkInterceptor()
        await ni.set_capture_filters(
            "i1", include_types=["XHR"], exclude_types=["Image"]
        )
        await ni.set_capture_filters("i1", capture_bodies=True)  # merge, not clobber
        filters = await ni.get_capture_filters("i1")
        assert filters["include"] == ["XHR"]
        assert filters["exclude"] == ["Image"]
        assert filters["capture_bodies"] is True

    async def test_get_capture_filters_reports_flag_and_store_stats(self, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_NETWORK_BODY_STORE_MAX_BYTES", "1000")
        monkeypatch.setenv("STEALTH_MCP_NETWORK_BODY_MAX_BYTES", "500")
        ni = NetworkInterceptor()
        ni._store_response("r1", _resp("r1", body=b"a" * 42))
        filters = await ni.get_capture_filters("i1")
        assert filters["capture_bodies"] is False  # resolved global default
        assert filters["body_store_bytes"] == 42
        assert filters["body_store_max_bytes"] == 1000
        assert filters["body_max_bytes"] == 500

    async def test_m10a_7b_debug_log_survives_when_body_fetch_raises(self, monkeypatch):
        # Carry-through pin: with capture ON, a failing body fetch still emits the
        # M10a-7b DEBUG record (the log line survived M9's _on_response rewrite).
        from stealth_chrome_devtools_mcp.embedded import network_interceptor as ni_mod

        calls = []
        monkeypatch.setattr(
            ni_mod.debug_logger,
            "log_debug",
            lambda *a, **k: calls.append(a),
        )
        ni = NetworkInterceptor()
        await ni.set_capture_filters("i1", capture_bodies=True)
        tab = _SpyTab(raises=ValueError("boom"))
        await ni._on_response(_FakeEvent("r1", _FakeResponse()), "i1", tab)
        assert tab.send_count == 1
        assert any("boom" in str(a) for a in calls), "expected M10a-7b debug log"
        assert (await ni.get_response("r1")).body is None  # metadata still stored


class TestResourceTypeCapture:
    """F-803: every captured row carries the CDP resource type.

    Released 2.0.0 read ``event.type`` while nodriver's generated dataclass
    names the field ``type_``, so the probe was permanently False, every row
    stored ``resource_type=None``, and ``filter_type`` could never match
    anything. These drive REAL ``from_json``-built CDP events.
    """

    async def test_real_cdp_event_populates_resource_type(self):
        ni = NetworkInterceptor()
        await ni._on_request(_cdp_request_event("r1", "https://x/", "Document"), "i1")
        assert ni._requests["r1"].resource_type == "Document"  # not None

    async def test_lowercase_filter_type_matches_cdp_casing(self):
        ni = NetworkInterceptor()
        await ni._on_request(_cdp_request_event("r1", "https://x/", "Document"), "i1")
        await ni._on_request(_cdp_request_event("r2", "https://x/a.js", "Script"), "i1")
        rows = await ni.list_requests("i1", filter_type="document")
        assert [r.request_id for r in rows] == ["r1"]

    async def test_lowercase_search_resource_type_matches_cdp_casing(self):
        ni = NetworkInterceptor()
        await ni._on_request(_cdp_request_event("r1", "https://x/", "Document"), "i1")
        result = await ni.search_requests("i1", resource_type="document")
        assert [r["request_id"] for r in result["results"]] == ["r1"]
        assert result["results"][0]["resource_type"] == "Document"

    async def test_missing_type_is_backfilled_from_the_response_event(self):
        # RequestWillBeSent.type_ is OPTIONAL (absent on some redirects and
        # preflights); ResponseReceived.type_ is not, so it closes the gap.
        ni = NetworkInterceptor()
        await ni._on_request(
            _cdp_request_event("r1", "https://x/api", rtype=None), "i1"
        )
        assert ni._requests["r1"].resource_type is None
        await ni._on_response(_cdp_response_event("r1", "https://x/api", "XHR"), "i1")
        assert ni._requests["r1"].resource_type == "XHR"

    async def test_backfill_does_not_clobber_an_existing_type(self):
        ni = NetworkInterceptor()
        await ni._on_request(_cdp_request_event("r1", "https://x/", "Document"), "i1")
        await ni._on_response(_cdp_response_event("r1", "https://x/", "Other"), "i1")
        assert ni._requests["r1"].resource_type == "Document"

    def test_resource_type_of_reads_both_spellings_and_bare_strings(self):
        assert resource_type_of(_cdp_request_event("r1", "https://x/", "Image")) == (
            "Image"
        )
        assert resource_type_of(_FakeReqEvent("r1", None, "XHR")) == "XHR"
        assert resource_type_of(type("E", (), {"type": "Fetch"})()) == "Fetch"
        assert resource_type_of(type("E", (), {})()) is None


class TestInternalUrlExclusion:
    """F-803: Chrome's own traffic is not the page's traffic.

    A live 2.0.0 capture of one page load returned 30 rows, 24 of them
    ``chrome://new-tab-page/*``. Non-web schemes are dropped at capture time
    unless explicitly opted back in.
    """

    def test_scheme_predicate(self):
        for url in (
            "chrome://new-tab-page/",
            "CHROME://New-Tab-Page/",  # case-insensitive
            "chrome-extension://abc/background.js",
            "chrome-error://chromewebdata/",
            "chrome-untrusted://foo",
            "chrome-search://local-ntp",
            "devtools://devtools/bundled/inspector.html",
            "about:blank",
        ):
            assert is_internal_url(url) is True, url
        for url in (
            "https://example.com/",
            "http://127.0.0.1:8000/network.html",
            "file:///tmp/page.html",
            "data:text/html,hi",
            "blob:https://example.com/1234",
            "https://example.com/chrome://not-a-scheme",
            None,
            "",
            object(),  # a non-str must never cause traffic to be DROPPED
        ):
            assert is_internal_url(url) is False, url

    async def test_internal_requests_are_dropped_by_default(self):
        ni = NetworkInterceptor()
        await ni._on_request(
            _cdp_request_event("r1", "chrome://new-tab-page/", "Document"), "i1"
        )
        await ni._on_request(_cdp_request_event("r2", "about:blank", "Document"), "i1")
        await ni._on_request(
            _cdp_request_event("r3", "https://example.com/", "Document"), "i1"
        )
        rows = await ni.list_requests("i1")
        assert [r.url for r in rows] == ["https://example.com/"]

    async def test_internal_responses_are_dropped_by_default(self):
        ni = NetworkInterceptor()
        await ni._on_response(
            _cdp_response_event("r1", "chrome://new-tab-page/"), "i1", None
        )
        assert await ni.get_response("r1") is None

    async def test_per_instance_opt_in_captures_internal_traffic(self):
        ni = NetworkInterceptor()
        await ni.set_capture_filters("i1", capture_internal_urls=True)
        await ni._on_request(
            _cdp_request_event("r1", "chrome://new-tab-page/", "Document"), "i1"
        )
        assert [r.url for r in await ni.list_requests("i1")] == [
            "chrome://new-tab-page/"
        ]

    async def test_global_env_opt_in_captures_internal_traffic(self, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_NETWORK_CAPTURE_INTERNAL_URLS", "1")
        ni = NetworkInterceptor()
        await ni._on_request(
            _cdp_request_event("r1", "chrome://new-tab-page/", "Document"), "i1"
        )
        assert len(await ni.list_requests("i1")) == 1

    async def test_per_instance_off_overrides_global_on(self, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_NETWORK_CAPTURE_INTERNAL_URLS", "1")
        ni = NetworkInterceptor()
        await ni.set_capture_filters("i1", capture_internal_urls=False)
        await ni._on_request(
            _cdp_request_event("r1", "chrome://new-tab-page/", "Document"), "i1"
        )
        assert await ni.list_requests("i1") == []

    async def test_get_capture_filters_reports_resolved_flag(self):
        ni = NetworkInterceptor()
        assert (await ni.get_capture_filters("i1"))["capture_internal_urls"] is False
        await ni.set_capture_filters("i1", capture_internal_urls=True)
        assert (await ni.get_capture_filters("i1"))["capture_internal_urls"] is True

    async def test_opt_in_merges_with_existing_filter_entry(self):
        # One filter vocabulary, not a parallel mechanism: the new key merges
        # into the same per-instance entry as include/exclude/capture_bodies.
        ni = NetworkInterceptor()
        await ni.set_capture_filters(
            "i1", include_types=["XHR"], exclude_types=["Image"], capture_bodies=True
        )
        await ni.set_capture_filters("i1", capture_internal_urls=True)
        filters = await ni.get_capture_filters("i1")
        assert filters["include"] == ["XHR"]
        assert filters["exclude"] == ["Image"]
        assert filters["capture_bodies"] is True
        assert filters["capture_internal_urls"] is True


# ---------------------------------------------------------------------------
# F-821 — set_cookie's ``same_site`` must reach CDP as a CookieSameSite enum
# ---------------------------------------------------------------------------


class TestCookieSameSite:
    """``cdp.network.set_cookie`` serialises ``same_site`` with ``.to_json()``
    while it builds its request frame, so a bare ``"Lax"`` string blows the
    command up with ``'str' object has no attribute 'to_json'``. The tool layer
    takes the value as a string, so the one CDP boundary must convert it.

    The doubles here are the library's own: the enum members come from
    ``nodriver.cdp.network.CookieSameSite`` and the frame is the one the real
    command generator yields (``FakeTab.send`` advances it, swallowing nothing).
    """

    def test_documented_names_map_to_the_real_enum(self):
        assert to_cookie_same_site("Strict") is uc.cdp.network.CookieSameSite.STRICT
        assert to_cookie_same_site("Lax") is uc.cdp.network.CookieSameSite.LAX
        assert to_cookie_same_site("None") is uc.cdp.network.CookieSameSite.NONE

    def test_accepts_the_documented_names_case_insensitively(self):
        assert to_cookie_same_site("lax") is uc.cdp.network.CookieSameSite.LAX
        assert to_cookie_same_site("STRICT") is uc.cdp.network.CookieSameSite.STRICT
        assert to_cookie_same_site("nOnE") is uc.cdp.network.CookieSameSite.NONE

    def test_already_typed_enum_passes_through(self):
        member = uc.cdp.network.CookieSameSite.STRICT
        assert to_cookie_same_site(member) is member

    def test_invalid_value_raises_tool_error_listing_the_valid_names(self):
        with pytest.raises(ToolError) as excinfo:
            to_cookie_same_site("SameSite=Lax")
        message = str(excinfo.value)
        assert "SameSite=Lax" in message
        for name in ("Strict", "Lax", "None"):
            assert name in message

    async def test_set_cookie_sends_the_cdp_same_site_enum(self):
        tab = FakeTab()
        ni = NetworkInterceptor()
        assert await ni.set_cookie(
            tab,
            {
                "name": "sid",
                "value": "abc",
                "url": "https://fake.test/page",
                "path": "/",
                "secure": True,
                "http_only": False,
                "same_site": "Lax",
            },
        )
        params = tab.cdp_frames[0]["params"]
        assert params["sameSite"] == uc.cdp.network.CookieSameSite.LAX.to_json()

    async def test_set_cookie_rejects_an_invalid_same_site(self):
        ni = NetworkInterceptor()
        with pytest.raises(ToolError, match=r"Strict"):
            await ni.set_cookie(
                FakeTab(),
                {"name": "s", "value": "a", "url": "https://x/", "same_site": "yes"},
            )

    async def test_set_cookie_without_same_site_still_works(self):
        # Regression: the omitted-attribute path must stay untouched.
        tab = FakeTab()
        ni = NetworkInterceptor()
        assert await ni.set_cookie(
            tab, {"name": "sid", "value": "abc", "url": "https://fake.test/page"}
        )
        assert "sameSite" not in tab.cdp_frames[0]["params"]

    async def test_set_cookie_does_not_mutate_the_callers_dict(self):
        cookie = {
            "name": "sid",
            "value": "abc",
            "url": "https://fake.test/page",
            "same_site": "Lax",
        }
        await NetworkInterceptor().set_cookie(FakeTab(), cookie)
        assert cookie["same_site"] == "Lax"

    async def test_set_cookie_tool_body_reaches_cdp_with_the_enum(
        self, call_tool, patched_server
    ):
        """The whole tool path, not just the boundary: server.py's ``set_cookie``
        builds the cookie dict from a plain string argument."""
        from fakes import FakeBrowserManager

        tab = FakeTab(url="https://fake.test/page")
        srv = patched_server(
            browser_manager=FakeBrowserManager(tabs={"i1": tab}),
            network_interceptor=NetworkInterceptor(),
        )
        assert await call_tool(
            srv,
            "set_cookie",
            instance_id="i1",
            name="sid",
            value="abc",
            same_site="Lax",
        )
        assert tab.cdp_frames[0]["params"]["sameSite"] == "Lax"
