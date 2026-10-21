"""Child-record construction and validation, JUnit parsing, shared helpers.

Split out of :mod:`release_evidence` (T-5431); that module re-exports every name
here. Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING
from xml.etree import ElementTree

from release_schema import (
    ARTIFACT_KINDS,
    CELL_RE,
    CELLS_BY_KEY,
    HASH_RE,
    IMAGE_OS_RE,
    MQ_RE,
    NODE_RE,
    NUMERIC_RE,
    PYTHON_RE,
    RECORD_KEYS,
    SCHEMA,
    SHA_RE,
    TERMINAL_OUTCOMES,
    VERSION_RE,
    WORKFLOW_EVENTS,
    CellSpec,
)

if TYPE_CHECKING:
    from pathlib import Path


# ── hashing / small helpers ─────────────────────────────────────────────────
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dumps(payload: object) -> str:
    """Deterministic JSON: sorted keys, fixed indent, trailing newline."""
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _sorted_unique(values: list[str]) -> list[str]:
    return sorted(set(values))


def _as_dict(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def _str_field(block: dict[str, object], name: str) -> str:
    value = block.get(name)
    return value if isinstance(value, str) else ""


# ── JUnit parsing ───────────────────────────────────────────────────────────
def _node_id(testcase: ElementTree.Element) -> str:
    """Rebuild pytest's fully-qualified node id from an xunit2 ``<testcase>``."""
    file_attr = (testcase.get("file") or "").replace("\\", "/")
    name = testcase.get("name") or ""
    classname = testcase.get("classname") or ""
    if not file_attr:
        # No file attribute (junit_family=xunit1): fall back to the dotted name.
        return f"{classname}::{name}" if classname else name
    module_dotted = (
        file_attr[:-3].replace("/", ".") if file_attr.endswith(".py") else ""
    )
    if module_dotted and classname.startswith(f"{module_dotted}."):
        inner = classname[len(module_dotted) + 1 :].replace(".", "::")
        return f"{file_attr}::{inner}::{name}"
    return f"{file_attr}::{name}"


def parse_junit(path: Path) -> dict[str, list[str]]:
    """Split a JUnit XML report into executed / skipped / xfail / failed nodes.

    ``executed`` holds every node that ran to a terminal result. A node that was
    collected but never ran (``skipped``) is deliberately absent from it.
    """
    tree = ElementTree.parse(path)  # noqa: S314  PERMANENT(the file is this run's own pytest output; defusedxml is not a permitted new dependency)
    executed: list[str] = []
    skipped: list[str] = []
    xfail: list[str] = []
    failed: list[str] = []
    for testcase in tree.iter("testcase"):
        node = _node_id(testcase)
        skip_el = testcase.find("skipped")
        if skip_el is not None:
            if (skip_el.get("type") or "") == "pytest.xfail":
                xfail.append(node)
                executed.append(node)
            else:
                skipped.append(node)
            continue
        if testcase.find("failure") is not None or testcase.find("error") is not None:
            failed.append(node)
        executed.append(node)
    return {
        "executed_node_ids": _sorted_unique(executed),
        "skipped": _sorted_unique(skipped),
        "xfail": _sorted_unique(xfail),
        "failed": _sorted_unique(failed),
    }


# ── record construction ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class EmitSpec:
    """Everything one cell needs to write its child record."""

    out_root: Path
    release_sha: str
    workflow_name: str
    run_id: str
    run_attempt: str
    event: str
    job_id: str
    matrix_cell: str
    terminal_outcome: str
    runner_identity: Path | None
    chrome_identity: Path | None
    chrome_launched: bool
    junit: Path | None
    artifacts: tuple[tuple[str, Path], ...]
    mq_ids: tuple[str, ...]


def _runner_block(spec: EmitSpec) -> dict[str, object]:
    if spec.runner_identity is None:
        return {"os": "", "arch": "", "image_os": "", "image_version": ""}
    data = json.loads(spec.runner_identity.read_text(encoding="utf-8"))
    return {
        "os": data.get("runner_os", ""),
        "arch": data.get("runner_arch", ""),
        "image_os": data.get("image_os", ""),
        "image_version": data.get("image_version", ""),
    }


