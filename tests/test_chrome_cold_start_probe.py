"""F-870 — the cold-start probe's polling and timing logic, with a FAKE browser.

The probe exists to measure a real Chrome, but what can go wrong in it is
ordinary code: looking for the readiness banner before it is written, calling a
port that is not listening yet, mistaking a dead process for a slow one,
throwing away the output the finding needs. None of that needs Chrome to
exercise, and a unit lane that launched a real browser would be neither
hermetic nor fast.

So the double here is a real subprocess that behaves the way Chrome does at the
two surfaces the probe reads: it writes a ``DevTools listening on ws://...``
banner to stderr and serves ``/json/version`` on loopback. It is a *process*,
not a mock, precisely because the memory of this repo is that a hand-written
double can encode the bug — a fake that returned the answer directly would prove
nothing about polling a log that has not been flushed yet. Its delays are
configurable, so each mode below is a shape the CI evidence actually showed or
would show.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

TOOLS = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

# tools/ is not a package; same sys.path + bare-import pattern as
# tests/test_release_evidence.py and tools/gen_release_contract.py.
import chrome_cold_start_probe as probe

# A stand-in for Chrome at the two surfaces the probe reads. Modes:
#   listen  — sleep `delay`, bind and ANNOUNCE, sleep `lead`, then serve. The
#             `lead` window is the real Chrome behaviour that matters: the socket
#             already accepts (bind+listen happen in the constructor) while no
#             handler answers yet, so the probe must keep polling through it.
#   silent  — start, never announce, never listen (hypothesis H3's shape).
#   die     — exit immediately with a distinctive code.
_FAKE_BROWSER = """
import http.server, json, pathlib, sys, time

profile, delay, lead, mode = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
sys.stderr.write("fake-browser start\\n")
sys.stderr.flush()
if mode == "die":
    sys.stderr.write("fake-browser aborting\\n")
    sys.stderr.flush()
    raise SystemExit(7)
time.sleep(delay)
if mode == "silent":
    time.sleep(600)
    raise SystemExit(0)

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"Browser": "FakeChrome/1.2.3"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
pathlib.Path(profile, "bound-port").write_text(str(port), encoding="utf-8")
sys.stderr.write("DevTools listening on ws://127.0.0.1:%d/devtools/browser/fake\\n" % port)
sys.stderr.flush()
time.sleep(lead)
server.serve_forever()
"""


@pytest.fixture
def fake_browser(tmp_path: Path) -> Path:
    script = tmp_path / "fake_browser.py"
    script.write_text(_FAKE_BROWSER, encoding="utf-8")
    return script


def _command(
    script: Path, profile: Path, *, delay: float, lead: float, mode: str
) -> list[str]:
    return [sys.executable, str(script), str(profile), str(delay), str(lead), mode]


def _profile(tmp_path: Path, name: str) -> Path:
    profile = tmp_path / name
    profile.mkdir()
    return profile


def _log(tmp_path: Path, name: str) -> Path:
    return tmp_path / f"{name}.log"


def _bound_port(profile: Path) -> int:
    """The port the fake actually bound, as IT recorded it — an independent
    witness to compare the probe's banner reading against."""
    return int((profile / "bound-port").read_text(encoding="utf-8").strip())


