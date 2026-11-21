"""What the captcha monitor did during a speed test, and how long the PAGE held it.

``tests/test_captcha_integration.py`` holds the monitor to a budget measured
from the moment the checkbox became clickable to the click. That number is the
monitor's own delay plus whatever the page costs the monitor: every scan, state
read and press is a chain of CDP round trips (about 15 in series between the
widget becoming due and the press), and each one is answered by the page's main
thread. A call a healthy page answers in 1-30 ms (``captcha_clicker``'s
docstring) took ~90 ms on a macOS publish runner whose page was busy running
the provider's script, which alone put the click at 1590 ms (v2.1.67's gate).

This module records every CDP call the monitor makes (``captcha_clicker._send``
and ``page_natives.frame_world``, which the state read uses) with its start and
end, so the test can say how long the page held the monitor and charge the
monitor only for the rest. Only CDP time is excused: a monitor that is slow
anywhere else (a longer interval, a longer first-click age, a sleep, a scan that
does more work) is charged in full.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

#: A CDP call the monitor makes takes longer than this only when the page held
#: it. Healthy calls took 1-30 ms for a whole scan of ~8 calls (the module
#: docstring of ``captcha_clicker``), so 25 ms per call is generous.
HEALTHY_CALL_MS = 25.0

#: A hold of at least this many ms is reported as a warning (and recorded).
STALL_WARN_MS = 100.0


def now_ms() -> float:
    return time.time() * 1000


@dataclass
class Timeline:
    """``events``: ``(epoch_ms, what)`` per scan, read and press. ``calls``:
    ``(start_ms, end_ms)`` of every CDP call. ``first_seen_ms``: when the scan
    that first found the widget ended (the age gate counts from there)."""

    events: list[tuple[float, str]] = field(default_factory=list)
    calls: list[tuple[float, float]] = field(default_factory=list)
    first_seen_ms: float | None = None

    def note(self, what: str) -> None:
        self.events.append((now_ms(), what))

    def call(self, start_ms: float, end_ms: float) -> None:
        self.calls.append((start_ms, end_ms))

    def held_ms(self, until_ms: float) -> float:
        """Milliseconds the page held the monitor's CDP calls, beyond what a
        healthy page takes, between the widget being found and ``until_ms``.

        Before the widget is found nothing waits on the monitor (the click
        cannot come before ``first_seen + FIRST_CLICK_AGE``), so those calls
        are not counted."""
        if self.first_seen_ms is None:
            return 0.0
        return sum(
            max(0.0, end - start - HEALTHY_CALL_MS)
            for start, end in self.calls
            if start >= self.first_seen_ms and end <= until_ms
        )

    def since(self, ready_ms: float) -> str:
        return "; ".join(
            f"{at - ready_ms:+.0f} {what}" for at, what in self.events[:60]
        )


def charged_ms(click_ms: float, held_ms: float) -> float:
    """What the budget holds: the click's delay without the page's hold."""
    return max(0.0, click_ms - held_ms)
