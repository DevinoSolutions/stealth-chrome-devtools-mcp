"""The evidence a failed gate node hands over (F-859 §7.3, §9-§11).

``release_gate_harness._backend_logs`` is what every W13 node appends to its
failure when the exception text alone cannot explain it. It used to return the
two NEWEST ``*.log`` files in the workspace — right for the single-client
journey (one backend log, one proxy log) and wrong for the 12-proxy herd, where
the two newest files are always two proxy logs: three wedge reports in a row
(F-859 §9, §10 and the integration cell of §11) carried no backend log at all,
and §9 read that absence as a datum about the workspace. The backend's own log
is the one file those reports existed to capture, so it can never be crowded
out by the number of proxies that were talking to it.

The proxy side is a DIGEST, not a dump: a herd's twelve proxies write mostly
DEBUG probe noise, and the lines that decide "one connection dropped" from
"every proxy lost the backend at the same second" are their WARNING+ lines.
"""

from __future__ import annotations

import os
import time

from release_gate_harness import (
    _PROXY_DIGEST_LINES,
    _backend_logs,
    _proxy_warnings,
    workspace_backend_logs,
    workspace_proxy_warnings,
)


def _write(path, text: str, mtime: float) -> None:
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _herd_workspace(log_dir, proxies: int = 12) -> float:
    """A herd's log directory: boot log, one backend log, then N newer proxy logs."""
    t0 = time.time() - 600
    _write(log_dir / "backend-boot.log", "boot: uvicorn starting\n", t0)
    _write(
        log_dir / "backend-4242.log",
        "2026-09-11 22:53:10,000 INFO 4242 [-] stealth.backend: serving on 55079\n",
        t0 + 1,
    )
    for i in range(proxies):
        _write(
            log_dir / f"proxy-{100 + i}.log",
            f"2026-09-11 22:53:{20 + i:02d},000 DEBUG {100 + i} [-] stealth.proxy: probe\n",
            t0 + 10 + i,
        )
    return t0


def test_backend_logs_survive_a_herd_of_newer_proxy_logs(tmp_path):
    _herd_workspace(tmp_path)
    out = _backend_logs(tmp_path)
    assert "[backend-4242.log]" in out
    assert "serving on 55079" in out
    assert "[backend-boot.log]" in out


def test_the_newest_proxy_logs_still_follow_the_backend_logs(tmp_path):
    _herd_workspace(tmp_path)
    out = _backend_logs(tmp_path)
    assert out.index("[backend-4242.log]") < out.index("[proxy-111.log]")
    assert "[proxy-110.log]" in out
    # A dozen raw proxy logs would drown the report; the digest carries the rest.
    assert "[proxy-100.log]" not in out


def test_a_single_client_journey_still_shows_both_of_its_logs(tmp_path):
    t0 = time.time() - 60
    _write(tmp_path / "backend-77.log", "backend line\n", t0)
    _write(tmp_path / "proxy-78.log", "proxy line\n", t0 + 1)
    out = _backend_logs(tmp_path)
    assert "[backend-77.log]" in out
    assert "[proxy-78.log]" in out


def test_proxy_warning_digest_names_every_proxy_that_warned(tmp_path):
    t0 = _herd_workspace(tmp_path, proxies=3)
    _write(
        tmp_path / "proxy-100.log",
        "2026-09-11 22:53:20,000 DEBUG 100 [-] stealth.proxy: probe\n"
        "2026-09-11 22:53:30,000 WARNING 100 [-] stealth.proxy: backend connection lost\n"
        "2026-09-11 22:53:31,000 ERROR 100 [-] stealth.proxy: backend did not become ready within 120s\n",
        t0 + 20,
    )
    _write(
        tmp_path / "proxy-102.log",
        "2026-09-11 22:53:22,000 INFO 102 [-] stealth.proxy: healed\n"
        "2026-09-11 22:53:33,000 CRITICAL 102 [-] stealth.proxy: giving up\n",
        t0 + 22,
    )
    out = _proxy_warnings(tmp_path)
    assert "[proxy-100.log]" in out
    assert "backend connection lost" in out
    assert "did not become ready within 120s" in out
    assert "[proxy-102.log]" in out
    assert "giving up" in out
    # A proxy with nothing above INFO is not in the digest, and INFO/DEBUG
    # lines of the ones that are do not pad it.
    assert "[proxy-101.log]" not in out
    assert "probe" not in out
    assert "healed" not in out


def test_proxy_warning_digest_is_capped_and_says_so(tmp_path):
    t0 = time.time() - 60
    body = "".join(
        f"2026-09-11 22:53:{i % 60:02d},000 WARNING 5 [-] stealth.proxy: strike {i}\n"
        for i in range(_PROXY_DIGEST_LINES * 3)
    )
    _write(tmp_path / "proxy-5.log", body, t0)
    out = _proxy_warnings(tmp_path)
    kept = [line for line in out.splitlines() if "strike" in line]
    assert len(kept) == _PROXY_DIGEST_LINES
    # The LAST lines are the ones kept: the end of a wedge is where the verdict is.
    assert f"strike {_PROXY_DIGEST_LINES * 3 - 1}" in out
    assert f"(+{_PROXY_DIGEST_LINES * 2} earlier warning line(s) elided)" in out


def test_an_empty_workspace_says_so_instead_of_returning_nothing(tmp_path):
    space = {"log_dir": tmp_path / "logs", "home_dir": tmp_path / "home"}
    space["log_dir"].mkdir()
    assert workspace_backend_logs(space) == "(no backend log files found)"
    assert workspace_proxy_warnings(space) == "(no proxy warnings)"
