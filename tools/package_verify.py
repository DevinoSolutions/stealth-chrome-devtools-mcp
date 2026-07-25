#!/usr/bin/env python3
"""The ONE home for distribution-artifact facts (plan_RELEASE W3, gap G-C).

Every job that needs to know something about the built distribution asks this
tool — ``build-dist`` (write the manifest), ``package-verify`` (re-check it),
``install-smoke`` (hash-check the cell's own download), and ``publish.yml``
(re-check + tag/version precondition). There is deliberately no second copy of
these rules in YAML: a check that lives in one workflow's shell block cannot be
unit-tested and cannot be reused by the publish path.

Subcommands
-----------
``manifest``      hash + describe ``dist/`` once, right after the single build.
``verify``        re-check a ``dist/`` against a manifest (hashes, metadata,
                  version agreement, wheel/sdist membership, package data).
``hash-check``    re-check ONE artifact file against the manifest (the cheap
                  precondition an install cell or the publish job runs).
``assert-version`` manifest version == a release tag (``v1.2.0`` or ``1.2.0``).

Package data
------------
``embedded/js/*.js`` are real package data: the cloner engine reads them at
runtime, so a wheel that omits them installs and imports fine and then fails on
first use. ``manifest`` records the sha256 of every one of them, ``verify``
proves the wheel and the sdist carry BYTE-IDENTICAL copies, and
``tools/install_smoke.py`` proves the files that land in a fresh site-packages
are those same bytes. Comparing the two artifacts to each other (rather than to
the git checkout) keeps the check hermetic — no line-ending policy of a
particular runner's checkout can make it lie.

Stdlib only. Exit 0 == every checked property holds; exit 1 == at least one
violation, each printed as a GitHub error annotation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

MANIFEST_SCHEMA_VERSION = 1
DIST_NAME = "stealth-chrome-devtools-mcp"
PKG = "stealth_chrome_devtools_mcp"

# The browser-side extraction scripts (CLAUDE.md: the cloner subsystem's `js/`).
EXPECTED_JS: tuple[str, ...] = (
    "comprehensive_element_extractor.js",
    "extract_animations.js",
    "extract_assets.js",
    "extract_events.js",
    "extract_related_files.js",
    "extract_structure.js",
    "extract_styles.js",
)

# Python modules whose absence would mean a structurally broken wheel.
EXPECTED_MODULES: tuple[str, ...] = (
    "__init__.py",
    "server.py",
    "cli.py",
    "settings.py",
    "embedded/server.py",
    "embedded/singleton.py",
    "embedded/cdp_element_cloner.py",
)

# Both console scripts (pyproject [project.scripts]). install-smoke resolves the
# first one out of a fresh environment, so a missing entry point must be caught
# here rather than as a confusing "launcher not found" three jobs later.
EXPECTED_ENTRY_POINTS: tuple[str, ...] = (
    "stealth-chrome-devtools-mcp",
    "stealth-chrome-devtools",
)

_CHUNK = 1024 * 1024
# "<name>-<version>[-<build/python/abi/platform>…]" — anything shorter than
# name+version cannot be a distribution filename.
_MIN_FILENAME_PARTS = 2


class VerificationError(Exception):
    """A checked property does not hold. Message is the human-readable reason."""


# ---------------------------------------------------------------------------
# Hashing + artifact discovery.
# ---------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize(name: str) -> str:
    """PEP 503 normalization, so ``foo_bar`` and ``Foo-Bar`` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def find_artifacts(dist_dir: Path) -> tuple[Path, Path]:
    """Return ``(wheel, sdist)``; exactly one of each must be present."""
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1:
        raise VerificationError(
            f"expected exactly 1 wheel in {dist_dir}, found {len(wheels)}: "
            f"{[w.name for w in wheels]}"
        )
    if len(sdists) != 1:
        raise VerificationError(
            f"expected exactly 1 sdist in {dist_dir}, found {len(sdists)}: "
            f"{[s.name for s in sdists]}"
        )
    return wheels[0], sdists[0]


