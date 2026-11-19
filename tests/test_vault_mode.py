"""A machine is vault-capable or caller-Python-capable, never both (T-19366).

The third review read the password, the username and the 32-byte vault key through
``create_python_binding``: the caller's Python runs in the backend with its own
authority. The fourth review showed a per-process "taint" is bookkeeping, because
such code can persist itself and come back after the restart that clears it. So the
rule is decided by durable state (``vault_mode``): while a vault file exists the
two tools are refused in every backend; with none they behave as they always did;
and a vault is not created while a backend has already run one. This file pins the
rule, the tools that carry it, the CLI side of it, and a census that fails when a
new tool evaluates caller Python without being in the set.
"""

from __future__ import annotations

import argparse
import ast
import errno
import pathlib

import pytest

from fakes import FakeBrowserManager, call_tool
from stealth_chrome_devtools_mcp import cli_login
from stealth_chrome_devtools_mcp.embedded import (
    backend_registry,
    dynamic_hook_ai_interface,
    dynamic_hook_system,
    vault_crypto,
    vault_mode,
    vault_routes,
    vault_store,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from vault_helpers import LOOPBACK_ENV, make_vault, password_entry, reset

SRC = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "stealth_chrome_devtools_mcp"
)


@pytest.fixture(autouse=True)
def _vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_crypto, "SCRYPT_N", 2**10)
    monkeypatch.setattr(vault_store, "VAULT_PATH", tmp_path / "vault.json")
    monkeypatch.setattr(vault_store, "AUDIT_PATH", tmp_path / "audit.jsonl")
    # the CLI asks the registry's backends: never the real ones
    monkeypatch.setattr(backend_registry, "SERVER_STATE_FILE", tmp_path / "server.json")
    monkeypatch.setattr(backend_registry, "read_backends", lambda _path: [])
    monkeypatch.setenv(LOOPBACK_ENV, "1")
    reset()
    yield
    reset()


@pytest.fixture
def key():
    return make_vault({"work": password_entry()})


@pytest.fixture
def server_mod(patched_server, monkeypatch):
    # the hook table is process-global: a hook made here must not outlive the test
    # (and a hook another file left must not decide this one)
    monkeypatch.setattr(dynamic_hook_system.dynamic_hook_system, "hooks", {})
    # a hook with no instance goes to every instance the process knows, and arming it
    # on one an earlier file left (a tab long gone) is refused: none is known here
    monkeypatch.setattr(dynamic_hook_system.dynamic_hook_system, "instance_hooks", {})
    monkeypatch.setattr(dynamic_hook_system.dynamic_hook_system, "_armed_tabs", {})
    return patched_server(browser_manager=FakeBrowserManager())


# ------------------------------------------------------------ the rule, as a unit


def test_the_set_is_the_two_tools_that_run_caller_python():
    assert {
        "create_python_binding",
        "create_dynamic_hook",
    } == vault_mode.RUNS_CALLER_PYTHON


@pytest.mark.parametrize("tool", sorted(vault_mode.RUNS_CALLER_PYTHON))
def test_with_no_vault_the_tool_runs_as_it_always_did(tool):
    vault_mode.admit(tool)
    assert vault_mode.ran() == tool


@pytest.mark.parametrize("tool", sorted(vault_mode.RUNS_CALLER_PYTHON))
def test_while_a_vault_exists_such_a_tool_is_refused_locked_or_unlocked(key, tool):
    for unlocked in (False, True):
        if unlocked:
            vault_store.unlock(key, None)
        with pytest.raises(ToolError) as caught:
            vault_mode.admit(tool)
        text = str(caught.value)
        assert tool in text
        assert "stealthy login destroy" in text  # how to remove the vault
        assert "disabled" in text
        assert vault_mode.ran() is None  # a refused call ran nothing


def test_removing_the_vault_turns_the_tools_back_on(key):
    with pytest.raises(ToolError):
        vault_mode.admit("create_python_binding")
    vault_store.VAULT_PATH.unlink()
    vault_mode.admit("create_python_binding")
    assert vault_mode.ran() == "create_python_binding"