def _pid_alive(pid: int) -> bool:
    """Liveness without psutil — the unit lane must stay dependency-light.

    On POSIX a zombie still answers signal 0, but the probe reaps its own child
    via `Popen.wait`, so a surviving pid here means a genuinely live process.
    """
    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],  # noqa: S607  PERMANENT(system binary; a full path would break on a non-default SystemRoot)
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@contextlib.contextmanager
def _garbage_server() -> Iterator[int]:
    """A socket that ACCEPTS and then replies with a non-HTTP line.

    This is the shape that raises `http.client.BadStatusLine` — not an
    `OSError`, and not wrapped by urllib — which is how an earlier version of
    this probe could have escaped its own except clause and reddened a job.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    port = int(server.getsockname()[1])
    stop = threading.Event()

    def _serve() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except (TimeoutError, OSError):
                continue
            with contextlib.suppress(OSError):
                conn.recv(4096)
                conn.sendall(b"NOT-HTTP-AT-ALL\r\n\r\n")
            with contextlib.suppress(OSError):
                conn.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        thread.join(timeout=5)
        server.close()


def test_both_timings_are_measured_and_ordered(fake_browser: Path, tmp_path: Path):
    """The two milestones are distinct and the banner comes first.

    This is the measurement F-870 §7 needs: a single "it worked" boolean could
    not distinguish "Chrome announced late" from "Chrome announced early and
    served late", and those point at different causes.
    """
    profile = _profile(tmp_path, "p1")
    record = probe.probe_once(
        _command(fake_browser, profile, delay=0.3, lead=0.5, mode="listen"),
        _log(tmp_path, "p1"),
        launch=1,
        port_requested=0,
        deadline_seconds=20.0,
    )
    assert record.listening is True
    assert record.error is None
    assert record.ms_to_devtools_banner is not None
    assert record.ms_to_json_version is not None
    assert record.ms_to_devtools_banner >= 300.0
    assert record.ms_to_json_version >= 800.0
    assert record.ms_to_json_version > record.ms_to_devtools_banner
    # The banner reading agrees with the port the fake itself recorded.
    assert record.port_from_banner == _bound_port(profile)
    assert record.launch == 1


def test_a_browser_that_never_listens_is_the_measurement_not_an_error(
    fake_browser: Path, tmp_path: Path
):
    """H3's shape. The probe must report it as data and stay quiet: this is the
    outcome that would prove more patience cannot help, so it must not be
    indistinguishable from a crash of the probe itself."""
    profile = _profile(tmp_path, "p2")
    record = probe.probe_once(
        _command(fake_browser, profile, delay=0, lead=0, mode="silent"),
        _log(tmp_path, "p2"),
        launch=1,
        port_requested=0,
        deadline_seconds=1.0,
    )
    assert record.listening is False
    assert record.ms_to_devtools_banner is None
    assert record.ms_to_json_version is None
    assert record.port_from_banner is None
    assert record.error is None


def test_a_browser_that_dies_reports_its_exit_code_promptly(
    fake_browser: Path, tmp_path: Path
):
    """A launch that exits is NOT a slow launch, and the probe must not burn the
    whole deadline on one. The five CI occurrences all left Chrome ALIVE, so an
    exit code here would be a genuinely new shape and must be visible.

    The elapsed assertion is the real point. Without the early dead-process
    break this still passes — via the `while/else` fallback, a full deadline
    late — and in CI that regression would cost 2 x 60 s on every cell that runs
    this step. So the test times itself.
    """
    profile = _profile(tmp_path, "p3")
    started = time.monotonic()
    record = probe.probe_once(
        _command(fake_browser, profile, delay=0, lead=0, mode="die"),
        _log(tmp_path, "p3"),
        launch=2,
        port_requested=0,
        deadline_seconds=20.0,
    )
    elapsed = time.monotonic() - started
    assert record.listening is False
    assert record.exit_code_if_died == 7
    assert elapsed < 10.0, f"noticed the dead process only after {elapsed:.1f}s"


def test_the_launched_process_is_killed_before_the_record_is_returned(
    fake_browser: Path, tmp_path: Path
):
    """A probe that left its browser running would poison the NEXT launch's
    measurement — and, on a gate runner, leak a Chrome tree into the job that
    follows it. `_terminate` returning early would fail no other test here.
    """
    profile = _profile(tmp_path, "p7")
    record = probe.probe_once(
        _command(fake_browser, profile, delay=0, lead=0, mode="listen"),
        _log(tmp_path, "p7"),
        launch=1,
        port_requested=0,
        deadline_seconds=20.0,
    )
    assert record.pid is not None
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and _pid_alive(record.pid):
        time.sleep(0.05)
    assert not _pid_alive(record.pid), f"pid {record.pid} survived the probe"


def test_a_socket_that_answers_with_garbage_does_not_escape():
    """`http.client.HTTPException` is NOT an `OSError` and urllib does not wrap
    it: a socket that accepts and replies with a non-HTTP line raises
    `BadStatusLine` straight out of `getresponse()`. That is reachable in
    exactly the case this probe studies — a DevTools port that accepts before
    its handler is ready, or one now held by something else — so it must read as
    "not ready yet", never as an exception.
    """
    with _garbage_server() as port:
        assert probe._json_version_answers(port) is False


def test_an_escaping_failure_still_writes_a_record_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The contract with the gate, pinned at the outermost level: whatever
    escapes the measurement, the step writes a record naming it and returns 0.
    """
    out = tmp_path / "chrome-cold-start.json"

    def _boom(*_args: object, **_kwargs: object) -> list[probe.LaunchRecord]:
        raise http.client.BadStatusLine("NOT HTTP\r\n")

    monkeypatch.setattr(probe, "_resolve_path", lambda: Path("/usr/bin/chrome"))
    monkeypatch.setattr(probe, "probe", _boom)
    monkeypatch.setattr(sys, "argv", ["probe", "--cell", "c", "--out", str(out)])
    assert probe.main() == 0
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["launches"] == []
    assert "BadStatusLine" in record["error"]


def test_a_port_the_browser_did_not_take_is_flagged(fake_browser: Path, tmp_path: Path):
    """The lost-port race (F-870 H2) is detectable only by comparing the port we
    asked for against the one Chrome says it bound. `--remote-debugging-port` is
    a REQUEST; the banner is the answer."""
    profile = _profile(tmp_path, "p4")
    record = probe.probe_once(
        _command(fake_browser, profile, delay=0, lead=0, mode="listen"),
        _log(tmp_path, "p4"),
        launch=1,
        port_requested=65500,
        deadline_seconds=20.0,
    )
    assert record.port_from_banner == _bound_port(profile)
    assert record.port_from_banner != 65500
    assert record.port_matches_request is False


