"""A child process is created OFF the backend's event loop on Windows (T-15385).

THE one home for how the backend spawns a subprocess without stopping every
session while Windows creates it.

**The defect.** On Windows the backend serves on ``ProactorEventLoop``, and its
``_make_subprocess_transport`` builds ``_WindowsSubprocessTransport``, whose
``_start`` calls ``windows_utils.Popen`` — i.e. ``CreateProcess`` — synchronously,
ON THE LOOP THREAD. Every ``asyncio.create_subprocess_exec`` in the backend goes
through it: nodriver's ``Browser.start`` for every Chrome launch, and the idle
canary ``scheduling_lag`` spawns for each health round. Measured on the fleet PC
(~1,500 processes, real-time antivirus) on 2026-11-07: each call held the loop
for 0.15-2.0 s, for chrome.exe and python.exe alike, and with #394's psutil poll
fixed, all ten remaining stalls over 1 s in a 150 s frozen-Chrome load had the
loop thread in ``subprocess.Popen._execute_child``. On a backend shared by ~25
sessions, every one of them waits through every other session's launch.

**The seam.** ``_make_subprocess_transport`` is a coroutine already, so the
replacement awaits the ``Popen`` on a dedicated thread and then builds the SAME
transport class around the process it got, through a ``_start`` that adopts the
process instead of creating it. Everything after creation — the exit wait on the
proactor, the pipe connections, the protocol callbacks, ``close``/``_wait`` on a
failed start — is the stock code, unchanged. ``_WindowsSubprocessTransport._start``
is identical in CPython 3.11-3.14 and ``_make_subprocess_transport`` in 3.11-3.13
(compared); 3.14 only wraps the failed-start ``transp._wait()`` in ``shield``,
which the replacement does on every version. A future CPython that moves the
spawn off the loop makes this a harmless detour.

**Why a thread is safe here.** ``_winapi.CreateProcess`` releases the GIL, which
is what lets the loop turn meanwhile. ``windows_utils.Popen`` passes the child's
std handles explicitly with ``close_fds=True``, which on Windows uses
``PROC_THREAD_ATTRIBUTE_HANDLE_LIST``, so two spawns racing on two threads cannot
leak each other's pipe handles into the wrong child. A dedicated thread per spawn
rather than the loop's default executor, so a launch never queues behind
whatever blocking work that pool is already doing.

**Cancellation.** A caller cancelled while ``CreateProcess`` runs cannot stop it;
the process still appears. The replacement then kills it and closes its pipes the
moment it exists, so a cancelled launch leaves no child — the stock path, which
cannot be cancelled mid-spawn at all, never had this case.

**Installed once, for the backend only**, beside ``proactor_accept.install``:
idempotent (a marker on the replacement) and a no-op off Windows. The stdio proxy
spawns the backend with plain ``subprocess``, not through a loop, so it is not
involved.

A leaf: stdlib only; imports no other embedded module.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import functools
import logging
import sys
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import subprocess
    from asyncio import base_subprocess
    from collections.abc import Sequence
    from typing import IO

_logger = logging.getLogger("stealth.backend")

_MARK = "_stealth_spawn_off_loop"
#: The two private stdlib names this replaces and reuses, named once.
_METHOD = "_make_subprocess_transport"
_TRANSPORT = "_WindowsSubprocessTransport"


def _spawn_in_thread(
    popen: functools.partial[subprocess.Popen[bytes]],
) -> concurrent.futures.Future[subprocess.Popen[bytes]]:
    """Run ``popen()`` on a fresh daemon thread; its result or error lands in
    the returned future."""
    done: concurrent.futures.Future[subprocess.Popen[bytes]] = (
        concurrent.futures.Future()
    )

    def run() -> None:
        try:
            done.set_result(popen())
        except BaseException as exc:  # noqa: BLE001  PERMANENT(handed to the awaiting coroutine)
            done.set_exception(exc)

    threading.Thread(target=run, name="stealth-spawn", daemon=True).start()
    return done


def _discard(done: concurrent.futures.Future[subprocess.Popen[bytes]]) -> None:
    """A spawn whose caller was cancelled: kill the child and close its pipes."""
    if done.cancelled() or done.exception() is not None:
        return
    proc = done.result()
    with contextlib.suppress(OSError):
        proc.kill()
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            with contextlib.suppress(OSError):
                pipe.close()
    _logger.info("killed child pid %d: its launch was cancelled mid-spawn", proc.pid)


def _adopting(
    transport_class: type[base_subprocess.BaseSubprocessTransport],
) -> type[base_subprocess.BaseSubprocessTransport]:
    """``transport_class`` with a ``_start`` that adopts an existing process.

    The stock ``_start`` is two statements: create the process, then ask the
    proactor to report its exit. The first becomes "take the process already
    made"; the second is copied verbatim (four lines, identical in 3.11-3.14),
    because reaching it through ``super()`` would mean swapping the module's
    ``Popen`` for the duration, which another thread could observe.
    """

    class _Adopting(transport_class):
        def _start(self, *_args, _spawned, **_kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202  PERMANENT(a private stdlib signature)
            self._proc = _spawned

            def callback(_f):  # noqa: ANN001, ANN202  PERMANENT(a future)
                self._process_exited(self._proc.poll())

            f = self._loop._proactor.wait_for_handle(int(self._proc._handle))
            f.add_done_callback(callback)

    _Adopting.__name__ = transport_class.__name__
    return _Adopting


def _replacement(original_transport: type[base_subprocess.BaseSubprocessTransport]):  # noqa: ANN202  PERMANENT(a private stdlib method)
    adopting = _adopting(original_transport)

    async def _make_subprocess_transport(  # noqa: PLR0913  PERMANENT(a private stdlib signature)
        self: asyncio.AbstractEventLoop,
        protocol: asyncio.SubprocessProtocol,
        args: str | bytes | Sequence[str | bytes],
        shell: bool,
        stdin: int | IO[bytes] | None,
        stdout: int | IO[bytes] | None,
        stderr: int | IO[bytes] | None,
        bufsize: int,
        extra: dict[str, object] | None = None,
        **kwargs: object,
    ) -> asyncio.SubprocessTransport:
        from asyncio import windows_utils

        popen = functools.partial(
            windows_utils.Popen,
            args,
            shell=shell,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            bufsize=bufsize,
            **kwargs,
        )
        done = _spawn_in_thread(popen)
        try:
            # Shielded: a cancellation must not cancel ``done`` itself, which
            # would lose the process the thread is still creating.
            proc = await asyncio.shield(asyncio.wrap_future(done))
        except asyncio.CancelledError:
            done.add_done_callback(_discard)
            raise
        waiter = self.create_future()
        transp = adopting(
            self,
            protocol,
            args,
            shell,
            stdin,
            stdout,
            stderr,
            bufsize,
            waiter=waiter,
            extra=extra,
            _spawned=proc,
        )
        try:
            await waiter
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException:
            transp.close()
            await asyncio.shield(transp._wait())
            raise
        return transp

    setattr(_make_subprocess_transport, _MARK, True)
    return _make_subprocess_transport


def install() -> bool:
    """Make the Proactor loop create child processes off the loop thread.
    Returns whether it patched now (False off Windows, or when already
    installed)."""
    if sys.platform != "win32":
        return False
    from asyncio import windows_events

    loop_class = windows_events.ProactorEventLoop
    if getattr(getattr(loop_class, _METHOD), _MARK, False):
        return False
    setattr(loop_class, _METHOD, _replacement(getattr(windows_events, _TRANSPORT)))
    return True
