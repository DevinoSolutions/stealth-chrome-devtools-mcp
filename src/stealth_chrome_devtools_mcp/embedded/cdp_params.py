"""THE one home for "a caller's JSON, as the type a CDP wrapper declares" (F-861).

``execute_cdp_command`` hands a caller's parameters to nodriver's generated
wrapper for the command (``uc.cdp.browser.set_window_bounds`` and friends). The
caller sends what the CDP docs show — ``{"windowId": 7, "bounds": {"width": 800}}``
— but the wrappers do not take JSON: every typed parameter is a generated class
with ``from_json``/``to_json`` (``WindowID(int)``, ``Bounds`` dataclass,
``TargetID(str)``, the enums), and the wrapper body calls ``.to_json()`` on
whatever it was handed. A raw value therefore crashed INSIDE nodriver — ``'str'
object has no attribute 'to_json'`` — one frame past anything that could say
which parameter wanted what (Sentry STEALTH-CHROME-DEVTOOLS-MCP-4T, 74 events on
``Input.dispatchMouseEvent``'s ``button``; -79 on ``Browser.grantPermissions``'
``permissions``; the ``int`` twin on ``Browser.setWindowBounds``' ``windowId``).

:func:`typed` turns each argument into the type the wrapper's OWN signature
declares, read from its resolved type hints — nothing is typed by hand here, so a
nodriver upgrade that adds a parameter is covered the moment it lands. The rules
are the protocol's: a generated class is built with its ``from_json``;
``Optional`` passes ``None`` through and types the rest; ``List[T]`` types every
element; primitives (``str``/``int``/``float``/``bool``) and values that are
already the type are untouched, so every frame F-816 pinned is byte-identical. A
value the type cannot take is a caller mistake — a ``ToolError`` naming the
parameter and the type, which ``observability``'s ``before_send`` drops by type
instead of shipping nodriver's AttributeError.

A leaf: imports only ``tool_errors``; never imports ``server``.
"""

from __future__ import annotations

import types
import typing

from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

# What a generated ``from_json`` raises for a value of the wrong shape: an enum
# with an unknown member (ValueError), a dataclass fed a string (TypeError) or a
# dict missing a required key (KeyError), a newtype over a non-scalar.
_SHAPE_ERRORS = (TypeError, ValueError, KeyError, AttributeError)


def typed(method: typing.Callable, kwargs: dict[str, object]) -> dict[str, object]:
    """``kwargs`` (already folded onto ``method``'s own names) with each value as
    the type ``method`` declares for it; names ``method`` does not declare pass
    through so the wrapper's own ``TypeError`` still names the unknown param.

    Raises:
        ToolError: a value the declared type cannot be built from.
    """
    hints = _hints(method)
    out: dict[str, object] = {}
    for name, value in kwargs.items():
        annotation = hints.get(name)
        if annotation is None:
            out[name] = value
            continue
        try:
            out[name] = _coerce(value, annotation)
        except _SHAPE_ERRORS as exc:
            raise ToolError(
                f"param {name!r} expects {_describe(annotation)}, got {value!r}: {exc}"
            ) from exc
    return out


def _hints(method: typing.Callable) -> dict[str, object]:
    """The wrapper's resolved annotations (they are strings under ``from
    __future__ import annotations``); a wrapper whose hints cannot resolve is
    passed through untyped, exactly as every call was before F-861."""
    try:
        return typing.get_type_hints(method)
    except (NameError, TypeError, AttributeError):
        return {}


def _coerce(value: object, annotation: object) -> object:
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        return _coerce_union(value, typing.get_args(annotation))
    if origin is list:
        (item,) = typing.get_args(annotation) or (object,)
        if isinstance(value, (list, tuple)):
            return [_coerce(element, item) for element in value]
        return value
    if origin is not None:  # dict / tuple / other generics: JSON already
        return value
    if _is_generated(annotation):
        return value if isinstance(value, annotation) else annotation.from_json(value)
    return value


def _coerce_union(value: object, members: tuple[object, ...]) -> object:
    if value is None:
        return None
    candidates = [member for member in members if member is not type(None)]
    if any(_is_generated(m) and isinstance(value, m) for m in candidates):
        return value
    last: Exception | None = None
    for member in candidates:
        try:
            return _coerce(value, member)
        except _SHAPE_ERRORS as exc:
            last = exc
    if last is not None:
        raise last
    return value


def _is_generated(annotation: object) -> bool:
    """A nodriver CDP type: a class carrying the generated ``from_json``."""
    return isinstance(annotation, type) and callable(
        getattr(annotation, "from_json", None)
    )


def _describe(annotation: object) -> str:
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation).replace("typing.", "").replace("nodriver.cdp.", "")
