"""plan_RELEASE W2 — platform lifecycle assertions with no existing home.

The three-OS gate newly runs the WHOLE suite on Windows and macOS, so the many
OS-branching behaviors that were only ever exercised on the Ubuntu runner
(``msvcrt`` vs ``fcntl``, the ``os.name`` session-root default, the Windows
detach ``creationflags``) now execute on the branch that matches each runner —
without a second copy of those tests. This module adds only the genuinely
MISSING platform assertion: the singleton's real file-lock branch and its
contention semantics.

Survey (plan_RELEASE W2 "survey first"): every existing singleton test
monkeypatches ``singleton._exclusive_lock`` away, so the real ``msvcrt.locking``
/ ``fcntl.flock`` branch — and lock contention — is asserted nowhere. The port-0
allocation, ``server.json`` PID/port ownership, and profile-handle cleanup are
already covered (``test_singleton_port_fallback``, ``test_singleton_version_aware``,
``test_close_instance_offload`` / ``test_browser_integration``) and are exercised
on each OS by the matrix; they are deliberately NOT duplicated here.

This case is applicable on every OS (each runs its own lock backend) and is
therefore never skipped or xfailed.
"""

from __future__ import annotations

from stealth_chrome_devtools_mcp.embedded import singleton


def _isolate_lock(monkeypatch, tmp_path):
    """Point the singleton lock at tmp_path so this never touches ~/.stealth-mcp."""
    monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
    monkeypatch.setattr(singleton, "LOCK_FILE", tmp_path / "singleton.lock")


class TestExclusiveLockRealBranch:
    """Exercise the REAL platform lock backend the runner ships (msvcrt on
    Windows, fcntl on POSIX) — not a monkeypatched stand-in."""

    def test_acquires_and_creates_lock_file(self, tmp_path, monkeypatch):
        _isolate_lock(monkeypatch, tmp_path)
        with singleton._exclusive_lock() as acquired:
            assert acquired is True
            assert (tmp_path / "singleton.lock").exists()

    def test_second_holder_is_refused_while_first_is_held(self, tmp_path, monkeypatch):
        # The true purpose of the lock: a second acquisition while the first is
        # held must fail (yield False), not deadlock or raise. This is the single
        # process safety guarantee singleton startup relies on.
        _isolate_lock(monkeypatch, tmp_path)
        with singleton._exclusive_lock() as first:
            assert first is True
            with singleton._exclusive_lock() as second:
                assert second is False

    def test_lock_is_released_and_reacquirable(self, tmp_path, monkeypatch):
        # Once the first holder exits its context, the lock backend must release
        # (msvcrt LK_UNLCK / fcntl LOCK_UN) so a later acquisition succeeds.
        _isolate_lock(monkeypatch, tmp_path)
        with singleton._exclusive_lock() as first:
            assert first is True
        with singleton._exclusive_lock() as again:
            assert again is True
