"""The sealed login's guards and refusals, with no browser (T-19366)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from stealth_chrome_devtools_mcp.embedded import (
    browser_cmdline,
    browser_trust,
    login_flow,
    login_seal,
    vault_crypto,
    vault_store,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from stealth_chrome_devtools_mcp.settings import get_settings
from vault_helpers import (
    LOOPBACK_ENV,
    password_entry,
    provider_entry,
    reset,
    unlocked_vault,
)

HOSTS = ["accounts.google.com"]
CLEAN = ["chrome", "--headless=new", "--remote-debugging-port=9222"]


@pytest.fixture(autouse=True)
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_crypto, "SCRYPT_N", 2**10)
    monkeypatch.setattr(vault_store, "VAULT_PATH", tmp_path / "vault.json")
    monkeypatch.setattr(vault_store, "AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.delenv(LOOPBACK_ENV, raising=False)
    get_settings.cache_clear()
    reset()
    yield
    reset()
    get_settings.cache_clear()
    while login_seal.any_sealing():
        for instance in ("inst-1",):
            login_seal.end(instance)


def audit_lines():
    if not vault_store.AUDIT_PATH.exists():
        return []
    return [
        json.loads(line)
        for line in vault_store.AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    ]


# ------------------------------------------------------------ launch_refusal


@pytest.mark.parametrize("flag", login_flow.FORBIDDEN_FLAGS)
def test_each_forbidden_flag_refuses(flag):
    reason = login_flow.launch_refusal(SimpleNamespace(proxy=None), [*CLEAN, flag])
    assert reason is not None
    assert flag in reason


@pytest.mark.parametrize("flag", login_flow.FORBIDDEN_FLAGS)
def test_each_forbidden_flag_refuses_in_its_value_form(flag):
    reason = login_flow.launch_refusal(
        SimpleNamespace(proxy=None), [*CLEAN, f"{flag}=MAP * evil.example"]
    )
    assert reason is not None
    assert flag in reason


def test_the_forbidden_list_names_every_switch_the_review_found_and_more():
    names = {flag.removeprefix("--") for flag in login_flow.FORBIDDEN_FLAGS}
    assert names >= {
        "host-resolver-rules",
        "host-rules",
        "ignore-certificate-errors",
        "ignore-certificate-errors-spki-list",
        "allow-insecure-localhost",
        "unsafely-treat-insecure-origin-as-secure",
        "proxy-server",
        "proxy-pac-url",
        "proxy-auto-detect",
        "proxy-bypass-list",
        "disable-web-security",
        "allow-running-insecure-content",
        "test-type",
        "load-extension",
        "ssl-key-log-file",
    }
    # a proxy-disabling switch is safe and is not on the list
    assert "no-proxy-server" not in names
    assert "user-agent" not in names


SPELLINGS = [
    "--{n}",
    "-{n}",
    "/{n}",
    "--{N}",
    "-{N}",
    "/{N}",
    "--{n}=x y",
    "-{n}=MAP * evil.example",
    "/{N}=value",
    "  --{n}",
]


@pytest.mark.parametrize("name", sorted(browser_trust.REFUSED_SWITCHES))
@pytest.mark.parametrize("spelling", SPELLINGS)
def test_every_spelling_chrome_accepts_of_every_refused_switch_refuses(name, spelling):
    argument = spelling.format(n=name, N=name.upper())
    reason = login_flow.launch_refusal(SimpleNamespace(proxy=None), [*CLEAN, argument])
    assert reason is not None, argument
    assert name in reason


@pytest.mark.parametrize(
    "argument",
    [
        "--no-proxy-server",
        "--user-agent=Mozilla/5.0",
        "--lang=fr",
        "--window-size=800,600",
        "--disable-gpu",
        "--proxy-server-note=1",
        "-",
        "--",
        "/",
        "about:blank",
        "https://example.com/proxy-server",
        "",
    ],
)
def test_harmless_arguments_do_not_refuse(argument):
    assert (
        login_flow.launch_refusal(SimpleNamespace(proxy=None), [*CLEAN, argument])
        is None
    )


@pytest.mark.parametrize(
    ("argument", "name"),
    [
        ("--proxy-server=a:1", "proxy-server"),
        ("-Proxy-Server=a:1", "proxy-server"),
        ("/PROXY-SERVER", "proxy-server"),
        ("--", None),
        ("-", None),
        ("plain", None),
        ("--=x", None),
    ],
)
def test_switch_name_reads_arguments_as_chrome_does(argument, name):
    assert browser_trust.switch_name(argument) == name


def test_the_review_repro_spellings_refuse():
    for argument in (
        "-ignore-certificate-errors",
        "/ignore-certificate-errors",
        "--Ignore-Certificate-Errors",
        "-host-resolver-rules=MAP accounts.example.com 127.0.0.1:443",
    ):
        assert login_flow.launch_refusal(
            SimpleNamespace(proxy=None), [*CLEAN, argument]
        )


# ------------------------------------------------- environment, profile, policy


def test_an_unreadable_environment_refuses():
    assert "could not be read" in browser_trust.environment_refusal(None)


def test_a_key_log_variable_refuses_whatever_its_case():
    assert "SSLKEYLOGFILE" in browser_trust.environment_refusal(
        {"SSLKEYLOGFILE": "k.log"}
    )
    assert "sslkeylogfile" in browser_trust.environment_refusal({"sslkeylogfile": "k"})
    assert browser_trust.environment_refusal({"SSLKEYLOGFILE": " "}) is None


def test_proxy_variables_refuse_where_chrome_honours_them(monkeypatch):
    monkeypatch.setattr(browser_trust.sys, "platform", "linux")
    assert browser_trust.environment_refusal({"HTTPS_PROXY": "http://p:1"})
    assert browser_trust.environment_refusal({"all_proxy": "socks5://p:1"})
    monkeypatch.setattr(browser_trust.sys, "platform", "win32")
    assert browser_trust.environment_refusal({"HTTPS_PROXY": "http://p:1"}) is None


def test_a_clean_environment_may_log_in():
    assert browser_trust.environment_refusal({"PATH": "x", "HOME": "y"}) is None


def test_chrome_flags_experiments_in_the_profile_refuse(tmp_path):
    (tmp_path / "Local State").write_text(
        json.dumps(
            {"browser": {"enabled_labs_experiments": ["allow-insecure-localhost"]}}
        ),
        encoding="utf-8",
    )
    assert "chrome://flags" in browser_trust.profile_refusal(str(tmp_path))


def test_a_profile_without_experiments_or_state_may_log_in(tmp_path):
    assert browser_trust.profile_refusal(str(tmp_path)) is None
    (tmp_path / "Local State").write_text('{"browser": {}}', encoding="utf-8")
    assert browser_trust.profile_refusal(str(tmp_path)) is None
    (tmp_path / "Local State").write_text("not json", encoding="utf-8")
    assert browser_trust.profile_refusal(str(tmp_path)) is None
    assert browser_trust.profile_refusal(None) is None


def test_a_proxy_policy_refuses(tmp_path):
    (tmp_path / "p.json").write_text('{"ProxyMode": "fixed_servers"}', encoding="utf-8")
    assert "ProxyMode".lower() in browser_trust.policy_refusal(set(), (tmp_path,))
    assert browser_trust.policy_refusal({"proxyserver"}, ()) is not None


def test_an_unrelated_policy_does_not_refuse(tmp_path):
    (tmp_path / "p.json").write_text('{"HomepageLocation": "x"}', encoding="utf-8")
    assert browser_trust.policy_refusal(set(), (tmp_path,)) is None
    assert browser_trust.policy_refusal({"homepagelocation"}, ()) is None


def test_the_user_data_dir_is_read_from_argv():
    cmdline = ["chrome", "--User-Data-Dir=C:/profiles/p", "--lang=fr"]
    assert browser_trust.user_data_dir_of(cmdline) == "C:/profiles/p"
    assert browser_trust.user_data_dir_of(["chrome"]) is None


def test_a_proxy_option_refuses():
    reason = login_flow.launch_refusal(SimpleNamespace(proxy="http://p:1"), CLEAN)
    assert reason is not None
    assert "proxy" in reason


def test_a_browser_without_overrides_may_log_in():
    assert login_flow.launch_refusal(SimpleNamespace(proxy=None), CLEAN) is None
    assert login_flow.launch_refusal(SimpleNamespace(proxy=""), CLEAN) is None
    assert login_flow.launch_refusal(None, CLEAN) is None


def test_a_flag_that_merely_starts_with_a_forbidden_name_is_not_one():
    cmdline = [*CLEAN, "--host-rules-note=1", "--proxy-server-x"]
    assert login_flow.launch_refusal(SimpleNamespace(proxy=None), cmdline) is None


# ------------------------------------------------------------ origin_allowed


@pytest.mark.parametrize(
    "url",
    [
        "http://accounts.google.com/",
        "https://accounts.google.com.evil.io/",
        "https://xaccounts.google.com/",
        "https://evilaccounts.google.com/",
        "https://accounts.google.com@evil.io/",
        "https://user:pw@accounts.google.com/",
        "https://sub.accounts.google.com/",
        "https://google.com/",
        "chrome-error://chromewebdata/",
        "about:blank",
        "data:text/html,hi",
        "javascript:1",
        "https://",
        "",
        "accounts.google.com",
        "ftp://accounts.google.com/",
        # another PORT is another service on the same name
        "https://accounts.google.com:8443/",
        "https://accounts.google.com:80/",
        "https://accounts.google.com:99999/",
        "https://accounts.google.com:abc/",
        "https://accounts.google.com:0/",
    ],
)
def test_origin_allowed_rejects(url):
    assert login_flow.origin_allowed(url, HOSTS) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://accounts.google.com/",
        "https://accounts.google.com/signin/v2?x=1#frag",
        "https://accounts.google.com:443/",
        "https://ACCOUNTS.google.com/",
        "https://accounts.google.com./".replace(".com./", ".com/"),
    ],
)
def test_origin_allowed_accepts_the_exact_host(url):
    assert login_flow.origin_allowed(url, HOSTS) is True


def test_any_of_the_listed_hosts_is_accepted():
    hosts = ["a.example.com", "b.example.com"]
    assert login_flow.origin_allowed("https://b.example.com/", hosts) is True
    assert login_flow.origin_allowed("https://c.example.com/", hosts) is False


def test_http_loopback_needs_the_test_variable(monkeypatch):
    url = "http://127.0.0.1:8123/login"
    assert login_flow.origin_allowed(url, ["127.0.0.1"]) is False
    monkeypatch.setenv(LOOPBACK_ENV, "1")
    get_settings.cache_clear()
    assert login_flow.origin_allowed(url, ["127.0.0.1"]) is True
    assert login_flow.origin_allowed("http://localhost:8123/", ["127.0.0.1"]) is False
    assert login_flow.origin_allowed("http://evil.example/", ["evil.example"]) is False
    assert login_flow.origin_allowed(url, ["other.example"]) is False
    # the test variable lets the test site's random port through, and only on http
    assert login_flow.origin_allowed("https://127.0.0.1:8123/", ["127.0.0.1"]) is False


# ------------------------------------------------------------ refusals in run


class _Browser:
    _process_pid = 4242


class _Manager:
    def __init__(self, options=None, browser=None):
        self.instance = {
            "browser": browser or _Browser(),
            "options": options or SimpleNamespace(proxy=None),
        }

    async def get_instance(self, instance_id):
        return self.instance if instance_id == "inst-1" else None


async def test_a_locked_vault_refuses_and_is_audited():
    with pytest.raises(vault_store.VaultLocked):
        await login_flow.run(_Manager(), "inst-1", "work", "caller-1")
    (line,) = audit_lines()
    assert (line["label"], line["outcome"]) == ("work", "locked")


async def test_an_unknown_label_is_a_tool_error_and_is_audited():
    unlocked_vault({"work": password_entry()})
    with pytest.raises(ToolError):
        await login_flow.run(_Manager(), "inst-1", "nowhere", "c")
    assert audit_lines()[-1]["outcome"] == "refused"


async def test_a_frozen_entry_is_refused_without_touching_the_browser():
    unlocked_vault({"work": password_entry(frozen="2026-11-18T00:00:00+00:00")})

    class Untouchable:
        async def get_instance(self, _):
            raise AssertionError("the browser was touched")

    with pytest.raises(ToolError) as caught:
        await login_flow.run(Untouchable(), "inst-1", "work", "c")
    assert "stealthy login edit work" in str(caught.value)
    assert not login_seal.any_sealing()


async def test_an_unknown_instance_is_instance_not_found():
    unlocked_vault({"work": password_entry()})
    with pytest.raises(ToolError) as caught:
        await login_flow.run(_Manager(), "inst-9", "work", "c")
    assert "inst-9" in str(caught.value)
    assert not login_seal.any_sealing()


@pytest.mark.parametrize(
    "bad", ["--proxy-server=http://x:1", "--host-resolver-rules=MAP a b"]
)
async def test_a_browser_with_an_override_is_refused_before_any_tab_opens(
    monkeypatch, bad
):
    unlocked_vault({"work": password_entry()})
    monkeypatch.setattr(browser_cmdline, "arguments", lambda pid: [*CLEAN, bad])
    with pytest.raises(ToolError) as caught:
        await login_flow.run(_Manager(), "inst-1", "work", "c")
    assert bad.partition("=")[0] in str(caught.value)
    assert not login_seal.any_sealing()
    assert audit_lines()[-1]["outcome"] == "refused"


async def test_a_browser_with_a_proxy_option_is_refused(monkeypatch):
    unlocked_vault({"work": password_entry()})
    monkeypatch.setattr(browser_cmdline, "arguments", lambda pid: CLEAN)
    manager = _Manager(options=SimpleNamespace(proxy="http://p:1"))
    with pytest.raises(ToolError):
        await login_flow.run(manager, "inst-1", "work", "c")


async def test_a_command_line_that_cannot_be_read_is_refused_not_assumed_clean(
    monkeypatch,
):
    unlocked_vault({"work": password_entry()})
    monkeypatch.setattr(login_flow, "_ARGV_RETRY_S", 0)
    monkeypatch.setattr(browser_cmdline, "arguments", lambda pid: [])
    with pytest.raises(ToolError) as caught:
        await login_flow.run(_Manager(), "inst-1", "work", "c")
    assert "command line" in str(caught.value)


async def test_a_provider_entry_runs_its_via_entry_and_answers_use_provider(
    monkeypatch,
):
    unlocked_vault({"work": password_entry(), "shop": provider_entry()})
    seen = []

    async def fake_sealed(tab, entry):
        raise AssertionError("not reached")

    async def fake_run(manager, instance_id, label, caller):
        seen.append(label)
        return {"outcome": "logged_in", "label": label, "detail": "signed in"}

    real_run = login_flow.run
    monkeypatch.setattr(login_flow, "run", fake_run)
    result = await login_flow._provider(
        _Manager(), "inst-1", "shop", "c", vault_store.entry("shop")
    )
    monkeypatch.setattr(login_flow, "run", real_run)
    assert seen == ["work"]
    assert result["outcome"] == "use_provider"
    assert result["label"] == "shop"


@pytest.mark.parametrize("inner", ["needs_user", "rejected", "needs_totp"])
async def test_a_provider_entry_passes_a_failed_provider_login_through(
    monkeypatch, inner
):
    unlocked_vault({"work": password_entry(), "shop": provider_entry()})

    async def fake_run(manager, instance_id, label, caller):
        return {"outcome": inner, "label": label, "detail": "x"}

    monkeypatch.setattr(login_flow, "run", fake_run)
    result = await login_flow._provider(
        _Manager(), "inst-1", "shop", "c", vault_store.entry("shop")
    )
    assert result["outcome"] == inner
    assert result["label"] == "shop"


async def test_no_audit_line_holds_a_credential():
    unlocked_vault({"work": password_entry()})
    with pytest.raises(ToolError):
        await login_flow.run(_Manager(), "inst-9", "work", "c")
    text = vault_store.AUDIT_PATH.read_text(encoding="utf-8")
    assert "Tr0ub4dor" not in text
    assert "someone@example.com" not in text


def test_frame_url_reads_the_named_frame_and_the_main_frame_by_default():
    child = SimpleNamespace(
        frame=SimpleNamespace(id_="CHILD", url="https://accounts.google.com/f"),
        child_frames=None,
    )
    tree = SimpleNamespace(
        frame=SimpleNamespace(id_="MAIN", url="https://host.example/"),
        child_frames=[child],
    )
    assert login_flow._frame_url(tree, None) == "https://host.example/"
    assert login_flow._frame_url(tree, "CHILD") == "https://accounts.google.com/f"
    assert login_flow._frame_url(tree, "NOPE") == ""


# ---------------------------------------- the login's own tab in a headed window


class _LoginTab:
    """A tab that records the CDP commands sent to it, by method."""

    def __init__(self):
        self.sent = []
        self.target = SimpleNamespace(target_id="LOGIN-TAB", type_="page")

    async def send(self, command):
        frame = next(command)
        self.sent.append(frame["method"])
        try:
            command.send(None)
        except StopIteration:
            pass


def _open_world(monkeypatch, tab):
    from stealth_chrome_devtools_mcp.embedded import debug_port_guard, tab_open

    async def clean(_browser):
        return None

    async def update(_browser):
        return None

    async def open_tab(_browser, url, *, background=False):
        assert background is True  # the login's own tab opens BEHIND the window's
        return tab

    monkeypatch.setattr(debug_port_guard, "verify_clean", clean)
    monkeypatch.setattr(tab_open, "update_targets", update)
    monkeypatch.setattr(tab_open, "open_tab", open_tab)
    monkeypatch.setattr(tab_open, "mark_internal", lambda *_a: None)


async def test_the_login_tab_is_given_focus_emulation_before_anything_is_typed(
    monkeypatch,
):
    """Headed Chrome drops the keys typed into a tab that is hidden and unfocused (both
    fields stayed empty). The tab is never adopted, so the one place it can be armed
    is where it is opened, and it must be sealed first."""
    tab = _LoginTab()
    _open_world(monkeypatch, tab)
    login_seal.begin("inst-1")
    try:
        opened = await login_flow._open_sealed_tab(SimpleNamespace(tabs=[]), "inst-1")
        assert opened is tab
        assert login_seal.is_sealed("inst-1", "LOGIN-TAB")
    finally:
        login_seal.end("inst-1")
    assert tab.sent == ["Emulation.setFocusEmulationEnabled"]


async def test_a_refused_focus_emulation_does_not_fail_the_open(monkeypatch):
    """Best effort, as everywhere else it is used: the login then fails toward
    ``needs_user`` (the field did not take the text), never toward a leak."""
    tab = _LoginTab()

    async def refuse(_command):
        raise RuntimeError("no")

    tab.send = refuse
    _open_world(monkeypatch, tab)
    login_seal.begin("inst-1")
    try:
        opened = await login_flow._open_sealed_tab(SimpleNamespace(tabs=[]), "inst-1")
        assert opened is tab
        assert login_seal.is_sealed("inst-1", "LOGIN-TAB")
    finally:
        login_seal.end("inst-1")


async def test_waiting_for_a_frame_is_bounded_and_never_raises(monkeypatch):
    from stealth_chrome_devtools_mcp.embedded import login_form

    async def world(_tab, _frame):
        return 1

    class Never:
        async def send(self, _command):
            await __import__("asyncio").sleep(60)

    monkeypatch.setattr(login_form, "_world", world)
    monkeypatch.setattr(login_form, "FRAME_WAIT_S", 0.05)
    await login_form.wait_for_frame(Never(), "F")  # returns, does not hang

    async def no_world(_tab, _frame):
        return None

    monkeypatch.setattr(login_form, "_world", no_world)
    await login_form.wait_for_frame(Never(), "F")  # a frame with no world: nothing


class _Field:
    def __init__(self, value):
        self.value = value

    async def apply(self, script):
        # the script is the page's own comparison; it is run here on the stored text
        assert "e.value" in script
        count = int(script.rsplit("=== ", 1)[1].rstrip("`"))
        return len(list(self.value)) == count


async def test_holds_counts_code_points_and_returns_a_boolean():
    from stealth_chrome_devtools_mcp.embedded import login_form

    assert await login_form.holds(_Field("abc"), 3) is True
    assert await login_form.holds(_Field("bc"), 3) is False
    assert await login_form.holds(_Field("h\U0001f44dx"), 3) is True  # one emoji