def test_a_tool_outside_the_set_is_never_refused_and_never_marks(key):
    vault_store.unlock(key, None)
    vault_mode.admit("execute_script")
    vault_mode.admit("execute_python_in_browser")  # transpiled to page JavaScript
    assert vault_mode.ran() is None


def test_the_first_tool_to_run_is_the_one_recorded():
    vault_mode.admit("create_dynamic_hook")
    vault_mode.admit("create_python_binding")
    assert vault_mode.ran() == "create_dynamic_hook"


class _Path:
    """A vault path whose ``stat`` fails the way a real one can: ``os.stat`` asks
    for ``__fspath__`` first. (Patching ``os.stat`` itself would break pytest.)"""

    def __init__(self, error):
        self.error = error

    def __fspath__(self):
        raise self.error


def test_a_vault_path_that_cannot_be_read_counts_as_a_vault(monkeypatch):
    """Fail closed: the tools stay off when the file cannot be looked at."""
    denied = PermissionError(errno.EACCES, "denied")
    monkeypatch.setattr(vault_store, "VAULT_PATH", _Path(denied))
    with pytest.raises(ToolError):
        vault_mode.admit("create_python_binding")


@pytest.mark.parametrize("code", [errno.ENOENT, errno.ENOTDIR])
def test_a_missing_file_or_folder_is_no_vault(monkeypatch, code):
    monkeypatch.setattr(vault_store, "VAULT_PATH", _Path(OSError(code, "missing")))
    vault_mode.admit("create_python_binding")


def test_the_process_is_marked_before_the_file_is_read(monkeypatch):
    """A vault being created at the same moment must see this tool: the CLI writes
    the file, then asks; the tool marks, then reads the file. Whichever is later
    sees the other."""
    seen = []

    def exists():
        seen.append(vault_mode.ran())
        return False

    monkeypatch.setattr(vault_mode, "vault_exists", exists)
    vault_mode.admit("create_dynamic_hook")
    assert seen == ["create_dynamic_hook"]


# ------------------------------------------------------- through the real tools


async def test_the_registry_refuses_create_python_binding_while_a_vault_exists(
    server_mod, key
):
    with pytest.raises(ToolError) as caught:
        await call_tool(
            server_mod,
            "create_python_binding",
            instance_id="i",
            binding_name="leak",
            python_code="def leak(x):\n    return 1\n",
        )
    assert "disabled" in str(caught.value)
    assert vault_mode.ran() is None


async def test_the_registry_refuses_create_dynamic_hook_while_a_vault_exists(
    server_mod, key
):
    vault_store.unlock(key, None)
    with pytest.raises(ToolError) as caught:
        await call_tool(
            server_mod,
            "create_dynamic_hook",
            name="h",
            requirements={"url_pattern": "*"},
            function_code="def process_request(request):\n    return None\n",
        )
    assert "disabled" in str(caught.value)
    assert not dynamic_hook_system.dynamic_hook_system.hooks


async def test_with_no_vault_the_registry_lets_a_hook_through(server_mod):
    result = await call_tool(
        server_mod,
        "create_dynamic_hook",
        name="h",
        requirements={"url_pattern": "*"},
        function_code="def process_request(request):\n    return None\n",
    )
    assert result
    assert vault_mode.ran() == "create_dynamic_hook"


async def test_a_tool_that_runs_marks_even_when_its_code_does_not_load(server_mod):
    with pytest.raises(Exception):  # noqa: B017, PT011  PERMANENT(any failure: the question is the mark)
        await call_tool(
            server_mod,
            "create_python_binding",
            instance_id="i",
            binding_name="b",
            python_code="this is not python (",
        )
    assert vault_mode.ran() == "create_python_binding"


async def test_a_simple_hook_is_not_caller_python_and_needs_no_vault_decision(
    server_mod, key
):
    vault_store.unlock(key, None)
    await call_tool(
        server_mod,
        "create_simple_dynamic_hook",
        name="s",
        url_pattern="*",
        action="block",
    )
    assert vault_mode.ran() is None


