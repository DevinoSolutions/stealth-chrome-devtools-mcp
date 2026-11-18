"""The exact-string secret registry (T-19366) and the three sinks it guards:
Sentry events, log records and the debug ring.

The random 24-character values below are built per test and never written down:
a test that hard-coded a password would put one in the repo.
"""

from __future__ import annotations

import copy
import json
import logging
import pickle
import secrets
import urllib.parse
from pathlib import Path

import pytest

from stealth_chrome_devtools_mcp import observability
from stealth_chrome_devtools_mcp.embedded import logging_setup, secret_registry
from stealth_chrome_devtools_mcp.embedded.debug_logger import DebugLogger


@pytest.fixture(autouse=True)
def _empty_registry():
    secret_registry.clear()
    yield
    secret_registry.clear()


@pytest.fixture
def factory():
    """The ONE record factory ``configure_logging`` installs, put back after."""
    before = logging.getLogRecordFactory()
    logging_setup.install_payload_arg_redaction()
    yield
    logging.setLogRecordFactory(before)


@pytest.fixture
def value() -> str:
    """A password with characters every encoding changes."""
    return "pa ss/" + secrets.token_urlsafe(12) + "&w=x+y"


def test_redact_masks_a_registered_value(value):
    secret_registry.register(value)
    assert secret_registry.redact(f"login with {value} now") == (
        f"login with {secret_registry.MASK} now"
    )


def test_redact_masks_every_url_encoded_form(value):
    secret_registry.register(value)
    forms = {
        urllib.parse.quote(value),
        urllib.parse.quote(value, safe=""),
        urllib.parse.quote_plus(value),
    }
    assert len(forms) >= 2
    for form in forms:
        out = secret_registry.redact(f"POST /login?p={form}&x=1")
        assert form not in out
        assert secret_registry.MASK in out


def test_redact_masks_the_json_escaped_form():
    quoted = 'he said "hi" \\ there ' + secrets.token_hex(4)
    secret_registry.register(quoted)
    body = json.dumps({"password": quoted})
    assert quoted not in body
    assert secret_registry.MASK in secret_registry.redact(body)


def test_redact_leaves_other_text_alone(value):
    secret_registry.register(value)
    text = "nothing to see here: GET /index.html 200"
    assert secret_registry.redact(text) == text


def test_the_longer_secret_wins_when_one_contains_the_other():
    short = secrets.token_hex(4)
    long = short + secrets.token_hex(4)
    secret_registry.register(short)
    secret_registry.register(long)
    out = secret_registry.redact(f"a {long} b")
    assert out == f"a {secret_registry.MASK} b"


def test_values_shorter_than_four_characters_are_ignored():
    secret_registry.register("abc")
    secret_registry.register("")
    assert secret_registry.redact("abc and more abc") == "abc and more abc"


def test_four_characters_are_registered():
    secret_registry.register("wxyz")
    assert secret_registry.redact("a wxyz b") == f"a {secret_registry.MASK} b"


def test_clear_forgets_everything(value):
    secret_registry.register(value)
    secret_registry.clear()
    assert secret_registry.redact(value) == value


def test_an_empty_registry_returns_the_same_string_object():
    text = "x" * 50
    assert secret_registry.redact(text) is text


def test_secret_never_renders(value):
    secret = secret_registry.Secret("x" * 8)
    assert repr(secret) == "***"
    assert str(secret) == "***"
    assert f"{secret}" == "***"
    assert f"{secret!r}" == "***"
    assert "***" in repr({"k": secret})
    assert secret.reveal() == "x" * 8


def test_a_secret_cannot_be_pickled_or_copied_into_a_dump():
    secret = secret_registry.Secret("x" * 8)
    with pytest.raises(TypeError):
        pickle.dumps(secret)
    with pytest.raises(TypeError):
        copy.deepcopy(secret)


def test_redact_value_walks_containers(value):
    secret_registry.register(value)
    original = {"a": [f"x {value}", ("y", value)], "n": 3, "k": {"deep": value}}
    out = secret_registry.redact_value(original)
    assert value not in json.dumps(out)
    assert out["n"] == 3
    assert value in json.dumps(original)  # the input is not mutated


