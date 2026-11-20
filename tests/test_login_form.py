"""Login form detection in a real Chrome, on local pages (T-19366)."""

from __future__ import annotations

import contextlib
import http.server
import threading

import pytest

from e2e_helpers import (
    get_fn,
    integration_pytestmark,
    sandbox_kwargs,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded import login_form
from stealth_chrome_devtools_mcp.embedded import tool_runtime as runtime

pytestmark = integration_pytestmark()

PAGES = {
    "/one-step": """<form>
      <input id=u type=text autocomplete=username>
      <input id=p type=password autocomplete=current-password>
    </form>""",
    "/username-only": """<form><input id=u type=email></form>""",
    "/plain-text-then-password": """<form>
      <input id=other type=text><input id=u type=text><input id=p type=password>
    </form>""",
    "/decoy": """<form>
      <input id=u type=text autocomplete=username>
      <input id=hidden1 type=password style="display:none">
      <input id=hidden2 type=password style="visibility:hidden">
      <input id=hidden3 type=password style="width:0;height:0;padding:0;border:0">
      <input id=hidden4 type=password disabled>
      <div style="display:none"><input id=hidden5 type=password></div>
    </form>""",
    "/decoy-then-real": """<form>
      <input id=hidden type=password style="display:none">
      <input id=u type=text autocomplete=username>
      <input id=real type=password>
    </form>""",
    "/action-other": """<form action="https://evil.example/collect" method=post>
      <input id=u type=text autocomplete=username>
      <input id=p type=password></form>""",
    "/action-relative": """<form action="/sub/login" method=post>
      <input id=u type=text autocomplete=username><input id=p type=password></form>""",
    "/action-button": """<form action="/sub/login" method=post>
      <input id=u type=text autocomplete=username><input id=p type=password>
      <button formaction="https://evil.example/steal">go</button></form>""",
    "/action-none": """<input id=u type=text autocomplete=username>
      <input id=p type=password>""",
    "/frame": """<h1>host</h1><iframe src="/one-step" width=400 height=200></iframe>""",
    "/otp": """<form><input id=code type=text autocomplete=one-time-code></form>""",
    "/otp-numeric": """<form><input id=code type=text inputmode=numeric maxlength=6></form>""",
    "/numeric-long": """<form><input id=n type=text inputmode=numeric maxlength=20></form>""",
    "/empty": """<p>nothing here</p>""",
    "/filled": """<form>
      <input id=u type=text autocomplete=username>
      <input id=p type=password value="not-a-real-secret-0000">
    </form>
    <iframe src="/filled-inner" width=300 height=100></iframe>""",
    "/filled-inner": """<input id=q type=password value="not-a-real-secret-1111">""",
    "/hostile": """<script>
      HTMLInputElement.prototype.getClientRects = () => [];
      window.getComputedStyle = () => ({display: 'none', visibility: 'hidden'});
    </script>
    <form><input id=p type=password></form>""",
}


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


@pytest.fixture(scope="module")
def site():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = PAGES.get(self.path.split("?")[0])
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write((body or "").encode())

        def log_message(self, *args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@contextlib.asynccontextmanager
async def _tab_on(site, path, tmp_path):
    spawn, close = get_fn("spawn_browser"), get_fn("close_instance")
    spawned = await spawn(
        headless=True,
        user_data_dir=str(tmp_path / "login-form"),
        **sandbox_kwargs(),
    )
    iid = spawned["instance_id"]
    try:
        await get_fn("navigate")(instance_id=iid, url=f"{site}{path}")
        tab = await runtime.browser_manager.get_tab(iid)
        assert tab is not None
        yield tab
    finally:
        await close(instance_id=iid)


async def _read(element, expression):
    return await element.apply(expression)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/one-step", {"username": True, "password": True, "otp": False}),
        ("/username-only", {"username": True, "password": False, "otp": False}),
        (
            "/plain-text-then-password",
            {"username": True, "password": True, "otp": False},
        ),
        ("/decoy", {"username": True, "password": False, "otp": False}),
        ("/frame", {"username": True, "password": True, "otp": False}),
        ("/otp", {"username": False, "password": False, "otp": True}),
        ("/otp-numeric", {"username": False, "password": False, "otp": True}),
        ("/numeric-long", {"username": False, "password": False, "otp": False}),
        ("/empty", {"username": False, "password": False, "otp": False}),
        ("/hostile", {"username": False, "password": True, "otp": False}),
    ],
)
async def test_probe_reads_the_form(site, tmp_path, path, expected):
    async with _tab_on(site, path, tmp_path) as tab:
        answer = await login_form.probe(tab)
    assert {k: answer[k] for k in expected} == expected
    assert answer["password_has_value"] is False


