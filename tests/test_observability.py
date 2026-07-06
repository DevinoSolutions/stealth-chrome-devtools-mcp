"""Tests for the Sentry error-shipping module (off by default, DSN-gated)."""

import builtins
from unittest.mock import patch

import pytest

from stealth_chrome_devtools_mcp import observability

_FAKE_DSN = "https://public@o0.ingest.sentry.io/123"


def test_sentry_init_is_noop_without_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    # No DSN configured -> Sentry stays OFF (the default for this local tool).
    assert observability.sentry_init() is False


def test_sentry_init_initializes_when_dsn_set(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", _FAKE_DSN)
    captured = {}

    def _fake_init(**kwargs):
        captured.update(kwargs)

    # Patch only the external SDK boundary so no real client/transport spins up;
    # assert the wiring (dsn, release, integrations) is correct.
    with patch("sentry_sdk.init", _fake_init):
        assert observability.sentry_init() is True

    assert captured["dsn"] == _FAKE_DSN
    assert captured["release"] is not None  # resolved from the installed package
    names = {type(i).__name__ for i in captured["integrations"]}
    assert "LoggingIntegration" in names
    assert "AsyncioIntegration" in names


def test_sentry_init_raises_if_dsn_set_but_sdk_missing(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", _FAKE_DSN)
    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name.startswith("sentry_sdk"):
            raise ImportError("no sentry_sdk")
        return real_import(name, *args, **kwargs)

    # Opting into error reporting and then getting silence is worse than a loud,
    # actionable failure -> a set DSN without the extra installed must raise.
    with (
        patch("builtins.__import__", _blocked_import),
        pytest.raises(RuntimeError, match="sentry"),
    ):
        observability.sentry_init()
