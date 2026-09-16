"""F-880 through the REAL ops CLI: what ``doctor`` says and ``cleanup`` writes.

Everything about this finding is already pinned hermetically
(``tests/test_registry_dead_entries.py``), and that tier is where the deadness
rule belongs. What it cannot answer is the operator's question, which is not
"does ``forget_dead`` behave" but "does the command I type do this". Between
the rule and the command sit ``cli._survey_records``' late binding of the two
witnesses, the entry point resolution, ``Path.home()`` being read at the child's
import time, and argparse. Each of those is a place the fix can be true and the
command still wrong.

So these nodes run the installed ``stealth-chrome-devtools`` console script as a
real subprocess — no import of ``cli``, no monkeypatch, no fake probe — against
a ``server.json`` this file writes by hand.

Isolation, stated exactly. ``backend_registry.STATE_DIR`` is
``Path.home() / ".stealth-mcp"`` with no environment override, so the ONLY way
to move it is to move the child's home. That is ``release_gate_harness.
_isolated_env``'s job and it is used here unchanged: the subprocess resolves
``Path.home()`` inside a ``tmp_path`` at its own import time, and the developer's
real ``~/.stealth-mcp`` is never opened — a property asserted below by reading
the real record's bytes before and after, rather than merely intended.

The two entries are the two shapes the finding is about, and they are
DIFFERENT on purpose:

* a **dead** record — a port with no listener, and a pid that exists but is not
  a backend of ours. Both witnesses, because either one alone is a rule the
  finding rejects: a pid that is gone says nothing about whether the port is
  free, and a port with no listener is every backend's whole cold start.
* an entry naming **no usable port** — ``NO_PORT``, which is never dead. We have
  no evidence about it, and inventing some is how a hand-edited entry would
  silently disappear. It is here to be KEPT, which is the half a test that only
  wrote one entry could not check.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from release_gate_harness import _isolated_env, resolve_launcher

# Deliberately UNMARKED, like ``tests/test_doc_examples.py``, which drives the
# same console script the same way. ``integration`` means "spawns real
# browsers" (pyproject) and nothing here does; marking it would move real-CLI
# coverage of a records bug out of the lane that runs on every push and into
# one that does not. The cost is ~12 s of subprocess time in the unit lane,
# which is what a non-mocked claim about a command costs.

#: The subprocess bound. Never a product deadline — ``doctor`` probes at most
#: two ports, neither of which has a listener, so this only fires if the
#: command never returned at all.
CLI_TIMEOUT = 120

DEAD_CONTEXT = "e2e-f880-dead"
NO_PORT_CONTEXT = "e2e-f880-noport"

#: ``backend_liveness.NO_PORT``, spelled out rather than imported: this file's
#: subject is what the COMMAND prints, and importing the constant would let a
#: rename of the operator-facing word pass silently in both places at once.
NO_PORT_WORD = "no port recorded"
DEAD_MARKER = "(dead record)"


@pytest.fixture()
def refused_port():
    """A port that is bound but never listened on, held for the whole node.

    A ``connect()`` to it is refused, so the liveness ladder answers ``down``
    deterministically — and because we hold the binding, nothing can take the
    port mid-node and turn the verdict into ``wedged``. Closing the socket and
    reusing the number would have left exactly that race.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


