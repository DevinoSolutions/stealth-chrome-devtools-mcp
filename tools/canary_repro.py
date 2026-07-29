#!/usr/bin/env python3
"""Write LOCAL-FIXTURE repro metadata for a canary run (plan_RELEASE W6).

The one bounded helper W6 allows. Its whole design problem is that "capture more
so the failure is easier to reproduce" and "never exfiltrate the operator's
browsing" pull in opposite directions, and a diagnostic helper that grows a
"just the DOM too" flag has already lost. So the capture surface is CLOSED, not
filtered: this tool can only ever write

  * synthetic fixture CALL IDS (opaque ids, not arguments),
  * fixture URL/PATH IDS (loopback fixture URLs, or paths under ``tests/``),
  * deterministic ORACLE VALUES (the expected/actual scalars a fixture asserts),
  * the W2 RUNNER + CHROME IDENTITY records, verbatim from the W2 tools.

It cannot write general DOM, screenshots, request headers or bodies, cookies,
profile data, or live-site content — not because it strips them, but because
every value it accepts must pass a scalar/length/shape check that a blob fails.
A caller who wants richer diagnostics does NOT extend this file: W12 later
defines the one canonical threat model and redaction API for that, and W6
deliberately does not invent a second one.

Destination rules (a repro dump is throwaway, and a throwaway that lands in your
home directory or the repository is not throwaway):

  * refuses the home directory, the repository (root or anything inside it), and
    the current working directory,
  * refuses to overwrite: the destination must not already exist,
  * refuses link traversal: no existing ancestor of the destination may be a
    symlink.

CI build tooling, stdlib only. Not application runtime.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: What every accepted value narrows to.
Scalar = str | int | float | bool

SCHEMA = "canary-repro/1"

#: Opaque synthetic ids only. Free text is refused precisely because it is the
#: shape a DOM/cookie/live-content leak would arrive in.
ID_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")

#: A fixture reference is a loopback fixture URL or a repo-relative test path.
LOOPBACK_RE = re.compile(r"^https?://(127\.0\.0\.1|\[::1\]|localhost)(:\d+)?(/|$)")
FIXTURE_PATH_RE = re.compile(r"^tests/[A-Za-z0-9_./-]{1,120}$")

MAX_ENTRIES = 200
MAX_VALUE_CHARS = 256
MAX_IDENTITY_KEYS = 64


class RefusedError(Exception):
    """The helper refused to write. Never a partial write: checks precede I/O."""


def _scalar(value: object, where: str) -> Scalar:
    """Accept only bounded scalars — the check a DOM/cookie/screenshot blob fails."""
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > MAX_VALUE_CHARS:
            raise RefusedError(
                f"{where}: value is {len(value)} chars, over the "
                f"{MAX_VALUE_CHARS}-char cap. W6 captures deterministic oracle "
                "scalars, never page content."
            )
        return value
    raise RefusedError(
        f"{where}: {type(value).__name__} is not a bounded scalar. W6 captures "
        "ids and oracle scalars only — no nested structures, no captured content."
    )


def _identity(path: Path | None, where: str) -> dict[str, Scalar]:
    """Load a W2 identity record: a flat mapping of bounded scalars, or nothing."""
    if path is None:
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RefusedError(
            f"{where}: cannot read identity JSON at {path}: {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise RefusedError(f"{where}: identity record must be a JSON object")
    if len(loaded) > MAX_IDENTITY_KEYS:
        raise RefusedError(f"{where}: identity record has {len(loaded)} keys, over cap")
    return {str(k): _scalar(v, f"{where}[{k}]") for k, v in loaded.items()}


def _check_id(value: str, where: str) -> str:
    if not ID_RE.match(value):
        raise RefusedError(
            f"{where}: {value!r} is not a synthetic id "
            f"(allowed: {ID_RE.pattern}). Free text is refused by design."
        )
    return value


def _check_fixture_ref(value: str, where: str) -> str:
    if LOOPBACK_RE.match(value) or FIXTURE_PATH_RE.match(value):
        if len(value) > MAX_VALUE_CHARS:
            raise RefusedError(f"{where}: fixture reference is over the length cap")
        return value
    raise RefusedError(
        f"{where}: {value!r} is neither a loopback fixture URL nor a tests/ path. "
        "W6 records LOCAL fixture references only — never live-site content or "
        "an absolute filesystem path."
    )


def _split_pair(raw: str, where: str) -> tuple[str, str]:
    key, sep, value = raw.partition("=")
    if not sep or not key:
        raise RefusedError(f"{where}: expected KEY=VALUE, got {raw!r}")
    return key, value


def build_record(
    *,
    call_ids: list[str],
    fixture_refs: list[str],
    oracles: list[str],
    runner_identity: Path | None,
    chrome_identity: Path | None,
) -> dict[str, object]:
    """Build the closed record, or raise :class:`RefusedError`. No I/O to the dest."""
    for name, seq in (
        ("--call-id", call_ids),
        ("--fixture-ref", fixture_refs),
        ("--oracle", oracles),
    ):
        if len(seq) > MAX_ENTRIES:
            raise RefusedError(
                f"{name}: {len(seq)} entries, over the {MAX_ENTRIES} cap"
            )

    oracle_map: dict[str, Scalar] = {}
    for raw in oracles:
        key, value = _split_pair(raw, "--oracle")
        oracle_map[_check_id(key, "--oracle key")] = _scalar(value, f"--oracle[{key}]")

    return {
        "schema": SCHEMA,
        "fixture_call_ids": [_check_id(c, "--call-id") for c in call_ids],
        "fixture_refs": [_check_fixture_ref(r, "--fixture-ref") for r in fixture_refs],
        "oracles": oracle_map,
        "runner_identity": _identity(runner_identity, "--runner-identity"),
        "chrome_identity": _identity(chrome_identity, "--chrome-identity"),
    }


def resolve_destination(dest: Path, *, repo_root: Path, home: Path, cwd: Path) -> Path:
    """Return the destination, or raise :class:`RefusedError`. Creates nothing."""
    # Ancestor symlinks are checked on the UNRESOLVED path: Path.resolve() would
    # follow the very link the check exists to catch.
    for ancestor in [dest, *dest.parents]:
        if ancestor.is_symlink():
            raise RefusedError(
                f"refusing destination {dest}: ancestor {ancestor} is a symlink. "
                "W6 does not traverse links to reach a write target."
            )

    resolved = dest.absolute()
    banned = {
        "the home directory": home.absolute(),
        "the current working directory": cwd.absolute(),
        "the repository": repo_root.absolute(),
    }
    for label, path in banned.items():
        if resolved == path:
            raise RefusedError(
                f"refusing destination {dest}: that is {label}. A repro dump is "
                "throwaway; point --out at a fresh temporary directory."
            )
    if resolved.is_relative_to(repo_root.absolute()):
        raise RefusedError(
            f"refusing destination {dest}: it is inside the repository. A repro "
            "dump must not be able to land in tracked or ignorable source."
        )
    if resolved.exists():
        raise RefusedError(
            f"refusing destination {dest}: it already exists. W6 never overwrites; "
            "pass a path that does not exist yet."
        )
    return resolved


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Write bounded local-fixture repro metadata for a canary run."
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="throwaway directory to create and write into (must not exist)",
    )
    parser.add_argument("--call-id", action="append", default=[])
    parser.add_argument("--fixture-ref", action="append", default=[])
    parser.add_argument("--oracle", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--runner-identity", type=Path, default=None)
    parser.add_argument("--chrome-identity", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        # Both checks run BEFORE anything is created, so a refusal leaves no
        # directory and no partial file behind.
        record = build_record(
            call_ids=args.call_id,
            fixture_refs=args.fixture_ref,
            oracles=args.oracle,
            runner_identity=args.runner_identity,
            chrome_identity=args.chrome_identity,
        )
        destination = resolve_destination(
            args.out, repo_root=repo_root, home=Path.home(), cwd=Path.cwd()
        )
    except RefusedError as exc:
        print(f"::error::canary_repro: {exc}", file=sys.stderr)
        return 1

    destination.mkdir(parents=True)
    target = destination / "canary-repro.json"
    target.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", "utf-8")
    print(f"canary_repro: wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
