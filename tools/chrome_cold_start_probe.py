#!/usr/bin/env python3
"""F-870 — measure how long THIS machine's Chrome takes to open DevTools.

``audit/stage2/finding_F870_posix_ci_nodriver_connect_failures.md`` established
the mechanism of the POSIX gate's intermittent ``Failed to connect to browser``
to within 18 ms: nodriver 0.47 hardcodes its connect patience at
``0.25 + 5 x 0.5 = 2.75 s`` (``nodriver/core/browser.py:411-435``, no ``Config``
knob), and on a hit it spends every millisecond of it against a Chrome that is
alive and simply not listening yet. What the finding could **not** establish is
the one number a fix needs: **how long Chrome's first launch actually takes to
open its DevTools endpoint on these images.** Two things destroy that evidence
on every occurrence — F-860's reaper kills the browser 2.8 s in, and nodriver
launches Chrome with ``stdout=PIPE, stderr=PIPE`` and never relays either, so
Chrome's ``DevTools listening on ws://...`` banner reaches no log anywhere.

This script is that missing measurement, and it is deliberately **product-free**:
it imports no part of ``stealth_chrome_devtools_mcp`` and never goes through
nodriver, so nothing it reports can be an artefact of the code under test. It
launches Chrome **twice**, back to back, on a fresh profile each time. The
first/second delta is the entire question (finding §3.4): a page-in cost makes
launch #1 slow and launch #2 fast, while machine contention makes both slow.

It reads readiness the way Chrome itself reports it, not the way nodriver
guesses it: Chrome's own ``DevTools listening on ws://127.0.0.1:<port>/...``
banner, captured from the launch's output. That line is Chrome stating that the
endpoint is up, and **the port in it is the port Chrome actually bound** — which
is exactly the fact nodriver never learns, because it pipes that stream and
never reads it (``core/browser.py:397-405``).

``DevToolsActivePort`` was tried first and rejected on measurement: with a FIXED
``--remote-debugging-port=<n>`` — which is what the product passes — Chrome does
**not** write that file at all. It is a port-discovery mechanism for
``--remote-debugging-port=0``, so using it would have forced a port idiom the
product does not use, and in a first real run it produced a null reading for
every launch while Chrome was demonstrably listening. The banner works for both
idioms and needs no second way.

**The launch mirrors the product's exactly**, which is the only way the numbers
can be compared to the 2.75 s window at all. :data:`NODRIVER_DEFAULT_ARGS` is
``nodriver 0.47.0``'s ``core/config.py:116-128`` ``_default_browser_args``
copied VERBATIM, and :func:`chrome_command` then reproduces
``Config.__call__`` (``:174-193``) for the gate's own ``headless=True,
sandbox=False`` spawn. A test compares the result against a real
``nodriver.Config``, so **a nodriver bump that changes the flags fails that test
rather than silently making this measurement describe a different browser**.
Notably this means NO ``--disable-gpu`` (nodriver never passes it) and it means
``--password-store=basic`` and ``--no-pings`` ARE passed: without them a
headless Linux Chrome probes the keyring and does GCM registration work that the
product's Chrome never does, which would inflate the very number being measured.

Ports also use nodriver's idiom rather than ``--remote-debugging-port=0``:
:func:`_reserve_port` binds ``127.0.0.1:0``, reads the number and **closes the
socket** before handing it to Chrome, exactly as ``nodriver/core/util.py:132-143``
does. That reproduces the lost-port race nodriver's own source flags at
``core/config.py:175-178``, and comparing the requested port with the one in the
banner is what SIZES hypothesis H2 instead of merely asserting it. Both launches
use it, so the only difference between launch #1 and #2 stays the state of the
machine.

**It never fails the job.** Every path exits 0, and a launch that never listens
inside the deadline is not an error — it is the measurement that would confirm
hypothesis H3.

Chrome is located through ``resolve_chrome``, the repo's one Chrome-Stable
resolver, so this does not become a second locator — but through
:func:`resolve_chrome._resolve_path` rather than the public
:func:`resolve_chrome.resolve_chrome`, because the public one additionally
shells out to the binary for ``--version`` and **that subprocess pages the
binary in**. The identity is not re-derived here at all: ``chrome-identity.json``
from the sibling step is its one home, and this record cites the path only.

**On Linux, launch #1 is nevertheless a WARM binary, and the record says so.**
The gate places this step AFTER "Resolve image Chrome Stable identity" — the
right placement, because the product's own first spawn also happens after it, so
this measures the conditions the product actually meets, and ``--freeze-updater``
has already run so Chrome cannot be swapped mid-measurement (F-819). But that
step calls the public ``resolve_chrome()``, and its per-OS version read differs:
``resolve_chrome.py:128`` execs ``chrome --version`` on **Linux**, while Windows
reads a sibling version directory (``:110-117``) and macOS reads ``Info.plist``
(``:118-126``) — neither of which execs the binary. So the exec already happened
on Linux and has not on Windows or macOS.

:data:`binary_prewarmed` in the record carries that fact per OS, so the numbers
are never compared blindly. **Where it is true, launch #1 is a FLOOR on the real
cold cost, not the cold cost itself.** (The one edge: macOS falls back to
``--version`` if ``Info.plist`` is unreadable — rare, and it would show up as an
outlier rather than a silent error.)

Stdlib only, for the same reason ``resolve_chrome`` is: it must run before, and
independently of, anything this repo installs.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# tools/ is not a package; this is the same sys.path + bare-import pattern
# tools/gen_release_contract.py:33-35 and tests/test_release_evidence.py use.
from resolve_chrome import _resolve_path

# The window we are measuring against is nodriver's 2.75 s. 60 s is ~22x that:
# generous enough that "never listened" means it, short enough that two launches
# cannot add more than two minutes to a cell in the worst case. Not a knob — a
# measurement bound, and the finding's §7 reads it as one.
DEADLINE_SECONDS = 60.0
POLL_SECONDS = 0.05
# Chrome's output is the evidence nodriver throws away. Keep BOTH ends: the
# "DevTools listening on ws://" banner is among the FIRST lines Chrome writes,
# and a slow or broken launch then spews far more after it — a tail-only excerpt
# would reliably discard the single most important line in the file.
OUTPUT_HEAD_BYTES = 2000
OUTPUT_TAIL_BYTES = 2000
# A Chrome that accepts the connection before its HTTP handler is ready would
# block this poll for the whole timeout. At 2 s that is most of the 2750 ms
# window being sized, so `ms_to_json_version` could overshoot by nearly the
# quantity under measurement. 0.25 s keeps a stall inside the poll interval's
# own order of magnitude.
JSON_VERSION_TIMEOUT_SECONDS = 0.25
LAUNCHES = 2

# nodriver 0.47.0 `core/config.py:116-128` `_default_browser_args`, VERBATIM.
# Copied rather than imported because this script is stdlib-only and must run
# before anything this repo installs. `tests/test_chrome_cold_start_probe.py`
# compares it against the installed nodriver, so a version bump that changes
# the flags fails there instead of silently re-defining what is measured.
NODRIVER_DEFAULT_ARGS = (
    "--remote-allow-origins=*",
    "--no-first-run",
    "--no-service-autorun",
    "--no-default-browser-check",
    "--homepage=about:blank",
    "--no-pings",
    "--password-store=basic",
    "--disable-infobars",
    "--disable-breakpad",
    "--disable-dev-shm-usage",
    "--disable-session-crashed-bubble",
    "--disable-search-engine-choice-screen",
)


def binary_prewarmed() -> bool:
    """Whether the gate's identity step already exec'd Chrome on this OS.

    `resolve_chrome._read_version` execs `chrome --version` on Linux only;
    Windows reads a sibling version directory and macOS reads `Info.plist`.
    See the module docstring — where this is true, launch #1 is a FLOOR on the
    cold cost rather than the cold cost.
    """
    return platform.system().lower() == "linux"


def _reserve_port() -> int:
    """A free localhost port, chosen exactly the way nodriver chooses one.

    `nodriver/core/util.py:132-143` binds `127.0.0.1:0`, reads the number and
    CLOSES the socket, leaving a window in which anything can take the port
    before Chrome binds it. Reproducing that idiom (rather than letting Chrome
    pick via `--remote-debugging-port=0`) is what makes the requested-vs-bound
    comparison able to SIZE hypothesis H2 instead of merely asserting it.
    """
    probe_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe_socket.bind(("127.0.0.1", 0))
        probe_socket.listen(5)
        return int(probe_socket.getsockname()[1])
    finally:
        probe_socket.close()


@dataclass(frozen=True)
class LaunchRecord:
    """One launch's measurement. ``None`` means "did not happen in the window"."""

    launch: int
    listening: bool
    ms_to_devtools_banner: float | None
    ms_to_json_version: float | None
    port_requested: int
    port_from_banner: int | None
    port_matches_request: bool | None
    pid: int | None
    exit_code_if_died: int | None
    deadline_ms: float
    output_excerpt: str
    error: str | None


