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
import hashlib
import json
import platform
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

SCHEMA = "release-evidence/v1"
CLAIMS_SCHEMA = "release-evidence/v1#tool-claims"
AGGREGATE_JOB = "release-gate"
AGGREGATE_CELL = "aggregate"

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
CLAIMS_PATH = TOOLS_DIR / "release_tool_claims.json"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
NUMERIC_RE = re.compile(r"^[0-9]+$")
CELL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PYTHON_RE = re.compile(r"^3\.\d+(\.\d+)?$")
VERSION_RE = re.compile(r"^\d+(\.\d+)*$")
NODE_RE = re.compile(r"^[^:]+\.py::.+$")
MQ_RE = re.compile(r"^MQ-\d+$")
# Real GitHub-hosted image ids carry hyphens and dots (`ubuntu24`, `macos15`,
# `win25-vs2026`). The check that matters is that there IS one: a self-hosted
# runner exposes no ImageOS at all, and an empty value still fails.
IMAGE_OS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

TERMINAL_OUTCOMES = frozenset({"success", "failure", "cancelled", "skipped"})
WORKFLOW_EVENTS = frozenset(
    {"pull_request", "push", "workflow_dispatch", "schedule", "merge_group", "release"}
)
ARTIFACT_KINDS = frozenset(
    {
        "runner-identity",
        "chrome-identity",
        "coverage",
        "junit",
        "build-manifest",
        "package-verify",
        "install-smoke",
    }
)
TOOL_STATES = ("release-qualified-success", "served-unqualified", "not-served")
TRANSPORTS = frozenset({"stdio", "http"})
CLAIMS_KEYS = frozenset(
    {
        "schema",
        "note",
        "qualified",
        "default_note",
        "served_unqualified_notes",
        "not_served",
    }
)

RECORD_KEYS = frozenset(
    {
        "schema",
        "release_sha",
        "workflow",
        "job",
        "runner",
        "python_version",
        "chrome",
        "pytest",
        "artifacts",
        "mq_ids",
    }
)

# W8 populates this as MQ steps acquire runtime evidence. It is a real check
# today: a declared id that no child records fails the aggregate.
REQUIRED_MQ_IDS: frozenset[str] = frozenset()

# plan_RELEASE §2.1/§2.5: the representative journey is ONE node covering many
# tools. It is release evidence that the wire path works; it is explicitly NOT
# per-tool evidence, so no tool row may cite it.
NON_PER_TOOL_NODES = frozenset(
    {"tests/test_e2e_transport.py::test_real_stdio_release_gate_journey"}
)


@dataclass(frozen=True)
class CellSpec:
    """One required job/matrix cell of the release gate.

    ``expects_pytest``/``expects_chrome``/``expects_launched_chrome`` are the
    fail-closed declarations: a cell that owes pytest evidence cannot satisfy the
    aggregate with ``pytest: null``, and a cell that owes a *launched* browser
    cannot satisfy it by merely resolving the executable on disk.
    """

    job: str
    cell: str
    runner_os: str
    runner_arch: str
    python_version: str
    expects_pytest: bool
    expects_chrome: bool
    expects_launched_chrome: bool
    proves: str

    @property
    def key(self) -> str:
        return f"{self.job}/{self.cell}"

    @property
    def label(self) -> str:
        return f"{self.runner_os}/{self.runner_arch}"


# (cell name, runner.os, runner.arch) for the three qualified runners.
LINUX = ("Linux-X64", "Linux", "X64")
WINDOWS = ("Windows-X64", "Windows", "X64")
MACOS = ("macOS-ARM64", "macOS", "ARM64")
ALL_OS = (LINUX, WINDOWS, MACOS)
# The ubuntu-only jobs run whatever ``python3`` the image provides; they pin no
# interpreter, so their record carries the version that actually ran and the
# validator checks its SHAPE rather than an invented equality.
UNPINNED = ""


