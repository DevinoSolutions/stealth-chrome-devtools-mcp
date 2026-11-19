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
LOCKED = "https://accounts.example.com/locked"
ELSEWHERE = "https://www.example.org/"
OLD, NEW = "loader-old", "loader-new"

NO_FORM = {
    "username": False,
    "password": False,
    "otp": False,
    "password_has_value": False,
    "error": False,
}
FORM_FILLED = {**NO_FORM, "password": True, "password_has_value": True}
FORM_EMPTY = {**NO_FORM, "password": True}
FORM_EMPTY_WITH_ERROR = {**FORM_EMPTY, "error": True}
OTP = {**NO_FORM, "otp": True}
USERNAME = {**NO_FORM, "username": True}

NO_MARKS = frozenset()
SESSION = frozenset({("sid", "accounts.example.com", "/", "0123456789abcdef")})
CSRF = frozenset({("csrf", "accounts.example.com", "/", "aaaaaaaaaaaaaaaa")})


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(login_flow, "POLL_S", 0.0)
    monkeypatch.setattr(login_flow, "OUTCOME_WAIT_S", 0.5)
    monkeypatch.setattr(login_flow, "_REJECT_SETTLE_S", 0.0)


def script(monkeypatch, readings, marks=NO_MARKS, confirm=None):
    """Serve ``readings`` (state, url, loader) one per poll, then the last forever;
    cookie reads return ``marks``; the revisit of the login page answers
    ``confirm`` (a ``(state, url)``), which defaults to "sent on to /home"."""
    queue = list(readings)
    visits = []

    async def snapshot(_tab):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def session_marks(_tab, _urls):
        return marks

    async def confirm_(_tab, _hosts, _login_url):
        visits.append(1)
        return confirm or ("logged_in", "signed in")

    monkeypatch.setattr(login_flow, "_snapshot", snapshot)
    monkeypatch.setattr(login_evidence, "session_marks", session_marks)
    monkeypatch.setattr(login_flow, "_confirm", confirm_)
    return visits


async def outcome(before_marks=NO_MARKS, error=False):
    baseline = login_flow._Baseline([LOGIN_URL], before_marks, LOGIN_URL, error)
    return await login_flow._outcome(None, HOSTS, OLD, baseline)


# ------------------------------------------------------------- logged_in


async def test_a_session_cookie_and_a_login_page_that_sends_us_on_is_logged_in(
    monkeypatch,
):
    visits = script(monkeypatch, [(NO_FORM, HOME, NEW)], marks=SESSION)
    assert (await outcome())[0] == "logged_in"
    assert visits == [1]  # the cookie alone was not taken as proof


async def test_a_changed_cookie_counts(monkeypatch):
    changed = frozenset({("sid", "accounts.example.com", "/", "fedcba9876543210")})
    script(monkeypatch, [(NO_FORM, HOME, NEW)], marks=changed)
    assert (await outcome(before_marks=SESSION))[0] == "logged_in"


async def test_leaving_the_login_hosts_is_evidence_with_no_revisit(monkeypatch):
    visits = script(monkeypatch, [(NO_FORM, ELSEWHERE, NEW)])
    assert (await outcome())[0] == "logged_in"
    assert visits == []


async def test_a_new_form_less_page_with_no_cookie_is_needs_user(monkeypatch):
    visits = script(monkeypatch, [(NO_FORM, LOCKED, NEW)])
    result = await outcome()
    assert result[0] == "needs_user"
    assert "no sign of a session" in result[1]
    assert visits == []


async def test_an_unchanged_cookie_jar_is_not_evidence(monkeypatch):
    script(monkeypatch, [(NO_FORM, HOME, NEW)], marks=SESSION)
    assert (await outcome(before_marks=SESSION))[0] == "needs_user"


async def test_a_locked_page_that_sets_a_cookie_is_not_logged_in(monkeypatch):
    """Re-review M4': a WRONG password, answered by a form-less "locked" page that
    sets a CSRF cookie (sites set one on nearly every response). The cookie is new,
    so it is not enough: the login page, revisited, still shows its form."""
    script(
        monkeypatch,
        [(NO_FORM, LOCKED, NEW)],
        marks=CSRF,
        confirm=("needs_user", "a cookie was set, but the login page still shows"),
    )
    result = await outcome()
    assert result[0] == "needs_user"


# ------------------------------------------------------------- the revisit


