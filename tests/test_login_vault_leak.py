"""The login vault's leak test (T-19366, design section 8).

A local login page, a vault holding a random 24-character password, one sealed
login that succeeds and one that fails inside the login code. Then every place a
password could land is searched for it, plain and URL-encoded:

* the ``login`` and ``list_logins`` results (and the error the forced failure
  raises);
* ``get_debug_view`` and ``export_debug_logs``;
* ``list_network_requests`` and every request's details;
* the backend's log file;
* a Sentry event built from the forced failure and run through the real
  ``before_send``.

The failure raises an exception whose message CONTAINS the password: the worst
case, a code path that interpolates a secret into an error. The guard under test
is that it still does not get out.

Everything is served from 127.0.0.1, so no real site, account or password is
involved; the password is random per run and never written down.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import logging
import secrets
import threading
import urllib.parse
from pathlib import Path

import pytest
from sentry_sdk.utils import event_from_exception

from e2e_helpers import (
    get_fn,
    integration_pytestmark,
    sandbox_kwargs,
    warmup_once,
)
from stealth_chrome_devtools_mcp import observability
from stealth_chrome_devtools_mcp.embedded import (
    logging_setup,
    login_form,
    login_seal,
    secret_registry,
    vault_crypto,
    vault_store,
)
from stealth_chrome_devtools_mcp.embedded import tool_runtime as runtime
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from stealth_chrome_devtools_mcp.settings import get_settings
from vault_helpers import (
    LOOPBACK_ENV,
    password_entry,
    reset,
    sealed_state,
    spawn_before_the_vault,
    unlocked_vault,
)

pytestmark = integration_pytestmark()

USER = "leak-test-user@example.test"
# 19 random characters plus five that percent-encode, so the URL-encoded form
# differs from the plain one: 24 characters in all.
PASSWORD = secrets.token_urlsafe(14)[:19] + "&=+/%"
FORMS = (PASSWORD, urllib.parse.quote(PASSWORD), urllib.parse.quote_plus(PASSWORD))
assert len(PASSWORD) == 24  # noqa: PLR2004  PERMANENT(the design says 24)

FORM = """<html><body><form method=post action="/login">
<input name=username type=text autocomplete=username>
<input name=password type=password autocomplete=current-password>
<button>go</button></form></body></html>"""


class Site:
    def __init__(self):
        self.sessions: set[str] = set()
        self.posts = 0
        self.server = None

    def url(self, path):
        return f"http://127.0.0.1:{self.server.server_port}{path}"


def _handler(site):
    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body="", headers=()):
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body.encode())

        def _signed_in(self):
            cookie = self.headers.get("Cookie", "")
            return any(f"sid={sid}" in cookie for sid in site.sessions)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/login":
                if self._signed_in():
                    return self._send(302, "", [("Location", "/home")])
                return self._send(200, FORM)
            if path == "/home":
                return self._send(200, "<html><body>HOME</body></html>")
            return self._send(404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode()
            fields = dict(urllib.parse.parse_qsl(body, keep_blank_values=True))
            site.posts += 1
            if fields.get("username") == USER and fields.get("password") == PASSWORD:
                sid = secrets.token_hex(8)
                site.sessions.add(sid)
                return self._send(
                    302,
                    "",
                    [("Set-Cookie", f"sid={sid}; Path=/"), ("Location", "/home")],
                )
            return self._send(200, FORM)

        def log_message(self, *args):
            return

    return Handler


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


@pytest.fixture
def site():
    site = Site()
    site.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(site))
    threading.Thread(target=site.server.serve_forever, daemon=True).start()
    yield site
    site.server.shutdown()
    site.server.server_close()


@pytest.fixture(autouse=True)
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_crypto, "SCRYPT_N", 2**10)
    monkeypatch.setattr(vault_store, "VAULT_PATH", tmp_path / "vault.json")
    monkeypatch.setattr(vault_store, "AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setenv(LOOPBACK_ENV, "1")
    monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    reset()
    yield
    reset()
    get_settings.cache_clear()


@pytest.fixture
def backend_log(tmp_path):
    """The backend's own log handler, writing into the test's directory."""
    role = f"leak{secrets.token_hex(3)}"
    path = logging_setup.configure_logging(role)
    yield path, logging.getLogger(f"stealth.{role}")
    logger = logging.getLogger(f"stealth.{role}")
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


@contextlib.asynccontextmanager
async def _browser(tmp_path):
    spawn, close = get_fn("spawn_browser"), get_fn("close_instance")
    with spawn_before_the_vault():
        spawned = await spawn(
            headless=True, user_data_dir=str(tmp_path / "leak"), **sandbox_kwargs()
        )
    iid = spawned["instance_id"]
    try:
        yield iid
    finally:
        await close(instance_id=iid)


def _leaks(label, text):
    """The forms of the password present in ``text``, as a failure message."""
    return [f"{label} holds {form!r}" for form in FORMS if form in text]


def _json(value):
    return json.dumps(value, default=str)


async def _network_text(iid):
    """``list_network_requests`` and the details of every request it names."""
    listing = await get_fn("list_network_requests")(instance_id=iid)
    pieces = [_json(listing)]
    ids: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            ids.extend(
                node[key]
                for key in ("request_id", "id")
                if isinstance(node.get(key), str)
            )
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(listing)
    pieces += [
        _json(await get_fn("get_request_details")(request_id=request_id))
        for request_id in dict.fromkeys(ids)
    ]
    return " ".join(pieces), len(ids)


async def test_no_password_reaches_a_model_a_log_or_an_error_report(
    site, tmp_path, monkeypatch, backend_log
):
    log_path, logger = backend_log
    unlocked_vault(
        {
            "work": password_entry(
                site="127.0.0.1",
                login_url=site.url("/login"),
                hosts=["127.0.0.1"],
                username=USER,
                password=PASSWORD,
            )
        }
    )
    collected: dict[str, str] = {}

    async with _browser(tmp_path) as iid:
        # 1. one successful sealed login, through the registered tool.
        results = [await get_fn("login")(instance_id=iid, label="work")]
        assert results[0]["outcome"] == "logged_in", results
        assert site.posts == 1
        results.append(await get_fn("list_logins")())
        # The agent's own tab, signed in by the login above: it is armed, so the
        # network record has real traffic to be searched.
        await get_fn("navigate")(instance_id=iid, url=site.url("/home"))

        # 2. a forced failure INSIDE the login, whose message carries the secret.
        raised: list[BaseException] = []

        async def boom(tab, kind):
            raised.append(RuntimeError(f"the page went wrong near {PASSWORD}"))
            raise raised[-1]

        monkeypatch.setattr(login_form, "find_field", boom)
        # Signed out again, so the second login meets the form (and the failure).
        site.sessions.clear()
        with pytest.raises(ToolError) as caught:
            await get_fn("login")(instance_id=iid, label="work")
        # What the caller is told: the class of the failure, not its text.
        assert "RuntimeError" in str(caught.value)
        collected["failure"] = str(caught.value)
        original = raised[0]

        # The same exception, offered to every other sink as if some code path
        # had logged or reported it.
        logger.error("login failed", exc_info=original)
        logger.warning("password was %s", PASSWORD)
        runtime.debug_logger.log_error("leak-test", "failure", original)
        runtime.debug_logger.log_info("leak-test", "canary", f"typed {PASSWORD}")
        event, _hint = event_from_exception(
            (type(original), original, original.__traceback__),
            mechanism={"type": "logging", "handled": True},
        )
        event["message"] = f"login failed: {PASSWORD}"
        event["breadcrumbs"] = {
            "values": [{"message": f"typing {PASSWORD}", "category": "test"}]
        }
        collected["sentry event"] = _json(observability._scrub_event(event, {}))

        collected["tool results"] = _json(results)
        collected["get_debug_view"] = _json(await get_fn("get_debug_view")())
        # format="json" because "auto" switches to pickle once the ring is big,
        # which depends on what ran before; include_all so no entry is cut off.
        exported = await get_fn("export_debug_logs")(
            filename=str(tmp_path / "debug-export.json"),
            format="json",
            include_all=True,
        )
        collected["export_debug_logs"] = Path(exported).read_text(encoding="utf-8")
        collected["network"], request_count = await _network_text(iid)
        assert request_count > 0, (
            "the network capture saw nothing: the check is vacuous"
        )

    for handler in logger.handlers:
        handler.flush()
    collected["backend log file"] = log_path.read_text(encoding="utf-8")
    collected["audit log"] = vault_store.AUDIT_PATH.read_text(encoding="utf-8")

    problems = [
        leak for label, text in collected.items() for leak in _leaks(label, text)
    ]
    assert problems == []

    # The searches above are only worth anything if the secret really was in play:
    # the canaries written with the password in them came out masked, not absent.
    assert secret_registry.MASK in collected["get_debug_view"]
    assert secret_registry.MASK in collected["backend log file"]
    assert secret_registry.MASK in collected["sentry event"]
    assert not login_seal.any_sealing(), sealed_state()
