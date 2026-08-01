"""Observability: the Sentry shipper, and what a FAILURE tells you (W15).

Two halves, one home (the conventions lens — there is no second observability
test module):

1. the Sentry error shipper (`src/…/observability.py`) — on by default, aimed at
   a hardcoded DSN, and forbidden to raise (issue #55);
2. **plan_RELEASE §2.15 (W15)** — what the product actually says, writes, and
   leaks when a call fails.

W15's bar is not "an error happened". It is: *can a human act on the failure,
and did acting on it cost the operator a secret?* Those two pull against each
other — richer diagnostics are more actionable and more disclosing — so both
directions are asserted here, and neither is allowed to drift alone.

The honest shape of this file
-----------------------------
Most of what W15 was asked to assert **does not exist in `src/`**, and this file
says so in pins rather than in prose. There is no error code, no failed-phase
field, no next-step field, and no correlation id on any raised error; there is
no inline truncation marker anywhere; there is no atomic write anywhere. Those
are recorded as characterization pins with candidate F-ids
(`audit/stage2/finding_F781..F786_*.md`) and as contract limitations. **Zero
`src/` edits**: a diagnostic gap is characterized, never invented in test prose
and never quietly fixed here.

What is genuinely proved, not merely described:

* every M6-pinned failure message is asserted **byte-for-byte**, including the
  bytes the product echoes back from the caller's own arguments;
* a failing call writes **nothing** to stdout — the stdio transport's framing
  channel stays uncontaminated;
* eight secret canaries are planted in real failing tool calls (URL userinfo and
  query, headers, cookies, environment, DOM/form values, filesystem paths,
  script arguments), and then searched **byte-for-byte** across stderr, stdout,
  the backend log records, the debug-logger view, the policy-processed
  diagnostic, and the local repro bundle. A control node proves the search can
  fail;
* redaction runs through **W12's canonical policy API** (`tools/release_evidence`
  `redact`/`redact_text`/`placeholder`) and the local bundle through **W6's
  writer** (`tools/canary_repro`). W15 adds no second redactor, policy table,
  or destination resolver;
* a sanitized transcript replays from a fresh directory to the same typed
  failure with the same message bytes, with DNS and socket creation blocked and
  the writable surface asserted to be the destination only.

Everything here is hermetic: no Chrome, no network, no real profile. The
deterministic fixture is `tests/fakes.py` (the one harness home).
"""

from __future__ import annotations

import builtins
import json
import locale
import logging
import socket
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from stealth_chrome_devtools_mcp import observability

# ── W12's canonical policy API and W6's bundle writer, imported, never re-declared ──
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import canary_repro as repro  # noqa: E402  PERMANENT(tools/ is not an importable package; the sys.path line above must run first)
import release_evidence as policy  # noqa: E402  PERMANENT(same: tools/ is a script directory, not a package)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ===========================================================================
# The Sentry error shipper — ON by default, hardcoded DSN, never raises (#55)
# ===========================================================================
def test_sentry_init_ships_to_the_hardcoded_dsn_by_default(monkeypatch):
    """No env var, no `.env`, no extra to install: reporting still works."""
    pytest.importorskip("sentry_sdk")
    monkeypatch.delenv("STEALTH_MCP_NO_ERROR_REPORTING", raising=False)
    captured = {}

    def _fake_init(**kwargs):
        captured.update(kwargs)

    with patch("sentry_sdk.init", _fake_init):
        assert observability.sentry_init() is True

    assert captured["dsn"] == observability._DSN
    assert captured["release"] is not None
    names = {type(i).__name__ for i in captured["integrations"]}
    assert "LoggingIntegration" in names
    assert "AsyncioIntegration" in names


def test_the_host_projects_sentry_dsn_is_never_read(monkeypatch):
    """Issue #55: a product repo's own `SENTRY_DSN` is not our configuration.

    The shared backend is launched with the host project's cwd, so reading this
    variable meant shipping THIS tool's errors into somebody else's app project.
    """
    pytest.importorskip("sentry_sdk")
    monkeypatch.setenv("SENTRY_DSN", "https://host-project@example.test/9")
    captured = {}

    with patch("sentry_sdk.init", lambda **kwargs: captured.update(kwargs)):
        assert observability.sentry_init() is True
    assert captured["dsn"] == observability._DSN


def test_the_namespaced_opt_out_disables_initialization(monkeypatch):
    monkeypatch.setenv("STEALTH_MCP_NO_ERROR_REPORTING", "true")

    def _must_not_run(**kwargs):
        raise AssertionError("sentry_sdk.init ran despite the opt-out")

    with patch("sentry_sdk.init", _must_not_run):
        assert observability.sentry_init() is False


def test_sentry_init_returns_false_instead_of_raising_when_the_sdk_is_missing(
    monkeypatch,
):
    """The old contract raised ``RuntimeError`` here and refused to boot.

    Its rationale — "opting in and getting silence is worse than a loud error" —
    died with the opt-in: reporting is now on by default with a bundled SDK, so
    there is no user choice left to betray, only a server that would take itself
    down over its own telemetry.
    """
    monkeypatch.delenv("STEALTH_MCP_NO_ERROR_REPORTING", raising=False)
    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name.startswith("sentry_sdk"):
            raise ImportError("no sentry_sdk")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", _blocked_import):
        assert observability.sentry_init() is False


# ===========================================================================
# plan_RELEASE W15 — observability on failure
# ===========================================================================

