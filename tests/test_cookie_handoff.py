"""F-898 — seeding a new session from a source whose browser is RUNNING.

Five questions, five classes, and they are separate because they fail for
different reasons.

**The decision** (``TestWhichRunningSourceMayBeSeededFrom``) — a running source
splits on one witness: do WE drive it. One of ours is seeded and recorded under
its own kind; anything else keeps F-897's refusal by name. The witness is asked
ONLY when the source is held, because an ordinary spawn must not pay for a
question it does not need.

**The translation** (``TestTheCookieParamACookieBecomes``) — and this one is
deliberately NOT driven through a double. ``FakeTab.send`` answers a canned
value straight, so a fixture built from our own objects would compare the
mapping to itself; every case here starts from Chrome 153's own measured WIRE
JSON through ``Cookie.from_json`` and asserts on ``CookieParam.to_json``, i.e.
on the bytes that would actually go back. That is
``fixtures-from-the-same-serializer-cannot-fail`` applied to the one place in
this finding where a field being wrong is silent.

**The PII rule** (``TestNoCookieNameOrValueEscapes``) — the rule the finding
proposes, enforced against the product on BOTH outcomes: no cookie name and no
cookie value in the tool's answer, in the debug ring, or in the message of the
error that is reported. A jar is credentials, and a name alone identifies.

**The wiring** (``TestWhatTheSpawnReports``) — that a live seed reaches the
hand-off at all, that the internal key carrying the source DIRECTORY never
reaches a caller, and that a failed hand-off still returns a working session.

**The snapshot** (``TestWhichProfilesThisBackendDrives``) — ``Driven``, which is
pure.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import FakeBrowser, FakeBrowserManager
from stealth_chrome_devtools_mcp.embedded import (
    clone_storage,
    cookie_handoff,
    profile_seed,
    profile_source,
)
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

# ---------------------------------------------------------------------------
# The measured jar
# ---------------------------------------------------------------------------

#: Chrome 153.0.8010.50's own `Storage.getCookies` rows, copied from the F-898
#: measurement (branch `test/F898-seed-storage-matrix`, commit 14351fd). These
#: are WIRE spellings — `httpOnly`, `sameParty`, `partitionKey` — and they are
#: the input to `Cookie.from_json`, never to a constructor of ours, so the pins
#: below measure the translation against Chrome's shape rather than against our
#: idea of it.
SESSION_COOKIE = {
    "name": "f898_session",
    "value": "s3cr3t-session-token",
    "domain": "127.0.0.1",
    "path": "/",
    "size": 32,
    "httpOnly": True,
    "secure": False,
    "session": True,
    "priority": "Medium",
    "sameParty": False,
    "sourceScheme": "NonSecure",
    "sourcePort": 8931,
    # A session cookie's expiry is reported as -1: a MARKER, not a date.
    "expires": -1,
    "sameSite": "Lax",
}

PERSISTENT_COOKIE = {
    "name": "f898_persistent",
    "value": "p3rs1stent-value",
    "domain": "127.0.0.1",
    "path": "/",
    "size": 30,
    "httpOnly": False,
    "secure": False,
    "session": False,
    "priority": "Medium",
    "sameParty": False,
    "sourceScheme": "NonSecure",
    "sourcePort": 8931,
    "expires": 4102444800.0,
    "sameSite": "Lax",
}

PARTITIONED_COOKIE = {
    "name": "f898_partitioned",
    "value": "chips-value",
    "domain": "[::1]",
    "path": "/",
    "size": 27,
    "httpOnly": True,
    "secure": True,
    "session": True,
    "priority": "Medium",
    "sameParty": False,
    "sourceScheme": "NonSecure",
    "sourcePort": 8932,
    "expires": -1,
    "sameSite": "None",
    # Measured port-LESS: two ports on one host are ONE site.
    "partitionKey": {
        "topLevelSite": "http://127.0.0.1",
        "hasCrossSiteAncestor": True,
    },
    "partitionKeyOpaque": False,
}

MEASURED_JAR = (SESSION_COOKIE, PERSISTENT_COOKIE, PARTITIONED_COOKIE)

#: Every secret in the fixtures above, which no product surface may repeat.
SECRETS = tuple(
    value for cookie in MEASURED_JAR for value in (cookie["name"], cookie["value"])
)


def cookies(*rows: dict) -> list:
    """The measured rows as nodriver would hand them to us."""
    import nodriver as uc

    return [uc.cdp.network.Cookie.from_json(row) for row in rows]


@pytest.fixture(autouse=True)
def empty_ring():
    """An empty debug ring per test, and one left behind — it is a process-wide
    singleton shared with the rest of the lane (``test_tool_failure_visibility``
    states the rule)."""

    def _reset() -> None:
        debug_logger.clear_debug_view()
        debug_logger._seen_errors.clear()

    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


class TestWhichRunningSourceMayBeSeededFrom:
    """``profile_source.seed_source``'s three answers for a named source."""

    @staticmethod
    def _roots(tmp_path: Path) -> profile_seed.Roots:
        for name in ("master", "master-snapshot", "sessions/work"):
            (tmp_path / name).mkdir(parents=True, exist_ok=True)
        return profile_seed.Roots(
            tmp_path,
            tmp_path / "sessions",
            tmp_path / "master",
            tmp_path / "master-snapshot",
        )

    def _ask(self, tmp_path: Path, *, held: bool, driven) -> profile_source.SeedSource:
        return profile_source.seed_source(
            "work",
            self._roots(tmp_path),
            lambda path, parent: str(path).startswith(str(parent)),
            held=lambda _p: held,
            driven=driven,
        )

    def test_a_closed_source_is_the_2_1_12_answer(self, tmp_path):
        """Nothing running: the ordinary file copy, under the ordinary word."""
        seed = self._ask(tmp_path, held=False, driven=lambda _p: True)
        assert seed.kind == "explicit-session"
        assert seed.path == tmp_path / "sessions" / "work"

    def test_b_a_running_source_we_drive_is_seeded_under_its_own_kind(self, tmp_path):
        """The finding's whole point. The kind is DIFFERENT from a closed
        source's, because what the copy contains is different — the file half
        lost whatever Chrome holds open, and the cookies arrive afterwards."""
        seed = self._ask(tmp_path, held=True, driven=lambda _p: True)
        assert seed.kind == profile_source.LIVE_SESSION_KIND
        assert seed.kind != "explicit-session"
        # For a NAMED source the copy source and the live source are one
        # directory. They are not for `default`, which is the whole of M1.
        assert seed.live == seed.path

    def test_c_a_running_source_we_do_not_drive_is_still_refused_by_name(
        self, tmp_path
    ):
        """F-897's refusal survives, and it now says WHICH half is missing: no
        connection of ours to ask for the cookies. A caller who reads only the
        first sentence must still learn the remedy."""
        with pytest.raises(ToolError) as excinfo:
            self._ask(tmp_path, held=True, driven=lambda _p: False)
        message = str(excinfo.value)
        assert "'work'" in message  # BY NAME
        assert "this backend does not drive" in message
        assert "no CDP connection of ours" in message
        assert "stealthy close" in message  # the remedy it always had
        assert "stealthy spawn --session work" in message  # and the new one

    def test_d_the_witness_is_not_asked_about_a_source_nobody_holds(self, tmp_path):
        """An ordinary spawn must not pay for F-898's question. The witness
        RAISES, so being consulted at all is the failure."""

        def _never(_profile):
            raise AssertionError("the hand-off witness was asked about a free source")

        assert self._ask(tmp_path, held=False, driven=_never).kind == "explicit-session"

    def test_e_an_omitted_witness_is_a_refusal_and_never_a_permission(self, tmp_path):
        """Fail-CLOSED. ``clone_storage``'s gate defaults ``driven`` to
        ``NOTHING_DRIVEN``, so every caller that does not hand one in gets
        2.1.12's answer — reading a browser's cookie jar is not something an
        omitted argument may authorise."""
        assert profile_source.NOTHING_DRIVEN(tmp_path) is False
        with pytest.raises(ToolError, match="does not drive"):
            self._ask(tmp_path, held=True, driven=profile_source.NOTHING_DRIVEN)