# The payloads are TEXT handed to the template under test, which must keep them as
# data; none is ever run.
@pytest.mark.parametrize(
    "payload",
    [
        'https://x/"); import os; os.system("calc"); HookAction(action="redirect", url="',
        "https://x/'\\\"\n__import__('os').getcwd()\n#",
    ],
)
def test_a_simple_hook_writes_the_callers_values_as_literals(payload):
    namespace = {"HookAction": lambda **kw: kw}
    code = dynamic_hook_ai_interface._simple_hook_code("redirect", payload, None)
    exec(code, namespace)  # noqa: S102  PERMANENT(the template under test)
    assert namespace["process_request"]({}) == {"action": "redirect", "url": payload}
    tree = ast.parse(code)
    assert [type(n).__name__ for n in tree.body] == ["FunctionDef"]  # nothing else


def test_simple_hook_headers_are_literals_too():
    headers = {"X-A": "a'b\"c", "X-B": "');import os;('"}
    code = dynamic_hook_ai_interface._simple_hook_code("add_headers", None, headers)
    namespace = {"HookAction": lambda **kw: kw}
    exec(code, namespace)  # noqa: S102  PERMANENT(the template under test)
    out = namespace["process_request"]({"headers": {"Existing": "1"}})
    assert out["headers"] == {"Existing": "1", **headers}


@pytest.mark.parametrize("headers", [{"a": 1}, {1: "a"}, ["a"], "a"])
def test_simple_hook_headers_must_be_strings(headers):
    with pytest.raises(Exception, match="custom_headers"):
        dynamic_hook_ai_interface._simple_hook_code("add_headers", None, headers)


# ------------------------------------------------------------ the surfaces


async def test_the_route_says_which_tool_ran_and_nothing_else():
    import json

    reply = await vault_routes._caller_python(None)
    assert json.loads(reply.body) == {"ran": None}
    vault_mode.admit("create_python_binding")
    reply = await vault_routes._caller_python(None)
    assert json.loads(reply.body) == {"ran": "create_python_binding"}


def test_the_status_and_the_unlock_route_have_no_taint_left_in_them(key):
    assert set(vault_store.status()) == {"exists", "unlocked", "expires_in"}
    assert not hasattr(vault_mode, "Tainted")
    assert not hasattr(vault_mode, "unlock_gate")


