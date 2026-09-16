"""THE one home for "what does this form control hold now, and did it take what
was asked".

F-877, for the two controls whose state is a SET of selected things: a
``<select>``'s options and an ``<input type="file">``'s ``FileList``.
``select_option`` and ``upload_file`` were the last two interaction tools that
reported the success of their own dispatch rather than of the interaction —
F-873's sentence, two tools over. Measured on Chrome 152.0.7977.83, product code
path, throwaway profile.

**What ``select_option`` answered ``True`` for.** Eleven cases, of which three
were worse than a silent no-op:

============================================ =========================================
call                                         what the page ended up with
============================================ =========================================
``value=`` naming no option                  ``selectedIndex = -1`` — the standing
                                             selection **cleared**, a ``change`` fired
                                             announcing it
``index=`` out of range                      nothing; the whole script body sat inside
                                             an ``if`` that did not run
``text=`` matching nothing                   nothing
``text=`` against a ``<select disabled>``    nothing *there* — the keys went to
                                             whichever control had focus, and moved it
an empty ``<select>``                        nothing, plus a ``change``
an ``<input type="text">`` selector          ``elem.value`` **written**, plus a
                                             ``change``
============================================ =========================================

**Why the matching rule is here and not in the page.** ``text=`` used to be
``Element.send_keys``, so its real consumer was Chrome's ``<select>`` typeahead:
a live buffer with a ~1 s timeout **shared with the previous call**, searching
from the option *after* the current one with wraparound, skipping ``disabled``
options, and — measured — resolving a ``label=``/text collision onto the wrong
option. That is not "select the option whose text is X". The rule this module
applies instead is three tiers and is stated where it can be read and tested:
exact ``option.text``, exact ``option.label``, then a case-insensitive **prefix**
over either. The last tier is not taste — ``"Bet"`` and ``"beta"`` both reached
``Beta`` through the typeahead, so a caller may be relying on it, and a fix that
dropped it would break them.

**The events this module fires are UNTRUSTED, and that is a real loss.**
``new Event('change', {bubbles: true})`` carries ``isTrusted: false``, where the
keystrokes the ``text=`` arm used to send produced TRUSTED ``input``/``change``
from Chrome itself (measured — see the §2b matrix). There is no trusted
alternative that is not the typeahead this module exists to remove, and the
``value``/``index`` arms were already untrusted, so the trade is: two of the
three arms are unchanged, the third loses trust and gains the ability to name its
own control. A page that gates on ``event.isTrusted`` was already unreachable
through two of the three arms and is now unreachable through all three. Named
here, in the tool's docstring and in the finding's §6, on ``click_target``'s
precedent — it labels its untrusted path ``synthetic`` rather than leaving the
caller to find out.

**Two reads, in that order, and each is one ``JSON.stringify`` round trip.**
:data:`READ_SELECT_JS` answers the option list and the standing selection;
:func:`apply_js` sets ``selectedIndex`` and dispatches ``input`` then ``change``
— the pair, and the order, Chrome's own typeahead produced (measured), where the
shipped arms fired ``change`` alone and fired it even when nothing moved. The
write re-checks the option's ``value`` against the index it was given, because a
dependent dropdown can repopulate between the two calls and an index resolved
against the old list addresses a different option in the new one. The state is
read back **after** the events, which are synchronous, so a page that resets the
control inside its own ``change`` handler has already done so.

**What ``upload_file`` answered.** ``{"uploaded": <the caller's own paths>,
"count": len(them)}`` — composed before the CDP call and regardless of it. Ten of
eleven measured cases were honest; the one that was not is the one nothing could
have caught without a read: ``DOM.setFileInputFiles`` **succeeds** with two files
on an input that has no ``multiple`` attribute (the raw call answers ``None``, no
error) and Chrome keeps the **first** file only. :data:`READ_FILES_JS` is the
read, and :func:`verify_attached` is the verdict.

**The ORDER the handler keeps, and why each step of it is load-bearing.**
``dom_handler.select_option`` / ``upload_file`` keep only the sequence of calls
into this module, and the sequence is the fix:

* ``select_option`` reads the options (:func:`read_select`) BEFORE anything is
  written, so a criterion that names no option raises having changed nothing —
  the shipped ``value`` arm assigned ``select.value`` first and so CLEARED the
  page's standing selection on its way to answering ``True``. It reads the
  control back AFTER the events (:func:`apply_selection` does both in one
  script), which are synchronous, so a page that resets the control inside its
  own ``change`` handler has already done so. Criterion precedence is unchanged:
  text, value, index.
* ``upload_file`` runs its two pre-flight guards (every path must exist;
  :func:`require_file_input`) BEFORE any CDP call, then reads the ``FileList``
  (:func:`read_files`) once, AFTER ``send_file`` — the only moment at which it
  can answer, because Chrome's decision to keep one file of two is made by the
  write.

**No message here ever carries an option's text or value, and none carries a file
path or name.** A ``<select>`` is frequently a list of account numbers and an
absolute path names the operating user; a raised ``ToolError`` reaches the
caller, the debug ring (``log_tool_failure`` — ring only, F-782/F-835) and Sentry
at once. Every message, and every field of both records, is a selector, a count
or an index. Same discipline as F-869's storage reader, F-873's text and F-876's
overlay, for the same reason.

A leaf: ``tool_errors`` only, and the element arrives as an argument.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

if TYPE_CHECKING:
    from nodriver import Element

#: Which criterion a caller supplied. ``select_option``'s own precedence
#: (text, then value, then index) is unchanged and stays at its call site.
BY_VALUE = "value"
BY_TEXT = "text"
BY_INDEX = "index"

#: How many ``<option>`` records one read may answer with. A page can carry a
#: ``<select>`` with a hundred thousand options; the matching rule runs in
#: Python, so the list crosses the CDP boundary and an unbounded read would be a
#: payload rather than a question. Country and currency lists — the long ones a
#: human actually meets — are in the low hundreds.
MAX_OPTIONS = 2000

#: The ONE option read. Answers with a JSON **string** for the same reason
#: ``text_entry.READ_JS`` and ``click_target.AIM_JS`` do: ``Element.apply``
#: returns ``result[0].value``, so a script that THREW lands there as ``None``
#: and must never be mistaken for a control with no options.
#:
#: ``option.text`` is Chrome's COLLAPSED text, not ``textContent`` (measured: an
#: option written over three lines answers ``"Spaced Out"``), and ``option.label``
#: falls back to it when the attribute is absent — so both are read, and the
#: caller's string is compared against what a browser RENDERS.
READ_SELECT_JS = """(select) => {
    const tag = String(select.tagName || '').toLowerCase();
    if (tag !== 'select') {
        return JSON.stringify({
            tag: tag, is_select: false, multiple: false, option_count: 0,
            selected_index: -1, selected_count: 0, selected_indexes: [],
            options: []
        });
    }
    const chosen = select.selectedOptions;
    return JSON.stringify({
        tag: tag,
        is_select: true,
        multiple: !!select.multiple,
        option_count: select.options.length,
        selected_index: select.selectedIndex,
        selected_count: chosen.length,
        selected_indexes: Array.prototype.map.call(chosen, (o) => o.index),
        options: Array.prototype.slice.call(
            select.options, 0, __MAX_OPTIONS__
        ).map((o) => ({
            index: o.index,
            value: String(o.value == null ? '' : o.value),
            text: String(o.text == null ? '' : o.text),
            label: String(o.label == null ? '' : o.label),
            disabled: !!o.disabled
        }))
    });
}""".replace("__MAX_OPTIONS__", str(MAX_OPTIONS))

#: The ONE selection write. The wanted ``{index, value}`` is embedded in the
#: source because ``Element.apply`` carries no arguments — a criterion reaches
#: the page in the function body or not at all.
#:
#: ``moved`` is computed from the selected-index **SET**, never from
#: ``selectedIndex``. On a ``<select multiple>`` holding ``[0, 2]``, assigning
#: ``selectedIndex = 0`` deselects option 2 — the spec's setter selects exactly
#: the option it names — while ``selectedIndex`` itself does not budge, so a
#: comparison of that one number would call the write a no-op, fire nothing, and
#: leave the page believing it still holds two options. That is also the ONE
#: place this fix could have regressed the shipped ``value`` arm, which fired
#: ``change`` unconditionally.
#:
#: The stale guard is value-equality at the index, and its residual is named
#: here rather than discovered later: a list replaced between the read and the
#: write whose option at that index happens to carry the SAME ``value`` passes
#: the guard. That is the correct trade — the alternative is re-sending the whole
#: option list to compare, which is the payload this module's bound exists to
#: avoid — and the caller is not misled, because the selection is still read back
#: and reported.
_APPLY_SELECT_JS = """(select) => {
    const state = () => ({
        selected_index: select.selectedIndex,
        selected_count: select.selectedOptions.length,
        selected_indexes: Array.prototype.map.call(
            select.selectedOptions, (o) => o.index),
        option_count: select.options.length,
        multiple: !!select.multiple
    });
    const same = (a, b) => a.length === b.length
        && a.every((v, i) => v === b[i]);
    const want = __WANT__;
    const option = select.options[want.index];
    if (!option || String(option.value == null ? '' : option.value) !== want.value) {
        return JSON.stringify(Object.assign(
            {applied: false, stale: true}, state()));
    }
    const before = state().selected_indexes;
    select.selectedIndex = want.index;
    if (!same(before, state().selected_indexes)) {
        select.dispatchEvent(new Event('input', {bubbles: true}));
        select.dispatchEvent(new Event('change', {bubbles: true}));
    }
    return JSON.stringify(Object.assign({applied: true, stale: false}, state()));
}"""

#: The ONE ``FileList`` read. No file NAME is read back: the count, the
#: ``multiple`` flag and the total size answer every question the verdict and the
#: record ask, and a name is the caller's document.
READ_FILES_JS = """(input) => {
    const files = input.files;
    return JSON.stringify({
        tag: String(input.tagName || '').toLowerCase(),
        has_files: !!files,
        count: files ? files.length : 0,
        multiple: !!input.multiple,
        total_bytes: files
            ? Array.prototype.reduce.call(files, (n, f) => n + (f.size || 0), 0)
            : 0
    });
}"""


def apply_js(index: int, value: str) -> str:
    """The selection write, addressed at *index* and guarded by *value*."""
    want = json.dumps({"index": int(index), "value": str(value)})
    return _APPLY_SELECT_JS.replace("__WANT__", want)


def _parsed(answer: object, selector: str, what: str) -> dict[str, object]:
    """One ``Element.apply`` answer as the promised JSON object.

    "I could not read it" and "there is nothing there" are different facts, and
    only one of them may be reported as an empty control. The same not-a-policy
    ``text_entry.entered_text`` and ``click_target.aim`` raise, for the same
    reason.
    """
    if not isinstance(answer, str):
        raise ToolError(
            f"could not read {what} of '{selector}': the page answered with "
            f"{type(answer).__name__}, not the promised JSON string"
        )
    try:
        record = json.loads(answer)
    except ValueError:
        raise ToolError(
            f"could not read {what} of '{selector}': the {len(answer)}-character "
            "answer is not valid JSON"
        ) from None
    if not isinstance(record, dict):
        raise ToolError(
            f"could not read {what} of '{selector}': the answer is a "
            f"{type(record).__name__}, not a record"
        )
    return record


async def read_select(element: Element, selector: str) -> dict[str, object]:
    """The ``<select>``'s options and its standing selection, read once."""
    return _parsed(await element.apply(READ_SELECT_JS), selector, "the options")


