#!/usr/bin/env python3
"""The ONE parser/generator for the ``release-evidence/v1`` ledger (plan_RELEASE W5).

Nothing else in this repository may parse, write, or interpret the ledger: the
release contract generator (:mod:`gen_release_contract`), W8's parity gate, and
W11's docs checker all import *this* module. A second reader would be a second
truth, and the whole point of the ledger is that there is exactly one.

What the ledger is
------------------
Every required job/matrix cell of ``.github/workflows/release-gate.yml`` writes
exactly one child record::

    release-evidence/<release_sha>/<job_id>/<matrix_cell>.json

and the ``release-evidence`` job writes the conditional aggregate::

    release-evidence/<release_sha>/release-gate/aggregate.json

The aggregate is a direct ``needs:`` edge of the ``release-gate`` check *in
addition to* every child job it validates, so the ledger can never turn a failed
job green — it can only ever turn a green run red.

Fail-closed by construction
---------------------------
:func:`build_aggregate` refuses to emit a successful aggregate when any of the
following holds (each has a negative test in ``tests/test_release_evidence.py``):

* a required child record is missing, duplicated, or unreadable;
* a child record exists for a cell that is not declared in :data:`REQUIRED_CELLS`;
* a child's own ``job.id``/``job.matrix_cell`` disagree with its path;
* a child's ``release_sha`` is not the SHA being qualified (stale evidence);
* a child's ``workflow.run_id``/``run_attempt`` are not this run's;
* a child's ``job.terminal_outcome`` is anything other than ``success``;
* a browser cell recorded no Chrome identity, or a cell that must prove a
  *launch* recorded no launched major version;
* a cell that must run pytest recorded no pytest block (or vice versa);
* a recorded artifact or JUnit hash does not match the bytes on disk;
* an ``MQ`` id is malformed, duplicated inside a record, or a declared required
  MQ id is absent from the whole run;
* a tool claim cites a node that did not execute-and-pass on every cell it
  claims, cites the representative journey, or claims a transport the citing
  job cannot evidence.

Deliberate schema notes
-----------------------
``chrome`` is ``null`` for a non-browser job (plan_RELEASE §2.5 says so
explicitly). ``pytest`` is ``null`` for a job that runs no pytest at all
(``quality``, ``known-gaps``, ``build-dist``, ``package-verify``,
``install-smoke`` — the last drives the journey through ``tools/install_smoke.py``
rather than through pytest). Which cells may be null is *declared* per cell in
:data:`REQUIRED_CELLS`, so "null" is never a way to omit evidence a cell owes.

``executed_node_ids`` holds every node the cell ran to a terminal result;
``skipped``/``xfail``/``failed`` are recorded separately and a claimed node that
appears in any of them is rejected. A skipped node is *not* in
``executed_node_ids`` — it was collected, not executed.

Stdlib only: this runs on runners that have not installed the project.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from release_policy import (
    _QUERY_VALUE_RE,
    _USERINFO_RE,
    BROWSER_EXEC_TOOLS,
    DEFAULT_BIND_HOST,
    DOWNLOAD_NAME_FRAGMENTS,
    DROP_KEY_FRAGMENTS,
    FILESYSTEM_READ_TOOLS,
    FILESYSTEM_WRITE_TOOLS,
    HOST_EXEC_SITES,
    HOST_EXEC_TOOLS,
    MIN_SECRET_LENGTH,
    PRESERVED_DIAGNOSTIC_FIELDS,
    REDACTION_ACTIONS,
    REDACTION_PLACEHOLDER,
    REDACTION_POLICY,
    SECRET_CLASSES,
    THREAT_CONTRACT,
    THREAT_DIMENSIONS,
    RedactionRule,
    ThreatRow,
    _drop_class_for_key,
    _normalise_secrets,
    _validate_redaction_policy,
    _validate_threat_contract,
    host_secret_values,
    placeholder,
    redact,
    redact_text,
    redaction_rows,
    threat_rows,
    validate_policy,
)
from release_records import (
    EmitSpec,
    _as_dict,
    _chrome_block,
    _copy_artifacts,
    _node_id,
    _python_version,
    _runner_block,
    _sorted_unique,
    _str_field,
    _validate_artifacts,
    _validate_chrome,
    _validate_job,
    _validate_mq_ids,
    _validate_node_list,
    _validate_pytest,
    _validate_runner,
    _validate_workflow,
    build_record,
    dumps,
    parse_junit,
    sha256_file,
    validate_record,
)
from release_schema import (
    AGGREGATE_CELL,
    AGGREGATE_JOB,
    ALL_OS,
    ARTIFACT_KINDS,
    CELL_RE,
    CELLS_BY_KEY,
    CLAIMS_KEYS,
    CLAIMS_PATH,
    CLAIMS_SCHEMA,
    FULL_TIER,
    HASH_RE,
    IMAGE_OS_RE,
    LINUX,
    MACOS,
    MQ_RE,
    NODE_RE,
    NON_PER_TOOL_NODES,
    NUMERIC_RE,
    PR_TIER,
    PYTHON_RE,
    RECORD_KEYS,
    REPO_ROOT,
    REQUIRED_CELLS,
    REQUIRED_KEYS,
    SCHEMA,
    SHA_RE,
    TERMINAL_OUTCOMES,
    TOOL_STATES,
    TOOLS_DIR,
    TRANSPORT_JOBS,
    TRANSPORTS,
    UNPINNED,
    VERSION_RE,
    WINDOWS,
    WORKFLOW_EVENTS,
    CellSpec,
    _build_required_cells,
    _spec,
    cells_for_tier,
    keys_for_tier,
    plan_matrices,
    tier_for_event,
)

#: Every name that moved to a sibling module stays importable from here (T-5431).
__all__ = [
    "AGGREGATE_CELL",
    "AGGREGATE_JOB",
    "ALL_OS",
    "ARTIFACT_KINDS",
    "BROWSER_EXEC_TOOLS",
    "CELLS_BY_KEY",
    "CELL_RE",
    "CLAIMS_KEYS",
    "CLAIMS_PATH",
    "CLAIMS_SCHEMA",
    "DEFAULT_BIND_HOST",
    "DOWNLOAD_NAME_FRAGMENTS",
    "DROP_KEY_FRAGMENTS",
    "FILESYSTEM_READ_TOOLS",
    "FILESYSTEM_WRITE_TOOLS",
    "FULL_TIER",
    "HASH_RE",
    "HOST_EXEC_SITES",
    "HOST_EXEC_TOOLS",
    "IMAGE_OS_RE",
    "LINUX",
    "MACOS",
    "MIN_SECRET_LENGTH",
    "MQ_RE",
    "NODE_RE",
    "NON_PER_TOOL_NODES",
    "NUMERIC_RE",
    "PRESERVED_DIAGNOSTIC_FIELDS",
    "PR_TIER",
    "PYTHON_RE",
    "RECORD_KEYS",
    "REDACTION_ACTIONS",
    "REDACTION_PLACEHOLDER",
    "REDACTION_POLICY",
    "REPO_ROOT",
    "REQUIRED_CELLS",
    "REQUIRED_KEYS",
    "SCHEMA",
    "SECRET_CLASSES",
    "SHA_RE",
    "TERMINAL_OUTCOMES",
    "THREAT_CONTRACT",
    "THREAT_DIMENSIONS",
    "TOOLS_DIR",
    "TOOL_STATES",
    "TRANSPORTS",
    "TRANSPORT_JOBS",
    "UNPINNED",
    "VERSION_RE",
    "WINDOWS",
    "WORKFLOW_EVENTS",
    "_QUERY_VALUE_RE",
    "_USERINFO_RE",
    "CellSpec",
    "EmitSpec",
    "RedactionRule",
    "ThreatRow",
    "_as_dict",
    "_build_required_cells",
    "_chrome_block",
    "_copy_artifacts",
    "_drop_class_for_key",
    "_node_id",
    "_normalise_secrets",
    "_python_version",
    "_runner_block",
    "_sorted_unique",
    "_spec",
    "_str_field",
    "_validate_artifacts",
    "_validate_chrome",
    "_validate_job",
    "_validate_mq_ids",
    "_validate_node_list",
    "_validate_pytest",
    "_validate_redaction_policy",
    "_validate_runner",
    "_validate_threat_contract",
    "_validate_workflow",
    "build_record",
    "cells_for_tier",
    "dumps",
    "host_secret_values",
    "keys_for_tier",
    "parse_junit",
    "placeholder",
    "plan_matrices",
    "redact",
    "redact_text",
    "redaction_rows",
    "sha256_file",
    "threat_rows",
    "tier_for_event",
    "validate_policy",
    "validate_record",
]

# W8 populates this as MQ steps acquire runtime evidence. It is a real check
# today: a declared id that no child records fails the aggregate.
REQUIRED_MQ_IDS: frozenset[str] = frozenset()


# ── aggregate ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AggregateSpec:
    """The identity the aggregate demands every child agree with."""

    root: Path
    release_sha: str
    workflow_name: str
    run_id: str
    run_attempt: str
    event: str
    claims_path: Path


def _discover_children(
    sha_dir: Path, tier: str = FULL_TIER
) -> tuple[dict[str, Path], list[str]]:
    """Map ``job/cell`` → record path; report extras and duplicates.

    A record for a declared cell the ``tier`` does not owe is tolerated; a missing
    record is only reported for a cell the tier owes (T-6910).
    """
    found: dict[str, Path] = {}
    problems: list[str] = []
    # Keys are path-derived, so an exact duplicate cannot exist on disk — but a
    # case-variant CAN on a case-sensitive filesystem, and merging artifacts
    # from many jobs is exactly where one would appear. Collision is checked
    # case-insensitively BEFORE the declared-cell check so a second record for a
    # cell reads as the duplicate it is rather than as an unrelated stray.
    by_lowered: dict[str, str] = {}
    for path in sorted(sha_dir.rglob("*.json")):
        rel = path.relative_to(sha_dir).as_posix()
        if rel.startswith(f"{AGGREGATE_JOB}/") or "/artifacts/" in rel:
            continue
        key = rel[: -len(".json")]
        lowered = key.lower()
        if lowered in by_lowered:
            problems.append(
                f"duplicate child record for {key!r} (already have "
                f"{by_lowered[lowered]!r})"
            )
            continue
        by_lowered[lowered] = key
        if key not in CELLS_BY_KEY:
            problems.append(f"extra child record for undeclared cell {key!r}")
            continue
        found[key] = path
    problems.extend(
        f"missing child record for required cell {key!r}"
        for key in keys_for_tier(tier)
        if key not in found
    )
    return found, problems


def _check_identity(record: dict[str, object], spec: AggregateSpec) -> list[str]:
    problems: list[str] = []
    if _str_field(record, "release_sha") != spec.release_sha:
        problems.append(
            f"stale evidence: release_sha {record.get('release_sha')!r} != "
            f"{spec.release_sha!r}"
        )
    workflow = _as_dict(record.get("workflow")) or {}
    if _str_field(workflow, "run_id") != spec.run_id:
        problems.append(
            f"foreign evidence: workflow.run_id {workflow.get('run_id')!r} != "
            f"{spec.run_id!r}"
        )
    if _str_field(workflow, "run_attempt") != spec.run_attempt:
        problems.append(
            f"foreign evidence: workflow.run_attempt "
            f"{workflow.get('run_attempt')!r} != {spec.run_attempt!r}"
        )
    job = _as_dict(record.get("job")) or {}
    if _str_field(job, "terminal_outcome") != "success":
        problems.append(f"non-success terminal outcome {job.get('terminal_outcome')!r}")
    return problems


def _check_hashes(record: dict[str, object], sha_dir: Path) -> list[str]:
    problems: list[str] = []
    artifacts = record.get("artifacts")
    junit_recorded = ""
    pytest_block = _as_dict(record.get("pytest"))
    if pytest_block is not None:
        junit_recorded = _str_field(pytest_block, "junit_sha256")
    if not isinstance(artifacts, list):
        return ["artifacts: not a list"]
    junit_seen = False
    for item in artifacts:
        entry = _as_dict(item)
        if entry is None:
            continue
        path = sha_dir / _str_field(entry, "path")
        if not path.is_file():
            problems.append(f"artifact {entry.get('path')!r} was not uploaded")
            continue
        actual = sha256_file(path)
        if actual != _str_field(entry, "sha256"):
            problems.append(
                f"artifact hash mismatch for {entry.get('path')!r}: recorded "
                f"{entry.get('sha256')!r}, on disk {actual!r}"
            )
        if _str_field(entry, "kind") == "junit":
            junit_seen = True
            if junit_recorded and actual != junit_recorded:
                problems.append(
                    f"JUnit hash mismatch: pytest.junit_sha256 {junit_recorded!r} "
                    f"!= uploaded report {actual!r}"
                )
    if junit_recorded and not junit_seen:
        problems.append("pytest.junit_sha256 recorded but no junit artifact uploaded")
    return problems


def _executed_index(children: dict[str, dict[str, object]]) -> dict[str, set[str]]:
    """cell key → the node ids that executed AND passed on that cell."""
    index: dict[str, set[str]] = {}
    for key, record in children.items():
        block = _as_dict(record.get("pytest"))
        if block is None:
            index[key] = set()
            continue
        bad: set[str] = set()
        for field in ("skipped", "xfail", "failed"):
            value = block.get(field)
            if isinstance(value, list):
                bad |= {item for item in value if isinstance(item, str)}
        executed = block.get("executed_node_ids")
        ran = (
            {item for item in executed if isinstance(item, str)}
            if isinstance(executed, list)
            else set()
        )
        index[key] = ran - bad
    return index


def _check_mq_ids(children: dict[str, dict[str, object]]) -> list[str]:
    seen: set[str] = set()
    for record in children.values():
        value = record.get("mq_ids")
        if isinstance(value, list):
            seen |= {item for item in value if isinstance(item, str)}
    return [
        f"required MQ id {mq!r} has no runtime evidence in this run"
        for mq in sorted(REQUIRED_MQ_IDS - seen)
    ]


def verify_claims(
    claims: dict[str, object],
    index: dict[str, set[str]],
    tier: str = FULL_TIER,
) -> list[str]:
    """Check every ``release-qualified-success`` claim against the real ledger.

    This is what stops the contract from claiming a tool the run did not prove:
    the claim's node must have executed AND passed on every cell it names, must
    not be the representative journey, and a ``stdio`` claim must be evidenced by
    a job that actually drives the real-stdio lane.
    """
    problems: list[str] = []
    owed = set(keys_for_tier(tier))
    for claim in claim_rows(claims):
        node = str(claim.get("node_id", ""))
        tool = str(claim.get("tool", ""))
        cells = claim.get("required_cells")
        if node in NON_PER_TOOL_NODES:
            problems.append(
                f"claim for {tool!r} cites the representative journey {node!r}, "
                f"which plan_RELEASE §2.5 forbids as per-tool evidence"
            )
        if not isinstance(cells, list) or not cells:
            problems.append(f"claim for {tool!r} names no required cells")
            continue
        for cell in cells:
            key = str(cell)
            if key not in CELLS_BY_KEY:
                problems.append(f"claim for {tool!r} names undeclared cell {key!r}")
                continue
            if key not in owed:
                continue  # a cell this tier does not run (T-6910)
            if str(claim.get("transport", "")) == "stdio" and (
                CELLS_BY_KEY[key].job not in TRANSPORT_JOBS
            ):
                problems.append(
                    f"claim for {tool!r} asserts stdio but cites {key!r}, which "
                    f"does not run the real-stdio lane"
                )
            if node not in index.get(key, set()):
                problems.append(
                    f"claim for {tool!r} cites {node!r} on {key!r}, where it did "
                    f"not execute and pass"
                )
    return problems


def build_aggregate(spec: AggregateSpec) -> tuple[dict[str, object], list[str]]:
    """Build the aggregate record and every reason it must not be trusted."""
    sha_dir = spec.root / spec.release_sha
    problems: list[str] = []
    if not sha_dir.is_dir():
        return {}, [f"no evidence directory for release SHA {spec.release_sha!r}"]
    tier = tier_for_event(spec.event)
    paths, problems_found = _discover_children(sha_dir, tier)
    problems.extend(problems_found)
    records: dict[str, dict[str, object]] = {}
    children: list[dict[str, object]] = []
    for key in sorted(paths):
        path = paths[key]
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"child {key!r} is unreadable: {exc}")
            continue
        problems.extend(f"{key}: {p}" for p in validate_record(record, expect_key=key))
        block = _as_dict(record)
        if block is None:
            continue
        records[key] = block
        problems.extend(f"{key}: {p}" for p in _check_identity(block, spec))
        problems.extend(f"{key}: {p}" for p in _check_hashes(block, sha_dir))
        children.append(
            {
                "key": key,
                "path": path.relative_to(sha_dir).as_posix(),
                "sha256": sha256_file(path),
            }
        )
    problems.extend(_check_mq_ids(records))
    claims = load_claims(spec.claims_path)
    problems.extend(verify_claims(claims, _executed_index(records), tier))
    surface = tool_surface(claims)
    aggregate: dict[str, object] = {
        "schema": SCHEMA,
        "release_sha": spec.release_sha,
        "workflow": {
            "name": spec.workflow_name,
            "run_id": spec.run_id,
            "run_attempt": spec.run_attempt,
            "event": spec.event,
        },
        "job": {
            "id": AGGREGATE_JOB,
            "matrix_cell": AGGREGATE_CELL,
            "terminal_outcome": "success" if not problems else "failure",
        },
        "tier": tier,
        "required_cells": list(keys_for_tier(tier)),
        "children": sorted(children, key=lambda item: str(item["key"])),
        "tool_surface": surface,
        "problems": problems,
    }
    return aggregate, problems


# ── tool claim ledger ───────────────────────────────────────────────────────
def registry_sections() -> dict[str, tuple[str, ...]]:
    """section → served tool names, DERIVED from the live registry (never typed).

    ``SECTION_TOOLS`` is filled by ``@section_tool`` at registration time, so the
    server module must be imported before it is read — exactly what
    ``tests/test_doc_claims.py`` does. Importing it as a module (not via runpy)
    registers each tool once.
    """
    from stealth_chrome_devtools_mcp.embedded import server as _server
    from stealth_chrome_devtools_mcp.embedded.tool_registry import SECTION_TOOLS

    assert _server is not None  # noqa: S101  PERMANENT(the import is the point: it populates SECTION_TOOLS)
    return {section: tuple(sorted(names)) for section, names in SECTION_TOOLS.items()}


def registry_tool_names() -> tuple[str, ...]:
    """The served tool surface, DERIVED from the live registry (never typed)."""
    return tuple(sorted({n for names in registry_sections().values() for n in names}))


def load_claims(path: Path | None = None) -> dict[str, object]:
    target = path if path is not None else CLAIMS_PATH
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != CLAIMS_SCHEMA:
        raise ValueError(f"{target}: not a {CLAIMS_SCHEMA} document")
    return data


def claim_rows(claims: dict[str, object]) -> list[dict[str, object]]:
    """The declared `release-qualified-success` rows (verified elsewhere)."""
    rows = claims.get("qualified")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _notes(claims: dict[str, object]) -> dict[str, object]:
    notes = claims.get("served_unqualified_notes")
    return notes if isinstance(notes, dict) else {}


def validate_claims_document(
    claims: dict[str, object], names: tuple[str, ...]
) -> list[str]:
    """Every served tool has exactly one state; no claim names an unknown tool."""
    problems: list[str] = []
    served = set(names)
    unknown_keys = set(claims) - CLAIMS_KEYS
    if unknown_keys:
        problems.append(f"claims: unknown top-level field(s) {sorted(unknown_keys)}")
    missing_keys = CLAIMS_KEYS - set(claims)
    if missing_keys:
        problems.append(f"claims: missing top-level field(s) {sorted(missing_keys)}")
    seen: list[str] = []
    for row in claim_rows(claims):
        tool = str(row.get("tool", ""))
        seen.append(tool)
        if tool not in served:
            problems.append(f"qualified claim for unknown tool {tool!r}")
        missing = {
            "tool",
            "outcome",
            "transport",
            "node_id",
            "site_shape",
            "required_cells",
        } - set(row)
        if missing:
            problems.append(f"claim for {tool!r} is missing {sorted(missing)}")
        if str(row.get("transport", "")) not in TRANSPORTS:
            problems.append(f"claim for {tool!r} names an unknown transport")
    if len(set(seen)) != len(seen):
        problems.append("duplicate qualified claim for the same tool")
    problems.extend(
        f"served-unqualified note for unknown tool {tool!r}"
        for tool in _notes(claims)
        if tool not in served
    )
    not_served = claims.get("not_served")
    if isinstance(not_served, list):
        problems.extend(
            f"not-served entry {name!r} is actually served by the registry"
            for name in not_served
            if str(name) in served
        )
    return problems


def tool_surface(claims: dict[str, object]) -> dict[str, object]:
    """Counts for the contract headline — derived, never typed."""
    names = registry_tool_names()
    qualified = {str(row.get("tool", "")) for row in claim_rows(claims)}
    not_served = claims.get("not_served")
    not_served_count = len(not_served) if isinstance(not_served, list) else 0
    return {
        "served_total": len(names),
        "release_qualified": len(qualified & set(names)),
        "served_unqualified": len(set(names) - qualified),
        "not_served": not_served_count,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────
def _artifact_pairs(values: list[str]) -> tuple[tuple[str, Path], ...]:
    pairs: list[tuple[str, Path]] = []
    for value in values:
        kind, _, raw = value.partition("=")
        if kind not in ARTIFACT_KINDS or not raw:
            raise SystemExit(f"--artifact must be <kind>=<path>, got {value!r}")
        pairs.append((kind, Path(raw)))
    return tuple(pairs)


def _optional(path: str) -> Path | None:
    """A cited input that a FAILED step never produced is reported, not raised.

    Emit runs with ``if: always()`` so a red cell still writes its record. When
    an input is absent the record simply lacks that evidence, and
    :func:`validate_record` fails the cell for the omission — the failure stays a
    ledger problem instead of an emit traceback that hides it.
    """
    if not path:
        return None
    candidate = Path(path)
    if not candidate.is_file():
        print(f"::warning::release_evidence: {path} was not produced by this cell")
        return None
    return candidate


def _cmd_emit(args: argparse.Namespace) -> int:
    key = f"{args.job_id}/{args.matrix_cell}"
    if key not in CELLS_BY_KEY:
        print(f"::error::release_evidence: {key!r} is not a declared required cell")
        return 1
    spec = EmitSpec(
        out_root=args.out_root,
        release_sha=args.release_sha,
        workflow_name=args.workflow_name,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        event=args.event,
        job_id=args.job_id,
        matrix_cell=args.matrix_cell,
        terminal_outcome=args.terminal_outcome,
        runner_identity=_optional(args.runner_identity),
        chrome_identity=_optional(args.chrome_identity),
        chrome_launched=args.chrome_launched,
        junit=_optional(args.junit),
        artifacts=_artifact_pairs(args.artifact),
        mq_ids=tuple(args.mq),
    )
    record = build_record(spec)
    out = args.out_root / args.release_sha / args.job_id / f"{args.matrix_cell}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dumps(record), encoding="utf-8", newline="\n")
    problems = validate_record(record, expect_key=key)
    print(f"wrote {out}")
    for problem in problems:
        print(f"::error::release_evidence: {problem}")
    return 1 if problems else 0


def _cmd_validate(args: argparse.Namespace) -> int:
    record = json.loads(args.record.read_text(encoding="utf-8"))
    problems = validate_record(record, expect_key=args.key)
    for problem in problems:
        print(f"::error::release_evidence: {problem}")
    if problems:
        return 1
    print(f"{args.record}: valid {SCHEMA} child record for {args.key}")
    return 0


def _cmd_aggregate(args: argparse.Namespace) -> int:
    spec = AggregateSpec(
        root=args.root,
        release_sha=args.release_sha,
        workflow_name=args.workflow_name,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        event=args.event,
        claims_path=args.claims,
    )
    aggregate, problems = build_aggregate(spec)
    if aggregate:
        out = args.root / args.release_sha / AGGREGATE_JOB / "aggregate.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(dumps(aggregate), encoding="utf-8", newline="\n")
        print(f"wrote {out}")
    for problem in problems:
        print(f"::error::release_evidence: {problem}")
    if problems:
        print(f"::error::release_evidence: {len(problems)} ledger problem(s)")
        return 1
    cells = len(keys_for_tier(tier_for_event(args.event)))
    print(f"aggregate: all {cells} {tier_for_event(args.event)}-tier cells validated")
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    """Print ``<job>=<matrix json>`` lines for ``$GITHUB_OUTPUT`` (T-6910)."""
    tier = tier_for_event(args.event)
    for job, matrix in sorted(plan_matrices(tier).items()):
        print(f"{job.replace('-', '_')}={json.dumps(matrix, separators=(',', ':'))}")
    print(f"tier={tier}")
    return 0


def _cmd_claims(args: argparse.Namespace) -> int:
    claims = load_claims(args.claims)
    problems = validate_claims_document(claims, registry_tool_names())
    for problem in problems:
        print(f"::error::release_evidence: {problem}")
    if problems:
        return 1
    print(dumps(tool_surface(claims)).rstrip())
    return 0


def _cmd_policy(_args: argparse.Namespace) -> int:
    problems = validate_policy()
    for problem in problems:
        print(f"::error::release_evidence: {problem}")
    if problems:
        return 1
    print(
        f"policy: {len(THREAT_CONTRACT)} threat rows over "
        f"{len(THREAT_DIMENSIONS)} required dimensions, "
        f"{len(REDACTION_POLICY)} redaction rules"
    )
    return 0


def _add_emit_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("emit", help="write this cell's child record")
    parser.add_argument("--out-root", type=Path, default=Path("release-evidence"))
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--workflow-name", default="release-gate")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--matrix-cell", required=True)
    parser.add_argument("--terminal-outcome", required=True)
    parser.add_argument("--runner-identity", default="")
    parser.add_argument("--chrome-identity", default="")
    parser.add_argument("--chrome-launched", action="store_true")
    parser.add_argument("--junit", default="")
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--mq", action="append", default=[])
    parser.set_defaults(func=_cmd_emit)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    _add_emit_parser(sub)

    validate = sub.add_parser("validate", help="validate one child record")
    validate.add_argument("--record", type=Path, required=True)
    validate.add_argument("--key", required=True)
    validate.set_defaults(func=_cmd_validate)

    aggregate = sub.add_parser("aggregate", help="build the fail-closed aggregate")
    aggregate.add_argument("--root", type=Path, default=Path("release-evidence"))
    aggregate.add_argument("--release-sha", required=True)
    aggregate.add_argument("--workflow-name", default="release-gate")
    aggregate.add_argument("--run-id", required=True)
    aggregate.add_argument("--run-attempt", required=True)
    aggregate.add_argument("--event", required=True)
    aggregate.add_argument("--claims", type=Path, default=CLAIMS_PATH)
    aggregate.set_defaults(func=_cmd_aggregate)

    plan = sub.add_parser("plan", help="print the matrices this event runs")
    plan.add_argument("--event", required=True)
    plan.set_defaults(func=_cmd_plan)

    claims = sub.add_parser("claims", help="validate the tool claim ledger")
    claims.add_argument("--claims", type=Path, default=CLAIMS_PATH)
    claims.set_defaults(func=_cmd_claims)

    policy = sub.add_parser("policy", help="validate the W12 trust-boundary policy")
    policy.set_defaults(func=_cmd_policy)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
