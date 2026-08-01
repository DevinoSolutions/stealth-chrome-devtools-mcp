"""Tests for the pydantic-settings ``Settings`` model — the single canonical env
home (the Python equivalent of zod schema validation for ``.env``)."""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from stealth_chrome_devtools_mcp.settings import Settings, get_settings

# Env names the defaults test must ensure are unset so the field defaults show.
_DEFAULT_SENSITIVE = [
    "BROWSER_IDLE_TIMEOUT",
    "BROWSER_IDLE_REAPER_INTERVAL",
    "PORT",
    "XPOOL_SAFE_MODE",
    "DEBUG",
]


def _clear_app_env(monkeypatch):
    for key in list(os.environ):
        if key.upper().startswith("STEALTH_MCP_"):
            monkeypatch.delenv(key, raising=False)
    for key in _DEFAULT_SENSITIVE:
        monkeypatch.delenv(key, raising=False)


def test_defaults_instantiate(monkeypatch):
    _clear_app_env(monkeypatch)
    s = Settings(_env_file=None)
    assert s.browser_session_storage_cap_gb == 20.0
    assert s.clone_storage_cap_gb == 10.0
    assert s.clone_trash_retention_hours == 24.0
    assert s.browser_idle_timeout == 0  # 0 = idle reaping disabled (never auto-close)
    assert s.browser_idle_reaper_interval == 60
    assert s.port == 8000
    assert s.no_auto_recovery is False
    assert s.xpool_safe_mode is False
    # Error reporting is ON by default; the opt-out is the only knob (#55).
    assert s.no_error_reporting is False
    assert not hasattr(s, "sentry_dsn")


def test_bad_value_names_the_field(monkeypatch):
    monkeypatch.setenv("STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB", "not-a-number")
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert "browser_session_storage_cap_gb" in str(excinfo.value)


def test_unknown_prefixed_env_var_is_rejected(monkeypatch):
    monkeypatch.setenv("STEALTH_MCP_NOT_A_REAL_KEY", "1")
    with pytest.raises(Exception) as excinfo:
        Settings(_env_file=None)
    assert "STEALTH_MCP_NOT_A_REAL_KEY" in str(excinfo.value)


def test_legacy_unprefixed_alias_is_read(monkeypatch):
    monkeypatch.setenv("BROWSER_IDLE_TIMEOUT", "5")
    assert Settings(_env_file=None).browser_idle_timeout == 5


def test_host_introspection_var_is_read(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":99")
    assert Settings(_env_file=None).display == ":99"


def test_unrelated_os_env_var_is_ignored(monkeypatch):
    monkeypatch.setenv("SOME_UNRELATED_TOOL_VAR", "x")
    Settings(_env_file=None)  # must not raise


def test_get_settings_is_cached():
    assert get_settings() is get_settings()


# ---------------------------------------------------------------------------
# Host-project config absorption (issues #55 / #56)
# ---------------------------------------------------------------------------
# MCP clients launch the shared backend with cwd set to whatever project folder
# the user opened, so a cwd-relative ``env_file`` made this server read THAT
# project's application config. The pins below are the three ways that hurt.
#
# The two that instantiate ``Settings()`` for real (rather than with
# ``_env_file=None``) need the operator's own state-dir file to be absent, or
# its contents — not the defaults — are what they would measure. CI never has
# one. ``test_the_env_file_is_our_state_dir_never_the_cwd`` carries the fix
# unconditionally, so a revert is caught even where these two cannot run.
_STATE_ENV_FILE = Path.home() / ".stealth-mcp" / ".env"
_needs_no_operator_config = pytest.mark.skipif(
    _STATE_ENV_FILE.exists(),
    reason=f"{_STATE_ENV_FILE} exists: its values, not the defaults, would be read",
)


def test_the_env_file_is_our_state_dir_never_the_cwd():
    """The one line that fixes all three collisions: read OUR file, not theirs."""
    configured = Settings.model_config["env_file"]
    assert Path(configured).is_absolute(), configured
    assert Path(configured) == _STATE_ENV_FILE


@_needs_no_operator_config
def test_a_host_project_env_file_does_not_crash_settings(monkeypatch, tmp_path):
    """The #56 reproduction: an ordinary Next.js repo used to kill the backend.

    ``extra="forbid"`` applies to the whole ``.env`` FILE, so every foreign key
    in it was a fatal ``ValidationError`` — one `DATABASE_URL` in the folder the
    client happened to open took the backend down for every connected session.
    """
    _clear_app_env(monkeypatch)
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgres://x\nNEXT_PUBLIC_FOO=bar\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    Settings()  # must not raise


@_needs_no_operator_config
def test_a_host_project_env_file_is_not_absorbed_as_our_config(monkeypatch, tmp_path):
    """The silent half: foreign values must not become this server's settings.

    ``SENTRY_DSN`` (issue #55) and the un-prefixed aliases ``PORT``/``DEBUG``
    are exactly the keys a product repo puts in its own ``.env``, and each one
    used to be adopted verbatim.
    """
    _clear_app_env(monkeypatch)
    (tmp_path / ".env").write_text(
        "SENTRY_DSN=https://host-project@example.test/9\nPORT=3000\nDEBUG=true\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.port == 8000
    assert s.debug is False
    # There is no field to absorb a DSN into any more — that IS the #55 fix.
    assert "sentry_dsn" not in Settings.model_fields
    assert s.no_error_reporting is False


def test_env_example_documents_every_field():
    repo_root = Path(__file__).resolve().parent.parent
    example = (repo_root / ".env.example").read_text(encoding="utf-8")
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias
        env_name = alias if isinstance(alias, str) else f"STEALTH_MCP_{name}".upper()
        assert env_name in example, f"{env_name} is not documented in .env.example"
