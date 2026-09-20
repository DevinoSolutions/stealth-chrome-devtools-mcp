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

    def test_structured_content_is_the_answer(self):
        assert backend_client.result_value({"instance_id": "a"}, []) == {
            "instance_id": "a"
        }

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
        """The measured prototype shape: ``content`` empty, the answer in
        ``structuredContent``. A reader that took ``content[0].text`` first
        raised IndexError on every successful call."""
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
        assert "3" in out
        assert "installed build" in out.lower()


class TestSessionHygiene:
    """The session this CLI opens is terminated, and it is terminated by the
    SDK's own DELETE rather than by a second spelling of one (F-862)."""

    async def test_opened_asks_the_sdk_to_terminate_on_close(self, monkeypatch):
        import mcp.client.streamable_http as sdk

        seen = {}

        class _Fake:
            def __init__(self, url, **kwargs):
                seen["url"] = url
                seen["kwargs"] = kwargs

            async def __aenter__(self):
                raise _StopError

            async def __aexit__(self, *exc):
                return False

        class _StopError(Exception):
            pass

        monkeypatch.setattr(sdk, "streamablehttp_client", _Fake)
        with pytest.raises(_StopError):
            async with backend_client.opened("http://127.0.0.1:1/mcp/"):
                pass  # pragma: no cover - __aenter__ raises

        assert seen["url"] == "http://127.0.0.1:1/mcp/"
        assert seen["kwargs"]["terminate_on_close"] is True