# ---------------------------------------------------------------------------
# Reading inside the artifacts.
# ---------------------------------------------------------------------------
def wheel_members(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as zf:
        return zf.namelist()


def sdist_members(sdist: Path) -> list[str]:
    with tarfile.open(sdist, "r:gz") as tf:
        return tf.getnames()


def _read_wheel(wheel: Path, member: str) -> bytes:
    with zipfile.ZipFile(wheel) as zf:
        return zf.read(member)


def _read_sdist(sdist: Path, member: str) -> bytes:
    with tarfile.open(sdist, "r:gz") as tf:
        extracted = tf.extractfile(member)
        if extracted is None:
            raise VerificationError(f"sdist member is not a regular file: {member}")
        with extracted:
            return extracted.read()


def _metadata_fields(text: str) -> tuple[str, str]:
    """``(Name, Version)`` from a core-metadata document (METADATA / PKG-INFO)."""
    message = Parser().parsestr(text)
    name = message.get("Name") or ""
    version = message.get("Version") or ""
    if not name or not version:
        raise VerificationError(
            f"core metadata is missing Name/Version (name={name!r} version={version!r})"
        )
    return name, version


def _dist_info_dir(members: list[str]) -> str:
    dirs = {
        m.split("/", 1)[0] for m in members if m.split("/", 1)[0].endswith(".dist-info")
    }
    if len(dirs) != 1:
        raise VerificationError(
            f"wheel must contain exactly one .dist-info directory, found {sorted(dirs)}"
        )
    return next(iter(dirs))


def _sdist_root(members: list[str]) -> str:
    roots = {m.split("/", 1)[0] for m in members if "/" in m}
    if len(roots) != 1:
        raise VerificationError(
            f"sdist must contain exactly one top-level directory, found {sorted(roots)}"
        )
    return next(iter(roots))


def _version_from_filename(filename: str, *, wheel: bool) -> str:
    """The version segment of a PEP 427 wheel / PEP 625 sdist filename."""
    stem = filename[: -len(".whl")] if wheel else filename[: -len(".tar.gz")]
    parts = stem.split("-")
    if len(parts) < _MIN_FILENAME_PARTS:
        raise VerificationError(f"cannot parse a version out of {filename!r}")
    return parts[1]


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------
def build_manifest(dist_dir: Path) -> dict[str, object]:
    """Describe ``dist/`` once: hashes, sizes, version, and package-data hashes.

    The package-data hashes come from the WHEEL (the artifact whose layout is
    what actually lands in site-packages); ``verify`` then proves the sdist
    agrees, and ``install_smoke`` proves site-packages agrees.
    """
    wheel, sdist = find_artifacts(dist_dir)
    members = wheel_members(wheel)
    dist_info = _dist_info_dir(members)
    name, version = _metadata_fields(
        _read_wheel(wheel, f"{dist_info}/METADATA").decode("utf-8")
    )

    package_data: dict[str, str] = {}
    with zipfile.ZipFile(wheel) as zf:
        for js in EXPECTED_JS:
            member = f"{PKG}/embedded/js/{js}"
            if member not in members:
                raise VerificationError(f"wheel is missing package data: {member}")
            package_data[member] = hashlib.sha256(zf.read(member)).hexdigest()

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "name": name,
        "version": version,
        "artifacts": [
            {
                "filename": path.name,
                "kind": kind,
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
            for path, kind in ((wheel, "wheel"), (sdist, "sdist"))
        ],
        "package_data": package_data,
    }


def load_manifest(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise VerificationError(f"manifest {path} is not a JSON object")
    if data.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise VerificationError(
            f"manifest schema_version {data.get('schema_version')!r} "
            f"!= expected {MANIFEST_SCHEMA_VERSION}"
        )
    for key in ("name", "version", "artifacts", "package_data"):
        if key not in data:
            raise VerificationError(f"manifest {path} is missing {key!r}")
    return data


def manifest_entry(manifest: dict[str, object], filename: str) -> dict[str, object]:
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, list):
        raise VerificationError("manifest 'artifacts' is not a list")
    for entry in artifacts:
        if isinstance(entry, dict) and entry.get("filename") == filename:
            return entry
    raise VerificationError(
        f"{filename!r} is not in the manifest (manifest lists "
        f"{[e.get('filename') for e in artifacts if isinstance(e, dict)]})"
    )


def check_artifact_hash(artifact: Path, manifest: dict[str, object]) -> list[str]:
    """Violations for ONE artifact file against its manifest entry."""
    problems: list[str] = []
    try:
        entry = manifest_entry(manifest, artifact.name)
    except VerificationError as exc:
        return [str(exc)]
    actual = sha256_file(artifact)
    if actual != entry.get("sha256"):
        problems.append(
            f"{artifact.name}: sha256 {actual} != manifest {entry.get('sha256')}"
        )
    actual_size = artifact.stat().st_size
    if actual_size != entry.get("size"):
        problems.append(
            f"{artifact.name}: size {actual_size} != manifest {entry.get('size')}"
        )
    return problems


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------
def verify_dist(dist_dir: Path, manifest: dict[str, object]) -> list[str]:
    """Every violation found in ``dist_dir`` relative to ``manifest`` (empty == ok).

    Collects rather than raises: one run should report every problem, not just
    the first, so a broken build costs one CI round instead of five.
    """
    problems: list[str] = []
    try:
        wheel, sdist = find_artifacts(dist_dir)
    except VerificationError as exc:
        return [str(exc)]

    version = str(manifest["version"])
    manifest_files = {
        e.get("filename") for e in manifest["artifacts"] if isinstance(e, dict)
    }
    if manifest_files != {wheel.name, sdist.name}:
        problems.append(
            f"dist contents {sorted({wheel.name, sdist.name})} != manifest "
            f"{sorted(str(f) for f in manifest_files)}"
        )

    # 1. Byte identity with what was built.
    problems.extend(check_artifact_hash(wheel, manifest))
    problems.extend(check_artifact_hash(sdist, manifest))

    # 2. Name/version agreement across manifest, both filenames, and both
    #    core-metadata documents. A single disagreeing source is a red gate.
    problems.extend(_check_identity(wheel, sdist, manifest, version))

    # 3. Structural membership + package data.
    problems.extend(_check_wheel_members(wheel))
    problems.extend(_check_sdist_members(sdist))
    problems.extend(_check_package_data(wheel, sdist, manifest))
    return problems


def _check_identity(
    wheel: Path, sdist: Path, manifest: dict[str, object], version: str
) -> list[str]:
    problems: list[str] = []
    if _normalize(str(manifest["name"])) != _normalize(DIST_NAME):
        problems.append(f"manifest name {manifest['name']!r} != expected {DIST_NAME!r}")
    for path, is_wheel in ((wheel, True), (sdist, False)):
        try:
            filename_version = _version_from_filename(path.name, wheel=is_wheel)
        except VerificationError as exc:
            problems.append(str(exc))
            continue
        if filename_version != version:
            problems.append(
                f"{path.name}: filename version {filename_version!r} "
                f"!= manifest version {version!r}"
            )
    try:
        members = wheel_members(wheel)
        w_name, w_version = _metadata_fields(
            _read_wheel(wheel, f"{_dist_info_dir(members)}/METADATA").decode("utf-8")
        )
        if _normalize(w_name) != _normalize(DIST_NAME):
            problems.append(f"wheel METADATA Name {w_name!r} != {DIST_NAME!r}")
        if w_version != version:
            problems.append(
                f"wheel METADATA Version {w_version!r} != manifest {version!r}"
            )
    except (VerificationError, KeyError) as exc:
        problems.append(f"wheel METADATA unreadable: {exc}")
    try:
        s_members = sdist_members(sdist)
        s_name, s_version = _metadata_fields(
            _read_sdist(sdist, f"{_sdist_root(s_members)}/PKG-INFO").decode("utf-8")
        )
        if _normalize(s_name) != _normalize(DIST_NAME):
            problems.append(f"sdist PKG-INFO Name {s_name!r} != {DIST_NAME!r}")
        if s_version != version:
            problems.append(
                f"sdist PKG-INFO Version {s_version!r} != manifest {version!r}"
            )
    except (VerificationError, KeyError) as exc:
        problems.append(f"sdist PKG-INFO unreadable: {exc}")
    return problems


def _check_wheel_members(wheel: Path) -> list[str]:
    problems: list[str] = []
    try:
        members = wheel_members(wheel)
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"wheel is unreadable: {exc}"]
    member_set = set(members)
    problems.extend(
        f"wheel is missing module: {PKG}/{module}"
        for module in EXPECTED_MODULES
        if f"{PKG}/{module}" not in member_set
    )
    problems.extend(
        f"wheel is missing package data: {PKG}/embedded/js/{js}"
        for js in EXPECTED_JS
        if f"{PKG}/embedded/js/{js}" not in member_set
    )
    # No stray copy of the js scripts outside the package (the duplicate-file
    # trap pyproject.toml warns about — a force-include would land them twice).
    stray = sorted(
        m
        for m in member_set
        if m.endswith(".js") and not m.startswith(f"{PKG}/embedded/js/")
    )
    if stray:
        problems.append(f"wheel carries .js outside {PKG}/embedded/js/: {stray}")
    try:
        dist_info = _dist_info_dir(members)
    except VerificationError as exc:
        return [*problems, str(exc)]
    entry_points_member = f"{dist_info}/entry_points.txt"
    if entry_points_member not in member_set:
        problems.append(f"wheel is missing {entry_points_member}")
    else:
        text = _read_wheel(wheel, entry_points_member).decode("utf-8")
        problems.extend(
            f"wheel entry_points.txt is missing {script!r}"
            for script in EXPECTED_ENTRY_POINTS
            if f"{script} =" not in text and f"{script}=" not in text
        )
    return problems


