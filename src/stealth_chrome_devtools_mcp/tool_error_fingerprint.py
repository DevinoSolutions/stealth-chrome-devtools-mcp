"""The Sentry fingerprint of an error we report (T-10034): what its group is made of.

THREAT MODEL, stated once. The guarantee is that caller DATA never becomes a
grouping key: not a tool argument (``click_element(text_match="my password is
swordfish")`` is raised as ``find 'my password is swordfish'``), not page content,
not a message, and not a name the caller's own code gives its own objects (a class
called ``my password is swordfish``, a function compiled under any file name, a
module of its own, an attribute set on an exception). What it does NOT claim:
``create_python_binding``, dynamic hooks and ``execute_python_in_browser`` run the
caller's Python in THIS process, and that code holds the whole process. It can
monkeypatch these modules, register a module in ``sys.modules`` under one of OUR
real names (or a covered library's) before that module loads, or call
``sentry_sdk.capture_message`` with any text; that is impersonation, and defeating
it is out of scope like any monkeypatch. What the checks below ensure is that such
code's own names do not enter OUR fingerprints by ordinary means.

So nothing is believed because an object SAYS it is ours (its ``__module__``, its
file name, its ``__file__``, an attribute): all of that is text the caller's code can
write by accident or intent. Two identity maps decide:

* the CODE map: a frame is ours when its code object EQUALS one compiled from a
  source file of this package. Equality is the bytecode, constants, names, line table
  and first line, so code a caller compiled (under our file name, or with our
  function's name) is not equal, and neither is any code carrying a caller's text.
  Compiling the files, rather than walking live modules, also reaches what a module
  walk misses: a decorated function's body, and ``server.py``, which the backend
  runs by path under ``runpy`` and no module name points at. The label
  ``module.qualname`` is made of OUR file's path and ``co_qualname``, so two
  functions that share a ``co_name`` (``resolve_element.<locals>._do`` and
  ``resolve_by_text.<locals>._do``) stay two.
* the CLASS map: a class is named when it IS (identity) a builtin exception, an
  exception class found in the namespace of one of our modules, or an exception
  class found in the namespace of a MODULE of a library the package handles
  (``_STDLIB_MODULES``, ``_LIBRARY_MODULES``: the one closed list, read by module and
  not by class, so a class the package starts to handle is covered when its module
  is); anything else, a caller's subclass of ``ToolError`` or of ``ProtocolException``
  included, reads ``<external>``. A library's class is named ``library.Name``
  (``psutil.Error``, ``sqlite3.Error``, ``httpx.HTTPError``, ``urllib.HTTPError``
  are four types, and a bare ``Error`` or ``HTTPError`` is shared by two or three
  libraries); only builtins, our own classes and nodriver's ``ProtocolException``
  are bare.

A declared ``sentry_fingerprint`` (``forensic_errors``) is read only off an instance
of one of OUR ``ForensicError`` classes, and only when it is made of a closed
vocabulary (:func:`declared`). The message is never read: it is still the event's
message, so every detail is on every event.

``observability._tool_error_fingerprint`` and ``_stable_fingerprint`` are the
callers; they redact what this returns before it is sent.
"""

from __future__ import annotations

import builtins
import importlib
import logging
import re
import sys
import threading
from pathlib import Path
from types import CodeType, ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

_log = logging.getLogger(__name__)
_IMPORT_NAME = __package__ or __name__.rpartition(".")[0]
_PACKAGE_DIR = Path(__file__).parent

#: The tool an ``Error calling tool 'name'`` log line (fastmcp's, or
#: ``tool_failure``'s copy of it) is about.
_TOOL_CALL_RE = re.compile(r"\AError calling tool '(?P<name>[A-Za-z0-9_.\-]{1,100})'\Z")

#: What an element reads when the event has nothing for it, and when what it has
#: is not something the program itself defines.
NOT_NAMED, EXTERNAL, UNREGISTERED = "-", "<external>", "<tool>"
#: The one cause element for every way of saying "the held element left the page"
#: (``element_resolution.detached_element``, T-11043).
DEPARTED = "ElementDeparted"

#: A bound on the traceback walk: a pathological recursion must not stall a send.
_MAX_TRACEBACK_FRAMES = 1000
#: A bound on a declaration: a closed vocabulary needs few elements.
_MAX_DECLARED = 8
#: ``<ExceptionType> at <step>``, in words.
_FAILURE_WORDS = 3

