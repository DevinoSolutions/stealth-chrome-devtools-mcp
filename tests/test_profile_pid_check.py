"""Regression: ``_profile_has_running_browser`` must survive an OSError / psutil
error raised by the PID lookup and fall back to the marker-file heuristic, rather
than raising ``NameError`` while evaluating ``psutil.Error`` in its except clause.

server.py referenced ``psutil.Error`` at the handler but never imported ``psutil``
(caught by ruff F821 during the 2.5-gates workstream). Any real failure in the
``try`` body therefore masked itself with a NameError and crashed the caller.
"""

from stealth_chrome_devtools_mcp.embedded import server


def test_profile_pid_check_survives_os_error(tmp_path, monkeypatch):
    def _raise(_user_data_dir):
        raise OSError("simulated PID-lookup failure")

    monkeypatch.setattr(server.process_cleanup, "_get_browser_pids_for_profile", _raise)

    # tmp_path carries no Chrome singleton markers, so the liveness heuristic must
    # fall back to False without raising.
    assert server._profile_has_running_browser(tmp_path) is False
