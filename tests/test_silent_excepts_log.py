"""Pinning tests for M10a-7: the 17 truly-silent `except Exception` handlers
in embedded/ now log via the M3 spine (debug_logger.log_warning/log_debug ->
stealth.backend) instead of swallowing silently. One class per sub-step
(7a/7b/7c/7d); each test raises inside the guarded try and asserts a record
now reaches stealth.backend, at the level-appropriate severity per plan_M3's
classification: WARNING for real degraded operations, DEBUG for deliberate
fallback chains / per-event handlers that must stay quiet by default.

`captured_backend_records` matches the fixture already used in
test_debug_logger_file_bridge.py / test_singleton_cold_start_logging.py:
direct handler attachment to "stealth.backend" (not caplog - configure_logging
sets propagate=False), forced to DEBUG so both WARNING and DEBUG sites are
observed uniformly across this file.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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


def _mock_tab(tab_id: str) -> MagicMock:
    tab = MagicMock()
    tab.target.target_id = tab_id
    return tab


# ---------------------------------------------------------------------------
# 7a: browser_manager.py interaction hot path (WARNING - real degraded ops)
# ---------------------------------------------------------------------------


class TestBrowserManagerSilentExcepts:
    @pytest.mark.asyncio
    async def test_switch_to_tab_logs_on_failure(self, captured_backend_records):
        from browser_manager import BrowserManager

        manager = BrowserManager()
        tab = _mock_tab("tab-1")
        tab.bring_to_front = AsyncMock(side_effect=RuntimeError("front-fail"))
        browser = MagicMock()
        browser.tabs = [tab]
        browser.update_targets = AsyncMock()

        with patch.object(manager, "get_browser", AsyncMock(return_value=browser)):
            result = await manager.switch_to_tab("inst-1", "tab-1")

        assert result is False
        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.WARNING
        assert "front-fail" in record.getMessage()

    @pytest.mark.asyncio
    async def test_close_tab_logs_on_failure(self, captured_backend_records):
        from browser_manager import BrowserManager

        manager = BrowserManager()
        tab = _mock_tab("tab-2")
        tab.close = AsyncMock(side_effect=RuntimeError("close-fail"))
        browser = MagicMock()
        browser.tabs = [tab]

        with patch.object(manager, "get_browser", AsyncMock(return_value=browser)):
            result = await manager.close_tab("inst-1", "tab-2")

        assert result is False
        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.WARNING
        assert "close-fail" in record.getMessage()


# ---------------------------------------------------------------------------
# 7a: dom_handler.py deliberate fallback chains (DEBUG - stay quiet by default)
# ---------------------------------------------------------------------------


class TestDomHandlerSilentExcepts:
    @pytest.mark.asyncio
    async def test_click_element_mouse_click_fallback_logs_at_debug(
        self, captured_backend_records
    ):
        from dom_handler import DOMHandler

        element = MagicMock()
        element.scroll_into_view = AsyncMock()
        element.mouse_click = AsyncMock(side_effect=RuntimeError("mouse-fail"))
        element.click = AsyncMock()
        tab = MagicMock()
        tab.select = AsyncMock(return_value=element)

        result = await DOMHandler.click_element(tab, "#btn")

        assert result is True
        element.click.assert_awaited_once()
        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.DEBUG
        assert "mouse-fail" in record.getMessage()

    @pytest.mark.asyncio
    async def test_type_text_clear_fallback_logs_at_debug(
        self, captured_backend_records
    ):
        from dom_handler import DOMHandler

        element = MagicMock()
        element.focus = AsyncMock()
        element.apply = AsyncMock(side_effect=RuntimeError("clear-fail"))
        element.send_keys = AsyncMock()
        tab = MagicMock()
        tab.select = AsyncMock(return_value=element)

        result = await DOMHandler.type_text(tab, "#input", "", delay_ms=0)

        assert result is True
        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.DEBUG
        assert "clear-fail" in record.getMessage()

    @pytest.mark.asyncio
    async def test_paste_text_clear_fallback_logs_at_debug(
        self, captured_backend_records
    ):
        from dom_handler import DOMHandler

        element = MagicMock()
        element.focus = AsyncMock()
        element.apply = AsyncMock(side_effect=RuntimeError("paste-clear-fail"))
        tab = MagicMock()
        tab.select = AsyncMock(return_value=element)
        tab.send = AsyncMock()

        result = await DOMHandler.paste_text(tab, "#input", "hello")

        assert result is True
        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.DEBUG
        assert "paste-clear-fail" in record.getMessage()

    @pytest.mark.asyncio
    async def test_get_page_content_iframe_skip_logs_at_debug(
        self, captured_backend_records
    ):
        from dom_handler import DOMHandler

        good_iframe = MagicMock()
        good_iframe.attrs = {"src": "https://good.example"}

        class _BadAttrs:
            def get(self, *_args, **_kwargs):
                raise RuntimeError("iframe-attrs-fail")

        bad_iframe = MagicMock()
        bad_iframe.attrs = _BadAttrs()

        tab = MagicMock()
        tab.select_all = AsyncMock(return_value=[good_iframe, bad_iframe])
        tab.get_content = AsyncMock(return_value="<html></html>")
        tab.evaluate = AsyncMock(return_value="")

        result = await DOMHandler.get_page_content(tab, include_frames=True)

        assert result["frames"] == [
            {"index": 0, "src": "https://good.example", "id": None, "name": None}
        ]
        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.DEBUG
        assert "iframe-attrs-fail" in record.getMessage()
