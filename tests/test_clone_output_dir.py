"""The default output dir for clone / large-response artifacts must live in a
per-user location, NEVER inside the installed package.

Regression: both ``ResponseHandler`` and ``FileBasedElementCloner`` defaulted
their output dir to ``Path(__file__).parent.parent / "element_clones"`` — i.e.
*inside the package* (``site-packages`` on a real install). Consequences:

  * read-only site-packages (system Python, containers, ``pip install --user``)
    -> the first screenshot / large response raises ``PermissionError``;
  * writable site-packages -> the install is silently polluted with artifacts
    that survive ``pip uninstall``.

The default must instead follow the project's existing ``~/.stealth-mcp``
state-dir convention and honor a ``STEALTH_MCP_*`` override, exactly like the
singleton state dir and the browser session root.

Pure filesystem tests: no browser. Constructor tests redirect the output dir via
the env override so they never touch the real home directory.
"""

from pathlib import Path

import response_handler as rh_mod
from file_based_element_cloner import FileBasedElementCloner
from response_handler import ResponseHandler, default_clone_output_dir

# .../src/stealth_chrome_devtools_mcp/embedded
PACKAGE_EMBEDDED_DIR = Path(rh_mod.__file__).resolve().parent
# .../src/stealth_chrome_devtools_mcp  (the importable package root)
PACKAGE_ROOT = PACKAGE_EMBEDDED_DIR.parent
ENV_VAR = "STEALTH_MCP_CLONE_OUTPUT_DIR"


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


class TestDefaultCloneOutputDir:
    def test_defaults_to_user_state_dir(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        assert (
            default_clone_output_dir()
            == Path.home() / ".stealth-mcp" / "element_clones"
        )

    def test_env_override_is_honored(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_VAR, str(tmp_path / "out"))
        assert default_clone_output_dir() == tmp_path / "out"

    def test_env_override_expands_user(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "~/custom-clones")
        assert default_clone_output_dir() == Path.home() / "custom-clones"

    def test_blank_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "   ")
        assert (
            default_clone_output_dir()
            == Path.home() / ".stealth-mcp" / "element_clones"
        )

    def test_default_is_never_inside_the_package(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        result = default_clone_output_dir()
        assert not _is_within(result, PACKAGE_ROOT), (
            "clone output must never land inside the installed package"
        )

    def test_helper_does_not_create_the_directory(self, monkeypatch, tmp_path):
        # Pure path computation — creation is the constructor's job, so merely
        # asking where the dir *is* must not touch the filesystem.
        target = tmp_path / "should-not-exist"
        monkeypatch.setenv(ENV_VAR, str(target))
        default_clone_output_dir()
        assert not target.exists()


class TestResponseHandlerDefault:
    def test_default_clone_dir_is_user_dir_not_package(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_VAR, str(tmp_path / "rh"))
        h = ResponseHandler()
        assert h.clone_dir == tmp_path / "rh"
        assert not _is_within(h.clone_dir, PACKAGE_ROOT)
        assert h.clone_dir.exists()

    def test_explicit_clone_dir_still_respected(self, tmp_path):
        target = tmp_path / "explicit"
        h = ResponseHandler(clone_dir=str(target))
        assert h.clone_dir == target

    def test_default_creates_nested_parents(self, monkeypatch, tmp_path):
        # The default lives under a parent (~/.stealth-mcp) that may not exist
        # yet, so mkdir must pass parents=True — the old exist_ok-only call would
        # raise FileNotFoundError on a fresh machine.
        nested = tmp_path / "brand" / "new" / "element_clones"
        monkeypatch.setenv(ENV_VAR, str(nested))
        ResponseHandler()
        assert nested.exists()


class TestFileBasedElementClonerDefault:
    def test_default_output_dir_is_user_dir_not_package(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_VAR, str(tmp_path / "fbc"))
        c = FileBasedElementCloner()
        assert c.output_dir == tmp_path / "fbc"
        assert not _is_within(c.output_dir, PACKAGE_ROOT)
        assert c.output_dir.exists()

    def test_explicit_absolute_output_dir_respected(self, tmp_path):
        target = tmp_path / "abs-out"
        c = FileBasedElementCloner(output_dir=str(target))
        assert c.output_dir == target
