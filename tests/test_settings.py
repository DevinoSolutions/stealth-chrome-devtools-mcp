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
    "SENTRY_DSN",
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
    assert s.session_storage_cap_gb == 20.0
    assert s.clone_storage_cap_gb == 10.0
    assert s.clone_trash_retention_hours == 24.0
    assert s.browser_idle_timeout == 0  # 0 = idle reaping disabled (never auto-close)
    assert s.browser_idle_reaper_interval == 60
    assert s.port == 8000
    assert s.no_auto_recovery is False
    assert s.xpool_safe_mode is False
    assert s.sentry_dsn is None


def test_bad_value_names_the_field(monkeypatch):
    monkeypatch.setenv("STEALTH_MCP_SESSION_STORAGE_CAP_GB", "not-a-number")
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert "session_storage_cap_gb" in str(excinfo.value)


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


def test_env_example_documents_every_field():
    repo_root = Path(__file__).resolve().parent.parent
    example = (repo_root / ".env.example").read_text(encoding="utf-8")
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias
        env_name = alias if isinstance(alias, str) else f"STEALTH_MCP_{name}".upper()
        assert env_name in example, f"{env_name} is not documented in .env.example"
