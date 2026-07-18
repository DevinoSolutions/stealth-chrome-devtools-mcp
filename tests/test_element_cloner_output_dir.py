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

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

EMBEDDED_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "stealth_chrome_devtools_mcp"
    / "embedded"
)


@pytest.fixture(autouse=True)
def _isolate_imports():
    """Remove cached module so each test gets a fresh import."""
    mod_name = "stealth_chrome_devtools_mcp.embedded.file_based_element_cloner"
    had = mod_name in sys.modules
    old = sys.modules.pop(mod_name, None)
    yield
    sys.modules.pop(mod_name, None)
    if had and old is not None:
        sys.modules[mod_name] = old


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
