"""F-870 — the cold-start probe's polling and timing logic, with a FAKE browser.

The probe exists to measure a real Chrome, but what can go wrong in it is
ordinary code: reading ``DevToolsActivePort`` before it is written, calling a
port that is not listening yet, mistaking a dead process for a slow one,
throwing away the stderr the finding needs. None of that needs Chrome to
exercise, and a unit lane that launched a real browser would be neither
hermetic nor fast.

So the double here is a real subprocess that behaves the way Chrome does at the
only interface the probe uses: it writes a ``DevToolsActivePort`` file and
serves ``/json/version`` on loopback. It is a *process*, not a mock, precisely
because the memory of this repo is that a hand-written double can encode the bug
(a fake that returned the answer directly would prove nothing about polling a
file that is not there yet). Its delays are configurable, so each mode below is
a shape the CI evidence actually showed or would show.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

# tools/ is not a package; same sys.path + bare-import pattern as
# tests/test_release_evidence.py and tools/gen_release_contract.py.
import chrome_cold_start_probe as probe

# A stand-in for Chrome at the two surfaces the probe reads. Modes:
#   listen  — sleep `delay`, write DevToolsActivePort, sleep `lead`, then serve.
#             `lead` is what makes "port file seen but /json/version not yet"
#             reachable, which is the state the probe must keep polling through.
#   silent  — start, never write the file, never listen (hypothesis H3's shape).
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
pathlib.Path(profile, "DevToolsActivePort").write_text(
    "%d\\n/devtools/browser/fake\\n" % port, encoding="utf-8"
)
time.sleep(lead)
sys.stderr.write("DevTools listening on ws://127.0.0.1:%d/devtools/browser/fake\\n" % port)
sys.stderr.flush()
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


def test_both_timings_are_measured_and_ordered(fake_browser: Path, tmp_path: Path):
    """The two milestones are distinct and the port milestone comes first.

    This is the measurement F-870 §7 needs: a single "it worked" boolean could
    not distinguish "Chrome bound late" from "Chrome bound early and served
    late", and those point at different causes.
    """
    profile = _profile(tmp_path, "p1")
    record = probe.probe_once(
        _command(fake_browser, profile, delay=0.3, lead=0.4, mode="listen"),
        profile,
        launch=1,
        port_requested=0,
        deadline_seconds=20.0,
    )
    assert record.listening is True
    assert record.error is None
    assert record.ms_to_active_port is not None
    assert record.ms_to_json_version is not None
    assert record.ms_to_active_port >= 300.0
    assert record.ms_to_json_version >= 700.0
    assert record.ms_to_json_version > record.ms_to_active_port
    assert record.port_from_file == int(
        (profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0]
    )
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
        profile,
        launch=1,
        port_requested=0,
        deadline_seconds=1.0,
    )
    assert record.listening is False
    assert record.ms_to_active_port is None
    assert record.ms_to_json_version is None
    assert record.port_from_file is None
    assert record.error is None


def test_a_browser_that_dies_reports_its_exit_code(fake_browser: Path, tmp_path: Path):
    """A launch that exits is NOT a slow launch, and the probe must not burn the
    whole deadline on one. The five CI occurrences all left Chrome ALIVE, so an
    exit code here would be a genuinely new shape and must be visible."""
    profile = _profile(tmp_path, "p3")
    record = probe.probe_once(
        _command(fake_browser, profile, delay=0, lead=0, mode="die"),
        profile,
        launch=2,
        port_requested=0,
        deadline_seconds=20.0,
    )
    assert record.listening is False
    assert record.exit_code_if_died == 7


def test_a_port_the_browser_did_not_take_is_flagged(fake_browser: Path, tmp_path: Path):
    """The lost-port race (F-870 H2) is detectable only by comparing the port we
    asked for against the one Chrome wrote down."""
    profile = _profile(tmp_path, "p4")
    record = probe.probe_once(
        _command(fake_browser, profile, delay=0, lead=0, mode="listen"),
        profile,
        launch=1,
        port_requested=65500,
        deadline_seconds=20.0,
    )
    assert record.port_from_file != 65500
    assert record.port_matches_request is False


def test_stderr_is_captured_rather_than_discarded(fake_browser: Path, tmp_path: Path):
    """The whole point of F-870 §6.1(d): nodriver pipes Chrome's stderr and never
    reads it, which is why `DevTools listening on ws://` appears in zero of ~100
    CI logs. The probe must not repeat that."""
    profile = _profile(tmp_path, "p5")
    record = probe.probe_once(
        _command(fake_browser, profile, delay=0, lead=0, mode="listen"),
        profile,
        launch=1,
        port_requested=0,
        deadline_seconds=20.0,
    )
    assert "DevTools listening on ws://" in record.stderr_tail


def test_an_unlaunchable_executable_is_reported_not_raised(tmp_path: Path):
    """The probe runs before everything else in a cell; an exception here would
    redden a gate for a step that asserts nothing."""
    profile = _profile(tmp_path, "p6")
    record = probe.probe_once(
        [str(tmp_path / "no-such-browser-binary")],
        profile,
        launch=1,
        port_requested=0,
        deadline_seconds=5.0,
    )
    assert record.listening is False
    assert record.error is not None
    assert record.pid is None


def test_the_launch_command_is_headless_and_asks_chrome_to_choose_the_port():
    """`--remote-debugging-port=0` is load-bearing: it is what makes
    DevToolsActivePort carry a port nobody reserved, so a mismatch can only mean
    Chrome chose differently."""
    profile = Path("/tmp/x")
    command = probe.chrome_command("/usr/bin/google-chrome", profile, 0)
    assert command[0] == "/usr/bin/google-chrome"
    assert "--headless=new" in command
    assert "--remote-debugging-port=0" in command
    # Built from the same Path so the assertion does not encode a separator.
    assert f"--user-data-dir={profile}" in command


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
    in a subprocess with the package made unimportable."""
    script = tmp_path / "check.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(TOOLS)!r})\n"
        "import chrome_cold_start_probe\n"
        "bad = [m for m in sys.modules if m.startswith('stealth_chrome_devtools_mcp')]\n"
        "print(bad)\n"
        "raise SystemExit(1 if bad else 0)\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout
