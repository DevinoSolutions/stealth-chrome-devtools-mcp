"""THE one home for "read a page's localStorage/sessionStorage" (F-869).

The read used to live inline in ``browser_manager.get_page_state`` as four
``tab.evaluate`` calls::

    local_storage_keys = await tab.evaluate("Object.keys(localStorage)")
    for key in local_storage_keys:
        value = await tab.evaluate(f"localStorage.getItem('{key}')")
        local_storage[key] = value

``tab.evaluate`` always sends ``SerializationOptions(serialization="deep")`` and
hands back ``remote_object.deep_serialized_value.value`` **raw** — nodriver's
``DeepSerializedValue.from_json`` keeps ``json["value"]`` verbatim and never
walks into it. A primitive therefore arrives plain, but an *array* arrives as a
list of BiDi nodes. Measured against Chrome 152 over a real http origin::

    >>> await tab.evaluate("Object.keys(localStorage)")
    [{'type': 'string', 'value': 'alpha'}, {'type': 'string', 'value': 'beta'}]

so ``local_storage[key] = value`` hashed a ``dict`` and raised
``TypeError: unhashable type: 'dict'`` on every page that has any storage at
all. This is the same deep-serialization trap F-844 hit with the viewport
object literal in the same function, and it is closed the same way: ask the page
for ``JSON.stringify(...)``, whose answer is a *string* primitive and therefore
survives deep serialization intact.

Two further properties of that one round trip, both of which the per-key loop
lacked:

* **No interpolation.** The old loop built ``localStorage.getItem('{key}')`` by
  f-string, so a key containing ``'`` produced a syntax error and a key
  containing ``');...`` was script injection from page-controlled data.
* **One evaluate, not 2N+2.** A page with 200 keys cost 402 CDP round trips
  inside ``get_instance_state``'s ``browser_state_timeout_seconds`` budget.

**The one degradation shape.** A page may legitimately refuse the read — an
opaque origin, a ``data:`` URL, third-party storage blocked by policy. Chrome
answers that with a ``SecurityError`` *thrown by the page*, which is not a
defect in this tool. That, and only that, is :class:`StorageBlockedError`; the caller
logs it and reports empty storage. Anything else — a bug here, a dying
connection, an answer that is not the JSON this module asked for — propagates,
and ``get_instance_state`` turns it into its ``partial: True`` +
``detail_error`` record (the named sub-field degradation, DESIGN §9). There is
no third outcome and no ``{"success": False}`` dict.

A leaf: it imports no other embedded module and takes the tab as an argument.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module a leaf
    from nodriver import Tab

#: Both stores in ONE round trip, as a JSON *string* (see the module docstring
#: for why a string and not an object). ``window[name]`` is read INSIDE the
#: ``try`` because it is the property *access* that throws on a blocked origin,
#: not the later ``Object.entries``. ``Object.entries`` on a ``Storage`` yields
#: its own enumerable string keys and their string values — the same set
#: ``Object.keys`` yielded, paired with the values the old loop fetched one by
#: one.
READ_JS = (
    "JSON.stringify((function(){"
    "function read(name){"
    "try{return {ok:true,entries:Object.entries(window[name])};}"
    "catch(e){return {ok:false,reason:String((e&&e.message)||e)};}"
    "}"
    "return {local:read('localStorage'),session:read('sessionStorage')};"
    "})())"
)


#: ``Object.entries`` yields ``[key, value]`` — two elements, always.
_PAIR = 2


class StorageBlockedError(Exception):
    """The PAGE refused the read — an expected answer, not a failure.

    Raised only when Chrome itself threw while the page touched
    ``window.localStorage`` / ``window.sessionStorage``: an opaque origin, a
    ``data:`` URL, storage disabled by policy. Measured message from Chrome 152
    on a ``data:`` URL::

        Failed to read the 'localStorage' property from 'Window':
        Storage is disabled inside 'data:' URLs.
    """


class StorageReadError(Exception):
    """The evaluate did not answer with the JSON this module asked for.

    Deliberately distinct from :class:`StorageBlockedError`: this one means the read
    itself went wrong (a JS throw outside the page's own ``try``, a CDP
    ``ExceptionDetails`` husk where a string was promised), so the caller must
    NOT report empty storage as if it were the truth.
    """


def _entries(record: object, store: str) -> dict[str, str]:
    """One store's ``{ok, entries}`` record as a plain ``{key: value}`` dict."""
    if not isinstance(record, dict):
        raise StorageReadError(f"{store}: unexpected record {record!r}")
    if not record.get("ok"):
        raise StorageBlockedError(f"{store}: {record.get('reason')}")
    rows = record.get("entries")
    if not isinstance(rows, list):
        raise StorageReadError(f"{store}: unexpected entries {rows!r}")
    entries: dict[str, str] = {}
    for row in rows:
        # Every row is checked rather than unpacked: a malformed answer must be
        # a named StorageReadError, not a ValueError out of tuple unpacking.
        if not isinstance(row, list) or len(row) != _PAIR:
            raise StorageReadError(f"{store}: unexpected entry {row!r}")
        entries[str(row[0])] = str(row[1])
    return entries


async def read(tab: Tab) -> tuple[dict[str, str], dict[str, str]]:
    """``(local_storage, session_storage)`` for the page ``tab`` is showing.

    Raises :class:`StorageBlockedError` when the page itself refused the read, and
    :class:`StorageReadError` when the answer was not the promised JSON.
    """
    answer = await tab.evaluate(READ_JS)
    if not isinstance(answer, str):
        raise StorageReadError(f"evaluate answered {type(answer).__name__}: {answer!r}")
    payload = json.loads(answer)
    return _entries(payload.get("local"), "localStorage"), _entries(
        payload.get("session"), "sessionStorage"
    )