def _python_version(spec: EmitSpec) -> str:
    if spec.runner_identity is None:
        return platform.python_version()
    data = json.loads(spec.runner_identity.read_text(encoding="utf-8"))
    value = data.get("python_version", "")
    return value if isinstance(value, str) else ""


def _chrome_block(spec: EmitSpec) -> dict[str, object] | None:
    if spec.chrome_identity is None:
        return None
    data = json.loads(spec.chrome_identity.read_text(encoding="utf-8"))
    version = data.get("version", "")
    version = version if isinstance(version, str) else ""
    launched: int | None = None
    if spec.chrome_launched and version:
        head = version.split(".", 1)[0]
        launched = int(head) if head.isdigit() else None
    return {
        "path": data.get("path", ""),
        "executable_version": version,
        "launched_major": launched,
    }


def _copy_artifacts(spec: EmitSpec) -> list[dict[str, object]]:
    """Copy each cited artifact next to the record and hash the copy.

    The record's ``path`` is relative to the evidence root, so the aggregate can
    re-hash exactly the bytes this cell recorded rather than trusting a path that
    only existed inside one job's workspace.
    """
    dest_dir = spec.out_root / spec.release_sha / spec.job_id / "artifacts"
    dest_dir = dest_dir / spec.matrix_cell
    entries: list[dict[str, object]] = []
    for kind, source in spec.artifacts:
        if not source.is_file():
            print(f"::warning::release_evidence: artifact {source} is absent")
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / source.name
        shutil.copyfile(source, target)
        rel = target.relative_to(spec.out_root / spec.release_sha).as_posix()
        entries.append(
            {
                "name": source.name,
                "path": rel,
                "kind": kind,
                "sha256": sha256_file(target),
            }
        )
    return sorted(entries, key=lambda item: str(item["name"]))


def build_record(spec: EmitSpec) -> dict[str, object]:
    pytest_block: dict[str, object] | None = None
    if spec.junit is not None:
        parsed = parse_junit(spec.junit)
        pytest_block = {"junit_sha256": sha256_file(spec.junit), **parsed}
    return {
        "schema": SCHEMA,
        "release_sha": spec.release_sha,
        "workflow": {
            "name": spec.workflow_name,
            "run_id": spec.run_id,
            "run_attempt": spec.run_attempt,
            "event": spec.event,
        },
        "job": {
            "id": spec.job_id,
            "matrix_cell": spec.matrix_cell,
            "terminal_outcome": spec.terminal_outcome,
        },
        "runner": _runner_block(spec),
        "python_version": _python_version(spec),
        "chrome": _chrome_block(spec),
        "pytest": pytest_block,
        "artifacts": _copy_artifacts(spec),
        "mq_ids": sorted(spec.mq_ids),
    }


# ── record validation ───────────────────────────────────────────────────────
def _validate_workflow(value: object) -> list[str]:
    block = _as_dict(value)
    if block is None:
        return ["workflow: not an object"]
    problems: list[str] = []
    if set(block) != {"name", "run_id", "run_attempt", "event"}:
        problems.append(f"workflow: unexpected field set {sorted(block)}")
    if not _str_field(block, "name"):
        problems.append("workflow.name: empty")
    problems.extend(
        f"workflow.{field}: {block.get(field)!r} is not numeric"
        for field in ("run_id", "run_attempt")
        if not NUMERIC_RE.match(_str_field(block, field))
    )
    if _str_field(block, "event") not in WORKFLOW_EVENTS:
        problems.append(f"workflow.event: {block.get('event')!r} is not a known event")
    return problems


