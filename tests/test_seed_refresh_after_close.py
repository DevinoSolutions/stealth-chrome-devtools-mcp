"""F-910's blast radius: what the close took with it beyond one profile.

Closing the `default` session does not only close a browser — `close_instance`
runs `_refresh_master_snapshot_if_safe("after-default-close")` in the same
breath, so the SEED every later session is copied from is rewritten from the
profile that close has just finished with. Two things therefore have to be true
of every close of the shared session, and this module states both:

* the refresh RAN and refreshed — it asks `_profile_has_running_browser` first
  and refuses a profile a live browser holds; and
* the copy it made found nothing locked — `profile_copy.copy_file` answers a
  locked file by SKIPPING it, which is a login missing from the seed that
  nothing afterwards can enumerate.

**These are CONTRACT pins and not F-910 REDs — measured, not assumed.** With
Phase 2b neutralised (the reviewer's own plugin, which replaces
`process_exit.wait_for_exit_async` with a no-op, restoring the pre-fix
ordering), both nodes pass: 10 runs, 20/20 node passes, 2026-09-21. The reason
is upstream of the refresh and was missed in the first draft of this module:
the refresh runs after `browser_manager.close_instance` RETURNS, and the last
thing that call does is Phase 3's `_blocking_teardown`, whose FIRST statement
is `process_cleanup.kill_browser_process` — a `terminate()` plus a blocking
`process.wait(timeout=3)` for every browser pid on the profile. So the profile
was already free when the refresh asked, and the seed refresh was NOT silently
disabled before F-910. What F-910 changes for the seed is the CONTENT of the
profile that gets copied, not whether the copy happens: the wait is what makes
Chrome's own shutdown — and therefore its cookie commit — land BEFORE the
copy, rather than being cut short by that terminate.

**Not under the stalled harness.** The brief asked for these under the arm that
suppresses `Browser.close`, and they cannot be: with nothing asking Chrome to
leave, no wait can make it leave, so the skips stand after the fix exactly as
before it. That arm proves the CAUSE and is recorded in the finding; what
proves the FIX is a real close, which is what runs here.

**The fixture's own stores had to be fixed before any of this could be
measured**: `tmp_session_root` wrote 18-byte placeholders where a Chrome
profile keeps SQLite, and a real browser opened on one keeps its cookie jar in
MEMORY — this finding's symptom, manufactured by the harness. That is fixed at
the source (`tests/conftest.py` writes real empty databases) and pinned there
(`test_profile_seed_truth.py`), not worked around here.

**The root is redirected TWICE and every node checks that the second one took.**
`conftest.py` already fences the whole suite away from the operator's session
root at import time (F-841). These nodes take a second, per-test redirect
because they do not merely add a session, they REWRITE THE SEED — the profile
every other module's named session is copied from — so `redirected_root`
resolves all four directories through the product's own accessors and refuses
to run if any of them still points outside its own tmp tree, before a single
browser is spawned.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    sandbox_kwargs,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded import clone_storage
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.settings import get_settings

pytestmark = integration_pytestmark()

COOKIE_NAME = "f910_seed_persistent"
COOKIE = f"{COOKIE_NAME}=f910-seed-value"
CLOSE_REASON = "after-default-close"
SKIP_METHOD = "copy_skip"


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


@pytest.fixture()
def redirected_root(tmp_session_root):
    """The four directories this module writes to, proven to be the tmp ones.

    `tmp_session_root` patches the environment; what matters is what the
    product RESOLVES, so that is what is asserted — through the same four
    accessors `_refresh_master_snapshot_if_safe` itself calls, and before any
    spawn.

    It is the SECOND fence and not the first: `conftest.py` already redirects
    the session root away from the operator's at import time (F-841). This one
    is per-test because these nodes do not merely add a session, they REWRITE
    THE SEED — so a redirect that silently failed to take would land on the
    shared test root's seed, which every other module's named profile is
    copied from, and the damage would be read as a product defect somewhere
    else entirely.
    """
    get_settings.cache_clear()
    root = Path(tmp_session_root["root"]).resolve()
    resolved = {
        "root": clone_storage.default_session_root().resolve(),
        "default": clone_storage.master_profile_dir().resolve(),
        "seed": clone_storage.master_snapshot_dir().resolve(),
        "sessions": clone_storage.clone_root_dir().resolve(),
    }
    assert resolved["root"] == root, (
        f"the session-root redirect did not take: the product resolves "
        f"{resolved['root']} while this test owns {root} — refusing to open "
        "and re-seed a profile that is not ours"
    )
    for name in ("default", "seed", "sessions"):
        assert root in resolved[name].parents, (
            f"{name} resolves to {resolved[name]}, outside the tmp root {root}"
        )
    yield resolved


class _Close:
    """One close of the shared session, and everything it was observed to do."""

    def __init__(self):
        self.refreshes: list[dict] = []
        self.answer: dict = {}
        self.skips_before = 0
        self.skips_after = 0

    @property
    def refresh(self) -> dict:
        """The refresh the CLOSE ran — never the one the spawn ran."""
        after = [r for r in self.refreshes if r.get("seed_reason") == CLOSE_REASON]
        assert len(after) == 1, (
            f"expected exactly one {CLOSE_REASON} refresh, saw "
            f"{[r.get('seed_reason') for r in self.refreshes]}"
        )
        return after[0]

    @property
    def skips(self) -> int:
        return self.skips_after - self.skips_before


def _copy_skips() -> int:
    """How many locked profile files the copier has given up on so far.

    Read out of the product's own debug ring rather than by replacing
    `copy_file`, so the copy under test is the shipped one. A DELTA, because
    the ring is process-wide and other tests share it.
    """
    view = debug_logger.get_debug_view_paginated()
    return sum(
        1
        for warning in view.get("all_warnings") or []
        if warning.get("method") == SKIP_METHOD
    )


async def _close_the_default_session(app_base, monkeypatch) -> _Close:
    """Open `default`, set a max-age cookie in it, close it. Report what ran."""
    observed = _Close()
    original = clone_storage._refresh_master_snapshot_if_safe

    def _watched(reason):
        result = original(reason)
        observed.refreshes.append(result)
        return result

    monkeypatch.setattr(clone_storage, "_refresh_master_snapshot_if_safe", _watched)

    spawn = await get_fn("spawn_browser")(
        session="default", headless=True, **sandbox_kwargs()
    )
    instance_id = spawn["instance_id"]

    try:
        # Inside the try, because everything from here on owes this browser a
        # close — a node that fails its way past one leaves a Chrome behind.
        selection = (spawn.get("spawn_diagnostics") or {}).get(
            "profile_selection"
        ) or {}
        assert selection.get("profile_role") == "default", (
            "this node must drive the SHARED session, because it is the only "
            f"role whose close refreshes the seed — it got {selection!r}"
        )
        await navigate_and_settle(instance_id, f"{app_base}/state/store.html")
        await eval_js(instance_id, f"document.cookie = '{COOKIE}; max-age=600; path=/'")
        assert COOKIE_NAME in await eval_js(instance_id, "document.cookie"), (
            "the probe cookie was never set, so this node would measure nothing"
        )
        observed.skips_before = _copy_skips()
        observed.answer = await get_fn("close_instance")(instance_id=instance_id)
        observed.skips_after = _copy_skips()
        assert observed.answer["closed"] is True, observed.answer
    except BaseException:
        with contextlib.suppress(Exception):
            await get_fn("close_instance")(instance_id=instance_id)
        raise
    return observed


def _seed_files(seed: Path) -> list[Path]:
    network = seed / "Default" / "Network"
    return [path for path in network.glob("*") if path.is_file()]


#: A file this big is not a cookie jar, and reading it to find out costs the
#: failure path more than the answer is worth.
_EVIDENCE_MAX_BYTES = 8 * 1024 * 1024


def _tree_evidence(label: str, profile: Path) -> str:
    """What a profile actually holds, so a POSIX-only failure can be read off
    the job log alone.

    Gate run 35633920183 failed this module's first node on BOTH POSIX cells
    with an empty `Default/Network`, while Windows passed — and an empty
    directory cannot say whether the browser never wrote the jar, the copy
    dropped it, or that host keeps it somewhere else. Naming all three is what
    turns one more gate round into an answer instead of another guess.
    """
    if not profile.exists():
        return f"{label}: MISSING at {profile}"
    default = profile / "Default"
    network = default / "Network"

    def _names(path: Path, limit: int) -> list[str]:
        if not path.is_dir():
            return [f"<not a directory: {path.name}>"]
        return sorted(entry.name for entry in path.iterdir())[:limit]

    sized = (
        sorted(f"{entry.name}:{entry.stat().st_size}" for entry in network.iterdir())
        if network.is_dir()
        else ["<no Default/Network>"]
    )
    return (
        f"{label} at {profile}: root={_names(profile, 20)} "
        f"Default={_names(default, 30)} Default/Network={sized}"
    )


def _needle_locations(root: Path, needle: bytes) -> list[str]:
    """Every file under *root* carrying the probe cookie's name.

    It tells a copy that DROPPED the jar apart from a browser that never wrote
    one: bytes present under the source and absent from the seed is the copy's
    defect, absent from both is the shutdown's.
    """
    found: list[str] = []
    for path in root.rglob("*"):
        if len(found) >= 10:
            break
        try:
            if not path.is_file() or path.stat().st_size > _EVIDENCE_MAX_BYTES:
                continue
            if needle in path.read_bytes():
                found.append(str(path.relative_to(root)))
        except OSError:
            continue
    return found


async def test_the_seed_refresh_after_a_default_close_carries_the_login(
    fixture_app_server, redirected_root, monkeypatch
):
    """The seed is refreshed on close, the CALLER is told so, and the cookie
    set before the close is in there.

    Three assertions in one node because they are one event: the refresh has to
    RUN before there is anything to ask about its contents, and what the tool
    REPORTED has to agree with what the product actually did — a refusal used
    to go into a dict `close_instance` discarded, which is how a seed that had
    stopped moving would have stayed invisible (the lead's M3 ruling).
    """
    observed = await _close_the_default_session(fixture_app_server, monkeypatch)

    # The reporting contract, read off the tool's own answer.
    assert observed.answer.get("seed_refreshed") is True, (
        f"close_instance did not report a refreshed seed: {observed.answer!r}"
    )
    assert "seed_error" not in observed.answer, (
        f"the tool reported a seed error: {observed.answer!r}"
    )
    # …and it agrees with what the product's own refresh returned.
    assert observed.refresh.get("seed_refreshed") is True, (
        "closing the default session did not refresh its seed: "
        f"{observed.refresh!r} — every later session is copied from that seed"
    )
    assert "seed_error" not in observed.refresh, (
        f"the refresh reported an error: {observed.refresh!r}"
    )

    seed = redirected_root["seed"]
    master = redirected_root["default"]
    needle = COOKIE_NAME.encode()
    carrying = [path.name for path in _seed_files(seed) if needle in path.read_bytes()]
    assert carrying, (
        "the refreshed seed does not carry the cookie set before the close — "
        f"searched {[p.name for p in _seed_files(seed)]} under {seed}. Every "
        "session created from here inherits a profile missing that login\n"
        f"  {_tree_evidence('SOURCE (shared profile)', master)}\n"
        f"  {_tree_evidence('SEED (snapshot)', seed)}\n"
        f"  cookie bytes under source: {_needle_locations(master, needle)}\n"
        f"  cookie bytes under seed:   {_needle_locations(seed, needle)}\n"
        f"  refresh={observed.refresh!r} copy_skips={observed.skips}"
    )


async def test_the_seed_refresh_after_a_default_close_skips_no_file(
    fixture_app_server, redirected_root, monkeypatch
):
    """The copy that follows a close must find nothing locked.

    The second exposure at the same site, and it is not pinned by the first:
    `copy_file` answers a file Chrome still holds by logging `copy_skip` and
    carrying on, so a seed built over a live browser is silently incomplete
    rather than refused. A CONTRACT pin, not an F-910 RED — it passes with
    Phase 2b neutralised too (see this module's docstring for the numbers and
    for why: the kill path already blocked on the browser's exit). What it
    states is that the property survives the fix, which reorders that path.
    """
    observed = await _close_the_default_session(fixture_app_server, monkeypatch)

    assert observed.skips == 0, (
        f"the seed refresh skipped {observed.skips} locked profile file(s) — "
        "it copied the shared profile while Chrome still held files in it, and "
        "a skipped file is a login missing from the seed that nothing "
        "afterwards can name"
    )
