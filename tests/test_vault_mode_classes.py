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
    path_confinement,
    platform_utils,
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
    "--disable-features=Foo,Bar",
    "--enable-blink-features=Foo,Bar",
    "--disable-blink-features=AutomationControlled",
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
            "disable-features": "A",
            "enable-blink-features": "A",
            "disable-blink-features": "A",
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