def _validate_job(value: object, *, expect_key: str) -> list[str]:
    block = _as_dict(value)
    if block is None:
        return ["job: not an object"]
    problems: list[str] = []
    if set(block) != {"id", "matrix_cell", "terminal_outcome"}:
        problems.append(f"job: unexpected field set {sorted(block)}")
    job_id = _str_field(block, "id")
    cell = _str_field(block, "matrix_cell")
    if not CELL_RE.match(cell):
        problems.append(f"job.matrix_cell: {cell!r} is malformed")
    key = f"{job_id}/{cell}"
    if key != expect_key:
        problems.append(f"job: record says {key!r} but its path says {expect_key!r}")
    if _str_field(block, "terminal_outcome") not in TERMINAL_OUTCOMES:
        problems.append(
            f"job.terminal_outcome: {block.get('terminal_outcome')!r} is not a "
            f"known outcome"
        )
    return problems


def _validate_runner(value: object, spec: CellSpec) -> list[str]:
    block = _as_dict(value)
    if block is None:
        return ["runner: not an object"]
    problems: list[str] = []
    if set(block) != {"os", "arch", "image_os", "image_version"}:
        problems.append(f"runner: unexpected field set {sorted(block)}")
    if _str_field(block, "os") != spec.runner_os:
        problems.append(
            f"runner.os: {block.get('os')!r} != declared cell {spec.runner_os!r}"
        )
    if _str_field(block, "arch") != spec.runner_arch:
        problems.append(
            f"runner.arch: {block.get('arch')!r} != declared cell {spec.runner_arch!r}"
        )
    if not IMAGE_OS_RE.match(_str_field(block, "image_os")):
        problems.append(
            f"runner.image_os: {block.get('image_os')!r} is not a GitHub-hosted "
            f"image id (a runner without one is outside the qualified matrix)"
        )
    if not _str_field(block, "image_version"):
        problems.append("runner.image_version: empty")
    return problems


def _validate_chrome(value: object, spec: CellSpec) -> list[str]:
    if value is None:
        if spec.expects_chrome:
            return [f"chrome: null on browser cell {spec.key!r}"]
        return []
    block = _as_dict(value)
    if block is None:
        return ["chrome: not an object"]
    problems: list[str] = []
    if not spec.expects_chrome:
        problems.append(f"chrome: recorded on non-browser cell {spec.key!r}")
    if set(block) != {"path", "executable_version", "launched_major"}:
        problems.append(f"chrome: unexpected field set {sorted(block)}")
    if not _str_field(block, "path"):
        problems.append("chrome.path: empty")
    if not VERSION_RE.match(_str_field(block, "executable_version")):
        problems.append(
            f"chrome.executable_version: {block.get('executable_version')!r} is "
            f"not a version"
        )
    launched = block.get("launched_major")
    if spec.expects_launched_chrome and not isinstance(launched, int):
        problems.append(
            f"chrome.launched_major: cell {spec.key!r} must prove a LAUNCHED "
            f"browser, not merely a resolved executable"
        )
    if not spec.expects_launched_chrome and launched is not None:
        problems.append(
            f"chrome.launched_major: cell {spec.key!r} is declared "
            f"non-launching but recorded a launch"
        )
    return problems


def _validate_node_list(block: dict[str, object], field: str) -> list[str]:
    value = block.get(field)
    if not isinstance(value, list):
        return [f"pytest.{field}: not a list"]
    problems: list[str] = []
    nodes = [item for item in value if isinstance(item, str)]
    if len(nodes) != len(value):
        problems.append(f"pytest.{field}: contains a non-string entry")
    if nodes != sorted(nodes):
        problems.append(f"pytest.{field}: not deterministically sorted")
    if len(set(nodes)) != len(nodes):
        problems.append(f"pytest.{field}: contains duplicates")
    problems.extend(
        f"pytest.{field}: {node!r} is not a fully-qualified node id"
        for node in nodes
        if not NODE_RE.match(node)
    )
    return problems


def _validate_pytest(value: object, spec: CellSpec) -> list[str]:
    if value is None:
        if spec.expects_pytest:
            return [f"pytest: null on cell {spec.key!r}, which owes pytest evidence"]
        return []
    block = _as_dict(value)
    if block is None:
        return ["pytest: not an object"]
    problems: list[str] = []
    if not spec.expects_pytest:
        problems.append(f"pytest: recorded on non-pytest cell {spec.key!r}")
    expected = {"junit_sha256", "executed_node_ids", "skipped", "xfail", "failed"}
    if set(block) != expected:
        problems.append(f"pytest: unexpected field set {sorted(block)}")
        return problems
    if not HASH_RE.match(_str_field(block, "junit_sha256")):
        problems.append("pytest.junit_sha256: not a sha256 digest")
    for field in ("executed_node_ids", "skipped", "xfail", "failed"):
        problems.extend(_validate_node_list(block, field))
    return problems


