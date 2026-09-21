"""Real-Chrome proof for F-898: a RUNNING session's cookies reach a new one.

The hermetic pins in ``test_cookie_handoff.py`` prove the decision, the
translation and the PII rule against doubles. They cannot prove the two facts
the finding is actually about, because a double answers whatever it was built
to answer:

1. that ``Storage.getCookies`` → ``Storage.setCookies`` across two real Chromes
   puts a cookie into the target's jar **the target then sends to a server**;
2. that the source browser is still running, and still ours, afterwards.

So these nodes use no double at all for the mechanism under test. Two real
headless Chromes, two real profiles under the node's own tmp session root, and
one real HTTP origin — and the oracle is deliberately **not** a cookie API of
ours. ``get_cookies`` reading back what ``set_cookies`` was handed would prove
only that CDP echoes; what a login needs is that the SERVER sees the cookie, so
the page POSTs to ``/api/echo`` and the assertion is made against the ``Cookie``
header the fixture server reflects back. That is the only reading that cannot be
satisfied by a jar entry Chrome declines to send.

The control is the same spawn from a CLOSED source, and it earns its place by
being the case the file copy already handled: it must NOT report a hand-off, and
it must NOT have the SESSION cookie — which is never written to disk, so it is
exactly what the copy provably cannot carry and the hand-off provably can. Two
nodes that differ in one thing (is the source open) and disagree about one
cookie is the whole finding, stated as an experiment.

Both nodes name their sessions with ``session=`` and their sources with
``seed_from=``, which is the vocabulary F-896/F-897 gave a caller — a path is
refused for both, so this is also the real-Chrome witness for those two words.
The names carry a per-run suffix because this tier's session root is SHARED
(see ``tests/conftest.py``): a NAME another process already holds is walked to
``<name>-2`` by the product, and a ``seed_from`` naming the session we asked for
rather than the one we got would then seed from a stranger's profile.
"""

import contextlib
import json
import uuid

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    released,
    sandbox_kwargs,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded import (
    clone_storage,
    cookie_handoff,
    tool_runtime,
)
from stealth_chrome_devtools_mcp.settings import get_settings

pytestmark = integration_pytestmark()


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


@pytest.fixture(autouse=True)
def _isolated_root(tmp_empty_root):
    """Make ``tmp_empty_root`` actually WIN, which by default it does not.

    ``get_settings()`` is ``@lru_cache``d and conftest clears it at each test's
    SETUP, so the first reader after that wins the whole test — and in this tier
    the first reader is the autouse ``_warmup`` spawn, which runs BEFORE
    ``tmp_empty_root`` patches the env (conftest says so, and names the six
    108 MB profiles that fact once wrote into a real root). Every session here
    is named rather than given as a path, so the root is not incidental to these
    nodes the way it is to the ``user_data_dir`` files: it decides where
    ``f898-src`` IS. Clearing the cache once the env patch is in place is the
    whole fixture, and it is autouse so a node added later cannot forget it.

    It is ordered after ``_warmup`` because ``_warmup`` is declared first, which
    is what we want: the warmup browser belongs in the shared root it has always
    used, and only the nodes' own spawns move.
    """
    get_settings.cache_clear()
    return tmp_empty_root


@pytest.fixture(autouse=True)
def _record_in_tmp(tmp_path, monkeypatch):
    """THE one redirection of the browser record, for every node in this file.

    Each node spawns through the REAL ``rt.process_cleanup``, which records an
    entry for every browser it launches, and node 1 deliberately leaves one
    running while the next spawn happens. Writing that into the developer's live
    ``~/.stealth-mcp/browser_pids.json`` is not a tidiness point — it is what the
    next real backend on this machine reads, spares from its orphan sweep and
    attaches to.
    """
    monkeypatch.setattr(
        tool_runtime.process_cleanup, "pid_file", tmp_path / "browser_pids.json"
    )


# The page's own request, answered by the fixture server's echo route — the one
# oracle in this file that does not go through a cookie API of ours. ``fetch``
# defaults to ``credentials: "same-origin"`` and the request IS same-origin, so
# the header the server reflects is exactly what Chrome decided to send.
_COOKIE_HEADER_JS = """
(async () => {
  const reply = await fetch('/api/echo', {method: 'POST', body: 'f898'});
  const answer = await reply.json();
  return answer.headers.cookie || '';
})()
"""


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _cookie_header(instance_id: str) -> str:
    """What the SERVER saw this browser send. The claim, not a proxy for it."""
    return str(await eval_js(instance_id, _COOKIE_HEADER_JS))


async def _log_in(instance_id: str, session_value: str, persist_value: str) -> None:
    """Put one SESSION cookie and one PERSISTENT cookie in the live browser.

    Written through ``document.cookie`` rather than our own ``set_cookie`` tool
    on purpose: this is meant to be the state a human's login leaves behind, and
    a cookie this backend placed by CDP is the one shape that could conceivably
    be special. The PAIR is the experiment — only one of the two can survive a
    file copy, because only one of the two is ever written to disk.
    """
    await eval_js(
        instance_id,
        f"document.cookie = 'f898_session={session_value}; Path=/'; "
        f"document.cookie = 'f898_persist={persist_value}; Path=/; Max-Age=3600'; "
        "document.cookie.length",
    )


def _selection(result: dict) -> dict:
    return result["spawn_diagnostics"]["profile_selection"]


def _directory(result: dict, sessions) -> str:
    """The directory a spawn REALLY got, checked to be under the tmp root.

    Read from the answer rather than composed from the name, which is conftest's
    standing rule for this tier — and the containment check is what proves the
    isolation fixture above actually took, since every failure mode of it ends
    with a real profile written somewhere else.
    """
    directory = _selection(result)["user_data_dir"]
    assert str(directory).startswith(str(sessions)), (
        f"{directory!r} is outside the node's own session root {sessions!r}"
    )
    return directory


