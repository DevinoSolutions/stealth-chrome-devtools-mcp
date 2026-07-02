"""Behavioral tests for NetworkInterceptor's query surface (no browser).

The capture path (_on_request/_on_response) needs a live CDP tab and is covered
by integration; here we seed the interceptor's in-memory stores with real
NetworkRequest/NetworkResponse models and exercise the search/list/get + capture
filter logic that the MCP network tools call. These are the filters an agent
relies on to find the one request that matters among thousands.
"""

import pytest

from network_interceptor import NetworkInterceptor
from models import NetworkRequest, NetworkResponse


def _req(rid, url, method="GET", post_data=None, resource_type="XHR", iid="i1"):
    return NetworkRequest(request_id=rid, instance_id=iid, url=url, method=method,
                          post_data=post_data, resource_type=resource_type)


def _resp(rid, status=200, body=None, content_type="application/json"):
    return NetworkResponse(request_id=rid, status=status, content_type=content_type, body=body)


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
    _seed(ni, "i1", [
        ("r1", _req("r1", "https://api.example.com/users", "GET", resource_type="XHR"),
         _resp("r1", 200, body=b'{"users":[1,2]}')),
        ("r2", _req("r2", "https://api.example.com/login", "POST",
                    post_data='{"password":"hunter2"}', resource_type="XHR"),
         _resp("r2", 401, body=b'unauthorized access')),
        ("r3", _req("r3", "https://cdn.example.com/app.js", "GET", resource_type="Script"),
         _resp("r3", 200, body=b'console.log(1)')),
    ])
    return ni


class TestCaptureFilters:
    async def test_default_filters_empty(self):
        ni = NetworkInterceptor()
        assert await ni.get_capture_filters("i1") == {"include": [], "exclude": []}

    async def test_set_and_get_filters(self):
        ni = NetworkInterceptor()
        await ni.set_capture_filters("i1", include_types=["XHR"], exclude_types=["Image"])
        assert await ni.get_capture_filters("i1") == {"include": ["XHR"], "exclude": ["Image"]}


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
        result = await _fixture().search_requests("i1", response_contains="unauthorized")
        assert [r["request_id"] for r in result["results"]] == ["r2"]

    async def test_resource_type_filter(self):
        result = await _fixture().search_requests("i1", resource_type="script")
        assert [r["request_id"] for r in result["results"]] == ["r3"]

    async def test_pagination_reports_has_more(self):
        page1 = await _fixture().search_requests("i1", limit=2, offset=0)
        assert len(page1["results"]) == 2 and page1["total"] == 3 and page1["has_more"] is True
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