def chrome_command(executable: str, profile_dir: Path, port: int) -> list[str]:
    """The launch the PRODUCT makes, reproduced flag for flag.

    This mirrors `nodriver.Config.__call__` (`core/config.py:174-193`) for the
    gate's own spawn shape — `headless=True`, `sandbox=False`, host and port set
    — in nodriver's own order. Anything else would time a different browser:
    omitting `--password-store=basic` makes headless Linux probe the keyring,
    and omitting `--no-pings` leaves GCM registration traffic in the startup
    path, neither of which the product's Chrome does.

    Deliberately NO `--disable-gpu` and no positional URL: nodriver passes
    neither.
    """
    return [
        executable,
        *NODRIVER_DEFAULT_ARGS,
        f"--user-data-dir={profile_dir}",
        # nodriver appends these two unconditionally, after the defaults, even
        # though the first is already in them. Faithfully duplicated.
        "--disable-session-crashed-bubble",
        "--disable-features=IsolateOrigins,site-per-process",
        "--headless=new",
        "--no-sandbox",
        "--remote-debugging-host=127.0.0.1",
        f"--remote-debugging-port={port}",
    ]


# Chrome's own statement that the endpoint is up, and the port it really bound.
_BANNER_RE = re.compile(rb"DevTools listening on ws://127\.0\.0\.1:(\d+)/")