async def test_a_running_source_hands_its_jar_over_and_keeps_running(
    fixture_app_server, tmp_empty_root
):
    """The whole finding: seed from a session that is still open, and prove it."""
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    sessions = tmp_empty_root["sessions"]

    session_value = uuid.uuid4().hex[:12]
    persist_value = uuid.uuid4().hex[:12]
    source_name = _unique("f898-src")

    opened = await spawn(session=source_name, headless=True, **sandbox_kwargs())
    source = opened["instance_id"]
    source_dir = _directory(opened, sessions)
    target = None
    target_dir = None
    try:
        await navigate_and_settle(source, f"{fixture_app_server}/index.html")
        await _log_in(source, session_value, persist_value)
        # In-page state, so the last assertion is about the SAME renderer and
        # not merely about some browser on that directory.
        await eval_js(source, "window.__f898 = 'source-alive'")

        # The source is STILL RUNNING here. Under F-897 this spawn was a refusal.
        result = await spawn(
            session=_unique("f898-dst"),
            seed_from=source_name,
            headless=True,
            **sandbox_kwargs(),
        )
        target = result["instance_id"]
        target_dir = _directory(result, sessions)
        selection = _selection(result)

        assert selection["seeded_via"] == cookie_handoff.VIA_CDP, selection
        assert "cookie_handoff_error" not in selection, selection
        # Two is the floor because two is what was put in; the jar is the WHOLE
        # browser's, so a profile that picked up anything else is fine.
        assert selection["cookies_carried"] >= 2, selection
        assert selection["cookies_read"] == selection["cookies_carried"], selection
        assert selection["cookies_session"] >= 1, selection
        assert selection["cookies_in_target"] >= selection["cookies_carried"], selection

        # The internal key never reaches a caller — `_public_profile_selection`'s
        # one line, asked of a real spawn rather than of a fixture dict.
        assert clone_storage.LIVE_SEED_KEY not in selection, selection
        # PII: counts and shape only. Neither cookie's name nor its value may
        # appear anywhere in what a client is handed.
        answer = json.dumps(result, default=str)
        for secret in (session_value, persist_value, "f898_session", "f898_persist"):
            assert secret not in answer, f"{secret!r} leaked into the spawn answer"

        # THE claim: the target's server sees both cookies. Not our jar reader —
        # the fixture server's own view of what Chrome chose to send.
        await navigate_and_settle(target, f"{fixture_app_server}/index.html")
        header = await _cookie_header(target)
        assert f"f898_session={session_value}" in header, header
        assert f"f898_persist={persist_value}" in header, header

        # And the source is untouched: the same renderer, still ours, still
        # logged in. A hand-off that MOVED the jar rather than copying it, or
        # that cost the source its browser, would fail here and nowhere else.
        assert await eval_js(source, "window.__f898") == "source-alive"
        assert f"f898_session={session_value}" in await _cookie_header(source)
    finally:
        for iid in (target, source):
            if iid is not None:
                with contextlib.suppress(Exception):
                    await close(instance_id=iid)
        for directory in (target_dir, source_dir):
            if directory is not None:
                await released(directory)


async def test_a_closed_source_seeds_by_copy_and_loses_the_session_cookie(
    fixture_app_server, tmp_empty_root
):
    """The control, and the reason the hand-off exists.

    Same request, same two cookies, one difference: the source is CLOSED first,
    so F-897's file copy is the whole mechanism. It must report no hand-off at
    all, and the cookie that was never on disk must be missing — which is what
    makes node 1's pass a statement about the CDP path and not about copying.
    """
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    sessions = tmp_empty_root["sessions"]

    session_value = uuid.uuid4().hex[:12]
    persist_value = uuid.uuid4().hex[:12]
    source_name = _unique("f898-closed")

    opened = await spawn(session=source_name, headless=True, **sandbox_kwargs())
    source = opened["instance_id"]
    source_dir = _directory(opened, sessions)
    target = None
    target_dir = None
    try:
        await navigate_and_settle(source, f"{fixture_app_server}/index.html")
        await _log_in(source, session_value, persist_value)

        await close(instance_id=source)
        source = None
        # The copy READS that directory, so it has to wait for Chrome to let go
        # of it — the same barrier every file in this tier takes, for the same
        # race, and here it is also what makes the control honest.
        await released(source_dir)

        result = await spawn(
            session=_unique("f898-copy"),
            seed_from=source_name,
            headless=True,
            **sandbox_kwargs(),
        )
        target = result["instance_id"]
        target_dir = _directory(result, sessions)
        selection = _selection(result)

        # No hand-off happened and none was attempted: a closed source has no
        # connection of ours to read through, and reporting one either way would
        # be a claim about where these cookies came from.
        assert "seeded_via" not in selection, selection
        assert "cookie_handoff_error" not in selection, selection
        # The copy itself DID run, which is what keeps the next assertion about
        # the session cookie rather than about a spawn that seeded nothing.
        assert selection["seeded_from"] == source_name, selection

        await navigate_and_settle(target, f"{fixture_app_server}/index.html")
        header = await _cookie_header(target)
        assert f"f898_session={session_value}" not in header, (
            "a session cookie is never written to disk, so a file copy cannot "
            f"carry it — this one did: {header!r}"
        )
    finally:
        for iid in (target, source):
            if iid is not None:
                with contextlib.suppress(Exception):
                    await close(instance_id=iid)
        for directory in (target_dir, source_dir):
            if directory is not None:
                await released(directory)
