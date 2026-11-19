"""Caller Python in the backend and the login vault never coexist (T-19366).

Some tools run PYTHON the caller wrote inside the backend process, with the
backend's own authority and no sandbox: ``create_python_binding`` ``exec``s it, and
``create_dynamic_hook`` ``exec``s a hook's function and ``eval``s its
``custom_condition``. That code can read everything the process holds, so with the
vault unlocked it can read the key, every entry and the password a login typed
(measured by the third review: a tools-only agent got the password, the username
and the 32-byte key in one ``create_python_binding`` call, with no browser trick).
Restricting builtins does not change this: a namespace with a handful of builtins
is escaped through any object that code is handed. So the rule is not a filter on
what the code may do; it is that the two never share a process:

1. **While the vault is unlocked, a tool in** :data:`RUNS_CALLER_PYTHON` **is
   refused** (:func:`admit`), with the sentence saying how to go on.
2. **After one has run, the vault cannot be unlocked** in this process
   (:func:`unlock_gate`, taken by ``vault_store.unlock``), whatever came first:
   code planted before an unlock could simply wait for the key. The first tool to
   have run is recorded (:func:`tainted_by`); only a backend restart clears it.
3. Hence a binding or a hook cannot exist at an unlock: with 2 none has ever been
   created in a process that holds a key.

JavaScript-only tools (``execute_script``, ``inject_and_execute_script``,
``create_persistent_function``, ``execute_python_in_browser``, which TRANSPILES to
JavaScript) run in the page, never here, and stay available.

**One home for the set.** ``tool_registry.section_tool`` calls :func:`admit` for
every tool; ``tests/test_caller_python_census.py`` reads the same set and fails
when any new ``exec`` / ``eval`` / ``compile`` / ``importlib`` site appears in
``src`` that is not pinned to a tool in it. A leaf: it imports ``vault_store`` only
inside functions.
"""

from __future__ import annotations

import contextlib
import threading
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Every tool that runs, or lets a caller choose, Python in the backend process.
#: ``create_simple_dynamic_hook`` is NOT here: its code is a fixed template with the
#: caller's values written in as string literals (``dynamic_hook_ai_interface``).
RUNS_CALLER_PYTHON = frozenset({"create_python_binding", "create_dynamic_hook"})

REFUSED_WHILE_UNLOCKED = (
    "{tool} runs your Python inside the backend process, which can read the login "
    "vault's key and passwords, so it is refused while the vault is unlocked. A "
    "person can lock the vault (`stealthy login lock`) first; once it has run, the "
    "vault cannot be unlocked again until the backend is restarted "
    "(`stealth-chrome-devtools restart`)."
)
UNLOCK_REFUSED = (
    "the login vault cannot be unlocked in this backend: {tool} has run caller "
    "Python in this process, which could read the key. Restart the backend "
    "(`stealth-chrome-devtools restart`), then unlock again."
)

_lock = threading.RLock()
_tainted_by: str | None = None


class Tainted(Exception):  # noqa: N818  PERMANENT(the name the unlock route and the CLI share)
    """The vault cannot be unlocked in this process: caller Python has run in it."""


def tainted_by() -> str | None:
    """The first tool that ran caller Python in this process, or ``None``."""
    return _tainted_by


def tainted() -> bool:
    return _tainted_by is not None


def unlock_refusal() -> str | None:
    """The sentence for a refused unlock, or ``None`` when the vault may be unlocked.
    Says which tool, never what the code was."""
    tool = _tainted_by
    return None if tool is None else UNLOCK_REFUSED.format(tool=tool)


def admit(tool: str) -> None:
    """Let ``tool`` run, or raise ``ToolError`` while the vault is unlocked.

    A tool outside :data:`RUNS_CALLER_PYTHON` is none of this module's business.
    One that is, and runs, TAINTS the process before any of its code is read, even
    when the code then fails to load: a refused or failed attempt is not the one
    to trust."""
    if tool not in RUNS_CALLER_PYTHON:
        return
    from stealth_chrome_devtools_mcp.embedded import vault_store

    global _tainted_by  # noqa: PLW0603  PERMANENT(one process-wide fact, cleared only by a restart)
    with _lock:
        if vault_store.is_unlocked():
            raise ToolError(REFUSED_WHILE_UNLOCKED.format(tool=tool))
        if _tainted_by is None:
            _tainted_by = tool


@contextlib.contextmanager
def unlock_gate() -> Iterator[None]:
    """Hold the gate while a key is put in memory: raises :class:`Tainted` when
    caller Python has run. Held with :func:`admit`'s lock, so a tool that is
    admitted and an unlock cannot both pass."""
    with _lock:
        refusal = unlock_refusal()
        if refusal is not None:
            raise Tainted(refusal)
        yield


def _reset_for_tests() -> None:
    """Forget the taint. Tests only: no tool, route or CLI command reaches this."""
    global _tainted_by  # noqa: PLW0603  PERMANENT(test hook)
    with _lock:
        _tainted_by = None
