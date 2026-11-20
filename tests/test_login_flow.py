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
    login_persistence,
    login_seal,
    tab_open,
    text_entry,
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
    sealed_state,
    spawn_before_the_vault,
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
#: A single-page app: Enter blanks the password field at once, the request goes
#: out ``DELAY`` ms later, and the page then signs in (a navigation to /home) or
#: shows an alert, all in the same document.
SPA = """<html><body><form id=f>
<input name=username type=text autocomplete=username>
<input name=password type=password autocomplete=current-password>
<button>go</button></form><div id=out></div><script>
f.onsubmit = async (e) => {{
  e.preventDefault();
  const u = f.username.value, p = f.password.value;
  f.password.value = '';
  await new Promise(r => setTimeout(r, {delay}));
  const r = await fetch('/spa-api', {{method: 'POST',
    body: new URLSearchParams({{username: u, password: p}})}});
  if (r.ok) {{ location.href = '/home'; return; }}
  const d = document.createElement('div');
  d.setAttribute('role', 'alert'); d.textContent = 'That password is wrong';
  out.appendChild(d);
}};
</script></body></html>"""
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
            if path in (
                "/login",
                "/two/login",
                "/spa-slow",
                "/spa-wrong",
                "/locked-cookie-login",
            ) and site.signed_in(self):
                return self._send(302, "", [("Location", "/home")])
            if path == "/login":
                return self._send(200, LOGIN_FORM.format(action="/login", note=""))
            if path == "/wrong-action":
                return self._send(
                    200,
                    LOGIN_FORM.format(action="https://evil.example/collect", note=""),
                )
            if path == "/framed-login":
                return self._send(
                    200,
                    '<html><body><iframe src="/login" width=400 height=300>'
                    "</iframe></body></html>",
                )
            if path == "/locked-login":
                return self._send(
                    200, LOGIN_FORM.format(action="/locked-login", note="")
                )
            if path == "/locked-cookie-login":
                return self._send(
                    200, LOGIN_FORM.format(action="/locked-cookie-login", note="")
                )
            if path in ("/spa-slow", "/spa-wrong"):
                delay = 3000 if path == "/spa-slow" else 800
                return self._send(200, SPA.format(delay=delay))
            if path == "/locked-page":
                return self._send(
                    200, "<html><body>Too many attempts. Account locked.</body></html>"
                )
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
            if self.path == "/spa-api":
                if fields.get("username") == USER and fields.get("password") == RIGHT:
                    sid = secrets.token_hex(8)
                    site.sessions.add(sid)
                    return self._send(200, "ok", [("Set-Cookie", f"sid={sid}; Path=/")])
                return self._send(401, "no")
            if self.path == "/locked-cookie-login":
                # the WRONG password, answered by a form-less page that sets a
                # cookie (sites set a CSRF or balancer cookie on nearly every reply)
                return self._send(
                    302,
                    "",
                    [
                        ("Set-Cookie", f"csrf={secrets.token_hex(8)}; Path=/"),
                        ("Location", "/locked-page"),
                    ],
                )
            if self.path == "/locked-login":
                # a form-less page on the same host, and no session cookie
                return self._send(302, "", [("Location", "/locked-page")])
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


#: The longest a browser's spawn or close may take in these tests.
STEP_BOUND_S = 90.0


@contextlib.asynccontextmanager
async def _browser(tmp_path):
    spawn, close = get_fn("spawn_browser"), get_fn("close_instance")
    # A step that never answers must fail with its name (the macOS publish gate
    # sat 300 s in one, and pytest-timeout's signal mode dumps no main-thread
    # stack), so the two ends of a browser's life are bounded here.
    try:
        async with asyncio.timeout(STEP_BOUND_S):
            with spawn_before_the_vault():
                spawned = await spawn(
                    headless=True,
                    user_data_dir=str(tmp_path / "login-flow"),
                    **sandbox_kwargs(),
                )
    except TimeoutError:
        raise AssertionError(f"spawn_browser took over {STEP_BOUND_S:g} s") from None
    iid = spawned["instance_id"]
    try:
        yield iid
    finally:
        try:
            async with asyncio.timeout(STEP_BOUND_S):
                await close(instance_id=iid)
        except TimeoutError:
            raise AssertionError(
                f"close_instance took over {STEP_BOUND_S:g} s"
            ) from None


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
        assert result["closed_devtools_windows"] == 0  # F5: always said
        assert RIGHT not in str(result)
        assert set(await _tab_ids(iid)) <= set(before)
        assert await _is_home(iid, site)
    assert site.posts == [{"path": "/login", "username": USER, "password": RIGHT}]
    assert not login_seal.sealing(iid)


