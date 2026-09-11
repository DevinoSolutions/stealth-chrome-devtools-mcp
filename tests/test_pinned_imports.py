"""F-865: every third-party module the source imports is pinned to the version
the lockfile carries, so the gate tests the versions users install.

The direct dependencies were already exact pins, but the source also imports
packages that arrive only transitively (``mcp`` through ``fastmcp``, ``anyio``,
``starlette``, ``httpx``) and one with a range (``requests``). A universal
``uv.lock`` resolves those once for every Python the project supports, while a
user's installer resolves them again for one Python: on 2026-09-11 the lock (and
so every gate cell) had ``mcp 1.27.1`` while ``uv tool install ==2.1.2`` on
Python 3.12 got ``mcp 1.30.0`` — a library whose private session manager
``session_hygiene`` subclasses. ``tools/check_pinned_imports.py`` is the one
check: an imported distribution must appear in ``[project.dependencies]`` as an
exact pin, and that pin must equal ``uv.lock``.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import check_pinned_imports as cpi  # noqa: E402  PERMANENT(tools/ is not an importable package; the sys.path line above must run first)

LOCK = textwrap.dedent(
    """
    version = 1

    [[package]]
    name = "httpx"
    version = "0.28.1"

    [[package]]
    name = "requests"
    version = "2.34.2"
    """
)


def _pyproject(deps: list[str], optional: dict[str, list[str]] | None = None) -> str:
    dep_lines = "".join(f'    "{d}",\n' for d in deps)
    text = f'[project]\nname = "x"\ndependencies = [\n{dep_lines}]\n'
    if optional:
        text += "\n[project.optional-dependencies]\n"
        for extra, specs in optional.items():
            text += f"{extra} = [{', '.join(repr(s) for s in specs)}]\n"
    return text


def _src(tmp_path: Path, **modules: str) -> Path:
    root = tmp_path / "pkg"
    root.mkdir()
    for name, body in modules.items():
        (root / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return root


def test_the_real_tree_has_no_floating_import() -> None:
    assert cpi.violations(cpi.SRC_ROOT, cpi.pyproject_text(), cpi.lock_text()) == []


def test_an_import_without_an_exact_pin_bites(tmp_path: Path) -> None:
    src = _src(tmp_path, a="import httpx\n")
    out = cpi.violations(src, _pyproject(["requests==2.34.2"]), LOCK)
    assert len(out) == 1
    assert "httpx" in out[0]
    assert "a.py" in out[0]
    assert "not pinned" in out[0]


def test_a_range_is_not_a_pin(tmp_path: Path) -> None:
    src = _src(tmp_path, a="import requests\n")
    out = cpi.violations(src, _pyproject(["requests>=2.32.0,<4"]), LOCK)
    assert len(out) == 1
    assert "requests" in out[0]


def test_a_pin_that_disagrees_with_the_lock_bites(tmp_path: Path) -> None:
    src = _src(tmp_path, a="from httpx import Client\n")
    out = cpi.violations(src, _pyproject(["httpx==0.27.0"]), LOCK)
    assert len(out) == 1
    assert "0.27.0" in out[0]
    assert "0.28.1" in out[0]


def test_pinned_at_the_locked_version_passes(tmp_path: Path) -> None:
    src = _src(tmp_path, a="import httpx\nimport requests\n")
    deps = ["httpx==0.28.1", "requests==2.34.2"]
    assert cpi.violations(src, _pyproject(deps), LOCK) == []


def test_stdlib_and_first_party_imports_are_not_third_party(tmp_path: Path) -> None:
    src = _src(
        tmp_path,
        a="import json\nfrom pathlib import Path\nfrom pkg.b import thing\n",
        b="thing = 1\n",
    )
    assert cpi.third_party_imports(src) == {}


def test_an_optional_extra_import_is_allowed(tmp_path: Path) -> None:
    src = _src(tmp_path, a="import py2js\n")
    text = _pyproject([], optional={"transpiler": ["py2js>=1.2.1"]})
    assert cpi.violations(src, text, LOCK) == []


def test_extras_and_case_do_not_hide_a_pin(tmp_path: Path) -> None:
    src = _src(tmp_path, a="import httpx\n")
    assert cpi.violations(src, _pyproject(["HTTPX[http2]==0.28.1"]), LOCK) == []
