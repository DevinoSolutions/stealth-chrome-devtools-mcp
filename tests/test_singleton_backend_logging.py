"""Pinning test for M3-3: the Popen boot-log redirect (F-303/F-503).

Before this change, ``_start_server_process`` spawned the backend with
``stdout``/``stderr`` = ``DEVNULL`` — an import-time crash in
``embedded/server.py`` (edited, syntax error, whatever) died before any
in-process logging could install itself, and the raw crash text went
nowhere. This pins that a boot-time crash's traceback now lands in
``backend-boot.log``.
"""

import sys
import time

import pytest
import singleton


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    """Point singleton state (and, via env, the log dir) at tmp_path so this
    test never touches ~/.stealth-mcp — same pattern as
    test_singleton_version_aware.py's fixture of the same name."""
    monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
    monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port")
    monkeypatch.setattr(
        singleton, "SERVER_STATE_FILE", tmp_path / "server.json", raising=False
    )
    monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path / "logs"))
    return tmp_path


class TestBootLogRedirect:
    def test_boot_crash_is_captured(self, isolated_state, monkeypatch):
        crashing_module = isolated_state / "crashing_boot_target.py"
        crashing_module.write_text(
            "raise RuntimeError('injected boot crash for test_boot_crash_is_captured')\n"
        )
        monkeypatch.setattr(
            singleton,
            "_server_process_cmd",
            lambda port: [sys.executable, str(crashing_module)],
        )

        singleton._start_server_process(19999)

        boot_log = isolated_state / "logs" / "backend-boot.log"
        deadline = time.monotonic() + 10
        text = ""
        while time.monotonic() < deadline:
            if boot_log.exists():
                text = boot_log.read_text(encoding="utf-8")
                if "injected boot crash" in text:
                    break
            time.sleep(0.05)

        assert "injected boot crash for test_boot_crash_is_captured" in text
        assert "RuntimeError" in text

    def test_stdin_still_devnull(self, isolated_state, monkeypatch):
        # stdin=DEVNULL remains legitimate (gate adaptation #7) - only
        # stdout/stderr move off DEVNULL. Confirmed via the real kwargs a
        # mocked Popen receives, so this pins the kwarg shape itself.
        import subprocess
        from unittest.mock import MagicMock

        fake_proc = MagicMock()
        fake_proc.pid = 4242
        popen_mock = MagicMock(return_value=fake_proc)
        monkeypatch.setattr(singleton.subprocess, "Popen", popen_mock)
        monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")

        singleton._start_server_process(4321)

        _, kwargs = popen_mock.call_args
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] != subprocess.DEVNULL
        assert kwargs["stderr"] != subprocess.DEVNULL

    def test_boot_log_path_under_resolved_log_dir(self, isolated_state, monkeypatch):
        from unittest.mock import MagicMock

        fake_proc = MagicMock()
        fake_proc.pid = 4242
        monkeypatch.setattr(
            singleton.subprocess, "Popen", MagicMock(return_value=fake_proc)
        )
        monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")

        singleton._start_server_process(4321)

        assert (isolated_state / "logs" / "backend-boot.log").exists()

    def test_server_process_cmd_unchanged_shape(self):
        # _start_server_process's extracted command-builder must still invoke
        # the same module the same way (no change to what actually launches).
        cmd = singleton._server_process_cmd(4321)
        assert cmd == [
            sys.executable,
            "-m",
            "stealth_chrome_devtools_mcp",
            "--transport",
            "http",
            "--port",
            "4321",
            "--host",
            "127.0.0.1",
        ]
