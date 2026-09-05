"""Log-hygiene pins: F-830 (the unbounded boot log) and F-840 (the pruner that
destroyed post-mortems).

**F-830.** ``~/.stealth-mcp/logs/backend-boot.log`` reached **794 MB** on the
reporting machine: ~13M uvicorn access-log lines (the watchdog probe pair, one
request every ~2 s per live stdio proxy) appended to ONE file across every boot
the machine ever did. Two halves, both pinned here:

1. the backend's uvicorn run-config turns HTTP access logging OFF, so the spam
   is never emitted (our own ``stealth.backend`` tool-call logging is a
   different logger and is untouched);
2. the LAUNCHER rolls the boot log before handing its fd to a new backend —
   the only moment rotation is possible, because the running child holds that
   fd open for its whole life.

**F-840.** ``prune_old_logs`` swept a dead backend's ``backend-<pid>.log`` and
``backend-<pid>-fault.log`` within hours of its crash (2026-08-30: the
OOM-killed worker's logs were gone by the next morning, so the death
investigation started blind). Dead-backend logs are exactly the artifact a
post-mortem needs, so they now get a retention exemption.

Every filesystem assertion runs against ``tmp_path``; nothing here reads or
writes the real ``~/.stealth-mcp``.
"""

from __future__ import annotations

import ast
import faulthandler
import logging
import os
import sys
import threading
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:  # ``Path`` is annotation-only here since the uvicorn-config
    # guard started deriving its file set from tests/source_scan.py.
    from pathlib import Path

from stealth_chrome_devtools_mcp.embedded import logging_setup, singleton
from stealth_chrome_devtools_mcp.embedded.logging_setup import (
    BOOT_LOG_NAME,
    backend_uvicorn_config,
    bootstrap_backend_process_logging,
    prune_old_logs,
    roll_boot_log,
)

DAY = 86400.0


@pytest.fixture(autouse=True)
def _cleanup_stealth_loggers():
    """No test may leave an open handler behind — on Windows an open
    ``RotatingFileHandler`` locks its file and breaks ``tmp_path`` cleanup."""
    yield
    manager = logging.Logger.manager
    for name in list(manager.loggerDict):
        if name.startswith("stealth."):
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()


@pytest.fixture()
def tiny_boot_log_cap(monkeypatch):
    """Exercise the rotation policy without writing tens of MB per test. The
    shipped threshold is asserted separately, on the real constant."""
    monkeypatch.setattr(logging_setup, "_BOOT_LOG_MAX_BYTES", 64)


def _age(path: Path, days: float) -> None:
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))


def _write_boot_log(log_dir: Path, payload: bytes) -> Path:
    boot = log_dir / BOOT_LOG_NAME
    boot.write_bytes(payload)
    return boot


# ---------------------------------------------------------------------------
# F-830 (a)/(b) — roll_boot_log
# ---------------------------------------------------------------------------


class TestRollBootLog:
    def test_oversize_boot_log_is_rolled_aside(self, tmp_path, tiny_boot_log_cap):
        boot = _write_boot_log(tmp_path, b"x" * 4096)

        returned = roll_boot_log(tmp_path)

        assert returned == boot
        # The live name is free again, so the new backend appends from zero.
        assert not boot.exists()
        assert (tmp_path / f"{BOOT_LOG_NAME}.1").read_bytes() == b"x" * 4096

    def test_small_boot_log_is_left_alone(self, tmp_path, tiny_boot_log_cap):
        boot = _write_boot_log(tmp_path, b"one previous boot's traceback\n")

        returned = roll_boot_log(tmp_path)

        assert returned == boot
        assert boot.read_bytes() == b"one previous boot's traceback\n"
        assert not (tmp_path / f"{BOOT_LOG_NAME}.1").exists()

    def test_missing_boot_log_is_not_an_error(self, tmp_path):
        assert roll_boot_log(tmp_path) == tmp_path / BOOT_LOG_NAME
        assert not (tmp_path / f"{BOOT_LOG_NAME}.1").exists()

    def test_rolls_are_bounded_not_accumulated(self, tmp_path, tiny_boot_log_cap):
        """Repeated oversize boots must not grow an unbounded ``.N`` tail —
        that would only rename the 794 MB problem."""
        for generation in range(6):
            _write_boot_log(tmp_path, str(generation).encode() * 4096)
            roll_boot_log(tmp_path)

        rolls = sorted(tmp_path.glob(f"{BOOT_LOG_NAME}.*"))
        assert 0 < len(rolls) <= logging_setup._BOOT_LOG_BACKUPS
        # ``.1`` is newest: it holds the generation that was live most recently.
        assert (tmp_path / f"{BOOT_LOG_NAME}.1").read_bytes()[:1] == b"5"

    def test_shipped_threshold_is_bounded_and_sane(self):
        assert 10 * 1024 * 1024 <= logging_setup._BOOT_LOG_MAX_BYTES <= 50 * 1024 * 1024
        assert 1 <= logging_setup._BOOT_LOG_BACKUPS <= 5

    def test_never_raises_when_the_directory_is_gone(self, tmp_path):
        roll_boot_log(tmp_path / "does-not-exist")  # must not raise