# ── The M6-pinned failure bytes. Asserted verbatim; never loosened. ─────────
#: ``_require_tab`` / ``_require_browser``, the one instance-not-found shape.
MSG_INSTANCE_NOT_FOUND = "Instance not found: {instance_id}"
#: ``select_option`` argument validation — echoes the caller's own value back.
MSG_INVALID_INDEX = "Invalid index value: {index}. Must be a number."
#: ``clone_element_complete`` argument validation — echoes the caller's payload.
MSG_INVALID_JSON_OPTIONS = "Invalid JSON in extraction_options: {value}"
#: ``_with_cdp_timeout``. ``{t:.0f}`` renders a sub-second budget as ``0s``.
MSG_CDP_TIMEOUT = (
    "CDP operation timed out after {t:.0f}s{tag}. "
    "The browser may have crashed or the connection dropped. "
    "Try closing the instance with close_instance and spawning a new one."
)
#: ``_script_rejection_reason`` size guard (U+2014 EM DASH is load-bearing).
MSG_SCRIPT_TOO_LARGE = (
    "Script too large ({size} bytes > {limit} limit). Inline payloads such as "
    "base64-encoded files overflow the transport — use the 'upload_file' "
    "tool for files, or a file-based approach."
)
#: ``export_debug_logs`` timeout — a RETURN value, not a raise.
MSG_EXPORT_TIMEOUT = (
    "Export timeout - file too large. Try with smaller limits or 'gzip-pickle' format."
)
#: ``response_handler`` spill envelope — the structural "this was bounded" marker.
MSG_RESPONSE_SPILLED = "Response too large, automatically saved to file"

#: Which pinned messages carry an actionable local next step, and its exact
#: clause. Derived from the product, not authored here: a message mapped to
#: ``None`` genuinely offers the reader nothing to do (see F-781).
RECOVERY_GUIDANCE: dict[str, str | None] = {
    MSG_CDP_TIMEOUT: (
        "Try closing the instance with close_instance and spawning a new one."
    ),
    MSG_SCRIPT_TOO_LARGE: (
        "use the 'upload_file' tool for files, or a file-based approach."
    ),
    MSG_EXPORT_TIMEOUT: "Try with smaller limits or 'gzip-pickle' format.",
    MSG_INSTANCE_NOT_FOUND: None,
    MSG_INVALID_INDEX: None,
    MSG_INVALID_JSON_OPTIONS: None,
}

#: W15's own canaries, one per W12 secret class. Distinct from W12's ``w12*``
#: literals so a hit names which workstream's fixture leaked.
CANARIES: dict[str, str] = {
    "url-userinfo": "w15userinfocanary",
    "url-query-value": "w15queryvaluecanary",
    "authorization-header": "w15authheadercanary",
    "cookie-header": "w15cookiecanary",
    "environment-canary": "w15envvaluecanary",
    "dom-form-value": "w15formvaluecanary",
    "script-argument": "w15scriptargcanary",
    "sensitive-path-component": "w15pathcanary",
}

#: Every canary, registered with its class, as the policy requires. W12's
#: ``test_a_bare_token_outside_its_structure_is_not_redacted`` pins WHY this is
#: mandatory: the structural rules recognise a secret by position, so a caller
#: that knows a value is secret must register it as a literal.
REGISTERED_SECRETS = tuple(CANARIES.items())


# ── the deterministic fixture ──────────────────────────────────────────────
@pytest.fixture()
def w15_server(patched_server):
    """The server module with a seeded hermetic browser_manager (instance ``i1``)."""
    from fakes import FakeBrowserManager, FakeTab

    return patched_server(
        browser_manager=FakeBrowserManager(tabs={"i1": FakeTab()}),
    )