async def apply_selection(
    element: Element, selector: str, index: int, value: str
) -> dict[str, object]:
    """Select option *index*, fire the events, and read the control back."""
    answer = await element.apply(apply_js(index, value))
    return _parsed(answer, selector, "the selection")


def options_of(facts: dict[str, object]) -> list[dict[str, object]]:
    """The option records a read answered with, typed. ``json.loads`` promises
    nothing, and a bare index into its answer is a cast rather than a check."""
    raw = facts.get("options")
    return [o for o in raw if isinstance(o, dict)] if isinstance(raw, list) else []


def value_at(options: list[dict[str, object]], index: int) -> str:
    """The ``value`` of the option at *index* — the guard the write re-checks."""
    for option in options:
        if option.get("index") == index:
            return _text_of(option, "value")
    return ""


def _text_of(option: dict[str, object], key: str) -> str:
    value = option.get(key)
    return value if isinstance(value, str) else ""


def resolve_option(  # noqa: PLR0911  PERMANENT(one return per matching tier)
    options: list[dict[str, object]],
    *,
    by: str,
    value: str | None = None,
    text: str | None = None,
    index: int | None = None,
) -> int:
    """Which option index the caller's criterion names, or ``-1`` for none.

    The ``value`` arm does NOT skip a ``disabled`` option and the ``text`` arm
    does, because that is the measured asymmetry of the two mechanisms this
    replaces: Chrome permits ``select.value = …`` onto a disabled option, and its
    typeahead refuses one. Neither is second-guessed here (F-876's reasoning for
    the ``disabled`` click).
    """
    if by == BY_INDEX:
        wanted = -1 if index is None else int(index)
        return wanted if 0 <= wanted < len(options) else -1
    if by == BY_VALUE:
        for option in options:
            if _text_of(option, "value") == value:
                return int(option.get("index", -1))
        return -1
    wanted = text or ""
    live = [o for o in options if not o.get("disabled")]
    for key in ("text", "label"):
        for option in live:
            if _text_of(option, key) == wanted:
                return int(option.get("index", -1))
    if not wanted:
        # An empty query prefix-matches everything, which would silently select
        # the first option for a caller who passed "".
        return -1
    lowered = wanted.casefold()
    for option in live:
        if _text_of(option, "text").casefold().startswith(lowered) or _text_of(
            option, "label"
        ).casefold().startswith(lowered):
            return int(option.get("index", -1))
    return -1


