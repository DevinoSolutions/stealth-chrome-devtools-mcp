"""The sealed login against a local login site, in a real Chrome (T-19366).

Everything is served from 127.0.0.1 (the vault's loopback test variable is set),
so no real site, account or password is involved. The server records what it
received, which is how a test knows the right text was typed and that nothing was
typed where it must not be.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import secrets
import threading
import urllib.parse

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    sandbox_kwargs,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded import (
    login_flow,
    login_form,
    login_seal,
    tab_open,
    vault_crypto,
    vault_store,
)
from stealth_chrome_devtools_mcp.embedded import tool_runtime as runtime
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from stealth_chrome_devtools_mcp.settings import get_settings
from vault_helpers import (
    LOOPBACK_ENV,
    password_entry,
    provider_entry,
    reset,
    unlocked_vault,
)

pytestmark = integration_pytestmark()

USER = "alice@example.test"
# Random per run, never written down: a test must not put a password in the repo.
RIGHT = secrets.token_urlsafe(18)
WRONG = secrets.token_urlsafe(18)

LOGIN_FORM = """<html><body><form method=post action="{action}">
<input name=username type=text autocomplete=username>
<input name=password type=password autocomplete=current-password>
<button>go</button></form>{note}</body></html>"""
USERNAME_STEP = """<html><body><form method=post action="/two/user">
<input name=username type=email></form></body></html>"""
PASSWORD_STEP = """<html><body><form method=post action="/two/pass">
<input type=hidden name=username value="{user}">
<input name=password type=password></form></body></html>"""


class Site:
    """A tiny login site and a record of what reached it."""

    def __init__(self):
        self.posts: list[dict[str, str]] = []
        self.sessions: set[str] = set()
        self.server = None

    def url(self, path, host="127.0.0.1"):
        return f"http://{host}:{self.server.server_port}{path}"

    def signed_in(self, handler):
        cookie = handler.headers.get("Cookie", "")
        return any(f"sid={sid}" in cookie for sid in self.sessions)


def _handler(site):
    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body="", headers=()):
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/login", "/two/login") and site.signed_in(self):
                return self._send(302, "", [("Location", "/home")])
            if path == "/login":
                return self._send(200, LOGIN_FORM.format(action="/login", note=""))
            if path == "/two/login":
                return self._send(200, USERNAME_STEP)
            if path == "/home":
                if not site.signed_in(self):
                    return self._send(302, "", [("Location", "/login")])
                return self._send(200, "<html><body>HOME-SIGNED-IN</body></html>")
            if path == "/unknown":
                return self._send(
                    200, "<html><body><button>Continue</button></body></html>"
                )
            if path == "/to-other-host":
                target = f"http://localhost:{site.server.server_port}/login"
                return self._send(302, "", [("Location", target)])
            return self._send(404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            fields = dict(
                urllib.parse.parse_qsl(
                    self.rfile.read(length).decode(), keep_blank_values=True
                )
            )
            site.posts.append({"path": self.path, **fields})
            if self.path == "/login":
                if fields.get("username") == USER and fields.get("password") == RIGHT:
                    sid = secrets.token_hex(8)
                    site.sessions.add(sid)
                    return self._send(
                        302,
                        "",
                        [("Set-Cookie", f"sid={sid}; Path=/"), ("Location", "/home")],
                    )
                return self._send(
                    200, LOGIN_FORM.format(action="/login", note="<p>wrong</p>")
                )
            if self.path == "/two/user":
                return self._send(
                    200, PASSWORD_STEP.format(user=fields.get("username", ""))
                )
            if self.path == "/two/pass":
                if fields.get("username") == USER and fields.get("password") == RIGHT:
                    sid = secrets.token_hex(8)
                    site.sessions.add(sid)
                    return self._send(
                        302,
                        "",
                        [("Set-Cookie", f"sid={sid}; Path=/"), ("Location", "/home")],
                    )
                return self._send(200, USERNAME_STEP)
            return self._send(404)

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
    get_settings.cache_clear()
    reset()
    yield
    reset()
    get_settings.cache_clear()


@contextlib.asynccontextmanager
async def _browser(tmp_path):
    spawn, close = get_fn("spawn_browser"), get_fn("close_instance")
    spawned = await spawn(
        headless=True, user_data_dir=str(tmp_path / "login-flow"), **sandbox_kwargs()
    )
    iid = spawned["instance_id"]
    try:
        yield iid
    finally:
        await close(instance_id=iid)


def _entry(site, path="/login", **overrides):
    fields = {
        "site": "127.0.0.1",
        "login_url": site.url(path),
        "hosts": ["127.0.0.1"],
        "username": USER,
        "password": RIGHT,
        **overrides,
    }
    return password_entry(**fields)


async def _tab_ids(iid):
    return [t["tab_id"] for t in await get_fn("list_tabs")(instance_id=iid)]


async def _run(iid, label="work"):
    return await login_flow.run(runtime.browser_manager, iid, label, "test-caller")


async def _is_home(iid, site):
    """A second look from the caller's own tab: is the session in the profile?"""
    await get_fn("navigate")(instance_id=iid, url=site.url("/home"))
    return "HOME-SIGNED-IN" in await eval_js(iid, "return document.body.innerText;")


