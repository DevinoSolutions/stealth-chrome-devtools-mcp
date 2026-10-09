"""Real-Chrome proof for F-950: logins made in a RE-ATTACHED master reach a clone.

The hermetic pins in ``test_adopted_master_handoff.py`` prove the decision
against doubles. They cannot prove the thing the owner reported, because every
one of them answers whatever it was built to answer: that after a backend
re-attaches to the shared (`default`) browser, a clone spawned from that same
backend carries the cookies logged in AFTER the seed was last refreshed.

So this node uses no double for the mechanism. One real headless Chrome is the
master on the node's own tmp session root; a session cookie, a persistent one
and a ``__Host-`` one are written into it; the backend then "dies" the only
honest way (the manager forgets the browser without killing it) and a fresh
adoption pass re-attaches to it, which re-stamps the record's owner to THIS
process. The ownership witness is patched to say this process is a live backend,
which is the production state the pre-F-950 hermetic pins never entered. An
unnamed spawn then has to come back as a clone with ``seeded_via:
"cdp-cookies"``, and the oracle is the fixture server's echo of the ``Cookie``
header the clone SENDS — not a cookie API of ours.

Two things the record is redirected for, as in the F-888 and F-898 files: the
spawn goes through the real ``rt.process_cleanup``, and a leaked persistent
entry is exactly what the next real backend on the machine would adopt.
"""

import asyncio
import contextlib
import json
import os
import uuid
from pathlib import Path
from unittest.mock import patch

import nodriver as uc
import psutil
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
    browser_reattach,
    cookie_handoff,
    tool_runtime,
)
from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup
from stealth_chrome_devtools_mcp.settings import get_settings

pytestmark = integration_pytestmark()

# The product's own bound is ATTACH_BUDGET_SECONDS (15 s); this is the test's
# outer guard so a hang names itself instead of hitting the suite timeout.
_ADOPT_DEADLINE = 45.0

#: A real https origin for the two cookies a page on the loopback http fixture
#: cannot honestly create: ``__Host-`` requires ``Secure`` and a Secure source
#: scheme, and CHIPS needs a partition. Chrome's ``Storage.setCookies`` with a
#: ``url`` records the scheme properly, which is what a real login looks like.
_SECURE_URL = "https://f950.example.test/"

_COOKIE_HEADER_JS = """
(async () => {
  const reply = await fetch('/api/echo', {method: 'POST', body: 'f950'});
  const answer = await reply.json();
  return answer.headers.cookie || '';
})()
"""


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


@pytest.fixture(autouse=True)
def _isolated_root(tmp_empty_root):
    """Make ``tmp_empty_root`` win over the warmup's cached settings; see
    ``test_e2e_seed_from_running_source._isolated_root`` for the whole story."""
    get_settings.cache_clear()
    return tmp_empty_root


@pytest.fixture(autouse=True)
def _record_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tool_runtime.process_cleanup, "pid_file", tmp_path / "browser_pids.json"
    )


def _cleanup_on(pid_file) -> ProcessCleanup:
    """A ProcessCleanup whose record is the test's file, built without __init__."""
    cleanup = ProcessCleanup.__new__(ProcessCleanup)
    cleanup.pid_file = pid_file
    cleanup.tracked_pids = set()
    cleanup.browser_processes = {}
    cleanup.orphan_profile_max_age_seconds = 0
    cleanup._init_time = 0.0
    return cleanup


async def _cookie_header(instance_id: str) -> str:
    return str(await eval_js(instance_id, _COOKIE_HEADER_JS))