def _spec(
    job: str,
    cell: tuple[str, str, str],
    python: str,
    flags: str,
    proves: str,
) -> CellSpec:
    """One cell. ``flags``: ``p`` owes pytest, ``c`` Chrome, ``l`` a LAUNCH."""
    return CellSpec(
        job,
        cell[0],
        cell[1],
        cell[2],
        python,
        expects_pytest="p" in flags,
        expects_chrome="c" in flags,
        expects_launched_chrome="l" in flags,
        proves=proves,
    )


def _build_required_cells() -> tuple[CellSpec, ...]:
    """The exact required job/cell key set the aggregate demands.

    Mirrors ``release-gate.yml``; ``tests/test_release_workflows.py`` asserts the
    two never drift apart, so adding a matrix cell without adding its evidence
    edge is a red test rather than a silent hole.
    """
    one = ("default", "Linux", "X64")
    specs: list[CellSpec] = [
        _spec("quality", one, UNPINNED, "", "lint/type/vulture/owner/budget gates"),
        _spec("known-gaps", one, UNPINNED, "", "the declared gaps, in the check list"),
        _spec("build-dist", one, UNPINNED, "", "the ONE build + its hashed manifest"),
        _spec(
            "package-verify",
            one,
            UNPINNED,
            "",
            "downloaded-bytes re-check + three bite proofs",
        ),
    ]
    specs += [
        _spec(
            "unit-tests",
            (f"{cell[0]}-py{py}", cell[1], cell[2]),
            py,
            "p",
            "hermetic unit suite (`-m 'not integration'`)",
        )
        for py in ("3.11", "3.12", "3.13")
        for cell in ALL_OS
    ]
    specs += [
        _spec(
            "coverage",
            cell,
            "3.12",
            "p",
            "per-OS coverage floor (no merged report hides a red OS)",
        )
        for cell in ALL_OS
    ]
    specs += [
        _spec(
            "integration",
            cell,
            "3.12",
            "pcl",
            "real-Chrome integration suite + Chrome identity"
            + (" MINUS the transport journey (F-773)" if cell is MACOS else ""),
        )
        for cell in ALL_OS
    ]
    specs += [
        _spec(
            "offline-stealth",
            cell,
            "3.12",
            "pcl",
            "offline stealth predicates and their failing controls",
        )
        for cell in ALL_OS
    ]
    specs += [
        _spec(
            "transport",
            cell,
            "3.12",
            "pcl",
            "real-stdio JSON-RPC journey against real Chrome",
        )
        for cell in (LINUX, WINDOWS)
    ]
    specs += [
        _spec(
            "install-smoke",
            (f"{kind}-{cell[0]}", cell[1], cell[2]),
            "3.12",
            "c" if cell is MACOS else "cl",
            f"clean install of the exact {kind} + "
            + ("handshake only (NO navigation, F-773)" if cell is MACOS else "journey"),
        )
        for kind in ("wheel", "sdist")
        for cell in ALL_OS
    ]
    return tuple(specs)


REQUIRED_CELLS: tuple[CellSpec, ...] = _build_required_cells()
CELLS_BY_KEY: dict[str, CellSpec] = {spec.key: spec for spec in REQUIRED_CELLS}
REQUIRED_KEYS: tuple[str, ...] = tuple(sorted(CELLS_BY_KEY))

# A stdio claim is only believable from a job whose selector runs the real-stdio
# lane. Transport-ness is proved by WHICH JOB executed the node, never by a
# hand-written label on the claim.
TRANSPORT_JOBS = frozenset({"transport"})


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


def _discover_children(sha_dir: Path) -> tuple[dict[str, Path], list[str]]:
    """Map ``job/cell`` → record path; report extras and duplicates."""
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
        for key in REQUIRED_KEYS
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