def _port_from_output(raw: bytes) -> int | None:
    """The port in Chrome's `DevTools listening on ws://...` banner, if written.

    Parsed from BYTES, so a partially-flushed line simply does not match yet and
    reads as "not up yet" — the same way a half-written file would.
    """
    match = _BANNER_RE.search(raw)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _read_log(log_path: Path) -> bytes:
    """The launch's output so far.

    Re-opened by NAME on every poll, deliberately. The child inherits a DUP of
    whatever handle it is given, and a dup SHARES the file offset — so seeking a
    handle the child is still writing through would move the child's write
    position and corrupt the very evidence being collected. A separate open has
    its own offset and cannot.
    """
    try:
        return log_path.read_bytes()
    except OSError:
        return b""


def _json_version_answers(port: int) -> bool:
    """Whether ``/json/version`` returns a JSON body — nodriver's own readiness
    test (``nodriver/core/browser.py:416``), asked the same way so the numbers
    are comparable."""
    try:
        # Loopback only, on a port Chrome itself wrote down.
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version",
            timeout=JSON_VERSION_TIMEOUT_SECONDS,
        ) as response:
            json.loads(response.read())
    except (
        urllib.error.URLError,
        # http.client.HTTPException is NOT an OSError and urllib does NOT wrap
        # it: a socket that accepts and then replies with a non-HTTP line raises
        # BadStatusLine straight through, and a truncated body raises
        # IncompleteRead. Both are reachable in exactly the case this probe
        # studies — a DevTools port that accepts before its handler is ready, or
        # one now held by something else — and either would escape the whole
        # measurement and redden the job.
        http.client.HTTPException,
        OSError,
        ValueError,
        TimeoutError,
    ):
        return False
    return True


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Kill the launch and everything it forked. Chrome is a process tree, and
    a zygote left behind would poison the *next* launch's measurement."""
    if process.poll() is not None:
        return
    try:
        if platform.system() == "Windows":
            subprocess.run(  # noqa: S603  PERMANENT(fixed argv; the only variable is our own child's pid)
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],  # noqa: S607  PERMANENT(taskkill is a system binary; a full path would break on a non-default SystemRoot)
                capture_output=True,
                check=False,
                timeout=30,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            if platform.system() != "Windows":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        except (OSError, subprocess.SubprocessError):
            pass


