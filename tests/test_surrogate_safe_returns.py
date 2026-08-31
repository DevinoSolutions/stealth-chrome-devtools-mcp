"""F-823 — a lone surrogate in page content must not crash a tool return.

Sentry STEALTH-CHROME-DEVTOOLS-MCP-4M (release 2.0.6, 2026-08-30) on
``execute_script``::

    PydanticSerializationError: Error serializing to JSON: UnicodeEncodeError:
    'utf-8' codec can't encode character '\\ud83d' in position 5811:
    surrogates not allowed

``\\ud83d`` is the HIGH half of an emoji pair: the page (or a JS
``slice``/``substring`` on a UTF-16 index, or a truncating extractor) handed
back half a character. FastMCP's ``tool.run`` →  ``_convert_to_content`` →
``default_serializer`` → ``pydantic_core.to_json(data, fallback=str)`` UTF-8
encodes the payload, and UTF-8 has no encoding for an unpaired surrogate. The
CDP work had already succeeded; the call died while *encoding* the answer —
and ``execute_script`` never touches ``ResponseHandler``, so the guard has to
sit at the boundary EVERY tool returns through, not in the large-response path.

The tests below therefore drive the real ``pydantic_core.to_json`` call from
the stack trace rather than a stand-in, and reach it through the ``.fn`` seam
of a real tool body so the pin covers the actual return path.
"""

import inspect
import json

import pytest
from pydantic_core import to_json

from stealth_chrome_devtools_mcp.embedded.response_handler import (
    ResponseHandler,
    surrogate_safe,
)

#: The exact character from the Sentry event — the high half of an emoji pair.
LONE_HIGH = "\ud83d"
#: A low half, the other way a slice can break a pair.
LONE_LOW = "\ude00"
#: U+1F600 GRINNING FACE — the WHOLE character the two halves above come from.
#: CPython stores it as ONE code point, never as a pair, so it must survive
#: byte-identically: the repair may not "fix" a character that is not broken.
EMOJI = "\U0001f600"
#: U+1D11E MUSICAL SYMBOL G CLEF — a second astral code point, non-emoji.
ASTRAL = "\U0001d11e"
#: What a broken half must become.
FFFD = "�"


def serializes_like_fastmcp(payload) -> str:
    """The exact call from the Sentry stack trace (``fastmcp/tools/tool.py``
    line 57). Raises what production raised if the payload is not encodable."""
    return to_json(payload, fallback=str).decode()


# ===========================================================================
# The one repair, as a unit: policy, identity, and what it must NOT touch.
# ===========================================================================


class TestSurrogateSafePolicy:
    def test_one_lone_surrogate_becomes_one_replacement_char(self):
        assert surrogate_safe("a" + LONE_HIGH + "b") == "a" + FFFD + "b"

    def test_a_lone_low_half_is_repaired_too(self):
        assert surrogate_safe(LONE_LOW) == FFFD

    def test_surrounding_text_is_preserved_exactly(self):
        broken = "<p>hello " + LONE_HIGH + " world</p>"
        assert surrogate_safe(broken) == "<p>hello " + FFFD + " world</p>"

    def test_a_whole_emoji_is_returned_byte_identical(self):
        text = "score: " + EMOJI + "!"
        assert surrogate_safe(text) is text
        assert serializes_like_fastmcp(surrogate_safe(text)) == json.dumps(
            text, ensure_ascii=False
        )

    def test_a_non_emoji_astral_character_survives(self):
        text = ASTRAL * 3
        assert surrogate_safe(text) is text

    def test_plain_ascii_payload_keeps_its_identity(self):
        data = {"success": True, "result": "<html>ok</html>", "error": None}
        assert surrogate_safe(data) is data

    def test_bmp_non_ascii_payload_keeps_its_identity(self):
        data = {"title": "Ünicode — naïve café, 日本語"}
        assert surrogate_safe(data) is data

    def test_repair_reaches_nested_containers(self):
        data = {"rows": [{"text": "x" + LONE_HIGH}, {"text": "clean"}]}
        assert surrogate_safe(data) == {
            "rows": [{"text": "x" + FFFD}, {"text": "clean"}]
        }

    def test_repair_reaches_dictionary_keys(self):
        assert surrogate_safe({"k" + LONE_HIGH: 1}) == {"k" + FFFD: 1}

    def test_non_string_leaves_are_left_alone(self):
        """Conservative on purpose: this is the ENCODING guard, not the
        type converter — an object it does not understand passes through
        untouched so no tool's payload shape changes."""

        class Opaque:
            pass

        opaque = Opaque()
        assert surrogate_safe({"o": opaque, "n": 3, "f": 1.5, "b": None})["o"] is opaque

    def test_a_bare_string_return_is_repaired(self):
        """Four tools return a bare ``str``; the boundary must cover them."""
        assert surrogate_safe(LONE_HIGH + "tail") == FFFD + "tail"

    def test_a_deeply_nested_payload_does_not_blow_the_stack(self):
        data = {"x": 1}
        for _ in range(200):
            data = {"nested": data}
        surrogate_safe(data)  # must not raise


# ===========================================================================
# (e) Through a real tool body — the boundary the Sentry event died at.
# ===========================================================================


def leaky_server(patched_server, script_value):
    """``execute_script`` over a fake tab whose JS returns ``script_value``."""
    from fakes import FakeBrowserManager, FakeTab

    tab = FakeTab(evaluate_result=script_value)
    return patched_server(browser_manager=FakeBrowserManager(tabs={"i1": tab}))