async def test_a_dropped_key_is_needs_user_and_never_a_wrong_submit_or_a_freeze(
    site, tmp_path, monkeypatch
):
    """Headed Chrome once dropped the first key of the first field (the username
    reached the site one character short, the site rejected it and the entry was
    FROZEN). A field that does not hold what was typed must end the login as
    ``needs_user`` with nothing submitted and the entry untouched."""
    unlocked_vault({"work": _entry(site)})
    real = text_entry.press_key
    sent = []

    async def dropping_first(tab, **kwargs):
        sent.append(kwargs.get("key"))
        if len(sent) == 1:
            return None  # the key that never arrives
        return await real(tab, **kwargs)

    monkeypatch.setattr(text_entry, "press_key", dropping_first)
    async with _browser(tmp_path) as iid:
        result = await _run(iid)
        assert result["outcome"] == "needs_user", result
        assert "username" in result["detail"]
        assert USER not in str(result) and RIGHT not in str(result)
    assert site.posts == []
    assert not vault_store.entry("work").get("frozen")
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


async def test_a_form_less_page_with_no_session_is_not_a_login(site, tmp_path):
    """The reviewer's case: the POST is answered with an account-locked page that
    has no form. A new document is not evidence of a session: ``needs_user``, not
    ``logged_in`` and not ``rejected``, and the entry is not frozen."""
    unlocked_vault({"work": _entry(site, "/locked-login")})
    async with _browser(tmp_path) as iid:
        result = await _run(iid)
        assert result["outcome"] == "needs_user", result
        assert "no sign of a session" in result["detail"]
        assert not vault_store.entry("work").get("frozen")
        assert not await _is_home(iid, site)
    assert [p["path"] for p in site.posts] == ["/locked-login"]


async def test_a_locked_page_that_sets_a_cookie_is_not_a_login(site, tmp_path):
    """Re-review M4': a wrong password, a form-less "locked" page, and a cookie the
    site sets on every reply. The cookie is new, but the login page, visited again,
    still offers its form: ``needs_user``, never ``logged_in``, and not frozen."""
    unlocked_vault({"work": _entry(site, "/locked-cookie-login", password=WRONG)})
    async with _browser(tmp_path) as iid:
        result = await _run(iid)
        assert result["outcome"] == "needs_user", result
        assert not vault_store.entry("work").get("frozen")
        assert not await _is_home(iid, site)


async def test_a_slow_single_page_app_that_blanks_the_password_is_not_rejected(
    site, tmp_path
):
    """Re-review L7': the app empties the password field on Enter and signs in three
    seconds later. An emptied field is not a rejection: the right password signs in
    and the entry is not frozen."""
    unlocked_vault({"work": _entry(site, "/spa-slow")})
    async with _browser(tmp_path) as iid:
        result = await _run(iid)
        assert result["outcome"] == "logged_in", result
        assert not vault_store.entry("work").get("frozen")
        assert await _is_home(iid, site)


async def test_a_single_page_app_that_shows_an_error_is_a_rejection(site, tmp_path):
    unlocked_vault({"work": _entry(site, "/spa-wrong", password=WRONG)})
    async with _browser(tmp_path) as iid:
        result = await _run(iid)
        assert result["outcome"] == "rejected", result
        assert vault_store.entry("work")["frozen"]


async def test_a_freeze_that_cannot_be_written_is_not_reported_as_done(
    site, tmp_path, monkeypatch
):
    """A rejected login whose entry could not be frozen must say so: the caller is
    told not to retry, and the detail never claims "now frozen"."""
    unlocked_vault({"work": _entry(site, password=WRONG)})

    def broken(_label):
        raise OSError("disk full")

    monkeypatch.setattr(vault_store, "freeze", broken)
    async with _browser(tmp_path) as iid:
        result = await _run(iid)
        assert result["outcome"] == "rejected", result
        assert "could NOT be frozen" in result["detail"]
        assert "now frozen" not in result["detail"]
        assert not vault_store.entry("work").get("frozen")


