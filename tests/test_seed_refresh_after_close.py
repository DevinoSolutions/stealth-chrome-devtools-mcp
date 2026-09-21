"""F-910's blast radius: what the close took with it beyond one profile.

Closing the `default` session does not only close a browser — `close_instance`
runs `_refresh_master_snapshot_if_safe("after-default-close")` in the same
breath, so the SEED every later session is copied from is rewritten from the
profile that close has just finished with. Before F-910 that close returned
while Chrome was still shutting down, which reaches the seed by two separate
routes:

* the refresh asks `_profile_has_running_browser(default)` first, sees the
  browser we just asked to leave STILL RUNNING, and refuses — so the whole
  refresh-on-close feature did nothing at all, silently, on every close; and
* were it not refused, it would copy a profile Chrome still holds files in,
  and `profile_copy.copy_file` answers a locked file by SKIPPING it, which is
  a login missing from the seed that nothing afterwards can enumerate.

Both are pinned here, and the first one's RED is deterministic on the shipped
code: Chrome was still running at that point on every close measured (20/20),
so the refusal is not a race, it is the behaviour.

**Not under the stalled harness.** The brief asked for these under the arm that
suppresses `Browser.close`, and they cannot be: with nothing asking Chrome to
leave, no wait can make it leave, so the refusal and the skips stand after the
fix exactly as before it. That arm proves the CAUSE and is recorded in the
finding; what proves the FIX is a real close, which is what runs here.

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
        closed = await get_fn("close_instance")(instance_id=instance_id)
        observed.skips_after = _copy_skips()
        assert closed is True
    except BaseException:
        with contextlib.suppress(Exception):
            await get_fn("close_instance")(instance_id=instance_id)
        raise
    return observed


def _seed_files(seed: Path) -> list[Path]:
    network = seed / "Default" / "Network"
    return [path for path in network.glob("*") if path.is_file()]


async def test_the_seed_refresh_after_a_default_close_carries_the_login(
    fixture_app_server, redirected_root, monkeypatch
):
    """The seed is refreshed on close, and the cookie set before it is in there.

    Two assertions in one node because they are one event: the refresh has to
    RUN before there is anything to ask about its contents, and running is the
    half whose RED is deterministic — before F-910 the browser was still alive
    when the refresh asked, so it answered `default-in-use` and copied nothing.
    """
    observed = await _close_the_default_session(fixture_app_server, monkeypatch)

    assert observed.refresh.get("seed_refreshed") is True, (
        "closing the default session did not refresh its seed: "
        f"{observed.refresh!r} — before F-910 close_instance returned while "
        "Chrome was still running, so the refresh saw a live browser on the "
        "profile and refused, and every later session was copied from a seed "
        "that had never been updated"
    )
    assert "seed_error" not in observed.refresh, (
        f"the refresh reported an error: {observed.refresh!r}"
    )

    seed = redirected_root["seed"]
    needle = COOKIE_NAME.encode()
    carrying = [path.name for path in _seed_files(seed) if needle in path.read_bytes()]
    assert carrying, (
        "the refreshed seed does not carry the cookie set before the close — "
        f"searched {[p.name for p in _seed_files(seed)]} under {seed}. Every "
        "session created from here inherits a profile missing that login"
    )


async def test_the_seed_refresh_after_a_default_close_skips_no_file(
    fixture_app_server, redirected_root, monkeypatch
):
    """The copy that follows a close must find nothing locked.

    The second exposure at the same site, and it is not pinned by the first:
    `copy_file` answers a file Chrome still holds by logging `copy_skip` and
    carrying on, so a seed built over a live browser is silently incomplete
    rather than refused. The bounded wait sits inside `close_instance`, so the
    refresh that runs after it inherits a browser that has already gone — this
    is what says that inheritance is real rather than argued.
    """
    observed = await _close_the_default_session(fixture_app_server, monkeypatch)

    assert observed.skips == 0, (
        f"the seed refresh skipped {observed.skips} locked profile file(s) — "
        "it copied the shared profile while Chrome still held files in it, and "
        "a skipped file is a login missing from the seed that nothing "
        "afterwards can name"
    )