class RevisitTab:
    """The tab :func:`login_flow._confirm` navigates: answers ``Page.navigate``."""

    def __init__(self, error=""):
        self.error = error
        self.navigated = []

    async def send(self, command):
        self.navigated.append(next(command)["params"]["url"])
        command.close()
        return ("frame", "loader", self.error or None)


def revisit(monkeypatch, state, url):
    async def wait_for(_tab, _seconds, ready):
        assert ready(state, url) in (True, False)
        return state, url

    monkeypatch.setattr(login_flow, "_wait_for", wait_for)


async def test_the_revisit_that_is_sent_on_is_logged_in(monkeypatch):
    revisit(monkeypatch, NO_FORM, HOME)
    tab = RevisitTab()
    result = await login_flow._confirm(tab, HOSTS, LOGIN_URL)
    assert result[0] == "logged_in"
    assert tab.navigated == [LOGIN_URL]


async def test_the_revisit_that_shows_the_form_again_is_needs_user(monkeypatch):
    revisit(monkeypatch, {**NO_FORM, "username": True, "password": True}, LOGIN_URL)
    assert (await login_flow._confirm(RevisitTab(), HOSTS, LOGIN_URL))[0] == (
        "needs_user"
    )


async def test_the_revisit_that_stays_put_without_a_form_is_needs_user(monkeypatch):
    revisit(monkeypatch, NO_FORM, LOGIN_URL)
    assert (await login_flow._confirm(RevisitTab(), HOSTS, LOGIN_URL))[0] == (
        "needs_user"
    )


async def test_a_revisit_that_cannot_load_is_needs_user(monkeypatch):
    assert (
        await login_flow._confirm(RevisitTab(error="net::ERR_X"), HOSTS, LOGIN_URL)
    )[0] == "needs_user"


# ------------------------------------------------------------- rejected


async def test_the_form_back_in_a_new_document_is_a_rejection(monkeypatch):
    script(monkeypatch, [(FORM_FILLED, LOGIN_URL, NEW)])
    assert (await outcome())[0] == "rejected"


async def test_an_error_the_page_did_not_show_before_is_a_rejection(monkeypatch):
    """A single-page app: same document, field emptied, and an error on screen."""
    script(monkeypatch, [(FORM_EMPTY_WITH_ERROR, LOGIN_URL, OLD)])
    result = await outcome()
    assert result[0] == "rejected"
    assert "error" in result[1]


async def test_an_error_that_was_there_before_enter_is_no_evidence(monkeypatch):
    script(monkeypatch, [(FORM_EMPTY_WITH_ERROR, LOGIN_URL, OLD)])
    assert (await outcome(error=True))[0] == "needs_user"


async def test_an_emptied_field_alone_is_not_a_rejection(monkeypatch):
    """Re-review L7': a slow single-page app blanks the field on submit and signs
    in seconds later. No new document and no error: nothing is frozen."""
    script(monkeypatch, [(FORM_EMPTY, LOGIN_URL, OLD)])
    assert (await outcome())[0] == "needs_user"


async def test_a_slow_single_page_app_that_signs_in_after_blanking_is_logged_in(
    monkeypatch,
):
    script(
        monkeypatch,
        [(FORM_EMPTY, LOGIN_URL, OLD)] * 6 + [(NO_FORM, HOME, NEW)],
        marks=SESSION,
    )
    assert (await outcome())[0] == "logged_in"


async def test_a_form_seen_for_one_poll_is_not_a_rejection(monkeypatch):
    """A page reloads its form for a moment, then signs in."""
    script(
        monkeypatch,
        [(FORM_FILLED, LOGIN_URL, NEW), (NO_FORM, HOME, NEW)],
        marks=SESSION,
    )
    assert (await outcome())[0] == "logged_in"


async def test_the_form_must_stay_for_the_settle_time(monkeypatch):
    monkeypatch.setattr(login_flow, "_REJECT_SETTLE_S", 60.0)
    script(monkeypatch, [(FORM_FILLED, LOGIN_URL, NEW)])
    assert (await outcome())[0] == "needs_user"


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


# ------------------------------------------------------------- the rest


async def test_a_one_time_code_prompt_is_reported(monkeypatch):
    script(monkeypatch, [(OTP, HOME, NEW)])
    assert (await outcome())[0] == "needs_totp"


async def test_the_username_step_again_is_needs_user(monkeypatch):
    script(monkeypatch, [(USERNAME, LOGIN_URL, NEW)])
    assert (await outcome())[0] == "needs_user"
