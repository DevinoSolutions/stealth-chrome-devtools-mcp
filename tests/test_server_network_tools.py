"""Tool-layer pins for M9-2 (F-605): the network tools surface capture state.

Hermetic — the module-global ``network_interceptor`` singleton is swapped for a
fresh, synthetically-seeded one and the ``@section_tool`` functions are invoked
through their FastMCP ``.fn`` (the real tool body, minus the transport layer).
"""

import sys
from pathlib import Path

import pytest

# Make embedded/ importable the same way the real entrypoint / conftest does.
EMBEDDED_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "stealth_chrome_devtools_mcp"
    / "embedded"
)
if str(EMBEDDED_DIR) not in sys.path:
    sys.path.insert(0, str(EMBEDDED_DIR))

import server
from models import NetworkResponse
from network_interceptor import NetworkInterceptor


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
