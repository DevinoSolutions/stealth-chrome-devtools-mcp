"""THE one home for animation/transition EDIT RECIPES (M10, F-849/F-850/F-852).

A recipe answers one question: *what exact bytes do I change, and where?* Three
defects made the old answer worse than no answer at all, and they compounded:

1. The ``find`` literal was built from Chrome's ``cssText``, a re-serialization —
   name moved to the end of the shorthand, ``.68`` expanded to ``0.68``, spaces
   inserted, ``running`` injected. It matched nothing on disk (F-849).
2. Every timing knob got the SAME ``find``: the whole declaration, each at
   ``confidence: "high"``, differing only in ``current``. "Find this, replace it
   with the new duration" turns ``.card { animation: fade 2s ease; }`` into
   ``.card { 3s; }`` — a file-corrupting instruction at our highest confidence.
3. The rule was picked by document order alone, ignoring specificity and
   ``!important``, while ``current`` came from computed style. So a recipe could
   confidently address a rule whose edit cannot change what renders.

So a recipe now carries three things that make it mechanically applicable
without re-parsing CSS: the AUTHOR's whole declaration as ``find`` (scoped to,
and verified within, the rule that actually wins the cascade for that knob), the
single ``token`` inside it that this knob owns, and ``replace`` — that same
declaration with only that token swapped for a named placeholder. Applying it
cannot drop the rest of the declaration, because the rest of the declaration is
carried in the replacement.

Every step that cannot be completed degrades to a rule pointer with no ``find``
rather than guessing, and says which step failed.

WHERE those bytes are — locating a rule's author text in the sheet, the openable
location, and which of the three causes it is when nothing is locatable — is
``animation_source``, the leaf below this one. This module composes recipes out
of what that returns; it never goes looking for a rule's text itself.

A leaf between ``animation_source`` and ``animation_advice``: it reads CSS
tokens, and imports no embedded module beyond those two levels.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from stealth_chrome_devtools_mcp.embedded.animation_facts import (
    Facts,
    Record,
    as_number,
    as_obj,
    as_rows,
    as_strings,
    as_text,
    cycle_get,
    duration_ms,
    iteration_count,
    specificity,
    split_css_list,
    warn,
)
from stealth_chrome_devtools_mcp.embedded.animation_source import (
    Span,
    author_declaration,
    indirect_property,
    inline_declarations,
    keyframe_span_for,
    line_column,
    source_by_id,
    source_span,
    stylesheet_file,
)

# Measured, not chosen round (R12). After the boilerplate hoist below a recipe
# averages ~310 bytes, so 20 recipes is ~6KB: the 5 timing knobs plus 15
# keyframe declarations. The old 40 produced 19KB of edits on a single record --
# more than the keyframes, checkpoints, timing and prose put together.
EDIT_CAP = 20

# The marker a recipe's ``replace`` carries where the new value goes. It appears
# INSIDE every ``replace`` string, so it is discoverable in place; the
# instruction for using it is stated ONCE at the top of the payload
# (``edit_protocol``) instead of being repeated per recipe. That repetition was
# 6,960 bytes -- 36% of the edits block -- on one measured record (R12).
PLACEHOLDER = "{{NEW_VALUE}}"

EDIT_PROTOCOL = {
    "placeholder": PLACEHOLDER,
    "how": (
        "To apply an edit: find the recipe's `find` string in the file named by "
        "`file`, inside the rule named by `rule_selector`, and replace it with "
        f"the recipe's `replace` string, putting your new value where "
        f"{PLACEHOLDER} appears. The rest of the declaration is carried in "
        "`replace` for you, so nothing else can be lost. `token` is the exact "
        "text being replaced, if you want to check it first. A recipe with no "
        "`find` is a pointer, not an edit: open it at `source_ref` yourself."
    ),
    # Said ONCE, for the same reason `how` is (R12): a frame-of-reference
    # sentence repeated on 20 recipes is pure payload tax, and the frame is a
    # property of the SHEET, not of any one recipe.
    "open": (
        "A recipe that has a `find` also has `char_offset`, `line` and `column`: "
        "where that literal starts. They are measured in the text named by "
        "`open.offsets_in` on this recipe's `source_ref` entry in `sources`. "
        "`style_element_text` means the text INSIDE that <style> element, NOT "
        "the bytes of the document at `open.url` — open the url, find the "
        "element, then count from there. `open.url` is a page URL and not a "
        "path on disk: map it with extract_related_files, which is the one tool "
        "that answers URL -> file."
    ),
}

_KNOB_LONGHAND = {
    "name": "animation-name",
    "duration": "animation-duration",
    "delay": "animation-delay",
    "easing": "animation-timing-function",
    "iterations": "animation-iteration-count",
}

_TRANSITION_LONGHAND = {
    "property": "transition-property",
    "duration": "transition-duration",
    "delay": "transition-delay",
    "easing": "transition-timing-function",
}

_TIME = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:ms|s)$", re.IGNORECASE)
_BARE_NUMBER = re.compile(r"^\d+\.?\d*$|^\.\d+$")
_EASING_WORDS = {
    "linear",
    "ease",
    "ease-in",
    "ease-out",
    "ease-in-out",
    "step-start",
    "step-end",
}
_EASING_FUNCTIONS = ("cubic-bezier(", "steps(", "linear(")

# ``!important`` belongs to the DECLARATION, not to any comma item of its value,
# so no knob's token may include it. A ``replace`` that swallowed the priority
# would turn "set the duration to 3s" into "set the duration to 3s AND stop
# winning the cascade" — precisely the "applying it cannot drop the rest of the
# declaration" promise this module exists to keep. It is not a rare path either:
# ``winning_rule`` ranks ``!important`` FIRST, so an important rule is the one a
# recipe most often points at.
_PRIORITY = re.compile(r"\s*!\s*important\s*$", re.IGNORECASE)


def value_body(value: str) -> str:
    """A declaration's value without its ``!important`` priority.

    A prefix of ``value``, so an offset into it is also an offset into ``value``
    and ``_swap``'s arithmetic is unchanged.
    """
    return _PRIORITY.sub("", value)


# ---------------------------------------------------------------------------
# Which rule wins the cascade for THIS knob (R2)
# ---------------------------------------------------------------------------


class Knob(NamedTuple):
    """One tweakable thing: what to call it, the value the browser currently
    computes for it, the longhand that can carry it, the shorthand that can also
    carry it, and — for a comma list — which item belongs to this animation."""

    name: str
    current: str
    longhand: str
    shorthand: str
    list_index: int = 0


class Winner(NamedTuple):
    """The rule that decides this knob, and how sure we are of that.

    ``rule`` is ``None`` when nothing readable declares the knob at all — still
    a verdict with a confidence attached, not an absence, so no caller has to
    invent one (F-850).

    ``props`` is every declaration in that rule which could carry the knob, in
    the order to TRY them against the author's text. This is not pedantry: a
    rule written as ``animation: fade 2s ease`` reports BOTH ``animation`` and
    ``animation-duration`` through the CSSOM, because the shorthand sets every
    longhand. The longhand is authoritative for the VALUE (it already reflects
    whichever declaration in that rule won), but only the shorthand exists in
    the file — so value and find-target can come from different declarations,
    and picking one for both breaks whichever authoring style you did not pick.
    """

    rule: Record | None
    props: list[str]
    confidence: str
    reason: str = ""


def applying_rule(facts: Facts, prefix: str = "animation") -> Record | None:
    """The last currently-matching rule declaring anything under ``prefix``.

    Still the right answer for "where does this element's motion come from" —
    but NOT for "what do I edit to change this knob", which is ``winning_rule``.
    """
    found = None
    for rule in as_rows(facts.get("matched_rules")):
        if any(prop.startswith(prefix) for prop in as_obj(rule.get("declares"))):
            found = rule
    return found


def rule_declaring(facts: Facts, name: str, pseudo: str = "") -> Record | None:
    """The rule that declares animation ``name``, optionally on ``pseudo``.

    A ``::before`` animation's rule never "matches" the element itself, so it
    lands in ``candidate_rules``; both lists are searched. Without this, a
    pseudo-element animation came back with empty ``edits`` and no reason —
    silence, where its rule was sitting in the same ``<style>`` block (F-849).
    """
    rules = as_rows(facts.get("matched_rules")) + as_rows(facts.get("candidate_rules"))
    found = None
    for rule in rules:
        declared = " ".join(
            str(value) for value in as_obj(rule.get("declares")).values()
        )
        if name not in split_css_list(declared) and name not in declared.split():
            continue
        selector = as_text(rule.get("selector_text"))
        has_pseudo = "::" in selector
        if pseudo:
            if pseudo not in selector and pseudo.replace("::", ":") not in selector:
                continue
        elif has_pseudo:
            continue
        found = rule
    return found


def _layer_of(rule: Record) -> str | None:
    """Which ``@layer`` block this rule sits in, or ``None`` when unlayered.

    Membership only. Layer ORDER is deliberately not inferred: a bare
    ``@layer a, b;`` statement declares it, that rule has no ``cssRules``, and
    the collector's walk therefore never sees it. Which is exactly why a
    disagreement between candidates is undecidable here rather than rankable —
    the cascade puts every layered declaration below every unlayered one, so
    ranking by specificity would hand the win to a rule that cannot render.
    """
    for context in as_strings(rule.get("at_rule_context")):
        if context.strip().lower().startswith("@layer"):
            return context.strip()
    return None


def contributed_value(knob: Knob, rule: Record, props: list[str]) -> str | None:
    """What ``rule`` gives this knob, or ``None`` when it cannot be read.

    The longhand answers first when the rule reports one, because a
    ``CSSStyleDeclaration``'s longhand value already reflects whichever
    declaration in that rule set it last — shorthand or not. Only when the rule
    reports the shorthand alone is the token pulled out of it.
    """
    declares = as_obj(rule.get("declares"))
    for prop in props:
        declared = as_text(declares.get(prop))
        if not declared:
            continue
        item = cycle_get(split_css_list(declared), knob.list_index, declared)
        if prop == knob.longhand:
            return item
        found = token_verdict(knob, item)
        if found.found:
            return item[found.start : found.end]
    return None


def winning_rule(facts: Facts, knob: Knob, rules: list[Record] | None = None) -> Winner:
    """The rule whose declaration actually decides ``knob``.

    ``rules`` defaults to the rules currently matching the element. A
    pseudo-element or descendant animation reaches us through
    ``getAnimations()``, and its rule never matches the element itself, so its
    caller hands in the one rule it identified instead (F-849).

    Specificity, then ``!important``, then document order — the cascade, rather
    than "the last rule that mentioned animation anything" (R2). Two situations
    return a LOW-confidence winner instead of a confident one, and in both the
    caller must omit ``find``:

    * a functional pseudo-class (``:is``/``:where``/``:not``/``:has``) whose
      specificity depends on its argument, with more than one candidate — we did
      not compute that cascade and will not pretend to;
    * a winner whose declared value disagrees with what the element actually
      computes, which means the knob is set somewhere this rule cannot reach —
      ``_mismatch_reason`` decides WHICH somewhere, from the facts, rather than
      listing suspects (D6).
    """
    scope = as_rows(facts.get("matched_rules")) if rules is None else rules
    candidates: list[tuple[int, Record, list[str]]] = []
    for position, rule in enumerate(scope):
        declares = as_obj(rule.get("declares"))
        props = [p for p in (knob.longhand, knob.shorthand) if p in declares]
        if props:
            candidates.append((position, rule, props))
    if not candidates:
        # Short on purpose, and it does NOT name a cause: the cause is the same
        # for every knob on the record and is a property of the document, so it
        # is discriminated once in the record's not_editable_reason rather than
        # guessed five times here (D6).
        return Winner(
            None,
            [],
            "low",
            "no readable CSS rule declares this knob; this record's "
            "not_editable_reason says where the declaration actually lives",
        )

    def rank(
        item: tuple[int, Record, list[str]],
    ) -> tuple[int, tuple[int, int, int], int]:
        position, rule, props = item
        important = any(p in as_strings(rule.get("important")) for p in props)
        spec = specificity(as_text(rule.get("selector_text"))) or (0, 0, 0)
        return (int(important), spec, position)

    _, rule, props = max(candidates, key=rank)
    if len({_layer_of(other) for _, other, _ in candidates}) > 1:
        return Winner(
            rule,
            props,
            "low",
            "these rules are not all in the same @layer: an unlayered "
            "declaration beats a layered one however specific the layered "
            "selector is, and the layer ORDER is not readable from here, so "
            "which of them renders is not decidable — open them at source_ref",
        )
    undecidable = any(
        specificity(as_text(other.get("selector_text"))) is None
        for _, other, _ in candidates
    )
    if undecidable and len(candidates) > 1:
        return Winner(
            rule,
            props,
            "low",
            "more than one rule declares this and at least one uses a functional "
            "pseudo-class (:is/:where/:not/:has) whose specificity depends on its "
            "argument, so which of them renders is not decidable from here",
        )
    contributed = contributed_value(knob, rule, props)
    if contributed is None or not knob_matches(knob.name, contributed, knob.current):
        return Winner(
            rule, props, "low", _mismatch_reason(facts, knob, rule, props, contributed)
        )
    return Winner(rule, props, "high")


def _mismatch_reason(
    facts: Facts, knob: Knob, rule: Record, props: list[str], contributed: str | None
) -> str:
    """Why the rule that should win cannot be edited to move this knob.

    Reached when the declared value and the computed one disagree, which used to
    be answered with one sentence listing three suspects ("an inline style, a UA
    sheet, an unreadable stylesheet") and telling the reader to "edit that
    instead" — without saying which, or where. Two of those three are decidable
    from facts already in the payload, so they are decided here (D6); only the
    genuinely unknown case keeps a list, and it no longer pretends to name a
    remedy it cannot.
    """
    declares = as_obj(rule.get("declares"))
    custom = indirect_property(" ".join(as_text(declares.get(p)) for p in props))
    if custom:
        # Not a missing stylesheet: an indirection the author WANTED, which the
        # payload named. The custom property is the whole answer.
        return (
            f"this rule sets the {knob.name} through {custom}, so the author's "
            f"text has no {knob.name} literal to swap — Chrome resolves it to "
            f"what `current` reports. Change {custom}'s own declaration, or add "
            f"an override for it on this selector"
        )
    inline = [
        prop
        for prop in inline_declarations(facts)
        if prop in {knob.longhand, knob.shorthand}
    ]
    if inline:
        # An inline declaration outranks every rule, and the collector saw it.
        return (
            f'this element\'s style="" attribute sets {", ".join(inline)}, which '
            f"outranks every rule, so editing this rule would not change what "
            f"renders; edit the attribute (or the JavaScript that sets "
            f"element.style)"
        )
    return (
        f"the rule that should win declares {contributed!r} for this knob but "
        f"the element computes {knob.current!r}; something this capture did not "
        f"see (a UA stylesheet, or a sheet whose rules were unreadable) is in "
        f"charge, so editing this rule would not change what renders"
    )


# ---------------------------------------------------------------------------
# Which TOKEN inside the declaration this knob owns (R1)
# ---------------------------------------------------------------------------


def _is_easing(token: str) -> bool:
    text = token.strip().lower()
    return text in _EASING_WORDS or text.startswith(_EASING_FUNCTIONS)


def normalize_easing(token: str) -> object:
    """An easing in a form two spellings of the same curve compare equal in.

    ``cubic-bezier(.68,-0.55,.27,1.55)`` and Chrome's
    ``cubic-bezier(0.68, -0.55, 0.27, 1.55)`` are the same curve; string
    comparison says otherwise, and that difference is the whole of F-849.
    """
    text = token.strip().lower()
    match = re.fullmatch(r"cubic-bezier\(([^)]*)\)", text)
    if match:
        try:
            return tuple(float(part) for part in match.group(1).split(","))
        except ValueError:
            return text
    return re.sub(r"\s+", "", text)


def knob_matches(knob_name: str, token: str, expected: str) -> bool:
    """Does ``token`` mean the same thing as ``expected`` for this knob?

    Compared by MEANING, never by spelling: ``2400ms`` and ``2.4s`` are one
    duration, and ``4.0`` and ``4`` are one iteration count.
    """
    if knob_name in {"duration", "delay"}:
        return duration_ms(token) == duration_ms(expected)
    if knob_name == "iterations":
        return iteration_count(token) == iteration_count(expected)
    if knob_name == "easing":
        return normalize_easing(token) == normalize_easing(expected)
    return token.strip() == expected.strip()


class TokenSpan(NamedTuple):
    """Where a knob's token sits inside a shorthand value, welded to the
    confidence the branch that looked produced. ``start < 0`` means it could not
    be identified without guessing — still a verdict, not an absence (F-850)."""

    start: int
    end: int
    confidence: str
    reason: str = ""

    @property
    def found(self) -> bool:
        return self.start >= 0


def token_verdict(knob: Knob, value: str) -> TokenSpan:
    """Where inside a SHORTHAND value this knob's token sits, with confidence.

    The CSS shorthand grammar is positional only for the two ``<time>`` values —
    the first is the duration, the second the delay — so those are read by
    position and everything else by shape. Anything ambiguous (an animation
    literally named ``ease``, say, which is both a name and an easing keyword)
    returns ``None``, and the caller degrades: a misread token would rewrite the
    wrong part of a working declaration.

    The identified token is finally checked against what the browser computed
    for this knob, so a shorthand we merely *think* we parsed cannot be offered.
    """
    tokens = [
        (text, start, end)
        for text, start, end in tokens_with_spans(value)
        if text  # a trailing separator produces nothing
    ]
    times = [item for item in tokens if _TIME.match(item[0])]
    if knob.name == "duration":
        found = times[:1]
    elif knob.name == "delay":
        found = times[1:2]
    elif knob.name == "easing":
        found = [item for item in tokens if _is_easing(item[0])]
    elif knob.name == "iterations":
        found = [
            item
            for item in tokens
            if item[0].lower() == "infinite" or _BARE_NUMBER.match(item[0])
        ]
    else:
        # name / property: the identifier that IS the value we are looking for.
        found = [item for item in tokens if item[0] == knob.current.strip()]
    if len(found) != 1 or not knob_matches(knob.name, found[0][0], knob.current):
        return TokenSpan(
            -1,
            -1,
            "low",
            f"the {knob.name} component could not be identified inside this "
            f"shorthand without guessing, so no replacement is offered; the "
            f"declaration itself is at source_ref",
        )
    _, start, end = found[0]
    return TokenSpan(start, end, "high")


def tokens_with_spans(value: str) -> list[tuple[str, int, int]]:
    """Whitespace-separated tokens of ``value`` with their offsets, keeping any
    parenthesised function whole."""
    spans: list[tuple[str, int, int]] = []
    depth = 0
    start = None
    for index, ch in enumerate(value or ""):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch.isspace() and depth == 0:
            if start is not None:
                spans.append((value[start:index], start, index))
                start = None
        elif start is None:
            start = index
    if start is not None:
        spans.append((value[start:], start, len(value)))
    return spans


def item_span(value: str, index: int) -> tuple[int, int]:
    """The offsets of comma item ``index`` (cycling) within a CSS list value."""
    spans: list[tuple[int, int]] = []
    depth = 0
    start = 0
    for position, ch in enumerate(value or ""):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            spans.append((start, position))
            start = position + 1
    spans.append((start, len(value or "")))
    begin, end = spans[index % len(spans)]
    # Trim the whitespace the split leaves on either side.
    while begin < end and value[begin].isspace():
        begin += 1
    while end > begin and value[end - 1].isspace():
        end -= 1
    return begin, end


# ---------------------------------------------------------------------------
# The recipe
# ---------------------------------------------------------------------------


class Located(NamedTuple):
    """A declaration found in the author's text, welded to the confidence the
    branch that found it produced. ``literal`` is empty when nothing was found —
    the confidence still travels, because "I could not read the source" is a
    conclusion about the edit, not the absence of one.

    ``offset`` is where ``literal`` starts in the sheet, or ``-1`` when there is
    nothing to point at."""

    literal: str
    value: str
    confidence: str
    reason: str = ""
    offset: int = -1


def locate(span: Span | None, prop: str) -> Located:
    """Whether ``prop``'s declaration is findable in the author's text, and how
    sure. The confidence is produced HERE, by the branch that actually looked,
    and no caller can upgrade it (F-850)."""
    found = author_declaration(span.text, prop) if span else None
    if found is None or span is None:
        return Located(
            "",
            "",
            "low",
            "the author's text for this rule is not readable, so no find literal "
            "is offered; open the rule at source_ref and edit it there",
        )
    literal, value, occurrences, at = found
    if occurrences == 1:
        return Located(literal, value, "high", "", span.start + at)
    return Located(
        literal,
        value,
        "medium",
        "this declaration repeats in the rule; match by position",
        span.start + at,
    )


def _stamp_position(recipe: Record, span: Span | None, located: Located) -> None:
    """Where the ``find`` literal IS, not just what it says.

    Without this the strongest thing a recipe could say was "look for this
    string somewhere inside <style> #0" — the offset that answers it was already
    computed by ``rule_span`` and discarded. The numbers are only ever stamped
    beside a ``find``, so there is no position without a literal to check it
    against; what they are measured IN is stated once, on the source
    (``open.offsets_in``), because it is a property of the sheet.
    """
    if span is None or located.offset < 0:
        return
    line, column = line_column(span.raw, located.offset)
    recipe["char_offset"] = located.offset
    recipe["line"] = line
    recipe["column"] = column


def _pointer(knob: Knob, rule: Record, source: Record | None) -> Record:
    """The part of a recipe that is true even when nothing else could be
    established: which knob, what it is now, and where its rule lives.

    ``rule_selector`` and ``at_rule_context`` are here because
    ``find_unique_in_rule`` is a RULE-scoped claim while ``file`` is file-wide;
    without the rule, the strongest thing the recipe says cannot be checked.
    """
    recipe: Record = {"knob": knob.name, "current": knob.current}
    if source is not None:
        recipe["source_ref"] = source.get("id")
        recipe["file"] = stylesheet_file(source)
    selector = as_text(rule.get("selector_text"))
    if selector:
        recipe["rule_selector"] = selector
    context = as_strings(rule.get("at_rule_context"))
    if context:
        recipe["at_rule_context"] = context
    return recipe


def _swap(literal: str, value: str, start: int, end: int) -> Record:
    """The find/replace pair for swapping ``value[start:end]`` inside ``literal``.

    ``literal`` ends with ``value``, so a value offset shifts by exactly the
    length of the ``property:`` head. The replacement is built by SLICING rather
    than by string substitution, because a token can legitimately appear twice
    in one declaration (``animation: x 2s ease 2s``) and only one of them is the
    one this knob owns.
    """
    head = len(literal) - len(value)
    return {
        "find": literal,
        "token": value[start:end],
        "replace": literal[: head + start] + PLACEHOLDER + literal[head + end :],
    }


def knob_recipe(facts: Facts, knob: Knob, rules: list[Record] | None = None) -> Record:
    """One edit recipe, addressed to the author's bytes and to one token.

    Four things must all succeed: a rule must win the cascade for this knob, its
    author text must be readable, the declaration must be locatable in it, and
    the knob's token must be identifiable inside the declaration. Whichever step
    fails, the recipe degrades to a pointer with no ``find`` and says so — an
    edit instruction that cannot be applied correctly is worse than none.
    """
    winner = winning_rule(facts, knob, rules)
    if winner.rule is None:
        return {
            "knob": knob.name,
            "current": knob.current,
            "confidence": winner.confidence,
            "note": winner.reason,
        }
    source = source_by_id(facts, winner.rule.get("source_ref"))
    recipe = _pointer(knob, winner.rule, source)
    if winner.confidence != "high":
        recipe["confidence"] = winner.confidence
        recipe["note"] = winner.reason
        return recipe
    span = source_span(facts, source)
    # Try each declaration this rule could carry the knob in: the longhand the
    # author may have written, then the shorthand they may have written instead.
    located = locate(span, winner.props[0])
    prop = winner.props[0]
    for candidate in winner.props[1:]:
        if located.literal:
            break
        located, prop = locate(span, candidate), candidate
    recipe["confidence"] = located.confidence
    if located.reason:
        recipe["note"] = located.reason
    if not located.literal:
        return recipe
    literal, value = located.literal, located.value
    # Spans are taken against the value WITHOUT its priority, so `!important`
    # can never end up inside a token a caller is told to replace.
    body = value_body(value)
    if prop == knob.longhand:
        # The knob's own longhand: the token is this animation's item of the
        # comma list. Taking the whole list would report "2s, 3s" as one
        # animation's duration and rewrite both.
        start, end = item_span(body, knob.list_index)
    else:
        item_start, item_end = item_span(body, knob.list_index)
        inside = token_verdict(knob, body[item_start:item_end])
        if not inside.found:
            recipe["confidence"] = inside.confidence
            recipe["note"] = inside.reason
            return recipe
        start, end = item_start + inside.start, item_start + inside.end
    recipe["find_unique_in_rule"] = located.confidence == "high"
    recipe.update(_swap(literal, value, start, end))
    _stamp_position(recipe, span, located)
    return recipe


def keyframe_recipe(
    source: Record | None, block: Span | None, prop: str, value: str
) -> Record:
    """A recipe for one keyframe declaration.

    A keyframe declaration's value is whole — ``box-shadow: a, b`` is one value,
    not a per-animation list — so the token IS the value and there is no
    sub-token to isolate.
    """
    knob = Knob(f"{prop}", value, prop, "", 0)
    recipe: Record = {"knob": knob.name, "current": value}
    if source is not None:
        recipe["source_ref"] = source.get("id")
        recipe["file"] = stylesheet_file(source)
    located = locate(block, prop)
    recipe["confidence"] = located.confidence
    if located.reason:
        recipe["note"] = located.reason
    if not located.literal:
        return recipe
    recipe["find_unique_in_rule"] = located.confidence == "high"
    recipe.update(
        _swap(located.literal, located.value, 0, len(value_body(located.value)))
    )
    _stamp_position(recipe, block, located)
    return recipe


def build_edits(
    facts: Facts,
    record: Record,
    list_index: int = 0,
    rules: list[Record] | None = None,
) -> list[Record]:
    """Edit recipes for one animation: the timing knobs plus every keyframe
    declaration, each addressed to the rule that wins it and the token it owns."""
    edits: list[Record] = []
    timing = as_obj(record.get("timing"))
    iterations = timing.get("iterations")
    for name, current in (
        ("duration", timing.get("duration_raw")),
        ("delay", timing.get("delay_raw")),
        ("easing", timing.get("easing")),
        ("iterations", None if iterations is None else str(iterations)),
        ("name", record.get("name")),
    ):
        if not isinstance(current, str) or not current:
            continue
        edits.append(
            knob_recipe(
                facts,
                Knob(name, current, _KNOB_LONGHAND[name], "animation", list_index),
                rules,
            )
        )
    keyframe_rule = None
    for rule in as_rows(facts.get("keyframe_rules")):
        if rule.get("name") == record.get("name"):
            keyframe_rule = rule
    if keyframe_rule is None:
        return edits
    source = source_by_id(facts, keyframe_rule.get("source_ref"))
    span = source_span(facts, source)
    for frame in as_rows(record.get("keyframes")):
        offset = as_number(frame.get("offset")) or 0.0
        # Scope to THIS keyframe's block: every block declares the same property
        # names, so a body-wide search always returns the first.
        block = keyframe_span_for(span, offset)
        for prop, value in as_obj(frame.get("properties")).items():
            if len(edits) >= EDIT_CAP:
                # Announced, never silent (F-853/F-855). ``caps.truncated``
                # reports animations and keyframes only, so a model handed 20 of
                # 38 recipes would otherwise conclude the keyframes it can find
                # no recipe for are not editable. EDIT_CAP is not caller-
                # settable, so the remedy named here is one the reader has.
                warn(
                    record,
                    "edit_cap_reached",
                    f"edit recipes truncated at {EDIT_CAP}; the keyframes "
                    f"without a recipe are still listed in this record's "
                    f"keyframes array, and their @keyframes block is at "
                    f"source_ref — edit it there. Extract a narrower selector "
                    f"for fewer animations per call.",
                    {"cap": EDIT_CAP, "name": record.get("name")},
                )
                return edits
            recipe = keyframe_recipe(source, block, prop, as_text(value))
            recipe["knob"] = f"keyframe[{frame['offset']}].{prop}"
            edits.append(recipe)
    return edits


def build_transition_edits(
    facts: Facts, record: Record, list_index: int = 0
) -> list[Record]:
    """Edit recipes for one transitioned longhand.

    A transition is as editable as an animation — its ``transition`` declaration
    is in the same rule — but it used to get no ``edits``, no ``editable`` and no
    reason, and absence reads as "editable" (R10). Same knobs, same cascade
    selection, same token addressing, same degradation.
    """
    edits: list[Record] = []
    for name, current in (
        ("duration", record.get("duration_raw")),
        ("delay", record.get("delay_raw")),
        ("easing", record.get("easing")),
        ("property", record.get("property")),
    ):
        if not isinstance(current, str) or not current:
            continue
        edits.append(
            knob_recipe(
                facts,
                Knob(
                    name,
                    current,
                    _TRANSITION_LONGHAND[name],
                    "transition",
                    list_index,
                ),
            )
        )
    return edits
