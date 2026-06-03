"""Guards the wheel build — the uvx/pip install path that `uv run` and the rest
of the suite bypass.

Regression test for a hatchling `force-include` that duplicated package files
already covered by `packages`, breaking `build_wheel` with a duplicate-file
error. That made `uvx --from git+...` fail to start the server (JSON-RPC -32000)
even though every source-run test still passed.
"""
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_wheel_builds_and_bundles_runtime_js(tmp_path):
    """The wheel must build cleanly and still contain the embedded js assets."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not available to build the wheel")

    result = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=str(ROOT),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, (
        "wheel build failed — this is exactly what breaks `uvx`/`pip install`:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"

    names = zipfile.ZipFile(wheels[0]).namelist()
    js_assets = [n for n in names if n.endswith(".js")]
    assert any(
        n.endswith("embedded/js/comprehensive_element_extractor.js") for n in js_assets
    ), f"runtime js assets missing from the wheel: {js_assets}"