async def test_a_one_step_login_signs_the_profile_in(site, tmp_path):
    unlocked_vault({"work": _entry(site)})
    async with _browser(tmp_path) as iid:
        before = await _tab_ids(iid)
        result = await _run(iid)
        assert result["outcome"] == "logged_in", result
        assert result["label"] == "work"
        assert RIGHT not in str(result)
        assert set(await _tab_ids(iid)) <= set(before)
        assert await _is_home(iid, site)
    assert site.posts == [{"path": "/login", "username": USER, "password": RIGHT}]
    assert not login_seal.sealing(iid)


async def test_a_two_step_login_signs_in(site, tmp_path):
    unlocked_vault({"work": _entry(site, "/two/login")})
    async with _browser(tmp_path) as iid:
        result = await _run(iid)
        assert result["outcome"] == "logged_in", (result, site.posts)
        assert await _is_home(iid, site)
    assert [p["path"] for p in site.posts] == ["/two/user", "/two/pass"]
    assert site.posts[0]["username"] == USER
    assert site.posts[1]["password"] == RIGHT


async def test_a_wrong_password_is_rejected_and_the_entry_frozen(site, tmp_path):
    unlocked_vault({"work": _entry(site, password=WRONG)})
    async with _browser(tmp_path) as iid:
        result = await _run(iid)
        assert result["outcome"] == "rejected", result
        assert WRONG not in str(result)
        assert vault_store.entry("work")["frozen"]
        # frozen survives a lock and an unlock: it is in the file
        assert vault_store.public_entries()[0]["frozen"]
        with pytest.raises(ToolError) as caught:
            await _run(iid)
        assert "frozen" in str(caught.value)
    assert len(site.posts) == 1  # the second call never reached the page


async def test_an_already_signed_in_profile_says_so(site, tmp_path):
    unlocked_vault({"work": _entry(site)})
    async with _browser(tmp_path) as iid:
        assert (await _run(iid))["outcome"] == "logged_in"
        second = await _run(iid)
        assert second["outcome"] == "already_logged_in", second
    assert len(site.posts) == 1


async def test_an_unknown_form_needs_the_user(site, tmp_path):
    unlocked_vault({"work": _entry(site, "/unknown")})
    async with _browser(tmp_path) as iid:
        login_flow.FIELD_WAIT_S = 2.0
        try:
            result = await _run(iid)
        finally:
            login_flow.FIELD_WAIT_S = 15.0
        assert result["outcome"] == "needs_user", result
    assert site.posts == []


async def test_a_login_page_on_another_host_gets_nothing_typed(site, tmp_path):
    unlocked_vault({"work": _entry(site, "/to-other-host")})
    async with _browser(tmp_path) as iid:
        before = await _tab_ids(iid)
        with pytest.raises(ToolError) as caught:
            await _run(iid)
        assert "not on a host" in str(caught.value)
        assert RIGHT not in str(caught.value)
        assert set(await _tab_ids(iid)) <= set(before)
    assert site.posts == []
    assert not login_seal.any_sealing()


async def test_the_seal_and_the_tab_are_dropped_when_the_flow_raises(
    site, tmp_path, monkeypatch
):
    unlocked_vault({"work": _entry(site)})

    async def boom(tab, kind):
        raise RuntimeError("the page went wrong")

    monkeypatch.setattr(login_form, "find_field", boom)
    async with _browser(tmp_path) as iid:
        before = await _tab_ids(iid)
        with pytest.raises(RuntimeError):
            await _run(iid)
        assert not login_seal.sealing(iid)
        assert set(await _tab_ids(iid)) <= set(before)
    assert audit_outcomes()[-1] == "error"


def audit_outcomes():
    return [
        json.loads(line)["outcome"]
        for line in vault_store.AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    ]


async def test_the_seal_refuses_the_agent_while_the_login_runs(
    site, tmp_path, monkeypatch
):
    """Mid-login, the sealed tab is not in ``list_tabs`` and a tool naming it is
    refused; the login itself is not disturbed."""
    unlocked_vault({"work": _entry(site)})
    seen = {}
    real = login_form.find_field

    async def spying(tab, kind):
        tab_id = str(tab.target.target_id)
        seen["listed"] = tab_id in [
            t["tab_id"] for t in await get_fn("list_tabs")(instance_id=seen["iid"])
        ]
        try:
            await get_fn("get_page_content")(instance_id=seen["iid"], tab_id=tab_id)
        except ToolError as error:
            seen["refused"] = str(error)
        return await real(tab, kind)

    monkeypatch.setattr(login_form, "find_field", spying)
    async with _browser(tmp_path) as iid:
        seen["iid"] = iid
        assert (await _run(iid))["outcome"] == "logged_in"
    assert seen["listed"] is False
    assert seen["refused"] == login_seal.REFUSAL


