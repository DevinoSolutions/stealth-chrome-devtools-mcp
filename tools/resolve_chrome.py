#!/usr/bin/env python3
"""Resolve the image-provided Google Chrome **Stable** identity (plan_RELEASE W2).

The three-OS release gate must prove that production's browser auto-discovery
(``embedded/platform_utils.check_browser_executable``) resolves to the *image
Chrome Stable* — not Chromium, not Edge, not another channel — and that the
launched binary reports that same identity over CDP ``Browser.getVersion``.

This module is the ONE source of the *expected* Chrome identity, consumed two
ways with no second implementation:

* the CI job runs it as ``python tools/resolve_chrome.py --out chrome.json`` to
  emit the canonical path + version as a run artifact, and
* ``tests/test_browser_integration.py`` (``TestChromeIdentity``) imports
  :func:`resolve_chrome` to get that same expected identity and assert
  auto-discovery + CDP ``Browser.getVersion`` agree with it.

Candidate lists are deliberately Chrome-Stable-only per OS: a match on Chromium
or Edge is a *different* identity and must surface as a mismatch, not a pass.
Stdlib only — no runtime dependency, no browser launch here.
"""

from __future__ import annotations

import argparse
import json
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


def _candidate_paths() -> list[Path]:
    """Canonical install locations of Google Chrome **Stable** for this OS."""
    system = platform.system().lower()
    if system == "windows":
        return [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ]
    if system == "darwin":
        return [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    return [
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/google-chrome-stable"),
        Path("/opt/google/chrome/google-chrome"),
        Path("/opt/google/chrome/chrome"),
    ]


def _resolve_path() -> Path:
    """The absolute, canonical Chrome Stable executable, or raise.

    Static install locations win first (they are unambiguous Chrome Stable);
    a PATH lookup of the ``google-chrome`` launcher names is the fallback for
    Linux images that only expose the symlinked launcher.
    """
    for candidate in _candidate_paths():
        if candidate.is_file():
            return candidate.resolve()
    for name in ("google-chrome-stable", "google-chrome"):
        found = shutil.which(name)
        if found:
            resolved = Path(found).resolve()
            if resolved.is_file():
                return resolved
    raise FileNotFoundError(
        "Google Chrome Stable executable not found in the canonical install "
        f"locations for {platform.system()!r}"
    )


def _version_via_cli(path: Path) -> str:
    """Best-effort version string from ``<chrome> --version`` (POSIX)."""
    try:
        completed = subprocess.run(  # noqa: S603  PERMANENT(runs the resolved absolute Chrome binary to read its version; no shell, no untrusted input)
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    line = (completed.stdout or "").strip()
    for token in line.split():
        if token[:1].isdigit() and "." in token:
            return token
    return line


def _read_version(path: Path) -> str:
    """The Chrome build version, resolved by the most reliable per-OS means.

    ``chrome.exe --version`` prints nothing on Windows, so the versioned sibling
    ``Application\\<version>\\`` directory is authoritative there. macOS carries
    the version in the app bundle's ``Info.plist``. Linux answers ``--version``.
    """
    system = platform.system().lower()
    if system == "windows":
        application_dir = path.parent
        versions = sorted(
            entry.name
            for entry in application_dir.iterdir()
            if entry.is_dir() and entry.name[:1].isdigit() and "." in entry.name
        )
        return versions[-1] if versions else ""
    if system == "darwin":
        info_plist = path.parent.parent / "Info.plist"
        try:
            data = plistlib.loads(info_plist.read_bytes())
        except (OSError, ValueError):
            data = {}
        version = data.get("CFBundleShortVersionString")
        if isinstance(version, str) and version:
            return version
        return _version_via_cli(path)
    return _version_via_cli(path)


def resolve_chrome() -> dict[str, str]:
    """Return the image Chrome Stable identity: os, arch, path, version, product.

    ``product`` is the shape CDP ``Browser.getVersion`` reports for this build
    (``Chrome/<version>``), so a caller can compare directly.
    """
    path = _resolve_path()
    version = _read_version(path)
    return {
        "os": platform.system(),
        "arch": platform.machine(),
        "path": str(path),
        "version": version,
        "product": f"Chrome/{version}" if version else "Chrome",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve the image-provided Google Chrome Stable identity."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write the JSON identity to this path (in addition to stdout)",
    )
    args = parser.parse_args(argv)

    try:
        identity = resolve_chrome()
    except FileNotFoundError as exc:
        print(f"resolve_chrome: {exc}", file=sys.stderr)
        return 1

    text = json.dumps(identity, indent=2, sort_keys=True)
    print(text)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
