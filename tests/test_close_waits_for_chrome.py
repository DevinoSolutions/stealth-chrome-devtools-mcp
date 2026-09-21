"""F-910. ``close_instance`` must not terminate a browser that is still leaving.

Chrome writes its cookie store on the way out. Before F-910 the kill path ran
the instant the graceful ``Browser.close`` had been SENT, so it landed on a
browser that was still shutting down — measured 20 times out of 20 — and when
the terminate beat the commit the user's login was gone.

The two nodes here are deliberately different in kind, and the difference is
stated because it decides what a failure means:

* ``test_the_kill_path_never_runs_against_a_live_browser`` is the MECHANISM,
  and its RED is deterministic: on the code this replaces, the browser was
  still running at the kill site on every single close measured.
* ``test_a_max_age_cookie_survives_close_and_respawn`` is the user-visible
  CONSEQUENCE, and its RED is probabilistic — one close in thirty locally, two
  Windows CI runs out of two. It is kept because it is the thing that actually
  matters, and because it is the only node that would catch a fix which stops
  the kill racing the flush by some other route.

Neither can be made deterministic by suppressing the graceful close: with
nothing asking Chrome to leave, no wait can help and the terminate is the only
ending there is. That arm proves the mechanism (it loses the cookie 5 times out
of 5) and is recorded in the finding rather than shipped as a pin that the fix
is not claimed to satisfy.
"""

from __future__ import annotations

import contextlib
import shutil
import time
import uuid
from pathlib import Path

import psutil
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
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

pytestmark = integration_pytestmark()

COOKIE = "f910_persistent=f910-persistent-value"
PROFILE_RELEASE_TIMEOUT = 30.0
PROFILE_REMOVAL_TIMEOUT = 20.0


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


class _Spawned:
    """Spawns instances for this module and guarantees their removal.

    A NAMED profile is the point — only a named one survives a restart, which
    is the whole question — and a named profile is not auto-cleaned, so every
    directory this module creates is tracked and deleted even when a node
    fails.
    """

    def __init__(self):
        self.instance_ids: list[str] = []
        self.profile_dirs: dict[str, str] = {}

    async def spawn(self, profile: str | None = None) -> str:
        kwargs = {"headless": True, **sandbox_kwargs()}
        if profile is not None:
            kwargs["user_data_dir"] = profile
        result = await get_fn("spawn_browser")(**kwargs)
        instance_id = result["instance_id"]
        self.instance_ids.append(instance_id)
        if profile is not None:
            directory = (result.get("spawn_diagnostics") or {}).get("user_data_dir")
            assert directory, f"spawn reported no profile directory: {result}"
            self.profile_dirs[profile] = directory
        return instance_id

    async def close(self, instance_id: str) -> bool:
        # F-910's own reporting change: the tool answers a record, and `closed`
        # is the boolean it used to return. Read strictly (see the same helper
        # in `test_stateful_i18n.py`).
        answer = await get_fn("close_instance")(instance_id=instance_id)
        with contextlib.suppress(ValueError):
            self.instance_ids.remove(instance_id)
        return answer["closed"]

    async def await_profile_free(self, profile: str) -> bool:
        directory = Path(self.profile_dirs[profile])
        deadline = time.monotonic() + PROFILE_RELEASE_TIMEOUT
        while (
            clone_storage._profile_has_running_browser(directory)
            and time.monotonic() < deadline
        ):
            await _idle()
        return not clone_storage._profile_has_running_browser(directory)

    async def remove_profile(self, profile: str) -> None:
        directory = Path(self.profile_dirs[profile])
        deadline = time.monotonic() + PROFILE_REMOVAL_TIMEOUT
        while directory.exists() and time.monotonic() < deadline:
            shutil.rmtree(directory, ignore_errors=True)
            if directory.exists():
                await _idle()


async def _idle() -> None:
    import asyncio

    await asyncio.sleep(0.25)


@pytest.fixture()
async def spawned():
    manager = _Spawned()
    try:
        yield manager
    finally:
        for instance_id in list(manager.instance_ids):
            with contextlib.suppress(Exception):
                await get_fn("close_instance")(instance_id=instance_id)
        for profile in manager.profile_dirs:
            with contextlib.suppress(Exception):
                await manager.await_profile_free(profile)
                await manager.remove_profile(profile)


def _profile(name: str) -> str:
    return f"f910-{name}-{uuid.uuid4().hex[:8]}"


def _exit_lines(instance_id: str) -> list[str]:
    """The close-diagnostics lines this instance's close left in the ring."""
    view = debug_logger.get_debug_view_paginated()
    return [
        entry.get("message", "")
        for entry in view.get("all_info") or []
        if entry.get("component") == "process_exit"
        and entry.get("method") == "close_exit_wait"
        and instance_id in entry.get("message", "")
    ]


