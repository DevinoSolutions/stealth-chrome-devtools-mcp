"""THE one home for WHERE a declaration lives (F-849/F-857).

``animation_edits`` answers *what bytes do I change*; this leaf answers the
question underneath it — *where are those bytes, and what do I do when there
are none*. Both halves used to sit inside the recipe builder, and both got the
same thing wrong for the same reason: an address was computed and then thrown
away, or invented when none existed.

* **Locating.** A rule's AUTHOR text is sliced out of the sheet the collector
  captured, never out of Chrome's ``cssText`` — that is a re-serialization which
  matches nothing on disk (F-849). A ``Span`` keeps the offset the slice was
  taken at, so a recipe can report the line to open instead of only the string
  to search for; ``open_location`` says what that offset is measured IN, which
  for a ``<style>`` block is the element's own text and NOT the document that
  contains it.
* **Failing to locate.** When nothing declares the thing at all, three genuinely
  different causes — an inline ``style=""``, a stylesheet actually refused as
  cross-origin, and a constructed sheet adopted at runtime — used to share one
  guessed sentence that was wrong in two of them (D6). Each is now decided by a
  fact the collector already sent.

Turning a url into a path on disk is deliberately NOT here: that is
``extract_related_files``, the one URL -> file answerer.

A leaf directly above ``animation_facts``: it imports no other embedded module.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from stealth_chrome_devtools_mcp.embedded.animation_facts import (
    Facts,
    Record,
    as_obj,
    as_rows,
    as_strings,
    as_text,
    keyframe_offsets,
)

# ---------------------------------------------------------------------------
# Source addressing — locating a rule's AUTHOR text (F-849)
# ---------------------------------------------------------------------------


def source_by_id(facts: Facts, source_id: object) -> Record | None:
    if not source_id:
        return None
    for source in as_rows(facts.get("sources")):
        if source.get("id") == source_id:
            return source
    return None


def stylesheet_file(source: Record | None) -> str | None:
    """A human-usable file name for a source: the href, else the sheet index."""
    if not source:
        return None
    sheet = as_obj(source.get("stylesheet"))
    href = sheet.get("href")
    if isinstance(href, str) and href:
        return href
    return f"<style> #{sheet.get('index')}"


def open_location(facts: Facts, source: Record) -> Record | None:
    """What an editor opens to reach this rule, and what offsets are counted in.

    The join the v2 audit asked for, made at the one place that holds both
    halves — and taken no further. A linked sheet's own url is openable; a
    ``<style>`` block's is the DOCUMENT that contains it, and a recipe's
    ``char_offset`` is then into the element's own text rather than the
    document's bytes, so the two are named apart instead of conflated. A
    constructed sheet gets nothing at all: its bytes exist only inside the
    script that called ``replaceSync``, and any url would be a fabricated
    address — the failure mode this whole module exists to avoid.
    """
    sheet = as_obj(source.get("stylesheet"))
    href = as_text(sheet.get("href"))
    if href:
        return {"url": href}
    if as_text(sheet.get("kind")) != "style":
        return None
    url = as_text(facts.get("url"))
    if not url:
        return None
    opened: Record = {"url": url}
    if source.get("source_text_available"):
        opened["offsets_in"] = "style_element_text"
    return opened


def _whitespace_tolerant(text: str) -> str:
    """A regex matching ``text`` with any whitespace run where it has one."""
    return re.sub(r"\\?\s+", r"\\s+", re.escape(text.strip()))


class Span(NamedTuple):
    """A located run of author text, and WHERE it was located.

    ``start`` is the offset of ``text`` inside ``raw``, the whole sheet. It used
    to be computed and thrown away, which left every recipe saying "find this
    string in <style> #0" — a search hint rather than an address. Carrying the
    offset (and the sheet it is an offset into) is what lets a recipe report the
    line an editor should open, without anybody re-deriving it from the text.
    """

    start: int
    text: str
    raw: str


def line_column(raw: str, offset: int) -> tuple[int, int]:
    """The 1-based line and column of ``offset`` in ``raw``, as an editor counts."""
    line = raw.count("\n", 0, offset) + 1
    column = offset - (raw.rfind("\n", 0, offset) + 1) + 1
    return line, column


def rule_span(raw: str, header: str) -> Span | None:
    """The AUTHOR's text of one rule body, located in the sheet's raw source.

    ``header`` is the CSSOM selector (or ``@keyframes name``). CSSOM normalizes
    whitespace inside a selector, so the search is whitespace-tolerant; the
    braces are then counted in the ORIGINAL text, which keeps every offset
    honest. Returns ``None`` when the rule cannot be located — a caller must
    then degrade, never guess.

    A header that occurs MORE THAN ONCE also returns ``None``: declaring the
    same selector twice is ordinary CSS, and we have no way to tell which block
    the CSSOM record came from. Picking the first would be a coin flip dressed
    as a verified address.
    """
    if not raw or not header:
        return None
    pattern = r"(?<![\w.#\-])" + _whitespace_tolerant(header) + r"\s*\{"
    matches = list(re.finditer(pattern, raw))
    if len(matches) != 1:
        return None
    match = matches[0]
    depth = 0
    for index in range(match.end() - 1, len(raw)):
        if raw[index] == "{":
            depth += 1
        elif raw[index] == "}":
            depth -= 1
            if depth == 0:
                return Span(match.end(), raw[match.end() : index], raw)
    return None


def source_span(facts: Facts, source: Record | None) -> Span | None:
    """The AUTHOR's text of the rule ``source`` points at, or ``None``.

    The sheet's raw text is carried once in ``facts["raw_sources"]``; each rule's
    span is sliced out here rather than shipped per rule.
    """
    if not source:
        return None
    raw_by_sheet = as_obj(facts.get("raw_sources"))
    index = as_obj(source.get("stylesheet")).get("index")
    raw = raw_by_sheet.get(str(index))
    if not isinstance(raw, str):
        return None
    if source.get("kind") == "keyframes":
        header = f"@keyframes {as_text(source.get('name'))}"
    else:
        header = as_text(source.get("selector_text"))
    return rule_span(raw, header)


def keyframe_spans(span: Span) -> list[tuple[list[float], Span]]:
    """Each keyframe block inside a ``@keyframes`` body: its offsets, its text.

    Needed so a recipe for the keyframe at 1.0 does not hand back the ``0%``
    block's declaration: the property name repeats in every block, so a
    body-wide search always returns the first one.
    """
    blocks: list[tuple[list[float], Span]] = []
    body = span.text
    index = 0
    while index < len(body):
        brace = body.find("{", index)
        if brace == -1:
            break
        selector = body[index:brace]
        depth = 0
        end = len(body) - 1
        for pos in range(brace, len(body)):
            if body[pos] == "{":
                depth += 1
            elif body[pos] == "}":
                depth -= 1
                if depth == 0:
                    end = pos
                    break
        blocks.append(
            (
                keyframe_offsets(selector.strip()),
                Span(span.start + brace + 1, body[brace + 1 : end], span.raw),
            )
        )
        index = end + 1
    return blocks


def keyframe_span_for(span: Span | None, offset: float) -> Span | None:
    """The author's text of the keyframe block that declares ``offset``."""
    if span is None:
        return None
    for offsets, block in keyframe_spans(span):
        if offset in offsets:
            return block
    return None


