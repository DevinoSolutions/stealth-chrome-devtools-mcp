"""Exact-string redaction of the login vault's secrets (T-19366).

The one rule of the vault is that no password reaches a model, and the sinks a
model can read are few: a Sentry event, a log line, and the debug ring behind
``get_debug_view`` / ``export_debug_logs``. Shape-based scrubbers
(:mod:`observability`) cannot know that ``Tr0ub4dor&3`` is a secret; this module
is told, by :func:`vault_store.unlock`, and from then on every one of those three
sinks passes its text through :func:`redact`.

**What is matched.** Each registered value, and the forms it takes when it
travels: ``urllib.parse.quote`` (with and without its ``/`` exemption),
``quote_plus`` (a form body) and its JSON-escaped spelling (a JSON request body).
Longest first, so a secret that contains another is masked whole.

**What is not.** A transformed secret (base64, hashed, split across two log
lines) is not recognised; the login code never logs one, which is the first line
of defence, and this is the second.

**Cost when idle.** With nothing registered :func:`redact` returns the SAME
string object after one truthiness check, so the logging factory and the debug
ring pay nothing on every installation that never unlocks a vault.

A leaf: stdlib only, so ``logging_setup``, ``debug_logger`` and ``observability``
can all import it without a cycle.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    import logging

MASK = "[vault-secret]"
#: Shorter values would mask ordinary words and prove nothing: a 3-character
#: password is not protected by hiding it in a log line.
MIN_LENGTH = 4

_lock = threading.Lock()
#: Every needle, longest first. Replaced wholesale (never mutated) so a reader on
#: another thread sees the old tuple or the new one, never a half-built one.
_needles: tuple[str, ...] = ()
_registered: frozenset[str] = frozenset()


class Secret:
    """One secret string that cannot be rendered by accident.

    ``repr`` and ``str`` (and therefore ``format`` and every f-string) answer
    ``***``; the only way to the text is :meth:`reveal`, which a reader of the
    code can grep for. It refuses to be pickled or copied, because the debug
    export pickles what it holds.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "***"

    __str__ = __repr__

    def __reduce__(self) -> NoReturn:
        raise TypeError("a Secret cannot be pickled or copied")


def register(value: str) -> None:
    """Start redacting ``value`` and its encoded forms. Values shorter than
    :data:`MIN_LENGTH` are ignored."""
    global _needles, _registered  # noqa: PLW0603  PERMANENT(one process-wide registry, replaced atomically)
    if not isinstance(value, str) or len(value) < MIN_LENGTH:
        return
    with _lock:
        registered = _registered | {value}
        forms = {form for original in registered for form in _forms(original)}
        _registered = frozenset(registered)
        _needles = tuple(sorted(forms, key=lambda form: (-len(form), form)))


def clear() -> None:
    """Forget every registered value (the vault was locked)."""
    global _needles, _registered  # noqa: PLW0603  PERMANENT(one process-wide registry, replaced atomically)
    with _lock:
        _registered = frozenset()
        _needles = ()


def redact(text: str) -> str:
    """``text`` with every registered value (and encoded form) replaced by
    :data:`MASK`. The same object when nothing is registered."""
    needles = _needles
    if not needles:
        return text
    for needle in needles:
        if needle in text:
            text = text.replace(needle, MASK)
    return text


def redact_value(value: object) -> object:
    """:func:`redact` through the containers a log or ring entry holds.

    Strings are redacted; ``dict`` values and keys, ``list`` and ``tuple`` items
    are walked; anything else is returned as it is. The same object comes back
    when nothing in it matched, so a caller can keep an identity check, and the
    input is never mutated.
    """
    if not _needles:
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return _redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return _redact_sequence(value)
    return value


def _redact_mapping(value: dict[object, object]) -> dict[object, object]:
    items = [(redact_value(k), redact_value(v)) for k, v in value.items()]
    unchanged = all(
        new_k is old_k and new_v is old_v
        for (new_k, new_v), (old_k, old_v) in zip(items, value.items(), strict=True)
    )
    return value if unchanged else dict(items)


def _redact_sequence(value: list[object] | tuple[object, ...]) -> object:
    walked = [redact_value(item) for item in value]
    if all(new is old for new, old in zip(walked, value, strict=True)):
        return value
    return walked if isinstance(value, list) else tuple(walked)


def redact_args(args: tuple[object, ...]) -> tuple[object, ...]:
    """A log record's ``args`` with the secret removed. A non-string argument
    that RENDERS a secret (a dict, an exception) is replaced by its redacted
    text; everything else, numbers included, is left for ``%d`` to format."""
    if not _needles:
        return args
    changed = False
    out: list[object] = []
    for arg in args:
        if isinstance(arg, (bool, int, float)) or arg is None:
            out.append(arg)
            continue
        if isinstance(arg, str):
            new: object = redact(arg)
        else:
            try:
                rendered = str(arg)
            except (TypeError, ValueError, AttributeError, RuntimeError):
                # An argument whose own __str__ fails fails again when logging
                # formats it, where the logging module handles it (handleError).
                out.append(arg)
                continue
            redacted = redact(rendered)
            new = arg if redacted == rendered else redacted
        changed = changed or new is not arg
        out.append(new)
    return tuple(out) if changed else args


def redact_record(record: logging.LogRecord) -> None:
    """Mask ``record.msg`` and ``record.args`` in place (a ``logging.LogRecord``).

    Called by the one record factory in ``logging_setup``, so it runs for every
    record in the process, ours and a library's. The message is only touched when
    it is a string; ``args`` as a lone mapping (logging's ``%(name)s`` form) is
    walked like any other container.
    """
    if not _needles:
        return
    if isinstance(record.msg, str):
        record.msg = redact(record.msg)
    args = record.args
    if isinstance(args, tuple) and args:
        record.args = redact_args(args)
    elif isinstance(args, dict) and args:
        redacted = redact_value(args)
        if isinstance(redacted, dict):
            record.args = redacted


def _forms(value: str) -> set[str]:
    """``value`` and the spellings it takes in a URL, a form body and JSON."""
    forms = {
        value,
        urllib.parse.quote(value),
        urllib.parse.quote(value, safe=""),
        urllib.parse.quote_plus(value),
        json.dumps(value)[1:-1],
    }
    return {form for form in forms if len(form) >= MIN_LENGTH}