async def test_the_kill_path_never_runs_against_a_live_browser(spawned, monkeypatch):
    """The browser must have exited on its own before the kill path runs, and
    the close must SAY so where a post-mortem can read it.

    This is the F-910 race stated as the property that removes it. The reading
    is taken at the top of Phase 3 — the first line of the kill path — through
    one `psutil` status call that changes nothing, so a RED here is the defect
    itself and not an artefact of the measurement.

    The diagnostic is asserted here rather than in a node of its own because it
    is the same event read from the other side: the wait happened, and what it
    observed survived the call. It is not decoration — a close that silently
    stops waiting looks exactly like one that waited and found the browser
    already gone, and this line is the only thing that tells them apart. It
    earned its pin immediately: `process_exit` first shipped importing the
    debug_logger MODULE rather than the singleton, so every line it wrote was
    an `AttributeError` that `report`'s own never-raises contract swallowed.
    """
    original = BrowserManager._blocking_teardown
    observed: list[str] = []

    def _watched(self, instance_id, browser):
        process = getattr(browser, "_process", None)
        pid = getattr(process, "pid", None) or getattr(browser, "_process_pid", None)
        if not pid:
            observed.append("no-pid")
        else:
            try:
                observed.append(psutil.Process(pid).status())
            except psutil.NoSuchProcess:
                observed.append("gone")
            except psutil.Error as err:  # pragma: no cover - platform dependent
                observed.append(f"unreadable: {err}")
        return original(self, instance_id, browser)

    monkeypatch.setattr(BrowserManager, "_blocking_teardown", _watched)

    # A NAMED profile, like the other node: an unnamed spawn lands on the
    # SHARED session whenever nothing else holds it, and closing that one
    # rewrites the seed every later session is copied from — which is not a
    # side effect a pin about a kill path is entitled to.
    profile = _profile("killpath")
    instance_id = await spawned.spawn(profile)
    assert await spawned.close(instance_id) is True

    assert observed, "the kill path never ran, so this node measured nothing"
    # `zombie` is an EXITED browser and belongs in this set: on POSIX the wait
    # deliberately does not reap (`process_exit`'s POSIX paragraph — asyncio's
    # child watcher owns that `waitpid`), so between Chrome's exit and its
    # parent collecting the status the process entry is still there, in exactly
    # that state. Windows has no zombies and answers `gone`.
    assert observed[0] in {"gone", "no-pid", psutil.STATUS_ZOMBIE}, (
        "close_instance reached its kill path while the browser was still "
        f"running (status {observed[0]!r}) — Chrome was mid-shutdown, and a "
        "terminate there truncates the cookie-store commit (F-910)"
    )

    reported = _exit_lines(instance_id)
    assert len(reported) == 1, (
        "the close wrote no close-diagnostics line for this instance, so "
        "nothing afterwards can say whether the browser left on its own or "
        f"was killed — saw {reported!r}"
    )
    assert "exited_unaided=True" in reported[0], (
        f"the browser did not leave on its own: {reported[0]!r}"
    )


async def test_a_max_age_cookie_survives_close_and_respawn(fixture_app_server, spawned):
    """A login made before a close is still there when the session re-opens.

    The consequence the mechanism pin exists for. Its RED is probabilistic —
    one close in thirty locally, two of two on a loaded Windows CI runner — so
    it is the node that states the contract, not the node that proves the bug.
    """
    base = fixture_app_server
    profile = _profile("cookie")

    first = await spawned.spawn(profile)
    seeded_dir = spawned.profile_dirs[profile]
    await navigate_and_settle(first, f"{base}/state/store.html")
    await eval_js(first, f"document.cookie = '{COOKIE}; max-age=600; path=/'")
    assert COOKIE in await eval_js(first, "document.cookie"), (
        "the probe cookie was never set, so this node would measure nothing"
    )
    assert await spawned.close(first) is True

    # The same named profile, a brand-new browser process. Without the barrier
    # a still-held profile resolves to a numbered variant copied from the seed,
    # and the node would report that nothing persisted when in truth it had
    # read a different profile.
    assert await spawned.await_profile_free(profile) is True
    second = await spawned.spawn(profile)
    assert spawned.profile_dirs[profile] == seeded_dir, (
        "the respawn resolved a DIFFERENT profile directory, so any result "
        "below would be about the wrong profile"
    )
    await navigate_and_settle(second, f"{base}/state/store.html")
    restored = await eval_js(second, "document.cookie")
    assert await spawned.close(second) is True

    assert COOKIE in (restored or ""), (
        "a max-age cookie set before close_instance was gone after a respawn "
        "on the same session — Chrome was terminated before it committed its "
        "cookie store (F-910)"
    )
