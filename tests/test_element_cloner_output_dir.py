"""Regression tests for FileBasedElementCloner output directory resolution.

Covers the fix for GitHub issue #5: when the server is launched by an MCP
client (e.g. Claude Desktop), CWD may be a non-writable system path.  The
cloner must anchor its default relative ``element_clones`` directory to the
package root, not to the working directory.
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
    mod_name = "file_based_element_cloner"
    had = mod_name in sys.modules
    old = sys.modules.pop(mod_name, None)
    yield
    sys.modules.pop(mod_name, None)
    if had and old is not None:
        sys.modules[mod_name] = old


def _import_cloner_class():
    if str(EMBEDDED_DIR) not in sys.path:
        sys.path.insert(0, str(EMBEDDED_DIR))
    from file_based_element_cloner import FileBasedElementCloner
    return FileBasedElementCloner


class TestOutputDirResolution:
    """Ensure the cloner resolves its output dir relative to the package."""

    def test_default_relative_dir_anchors_to_package_root(self, tmp_path):
        """Default 'element_clones' must resolve under the package, not CWD."""
        FileBasedElementCloner = _import_cloner_class()

        with patch.object(Path, "mkdir"):
            cloner = FileBasedElementCloner()

        package_root = Path(EMBEDDED_DIR).resolve().parent
        expected = package_root / "element_clones"
        assert cloner.output_dir == expected

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