class _RecordCollector(logging.Handler):
    """Collects backend log records with their stamped correlation id."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture()
def backend_records():
    """Yield the list that receives every ``stealth.backend`` record."""
    from stealth_chrome_devtools_mcp.embedded.logging_setup import CorrelationIdFilter

    handler = _RecordCollector()
    handler.addFilter(CorrelationIdFilter())
    logger = logging.getLogger("stealth.backend")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


async def _failure(server_mod: Any, tool: str, **kwargs: Any) -> BaseException:
    """Drive ``tool`` and return the exception it raised. Fails if it does not."""
    from fakes import call_tool

    try:
        result = await call_tool(server_mod, tool, **kwargs)
    except BaseException as exc:  # noqa: BLE001  PERMANENT(W15 characterizes whatever type escapes, including non-ToolError)
        return exc
    raise AssertionError(f"{tool} returned {result!r} instead of failing")


# ---------------------------------------------------------------------------
# MQ-150 — the structured diagnostic oracle
# ---------------------------------------------------------------------------
class TestDiagnosticOracle:
    """Stable type + exact bytes for one representative failure per class."""

    @pytest.mark.asyncio
    async def test_validation_failure_echoes_the_pinned_bytes(self, w15_server):
        exc = await _failure(
            w15_server, "select_option", instance_id="i1", selector="#s", index="abc"
        )
        from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

        assert type(exc) is ToolError
        assert str(exc) == MSG_INVALID_INDEX.format(index="abc")

    @pytest.mark.asyncio
    async def test_payload_validation_failure_echoes_the_pinned_bytes(self, w15_server):
        exc = await _failure(
            w15_server,
            "clone_element_complete",
            instance_id="i1",
            selector="#s",
            extraction_options="{oops",
        )
        from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

        assert type(exc) is ToolError
        assert str(exc) == MSG_INVALID_JSON_OPTIONS.format(value="{oops")

    @pytest.mark.asyncio
    async def test_browser_failure_uses_the_one_instance_not_found_shape(
        self, w15_server
    ):
        exc = await _failure(w15_server, "get_page_content", instance_id="ghost")
        from stealth_chrome_devtools_mcp.embedded.tool_errors import (
            InstanceNotFoundError,
            ToolError,
        )

        assert type(exc) is InstanceNotFoundError
        assert isinstance(exc, ToolError)
        assert str(exc) == MSG_INSTANCE_NOT_FOUND.format(instance_id="ghost")

    @pytest.mark.asyncio
    async def test_timeout_failure_pins_its_exact_bytes(self, w15_server):
        import asyncio

        async def never() -> None:
            await asyncio.sleep(30)

        with pytest.raises(Exception) as caught:  # noqa: B017  PERMANENT(F-783: the timeout path raises bare Exception; narrowing here would hide that)
            await w15_server._with_cdp_timeout(
                never(), timeout=0.01, instance_id="w15-dead"
            )
        assert str(caught.value) == MSG_CDP_TIMEOUT.format(
            t=0.01, tag=" (instance w15-dead)"
        )

    @pytest.mark.asyncio
    async def test_the_timeout_tag_is_omitted_without_an_instance(self, w15_server):
        import asyncio

        async def never() -> None:
            await asyncio.sleep(30)

        with pytest.raises(Exception) as caught:  # noqa: B017  PERMANENT(F-783, as above)
            await w15_server._with_cdp_timeout(never(), timeout=0.01)
        assert str(caught.value) == MSG_CDP_TIMEOUT.format(t=0.01, tag="")

    @pytest.mark.asyncio
    async def test_filesystem_failure_names_the_path_it_could_not_reach(
        self, w15_server, tmp_path
    ):
        missing = tmp_path / "absent-dir" / "capture.json"
        exc = await _failure(
            w15_server, "import_network_data", instance_id="i1", filepath=str(missing)
        )
        assert isinstance(exc, FileNotFoundError)
        # The unreachable path is the only actionable content the failure has.
        # Assert it via ``filename`` and a separator-free component: ``str(exc)``
        # repr-escapes the path, so a Windows message carries DOUBLED
        # backslashes and a raw ``str(path)`` substring can never match there.
        assert exc.filename == str(missing)
        assert "capture.json" in str(exc)
        assert MSG_INSTANCE_NOT_FOUND[:8] not in str(exc)

    def test_the_oversize_script_guard_pins_its_exact_bytes(self, w15_server):
        limit = w15_server.MAX_USER_SCRIPT_BYTES
        oversized = "x" * (limit + 1)
        assert w15_server._script_rejection_reason(
            oversized
        ) == MSG_SCRIPT_TOO_LARGE.format(size=limit + 1, limit=limit)


class TestCorrelation:
    """Where the correlation id exists, and where it does not."""

    @pytest.mark.asyncio
    async def test_a_failing_call_still_stamps_one_id_on_its_log_pair(
        self, w15_server, backend_records
    ):
        await _failure(w15_server, "get_page_content", instance_id="ghost")
        pair = [r for r in backend_records if "get_page_content" in r.getMessage()]
        assert len(pair) == 2, "a failing call must still emit its start/end pair"
        ids = {r.correlation_id for r in pair}
        assert len(ids) == 1, "start and end must share one id"
        (only,) = ids
        assert len(only) == 12 and int(only, 16) >= 0, only

    @pytest.mark.asyncio
    async def test_each_call_gets_a_distinct_id(self, w15_server, backend_records):
        await _failure(w15_server, "get_page_content", instance_id="ghost")
        await _failure(w15_server, "get_page_content", instance_id="ghost2")
        ids = {r.correlation_id for r in backend_records}
        assert len(ids) == 2

    @pytest.mark.asyncio
    async def test_the_context_var_is_reset_after_a_failure(self, w15_server):
        from stealth_chrome_devtools_mcp.embedded.logging_setup import (
            correlation_id_var,
        )

        await _failure(w15_server, "get_page_content", instance_id="ghost")
        assert correlation_id_var.get() == "-"

    @pytest.mark.characterization
    @pytest.mark.asyncio
    async def test_the_raised_error_carries_no_correlation_id(self, w15_server):
        """route:F-781 — correlation never reaches the caller.

        The id is set on a contextvar for the duration of the call and stamped
        onto log records only. The exception that the MCP client actually
        receives carries no id, so a user reporting a failure cannot quote the
        one token that would find it in the backend log.
        """
        exc = await _failure(w15_server, "get_page_content", instance_id="ghost")
        assert not hasattr(exc, "correlation_id")
        assert vars(exc) == {}
        assert exc.args == (MSG_INSTANCE_NOT_FOUND.format(instance_id="ghost"),)

    @pytest.mark.characterization
    @pytest.mark.asyncio
    async def test_a_failed_call_logs_no_error_record(
        self, w15_server, backend_records
    ):
        """route:F-782 — the failure itself is never logged.

        ``with_correlation_id`` wraps the call in ``try/finally`` with no
        ``except``, so the INFO ``end`` line is indistinguishable from a
        success. Nothing at WARNING or above records that the call failed, or
        what it failed with.
        """
        await _failure(w15_server, "get_page_content", instance_id="ghost")
        assert [r for r in backend_records if r.levelno >= logging.WARNING] == []
        assert [r.getMessage() for r in backend_records] == [
            "tool get_page_content start",
            backend_records[-1].getMessage(),
        ]
        assert backend_records[-1].getMessage().startswith("tool get_page_content end")


class TestErrorConventionGaps:
    """The fields W15 was asked to assert, and which the product does not have."""

    @pytest.mark.characterization
    def test_the_error_types_carry_no_code_phase_or_next_step(self):
        """route:F-781 — ToolError is a bare Exception.

        No error code, no failed phase, no next step, no structured payload of
        any kind. A caller can only string-match the message, which is why
        every message in this file is pinned byte-for-byte.
        """
        from stealth_chrome_devtools_mcp.embedded.tool_errors import (
            InstanceNotFoundError,
            ToolError,
        )

        err = ToolError("boom")
        assert vars(err) == {}
        for field in ("code", "error_code", "phase", "next_step", "correlation_id"):
            assert not hasattr(err, field), field
        assert ToolError.__mro__[1] is Exception
        assert InstanceNotFoundError.__mro__[1] is ToolError

    @pytest.mark.characterization
    @pytest.mark.asyncio
    async def test_the_timeout_path_escapes_the_one_error_convention(self, w15_server):
        """route:F-783 — ``_with_cdp_timeout`` raises a bare ``Exception``.

        CLAUDE.md convention 2 says tools raise ``ToolError``. The CDP timeout
        path — the single most likely runtime failure — raises the base class,
        so a client cannot tell a timeout from an interpreter bug by type.
        """
        import asyncio

        from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

        async def never() -> None:
            await asyncio.sleep(30)

        with pytest.raises(Exception) as caught:  # noqa: B017  PERMANENT(that the type is exactly Exception IS the finding)
            await w15_server._with_cdp_timeout(never(), timeout=0.01)
        assert type(caught.value) is Exception
        assert not isinstance(caught.value, ToolError)

    @pytest.mark.characterization
    @pytest.mark.asyncio
    async def test_filesystem_paths_leak_raw_stdlib_exceptions(
        self, w15_server, tmp_path
    ):
        """route:F-784 — the filesystem tools never wrap their failures.

        ``import_network_data`` / ``export_network_data`` let ``OSError`` and
        ``json.JSONDecodeError`` escape verbatim. The message is the stdlib's,
        so it carries an absolute host path (see MQ-151) and no next step.
        """
        from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

        missing = tmp_path / "absent-dir" / "capture.json"
        exc = await _failure(
            w15_server, "import_network_data", instance_id="i1", filepath=str(missing)
        )
        assert isinstance(exc, OSError)
        assert not isinstance(exc, ToolError)

        malformed = tmp_path / "malformed.json"
        malformed.write_text("not json", encoding="utf-8")
        exc = await _failure(
            w15_server, "import_network_data", instance_id="i1", filepath=str(malformed)
        )
        assert isinstance(exc, json.JSONDecodeError)
        assert not isinstance(exc, ToolError)

    @pytest.mark.characterization
    @pytest.mark.asyncio
    async def test_cancellation_propagates_unconverted(self, w15_server):
        """route:F-783 — nothing turns ``CancelledError`` into a tool failure.

        A cancelled call raises ``asyncio.CancelledError`` (a ``BaseException``)
        straight through the correlation wrapper. The wrapper's ``finally``
        still resets the contextvar, which is the one thing that must hold.
        """
        import asyncio

        from stealth_chrome_devtools_mcp.embedded.logging_setup import (
            correlation_id_var,
        )
        from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

        async def cancelled() -> None:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError) as caught:
            await w15_server._with_cdp_timeout(cancelled(), timeout=5)
        assert not isinstance(caught.value, ToolError)
        assert correlation_id_var.get() == "-"


class TestStdoutPurity:
    """stdout is the stdio transport's framing channel and must stay empty."""

    @pytest.mark.asyncio
    async def test_a_burst_of_failures_writes_nothing_to_stdout(
        self, w15_server, tmp_path, capsys
    ):
        capsys.readouterr()
        await _failure(
            w15_server, "select_option", instance_id="i1", selector="#s", index="abc"
        )
        await _failure(w15_server, "get_page_content", instance_id="ghost")
        await _failure(
            w15_server,
            "clone_element_complete",
            instance_id="i1",
            selector="#s",
            extraction_options="{oops",
        )
        await _failure(
            w15_server,
            "import_network_data",
            instance_id="i1",
            filepath=str(tmp_path / "absent" / "x.json"),
        )
        captured = capsys.readouterr()
        assert captured.out == "", f"stdout contaminated: {captured.out!r}"
        assert captured.err == "", f"stderr contaminated: {captured.err!r}"


