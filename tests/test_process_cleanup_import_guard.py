"""Tests for the side-effect-free ProcessCleanup init (M11a-2).

Validates:
- Constructing ProcessCleanup() does NOT call _setup_cleanup_handlers or
  _recover_orphaned_processes (the import-time footgun F-124 is gone).
- activate() runs recovery when no_auto_recovery is False and is a no-op
  when it is True.
- recover_orphans() delegates to _recover_orphaned_processes.
- The conftest sets STEALTH_MCP_NO_AUTO_RECOVERY=1 for the test session.
"""

import os
from unittest.mock import patch

import pytest
from process_cleanup import ProcessCleanup


class TestSideEffectFreeInit:
    """ProcessCleanup() must not call handlers or recovery on construction."""

    def test_construction_does_not_call_setup_handlers(self):
        with patch.object(ProcessCleanup, "_setup_cleanup_handlers") as mock_setup:
            with patch.object(ProcessCleanup, "_recover_orphaned_processes"):
                pc = ProcessCleanup()
            mock_setup.assert_not_called()

    def test_construction_does_not_call_recover(self):
        with patch.object(ProcessCleanup, "_setup_cleanup_handlers"):
            with patch.object(
                ProcessCleanup, "_recover_orphaned_processes"
            ) as mock_recover:
                pc = ProcessCleanup()
            mock_recover.assert_not_called()


class TestActivate:
    """activate() is the single trigger for cleanup handlers + recovery."""

    def test_activate_runs_handlers_and_recovery_when_allowed(self):
        env = os.environ.copy()
        env.pop("STEALTH_MCP_NO_AUTO_RECOVERY", None)
        with (
            patch.object(ProcessCleanup, "_setup_cleanup_handlers") as mock_setup,
            patch.object(ProcessCleanup, "_recover_orphaned_processes") as mock_recover,
            patch.dict(os.environ, env, clear=True),
        ):
            pc = ProcessCleanup()
            pc.activate()
            mock_setup.assert_called_once()
            mock_recover.assert_called_once()

    def test_activate_is_noop_when_no_auto_recovery_set(self):
        with (
            patch.object(ProcessCleanup, "_setup_cleanup_handlers") as mock_setup,
            patch.object(ProcessCleanup, "_recover_orphaned_processes") as mock_recover,
            patch.dict(os.environ, {"STEALTH_MCP_NO_AUTO_RECOVERY": "1"}),
        ):
            pc = ProcessCleanup()
            pc.activate()
            mock_setup.assert_not_called()
            mock_recover.assert_not_called()

    @pytest.mark.parametrize("truthy_val", ["true", "yes", "on", "True", "1"])
    def test_activate_honors_truthy_values(self, truthy_val):
        with (
            patch.object(ProcessCleanup, "_setup_cleanup_handlers") as mock_setup,
            patch.object(ProcessCleanup, "_recover_orphaned_processes") as mock_recover,
            patch.dict(os.environ, {"STEALTH_MCP_NO_AUTO_RECOVERY": truthy_val}),
        ):
            pc = ProcessCleanup()
            pc.activate()
            mock_setup.assert_not_called()
            mock_recover.assert_not_called()


class TestRecoverOrphans:
    """recover_orphans() is the public seam for CLI kill-orphans."""

    def test_recover_orphans_delegates(self):
        with (
            patch.object(ProcessCleanup, "_setup_cleanup_handlers"),
            patch.object(ProcessCleanup, "_recover_orphaned_processes") as mock_recover,
        ):
            pc = ProcessCleanup()
            pc.recover_orphans()
            mock_recover.assert_called_once()


class TestConftestOptOut:
    """The conftest sets the env var so activate() is always a no-op in tests."""

    def test_no_auto_recovery_env_set_during_tests(self):
        val = os.environ.get("STEALTH_MCP_NO_AUTO_RECOVERY", "")
        assert val, (
            "STEALTH_MCP_NO_AUTO_RECOVERY must be set in the test session "
            "(conftest.py setdefault)"
        )
