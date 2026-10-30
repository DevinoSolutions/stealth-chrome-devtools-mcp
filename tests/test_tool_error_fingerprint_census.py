"""The class map of ``tool_error_fingerprint`` names every exception the package handles.

T-10034, review of e03fd55: the library classes the map names were a hand-written
list, made by reading the package once, and it missed every standard-library class
the package handles (``json.JSONDecodeError``, ``http.client.RemoteDisconnected``,
``urllib.error.URLError``, ``asyncio.CancelledError``, ...) and the websockets
handshake classes. A test parametrized over the list itself could not see a name
that is not in it. This one reads the package's own source, finds every class it
names in an ``except`` / ``isinstance`` / ``issubclass`` / ``suppress`` / ``raise`` or
a tuple of classes, resolves each to the class object, and fails when the module that
defines it is not one the map covers.
"""

from __future__ import annotations

import ast
import importlib
import logging
import pathlib
import subprocess
import sys
import types

import pytest

from stealth_chrome_devtools_mcp import tool_error_fingerprint
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

SOURCE = pathlib.Path(tool_error_fingerprint.__file__).parent
PACKAGE = SOURCE.name


def _bindings(tree: ast.AST) -> dict[str, str]:
    """Each name a file imports, as the dotted path it stands for (relative imports
    are banned in this package, so every one is absolute)."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.partition(".")[0]
                bound[alias.asname or top] = alias.name if alias.asname else top
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bound


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (base := _dotted(node.value)):
        return f"{base}.{node.attr}"
    return None


def _named_in_handlers(tree: ast.AST) -> list[ast.AST]:
    """Every expression that can name an exception class: the type of an ``except``,
    the second argument of ``isinstance`` / ``issubclass``, the arguments of
    ``suppress``, what is ``raise``d, and any element of a tuple (a table of classes
    the code reads later: ``judgements = ((httpx.HTTPError, OSError), ...)``)."""
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            found.append(node.type)
        elif isinstance(node, ast.Tuple):
            found.extend(node.elts)
        elif isinstance(node, ast.Raise) and node.exc is not None:
            found.append(node.exc.func if isinstance(node.exc, ast.Call) else node.exc)
        elif isinstance(node, ast.Call):
            callee = _dotted(node.func)
            if callee in {"isinstance", "issubclass"} and len(node.args) == 2:
                found.append(node.args[1])
            elif callee in {"suppress", "contextlib.suppress"}:
                found.extend(node.args)
    return found


def _aliases(tree: ast.AST) -> dict[str, list[ast.AST]]:
    """``_NOT_OPEN_YET = (OSError, http.client.HTTPException)``: a name a file
    assigns an expression, which ``except _NOT_OPEN_YET`` then names."""
    assigned: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, []).append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            assigned.setdefault(node.target.id, []).append(node.value)
    return assigned


def _expanded(
    nodes: list[ast.AST], aliases: dict[str, list[ast.AST]], depth: int = 0
) -> list[ast.AST]:
    """*nodes* with each assigned name replaced by what it was assigned, and each
    tuple by its elements."""
    out: list[ast.AST] = []
    for node in nodes:
        if isinstance(node, ast.Tuple):
            out.extend(_expanded(node.elts, aliases, depth))
        elif isinstance(node, ast.Name) and node.id in aliases and depth < 4:  # noqa: PLR2004  PERMANENT(a chain of aliases is short)
            out.extend(_expanded(aliases[node.id], aliases, depth + 1))
        else:
            out.append(node)
    return out


def _resolve(dotted: str) -> object | None:
    """The object *dotted* names: the longest importable prefix, then attributes."""
    parts = dotted.split(".")
    for end in range(len(parts), 0, -1):
        try:
            found = importlib.import_module(".".join(parts[:end]))
        except Exception:  # noqa: BLE001, S112  PERMANENT(not importable: a shorter prefix, or no object)
            continue
        for attribute in parts[end:]:
            found = getattr(found, attribute, None)
        return found
    return None


def _handled_classes() -> dict[type, set[str]]:
    """``class -> files that name it`` for every exception class the package names
    that is neither a builtin nor defined by the package."""
    handled: dict[type, set[str]] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bound = _bindings(tree)
        for node in _expanded(_named_in_handlers(tree), _aliases(tree)):
            dotted = _dotted(node)
            if dotted is None:
                continue
            head, _, rest = dotted.partition(".")
            if head not in bound:
                continue  # a builtin, a class of this file, or a variable
            kind = _resolve(bound[head] + (f".{rest}" if rest else ""))
            if (
                isinstance(kind, type)
                and issubclass(kind, BaseException)
                and kind.__module__ != "builtins"
                and kind.__module__.partition(".")[0] != PACKAGE
            ):
                handled.setdefault(kind, set()).add(path.name)
    return handled


HANDLED = _handled_classes()


def _label(kind: type) -> str:
    return f"{kind.__module__}.{kind.__qualname__}"


def test_the_census_finds_what_it_is_known_to_find():
    """A census that read nothing would pass every other test here."""
    found = {_label(kind) for kind in HANDLED}

    assert {
        "nodriver.core.connection.ProtocolException",
        "json.decoder.JSONDecodeError",
        "http.client.HTTPException",
        "urllib.error.HTTPError",
        "asyncio.exceptions.CancelledError",
        "subprocess.SubprocessError",
        "struct.error",
        "importlib.metadata.PackageNotFoundError",
        "websockets.exceptions.ConnectionClosed",
        "psutil.NoSuchProcess",
        "httpx.HTTPError",
        "mcp.shared.exceptions.McpError",
    } <= found
    assert len(HANDLED) >= 15


@pytest.mark.parametrize("kind", HANDLED, ids=_label)
def test_a_class_the_package_handles_keeps_a_name(kind):
    """Fails when the module that defines *kind* is in neither module list."""
    named = tool_error_fingerprint.type_name(kind)

    assert named != tool_error_fingerprint.EXTERNAL, (
        f"{_label(kind)} (named in {sorted(HANDLED[kind])}) reads <external>: add the "
        "module that defines it to _STDLIB_MODULES / _LIBRARY_MODULES"
    )
    assert named.endswith(kind.__qualname__)


def test_the_module_lists_are_what_they_say_they_are():
    """The standard-library group is the standard library, the other is not, and a
    module is listed once."""
    stdlib = tool_error_fingerprint._STDLIB_MODULES  # noqa: SLF001  PERMANENT(the lists under test)
    libraries = tool_error_fingerprint._LIBRARY_MODULES  # noqa: SLF001  PERMANENT(the lists under test)

    assert all(name.partition(".")[0] in sys.stdlib_module_names for name in stdlib)
    assert not any(
        name.partition(".")[0] in sys.stdlib_module_names for name in libraries
    )
    assert len({*stdlib, *libraries}) == len(stdlib) + len(libraries)
    assert {"json", "http.client", "urllib.error", "asyncio", "subprocess"} <= {*stdlib}


def test_no_two_classes_share_a_name():
    """Two types under one name are one issue: ``psutil.Error`` and ``sqlite3.Error``,
    ``urllib.error.HTTPError`` and ``httpx.HTTPError``, a builtin ``ConnectionError``
    and requests'."""
    for name in tool_error_fingerprint._LIBRARY_MODULES:  # noqa: SLF001  PERMANENT(every covered library is read)
        try:
            importlib.import_module(name)
        except ImportError:
            continue
    tool_error_fingerprint._refresh()  # noqa: SLF001  PERMANENT(read what was just imported)
    names: dict[str, list[type]] = {}
    for kind, name in tool_error_fingerprint._classes.values():  # noqa: SLF001  PERMANENT(the map under test)
        names.setdefault(name, []).append(kind)

    assert {name: kinds for name, kinds in names.items() if len(kinds) > 1} == {}