class TestWhereTheDefaultSessionIsSeededFrom:
    """``--from default``, and an unset ``--from``, which are one code path.

    The source that matters most, and the one the first draft never gave a
    hand-off to (review M1): ``default`` is the session a human logs in to,
    since F-888 its browser survives its backend, and while it runs the seed is
    never refreshed — so a copy taken from that seed is as old as the last time
    the window was closed, with a live CDP connection to the real jar open in
    this very process.

    Four cases, and the first two differ ONLY in whether we drive it.
    """

    @staticmethod
    def _roots(tmp_path: Path, *, seed: bool) -> profile_seed.Roots:
        (tmp_path / "master").mkdir(parents=True, exist_ok=True)
        (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
        if seed:
            (tmp_path / "master-snapshot").mkdir(parents=True, exist_ok=True)
        return profile_seed.Roots(
            tmp_path,
            tmp_path / "sessions",
            tmp_path / "master",
            tmp_path / "master-snapshot",
        )

    def _ask(self, tmp_path, requested, *, held, driven, seed=True):
        return profile_source.seed_source(
            requested,
            self._roots(tmp_path, seed=seed),
            lambda path, parent: str(path).startswith(str(parent)),
            held=lambda _p: held,
            driven=lambda _p: driven,
        )

    @pytest.mark.parametrize("requested", [None, "default"])
    def test_a_a_running_default_we_drive_copies_the_seed_and_hands_the_jar(
        self, tmp_path, requested
    ):
        """The M1 fix, and the claim is that the two paths are DIFFERENT.

        The copy still comes from the closed seed — F-893's argument is
        untouched — while the jar comes from the live shared profile. A
        ``live`` read off ``kind`` could not express this, which is why the
        first draft silently did nothing here.
        """
        seed = self._ask(tmp_path, requested, held=True, driven=True)
        assert seed.path == tmp_path / "master-snapshot"
        assert seed.live == tmp_path / "master"
        assert seed.live != seed.path

    @pytest.mark.parametrize("requested", [None, "default"])
    def test_b_a_running_default_we_do_not_drive_is_copied_and_never_refused(
        self, tmp_path, requested
    ):
        """The human's own Chrome. NOT the named branch's refusal: the closed
        copyable form exists and is exactly what F-897 promised, so the caller
        gets their session and ``seed_changed_since`` says the seed is behind.
        Refusing here would break ``--from default`` for the one person this
        product is for, on the machine state that is most normal."""
        seed = self._ask(tmp_path, requested, held=True, driven=False)
        assert seed.path == tmp_path / "master-snapshot"
        assert seed.live is None

    def test_c_a_closed_default_is_the_2_1_12_answer(self, tmp_path):
        """Nothing running: no hand-off to make, and nothing new said."""
        seed = self._ask(tmp_path, None, held=False, driven=True)
        assert seed.path == tmp_path / "master-snapshot"
        assert seed.live is None

    def test_d_no_seed_and_a_running_default_is_refused_by_name(self, tmp_path):
        """The silent zero-cookie path, closed.

        With no seed the fallback was a file copy of the LIVE shared directory,
        which this finding's own measurement says carries ZERO cookies — no
        ``seeded_via``, no ``cookie_handoff_error``, no refusal, indistinguishable
        from an ordinary successful spawn. The remedy has to be ONE action and
        the message has to name it.
        """
        with pytest.raises(ToolError) as excinfo:
            self._ask(tmp_path, None, held=True, driven=True, seed=False)
        message = str(excinfo.value)
        assert "'default'" in message  # BY NAME
        assert "no copyable form yet" in message
        assert "carries no cookies at all" in message
        assert "stealthy close" in message  # the one action

    def test_e_no_seed_and_nothing_running_is_still_the_first_run_path(self, tmp_path):
        """A fresh install with the shared profile CLOSED still copies the live
        directory, because a directory at rest copies fine — and that path is
        what writes the first seed. The refusal above is about the RUNNING half
        only, or a first named session would be impossible."""
        seed = self._ask(tmp_path, None, held=False, driven=True, seed=False)
        assert seed.path == tmp_path / "master"
        assert seed.kind == "explicit-default"
        assert seed.live is None


class TestTheResolverStampsTheLiveSourceAndNotTheCopySource:
    """The M1 claim one layer up, through the REAL resolver and a real copy.

    ``TestWhereTheDefaultSessionIsSeededFrom`` pins the decision; this pins that
    ``clone_storage`` acts on ``SeedSource.live`` rather than re-deriving it
    from ``kind`` — the two disagree for exactly this case, and reading ``kind``
    is what made ``--from default`` a no-op.
    """

    async def test_the_copy_comes_from_the_seed_and_the_jar_from_the_shared_dir(
        self, tmp_session_root, monkeypatch
    ):
        monkeypatch.setattr(
            clone_storage, "_profile_has_running_browser", lambda path: True
        )
        selection = await clone_storage.resolve_profile_selection(
            "work2", seed_from="default", driven=lambda _p: True
        )

        master = tmp_session_root["master"]
        snapshot = tmp_session_root["snapshot"]
        assert selection[clone_storage.LIVE_SEED_KEY] == str(master)
        # The COPY came from the seed, which is a different directory.
        marker = profile_seed.read_marker(Path(selection["user_data_dir"]))
        assert marker["source"] == str(snapshot)
        assert str(master) != str(snapshot)
        # And the instruction still never reaches the caller.
        public = clone_storage._public_profile_selection(selection)
        assert clone_storage.LIVE_SEED_KEY not in public

    async def test_a_default_we_do_not_drive_stamps_nothing(
        self, tmp_session_root, monkeypatch
    ):
        monkeypatch.setattr(
            clone_storage, "_profile_has_running_browser", lambda path: True
        )
        selection = await clone_storage.resolve_profile_selection(
            "work3", seed_from="default", driven=profile_source.NOTHING_DRIVEN
        )
        assert clone_storage.LIVE_SEED_KEY not in selection


class TestTheWholeJarPremise:
    """ "The whole jar" is a claim about the PRODUCT, so it is pinned here.

    ``Storage.getCookies`` with no ``browserContextId`` answers the DEFAULT
    browser context's cookies — which is every cookie in the browser only
    because this product never creates another context, and never launches one
    incognito. That is an absence, and an absence is exactly the kind of premise
    this repo pins rather than believes (review S6; ``cdp_transport``'s AST scan
    of nodriver's 810 ``from_json`` parsers and ``profile_seed``'s grep for a
    second spelling of ``LOGIN_WITNESSES`` are the precedents).

    Scanned as TOKENS and not as text, so prose explaining the premise — this
    docstring included — cannot trip it, and so a future ``--incognito`` feature
    fails here with the reason written down rather than quietly making
    ``cookie_handoff``'s docstring false.
    """

    #: Creating a second context needs one of these NAMES; there is no way to
    #: reach `Target.createBrowserContext` or to pass a context id without one.
    _CONTEXT_NAMES = frozenset(
        {"create_browser_context", "createBrowserContext", "browser_context_id"}
    )
    #: A launch arg is passed as its own literal, so an exact match cannot be
    #: tripped by a sentence that merely mentions the flag.
    _OFF_THE_RECORD = "--incognito"
    #: Anti-vacuous-pass floor, `source_scan.MIN_TOOL_SOURCE_FILES`' discipline:
    #: a scan that finds no files passes over nothing at all.
    _MIN_FILES = 60

    @staticmethod
    def _package_files() -> list[Path]:
        root = Path(cookie_handoff.__file__).resolve().parent.parent
        return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)

    def test_no_module_creates_a_second_browser_context(self):
        import tokenize

        files = self._package_files()
        assert len(files) >= self._MIN_FILES, (
            f"only {len(files)} package files scanned — the set collapsed, so "
            "this guard would pass over nothing"
        )
        offences: list[str] = []
        for path in files:
            with tokenize.open(path) as handle:
                for token in tokenize.generate_tokens(handle.readline):
                    name = token.type == tokenize.NAME and token.string
                    literal = token.type == tokenize.STRING and token.string
                    if name and token.string in self._CONTEXT_NAMES:
                        offences.append(f"{path.name}:{token.start[0]} {token.string}")
                    elif literal and literal.strip("\"'") == self._OFF_THE_RECORD:
                        offences.append(f"{path.name}:{token.start[0]} incognito")
        assert not offences, (
            "cookie_handoff documents the hand-off as carrying the WHOLE jar, "
            "which holds only while every cookie lives in the default browser "
            f"context. These break that premise: {offences}"
        )