def _write_record(home: Path, port: int, pid: int) -> Path:
    """The v2 record both nodes start from, with one entry of each shape."""
    state_dir = home / ".stealth-mcp"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "server.json"
    path.write_text(
        json.dumps(
            {
                "schema": 2,
                "backends": {
                    DEAD_CONTEXT: {
                        "port": port,
                        "pid": pid,
                        "version": "2.1.8",
                        "source_fingerprint": "f880-e2e",
                        "display_context": DEAD_CONTEXT,
                    },
                    NO_PORT_CONTEXT: {
                        "port": "not-a-port",
                        "pid": pid,
                        "version": "2.1.8",
                        "source_fingerprint": "f880-e2e",
                        "display_context": NO_PORT_CONTEXT,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _workspace(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "home"
    session_root = tmp_path / "session-root"
    log_dir = tmp_path / "logs"
    clone_dir = tmp_path / "clones"
    for directory in (home, session_root, log_dir, clone_dir):
        directory.mkdir(parents=True, exist_ok=True)
    env = _isolated_env(
        home_dir=home, session_root=session_root, log_dir=log_dir, clone_dir=clone_dir
    )
    return home, env


def _run(env: dict[str, str], cwd: Path, *argv: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [str(resolve_launcher(name="stealth-chrome-devtools")), *argv],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT,
        check=False,
    )
    assert "Traceback (most recent call last)" not in proc.stderr, proc.stderr
    return proc


def _real_state_file() -> Path:
    """The developer's REAL record — read only to prove it was not touched."""
    return Path.home() / ".stealth-mcp" / "server.json"


def _real_state_bytes() -> bytes | None:
    try:
        return _real_state_file().read_bytes()
    except OSError:
        return None


def _decoy_pid() -> int:
    """A pid that EXISTS and is not a backend of ours: this pytest process.

    Deliberately not a pid that is gone. "The pid is gone" is the weaker
    witness the finding refuses to decide on alone, so a fixture built from one
    would pass against a rule this test exists to forbid. Checked rather than
    assumed — if pytest were ever launched in a way that matched, the node
    would be silently testing nothing.
    """
    from stealth_chrome_devtools_mcp.embedded import singleton

    pid = os.getpid()
    assert not singleton._is_our_backend(pid), (
        "this pytest process looks like one of our backends, so it cannot "
        "stand in for a foreign pid"
    )
    return pid


def test_doctor_names_the_dead_record_and_the_entry_with_no_port(
    tmp_path, refused_port
):
    """``doctor`` REPORTS both shapes, and writes nothing.

    Read-only is half the finding: a verb an operator runs to find out what is
    wrong must not change what is wrong. The record's bytes are compared before
    and after, which is stricter than an mtime check and does not depend on the
    filesystem's timestamp resolution.
    """
    home, env = _workspace(tmp_path)
    record = _write_record(home, refused_port, _decoy_pid())
    before = record.read_bytes()
    real_before = _real_state_bytes()

    proc = _run(env, tmp_path, "doctor")

    # One line per recorded backend, and each says what it is.
    dead_line = [
        line
        for line in proc.stdout.splitlines()
        if DEAD_CONTEXT in line and "backend" in line
    ]
    assert dead_line, proc.stdout
    assert DEAD_MARKER in dead_line[0], dead_line[0]
    assert "down" in dead_line[0], dead_line[0]

    no_port_line = [
        line
        for line in proc.stdout.splitlines()
        if NO_PORT_CONTEXT in line and "backend" in line
    ]
    assert no_port_line, proc.stdout
    assert NO_PORT_WORD in no_port_line[0], no_port_line[0]
    # ...and it is NOT marked dead. An entry we have no evidence about is not
    # residue, and reporting it as residue is how it would then be deleted.
    assert DEAD_MARKER not in no_port_line[0], no_port_line[0]

    assert record.read_bytes() == before, "doctor wrote to the backend record"
    assert _real_state_bytes() == real_before, (
        f"the real {_real_state_file()} changed during an isolated CLI run"
    )


def test_cleanup_apply_forgets_the_dead_entry_and_keeps_the_other(
    tmp_path, refused_port
):
    """``cleanup --apply`` is the ONE verb that writes, and it writes exactly
    the dead entries.

    The dry run is asserted first and against the same record, because the
    finding's argument is that the two cannot disagree: both go through the one
    survey, so "1 dead" previewed must be "forgot 1" applied.
    """
    home, env = _workspace(tmp_path)
    record = _write_record(home, refused_port, _decoy_pid())
    real_before = _real_state_bytes()

    preview = _run(env, tmp_path, "cleanup")
    assert "1 dead" in preview.stdout, preview.stdout
    assert DEAD_CONTEXT in preview.stdout, preview.stdout
    assert "--apply to forget" in preview.stdout, preview.stdout
    # The preview changed nothing.
    assert set(json.loads(record.read_text())["backends"]) == {
        DEAD_CONTEXT,
        NO_PORT_CONTEXT,
    }

    applied = _run(env, tmp_path, "cleanup", "--apply")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert f"forgot 1 dead ({DEAD_CONTEXT})" in applied.stdout, applied.stdout

    remaining = json.loads(record.read_text())["backends"]
    assert set(remaining) == {NO_PORT_CONTEXT}, remaining
    # The survivor is kept whole, not rewritten.
    assert remaining[NO_PORT_CONTEXT]["port"] == "not-a-port", remaining

    # A second apply has nothing left to do and says so — the rule is stable,
    # not a one-shot that eats an entry per run.
    again = _run(env, tmp_path, "cleanup", "--apply")
    assert "1 recorded, 0 dead" in again.stdout, again.stdout
    assert set(json.loads(record.read_text())["backends"]) == {NO_PORT_CONTEXT}

    assert _real_state_bytes() == real_before, (
        f"the real {_real_state_file()} changed during an isolated CLI run"
    )
