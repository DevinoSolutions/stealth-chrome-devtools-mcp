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


@pytest.mark.parametrize(
    "line",
    [
        "def login(self, password: str, token: str) -> bool:",
        "client = Client(token=self._token)",
    ],
)
def test_scrub_event_keeps_source_lines_in_a_frame(line):
    """A frame's ``context_line``/``pre_context``/``post_context`` are code. The
    key-value rules read ``password: str`` as a credential and ate the signature;
    a message with a real ``password=hunter2`` is still redacted."""
    event = {
        "exception": {
            "values": [
                {
                    "stacktrace": {
                        "frames": [
                            {
                                "context_line": line,
                                "pre_context": [line],
                                "post_context": [line],
                            }
                        ]
                    }
                }
            ]
        },
        "message": "login failed password=hunter2 retrying",
    }
    out = observability._scrub_event(event)
    assert out is not None
    frame = out["exception"]["values"][0]["stacktrace"]["frames"][0]
    assert frame["context_line"] == line
    assert frame["pre_context"] == [line]
    assert frame["post_context"] == [line]
    assert "hunter2" not in out["message"]


def test_scrub_event_still_runs_shape_rules_on_source_lines():
    line = f"url = 'https://h.example/p?token=abc' ; jwt = '{JWT}'"
    frame = {"context_line": line}
    out = _scrubbed({"exception": {"values": [{"stacktrace": {"frames": [frame]}}]}})
    assert "token=abc" not in out
    assert JWT not in out


@pytest.mark.parametrize(
    ("text", "leaked"),
    [
        ("sent Bearer opaqueTok123 upstream", "opaqueTok123"),
        ("token: Bearer opaqueTok123 end", "opaqueTok123"),
        ('{"token": "Bearer opaqueTok123"}', "opaqueTok123"),
        ("api_key=ak-not-real-1 x", "ak-not-real-1"),
        ("apikey=ak-not-real-2 x", "ak-not-real-2"),
        ("secret_key=sk-not-real-3 x", "sk-not-real-3"),
        ("access_token=at-not-real-4 x", "at-not-real-4"),
        ("refresh_token=rt-not-real-5 x", "rt-not-real-5"),
        ("x-api-key: xk-not-real-6 x", "xk-not-real-6"),
        ("x-auth-token: xt-not-real-7 x", "xt-not-real-7"),
        ('password: "my secret phrase" after', "phrase"),
        ("password: 'two words' after", "words"),
    ],
)
def test_scrub_event_redacts_the_credential_shapes_the_first_cut_missed(text, leaked):
    out = _scrubbed({"message": text})
    assert leaked not in out


def test_scrub_event_quoted_secret_keeps_its_quotes_and_the_prose_after():
    assert (
        observability._redact_text('password: "a b c" and more')
        == 'password: "[redacted-secret]" and more'
    )


def test_scrub_event_leaves_bearer_in_source_alone():
    for text in ('f"Bearer {token}"', "headers = 'Bearer %s' % tok"):
        assert observability._redact_text(text) == text


@pytest.mark.parametrize(
    "hostile",
    [
        "\"password='" * 20000,
        'password: "' + "a" * 100_000,
        "Bearer " * 20000,
        "Bearer " + "a" * 100_000,
        "api_key" * 15000 + "=" + "b" * 50_000,
        "token: Bearer " * 8000,
    ],
    ids=[
        "quote-runs",
        "unterminated-quote",
        "bearer-repeats",
        "bearer-long-token",
        "key-repeats",
        "typed-bearer-repeats",
    ],
)
def test_scrub_event_new_rules_stay_linear_on_100k_hostile_text(hostile):
    start = time.perf_counter()
    observability._redact_text(hostile)
    assert time.perf_counter() - start < 2.0


@pytest.mark.parametrize(
    "headers",
    [
        [["Cookie", "sid=pair-secret-1"], ["Accept", "x"]],
        [("Authorization", "Bearer pair-secret-1"), ("Accept", "x")],
        [
            {"name": "Cookie", "value": "sid=pair-secret-1"},
            {"name": "Accept", "value": "x"},
        ],
        {"X-Api-Key": "pair-secret-1", "Accept": "x"},
        [["x-auth-token", "pair-secret-1"]],
    ],
)
def test_scrub_event_drops_credential_header_values_in_every_header_shape(headers):
    out = _scrubbed({"request": {"headers": headers}})
    assert "pair-secret-1" not in out


def test_scrub_event_keeps_ordinary_headers_in_every_shape():
    event = {
        "request": {
            "headers": [
                ["Accept", "application/json"],
                {"name": "User-Agent", "value": "ua/1"},
            ]
        }
    }
    out = _scrubbed(event)
    assert "application/json" in out and "ua/1" in out


def test_scrub_event_drops_the_request_cookies_entry():
    request = {"cookies": {"sid": "cookie-secret-1"}, "url": "https://h.example/x"}
    out = _scrubbed({"request": request})
    assert "cookie-secret-1" not in out
    assert "h.example" in out


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
def test_existing_loose_default_spill_directory_is_narrowed(tmp_path, monkeypatch):
    monkeypatch.setattr(response_handler, "STATE_DIR", tmp_path)
    spill = tmp_path / "element_clones"
    spill.mkdir()
    spill.chmod(0o755)

    ResponseHandler(clone_dir=str(spill))

    assert stat.S_IMODE(spill.stat().st_mode) == 0o700


@posix_only
def test_existing_configured_directory_keeps_its_mode(tmp_path):
    """``STEALTH_MCP_CLONE_OUTPUT_DIR`` may name a shared group directory or a
    home: it is the user's, and import must not tighten it. Its spill FILES are
    still 0600."""
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o2775)
    before = stat.S_IMODE(shared.stat().st_mode)

    handler = ResponseHandler(max_tokens=10, clone_dir=str(shared))
    result = handler.handle_response({"cookies": "x" * 500})

    assert stat.S_IMODE(shared.stat().st_mode) == before
    assert stat.S_IMODE((shared / result["filename"]).stat().st_mode) == 0o600


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


def _pretend_posix_loose(monkeypatch) -> list[int]:
    """Windows' ``stat`` reports 0777 for every directory, so on the real platform
    this reads as "loose" everywhere; the module-local ``sys`` stands in for POSIX
    so the narrowing branch runs on every developer machine. Returns the list the
    ``chmod`` requests land in."""
    narrowed: list[int] = []
    monkeypatch.setattr(
        response_handler, "sys", types.SimpleNamespace(platform="linux")
    )
    monkeypatch.setattr(
        Path, "stat", lambda self, **_kw: types.SimpleNamespace(st_mode=0o40755)
    )
    monkeypatch.setattr(Path, "chmod", lambda self, mode, **_kw: narrowed.append(mode))
    return narrowed


def test_loose_existing_default_spill_directory_gets_a_chmod_on_posix(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(response_handler, "STATE_DIR", tmp_path)
    spill = tmp_path / "element_clones"
    spill.mkdir()
    narrowed = _pretend_posix_loose(monkeypatch)

    ResponseHandler(clone_dir=str(spill))

    assert narrowed == [0o700]


def test_configured_existing_directory_is_never_chmodded(tmp_path, monkeypatch):
    """The portable pin for the user-owned directory: no ``chmod`` is requested
    for a directory that already existed and is not the default spill dir."""
    monkeypatch.setattr(response_handler, "STATE_DIR", tmp_path / "state")
    narrowed = _pretend_posix_loose(monkeypatch)

    ResponseHandler(clone_dir=str(tmp_path))

    assert narrowed == []


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
