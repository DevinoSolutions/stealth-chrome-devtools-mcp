"""Unit tests for the execute_script safety guard (denylist + size cap).

No browser required — these run in the `unit-tests` CI job (pytest -m
"not integration") across all supported Python versions. They lock in the
foot-guns that previously froze the renderer — synchronous XHR, infinite
loops, blocking dialogs and oversized inline payloads — and confirm that
benign async/DOM code is allowed through unchanged.
"""

import sys
from pathlib import Path

import pytest

# Make embedded/ importable the same way the real entrypoint does.
EMBEDDED_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "stealth_chrome_devtools_mcp"
    / "embedded"
)
if str(EMBEDDED_DIR) not in sys.path:
    sys.path.insert(0, str(EMBEDDED_DIR))

import server
from server import (
    EXECUTE_SCRIPT_TIMEOUT,
    MAX_USER_SCRIPT_BYTES,
    _script_rejection_reason,
)


def _unwrap(fn):
    """FastMCP wraps tool coroutines in a FunctionTool; return the raw coroutine."""
    return getattr(fn, "fn", fn)


# ---------------------------------------------------------------------------
# _script_rejection_reason — pure logic, no browser
# ---------------------------------------------------------------------------


class TestScriptDenylist:
    """Blocking patterns are rejected with an actionable message."""

    @pytest.mark.parametrize(
        "script",
        [
            "var x=new XMLHttpRequest(); x.open('GET', url, false); x.send();",
            "xhr.open('POST', '/api', false)",
            "r.open( 'GET', u , false )",
        ],
    )
    def test_sync_xhr_rejected(self, script):
        reason = _script_rejection_reason(script)
        assert reason is not None
        assert "Synchronous" in reason and "fetch" in reason

    @pytest.mark.parametrize(
        "script",
        [
            "while (true) { work(); }",
            "while(1){}",
            "for (;;) { tick(); }",
            "for(;;){}",
        ],
    )
    def test_infinite_loops_rejected(self, script):
        assert _script_rejection_reason(script) is not None

    @pytest.mark.parametrize(
        "script",
        [
            "alert('hi')",
            "window.confirm('ok?')",
            "const v = prompt('name')",
        ],
    )
    def test_blocking_dialogs_rejected(self, script):
        assert _script_rejection_reason(script) is not None

    def test_oversized_script_rejected(self):
        reason = _script_rejection_reason("a" * (MAX_USER_SCRIPT_BYTES + 1))
        assert reason is not None
        assert "too large" in reason and "upload_file" in reason

    def test_script_exactly_at_limit_allowed(self):
        assert _script_rejection_reason("a" * MAX_USER_SCRIPT_BYTES) is None

    @pytest.mark.parametrize(
        "script",
        [
            "const r = await fetch('/x'); return r.status;",
            "document.querySelector('#f').files.length",
            "window.open('https://example.com', '_blank', 'noopener')",
            "el.setAttribute('aria-hidden', false)",
            "[...document.querySelectorAll('a')].map(a => a.href)",
            "indexedDB; document.title",
        ],
    )
    def test_benign_scripts_allowed(self, script):
        assert _script_rejection_reason(script) is None

    def test_non_string_input_is_ignored(self):
        assert _script_rejection_reason(None) is None
        assert _script_rejection_reason(123) is None

    def test_execute_script_timeout_is_short(self):
        """User JS must fail fast, not inherit the 30s CDP window."""
        assert 0 < EXECUTE_SCRIPT_TIMEOUT <= 15


# ---------------------------------------------------------------------------
# execute_script tool — rejection happens before any browser lookup
# ---------------------------------------------------------------------------


class TestExecuteScriptToolGuard:
    """The tool returns a structured rejection without needing a live instance."""

    @pytest.mark.asyncio
    async def test_sync_xhr_rejected_without_browser(self):
        execute = _unwrap(server.execute_script)
        res = await execute(
            instance_id="no-such-instance",
            script="x.open('GET', u, false); x.send();",
        )
        assert res["success"] is False
        assert res["result"] is None
        assert "Synchronous" in res["error"]

    @pytest.mark.asyncio
    async def test_oversized_rejected_without_browser(self):
        execute = _unwrap(server.execute_script)
        res = await execute(
            instance_id="no-such-instance",
            script="var b='" + ("a" * (MAX_USER_SCRIPT_BYTES + 10)) + "';",
        )
        assert res["success"] is False
        assert "too large" in res["error"]

    @pytest.mark.asyncio
    async def test_benign_script_passes_guard_then_reports_missing_instance(self):
        """A clean script clears the guard and proceeds to instance lookup."""
        execute = _unwrap(server.execute_script)
        with pytest.raises(Exception, match="Instance not found"):
            await execute(instance_id="no-such-instance", script="1 + 1")