def _output_excerpt(log_path: Path) -> str:
    """Head + tail of the launch's own output — the stream nodriver discards.

    ``DevTools listening on ws://...`` goes to Chrome's stderr, so capturing it
    is what lets a post-mortem tell "Chrome never listened" from "Chrome
    listened late" (finding §6.1(d)). BOTH ends are kept because that banner is
    among the FIRST lines written and a struggling launch then spews far more
    after it — a tail-only excerpt would discard exactly the line that matters.

    The output is collected into a FILE rather than a pipe on purpose. A pipe
    has a fixed kernel buffer, and a child that fills it blocks forever on its
    next write while we are still polling — the probe would then measure its own
    deadlock. A file cannot back up, and it also avoids ``subprocess.DEVNULL``,
    which this repo bans outright (TID251): throwing a launched process's output
    away is the very habit F-303 and this finding both exist to correct.
    """
    raw = _read_log(log_path)
    if len(raw) > OUTPUT_HEAD_BYTES + OUTPUT_TAIL_BYTES:
        elided = len(raw) - OUTPUT_HEAD_BYTES - OUTPUT_TAIL_BYTES
        raw = (
            raw[:OUTPUT_HEAD_BYTES]
            + f"\n...[{elided} bytes elided]...\n".encode()
            + raw[-OUTPUT_TAIL_BYTES:]
        )
    return raw.decode("utf-8", errors="replace").strip()


def _launch_failure(message: str, blank: dict[str, object]) -> LaunchRecord:
    """A record for a launch that never started. One shape, one place."""
    return LaunchRecord(
        listening=False,
        ms_to_devtools_banner=None,
        ms_to_json_version=None,
        port_from_banner=None,
        port_matches_request=None,
        pid=None,
        exit_code_if_died=None,
        output_excerpt="",
        error=message,
        **blank,
    )


def probe_once(  # noqa: PLR0913  PERMANENT(each argument is a measurement parameter the hermetic test varies independently)
    command: list[str],
    log_path: Path,
    launch: int,
    port_requested: int,
    deadline_seconds: float = DEADLINE_SECONDS,
    poll_seconds: float = POLL_SECONDS,
) -> LaunchRecord:
    """Launch *command*, time it to DevTools, kill it, and report.

    *command* is passed in fully built rather than derived here so the hermetic
    test can substitute a fake browser: what is under test is the polling and
    timing, and that logic must not need a real Chrome to exercise. *log_path*
    is where the launch's own output is collected — opened twice, once for the
    child to write through and once, by name, for this process to read.
    """
    deadline_ms = deadline_seconds * 1000.0
    blank: dict[str, object] = {
        "launch": launch,
        "port_requested": port_requested,
        "deadline_ms": deadline_ms,
    }
    new_session = platform.system() != "Windows"
    try:
        sink = log_path.open("wb")
    except OSError as exc:
        return _launch_failure(f"{type(exc).__name__}: {exc}", blank)
    try:
        process = subprocess.Popen(  # noqa: S603  PERMANENT(command is built by chrome_command from a resolved binary, never from user input)
            command,
            stdout=sink,
            stderr=sink,
            start_new_session=new_session,
        )
    except (OSError, ValueError) as exc:
        sink.close()
        return _launch_failure(f"{type(exc).__name__}: {exc}", blank)

    started = time.monotonic()
    ms_to_devtools_banner: float | None = None
    ms_to_json_version: float | None = None
    port_from_banner: int | None = None
    died: int | None = None
    try:
        while (time.monotonic() - started) < deadline_seconds:
            if port_from_banner is None:
                port_from_banner = _port_from_output(_read_log(log_path))
                if port_from_banner is not None:
                    ms_to_devtools_banner = (time.monotonic() - started) * 1000.0
            # Ask the port Chrome SAYS it bound once it has said so, and the one
            # we requested until then — so a lost-port race surfaces as a banner
            # that disagrees, rather than as a silent never-ready.
            asking = (
                port_from_banner if port_from_banner is not None else port_requested
            )
            if asking and _json_version_answers(asking):
                ms_to_json_version = (time.monotonic() - started) * 1000.0
                break
            if process.poll() is not None:
                died = process.returncode
                break
            time.sleep(poll_seconds)
        else:
            died = process.poll()
    finally:
        _terminate(process)
        sink.close()
        excerpt = _output_excerpt(log_path)

    return LaunchRecord(
        listening=ms_to_json_version is not None,
        ms_to_devtools_banner=ms_to_devtools_banner,
        ms_to_json_version=ms_to_json_version,
        port_from_banner=port_from_banner,
        port_matches_request=(
            None if port_from_banner is None else port_from_banner == port_requested
        ),
        pid=process.pid,
        exit_code_if_died=died,
        output_excerpt=excerpt,
        error=None,
        **blank,
    )


