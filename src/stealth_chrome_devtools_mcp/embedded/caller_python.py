"""A machine is vault-capable or caller-Python-capable, never both (T-19366).

Some tools run PYTHON the caller wrote inside the backend process, with the
backend's own authority and no sandbox: ``create_python_binding`` ``exec``s it, and
``create_dynamic_hook`` ``exec``s a hook's function and ``eval``s its
``custom_condition``. That code can read everything the process holds, so with the
vault unlocked it can read the key, every entry and the password a login typed
(measured by the third review: a tools-only agent got all three in one call).
Restricting builtins does not change this, and neither does a per-process
bookkeeping flag: the fourth review showed such code can persist itself (a ``.pth``
file) and run again, with the key, in the backend that the restart brings up. So
the rule is decided by DURABLE state, not by process history:

1. **While a vault file exists** (``vault_store.VAULT_PATH``, locked or unlocked, in
   every backend, always) the tools in :data:`RUNS_CALLER_PYTHON` are refused
   (:func:`admit`) with a sentence that says why and how to remove the vault
   (``stealthy login destroy``). With no vault file they behave exactly as before.
2. **A vault is not created while caller Python is resident** in any backend the
   registry knows (``cli_login``, through :func:`ran`): the CLI writes the file,
   then asks each backend; :func:`admit` marks the process BEFORE it reads the
   file. One of the two always sees the other, so the first secret is never stored
   beside code that is already running.

JavaScript-only tools (``execute_script``, ``inject_and_execute_script``,
``create_persistent_function``, ``execute_python_in_browser``, which TRANSPILES to
JavaScript) run in the page, never here, and stay available.

**What this does not cover, and says so** (README and the design spec carry the same
paragraph): the guarantee is against an agent that has only this MCP's tools. If
caller Python ever ran on this machine before the vault was created, or the agent
has any other way to run code as the same OS user (a shell tool, say), nothing here
can protect the vault from what that code left behind.

**One home for the set.** ``tool_registry.section_tool`` calls :func:`admit` for
every tool; ``tests/test_caller_python.py`` reads the same set and fails when any
new ``exec`` / ``eval`` / ``compile`` / ``importlib`` site appears in ``src`` that
is not pinned to a tool in it. A leaf: it imports ``vault_store`` only inside
functions.
"""

from __future__ import annotations

import errno
import os
import threading

from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

#: Every tool that runs, or lets a caller choose, Python in the backend process.
#: ``create_simple_dynamic_hook`` is NOT here: its code is a fixed template with the
#: caller's values written in as string literals (``dynamic_hook_ai_interface``).
RUNS_CALLER_PYTHON = frozenset({"create_python_binding", "create_dynamic_hook"})

DISABLED = (
    "{tool} runs your Python inside the backend process, which could read the login "
    "vault's key and passwords. A login vault is set up on this machine, so this "
    "tool is disabled in every backend for as long as the vault exists. A person "
    "can remove the vault with `stealthy login destroy` (that deletes every stored "
    "login); until then use execute_script or inject_and_execute_script, which run "
    "in the page."
)

_lock = threading.RLock()
_ran: str | None = None


def vault_exists() -> bool:
    """Whether the vault file is there. Fails CLOSED: a path that cannot be read
    (permissions, a loop) counts as existing, so the tools stay off."""
    from stealth_chrome_devtools_mcp.embedded import vault_store

    try:
        os.stat(vault_store.VAULT_PATH)  # noqa: PTH116  PERMANENT(exists() swallows more errors than it should)
    except OSError as error:
        return error.errno not in {errno.ENOENT, errno.ENOTDIR}
    return True


def ran() -> str | None:
    """The first tool that ran caller Python in THIS process since it started, or
    ``None``. Not "live" bindings: the code of a tool runs as it is called (module
    level statements, threads it starts), so a binding table cannot say what is
    resident; only a restart clears this."""
    return _ran


def admit(tool: str) -> None:
    """Let ``tool`` run, or raise ``ToolError`` while a vault exists on this machine.

    A tool outside :data:`RUNS_CALLER_PYTHON` is none of this module's business.
    One that is, marks this process BEFORE the file is read (so a vault being
    created at the same moment sees it), and un-marks again when it is refused:
    a refused call ran nothing."""
    if tool not in RUNS_CALLER_PYTHON:
        return
    global _ran  # noqa: PLW0603  PERMANENT(one process-wide fact, cleared only by a restart)
    with _lock:
        first = _ran is None
        if first:
            _ran = tool
        if vault_exists():
            if first:
                _ran = None
            raise ToolError(DISABLED.format(tool=tool))


def _reset_for_tests() -> None:
    """Forget what ran. Tests only: no tool, route or CLI command reaches this."""
    global _ran  # noqa: PLW0603  PERMANENT(test hook)
    with _lock:
        _ran = None
