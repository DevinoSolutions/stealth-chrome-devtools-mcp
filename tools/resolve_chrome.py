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

``--freeze-updater`` (F-819) neutralises the OS's background Chrome updater
*before* the identity is read, so the answer stays true for the rest of the job.
It is opt-in for a reason: it changes machine state, so only CI asks for it.
:func:`resolve_chrome` itself stays side-effect-free — importers get the reading
and nothing else.
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


# ---------------------------------------------------------------------------
# F-819 — freeze the updater before reading the identity.
#
# A CI job resolves this identity once and then trusts it for the whole run: the
# JSON artifact records it, and TestChromeIdentity compares it against CDP
# ``Browser.getVersion`` from a browser launched minutes later. On GitHub's macOS
# runners Keystone can swap Chrome Stable in between, and the comparison then
# reports a product mismatch that no product change can prevent — the two
# readings were of two different binaries.
#
# The mechanism per OS is deliberately the *destructive* one, not the polite one:
#
# * **macOS** — unload and delete Keystone's launchd jobs, delete both
#   ``GoogleSoftwareUpdate`` trees, and leave each path as a root-owned mode-000
#   stub inside a root-owned parent. The documented soft knob
#   (``defaults write com.google.Keystone.Agent checkInterval 0``) was rejected:
#   Chrome re-registers Keystone through KeystoneRegistration.framework every
#   time it launches, and launching Chrome is precisely what this run does next,
#   so an advisory setting can be undone by the very act it must survive. The
#   stub is what makes the removal durable — a reinstall has nowhere to land.
# * **Windows** — stop and disable the two Google Update services, disable the
#   machine update tasks, and set the enterprise policy that turns update checks
#   off. Runners are administrators.
# * **Linux** — nothing. The images carry no background updater; the package is
#   only upgraded when a job runs apt, and no job here does.
#
# Every sub-step is best-effort by construction: exit codes are recorded, never
# checked, and an unrunnable tool is swallowed. A missing service, task, plist or
# directory is the normal case on at least one OS, and this must never be the
# thing that turns a green run red.
# ---------------------------------------------------------------------------
_KEYSTONE_USER_PLISTS = (
    ("Library", "LaunchAgents", "com.google.keystone.agent.plist"),
    ("Library", "LaunchAgents", "com.google.keystone.xpcservice.plist"),
)
_KEYSTONE_SYSTEM_PLISTS = (
    "/Library/LaunchAgents/com.google.keystone.agent.plist",
    "/Library/LaunchDaemons/com.google.keystone.daemon.plist",
    "/Library/LaunchDaemons/com.google.keystone.system.agent.plist",
)
_GOOGLE_UPDATE_SERVICES = ("gupdate", "gupdatem")
_GOOGLE_UPDATE_TASKS = ("GoogleUpdateTaskMachineCore", "GoogleUpdateTaskMachineUA")
_GOOGLE_UPDATE_POLICY_KEY = r"HKLM\SOFTWARE\Policies\Google\Update"


def _macos_freeze_steps() -> list[list[str]]:
    """Keystone's launchd jobs, its trees, and the stubs that keep them gone."""
    home = Path.home()
    steps: list[list[str]] = []

    for relative in _KEYSTONE_USER_PLISTS:
        plist = str(home.joinpath(*relative))
        # The user agents live in the calling user's launchd domain: `sudo` here
        # would target root's domain and unload nothing.
        steps.append(["launchctl", "unload", "-w", plist])
        steps.append(["rm", "-f", plist])

    for plist in _KEYSTONE_SYSTEM_PLISTS:
        steps.append(["sudo", "launchctl", "unload", "-w", plist])
        steps.append(["sudo", "rm", "-f", plist])

    for root in (
        str(home / "Library" / "Google" / "GoogleSoftwareUpdate"),
        "/Library/Google/GoogleSoftwareUpdate",
    ):
        steps.append(["sudo", "rm", "-rf", root])
        steps.append(["sudo", "mkdir", "-p", root])
        steps.append(["sudo", "chown", "root:wheel", root])
        steps.append(["sudo", "chmod", "000", root])

    # A root-owned stub inside a user-owned parent is still removable by that
    # user — unlinking is governed by the parent's write bit. `/Library/Google`
    # is already root-owned; the one in the home directory is not.
    home_parent = str(home / "Library" / "Google")
    steps.append(["sudo", "chown", "root:wheel", home_parent])
    steps.append(["sudo", "chmod", "555", home_parent])
    return steps


def _windows_freeze_steps() -> list[list[str]]:
    """Google Update's services, its scheduled tasks, and the policy."""
    steps: list[list[str]] = []
    for service in _GOOGLE_UPDATE_SERVICES:
        # Stop first: disabling a running service leaves it running for this boot.
        steps.append(["sc.exe", "stop", service])
        steps.append(["sc.exe", "config", service, "start=", "disabled"])
    steps.extend(
        ["schtasks.exe", "/Change", "/TN", task, "/DISABLE"]
        for task in _GOOGLE_UPDATE_TASKS
    )
    # UpdateDefault=0 forbids updates; AutoUpdateCheckPeriodMinutes=0 stops the
    # checks that would otherwise still run. Google documents both as the policy
    # pair — neither alone is the off switch.
    steps.extend(
        [
            "reg.exe",
            "add",
            _GOOGLE_UPDATE_POLICY_KEY,
            "/v",
            value,
            "/t",
            "REG_DWORD",
            "/d",
            "0",
            "/f",
        ]
        for value in ("UpdateDefault", "AutoUpdateCheckPeriodMinutes")
    )
    return steps


def _run_freeze_step(command: list[str]) -> str:
    """Run one freeze step and narrate it. Never raises, never checks the code."""
    rendered = " ".join(command)
    try:
        completed = subprocess.run(  # noqa: S603  PERMANENT(F-819: the argv comes from the fixed per-OS tables above — no shell, no caller input; the OS tools are named rather than absolute because their absence is a legitimate outcome this step tolerates)
            command,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"skipped  {rendered}  :: {type(exc).__name__}: {exc}"
    detail = (completed.stderr or completed.stdout or "").strip().splitlines()
    tail = f"  :: {detail[-1].strip()}" if detail else ""
    return f"rc={completed.returncode}  {rendered}{tail}"


def freeze_updater() -> list[str]:
    """Neutralise this OS's background Chrome updater. Best-effort, idempotent.

    Returns one narration line per step attempted, so the CI log shows what the
    freeze actually did on this image rather than what it hoped to do.
    """
    system = platform.system().lower()
    if system == "darwin":
        steps = _macos_freeze_steps()
    elif system == "windows":
        steps = _windows_freeze_steps()
    else:
        return [
            f"freeze_updater: {platform.system()} has no background Chrome "
            f"updater on the runner images — nothing to freeze"
        ]
    return [_run_freeze_step(step) for step in steps]


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
    parser.add_argument(
        "--freeze-updater",
        action="store_true",
        help=(
            "neutralise the OS's background Chrome updater BEFORE reading the "
            "version, so the resolved identity stays true for the whole job "
            "(F-819). Changes machine state; intended for CI runners only."
        ),
    )
    args = parser.parse_args(argv)

    if args.freeze_updater:
        for note in freeze_updater():
            print(f"freeze-updater: {note}", file=sys.stderr)

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