def test_a_builtin_a_class_of_ours_and_nodrivers_read_bare_and_the_rest_by_library():
    from nodriver.core.connection import ProtocolException

    named = tool_error_fingerprint.type_name
    assert named(KeyError) == "KeyError"
    assert named(ToolError) == "ToolError"
    assert named(ProtocolException) == "ProtocolException"
    assert named(importlib.import_module("json").JSONDecodeError) == (
        "json.JSONDecodeError"
    )
    assert named(importlib.import_module("psutil").Error) == "psutil.Error"
    assert named(importlib.import_module("sqlite3").Error) == "sqlite3.Error"
    assert named(importlib.import_module("subprocess").TimeoutExpired) == (
        "subprocess.TimeoutExpired"
    )
    assert named(importlib.import_module("psutil").TimeoutExpired) == (
        "psutil.TimeoutExpired"
    )


def test_a_subclass_or_lookalike_of_a_covered_class_is_external():
    covered = importlib.import_module("urllib.error").URLError
    subclass = type("URLError", (covered,), {})
    lookalike = type("URLError", (OSError,), {"__module__": "urllib.error"})
    claims_json = type("JSONDecodeError", (ValueError,), {"__module__": "json.decoder"})

    assert tool_error_fingerprint.type_name(covered) == "urllib.URLError"
    for fake in (subclass, lookalike, claims_json):
        assert tool_error_fingerprint.type_name(fake) == "<external>"


