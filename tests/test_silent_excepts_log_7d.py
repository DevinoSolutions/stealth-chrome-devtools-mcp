"""Pinning tests for M10a-7d: server.py silent excepts now log (F-181 rows
15-17, the final three of the 17-site sweep). See test_silent_excepts_log.py
(7a) for the fixture/rationale this file continues.

server.py:181 (_install_nodriver_cookie_compat) and server.py:1001
(_client_session_seed) are classified WARNING: both are real degraded
conditions (a nodriver internals import failing at startup; the MCP client
roots API failing at session-seed time), not routine/expected cases.

server.py:1249 (apply_disabled_sections) is classified DEBUG: removing a
tool that's already been removed by another section's disable policy is the
documented, expected case (see the existing comment) and must stay quiet by
default.
"""

import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

EMBEDDED_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "stealth_chrome_devtools_mcp"
    / "embedded"
)
if str(EMBEDDED_DIR) not in sys.path:
    sys.path.insert(0, str(EMBEDDED_DIR))

import server


@pytest.fixture()
def captured_backend_records():
    records = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("stealth.backend")
    handler = _ListHandler()
    logger.addHandler(handler)
    prior_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)


class TestInstallNodriverCookieCompatSilentExcept:
    def test_import_failure_logs_at_warning(self, captured_backend_records):
        with patch.dict(sys.modules, {"nodriver.cdp.network": None}):
            server._install_nodriver_cookie_compat()

        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.WARNING


class TestClientSessionSeedSilentExcept:
    @pytest.mark.asyncio
    async def test_list_roots_failure_logs_at_warning(
        self, captured_backend_records, monkeypatch
    ):
        # Force the configured-key fast path to miss so execution reaches the
        # try/except around context.list_roots().
        monkeypatch.setattr(
            server.get_settings(), "stealth_chrome_profile_key", None, raising=False
        )
        monkeypatch.setattr(
            server.get_settings(), "browser_profile_key", None, raising=False
        )
        monkeypatch.setattr(
            server.get_settings(), "codex_workspace", None, raising=False
        )
        monkeypatch.setattr(
            server.get_settings(), "claude_project_dir", None, raising=False
        )

        def _boom_get_context():
            raise RuntimeError("no active MCP context")

        with patch(
            "fastmcp.server.dependencies.get_context", side_effect=_boom_get_context
        ):
            await server._client_session_seed()

        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.WARNING
        assert "no active MCP context" in record.getMessage()


class TestApplyDisabledSectionsSilentExcept:
    def test_already_removed_tool_logs_at_debug(self, captured_backend_records):
        section = "__m10a_test_section__"
        tool_name = "__m10a_test_tool__"
        server.DISABLED_SECTIONS.add(section)
        server.SECTION_TOOLS[section].append(tool_name)
        try:
            with patch.object(
                server.mcp,
                "remove_tool",
                side_effect=KeyError(tool_name),
            ):
                server.apply_disabled_sections()
        finally:
            server.DISABLED_SECTIONS.discard(section)
            server.SECTION_TOOLS.pop(section, None)

        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.DEBUG
