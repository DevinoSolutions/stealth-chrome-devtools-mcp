"""RELEASE-FIX-A C7 (A11): BrowserInstance timestamps must be tz-aware (UTC).

update_activity writes an aware UTC ``last_activity`` (models.py:37), but the
defaults were naive ``datetime.now``. The idle reaper subtracts an aware "now"
from ``last_activity``; a naive default makes that raise
``TypeError: can't subtract offset-naive and offset-aware datetimes`` (currently
caught → the reaper silently reaps nothing). Defaults are now aware UTC.
"""

from datetime import datetime, timezone

from stealth_chrome_devtools_mcp.embedded.models import BrowserInstance


def test_default_timestamps_are_utc_aware_and_reaper_safe():
    inst = BrowserInstance(instance_id="i1")

    # (a) both default timestamps are tz-aware
    assert inst.created_at.tzinfo is not None, "created_at default is naive"
    assert inst.last_activity.tzinfo is not None, "last_activity default is naive"

    # (b) the exact idle-reaper hot-path subtraction must not raise TypeError
    delta = datetime.now(timezone.utc) - inst.last_activity
    assert delta.total_seconds() >= 0