# ---------------------------------------------------------------------------
# The translation
# ---------------------------------------------------------------------------


class TestTheCookieParamACookieBecomes:
    """What goes back on the wire, measured from what came off it."""

    @staticmethod
    def _sent(row: dict) -> dict:
        (cookie,) = cookies(row)
        return cookie_handoff.as_param(cookie).to_json()

    def test_a_a_session_cookies_minus_one_expiry_is_omitted(self):
        """``-1`` is a marker and not a date; forwarding it is a claim about
        1969, and OMITTING it is what makes the target treat the cookie as a
        session cookie too. This is also the one shape a file copy can never
        carry, because a session cookie is never written to the jar on disk."""
        sent = self._sent(SESSION_COOKIE)
        assert "expires" not in sent

    def test_b_a_real_expiry_round_trips(self):
        sent = self._sent(PERSISTENT_COOKIE)
        assert sent["expires"] == PERSISTENT_COOKIE["expires"]

    def test_c_the_partition_key_is_carried_with_both_sub_fields(self):
        """CHIPS. Both halves or neither — a partition key missing its
        ``hasCrossSiteAncestor`` describes a different cookie."""
        assert self._sent(PARTITIONED_COOKIE)["partitionKey"] == {
            "topLevelSite": "http://127.0.0.1",
            "hasCrossSiteAncestor": True,
        }

    def test_d_same_party_is_never_written_back(self):
        """F-902's fix read from the other side. Chrome 153 does not SEND
        ``sameParty``; ``cdp_transport._RETIRED_COOKIE_FIELDS`` synthesises it
        so the reply can be parsed at all. Forwarding that invention would write
        a value Chrome never gave us straight back into a browser."""
        assert "sameParty" in SESSION_COOKIE  # the measurement's own input
        for row in MEASURED_JAR:
            assert "sameParty" not in self._sent(row)

    def test_e_the_read_only_report_fields_are_dropped(self):
        """``size``, ``session`` and ``partitionKeyOpaque`` have no
        ``CookieParam`` counterpart; ``url`` is ``CookieParam``'s alone and
        inventing one would change which host a cookie belongs to."""
        sent = self._sent(PARTITIONED_COOKIE)
        for absent in ("size", "session", "partitionKeyOpaque", "url"):
            assert absent not in sent

    def test_f_every_other_field_is_a_verbatim_pass_through(self):
        sent = self._sent(PARTITIONED_COOKIE)
        for field in (
            "name",
            "value",
            "domain",
            "path",
            "secure",
            "httpOnly",
            "sameSite",
            "priority",
            "sourceScheme",
            "sourcePort",
        ):
            assert sent[field] == PARTITIONED_COOKIE[field], field

    def test_g_the_carried_set_is_the_measured_one(self):
        """The set is DERIVED from nodriver's two dataclasses, so a CDP bump
        that adds or retires a shared field moves it. Pinned against the eleven
        measured on Chrome 153 so that movement is a decision and not a
        silence."""
        assert set(cookie_handoff.CARRIED_FIELDS) == {
            "name",
            "value",
            "domain",
            "path",
            "secure",
            "http_only",
            "same_site",
            "priority",
            "source_scheme",
            "source_port",
            "partition_key",
        }


