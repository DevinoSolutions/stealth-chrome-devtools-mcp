"""Caller Python in the backend process and the login vault never coexist (T-19366).

The third review read the password, the username and the 32-byte vault key through
``create_python_binding``: the caller's Python runs in the backend with its own
authority. ``caller_python`` is the rule (refuse while unlocked, taint on first run,
no unlock afterwards) and this file pins it, the tools that carry it, the surfaces
that report it, and a census that fails when a new tool evaluates caller Python
without being in the set.
"""

from __future__ import annotations

import ast
import base64
import pathlib

import pytest

from fakes import FakeBrowserManager, call_tool
from stealth_chrome_devtools_mcp.embedded import (
    caller_python,
    dynamic_hook_ai_interface,
    dynamic_hook_system,
    vault_crypto,
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
    monkeypatch.setenv(LOOPBACK_ENV, "1")
    reset()
    yield
    reset()


@pytest.fixture
def key():
    return make_vault({"work": password_entry()})


@pytest.fixture
def server_mod(patched_server):
    return patched_server(browser_manager=FakeBrowserManager())


# ------------------------------------------------------------ the rule, as a unit


def test_the_set_is_the_two_tools_that_run_caller_python():
    assert {
        "create_python_binding",
        "create_dynamic_hook",
    } == caller_python.RUNS_CALLER_PYTHON


@pytest.mark.parametrize("tool", sorted(caller_python.RUNS_CALLER_PYTHON))
def test_while_the_vault_is_unlocked_such_a_tool_is_refused_and_taints_nothing(
    key, tool
):
    vault_store.unlock(key, None)
    with pytest.raises(ToolError) as caught:
        caller_python.admit(tool)
    assert tool in str(caught.value)
    assert "stealthy login lock" in str(caught.value)
    assert not caller_python.tainted()  # a refused call ran nothing


@pytest.mark.parametrize("tool", sorted(caller_python.RUNS_CALLER_PYTHON))
def test_once_one_has_run_the_vault_cannot_be_unlocked(key, tool):
    caller_python.admit(tool)
    assert caller_python.tainted_by() == tool
    with pytest.raises(caller_python.Tainted) as caught:
        vault_store.unlock(key, None)
    assert tool in str(caught.value)
    assert "restart" in str(caught.value)
    assert not vault_store.is_unlocked()  # and nothing was held


def test_a_wrong_key_is_refused_the_same_way_so_the_state_does_not_leak_a_guess(key):
    caller_python.admit("create_python_binding")
    with pytest.raises(caller_python.Tainted):
        vault_store.unlock(b"\x00" * 32, None)


def test_a_tool_outside_the_set_neither_taints_nor_is_refused(key):
    vault_store.unlock(key, None)
    caller_python.admit("execute_script")
    caller_python.admit("execute_python_in_browser")  # transpiled to page JavaScript
    assert not caller_python.tainted()


def test_the_first_tool_is_the_one_recorded():
    caller_python.admit("create_dynamic_hook")
    caller_python.admit("create_python_binding")
    assert caller_python.tainted_by() == "create_dynamic_hook"


def test_unlock_then_lock_then_a_tool_runs_but_the_vault_stays_shut(key):
    vault_store.unlock(key, None)
    vault_store.lock()
    caller_python.admit("create_python_binding")
    with pytest.raises(caller_python.Tainted):
        vault_store.unlock(key, None)


def test_a_hook_that_exists_means_no_key_is_held_or_can_be(key):
    """Existing bindings and hooks are never left callable across an unlock: with
    the rule, none can exist at one. A hook is created while the vault is locked;
    from then on it cannot be unlocked."""
    caller_python.admit("create_dynamic_hook")
    with pytest.raises(caller_python.Tainted):
        vault_store.unlock(key, None)
    assert not vault_store.is_unlocked()
    with pytest.raises(vault_store.VaultLocked):
        vault_store.public_entries()


# ------------------------------------------------------- through the real tools


async def test_the_registry_refuses_create_python_binding_while_unlocked(
    server_mod, key
):
    vault_store.unlock(key, None)
    with pytest.raises(ToolError) as caught:
        await call_tool(
            server_mod,
            "create_python_binding",
            instance_id="i",
            binding_name="leak",
            python_code="def leak(x):\n    return 1\n",
        )
    assert "refused while the vault is unlocked" in str(caught.value)
    assert not caller_python.tainted()


async def test_the_registry_refuses_create_dynamic_hook_while_unlocked(server_mod, key):
    vault_store.unlock(key, None)
    with pytest.raises(ToolError) as caught:
        await call_tool(
            server_mod,
            "create_dynamic_hook",
            name="h",
            requirements={"url_pattern": "*"},
            function_code="def process_request(request):\n    return None\n",
        )
    assert "refused while the vault is unlocked" in str(caught.value)
    assert not dynamic_hook_system.dynamic_hook_system.hooks


async def test_a_tool_that_runs_taints_even_when_its_code_does_not_load(server_mod):
    with pytest.raises(Exception):  # noqa: B017, PT011  PERMANENT(any failure: the question is the taint)
        await call_tool(
            server_mod,
            "create_python_binding",
            instance_id="i",
            binding_name="b",
            python_code="this is not python (",
        )
    assert caller_python.tainted_by() == "create_python_binding"


async def test_a_simple_hook_is_not_caller_python_and_needs_no_lock(server_mod, key):
    vault_store.unlock(key, None)
    await call_tool(
        server_mod,
        "create_simple_dynamic_hook",
        name="s",
        url_pattern="*",
        action="block",
    )
    assert not caller_python.tainted()


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


def _body(key_bytes):
    return {"key": base64.b64encode(key_bytes).decode("ascii"), "ttl_seconds": None}


async def test_the_unlock_route_answers_409_with_the_sentence_once_tainted(key):
    caller_python.admit("create_python_binding")

    class Request:
        async def body(self):
            import json

            return json.dumps(_body(key)).encode()

        async def json(self):

            return _body(key)

    reply = await vault_routes._unlock(Request())
    assert reply.status_code == 409
    import json

    payload = json.loads(reply.body)
    assert payload["unlock_blocked"] is True
    assert "create_python_binding" in payload["error"]
    assert "restart" in payload["error"]
    assert not vault_store.is_unlocked()


def test_status_reports_the_block_without_saying_why_in_code_terms(key):
    assert vault_store.status()["unlock_blocked"] is False
    caller_python.admit("create_dynamic_hook")
    assert vault_store.status()["unlock_blocked"] is True


async def test_list_logins_and_login_say_the_vault_cannot_be_unlocked(server_mod):
    caller_python.admit("create_python_binding")
    for name, args in (
        ("list_logins", {}),
        ("login", {"instance_id": "i", "label": "work"}),
    ):
        with pytest.raises(ToolError) as caught:
            await call_tool(server_mod, name, **args)
        text = str(caught.value)
        assert "locked" in text
        assert "create_python_binding" in text
        assert "restart" in text


def test_stealthy_login_unlock_prints_the_sentence_plainly(monkeypatch, capsys):
    from stealth_chrome_devtools_mcp import cli_login

    sentence = caller_python.UNLOCK_REFUSED.format(tool="create_python_binding")
    monkeypatch.setattr(cli_login, "_require_terminal", lambda _what: None)
    monkeypatch.setattr(cli_login, "_read_header", dict)
    monkeypatch.setattr(cli_login, "hidden_prompt", lambda *_a: "x", raising=False)
    monkeypatch.setattr(cli_login, "_hidden", lambda _prompt: "x")
    from stealth_chrome_devtools_mcp.embedded import vault_crypto as crypto

    monkeypatch.setattr(crypto, "derive_key", lambda *_a: b"k" * 32)
    monkeypatch.setattr(crypto, "open_sealed", lambda *_a: {})
    monkeypatch.setattr(cli_login, "_post_key", lambda *_a: (409, {"error": sentence}))
    import argparse

    code = cli_login.cmd_login(
        argparse.Namespace(login_command="unlock", duration=None)
    )
    assert code != 0
    assert sentence in capsys.readouterr().err


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
        "with a caller's text, add that tool to caller_python.RUNS_CALLER_PYTHON and "
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
    assert reached == caller_python.RUNS_CALLER_PYTHON
    assert registered >= caller_python.RUNS_CALLER_PYTHON
