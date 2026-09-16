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

import json
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
        from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager

        manager = BrowserManager()
        browser = MagicMock()
        browser.tabs = [_mock_tab("tab-1")]
        browser.update_targets = AsyncMock()
        # RELEASE-FIX-F (F-775c): the failure seam moved from the Tab-only
        # ``bring_to_front()`` to ``Target.activateTarget`` on the browser
        # connection. The guarantee pinned here is unchanged — the broad handler
        # must LOG, not swallow.
        browser.connection.send = AsyncMock(side_effect=RuntimeError("front-fail"))

        with patch.object(manager, "get_browser", AsyncMock(return_value=browser)):
            result = await manager.switch_to_tab("inst-1", "tab-1")

        assert result is False
        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.WARNING
        assert "front-fail" in record.getMessage()

    @pytest.mark.asyncio
    async def test_close_tab_logs_on_failure(self, captured_backend_records):
        from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager

        manager = BrowserManager()
        browser = MagicMock()
        browser.tabs = [_mock_tab("tab-2")]
        # RELEASE-FIX-F (F-775b): seam moved from the Tab-only ``close()`` to
        # ``Target.closeTarget`` on the browser connection; same guarantee.
        browser.connection.send = AsyncMock(side_effect=RuntimeError("close-fail"))

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
        from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler

        element = MagicMock()
        element.scroll_into_view = AsyncMock()
        element.mouse_click = AsyncMock(side_effect=RuntimeError("mouse-fail"))
        element.click = AsyncMock()
        # F-876: the aim is read once, BEFORE the click. The fallback chain this
        # test pins is unchanged; what it now also proves is that the fallback is
        # LABELLED rather than reported as an ordinary click.
        element.apply = AsyncMock(
            return_value=json.dumps(
                {
                    "rendered": True,
                    "rect": {"left": 0, "top": 0, "width": 10, "height": 10},
                    "point": {"x": 5, "y": 5},
                    "target": {"tag": "button", "id": "btn", "classes": []},
                    "hit": {"tag": "button", "id": "btn", "classes": []},
                    "hit_is_target": True,
                    "disabled": False,
                    "pointer_events": "auto",
                    "visibility": "visible",
                }
            )
        )
        tab = MagicMock()
        tab.select = AsyncMock(return_value=element)

        result = await DOMHandler.click_element(tab, "#btn")

        assert result["dispatch"] == "synthetic"
        element.click.assert_awaited_once()
        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.DEBUG
        assert "mouse-fail" in record.getMessage()

    @pytest.mark.asyncio
    async def test_type_text_clear_fallback_logs_at_debug(
        self, captured_backend_records
    ):
        from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler

        element = MagicMock()
        element.focus = AsyncMock()
        element.apply = AsyncMock(side_effect=RuntimeError("clear-fail"))
        tab = MagicMock()
        tab.select = AsyncMock(return_value=element)
        # F-873: the keyboard clear is now the ONE CDP select-all+Delete
        # (text_entry.clear_via_keyboard), shared with paste_text, instead of
        # two WebDriver private-use codepoints down element.send_keys that CDP
        # never understood. The fallback therefore goes through the tab.
        tab.send = AsyncMock()

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
        from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler

        element = MagicMock()
        element.focus = AsyncMock()
        # F-876: only the CLEAR fails. The read-back is a separate apply on the
        # same element and must still answer, or the tool would raise for an
        # unreadable field before it ever reached the clear fallback this pins.
        reads = iter(["", "hello"])

        async def _apply(js_function, *args, **kwargs):
            if "elem.value = ''" in js_function:
                raise RuntimeError("paste-clear-fail")
            return json.dumps({"editable": False, "text": next(reads)})

        element.apply = _apply
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
        from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler

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