class TestExecuteScriptReturnBoundary:
    async def test_lone_surrogate_result_is_encodable_by_the_mcp_serializer(
        self, call_tool, patched_server
    ):
        """RED before the fix: ``UnicodeEncodeError: 'utf-8' codec can't encode
        character '\\ud83d' ... surrogates not allowed`` — the Sentry crash."""
        srv = leaky_server(patched_server, "prefix" + LONE_HIGH + "suffix")

        result = await call_tool(
            srv, "execute_script", instance_id="i1", script="document.title"
        )

        serializes_like_fastmcp(result)  # must not raise

    async def test_the_broken_half_reaches_the_client_as_u_fffd(
        self, call_tool, patched_server
    ):
        """Lossy but visible, and only where the loss happened: the rest of the
        page content is delivered rather than the whole call failing."""
        srv = leaky_server(patched_server, "prefix" + LONE_HIGH + "suffix")

        result = await call_tool(
            srv, "execute_script", instance_id="i1", script="document.title"
        )

        assert result == {
            "success": True,
            "result": "prefix" + FFFD + "suffix",
            "error": None,
        }

    async def test_a_whole_emoji_result_passes_through_untouched(
        self, call_tool, patched_server
    ):
        value = "reaction: " + EMOJI + " " + ASTRAL
        srv = leaky_server(patched_server, value)

        result = await call_tool(
            srv, "execute_script", instance_id="i1", script="document.title"
        )

        assert result["result"] == value
        assert result["result"] is value  # byte-identical, not re-built

    async def test_an_ordinary_result_is_unchanged(self, call_tool, patched_server):
        value = {"rows": [1, 2, 3], "html": "<p>plain ascii</p>"}
        srv = leaky_server(patched_server, value)

        result = await call_tool(
            srv, "execute_script", instance_id="i1", script="document.title"
        )

        assert result["result"] is value

    async def test_the_wrapper_preserves_the_schema_fastmcp_introspects(
        self, patched_server
    ):
        """``functools.wraps`` all the way down: an extra return-path wrapper
        must not cost the tool its name, docstring or signature."""
        srv = patched_server()
        fn = srv.execute_script.fn

        assert fn.__name__ == "execute_script"
        assert "Execute JavaScript source" in fn.__doc__
        assert list(inspect.signature(fn).parameters) == [
            "instance_id",
            "script",
            "args",
            "timeout_ms",
        ]


# ===========================================================================
# (c) The file-fallback write path — the same encoder, one layer down.
# ===========================================================================


class TestFileFallbackWritePath:
    def test_spill_file_is_written_when_the_data_holds_a_lone_surrogate(self, tmp_path):
        """RED before the fix: ``json.dump(..., ensure_ascii=False)`` into a
        ``utf-8`` file raised ``UnicodeEncodeError`` and left a truncated
        spill file behind."""
        h = ResponseHandler(max_tokens=10, clone_dir=str(tmp_path))
        payload = {"html": "y" * 500 + LONE_HIGH}

        result = h.handle_response(payload, "page_content")

        on_disk = json.loads((tmp_path / result["filename"]).read_text("utf-8"))
        assert on_disk["data"]["html"] == "y" * 500 + FFFD

    def test_caller_metadata_is_repaired_on_the_spill_path_too(self, tmp_path):
        """The descriptor's metadata is caller-supplied and never went through
        the payload conversion — it reaches the same utf-8 encoder."""
        h = ResponseHandler(max_tokens=10, clone_dir=str(tmp_path))

        result = h.handle_response(
            {"html": "y" * 500}, "page_content", {"title": "tab " + LONE_HIGH}
        )

        on_disk = json.loads((tmp_path / result["filename"]).read_text("utf-8"))
        assert on_disk["metadata"]["title"] == "tab " + FFFD

    def test_the_inline_path_repairs_too(self, tmp_path):
        h = ResponseHandler(max_tokens=100_000, clone_dir=str(tmp_path))
        assert h.handle_response({"html": "ok" + LONE_HIGH}) == {"html": "ok" + FFFD}

    def test_a_clean_payload_still_passes_through_by_identity(self, tmp_path):
        h = ResponseHandler(max_tokens=100_000, clone_dir=str(tmp_path))
        data = {"ok": True, "items": [1, 2, 3], "emoji": EMOJI}
        assert h.handle_response(data) is data

    def test_a_whole_emoji_survives_the_spill_file_byte_identically(self, tmp_path):
        h = ResponseHandler(max_tokens=10, clone_dir=str(tmp_path))
        payload = {"html": "y" * 500 + EMOJI}

        result = h.handle_response(payload, "page_content")

        on_disk = json.loads((tmp_path / result["filename"]).read_text("utf-8"))
        assert on_disk["data"]["html"] == "y" * 500 + EMOJI


@pytest.mark.parametrize(
    "payload",
    [
        LONE_HIGH,
        {"a": LONE_LOW},
        ["ok", LONE_HIGH + LONE_LOW],
        {"nested": [{"deep": "t" + LONE_HIGH}]},
    ],
)
def test_every_repaired_payload_is_encodable_by_the_mcp_serializer(payload):
    serializes_like_fastmcp(surrogate_safe(payload))  # must not raise