def test_redact_value_returns_the_same_object_when_nothing_matches(value):
    secret_registry.register(value)
    payload = {"a": ["clean", 1], "b": None}
    assert secret_registry.redact_value(payload) is payload


# ---------------------------------------------------------------- Sentry


def test_a_sentry_event_is_masked_in_message_exception_and_breadcrumbs(value):
    secret_registry.register(value)
    event = {
        "message": f"failed for {value}",
        "logentry": {"message": "pw=%s", "params": [value]},
        "exception": {"values": [{"type": "ValueError", "value": f"bad {value}"}]},
        "breadcrumbs": {
            "values": [{"message": f"typed {value}", "data": {"v": value}}]
        },
    }
    out = observability._scrub_event(copy.deepcopy(event))
    assert out is not None
    assert value not in json.dumps(out)
    assert secret_registry.MASK in json.dumps(out)


def test_a_sentry_event_is_masked_in_its_url_encoded_form(value):
    secret_registry.register(value)
    encoded = urllib.parse.quote_plus(value)
    event = {"message": f"GET /cb/{encoded}/done"}
    out = observability._scrub_event(event)
    assert out is not None
    assert encoded not in json.dumps(out)


# ---------------------------------------------------------------- logging


def _formatted(caplog, logger_name="stealth.test_secret_registry"):
    return "\n".join(r.getMessage() for r in caplog.records if r.name == logger_name)


def test_a_log_record_is_masked_in_its_message(caplog, value, factory):
    secret_registry.register(value)
    with caplog.at_level(logging.INFO):
        message = f"typed {value}"
        logging.getLogger("stealth.test_secret_registry").info(message)
    assert value not in _formatted(caplog)
    assert secret_registry.MASK in _formatted(caplog)


def test_a_log_record_is_masked_in_its_args(caplog, value, factory):
    secret_registry.register(value)
    with caplog.at_level(logging.INFO):
        logging.getLogger("stealth.test_secret_registry").info(
            "typed %s into %s", value, "field"
        )
    out = _formatted(caplog)
    assert value not in out
    assert "into field" in out


def test_a_log_record_is_masked_when_the_secret_is_inside_an_argument_object(
    caplog, value, factory
):
    secret_registry.register(value)
    with caplog.at_level(logging.INFO):
        logging.getLogger("stealth.test_secret_registry").info(
            "payload %s", {"password": value}
        )
    assert value not in _formatted(caplog)


def test_a_log_record_with_numeric_args_still_formats(caplog, value, factory):
    secret_registry.register(value)
    with caplog.at_level(logging.INFO):
        logging.getLogger("stealth.test_secret_registry").info("n=%d s=%s", 7, "ok")
    assert "n=7 s=ok" in _formatted(caplog)


def test_log_masking_stops_after_clear(caplog, value, factory):
    secret_registry.register(value)
    secret_registry.clear()
    with caplog.at_level(logging.INFO):
        message = f"typed {value}"
        logging.getLogger("stealth.test_secret_registry").info(message)
    assert value in _formatted(caplog)


# ---------------------------------------------------------------- debug ring


def test_the_debug_ring_is_masked(value):
    secret_registry.register(value)
    log = DebugLogger()
    log.log_info("comp", "meth", f"typed {value}", data={"v": value})
    log.log_warning("comp", "meth", f"warn {value}", context={"v": value})
    log.log_error("comp", "meth", ValueError(f"bad {value}"), context={"v": [value]})
    view = json.dumps(log.get_debug_view(), default=str)
    assert value not in view
    assert secret_registry.MASK in view


def test_the_debug_ring_is_masked_in_a_tool_failure(value):
    secret_registry.register(value)
    log = DebugLogger()
    log.log_tool_failure("some_tool", ValueError(f"bad {value}"))
    assert value not in json.dumps(log.get_debug_view(), default=str)


def test_the_debug_ring_is_masked_in_its_export(value, tmp_path):
    secret_registry.register(value)
    log = DebugLogger()
    log.log_info("comp", "meth", f"typed {value}")
    log.log_warning("comp", "meth", f"warn {value}")
    target = tmp_path / "ring.json"
    written = log.export_to_file_paginated(str(target), fmt="json")
    text = Path(written).read_text(encoding="utf-8")
    assert value not in text
    assert secret_registry.MASK in text