def _count(facts: dict[str, object], key: str) -> int:
    raw = facts.get(key)
    return raw if isinstance(raw, int) else 0


def verify_matched(
    selector: str,
    by: str,
    facts: dict[str, object],
    target: int,
    requested_index: int | None = None,
) -> None:
    """Raise unless the criterion names an option of a real ``<select>``.

    Nothing has been written when this raises: the shipped ``value`` arm cleared
    the page's standing selection on its way to answering ``True``, and the
    order here is what makes that unreachable.

    ``requested_index`` exists for exactly one message. An ``index=`` that is
    genuinely inside the control but past :data:`MAX_OPTIONS` is not "no option
    matches" — it is "this control is larger than one read", a different fact
    with a different remedy, and the caller should not be told the option does
    not exist when it does.
    """
    if not facts.get("is_select"):
        raise ToolError(
            f"'{selector}' resolved to <{facts.get('tag') or 'unknown'}>, not a "
            "<select>. Point the selector at a <select> element."
        )
    if target >= 0:
        return
    total = _count(facts, "option_count")
    read = len(options_of(facts))
    if (
        by == BY_INDEX
        and requested_index is not None
        and read <= requested_index < total
    ):
        raise ToolError(
            f"index {requested_index} is within '{selector}''s {total} options "
            f"but beyond the {MAX_OPTIONS}-option read cap, so it could not be "
            "resolved. Nothing was changed."
        )
    raise ToolError(
        f"no option of '{selector}' matches the requested {by} "
        f"({total} option(s) on the control, {read} read). Nothing was changed."
    )


