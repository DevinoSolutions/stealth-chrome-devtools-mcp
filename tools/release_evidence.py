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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