def _check_sdist_members(sdist: Path) -> list[str]:
    problems: list[str] = []
    try:
        members = sdist_members(sdist)
        root = _sdist_root(members)
    except (OSError, tarfile.TarError, VerificationError) as exc:
        return [f"sdist is unreadable: {exc}"]
    member_set = set(members)
    problems.extend(
        f"sdist is missing {required}"
        for required in ("pyproject.toml", "README.md")
        if f"{root}/{required}" not in member_set
    )
    problems.extend(
        f"sdist is missing package data: src/{PKG}/embedded/js/{js}"
        for js in EXPECTED_JS
        if f"{root}/src/{PKG}/embedded/js/{js}" not in member_set
    )
    return problems


def _check_package_data(
    wheel: Path, sdist: Path, manifest: dict[str, object]
) -> list[str]:
    """Wheel js == manifest js == sdist js, byte for byte."""
    problems: list[str] = []
    recorded = manifest["package_data"]
    if not isinstance(recorded, dict):
        return ["manifest 'package_data' is not an object"]
    expected_keys = {f"{PKG}/embedded/js/{js}" for js in EXPECTED_JS}
    if set(recorded) != expected_keys:
        problems.append(
            f"manifest package_data keys {sorted(recorded)} != expected "
            f"{sorted(expected_keys)}"
        )
    try:
        sdist_root = _sdist_root(sdist_members(sdist))
    except (OSError, tarfile.TarError, VerificationError) as exc:
        return [*problems, f"sdist is unreadable: {exc}"]

    wheel_names = set(wheel_members(wheel))
    for member, expected_hash in sorted(recorded.items()):
        if member not in wheel_names:
            problems.append(f"wheel is missing recorded package data: {member}")
            continue
        actual = hashlib.sha256(_read_wheel(wheel, member)).hexdigest()
        if actual != expected_hash:
            problems.append(
                f"{member}: wheel sha256 {actual} != manifest {expected_hash}"
            )
        js_name = member.rsplit("/", 1)[-1]
        sdist_member = f"{sdist_root}/src/{PKG}/embedded/js/{js_name}"
        try:
            sdist_hash = hashlib.sha256(_read_sdist(sdist, sdist_member)).hexdigest()
        except (KeyError, VerificationError) as exc:
            problems.append(f"sdist package data unreadable ({sdist_member}): {exc}")
            continue
        if sdist_hash != expected_hash:
            problems.append(
                f"{js_name}: sdist sha256 {sdist_hash} != wheel/manifest "
                f"{expected_hash} (the two publishable artifacts disagree)"
            )
    return problems


