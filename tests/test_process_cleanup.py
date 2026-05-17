"""Unit tests for ProcessCleanup — orphan recovery guards and metadata.

Validates that:
- _init_time is set before recovery runs
- _normalize_process_metadata handles legacy (int) and current (dict) formats
- Recovery filtering respects create_time vs _init_time
- _extract_profile_dir_from_cmdline parses both --flag=value and --flag value
- _is_browser_process_name matches Chrome/Edge/Chromium/Brave

No browser required — pure logic tests with mocked psutil.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

from process_cleanup import ProcessCleanup


# ---------------------------------------------------------------------------
# ProcessCleanup init
# ---------------------------------------------------------------------------

class TestProcessCleanupInit:
    def test_init_time_set_before_recovery(self):
        """_init_time must be set before _recover_orphaned_processes runs."""
        call_order = []

        original_init = ProcessCleanup.__init__

        def patched_recover(self_obj):
            call_order.append(("recover", hasattr(self_obj, "_init_time")))

        with patch.object(ProcessCleanup, "_recover_orphaned_processes", patched_recover):
            with patch.object(ProcessCleanup, "_setup_cleanup_handlers", lambda self: None):
                pc = ProcessCleanup.__new__(ProcessCleanup)
                pc.pid_file = Path(os.path.expanduser("~/.stealth_browser_pids_test.json"))
                pc.tracked_pids = set()
                pc.browser_processes = {}
                pc.orphan_profile_max_age_seconds = 21600
                pc._init_time = time.time()
                pc._setup_cleanup_handlers()
                pc._recover_orphaned_processes()

        assert call_order == [("recover", True)]


# ---------------------------------------------------------------------------
# _normalize_process_metadata
# ---------------------------------------------------------------------------

class TestNormalizeProcessMetadata:
    def test_legacy_int_format(self):
        raw = {"instance-1": 12345}
        result = ProcessCleanup._normalize_process_metadata(raw)
        assert "instance-1" in result
        meta = result["instance-1"]
        assert meta["pid"] == 12345
        assert meta["create_time"] is None
        assert meta["user_data_dir"] is None

    def test_dict_format_with_create_time(self):
        raw = {
            "instance-2": {
                "pid": 9999,
                "create_time": 1700000000.0,
                "user_data_dir": "/tmp/profile",
                "uses_custom_data_dir": True,
                "timestamp": 1700000001.0,
            }
        }
        result = ProcessCleanup._normalize_process_metadata(raw)
        meta = result["instance-2"]
        assert meta["pid"] == 9999
        assert meta["create_time"] == 1700000000.0
        assert meta["user_data_dir"] is not None

    def test_dict_without_pid_skipped(self):
        raw = {"bad": {"no_pid": True}}
        result = ProcessCleanup._normalize_process_metadata(raw)
        assert len(result) == 0

    def test_non_int_non_dict_skipped(self):
        raw = {"bad": "string-value", "also-bad": [1, 2, 3]}
        result = ProcessCleanup._normalize_process_metadata(raw)
        assert len(result) == 0

    def test_mixed_formats(self):
        raw = {
            "legacy": 1111,
            "modern": {"pid": 2222, "create_time": 1700000000.0},
            "bad": "skip",
        }
        result = ProcessCleanup._normalize_process_metadata(raw)
        assert len(result) == 2
        assert result["legacy"]["pid"] == 1111
        assert result["modern"]["pid"] == 2222


# ---------------------------------------------------------------------------
# _extract_profile_dir_from_cmdline
# ---------------------------------------------------------------------------

class TestExtractProfileDir:
    def test_equals_format(self):
        cmdline = ["chrome.exe", "--user-data-dir=/tmp/profile", "--no-sandbox"]
        result = ProcessCleanup._extract_profile_dir_from_cmdline(cmdline)
        assert result is not None
        assert "profile" in result

    def test_space_format(self):
        cmdline = ["chrome.exe", "--user-data-dir", "/tmp/profile2"]
        result = ProcessCleanup._extract_profile_dir_from_cmdline(cmdline)
        assert result is not None
        assert "profile2" in result

    def test_no_profile_dir(self):
        cmdline = ["chrome.exe", "--headless"]
        result = ProcessCleanup._extract_profile_dir_from_cmdline(cmdline)
        assert result is None

    def test_empty_cmdline(self):
        result = ProcessCleanup._extract_profile_dir_from_cmdline([])
        assert result is None

    def test_space_format_at_end(self):
        """--user-data-dir at end of cmdline with no following arg."""
        cmdline = ["chrome.exe", "--user-data-dir"]
        result = ProcessCleanup._extract_profile_dir_from_cmdline(cmdline)
        assert result is None


# ---------------------------------------------------------------------------
# _is_browser_process_name
# ---------------------------------------------------------------------------

class TestIsBrowserProcessName:
    @pytest.mark.parametrize("name,expected", [
        ("chrome.exe", True),
        ("Chrome", True),
        ("google-chrome-stable", True),
        ("chromium-browser", True),
        ("msedge.exe", True),
        ("Microsoft Edge", True),
        ("brave", True),
        ("Brave Browser", True),
        ("python.exe", False),
        ("node", False),
        ("firefox", False),
        ("", False),
    ])
    def test_browser_detection(self, name, expected):
        assert ProcessCleanup._is_browser_process_name(name) is expected


# ---------------------------------------------------------------------------
# Recovery filtering (create_time guard)
# ---------------------------------------------------------------------------

class TestRecoveryFiltering:
    """Test that _kill_processes_for_metadata respects create_time in recovery mode."""

    def _make_cleanup(self):
        """Create a ProcessCleanup without running __init__."""
        pc = ProcessCleanup.__new__(ProcessCleanup)
        pc.pid_file = Path(os.path.expanduser("~/.stealth_browser_pids_test.json"))
        pc.tracked_pids = set()
        pc.browser_processes = {}
        pc.orphan_profile_max_age_seconds = 21600
        pc._init_time = 1700000100.0  # server started at T+100
        return pc

    def test_old_process_killed_in_recovery(self):
        """Process with create_time < _init_time should be killed."""
        pc = self._make_cleanup()
        metadata = {
            "pid": 99999,
            "create_time": 1700000050.0,  # started before server (T+50 < T+100)
            "user_data_dir": None,
            "uses_custom_data_dir": None,
            "timestamp": 0,
        }

        killed = []
        def mock_kill_by_pid(pid, instance_id):
            killed.append(pid)
            return True

        with patch.object(pc, "_get_browser_pids_for_profile", return_value={99999}):
            with patch.object(pc, "_kill_process_by_pid", mock_kill_by_pid):
                with patch("process_cleanup.psutil.Process") as mock_proc_cls:
                    mock_proc = MagicMock()
                    mock_proc.create_time.return_value = 1700000050.0
                    mock_proc_cls.return_value = mock_proc
                    pc._kill_processes_for_metadata("test-instance", metadata, recovery=True)

        assert 99999 in killed

    def test_new_process_spared_in_recovery(self):
        """Process with create_time >= _init_time should NOT be killed."""
        pc = self._make_cleanup()
        metadata = {
            "pid": 88888,
            "create_time": 1700000200.0,  # started AFTER server (T+200 > T+100)
            "user_data_dir": None,
            "uses_custom_data_dir": None,
            "timestamp": 0,
        }

        killed = []
        def mock_kill_by_pid(pid, instance_id):
            killed.append(pid)
            return True

        with patch.object(pc, "_get_browser_pids_for_profile", return_value={88888}):
            with patch.object(pc, "_kill_process_by_pid", mock_kill_by_pid):
                with patch("process_cleanup.psutil.Process") as mock_proc_cls:
                    mock_proc = MagicMock()
                    mock_proc.create_time.return_value = 1700000200.0
                    mock_proc_cls.return_value = mock_proc
                    pc._kill_processes_for_metadata("test-instance", metadata, recovery=True)

        assert 88888 not in killed

    def test_non_recovery_kills_regardless(self):
        """Normal (non-recovery) cleanup kills without checking create_time."""
        pc = self._make_cleanup()
        metadata = {
            "pid": 77777,
            "create_time": 1700000200.0,  # newer than server
            "user_data_dir": None,
            "uses_custom_data_dir": None,
            "timestamp": 0,
        }

        killed = []
        def mock_kill_by_pid(pid, instance_id):
            killed.append(pid)
            return True

        with patch.object(pc, "_get_browser_pids_for_profile", return_value={77777}):
            with patch.object(pc, "_kill_process_by_pid", mock_kill_by_pid):
                pc._kill_processes_for_metadata("test-instance", metadata, recovery=False)

        assert 77777 in killed


# ---------------------------------------------------------------------------
# PID file persistence
# ---------------------------------------------------------------------------

class TestPidFilePersistence:
    def test_save_and_load(self, tmp_path):
        pc = ProcessCleanup.__new__(ProcessCleanup)
        pc.pid_file = tmp_path / "test_pids.json"
        pc.tracked_pids = set()
        pc.browser_processes = {
            "inst-1": {
                "pid": 1234,
                "create_time": 1700000000.0,
                "user_data_dir": "/tmp/test",
                "uses_custom_data_dir": True,
                "timestamp": time.time(),
            }
        }
        pc.orphan_profile_max_age_seconds = 21600
        pc._init_time = time.time()

        pc._save_tracked_pids()
        assert pc.pid_file.exists()

        loaded = pc._load_tracked_pids()
        assert "inst-1" in loaded
        assert loaded["inst-1"]["pid"] == 1234
        assert loaded["inst-1"]["create_time"] == 1700000000.0
