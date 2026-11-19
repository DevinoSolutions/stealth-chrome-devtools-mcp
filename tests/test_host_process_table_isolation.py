"""A hermetic test never depends on the HOST's unreadable browser processes (T-19642).

``conftest._the_host_has_no_unreadable_browser`` stubs
``browser_cmdline.unreadable_browsers`` to "none" for every test except those marked
``integration`` or ``real_process_table``. These pins hold the three halves of that
rule: the default is stubbed, the opt-out sees the table, and the decision is made on
the two markers and nothing else, so an integration test can never lose the real host.
"""

from __future__ import annotations

import psutil
import pytest

from conftest import REAL_PROCESS_TABLE_MARKERS, sees_the_real_process_table
from stealth_chrome_devtools_mcp.embedded import browser_cmdline


class _Elevated:
    """A browser-named process whose command line this account may not read."""

    pid = 424242

    def cmdline(self) -> list[str]:
        raise psutil.AccessDenied(self.pid)


def _a_table_with_an_elevated_chrome(monkeypatch: pytest.MonkeyPatch) -> _Elevated:
    chrome = _Elevated()
    monkeypatch.setattr(
        browser_cmdline, "named_processes", lambda: iter([(chrome, "chrome.exe")])
    )
    return chrome


def test_by_default_the_hosts_unreadable_browsers_are_not_seen(monkeypatch):
    """The fixture replaced ``unreadable_browsers`` itself, so a table that WOULD
    name an unreadable Chrome is never asked."""
    _a_table_with_an_elevated_chrome(monkeypatch)

    assert browser_cmdline.unreadable_browsers(lambda _name: True) == ()


@pytest.mark.real_process_table
def test_a_test_that_opts_out_sees_the_table(monkeypatch):
    chrome = _a_table_with_an_elevated_chrome(monkeypatch)

    assert browser_cmdline.unreadable_browsers(lambda _name: True) == (chrome.pid,)


class _Node:
    def __init__(self, *markers: str) -> None:
        self._markers = set(markers)

    def get_closest_marker(self, name: str) -> object | None:
        return object() if name in self._markers else None


@pytest.mark.parametrize(
    ("markers", "real"),
    [
        pytest.param((), False, id="a hermetic test is stubbed"),
        pytest.param(("integration",), True, id="an integration test sees the host"),
        pytest.param(("real_process_table",), True, id="the opt-out sees the table"),
        pytest.param(("characterization",), False, id="another marker changes nothing"),
    ],
)
def test_only_the_two_markers_lift_the_stub(markers, real):
    assert sees_the_real_process_table(_Node(*markers)) is real


def test_the_two_markers_are_the_documented_pair():
    assert REAL_PROCESS_TABLE_MARKERS == ("integration", "real_process_table")