# ---------------------------------------------------------------------------
# MQ-151 — secret canaries
# ---------------------------------------------------------------------------
def _build_record(
    failures: list[tuple[str, BaseException, dict[str, Any]]],
) -> dict[str, Any]:
    """The diagnostic a capture layer would build, carrying all eight classes."""
    return {
        # the fields the policy must PRESERVE
        "error_type": type(failures[0][1]).__name__,
        "error_code": "E_W15_SYNTHETIC",
        "correlation_id": "w15-corr-0001",
        "phase": "invoke",
        "tool": failures[0][0],
        "next_step": "re-run the sanitized transcript locally",
        # the fields the policy must SCRUB
        "url": (
            f"https://svc:{CANARIES['url-userinfo']}@example.test/p"
            f"?token={CANARIES['url-query-value']}&mode=fast"
        ),
        "request_headers": {
            "Authorization": f"Bearer {CANARIES['authorization-header']}",
            "Cookie": f"sid={CANARIES['cookie-header']}",
            "Accept": "text/html",
        },
        "form_values": {"password": CANARIES["dom-form-value"]},
        "script": f"const k = '{CANARIES['script-argument']}';",
        "environment": {"W15_CANARY_ENV": CANARIES["environment-canary"]},
        "call_kwargs": [dict(kwargs) for _, _, kwargs in failures],
        "messages": [str(exc) for _, exc, _ in failures],
    }


