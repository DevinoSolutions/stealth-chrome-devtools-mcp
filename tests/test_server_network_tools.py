"""Tool-layer pins for M9-2 (F-605): the network tools surface capture state.

Hermetic — the module-global ``network_interceptor`` singleton is swapped for a
fresh, synthetically-seeded one and the ``@section_tool`` functions are invoked
through their FastMCP ``.fn`` (the real tool body, minus the transport layer).
"""

import json

import pytest

from stealth_chrome_devtools_mcp.embedded import server
from stealth_chrome_devtools_mcp.embedded.models import NetworkResponse
from stealth_chrome_devtools_mcp.embedded.network_interceptor import NetworkInterceptor

# Real CDP event builders live once, in the interceptor's own test module
# (tests/ is on sys.path via conftest) — F-803 pins must not fork a second
# hand-rolled event shape, which is how the original defect stayed invisible.
from test_network_interceptor import (  # noqa: E402  PERMANENT(import follows the sys.path comment above)
    _cdp_request_event,
)


@pytest.fixture()
def fresh_interceptor(monkeypatch):
    """Swap the server's shared interceptor for an empty one per test."""
    ni = NetworkInterceptor()
    monkeypatch.setattr(server, "network_interceptor", ni)
    return ni


class TestCaptureNote:
    async def test_get_response_details_notes_capture_off(self, fresh_interceptor):
        fresh_interceptor._responses["r1"] = NetworkResponse(
            request_id="r1", status=200, body=None
        )
        result = await server.get_response_details.fn(request_id="r1")
        assert result["status"] == 200  # metadata still surfaced
        assert "capture" in result["capture_note"].lower()

    async def test_get_response_details_no_note_when_capture_on(
        self, fresh_interceptor, monkeypatch
    ):
        monkeypatch.setenv("STEALTH_MCP_NETWORK_CAPTURE_BODIES", "1")
        fresh_interceptor._responses["r1"] = NetworkResponse(
            request_id="r1", status=200, body=None
        )
        result = await server.get_response_details.fn(request_id="r1")
        assert "capture_note" not in result

    async def test_search_network_requests_notes_capture_off(self, fresh_interceptor):
        result = await server.search_network_requests.fn(instance_id="i1")
        assert "capture_note" in result

    async def test_search_no_note_when_capture_on(self, fresh_interceptor):
        await fresh_interceptor.set_capture_filters("i1", capture_bodies=True)
        result = await server.search_network_requests.fn(instance_id="i1")
        assert "capture_note" not in result

    async def test_export_notes_capture_off(self, fresh_interceptor, tmp_path):
        fp = tmp_path / "net.json"
        result = await server.export_network_data.fn(instance_id="i1", filepath=str(fp))
        assert result["success"] is True
        assert "capture_note" in result


class TestCaptureBodiesRoundTrip:
    async def test_set_and_get_capture_bodies_round_trips(self, fresh_interceptor):
        await server.set_network_capture_filters.fn(
            instance_id="i1", include_types=["XHR"], capture_bodies=True
        )
        filters = await server.get_network_capture_filters.fn(instance_id="i1")
        assert filters["capture_bodies"] is True
        assert filters["include"] == ["XHR"]


class TestResourceTypeSurfacing:
    """F-803 at the tool layer: the rows agents actually read carry the type,
    and ``filter_type`` is a working feature rather than a dead parameter."""

    async def test_list_network_requests_surfaces_resource_type(
        self, fresh_interceptor
    ):
        await fresh_interceptor._on_request(
            _cdp_request_event("r1", "https://example.com/", "Document"), "i1"
        )
        rows = await server.list_network_requests.fn(instance_id="i1")
        assert [r["resource_type"] for r in rows] == ["Document"]

    async def test_filter_type_lowercase_document_matches(self, fresh_interceptor):
        await fresh_interceptor._on_request(
            _cdp_request_event("r1", "https://example.com/", "Document"), "i1"
        )
        await fresh_interceptor._on_request(
            _cdp_request_event("r2", "https://example.com/a.css", "Stylesheet"), "i1"
        )
        rows = await server.list_network_requests.fn(
            instance_id="i1", filter_type="document"
        )
        assert [r["url"] for r in rows] == ["https://example.com/"]

    async def test_get_request_details_and_export_carry_resource_type(
        self, fresh_interceptor, tmp_path
    ):
        await fresh_interceptor._on_request(
            _cdp_request_event("r1", "https://example.com/", "Document"), "i1"
        )
        details = await server.get_request_details.fn(request_id="r1")
        assert details["resource_type"] == "Document"

        fp = tmp_path / "net.json"
        await server.export_network_data.fn(instance_id="i1", filepath=str(fp))
        exported = json.loads(fp.read_text())
        assert exported["requests"][0]["resource_type"] == "Document"


class TestInternalUrlExclusionAtToolLayer:
    async def test_chrome_internal_rows_never_reach_list(self, fresh_interceptor):
        for i, url in enumerate(
            ("chrome://new-tab-page/", "chrome-error://chromewebdata/", "about:blank")
        ):
            await fresh_interceptor._on_request(
                _cdp_request_event(f"c{i}", url, "Document"), "i1"
            )
        await fresh_interceptor._on_request(
            _cdp_request_event("r1", "https://example.com/", "Document"), "i1"
        )
        rows = await server.list_network_requests.fn(instance_id="i1")
        assert [r["url"] for r in rows] == ["https://example.com/"]

    async def test_capture_internal_urls_round_trips_through_the_tools(
        self, fresh_interceptor
    ):
        filters = await server.get_network_capture_filters.fn(instance_id="i1")
        assert filters["capture_internal_urls"] is False  # default: excluded
        await server.set_network_capture_filters.fn(
            instance_id="i1", capture_internal_urls=True
        )
        filters = await server.get_network_capture_filters.fn(instance_id="i1")
        assert filters["capture_internal_urls"] is True
        await fresh_interceptor._on_request(
            _cdp_request_event("c0", "chrome://new-tab-page/", "Document"), "i1"
        )
        rows = await server.list_network_requests.fn(instance_id="i1")
        assert [r["url"] for r in rows] == ["chrome://new-tab-page/"]
