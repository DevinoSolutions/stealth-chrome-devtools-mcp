"""Regression: ``_profile_has_running_browser`` must survive an OSError / psutil
error raised by the PID lookup and fall back to the marker-file heuristic, rather
than raising ``NameError`` while evaluating ``psutil.Error`` in its except clause.

server.py referenced ``psutil.Error`` at the handler but never imported ``psutil``
(caught by ruff F821 during the 2.5-gates workstream). Any real failure in the
``try`` body therefore masked itself with a NameError and crashed the caller.

SOFT PIN UPDATED for F-871. What must never change is that the failure is
survived rather than raised. What the surviving ANSWER is was main's behaviour,
not an invariant, and main's was not uniform: an unanswerable ``_pid_alive``
counts a profile as held, while an unanswerable process scan counted it free.
The direction is now uniform — an unanswerable question resolves toward HELD,
because one extra walk is survivable and two browsers on one profile is not —
and it is visible only where the process scan is the ONLY witness. On POSIX the
``SingletonLock`` still answers (and an empty tmp_path holds no lock, so the
answer is still False); on Windows Chrome writes no readable lock at all, so
there is nothing to fall back to.
"""

from stealth_chrome_devtools_mcp.embedded import clone_storage, profile_lock


def test_profile_pid_check_survives_os_error(tmp_path, monkeypatch):
    def _raise(_user_data_dir):
        raise OSError("simulated PID-lookup failure")

    monkeypatch.setattr(
        clone_storage.process_cleanup, "_get_browser_pids_for_profile", _raise
    )

    held = clone_storage._profile_has_running_browser(tmp_path)
    assert held is not profile_lock._LOCK_IS_A_WITNESS