async def test_a_form_that_submits_to_another_origin_gets_nothing_typed(site, tmp_path):
    unlocked_vault({"work": _entry(site, "/wrong-action")})
    async with _browser(tmp_path) as iid:
        with pytest.raises(ToolError) as caught:
            await _run(iid)
        assert "not on a host" in str(caught.value)
        assert RIGHT not in str(caught.value)
    assert site.posts == []
    assert not login_seal.any_sealing(), sealed_state()


async def test_a_login_form_framed_by_another_page_is_not_typed_into(
    site, tmp_path, monkeypatch
):
    """The field's own frame is on a login host, but the page around it is not:
    the top frame is checked too. The framing page here is the SAME host the test
    site serves from, so the top URL is made to read as another origin."""
    unlocked_vault({"work": _entry(site, "/framed-login")})
    real = login_flow._frame_url

    def framed_by_a_stranger(tree, frame_id):
        url = real(tree, frame_id)
        return "https://framer.example/" if frame_id is None else url

    monkeypatch.setattr(login_flow, "_frame_url", framed_by_a_stranger)
    async with _browser(tmp_path) as iid:
        with pytest.raises(ToolError) as caught:
            await _run(iid)
        assert "not on a host" in str(caught.value)
    assert site.posts == []


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
    assert not login_seal.any_sealing(), sealed_state()


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
    with spawn_before_the_vault():
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
    assert not login_seal.any_sealing(), sealed_state()


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
        # Bounded by time, not by a count of polls: how long the cleanup takes
        # (a cancelled login's strays are closed by a task of their own) depends
        # on the machine, and the invariant is about ORDER, not speed.
        end = asyncio.get_running_loop().time() + 60
        while asyncio.get_running_loop().time() < end:
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
        # the tab a cancelled login left is closed by a task of its own
        await asyncio.gather(*tuple(login_flow._STRAYS))
        assert exposed == []
        assert not login_seal.sealing(iid)
        assert not login_seal.is_sealed(iid, sealed)
        # the tab really is gone, not merely unlisted
        browser = await runtime.browser_manager.get_browser(iid)
        await tab_open.update_targets(browser)
        assert tab_open.find(browser, sealed) is None


async def test_a_cancel_after_chrome_makes_the_login_tab_leaves_no_blank_tab(
    site, tmp_path, monkeypatch
):
    """A cancel that lands after Chrome created the login's tab and before the open
    returned: the tab is not yet sealed by id, so it must be closed, not left as a
    visible blank tab."""
    unlocked_vault({"work": _entry(site)})
    real_open = tab_open.open_tab

    async def open_then_cancelled(*args, **kwargs):
        await real_open(*args, **kwargs)
        raise asyncio.CancelledError

    async with _browser(tmp_path) as iid:
        # the spawn's own background work opens tabs through the same function
        browser = await runtime.browser_manager.get_browser(iid)
        await login_persistence.settle_session_restore(browser)
        monkeypatch.setattr(tab_open, "open_tab", open_then_cancelled)
        await tab_open.update_targets(browser)
        # F7: every target there is NOW, the spawn's own internal ones included.
        # A tab that goes away on its own (the session-restore tab) must not make
        # this test fail; only a tab that is NEW and stays is the defect.
        known = {str(t.target.target_id) for t in browser.tabs}
        with pytest.raises(asyncio.CancelledError):
            await _run(iid)
        await asyncio.gather(*getattr(login_flow, "_STRAYS", ()))
        await asyncio.sleep(1)
        assert not login_seal.sealing(iid)
        await tab_open.update_targets(browser)
        now = {str(t.target.target_id) for t in browser.tabs}
        assert now - known == set(), "the login's tab was left behind"
        assert not set(await _tab_ids(iid)) - known