def test_building_the_maps_imports_no_third_party_library():
    """A heavy library is read when something else has already loaded it, never
    imported for the sake of a table (nodriver alone costs every start)."""
    program = (
        "import sys\n"
        "from stealth_chrome_devtools_mcp import tool_error_fingerprint as t\n"
        "libraries = sorted({m.partition('.')[0] for m in t._LIBRARY_MODULES})\n"
        "before = {m for m in libraries if m in sys.modules}\n"
        "t._refresh()\n"
        "print(sorted(m for m in libraries if m in sys.modules and m not in before))\n"
    )
    result = subprocess.run(  # noqa: S603  PERMANENT(our own interpreter and a fixed program)
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "[]"


@pytest.fixture
def empty_maps(monkeypatch):
    """The module as a fresh process has it, with nothing built, judged or read."""
    tool_error_fingerprint._refresh()  # noqa: SLF001  PERMANENT(the real maps are built first, and put back)
    module = tool_error_fingerprint
    bare = {
        key: value
        for key, value in module._classes.items()  # noqa: SLF001  PERMANENT(builtins only)
        if value[0].__module__ == "builtins"
    }
    for name, value in (
        ("_built", False),
        ("_judged", False),
        ("_classes", bare),
        ("_code_labels", {}),
        ("_module_names", {}),
        ("_classes_read", set()),
        ("_stdlib_pending", []),
        ("_library_pending", []),
    ):
        monkeypatch.setattr(module, name, value)


class TestAnEmptyMapIsSaidOnce:
    MESSAGE = "no package source found"

    def _warnings(self, caplog) -> list[logging.LogRecord]:
        return [
            record
            for record in caplog.records
            if record.name == tool_error_fingerprint.__name__
            and record.levelno >= logging.WARNING
        ]

    def test_a_package_with_no_source_files_warns_once_and_raises_nothing(
        self, empty_maps, monkeypatch, tmp_path, caplog
    ):
        """Review of e03fd55, LOW: a zip or sourceless install has no ``.py`` to read,
        every ``ToolError`` reads ``<external>`` and nothing said why."""
        monkeypatch.setattr(tool_error_fingerprint, "_PACKAGE_DIR", tmp_path)

        with caplog.at_level(logging.DEBUG, logger=tool_error_fingerprint.__name__):
            assert tool_error_fingerprint.type_name(ToolError) == "<external>"
            assert tool_error_fingerprint.type_name(KeyError) == "KeyError"
            assert tool_error_fingerprint.frame_name(ToolError("x")) is None

        (record,) = self._warnings(caplog)
        assert record.levelno == logging.WARNING
        assert self.MESSAGE in record.getMessage()
        assert "<external>" in record.getMessage()

    def test_a_package_that_is_read_says_nothing(self, empty_maps, caplog):
        with caplog.at_level(logging.DEBUG, logger=tool_error_fingerprint.__name__):
            assert tool_error_fingerprint.type_name(ToolError) == "ToolError"
            tool_error_fingerprint.type_name(KeyError)

        assert self._warnings(caplog) == []


def test_an_object_that_does_not_claim_a_covered_name_is_not_read(monkeypatch):
    """``sys.modules`` is a dict anyone writes to; a module object that does not even
    claim the name it is registered under is not the module."""
    fake = types.ModuleType("something.else")
    fake.Pretend = type("Pretend", (Exception,), {"__module__": "psutil"})
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(tool_error_fingerprint, "_library_pending", ["psutil"])

    tool_error_fingerprint._refresh()  # noqa: SLF001  PERMANENT(the read under test)

    assert tool_error_fingerprint.type_name(fake.Pretend) == "<external>"
