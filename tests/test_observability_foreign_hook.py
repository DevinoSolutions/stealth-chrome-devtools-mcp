"""T-7828 — a process-global hook must not report a FOREIGN interpreter's crash.

``sentry_sdk.init`` installs ``sys.excepthook`` (and the threading integration)
for the whole interpreter. The OSSF package-analysis sandbox runs our entry
points inside ``/usr/local/bin/analyze-python.py``; that script's own
``ValueError: I/O operation on closed file`` reached the hook and shipped under
our release, one event per PyPI release. ``observability._scrub_event`` now drops
an event whose mechanism is such a hook when no frame in it is ours.

Both directions are pinned through the REAL SDK: each scenario is a child
interpreter that initialises Sentry with our own ``sentry_init`` (so the hook,
the integrations and ``before_send`` are the production ones) over a transport
that prints what would have been sent. A hand-written event dict would encode
whatever mechanism and frame shape we assumed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from stealth_chrome_devtools_mcp import observability

sentry_sdk = pytest.importorskip("sentry_sdk")
event_from_exception = pytest.importorskip("sentry_sdk.utils").event_from_exception

PACKAGE_DIR = Path(observability.__file__).parent

_PRELUDE = """
import sentry_sdk
from sentry_sdk.transport import Transport

class Capture(Transport):
    def capture_envelope(self, envelope):
        for item in envelope.items:
            event = item.get_event()
            if event is None:
                continue
            for value in (event.get("exception") or {}).get("values", []):
                print("SENT", value["mechanism"]["type"], value["type"], flush=True)

_init = sentry_sdk.init
sentry_sdk.init = lambda **options: _init(transport=Capture, **options)

from stealth_chrome_devtools_mcp import observability
assert observability.sentry_init()
print("READY", flush=True)
"""


def _run(tmp_path: Path, body: str) -> list[str]:
    """Run ``body`` in a child interpreter after a real ``sentry_init`` and return
    the ``SENT`` lines. The script lives in ``tmp_path``: foreign code."""
    script = tmp_path / "analyze_python_stand_in.py"
    script.write_text(_PRELUDE + textwrap.dedent(body), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "STEALTH_MCP_NO_ERROR_REPORTING"}
    done = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=tmp_path,
        check=False,
    )
    # A child that died before the hook existed would "send nothing" for the wrong reason.
    assert "READY" in done.stdout, done.stderr
    return [line for line in done.stdout.splitlines() if line.startswith("SENT")]


class TestTheRealHooks:
    def test_a_foreign_uncaught_exception_sends_nothing(self, tmp_path):
        sent = _run(tmp_path, 'raise ValueError("I/O operation on closed file")')
        assert sent == []

    def test_a_foreign_thread_crash_sends_nothing(self, tmp_path):
        body = """
        import threading
        t = threading.Thread(target=lambda: 1 / 0)
        t.start()
        t.join()
        """
        assert _run(tmp_path, body) == []

    def test_an_uncaught_raise_from_our_package_is_sent(self, tmp_path):
        # TypeError is raised by the regex engine, but our frame is in the traceback.
        sent = _run(tmp_path, "observability._redact_text(None)")
        assert sent == ["SENT excepthook TypeError"]

    def test_a_thread_crash_in_our_package_is_sent(self, tmp_path):
        body = """
        import threading
        t = threading.Thread(target=observability._redact_text, args=(None,))
        t.start()
        t.join()
        """
        assert _run(tmp_path, body) == ["SENT threading TypeError"]

    def test_a_foreign_handled_capture_is_still_sent(self, tmp_path):
        body = """
        try:
            raise ValueError("handled elsewhere")
        except ValueError:
            sentry_sdk.capture_exception()
        """
        sent = _run(tmp_path, body)
        assert len(sent) == 1
        assert sent[0].endswith("ValueError")
        assert "excepthook" not in sent[0]
        assert "threading" not in sent[0]


def _hook_event(filename: Path, module: str) -> tuple[dict, dict]:
    """An excepthook event for an exception raised from code compiled as ``filename``."""
    try:
        exec(  # noqa: S102  PERMANENT(the fixture IS code compiled under a chosen filename; that path is what the SDK serializes)
            compile("raise ValueError('x')", str(filename), "exec"),
            {"__name__": module},
        )
    except ValueError:
        return event_from_exception(
            sys.exc_info(), mechanism={"type": "excepthook", "handled": False}
        )
    raise AssertionError("fixture did not raise")


class TestAFrameIsOursByFileToo:
    """The backend runs ``embedded/server.py`` as ``__main__`` under ``runpy``, so
    its frames carry module ``__main__``: only the path says they are ours."""

    def test_a_main_module_frame_inside_the_package_is_kept(self):
        event, hint = _hook_event(PACKAGE_DIR / "embedded" / "server.py", "__main__")
        assert observability._scrub_event(event, hint) is not None

    def test_a_main_module_frame_elsewhere_is_dropped(self, tmp_path):
        event, hint = _hook_event(tmp_path / "analyze-python.py", "__main__")
        assert observability._scrub_event(event, hint) is None


def _hand_event(*chain: tuple[str, ...]) -> dict:
    """An excepthook event whose i-th exception has the frames ``chain[i]``, each a
    ``(module, abs_path)`` pair. Hand-built on purpose: the SDK cannot be made to
    put an arbitrary frame at an arbitrary position in a chain."""
    return {
        "exception": {
            "values": [
                {
                    "type": "ValueError",
                    "value": "x",
                    "mechanism": {"type": "excepthook", "handled": False},
                    "stacktrace": {
                        "frames": [
                            {"module": module, "abs_path": abs_path, "lineno": 1}
                            for module, abs_path in frames
                        ]
                    },
                }
                for frames in chain
            ]
        }
    }


OURS = ("stealth_chrome_devtools_mcp.x", "/elsewhere/x.py")
FOREIGN = ("analyze_python", "/elsewhere/analyze-python.py")


class TestEveryExceptionInTheChainIsJudged:
    """An unrelated crash must not hide behind, or be exposed by, its position."""

    def test_an_event_with_no_frames_is_kept(self):
        event = _hand_event(())
        assert observability._scrub_event(event, None) is not None

    def test_ours_in_a_later_value_keeps_the_event(self):
        event = _hand_event((FOREIGN,), (OURS,))
        assert observability._scrub_event(event, None) is not None

    def test_ours_in_the_first_value_keeps_the_event(self):
        event = _hand_event((OURS,), (FOREIGN,))
        assert observability._scrub_event(event, None) is not None

    def test_foreign_in_every_value_drops_the_event(self):
        event = _hand_event((FOREIGN,), (FOREIGN,))
        assert observability._scrub_event(event, None) is None


class TestAKeptHookEventIsStillScrubbed:
    def test_the_home_directory_is_anonymised_and_the_mechanism_unchanged(self):
        event = _hand_event(
            (
                (FOREIGN[0], "/home/jdoe/analyze-python.py"),
                ("stealth_chrome_devtools_mcp.x", "/home/jdoe/site-packages/x.py"),
            )
        )
        out = observability._scrub_event(event, None)
        assert out is not None
        value = out["exception"]["values"][0]
        assert value["mechanism"] == {"type": "excepthook", "handled": False}
        assert "jdoe" not in repr(out)
        assert (
            value["stacktrace"]["frames"][1]["abs_path"] == "/home/~/site-packages/x.py"
        )