# ---------------------------------------------------------------------------
# The PII rule
# ---------------------------------------------------------------------------


def _no_secret_in(blob: object) -> None:
    text = json.dumps(blob, default=str)
    for secret in SECRETS:
        assert secret not in text, f"a cookie name or value escaped: {secret!r}"


class TestNoCookieNameOrValueEscapes:
    """The finding's own rule, enforced against the product.

    Counts, the CDP method and an exception TYPE — nothing else. A cookie NAME
    identifies on its own (``NID`` names Google; a jar is a list of the sites
    its human uses) and a VALUE is the session itself.
    """

    def test_a_the_handoff_record_is_counts_only(self):
        record = cookie_handoff.Handoff(
            read=3, sent=3, jar_after=4, session_cookies=2, partitioned=1
        ).record()
        _no_secret_in(record)
        assert record["seeded_via"] == cookie_handoff.VIA_CDP
        assert record["cookies_carried"] == 3
        assert all(isinstance(v, (int, str)) for v in record.values())

    def test_b_a_failure_names_the_method_and_the_type_and_nothing_else(self):
        """The text of a ``Storage.setCookies`` failure is Chrome's answer to a
        command whose parameters WERE the jar, so it is never repeated — even
        when the exception carries one."""
        leaky = RuntimeError(f"rejected cookie {SESSION_COOKIE['name']}=xyz")
        reason = cookie_handoff.failure(
            cookie_handoff._failed(cookie_handoff.WRITE_METHOD, leaky)
        )
        assert reason == "RuntimeError from Storage.setCookies"
        _no_secret_in(reason)

    def test_b2_an_exception_from_outside_the_handoff_reports_its_type_alone(self):
        """The OTHER branch, and the one that protects against everything this
        module did not write. A failure from ``driven_profiles``, from
        ``get_browser`` or from the CDP budget is not a ``HandoffError``, so its
        text is not ours to trust — it is reported by TYPE and nothing more.

        This node exists because a mutation that made the branch
        ``return str(error)`` passed every other pin in this file: the two
        failure paths above both convert to ``HandoffError`` first, so the
        fallback was never exercised.
        """
        leaky = RuntimeError(
            f"cookie {SESSION_COOKIE['name']}={SESSION_COOKIE['value']}"
        )
        reason = cookie_handoff.failure(leaky)
        assert reason == "RuntimeError"
        _no_secret_in(reason)

    async def test_b3_a_leak_from_outside_the_handoff_never_reaches_the_answer(
        self, call_tool, patched_server, monkeypatch
    ):
        """The same branch, driven through the product: a browser lookup that
        fails with a message carrying a cookie."""
        leaky = RuntimeError(f"no browser for {SESSION_COOKIE['value']}")
        result = await _spawn_with_live_seed(
            call_tool,
            patched_server,
            monkeypatch,
            jar=cookies(*MEASURED_JAR),
            browser_error=leaky,
        )
        _no_secret_in(result)
        _no_secret_in(debug_logger.get_debug_view())
        selection = result["spawn_diagnostics"]["profile_selection"]
        assert selection["cookie_handoff_error"] == "RuntimeError"

    async def test_c_a_successful_handoff_leaves_nothing_in_the_answer_or_the_ring(
        self, call_tool, patched_server, monkeypatch
    ):
        result = await _spawn_with_live_seed(
            call_tool, patched_server, monkeypatch, jar=cookies(*MEASURED_JAR)
        )
        _no_secret_in(result)
        _no_secret_in(debug_logger.get_debug_view())

    async def test_d_a_failed_handoff_leaves_nothing_either(
        self, call_tool, patched_server, monkeypatch
    ):
        """The failure path is the one that carries an exception, so it is the
        one a leak would come through."""
        boom = RuntimeError(f"Invalid cookie fields: {SESSION_COOKIE['value']}")
        result = await _spawn_with_live_seed(
            call_tool, patched_server, monkeypatch, jar=boom
        )
        _no_secret_in(result)
        _no_secret_in(debug_logger.get_debug_view())
        selection = result["spawn_diagnostics"]["profile_selection"]
        assert (
            selection["cookie_handoff_error"] == "RuntimeError from Storage.getCookies"
        )