class _Reply:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _backends(monkeypatch, replies, *, gone=True, pids=None):
    """Record these backends and answer each port from ``replies``. ``gone`` is what
    a refused connection means for their processes (dead, or still running)."""
    import httpx

    entries = [
        {"port": port, "version": "9.9", **({"pid": pids[port]} if pids else {})}
        for port in replies
    ]
    monkeypatch.setattr(backend_registry, "read_backends", lambda _path: entries)
    monkeypatch.setattr(cli_login, "_backend_gone", lambda _entry: gone)
    monkeypatch.setattr(backend_registry, "read_record", lambda _path: {})
    requests = []

    class Client:
        def __init__(self, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def request(self, method, url, headers=None):
            port = int(url.split(":")[2].split("/")[0])
            requests.append((method, port, url.split(str(port))[1]))
            answer = replies[port]
            if isinstance(answer, Exception):
                raise answer
            return answer

    monkeypatch.setattr(httpx, "Client", Client)
    return requests


def test_a_clean_backend_and_a_dead_one_do_not_stop_a_vault(monkeypatch):
    import httpx

    _backends(
        monkeypatch,
        {1001: _Reply(200, {"ran": None}), 1002: httpx.ConnectError("gone")},
    )
    assert cli_login._backend_problems() == []


def test_a_backend_that_ran_a_tool_stops_a_vault_and_the_sentence_names_the_tool(
    monkeypatch,
):
    _backends(monkeypatch, {1001: _Reply(200, {"ran": "create_python_binding"})})
    problems = cli_login._backend_problems()
    assert len(problems) == 1
    assert "1001" in problems[0]
    assert "create_python_binding" in problems[0]


@pytest.mark.parametrize(
    "answer",
    [_Reply(404, {}), _Reply(200, ValueError("not json")), _Reply(200, {"x": 1})],
)
def test_a_backend_that_cannot_say_stops_a_vault_too(monkeypatch, answer):
    _backends(monkeypatch, {1001: answer})
    problems = cli_login._backend_problems()
    assert len(problems) == 1
    assert "cannot say" in problems[0]
    assert "9.9" in problems[0]  # an older release is named


def test_a_backend_that_times_out_stops_a_vault(monkeypatch):
    import httpx

    _backends(monkeypatch, {1001: httpx.ReadTimeout("slow")})
    assert "did not answer" in cli_login._backend_problems()[0]


def _cli(monkeypatch, tmp_path, problems, passphrase="x"):
    """``stealthy login init`` with the prompts answered and the backends faked.
    ``problems`` is the list of answers ``_backend_problems`` gives in turn."""
    monkeypatch.setattr(cli_login, "_require_terminal", lambda _what: None)
    monkeypatch.setattr(cli_login, "_hidden", lambda _prompt: passphrase)
    asked = []

    def backend_problems():
        asked.append(vault_store.VAULT_PATH.exists())
        return problems.pop(0)

    monkeypatch.setattr(cli_login, "_backend_problems", backend_problems)
    return asked


def _run(command, **fields):
    return cli_login.cmd_login(argparse.Namespace(login_command=command, **fields))


def test_init_says_what_it_disables_before_it_asks_for_a_passphrase(
    monkeypatch, tmp_path, capsys
):
    asked = _cli(monkeypatch, tmp_path, [[], []])
    order = []
    monkeypatch.setattr(
        cli_login, "_hidden", lambda _prompt: order.append("asked") or "x"
    )
    assert _run("init") == 0
    out = capsys.readouterr().out
    for line in vault_mode.describe():  # EVERY restriction, from the one list
        assert line in out
    assert "VAULT MODE" in out
    assert "stealthy login destroy" in out
    assert out.index("VAULT MODE") < out.index("created")
    assert asked == [False, True]  # before the file, and again once it exists
    assert vault_store.VAULT_PATH.exists()
    assert order  # and the passphrase was asked after the notice


def test_init_refuses_while_a_backend_has_run_caller_python(
    monkeypatch, tmp_path, capsys
):
    asked = _cli(monkeypatch, tmp_path, [["the backend on port 1 has run X"]])
    monkeypatch.setattr(
        cli_login, "_hidden", lambda _p: pytest.fail("a passphrase was asked for")
    )
    assert _run("init") == 1
    err = capsys.readouterr().err
    assert "has run X" in err
    assert "stealth-chrome-devtools restart" in err
    assert not vault_store.VAULT_PATH.exists()
    assert asked == [False]


def test_init_takes_the_vault_back_when_a_tool_ran_while_it_was_made(
    monkeypatch, tmp_path, capsys
):
    """The tool was admitted between the first look and the file: the second look
    sees it, and the file is gone again, so no secret sits beside that code."""
    _cli(monkeypatch, tmp_path, [[], ["the backend on port 1 has run X"]])
    assert _run("init") == 1
    assert "has run X" in capsys.readouterr().err
    assert not vault_store.VAULT_PATH.exists()


def test_status_shows_that_the_tools_are_disabled_only_while_a_vault_exists(
    monkeypatch, capsys
):
    monkeypatch.setattr(cli_login, "_request", _no_backend)
    _run("status")
    out = capsys.readouterr().out
    assert "vault mode: OFF" in out
    assert "are disabled" not in out
    make_vault({"work": password_entry()})
    _run("status")
    out = capsys.readouterr().out
    assert "vault mode: ON" in out
    for line in vault_mode.describe():
        assert line in out


def _no_backend(*_a, **_k):
    raise cli_login._NoBackendError("no backend")


def test_destroy_deletes_the_vault_after_the_word_and_turns_the_tools_back_on(
    monkeypatch, capsys
):
    make_vault({"work": password_entry()})
    monkeypatch.setattr(cli_login, "_require_terminal", lambda _what: None)
    monkeypatch.setattr(cli_login, "_request", _no_backend)
    monkeypatch.setattr("builtins.input", lambda _prompt: "DESTROY")
    with pytest.raises(ToolError):
        vault_mode.admit("create_python_binding")
    assert _run("destroy") == 0
    assert not vault_store.VAULT_PATH.exists()
    vault_mode.admit("create_python_binding")
    assert "deleted" in capsys.readouterr().out


def test_destroy_locks_a_backend_that_still_holds_the_key(monkeypatch):
    make_vault({"work": password_entry()})
    calls = []
    monkeypatch.setattr(cli_login, "_require_terminal", lambda _what: None)
    monkeypatch.setattr(
        cli_login, "_request", lambda *a, **k: calls.append(a) or (200, {})
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "DESTROY")
    assert _run("destroy") == 0
    assert calls == [("POST", vault_routes.LOCK_PATH)]  # the one this shell resolves


def test_destroy_does_nothing_without_the_word_or_without_a_vault(monkeypatch, capsys):
    monkeypatch.setattr(cli_login, "_require_terminal", lambda _what: None)
    assert _run("destroy") == 1  # no vault
    make_vault({"work": password_entry()})
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert _run("destroy") == 1
    assert vault_store.VAULT_PATH.exists()
    assert "nothing was deleted" in capsys.readouterr().err


def test_destroy_needs_a_terminal(monkeypatch):
    make_vault({"work": password_entry()})
    monkeypatch.setattr(cli_login.sys.stdin, "isatty", lambda: False, raising=False)
    assert _run("destroy") != 0
    assert vault_store.VAULT_PATH.exists()


async def test_list_logins_and_login_answer_locked_with_no_caller_python_talk(
    server_mod,
):
    for name, args in (
        ("list_logins", {}),
        ("login", {"instance_id": "i", "label": "work"}),
    ):
        with pytest.raises(ToolError) as caught:
            await call_tool(server_mod, name, **args)
        assert "locked" in str(caught.value)
        assert "restart" not in str(caught.value)


# ------------------------------------------------------------------ the census

#: Every call that evaluates Python text or loads Python code by name, in ``src``:
#: ``(file, enclosing function, call)`` -> the tool in ``RUNS_CALLER_PYTHON`` that
#: reaches it, or ``None`` with the reason the input is not the caller's.
PINNED = {
    (
        "embedded/tool_sections/cdp_functions.py",
        "create_python_binding",
        "exec",
    ): "create_python_binding",
    (
        "embedded/dynamic_hook_model.py",
        "DynamicHook._compile_function",
        "exec",
    ): "create_dynamic_hook",
    (
        "embedded/dynamic_hook_model.py",
        "DynamicHook.matches",
        "eval",
    ): "create_dynamic_hook",
    ("server.py", "main", "run_path"): None,  # the backend's own module, at startup
    ("tool_error_fingerprint.py", "_build", "compile"): None,  # a repo file
    ("tool_error_fingerprint.py", "_loaded", "import_module"): None,  # a repo module
}
_CALLS = {
    "exec",
    "eval",
    "compile",
    "__import__",
    "exec_module",
    "import_module",
    "run_path",
    "run_module",
    "spec_from_file_location",
    "load_module",
    "loads",
    "load",
}
_ATTRIBUTE_ONLY = {"loads", "load"}  # pickle / marshal, not json
_UNSAFE_OWNERS = {"pickle", "marshal", "cloudpickle", "dill"}


class _Sites(ast.NodeVisitor):
    """Every call in one module that evaluates Python text or loads code by name."""

    def __init__(self, relative: str) -> None:
        self.relative = relative
        self.stack: list[str] = []
        self.found: set[tuple[str, str, str]] = set()

    def _scope(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):  # noqa: N802  PERMANENT(ast.NodeVisitor's naming)
        self._scope(node)

    visit_AsyncFunctionDef = visit_ClassDef = visit_FunctionDef  # noqa: N815  PERMANENT(ast.NodeVisitor's naming)

    def _note(self, call: str) -> None:
        self.found.add((self.relative, ".".join(self.stack), call))

    def visit_Call(self, node):  # noqa: N802  PERMANENT(ast.NodeVisitor's naming)
        function = node.func
        if isinstance(function, ast.Name) and function.id in _CALLS - _ATTRIBUTE_ONLY:
            self._note(function.id)
        elif isinstance(function, ast.Attribute) and function.attr in _CALLS:
            owner = function.value
            if function.attr in _ATTRIBUTE_ONLY:
                if isinstance(owner, ast.Name) and owner.id in _UNSAFE_OWNERS:
                    self._note(function.attr)
            elif function.attr != "compile":  # re.compile is a regular expression
                self._note(function.attr)
        self.generic_visit(node)


def _sites():
    found = set()
    for path in SRC.rglob("*.py"):
        visitor = _Sites(path.relative_to(SRC).as_posix())
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        found |= visitor.found
    return found


def test_every_site_that_evaluates_python_is_pinned_to_a_tool_or_to_a_reason():
    found = _sites()
    unpinned = sorted(found - set(PINNED))
    assert not unpinned, (
        "a new exec / eval / compile / import-by-name site: if a tool can reach it "
        "with a caller's text, add that tool to vault_mode.RUNS_CALLER_PYTHON and "
        f"pin the site in PINNED; otherwise pin it with the reason: {unpinned}"
    )
    vanished = sorted(set(PINNED) - found)
    assert not vanished, f"a pinned site is gone, drop it from PINNED: {vanished}"


def test_every_pinned_tool_is_in_the_set_and_is_a_registered_tool():
    from stealth_chrome_devtools_mcp.embedded import tool_sections

    registered = {
        tool.__name__
        for module in tool_sections.SECTION_MODULES
        for tool in module.TOOLS
    }
    reached = {tool for tool in PINNED.values() if tool is not None}
    assert reached == vault_mode.RUNS_CALLER_PYTHON
    assert registered >= vault_mode.RUNS_CALLER_PYTHON


# ----------------------------------------- unlock, status, destroy, init (audit 5)


def test_unlock_runs_the_census_before_a_passphrase_is_asked(monkeypatch, capsys):
    """A vault restored from a backup never went through ``init``: A4."""
    make_vault({"work": password_entry()})
    monkeypatch.setattr(cli_login, "_require_terminal", lambda _what: None)
    monkeypatch.setattr(
        cli_login, "_backend_problems", lambda: ["the backend on port 7 has run X"]
    )
    monkeypatch.setattr(
        cli_login, "_hidden", lambda _p: pytest.fail("a passphrase was asked for")
    )
    posted = []
    monkeypatch.setattr(cli_login, "_post_key", lambda *a: posted.append(a))
    assert _run("unlock", duration=None) == 1
    err = capsys.readouterr().err
    assert "has run X" in err
    assert "nothing was unlocked" in err
    assert not posted


def test_unlock_goes_on_when_every_backend_is_clean(monkeypatch, capsys):
    make_vault({"work": password_entry()})
    monkeypatch.setattr(cli_login, "_require_terminal", lambda _what: None)
    monkeypatch.setattr(cli_login, "_backend_problems", list)
    monkeypatch.setattr(cli_login, "_hidden", lambda _p: "correct horse")
    monkeypatch.setattr(cli_login, "_post_key", lambda *a: (200, {"entries": 1}))
    assert _run("unlock", duration=None) == 0
    assert "unlocked" in capsys.readouterr().out


def test_status_reports_a_backend_that_ran_caller_python_and_a_disagreement(
    monkeypatch, capsys
):
    make_vault({"work": password_entry()})
    monkeypatch.setattr(
        cli_login, "_backend_problems", lambda: ["the backend on port 7 has run X"]
    )
    monkeypatch.setattr(
        cli_login,
        "_request",
        lambda *a, **k: (200, {"exists": False, "unlocked": False, "expires_in": None}),
    )
    assert _run("status") == 0
    out = capsys.readouterr().out
    assert "backends: ! the backend on port 7 has run X" in out
    assert "disagree" in out  # this shell sees a vault the backend does not


def test_status_says_so_when_every_backend_is_clean(monkeypatch, capsys):
    monkeypatch.setattr(cli_login, "_backend_problems", list)
    monkeypatch.setattr(cli_login, "_request", _no_backend)
    assert _run("status") == 0
    assert "no registered backend has run caller Python" in capsys.readouterr().out


def test_an_older_release_is_named_in_the_sentence_that_refuses(monkeypatch):
    _backends(monkeypatch, {1001: _Reply(404, {})})
    (problem,) = cli_login._backend_problems()
    assert "9.9" in problem
    assert "before vault mode" in problem


def test_a_refused_connection_to_a_live_process_is_cannot_say_not_dead(monkeypatch):
    import httpx

    _backends(
        monkeypatch, {1001: httpx.ConnectError("refused")}, gone=False, pids={1001: 42}
    )
    (problem,) = cli_login._backend_problems()
    assert "1001" in problem
    assert "pid 42" in problem
    assert "still running" in problem


def test_a_refused_connection_to_a_gone_process_is_dead(monkeypatch):
    import httpx

    _backends(monkeypatch, {1001: httpx.ConnectError("refused")}, gone=True)
    assert cli_login._backend_problems() == []


def test_a_process_is_gone_when_it_does_not_exist_or_is_not_ours(monkeypatch):
    import psutil

    assert cli_login._backend_gone({"pid": 2**22 + 12345}) is True
    assert cli_login._backend_gone({}) is False  # no pid recorded: cannot say
    assert cli_login._backend_gone({"pid": None}) is False

    class Process:
        def __init__(self, argv):
            self.argv = argv

        def cmdline(self):
            return self.argv

    for argv, gone in (
        (["python", "-m", "stealth_chrome_devtools_mcp", "--transport", "http"], False),
        (["C:/tools/stealth-chrome-devtools.exe"], False),
        (["notepad.exe"], True),  # the pid was reused by something else
    ):
        monkeypatch.setattr(psutil, "Process", lambda _pid, argv=argv: Process(argv))
        assert cli_login._backend_gone({"pid": 5}) is gone, argv

    def denied(_pid):
        raise psutil.AccessDenied(_pid)

    monkeypatch.setattr(psutil, "Process", denied)
    assert cli_login._backend_gone({"pid": 5}) is False  # cannot read it: cannot say


def test_an_unreadable_registry_is_cannot_say_and_a_missing_one_is_no_backends():
    registry = backend_registry.SERVER_STATE_FILE
    assert cli_login._registry_problem() is None  # no file: no backends recorded
    registry.write_text("{not json", encoding="utf-8")
    assert "unreadable" in cli_login._registry_problem()
    registry.write_text('{"schema": 3, "backends": []}', encoding="utf-8")
    assert cli_login._registry_problem() is None
    registry.unlink()
    registry.mkdir()  # there, and not a file anyone can read
    assert "unreadable" in cli_login._registry_problem()


def test_init_refuses_when_the_registry_cannot_be_read(monkeypatch, capsys):
    backend_registry.SERVER_STATE_FILE.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cli_login, "_require_terminal", lambda _what: None)
    monkeypatch.setattr(
        cli_login, "_hidden", lambda _p: pytest.fail("a passphrase was asked for")
    )
    assert _run("init") == 1
    assert "server.json" in capsys.readouterr().err
    assert not vault_store.VAULT_PATH.exists()


