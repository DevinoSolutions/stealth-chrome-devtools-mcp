"""Pinning tests for M10a-7c: proxy_utils.py + proxy_forwarder.py +
platform_utils.py silent excepts now log (F-181 rows 11-14). See
test_silent_excepts_log.py (7a) for the fixture/rationale this file
continues.

proxy_utils.py:104,119 are classified WARNING (a genuinely malformed
launch-arg URL that fails to parse is worth surfacing) with a SECURITY
CONSTRAINT the brief calls out explicitly: log only the exception TYPE,
never str(e) or the raw arg/value - urlsplit's exception messages are not
contractually guaranteed never to echo back fragments of the malformed
input it failed on, and the whole point of redact_launch_arg is that the
un-redacted value (which may embed proxy credentials) must never reach a
log sink. Un-redacted user/pass is exactly the input this function exists
to protect.

proxy_forwarder.py:413 and platform_utils.py:323 are classified DEBUG
(per-event stream relay teardown / a single browser-name probe failing
during executable discovery - routine, must stay droppable by default).
"""

import logging

import pytest


@pytest.fixture()
def captured_backend_records():
    records = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("stealth.backend")
    handler = _ListHandler()
    logger.addHandler(handler)
    prior_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)


# ---------------------------------------------------------------------------
# 7c: proxy_utils.py (WARNING - exception TYPE only, NEVER the raw value)
# ---------------------------------------------------------------------------


class TestProxyUtilsSilentExcepts:
    def test_redact_proxy_server_arg_parse_failure_logs_type_only(
        self, captured_backend_records
    ):
        from proxy_utils import redact_launch_arg

        malformed = "--proxy-server=http://user:secret@[bad-ipv6"
        out = redact_launch_arg(malformed)

        assert out == "--proxy-server=<redacted>"
        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.WARNING
        message = record.getMessage()
        # The security property under test: the un-redacted value (which may
        # embed real proxy credentials) must NEVER reach the log sink.
        assert "user" not in message
        assert "secret" not in message
        assert "bad-ipv6" not in message
        assert "ValueError" in message

    def test_redact_bare_url_parse_failure_logs_type_only(
        self, captured_backend_records
    ):
        from proxy_utils import redact_launch_arg

        malformed = "http://user:secret@[bad-ipv6@host/path"
        out = redact_launch_arg(malformed)

        assert out == "<redacted>"
        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.WARNING
        message = record.getMessage()
        assert "user" not in message
        assert "secret" not in message
        assert "bad-ipv6" not in message
        assert "ValueError" in message


# ---------------------------------------------------------------------------
# 7c: proxy_forwarder.py (DEBUG - routine stream-relay teardown)
# ---------------------------------------------------------------------------


class TestProxyForwarderSilentExcepts:
    @pytest.mark.asyncio
    async def test_pipe_unexpected_error_logs_at_debug(self, captured_backend_records):
        import asyncio

        from proxy_forwarder import AuthenticatedProxyForwarder

        class _BoomReader:
            async def read(self, _n):
                raise ValueError("relay-teardown-fail")

        reader = _BoomReader()

        class _NoopWriter:
            def write(self, _data):
                pass

            async def drain(self):
                pass

        writer = _NoopWriter()
        event = asyncio.Event()

        await AuthenticatedProxyForwarder.pipe(reader, writer, event)

        assert event.is_set()
        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.DEBUG
        assert "relay-teardown-fail" in record.getMessage()


# ---------------------------------------------------------------------------
# 7c: platform_utils.py (DEBUG - a single browser-name probe failing)
# ---------------------------------------------------------------------------


class TestPlatformUtilsSilentExcepts:
    def test_check_browser_executable_probe_failure_logs_at_debug(
        self, captured_backend_records, monkeypatch
    ):
        import shutil

        import platform_utils

        # Force the static-path pre-check (platform_utils.py:295-301) to miss on
        # every candidate so execution actually reaches the shutil.which
        # fallback loop this test targets - otherwise a real local Chrome
        # install short-circuits via the static-path branch first.
        monkeypatch.setattr(platform_utils.Path, "is_file", lambda self: False)

        def _boom_which(_name):
            raise OSError("shutil.which probe failed")

        monkeypatch.setattr(shutil, "which", _boom_which)

        result = platform_utils.check_browser_executable()

        assert result is None
        # One probe failure is logged per candidate browser name tried.
        assert len(captured_backend_records) >= 1
        assert all(r.levelno == logging.DEBUG for r in captured_backend_records)
        assert all(
            "shutil.which probe failed" in r.getMessage()
            for r in captured_backend_records
        )