# ---------------------------------------------------------------------------
# The wiring
# ---------------------------------------------------------------------------

SOURCE_DIR = "/sessions/logged-in"
TARGET_DIR = "/sessions/work2"


async def _spawn_with_live_seed(
    call_tool,
    patched_server,
    monkeypatch,
    *,
    jar,
    live: bool = True,
    browser_error=None,
    source_driven: bool = True,
    source_browser: bool = True,
    source_connection: bool = True,
    timeout: bool = False,
):
    """Drive ``spawn_browser`` for a session seeded from a LIVE source.

    The resolver is doubled because what it decides is pinned elsewhere
    (``test_seed_from_session``); what is under test here is everything AFTER
    it. ``jar`` is either the cookies the source answers with, or an exception
    the read raises.

    The four flags after it each disable ONE thing the hand-off site needs, so
    each reaches a different reported reason: ``source_driven`` (the snapshot no
    longer names the source — the observable end of re-deriving it at the moment
    of use), ``source_browser`` (the manager has no handle), ``source_connection``
    (a ``Browser`` that never finished starting) and ``timeout`` (the CDP budget
    expired). Review S1/S2/S3: all four were unreachable from the pins.
    """
    source = FakeBrowser()
    target = FakeBrowser()
    source.connection._cdp_responses = {
        "get_cookies": _raise_or(jar),
        "set_cookies": None,
    }
    target.connection._cdp_responses = {"get_cookies": [], "set_cookies": None}
    if not source_connection:
        source.connection = None

    fbm = FakeBrowserManager(
        instances=[
            SimpleNamespace(instance_id="source-1"),
            SimpleNamespace(instance_id="target-1"),
        ],
        profiles=(
            {"source-1": SOURCE_DIR, "target-1": TARGET_DIR}
            if source_driven
            else {"target-1": TARGET_DIR}
        ),
        browsers=(
            {"source-1": source, "target-1": target}
            if source_browser
            else {"target-1": target}
        ),
        spawn_instance=SimpleNamespace(
            instance_id="target-1",
            state="active",
            headless=True,
            viewport={"width": 800, "height": 600},
        ),
        spawn_diagnostics={},
        tabs={"target-1": target.connection},
    )

    async def fake_resolve(user_data_dir, **kwargs):
        selection = {
            "user_data_dir": TARGET_DIR,
            "profile_role": "explicit",
            "clone_source": None,
        }
        if live:
            selection[clone_storage.LIVE_SEED_KEY] = SOURCE_DIR
        return selection

    if browser_error is not None:

        async def _refuse(_instance_id, *_a, **_k):
            raise browser_error

        fbm.get_browser = _refuse

    monkeypatch.setattr(clone_storage, "resolve_profile_selection", fake_resolve)
    monkeypatch.setattr(
        clone_storage, "require_allowed_seed_from", lambda *a, **k: "work"
    )
    monkeypatch.setattr(
        clone_storage, "require_allowed_user_data_dir", lambda *a, **k: TARGET_DIR
    )
    singletons = {"browser_manager": fbm}
    if timeout:

        async def _expire(operation, **_kwargs):
            operation.close()  # never awaited, so the coroutine must be closed
            raise ToolError("CDP operation timed out after 30s")

        singletons["_with_cdp_timeout"] = _expire
    srv = patched_server(**singletons)
    return await call_tool(
        srv,
        "spawn_browser",
        headless=True,
        session="work2",
        seed_from="work",
        sandbox=False,
    )