async def test_a_hidden_decoy_password_field_is_ignored(site, tmp_path):
    async with _tab_on(site, "/decoy-then-real", tmp_path) as tab:
        found = await login_form.find_field(tab, "password")
        assert found is not None
        element, _frame = found
        assert await _read(element, "(e) => e.id") == "real"


async def test_find_field_returns_username_and_password_of_a_one_step_form(
    site, tmp_path
):
    async with _tab_on(site, "/one-step", tmp_path) as tab:
        user, _ = await login_form.find_field(tab, "username")
        password, _ = await login_form.find_field(tab, "password")
        assert await _read(user, "(e) => e.id") == "u"
        assert await _read(password, "(e) => e.id") == "p"


async def test_the_text_input_before_the_password_field_is_the_username(site, tmp_path):
    async with _tab_on(site, "/plain-text-then-password", tmp_path) as tab:
        user, _ = await login_form.find_field(tab, "username")
        assert await _read(user, "(e) => e.id") == "u"


async def test_find_field_looks_in_a_child_frame(site, tmp_path):
    async with _tab_on(site, "/frame", tmp_path) as tab:
        found = await login_form.find_field(tab, "password")
        assert found is not None
        element, frame_id = found
        assert await _read(element, "(e) => e.id") == "p"
        top = (await tab.send(login_form.cdp.page.get_frame_tree())).frame.id_
        assert frame_id != str(top)


async def test_find_field_is_none_when_the_form_is_absent(site, tmp_path):
    async with _tab_on(site, "/username-only", tmp_path) as tab:
        assert await login_form.find_field(tab, "password") is None
    async with _tab_on(site, "/empty", tmp_path) as tab:
        assert await login_form.find_field(tab, "username") is None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/action-other", "https://evil.example/collect"),
        ("/action-relative", "/sub/login"),
        ("/action-button", "https://evil.example/steal"),
        ("/one-step", "/one-step"),
        ("/action-none", None),
    ],
)
async def test_form_action_is_where_the_form_submits_resolved(
    site, tmp_path, path, expected
):
    async with _tab_on(site, path, tmp_path) as tab:
        for kind in ("username", "password"):
            found = await login_form.find_field(tab, kind)
            assert found is not None
            action = await login_form.form_action(tab, kind, found[1])
            assert action == (None if expected is None else _resolve(site, expected))


def _resolve(site, expected):
    return expected if "://" in expected else f"{site}{expected}"


async def test_find_field_refuses_an_unknown_kind(site, tmp_path):
    async with _tab_on(site, "/one-step", tmp_path) as tab:
        with pytest.raises(ValueError):
            await login_form.find_field(tab, "pin")


async def test_probe_sees_a_typed_value_and_clear_passwords_empties_it(site, tmp_path):
    async with _tab_on(site, "/filled", tmp_path) as tab:
        assert (await login_form.probe(tab))["password_has_value"] is True
        await login_form.clear_passwords(tab)
        assert (await login_form.probe(tab))["password_has_value"] is False
        # the one in the child frame is empty too
        for frame in await login_form._frame_ids(tab, everything=True):
            result = await login_form._evaluate(
                tab,
                frame,
                "Array.from(document.querySelectorAll('input[type=password]'))"
                ".every((e) => e.value === '')",
                by_value=True,
            )
            assert result.value is True


async def test_the_page_cannot_hide_a_field_by_overriding_its_natives(site, tmp_path):
    """``/hostile`` redefines ``getClientRects`` and ``getComputedStyle`` in the
    page's world; the probe runs in an isolated world and still sees the field."""
    async with _tab_on(site, "/hostile", tmp_path) as tab:
        answer = await login_form.probe(tab)
    assert answer["password"] is True
