"""The trust boundary this server actually has (plan_RELEASE W12).

These tests exist to keep §6 of `RELEASE_CONTRACT.md` honest, and the honesty
runs in one direction only: **nothing here claims the server is safe against a
hostile caller.** It is not, by design. A caller can run Python in the server
process, drive the user's logged-in browser profiles, and write files as the
user. There is no boundary between the caller and the host, and W12 does not
add one.

What these tests DO pin, and why each is worth a red build:

* the HTTP bind default is the literal loopback address, and no setting can
  move it — because the one thing standing between that unauthenticated port
  and the user's browser is the address it binds to;
* the set of host-Python `exec`/`eval` sites is exactly the declared set — a
  NEW one must be a deliberate, reviewed act, not a discovery;
* no download tool is served — the contract states an ABSENCE, and an absence
  rots silently unless something checks it;
* the canonical redaction policy removes every one of the eight secret classes
  while leaving error type, code and correlation intact;
* the exact resolved destination and overwrite behaviour of the filesystem
  paths reachable WITHOUT a browser.

What they deliberately do NOT cover, so the gap is visible rather than implied:
browser-JS containment, `upload_file` bytes/name, and the seven browser-backed
`*_to_file` destinations. Those need real Chrome; §6 marks each as documented,
not tested.

Every escape probe targets a canary inside the test's own `tmp_path`. Nothing
here touches a real user file, a real profile, or the browser-session root.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import release_evidence as re_mod  # noqa: E402  PERMANENT(tools/ is not an importable package; the sys.path line above must run first)

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = REPO_ROOT / "src" / "stealth_chrome_devtools_mcp"


# ── the policy surface itself ───────────────────────────────────────────────
def test_the_policy_is_structurally_well_formed():
    assert re_mod.validate_policy() == []


def test_the_threat_contract_covers_every_required_dimension():
    """§2.12 names nine dimensions; a table missing one is a table with a hole."""
    covered = [row.dimension for row in re_mod.threat_rows()]
    assert covered == list(re_mod.THREAT_DIMENSIONS)


def test_every_threat_row_declares_whether_a_test_stands_behind_it():
    """The distinction the whole workstream turns on.

    A row must say `TESTED`, `PARTIALLY TESTED`, or `documented`. A row that
    says none of them lets a reader assume coverage that may not exist.
    """
    for row in re_mod.threat_rows():
        assert any(
            token in row.evidence
            for token in ("TESTED", "PARTIALLY TESTED", "documented")
        ), f"threat row {row.dimension!r} does not state its evidence state"


def test_a_tested_threat_row_names_a_node_that_exists_in_this_file():
    """A row may not advertise a test that was renamed or never written."""
    source = Path(__file__).read_text(encoding="utf-8")
    for row in re_mod.threat_rows():
        if "TESTED" not in row.evidence:
            continue
        cited = [
            fragment.split("`")[0].strip()
            for fragment in row.evidence.split("::")[1:]
            if fragment.strip()
        ]
        assert cited, f"threat row {row.dimension!r} says TESTED but names no node"
        for node in cited:
            name = node.split("`")[0].strip()
            assert f"def {name}" in source or f"class {name}" in source, (
                f"threat row {row.dimension!r} cites {name!r}, which does not "
                f"exist in {Path(__file__).name}"
            )


# ── bind exposure ───────────────────────────────────────────────────────────
def _argparse_default(source: Path, flag: str) -> object:
    """The literal default of one `add_argument` call, read from the AST.

    Read statically rather than by building the parser: the server module's
    parser is constructed inside `main()`, and importing-to-run `main()` would
    start a server. The AST is the same bytes the interpreter would see.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if node.args[0].value != flag:
            continue
        for keyword in node.keywords:
            if keyword.arg == "default" and isinstance(keyword.value, ast.Constant):
                return keyword.value.value
    raise AssertionError(f"{source.name} has no {flag} with a literal default")


def test_http_bind_defaults_to_literal_loopback():
    """The default must be the literal address, not a name that could resolve.

    `localhost` is not equivalent: it resolves through the host's name service
    and can answer on an interface the user did not intend. The contract says
    'literal loopback', and this is what makes that word literal.
    """
    default = _argparse_default(PKG_ROOT / "embedded" / "server.py", "--host")
    assert default == re_mod.DEFAULT_BIND_HOST
    assert default not in ("0.0.0.0", "", "::", "localhost")  # noqa: S104  PERMANENT(this is the assertion that we do NOT bind it)


