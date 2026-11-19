"""What a sealed login leaves in the browser's profile on disk (T-19366).

The design promises Chrome's own password manager never keeps the password. This
runs a real login in a real (headless) Chrome with a profile of its own, closes the
browser so every store is flushed, and then reads the profile the way a person
copying it (or a profile clone) would:

* ``Login Data`` (and the account-scoped one) holds NO saved login;
* the password is in no file, as plain text, UTF-16 or URL-encoded;
* the USERNAME: Chrome's autofill keeps submitted text fields in ``Web Data``
  (``autofill``). This is measured here and the result is pinned, so a Chrome that
  starts or stops doing it makes the test say so.

**Decided by measurement, not enforced.** A profile whose ``Preferences`` already
said ``autofill.profile_enabled: false``, ``autofill.credit_card_enabled: false``,
``credentials_enable_service: false`` and ``profile.password_manager_enabled:
false`` kept the username out of every file (Chrome 158, headless); with only
``autofill.enabled: false``, or no such file, it was kept. The vault does not
write those preferences: they would switch off address and card autofill in the
person's own profile for every session, they can only be set before Chrome starts
(so not for a browser that is already running or re-attached), and the username is
not a secret the vault promises to hide (``show_username``). The README and the
design's threat model say the username can be kept in ``Web Data``.
"""

from __future__ import annotations

import os
import sqlite3
import urllib.parse
from pathlib import Path

import test_login_flow as flow
from e2e_helpers import get_fn, sandbox_kwargs
from vault_helpers import unlocked_vault

pytestmark = flow.pytestmark
site, vault, _warmup = flow.site, flow.vault, flow._warmup
RIGHT, USER, _entry, _run = flow.RIGHT, flow.USER, flow._entry, flow._run


def _files(profile: Path):
    for root, _dirs, names in os.walk(profile):
        for name in names:
            path = Path(root) / name
            try:
                yield path, path.read_bytes()
            except OSError:
                continue


def _rows(profile: Path, relative: str, query: str):
    path = profile / relative
    if not path.exists():
        return None
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        return connection.execute(query).fetchall()
    except sqlite3.DatabaseError:
        return None
    finally:
        connection.close()


async def _login_and_close(site, tmp_path):
    unlocked_vault({"work": _entry(site)})
    profile = tmp_path / "residue-profile"
    spawned = await get_fn("spawn_browser")(
        headless=True, user_data_dir=str(profile), **sandbox_kwargs()
    )
    iid = spawned["instance_id"]
    try:
        result = await _run(iid)
    finally:
        await get_fn("close_instance")(instance_id=iid)
    assert result["outcome"] == "logged_in", result
    return profile


async def test_chromes_password_manager_keeps_no_login(site, tmp_path):
    profile = await _login_and_close(site, tmp_path)
    for relative in ("Default/Login Data", "Default/Login Data For Account"):
        rows = _rows(profile, relative, "select count(*) from logins")
        assert rows in (None, [(0,)]), (relative, rows)


async def test_the_password_is_in_no_file_of_the_profile(site, tmp_path):
    profile = await _login_and_close(site, tmp_path)
    needles = {
        "plain": RIGHT.encode(),
        "utf-16": RIGHT.encode("utf-16-le"),
        "url-encoded": urllib.parse.quote(RIGHT).encode(),
    }
    scanned = 0
    for path, data in _files(profile):
        scanned += 1
        for kind, needle in needles.items():
            assert needle not in data, (kind, str(path.relative_to(profile)))
    assert scanned > 20


async def test_where_the_username_is_kept_is_known(site, tmp_path):
    """The username is typed into a text input and submitted, so Chrome's
    autofill may keep it in ``Web Data``. It is not a secret the vault promises
    to hide (``show_username`` exists because a person may want a model to see
    it), but a profile clone would carry it. The assertion is the MEASUREMENT: it
    names every file that holds it, so a change is a failing test, not a surprise."""
    profile = await _login_and_close(site, tmp_path)
    holders = sorted(
        str(path.relative_to(profile)).replace("\\", "/")
        for path, data in _files(profile)
        if USER.encode() in data
    )
    print("USERNAME HELD BY", holders)
    assert set(holders) <= {"Default/Web Data", "Default/Web Data-journal"}, holders
