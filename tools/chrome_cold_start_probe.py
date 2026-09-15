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
guesses it: ``DevToolsActivePort``, the file Chrome writes into its user-data-dir
whose first line is **the port Chrome actually bound**. Comparing that port with
the one the launcher asked for is what would catch a lost-port race
(``nodriver/core/util.py:132-143`` binds ``:0``, closes the socket, then hands
the number to a Chrome that binds it later — a race nodriver's own source flags
at ``core/config.py:175-178``). Asking for ``--remote-debugging-port=0`` here
means Chrome chooses, so the file is the only way to learn the answer at all.

**It never fails the job.** Every path exits 0, and a launch that never listens
inside the deadline is not an error — it is the measurement that would confirm
hypothesis H3.

Chrome is located through ``resolve_chrome``, the repo's one Chrome-Stable
resolver, so this does not become a second locator — but through
:func:`resolve_chrome._resolve_path` rather than the public
:func:`resolve_chrome.resolve_chrome`. That is deliberate and it matters:
``resolve_chrome()`` additionally shells out to the binary for ``--version``,
and **that subprocess pages the binary in**, warming the exact thing this script
exists to time. The identity (version, product) is not re-derived here at all —
``chrome-identity.json`` from the sibling "Resolve image Chrome Stable identity"
step is its one home, and this record cites the path only.

Stdlib only, for the same reason ``resolve_chrome`` is: it must run before, and
independently of, anything this repo installs.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO

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
# Chrome's stderr is the evidence nodriver throws away; keep the tail of it.
STDERR_TAIL_BYTES = 4000
LAUNCHES = 2


@dataclass(frozen=True)
class LaunchRecord:
    """One launch's measurement. ``None`` means "did not happen in the window"."""

    launch: int
    listening: bool
    ms_to_active_port: float | None
    ms_to_json_version: float | None
    port_requested: int
    port_from_file: int | None
    port_matches_request: bool | None
    pid: int | None
    exit_code_if_died: int | None
    deadline_ms: float
    stderr_tail: str
    error: str | None


def chrome_command(executable: str, profile_dir: Path, port: int) -> list[str]:
    """The launch a headless CI spawn makes, with nothing of ours in it.

    ``--remote-debugging-port=0`` lets Chrome pick, which is what makes
    ``DevToolsActivePort`` informative: the file then carries a port nobody
    reserved, so a mismatch against *port* can only mean Chrome chose otherwise.
    """
    return [
        executable,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "about:blank",
    ]


def _read_active_port(profile_dir: Path) -> int | None:
    """The port in ``DevToolsActivePort``, or ``None`` until Chrome writes it.

    Chrome writes the file in two lines (port, then the ws path) and does so
    only once the DevTools HTTP server is listening, which is precisely the
    transition nodriver cannot see. A partial read during the write is normal
    and reads as "not yet".
    """
    try:
        first = (
            profile_dir.joinpath("DevToolsActivePort")
            .read_text(encoding="utf-8")
            .splitlines()
        )
    except (OSError, UnicodeDecodeError):
        return None
    if not first:
        return None
    try:
        return int(first[0].strip())
    except ValueError:
        return None


def _json_version_answers(port: int) -> bool:
    """Whether ``/json/version`` returns a JSON body — nodriver's own readiness
    test (``nodriver/core/browser.py:416``), asked the same way so the numbers
    are comparable."""
    try:
        # Loopback only, on a port Chrome itself chose and wrote down.
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=2.0
        ) as response:
            json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
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


def _output_tail(sink: IO[bytes]) -> str:
    """The tail of the launch's own output — the stream nodriver discards.

    ``DevTools listening on ws://...`` goes to Chrome's stderr, so capturing it
    is what lets a post-mortem tell "Chrome never listened" from "Chrome
    listened late" (finding §6.1(d)).

    It is collected into a temporary FILE rather than a pipe on purpose. A pipe
    has a fixed kernel buffer, and a child that fills it blocks forever on its
    next write while we are still polling — the probe would then measure its own
    deadlock. A file cannot back up, and it also avoids ``subprocess.DEVNULL``,
    which this repo bans outright (TID251): throwing a launched process's output
    away is the very habit F-303 and this finding both exist to correct.
    """
    try:
        sink.flush()
        size = sink.tell()
        sink.seek(max(0, size - STDERR_TAIL_BYTES))
        raw = sink.read() or b""
    except (OSError, ValueError):
        return ""
    return raw.decode("utf-8", errors="replace").strip()


def probe_once(  # noqa: PLR0913  PERMANENT(each argument is a measurement parameter the hermetic test varies independently)
    command: list[str],
    profile_dir: Path,
    launch: int,
    port_requested: int,
    deadline_seconds: float = DEADLINE_SECONDS,
    poll_seconds: float = POLL_SECONDS,
) -> LaunchRecord:
    """Launch *command*, time it to DevTools, kill it, and report.

    *command* is passed in fully built rather than derived here so the hermetic
    test can substitute a fake browser: what is under test is the polling and
    timing, and that logic must not need a real Chrome to exercise.
    """
    deadline_ms = deadline_seconds * 1000.0
    blank = {
        "launch": launch,
        "port_requested": port_requested,
        "deadline_ms": deadline_ms,
    }
    new_session = platform.system() != "Windows"
    sink = tempfile.TemporaryFile()  # noqa: SIM115  PERMANENT(closed in the finally below)
    try:
        process = subprocess.Popen(  # noqa: S603  PERMANENT(command is built by chrome_command from a resolved binary, never from user input)
            command,
            stdout=sink,
            stderr=sink,
            start_new_session=new_session,
        )
    except (OSError, ValueError) as exc:
        sink.close()
        return LaunchRecord(
            listening=False,
            ms_to_active_port=None,
            ms_to_json_version=None,
            port_from_file=None,
            port_matches_request=None,
            pid=None,
            exit_code_if_died=None,
            stderr_tail="",
            error=f"{type(exc).__name__}: {exc}",
            **blank,
        )

    started = time.monotonic()
    ms_to_active_port: float | None = None
    ms_to_json_version: float | None = None
    port_from_file: int | None = None
    died: int | None = None
    try:
        while (time.monotonic() - started) < deadline_seconds:
            if port_from_file is None:
                port_from_file = _read_active_port(profile_dir)
                if port_from_file is not None:
                    ms_to_active_port = (time.monotonic() - started) * 1000.0
            if port_from_file is not None and _json_version_answers(port_from_file):
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
        tail = _output_tail(sink)
        sink.close()

    return LaunchRecord(
        listening=ms_to_json_version is not None,
        ms_to_active_port=ms_to_active_port,
        ms_to_json_version=ms_to_json_version,
        port_from_file=port_from_file,
        port_matches_request=(
            None
            if port_from_file is None or port_requested == 0
            else port_from_file == port_requested
        ),
        pid=process.pid,
        exit_code_if_died=died,
        stderr_tail=tail,
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
        with tempfile.TemporaryDirectory(prefix=f"f870-cold-{index}-") as tmp:
            profile_dir = Path(tmp)
            records.append(
                probe_once(
                    chrome_command(executable, profile_dir, 0),
                    profile_dir,
                    launch=index,
                    port_requested=0,
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
    }
    try:
        chrome_path = _resolve_path()
    except (OSError, RuntimeError, ValueError) as exc:
        record["chrome_path"] = None
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["launches"] = []
    else:
        record["chrome_path"] = str(chrome_path)
        launches = [
            asdict(r) for r in probe(str(chrome_path), deadline_seconds=args.deadline)
        ]
        record["launches"] = launches
        for row in launches:
            print(json.dumps(row, sort_keys=True))

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
