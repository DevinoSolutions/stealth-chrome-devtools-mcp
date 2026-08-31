"""Behavioral tests for ResponseHandler — the large-response file fallback.

No browser, no mocks: real token estimation and real file I/O into a tmp dir.
Pins the contract the MCP tools rely on: small payloads pass through untouched,
oversized payloads are spilled to a JSON file on disk and replaced with a
compact descriptor, and the on-disk file faithfully carries the original data
plus size metadata.

Also pins the two 2.0.8 defects this module owns:

* **F-822** — the estimation/spill path crashed (``TypeError: Object of type
  ExceptionDetails is not JSON serializable``) on a payload carrying a raw
  nodriver CDP object. ``Tab.evaluate`` *returns* ``cdp.runtime.ExceptionDetails``
  in the value's place when the evaluated JS throws, and
  ``dom_handler.get_page_content`` calls it three times unguarded, so
  ``get_page_content`` fed that record straight into ``handle_response``.
  The CDP records here are built by **nodriver's own** ``from_json`` — a
  hand-rolled double would be free to encode the bug it is meant to catch.
* **F-837** — the inline threshold sat above the MCP client's practical token
  ceiling, leaving a band too big to deliver and too small to divert.
"""

import json

import pytest

from stealth_chrome_devtools_mcp.embedded.response_handler import (
    INLINE_TOKEN_CEILING,
    ResponseHandler,
)


def real_cdp_exception_details():
    """A real ``cdp.runtime.ExceptionDetails``, built by nodriver's own
    ``from_json`` from a genuine ``Runtime.evaluate`` error payload."""
    from nodriver.cdp.runtime import ExceptionDetails

    return ExceptionDetails.from_json(
        {
            "exceptionId": 1,
            "text": "Uncaught",
            "lineNumber": 0,
            "columnNumber": 0,
            "exception": {
                "type": "object",
                "className": "TypeError",
                "description": (
                    "TypeError: Cannot read properties of null (reading 'innerText')"
                ),
            },
        }
    )


def real_cdp_remote_object():
    """A real ``cdp.runtime.RemoteObject`` — the *other* CDP dataclass
    ``Tab.evaluate`` hands back (its final ``return remote_object``, taken
    whenever the value is falsy, e.g. an empty ``document.body.innerText``)."""
    from nodriver.cdp.runtime import RemoteObject

    return RemoteObject.from_json({"type": "undefined"})


def contains_foreign_object(value) -> bool:
    """True if any node in ``value`` is not plain JSON data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return False
    if isinstance(value, dict):
        return any(contains_foreign_object(v) for v in value.values())
    if isinstance(value, list):
        return any(contains_foreign_object(v) for v in value)
    return True


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


# ===========================================================================
# F-822 — the estimation / spill path must never crash on a foreign object,
# and no raw CDP record may leave in a tool payload.
# ===========================================================================


class TestNonSerializablePayloads:
    def test_estimate_tokens_survives_a_cdp_exception_details(self, tmp_path):
        """RED before the fix: ``json.dumps`` raised ``TypeError: Object of type
        ExceptionDetails is not JSON serializable`` — the Sentry-observed crash."""
        h = ResponseHandler(clone_dir=str(tmp_path))
        payload = {"html": "<p>ok</p>", "text": real_cdp_exception_details()}
        assert h.estimate_tokens(payload) > 0

    def test_estimate_tokens_survives_a_cdp_remote_object(self, tmp_path):
        h = ResponseHandler(clone_dir=str(tmp_path))
        assert h.estimate_tokens({"text": real_cdp_remote_object()}) > 0

    def test_inline_payload_carries_no_raw_cdp_object(self, tmp_path):
        """The small/inline path converts foreign objects to plain data, so the
        MCP layer never has to serialize a dataclass it cannot encode."""
        h = ResponseHandler(max_tokens=100_000, clone_dir=str(tmp_path))
        payload = {"html": "<p>ok</p>", "text": real_cdp_exception_details()}

        result = h.handle_response(payload)

        assert not contains_foreign_object(result)
        json.dumps(result, ensure_ascii=False)  # must not raise

    def test_conversion_preserves_the_cdp_records_own_fields(self, tmp_path):
        """Converted, not stringified into uselessness: the record's own
        ``to_json`` shape survives so the caller can still read the error."""
        h = ResponseHandler(max_tokens=100_000, clone_dir=str(tmp_path))

        result = h.handle_response({"text": real_cdp_exception_details()})

        assert result["text"]["text"] == "Uncaught"
        assert "Cannot read properties of null" in json.dumps(result["text"])

    def test_clean_payload_still_passes_through_by_identity(self, tmp_path):
        """Conversion must not cost the identity contract the suite already
        pins for ordinary payloads."""
        h = ResponseHandler(max_tokens=100_000, clone_dir=str(tmp_path))
        data = {"ok": True, "items": [1, 2, 3]}
        assert h.handle_response(data) is data

    def test_large_non_serializable_payload_spills_valid_json(self, tmp_path):
        """The spill path writes real JSON too — a foreign object must not make
        ``json.dump`` blow up half-way through the file."""
        h = ResponseHandler(max_tokens=10, clone_dir=str(tmp_path))
        payload = {"html": "y" * 5_000, "text": real_cdp_exception_details()}

        result = h.handle_response(payload)

        on_disk = json.loads((tmp_path / result["filename"]).read_text("utf-8"))
        assert on_disk["data"]["html"] == "y" * 5_000
        assert on_disk["data"]["text"]["text"] == "Uncaught"

    def test_unconvertible_object_degrades_to_its_string_form(self, tmp_path):
        """Last resort, never a crash: an object with no JSON shape at all."""

        class Opaque:
            def __repr__(self):
                return "<opaque>"

        h = ResponseHandler(max_tokens=100_000, clone_dir=str(tmp_path))
        assert h.handle_response({"x": Opaque()}) == {"x": "<opaque>"}


class TestGetPageContentDoesNotLeakCdpObjects:
    """F-822 at the tool boundary: the real ``dom_handler.get_page_content``
    stores whatever ``tab.evaluate`` returned under ``text``/``url``/``title``,
    so a throwing page put a CDP record into the tool's own result."""

    async def test_tool_result_is_plain_serializable_data(
        self, tmp_path, call_tool, patched_server
    ):
        from fakes import FakeBrowserManager, FakeTab

        details = real_cdp_exception_details()

        class LeakyTab(FakeTab):
            async def get_content(self):
                return "<html><body></body></html>"

        tab = LeakyTab(evaluate_result=details)
        srv = patched_server(
            browser_manager=FakeBrowserManager(tabs={"i1": tab}),
            response_handler=ResponseHandler(clone_dir=str(tmp_path)),
        )

        result = await call_tool(srv, "get_page_content", instance_id="i1")

        assert not contains_foreign_object(result)
        json.dumps(result, ensure_ascii=False)  # must not raise
        assert result["text"]["text"] == "Uncaught"