def test_backend_spawn_argv_pins_the_loopback_host():
    """The detached backend is spawned by us, so OUR argv is the real default.

    The argparse default protects a user who runs the server by hand. This
    protects everyone else: the singleton spawns the backend itself, and if it
    passed a wildcard host the argparse default would never be consulted.
    """
    source = (PKG_ROOT / "embedded" / "singleton.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    hosts: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        values = [
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        if "--host" in values:
            hosts.append(values[values.index("--host") + 1])
    assert hosts, "singleton.py builds no backend argv containing --host"
    assert set(hosts) == {re_mod.DEFAULT_BIND_HOST}


#: The one settings field whose name contains "host". It reads the k8s-injected
#: `KUBERNETES_SERVICE_HOST` to detect a container; it is never a bind address.
#: Pinned by name so a genuinely new host knob cannot hide behind it.
ALLOWED_HOST_SETTINGS = frozenset({"kubernetes_service_host"})


def test_no_environment_knob_can_change_the_bind_host():
    """Remote exposure must require an explicit command-line act.

    `settings.py` is the one env home. If it grew a bind-host field, a stray
    `STEALTH_MCP_*` value in a shell profile could publish the port without
    anyone typing `--host`, and §6.1's promise would be false.
    """
    from stealth_chrome_devtools_mcp.settings import Settings

    host_fields = {
        name for name in Settings.model_fields if "host" in name.lower()
    } - ALLOWED_HOST_SETTINGS
    assert not host_fields, (
        f"settings grew host field(s) {sorted(host_fields)}: the bind address "
        "may now be environment-controlled, and §6.1 claims it cannot be"
    )
    server_source = (PKG_ROOT / "embedded" / "server.py").read_text(encoding="utf-8")
    for allowed in ALLOWED_HOST_SETTINGS:
        assert f"get_settings().{allowed}" not in server_source.split("--host")[-1], (
            f"{allowed} now feeds the --host argument"
        )


# ── host-code execution ─────────────────────────────────────────────────────
def _host_exec_calls() -> set[tuple[str, int]]:
    """Every `exec`/`eval`/`compile` CALL in `src/`, as (relative path, line)."""
    found: set[tuple[str, int]] = set()
    for py in sorted(PKG_ROOT.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in ("exec", "eval", "compile"):
                rel = str(py.relative_to(PKG_ROOT)).replace("\\", "/")
                found.add((rel, node.lineno))
    return found


def test_host_python_execution_sites_are_exactly_the_declared_set():
    """A NEW host-exec site must be a reviewed decision, not a surprise.

    This is an inventory assertion and nothing more. It does not claim the
    execution is contained — it is not, and §6 says so. What it buys is that
    the contract's list of tools running host Python cannot silently go stale.
    """
    found = _host_exec_calls()
    declared_modules = {module for module, _ in re_mod.HOST_EXEC_SITES}
    found_modules = {module for module, _ in found}
    assert found_modules == declared_modules, (
        "the set of modules that execute caller Python on the HOST changed.\n"
        f"declared: {sorted(declared_modules)}\nfound:    {sorted(found_modules)}\n"
        "Update release_evidence.HOST_EXEC_SITES and RELEASE_CONTRACT.md §6 in "
        "the same commit, or remove the new site."
    )
    assert len(found) == len(re_mod.HOST_EXEC_SITES), (
        f"expected {len(re_mod.HOST_EXEC_SITES)} host-exec call sites, found "
        f"{len(found)}: {sorted(found)}"
    )


def test_the_declared_exec_tools_are_all_really_served():
    """The threat table names tools; the registry decides what exists."""
    served = set(re_mod.registry_tool_names())
    for label, declared in (
        ("browser", re_mod.BROWSER_EXEC_TOOLS),
        ("host", re_mod.HOST_EXEC_TOOLS),
        ("fs-write", re_mod.FILESYSTEM_WRITE_TOOLS),
        ("fs-read", re_mod.FILESYSTEM_READ_TOOLS),
    ):
        missing = declared - served
        assert not missing, (
            f"{label} inventory names unserved tool(s) {sorted(missing)}"
        )


def test_every_to_file_tool_is_in_the_filesystem_inventory():
    """A new `*_to_file` tool joins the boundary whether or not anyone says so."""
    to_file = {n for n in re_mod.registry_tool_names() if n.endswith("_to_file")}
    assert to_file <= re_mod.FILESYSTEM_WRITE_TOOLS, (
        f"{sorted(to_file - re_mod.FILESYSTEM_WRITE_TOOLS)} write files but are "
        "absent from release_evidence.FILESYSTEM_WRITE_TOOLS"
    )


def test_no_download_tool_is_served():
    """§6.3 states an ABSENCE. An unchecked absence is just an old sentence."""
    offenders = [
        name
        for name in re_mod.registry_tool_names()
        if any(fragment in name for fragment in re_mod.DOWNLOAD_NAME_FRAGMENTS)
    ]
    assert not offenders, (
        f"{offenders} look like download tools, but RELEASE_CONTRACT.md §6.3 "
        "promises there is no download contract. Land the contract change in "
        "the same commit as the tool."
    )


# ── the canonical redaction policy ──────────────────────────────────────────
CANARIES = {
    "url-userinfo": "w12userinfocanary",
    "url-query-value": "w12queryvaluecanary",
    "authorization-header": "w12authheadercanary",
    "cookie-header": "w12cookiecanary",
    "environment-canary": "w12envvaluecanary",
    "dom-form-value": "w12formvaluecanary",
    "script-argument": "w12scriptargcanary",
    "sensitive-path-component": "w12pathcanary",
}


def _diagnostic(home: str) -> dict[str, object]:
    """One realistic diagnostic record carrying every secret class at once."""
    return {
        "error_type": "ToolError",
        "error_code": "E_NAVIGATION_TIMEOUT",
        "correlation_id": "w12-corr-0001",
        "phase": "navigate",
        "tool": "navigate",
        "next_step": "retry with a longer timeout",
        "url": (
            f"https://user:{CANARIES['url-userinfo']}@example.test/p"
            f"?token={CANARIES['url-query-value']}&mode=fast"
        ),
        "request_headers": {
            "Authorization": f"Bearer {CANARIES['authorization-header']}",
            "Proxy-Authorization": f"Basic {CANARIES['authorization-header']}",
            "Cookie": f"sid={CANARIES['cookie-header']}",
            "Set-Cookie": f"sid={CANARIES['cookie-header']}",
            "Accept": "text/html",
        },
        "form_values": {"password": CANARIES["dom-form-value"]},
        "script": f"const key = '{CANARIES['script-argument']}';",
        "python_code": f"SECRET = '{CANARIES['script-argument']}'",
        "message": (
            f"failed under {home} while {CANARIES['environment-canary']} was set"
        ),
        "trace": [
            f"at {home}{os.sep}pkg{os.sep}mod.py",
            # In its structural position — see
            # test_a_bare_token_outside_its_structure_is_not_redacted for what
            # happens when a value escapes the shape the policy can recognise.
            f"retrying https://example.test/p?token={CANARIES['url-query-value']}",
        ],
        "attempts": 3,
        "recovered": False,
    }


class TestRedactionPolicy:
    """Every secret class out; every field a reader needs to act, in."""

    @pytest.fixture
    def processed(self) -> tuple[dict[str, object], str]:
        home = str(Path.home())
        record = _diagnostic(home)
        out = re_mod.redact(
            record,
            secrets=[
                ("environment-canary", CANARIES["environment-canary"]),
                ("sensitive-path-component", CANARIES["sensitive-path-component"]),
                ("script-argument", CANARIES["script-argument"]),
            ],
        )
        return out, json.dumps(out)

    @pytest.mark.parametrize("secret_class", sorted(CANARIES))
    def test_each_secret_class_is_absent_from_processed_output(
        self, secret_class: str, processed: tuple[dict[str, object], str]
    ):
        _, blob = processed
        assert CANARIES[secret_class] not in blob, (
            f"{secret_class} canary survived redaction"
        )

    def test_the_host_home_directory_is_absent(
        self, processed: tuple[dict[str, object], str]
    ):
        """A path prefix is the most common accidental identity leak."""
        _, blob = processed
        assert str(Path.home()) not in blob
        assert str(Path.home()).replace("\\", "/") not in blob

    def test_the_fields_that_make_a_diagnostic_actionable_survive(
        self, processed: tuple[dict[str, object], str]
    ):
        """Redacting the error code would be safe and useless in equal measure."""
        out, _ = processed
        assert out["error_type"] == "ToolError"
        assert out["error_code"] == "E_NAVIGATION_TIMEOUT"
        assert out["correlation_id"] == "w12-corr-0001"
        assert out["phase"] == "navigate"
        assert out["next_step"] == "retry with a longer timeout"

    def test_non_secret_context_survives(
        self, processed: tuple[dict[str, object], str]
    ):
        """The scheme, host and path are what make a URL failure diagnosable."""
        out, blob = processed
        assert "example.test" in blob
        assert "text/html" in blob
        assert out["attempts"] == 3
        assert out["recovered"] is False

    def test_credential_carrying_entries_are_dropped_not_placeheld(
        self, processed: tuple[dict[str, object], str]
    ):
        out, _ = processed
        headers = out["request_headers"]
        assert isinstance(headers, dict)
        assert set(headers) == {"Accept"}
        assert "form_values" not in out
        assert "script" not in out
        assert "python_code" not in out

    def test_a_short_literal_is_refused_rather_than_redacting_everything(self):
        """A 2-character 'secret' would hide the failure, not the value."""
        with pytest.raises(ValueError, match="refusing to register"):
            re_mod.redact_text("anything", secrets=[("environment-canary", "ab")])

    def test_an_unknown_secret_class_is_refused(self):
        with pytest.raises(ValueError, match="unknown secret class"):
            re_mod.redact_text("x", secrets=[("not-a-class", "abcdefgh")])

    def test_the_placeholder_leaks_nothing_about_the_value(self):
        """No length, no hash, no prefix — only the class."""
        short = re_mod.redact_text(
            "abcdefgh", secrets=[("environment-canary", "abcdefgh")]
        )
        long = re_mod.redact_text(
            "abcdefgh" * 40, secrets=[("environment-canary", "abcdefgh" * 40)]
        )
        assert short == long == re_mod.placeholder("environment-canary")

    def test_a_bare_token_outside_its_structure_is_not_redacted(self):
        """The policy's real boundary, asserted rather than discovered later.

        The structural rules recognise a secret by its POSITION — inside URL
        userinfo, after a `?k=`, under a credential-shaped key. A token that has
        escaped that shape, sitting alone in a log line, is indistinguishable
        from a request id, and no rule can classify it. The caller that knows a
        value is secret must register it as a literal.

        This is a limitation, not a defect, and it is why W15's canary discipline
        registers values instead of trusting the structural rules. Pinned here so
        a later reader cannot mistake §6.2's table for "any secret, anywhere".
        """
        bare = "w12barecanaryvalue"
        assert re_mod.redact_text(f"failed: {bare}") == f"failed: {bare}"
        assert (
            re_mod.redact_text(
                f"failed: {bare}", secrets=[("environment-canary", bare)]
            )
            == f"failed: {re_mod.placeholder('environment-canary')}"
        )

    def test_redaction_is_idempotent(self):
        """Re-processing an already-redacted record may not change it again."""
        once = re_mod.redact(_diagnostic(str(Path.home())))
        twice = re_mod.redact(once)
        assert once == twice


# ── the filesystem destination matrix (hermetic paths only) ─────────────────
def _interceptor():
    from stealth_chrome_devtools_mcp.embedded.network_interceptor import (
        NetworkInterceptor,
    )

    return NetworkInterceptor()


def _seed(path: Path) -> None:
    path.write_text(json.dumps({"requests": [], "responses": []}), encoding="utf-8")


class TestFilesystemDestinationMatrix:
    """Where a caller-chosen path actually lands, pinned exactly.

    These tools accept absolute paths and `..` traversal and write wherever the
    host user can write. That is **intended** under the trusted-caller model —
    plan_RELEASE §2.12 says an intentionally-allowed capability is recorded as
    such. What is NOT acceptable is for it to be *assumed*: these tests pin the
    exact resolved destination, so a change in path handling is a red build and
    §6 stays accurate.

    Only paths reachable without a browser are covered. Every probe writes
    inside `tmp_path`.
    """

    def test_a_relative_path_resolves_against_the_process_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        interceptor = _interceptor()
        assert asyncio.run(interceptor.export_to_json("inst", "out.json"))
        assert (tmp_path / "out.json").is_file()

    def test_an_absolute_path_is_honoured_verbatim(self, tmp_path: Path):
        target = tmp_path / "nested" / "abs.json"
        target.parent.mkdir()
        interceptor = _interceptor()
        assert asyncio.run(interceptor.export_to_json("inst", str(target)))
        assert target.is_file()

    def test_dot_dot_traversal_is_accepted_and_escapes_the_given_directory(
        self, tmp_path: Path
    ):
        """Pinned as a CAPABILITY, not asserted as containment.

        The probe deliberately escapes only into `tmp_path`, one level above
        the directory it was handed. Nothing outside the throwaway tree is
        touched — but the point is that nothing stopped it.
        """
        inner = tmp_path / "inner"
        inner.mkdir()
        escaped = tmp_path / "escaped.json"
        interceptor = _interceptor()
        assert asyncio.run(
            interceptor.export_to_json("inst", str(inner / ".." / "escaped.json"))
        )
        assert escaped.is_file(), "traversal did not land where the path pointed"

    def test_mixed_separators_resolve_to_one_destination(self, tmp_path: Path):
        sub = tmp_path / "mixed"
        sub.mkdir()
        mixed = f"{tmp_path}{os.sep}mixed/mixed.json"
        interceptor = _interceptor()
        assert asyncio.run(interceptor.export_to_json("inst", mixed))
        assert (sub / "mixed.json").is_file()

    def test_an_existing_target_is_overwritten_without_warning(self, tmp_path: Path):
        """The overwrite policy, stated: there isn't one. There is no refusal,
        no backup, and no signal in the return value."""
        target = tmp_path / "existing.json"
        target.write_text("PRIOR CONTENT", encoding="utf-8")
        interceptor = _interceptor()
        assert asyncio.run(interceptor.export_to_json("inst", str(target)))
        assert "PRIOR CONTENT" not in target.read_text(encoding="utf-8")

    def test_two_exports_to_the_same_name_leave_one_file(self, tmp_path: Path):
        target = tmp_path / "dup.json"
        interceptor = _interceptor()
        for _ in range(2):
            assert asyncio.run(interceptor.export_to_json("inst", str(target)))
        assert sorted(p.name for p in tmp_path.iterdir()) == ["dup.json"]

    def test_a_symlinked_directory_is_followed_to_its_real_target(self, tmp_path: Path):
        """POSIX symlink / Windows junction-or-symlink, same question.

        Skipped where the OS refuses to create the link at all (unprivileged
        Windows without Developer Mode). A skip here is an absent measurement,
        never a pass: §6 marks the filesystem row PARTIALLY TESTED for exactly
        this kind of reason.
        """
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"this runner cannot create a directory link: {exc}")
        interceptor = _interceptor()
        assert asyncio.run(interceptor.export_to_json("inst", str(link / "via.json")))
        assert (real / "via.json").is_file(), "the link was not followed to its target"

    def test_import_reads_the_exact_absolute_path_it_is_given(self, tmp_path: Path):
        source = tmp_path / "in.json"
        _seed(source)
        interceptor = _interceptor()
        assert asyncio.run(interceptor.import_from_json("inst", str(source)))

    def test_import_traverses_out_of_the_named_directory(self, tmp_path: Path):
        """The read side of the same capability, pinned the same way."""
        source = tmp_path / "outer.json"
        _seed(source)
        inner = tmp_path / "inner"
        inner.mkdir()
        interceptor = _interceptor()
        assert asyncio.run(
            interceptor.import_from_json("inst", str(inner / ".." / "outer.json"))
        )

    def test_importing_a_missing_file_raises_rather_than_returning_false(
        self, tmp_path: Path
    ):
        """A silent False would be the §8.1 'lying success' shape."""
        interceptor = _interceptor()
        with pytest.raises(OSError):
            asyncio.run(
                interceptor.import_from_json("inst", str(tmp_path / "absent.json"))
            )

    def test_debug_log_export_writes_the_caller_named_path(self, tmp_path: Path):
        from stealth_chrome_devtools_mcp.embedded.debug_logger import DebugLogger

        target = tmp_path / "logs" / "debug.json"
        target.parent.mkdir()
        logger = DebugLogger()
        written = logger.export_to_file_paginated(str(target), 1, 1, 1, "json")
        assert Path(written) == target
        assert target.is_file()
