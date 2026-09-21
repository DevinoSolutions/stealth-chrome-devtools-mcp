"""THE one home for turning a backend's answer into the lines a human reads
(F-897).

Three renderings, one subject: the ``ls`` table (:func:`instance_rows`), the
``spawn`` block (:func:`spawn_lines`) and the ``tools`` listing
(:func:`tool_lines`), plus the clipping rule and the column widths all three
share. They left :mod:`cli_call` when that file stood at exactly its 1000-LOC
budget and ``spawn`` needed a flag: caps ratchet DOWN only, so the payment was
a cut and the honest cut is the one subject that is not this CLI's question.

Because that is the line between the two files. :mod:`cli_call` decides WHAT to
ask the backend, which exit code every exception there is maps onto, and
whether the answer goes out as JSON or as text — decisions with consequences
for a script. What a text answer LOOKS like has no consequences at all: it is
explicitly not a contract (which is why :func:`cli_call.wants_json` sends JSON
the moment stdout is not a terminal), and keeping the shape of a table next to
the classification of a broken pipe made one file answer two questions.

Nothing here reaches a backend, raises, or decides anything. Every function is
pure: records in, strings out — which is what lets the F-874 discipline below
be pinned without a Chrome, a socket or a process.

A leaf: stdlib only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

#: The mark a table puts in front of a url or title that is the LAST KNOWN one
#: rather than the current one. F-874 is the whole reason it exists: a `partial`
#: or `stored` record deliberately carries NO `current_url`, and a table that
#: filled that column from `last_navigated_url` would report a login page for an
#: instance sitting on a feed — the exact defect that finding closed.
LAST_KNOWN_MARK = "~"

#: Where `tools` files a live tool the installed registry has never heard of.
#: Named rather than inline because it is the row that MEANS something: the
#: shell and the backend are different builds.
UNKNOWN_SECTION = "(unknown section)"

#: Table column widths. A url is unbounded and a title is page-authored, so both
#: are clipped; the full values are one `--json` away.
_URL_WIDTH = 52
_TITLE_WIDTH = 28
_ID_WIDTH = 38


def clip(value: object, width: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def instance_rows(records: Sequence[dict[str, object]]) -> list[str]:
    """The `ls` table: one row per instance, and never a stale url in a column
    headed as the current one — see :data:`LAST_KNOWN_MARK`."""
    rows = [f"{'ID':<{_ID_WIDTH}} {'STATE':<18} {'SRC':<7} {'URL':<{_URL_WIDTH}} TITLE"]
    for record in records:
        live = record.get("source") == "active" and not record.get("partial")
        mark = "" if live else LAST_KNOWN_MARK
        url = record.get("current_url") if live else record.get("last_navigated_url")
        title = record.get("title") if live else record.get("last_navigated_title")
        shown_url = clip(mark + clip(url, _URL_WIDTH), _URL_WIDTH)
        shown_title = clip(mark + clip(title, _TITLE_WIDTH), _TITLE_WIDTH)
        rows.append(
            f"{clip(record.get('instance_id'), _ID_WIDTH):<{_ID_WIDTH}} "
            f"{clip(record.get('state'), 18):<18} "
            f"{clip(record.get('source'), 7):<7} "
            f"{shown_url:<{_URL_WIDTH}} {shown_title}"
        )
    rows.append(
        f"({LAST_KNOWN_MARK} = last known, not current: this instance's live tab "
        "could not be read)"
    )
    return rows


def spawn_lines(result: dict[str, object]) -> list[str]:
    """What a human needs to see about the browser they just got.

    ``reattached`` first and unmissable: it is the difference between a fresh
    Chrome and the one that still holds the login the caller came for, and F-888
    put that fact in ``spawn_diagnostics``, where nobody reads it.
    """
    diagnostics = result.get("spawn_diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    selection = diagnostics.get("profile_selection")
    selection = selection if isinstance(selection, dict) else {}
    lines = [f"instance   : {result.get('instance_id')}"]
    if diagnostics.get("reattached"):
        lines.append(
            f"REATTACHED : yes — this is the browser that was already running "
            f"(pid {diagnostics.get('reattached_pid')})"
        )
    if diagnostics.get("reattach_declined"):
        lines.append(f"reattach   : declined — {diagnostics['reattach_declined']}")
    lines.append(f"role       : {selection.get('profile_role', '-')}")
    lines.append(f"profile    : {selection.get('user_data_dir', '-')}")
    if selection.get("walked_to"):
        lines.append(
            f"walked to  : {selection['walked_to']} ({selection.get('walk_reason')})"
        )
    return lines


def tool_lines(
    tools: Sequence[dict[str, object]], sections: dict[str, str]
) -> list[str]:
    """The live surface, grouped by the section the installed registry knows it
    by. A live tool the registry does not know goes under ``(unknown section)``
    rather than being dropped — the LIVE list is the truth about the running
    backend, and a name this build has never heard of is the most interesting
    row on the page."""
    grouped: dict[str, list[dict[str, object]]] = {}
    for tool in tools:
        section = sections.get(str(tool.get("name")), UNKNOWN_SECTION)
        grouped.setdefault(section, []).append(tool)
    lines: list[str] = []
    for section in sorted(grouped):
        lines.append(f"\n{section} ({len(grouped[section])})")
        for tool in grouped[section]:
            summary = str(tool.get("description") or "").strip().splitlines()
            first = summary[0] if summary else ""
            lines.append(f"  {tool.get('name')!s:<34} {clip(first, 60)}")
    return lines