def test_the_launch_output_is_captured_rather_than_discarded(
    fake_browser: Path, tmp_path: Path
):
    """The whole point of F-870 §6.1(d): nodriver pipes Chrome's stderr and never
    reads it, which is why `DevTools listening on ws://` appears in zero of ~100
    CI logs. The probe must not repeat that."""
    profile = _profile(tmp_path, "p5")
    record = probe.probe_once(
        _command(fake_browser, profile, delay=0, lead=0, mode="listen"),
        _log(tmp_path, "p5"),
        launch=1,
        port_requested=0,
        deadline_seconds=20.0,
    )
    assert "DevTools listening on ws://" in record.output_excerpt
    assert "fake-browser start" in record.output_excerpt


def test_an_unlaunchable_executable_is_reported_not_raised(tmp_path: Path):
    """The probe runs before everything else in a cell; an exception here would
    redden a gate for a step that asserts nothing."""
    record = probe.probe_once(
        [str(tmp_path / "no-such-browser-binary")],
        _log(tmp_path, "p6"),
        launch=1,
        port_requested=0,
        deadline_seconds=5.0,
    )
    assert record.listening is False
    assert record.error is not None
    assert record.pid is None


def test_the_command_mirrors_nodrivers_own_launch(tmp_path: Path):
    """The measurement is only comparable to the 2.75 s window if it times the
    SAME browser the product launches.

    Compared against a real `nodriver.Config`, not a copied list, so a nodriver
    bump that adds, drops or renames a flag fails HERE — rather than silently
    leaving the probe describing a browser the product no longer starts.
    nodriver is a runtime dependency, so importing it in the unit lane is fine;
    the probe itself still never imports it.
    """
    nodriver_config = pytest.importorskip("nodriver.core.config")
    profile = tmp_path / "profile"
    port = 45123
    theirs = nodriver_config.Config(
        user_data_dir=str(profile),
        headless=True,
        sandbox=False,
        host="127.0.0.1",
        port=port,
    )()
    ours = probe.chrome_command("/usr/bin/google-chrome", profile, port)
    assert set(ours[1:]) == set(theirs), (
        f"only in the probe: {sorted(set(ours[1:]) - set(theirs))}; "
        f"only in nodriver: {sorted(set(theirs) - set(ours[1:]))}"
    )


def test_the_default_args_are_nodrivers_verbatim():
    """The copied constant, checked against its source of truth."""
    nodriver_config = pytest.importorskip("nodriver.core.config")
    theirs = nodriver_config.Config(user_data_dir="x")._default_browser_args
    assert list(probe.NODRIVER_DEFAULT_ARGS) == list(theirs)


def test_prewarm_is_flagged_on_linux_only(monkeypatch: pytest.MonkeyPatch):
    """`resolve_chrome._read_version` execs the binary on Linux only, so only
    there is launch #1 measuring an already-paged-in Chrome. Comparing a Linux
    number with a macOS one without this flag would compare two different
    quantities."""
    for system, expected in (("Linux", True), ("Windows", False), ("Darwin", False)):
        monkeypatch.setattr(probe.platform, "system", lambda s=system: s)
        assert probe.binary_prewarmed() is expected


def test_the_output_excerpt_keeps_the_head_where_the_banner_is(tmp_path: Path):
    """`DevTools listening on ws://` is among the FIRST lines Chrome writes. A
    tail-only excerpt discards exactly the line this finding needs."""
    log = tmp_path / "big.log"
    banner = b"DevTools listening on ws://127.0.0.1:1/devtools/browser/x\n"
    log.write_bytes(banner + b"F" * 50_000 + b"LAST-LINE\n")
    excerpt = probe._output_excerpt(log)
    assert "DevTools listening on ws://" in excerpt
    assert "LAST-LINE" in excerpt
    assert "bytes elided" in excerpt


def test_main_exits_zero_and_writes_a_record_when_chrome_cannot_be_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Exit 0 on every path is the contract with the gate. A machine without
    Chrome must still produce a readable record saying so."""
    out = tmp_path / "chrome-cold-start.json"

    def _no_chrome() -> Path:
        raise OSError("no Chrome on this machine")

    monkeypatch.setattr(probe, "_resolve_path", _no_chrome)
    monkeypatch.setattr(
        sys, "argv", ["probe", "--cell", "test-cell", "--out", str(out)]
    )
    assert probe.main() == 0
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["cell"] == "test-cell"
    assert record["chrome_path"] is None
    assert record["launches"] == []
    assert "no Chrome on this machine" in record["error"]


def test_the_probe_imports_nothing_from_the_product(tmp_path: Path):
    """Product-free by construction (F-870 §7): a probe that went through our own
    code could not answer a question ABOUT our own code. Asserted by importing it
    in a subprocess and looking at what that import dragged in."""
    script = tmp_path / "check.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(TOOLS)!r})\n"
        "import chrome_cold_start_probe\n"
        "bad = [m for m in sys.modules if m.startswith('stealth_chrome_devtools_mcp')]\n"
        "bad += [m for m in sys.modules if m == 'nodriver' or m.startswith('nodriver.')]\n"
        "print(bad)\n"
        "raise SystemExit(1 if bad else 0)\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout
