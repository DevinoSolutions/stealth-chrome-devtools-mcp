"""F-891 — the `stealthy` CLI: the six verbs that drive the LIVE backend.

Hermetic. Nothing here starts a backend, opens a socket or launches Chrome: the
one selection binding and the one MCP client are both patched, which is the
point — what these nodes pin is the layer between a shell invocation and a
`tools/call`, and every part of it is a place the feature can be true and the
command still wrong.

Four things are load-bearing and each has its own class:

* **Argument parsing.** `--arg k=v` is JSON when it parses and a string when it
  does not, so `headless=false` is a bool and `session=prod` is not a name
  error. `--json` supplies the whole object and `--arg` wins per key. There is
  deliberately no per-tool argparse mirror, so this is the ONLY place a caller's
  shell words become a tool's arguments.
* **Result unwrapping.** FastMCP answers in `structuredContent` and `content`
  may be EMPTY — measured on the 2026-09-19 prototype (`reattach_seller_central
  .py`), which is why a reader that only looked at `content[0].text` would have
  reported nothing for every tool that worked. A non-dict return arrives wrapped
  as `{"result": ...}`.
* **Backend selection.** It must be the SAME selection `status` makes, made
  ONCE. A second record read is how a live backend's status came to sit above a
  dead sibling's pid (F-868), and a CLI that selected differently from `status`
  would report one backend and drive another.
* **The session is terminated.** F-862 reaps abandoned sessions, but a CLI that
  leaks one per invocation is asking the backend to clean up after it.
"""

from __future__ import annotations

import io
import json
import sys
import tomllib
from pathlib import Path

import pytest

from stealth_chrome_devtools_mcp import cli, cli_call
from stealth_chrome_devtools_mcp.embedded import backend_client

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _never_the_operators_record(monkeypatch, tmp_path):
    """No node in this file may read or write the real `~/.stealth-mcp`.

    Autouse and structural rather than per-test, because the hazard is not
    hypothetical: a selection step that reached `SERVER_STATE_FILE` directly
    sent a real `initialize` at the operator's live backend from a node whose
    `_probe_backend_status` was patched to "nothing is running". A patched
    binding only isolates the paths it is on; the record path is isolated here
    for all of them.

    BOTH homes, not just the one `singleton` re-exports (F-891 review S4). The
    one `setattr` was sufficient for today's nodes — none reaches a writer on a
    default path — but a fixture whose docstring says "the record path" and
    redirects one NAME is one un-redirected writer away from the leak it exists
    to prevent, and `backend_registry` is where every writer resolves its own.
    """
    from stealth_chrome_devtools_mcp.embedded import backend_registry, singleton

    monkeypatch.setattr(singleton, "SERVER_STATE_FILE", tmp_path / "server.json")
    monkeypatch.setattr(backend_registry, "STATE_DIR", tmp_path)
    monkeypatch.setattr(backend_registry, "SERVER_STATE_FILE", tmp_path / "server.json")
    monkeypatch.setattr(backend_registry, "PORT_FILE", tmp_path / "server.port")


def _pyproject() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


class TestConsoleScriptNames:
    """One `main`, two names. `stealthy` is the name the docs teach; the old one
    stays because it is in every operator's muscle memory and in this repo's own
    RUNBOOK — an alias, never a second CLI (convention 4)."""

    def test_stealthy_is_declared_and_points_at_the_same_main(self):
        scripts = _pyproject()["project"]["scripts"]
        assert scripts["stealthy"] == "stealth_chrome_devtools_mcp.cli:main"
        assert scripts["stealthy"] == scripts["stealth-chrome-devtools"]

    def test_help_names_the_script_it_was_invoked_as(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["stealthy", "status"])
        assert cli.build_parser().prog == "stealthy"
        monkeypatch.setattr(sys, "argv", ["stealth-chrome-devtools.exe", "status"])
        assert cli.build_parser().prog == "stealth-chrome-devtools"

    def test_an_unknown_argv0_falls_back_to_the_canonical_name(self, monkeypatch):
        """Never "pytest" and never "python": the prog name comes from a CLOSED
        set of declared scripts, so help text cannot advertise a command that
        does not exist."""
        monkeypatch.setattr(sys, "argv", ["pytest"])
        assert cli.build_parser().prog == "stealthy"


class TestArgumentParsing:
    def test_json_scalars_parse_as_json(self):
        args = cli_call.parse_arguments(
            ["headless=false", "viewport_width=1200", "timeout=1.5"], None
        )
        assert args == {
            "headless": False,
            "viewport_width": 1200,
            "timeout": 1.5,
        }

    def test_json_containers_parse_as_json(self):
        args = cli_call.parse_arguments(['browser_args=["--x","--y"]'], None)
        assert args == {"browser_args": ["--x", "--y"]}

    def test_a_bare_word_is_a_string(self):
        args = cli_call.parse_arguments(["user_data_dir=seller-central"], None)
        assert args == {"user_data_dir": "seller-central"}

    def test_a_windows_path_is_a_string_not_a_json_error(self):
        args = cli_call.parse_arguments([r"user_data_dir=C:\Users\me\profile"], None)
        assert args == {"user_data_dir": r"C:\Users\me\profile"}

    def test_a_value_may_contain_equals_signs(self):
        args = cli_call.parse_arguments(["url=https://x.test/?a=1&b=2"], None)
        assert args == {"url": "https://x.test/?a=1&b=2"}

    def test_json_blob_supplies_the_whole_object(self):
        args = cli_call.parse_arguments([], '{"instance_id": "abc", "x": 1}')
        assert args == {"instance_id": "abc", "x": 1}

    def test_arg_wins_over_the_json_blob_per_key(self):
        args = cli_call.parse_arguments(["x=2"], '{"instance_id": "abc", "x": 1}')
        assert args == {"instance_id": "abc", "x": 2}

    def test_a_pair_with_no_equals_is_a_usage_error(self):
        with pytest.raises(cli_call.UsageError) as excinfo:
            cli_call.parse_arguments(["headless"], None)
        assert "headless" in str(excinfo.value)

    def test_a_malformed_json_blob_is_a_usage_error(self):
        with pytest.raises(cli_call.UsageError):
            cli_call.parse_arguments([], "{not json")

    def test_a_json_blob_that_is_not_an_object_is_a_usage_error(self):
        """A tool's arguments are a mapping. `--json '[1,2]'` is a caller
        mistake, and reporting it here is cheaper than a backend round trip."""
        with pytest.raises(cli_call.UsageError):
            cli_call.parse_arguments([], "[1, 2]")