def _raise_or(jar):
    if isinstance(jar, BaseException):

        def _boom(_name):
            raise jar

        return _boom
    return jar


class TestWhatTheSpawnReports:
    async def test_a_a_live_seed_reports_the_mechanism_and_the_counts(
        self, call_tool, patched_server, monkeypatch
    ):
        result = await _spawn_with_live_seed(
            call_tool, patched_server, monkeypatch, jar=cookies(*MEASURED_JAR)
        )
        selection = result["spawn_diagnostics"]["profile_selection"]
        assert selection["seeded_via"] == cookie_handoff.VIA_CDP
        assert selection["cookies_carried"] == 3
        assert selection["cookies_session"] == 2  # the two with a -1 expiry
        assert selection["cookies_partitioned"] == 1
        assert "cookie_handoff_error" not in selection

    async def test_b_the_source_directory_never_reaches_the_caller(
        self, call_tool, patched_server, monkeypatch
    ):
        """``LIVE_SEED_KEY`` is an instruction to this process: it carries a
        path, and a path names the operating user. ``_public_profile_selection``
        is the line between what the resolver decided and what is reported, so
        the key is dropped THERE rather than hoped over."""
        result = await _spawn_with_live_seed(
            call_tool, patched_server, monkeypatch, jar=cookies(*MEASURED_JAR)
        )
        selection = result["spawn_diagnostics"]["profile_selection"]
        assert clone_storage.LIVE_SEED_KEY not in selection
        assert SOURCE_DIR not in json.dumps(result, default=str)

    async def test_c_a_failed_handoff_still_returns_a_working_session(
        self, call_tool, patched_server, monkeypatch
    ):
        """The session EXISTS and its browser is running; what failed is an
        augmentation. Raising would take a working session away AND leave a
        directory a retry then refuses as "already exists"."""
        result = await _spawn_with_live_seed(
            call_tool, patched_server, monkeypatch, jar=RuntimeError("nope")
        )
        assert result["instance_id"] == "target-1"
        selection = result["spawn_diagnostics"]["profile_selection"]
        assert selection["seeded_via"] == cookie_handoff.VIA_COPY
        assert "cookies_carried" not in selection

    async def test_d_a_failed_handoff_is_recorded_as_a_warning(
        self, call_tool, patched_server, monkeypatch
    ):
        """Reported, not silent — and the traceback is deliberately withheld
        (no ``error=exc``), because the exception behind a cookie command is
        Chrome's answer to a command whose parameters were the jar."""
        await _spawn_with_live_seed(
            call_tool, patched_server, monkeypatch, jar=RuntimeError("nope")
        )
        view = debug_logger.get_debug_view()
        assert view["summary"]["total_warnings"] == 1
        entry = view["all_warnings"][-1]
        assert entry["method"] == "_seed_cookies_over_cdp"
        assert "RuntimeError from Storage.getCookies" in entry["message"]

    async def test_e_an_ordinary_spawn_says_nothing_about_a_hand_off(
        self, call_tool, patched_server, monkeypatch
    ):
        """No live source, no cookie fields at all — ``seeded_from`` already
        says everything there is about where an ordinary session came from."""
        result = await _spawn_with_live_seed(
            call_tool, patched_server, monkeypatch, jar=cookies(), live=False
        )
        selection = result["spawn_diagnostics"]["profile_selection"]
        assert "seeded_via" not in selection
        assert "cookie_handoff_error" not in selection


