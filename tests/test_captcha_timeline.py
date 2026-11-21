"""``tests/captcha_timeline.py``: what the captcha speed test excuses, and what it does not.

The speed test in ``test_captcha_integration.py`` charges the monitor for the
delay it adds, not for the time the page held its CDP calls. These pin the
arithmetic without a browser, so a change to it cannot quietly widen the excuse.
"""

from __future__ import annotations

from captcha_timeline import HEALTHY_CALL_MS, Timeline, charged_ms


def _timeline(first_seen: float | None, *spans: tuple[float, float]) -> Timeline:
    line = Timeline(first_seen_ms=first_seen)
    for start, end in spans:
        line.call(start, end)
    return line


def _chain(start: float, count: int, each: float) -> list[tuple[float, float]]:
    """``count`` calls back to back, ``each`` ms long, from ``start``."""
    return [(start + i * each, start + (i + 1) * each) for i in range(count)]


def test_healthy_calls_hold_nothing():
    line = _timeline(49, *_chain(349, 8, 2.0), *_chain(400, 3, 20.0))
    assert line.held_ms(1000) == 0.0


def test_a_slow_call_holds_only_what_a_healthy_one_would_not_have_taken():
    line = _timeline(0, (100, 100 + HEALTHY_CALL_MS + 300))
    assert line.held_ms(1000) == 300


def test_v2_1_67s_gate_shape_is_charged_what_the_monitor_added():
    """The publish gate's click came 1590 ms after ready: a scan of 8 calls took
    781 ms (~98 ms each), the state read 101 ms, the press's three calls ~92 ms
    each (its 70 ms of sleeps are the monitor's own)."""
    calls = [
        *_chain(349, 8, 781 / 8),  # the scan that waited on the page
        *_chain(1142, 3, 34.0),  # the state read
        *_chain(1243, 3, 92.0),  # the press
    ]
    line = _timeline(49, *calls)
    held = line.held_ms(1590)
    assert 700 < held < 900
    assert charged_ms(1590, held) <= 1000


def test_time_the_monitor_spends_outside_cdp_is_charged_in_full():
    # a longer interval, a longer age, a sleep, more work per scan: calls stay fast
    line = _timeline(49, *_chain(1300, 8, 2.0))
    assert line.held_ms(2573) == 0.0
    assert charged_ms(2573, line.held_ms(2573)) == 2573


def test_calls_before_the_widget_was_found_do_not_count():
    line = _timeline(500, (0, 900), (600, 700 + HEALTHY_CALL_MS))
    assert line.held_ms(2000) == 100


def test_calls_after_the_click_do_not_count():
    line = _timeline(0, (100, 400), (1700, 2000))
    assert line.held_ms(1600) == 300 - HEALTHY_CALL_MS


def test_nothing_is_excused_before_the_widget_was_ever_found():
    assert _timeline(None, (0, 900)).held_ms(2000) == 0.0


def test_the_charge_never_goes_below_zero():
    assert charged_ms(100, 800) == 0.0