async def test_a_provider_entry_signs_the_provider_account_in(site, tmp_path):
    unlocked_vault({"work": _entry(site), "shop": provider_entry(via="work")})
    async with _browser(tmp_path) as iid:
        result = await _run(iid, "shop")
        assert result["outcome"] == "use_provider", result
        assert result["label"] == "shop"
        assert await _is_home(iid, site)


async def test_every_attempt_is_audited_without_a_secret(site, tmp_path):
    unlocked_vault({"work": _entry(site)})
    async with _browser(tmp_path) as iid:
        await _run(iid)
    text = vault_store.AUDIT_PATH.read_text(encoding="utf-8")
    assert audit_outcomes() == ["logged_in"]
    assert RIGHT not in text
    assert USER not in text


@pytest.mark.parametrize(
    "argument",
    [
        "-ignore-certificate-errors",
        "/ignore-certificate-errors",
        "--Ignore-Certificate-Errors",
        "-host-resolver-rules=MAP accounts.example.com 127.0.0.1:1",
    ],
)
async def test_a_browser_started_with_another_spelling_of_a_forbidden_flag_is_refused(
    site, tmp_path, argument
):
    """Chrome reads ``-flag``, ``/flag`` and any case as the flag (the review's repro)."""
    unlocked_vault({"work": _entry(site)})
    spawn, close = get_fn("spawn_browser"), get_fn("close_instance")
    spawned = await spawn(
        headless=True,
        user_data_dir=str(tmp_path / "spelling"),
        browser_args=[argument],
        **sandbox_kwargs(),
    )
    iid = spawned["instance_id"]
    try:
        with pytest.raises(ToolError) as caught:
            await _run(iid)
        assert "vault logins are refused" in str(caught.value)
        assert RIGHT not in str(caught.value)
    finally:
        await close(instance_id=iid)
    assert site.posts == []
    assert not login_seal.any_sealing()


async def _wait_for_sealed_tab(iid):
    for _ in range(200):
        if login_seal.sealing(iid) and login_seal._tabs.get(iid):
            await asyncio.sleep(0.3)
            return next(iter(login_seal._tabs[iid]))
        await asyncio.sleep(0.05)
    raise AssertionError("the login never sealed a tab")


async def test_a_cancelled_login_never_has_its_tab_open_while_the_seal_is_down(
    site, tmp_path, monkeypatch
):
    """The review's repro: anyio scopes are level-triggered, so a cancelled login's
    next ``await`` raises again. Cancelled twice, the seal must still outlive the
    tab: at no moment is the sealed tab listed or scriptable."""
    unlocked_vault({"work": _entry(site)})

    async def parked(tab, entry):
        await asyncio.sleep(60)

    monkeypatch.setattr(login_flow, "_sealed_login", parked)
    async with _browser(tmp_path) as iid:
        task = asyncio.create_task(_run(iid))
        sealed = await _wait_for_sealed_tab(iid)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        exposed = []
        for _ in range(300):
            listed = sealed in await _tab_ids(iid)
            try:
                await get_fn("execute_script")(
                    instance_id=iid, tab_id=sealed, script="location.href"
                )
                reached = True
            except ToolError:
                reached = False
            if listed or reached:
                exposed.append((listed, reached, login_seal.sealing(iid)))
            if task.done() and not login_seal.sealing(iid):
                break
            await asyncio.sleep(0.01)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert exposed == []
        assert not login_seal.sealing(iid)
        assert not login_seal.is_sealed(iid, sealed)
        # the tab really is gone, not merely unlisted
        browser = await runtime.browser_manager.get_browser(iid)
        await tab_open.update_targets(browser)
        assert tab_open.find(browser, sealed) is None


async def test_a_tab_that_will_not_close_stays_sealed_and_is_reported(
    site, tmp_path, monkeypatch
):
    unlocked_vault({"work": _entry(site)})
    manager = runtime.browser_manager
    real_send = tab_open.send

    async def refusing_close(instance_id, tab_id):
        return False

    async def send(browser, command, *args):
        if getattr(command, "__name__", "") == "close_target":
            raise RuntimeError("Chrome refused")
        return await real_send(browser, command, *args)

    monkeypatch.setattr(manager, "close_tab", refusing_close)
    monkeypatch.setattr(tab_open, "send", send)
    monkeypatch.setattr(login_flow, "_CLOSE_RETRY_S", 0.01)
    async with _browser(tmp_path) as iid:
        before = await _tab_ids(iid)
        result = await _run(iid)
        assert result["outcome"] == "logged_in", result
        assert result["detail"].endswith(login_flow.UNCLOSED_NOTE)
        assert not login_seal.sealing(iid)
        leftover = list(login_seal._unclosed.get(iid, ()))
        assert len(leftover) == 1
        # still open in Chrome, but hidden from and refused to every tool
        assert set(await _tab_ids(iid)) <= set(before)
        assert leftover[0] not in await _tab_ids(iid)
        with pytest.raises(ToolError):
            await get_fn("execute_script")(
                instance_id=iid, tab_id=leftover[0], script="location.href"
            )
        with pytest.raises(ToolError):
            await get_fn("take_screenshot")(instance_id=iid, tab_id=leftover[0])