def verify_claims(claims: dict[str, object], index: dict[str, set[str]]) -> list[str]:
    """Check every ``release-qualified-success`` claim against the real ledger.

    This is what stops the contract from claiming a tool the run did not prove:
    the claim's node must have executed AND passed on every cell it names, must
    not be the representative journey, and a ``stdio`` claim must be evidenced by
    a job that actually drives the real-stdio lane.
    """
    problems: list[str] = []
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
    paths, problems_found = _discover_children(sha_dir)
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
    problems.extend(verify_claims(claims, _executed_index(records)))
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
        "required_cells": list(REQUIRED_KEYS),
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


# ── W12: the trust boundary and the ONE redaction policy ────────────────────
#
# plan_RELEASE §2.12 puts this here rather than in a security module of its own:
# "Extend tools/release_evidence.py and the W5 contract generator — do not add a
# parallel policy file/parser." W15's diagnostics import THIS API; a second rule
# table would be a second truth about what counts as a secret.
#
# The framing matters and is load-bearing: this tests the boundary that exists.
# It does not pretend an exec-capable local automation server is a sandbox.

#: The nine dimensions plan_RELEASE §2.12 requires the threat contract to cover.
THREAT_DIMENSIONS: tuple[str, ...] = (
    "caller trust",
    "bind exposure",
    "authentication",
    "host-code execution",
    "browser-code execution",
    "filesystem reads/writes",
    "uploads",
    "downloads",
    "secrets",
)

#: The literal loopback address the HTTP transport binds by default. Asserted
#: against the real argparse default AND against the backend spawn argv, so a
#: change to either is a red test rather than a silent remote exposure.
DEFAULT_BIND_HOST = "127.0.0.1"

#: Every host-Python execution site in ``src/``: caller-supplied text reaching
#: ``exec``/``eval``/``compile`` in the SERVER process, at the server's own
#: privileges. Declared here so a NEW one is a test failure, not a discovery.
#: (module path relative to the package, symbol that receives the caller text).
HOST_EXEC_SITES: tuple[tuple[str, str], ...] = (
    # plan_SERVERSPLIT slice 9: the `exec` moved with create_python_binding's
    # body out of embedded/server.py into the cdp-functions section module. The
    # SITE is unchanged — same tool, same caller text, same host privileges;
    # only the file that holds it moved, which is exactly what this inventory
    # is here to make a reviewed decision rather than a discovery.
    ("embedded/tool_sections/cdp_functions.py", "create_python_binding"),
    ("embedded/dynamic_hook_system.py", "function_code"),
    ("embedded/dynamic_hook_system.py", "custom_condition"),
)

#: Tools whose declared purpose is to run caller-supplied code in the BROWSER.
#: JavaScript here reaches page contexts, not the host interpreter.
BROWSER_EXEC_TOOLS: frozenset[str] = frozenset(
    {
        "call_javascript_function",
        "execute_cdp_command",
        "execute_function_sequence",
        "execute_python_in_browser",
        "execute_script",
        "inject_and_execute_script",
    }
)

#: Tools that reach :data:`HOST_EXEC_SITES` — caller text executed by the host
#: interpreter with the server's privileges. This is intended behaviour under
#: the trusted-caller model; it is a TRUST REQUIREMENT, never an isolation
#: claim, and the contract says so in those words.
HOST_EXEC_TOOLS: frozenset[str] = frozenset(
    {
        "create_dynamic_hook",
        "create_python_binding",
        "create_simple_dynamic_hook",
        "validate_hook_function",
    }
)

#: Tools that write host files at a caller-chosen destination.
FILESYSTEM_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "clone_element_to_file",
        "export_debug_logs",
        "export_network_data",
        "extract_complete_element_to_file",
        "extract_element_animations_to_file",
        "extract_element_assets_to_file",
        "extract_element_events_to_file",
        "extract_element_structure_to_file",
        "extract_element_styles_to_file",
        "take_screenshot",
    }
)

#: Tools that read host files at a caller-chosen source.
FILESYSTEM_READ_TOOLS: frozenset[str] = frozenset(
    {"import_network_data", "upload_file"}
)