#: ``id(class) -> (class, name)``: keyed by id and checked with ``is``, so no
#: ``__hash__`` or ``__eq__`` of a caller's class ever runs. Builtins are taken at
#: import, before any caller code can add an attribute to ``builtins``; our own
#: classes and the covered modules' are added as they load.
_classes: dict[int, tuple[type, str]] = {
    id(value): (value, value.__name__)
    for value in vars(builtins).values()
    if isinstance(value, type) and issubclass(value, BaseException)
}
#: THE closed list of modules whose exception classes may be named, found by reading
#: every ``except`` / ``isinstance`` / ``raise`` / tuple of classes of the package for
#: a class that is neither builtin nor ours (``test_tool_error_fingerprint_census``
#: repeats that read and fails on a class that no module here covers). A module, not
#: a class: a probe fails with a ``ProtocolException``, a ``ConnectionClosed``, a
#: ``URLError`` or a ``RemoteDisconnected`` far more often than with anything else,
#: and as ``<external>`` they would all be one issue. Every attribute of a listed
#: module that is an exception class of the same library (a re-export of another
#: library's is skipped: that library's own module names it) is named, by the
#: class's own ``__qualname__`` taken when the module is read: a subclass, or a class
#: that merely carries the name, is not in the map. Add a module here when the package
#: starts to handle a class that no module of this list defines.
#:
#: The standard library is cheap and always present, so its modules are imported when
#: the maps are built; one a Python was built without names nothing.
_STDLIB_MODULES = (
    "json",
    "json.decoder",
    "http.client",
    "urllib.error",
    "asyncio",
    "asyncio.exceptions",
    "concurrent.futures",
    "subprocess",
    "struct",
    "importlib.metadata",
    "socket",
    "ssl",
    "zipfile",
    "tarfile",
    "shutil",
    "sqlite3",
    "xml.etree.ElementTree",
    "email.errors",
    "queue",
)
#: A third-party library is NEVER imported for its exceptions (importing nodriver or
#: httpx to fill a table would cost every start): a module is read once it is already
#: in ``sys.modules`` and finished loading, which it is before any of its exceptions
#: can reach an event.
_LIBRARY_MODULES = (
    "nodriver.core.connection",
    "websockets.exceptions",
    "psutil",
    "httpx",
    "pydantic_core",
    "requests.exceptions",
    "mcp.shared.exceptions",
)
#: The libraries whose classes read bare (``ProtocolException``); every other
#: library's read ``library.Name``, because the standard library and the others define
#: a class of the same name (``Error``, ``HTTPError``, ``TimeoutExpired``) often enough
#: to merge two types into one issue.
_BARE_LIBRARIES = frozenset({"nodriver"})
_stdlib_pending = list(_STDLIB_MODULES)
_library_pending = list(_LIBRARY_MODULES)
#: The dotted name of every module the package's files make up, and which of them
#: have had their classes read. Names come from the files on disk, never from
#: ``sys.modules``, so a module a caller registers under another name is no module.
_module_names: dict[str, Path] = {}
_classes_read: set[str] = set()
#: ``code -> "module.qualname"``; ``None`` where two files hold equal code under
#: two names (then neither is claimed).
_code_labels: dict[CodeType, str | None] = {}
_built = False
#: Whether the maps have been judged (see :func:`_judge`): at most one verdict, and
#: so at most one warning, per process.
_judged = False
#: One lock for the maps: whoever needs them first builds them, and a second caller
#: waits for that build instead of making another. Re-entrant: a module imported
#: while building can log, and a log can ask for a name.
_lock = threading.RLock()
_warm_lock = threading.Lock()
_warmer: threading.Thread | None = None


def _code_objects(code: CodeType) -> Iterator[CodeType]:
    yield code
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            yield from _code_objects(constant)


def _build() -> None:
    """Read the package's files once: the dotted name of each, and its code objects."""
    for path in sorted(_PACKAGE_DIR.rglob("*.py")):
        parts = path.relative_to(_PACKAGE_DIR).with_suffix("").parts
        name = ".".join(part for part in (_IMPORT_NAME, *parts) if part != "__init__")
        _module_names[name] = path
        try:
            top = compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
        except (OSError, SyntaxError, ValueError):
            continue  # a file we cannot read names no frame
        for code in _code_objects(top):
            label = f"{name}.{code.co_qualname}"
            if _code_labels.setdefault(code, label) != label:
                _code_labels[code] = None


def _loaded(name: str, *, importing: bool) -> ModuleType | None:
    """The finished module *name*, or ``None``: not loaded yet (and not to be
    imported), half loaded, absent, or an object that does not claim to be it."""
    try:
        module = importlib.import_module(name) if importing else sys.modules.get(name)
    except Exception:  # noqa: BLE001  PERMANENT(a module that fails to import names nothing; it must not fail an event)
        return None
    spec = getattr(module, "__spec__", None)
    if not isinstance(module, ModuleType) or getattr(spec, "name", None) != name:
        return None
    return None if getattr(spec, "_initializing", False) else module


