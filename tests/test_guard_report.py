"""The wording of the DevTools-port guard's refusals (T-20441)."""

from __future__ import annotations

import logging

import pytest

from stealth_chrome_devtools_mcp.embedded.guard_report import refused, where, why


@pytest.mark.parametrize(
    ("url", "said"),
    [
        ("chrome://settings/onStartup", "chrome://settings"),
        ("devtools://devtools/bundled/inspector.html?ws=x", "devtools://devtools"),
        ("about:blank", "about:blank"),
        ("https://user:pw@bank.example/a?t=1#f", "https: a site"),
        ("http://intranet.corp/", "http: a site"),
        ("file:///C:/Users/me/secret.txt", "file: a site"),
        ("data:text/html,<p>private</p>", "data: a site"),
        ("blob:https://bank.example/1234", "blob: a site"),
        ("", "no scheme: a site"),
        (None, "no scheme: a site"),
        ("http://[broken", "unreadable url"),
    ],
)
def test_only_chromes_own_pages_are_named(url, said):
    assert where(url) == said


def test_a_refusal_carries_chromes_message_or_its_code():
    assert refused("Fetch.enable", {"message": "no"}) == "Fetch.enable refused: no"
    assert refused("Fetch.enable", {"code": -32000}) == (
        "Fetch.enable refused: code -32000"
    )
    assert len(refused("M", {"message": "x" * 500})) == len("M refused: ") + 200


def test_a_frame_that_timed_out_is_named_by_the_timeout(caplog):
    answers = [{"ok": 1}, TimeoutError(), RuntimeError("Page.enable refused: %s 5%")]
    with caplog.at_level("WARNING"):
        logging.getLogger("t").warning(
            *why("iframe", {"url": "https://bank.example/x"}, answers)
        )
    assert caplog.messages == [
        "a frame could not be guarded; blanking its page "
        "(https: a site: TimeoutError; RuntimeError Page.enable refused: %s 5%)"
    ]


def test_an_error_whose_text_cannot_be_read_is_named_by_its_type():
    class MuteError(Exception):
        def __str__(self):
            raise ValueError("no text")

    assert why("page", {}, [MuteError()])[2] == "MuteError"