#: Name fragments any download tool would have to carry. The contract states the
#: ABSENCE of a download contract; this is what makes that statement checkable
#: instead of merely asserted once and left to rot.
DOWNLOAD_NAME_FRAGMENTS: tuple[str, ...] = ("download", "save_as", "fetch_to_disk")

#: The closed set of secret classes the policy classifies (plan_RELEASE §2.12).
SECRET_CLASSES: tuple[str, ...] = (
    "url-userinfo",
    "url-query-value",
    "authorization-header",
    "cookie-header",
    "environment-canary",
    "dom-form-value",
    "script-argument",
    "sensitive-path-component",
)

#: ``replace`` rewrites the matched span in place; ``drop`` removes the whole
#: mapping entry; ``preserve`` passes a value through untouched.
REDACTION_ACTIONS: frozenset[str] = frozenset({"replace", "drop", "preserve"})

#: Diagnostic fields that MUST survive redaction — a diagnostic that redacts its
#: own error code is not a safer diagnostic, it is a useless one. W15 asserts
#: both halves: every canary gone, every one of these intact.
PRESERVED_DIAGNOSTIC_FIELDS: frozenset[str] = frozenset(
    {
        "correlation_id",
        "error_code",
        "error_type",
        "next_step",
        "phase",
        "tool",
    }
)

#: The bounded replacement format. It carries the CLASS and nothing derived from
#: the secret — no length, no hash, no prefix — so the placeholder itself can
#: never become the disclosure.
REDACTION_PLACEHOLDER = "[redacted:{secret_class}]"

#: Shortest literal a caller may register as a secret. A 1-2 character "secret"
#: would redact half the diagnostic and hide the failure instead of the value.
MIN_SECRET_LENGTH = 4


@dataclass(frozen=True)
class RedactionRule:
    """One classified secret class and what the policy does with it."""

    secret_class: str
    detects: str
    action: str
    rationale: str


REDACTION_POLICY: tuple[RedactionRule, ...] = (
    RedactionRule(
        "url-userinfo",
        "the `user:password@` span of any absolute URL",
        "replace",
        "A proxy or basic-auth URL carries the credential in the host part, so "
        "the URL is unusable as-is but its scheme/host/path are what make the "
        "failure diagnosable.",
    ),
    RedactionRule(
        "url-query-value",
        "every `?k=v` / `&k=v` VALUE; keys are kept",
        "replace",
        "Tokens ride in query values. Keeping the key names preserves the shape "
        "a reader needs without preserving what the value was.",
    ),
    RedactionRule(
        "authorization-header",
        "`authorization` / `proxy-authorization` mapping entries",
        "drop",
        "There is no diagnosable content in a credential header, so the entry "
        "goes rather than being echoed as a placeholder.",
    ),
    RedactionRule(
        "cookie-header",
        "`cookie` / `set-cookie` mapping entries",
        "drop",
        "Session cookies for the user's real logged-in profiles. Same reasoning "
        "as the authorization header.",
    ),
    RedactionRule(
        "environment-canary",
        "caller-registered literal values from the process environment",
        "replace",
        "Environment values cannot be recognised structurally; the caller that "
        "knows a value is a secret registers it, and the policy removes every "
        "occurrence wherever it surfaced.",
    ),
    RedactionRule(
        "dom-form-value",
        "mapping entries holding DOM/form input values",
        "drop",
        "Whatever the user typed into the page — passwords, card numbers, "
        "message bodies. The field NAME is diagnostic; the value never is.",
    ),
    RedactionRule(
        "script-argument",
        "mapping entries holding caller-supplied script/code arguments",
        "drop",
        "`execute_script`/`create_python_binding` arguments routinely carry "
        "credentials the caller pasted into the code it asked us to run.",
    ),
    RedactionRule(
        "sensitive-path-component",
        "the host home directory and account name, in either separator form",
        "replace",
        "A path is the most common accidental identity leak in a stack trace, "
        "and the interesting part of a path is its tail, not its prefix.",
    ),
)