def author_declaration(span: str, prop: str) -> tuple[str, str, int, int] | None:
    """The author's declaration of ``prop``: ``(literal, value, count, offset)``.

    ``literal`` is the text exactly as typed — the thing a find/replace looks
    for — and ``literal`` always ENDS with ``value``, which is what lets a token
    offset inside the value map onto the literal. ``offset`` is where ``literal``
    starts inside ``span``, so a caller holding the span's own offset can report
    an absolute position without searching the text a second time.

    The lookbehind matters: searching for ``animation`` must not match inside
    ``animation-duration``.
    """
    pattern = rf"(?<![\w-]){re.escape(prop)}\s*:\s*([^;{{}}]+)"
    matches = list(re.finditer(pattern, span or ""))
    if not matches:
        return None
    first = matches[0]
    return (
        first.group(0).strip(),
        first.group(1).strip(),
        len(matches),
        first.start(),
    )


# ---------------------------------------------------------------------------
# Where the declaration actually lives when there is nothing to find (F-857/D6)
# ---------------------------------------------------------------------------

_STYLED = frozenset({"animation", "transition"})
_VAR_REFERENCE = re.compile(r"var\(\s*(--[\w-]+)")


def inline_declarations(facts: Facts) -> list[str]:
    """The animation/transition properties this element carries in ``style=""``."""
    element = as_obj(facts.get("element"))
    return [
        prop
        for prop in as_strings(element.get("inline_properties"))
        if prop.split("-")[0] in _STYLED
    ]


