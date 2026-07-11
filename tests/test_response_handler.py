"""Behavioral tests for ResponseHandler — the large-response file fallback.

No browser, no mocks: real token estimation and real file I/O into a tmp dir.
Pins the contract the MCP tools rely on: small payloads pass through untouched,
oversized payloads are spilled to a JSON file on disk and replaced with a
compact descriptor, and the on-disk file faithfully carries the original data
plus size metadata.
"""

import json

from stealth_chrome_devtools_mcp.embedded.response_handler import ResponseHandler


class TestEstimateTokens:
    def test_dict_estimated_from_json_length(self, tmp_path):
        h = ResponseHandler(clone_dir=str(tmp_path))
        data = {"a": 1, "b": "hello"}
        assert h.estimate_tokens(data) == len(json.dumps(data, ensure_ascii=False)) // 4

    def test_string_estimated_from_length(self, tmp_path):
        h = ResponseHandler(clone_dir=str(tmp_path))
        assert h.estimate_tokens("x" * 40) == 10

    def test_non_serializable_scalar_uses_str(self, tmp_path):
        h = ResponseHandler(clone_dir=str(tmp_path))
        assert h.estimate_tokens(123456) == len("123456") // 4


class TestHandleResponse:
    def test_small_payload_passes_through_unchanged(self, tmp_path):
        h = ResponseHandler(max_tokens=1000, clone_dir=str(tmp_path))
        data = {"ok": True, "items": [1, 2, 3]}
        assert h.handle_response(data) is data
        # nothing spilled to disk
        assert list(tmp_path.glob("*.json")) == []

    def test_large_payload_spills_to_file(self, tmp_path):
        h = ResponseHandler(max_tokens=10, clone_dir=str(tmp_path))
        data = {"blob": "y" * 500}

        result = h.handle_response(data)

        assert result is not data
        assert result["reason"].startswith("Response too large")
        assert result["estimated_tokens"] > 10
        spilled = tmp_path / result["filename"]
        assert spilled.exists()
        assert result["file_path"] == str(spilled)
        assert result["file_size_kb"] > 0

    def test_spilled_file_preserves_data_and_marks_metadata(self, tmp_path):
        h = ResponseHandler(max_tokens=10, clone_dir=str(tmp_path))
        data = {"blob": "z" * 500, "n": 7}

        result = h.handle_response(data, metadata={"tool": "list_network_requests"})

        on_disk = json.loads(
            (tmp_path / result["filename"]).read_text(encoding="utf-8")
        )
        assert on_disk["data"] == data
        assert on_disk["metadata"]["auto_saved_due_to_size"] is True
        assert on_disk["metadata"]["tool"] == "list_network_requests"
        # caller-supplied metadata is echoed back in the descriptor too
        assert result["metadata"] == {"tool": "list_network_requests"}

    def test_custom_prefix_used_in_filename(self, tmp_path):
        h = ResponseHandler(max_tokens=1, clone_dir=str(tmp_path))
        result = h.handle_response("q" * 100, fallback_filename_prefix="netlog")
        assert result["filename"].startswith("netlog_")

    def test_clone_dir_created_when_missing(self, tmp_path):
        target = tmp_path / "does-not-exist-yet"
        assert not target.exists()
        ResponseHandler(clone_dir=str(target))
        assert target.exists()