class TestEveryReasonAHandOffCanReport:
    """The four failures that are REPORTED rather than raised, each named.

    Every one of these was unreachable from the pins (review S1/S2/S3), and one
    of them — the source no longer being driven — is the observable end of the
    single design decision §11 flags for a reviewer: the snapshot is re-derived
    at the moment of use precisely so a source that went away becomes a reported
    degradation. The report was the untested half.

    Each node asserts the SENTENCE, because the value of these is entirely in
    what an operator is told; `NOT carried (ToolError)` is a correct field and a
    useless one.
    """

    @staticmethod
    def _reason(result: dict) -> str:
        selection = result["spawn_diagnostics"]["profile_selection"]
        assert selection["seeded_via"] == cookie_handoff.VIA_COPY
        assert result["instance_id"]  # the session still exists, every time
        return selection["cookie_handoff_error"]

    async def test_a_a_source_that_stopped_being_ours_says_so(
        self, call_tool, patched_server, monkeypatch
    ):
        """The TOCTOU end: the pre-flight said yes, a whole browser launch
        happened, and by the time the jar is wanted the source is not ours."""
        result = await _spawn_with_live_seed(
            call_tool, patched_server, monkeypatch, jar=cookies(), source_driven=False
        )
        assert self._reason(result) == (
            "the source browser is no longer driven by this backend"
        )

    async def test_b_an_unresolvable_browser_says_so(
        self, call_tool, patched_server, monkeypatch
    ):
        result = await _spawn_with_live_seed(
            call_tool, patched_server, monkeypatch, jar=cookies(), source_browser=False
        )
        assert self._reason(result) == (
            "a browser for the hand-off could not be resolved"
        )

    async def test_c_a_browser_with_no_connection_keeps_its_own_words(
        self, call_tool, patched_server, monkeypatch
    ):
        """Review S2. ``failure`` repeats a ``HandoffError``'s text and nothing
        else, so this message survives only because ``_connection`` raises that
        class — as a bare ``RuntimeError`` it was reported as ``RuntimeError
        from Storage.getCookies``, which tells an operator a CDP call failed
        when the truth is there was no connection to make one on."""
        result = await _spawn_with_live_seed(
            call_tool,
            patched_server,
            monkeypatch,
            jar=cookies(),
            source_connection=False,
        )
        assert self._reason(result) == "browser has no CDP connection"

    async def test_d_a_timeout_names_the_budget_and_not_its_exception_class(
        self, call_tool, patched_server, monkeypatch
    ):
        """Review S3. A wedged source browser is the likeliest real failure
        there is, and ``_with_cdp_timeout`` raises ``ToolError`` — which
        ``failure`` reports by TYPE, so the operator read ``NOT carried
        (ToolError)``: neither the mechanism nor the half that failed."""
        result = await _spawn_with_live_seed(
            call_tool, patched_server, monkeypatch, jar=cookies(), timeout=True
        )
        reason = self._reason(result)
        assert reason == "the hand-off did not finish inside the CDP timeout"
        assert "ToolError" not in reason

    async def test_e_no_reported_reason_ever_carries_a_cookie(
        self, call_tool, patched_server, monkeypatch
    ):
        """The PII rule over the four new paths at once — a reason is composed
        text and composed text is where a name leaks back in."""
        for flags in (
            {"source_driven": False},
            {"source_browser": False},
            {"source_connection": False},
            {"timeout": True},
        ):
            result = await _spawn_with_live_seed(
                call_tool,
                patched_server,
                monkeypatch,
                jar=cookies(SESSION_COOKIE),
                **flags,
            )
            _no_secret_in(result)