class TestSecretCanaries:
    """Any unauthorized disclosure here is a RELEASE BLOCKER, never a limitation."""

    @pytest.fixture()
    def record(self, w15_server, tmp_path, monkeypatch):
        import asyncio

        monkeypatch.setenv("W15_CANARY_ENV", CANARIES["environment-canary"])
        missing = tmp_path / CANARIES["sensitive-path-component"] / "capture.json"

        async def _drive():
            specs = (
                (
                    "clone_element_complete",
                    {
                        "instance_id": "i1",
                        "selector": "#s",
                        "extraction_options": "{token: " + CANARIES["url-query-value"],
                    },
                ),
                (
                    "select_option",
                    {
                        "instance_id": "i1",
                        "selector": "#s",
                        "index": CANARIES["dom-form-value"],
                    },
                ),
                (
                    "get_page_content",
                    {"instance_id": "ghost-" + CANARIES["environment-canary"]},
                ),
                (
                    "import_network_data",
                    {"instance_id": "i1", "filepath": str(missing)},
                ),
            )
            return [
                (tool, await _failure(w15_server, tool, **kw), kw) for tool, kw in specs
            ]

        return _build_record(asyncio.run(_drive()))

    def test_the_control_proves_the_search_can_fail(self, record):
        """Without this, every canary assertion below could be vacuously green."""
        blob = json.dumps(record)
        missing = [name for name, value in CANARIES.items() if value not in blob]
        assert missing == [], f"unplanted canaries: {missing}"

    @pytest.mark.parametrize("secret_class", sorted(CANARIES))
    def test_no_canary_survives_the_canonical_policy(self, record, secret_class):
        """RELEASE BLOCKER if red — a real secret would take this same path."""
        out = policy.redact(record, secrets=REGISTERED_SECRETS)
        assert CANARIES[secret_class] not in json.dumps(out), (
            f"{secret_class} canary survived W12's policy"
        )

    def test_the_actionable_fields_survive_the_policy(self, record):
        """A diagnostic that redacts its own error code is useless, not safer."""
        out = policy.redact(record, secrets=REGISTERED_SECRETS)
        for field in sorted(policy.PRESERVED_DIAGNOSTIC_FIELDS):
            if field in record:
                assert out[field] == record[field], field
        assert out["error_code"] == "E_W15_SYNTHETIC"
        assert out["correlation_id"] == "w15-corr-0001"
        assert out["phase"] == "invoke"
        assert out["next_step"] == "re-run the sanitized transcript locally"

    def test_the_structural_rules_catch_the_url_classes_unregistered(self, record):
        """URL userinfo and query values are recognised by POSITION, not literal."""
        out = policy.redact(record, secrets=())
        url = out["url"]
        assert CANARIES["url-userinfo"] not in url
        assert CANARIES["url-query-value"] not in url
        assert policy.placeholder("url-userinfo") in url
        assert policy.placeholder("url-query-value") in url
        # …and the diagnosable part of the URL is still there.
        assert "example.test" in url

    def test_credential_entries_are_dropped_entirely(self, record):
        out = policy.redact(record, secrets=REGISTERED_SECRETS)
        assert set(out["request_headers"]) == {"Accept"}
        assert "form_values" not in out
        assert "script" not in out

    @pytest.mark.asyncio
    async def test_no_canary_reaches_stdout_stderr_or_the_backend_log(
        self, w15_server, tmp_path, monkeypatch, capsys, backend_records
    ):
        monkeypatch.setenv("W15_CANARY_ENV", CANARIES["environment-canary"])
        capsys.readouterr()
        missing = tmp_path / CANARIES["sensitive-path-component"] / "capture.json"
        await _failure(
            w15_server,
            "select_option",
            instance_id="i1",
            selector="#s",
            index=CANARIES["dom-form-value"],
        )
        await _failure(
            w15_server,
            "get_page_content",
            instance_id="ghost-" + CANARIES["environment-canary"],
        )
        await _failure(
            w15_server, "import_network_data", instance_id="i1", filepath=str(missing)
        )
        captured = capsys.readouterr()
        log_text = "\n".join(r.getMessage() for r in backend_records)
        view_text = json.dumps(w15_server.debug_logger.get_debug_view(), default=str)

        for surface, text in (
            ("stdout", captured.out),
            ("stderr", captured.err),
            ("backend-log", log_text),
            ("debug-view", view_text),
        ):
            leaked = [n for n, v in CANARIES.items() if v in text]
            assert leaked == [], f"RELEASE BLOCKER: {leaked} disclosed via {surface}"

    def test_the_bundle_writer_refuses_a_canary_bearing_value(self, tmp_path):
        """W6's closed surface, not a filter, is what keeps a secret out."""
        with pytest.raises(repro.RefusedError, match="not a synthetic id"):
            repro.build_record(
                call_ids=[f"call {CANARIES['script-argument']}"],
                fixture_refs=[],
                oracles=[],
                runner_identity=None,
                chrome_identity=None,
            )
        with pytest.raises(repro.RefusedError, match="never live-site content"):
            repro.build_record(
                call_ids=[],
                fixture_refs=[f"https://svc:{CANARIES['url-userinfo']}@example.test/p"],
                oracles=[],
                runner_identity=None,
                chrome_identity=None,
            )

    def test_no_canary_reaches_the_written_bundle(self, tmp_path):
        record = repro.build_record(
            call_ids=["w15-call-0001"],
            fixture_refs=["tests/fakes.py"],
            oracles=["error_type=InstanceNotFoundError"],
            runner_identity=None,
            chrome_identity=None,
        )
        destination = repro.resolve_destination(
            tmp_path / "bundle",
            repo_root=REPO_ROOT,
            home=Path.home(),
            cwd=Path.cwd(),
        )
        destination.mkdir(parents=True)
        target = destination / "canary-repro.json"
        target.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", "utf-8")
        blob = target.read_text(encoding="utf-8")
        leaked = [n for n, v in CANARIES.items() if v in blob]
        assert leaked == [], f"RELEASE BLOCKER: {leaked} disclosed via the bundle"


