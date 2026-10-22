"""F-I / F-J / T-6534: a script body or a credential must not leave through a log,
a Sentry event, or a world-readable spill file.

Three surfaces, one defect class (secrets reach a place a third party can read):

* **the durable log** — ``cdp_function_executor`` logged translated script
  bodies at INFO on ``stealth.backend``. That logger is written to
  ``~/.stealth-mcp/logs`` and ``LoggingIntegration`` turns INFO into a Sentry
  breadcrumb on the next error event. A script such as ``document.cookie='sid=…'``
  therefore shipped verbatim. The pins listen on the logger itself, so they see
  exactly what the file handler and the breadcrumb hook see.
* **the Sentry scrubber** — ``_scrub_event`` had no rule for credential shapes, so
  even a body that did get logged could not be caught afterwards. Pinned as a
  pure ``event -> event`` function, like ``test_observability_scrubbing``.
* **the spill file** — over-size tool results are written to the clone directory
  as plain JSON; they can hold cookies and headers.

The secret strings below are made up. A fixture that scrubbed a real token would
pass for the wrong reason on one machine only.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from stealth_chrome_devtools_mcp import observability
from stealth_chrome_devtools_mcp.embedded import response_handler
from stealth_chrome_devtools_mcp.embedded.cdp_function_executor import (
    CDPFunctionExecutor,
)
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.response_handler import ResponseHandler

BODY_SECRET = "SECRETVALUE-9f3a1c"
SCRIPT = f"document.cookie = 'sid={BODY_SECRET}'\nlen('{BODY_SECRET}')"

# header.payload.signature, each a real base64url segment ("{"alg":"none"}" etc.)
JWT = (
    "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."
    "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
    "c2lnbmF0dXJlLXZhbHVlLTEyMzQ1"
)

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX mode bits: Windows has no 0700/0600, the profile ACL protects it",
)


class _Records(logging.Handler):
    """Collects every record the backend logger emits, at every level."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def backend_records():
    logger = logging.getLogger("stealth.backend")
    handler = _Records()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


@pytest.fixture
def fake_py2js(monkeypatch):
    """``py2js`` is an optional extra and is absent from the test env; the two log
    sites that name it only run when it imports, so give them one. ``convert``
    echoes the body, which is the worst case for a log line."""
    module = types.ModuleType("py2js")
    module.convert = lambda code: code
    monkeypatch.setitem(sys.modules, "py2js", module)
    return module


# ---------------------------------------------------------------------------
# F-I: the executor logs a length and a digest, never the body
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_execute_python_in_browser_logs_no_script_body(
    backend_records, fake_py2js
):
    executor = CDPFunctionExecutor()
    executor.inject_and_execute_script = AsyncMock(return_value={"success": True})

    await executor.execute_python_in_browser(MagicMock(), SCRIPT)

    assert backend_records.messages, "the test saw no backend log at all"
    joined = "\n".join(backend_records.messages)
    assert BODY_SECRET not in joined
    assert "document.cookie" not in joined
    # what IS left identifies the body without containing it
    assert "Translated JS: " in joined
    assert "py2js generated: " in joined
    assert "chars, sha256:" in joined


def test_translation_failure_context_carries_no_python_body(monkeypatch, fake_py2js):
    """The error path's ``{"python_code": ...}`` context goes to the in-memory ring
    that ``export_debug_logs`` writes out; it must not hold the body either."""

    def boom(_code):
        raise ValueError("py2js exploded")

    monkeypatch.setattr(fake_py2js, "convert", boom)
    seen: list[object] = []
    monkeypatch.setattr(
        debug_logger,
        "log_error",
        lambda _component, _method, _error, context=None: seen.append(context),
    )

    CDPFunctionExecutor()._translate_python_to_js(SCRIPT)

    assert seen, "the error path did not run"
    assert BODY_SECRET not in json.dumps(seen, default=str)


