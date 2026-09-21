"""Regression tests for FileBasedElementCloner output directory resolution.

Covers GitHub issue #5: when the server is launched by an MCP client (e.g.
Claude Desktop), CWD may be a non-writable system path, so the default output
dir must not resolve to CWD. The original fix anchored it to the *package root*
— but that is itself unsafe on a real install (``site-packages`` is frequently
read-only). The default now resolves to a stable, writable, per-user location
(``~/.stealth-mcp/element_clones``, overridable via ``STEALTH_MCP_CLONE_OUTPUT_DIR``),
which is independent of both CWD and the install location. An explicit relative
path is still anchored to the package for backward compatibility.
"""

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import module_cache

MODULE_NAME = "stealth_chrome_devtools_mcp.embedded.file_based_element_cloner"

EMBEDDED_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "stealth_chrome_devtools_mcp"
    / "embedded"
)


@pytest.fixture(autouse=True)
def _isolate_imports():
    """Remove cached module so each test gets a fresh import.

    The restore is ``tests/module_cache.py``'s because a module's identity
    lives in TWO places and this fixture used to move one of them: it popped
    ``sys.modules[name]``, the test re-imported the module — which re-binds the
    parent package's ``file_based_element_cloner`` attribute to the NEW object —
    and then it put the OLD object back in the mapping alone. The parent named a
    module the cache did not, for the rest of the session.

    That is not local to this file. It needs a file sorting BEFORE this one to
    have cached the module already (``tests/test_clone_output_dir.py`` does), so
    it is invisible in every single-file run and in every pair, and it surfaced
    only in the full alphabetical lane, at
    ``tests/test_package_entrypoints.py``'s import-tree pin — which is where a
    dotted ``monkeypatch.setattr`` would otherwise have found it, in whichever
    file happened to walk that attribute next.
    """
    with module_cache.absent(MODULE_NAME):
        yield


def _import_cloner_class():
    from stealth_chrome_devtools_mcp.embedded.file_based_element_cloner import (
        FileBasedElementCloner,
    )

    return FileBasedElementCloner


class TestOutputDirResolution:
    """Ensure the cloner resolves its output dir to a safe, writable location."""

    def test_default_dir_is_per_user_not_package_or_cwd(self, tmp_path):
        """Issue #5, correctly resolved: the default output dir must be a
        stable, writable, per-user location — never CWD (may be a read-only
        system path) and never inside the installed package (``site-packages``
        is frequently read-only on real installs)."""
        FileBasedElementCloner = _import_cloner_class()
        from stealth_chrome_devtools_mcp.embedded.response_handler import (
            default_clone_output_dir,
        )

        with patch.object(Path, "mkdir"):
            cloner = FileBasedElementCloner()

        package_root = Path(EMBEDDED_DIR).resolve().parent
        assert cloner.output_dir == default_clone_output_dir()
        assert package_root not in cloner.output_dir.resolve().parents
        assert (
            cloner.output_dir.resolve() != (package_root / "element_clones").resolve()
        )

    def test_relative_dir_does_not_use_cwd(self, tmp_path):
        """Even from a weird CWD the output dir must NOT land there."""
        FileBasedElementCloner = _import_cloner_class()

        original_cwd = os.getcwd()
        try:
            os.chdir(str(tmp_path))
            with patch.object(Path, "mkdir"):
                cloner = FileBasedElementCloner("my_clones")
            assert tmp_path not in cloner.output_dir.parents
        finally:
            os.chdir(original_cwd)

    def test_absolute_dir_is_used_as_is(self, tmp_path):
        """An explicit absolute path must be honoured verbatim."""
        FileBasedElementCloner = _import_cloner_class()
        abs_dir = tmp_path / "custom_clones"

        with patch.object(Path, "mkdir"):
            cloner = FileBasedElementCloner(str(abs_dir))

        assert cloner.output_dir == abs_dir

    def test_mkdir_is_called(self, tmp_path):
        """The output directory must be created on init."""
        FileBasedElementCloner = _import_cloner_class()
        target = tmp_path / "clone_out"

        cloner = FileBasedElementCloner(str(target))

        assert target.is_dir()

    def test_non_writable_cwd_does_not_crash(self, tmp_path):
        """Simulates the MCP client scenario: CWD is non-writable."""
        FileBasedElementCloner = _import_cloner_class()

        read_only = tmp_path / "read_only_dir"
        read_only.mkdir()

        original_cwd = os.getcwd()
        try:
            os.chdir(str(read_only))
            cloner = FileBasedElementCloner()
            assert cloner.output_dir.is_dir()
        finally:
            os.chdir(original_cwd)


class TestTheIsolationLeavesTheImportTreeIntact:
    """The fixture above must hand the package back exactly as it found it.

    This is the RED half of the defect it pins: with the pop-only restore this
    file shipped with, the last two assertions fail — ``sys.modules`` holds the
    original module while ``embedded.file_based_element_cloner`` names the copy
    the body imported. It is pinned HERE, beside the fixture that does it,
    rather than only at the whole-tree invariant in
    ``tests/test_package_entrypoints.py``: that one sorts after this file and
    catches the same bug, but it names the symptom, and the file that has to
    change is this one.
    """

    def test_isolating_the_module_restores_its_parent_binding(self):
        parent = importlib.import_module("stealth_chrome_devtools_mcp.embedded")
        before = importlib.import_module(MODULE_NAME)

        with module_cache.absent(MODULE_NAME):
            during = importlib.import_module(MODULE_NAME)
            assert during is not before, (
                "the block must get a FRESH module — that is what the fixture "
                "exists for"
            )
            assert parent.file_based_element_cloner is during

        assert sys.modules[MODULE_NAME] is before
        assert parent.file_based_element_cloner is before, (
            "the parent package still names the copy imported inside the "
            "block: restoring sys.modules alone moves one half of a module's "
            "identity (see tests/module_cache.py)"
        )
