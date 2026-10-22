"""F-1018 follow-up (T-6367): the token file's shape and owner are checked
exactly.

* The token is EXACTLY ``secrets.token_urlsafe(32)``: 43 characters. A longer
  string used to match, so a hand-edited or appended-to file was trusted instead
  of replaced.
* A token file another principal owns is refused on Windows as it is on POSIX
  (owner SID against the current user's), and an owner that cannot be read is
  refused too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from stealth_chrome_devtools_mcp.embedded import backend_token
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

TOKEN = "A" * 43
MINE = "S-1-5-21-1-2-3-1001"
SOMEONE_ELSE = "S-1-5-21-1-2-3-1002"


def test_a_valid_43_character_token_is_accepted(tmp_path):
    path = tmp_path / "backend.token"
    path.write_text(TOKEN, encoding="ascii")
    assert backend_token.read(path) == TOKEN


@pytest.mark.parametrize("junk", ["x", "-", "_", "AAAA"])
def test_a_longer_token_is_rejected_not_trusted(tmp_path, junk):
    path = tmp_path / "backend.token"
    path.write_text(TOKEN + junk, encoding="ascii")
    assert backend_token.read(path) is None
    assert backend_token.auth_headers(path) == {}


def test_a_longer_file_is_replaced_by_ensure(tmp_path):
    path = tmp_path / "backend.token"
    path.write_text(TOKEN + "x", encoding="ascii")
    fresh = backend_token.ensure(path)
    assert len(fresh) == 43
    assert backend_token.read(path) == fresh != TOKEN + "x"


@pytest.fixture
def as_windows(monkeypatch):
    """Take the Windows branch of ``_refuse_a_foreign_file`` on any host, with the
    two SID lookups stubbed (the real ones are pinned below on Windows)."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(backend_token, "_user_sid", lambda: MINE)


def test_windows_a_file_owned_by_another_sid_is_refused(
    tmp_path, monkeypatch, as_windows
):
    path = tmp_path / "backend.token"
    path.write_text(TOKEN, encoding="ascii")
    monkeypatch.setattr(backend_token, "_file_owner_sid", lambda _p: SOMEONE_ELSE)
    with pytest.raises(ToolError, match="owned by another user") as refused:
        backend_token._refuse_a_foreign_file(path)
    assert SOMEONE_ELSE in str(refused.value)
    assert TOKEN not in str(refused.value)


def test_windows_a_file_owned_by_the_current_user_is_accepted(
    tmp_path, monkeypatch, as_windows
):
    monkeypatch.setattr(backend_token, "_file_owner_sid", lambda _p: MINE)
    backend_token._refuse_a_foreign_file(tmp_path / "backend.token")


def test_windows_a_file_that_is_not_there_is_nothing_to_refuse(
    tmp_path, monkeypatch, as_windows
):
    def missing(path):
        raise FileNotFoundError(str(path))

    monkeypatch.setattr(backend_token, "_file_owner_sid", missing)
    backend_token._refuse_a_foreign_file(tmp_path / "backend.token")


def test_windows_an_owner_that_cannot_be_read_is_refused(
    tmp_path, monkeypatch, as_windows
):
    def denied(_path):
        raise OSError("GetNamedSecurityInfoW failed (error 5)")

    monkeypatch.setattr(backend_token, "_file_owner_sid", denied)
    with pytest.raises(ToolError, match="owner could not be read"):
        backend_token._refuse_a_foreign_file(tmp_path / "backend.token")


def test_windows_ensure_refuses_a_foreign_owned_token_before_reading_it(
    tmp_path, monkeypatch, as_windows
):
    path = tmp_path / "backend.token"
    path.write_text(TOKEN, encoding="ascii")
    monkeypatch.setattr(backend_token, "_file_owner_sid", lambda _p: SOMEONE_ELSE)
    with pytest.raises(ToolError, match="owned by another user"):
        backend_token.ensure(path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows security descriptors")
class TestTheRealWindowsOwnerLookup:
    def test_a_file_this_process_created_is_owned_by_the_current_user(self, tmp_path):
        path = tmp_path / "backend.token"
        backend_token.ensure(path)
        assert backend_token._file_owner_sid(path) == backend_token._user_sid()
        backend_token._refuse_a_foreign_file(path)  # and is not refused

    def test_a_missing_file_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            backend_token._file_owner_sid(tmp_path / "nope")

    def test_a_file_owned_by_a_foreign_principal_is_refused(self):
        """The real owner lookup against a real foreign owner: the Windows hosts
        file is owned by SYSTEM or TrustedInstaller, never by an ordinary user."""
        hosts = Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / (
            "System32/drivers/etc/hosts"
        )
        if not hosts.exists():
            pytest.skip("no hosts file to borrow an owner from")
        with pytest.raises(ToolError, match="owned by another user"):
            backend_token._refuse_a_foreign_file(hosts)
