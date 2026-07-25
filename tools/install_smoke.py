#!/usr/bin/env python3
"""Install ONE built artifact into a fresh environment and drive W1's journey.

plan_RELEASE W3 (gap G-C). The gate's other lanes test the *source tree*; this
one tests the *files that will be uploaded to PyPI*. It:

1. re-checks the downloaded artifact's sha256 against the build manifest
   (``tools/package_verify.py hash-check`` — same code the publish job runs);
2. creates a fresh virtual environment and installs that artifact **by absolute
   path with caches disabled**, so nothing can be satisfied from a wheel cache,
   a site-packages left over from the checkout, or a PyPI download of the same
   version;
3. proves the installed distribution is the artifact's: version matches the
   manifest, the package imports from *that environment's* site-packages, and
   every ``embedded/js`` script on disk is byte-identical to the hash recorded
   from the wheel;
4. resolves that environment's console launcher through W1's existing
   ``resolve_launcher`` (which uses ``Path.absolute()``, never ``.resolve()`` —
   resolving would follow a POSIX venv's ``bin/python`` symlink out of the venv);
5. runs ``tests/release_gate_harness.run_release_gate_journey`` **unchanged**.
   There is exactly one canonical journey and this is not a second one.

Run from the repository root under an environment that has the test extra
installed (the harness needs ``fastmcp`` and ``psutil`` client-side); the
artifact under test supplies its own copies inside the fresh venv.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# The harness and its e2e_helpers seam live in tests/ (not an installed package).
for _extra_path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(_extra_path) not in sys.path:
        sys.path.insert(0, str(_extra_path))

import package_verify  # noqa: E402  PERMANENT(sys.path bootstrap above must run first)

from release_gate_harness import (  # noqa: E402  PERMANENT(sys.path bootstrap above must run first)
    gate_work_dir,
    resolve_launcher,
    run_release_gate_journey,
)

RESULT_SCHEMA_VERSION = 1
INSTALL_TIMEOUT = 900
PROBE_TIMEOUT = 180

# Printed by the in-venv probe as one JSON line; keeping the marker explicit
# means arbitrary installer chatter on stdout cannot be mistaken for the result.
_PROBE_MARKER = "__INSTALL_SMOKE_PROBE__"

_PROBE_SOURCE = f"""
import hashlib, importlib.metadata, json, sys
from pathlib import Path

import stealth_chrome_devtools_mcp as pkg