async def test_a_clone_of_a_re_attached_master_carries_its_logins(
    fixture_app_server, tmp_empty_root, tmp_path
):
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    manager = tool_runtime.browser_manager
    master = tmp_empty_root["master"]
    sessions = tmp_empty_root["sessions"]

    session_value = uuid.uuid4().hex[:12]
    persist_value = uuid.uuid4().hex[:12]
    host_value = uuid.uuid4().hex[:12]
    chips_value = uuid.uuid4().hex[:12]
    loopback_secure_value = uuid.uuid4().hex[:12]

    opened = await spawn(headless=True, **sandbox_kwargs())
    iid = opened["instance_id"]
    clone_id = None
    clone_dir = None
    adopted_here = False
    chrome_pid = None
    try:
        selection = opened["spawn_diagnostics"]["profile_selection"]
        assert selection["profile_role"] == "default", selection

        await navigate_and_settle(iid, f"{fixture_app_server}/index.html")
        await eval_js(
            iid,
            f"document.cookie = 'f950_session={session_value}; Path=/'; "
            f"document.cookie = 'f950_persist={persist_value}; Path=/; Max-Age=3600'; "
            # A Secure cookie stored from a loopback http page: Chrome keeps it
            # with sourceScheme NonSecure and then REFUSES it in setCookies,
            # which turned the whole hand-off into the stale copy (F-950).
            f"document.cookie = '__Host-f950-loopback={loopback_secure_value}; "
            "Path=/; Secure'; document.cookie.length",
        )
        source_connection = manager._instances[iid]["browser"].connection
        await source_connection.send(
            uc.cdp.storage.set_cookies(
                cookies=[
                    uc.cdp.network.CookieParam(
                        name="__Host-f950",
                        value=host_value,
                        url=_SECURE_URL,
                        path="/",
                        secure=True,
                    ),
                    uc.cdp.network.CookieParam(
                        name="f950_chips",
                        value=chips_value,
                        url=_SECURE_URL,
                        path="/",
                        secure=True,
                        same_site=uc.cdp.network.CookieSameSite.NONE,
                        partition_key=uc.cdp.network.CookiePartitionKey(
                            top_level_site="https://f950.example.test",
                            has_cross_site_ancestor=False,
                        ),
                    ),
                ]
            )
        )
        source_header = await _cookie_header(iid)
        for value in (session_value, persist_value):
            assert value in source_header, "the control: the source sends its own jar"
        source_jar = await cookie_handoff.read_jar(manager._instances[iid]["browser"])
        source_names = {cookie.name for cookie in source_jar}
        assert {"__Host-f950", "f950_chips"} <= source_names, "the control"

        entry = manager._instances[iid]
        browser = entry["browser"]
        chrome_pid = browser._process_pid
        port = browser.config.port

        # The backend dies: Chrome keeps running, only the record names it.
        record = tmp_path / "browser_pids.json"
        record.write_text(
            json.dumps(
                {
                    "browser_processes": {
                        iid: {
                            "pid": chrome_pid,
                            "create_time": psutil.Process(chrome_pid).create_time(),
                            "user_data_dir": os.path.normcase(
                                os.path.normpath(str(master))
                            ),
                            "uses_custom_data_dir": True,
                            "auto_clone": False,
                            "cdp_port": port,
                            "timestamp": 0,
                            "owner_pid": os.getpid(),
                            "owner_create_time": None,
                        }
                    },
                    "timestamp": 0,
                }
            )
        )
        manager._instances.pop(iid)
        manager._spawn_diagnostics.pop(iid, None)

        cleanup = _cleanup_on(record)
        with patch.object(
            browser_reattach.browser_pid_registry, "is_reapable", return_value=True
        ):
            adopted = await asyncio.wait_for(
                browser_reattach.run(manager, cleanup), timeout=_ADOPT_DEADLINE
            )
        assert adopted == [iid]
        adopted_here = True
        assert (
            manager._spawn_diagnostics[iid]["profile_selection"]["profile_role"]
            == "default"
        )

        # Production: the owner the adoption just stamped is a LIVE backend.
        with patch.object(
            tool_runtime.process_cleanup,
            "_owner_backend_alive",
            side_effect=lambda pid, _created: pid == os.getpid(),
        ):
            result = await spawn(headless=True, **sandbox_kwargs())
        clone_id = result["instance_id"]
        clone_selection = result["spawn_diagnostics"]["profile_selection"]
        clone_dir = clone_selection["user_data_dir"]
        assert str(clone_dir).startswith(str(sessions)), clone_dir

        assert "reattach_declined" not in result["spawn_diagnostics"], result
        assert clone_selection["seeded_via"] == cookie_handoff.VIA_CDP, clone_selection
        assert "cookie_handoff_error" not in clone_selection, clone_selection

        assert (
            clone_selection["cookies_carried"] + clone_selection["cookies_rejected"]
            == clone_selection["cookies_read"]
        ), clone_selection
        if "__Host-f950-loopback" in source_names:
            # Measured on Chrome 153: it will not take back its own loopback
            # Secure cookie, and that must cost that cookie and no other.
            assert clone_selection["cookies_rejected"] == 1, clone_selection
        if "__Host-f950-loopback" in source_names:
            # Measured on Chrome 153: it will not take back its own loopback
            # Secure cookie, and that must cost that cookie and no other.
            assert clone_selection["cookies_rejected"] == 1, clone_selection

        await navigate_and_settle(clone_id, f"{fixture_app_server}/index.html")
        header = await _cookie_header(clone_id)
        for value in (session_value, persist_value):
            assert value in header, "a login made in the master did not reach the clone"

        clone_jar = await cookie_handoff.read_jar(
            manager._instances[clone_id]["browser"]
        )
        landed = {cookie.name: cookie for cookie in clone_jar}
        assert landed["__Host-f950"].value == host_value
        assert landed["f950_chips"].value == chips_value
        assert landed["f950_chips"].partition_key is not None
        assert landed["f950_session"].expires <= 0, "a session cookie stayed one"
    finally:
        for instance in (clone_id, iid):
            if instance:
                with contextlib.suppress(Exception):
                    await close(instance_id=instance)
        if not adopted_here and chrome_pid:
            with contextlib.suppress(Exception):
                psutil.Process(chrome_pid).kill()
        await released(master)
        if clone_dir:
            await released(Path(clone_dir))
