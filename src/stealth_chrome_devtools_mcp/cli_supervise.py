"""The ``supervise`` verb (T-15386): one pass of the out-of-process backend
supervisor, meant to be run by a scheduled task once a minute.

The rule, the state and the act are ``embedded.backend_supervisor``'s; this file
is the parser, the output and the BINDING — which probes are ours, which is
``singleton``'s knowledge, handed in exactly as ``singleton`` hands its own to
``backend_eviction``. Its own file, on ``cli_call``'s precedent, so ``cli.py``
keeps the parser tree and the ops table and gains two lines.

Module scope imports only the standard library; every ``embedded`` import is
inside :func:`cmd_supervise`, so building the parser for any other verb costs
nothing. It never imports ``server``, so a pass can never run orphan recovery.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from stealth_chrome_devtools_mcp.embedded.backend_supervisor import Verdict


def add_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Contribute ``supervise`` to ``cli``'s ONE parser tree."""
    supervise = sub.add_parser(
        "supervise",
        help="one pass of the backend watchdog: terminate a backend that has been "
        "unusable for 3+ minutes (10 if its own loop says it is fine) so the next "
        "call starts a fresh one; run it from a scheduled task every minute",
    )
    supervise.add_argument(
        "--dry-run",
        action="store_true",
        help="judge and keep the clocks, but terminate nothing",
    )
    supervise.add_argument("--json", action="store_true", help="JSON output")


def _line(verdict: Verdict, dry_run: bool) -> str:
    action = ""
    if verdict.kill:
        action = "  -> would terminate" if dry_run else "  -> terminating"
    return (
        f"port {verdict.port}  pid {verdict.pid}  {verdict.state}"
        f"  unusable for {verdict.unusable_for:.0f}s{action}"
    )


def cmd_supervise(args: argparse.Namespace) -> int:
    """Run one pass and print one line per backend of ours. Exit 0 always: a
    scheduled task has nobody to read a non-zero status, and the verdicts and
    kills are in the output, the state file and (on trouble) the log."""
    # The scheduled task runs this under pythonw.exe, so no console window
    # flashes up every minute; pythonw has no stdout, and `cli.main` flushes it.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, Path(os.devnull).open("w", encoding="utf-8"))  # noqa: SIM115  PERMANENT(process-lifetime stream)
    from stealth_chrome_devtools_mcp.embedded import (
        backend_probe,
        backend_supervisor,
        logging_setup,
        singleton,
    )

    def healthy(port: int) -> bool:
        beat = backend_probe.Heartbeat(singleton._backend_http_url(port))
        try:
            return beat.alive(backend_supervisor.HEALTH_TIMEOUT_SECONDS)
        finally:
            beat.close()

    def open_log() -> None:
        logging_setup.configure_logging("supervisor")

    verdicts = backend_supervisor.run_pass(
        singleton.SERVER_STATE_FILE,
        is_ours=singleton._is_our_backend,
        healthy=healthy,
        is_healthy=singleton._server_is_healthy,
        dry_run=args.dry_run,
        on_trouble=open_log,
    )
    if args.json:
        print(json.dumps([v._asdict() for v in verdicts], indent=2))
        return 0
    if not verdicts:
        print("no backend of ours is recorded.")
    for verdict in verdicts:
        print(_line(verdict, args.dry_run))
    return 0


#: Verb name -> body, for ``cli.main``'s dispatch (``cli_call.DISPATCH``'s shape).
DISPATCH: dict[str, Callable[[argparse.Namespace], int]] = {
    "supervise": cmd_supervise,
}