class TestResultUnwrapping:
    """THE one reading of a tool's answer, and the shapes it has to survive."""

    def test_a_sole_result_key_is_unwrapped(self):
        """FastMCP wraps a NON-dict return (``list_instances`` returns a list)
        in ``{"result": ...}``; unwrapping only the SOLE-key shape is what keeps
        a tool that genuinely answers with a ``result`` field among others
        intact."""
        assert backend_client.result_value({"result": [1, 2]}, []) == [1, 2]

    def test_a_result_key_beside_others_is_left_alone(self):
        payload = {"result": 1, "other": 2}
        assert backend_client.result_value(payload, []) == payload

    def test_empty_content_with_structured_content_still_answers(self):
        """The measured prototype shape and the COMMON one: ``content`` empty,
        the answer in ``structuredContent``. A reader that took
        ``content[0].text`` first raised IndexError on every successful call.

        Structured content being the answer at all is this same node — one
        assertion, because "``structuredContent`` wins" and "an empty
        ``content`` is not a failure" are one claim about one call shape."""
        assert backend_client.result_value({"ok": True}, []) == {"ok": True}

    def test_text_content_is_the_fallback_and_is_parsed_when_it_is_json(self):
        assert backend_client.result_value(None, ['{"a": 1}']) == {"a": 1}

    def test_non_json_text_content_comes_back_as_text(self):
        assert backend_client.result_value(None, ["plain words"]) == "plain words"

    def test_no_structured_content_and_no_text_is_none(self):
        assert backend_client.result_value(None, []) is None


class TestInstancePrefixResolution:
    RECORDS = (  # noqa: RUF012  PERMANENT(test fixture data, never mutated)
        {"instance_id": "e364c31b-1111"},
        {"instance_id": "e364c31b-2222"},
        {"instance_id": "ff0a9d55-3333"},
    )

    def test_an_exact_id_resolves_to_itself(self):
        assert cli_call.resolve_instance(self.RECORDS, "e364c31b-1111") == (
            "e364c31b-1111"
        )

    def test_a_unique_prefix_resolves(self):
        assert cli_call.resolve_instance(self.RECORDS, "ff") == "ff0a9d55-3333"

    def test_an_ambiguous_prefix_names_every_match(self):
        with pytest.raises(cli_call.UsageError) as excinfo:
            cli_call.resolve_instance(self.RECORDS, "e364")
        message = str(excinfo.value)
        assert "e364c31b-1111" in message and "e364c31b-2222" in message

    def test_an_unknown_prefix_is_a_usage_error(self):
        with pytest.raises(cli_call.UsageError):
            cli_call.resolve_instance(self.RECORDS, "nope")

    def test_an_exact_id_wins_over_being_a_prefix_of_another(self):
        records = ({"instance_id": "ab"}, {"instance_id": "abc"})
        assert cli_call.resolve_instance(records, "ab") == "ab"


class TestOutputMode:
    """JSON when stdout is not a terminal, or when `--json` is passed. A table
    that reaches a pipe is a table something downstream has to parse."""

    def test_a_pipe_gets_json(self):
        assert cli_call.wants_json(io.StringIO(), explicit=False) is True

    def test_a_tty_gets_the_table(self, monkeypatch):
        stream = io.StringIO()
        monkeypatch.setattr(stream, "isatty", lambda: True, raising=False)
        assert cli_call.wants_json(stream, explicit=False) is False

    def test_explicit_json_beats_a_tty(self, monkeypatch):
        stream = io.StringIO()
        monkeypatch.setattr(stream, "isatty", lambda: True, raising=False)
        assert cli_call.wants_json(stream, explicit=True) is True


class _Recorder:
    """The one MCP client, replaced. Records what a verb asked the backend."""

    def __init__(self, answers=None, fail=None):
        self.answers = answers or {}
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []
        self.listed = 0
        self.urls: list[str] = []
        self.closed = 0

    async def call_tool(self, url, name, arguments, *, budget_seconds=None):
        self.urls.append(url)
        self.calls.append((name, arguments))
        if self.fail == name:
            raise backend_client.BackendCallError(f"{name} exploded")
        return self.answers.get(name)

    async def list_tools(self, url, *, budget_seconds=None):
        self.urls.append(url)
        self.listed += 1
        return self.answers.get("__tools__", [])


@pytest.fixture()
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(backend_client, "call_tool", rec.call_tool)
    monkeypatch.setattr(backend_client, "list_tools", rec.list_tools)
    return rec


@pytest.fixture()
def responsive(monkeypatch):
    """The ONE selection, answering `responsive` — patched at the name the rest
    of the suite already patches (`singleton._probe_backend_status`), because
    that is the only way to prove the CLI asks the same question `status` does.
    """
    from stealth_chrome_devtools_mcp.embedded import singleton

    seen = {"probes": 0, "starts": 0}

    def probe():
        seen["probes"] += 1
        return ("responsive", 41999)

    def ensure(port=None):  # pragma: no cover - the failure this pins is a CALL
        seen["starts"] += 1
        return 41999

    monkeypatch.setattr(singleton, "_probe_backend_status", probe)
    monkeypatch.setattr(singleton, "ensure_server_running", ensure)
    return seen


