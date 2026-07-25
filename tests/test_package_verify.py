"""Hermetic pins for the W3 artifact contract (plan_RELEASE §2.3, gap G-C).

``tools/package_verify.py`` is the ONE home for "is this distribution the one we
built, and does it carry what it must". These tests build synthetic wheels and
sdists in a tmp dir — no network, no real ``uv build`` — so every rule can be
proven to BITE, not just to pass. The same negative controls run as in-job bite
proofs in CI against a throwaway copy of the real artifact; this file is what
makes them cheap to keep honest.
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import package_verify as pv  # noqa: E402  PERMANENT(tools/ is not an importable package; the sys.path line above must run first)

VERSION = "1.2.0"
DIST_STEM = f"stealth_chrome_devtools_mcp-{VERSION}"


def _js_body(name: str) -> str:
    return f"// {name}\nexport const marker = '{name}';\n"


def _metadata(version: str = VERSION, name: str = pv.DIST_NAME) -> str:
    return (
        "Metadata-Version: 2.3\n"
        f"Name: {name}\n"
        f"Version: {version}\n"
        "Summary: synthetic fixture\n"
        "\n"
        "body\n"
    )


def _entry_points() -> str:
    return (
        "[console_scripts]\n"
        "stealth-chrome-devtools = stealth_chrome_devtools_mcp.cli:main\n"
        "stealth-chrome-devtools-mcp = stealth_chrome_devtools_mcp.server:main\n"
    )


def make_wheel(
    path: Path,
    *,
    version: str = VERSION,
    metadata_version: str | None = None,
    js: dict[str, str] | None = None,
    omit_modules: tuple[str, ...] = (),
    entry_points: str | None = None,
    extra: dict[str, str] | None = None,
) -> Path:
    js = js if js is not None else {n: _js_body(n) for n in pv.EXPECTED_JS}
    dist_info = f"stealth_chrome_devtools_mcp-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as zf:
        for module in pv.EXPECTED_MODULES:
            if module in omit_modules:
                continue
            zf.writestr(f"{pv.PKG}/{module}", f"# {module}\n")
        for name, body in js.items():
            zf.writestr(f"{pv.PKG}/embedded/js/{name}", body)
        zf.writestr(f"{dist_info}/METADATA", _metadata(metadata_version or version))
        zf.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\n")
        if entry_points is not None:
            zf.writestr(f"{dist_info}/entry_points.txt", entry_points)
        else:
            zf.writestr(f"{dist_info}/entry_points.txt", _entry_points())
        for member, body in (extra or {}).items():
            zf.writestr(member, body)
    return path


def make_sdist(
    path: Path,
    *,
    version: str = VERSION,
    pkg_info_version: str | None = None,
    js: dict[str, str] | None = None,
    omit_files: tuple[str, ...] = (),
) -> Path:
    js = js if js is not None else {n: _js_body(n) for n in pv.EXPECTED_JS}
    root = f"stealth_chrome_devtools_mcp-{version}"
    files: dict[str, str] = {
        "PKG-INFO": _metadata(pkg_info_version or version),
        "pyproject.toml": "[project]\nname = 'x'\n",
        "README.md": "# readme\n",
    }
    for name, body in js.items():
        files[f"src/{pv.PKG}/embedded/js/{name}"] = body
    with tarfile.open(path, "w:gz") as tf:
        for rel, body in files.items():
            if rel in omit_files:
                continue
            data = body.encode("utf-8")
            info = tarfile.TarInfo(f"{root}/{rel}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def make_dist(tmp_path: Path, **kwargs: object) -> Path:
    """A dist/ dir holding one synthetic wheel + one synthetic sdist."""
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    wheel_kwargs = {
        k[len("wheel_") :]: v for k, v in kwargs.items() if k.startswith("wheel_")
    }
    sdist_kwargs = {
        k[len("sdist_") :]: v for k, v in kwargs.items() if k.startswith("sdist_")
    }
    version = str(kwargs.get("version", VERSION))
    make_wheel(
        dist / f"{DIST_STEM.replace(VERSION, version)}-py3-none-any.whl",
        version=version,
        **wheel_kwargs,
    )
    make_sdist(
        dist / f"{DIST_STEM.replace(VERSION, version)}.tar.gz",
        version=version,
        **sdist_kwargs,
    )
    return dist


@pytest.fixture
def good_dist(tmp_path: Path) -> Path:
    return make_dist(tmp_path)


@pytest.fixture
def good_manifest(good_dist: Path) -> dict:
    return pv.build_manifest(good_dist)


# ---------------------------------------------------------------------------
# The contract holds on a well-formed pair.
# ---------------------------------------------------------------------------
def test_manifest_records_version_hashes_and_package_data(good_dist, good_manifest):
    assert good_manifest["version"] == VERSION
    assert good_manifest["schema_version"] == pv.MANIFEST_SCHEMA_VERSION
    kinds = {a["kind"] for a in good_manifest["artifacts"]}
    assert kinds == {"wheel", "sdist"}
    for entry in good_manifest["artifacts"]:
        assert len(entry["sha256"]) == 64
        assert entry["size"] == (good_dist / entry["filename"]).stat().st_size
    assert set(good_manifest["package_data"]) == {
        f"{pv.PKG}/embedded/js/{n}" for n in pv.EXPECTED_JS
    }


def test_verify_accepts_the_artifacts_it_described(good_dist, good_manifest):
    assert pv.verify_dist(good_dist, good_manifest) == []


# ---------------------------------------------------------------------------
# Bite proofs: each rule must reject something.
# ---------------------------------------------------------------------------
def test_flipped_byte_is_rejected_by_hash(tmp_path, good_dist, good_manifest):
    """The publish-path precondition: mutated bytes never pass as the built ones."""
    wheel = next(good_dist.glob("*.whl"))
    corrupt = tmp_path / "corrupt" / wheel.name
    corrupt.parent.mkdir()
    data = bytearray(wheel.read_bytes())
    data[-1] ^= 0xFF
    corrupt.write_bytes(bytes(data))

    problems = pv.check_artifact_hash(corrupt, good_manifest)
    assert problems, "a flipped byte must fail the hash check"
    assert any("sha256" in p for p in problems)
    # The original is untouched — a bite proof never damages the real artifact.
    assert pv.check_artifact_hash(wheel, good_manifest) == []


def test_removing_package_data_is_rejected_at_manifest_time(tmp_path):
    js = {n: _js_body(n) for n in pv.EXPECTED_JS if n != "extract_styles.js"}
    dist = make_dist(tmp_path, wheel_js=js)
    with pytest.raises(pv.VerificationError, match="missing package data"):
        pv.build_manifest(dist)


def test_removing_package_data_is_rejected_by_verify(tmp_path, good_manifest):
    """Even with matching hashes elsewhere, a missing member is a violation."""
    js = {n: _js_body(n) for n in pv.EXPECTED_JS if n != "extract_events.js"}
    dist = make_dist(tmp_path, wheel_js=js, sdist_js=js)
    problems = pv.verify_dist(dist, good_manifest)
    assert any("extract_events.js" in p for p in problems), problems


def test_wheel_and_sdist_package_data_must_agree(tmp_path):
    sdist_js = {n: _js_body(n) for n in pv.EXPECTED_JS}
    sdist_js["extract_assets.js"] = "// tampered\n"
    dist = make_dist(tmp_path, sdist_js=sdist_js)
    manifest = pv.build_manifest(dist)
    problems = pv.verify_dist(dist, manifest)
    assert any("disagree" in p for p in problems), problems


def test_missing_module_is_rejected(tmp_path):
    dist = make_dist(tmp_path, wheel_omit_modules=("embedded/cdp_element_cloner.py",))
    manifest = pv.build_manifest(dist)
    problems = pv.verify_dist(dist, manifest)
    assert any("cdp_element_cloner.py" in p for p in problems), problems


def test_missing_entry_point_is_rejected(tmp_path):
    only_one = (
        "[console_scripts]\n"
        "stealth-chrome-devtools = stealth_chrome_devtools_mcp.cli:main\n"
    )
    dist = make_dist(tmp_path, wheel_entry_points=only_one)
    manifest = pv.build_manifest(dist)
    problems = pv.verify_dist(dist, manifest)
    assert any("stealth-chrome-devtools-mcp" in p for p in problems), problems


def test_metadata_version_disagreeing_with_filename_is_rejected(tmp_path):
    dist = make_dist(tmp_path, wheel_metadata_version="9.9.9")
    manifest = pv.build_manifest(dist)
    # build_manifest reads the version FROM metadata, so the filename disagrees.
    problems = pv.verify_dist(dist, manifest)
    assert any("filename version" in p for p in problems), problems


def test_stray_js_outside_the_package_is_rejected(tmp_path):
    dist = make_dist(tmp_path, wheel_extra={"data/extract_styles.js": "// dup\n"})
    manifest = pv.build_manifest(dist)
    problems = pv.verify_dist(dist, manifest)
    assert any("outside" in p for p in problems), problems


def test_sdist_missing_package_data_is_rejected(tmp_path):
    dist = make_dist(
        tmp_path, sdist_omit_files=(f"src/{pv.PKG}/embedded/js/extract_structure.js",)
    )
    manifest = pv.build_manifest(dist)
    problems = pv.verify_dist(dist, manifest)
    assert any("extract_structure.js" in p for p in problems), problems


def test_two_wheels_in_dist_is_rejected(tmp_path, good_dist):
    make_wheel(good_dist / "stealth_chrome_devtools_mcp-1.2.0-py2-none-any.whl")
    with pytest.raises(pv.VerificationError, match="exactly 1 wheel"):
        pv.find_artifacts(good_dist)


# ---------------------------------------------------------------------------
# Manifest loading + the tag precondition.
# ---------------------------------------------------------------------------
def test_manifest_schema_version_is_enforced(tmp_path, good_manifest):
    path = tmp_path / "m.json"
    good_manifest["schema_version"] = 99
    path.write_text(json.dumps(good_manifest), encoding="utf-8")
    with pytest.raises(pv.VerificationError, match="schema_version"):
        pv.load_manifest(path)


@pytest.mark.parametrize("tag", ["v1.2.0", "1.2.0", "refs/tags/v1.2.0"])
def test_matching_tag_forms_are_accepted(good_manifest, tag):
    assert pv.assert_tag_version(good_manifest, tag) == []


@pytest.mark.parametrize("tag", ["v1.2.1", "v0.9.0", "refs/tags/v2.0.0"])
def test_mismatched_tag_blocks_publication(good_manifest, tag):
    problems = pv.assert_tag_version(good_manifest, tag)
    assert problems and "disagree" in problems[0]


def test_unknown_artifact_filename_is_rejected(tmp_path, good_manifest):
    stranger = tmp_path / "stealth_chrome_devtools_mcp-9.9.9-py3-none-any.whl"
    make_wheel(stranger, version="9.9.9")
    problems = pv.check_artifact_hash(stranger, good_manifest)
    assert problems and "not in the manifest" in problems[0]


# ---------------------------------------------------------------------------
# CLI exit codes (what the workflow steps actually depend on).
# ---------------------------------------------------------------------------
def test_cli_manifest_then_verify_round_trips(tmp_path, good_dist):
    manifest_path = tmp_path / "out" / "release-manifest.json"
    assert (
        pv.main(["manifest", "--dist", str(good_dist), "--out", str(manifest_path)])
        == 0
    )
    evidence = tmp_path / "out" / "package-verify.json"
    assert (
        pv.main(
            [
                "verify",
                "--dist",
                str(good_dist),
                "--manifest",
                str(manifest_path),
                "--expect-version",
                f"v{VERSION}",
                "--out",
                str(evidence),
            ]
        )
        == 0
    )
    record = json.loads(evidence.read_text(encoding="utf-8"))
    assert record["verified"] is True
    assert record["violations"] == []


def test_cli_hash_check_rejects_a_corrupted_copy(tmp_path, good_dist):
    manifest_path = tmp_path / "release-manifest.json"
    assert (
        pv.main(["manifest", "--dist", str(good_dist), "--out", str(manifest_path)])
        == 0
    )
    wheel = next(good_dist.glob("*.whl"))
    copy = tmp_path / wheel.name
    copy.write_bytes(wheel.read_bytes() + b"\x00")
    assert (
        pv.main(
            [
                "hash-check",
                "--artifact",
                str(copy),
                "--manifest",
                str(manifest_path),
            ]
        )
        == 1
    )


def test_cli_assert_version_blocks_a_wrong_tag(tmp_path, good_dist):
    manifest_path = tmp_path / "release-manifest.json"
    pv.main(["manifest", "--dist", str(good_dist), "--out", str(manifest_path)])
    assert (
        pv.main(["assert-version", "--manifest", str(manifest_path), "--tag", "v9.9.9"])
        == 1
    )
    assert (
        pv.main(
            ["assert-version", "--manifest", str(manifest_path), "--tag", f"v{VERSION}"]
        )
        == 0
    )
