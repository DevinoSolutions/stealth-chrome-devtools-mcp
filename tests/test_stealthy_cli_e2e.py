"""F-891 through the REAL `stealthy` console script, against a REAL backend.

Everything about the six verbs is pinned hermetically in
``tests/test_stealthy_cli.py``, and that tier is where the parsing, the
unwrapping and the exit codes belong. What it cannot answer is the operator's
question, which is not "does `result_value` behave" but "does the command I type
reach the backend". Between the two sit the entry point, the console script's
own name, ``Path.home()`` resolved at the child's import time, the record read,
``singleton._probe_backend_status``' adoption walk, a real MCP handshake over
streamable HTTP and a real ``tools/call``. Each is a place the feature can be
true and the command still wrong — and the first draft's hang (a ``responsive``
backend re-probed on the cold-start deadline) is exactly the class of defect only
this tier sees.

So this node starts a genuine backend as a subprocess and drives the installed
``stealthy`` launcher against it. **No Chrome**: the three verbs exercised
(``tools``, ``call list_instances``, ``ls``) reach the tool layer and stop there,
which is why this is one node and not a journey.

Isolation, stated exactly. ``backend_registry.STATE_DIR`` is
``Path.home() / ".stealth-mcp"`` with no environment override, so the only way to
move it is to move the child's home — ``release_gate_harness._isolated_env``'s
job, used here unchanged, exactly as ``tests/test_cli_backend_records_e2e.py``
uses it. The backend binds a port from ``_pick_free_port``, which excludes the
product's default and every port the DEVELOPER'S real record names, so this node
can neither evict a real backend nor be adopted by one. The real record's bytes
are compared before and after.

The record is written BY HAND rather than by letting the CLI cold-start a
backend, for two reasons and both matter: a cold start would leave a detached
backend this node did not spawn and cannot reliably reap, and the selection
under test is the RECORD walk — writing the entry is how the walk is given
something to find.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from release_gate_harness import _isolated_env, _pick_free_port, resolve_launcher
from stealth_chrome_devtools_mcp.embedded import backend_probe, display_context

pytestmark = pytest.mark.integration

#: The subprocess bound. Never a product deadline — each verb makes one
#: handshake and one call against a backend already proven ready, so this only
#: fires if a command never returned at all.
CLI_TIMEOUT = 180

#: How long the backend gets to answer a real ``initialize``. Generous because a
#: cold Python import of the whole server stack is the slow part on a loaded box.
BACKEND_READY_SECONDS = 120


def _real_state_bytes() -> bytes | None:
    try:
        return (Path.home() / ".stealth-mcp" / "server.json").read_bytes()
    except OSError:
        return None


def _write_record(home: Path, port: int) -> Path:
    """A v3 record naming our isolated backend under THIS process's display
    context, so ``_probe_backend_status``' adoption walk finds it.

    Version and fingerprint are deliberately absent: selection is the adoption
    walk plus a live probe and does not consult identity (identity is the REUSE
    gate's question, and nothing here reuses). Writing plausible-looking values
    would imply this node pins something it does not.
    """
    state_dir = home / ".stealth-mcp"
    state_dir.mkdir(parents=True, exist_ok=True)
    record = state_dir / "server.json"
    record.write_text(
        json.dumps(
            {
                "schema": 3,
                "backends": [
                    {
                        "port": port,
                        "pid": os.getpid(),
                        "display_context": display_context.display_context(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return record


def _await_backend(url: str, deadline_seconds: int) -> bool:
    """Poll the backend's own readiness answer, through the ONE probe.

    ``backend_probe.ready`` and not a socket connect: a freshly bound uvicorn
    answers 4xx while FastMCP's session manager is still starting, and a node
    that proceeded on a socket would fail inside the CLI for a reason that is
    not the CLI's.
    """
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if backend_probe.ready(url, timeout=5.0, client_name="f891-e2e"):
            return True
        time.sleep(0.5)
    return False


@pytest.fixture()
def isolated_backend(tmp_path):
    """A real HTTP backend in a throwaway home, with the record pointing at it.

    **Its stdout goes to a FILE and must never go to a pipe**, and that is not a
    style preference — it is a measured trap this fixture fell into. A tool that
    raises makes the backend render a full rich traceback to stdout; against an
    undrained ``subprocess.PIPE`` that fills the ~64 KB buffer and the backend
    BLOCKS ON THE WRITE, so its event loop stops. Measured here on 2026-09-20:
    with a pipe, ``navigate`` on an unknown instance took 50 s and then never
    answered, and every later ``initialize`` probe failed — i.e. the harness
    manufactured a wedged backend and it read exactly like the product defect
    F-501's watchdog exists for. With the same call and stdout on a file, the
    answer is ``isError`` in 0.1 s and the backend stays responsive. A file is
    also better evidence: it survives the node and is printed on failure.
    """
    home = tmp_path / "home"
    for name in ("home", "session-root", "logs", "clones"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    env = _isolated_env(
        home_dir=home,
        session_root=tmp_path / "session-root",
        log_dir=tmp_path / "logs",
        clone_dir=tmp_path / "clones",
    )
    port = _pick_free_port()
    console = tmp_path / "backend-console.log"
    with console.open("w", encoding="utf-8") as sink:
        proc = subprocess.Popen(  # noqa: S603  PERMANENT(the gate harness drives real subprocesses)
            [
                sys.executable,
                "-m",
                "stealth_chrome_devtools_mcp",
                "--transport",
                "http",
                "--port",
                str(port),
                "--host",
                "127.0.0.1",
            ],
            cwd=tmp_path,
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            url = f"http://127.0.0.1:{port}/mcp/"
            if not _await_backend(url, BACKEND_READY_SECONDS):
                proc.kill()
                pytest.skip(
                    f"the isolated backend on {port} never became ready; console:\n"
                    + console.read_text(encoding="utf-8", errors="replace")[-4000:]
                )
            _write_record(home, port)
            yield {"env": env, "cwd": tmp_path, "port": port, "console": console}
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()


def _stealthy(space: dict, *argv: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(  # noqa: S603  PERMANENT(the gate harness drives real subprocesses)
        [str(resolve_launcher(name="stealthy")), *argv, "--no-start"],
        cwd=space["cwd"],
        env=space["env"],
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT,
        check=False,
    )
    assert "Traceback (most recent call last)" not in proc.stderr, proc.stderr
    return proc


def test_the_installed_stealthy_script_drives_a_real_backend(isolated_backend):
    """One backend, three verbs, through the console script a user types.

    ``--no-start`` on every invocation is not a convenience: it is the assertion
    that each command reached the backend this node started, by finding it in the
    record. A verb that silently cold-started its own would pass a looser test
    and prove nothing about selection.
    """
    real_before = _real_state_bytes()

    listed = _stealthy(isolated_backend, "tools", "--json")
    assert listed.returncode == 0, listed.stdout + listed.stderr
    tools = json.loads(listed.stdout)
    names = {tool["name"] for tool in tools}
    # The live surface, not a number typed here: what is asserted is that the
    # verbs this CLI is sugar for are actually served by the thing it reached.
    assert {"spawn_browser", "list_instances", "navigate", "close_instance"} <= names

    called = _stealthy(isolated_backend, "call", "list_instances", "--json", "{}")
    assert called.returncode == 0, called.stdout + called.stderr
    # No browser was spawned, so the honest answer is an empty list — and it
    # arrives through the `{"result": ...}` wrapper FastMCP puts a non-dict
    # return in, which is the shape `result_value` exists to unwrap.
    assert json.loads(called.stdout) == []

    piped = _stealthy(isolated_backend, "ls")
    assert piped.returncode == 0, piped.stdout + piped.stderr
    # stdout is a pipe here, so `ls` must emit JSON without being asked.
    assert json.loads(piped.stdout) == []

    assert _real_state_bytes() == real_before, (
        "the real ~/.stealth-mcp/server.json changed during an isolated CLI run"
    )


def test_a_tool_failure_exits_1_with_the_tools_own_message(isolated_backend):
    """The error convention, at the wire: a tool that RAISES comes back as a
    message on stderr and exit 1, with nothing on stdout for a script to parse
    as an answer.

    ``navigate`` and deliberately not ``close_instance``: that one answers
    ``false`` for an unknown instance (measured — the call exits 0 and prints
    ``false``), which is its contract and not a failure, so building the error
    pin on it would have pinned the opposite of what it claims. Measured the
    same way, ``get_instance_state`` and ``list_tabs`` also answer rather than
    raise for an unknown id; ``navigate`` and ``execute_script`` are the two
    that reach ``InstanceNotFoundError``, in 0.1 s.
    """
    proc = _stealthy(
        isolated_backend,
        "call",
        "navigate",
        "--arg",
        "instance_id=no-such-instance",
        "--arg",
        "url=https://example.test/",
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert proc.stdout == ""
    assert "no-such-instance" in proc.stderr
