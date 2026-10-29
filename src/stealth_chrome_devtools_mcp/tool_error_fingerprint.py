"""The Sentry fingerprint of a ``ToolError`` (T-10034): what its group is made of.

Built ONLY from facts the program controls, none of them text a caller can set:
a registered tool's name, the names of classes the program itself defines (any
other is ``<external>``), and the file and function name of our own innermost
frame. Never from the message: a ``ToolError`` can carry what the CALLER typed
(``click_element(text_match="my password is swordfish")`` is raised as ``find
'my password is swordfish'``) or what the machine holds, and no redaction rule
can tell either from prose. Nor from a class's name, ``__module__`` or a frame's
``__name__``, which code the caller executes (``create_python_binding``) can set
to anything. "Ours" is a PLACE ON DISK: a builtin, or a file under this package.
The full message is still the event's message, so every detail is on every event.

``observability._tool_error_fingerprint`` is the one caller; it redacts what this
returns before it is sent.
"""

from __future__ import annotations

import builtins
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_IMPORT_NAME = __package__ or __name__.rpartition(".")[0]
#: Where "ours" is: the directories whose files the program is made of. A tuple
#: so a test can add a directory of fixture code.
_ROOTS = (Path(__file__).parent,)

#: The tool an ``Error calling tool 'name'`` log line (fastmcp's, or
#: ``tool_failure``'s copy of it) is about.
_TOOL_CALL_RE = re.compile(r"\AError calling tool '(?P<name>[A-Za-z0-9_.\-]{1,100})'\Z")

#: What an element reads when the event has nothing for it, and when what it has
#: is not something the program itself defines.
NOT_NAMED, EXTERNAL, UNREGISTERED, UNNAMED_CODE = "-", "<external>", "<tool>", "<code>"

#: A bound on the traceback walk: a pathological recursion must not stall a send.
_MAX_TRACEBACK_FRAMES = 1000


def _locate(path: object) -> tuple[Path, Path] | None:
    """``(root, file)`` when *path* resolves to a REAL FILE under one of the roots."""
    try:
        resolved = Path(str(path)).resolve(strict=True)
        if resolved.is_file():
            for root in _ROOTS:
                if resolved.is_relative_to(root.resolve()):
                    return root.resolve(), resolved
    except (OSError, ValueError):
        pass
    return None


def type_name(kind: type) -> str:
    """*kind*'s name when the program defines it: a builtin, or a class of a module
    that is a file in this package. Any other type reads ``<external>``, and so
    does a class that only CLAIMS to be one (``type("my password is swordfish",
    (Exception,), {})``, or the same with ``__module__ = "builtins"``): its name
    must resolve, in the module the interpreter holds under its ``__module__``, to
    this very class."""
    try:
        module = (
            builtins
            if kind.__module__ == "builtins"
            else sys.modules.get(kind.__module__)
        )
        if module is None or (
            module is not builtins and not _locate(getattr(module, "__file__", None))
        ):
            return EXTERNAL
        found: object = module
        for part in kind.__qualname__.split("."):
            found = getattr(found, part)
    except Exception:  # noqa: BLE001  PERMANENT(a hostile class must not stall a send: it reads external)
        return EXTERNAL
    return kind.__qualname__ if found is kind else EXTERNAL


def tool_name(event: Mapping[str, object]) -> str | None:
    """The tool the event's log line names, ``<tool>`` when it is not a registered
    tool (a logger of a caller's can write that line too), ``None`` when it names
    none."""
    entry = event.get("logentry")
    for key in ("formatted", "message"):
        text = entry.get(key) if isinstance(entry, dict) else None
        if isinstance(text, str) and (found := _TOOL_CALL_RE.match(text)):
            name = found.group("name")
            # Lazily: the registry is the server's and is filled only in a process
            # that bound the tools; unfilled, nothing is registered.
            from stealth_chrome_devtools_mcp.embedded import tool_registry

            known = any(name in names for names in tool_registry.SECTION_TOOLS.values())
            return name if known else UNREGISTERED
    return None


def frame_name(error: BaseException) -> str | None:
    """``module.function`` of the innermost frame of OUR code in *error*'s
    traceback, or ``None``: no line number (lines move across releases, which is
    what split STEALTH-CHROME-DEVTOOLS-MCP-ET from -EV), no local. A frame is ours
    only when its code object's file is a real file in the package; its module is
    that file's path under the package (so ``server.py`` run as ``__main__`` under
    ``runpy`` has one name), never ``f_globals['__name__']``."""
    found, traceback = None, error.__traceback__
    for _ in range(_MAX_TRACEBACK_FRAMES):
        if traceback is None:
            break
        code = traceback.tb_frame.f_code
        if (located := _locate(code.co_filename)) is not None:
            relative = located[1].relative_to(located[0]).with_suffix("")
            parts = [part for part in relative.parts if part != "__init__"]
            name = code.co_name if code.co_name.isidentifier() else UNNAMED_CODE
            found = ".".join(filter(None, (_IMPORT_NAME, *parts, name)))
        traceback = traceback.tb_next
    return found


def parts(event: Mapping[str, object], chain: Sequence[BaseException]) -> list[str]:
    """``["tool-error", tool, ToolError type, "Cause>Cause", "module.function"]``
    for the exception ``chain[0]`` (outermost first, as ``_exception_chain`` returns).

    * the chain element is the exception types UNDER the ``ToolError``, outermost
      first (``ProtocolException>KeyError``), ``-`` when there are none;
    * the frame is the innermost frame of our code in the DEEPEST cause's
      traceback (the raise site of the real failure), else the ``ToolError``'s own
      raise site, else the element is left out (the tool and the types alone).

    Accepted: two ``KeyError`` keys raised in one function are ONE issue (each
    event shows its own key in its message); another tool, function or type is not.
    """
    frame = frame_name(chain[-1]) or frame_name(chain[0])
    return [
        "tool-error",
        tool_name(event) or NOT_NAMED,
        type_name(type(chain[0])),
        ">".join(type_name(type(link)) for link in chain[1:]) or NOT_NAMED,
        *([frame] if frame else []),
    ]