class TestBackendSelection:
    def test_a_responsive_backend_is_used_and_never_started(
        self, responsive, recorder, capsys
    ):
        recorder.answers["list_instances"] = []
        assert cli.main(["ls", "--json"]) == 0
        assert responsive["probes"] == 1, "the backend must be selected exactly ONCE"
        assert responsive["starts"] == 0
        assert recorder.urls == ["http://127.0.0.1:41999/mcp/"]

    def test_no_backend_and_no_start_exits_3(self, monkeypatch, recorder, capsys):
        from stealth_chrome_devtools_mcp.embedded import singleton

        monkeypatch.setattr(singleton, "_probe_backend_status", lambda: ("none", None))
        assert cli.main(["ls", "--no-start"]) == cli_call.EXIT_NO_BACKEND
        assert "no backend" in capsys.readouterr().err.lower()
        assert recorder.calls == []

    def test_no_backend_starts_one_through_the_existing_startup_path(
        self, monkeypatch, recorder
    ):
        """`ensure_server_running` and nothing else: the cold-start lock, F-886's
        step-aside and F-889's adopt-newer rule all live behind that one call,
        and a CLI that spawned its own backend would have none of them."""
        from stealth_chrome_devtools_mcp.embedded import singleton

        started = {}

        async def ready(url, *_args, **_kwargs):
            started["awaited"] = url
            return True

        monkeypatch.setattr(singleton, "_probe_backend_status", lambda: ("none", None))
        monkeypatch.setattr(
            singleton,
            "ensure_server_running",
            lambda *a, **k: started.setdefault("port", 42001),
        )
        monkeypatch.setattr(singleton, "_await_backend_http", ready)
        recorder.answers["list_instances"] = []

        assert cli.main(["ls", "--json"]) == 0
        assert started["awaited"] == "http://127.0.0.1:42001/mcp/"

    def test_a_backend_that_never_becomes_ready_exits_3(
        self, monkeypatch, recorder, capsys
    ):
        from stealth_chrome_devtools_mcp.embedded import singleton

        async def never(url, *_args, **_kwargs):
            return False

        monkeypatch.setattr(singleton, "_probe_backend_status", lambda: ("none", None))
        monkeypatch.setattr(singleton, "ensure_server_running", lambda *a, **k: 42002)
        monkeypatch.setattr(singleton, "_await_backend_http", never)

        assert cli.main(["ls"]) == cli_call.EXIT_NO_BACKEND
        assert recorder.calls == []


class TestTheCliNeverEvictsALiveBackend:
    """F-891 review S1. A `stealthy` invocation is a one-shot borrow of a
    socket; the thing a cold start would replace is somebody's live session.

    What makes "never evicts a LIVE backend" true is not a new rule — it is that
    the one selection is IDENTITY-BLIND. `backend_liveness.probe_recorded` asks
    `adoption_candidates` and the liveness ladder and nothing else, so a backend
    built from a different source tree ANSWERS and is adopted, and
    `ensure_server_running` — the only path that can evict — is never reached.
    These drive the REAL walk against a real record file rather than patching
    `_probe_backend_status`, because the identity-blindness is the claim.

    What is NOT claimed, and is disclosed in `backend_url`, RUNBOOK and the
    finding's §6: when NOTHING answers, the cold start is the proxy's own path
    and can evict a WEDGED foreign backend holding no live browser.
    """

    def _record(self, tmp_path, monkeypatch, *, port):
        """A stranger's backend — OUR display context, a build that is not ours
        — written through the ONE writer.

        Hand-written JSON is deliberately not used: the first draft of this
        fixture spelled the schema key `schema_version` where the reader asks
        for `schema`, so every entry read as no entries and the pin passed for
        the wrong reason. `record_backend` cannot drift from `backends_in`.
        """
        from stealth_chrome_devtools_mcp.embedded import (
            backend_registry,
            display_context,
            singleton,
        )

        record = tmp_path / "server.json"
        monkeypatch.setattr(singleton, "SERVER_STATE_FILE", record)
        backend_registry.record_backend(
            record,
            port=port,
            version="0.0.1-someone-elses-build",
            pid=4242,
            source_fingerprint="a-digest-that-is-not-ours",
            display_context=display_context.display_context(),
        )
        assert backend_registry.read_backends(record), "the record must be readable"
        return record

    def test_a_responsive_backend_of_a_foreign_identity_is_adopted_not_replaced(
        self, tmp_path, monkeypatch, recorder
    ):
        from stealth_chrome_devtools_mcp.embedded import singleton

        self._record(tmp_path, monkeypatch, port=43111)
        starts = []
        monkeypatch.setattr(singleton, "_server_is_healthy", lambda port: True)
        monkeypatch.setattr(singleton, "_backend_http_ready", lambda port: True)
        monkeypatch.setattr(
            singleton, "ensure_server_running", lambda *a, **k: starts.append(1)
        )
        recorder.answers["list_instances"] = []

        assert cli.main(["ls", "--json"]) == 0
        assert recorder.urls == ["http://127.0.0.1:43111/mcp/"]
        assert starts == [], (
            "a cold start beside a live backend is the ONE way this CLI evicts"
        )

    def test_a_foreign_identity_never_reaches_the_reuse_gate_at_all(
        self, tmp_path, monkeypatch, recorder
    ):
        """The gate is what a PROXY consults before reusing a backend for a whole
        session, and it is where a fingerprint mismatch becomes an eviction. A
        CLI that asked it would inherit that consequence for a single call."""
        from stealth_chrome_devtools_mcp.embedded import singleton

        self._record(tmp_path, monkeypatch, port=43111)
        asked = []
        monkeypatch.setattr(singleton, "_server_is_healthy", lambda port: True)
        monkeypatch.setattr(singleton, "_backend_http_ready", lambda port: True)
        monkeypatch.setattr(
            singleton,
            "_same_identity_backend_ready",
            lambda port: asked.append(port) or False,
        )
        monkeypatch.setattr(
            singleton,
            "ensure_server_running",
            lambda *a, **k: pytest.fail("a cold start is what evicts"),
        )
        recorder.answers["list_instances"] = []

        assert cli.main(["ls", "--json"]) == 0
        assert asked == []

    def test_nothing_answering_is_the_only_road_to_a_cold_start(
        self, tmp_path, monkeypatch, recorder
    ):
        from stealth_chrome_devtools_mcp.embedded import singleton

        async def ready(url, *_args, **_kwargs):
            return True

        self._record(tmp_path, monkeypatch, port=43111)
        monkeypatch.setattr(singleton, "_server_is_healthy", lambda port: False)
        monkeypatch.setattr(singleton, "ensure_server_running", lambda *a, **k: 42003)
        monkeypatch.setattr(singleton, "_await_backend_http", ready)
        recorder.answers["list_instances"] = []

        assert cli.main(["ls", "--json"]) == 0
        assert recorder.urls == ["http://127.0.0.1:42003/mcp/"]