@pytest.mark.asyncio
async def test_execute_cdp_command_logs_param_names_not_values(backend_records):
    """``Network.setCookie`` / ``setExtraHTTPHeaders`` / ``Runtime.evaluate`` params
    are credentials and script bodies by construction."""
    tab = MagicMock()
    tab.send = AsyncMock(return_value=None)

    await CDPFunctionExecutor().execute_cdp_command(
        tab,
        "Network.setCookie",
        {"name": "sid", "value": BODY_SECRET, "domain": "example.test"},
    )

    joined = "\n".join(backend_records.messages)
    assert "Executed Network.setCookie" in joined
    assert BODY_SECRET not in joined
    assert "domain" in joined  # the parameter NAMES are still diagnostic


def test_fingerprint_is_stable_and_hides_the_body():
    first = debug_logger.fingerprint(SCRIPT)
    assert first == debug_logger.fingerprint(SCRIPT)
    assert first != debug_logger.fingerprint(SCRIPT + " ")
    assert first.startswith(f"{len(SCRIPT)} chars, sha256:")
    assert BODY_SECRET not in first


# ---------------------------------------------------------------------------
# F-I / T-6534: the Sentry scrubber has a credential rule
# ---------------------------------------------------------------------------
def _scrubbed(event: dict) -> str:
    out = observability._scrub_event(event)
    assert out is not None
    return json.dumps(out)


def test_scrub_event_drops_credential_header_values_by_name():
    out = _scrubbed(
        {
            "request": {
                "headers": {
                    "Authorization": "Bearer opaque-not-a-jwt-123",
                    "cookie": "sid=abc123; theme=dark",
                    "Set-Cookie": "sid=abc123; HttpOnly",
                    "Proxy-Authorization": "Basic dXNlcjpwYXNz",
                    "Accept": "application/json",
                }
            }
        }
    )
    for leaked in ("opaque-not-a-jwt-123", "abc123", "dXNlcjpwYXNz", "dark"):
        assert leaked not in out
    assert "application/json" in out  # an ordinary header is untouched


def test_scrub_event_redacts_a_typed_secret_in_a_message():
    out = _scrubbed(
        {"logentry": {"message": "refresh failed token=abc123XYZ retrying"}}
    )
    assert "abc123XYZ" not in out
    assert "refresh failed" in out
    assert "retrying" in out  # one whitespace-free run: the prose survives


@pytest.mark.parametrize(
    "text",
    [
        'password: "hunter2-not-real"',
        "client_secret=s3cr3t-value-not-real",
        '{"token": "tok-not-real-1"}',
    ],
)
def test_scrub_event_redacts_each_typed_secret_shape(text):
    out = _scrubbed({"message": text})
    for leaked in ("hunter2-not-real", "s3cr3t-value-not-real", "tok-not-real-1"):
        assert leaked not in out


def test_scrub_event_redacts_a_jwt_in_a_breadcrumb():
    out = _scrubbed(
        {
            "breadcrumbs": {
                "values": [
                    {
                        "category": "stealth.backend",
                        "level": "info",
                        "message": f"saw {JWT} in storage",
                    }
                ]
            }
        }
    )
    assert JWT not in out
    assert "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0" not in out
    assert "saw " in out and " in storage" in out


def test_scrub_event_redacts_a_cookie_header_in_a_breadcrumb_and_a_script():
    out = _scrubbed(
        {
            "breadcrumbs": {
                "values": [
                    {"message": "sent Cookie: sid=abc123; other=zzz999 to host"},
                    {"message": "Translated JS: document.cookie='sid=abc123'"},
                ]
            }
        }
    )
    assert "abc123" not in out
    assert "zzz999" not in out


def test_scrub_event_keeps_what_is_not_a_credential():
    message = "max_tokens=5 reached; tokens left 12; eyJ short; cookies enabled"
    assert observability._redact_text(message) == message