def test_destroy_locks_every_registered_backend_not_only_the_resolved_one(
    monkeypatch, capsys
):
    make_vault({"work": password_entry()})
    requests = _backends(
        monkeypatch,
        {1001: _Reply(200, {}), 1002: _Reply(200, {}), 1003: _Reply(200, {})},
    )
    monkeypatch.setattr(cli_login, "_require_terminal", lambda _what: None)
    monkeypatch.setattr(cli_login, "_request", _no_backend)
    monkeypatch.setattr("builtins.input", lambda _prompt: "DESTROY")
    assert _run("destroy") == 0
    assert sorted(requests) == [
        ("POST", port, vault_routes.LOCK_PATH) for port in (1001, 1002, 1003)
    ]
    assert not vault_store.VAULT_PATH.exists()
    assert capsys.readouterr().err == ""


def test_destroy_deletes_anyway_and_says_which_backend_may_still_hold_the_key(
    monkeypatch, capsys
):
    import httpx

    make_vault({"work": password_entry()})
    _backends(
        monkeypatch,
        {
            1001: _Reply(500, {}),
            1002: httpx.ReadTimeout("slow"),
            1003: httpx.ConnectError("refused"),
        },
        gone=False,
        pids={1001: 1, 1002: 2, 1003: 3},
    )
    monkeypatch.setattr(cli_login, "_require_terminal", lambda _what: None)
    monkeypatch.setattr(cli_login, "_request", _no_backend)
    monkeypatch.setattr("builtins.input", lambda _prompt: "DESTROY")
    assert _run("destroy") == 0
    err = capsys.readouterr().err
    for port in ("1001", "1002", "1003"):
        assert f"port {port}" in err
    assert "may still hold the key" in err
    assert not vault_store.VAULT_PATH.exists()