# ---------------------------------------------------------------------------
# MQ-152 — bounded capture
# ---------------------------------------------------------------------------
class TestBoundedCapture:
    """How much a diagnostic may carry, and how the boundary is signalled."""

    def _handler(self, tmp_path, max_tokens: int):
        from stealth_chrome_devtools_mcp.embedded.response_handler import (
            ResponseHandler,
        )

        return ResponseHandler(max_tokens=max_tokens, clone_dir=str(tmp_path / "spill"))

    def test_an_oversized_payload_is_replaced_by_a_bounded_envelope(self, tmp_path):
        handler = self._handler(tmp_path, max_tokens=10)
        payload = {"dom": "<div>" + ("x" * 20_000) + "</div>"}
        envelope = handler.handle_response(payload, fallback_filename_prefix="w15")

        assert sorted(envelope) == [
            "estimated_tokens",
            "file_path",
            "file_size_kb",
            "filename",
            "metadata",
            "reason",
        ]
        assert envelope["reason"] == MSG_RESPONSE_SPILLED
        # The envelope itself is the bound: orders of magnitude under the payload.
        assert len(json.dumps(envelope)) < len(json.dumps(payload)) // 10

    def test_a_payload_within_budget_passes_through_untouched(self, tmp_path):
        handler = self._handler(tmp_path, max_tokens=20_000)
        payload = {"dom": "<div>ok</div>"}
        assert handler.handle_response(payload) is payload

    def test_the_spilled_bytes_are_valid_utf8(self, tmp_path):
        handler = self._handler(tmp_path, max_tokens=10)
        payload = {"dom": "café \U0001f600 مرحبا" * 400}
        envelope = handler.handle_response(payload)
        raw = Path(envelope["file_path"]).read_bytes()
        # Declared UTF-8 + ensure_ascii=False -> real multibyte bytes, decodable.
        assert raw.decode("utf-8")
        assert b"\xc3\xa9" in raw, "non-ASCII was escaped away instead of encoded"
        assert json.loads(raw.decode("utf-8"))["data"] == payload

    def test_the_debug_view_reports_total_versus_returned(self):
        """The counter pair is the only "you are seeing a subset" signal there is.

        An isolated logger, not the module global: writing into the shared
        singleton from a test both pollutes every later reader of it and makes
        the assertion depend on how much the rest of the suite happened to log.
        """
        from stealth_chrome_devtools_mcp.embedded.debug_logger import DebugLogger

        isolated = DebugLogger()
        for index in range(5):
            isolated.log_info("w15", "bounded", f"entry-{index}")
        summary = isolated.get_debug_view_paginated(max_info=2)["summary"]
        assert summary["total_info"] == 5
        assert summary["returned_info"] == 2
        assert summary["returned_info"] < summary["total_info"]

    def test_the_per_field_and_total_bundle_caps_are_enforced(self, tmp_path):
        """W6's writer is the only bounded local diagnostic budget that exists."""
        assert repro.MAX_VALUE_CHARS == 256
        assert repro.MAX_ENTRIES == 200
        assert repro.MAX_IDENTITY_KEYS == 64

        with pytest.raises(repro.RefusedError, match="over the 256-char cap"):
            repro.build_record(
                call_ids=[],
                fixture_refs=[],
                oracles=["k=" + "x" * (repro.MAX_VALUE_CHARS + 1)],
                runner_identity=None,
                chrome_identity=None,
            )
        with pytest.raises(repro.RefusedError, match="over the 200 cap"):
            repro.build_record(
                call_ids=[f"c-{i}" for i in range(repro.MAX_ENTRIES + 1)],
                fixture_refs=[],
                oracles=[],
                runner_identity=None,
                chrome_identity=None,
            )

    @pytest.mark.characterization
    def test_there_is_no_total_byte_budget_across_the_whole_record(self):
        """route:F-785 — the total is a PRODUCT of per-field caps, not a cap.

        A record at the per-field and per-list maxima is accepted whole. Nothing
        bounds the serialized size of the diagnostic itself, so the real ceiling
        is ``MAX_ENTRIES x MAX_VALUE_CHARS`` per list rather than a declared
        total. Pinned so a future 'just raise MAX_ENTRIES' cannot pass unnoticed.
        """
        value = "v" * (repro.MAX_VALUE_CHARS - 2)
        record = repro.build_record(
            call_ids=[f"c-{i}" for i in range(repro.MAX_ENTRIES)],
            fixture_refs=[],
            oracles=[f"k{i}={value}" for i in range(repro.MAX_ENTRIES)],
            runner_identity=None,
            chrome_identity=None,
        )
        serialized = json.dumps(record)
        assert len(serialized) > 50_000, len(serialized)
        assert len(record["oracles"]) == repro.MAX_ENTRIES

    @pytest.mark.characterization
    def test_no_diagnostic_surface_emits_an_inline_truncation_marker(self, tmp_path):
        """route:F-785 — boundedness is structural, never an in-band marker.

        §2.15 asks for "explicit truncation markers/checksums". There are none.
        Each surface signals its bound differently and none of them appends a
        marker to the value: the response handler swaps the payload for an
        envelope, the network interceptor sets the body to ``None``, and the
        debug view emits a total/returned counter pair. A reader holding only a
        truncated value cannot tell that it was truncated, and no checksum of
        the dropped content is recorded anywhere.
        """
        handler = self._handler(tmp_path, max_tokens=10)
        envelope = handler.handle_response({"dom": "x" * 20_000})
        rendered = json.dumps(envelope)
        for marker in ("truncat", "[...]", "…", "elided", "sha256", "checksum"):
            assert marker not in rendered.lower(), marker
        # The bound is signalled by REPLACEMENT, and the original is on disk.
        assert "x" * 100 not in rendered
        assert Path(envelope["file_path"]).exists()

    @pytest.mark.characterization
    def test_the_json_exports_are_ascii_by_default_not_declared_utf8(self, tmp_path):
        """route:F-785 — UTF-8 validity here is an accident of ``json.dumps``.

        ``network_interceptor.export_to_json`` and ``debug_logger._export_json``
        open their target with **no** ``encoding=``, so the bytes are whatever
        the platform default is (cp1252 on a default Windows runner). They are
        nonetheless always valid UTF-8 because ``json.dump`` defaults to
        ``ensure_ascii=True`` and escapes every non-ASCII code point. The
        property holds, but it is not declared — passing ``ensure_ascii=False``
        would break it on exactly one of the three gate OSes.

        ``fmt="json"`` is passed explicitly. The default ``"auto"`` switches to
        binary pickle above 100 buffered items and gzip-pickle above 1000, so on
        the shared singleton this write site is reached only while the process
        happens to be lightly logged — which is itself worth knowing, and is why
        this test owns an isolated logger rather than the global one.
        """
        from stealth_chrome_devtools_mcp.embedded.debug_logger import DebugLogger

        target = tmp_path / "debug.json"
        isolated = DebugLogger()
        isolated.log_info("w15", "unicode", "café \U0001f600")
        isolated.export_to_file_paginated(str(target), fmt="json")

        raw = target.read_bytes()
        assert raw.decode("utf-8"), "export must at least be UTF-8 decodable"
        assert raw.decode("ascii"), "…and today it is pure ASCII, by escaping"
        assert b"caf\\u00e9" in raw, "non-ASCII must have been escaped, not encoded"
        assert "café".encode() not in raw, "…so no real multibyte UTF-8 is present"
        # Recorded, never asserted equal: the finding is the missing declaration,
        # not the value the platform happens to default to.
        assert locale.getpreferredencoding(False)

    @pytest.mark.characterization
    def test_no_local_artifact_write_is_atomic(self):
        """route:F-786 — there is no write-then-rename anywhere.

        §2.15 asks for "atomic local writes". The bundle writer has no temp
        file and no ``os.replace``; a crash mid-``write_text`` leaves a partial
        JSON file. What it DOES guarantee is weaker but real and already pinned
        by ``tests/test_canary_repro.py``: every check precedes all I/O, so a
        REFUSAL leaves nothing behind. Only the absence of atomicity is pinned
        here — the refusal path is not re-tested.
        """
        source = Path(repro.__file__).read_text(encoding="utf-8")
        assert "os.replace" not in source
        assert "tempfile" not in source
        assert "fsync" not in source
        assert "Never a partial write: checks precede I/O." in source