def verify_selected(selector: str, target: int, facts: dict[str, object]) -> None:
    """Raise unless the control now holds the option that was aimed at."""
    if facts.get("stale"):
        raise ToolError(
            f"the options of '{selector}' changed between reading them and "
            f"selecting index {target}, so the index no longer names the option "
            "it was resolved from. Nothing was changed."
        )
    landed = facts.get("selected_index")
    if landed != target:
        raise ToolError(
            f"selected index {target} of '{selector}' but the control now holds "
            f"index {landed} (of {_count(facts, 'option_count')}) — the page did "
            "not keep the selection. A script bound to the control's change "
            "event may have reset it."
        )


def select_record(
    selector: str, by: str, before: dict[str, object], after: dict[str, object]
) -> dict[str, object]:
    """What ``select_option`` answers with — counts and indices, never text."""
    return {
        "selector": selector,
        "by": by,
        "selected_index": after.get("selected_index"),
        "selected_count": _count(after, "selected_count"),
        "option_count": _count(after, "option_count"),
        "multiple": bool(after.get("multiple")),
        "changed": before.get("selected_indexes") != after.get("selected_indexes"),
    }


def require_file_input(element: Element, selector: str) -> None:
    """Raise unless *element* is an ``<input type="file">``.

    The file-input twin of :func:`verify_matched`'s ``is_select`` gate, and for
    the same reason it sits BEFORE the write: ``DOM.setFileInputFiles`` against
    anything else is a CDP error at best, and the caller pointed the selector at
    a control this tool cannot answer for. Read from what nodriver already
    holds (``tag_name``/``attrs``) — no round trip — and tolerant of a double
    that carries neither, which is why an EMPTY tag or type passes: absence of
    metadata is not evidence of the wrong control.
    """
    tag_name = (getattr(element, "tag_name", "") or "").lower()
    input_type = ""
    if hasattr(element, "attrs") and element.attrs:
        input_type = (element.attrs.get("type") or "").lower()
    if tag_name and tag_name != "input":
        raise ToolError(
            f"Selector '{selector}' resolved to <{tag_name}>, not a file input. "
            'Point the selector at an <input type="file"> element.'
        )
    if input_type and input_type != "file":
        raise ToolError(
            f"Selector '{selector}' is an <input type=\"{input_type}\">, "
            'not type="file".'
        )