class TestLauncherRollsBeforeOpeningTheFd:
    def test_start_server_process_rolls_the_boot_log(
        self, tmp_path, monkeypatch, tiny_boot_log_cap
    ):
        """Rotation has to happen in ``_start_server_process``: once ``Popen``
        inherits the fd the child pins that file for its lifetime, and no
        in-process or external rotation can move it any more."""
        monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
        monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port")
        monkeypatch.setattr(
            singleton, "SERVER_STATE_FILE", tmp_path / "server.json", raising=False
        )
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(log_dir))
        _write_boot_log(log_dir, b"y" * 4096)

        fake_proc = MagicMock()
        fake_proc.pid = 4242
        monkeypatch.setattr(
            singleton.subprocess, "Popen", MagicMock(return_value=fake_proc)
        )
        monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")

        singleton._start_server_process(4321)

        assert (log_dir / f"{BOOT_LOG_NAME}.1").read_bytes() == b"y" * 4096
        # The fresh boot log the child was handed starts empty.
        assert (log_dir / BOOT_LOG_NAME).stat().st_size == 0


# ---------------------------------------------------------------------------
# F-830 (f) — the uvicorn access log is OFF in the composed run-config
# ---------------------------------------------------------------------------


class TestBackendUvicornConfig:
    def test_access_logging_is_off(self):
        assert backend_uvicorn_config()["access_log"] is False

    def test_still_carries_the_graceful_shutdown_timeout(self):
        """F-809's contract rides in the same config — composing them in one
        place must not drop it."""
        timeout = backend_uvicorn_config()["timeout_graceful_shutdown"]
        assert isinstance(timeout, (int, float))
        assert 0 < timeout < 5

    def test_the_http_run_call_uses_the_composed_config(self):
        """Source-level: the ``mcp.run`` call lives under
        ``if __name__ == "__main__":`` and is unreachable by import."""
        # Derived from tests/source_scan.py rather than hard-wired to server.py:
        # plan_SERVERSPLIT §1.4 — a source-text guard naming ONE file goes
        # vacuous, not red, when what it polices moves. The ``mcp.run`` call
        # itself stays in server.py's __main__ block, so the expected count is
        # still exactly 1 across the whole set.
        from source_scan import tool_source_files

        composed = [
            kw.value
            for path in tool_source_files()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "uvicorn_config"
        ]
        assert len(composed) == 1
        call = composed[0]
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Name)
        assert call.func.id == "backend_uvicorn_config"


# ---------------------------------------------------------------------------
# F-840 (c)/(d)/(e) — the pruner keeps post-mortems
# ---------------------------------------------------------------------------


def _write_backend_set(log_dir: Path, pid: int, age_days: float) -> None:
    """One dead backend's artifacts: its structured log and its fault log."""
    for path in (log_dir / f"backend-{pid}.log", log_dir / f"backend-{pid}-fault.log"):
        path.write_text(f"pid {pid}\n", encoding="utf-8")
        _age(path, age_days)


def _set_exists(log_dir: Path, pid: int) -> bool:
    return (log_dir / f"backend-{pid}.log").exists() and (
        log_dir / f"backend-{pid}-fault.log"
    ).exists()