#: Mapping-key fragments that trigger each ``drop`` rule, lower-cased.
DROP_KEY_FRAGMENTS: dict[str, tuple[str, ...]] = {
    "authorization-header": ("authorization",),
    "cookie-header": ("cookie",),
    "dom-form-value": ("form_value", "form_data", "input_value", "dom_value"),
    "script-argument": (
        "script",
        "python_code",
        "function_code",
        "script_args",
        "arguments",
    ),
}

_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/@\s]+)@")
_QUERY_VALUE_RE = re.compile(r"([?&][A-Za-z0-9_.%\[\]-]+=)([^&\s\"'<>]*)")


@dataclass(frozen=True)
class ThreatRow:
    """One dimension of the trust boundary, per transport.

    ``evidence`` is the honest half: it names what actually verifies the row, or
    says in plain words that nothing does. A row that reads "documented" has NOT
    been tested, and the contract prints that distinction rather than blurring
    the two into a single reassuring table.
    """

    dimension: str
    stdio: str
    http: str
    evidence: str


THREAT_CONTRACT: tuple[ThreatRow, ...] = (
    ThreatRow(
        "caller trust",
        "TRUSTED. The client spawns the server as a child process and already "
        "has the user's privileges; there is nothing to escalate.",
        "TRUSTED. Anything that can reach the port is treated as the user.",
        "documented — an untrusted MCP client is OUT OF SCOPE for this "
        "release. No test simulates a hostile caller, because the design "
        "grants a caller everything a hostile one would want.",
    ),
    ThreatRow(
        "bind exposure",
        "No socket. stdin/stdout of the child process only.",
        f"Loopback: the `--host` default is the literal `{DEFAULT_BIND_HOST}`, "
        "and the backend is spawned with that literal host. Remote exposure "
        "requires the user to pass `--host 0.0.0.0` themselves.",
        "TESTED — `tests/test_security_boundary.py::"
        "test_http_bind_defaults_to_literal_loopback` and `::"
        "test_backend_spawn_argv_pins_the_loopback_host` read the real "
        "argparse default and the real spawn argv; `::"
        "test_no_environment_knob_can_change_the_bind_host` proves no "
        "`STEALTH_MCP_*` setting reaches the host.",
    ),
    ThreatRow(
        "authentication",
        "NONE, and none is meaningful: the caller owns the process.",
        "NONE. There is no token, no session auth, no allow-list. The port is "
        "the only boundary, and loopback is the only thing enforcing it.",
        "documented — the gate runs no live HTTP acceptance test at all, so "
        "HTTP stays DESCRIBED, never qualified.",
    ),
    ThreatRow(
        "host-code execution",
        "YES, by design. `create_python_binding`, the two hook-creation tools "
        "and `validate_hook_function` run caller-supplied Python through "
        "`exec`/`eval` IN THE SERVER PROCESS, at the user's privileges.",
        "Identical, reachable by anything that reaches the port.",
        "TESTED as an INVENTORY, not as isolation — `::"
        "test_host_python_execution_sites_are_exactly_the_declared_set` "
        "AST-scans `src/` so a new host-exec site cannot appear unannounced. "
        "No test claims the execution is contained, because it is not.",
    ),
    ThreatRow(
        "browser-code execution",
        "YES, by design: six tools run caller JavaScript (or Python "
        "transpiled to JavaScript) in page contexts, and `execute_cdp_command` "
        "reaches the raw CDP surface.",
        "Identical.",
        "documented — the browser-side containment question needs real Chrome "
        "and is NOT answered here. No isolation property is claimed.",
    ),
    ThreatRow(
        "filesystem reads/writes",
        "UNRESTRICTED within the user's own permissions. Ten tools write to a "
        "caller-chosen destination and two read from a caller-chosen source; "
        "absolute paths and `..` traversal are accepted, not rejected.",
        "Identical.",
        "PARTIALLY TESTED — `::TestFilesystemDestinationMatrix` pins the exact "
        "resolved destination and overwrite behaviour of the three hermetic "
        "paths (`export_network_data`, `import_network_data`, "
        "`export_debug_logs`) across relative, absolute, `..`, mixed-separator "
        "and symlink/junction-escape forms. The seven browser-backed "
        "`*_to_file` tools are NOT covered.",
    ),
    ThreatRow(
        "uploads",
        "`upload_file` hands host files to a page's file input by absolute "
        "path. The page then receives their bytes.",
        "Identical.",
        "documented — needs real Chrome; NOT tested here. Exact-bytes and "
        "exact-name verification remain unproven.",
    ),
    ThreatRow(
        "downloads",
        "NO download tool exists. There is no destination contract, no "
        "completion signal, and no path guarantee for a browser-initiated "
        "download.",
        "Identical.",
        "TESTED — `::test_no_download_tool_is_served` keeps the ABSENCE true "
        "by checking the live registry, so a future download tool cannot land "
        "while the contract still promises there is none.",
    ),
    ThreatRow(
        "secrets",
        "The server holds no credential of its own, but it drives the user's "
        "real logged-in browser profiles and can read anything they can.",
        "Identical, without even a loopback caller check.",
        "TESTED for the DIAGNOSTIC path only — `::TestRedactionPolicy` proves "
        "every one of the eight secret classes is absent from "
        "policy-processed output while error type/code/correlation survive. "
        "It says nothing about what a tool RESULT may contain: results are "
        "returned to the trusted caller verbatim, by design.",
    ),
)


