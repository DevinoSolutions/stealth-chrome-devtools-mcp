#!/usr/bin/env python3
"""Make a DAMAGED COPY of a built artifact, for the W3 bite proofs.

plan_RELEASE §2.3 requires proof that ``package-verify``/smoke and the publish
precondition actually reject a bad artifact — a check nobody has ever seen fail
is not evidence. This tool produces the damaged input for that negative test.

It is deliberately copy-only: ``--source`` is opened read-only and ``--out`` must
be a different path, so the run's real hashed artifact can never be mutated by a
bite proof. No branch, commit, tag, or upload is involved.

Damage modes
------------
``--flip-byte``       flip the last byte of the copy (hash changes, nothing else).
``--drop-member M``   rewrite the copied wheel without member ``M`` (e.g. one
                      ``embedded/js`` script), so the *membership* rule is tested
                      independently of the hash rule.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path


class CorruptionError(Exception):
    """The requested damage could not be applied."""


def flip_last_byte(source: Path, out: Path) -> None:
    data = bytearray(source.read_bytes())
    if not data:
        raise CorruptionError(f"{source} is empty; nothing to flip")
    data[-1] ^= 0xFF
    out.write_bytes(bytes(data))


def drop_wheel_member(source: Path, out: Path, member: str) -> None:
    """Copy the wheel omitting ``member`` (and leaving everything else intact)."""
    with zipfile.ZipFile(source) as src:
        names = src.namelist()
        if member not in names:
            raise CorruptionError(
                f"{member!r} is not in {source.name}; cannot drop it "
                f"(the bite proof would prove nothing)"
            )
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                if info.filename == member:
                    continue
                dst.writestr(info, src.read(info.filename))


def _guard_copy_only(source: Path, out: Path) -> None:
    """Refuse anything that could damage the run's real, hashed artifact."""
    if source == out:
        raise CorruptionError(
            "--out must differ from --source: a bite proof never damages "
            "the run's real artifact"
        )
    if not source.is_file():
        raise CorruptionError(f"source artifact not found: {source}")


def _assert_source_intact(source: Path, size_before: int) -> None:
    if source.stat().st_size != size_before:
        raise CorruptionError(f"source artifact changed size: {source}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--flip-byte", action="store_true")
    group.add_argument("--drop-member", default="")
    args = parser.parse_args(argv)

    source = args.source.absolute()
    out = args.out.absolute()
    try:
        _guard_copy_only(source, out)
        out.parent.mkdir(parents=True, exist_ok=True)

        before = source.stat().st_size
        if args.flip_byte:
            flip_last_byte(source, out)
            damage = "flipped the last byte"
        else:
            drop_wheel_member(source, out, args.drop_member)
            damage = f"dropped member {args.drop_member!r}"
        _assert_source_intact(source, before)
        print(f"corrupt_artifact: {damage} -> {out} (source {source} untouched)")
    except (CorruptionError, OSError, zipfile.BadZipFile) as exc:
        print(f"::error title=corrupt-artifact::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