def _record(
    module: ModuleType, defined_here: Callable[[str], bool], prefix: str = ""
) -> None:
    """Name every exception class *module* holds whose ``__module__`` satisfies
    *defined_here*: defined by the module or its library, not imported from another."""
    for value in list(vars(module).values()):
        if (
            isinstance(value, type)
            and issubclass(value, BaseException)
            and defined_here(value.__module__)
        ):
            _classes.setdefault(id(value), (value, prefix + value.__qualname__))


def _read_modules() -> None:
    """Read the classes of every module loaded since the last read."""
    for name in _module_names.keys() - _classes_read:
        module = _loaded(name, importing=False)
        if module is not None:  # not loaded yet, or half loaded: read it next time
            _classes_read.add(name)
            _record(module, lambda home, name=name: home == name)
    for pending, importing in ((_stdlib_pending, True), (_library_pending, False)):
        for name in tuple(pending):
            module = _loaded(name, importing=importing)
            if module is not None or importing:  # a stdlib module not there never comes
                pending.remove(name)
            if module is not None:
                library = name.partition(".")[0]
                _record(
                    module,
                    lambda home, library=library: home.partition(".")[0] == library,
                    "" if library in _BARE_LIBRARIES else f"{library}.",
                )


def _judge() -> None:
    """Say, once per process, that the maps came out empty. A package with no source
    files to read (a zip or a sourceless install) names no frame and no class of
    ours, so every fingerprint would read ``<external>`` and an event whose only
    difference is its message would regroup by message (the original T-10034 bug)
    with nothing to say why."""
    global _judged  # noqa: PLW0603  PERMANENT(one verdict per process)
    try:
        # Ours and always present: when even this is not named, nothing of ours is.
        from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
    except ImportError:
        return  # being imported right now: judge on a later pass
    _read_modules()
    entry = _classes.get(id(ToolError))
    _judged = True
    if not _code_labels or entry is None or entry[0] is not ToolError:
        _log.warning(
            "tool_error_fingerprint: no package source found; "
            "Sentry fingerprints fall back to %s",
            EXTERNAL,
        )


def _refresh() -> None:
    """Build on first use; afterwards read the classes of any module loaded since."""
    global _built  # noqa: PLW0603  PERMANENT(the identity maps are built once, on first use)
    with _lock:
        if not _built:
            _build()
            _built = True
        _read_modules()
        if not _judged:
            _judge()


def warm() -> threading.Thread | None:
    """Build the maps on a daemon thread, once, so no event pays for the first build
    (compiling the package's files takes about 0.4 s). Returns the thread, or
    ``None`` when a warm-up was already started, or could not be (no thread to be
    had). The first use stays the fallback: it and the warm-up share
    :func:`_refresh`'s lock, so there is one build."""
    global _warmer  # noqa: PLW0603  PERMANENT(one warm-up per process)
    with _warm_lock:
        if _warmer is not None:
            return None
        thread = threading.Thread(
            target=_warm_up, name="fingerprint-warm-up", daemon=True
        )
        try:
            thread.start()
        except Exception:  # noqa: BLE001  PERMANENT(no thread to be had leaves the first use to build, and sentry_init never raises)
            _log.debug("tool_error_fingerprint: no warm-up thread", exc_info=True)
            return None
        _warmer = thread
        return thread


def _warm_up() -> None:
    try:
        _refresh()
    except Exception:  # noqa: BLE001  PERMANENT(a warm-up that fails leaves the first use to build)
        return


def type_name(kind: type) -> str:
    """*kind*'s name when it IS a builtin exception, an exception class of the
    package or one of a covered library module (``library.Name``, see
    ``_STDLIB_MODULES``); ``<external>`` for any other, including a class that only
    CLAIMS to be one (``type("my password is swordfish", (ToolError,), {})``, or one
    whose ``__module__`` is ``builtins``): the maps hold classes, not what they say."""
    _refresh()
    entry = _classes.get(id(kind))
    return entry[1] if entry is not None and entry[0] is kind else EXTERNAL


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
    """``module.qualname`` of the innermost frame of OUR code in *error*'s
    traceback, or ``None``: no line number (lines move across releases, which is
    what split STEALTH-CHROME-DEVTOOLS-MCP-ET from -EV), no local. A frame is ours
    only when its code equals code of a file of the package (see the module
    docstring); the name is built from that file, never from the frame."""
    _refresh()
    found, traceback = None, error.__traceback__
    for _ in range(_MAX_TRACEBACK_FRAMES):
        if traceback is None:
            break
        found = _code_labels.get(traceback.tb_frame.f_code) or found
        traceback = traceback.tb_next
    return found