def test_scrub_event_secret_rules_stay_linear_on_hostile_text():
    """The module's own rule for every regex in it: no quadratic scan on page
    content quoted into an error."""
    hostile = ("token" * 40000) + (" cookie" * 5000) + ("eyJ" + "a" * 100000)
    start = time.perf_counter()
    observability._redact_text(hostile)
    assert time.perf_counter() - start < 2.0


# ---------------------------------------------------------------------------
# F-J: the spill directory and file are owner-only
# ---------------------------------------------------------------------------
@posix_only
def test_spill_file_is_owner_only(tmp_path):
    handler = ResponseHandler(max_tokens=10, clone_dir=str(tmp_path / "spill"))

    result = handler.handle_response({"cookies": "x" * 500})

    mode = stat.S_IMODE((tmp_path / "spill" / result["filename"]).stat().st_mode)
    assert mode == 0o600


@posix_only
def test_spill_directory_is_created_owner_only(tmp_path):
    ResponseHandler(clone_dir=str(tmp_path / "fresh" / "spill"))

    assert stat.S_IMODE((tmp_path / "fresh" / "spill").stat().st_mode) == 0o700


@posix_only
def test_existing_loose_spill_directory_is_narrowed(tmp_path):
    spill = tmp_path / "spill"
    spill.mkdir()
    spill.chmod(0o755)

    ResponseHandler(clone_dir=str(spill))

    assert stat.S_IMODE(spill.stat().st_mode) == 0o700


def test_spill_requests_owner_only_modes_on_every_platform(tmp_path, monkeypatch):
    """The mode-bit tests above cannot run on Windows, so pin the REQUEST: the
    file is opened with 0600 and a new directory is made with 0700. What the OS
    then does with the bits is the OS's; asking for them is ours."""
    opened: list[int] = []
    real_open = os.open

    def spy_open(path, flags, mode=0o777, **kwargs):
        opened.append(mode)
        return real_open(path, flags, mode, **kwargs)

    made: list[int] = []
    real_mkdir = Path.mkdir

    def spy_mkdir(self, mode=0o777, parents=False, exist_ok=False):
        made.append(mode)
        return real_mkdir(self, mode, parents, exist_ok)

    monkeypatch.setattr(response_handler.os, "open", spy_open)
    monkeypatch.setattr(Path, "mkdir", spy_mkdir)

    handler = ResponseHandler(max_tokens=10, clone_dir=str(tmp_path / "spill"))
    handler.handle_response({"cookies": "x" * 500})

    assert made[0] == 0o700
    assert opened == [0o600]


def test_loose_existing_spill_directory_gets_a_chmod_on_posix(tmp_path, monkeypatch):
    """Windows' ``stat`` reports 0777 for every directory, so on the real platform
    this reads as "loose" everywhere; the module-local ``sys`` stands in for POSIX
    so the narrowing branch runs on every developer machine."""
    narrowed: list[int] = []
    monkeypatch.setattr(
        response_handler, "sys", types.SimpleNamespace(platform="linux")
    )
    monkeypatch.setattr(
        Path, "stat", lambda self, **_kw: types.SimpleNamespace(st_mode=0o40755)
    )
    monkeypatch.setattr(Path, "chmod", lambda self, mode, **_kw: narrowed.append(mode))

    ResponseHandler(clone_dir=str(tmp_path))

    assert narrowed == [0o700]


def test_spill_content_is_unchanged_by_the_private_write(tmp_path):
    """Runs everywhere: the hardened write path still round-trips the payload."""
    handler = ResponseHandler(max_tokens=10, clone_dir=str(tmp_path / "spill"))
    data = {"blob": "é" * 200}

    result = handler.handle_response(data, metadata={"tool": "t"})

    on_disk = json.loads(
        (tmp_path / "spill" / result["filename"]).read_text(encoding="utf-8")
    )
    assert on_disk["data"] == data
    assert on_disk["metadata"]["tool"] == "t"