class TestPrunerKeepsPostMortems:
    def test_keeps_the_most_recent_dead_backend_sets_despite_age(self, tmp_path):
        """The 2026-08-30 regression verbatim: by the time anyone looked, the
        OOM-killed worker's set was old enough that an age+count sweep had
        already deleted the one artifact the investigation needed."""
        for index, pid in enumerate((1001, 1002, 1003, 1004, 1005)):
            _write_backend_set(tmp_path, pid, age_days=30 + index)

        prune_old_logs(tmp_path, keep_days=1, keep_files=1)

        for pid in (1001, 1002, 1003):
            assert _set_exists(tmp_path, pid), pid

    def test_young_fault_logs_survive_a_brutal_sweep(self, tmp_path):
        for pid in range(2000, 2020):
            _write_backend_set(tmp_path, pid, age_days=10)

        prune_old_logs(tmp_path, keep_days=1, keep_files=1)

        assert len(list(tmp_path.glob("*-fault.log"))) == 20

    def test_genuinely_old_surplus_logs_are_still_pruned(self, tmp_path):
        """Regression guard: the exemption is narrow. Proxy logs and long-dead
        surplus backend sets must still go, or the sweep becomes a no-op."""
        for index in range(10):
            proxy_log = tmp_path / f"proxy-{3000 + index}.log"
            proxy_log.write_text("proxy\n", encoding="utf-8")
            _age(proxy_log, 30 + index)
        for index in range(10):
            _write_backend_set(tmp_path, 4000 + index, age_days=400 + index)

        prune_old_logs(tmp_path, keep_days=7, keep_files=50)

        assert list(tmp_path.glob("proxy-*.log")) == []
        for pid in (4000, 4001, 4002):
            assert _set_exists(tmp_path, pid), pid
        for pid in range(4003, 4010):
            assert not (tmp_path / f"backend-{pid}.log").exists(), pid
            assert not (tmp_path / f"backend-{pid}-fault.log").exists(), pid

    def test_boot_log_rolls_are_not_post_mortem_exempt(self, tmp_path):
        """``backend-boot.log`` is shared across boots, not one backend's
        post-mortem — the F-840 exemption must not re-open F-830."""
        for index in (1, 2):
            roll = tmp_path / f"{BOOT_LOG_NAME}.{index}"
            roll.write_text("old boots\n", encoding="utf-8")
            _age(roll, 400)

        prune_old_logs(tmp_path, keep_days=7, keep_files=50)

        assert list(tmp_path.glob(f"{BOOT_LOG_NAME}.*")) == []

    def test_prune_never_raises_on_missing_dir(self, tmp_path):
        prune_old_logs(tmp_path / "nope", keep_days=7, keep_files=2)


# ---------------------------------------------------------------------------
# F-840 bonus — the boot line names the launch context
# ---------------------------------------------------------------------------


@pytest.fixture()
def restore_process_hooks():
    """``bootstrap_backend_process_logging`` mutates process-global state
    (excepthooks, faulthandler, the bootstrapped-roles set). Put every one of
    them back so this file leaves the interpreter as it found it."""
    prior_excepthook = sys.excepthook
    prior_thread_excepthook = threading.excepthook
    was_fault_enabled = faulthandler.is_enabled()
    logging_setup._bootstrapped_roles.discard("backend")
    yield
    sys.excepthook = prior_excepthook
    threading.excepthook = prior_thread_excepthook
    faulthandler.disable()
    if was_fault_enabled:
        faulthandler.enable()
    logging_setup._bootstrapped_roles.discard("backend")


class TestStartupLineRecordsLaunchContext:
    def test_argv_is_recorded_once_at_startup(
        self, tmp_path, monkeypatch, restore_process_hooks
    ):
        """A post-mortem must be able to tell a console-attached
        ``serve --http`` birth from the detached ``_start_server_process``
        spawn — only the launch argv distinguishes them."""
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        monkeypatch.setattr(sys, "argv", ["prog", "--transport", "http"])
        logging_setup._bootstrapped_roles.discard("backend")

        log_path = bootstrap_backend_process_logging()
        for handler in logging.getLogger("stealth.backend").handlers:
            handler.flush()

        text = log_path.read_text(encoding="utf-8")
        assert "backend process starting" in text
        assert "argv=" in text
        assert "--transport" in text