# ===========================================================================
# F-837 — the inline threshold must sit BELOW the MCP client's token ceiling.
# ===========================================================================

#: The live-measured response (2026-08-30) that was returned INLINE and then
#: rejected by the client with "result exceeds maximum allowed tokens".
REGRESSION_RESPONSE_CHARS = 59_734

#: Two live-measured payloads that already diverted correctly — the fix may
#: only lower the threshold, never raise it past these.
ALREADY_DIVERTING_CHARS = (int(138.91 * 1024), int(282.83 * 1024))


def payload_of_serialized_length(chars: int) -> dict:
    """A ``{"html": ...}`` payload whose JSON text is exactly ``chars`` long."""
    envelope = len(json.dumps({"html": ""}, ensure_ascii=False))
    payload = {"html": "x" * (chars - envelope)}
    assert len(json.dumps(payload, ensure_ascii=False)) == chars
    return payload


class TestInlineThresholdBelowClientCeiling:
    def test_regression_size_diverts_to_the_file_fallback(self, tmp_path):
        """The measured 59,734-char response must NOT come back inline."""
        h = ResponseHandler(clone_dir=str(tmp_path))
        payload = payload_of_serialized_length(REGRESSION_RESPONSE_CHARS)

        result = h.handle_response(payload, "page_content")

        assert result is not payload
        assert result["reason"].startswith("Response too large")
        assert (tmp_path / result["filename"]).exists()

    @pytest.mark.parametrize("chars", ALREADY_DIVERTING_CHARS)
    def test_previously_diverting_sizes_still_divert(self, chars, tmp_path):
        h = ResponseHandler(clone_dir=str(tmp_path))
        result = h.handle_response(payload_of_serialized_length(chars))
        assert "file_path" in result

    def test_a_genuinely_small_response_stays_inline(self, tmp_path):
        """Lowering the ceiling must not start spilling ordinary results."""
        h = ResponseHandler(clone_dir=str(tmp_path))
        payload = payload_of_serialized_length(4_000)
        assert h.handle_response(payload) is payload
        assert list(tmp_path.glob("*.json")) == []

    def test_default_ceiling_is_the_documented_constant(self, tmp_path):
        """The threshold is a module constant, not a literal sprinkled around,
        and the default handler uses it."""
        assert INLINE_TOKEN_CEILING == 10_000
        assert ResponseHandler(clone_dir=str(tmp_path)).max_tokens == (
            INLINE_TOKEN_CEILING
        )

    def test_the_regression_size_clears_the_ceiling_with_margin(self, tmp_path):
        """Not a knife-edge pass: the failing size is comfortably above the
        threshold, so estimator jitter cannot put it back in the dead band."""
        h = ResponseHandler(clone_dir=str(tmp_path))
        estimated = h.estimate_tokens(
            payload_of_serialized_length(REGRESSION_RESPONSE_CHARS)
        )
        assert estimated > INLINE_TOKEN_CEILING * 1.25