async def test_a_login_the_browser_stops_answering_is_a_bounded_refusal(
    site, tmp_path, monkeypatch
):
    """The macOS publish gate hung for 300 s inside a login and said nothing (no
    CDP call has a bound of its own). A login that never ends is now stopped, with
    a message that says only what is known, its tab closed and its seal down, and
    audited as ``timed_out``, not ``refused``: the submit may have gone through. It
    is NOT a rejection, so the entry is not frozen. The OUTER bound here is only so
    a tree without the budget fails in 20 s instead of hanging."""
    unlocked_vault({"work": _entry(site)})

    async def never(tab, entry):
        await asyncio.Event().wait()

    monkeypatch.setattr(login_flow, "_sealed_login", never)
    monkeypatch.setattr(login_flow, "LOGIN_BUDGET_S", 1.5)
    async with _browser(tmp_path) as iid:
        before = await _tab_ids(iid)
        async with asyncio.timeout(20):
            with pytest.raises(
                login_flow.LoginTimedOutError, match=r"did not finish within 1\.5 s"
            ) as caught:
                await _run(iid)
        said = str(caught.value)
        assert "whether the form was already submitted is not known" in said
        assert said.endswith("; its tab is closed")
        assert "cleared" not in said
        assert not login_seal.sealing(iid)
        assert set(await _tab_ids(iid)) <= set(before)
    assert audit_outcomes()[-1] == "timed_out"
    assert not vault_store.entry("work").get("frozen")


async def test_a_timed_out_login_whose_tab_will_not_close_says_so(
    site, tmp_path, monkeypatch
):
    """The tab's fate is read AFTER the cleanup: the message must not say "closed"
    for a tab that is not, and carries the same note a finished login does."""
    unlocked_vault({"work": _entry(site)})
    real_send = tab_open.send

    async def refusing_close(instance_id, tab_id):
        return False

    async def send(browser, command, *args):
        if getattr(command, "__name__", "") == "close_target":
            raise RuntimeError("Chrome refused")
        return await real_send(browser, command, *args)

    reached = asyncio.Event()

    async def never(tab, entry):
        reached.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime.browser_manager, "close_tab", refusing_close)
    monkeypatch.setattr(tab_open, "send", send)
    monkeypatch.setattr(login_flow, "_CLOSE_RETRY_S", 0.01)
    monkeypatch.setattr(login_flow, "_sealed_login", never)
    # The budget must outlast the OPEN of the login's tab, which takes seconds on a
    # loaded machine: a budget that ended first left no tab to refuse to close (the
    # message then said "closed"), which is a different test.
    monkeypatch.setattr(login_flow, "LOGIN_BUDGET_S", 15.0)
    async with _browser(tmp_path) as iid:
        async with asyncio.timeout(60):
            with pytest.raises(login_flow.LoginTimedOutError) as caught:
                await _run(iid)
        said = str(caught.value)
        assert reached.is_set(), f"the budget ended before the tab was open: {said}"
        assert said.endswith(login_flow.UNCLOSED_NOTE)
        assert "its tab is closed" not in said
        assert len(login_seal._unclosed.get(iid, ())) == 1
    assert audit_outcomes()[-1] == "timed_out"


async def _unreachable(iid, tab_id):
    """The sealed tab is not listed and no tool that names it runs."""
    assert tab_id not in await _tab_ids(iid)
    for call, extra in (
        ("execute_script", {"script": "location.href"}),
        ("take_screenshot", {}),
    ):
        with pytest.raises(ToolError):
            await get_fn(call)(instance_id=iid, tab_id=tab_id, **extra)