def threat_rows() -> tuple[ThreatRow, ...]:
    """The threat contract — the contract generator's only source for it."""
    return THREAT_CONTRACT


def redaction_rows() -> tuple[RedactionRule, ...]:
    """The redaction policy — one table, imported, never re-declared."""
    return REDACTION_POLICY


def placeholder(secret_class: str) -> str:
    """The bounded replacement for one secret class."""
    if secret_class not in SECRET_CLASSES:
        raise ValueError(f"unknown secret class {secret_class!r}")
    return REDACTION_PLACEHOLDER.format(secret_class=secret_class)


def host_secret_values() -> tuple[tuple[str, str], ...]:
    """The host identity strings the policy always removes.

    Both separator forms, because a Windows diagnostic mixes them freely and a
    policy that only knows one of them leaks through the other.
    """
    import contextlib
    import getpass

    values: list[str] = []
    home = str(Path.home())
    values.extend([home, home.replace("\\", "/")])
    with contextlib.suppress(Exception):  # PERMANENT(no account name is not a failure)
        values.append(getpass.getuser())
    seen: list[tuple[str, str]] = []
    for value in values:
        if len(value) >= MIN_SECRET_LENGTH and value not in {v for _, v in seen}:
            seen.append(("sensitive-path-component", value))
    return tuple(seen)


def _normalise_secrets(
    secrets: object, *, include_host_paths: bool
) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for entry in secrets or ():
        secret_class, value = entry  # type: ignore[misc]
        if secret_class not in SECRET_CLASSES:
            raise ValueError(f"unknown secret class {secret_class!r}")
        if len(value) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"refusing to register a {len(value)}-character secret: a literal "
                f"shorter than {MIN_SECRET_LENGTH} characters would redact the "
                f"diagnostic instead of the value"
            )
        pairs.append((secret_class, value))
    if include_host_paths:
        pairs.extend(host_secret_values())
    # Longest first: a secret that contains another must be replaced whole.
    return tuple(sorted(pairs, key=lambda pair: len(pair[1]), reverse=True))