async def read_files(element: Element, selector: str) -> dict[str, object]:
    """The ``FileList`` the input holds now, read once."""
    return _parsed(await element.apply(READ_FILES_JS), selector, "the attached files")


def verify_attached(selector: str, requested: int, facts: dict[str, object]) -> None:
    """Raise unless the input holds exactly the number of files that were sent.

    The zero case — an input that took nothing — is this check's lower bound
    rather than a second one: ``upload_file`` could not report an empty
    ``FileList`` at all, because it never looked at one.
    """
    attached = _count(facts, "count")
    if attached == requested:
        return
    hint = (
        " The input has no `multiple` attribute, and Chrome keeps only the first "
        "file when more than one is sent to such an input."
        if requested > 1 and not facts.get("multiple")
        else ""
    )
    raise ToolError(
        f"sent {requested} file(s) to '{selector}' but the input holds "
        f"{attached}.{hint}"
    )


def upload_record(
    selector: str, requested: int, facts: dict[str, object]
) -> dict[str, object]:
    """What ``upload_file`` answers with — counts and bytes, never a path."""
    return {
        "selector": selector,
        "requested": requested,
        "attached": _count(facts, "count"),
        "multiple": bool(facts.get("multiple")),
        "total_bytes": _count(facts, "total_bytes"),
    }