# ---------------------------------------------------------------------------
# MQ-153 — environment and recovery guidance
# ---------------------------------------------------------------------------
class TestRecoveryGuidance:
    """Which failures tell the reader what to do next, and which do not."""

    @pytest.mark.parametrize(
        "template",
        [t for t, clause in RECOVERY_GUIDANCE.items() if clause is not None],
    )
    def test_the_guided_messages_carry_their_exact_clause(self, template):
        clause = RECOVERY_GUIDANCE[template]
        assert clause is not None
        assert clause in template

    def test_the_timeout_names_a_local_recoverable_action(self, w15_server):
        rendered = MSG_CDP_TIMEOUT.format(t=30, tag="")
        assert "close_instance" in rendered
        # The named tool must actually exist, or the advice is a dead end.
        assert hasattr(w15_server, "close_instance")

    def test_the_script_guard_names_the_tool_to_use_instead(self, w15_server):
        rendered = MSG_SCRIPT_TOO_LARGE.format(size=200_001, limit=100_000)
        assert "'upload_file'" in rendered
        assert hasattr(w15_server, "upload_file")

    def test_the_export_timeout_names_its_alternative_format(self, w15_server):
        assert "gzip-pickle" in MSG_EXPORT_TIMEOUT
        import inspect

        assert MSG_EXPORT_TIMEOUT in inspect.getsource(w15_server)

    @pytest.mark.characterization
    def test_the_commonest_failures_offer_no_next_step(self):
        """route:F-781 — the three highest-traffic messages are dead ends.

        ``Instance not found`` is what a user hits after any browser death, and
        it names no recovery: not ``list_instances``, not ``spawn_browser``,
        nothing. The two validation messages echo the bad value but never say
        what a good one looks like. Recorded as a contract limitation.
        """
        unguided = sorted(t for t, c in RECOVERY_GUIDANCE.items() if c is None)
        assert unguided == sorted(
            [MSG_INSTANCE_NOT_FOUND, MSG_INVALID_INDEX, MSG_INVALID_JSON_OPTIONS]
        )
        for template in unguided:
            lowered = template.lower()
            for verb in ("try ", "run ", "use ", "see ", "check "):
                assert verb not in lowered, (template, verb)


# ---------------------------------------------------------------------------
# MQ-154 — local replay, and no external mutation
# ---------------------------------------------------------------------------
TRANSCRIPT_SCHEMA = "w15-transcript/1"


