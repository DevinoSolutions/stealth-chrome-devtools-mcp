"""F-950 — one cookie Chrome refuses must not cost the clone the whole jar.

``Storage.setCookies`` is all-or-nothing: Chrome answers ``Invalid cookie
fields`` for the batch if ANY entry is unacceptable, and stores none of them.
Measured on Chrome 153 with a real hand-off: a ``Secure`` cookie that Chrome
itself had stored from a loopback ``http`` page (so its ``sourceScheme`` is
``NonSecure``) is a cookie Chrome will not accept back. Before this fix the
hand-off failed whole, the clone fell back to the stale file copy, and every
login made after the seed was last refreshed was gone with it.

The double here is a wire, not a canned value: ``send`` drives the real
generated command, so the params that reach "Chrome" are the bytes the product
built, and the batch is judged the way Chrome judges it (all or nothing).
"""

import json
from types import SimpleNamespace

import pytest

from stealth_chrome_devtools_mcp.embedded import cookie_handoff

GOOD_A = {
    "name": "f950_a",
    "value": "value-of-a",
    "domain": "example.test",
    "path": "/",
    "size": 10,
    "httpOnly": True,
    "secure": True,
    "session": True,
    "priority": "Medium",
    "sameParty": False,
    "sourceScheme": "Secure",
    "sourcePort": 443,
    "expires": -1,
    "sameSite": "Lax",
}
GOOD_B = {**GOOD_A, "name": "f950_b", "value": "value-of-b"}
REFUSED = {
    **GOOD_A,
    "name": "f950_refused",
    "value": "value-of-refused",
    "sourceScheme": "NonSecure",
    "sourcePort": 80,
}

SECRETS = tuple(
    value for row in (GOOD_A, GOOD_B, REFUSED) for value in (row["name"], row["value"])
)


class ChromeRefusedError(Exception):
    """Stands in for nodriver's ``ProtocolException``."""


class Wire:
    """A browser-level connection that answers like Chrome does."""

    def __init__(self, jar=(), refuse=()):
        self.jar = list(jar)
        self.refuse = set(refuse)
        self.set_calls: list[int] = []

    async def send(self, command):
        frame = next(command)
        if frame["method"] == "Storage.getCookies":
            reply = {"cookies": self.jar}
        else:
            sent = frame["params"]["cookies"]
            self.set_calls.append(len(sent))
            if any(cookie["name"] in self.refuse for cookie in sent):
                raise ChromeRefusedError("Invalid cookie fields")
            names = {row["name"] for row in self.jar}
            self.jar += [
                row
                for row in ALL_ROWS
                if row["name"] in {c["name"] for c in sent} and row["name"] not in names
            ]
            reply = {}
        try:
            command.send(reply)
        except StopIteration as done:
            return done.value
        raise AssertionError("the command did not finish")  # pragma: no cover


ALL_ROWS = (GOOD_A, GOOD_B, REFUSED)


def browser(wire):
    return SimpleNamespace(connection=wire)


def _no_secret_in(blob):
    text = json.dumps(blob, default=str)
    for secret in SECRETS:
        assert secret not in text, f"a cookie name or value escaped: {secret!r}"


async def _hand_off(rows, refuse):
    source, target = Wire(jar=rows), Wire(refuse=refuse)
    result = await cookie_handoff.hand_off(browser(source), browser(target))
    return result, target


async def test_a_clean_jar_is_one_write_and_reports_nothing_refused():
    result, target = await _hand_off([GOOD_A, GOOD_B], refuse=())

    assert target.set_calls == [2]
    assert result.rejected == 0
    assert result.record()["cookies_rejected"] == 0


async def test_one_refused_cookie_does_not_cost_the_rest_of_the_jar():
    """The defect: this raised ``HandoffError`` and the clone got the stale
    seed. The two acceptable cookies must land and the refusal be COUNTED."""
    result, target = await _hand_off(
        [GOOD_A, REFUSED, GOOD_B], refuse={REFUSED["name"]}
    )

    assert {row["name"] for row in target.jar} == {GOOD_A["name"], GOOD_B["name"]}
    record = result.record()
    assert record["seeded_via"] == cookie_handoff.VIA_CDP
    assert record["cookies_read"] == 3
    assert record["cookies_carried"] == 2
    assert record["cookies_rejected"] == 1


async def test_the_refusal_is_found_in_a_logarithmic_number_of_writes():
    rows = [{**GOOD_A, "name": f"f950_{n}"} for n in range(64)] + [REFUSED]
    result, target = await _hand_off(rows, refuse={REFUSED["name"]})

    assert result.rejected == 1
    assert len(target.set_calls) < 30


async def test_what_is_reported_names_no_cookie_and_carries_no_value():
    result, _ = await _hand_off([GOOD_A, REFUSED], refuse={REFUSED["name"]})

    _no_secret_in(result.record())


async def test_a_jar_where_nothing_is_accepted_still_fails_as_one_handoff():
    """Every cookie refused is not a cookie problem — the clone would carry
    nothing and say it was seeded. It keeps failing as before, by type alone."""
    with pytest.raises(cookie_handoff.HandoffError) as excinfo:
        await _hand_off([GOOD_A, GOOD_B], refuse={GOOD_A["name"], GOOD_B["name"]})

    assert str(excinfo.value) == "ChromeRefusedError from Storage.setCookies"
    _no_secret_in(str(excinfo.value))


async def test_a_dead_connection_gives_up_after_a_bounded_number_of_writes():
    rows = [{**GOOD_A, "name": f"f950_{n}"} for n in range(500)]
    refuse = {row["name"] for row in rows}
    source, target = Wire(jar=rows), Wire(refuse=refuse)

    with pytest.raises(cookie_handoff.HandoffError):
        await cookie_handoff.hand_off(browser(source), browser(target))

    assert len(target.set_calls) <= 3 * cookie_handoff.MAX_REFUSED


async def test_the_debug_port_path_tolerates_a_refusal_too(monkeypatch):
    async def read(_port):
        return [GOOD_A, REFUSED, GOOD_B]

    monkeypatch.setattr(cookie_handoff, "_read_raw_jar_over_port", read)
    target = Wire(refuse={REFUSED["name"]})

    result = await cookie_handoff.hand_off_from_port(9222, browser(target))

    assert result.rejected == 1
    assert result.sent == 2
