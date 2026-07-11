"""A clone whose owning instance has closed, but whose on-close delete was
deferred (Windows held a file at close time), stays tracked (pid cleared) with
its directory still on disk. The deferred-cleanup retry exists
(`ProcessCleanup.cleanup_deferred_profiles`) but nothing periodically drives it —
only the next `close_instance` or full shutdown does. So a single close followed
by an idle session leaks the clone for the rest of the run, which in turn keeps
the storage cap perpetually exceeded.

The background storage sweep already runs at startup and before every spawn. It
must also drive deferred profile cleanup so leaked clones are reclaimed promptly
and reliably — not only when another instance happens to close.

Pure filesystem + seeded ProcessCleanup: no browser.
"""

import json
import time

from stealth_chrome_devtools_mcp.embedded import server
from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup

MARKER = ".stealth_chrome_devtools_mcp_clone.json"
GIB = 1024**3


def _make_leaked_clone(clone_root, name):
    d = clone_root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "data.bin").write_bytes(b"x" * 4096)
    (d / MARKER).write_text(
        json.dumps(
            {
                "source": "master",
                "source_kind": "master-snapshot",
                "created_at": "2026-01-01T00:00:00Z",
                "auto_clean": True,
            }
        ),
        encoding="utf-8",
    )
    return d


def _seed_process_cleanup(tmp_path, instance_id, user_data_dir):
    """A ProcessCleanup holding one deferred auto-clone entry: the browser is
    gone (pid cleared) but the profile dir was never deleted."""
    pc = ProcessCleanup.__new__(ProcessCleanup)
    pc.pid_file = tmp_path / "pids.json"
    pc.tracked_pids = set()
    pc.orphan_profile_max_age_seconds = 21600
    pc._init_time = time.time()
    pc.browser_processes = {
        instance_id: {
            "pid": None,  # process already gone — the deferred state
            "create_time": None,
            "user_data_dir": str(user_data_dir),
            "uses_custom_data_dir": False,
            "auto_clone": True,
            "timestamp": time.time(),
        }
    }
    # Hermetic: never scan the real machine's processes for "is this dir in use".
    pc._get_active_browser_profile_dirs = lambda: set()
    pc._get_browser_pids_for_profile = lambda _dir: set()
    return pc


def test_storage_sweep_reclaims_deferred_leaked_clone(tmp_path, monkeypatch):
    clone_root = tmp_path / "sessions"
    clone_root.mkdir()
    leaked = _make_leaked_clone(clone_root, "sess-deferred")

    pc = _seed_process_cleanup(tmp_path, "dead-instance", leaked)
    monkeypatch.setattr(server, "process_cleanup", pc)

    # Cap is far above the tiny leaked clone, so the SIZE sweep selects nothing —
    # the only thing that can reclaim this dir is deferred-profile cleanup.
    server._run_storage_sweep(
        clone_root, clone_cap=10 * GIB, session_cap=20 * GIB, reason="test"
    )

    assert not leaked.exists(), "sweep must drive deferred cleanup of the leaked clone"
    assert "dead-instance" not in pc.browser_processes, (
        "entry must be untracked after reclaim"
    )
