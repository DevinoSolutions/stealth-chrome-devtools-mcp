"""F-890 — the backend must not inherit the MCP client's ``FASTMCP_*`` variables.

For sixty-six minutes on 2026-09-18 every backend launch died at IMPORT with a
``pydantic.ValidationError``: ``fastmcp`` builds a ``BaseSettings`` at module
import whose ``env_prefixes`` are ``["FASTMCP_", "FASTMCP_SERVER_"]``, and an
inherited ``FASTMCP_PORT=""`` is the string ``""``, which is not a valid ``int``.
The crash lands before ``configure_logging`` exists, so nothing but
``backend-boot.log`` records it, and the proxy above simply retried and gave up.

The pins here are driven through ``singleton._start_server_process`` — THE one
child-env composition site — with ``backend_launch.spawn`` captured, rather than
against the leaf in isolation: what F-890 is about is which environment actually
reaches the spawn, and a leaf test alone would pass with the call site missing.

The last node is the library pin. The prefix constant is OURS (the proxy must
never import ``fastmcp`` to compute a list of names we can write down), so what
has to be checked against the installed library is that the constant still
COVERS every prefix that library reads.
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

from stealth_chrome_devtools_mcp.embedded import backend_env, backend_launch, singleton


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    """Divert every side effect of ``_start_server_process`` away from the real
    ``~/.stealth-mcp`` — including ``SERVER_STATE_FILE``, which
    ``_write_server_state`` reads off ``singleton`` at call time and which a
    live user backend is recorded in."""
    monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port")
    monkeypatch.setattr(singleton, "SERVER_STATE_FILE", tmp_path / "server.json")
    monkeypatch.setattr(singleton, "_ensure_state_dir", lambda: None)
    return tmp_path


@pytest.fixture()
def captured_spawn(monkeypatch):
    """``backend_launch.spawn`` replaced by a recorder, so nothing is created."""
    calls = []

    def fake_spawn(cmd, env, boot_log):
        calls.append({"cmd": cmd, "env": env, "boot_log": boot_log})
        return backend_launch.Launched(pid=4242, rung="test")

    monkeypatch.setattr(backend_launch, "spawn", fake_spawn)
    monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
    return calls


def _child_env(calls) -> dict[str, str]:
    assert len(calls) == 1, "the backend is spawned exactly once"
    return calls[0]["env"]


class TestTheChildEnvIsScrubbed:
    def test_an_inherited_empty_fastmcp_port_never_reaches_the_backend(
        self, isolated_state, monkeypatch, captured_spawn
    ):
        """THE F-890 pin. This exact variable, at this exact value, is what made
        the backend unstartable for as long as the client that held it ran."""
        monkeypatch.setenv("FASTMCP_PORT", "")

        singleton._start_server_process(4321)

        assert "FASTMCP_PORT" not in _child_env(captured_spawn)

    @pytest.mark.parametrize(
        "name",
        [
            "FASTMCP_PORT",
            "FASTMCP_HOST",
            "FASTMCP_LOG_LEVEL",
            "FASTMCP_SERVER_PORT",
            "FASTMCP_EXPERIMENTAL_ENABLE_NEW_OPENAPI_PARSER",
            "fastmcp_port",
        ],
    )
    def test_every_name_in_the_family_goes_whatever_its_case(
        self, isolated_state, monkeypatch, captured_spawn, name
    ):
        """One PREFIX covers all three families, and the fold is required on
        both sides: ``fastmcp``'s ``model_config`` is ``case_sensitive=False``
        and Windows folds env-var case in the OS as well."""
        monkeypatch.setenv(name, "anything")

        singleton._start_server_process(4321)

        env = _child_env(captured_spawn)
        assert not [k for k in env if k.upper().startswith("FASTMCP_")]

    def test_unrelated_names_survive(self, isolated_state, monkeypatch, captured_spawn):
        """The child must keep the rest of the parent's environment — PATH above
        all, without which it cannot locate its own interpreter or DLLs."""
        monkeypatch.setenv("FASTMCP_PORT", "")
        monkeypatch.setenv("F890_CANARY", "canary-value")

        singleton._start_server_process(4321)

        env = _child_env(captured_spawn)
        assert env["F890_CANARY"] == "canary-value"
        assert env.get("PATH"), "PATH must cross"
        assert "FASTMCP_PORT" not in env

    def test_the_no_auto_recovery_pop_is_still_performed(
        self, isolated_state, monkeypatch, captured_spawn
    ):
        """M8-2's removal was ABSORBED by the scrub, not displaced by it: a
        spawned backend must always reap its own orphaned browsers even when the
        CLI-invoking parent set this flag for its own import."""
        monkeypatch.setenv("STEALTH_MCP_NO_AUTO_RECOVERY", "1")

        singleton._start_server_process(4321)

        assert "STEALTH_MCP_NO_AUTO_RECOVERY" not in _child_env(captured_spawn)

    def test_scrubbing_never_touches_the_proxys_own_environment(
        self, isolated_state, monkeypatch, captured_spawn
    ):
        """It mutates the dict copy the composer owns. A scrub that reached
        ``os.environ`` would reconfigure the running proxy."""
        import os

        monkeypatch.setenv("FASTMCP_PORT", "")

        singleton._start_server_process(4321)

        assert os.environ.get("FASTMCP_PORT") == ""


class TestOurOwnEnvironmentIsScrubbedBeforeFastMCPIsImported:
    """F-890 review M5 — the child-env composer is not every entry path.

    ``_start_server_process`` covers the backend the PROXY spawns, which is how
    the 2026-09-18 incident happened and is the common case. It is not the only
    way this package comes to import ``fastmcp``: ``main()``'s ``runpy``
    fallthrough runs ``embedded/server.py`` IN THIS PROCESS (that is what
    ``--transport http`` does), and ``stealth-chrome-devtools serve --http``
    reaches the same line through ``_cmd_serve``. An operator with a stray
    ``FASTMCP_PORT=""`` in their shell gets the identical import-time
    ``ValidationError`` on both, with the identical absence of a log line.

    One function, in the one home, called once from the one entrypoint every
    path goes through.
    """

    def test_main_scrubs_this_process_before_reaching_runpy(self, monkeypatch):
        """THE M5 pin, driven through the real ``main()``."""
        import os

        from stealth_chrome_devtools_mcp import server as shim

        monkeypatch.setenv("FASTMCP_PORT", "")
        monkeypatch.setattr(sys, "argv", ["x", "--transport", "http"])
        seen = {}

        def fake_runpy(path, run_name):
            seen["fastmcp_port"] = os.environ.get("FASTMCP_PORT", "<absent>")

        monkeypatch.setattr(shim.runpy, "run_path", fake_runpy)

        shim.main()

        assert seen["fastmcp_port"] == "<absent>", (
            "the backend this process becomes must not inherit it either"
        )

    def test_the_stdio_proxy_branch_is_scrubbed_too(self, monkeypatch):
        """One call at the top of the one entrypoint, not one per branch: a
        scrub placed per-branch is the second-way defect waiting for a third
        branch. It costs the proxy nothing — it never reads a ``FASTMCP_*``
        name — and it means the env ``_start_server_process`` copies is already
        clean, with that composer's own scrub as the belt to this braces (the
        CLI's ``restart`` reaches it without passing through here at all)."""
        import os

        from stealth_chrome_devtools_mcp import server as shim

        monkeypatch.setenv("FASTMCP_PORT", "8000")
        monkeypatch.setattr(sys, "argv", ["x", "--transport", "stdio"])
        monkeypatch.setattr(shim, "_start_proxy_error_reporting", lambda: None)
        monkeypatch.setattr(
            "stealth_chrome_devtools_mcp.embedded.logging_setup.configure_logging",
            lambda _role: None,
        )
        monkeypatch.setattr(
            "stealth_chrome_devtools_mcp.embedded.singleton.ensure_server_running",
            lambda port: None,
        )
        monkeypatch.setattr(shim.runpy, "run_path", lambda *_a, **_kw: None)

        shim.main()

        assert "FASTMCP_PORT" not in os.environ

    def test_it_returns_what_it_removed_and_leaves_the_rest(self, monkeypatch):
        monkeypatch.setenv("FASTMCP_LOG_LEVEL", "TRACE")
        monkeypatch.setenv("STEALTH_MCP_NO_ERROR_REPORTING", "1")

        removed = backend_env.scrub_process_env()

        import os

        assert "FASTMCP_LOG_LEVEL" in removed
        assert os.environ["STEALTH_MCP_NO_ERROR_REPORTING"] == "1"

    @pytest.mark.parametrize(
        "spelling",
        ["STEALTH_MCP_NO_AUTO_RECOVERY", "stealth_mcp_no_auto_recovery"],
    )
    def test_it_never_deletes_a_typed_setting_of_ours(self, monkeypatch, spelling):
        """The two environments are not the same environment, and the table is
        not the same table (F-889 review N1).

        ``STEALTH_MCP_NO_AUTO_RECOVERY`` is removed from the CHILD's env because
        a spawned backend must reap its own orphans whatever its parent decided
        for itself. Applied to OUR OWN process it says the opposite thing: it is
        the operator's answer, set by ``cli.py``'s ``os.environ.setdefault``
        before ``doctor`` and ``status`` import anything, and deleting it
        silently re-enables the orphan reaping a read-only verb exists not to
        do. Measured on ``987d363``: ``scrub_process_env`` removed
        ``['FASTMCP_PORT', 'STEALTH_MCP_NO_AUTO_RECOVERY']``.

        Both spellings, because the removal is case-folded on both sides and a
        rule that only spares the shouting one spares nothing on Windows.
        """
        monkeypatch.setenv(spelling, "1")
        monkeypatch.setenv("FASTMCP_PORT", "")

        removed = backend_env.scrub_process_env()

        assert removed == ["FASTMCP_PORT"]
        assert os.environ.get(spelling) == "1", (
            "our own environment is the operator's; only the third party's "
            "names are ours to delete from it"
        )

    def test_the_ops_cli_scrubs_before_it_imports_the_server_too(self, monkeypatch):
        """``server.main()`` is not the only door onto an ``import fastmcp``
        (F-889 review N3): ``cli._server()`` is the other one, and ``status``,
        ``doctor``, ``stop``, ``restart``, ``cleanup`` and ``kill-orphans`` all
        go through it. An operator with a stray ``FASTMCP_PORT=""`` got the same
        import-time ``ValidationError`` from the verb they were running to
        diagnose it. It is already the one place that writes the environment
        before that import, so the scrub joins the line that is there.
        """
        from stealth_chrome_devtools_mcp import cli

        monkeypatch.setenv("STEALTH_MCP_NO_AUTO_RECOVERY", "1")
        monkeypatch.setenv("FASTMCP_PORT", "")

        cli._server()

        assert "FASTMCP_PORT" not in os.environ
        assert os.environ["STEALTH_MCP_NO_AUTO_RECOVERY"] == "1", (
            "the read-only guard the CLI sets for itself is not ours to delete"
        )

    def test_the_child_env_still_loses_it(self):
        """The other half of N1: narrowing the PROCESS scrub must not narrow the
        COMPOSER's, which is where M8-2's rule lives and always did."""
        env = {"STEALTH_MCP_NO_AUTO_RECOVERY": "1", "PATH": "/bin"}

        assert backend_env.scrub(env) == ["STEALTH_MCP_NO_AUTO_RECOVERY"]
        assert env == {"PATH": "/bin"}

    def test_the_child_env_reaches_for_no_other_setting_of_ours(self):
        """The composer audit, as a pin rather than a paragraph: exactly ONE
        ``STEALTH_MCP_*`` name is named, so a knob added to ``settings.py``
        tomorrow still reaches the backend it configures."""
        env = {
            "STEALTH_MCP_NO_AUTO_RECOVERY": "1",
            "STEALTH_MCP_NO_ERROR_REPORTING": "1",
            "STEALTH_MCP_LOG_DIR": "/tmp/logs",
            "FASTMCP_PORT": "",
            "PATH": "/bin",
        }

        backend_env.scrub(env)

        assert sorted(env) == [
            "PATH",
            "STEALTH_MCP_LOG_DIR",
            "STEALTH_MCP_NO_ERROR_REPORTING",
        ]

    def test_the_fastmcp_half_is_one_rule_and_not_a_second_one(self, monkeypatch):
        """Two mappings, two tables — but the ``FASTMCP_`` half is ONE rule, and
        a second prefix list here would be exactly the drift convention 4 is
        about. Pinned by asking both functions about the same spellings.

        Compared case-FOLDED on purpose: ``os.environ`` upper-cases its keys on
        Windows and keeps them verbatim on POSIX, so a name-for-name comparison
        would pin the platform rather than the rule.
        """
        spellings = {"FASTMCP_PORT": "", "fastmcp_log_level": "TRACE", "PATH": "/bin"}
        for name, value in spellings.items():
            monkeypatch.setenv(name, value)

        from_process = {name.upper() for name in backend_env.scrub_process_env()}
        from_composer = {name.upper() for name in backend_env.scrub(dict(spellings))}

        assert from_process == from_composer == {"FASTMCP_PORT", "FASTMCP_LOG_LEVEL"}

    def test_the_process_scrub_reports_at_a_level_that_can_be_heard(self):
        """``scrub_process_env`` runs BEFORE ``configure_logging`` (that is the
        point of it), and Python's last-resort handler emits at WARNING and
        above — so an INFO line there is written to nothing at all (review N4).
        The composer's call keeps INFO: by then the proxy's logging is up and
        the line lands in the durable log.
        """
        records: list[logging.LogRecord] = []

        class _ListHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("stealth.proxy")
        handler = _ListHandler()
        logger.addHandler(handler)
        prior = logger.level
        logger.setLevel(logging.DEBUG)
        os.environ["FASTMCP_PORT"] = "s3cret-value"
        try:
            backend_env.scrub_process_env()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(prior)
            os.environ.pop("FASTMCP_PORT", None)

        assert [r.levelno for r in records] == [logging.WARNING]
        assert "FASTMCP_PORT" in records[0].getMessage()
        assert "s3cret-value" not in records[0].getMessage()