def _sanitized_transcript(tool: str, kwargs: dict[str, Any], exc: BaseException):
    """A replayable transcript with every value already through W12's policy."""
    return policy.redact(
        {
            "schema": TRANSCRIPT_SCHEMA,
            "tool": tool,
            "error_type": type(exc).__name__,
            "call_kwargs": dict(kwargs),
            "expected_message": str(exc),
        },
        secrets=REGISTERED_SECRETS,
    )


class TestLocalReplay:
    """From a fresh directory, offline, to the same typed failure."""

    @pytest.fixture()
    def transcript_dir(self, w15_server, tmp_path):
        import asyncio

        tool = "select_option"
        kwargs = {
            "instance_id": "i1",
            "selector": "#s",
            "index": CANARIES["dom-form-value"],
        }
        exc = asyncio.run(_failure(w15_server, tool, **kwargs))
        transcript = _sanitized_transcript(tool, kwargs, exc)

        destination = repro.resolve_destination(
            tmp_path / "capture" / "bundle",
            repo_root=REPO_ROOT,
            home=Path.home(),
            cwd=Path.cwd(),
        )
        destination.mkdir(parents=True)
        (destination / "transcript.json").write_text(
            json.dumps(transcript, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return destination, exc

    @pytest.mark.asyncio
    async def test_the_transcript_replays_to_the_same_typed_failure(
        self, transcript_dir, patched_server, tmp_path, monkeypatch
    ):
        from fakes import FakeBrowserManager, FakeTab

        source, original = transcript_dir
        # A FRESH directory: nothing from the capture run is reachable.
        fresh = tmp_path / "fresh-replay"
        fresh.mkdir()
        monkeypatch.chdir(fresh)
        loaded = json.loads((source / "transcript.json").read_text(encoding="utf-8"))
        assert loaded["schema"] == TRANSCRIPT_SCHEMA

        replay_server = patched_server(
            browser_manager=FakeBrowserManager(tabs={"i1": FakeTab()})
        )
        replayed = await _failure(
            replay_server, loaded["tool"], **loaded["call_kwargs"]
        )
        assert type(replayed).__name__ == loaded["error_type"]
        assert type(replayed) is type(original)
        # Same SHAPE of message, with the sanitized argument echoed back.
        assert str(replayed) == MSG_INVALID_INDEX.format(
            index=policy.placeholder("dom-form-value")
        )
        assert str(replayed) == loaded["expected_message"]
        assert CANARIES["dom-form-value"] not in str(replayed)

    @pytest.mark.asyncio
    async def test_the_replay_resolves_no_name_and_opens_no_socket(
        self, transcript_dir, patched_server, monkeypatch
    ):
        from fakes import FakeBrowserManager, FakeTab

        source, _ = transcript_dir
        loaded = json.loads((source / "transcript.json").read_text(encoding="utf-8"))

        def _forbidden(*args: object, **kwargs: object):
            raise AssertionError("the replay reached the network")

        monkeypatch.setattr(socket, "getaddrinfo", _forbidden)
        monkeypatch.setattr(socket, "create_connection", _forbidden)
        monkeypatch.setattr(socket, "socket", _forbidden)

        replay_server = patched_server(
            browser_manager=FakeBrowserManager(tabs={"i1": FakeTab()})
        )
        replayed = await _failure(
            replay_server, loaded["tool"], **loaded["call_kwargs"]
        )
        assert str(replayed) == loaded["expected_message"]

    @pytest.mark.asyncio
    async def test_the_replay_writes_nothing_outside_the_destination(
        self, transcript_dir, patched_server, tmp_path, monkeypatch
    ):
        from fakes import FakeBrowserManager, FakeTab

        source, _ = transcript_dir
        loaded = json.loads((source / "transcript.json").read_text(encoding="utf-8"))

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.chdir(workspace)
        before = set(workspace.rglob("*"))
        source_before = set(source.rglob("*"))

        replay_server = patched_server(
            browser_manager=FakeBrowserManager(tabs={"i1": FakeTab()})
        )
        await _failure(replay_server, loaded["tool"], **loaded["call_kwargs"])

        assert set(workspace.rglob("*")) == before, "the replay wrote into the cwd"
        assert set(source.rglob("*")) == source_before, "the replay mutated the bundle"

    def test_the_destination_resolver_refuses_every_non_throwaway_target(
        self, tmp_path
    ):
        """W6's resolver is reused verbatim; W15 adds no second one."""
        assert repro.resolve_destination.__module__ == "canary_repro"
        for target, expected in (
            (REPO_ROOT, "that is the repository"),
            (REPO_ROOT / "audit" / "w15", "inside the repository"),
            (Path.home(), "that is the home directory"),
        ):
            with pytest.raises(repro.RefusedError, match=expected):
                repro.resolve_destination(
                    target, repo_root=REPO_ROOT, home=Path.home(), cwd=tmp_path
                )

    def test_the_bundle_writer_exposes_no_upload_or_notification_flag(
        self, tmp_path, capsys
    ):
        """§1.2: W15 automation may write the local workspace and nothing else.

        Asserted against the real CLI parser: an exfil-shaped flag is rejected,
        so the no-external-mutation claim cannot rot into a comment.
        """
        for flag in (
            "--upload",
            "--post-comment",
            "--webhook",
            "--issue",
            "--notify",
            "--publish",
            "--remote",
        ):
            with pytest.raises(SystemExit) as caught:
                repro.main(["--out", str(tmp_path / "never"), flag, "x"])
            assert caught.value.code == 2, flag
            capsys.readouterr()
        assert not (tmp_path / "never").exists()
