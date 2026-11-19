"""What ``login_flow._outcome`` concludes from what the page did (T-19366).

No browser: the page is a script of readings (:func:`login_flow._snapshot`) and of
cookie marks. The integration tests in ``test_login_flow.py`` run the same code
against a real Chrome; these pin the decisions one by one, including the ones a
real page only produces by a race.
"""

from __future__ import annotations

import pytest

from stealth_chrome_devtools_mcp.embedded import login_evidence, login_flow

HOSTS = ["accounts.example.com"]
LOGIN_URL = "https://accounts.example.com/login"
HOME = "https://accounts.example.com/home"
ELSEWHERE = "https://www.example.org/"
OLD, NEW = "loader-old", "loader-new"

NO_FORM = {
    "username": False,
    "password": False,
    "otp": False,
    "password_has_value": False,
}
FORM_FILLED = {**NO_FORM, "password": True, "password_has_value": True}
FORM_EMPTY = {**NO_FORM, "password": True}
OTP = {**NO_FORM, "otp": True}
USERNAME = {**NO_FORM, "username": True}

NO_MARKS = frozenset()
SESSION = frozenset({("sid", "accounts.example.com", "/", "0123456789abcdef")})


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(login_flow, "POLL_S", 0.0)
    monkeypatch.setattr(login_flow, "OUTCOME_WAIT_S", 0.3)


def script(monkeypatch, readings, marks=NO_MARKS):
    """Serve ``readings`` (state, url, loader) one per poll, then the last forever;
    cookie reads return ``marks``."""
    queue = list(readings)

    async def snapshot(_tab):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def session_marks(_tab, _urls):
        return marks

    monkeypatch.setattr(login_flow, "_snapshot", snapshot)
    monkeypatch.setattr(login_evidence, "session_marks", session_marks)


async def outcome(before_marks=NO_MARKS):
    return await login_flow._outcome(None, HOSTS, OLD, ([LOGIN_URL], before_marks))


async def test_a_session_cookie_is_the_evidence_for_logged_in(monkeypatch):
    script(monkeypatch, [(NO_FORM, HOME, NEW)], marks=SESSION)
    assert (await outcome())[0] == "logged_in"


async def test_a_changed_cookie_counts(monkeypatch):
    changed = frozenset({("sid", "accounts.example.com", "/", "fedcba9876543210")})
    script(monkeypatch, [(NO_FORM, HOME, NEW)], marks=changed)
    assert (await outcome(before_marks=SESSION))[0] == "logged_in"


async def test_leaving_the_login_hosts_is_evidence(monkeypatch):
    script(monkeypatch, [(NO_FORM, ELSEWHERE, NEW)])
    assert (await outcome())[0] == "logged_in"


async def test_a_new_form_less_page_with_no_session_is_needs_user(monkeypatch):
    script(monkeypatch, [(NO_FORM, "https://accounts.example.com/locked", NEW)])
    result = await outcome()
    assert result[0] == "needs_user"
    assert "no sign of a session" in result[1]


async def test_an_unchanged_cookie_jar_is_not_evidence(monkeypatch):
    script(monkeypatch, [(NO_FORM, HOME, NEW)], marks=SESSION)
    assert (await outcome(before_marks=SESSION))[0] == "needs_user"


async def test_the_form_back_for_good_is_a_rejection(monkeypatch):
    script(monkeypatch, [(FORM_FILLED, LOGIN_URL, NEW)])
    assert (await outcome())[0] == "rejected"


async def test_an_emptied_field_that_stays_empty_is_a_rejection(monkeypatch):
    script(monkeypatch, [(FORM_EMPTY, LOGIN_URL, OLD)])
    assert (await outcome())[0] == "rejected"


async def test_a_form_seen_for_one_poll_is_not_a_rejection(monkeypatch):
    """A page clears the field while its request is in flight, then signs in."""
    script(
        monkeypatch,
        [(FORM_EMPTY, LOGIN_URL, OLD), (NO_FORM, HOME, NEW)],
        marks=SESSION,
    )
    assert (await outcome())[0] == "logged_in"


async def test_a_reading_across_a_navigation_is_thrown_away(monkeypatch):
    """The probe saw the old document's form while the loader had moved on (``None``):
    that mixed reading used to be a rejection of a password that was right."""
    script(
        monkeypatch,
        [(FORM_FILLED, HOME, None)] * 5 + [(NO_FORM, HOME, NEW)],
        marks=SESSION,
    )
    assert (await outcome())[0] == "logged_in"


async def test_a_form_that_never_settles_is_not_frozen(monkeypatch):
    flicker = [(FORM_FILLED, LOGIN_URL, NEW), (FORM_FILLED, LOGIN_URL, None)] * 6
    script(monkeypatch, flicker)
    assert (await outcome())[0] == "needs_user"


async def test_a_one_time_code_prompt_is_reported(monkeypatch):
    script(monkeypatch, [(OTP, HOME, NEW)])
    assert (await outcome())[0] == "needs_totp"


async def test_the_username_step_again_is_needs_user(monkeypatch):
    script(monkeypatch, [(USERNAME, LOGIN_URL, NEW)])
    assert (await outcome())[0] == "needs_user"