def test_destroy_on_a_directory_is_a_clean_error_and_the_mode_stays_on(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "a-directory"
    path.mkdir()
    monkeypatch.setattr(vault_store, "VAULT_PATH", path)
    monkeypatch.setattr(cli_login, "_require_terminal", lambda _what: None)
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("asked to confirm")
    )
    assert _run("destroy") == 1
    err = capsys.readouterr().err
    assert "is a directory" in err
    assert "Traceback" not in err
    assert vault_mode.vault_exists()
    with pytest.raises(ToolError):
        vault_mode.admit("create_python_binding")


def test_destroy_that_cannot_delete_says_so_and_the_mode_stays_on(monkeypatch, capsys):
    make_vault({"work": password_entry()})
    monkeypatch.setattr(cli_login, "_require_terminal", lambda _what: None)
    monkeypatch.setattr(cli_login, "_request", _no_backend)
    monkeypatch.setattr("builtins.input", lambda _prompt: "DESTROY")

    def deny(self, *a, **k):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(pathlib.Path, "unlink", deny)
    assert _run("destroy") == 1
    err = capsys.readouterr().err
    assert "could not delete" in err
    assert "stays on" in err
    assert "Traceback" not in err


def test_an_interrupted_init_takes_the_file_back_and_says_nothing_false(
    monkeypatch, tmp_path, capsys
):
    """B2: the second look is interrupted AFTER the file was written."""
    _cli(monkeypatch, tmp_path, [[]])
    calls = []

    def problems():
        calls.append(vault_store.VAULT_PATH.exists())
        if len(calls) == 2:
            raise KeyboardInterrupt
        return []

    monkeypatch.setattr(cli_login, "_backend_problems", problems)
    assert _run("init") != 0
    assert calls == [False, True]
    assert not vault_store.VAULT_PATH.exists()  # so "nothing was changed" is true
    assert "nothing was changed" in capsys.readouterr().err