def declared(error: BaseException) -> list[str] | None:
    """The fingerprint *error* declares, or ``None`` to compute one.

    Accepted only from an instance of one of OUR ``ForensicError`` classes (the
    class is looked up by identity, and the attribute read from the instance's own
    ``__dict__``, so no code of a caller's class runs here), and only when it is
    made of the closed vocabulary of ``forensic_errors``: a kind, then elements
    each made of a site, a ``<ExceptionType> at <step>`` failure, or
    ``no instance``. An exception type name is kept only when :func:`type_name`
    knows it, and reads ``<external>`` otherwise, as in a ``ToolError``'s chain;
    any other word rejects the whole declaration. So an attribute set to
    ``("my password is swordfish",)`` on a ``ToolError``, or on a ``ForensicError``,
    is not read.
    """
    try:
        raw = vars(error).get("sentry_fingerprint")
    except TypeError:
        return None
    if raw is None:
        return None  # nearly every exception: do not even load the maps for it
    from stealth_chrome_devtools_mcp.embedded import forensic_errors

    kind = type(error)
    if type_name(kind) == EXTERNAL or not issubclass(
        kind, forensic_errors.ForensicError
    ):
        return None
    # Exact builtin types, not isinstance: a caller's str subclass can answer
    # every comparison with True and would carry its own text through.
    if not (
        type(raw) is tuple
        and 0 < len(raw) <= _MAX_DECLARED
        and all(type(part) is str for part in raw)
        and raw[0] in forensic_errors.KINDS
    ):
        return None
    written = [raw[0]]
    for part in raw[1:]:
        if (element := _in_vocabulary(part)) is None:
            return None
        written.append(element)
    return written


def _in_vocabulary(element: str) -> str | None:
    """*element* in the closed vocabulary's words, or ``None`` if not made of them."""
    from stealth_chrome_devtools_mcp.embedded import forensic_errors as vocabulary

    words = element.split(" ")
    site = [words.pop(0)] if words[0] in vocabulary.SITES else []
    if not words:
        return " ".join(site) or None
    if words == vocabulary.NO_INSTANCE.split(" "):
        return " ".join([*site, *words])
    at_a_step = len(words) == _FAILURE_WORDS and (
        words[1] == "at" and words[2] in vocabulary.STEPS
    )
    if not (at_a_step or (len(words) == 1 and site)) or not words[0].isidentifier():
        return None
    # An exception type: kept when it is one we know, else the one word for "not ours".
    known = {kind.__name__ for kind, _ in _classes.values()}
    return " ".join([*site, words[0] if words[0] in known else EXTERNAL, *words[1:]])


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

    One exception to both rules (T-11043): a cause that
    ``element_resolution.detached_element`` classifies as "the element left the
    page" is ONE cause, :data:`DEPARTED`, whichever way Chrome said so. Chrome's
    own ``No node with given id found`` (raised in ``page_natives``) and the page's
    ``isConnected`` answer (``ElementDisconnected``, raised in ``dom_handler``) are
    a race inside Chrome over one event, so neither their types nor their raise
    sites tell them apart: the chain ends at the first departure, and the frame is
    that of the link above it. The event itself still carries the whole chain.
    """
    departed = _first_departure(chain)
    if departed is None:
        causes = [type_name(type(link)) for link in chain[1:]]
        frame = frame_name(chain[-1]) or frame_name(chain[0])
    else:
        causes = [type_name(type(link)) for link in chain[1:departed]] + [DEPARTED]
        frame = frame_name(chain[departed - 1]) or frame_name(chain[0])
    return [
        "tool-error",
        tool_name(event) or NOT_NAMED,
        type_name(type(chain[0])),
        ">".join(causes) or NOT_NAMED,
        *([frame] if frame else []),
    ]


def _first_departure(chain: Sequence[BaseException]) -> int | None:
    """Index in *chain* (never 0, the ``ToolError`` itself) of the outermost cause
    that says a held element left the page, else ``None``. The question is
    ``element_resolution.detached_element``'s, THE one place it is answered."""
    # Lazily, as :func:`tool_name` does: nothing here needs the page layer loaded
    # unless a tool error is being fingerprinted.
    from stealth_chrome_devtools_mcp.embedded import element_resolution

    for index, link in enumerate(chain[1:], start=1):
        if element_resolution.detached_element(link) is not None:
            return index
    return None