def probe(
    executable: str,
    launches: int = LAUNCHES,
    deadline_seconds: float = DEADLINE_SECONDS,
) -> list[LaunchRecord]:
    """Run *launches* back-to-back cold launches, each on a fresh profile.

    A fresh profile per launch keeps the only difference between launch #1 and
    launch #2 the state of the MACHINE (page cache, warm loader), which is the
    comparison the finding needs — the profile is deliberately not the variable.
    """
    records: list[LaunchRecord] = []
    for index in range(1, launches + 1):
        # ignore_cleanup_errors: Windows cells run this too, and Chrome can
        # still hold a handle under the profile for a moment after
        # `taskkill /T /F` — a cleanup error there must not become the job's
        # failure when the measurement itself already succeeded.
        with tempfile.TemporaryDirectory(
            prefix=f"f870-cold-{index}-", ignore_cleanup_errors=True
        ) as tmp:
            root = Path(tmp)
            profile_dir = root / "profile"
            profile_dir.mkdir()
            port = _reserve_port()
            records.append(
                probe_once(
                    chrome_command(executable, profile_dir, port),
                    root / "launch.log",
                    launch=index,
                    port_requested=port,
                    deadline_seconds=deadline_seconds,
                )
            )
    return records


def main() -> int:
    """Always returns 0. A probe that reddens a gate would be worse than no
    probe: it converts an evidence-gathering step into a new flake source."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cell", default="unknown", help="matrix cell label")
    parser.add_argument("--out", type=Path, help="write the JSON record here")
    parser.add_argument(
        "--deadline", type=float, default=DEADLINE_SECONDS, help="seconds per launch"
    )
    args = parser.parse_args()

    record: dict[str, object] = {
        "finding": "F-870",
        "cell": args.cell,
        "os": platform.system(),
        "arch": platform.machine(),
        "nodriver_connect_window_ms": 2750,
        # See binary_prewarmed(): where True, launch #1 is a FLOOR on the cold
        # cost, because the gate's identity step already exec'd the binary.
        "binary_prewarmed": binary_prewarmed(),
        "launches": [],
    }
    try:
        chrome_path = _resolve_path()
        record["chrome_path"] = str(chrome_path)
        launches = [
            asdict(r) for r in probe(str(chrome_path), deadline_seconds=args.deadline)
        ]
        record["launches"] = launches
        for row in launches:
            print(json.dumps(row, sort_keys=True))
    except Exception as exc:  # noqa: BLE001  PERMANENT(the probe must never fail a gate job; the failure IS the measurement)
        record.setdefault("chrome_path", None)
        record["error"] = f"{type(exc).__name__}: {exc}"

    print(
        json.dumps({k: v for k, v in record.items() if k != "launches"}, sort_keys=True)
    )
    if args.out:
        try:
            args.out.write_text(json.dumps(record, indent=2), encoding="utf-8")
        except OSError as exc:
            print(f"could not write {args.out}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