def blocked_stylesheet(facts: Facts) -> str | None:
    """The sheet the collector was actually REFUSED, or ``None`` if none was.

    The difference between a witnessed CORS failure and a guessed one. The
    collector emits ``cross_origin_stylesheet`` at the exact moment
    ``sheet.cssRules`` throws, so its absence means every enumerable sheet was
    read — and "likely cross-origin" is then a claim the payload's own warnings
    contradict.
    """
    for warning in as_rows(facts.get("warnings")):
        if as_text(warning.get("code")) == "cross_origin_stylesheet":
            href = as_text(as_obj(warning.get("detail")).get("href"))
            return href or "a stylesheet that reported no href"
    return None


def indirect_property(declared: str) -> str | None:
    """The custom property a declared value defers to: ``var(--dur)`` -> ``--dur``."""
    match = _VAR_REFERENCE.search(declared or "")
    return match.group(1) if match else None


def missing_source_reason(facts: Facts, subject: str) -> str:
    """Why there is no rule to edit — discriminated, never guessed (D6).

    Three different causes used to share one sentence, "its stylesheet is likely
    cross-origin, so there is nothing here to find/replace". It is a guess in
    all three cases and wrong in two, and it is the worst kind of wrong: it
    sends a weak model hunting through a file that is not involved, which reads
    as work rather than as a dead end. Each branch below is decided by a fact
    the collector already put in the payload.
    """
    inline = inline_declarations(facts)
    if inline:
        return (
            f'{subject} is declared in this element\'s own style="" attribute '
            f"({', '.join(inline)}), not in a stylesheet: edit the attribute, or "
            f"the JavaScript that sets element.style"
        )
    blocked = blocked_stylesheet(facts)
    if blocked:
        return (
            f"no readable CSS rule declares {subject}: the stylesheet at "
            f"{blocked} was refused as cross-origin, so none of its rules were "
            f"captured — open that stylesheet to edit this"
        )
    return (
        f"no rule declares {subject} in any stylesheet this page enumerates, and "
        f"every one of those WAS readable — so the declaration is not in a "
        f"document stylesheet at all. What is left: a constructed sheet adopted "
        f"at runtime (new CSSStyleSheet + replaceSync, which document.styleSheets "
        f"does not list), a shadow root's own <style>, or JavaScript. Edit the "
        f"code that creates it"
    )


def pointer_reason(facts: Facts, edits: list[Record], subject: str) -> str:
    """Why the recipes on this record that are pointers are pointers.

    Two shapes, and the split is mechanical rather than editorial. When NO
    recipe on the record named a rule, nothing was located at all and the cause
    is a property of the document, stated once here. When some recipe did name
    a rule, the causes differ per knob — a ``@layer`` that makes the winner
    undecidable, a ``var()`` indirection, a shorthand token that cannot be
    isolated — and each recipe's own ``note`` already carries its own; restating
    five variants of them at record level would be the payload tax R12 measured.
    """
    if any("rule_selector" in edit for edit in edits):
        return (
            "the knobs listed in not_editable are rule pointers rather than "
            "edits; each one's note says why it could not be addressed"
        )
    return missing_source_reason(facts, subject)