def redact_text(
    text: str, *, secrets: object = (), include_host_paths: bool = True
) -> str:
    """Apply the canonical policy to one string.

    Literal secrets are removed case-insensitively and longest-first, then the
    structural URL rules run. Both orders are safe: a literal already replaced
    by its placeholder is still matched (and re-replaced) by the query rule, so
    no ordering produces a leak — only a different placeholder.
    """
    result = text
    for secret_class, value in _normalise_secrets(
        secrets, include_host_paths=include_host_paths
    ):
        result = re.sub(
            re.escape(value), placeholder(secret_class), result, flags=re.IGNORECASE
        )
    result = _USERINFO_RE.sub(
        lambda m: f"{m.group(1)}{placeholder('url-userinfo')}@", result
    )
    return _QUERY_VALUE_RE.sub(
        lambda m: f"{m.group(1)}{placeholder('url-query-value')}", result
    )


def _drop_class_for_key(key: str) -> str | None:
    lowered = key.lower()
    for secret_class, fragments in DROP_KEY_FRAGMENTS.items():
        if any(fragment in lowered for fragment in fragments):
            return secret_class
    return None


def redact(
    value: object, *, secrets: object = (), include_host_paths: bool = True
) -> object:
    """THE redactor. Every later diagnostic surface calls this, not its own copy.

    Mappings lose their ``drop``-classified entries entirely, keep
    :data:`PRESERVED_DIAGNOSTIC_FIELDS` byte-for-byte, and recurse everywhere
    else. Strings go through :func:`redact_text`. Numbers and booleans pass.
    """
    if isinstance(value, str):
        return redact_text(
            value, secrets=secrets, include_host_paths=include_host_paths
        )
    if isinstance(value, dict):
        out: dict[object, object] = {}
        for key, item in value.items():
            name = str(key)
            if name.lower() in PRESERVED_DIAGNOSTIC_FIELDS:
                out[key] = item
                continue
            if _drop_class_for_key(name) is not None:
                continue
            out[key] = redact(
                item, secrets=secrets, include_host_paths=include_host_paths
            )
        return out
    if isinstance(value, (list, tuple)):
        return [
            redact(item, secrets=secrets, include_host_paths=include_host_paths)
            for item in value
        ]
    return value


def _validate_threat_contract() -> list[str]:
    problems: list[str] = []
    dimensions = [row.dimension for row in THREAT_CONTRACT]
    missing = [name for name in THREAT_DIMENSIONS if name not in dimensions]
    if missing:
        problems.append(f"threat contract is missing dimension(s) {missing}")
    if len(set(dimensions)) != len(dimensions):
        problems.append("threat contract has a duplicate dimension")
    for row in THREAT_CONTRACT:
        problems.extend(
            f"threat row {row.dimension!r} has an empty {field}"
            for field, text in (("stdio", row.stdio), ("http", row.http))
            if not text.strip()
        )
        if not row.evidence.strip():
            problems.append(f"threat row {row.dimension!r} declares no evidence state")
    return problems


def _validate_redaction_policy() -> list[str]:
    problems: list[str] = []
    classes = [rule.secret_class for rule in REDACTION_POLICY]
    if sorted(classes) != sorted(SECRET_CLASSES):
        problems.append("redaction policy does not cover SECRET_CLASSES exactly")
    if len(set(classes)) != len(classes):
        problems.append("redaction policy has a duplicate secret class")
    for rule in REDACTION_POLICY:
        if rule.action not in REDACTION_ACTIONS:
            problems.append(f"rule {rule.secret_class!r} has action {rule.action!r}")
        if rule.action == "drop" and rule.secret_class not in DROP_KEY_FRAGMENTS:
            problems.append(f"drop rule {rule.secret_class!r} has no key fragments")
        if rule.action == "replace" and rule.secret_class in DROP_KEY_FRAGMENTS:
            problems.append(f"replace rule {rule.secret_class!r} also drops keys")
    return problems


def validate_policy() -> list[str]:
    """Structural problems with the threat contract or the redaction policy."""
    return _validate_threat_contract() + _validate_redaction_policy()


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
    print(f"aggregate: all {len(REQUIRED_KEYS)} required cells validated")
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
