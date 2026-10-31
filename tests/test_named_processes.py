"""Pins for ``browser_cmdline.named_processes``, the one walk of the process table (T-11618).

The defect: ``psutil.process_iter(["name"])`` reads the name inside the
generator, so a ``SystemError`` from one process (macOS, psutil 7.2.2, where
``name()`` falls back to ``cmdline()``) ended the walk and failed
``spawn_browser``. No ``try`` in a caller's loop body can catch that.

The doubles are ``fakes.PsutilProc`` / ``psutil_process_iter``, which read
requested attrs while they iterate the way psutil does; the first node pins
that premise against the real library so the doubles cannot drift from it.
"""

import os

import psutil
import pytest

from fakes import PsutilProc, psutil_process_iter
from stealth_chrome_devtools_mcp.embedded import browser_cmdline


def test_real_as_dict_lets_a_system_error_out_of_the_walk(monkeypatch):
    """The premise, against real psutil: ``as_dict`` (which ``process_iter``
    runs for each process when given attrs) absorbs ``AccessDenied`` but not
    ``SystemError``. Reads our own process only."""
    me = psutil.Process(os.getpid())
    monkeypatch.setattr(
        psutil.Process, "name", lambda self: (_ for _ in ()).throw(SystemError("x"))
    )
    with pytest.raises(SystemError):
        me.as_dict(attrs=["name"])
    monkeypatch.setattr(
        psutil.Process,
        "name",
        lambda self: (_ for _ in ()).throw(psutil.AccessDenied(pid=1)),
    )
    assert me.as_dict(attrs=["name"], ad_value="denied") == {"name": "denied"}


def test_walk_asks_for_no_attrs(monkeypatch):
    """Nothing may be read inside psutil's generator, where it cannot be skipped."""
    asked = []

    def process_iter(*args, **kwargs):
        asked.append((args, kwargs))
        return iter([])

    monkeypatch.setattr(psutil, "process_iter", process_iter)
    assert list(browser_cmdline.named_processes()) == []
    assert asked == [((), {})]


@pytest.mark.parametrize(
    "error",
    [
        SystemError("proc_cmdline"),
        OSError("EIO"),
        psutil.AccessDenied(pid=3),
        psutil.NoSuchProcess(3),
        psutil.ZombieProcess(3),
    ],
    ids=lambda error: type(error).__name__,
)
def test_a_process_whose_name_cannot_be_read_is_skipped(monkeypatch, error):
    procs = [
        PsutilProc("first", pid=1),
        PsutilProc("broken", pid=2, name_error=error),
        PsutilProc("last", pid=3),
    ]
    monkeypatch.setattr(psutil, "process_iter", psutil_process_iter(procs))
    assert [(p.pid, name) for p, name in browser_cmdline.named_processes()] == [
        (1, "first"),
        (3, "last"),
    ]


def test_the_walk_reads_no_cmdline(monkeypatch):
    """The cheap name only: ``cmdline`` is one PEB read per process on Windows."""
    boom = AssertionError("named_processes must not read cmdline")
    procs = [PsutilProc("a", pid=1, cmdline_error=boom)]
    monkeypatch.setattr(psutil, "process_iter", psutil_process_iter(procs))
    assert [name for _, name in browser_cmdline.named_processes()] == ["a"]


def test_an_empty_name_is_an_empty_string(monkeypatch):
    procs = [PsutilProc("", pid=1)]
    monkeypatch.setattr(psutil, "process_iter", psutil_process_iter(procs))
    assert [name for _, name in browser_cmdline.named_processes()] == [""]