class _ClosedReader(io.StringIO):
    """A stdout whose reader has gone — `stealthy ls | head -1` after `head`
    has taken its line. The failure is raised by the WRITE, which is where a
    real broken pipe raises it, so the exception travels the production path
    (out of `print`, out of `_emit_json`, into `_run`) rather than being handed
    to `_verdict` by the test."""

    def write(self, text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    def isatty(self) -> bool:
        return False


class TestExitCodesAreClosed:
    """F-891 review M1. Every exception becomes one of the codes, and never a
    traceback: exit 1 means "the tool answered and said no", so a crash in the
    CLI wearing that code hands a script our bug as the backend's answer."""

    def test_a_transport_failure_is_no_backend_and_not_a_tool_error(
        self, responsive, monkeypatch, capsys
    ):
        """Nothing on the backend ever saw the request, so there is no answer to
        report — and the remedy (re-run, check `status`) is the no-backend one."""
        import httpx

        async def boom(*_args, **_kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(backend_client, "call_tool", boom)
        assert cli.main(["call", "list_tabs"]) == cli_call.EXIT_NO_BACKEND
        assert "could not reach the backend" in capsys.readouterr().err

    def test_an_unexpected_exception_is_70_and_never_1(self):
        code, line = cli_call._verdict(ValueError("a bug of ours"))
        assert code == cli_call.EXIT_INTERNAL == 70
        assert "internal error" in line
        assert "--traceback" in line

    def test_a_keyboard_interrupt_is_130(self):
        code, line = cli_call._verdict(KeyboardInterrupt())
        assert code == cli_call.EXIT_INTERRUPTED == 130
        assert line == "interrupted"

    @pytest.mark.parametrize(
        ("raises", "code"),
        [
            (KeyboardInterrupt, 130),
            (ValueError, 70),
            (ConnectionResetError, 3),
        ],
        ids=["ctrl-c", "our own bug", "the socket went"],
    )
    def test_no_path_out_of_main_prints_a_traceback(
        self, responsive, monkeypatch, capsys, raises, code
    ):
        """The closed set is a claim about `main`, not about `_verdict`, so each
        kind is driven through the whole command. A traceback on stderr is the
        failure this exists to prevent as much as a wrong code is: it is what a
        script's error channel would carry, and Python pairs it with exit 1 —
        the code that means "the tool answered and said no"."""

        async def boom(*_args, **_kwargs):
            raise raises("no")

        monkeypatch.setattr(backend_client, "call_tool", boom)
        assert cli.main(["call", "list_tabs"]) == code
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
        assert captured.err.strip(), "silence is not an answer either"
        assert len(captured.err.strip().splitlines()) == 1, "one line, per the contract"

    def test_a_single_exception_inside_a_group_is_judged_on_its_merits(self):
        """The transport runs under an anyio task group, which raises a GROUP.
        Without unwrapping, every transport failure would be reported as our own
        bug — the exact miscategorisation this class exists to prevent."""
        import httpx

        group = ExceptionGroup("tg", [httpx.ReadTimeout("slow")])
        assert cli_call._verdict(group)[0] == cli_call.EXIT_NO_BACKEND

    def test_a_group_of_several_stays_internal(self):
        """Picking one to report would be picking which half of the truth to
        tell."""
        group = ExceptionGroup("tg", [ValueError("a"), ValueError("b")])
        assert cli_call._verdict(group)[0] == cli_call.EXIT_INTERNAL

    def test_a_protocol_refusal_is_a_tool_error(self):
        """An unknown tool is the everyday one: the round trip worked and the
        fix is in what was asked, so it is exit 1 and not exit 3."""
        from mcp.shared.exceptions import McpError
        from mcp.types import ErrorData

        error = McpError(ErrorData(code=-32602, message="Unknown tool: nope"))
        assert cli_call._verdict(error)[0] == cli_call.EXIT_TOOL_ERROR

    def test_traceback_prints_the_stack_and_still_returns_the_verdict(
        self, responsive, recorder, capsys
    ):
        """Both, never one instead of the other: the operator debugging this
        needs the stack, and the line is what everyone else reads."""
        recorder.fail = "navigate"
        assert cli.main(["call", "navigate", "--traceback"]) == (
            cli_call.EXIT_TOOL_ERROR
        )
        err = capsys.readouterr().err
        assert "navigate exploded" in err
        assert "Traceback" in err

    def test_traceback_never_lets_the_exception_leave_main(
        self, responsive, recorder, capsys
    ):
        """F-891 review S1. `cli.main`'s first statement is `sentry_init()`, so
        an exception that escapes is shipped by `sys.excepthook` — carrying a
        `BackendCallError` built from the TOOL'S OWN WORDS, which is the one
        thing this module's docstring promises it never sends anywhere (and
        `BackendCallError` is not a `ToolError`, so `expected_events`' rule
        would not drop it). The stack has to reach the operator without the
        payload reaching the network."""
        recorder.fail = "navigate"
        # No `pytest.raises`: nothing may propagate. A re-raise fails here.
        assert isinstance(cli.main(["call", "navigate", "--traceback"]), int)
        assert "navigate exploded" in capsys.readouterr().err

    def test_no_subcommand_is_a_usage_error_and_not_a_tool_error(self, capsys):
        """F-891 review M1. `stealthy` bare prints help, and that is a USAGE
        condition: exit 1 would tell a script the tool answered and refused.
        It is the one path through `main` that reaches no verb, which is why
        the rest of this class could not see it."""
        assert cli.main([]) == cli_call.EXIT_USAGE == 2
        assert "usage" in capsys.readouterr().out.lower()

    def test_a_broken_pipe_is_not_a_missing_backend(
        self, responsive, recorder, monkeypatch, capsys
    ):
        """F-891 review M2. `stealthy ls | head -1` is the commonest idiom in
        the shell, and `BrokenPipeError` is an `OSError`, so before its own row
        it fell through to the transport row and reported "could not reach the
        backend" about a round trip that had already succeeded. The pipe row
        must sit ABOVE that one.

        This is the ONE pin for that order, end to end rather than on `_verdict`
        alone, because the order is only half of it: `_abandon_stdout` and the
        silent stderr are the other half and a unit node cannot see either.
        Measured under mutation — moving the row below the transport row
        reproduces the shipped sentence verbatim. That the transport row itself
        still answers 3 is `test_no_path_out_of_main_prints_a_traceback`'s
        `ConnectionResetError` case, so neither claim is pinned twice."""
        abandoned = []
        monkeypatch.setattr(cli_call, "_abandon_stdout", lambda: abandoned.append(1))
        monkeypatch.setattr(sys, "stdout", _ClosedReader())
        recorder.answers["list_instances"] = []

        code = cli.main(["ls", "--json"])
        assert code == cli_call.EXIT_BROKEN_PIPE == 141
        assert code != cli_call.EXIT_NO_BACKEND
        assert capsys.readouterr().err == "", (
            "the operator's own `head` did what they asked; a diagnostic is noise"
        )
        assert abandoned == [1], (
            "without this the interpreter's exit flush fails again outside every "
            "handler and CPython exits 120, outside the advertised set"
        )

    def test_a_bare_base_exception_group_is_caught_by_main(
        self, responsive, monkeypatch
    ):
        """The third name in `_run`'s `except` tuple. A `BaseExceptionGroup`
        that is NOT an `ExceptionGroup` is what an interrupt becomes through the
        transport's own anyio task group, and neither `Exception` nor
        `KeyboardInterrupt` catches it — so deleting that name from the tuple
        used to pass the whole file."""

        async def boom(*_args, **_kwargs):
            raise BaseExceptionGroup("tg", [KeyboardInterrupt()])

        monkeypatch.setattr(backend_client, "call_tool", boom)
        assert cli.main(["call", "list_tabs"]) == cli_call.EXIT_INTERRUPTED


class TestCallVerb:
    def test_call_prints_the_structured_result_as_json(
        self, responsive, recorder, capsys
    ):
        recorder.answers["list_tabs"] = {"tabs": [{"url": "https://x.test/"}]}
        assert cli.main(["call", "list_tabs", "--arg", "instance_id=abc"]) == 0
        assert recorder.calls == [("list_tabs", {"instance_id": "abc"})]
        assert json.loads(capsys.readouterr().out) == recorder.answers["list_tabs"]

    def test_a_tool_error_goes_to_stderr_and_exits_1(
        self, responsive, recorder, capsys
    ):
        recorder.fail = "navigate"
        rc = cli.main(["call", "navigate", "--arg", "instance_id=abc"])
        captured = capsys.readouterr()
        assert rc == cli_call.EXIT_TOOL_ERROR
        assert "navigate exploded" in captured.err
        assert captured.out == ""

    def test_a_bad_arg_pair_exits_2_before_any_backend_call(
        self, responsive, recorder, capsys
    ):
        rc = cli.main(["call", "navigate", "--arg", "oops"])
        assert rc == cli_call.EXIT_USAGE
        assert recorder.calls == []
        assert "oops" in capsys.readouterr().err

    def test_call_takes_no_per_tool_argparse_mirror(self):
        """The tool's own schema on the backend is the validation. A CLI that
        mirrored 94 signatures would need a release to serve a 95th tool."""
        parsed = cli.build_parser().parse_args(
            ["call", "spawn_browser", "--arg", "headless=false"]
        )
        assert parsed.tool == "spawn_browser"
        assert parsed.arg == ["headless=false"]


class TestSpawnVerb:
    def test_profile_passes_user_data_dir_through_untouched(self, responsive, recorder):
        """A name and a path both go through VERBATIM. What a name RESOLVES to is
        `clone_storage.resolve_profile_selection`'s answer, and this verb's job is
        to report that answer back (`profile_selection`), never to pre-empt it."""
        recorder.answers["spawn_browser"] = {"instance_id": "abc"}
        assert cli.main(["spawn", "--profile", "seller-central", "--json"]) == 0
        assert recorder.calls[0][1]["user_data_dir"] == "seller-central"
        assert cli.main(["spawn", "--profile", r"C:\p\dir", "--json"]) == 0
        assert recorder.calls[-1][1]["user_data_dir"] == r"C:\p\dir"

    def test_there_is_no_master_flag(self):
        """`--master` was built and REMOVED before shipping; this pin keeps it out.

        Two measured reasons. `master` as a bare NAME resolves to
        `sessions/master`, a DIFFERENT profile (F-894), so a flag spelled that way
        teaches a word that is a trap one resolution step from the thing it names.
        And the vocabulary replacing it — `--session NAME` + `--from <session>`,
        with `default` reserved — owns that question (F-892+), so shipping the flag
        would mean renaming it next release. `--profile <absolute path>` already
        reaches any directory, master's included.
        """
        with pytest.raises(SystemExit) as excinfo:
            cli.build_parser().parse_args(["spawn", "--master"])
        assert excinfo.value.code == cli_call.EXIT_USAGE

    def test_neither_headed_nor_headless_sends_no_headless_argument(
        self, responsive, recorder
    ):
        """The tool's own default decides. A CLI that always sent one would be a
        second answer to a question `spawn_browser` already answers."""
        recorder.answers["spawn_browser"] = {"instance_id": "abc"}
        assert cli.main(["spawn", "--json"]) == 0
        assert "headless" not in recorder.calls[0][1]

    def test_headed_and_headless_map_to_the_tool_argument(self, responsive, recorder):
        recorder.answers["spawn_browser"] = {"instance_id": "abc"}
        cli.main(["spawn", "--headed", "--json"])
        assert recorder.calls[-1][1]["headless"] is False
        cli.main(["spawn", "--headless", "--json"])
        assert recorder.calls[-1][1]["headless"] is True

    def test_a_reattach_is_reported_prominently_on_a_tty(
        self, responsive, recorder, monkeypatch, capsys
    ):
        """F-888's whole point, and the reason this verb exists: the operator has
        to SEE that the browser they got is the one that was already running."""
        monkeypatch.setattr(cli_call, "wants_json", lambda *a, **k: False)
        recorder.answers["spawn_browser"] = {
            "instance_id": "abc",
            "spawn_diagnostics": {
                "reattached": True,
                "reattached_pid": 115652,
                "profile_selection": {"profile_role": "explicit"},
            },
        }
        assert cli.main(["spawn", "--profile", "seller-central"]) == 0
        out = capsys.readouterr().out
        assert "reattached" in out.lower()
        assert "115652" in out

    def test_the_profile_role_is_printed_so_the_caller_sees_what_they_got(
        self, responsive, recorder, monkeypatch, capsys
    ):
        monkeypatch.setattr(cli_call, "wants_json", lambda *a, **k: False)
        recorder.answers["spawn_browser"] = {
            "instance_id": "abc",
            "spawn_diagnostics": {"profile_selection": {"profile_role": "clone"}},
        }
        cli.main(["spawn"])
        assert "clone" in capsys.readouterr().out

    def test_url_navigates_after_the_spawn_in_the_same_session(
        self, responsive, recorder
    ):
        recorder.answers["spawn_browser"] = {"instance_id": "abc"}
        recorder.answers["navigate"] = {"url": "https://x.test/"}
        assert cli.main(["spawn", "--url", "https://x.test/", "--json"]) == 0
        assert [name for name, _ in recorder.calls] == ["spawn_browser", "navigate"]
        assert recorder.calls[1][1] == {
            "instance_id": "abc",
            "url": "https://x.test/",
        }
        assert responsive["probes"] == 1, "two tool calls, ONE backend selection"

    def test_a_failed_navigation_still_leaves_the_caller_the_instance_id(
        self, responsive, recorder, capsys
    ):
        """F-891 review M3. The browser EXISTS the moment the first call returns.
        Printing at the end meant a bad url, a challenge page or an expired
        budget took the id down with it and left a running browser — on a headed
        spawn, a visible window — that the caller could not `nav` or `close`."""
        recorder.answers["spawn_browser"] = {"instance_id": "abc-123"}
        recorder.fail = "navigate"

        assert cli.main(["spawn", "--url", "https://x.test/", "--json"]) == (
            cli_call.EXIT_TOOL_ERROR
        )
        captured = capsys.readouterr()
        assert "abc-123" in captured.out, "the id the caller needs to act"
        assert "navigate exploded" in captured.err, "and the failure, not instead of it"

    def test_the_id_reaches_a_terminal_too_and_not_only_a_pipe(
        self, responsive, recorder, monkeypatch, capsys
    ):
        """The table path is the one an operator actually watches, so the M3
        ordering has to hold on BOTH sides of `wants_json`.

        The terminal is REAL here — `isatty`, not a patched `wants_json` —
        because this node's whole claim is about the side a terminal lands on:
        patching the rule out would pin the branch while asserting nothing
        about how a shell reaches it, and `wants_json(sys.stdout, …)` resolving
        its stream at call time is the link that makes the pair work. The other
        table nodes patch the rule deliberately: their claim is the table's
        CONTENT, and it is this file's one `isatty` that says which mode a
        terminal gets."""
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        recorder.answers["spawn_browser"] = {"instance_id": "abc-123"}
        recorder.fail = "navigate"

        assert cli.main(["spawn", "--url", "https://x.test/"]) == (
            cli_call.EXIT_TOOL_ERROR
        )
        out = capsys.readouterr().out
        # The TABLE line, not the id alone: an `isatty` patch that failed to
        # take would emit the id as JSON and satisfy a bare `"abc-123" in out`,
        # so the node would pass while measuring the other branch.
        assert "instance   : abc-123" in out


class TestLsVerb:
    THREE_SHAPES = [  # noqa: RUF012  PERMANENT(test fixture data, never mutated)
        {
            "instance_id": "aaa",
            "state": "running",
            "source": "active",
            "partial": False,
            "current_url": "https://live.test/",
            "title": "Live",
        },
        {
            "instance_id": "bbb",
            "state": "running",
            "source": "active",
            "partial": True,
            "detail_error": "Could not read the active tab: TimeoutError: ",
            "last_navigated_url": "https://stale.test/",
            "last_navigated_title": "Stale",
        },
        {
            "instance_id": "ccc",
            "state": "closed (stored)",
            "source": "stored",
            "last_navigated_url": "https://gone.test/",
            "last_navigated_title": "Gone",
        },
    ]

    def test_json_output_is_the_tool_record_verbatim(
        self, responsive, recorder, capsys
    ):
        recorder.answers["list_instances"] = self.THREE_SHAPES
        assert cli.main(["ls", "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == self.THREE_SHAPES

    def test_the_table_never_presents_a_stale_url_as_current(
        self, responsive, recorder, monkeypatch, capsys
    ):
        """F-874's finding, at the CLI: a `partial` row carries no
        `current_url`, and a table that filled the column from
        `last_navigated_url` would re-commit the exact defect — reporting a
        login page for an instance on a feed."""
        monkeypatch.setattr(cli_call, "wants_json", lambda *a, **k: False)
        recorder.answers["list_instances"] = self.THREE_SHAPES
        assert cli.main(["ls"]) == 0
        lines = {
            row.split()[0]: row
            for row in capsys.readouterr().out.splitlines()
            if row[:1].isalnum()
        }
        assert "https://live.test/" in lines["aaa"]
        assert cli_call.LAST_KNOWN_MARK not in lines["aaa"], (
            "a regression that marked EVERY row still contains the live url"
        )
        assert cli_call.LAST_KNOWN_MARK in lines["bbb"]
        assert cli_call.LAST_KNOWN_MARK in lines["ccc"]

    def test_no_instances_says_so_rather_than_printing_an_empty_table(
        self, responsive, recorder, monkeypatch, capsys
    ):
        monkeypatch.setattr(cli_call, "wants_json", lambda *a, **k: False)
        recorder.answers["list_instances"] = []
        assert cli.main(["ls"]) == 0
        assert "no browser instances" in capsys.readouterr().out.lower()


class TestNavAndCloseVerbs:
    RECORDS = [  # noqa: RUF012  PERMANENT(test fixture data, never mutated)
        {"instance_id": "e364c31b-1111", "state": "running", "source": "active"},
        {"instance_id": "ff0a9d55-3333", "state": "running", "source": "active"},
    ]

    def test_nav_resolves_a_prefix_and_forwards_the_wait(self, responsive, recorder):
        recorder.answers["list_instances"] = self.RECORDS
        recorder.answers["navigate"] = {"url": "https://x.test/"}
        rc = cli.main(
            ["nav", "ff", "https://x.test/", "--wait", "domcontentloaded", "--json"]
        )
        assert rc == 0
        assert recorder.calls[-1] == (
            "navigate",
            {
                "instance_id": "ff0a9d55-3333",
                "url": "https://x.test/",
                "wait_until": "domcontentloaded",
            },
        )

    def test_nav_without_wait_sends_no_wait_until(self, responsive, recorder):
        recorder.answers["list_instances"] = self.RECORDS
        recorder.answers["navigate"] = {}
        cli.main(["nav", "ff", "https://x.test/", "--json"])
        assert "wait_until" not in recorder.calls[-1][1]

    def test_close_resolves_a_prefix(self, responsive, recorder):
        recorder.answers["list_instances"] = self.RECORDS
        recorder.answers["close_instance"] = True
        assert cli.main(["close", "e364", "--json"]) == 0
        assert recorder.calls[-1] == (
            "close_instance",
            {"instance_id": "e364c31b-1111"},
        )

    def test_an_ambiguous_prefix_exits_2_and_never_closes_anything(
        self, responsive, recorder, capsys
    ):
        recorder.answers["list_instances"] = [
            {"instance_id": "e364c31b-1111"},
            {"instance_id": "e364c31b-2222"},
        ]
        rc = cli.main(["close", "e364"])
        assert rc == cli_call.EXIT_USAGE
        assert [name for name, _ in recorder.calls] == ["list_instances"]
        assert "e364c31b-2222" in capsys.readouterr().err


class TestToolsVerb:
    @pytest.fixture(autouse=True)
    def _the_process_environment_is_borrowed(self, monkeypatch):
        """Every node here reaches `cli._server()`, which MUTATES `os.environ`
        — a `setdefault` of `STEALTH_MCP_NO_AUTO_RECOVERY` and then
        `backend_env.scrub_process_env()`, whose `_remove` is a real `del`
        (F-891 review S3). Neither is restored and `conftest` has no autouse env
        isolation, so without this the residue reaches every later node in the
        session: one that depends on the guard being UNSET, or on a `FASTMCP_*`
        name still existing, fails for a reason no part of it names.

        Swapping the whole mapping is what covers a `del` as well as a set —
        `monkeypatch.delenv` restores a name only if the test names it, and the
        names the scrub removes are the operator's, not this file's.
        """
        import os

        monkeypatch.setattr(os, "environ", dict(os.environ))

    TOOLS = [  # noqa: RUF012  PERMANENT(test fixture data, never mutated)
        {"name": "spawn_browser", "description": "Spawn a new browser instance."},
        {"name": "navigate", "description": "Navigate to a URL."},
        {"name": "clone_element_complete", "description": "Clone an element."},
    ]

    def test_the_live_list_is_the_answer(self, responsive, recorder, capsys):
        recorder.answers["__tools__"] = self.TOOLS
        assert cli.main(["tools", "--json"]) == 0
        assert [t["name"] for t in json.loads(capsys.readouterr().out)] == [
            "spawn_browser",
            "navigate",
            "clone_element_complete",
        ]

    def test_a_section_filter_uses_the_installed_registry(
        self, responsive, recorder, capsys
    ):
        recorder.answers["__tools__"] = self.TOOLS
        assert cli.main(["tools", "--section", "browser-management", "--json"]) == 0
        names = {t["name"] for t in json.loads(capsys.readouterr().out)}
        assert "spawn_browser" in names
        assert "clone_element_complete" not in names

    def test_a_count_disagreement_is_reported_not_hidden(
        self, responsive, recorder, monkeypatch, capsys
    ):
        """The LIVE list is the truth about the running backend and the registry
        is the truth about the installed build. When they differ the operator is
        talking to a backend built from other source — which is the single most
        useful thing this verb can tell them."""
        monkeypatch.setattr(cli_call, "wants_json", lambda *a, **k: False)
        recorder.answers["__tools__"] = self.TOOLS
        assert cli.main(["tools"]) == 0
        out = capsys.readouterr().out
        headline = out.splitlines()[0]
        assert headline.startswith(f"{len(self.TOOLS)} tools on the live backend")
        assert "installed build" in headline.lower()
        # The two numbers must actually DISAGREE, or the node passes for a
        # version of this line that prints one count twice.
        registry = int(headline.rsplit(" ", 1)[-1])
        assert registry != len(self.TOOLS) and registry > 3


SESSION_ID = "session-under-test"


class FakeBackend:
    """A streamable-HTTP MCP server, answered at the HTTP layer.

    The whole point is that NOTHING of ours and nothing of the SDK's is faked:
    the real `mcp` client negotiates a real session over a real
    `httpx.AsyncClient`, and only the socket underneath it is ours. The first
    version of this pin replaced `streamablehttp_client` with a double whose
    `__aenter__` raised, so the exit path — the one thing the pin was named for
    — never ran at all, and the assertion was about a keyword argument rather
    than about a DELETE (F-891 review M2, and `mocked-fakes-can-encode-the-bug`:
    a hand-written double can encode the very behaviour it is asked to prove).
    """

    def __init__(self, *, tool_result=None, fail_call=False):
        self.tool_result = tool_result if tool_result is not None else {"ok": True}
        self.fail_call = fail_call
        self.error_call = False
        self.budget: float | None = None
        self.methods: list[str] = []
        self.rpc: list[str] = []
        self.deleted_session: str | None = None

    def transport(self):
        import httpx

        return httpx.MockTransport(self._handle)

    def _handle(self, request):
        import httpx

        self.methods.append(request.method)
        if request.method == "DELETE":
            self.deleted_session = request.headers.get("mcp-session-id")
            return httpx.Response(200)
        if request.method == "GET":
            # What our own backend answers when a client opens the standing
            # event stream it does not need; the SDK treats it as "no stream".
            return httpx.Response(405)
        message = json.loads(request.content)
        method = message.get("method", "")
        self.rpc.append(method)
        if "id" not in message:
            return httpx.Response(202)
        if method == "tools/call" and self.fail_call:
            raise httpx.ReadError("the socket died mid-call")
        return self._answer(message, method)

    def _answer(self, message, method):
        import httpx

        results = {
            "initialize": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-backend", "version": "0"},
            },
            "tools/call": (
                # A tool that answered and said no. FastMCP reports a raised
                # `ToolError` exactly here — `isError` with the message as text
                # content — which is what `call_tool` turns into a
                # `BackendCallError`.
                {
                    "content": [{"type": "text", "text": "navigate: no such instance"}],
                    "isError": True,
                }
                if self.error_call
                else {
                    "content": [],
                    "structuredContent": self.tool_result,
                    "isError": False,
                }
            ),
            "tools/list": {"tools": []},
        }
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": message["id"], "result": results[method]},
            headers={
                "content-type": "application/json",
                "mcp-session-id": SESSION_ID,
            },
        )


@pytest.fixture()
def fake_backend(monkeypatch):
    """Binds a :class:`FakeBackend` under `backend_client.http_client`, the ONE
    transport seam — so the substitution is the socket and never the protocol."""
    import httpx

    backend = FakeBackend()

    def client(budget_seconds):
        backend.budget = budget_seconds
        return httpx.AsyncClient(
            transport=backend.transport(),
            follow_redirects=True,
            timeout=httpx.Timeout(
                backend_client.CONNECT_TIMEOUT_SECONDS, read=budget_seconds
            ),
        )

    monkeypatch.setattr(backend_client, "http_client", client)
    return backend


class TestSessionHygiene:
    """The session this CLI opens is terminated on EVERY path, and it is
    terminated by the SDK's own DELETE rather than by a second spelling of one.

    F-862's sweep reaps sessions whose client vanished; a CLI leaking one per
    invocation would be asking that sweep to clean up after it. Each node below
    is a different way out of :func:`backend_client.opened`.
    """

    async def test_a_successful_call_deletes_its_session(self, fake_backend):
        answer = await backend_client.call_tool(
            "http://127.0.0.1:1/mcp/", "list_tabs", {}
        )
        assert answer == {"ok": True}
        assert fake_backend.deleted_session == SESSION_ID
        assert fake_backend.methods[-1] == "DELETE"

    async def test_a_tool_error_deletes_its_session(self, fake_backend):
        """The tool answered and said no — a failure INSIDE a live session, so
        the session still has to be ended on the way out. It is correct today by
        ORDERING alone (`call_tool` leaves the `async with` before it raises),
        which is exactly the kind of thing a refactor breaks in silence."""
        fake_backend.error_call = True
        with pytest.raises(backend_client.BackendCallError) as excinfo:
            await backend_client.call_tool("http://127.0.0.1:1/mcp/", "navigate", {})
        assert "no such instance" in str(excinfo.value)
        assert fake_backend.deleted_session == SESSION_ID
        assert fake_backend.methods[-1] == "DELETE"

    async def test_a_transport_failure_mid_call_still_deletes(self, fake_backend):
        """The session EXISTS — `initialize` already answered and handed back an
        id — so a socket that dies during `tools/call` leaves a real session on
        a real backend. This is the path a naive `try`/`finally` around the
        happy case misses."""
        fake_backend.fail_call = True
        # The broad catch is the point: what is measured is the DELETE, not
        # which shape the SDK's own task group wraps a dead socket in. Naming a
        # type here would pin the SDK's internals instead of our contract.
        with pytest.raises(BaseException):  # noqa: B017, PT011  PERMANENT(F-891)
            await backend_client.call_tool("http://127.0.0.1:1/mcp/", "navigate", {})
        assert fake_backend.deleted_session == SESSION_ID

    async def test_a_cancellation_still_deletes(self, fake_backend):
        """`Ctrl-C` and an expired outer budget both arrive as a cancellation
        AT the await, and neither is allowed to leak a session."""
        import asyncio

        async def call():
            async with backend_client.opened("http://127.0.0.1:1/mcp/"):
                await asyncio.sleep(3600)

        task = asyncio.ensure_future(call())
        await asyncio.sleep(0)
        while not fake_backend.rpc:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake_backend.deleted_session == SESSION_ID

    async def test_the_session_announces_itself_as_the_cli(self, fake_backend):
        """`clientInfo` is how a backend's log tells a CLI call apart from a
        real MCP session and from a liveness probe (`backend_probe`'s two)."""
        sent = []
        original = fake_backend._handle

        def handle(request):
            if request.method == "POST":
                sent.append(json.loads(request.content))
            return original(request)

        fake_backend._handle = handle
        await backend_client.call_tool("http://127.0.0.1:1/mcp/", "list_tabs", {})
        info = sent[0]["params"]["clientInfo"]
        assert info["name"] == backend_client.CLIENT_NAME == "stealthy-cli"

    def test_the_sdk_still_names_the_replacement_this_module_uses(self):
        """F-891 review S2. `streamablehttp_client` is `@deprecated` at the
        pinned mcp 1.27.1 and `streamable_http_client` is what it says to use;
        this module uses the latter, and the latter takes a client rather than
        the two timeout numbers — which is why `http_client` exists. If a bump
        renames or re-signatures it, that is a decision to make deliberately."""
        import inspect

        import mcp.client.streamable_http as sdk

        params = inspect.signature(sdk.streamable_http_client).parameters
        assert "http_client" in params
        assert "terminate_on_close" in params
        assert "timeout" not in params, (
            "the budget moved onto the httpx client; re-read backend_client."
            "http_client before trusting --timeout again"
        )

    async def test_the_one_transport_seam_puts_the_budget_on_the_read_clock(self):
        """F-891 review S2. `http_client` is documented as THE one transport
        seam and as the one place the two clocks are decided — and every node
        above reaches it through a fixture that RE-IMPLEMENTS its body, so
        swapping which clock got the budget, or dropping `follow_redirects`,
        failed nothing at all. This drives the real function.

        The two clocks are two for a reason worth failing over: a tool call
        waits under `read`, so a backend whose socket is gone must be reported
        on the short `connect` budget rather than at the end of a 180 s one.
        """
        client = backend_client.http_client(7.0)
        try:
            assert client.timeout.read == 7.0
            assert client.timeout.connect == backend_client.CONNECT_TIMEOUT_SECONDS
            assert client.timeout.connect != 7.0, "the two clocks are not one clock"
            assert client.follow_redirects is True, (
                "the MCP default; nothing here may be narrower than the transport"
                " the SDK would have built"
            )
        finally:
            await client.aclose()

    def test_the_timeout_flag_reaches_that_seam_end_to_end(
        self, responsive, fake_backend
    ):
        """`--timeout` → `_timeout()` → `budget_seconds` → `http_client`. Every
        link was there and none of them was asserted, so a verb that dropped the
        keyword would have served the default budget in silence."""
        assert cli.main(["ls", "--timeout", "9", "--json"]) == 0
        assert fake_backend.budget == 9.0
        assert fake_backend.budget != backend_client.DEFAULT_TIMEOUT_SECONDS