class TestTheRemovalIsReportedByNameOnly:
    def test_the_info_line_names_the_variables_and_carries_no_value(self):
        """An environment variable is a place secrets live and this line reaches
        the durable proxy log, so the report is names and a count — never a
        value (F-869's discipline)."""
        records: list[logging.LogRecord] = []

        class _ListHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("stealth.proxy")
        handler = _ListHandler()
        logger.addHandler(handler)
        prior = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            removed = backend_env.scrub(
                {"FASTMCP_PORT": "s3cret-value", "PATH": "/bin"}
            )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(prior)

        assert removed == ["FASTMCP_PORT"]
        lines = [r.getMessage() for r in records if r.levelno == logging.INFO]
        assert any("FASTMCP_PORT" in line for line in lines)
        assert not any("s3cret-value" in line for line in lines)

    def test_a_clean_environment_reports_nothing_and_changes_nothing(self):
        env = {"PATH": "/bin", "HOME": "/root"}

        assert backend_env.scrub(env) == []
        assert env == {"PATH": "/bin", "HOME": "/root"}


class TestTheConstantStillCoversTheInstalledLibrary:
    def test_every_fastmcp_env_prefix_starts_with_ours(self):
        """The constant is ours and the pin is the library's. If ``fastmcp``
        ever reads a prefix outside ``FASTMCP_``, this fails on the day we
        upgrade rather than the day a backend will not start."""
        import importlib

        settings_module = importlib.import_module("fastmcp.settings")
        prefixes = settings_module.Settings.model_config.get("env_prefixes")

        assert prefixes, "fastmcp's Settings must declare env_prefixes"
        assert all(p.startswith(backend_env.FASTMCP_PREFIX) for p in prefixes), (
            f"fastmcp reads {prefixes}, which backend_env.FASTMCP_PREFIX "
            f"({backend_env.FASTMCP_PREFIX!r}) no longer covers"
        )

    def test_importing_fastmcp_under_the_inherited_variable_really_does_crash(self):
        """The mechanism, not an assertion about it. Everything else in this
        file would pass just as happily if ``FASTMCP_PORT=""`` were harmless;
        this is the node that says the scrub is load-bearing.

        A subprocess because the crash is at IMPORT and this process has already
        imported ``fastmcp``. Measured 2026-09-19 on ``fastmcp`` 2.11.2 /
        pydantic 2.11: ``ValidationError: 1 validation error for Settings /
        port / Input should be a valid integer, unable to parse string as an
        integer [type=int_parsing, input_value='', input_type=str]``.
        """
        import os
        import subprocess
        import sys

        def _import_fastmcp(env: dict[str, str]) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, "-c", "import fastmcp"],
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
                check=False,
            )

        inherited = {**os.environ, "FASTMCP_PORT": ""}
        crashed = _import_fastmcp(inherited)
        assert crashed.returncode != 0, "the inherited empty value must crash"
        assert "ValidationError" in crashed.stderr
        assert "port" in crashed.stderr

        scrubbed = dict(inherited)
        backend_env.scrub(scrubbed)
        clean = _import_fastmcp(scrubbed)
        assert clean.returncode == 0, (
            f"the scrubbed environment must import cleanly: {clean.stderr}"
        )

    def test_the_field_that_crashed_us_is_still_an_int(self):
        """``port: int`` is why an EMPTY value is a ValidationError rather than
        a harmless override. Named so the finding's mechanism stays checkable."""
        import importlib

        settings_module = importlib.import_module("fastmcp.settings")

        assert settings_module.Settings.model_fields["port"].annotation is int