def _validate_artifacts(value: object) -> list[str]:
    if not isinstance(value, list):
        return ["artifacts: not a list"]
    problems: list[str] = []
    names: list[str] = []
    for index, item in enumerate(value):
        entry = _as_dict(item)
        if entry is None:
            problems.append(f"artifacts[{index}]: not an object")
            continue
        if set(entry) != {"name", "path", "kind", "sha256"}:
            problems.append(f"artifacts[{index}]: unexpected field set {sorted(entry)}")
            continue
        names.append(_str_field(entry, "name"))
        if _str_field(entry, "kind") not in ARTIFACT_KINDS:
            problems.append(f"artifacts[{index}].kind: {entry.get('kind')!r} unknown")
        if not HASH_RE.match(_str_field(entry, "sha256")):
            problems.append(f"artifacts[{index}].sha256: not a sha256 digest")
        path = _str_field(entry, "path")
        if not path or path.startswith("/") or ".." in path.split("/"):
            problems.append(f"artifacts[{index}].path: {path!r} is not ledger-relative")
    if names != sorted(names):
        problems.append("artifacts: not deterministically sorted by name")
    if len(set(names)) != len(names):
        problems.append("artifacts: duplicate artifact name")
    return problems


def _validate_mq_ids(value: object) -> list[str]:
    if not isinstance(value, list):
        return ["mq_ids: not a list"]
    ids = [item for item in value if isinstance(item, str)]
    problems: list[str] = []
    if len(ids) != len(value):
        problems.append("mq_ids: contains a non-string entry")
    if len(set(ids)) != len(ids):
        problems.append("mq_ids: duplicate id inside one record")
    if ids != sorted(ids):
        problems.append("mq_ids: not deterministically sorted")
    problems.extend(f"mq_ids: {mq!r} is malformed" for mq in ids if not MQ_RE.match(mq))
    return problems


def validate_record(record: object, *, expect_key: str) -> list[str]:
    """Return every schema violation in one child record (empty == valid)."""
    block = _as_dict(record)
    if block is None:
        return ["record: not a JSON object"]
    spec = CELLS_BY_KEY.get(expect_key)
    if spec is None:
        return [f"record: {expect_key!r} is not a declared required cell"]
    problems: list[str] = []
    missing = RECORD_KEYS - set(block)
    unknown = set(block) - RECORD_KEYS
    if missing:
        problems.append(f"record: missing field(s) {sorted(missing)}")
    if unknown:
        problems.append(f"record: unknown field(s) {sorted(unknown)}")
    if block.get("schema") != SCHEMA:
        problems.append(f"schema: {block.get('schema')!r} != {SCHEMA!r}")
    if not SHA_RE.match(_str_field(block, "release_sha")):
        problems.append(f"release_sha: {block.get('release_sha')!r} is malformed")
    if not PYTHON_RE.match(_str_field(block, "python_version")):
        problems.append(f"python_version: {block.get('python_version')!r} is malformed")
    if spec.python_version and _str_field(block, "python_version") != (
        spec.python_version
    ):
        problems.append(
            f"python_version: {block.get('python_version')!r} != declared cell "
            f"{spec.python_version!r}"
        )
    problems.extend(_validate_workflow(block.get("workflow")))
    problems.extend(_validate_job(block.get("job"), expect_key=expect_key))
    problems.extend(_validate_runner(block.get("runner"), spec))
    problems.extend(_validate_chrome(block.get("chrome"), spec))
    problems.extend(_validate_pytest(block.get("pytest"), spec))
    problems.extend(_validate_artifacts(block.get("artifacts")))
    problems.extend(_validate_mq_ids(block.get("mq_ids")))
    return problems


# ── aggregate ───────────────────────────────────────────────────────────────
