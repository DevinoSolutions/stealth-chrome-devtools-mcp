"""T-18116: the job log prints every ``record_property`` value (conftest hook)."""

from types import SimpleNamespace

from conftest import pytest_terminal_summary


class _Reporter:
    def __init__(self, stats):
        self.stats = stats
        self.lines = []

    def write_sep(self, sep, title):
        self.lines.append(f"{sep * 3} {title}")

    def write_line(self, line):
        self.lines.append(line)


def _report(nodeid, when, props):
    return SimpleNamespace(nodeid=nodeid, when=when, user_properties=props)


def test_call_properties_of_passed_and_failed_nodes_are_printed():
    reporter = _Reporter(
        {
            "passed": [
                _report("t.py::a", "call", [("captcha_turnstile_click_ms", 213)])
            ],
            "failed": [
                _report("t.py::b", "call", [("captcha_hcaptcha_click_ms", 1045)])
            ],
            "": [_report("t.py::a", "setup", [("ignored", 1)])],
        }
    )
    pytest_terminal_summary(reporter)
    assert reporter.lines == [
        "--- recorded properties",
        "t.py::a: captcha_turnstile_click_ms=213",
        "t.py::b: captcha_hcaptcha_click_ms=1045",
    ]


def test_nothing_is_printed_when_no_node_recorded_a_property():
    reporter = _Reporter(
        {"passed": [_report("t.py::a", "call", [])], "warnings": [object()]}
    )
    pytest_terminal_summary(reporter)
    assert reporter.lines == []