def test_a_crash_in_the_second_look_takes_the_file_back_too(monkeypatch, tmp_path):
    _cli(monkeypatch, tmp_path, [[]])
    calls = []

    def problems():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return []

    monkeypatch.setattr(cli_login, "_backend_problems", problems)
    with pytest.raises(RuntimeError):
        cli_login._cmd_init(argparse.Namespace())
    assert not vault_store.VAULT_PATH.exists()


def test_an_interrupted_init_that_cannot_remove_the_file_says_what_remains(
    monkeypatch, tmp_path, capsys
):
    _cli(monkeypatch, tmp_path, [[]])
    calls = []

    def problems():
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return []

    def deny(self, *a, **k):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(cli_login, "_backend_problems", problems)
    monkeypatch.setattr(pathlib.Path, "unlink", deny)
    assert _run("init") == 1
    err = capsys.readouterr().err
    assert "was created" in err
    assert "could not be removed" in err
    assert "stealthy login destroy" in err
    assert "nothing was changed" not in err


def test_init_warns_about_a_short_passphrase_and_not_about_a_long_one(
    monkeypatch, tmp_path, capsys
):
    _cli(monkeypatch, tmp_path, [[], []], passphrase="short")
    assert _run("init") == 0
    assert "fewer than 12" in capsys.readouterr().err
    vault_store.VAULT_PATH.unlink()
    _cli(monkeypatch, tmp_path, [[], []], passphrase="a long enough passphrase")
    assert _run("init") == 0
    assert "fewer than" not in capsys.readouterr().err


def test_init_refuses_over_a_dangling_link(tmp_path, monkeypatch, capsys):
    link = tmp_path / "link.json"
    try:
        link.symlink_to(tmp_path / "nowhere.json")
    except OSError:
        pytest.skip("this machine does not let the test make a symbolic link")
    monkeypatch.setattr(vault_store, "VAULT_PATH", link)
    monkeypatch.setattr(cli_login, "_require_terminal", lambda _what: None)
    assert _run("init") == 1
    assert "already exists" in capsys.readouterr().err
    assert link.is_symlink()  # not replaced
