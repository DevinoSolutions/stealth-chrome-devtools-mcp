"""Vault mode: what each capability class refuses, and the census that keeps it so (T-19366).

While a login vault exists every raw or unbounded tool capability is refused from ONE
home (``vault_mode``): raw CDP, browser switches off an allow-list, ``file:`` URLs,
pickle exports and unpinned downloads (caller Python is ``test_vault_mode.py``).
With no vault file every one behaves as it always did, and each class is pinned on
both sides. The census tests fail when a NEW tool forwards raw CDP, takes browser
arguments, a URL or a caller's file path without being in the home's list.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from fakes import FakeBrowserManager, call_tool
from stealth_chrome_devtools_mcp.embedded import (
    browser_trust,
    debug_logger,
    debug_port_guard,
    login_seal,
    path_confinement,
    platform_utils,
    tab_binding,
    tool_registry,
    tool_sections,
    vault_mode,
    vault_store,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

SRC = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "stealth_chrome_devtools_mcp"
)


@pytest.fixture
def mode_on(tmp_path, monkeypatch):
    """A vault file exists: vault mode is on."""
    vault = tmp_path / "vault.json"
    vault.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(vault_store, "VAULT_PATH", vault)
    monkeypatch.setattr(vault_mode, "download_dir", lambda: tmp_path / "downloads")
    return vault


@pytest.fixture
def server_mod(patched_server):
    return patched_server(browser_manager=FakeBrowserManager())


def _guarded(function):
    """The chokepoint every tool passes, around a stub of that name."""
    return tool_registry._refusing_sealed_tabs(function)


def _assert_names_the_vault(error: BaseException) -> None:
    text = str(error)
    assert "vault" in text
    assert "stealthy login destroy" in text


# ------------------------------------------------------------- the one list


def test_every_restriction_is_listed_once_for_the_notice_and_the_status():
    assert [r.key for r in vault_mode.RESTRICTIONS] == [
        "caller_python",
        "raw_cdp",
        "browser_args",
        "file_urls",
        "browser_pages",
        "export_format",
        "downloads",
    ]
    assert vault_mode.describe() == [r.summary for r in vault_mode.RESTRICTIONS]
    assert vault_mode.RESTRICTED_TOOLS == (
        vault_mode.RUNS_CALLER_PYTHON
        | vault_mode.RAW_CDP
        | vault_mode.TAKES_BROWSER_ARGS
        | vault_mode.LOADS_URLS
        | vault_mode.CHOOSES_EXPORT_FORMAT
    )
    assert vault_mode.GATED == (
        vault_mode.RUNS_CALLER_PYTHON
        | vault_mode.RAW_CDP
        | vault_mode.TAKES_BROWSER_ARGS
    )


def test_a_dangling_symlink_at_the_vault_path_is_a_vault(tmp_path, monkeypatch):
    link = tmp_path / "vault.json"
    try:
        link.symlink_to(tmp_path / "nowhere.json")
    except OSError:
        pytest.skip("this machine does not let the test make a symbolic link")
    monkeypatch.setattr(vault_store, "VAULT_PATH", link)
    assert vault_mode.vault_exists()
    assert vault_mode.active()


def test_a_directory_at_the_vault_path_is_a_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_store, "VAULT_PATH", tmp_path)
    assert vault_mode.vault_exists()
    assert vault_store.status()["exists"] is True


# ------------------------------------------------------------------ raw CDP


async def _execute_cdp_command(instance_id, command, params=None):
    return "ran"


_execute_cdp_command.__name__ = "execute_cdp_command"


async def test_raw_cdp_is_refused_whole_while_a_vault_exists(mode_on):
    call = _guarded(_execute_cdp_command)
    for command in (
        "Page.reload",
        "Browser.setDownloadBehavior",
        "browser.set_download_behavior",
        "page.SetDownloadBehavior",
        "DOM.setFileInputFiles",
        "Target.sendMessageToTarget",
        "evaluate",
    ):
        with pytest.raises(ToolError) as caught:
            await call("inst", command, {"x": 1})
        _assert_names_the_vault(caught.value)
        assert "execute_cdp_command" in str(caught.value)


async def test_raw_cdp_is_unchanged_with_no_vault():
    call = _guarded(_execute_cdp_command)
    assert await call("inst", "Browser.setDownloadBehavior", {}) == "ran"
    assert await call(instance_id="inst", command="Page.reload") == "ran"


async def test_the_real_tool_is_refused_and_list_cdp_commands_is_not(
    server_mod, mode_on
):
    with pytest.raises(ToolError) as caught:
        await call_tool(
            server_mod,
            "execute_cdp_command",
            instance_id="i",
            command="Browser.setDownloadBehavior",
            params={"behavior": "allow", "downloadPath": "C:/x"},
        )
    _assert_names_the_vault(caught.value)
    # the list is information, not a capability
    assert isinstance(await call_tool(server_mod, "list_cdp_commands"), list)


# ------------------------------------------------------------ browser arguments


async def _spawn_browser(
    headless=False, browser_args=None, user_data_dir=None, session=None
):
    return "spawned"


_spawn_browser.__name__ = "spawn_browser"

ALLOWED = [
    "--window-size=1280,800",
    "-window-size=1280,800",
    "--WINDOW-SIZE=1280,800",
    "--window-position=-10,20",
    "--start-maximized",
    "--lang=en-US",
    "/lang=de",
    "--accept-lang=en-US,fr-CA",
    "--user-agent=Mozilla/5.0 (X11; Linux x86_64) $(x) `y` 'z' \u2018q\u2019",
    "https://example.com/start",
    "about:blank",
]

REFUSED = [
    "--log-file=C:/Python/Lib/site-packages/evil.pth",
    "--enable-logging",
    "--enable-logging=stderr",
    "--net-log=out.json",
    "--log-net-log=out.json",
    "--trace-startup-file=out.json",
    "--renderer-cmd-prefix=calc",
    "--utility-cmd-prefix=calc",
    "--gpu-launcher=calc",
    "--browser-subprocess-path=calc.exe",
    "--user-data-dir=C:/anywhere",
    "--disk-cache-dir=C:/anywhere",
    "--crash-dumps-dir=C:/anywhere",
    "--load-extension=C:/ext",
    "--proxy-server=http://u:p@10.0.0.1:8080",
    "--host-resolver-rules=MAP * 127.0.0.1",
    "--ignore-certificate-errors",
    "--disable-web-security",
    "--allow-file-access-from-files",
    "--no-sandbox",
    "--enable-automation",
    "--remote-debugging-port=9222",
    "--remote-allow-origins=*",
    "-log-file=x",
    "/Log-File=x",
    "--LOG-FILE=x",
    "  --log-file=x",
    "--window-size=800,600 --log-file=x",
    "--lang=en-US --log-file=x",
    "--window-size=800",
    "--window-size",
    "--start-maximized=1",
    "--lang=",
    "--lang=en;calc",
    # the feature-list switches (audit 6, N1): feature names are an open vocabulary
    # that includes site isolation and private-network protection
    "--disable-features=Foo,Bar",
    "--disable-features=IsolateOrigins,site-per-process",
    "--DISABLE-FEATURES=BlockInsecurePrivateNetworkRequests",
    "-disable-features=Foo",
    "--enable-blink-features=Foo,Bar",
    "--enable-features=Foo",
    # F6: only that exact value of --disable-blink-features
    "--disable-blink-features",
    "--disable-blink-features=",
    "--disable-blink-features=Foo",
    "--disable-blink-features=AutomationControlled,Foo",
    "--disable-blink-features=Foo,AutomationControlled",
    "--disable-blink-features=AutomationControlled,",
    "--disable-blink-features=automationcontrolled",
    "--disable-blink-features= AutomationControlled",
    "--disable-blink-features=AutomationControlled;Foo",
    "--disable-blink-features=AutomationControlled --log-file=x",
    "--disable-blink-features=SiteIsolation",
    "--disable-features=Foo;Bar",
    "--user-agent",
    "--user-agent=a\nb",
    "file:///C:/Users/x/.stealth-mcp/vault.json",
    "view-source:file:///etc/passwd",
    "C:\\Users\\x\\vault.json",
    "javascript:alert(1)",
    "chrome://inspect",
    "https://exa mple.com/",
    "",
    "--",
]


@pytest.mark.parametrize("argument", ALLOWED)
def test_an_allowed_switch_passes_while_a_vault_exists(mode_on, argument):
    vault_mode.check_browser_args([argument])


@pytest.mark.parametrize("argument", REFUSED)
def test_any_other_switch_is_refused_by_name_while_a_vault_exists(mode_on, argument):
    with pytest.raises(ToolError) as caught:
        vault_mode.check_browser_args(["--window-size=1,2", argument])
    _assert_names_the_vault(caught.value)
    # the SWITCH is named; a value (a path, a credential) never is
    for secret in ("u:p@", "site-packages", ".stealth-mcp", "evil.pth", "C:/anywhere"):
        assert secret not in str(caught.value)


def test_the_refusal_names_the_switch():
    # (the vault is off here: the check itself does not look at the vault)
    with pytest.raises(ToolError, match=r"--log-file"):
        vault_mode.check_browser_args(["-LOG-file=x"])
    with pytest.raises(ToolError, match=r"--renderer-cmd-prefix"):
        vault_mode.check_browser_args(["/renderer-cmd-prefix=x"])


@pytest.mark.parametrize("argument", ALLOWED + REFUSED)
async def test_browser_args_are_unchanged_with_no_vault(argument):
    call = _guarded(_spawn_browser)
    assert await call(browser_args=[argument]) == "spawned"
    assert await call(True, [argument]) == "spawned"  # positional too


async def test_the_spawn_tool_refuses_the_args_before_anything_starts(mode_on):
    call = _guarded(_spawn_browser)
    with pytest.raises(ToolError) as caught:
        await call(browser_args=["--enable-logging", "--log-file=x"])
    _assert_names_the_vault(caught.value)
    with pytest.raises(ToolError):
        await call(True, ["--renderer-cmd-prefix=calc"])  # positional
    assert await call(browser_args=["--window-size=800,600", "--lang=de"]) == "spawned"
    assert await call(headless=True) == "spawned"  # no args at all
    assert await call(browser_args=None) == "spawned"


async def test_the_real_spawn_tool_is_refused_before_it_touches_a_profile(
    server_mod, mode_on
):
    with pytest.raises(ToolError) as caught:
        await call_tool(
            server_mod,
            "spawn_browser",
            browser_args=["--log-file=x.pth", "--enable-logging"],
        )
    _assert_names_the_vault(caught.value)
    assert "--log-file" in str(caught.value)


@pytest.mark.parametrize(
    "value",
    [
        "C:/profiles/p",
        "C:\\profiles\\p",
        "/home/me/p",
        "sub/acme",
        "~/p",
        "..",
        ".",
        "C:p",
    ],
)
async def test_user_data_dir_takes_a_name_only_while_a_vault_exists(mode_on, value):
    call = _guarded(_spawn_browser)
    with pytest.raises(ToolError) as caught:
        await call(user_data_dir=value)
    _assert_names_the_vault(caught.value)
    if len(value) > 3:  # (a one-dot value is a letter of every sentence)
        assert value not in str(caught.value)
    assert await call(user_data_dir="work") == "spawned"
    assert await call(session="work") == "spawned"
    assert await call(user_data_dir="") == "spawned"


@pytest.mark.parametrize("value", ["C:/profiles/p", "/home/me/p", "work"])
async def test_user_data_dir_is_unchanged_with_no_vault(value):
    assert await _guarded(_spawn_browser)(user_data_dir=value) == "spawned"


def test_every_allowed_switch_is_one_the_other_filters_leave_alone():
    """The allow-list and the stealth filter must not disagree: an allowed switch is
    not stripped, and it is not one a login is refused under."""
    for name, (_why, pattern) in vault_mode.ALLOWED_SWITCHES.items():
        sample = {
            "window-size": "1,2",
            "window-position": "1,2",
            "lang": "en",
            "accept-lang": "en",
            "user-agent": "x",
            "disable-blink-features": "AutomationControlled",
        }.get(name)
        argument = f"--{name}" if pattern is None else f"--{name}={sample}"
        assert pattern is None or pattern.fullmatch(sample), name
        assert name not in browser_trust.REFUSED_SWITCHES, name
        clean, warnings = platform_utils.filter_stealth_args([argument])
        assert clean == [argument] and not warnings, name


def test_every_switch_a_login_is_refused_under_is_refused_here_too():
    for name in browser_trust.REFUSED_SWITCHES:
        assert name not in vault_mode.ALLOWED_SWITCHES, name
        with pytest.raises(ToolError):
            vault_mode.check_browser_args([f"--{name}=x"])


@pytest.mark.parametrize(
    "name",
    [
        "renderer-cmd-prefix",
        "utility-cmd-prefix",
        "gpu-launcher",
        "browser-subprocess-path",
        "ppapi-plugin-launcher",
        "trace-startup-file",
        "net-log",
    ],
)
def test_a_login_is_refused_in_a_browser_started_with_a_program_or_file_switch(name):
    reason = browser_trust.switch_refusal(None, ["chrome.exe", f"--{name}=x"])
    assert reason is not None and f"--{name}" in reason


def test_a_login_is_refused_under_a_foreign_log_file_but_not_under_ours(
    tmp_path, monkeypatch
):
    from stealth_chrome_devtools_mcp.embedded import logging_setup

    monkeypatch.setattr(logging_setup, "resolve_log_dir", lambda: tmp_path / "logs")
    (tmp_path / "logs").mkdir()
    ours = tmp_path / "logs" / "chrome-work-ab12cd34-20261119T000000Z.log"
    assert browser_trust.switch_refusal(None, ["c", f"--log-file={ours}"]) is None
    for foreign in (
        tmp_path / "evil.pth",
        tmp_path / "logs" / "evil.pth",
        tmp_path / "logs" / "chrome-x.pth",
        tmp_path / "other" / "chrome-work-ab12cd34-20261119T000000Z.log",
    ):
        reason = browser_trust.switch_refusal(None, ["c", f"--log-file={foreign}"])
        assert reason is not None and "--log-file" in reason


# --------------------------------------------------------------------- URLs

FILE_URLS = [
    "file:///C:/Users/x/.stealth-mcp/vault.json",
    "FILE:///etc/passwd",
    "File://localhost/etc/passwd",
    "  file:///x",
    "\x01file:///x",
    "fi\tle:///x",
    "fi\nle:///x",
    "view-source:file:///x",
    "VIEW-SOURCE:FILE:///x",
    "view-source:view-source:file:///x",
    "file:C:/x",
    "file:\\\\server\\share\\x",
    "file://[::1/x",
    "C:\\Users\\x\\vault.json",
    "c:/Users/x/vault.json",
    "\\\\server\\share\\x",
]
NOT_FILE_URLS = [
    "https://example.com/file:///x",
    "https://example.com/?u=file:///x",
    "http://file/",
    "about:blank",
    "chrome://version",
    "data:text/html,file:///x",
    "blob:https://example.com/abc",
    "//example.com/x",
    "",
    None,
    7,
]


@pytest.mark.parametrize("url", FILE_URLS)
def test_a_file_url_in_every_spelling_is_one(url):
    assert debug_port_guard.file_url(url)


@pytest.mark.parametrize("url", NOT_FILE_URLS)
def test_a_url_that_is_not_a_file_page_is_not_one(url):
    assert not debug_port_guard.file_url(url)


@pytest.mark.parametrize("url", FILE_URLS)
def test_a_file_url_is_refused_while_a_vault_exists(mode_on, url):
    reason = debug_port_guard.refusal(url)
    assert reason is not None
    _assert_names_the_vault(ToolError(reason))


@pytest.mark.parametrize("url", FILE_URLS)
def test_a_file_url_is_unchanged_with_no_vault(url):
    assert debug_port_guard.refusal(url) is None
    assert not debug_port_guard.barred_url(url)


@pytest.mark.parametrize(
    ("tool", "argument"),
    [
        ("navigate", "url"),
        ("new_tab", "url"),
        ("create_simple_dynamic_hook", "target_url"),
    ],
)
async def test_every_url_tool_refuses_a_file_url_while_a_vault_exists(
    server_mod, mode_on, tool, argument
):
    arguments = {"instance_id": "inst-1", argument: "file:///C:/secret.txt"}
    if tool == "create_simple_dynamic_hook":
        arguments = {
            "name": "h",
            "url_pattern": "*",
            "action": "redirect",
            argument: "view-source:file:///C:/secret.txt",
        }
    with pytest.raises(ToolError) as caught:
        await call_tool(server_mod, tool, **arguments)
    _assert_names_the_vault(caught.value)


def test_the_url_tools_the_mode_names_are_the_ones_the_guard_checks():
    assert set(debug_port_guard.URL_ARGUMENTS) == vault_mode.LOADS_URLS


# ------------------------------------------------------------------- the export


def _logger_with_items(count):
    logger = debug_logger.DebugLogger()
    for number in range(count):
        logger.log_info("t", "m", f"line {number}")
    return logger


def test_a_pickle_export_is_refused_while_a_vault_exists(mode_on, tmp_path):
    logger = _logger_with_items(3)
    for fmt in ("pickle", "gzip-pickle", "PICKLE", " Gzip-Pickle "):
        with pytest.raises(ToolError) as caught:
            logger.export_to_file_paginated(str(tmp_path / "a.json"), fmt=fmt)
        _assert_names_the_vault(caught.value)
    assert not list(tmp_path.glob("a.*"))


def test_auto_is_json_in_vault_mode_however_much_there_is_to_write(
    mode_on, tmp_path, monkeypatch
):
    logger = _logger_with_items(5)
    monkeypatch.setattr(logger, "_PICKLE_THRESHOLD", 1)
    monkeypatch.setattr(logger, "_GZIP_THRESHOLD", 2)  # auto would pick gzip-pickle
    written = logger.export_to_file_paginated(str(tmp_path / "a.json"), fmt="auto")
    assert written.endswith(".json")
    assert not list(tmp_path.glob("*.pkl*"))
    assert logger.export_to_file_paginated(str(tmp_path / "b.json"), fmt="json")


def test_the_lock_free_export_cannot_pick_a_pickle_either(
    mode_on, tmp_path, monkeypatch
):
    logger = _logger_with_items(5)
    monkeypatch.setattr(logger, "_PICKLE_THRESHOLD", 1)
    monkeypatch.setattr(logger, "_GZIP_THRESHOLD", 2)
    monkeypatch.setattr(
        logger, "get_lock_status", lambda: (_ for _ in ()).throw(RuntimeError("x"))
    )
    written = logger.export_to_file_paginated(str(tmp_path / "c.json"), fmt="auto")
    assert written.endswith(".json")


def test_the_export_formats_are_unchanged_with_no_vault(tmp_path, monkeypatch):
    logger = _logger_with_items(5)
    assert logger.export_to_file_paginated(
        str(tmp_path / "p.json"), fmt="pickle"
    ).endswith(".pkl")
    assert logger.export_to_file_paginated(
        str(tmp_path / "g.json"), fmt="gzip-pickle"
    ).endswith(".pkl.gz")
    monkeypatch.setattr(logger, "_PICKLE_THRESHOLD", 1)
    monkeypatch.setattr(logger, "_GZIP_THRESHOLD", 2)
    assert logger.export_to_file_paginated(
        str(tmp_path / "a.json"), fmt="auto"
    ).endswith(".pkl.gz")


# ------------------------------------------------------------ the download pin


def test_the_pin_is_none_with_no_vault():
    assert vault_mode.download_pin() is None


def test_the_pin_names_generated_files_in_one_folder(mode_on, tmp_path):
    pin = vault_mode.download_pin()
    assert pin["behavior"] == "allowAndName"
    assert pathlib.Path(pin["downloadPath"]).samefile(tmp_path / "downloads")
    assert pin["eventsEnabled"] is False
    assert (tmp_path / "downloads").is_dir()


def test_a_folder_the_write_rule_refuses_is_no_pin_at_all(mode_on, monkeypatch):
    def refuse(*_a, **_k):
        raise ToolError("refused")

    monkeypatch.setattr(path_confinement, "refuse_write_path", refuse)
    assert vault_mode.download_pin() == {"behavior": "deny"}


def test_a_folder_that_cannot_be_made_is_no_pin_at_all(mode_on, tmp_path, monkeypatch):
    blocker = tmp_path / "downloads"
    blocker.write_text("a file where the folder goes", encoding="utf-8")
    assert vault_mode.download_pin() == {"behavior": "deny"}


def test_a_denied_pin_is_asked_again_and_the_folder_is_used_once_it_can_be_made(
    mode_on, tmp_path
):
    """A folder that could not be made (a full disk, a share not yet mounted) must
    not hold every download of the process until a restart (audit 7)."""
    blocker = tmp_path / "downloads"
    blocker.write_text("a file where the folder goes", encoding="utf-8")
    assert vault_mode.download_pin() == {"behavior": "deny"}
    blocker.unlink()
    pin = vault_mode.download_pin()
    assert pin["behavior"] == "allowAndName"
    assert pathlib.Path(pin["downloadPath"]).samefile(tmp_path / "downloads")


def test_the_default_download_folder_is_outside_every_refused_place():
    """The real default, not the test's: ``~/Downloads/stealth-mcp-downloads`` passes
    the write rule on this machine (the rule's own answer, not a copy of it)."""
    folder = pathlib.Path.home() / "Downloads" / vault_mode.DOWNLOAD_FOLDER
    assert path_confinement.secret_location_rule(folder / "download") is None
    assert path_confinement.code_location_rule(folder / "download") is None
    assert path_confinement.code_name_rule(folder / "download") is None


# ------------------------------------------------------------------ the census


def _tools():
    return {
        tool.__name__: tool
        for module in tool_sections.SECTION_MODULES
        for tool in module.TOOLS
    }


def _tools_with(*names):
    wanted = set(names)
    return {
        name
        for name, tool in _tools().items()
        if wanted & set(inspect.signature(tool).parameters)
    }


def _calls(tree, wanted):
    """Names (and attribute names) in ``tree`` that are in ``wanted``: called, or
    handed on to be called (``asyncio.to_thread(rule, ...)``)."""
    found = set()
    for node in ast.walk(tree):
        name = (
            node.id
            if isinstance(node, ast.Name)
            else node.attr
            if isinstance(node, ast.Attribute)
            else None
        )
        if name in wanted:
            found.add(name)
    return found


def _function(path, name):
    tree = ast.parse((SRC / path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == name
        ):
            return node
    raise AssertionError(f"{name} is not in {path}")


def test_census_raw_cdp_only_the_named_tool_forwards_a_caller_method():
    """A tool that hands a caller's CDP method to the browser must be in
    ``vault_mode.RAW_CDP``. Two shapes: the executor's own entry point, and a
    ``.send(<a parameter of the tool>, ...)``."""
    reaches = set()
    for name, tool in _tools().items():
        tree = ast.parse(inspect.getsource(tool).lstrip())
        parameters = set(inspect.signature(tool).parameters)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "execute_cdp_command"
            ):
                reaches.add(name)
            if (
                isinstance(function, ast.Attribute)
                and function.attr in {"send", "_send"}
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in parameters
            ):  # tab.send(<the caller's method>)
                reaches.add(name)
    assert reaches == vault_mode.RAW_CDP, (
        "a tool forwards a caller's raw CDP: add it to vault_mode.RAW_CDP (and say "
        f"so in its description): {sorted(reaches ^ vault_mode.RAW_CDP)}"
    )
    # and the executor's resolver is reached from nowhere else
    callers = set()
    for path in SRC.rglob("*.py"):
        if "resolve_cdp_command" in path.read_text(encoding="utf-8"):
            callers.add(path.name)
    assert callers == {"cdp_function_executor.py"}


def test_census_only_the_named_tool_takes_browser_arguments():
    names = (
        "browser_args",
        "user_data_dir",
        "chrome_args",
        "extra_args",
        "launch_args",
    )
    assert _tools_with(*names) == vault_mode.TAKES_BROWSER_ARGS, (
        "a tool takes browser arguments: gate it in vault_mode._ARGUMENT_RULES"
    )
    assert set(vault_mode._ARGUMENT_RULES) == vault_mode.TAKES_BROWSER_ARGS


#: Parameters named like a URL that a tool does NOT load: cookie scopes.
COOKIE_SCOPES = {"clear_cookies", "get_cookies", "set_cookie"}


def test_census_every_tool_that_loads_a_url_is_checked_for_file_urls():
    loads = _tools_with("url", "urls", "target_url", "start_url", "page_url", "link")
    assert loads - COOKIE_SCOPES == vault_mode.LOADS_URLS, (
        "a tool takes a URL: add it to debug_port_guard.URL_ARGUMENTS and to "
        "vault_mode.LOADS_URLS, or pin it as a non-loading scope here"
    )


#: Every tool that takes a caller's file path, and what it does with it. Each is
#: pinned to the function that holds the rule and the rule it must call.
PATH_TOOLS = {
    "upload_file": ("read", "embedded/tool_sections/element_interaction.py", "_checked_paths", "refuse_secret_path"),
    "import_network_data": ("read", "embedded/network_interceptor.py", "import_from_json", "refuse_secret_path"),
    "export_network_data": ("write", "embedded/network_interceptor.py", "export_to_json", "open_text"),
    "take_screenshot": ("write", "embedded/tool_sections/element_interaction.py", "take_screenshot", "precreate_private_off_loop"),
    "export_debug_logs": ("write", "embedded/debug_logger.py", "_export_json", "open_text"),
}  # fmt: skip
#: Parameters named like a path that are NOT a file the tool opens.
NOT_FILES = {("set_cookie", "path")}


def test_census_every_tool_that_takes_a_file_path_is_pinned_to_the_rule():
    names = (
        "file_path", "filepath", "file_paths", "filename", "path", "output_path",
        "output_dir", "directory", "save_path", "dest", "destination",
    )  # fmt: skip
    found = {
        tool
        for tool in _tools_with(*names)
        if not all(
            (tool, parameter) in NOT_FILES
            for parameter in set(inspect.signature(_tools()[tool]).parameters)
            & set(names)
        )
    }
    assert found == set(PATH_TOOLS), (
        "a tool takes a caller's file path: pin it here with the function that "
        f"holds path_confinement's rule: {sorted(found ^ set(PATH_TOOLS))}"
    )
    for tool, (_mode, path, function, rule) in PATH_TOOLS.items():
        assert rule in _calls(_function(path, function), {rule}), (tool, rule)
    # the writers' rule is the one that also refuses code locations and names
    for rule in ("open_text", "open_binary", "precreate_private"):
        assert "refuse_write_path" in inspect.getsource(getattr(path_confinement, rule))


@pytest.fixture
def spy(monkeypatch):
    """Every rule answers with a refusal, and says which one was asked."""
    asked = []

    def writer(path, *, tool, **_k):
        asked.append(("write", tool))
        raise ToolError("spy")

    def reader(path, *, tool, action, **_k):
        asked.append(("read", tool))
        raise ToolError("spy")

    monkeypatch.setattr(path_confinement, "refuse_write_path", writer)
    monkeypatch.setattr(path_confinement, "refuse_secret_path", reader)
    from stealth_chrome_devtools_mcp.embedded import network_interceptor
    from stealth_chrome_devtools_mcp.embedded.tool_sections import element_interaction

    monkeypatch.setattr(network_interceptor, "refuse_secret_path", reader)
    monkeypatch.setattr(element_interaction, "refuse_secret_path", reader)
    return asked


async def test_census_every_writer_and_reader_asks_the_rule_before_touching_a_path(
    spy, tmp_path
):
    from stealth_chrome_devtools_mcp.embedded.network_interceptor import (
        NetworkInterceptor,
    )
    from stealth_chrome_devtools_mcp.embedded.tool_sections import element_interaction

    target = str(tmp_path / "x.json")
    with pytest.raises(ToolError, match="spy"):
        _logger_with_items(1).export_to_file_paginated(target, fmt="json")
    with pytest.raises(ToolError, match="spy"):
        await NetworkInterceptor().export_to_json("i", target)
    with pytest.raises(ToolError, match="spy"):
        await NetworkInterceptor().import_from_json("i", target)
    with pytest.raises(ToolError, match="spy"):
        await path_confinement.precreate_private_off_loop(
            target, tool="take_screenshot"
        )
    with pytest.raises(ToolError, match="spy"):
        element_interaction._checked_paths([target])
    assert sorted(spy) == sorted(
        [
            ("write", "export_debug_logs"),
            ("write", "export_network_data"),
            ("read", "import_network_data"),
            ("write", "take_screenshot"),
            ("read", "upload_file"),
        ]
    )
    assert not list(tmp_path.iterdir())  # and nothing was created


def test_census_the_export_format_is_chosen_in_one_place():
    """Every ``pickle`` write in ``src`` is in ``debug_logger``, and the one place a
    format is picked asks vault mode first."""
    users = {
        path.name
        for path in SRC.rglob("*.py")
        if "pickle.dump" in path.read_text(encoding="utf-8")
    }
    assert users == {"debug_logger.py"}
    asked = _calls(
        _function("embedded/debug_logger.py", "export_to_file_paginated"),
        {"export_format"},
    )
    assert asked == {"export_format"}
    assert _tools_with("format") == {
        "export_debug_logs",
        "take_screenshot",
    }  # + an image's


def test_census_only_the_guard_sets_a_download_folder():
    """A second place that points downloads somewhere is a second way."""
    users = {
        path.name
        for path in SRC.rglob("*.py")
        if any(
            word in path.read_text(encoding="utf-8")
            for word in ("setDownloadBehavior", "set_download_behavior")
        )
    }
    assert users == {"debug_port_guard.py", "vault_mode.py"}


# ------------------------------------------- call-site census (audit 6, N2)
#
# The census tests above look at what a TOOL takes (a parameter named like a path, a
# URL, browser arguments) and at what a tool's own body calls. A tool that hands a
# differently named parameter to a helper in another module would pass them. A true
# taint analysis (does a tool's text reach an OS command, a file, a navigation?)
# across every helper is a project of its own, so these tests work from the SINK
# side instead: every call site in ``src`` that starts an OS command, writes a file
# or navigates a page is listed below with the reason it is safe, and a new site, or
# one that moved to another function, fails until someone has said why. A pin is a
# (file, enclosing function) pair, so a second site in an already pinned function
# is NOT seen, so the reason given must hold for the whole function.

_OS_COMMANDS = {
    "run", "Popen", "call", "check_call", "check_output", "getoutput",
    "getstatusoutput", "system", "popen", "startfile", "execv", "execve",
    "execvp", "execl", "execlp", "spawnl", "spawnv", "create_subprocess_exec",
    "create_subprocess_shell",
}  # fmt: skip
_OS_OWNERS = {"subprocess", "os", "asyncio", "webbrowser", "psutil"}
_NAVIGATIONS = {"navigate", "create_target", "open_tab", "new_tab", "goto"}
_GET_RECEIVERS = {"tab", "browser", "main_tab", "page", "new_tab"}


class _Sites(ast.NodeVisitor):
    def __init__(self, rel):
        self.rel, self.stack = rel, []
        self.commands, self.writes, self.navigations = [], [], []

    def _scope(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):  # noqa: N802  PERMANENT(ast.NodeVisitor's own method names)
        self._scope(node)

    def visit_AsyncFunctionDef(self, node):  # noqa: N802  PERMANENT(ast.NodeVisitor's own method names)
        self._scope(node)

    def visit_ClassDef(self, node):  # noqa: N802  PERMANENT(ast.NodeVisitor's own method names)
        self._scope(node)

    def _site(self):
        return self.rel, ".".join(self.stack) or "<module>"

    def visit_Call(self, node):
        function = node.func
        name = (
            function.attr
            if isinstance(function, ast.Attribute)
            else getattr(function, "id", "")
        )
        owner = (
            ast.unparse(function.value) if isinstance(function, ast.Attribute) else ""
        )
        last = owner.split(".")[-1]
        if name in _OS_COMMANDS and (
            last in _OS_OWNERS or name in {"Popen", "create_subprocess_exec"}
        ):
            self.commands.append(self._site())
        if name in {"write_text", "write_bytes"} or (name == "open" and owner == "os"):
            self.writes.append(self._site())
        elif name == "open" and last not in {"webbrowser", "gzip", "zipfile"}:
            mode = None
            arguments = node.args
            if isinstance(function, ast.Attribute):  # path.open(mode)
                if arguments and isinstance(arguments[0], ast.Constant):
                    mode = arguments[0].value
            elif len(arguments) > 1 and isinstance(arguments[1], ast.Constant):
                mode = arguments[1].value
            for keyword in node.keywords:
                if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                    mode = keyword.value.value
            if isinstance(mode, str) and set("wax+") & set(mode):
                self.writes.append(self._site())
        if isinstance(function, ast.Attribute) and (
            name in _NAVIGATIONS
            or (name == "get" and last in _GET_RECEIVERS and node.args)
        ):
            self.navigations.append(self._site())
        self.generic_visit(node)


def _sites():
    import collections

    found = {
        "commands": collections.Counter(),
        "writes": collections.Counter(),
        "navigations": collections.Counter(),
    }
    for path in sorted(SRC.rglob("*.py")):
        visitor = _Sites(path.relative_to(SRC).as_posix())
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        for kind, counter in found.items():
            counter.update(getattr(visitor, kind))
    return found


#: Every site that starts an OS program. None takes text from a tool: a fixed argv,
#: or a path the OPERATOR configured. The one place a tool's text reaches a program's
#: arguments (``spawn_browser(browser_args)``) is the delegated launch's .ps1, which
#: ``desktop_launch._ps_quote`` quotes and ``test_launcher_script`` parses.
OS_COMMAND_SITES = {
    ("cli_call.py", "_run"): "the `stealthy call` CLI's own subprocess (a shell, no tool)",
    ("embedded/backend_launch.py", "_popen"): "starts the backend: a fixed argv",
    ("embedded/desktop_launch.py", "_schtasks"): "schtasks with a fixed argv; the task name is a token",
    ("embedded/display_context.py", "_macos_context"): "a fixed macOS argv",
    ("embedded/platform_utils.py", "_probe_browser_major_version"): "`<operator's browser> --version`",
    ("embedded/scheduling_lag.py", "_spawn_canary"): "a fixed `python -c` canary",
}  # fmt: skip

#: Every site that writes a file, and why no caller path or caller name decides
#: where it lands. A caller-NAMED path goes through ``path_confinement`` (the
#: spy test above proves each writer asks); the rest are the product's own files.
WRITE_SITES = {
    ("cli_call.py", "_abandon_stdout"): "the CLI's own stdout redirect",
    ("cli_supervise.py", "cmd_supervise"): "the supervisor's own log",
    ("embedded/atomic_file.py", "write_text"): "THE atomic writer: its callers pass the product's own state files",
    ("embedded/atomic_file.py", "write_json"): "the same writer, serialising first",
    ("embedded/atomic_file.py", "_stage_private"): "the same writer for a PRIVATE file (the vault): owner-only before the first byte",
    ("embedded/clone_trash.py", "discard"): "a trash marker beside a disposable clone, the pid and a time",
    ("embedded/profile_copy.py", "replace_tree"): "the staging owner marker (a pid) inside a profile copy",
    ("embedded/backend_launch.py", "_open_boot_log"): "the backend's boot log, fixed name",
    ("embedded/backend_launch.py", "_scheduler_spawn"): "a launch script under the product's state dir",
    ("embedded/backend_token.py", "_create"): "the bearer token file, owner-only",
    ("embedded/browser_pid_registry.py", "_hold_lock"): "a lock file in the state dir",
    ("embedded/chrome_log.py", "_claim"): "chrome-*.log in the log dir, a generated name",
    ("embedded/desktop_launch.py", "write_launcher_script"): "the .ps1 (BOM; every quote doubled; test_launcher_script)",
    ("embedded/file_based_element_cloner.py", "FileBasedElementCloner._save_to_file"): "a GENERATED name in the operator's clone dir; JSON, no caller text as a name",
    ("embedded/fleet_session.py", "record_live_seed"): "the fleet seed marker, the product's state",
    ("embedded/freeze_capture.py", "_write_with_dbghelp"): "a crash dump in the log dir",
    ("embedded/loop_stall.py", "open_fault_log"): "the stall fault log, fixed name",
    ("embedded/path_confinement.py", "_open_fd"): "THE opener behind refuse_write_path",
    ("embedded/profile_lock.py", "_claim_launch"): "a lock file in the profile dir",
    ("embedded/profile_seed.py", "write_marker"): "a session marker the product writes",
    ("embedded/response_handler.py", "ResponseHandler.handle_response"): "a GENERATED name for a large response spill",
    ("embedded/singleton.py", "_exclusive_lock"): "the singleton lock file",
    ("embedded/singleton.py", "_start_server_process"): "the backend's log file, fixed name",
    ("embedded/tool_sections/element_interaction.py", "take_screenshot"): "the caller path, checked by precreate_private_off_loop (PATH_TOOLS)",
    ("embedded/vault_crypto.py", "_file_lock"): "the vault's lock file",
    ("embedded/vault_crypto.py", "write_file"): "the vault file itself, through atomic_file as a private file",
    ("embedded/vault_store.py", "audit"): "the audit log, append only",
}  # fmt: skip

#: Every site that navigates a page, and where its URL comes from. The tools that
#: take a caller URL (``navigate``, ``new_tab``) are refused by ``refuse_urls`` in
#: ``tool_registry`` BEFORE the body runs; every other site has a URL the product
#: made (a literal, or the login's own ``login_url`` from the vault).
NAVIGATION_SITES = {
    ("embedded/browser_manager.py", "BrowserManager._replace_main_tab"): "about:blank (the product's own tab)",
    ("embedded/browser_manager.py", "BrowserManager.navigate"): "the `navigate` tool: refuse_urls runs first",
    ("embedded/browser_trust.py", "profile_refusal"): "the product's own page read",
    ("embedded/login_flow.py", "_open_sealed_tab"): "about:blank",
    ("embedded/login_flow.py", "_sealed_login"): "the vault entry's login_url (a person's, never an agent's)",
    ("embedded/login_flow.py", "_confirm"): "the vault entry's login_url",
    ("embedded/login_persistence.py", "_set_restore_pref"): "chrome://settings, the product's own (why WebUI pages are hidden, not blanked)",
    ("embedded/navigation_milestone.py", "_send_navigate.send"): "the navigate tool's URL, after refuse_urls",
    ("embedded/tab_binding.py", "claim"): "about:blank",
    ("embedded/tab_binding.py", "navigate_callers_tab"): "forwards to BrowserManager.navigate (the `navigate` tool's own)",
    ("embedded/tab_open.py", "open_tab"): "THE tab opener: its callers are pinned here",
    ("embedded/tool_sections/tabs.py", "new_tab"): "the `new_tab` tool: refuse_urls runs first",
}  # fmt: skip


def _census(kind, pinned):
    found = _sites()[kind]
    new = sorted(set(found) - set(pinned))
    gone = sorted(set(pinned) - set(found))
    assert not new, (
        f"a new {kind} call site in src: say why it cannot be reached with an "
        f"agent's text, and pin it here: {new}"
    )
    assert not gone, f"a pinned {kind} site is gone, drop it from the pins: {gone}"


def test_census_every_site_that_starts_an_os_program_is_pinned():
    _census("commands", OS_COMMAND_SITES)


def test_census_every_site_that_writes_a_file_is_pinned():
    _census("writes", WRITE_SITES)


def test_census_every_site_that_navigates_a_page_is_pinned():
    _census("navigations", NAVIGATION_SITES)


def test_the_url_tools_that_navigate_with_a_caller_url_are_checked_before_they_run():
    """The two navigation pins that carry a caller's URL rest on ``refuse_urls``
    being called by the registry for them: pin that, not just the call site."""
    from stealth_chrome_devtools_mcp.embedded import debug_port_guard

    assert {"navigate", "new_tab"} <= set(debug_port_guard.URL_ARGUMENTS)
    assert set(debug_port_guard.URL_ARGUMENTS) == vault_mode.LOADS_URLS


# ----------------------------------------- the tab a call resolves with no tab_id (F1)


class _Tab:
    def __init__(self, target_id: str) -> None:
        self.target = type("T", (), {"target_id": target_id, "type_": "page"})()


@pytest.fixture
def left_on_a_webui_page(monkeypatch):
    """The instance's current tab is ``WEBUI``, a ``chrome://`` page a person left
    there; ``ok`` is a tab of the caller's own."""
    monkeypatch.setattr(tab_binding, "caller", lambda: None)
    monkeypatch.setattr(login_seal, "_webui", {"WEBUI"})
    tabs = {"i": _Tab("WEBUI")}
    return FakeBrowserManager(tabs=tabs)


async def test_a_call_with_no_tab_id_is_refused_on_a_hidden_page_in_vault_mode(
    left_on_a_webui_page, mode_on
):
    with pytest.raises(ToolError) as caught:
        await tab_binding.tab_for_caller(left_on_a_webui_page, "i")
    text = str(caught.value)
    _assert_names_the_vault(caught.value)
    assert "call new_tab" in text
    assert "login is running" not in text
    assert "WEBUI" not in text


async def test_the_same_call_acts_on_that_page_as_before_with_no_vault(
    left_on_a_webui_page,
):
    assert not vault_mode.active()
    tab = await tab_binding.tab_for_caller(left_on_a_webui_page, "i")
    assert tab.target.target_id == "WEBUI"


async def test_a_bound_tab_that_turns_into_a_hidden_page_is_refused_too(
    left_on_a_webui_page, mode_on, monkeypatch
):
    """``go_back`` over pre-vault history: the caller's OWN tab becomes a WebUI page."""
    monkeypatch.setattr(tab_binding, "caller", lambda: "A")
    monkeypatch.setattr(tab_binding, "_bound", {("A", "i"): "OWN"})

    class Browser:
        def __init__(self) -> None:
            self.tabs = [_Tab("OWN")]

        async def update_targets(self) -> None:
            return None

    left_on_a_webui_page._browsers["i"] = Browser()
    monkeypatch.setattr(login_seal, "_webui", {"OWN"})
    with pytest.raises(ToolError, match="stealthy login destroy"):
        await tab_binding.tab_for_caller(left_on_a_webui_page, "i")
    monkeypatch.setattr(login_seal, "_webui", set())
    assert (await tab_binding.tab_for_caller(left_on_a_webui_page, "i")) is not None


async def test_a_tab_of_the_callers_own_is_still_resolved_in_vault_mode(
    mode_on, monkeypatch
):
    monkeypatch.setattr(tab_binding, "caller", lambda: None)
    monkeypatch.setattr(login_seal, "_webui", {"OTHER"})
    manager = FakeBrowserManager(tabs={"i": _Tab("MINE")})
    assert (await tab_binding.tab_for_caller(manager, "i")).target.target_id == "MINE"


async def test_the_state_resources_refuse_a_hidden_current_tab_in_vault_mode(
    server_mod, left_on_a_webui_page, mode_on, monkeypatch
):
    from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt

    monkeypatch.setattr(rt, "browser_manager", left_on_a_webui_page)
    with pytest.raises(ToolError, match="stealthy login destroy"):
        await server_mod._refuse_sealed_resource("i")


async def test_the_state_resources_ask_nothing_with_no_vault(
    server_mod, left_on_a_webui_page, monkeypatch
):
    from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt

    monkeypatch.setattr(rt, "browser_manager", left_on_a_webui_page)
    await server_mod._refuse_sealed_resource("i")


def test_the_refusal_lists_the_one_blink_value_never_the_bare_switch():
    with pytest.raises(ToolError) as caught:
        vault_mode.check_browser_args(["--log-file=x"])
    text = str(caught.value)
    assert "--disable-blink-features=AutomationControlled" in text
    assert "--disable-blink-features," not in text
    assert "--disable-blink-features are" not in text


def test_the_blink_value_is_allowed_inside_a_larger_list_of_allowed_switches(mode_on):
    vault_mode.check_browser_args(
        ["--window-size=1,2", "--disable-blink-features=AutomationControlled"]
    )