# ---------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------


class TestWhatTheCliPrints:
    """``stealthy spawn``'s cookie line — counts only, and only when there was
    a hand-off to report."""

    @staticmethod
    def _lines(selection: dict) -> list[str]:
        from stealth_chrome_devtools_mcp import cli_render

        return cli_render.spawn_lines(
            {
                "instance_id": "i1",
                "spawn_diagnostics": {"profile_selection": selection},
            }
        )

    def test_a_a_successful_hand_off_prints_the_count(self):
        lines = self._lines(
            {
                "seeded_via": "cdp-cookies",
                "cookies_carried": 7,
                "profile_role": "explicit",
            }
        )
        assert any("cookies    : 7 handed over" in line for line in lines)

    def test_b_a_failure_says_the_session_still_works(self):
        """The commonest reaction to a red word is to throw the session away
        and make another — which here loses nothing and costs a Chrome."""
        lines = self._lines(
            {"seeded_via": "copy", "cookie_handoff_error": "RuntimeError"}
        )
        line = next(line for line in lines if line.startswith("cookies"))
        assert "NOT carried (RuntimeError)" in line
        assert "works" in line

    def test_c_an_ordinary_spawn_prints_no_cookie_line(self):
        lines = self._lines({"profile_role": "clone"})
        assert not any(line.startswith("cookies") for line in lines)


class TestWhichProfilesThisBackendDrives:
    def test_a_a_directory_we_drive_names_its_instance(self, tmp_path):
        driven = cookie_handoff.Driven([(tmp_path / "work", "i-1")])
        assert driven.instance(tmp_path / "work") == "i-1"
        assert driven.holds(tmp_path / "work") is True

    def test_b_a_directory_we_do_not_drive_is_not_ours(self, tmp_path):
        driven = cookie_handoff.Driven([(tmp_path / "work", "i-1")])
        assert driven.instance(tmp_path / "other") is None
        assert driven.holds(tmp_path / "other") is False

    def test_c_nothing_driven_holds_nothing(self, tmp_path):
        assert cookie_handoff.NOTHING_DRIVEN.holds(tmp_path) is False

    async def test_d_an_instance_with_no_recorded_directory_is_skipped(self):
        """Not a candidate rather than a guess: an instance that cannot say
        which directory it drives cannot be matched against one."""
        fbm = FakeBrowserManager(
            instances=[
                SimpleNamespace(instance_id="i-1"),
                SimpleNamespace(instance_id="i-2"),
            ],
            profiles={"i-2": SOURCE_DIR},
        )
        driven = await cookie_handoff.driven_profiles(fbm)
        assert driven.instance(Path(SOURCE_DIR)) == "i-2"

    def test_e_the_instance_model_is_not_where_the_directory_lives(self):
        """Pinned as an ABSENCE, because this is the trap the first draft fell
        into and it fails SILENTLY.

        ``driven_profiles`` reached for ``instance.user_data_dir``. There is no
        such field on ``BrowserInstance`` — it is ``BrowserOptions``', kept
        under the entry's ``"options"`` — so ``getattr`` answered None for every
        real instance and the hand-off would never have fired on a live
        backend. Every hermetic pin still PASSED, because the double offered the
        attribute the product does not have (``mocked-fakes-can-encode-the-bug``
        reached through the double's own convenience). ``ty`` is what caught it;
        this node is what keeps it caught.
        """
        from stealth_chrome_devtools_mcp.embedded.models import (
            BrowserInstance,
            BrowserOptions,
        )

        assert "user_data_dir" not in BrowserInstance.model_fields
        assert "user_data_dir" in BrowserOptions.model_fields