async def test_a_budget_that_fires_after_the_password_is_typed_leaves_nothing_behind(
    site, tmp_path, monkeypatch
):
    """The budget lands with the secret IN the field. Until the cleanup ends the
    seal holds and no tool can address the tab; the cleanup clears the field and
    closes the tab; the attempt is ``timed_out`` and nothing was submitted."""
    unlocked_vault({"work": _entry(site)})
    real_type, real_clear = login_flow._type_into, login_form.clear_passwords
    real_cleanup = login_flow._cleanup
    gate, typed, seen = asyncio.Event(), asyncio.Event(), {}
    read = "document.querySelector('input[type=password]').value"

    async def type_then_stall(tab, kind, text, hosts):
        held = await real_type(tab, kind, text, hosts)
        if kind == "password":
            seen["typed"] = await tab.evaluate(read) == RIGHT
            typed.set()
            await asyncio.Event().wait()
        return held

    async def spying_clear(tab):
        await real_clear(tab)
        seen["after_clear"] = await tab.evaluate(read)

    async def stalled(*args):
        await gate.wait()
        return await real_cleanup(*args)

    monkeypatch.setattr(login_flow, "_type_into", type_then_stall)
    monkeypatch.setattr(login_form, "clear_passwords", spying_clear)
    monkeypatch.setattr(login_flow, "_cleanup", stalled)
    monkeypatch.setattr(login_flow, "LOGIN_BUDGET_S", 12.0)
    monkeypatch.setattr(login_flow, "CLEANUP_BUDGET_S", 0.5)
    async with _browser(tmp_path) as iid:
        task = asyncio.create_task(_run(iid))
        async with asyncio.timeout(30):
            await typed.wait()
        tab_id = next(iter(login_seal._tabs[iid]))
        assert seen["typed"] is True, "the secret was not in the field when it stalled"
        await _unreachable(iid, tab_id)
        async with asyncio.timeout(40):
            with pytest.raises(login_flow.LoginTimedOutError) as caught:
                await task
        # a CI log of a stalled login says WHERE it stalled (never a secret)
        assert "(it was reading back the password)" in str(caught.value)
        assert RIGHT not in str(caught.value)
        # the caller has its answer; the cleanup has not run, so the seal must hold
        assert login_seal.sealing(iid)
        assert "after_clear" not in seen
        await _unreachable(iid, tab_id)
        gate.set()
        async with asyncio.timeout(30):
            await asyncio.gather(*login_flow._CLEANUPS)
        assert seen["after_clear"] == ""
        assert not login_seal.sealing(iid)
        browser = await runtime.browser_manager.get_browser(iid)
        await tab_open.update_targets(browser)
        assert tab_open.find(browser, tab_id) is None
    assert audit_outcomes()[-1] == "timed_out"
    assert not vault_store.entry("work").get("frozen")
    assert site.posts == []


async def test_a_cleanup_whose_steps_never_answer_ends_with_the_tab_sealed(
    site, tmp_path, monkeypatch
):
    """Bounded without unsealing: a clear or a close that never returns ends as a
    tab NOT known closed, which stays sealed for good (the cost of a browser that
    stopped answering is a hidden tab, never an exposed one)."""
    unlocked_vault({"work": _entry(site)})

    async def done(tab, entry):
        return "logged_in", "signed in"

    async def hangs(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(login_flow, "_sealed_login", done)
    monkeypatch.setattr(login_form, "clear_passwords", hangs)
    monkeypatch.setattr(login_flow, "_close_tab", hangs)
    monkeypatch.setattr(login_flow, "CLEAR_BUDGET_S", 0.3)
    monkeypatch.setattr(login_flow, "CLOSE_BUDGET_S", 0.3)
    async with _browser(tmp_path) as iid:
        async with asyncio.timeout(20):
            result = await _run(iid)
        assert result["detail"] == "signed in" + login_flow.UNCLOSED_NOTE
        assert not login_seal.sealing(iid)
        (leftover,) = login_seal._unclosed[iid]
        assert login_seal.is_sealed(iid, leftover)
        await _unreachable(iid, leftover)


async def test_a_cleanup_that_hangs_is_not_waited_for_and_the_tab_stays_sealed(
    site, tmp_path, monkeypatch
):
    unlocked_vault({"work": _entry(site)})
    real_cleanup, gate = login_flow._cleanup, asyncio.Event()

    async def done(tab, entry):
        return "logged_in", "signed in"

    async def stalled(*args):
        await gate.wait()
        return await real_cleanup(*args)

    monkeypatch.setattr(login_flow, "_sealed_login", done)
    monkeypatch.setattr(login_flow, "_cleanup", stalled)
    monkeypatch.setattr(login_flow, "CLEANUP_BUDGET_S", 0.5)
    async with _browser(tmp_path) as iid:
        async with asyncio.timeout(20):
            result = await _run(iid)
        assert result["detail"] == "signed in" + login_flow.UNCLOSED_NOTE
        assert login_seal.sealing(iid), "the seal must outlive a cleanup still running"
        gate.set()
        async with asyncio.timeout(20):
            await asyncio.gather(*login_flow._CLEANUPS)
        assert not login_seal.sealing(iid)


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