root = Path(pkg.__file__).parent
js_dir = root / "embedded" / "js"
payload = {{
    "version": importlib.metadata.version("{package_verify.DIST_NAME}"),
    "package_root": str(root),
    "executable": sys.executable,
    "js": {{
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(js_dir.glob("*.js"))
    }},
}}
print("{_PROBE_MARKER}" + json.dumps(payload))
"""


class SmokeError(Exception):
    """A smoke precondition failed; the message is the human-readable reason."""


def _run(cmd: list[str], *, timeout: int, cwd: Path | None = None) -> str:
    """Run a fully-resolved command, echoing it, and return stdout."""
    print(f"$ {' '.join(cmd)}")
    completed = subprocess.run(  # noqa: S603  PERMANENT(CI tool; argv is built from resolved absolute paths, never a shell string)
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
    )
    if completed.stdout:
        print(completed.stdout)
    if completed.stderr:
        print(completed.stderr, file=sys.stderr)
    if completed.returncode != 0:
        raise SmokeError(
            f"command failed (exit {completed.returncode}): {' '.join(cmd)}"
        )
    return completed.stdout


def _uv() -> str:
    uv = shutil.which("uv")
    if uv is None:
        raise SmokeError("uv is not on PATH; the smoke needs it to build the fresh env")
    return uv


def venv_python(venv_dir: Path) -> Path:
    """The interpreter inside ``venv_dir`` (absolute, not symlink-resolved)."""
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def create_fresh_env(venv_dir: Path, python_version: str) -> Path:
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    _run([_uv(), "venv", "--python", python_version, str(venv_dir)], timeout=300)
    interpreter = venv_python(venv_dir)
    if not interpreter.is_file():
        raise SmokeError(f"fresh environment has no interpreter at {interpreter}")
    return interpreter


def install_artifact(interpreter: Path, artifact: Path) -> None:
    """Install the LOCAL artifact by absolute path with every cache disabled."""
    if not artifact.is_absolute():
        raise SmokeError(f"artifact path must be absolute, got {artifact}")
    _run(
        [
            _uv(),
            "pip",
            "install",
            "--no-cache",
            "--python",
            str(interpreter),
            str(artifact),
        ],
        timeout=INSTALL_TIMEOUT,
    )


def probe_installation(interpreter: Path, cwd: Path) -> dict[str, object]:
    """Ask the fresh environment what it actually installed.

    Runs with ``cwd`` outside the repository so an accidental import of the
    checkout (rather than site-packages) cannot pass for the installed package.
    """
    stdout = _run(
        [str(interpreter), "-c", _PROBE_SOURCE], timeout=PROBE_TIMEOUT, cwd=cwd
    )
    for line in stdout.splitlines():
        if line.startswith(_PROBE_MARKER):
            return json.loads(line[len(_PROBE_MARKER) :])
    raise SmokeError("in-venv probe produced no result line")


def check_installation(
    probe: dict[str, object], manifest: dict[str, object], venv_dir: Path
) -> list[str]:
    """Violations of "what is installed IS the artifact" (empty == ok)."""
    problems: list[str] = []
    expected_version = str(manifest["version"])
    if probe.get("version") != expected_version:
        problems.append(
            f"installed version {probe.get('version')!r} != artifact "
            f"{expected_version!r}"
        )

    package_root = Path(str(probe.get("package_root", "")))
    try:
        inside = package_root.is_relative_to(venv_dir)
    except ValueError:  # pragma: no cover - differing drives on Windows
        inside = False
    if not inside:
        problems.append(
            f"package imports from {package_root} which is OUTSIDE the fresh "
            f"environment {venv_dir} — the smoke would have tested the checkout"
        )

    recorded = manifest["package_data"]
    if not isinstance(recorded, dict):
        return [*problems, "manifest 'package_data' is not an object"]
    expected_js = {member.rsplit("/", 1)[-1]: h for member, h in recorded.items()}
    installed_js = probe.get("js")
    if not isinstance(installed_js, dict):
        return [*problems, "probe returned no package-data hashes"]
    if set(installed_js) != set(expected_js):
        problems.append(
            f"installed embedded/js {sorted(installed_js)} != artifact "
            f"{sorted(expected_js)}"
        )
    for name, expected_hash in sorted(expected_js.items()):
        actual = installed_js.get(name)
        if actual is None:
            problems.append(f"package data missing after install: embedded/js/{name}")
        elif actual != expected_hash:
            problems.append(
                f"embedded/js/{name}: installed sha256 {actual} != artifact "
                f"{expected_hash}"
            )
    return problems


def select_artifact(dist_dir: Path, kind: str) -> Path:
    """The one wheel or the one sdist in ``dist_dir``, as an ABSOLUTE path.

    Resolved here rather than in the workflow so no shell has to glob a
    version-dependent filename and quote a native absolute path on three OSes.
    """
    wheel, sdist = package_verify.find_artifacts(dist_dir)
    return (wheel if kind == "wheel" else sdist).absolute()


def smoke(
    *,
    dist_dir: Path,
    manifest_path: Path,
    kind: str,
    work_dir: Path,
    python_version: str,
) -> dict[str, object]:
    artifact = select_artifact(dist_dir, kind)
    manifest = package_verify.load_manifest(manifest_path)

    # (1) The downloaded bytes are the built bytes — the same precondition the
    # publish job re-runs before it uploads anything.
    hash_problems = package_verify.check_artifact_hash(artifact, manifest)
    if hash_problems:
        raise SmokeError("; ".join(hash_problems))

    work_dir.mkdir(parents=True, exist_ok=True)
    venv_dir = work_dir / "venv"
    probe_cwd = work_dir / "probe-cwd"
    probe_cwd.mkdir(exist_ok=True)

    # (2) fresh env + local install, caches off.
    interpreter = create_fresh_env(venv_dir, python_version)
    install_artifact(interpreter, artifact)

    # (3) what landed IS the artifact.
    probe = probe_installation(interpreter, probe_cwd)
    problems = check_installation(probe, manifest, venv_dir)
    if problems:
        raise SmokeError("; ".join(problems))

    # (4) that environment's console launcher, absolute and unfollowed.
    launcher = resolve_launcher(interpreter)
    if not launcher.is_relative_to(venv_dir):
        raise SmokeError(
            f"resolved launcher {launcher} is outside the fresh environment {venv_dir}"
        )

    # (5) W1's canonical journey, unchanged, against that launcher.
    journey_dir = gate_work_dir(work_dir / "gate")
    journey_dir.mkdir(parents=True, exist_ok=True)
    record = asyncio.run(
        run_release_gate_journey(launcher=launcher, work_dir=journey_dir)
    )
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "artifact": artifact.name,
        "artifact_kind": kind,
        "artifact_sha256": package_verify.sha256_file(artifact),
        "version": manifest["version"],
        "installed_version": probe.get("version"),
        "package_root": probe.get("package_root"),
        "launcher": str(launcher),
        "venv": str(venv_dir),
        "python_version": python_version,
        "journey": record,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install one built artifact into a fresh env and run W1's journey."
    )
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--kind", choices=("wheel", "sdist"), required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--python", default="3.12")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        result = smoke(
            dist_dir=args.dist_dir,
            manifest_path=args.manifest,
            kind=args.kind,
            work_dir=args.work_dir,
            python_version=args.python,
        )
    except (SmokeError, package_verify.VerificationError) as exc:
        print(f"::error title=install-smoke::{exc}", file=sys.stderr)
        return 1

    text = json.dumps(result, indent=2, sort_keys=True, default=str)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"install-smoke {args.kind}: OK ({result['artifact']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