# ---------------------------------------------------------------------------
# assert-version
# ---------------------------------------------------------------------------
def assert_tag_version(manifest: dict[str, object], tag: str) -> list[str]:
    """``v1.2.0``/``refs/tags/v1.2.0``/``1.2.0`` must equal the built version."""
    bare = tag.removeprefix("refs/tags/").removeprefix("v")
    version = str(manifest["version"])
    if bare != version:
        return [
            f"release tag {tag!r} (version {bare!r}) != built artifact version "
            f"{version!r} — the tag and the distribution disagree"
        ]
    return []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _emit(problems: list[str], *, context: str) -> int:
    if problems:
        print(f"{context}: {len(problems)} violation(s)")
        for problem in problems:
            print(f"::error title={context}::{problem}", file=sys.stderr)
            print(f"  - {problem}")
        return 1
    print(f"{context}: OK")
    return 0


def _cmd_manifest(args: argparse.Namespace) -> int:
    manifest = build_manifest(args.dist)
    text = json.dumps(manifest, indent=2, sort_keys=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    problems = verify_dist(args.dist, manifest)
    if args.expect_version:
        problems.extend(assert_tag_version(manifest, args.expect_version))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "schema_version": MANIFEST_SCHEMA_VERSION,
                    "dist_dir": str(args.dist),
                    "version": manifest["version"],
                    "artifacts": manifest["artifacts"],
                    "violations": problems,
                    "verified": not problems,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return _emit(problems, context="package-verify")


def _cmd_hash_check(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    if not args.artifact.is_file():
        return _emit([f"artifact not found: {args.artifact}"], context="hash-check")
    return _emit(
        check_artifact_hash(args.artifact, manifest),
        context=f"hash-check {args.artifact.name}",
    )


def _cmd_assert_version(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    return _emit(assert_tag_version(manifest, args.tag), context="assert-version")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_manifest = sub.add_parser("manifest", help="hash dist/ and write the manifest")
    p_manifest.add_argument("--dist", type=Path, default=Path("dist"))
    p_manifest.add_argument("--out", type=Path, required=True)
    p_manifest.set_defaults(func=_cmd_manifest)

    p_verify = sub.add_parser("verify", help="re-check dist/ against a manifest")
    p_verify.add_argument("--dist", type=Path, default=Path("dist"))
    p_verify.add_argument("--manifest", type=Path, required=True)
    p_verify.add_argument("--expect-version", default="")
    p_verify.add_argument("--out", type=Path, default=None)
    p_verify.set_defaults(func=_cmd_verify)

    p_hash = sub.add_parser("hash-check", help="re-check one artifact file")
    p_hash.add_argument("--artifact", type=Path, required=True)
    p_hash.add_argument("--manifest", type=Path, required=True)
    p_hash.set_defaults(func=_cmd_hash_check)

    p_tag = sub.add_parser("assert-version", help="manifest version == release tag")
    p_tag.add_argument("--manifest", type=Path, required=True)
    p_tag.add_argument("--tag", required=True)
    p_tag.set_defaults(func=_cmd_assert_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except VerificationError as exc:
        return _emit([str(exc)], context=args.command)
    except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
        return _emit([f"{type(exc).__name__}: {exc}"], context=args.command)


if __name__ == "__main__":
    sys.exit(main())
