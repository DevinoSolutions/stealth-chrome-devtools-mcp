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

A leaf between ``animation_facts`` and ``animation_advice``: it reads CSS tokens
and source text, and imports no other embedded module beyond ``animation_facts``.
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
    keyframe_offsets,
    specificity,
    split_css_list,
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


def _whitespace_tolerant(text: str) -> str:
    """A regex matching ``text`` with any whitespace run where it has one."""
    return re.sub(r"\\?\s+", r"\\s+", re.escape(text.strip()))


def rule_span(raw: str, header: str) -> str | None:
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
                return raw[match.end() : index]
    return None


def source_span(facts: Facts, source: Record | None) -> str | None:
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


def keyframe_spans(span: str) -> list[tuple[list[float], str]]:
    """Each keyframe block inside a ``@keyframes`` body: its offsets, its text.

    Needed so a recipe for the keyframe at 1.0 does not hand back the ``0%``
    block's declaration: the property name repeats in every block, so a
    body-wide search always returns the first one.
    """
    blocks: list[tuple[list[float], str]] = []
    index = 0
    while index < len(span):
        brace = span.find("{", index)
        if brace == -1:
            break
        selector = span[index:brace]
        depth = 0
        end = len(span) - 1
        for pos in range(brace, len(span)):
            if span[pos] == "{":
                depth += 1
            elif span[pos] == "}":
                depth -= 1
                if depth == 0:
                    end = pos
                    break
        blocks.append((keyframe_offsets(selector.strip()), span[brace + 1 : end]))
        index = end + 1
    return blocks


def keyframe_span_for(span: str | None, offset: float) -> str | None:
    """The author's text of the keyframe block that declares ``offset``."""
    if not span:
        return None
    for offsets, text in keyframe_spans(span):
        if offset in offsets:
            return text
    return None


def author_declaration(span: str, prop: str) -> tuple[str, str, int] | None:
    """The author's own declaration of ``prop``: ``(literal, value, count)``.

    ``literal`` is the text exactly as typed — the thing a find/replace looks
    for — and ``literal`` always ENDS with ``value``, which is what lets a token
    offset inside the value map onto the literal.

    The lookbehind matters: searching for ``animation`` must not match inside
    ``animation-duration``.
    """
    pattern = rf"(?<![\w-]){re.escape(prop)}\s*:\s*([^;{{}}]+)"
    matches = list(re.finditer(pattern, span or ""))
    if not matches:
        return None
    first = matches[0]
    return first.group(0).strip(), first.group(1).strip(), len(matches)


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
      computes, which means something we never saw (an inline style, a UA sheet,
      an unreadable stylesheet) is in charge.
    """
    scope = as_rows(facts.get("matched_rules")) if rules is None else rules
    candidates: list[tuple[int, Record, list[str]]] = []
    for position, rule in enumerate(scope):
        declares = as_obj(rule.get("declares"))
        props = [p for p in (knob.longhand, knob.shorthand) if p in declares]
        if props:
            candidates.append((position, rule, props))
    if not candidates:
        return Winner(
            None,
            [],
            "low",
            "no readable CSS rule declares this knob; it may come from a "
            "cross-origin stylesheet, a UA default, or JavaScript",
        )

    def rank(
        item: tuple[int, Record, list[str]],
    ) -> tuple[int, tuple[int, int, int], int]:
        position, rule, props = item
        important = any(p in as_strings(rule.get("important")) for p in props)
        spec = specificity(as_text(rule.get("selector_text"))) or (0, 0, 0)
        return (int(important), spec, position)

    _, rule, props = max(candidates, key=rank)
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
            rule,
            props,
            "low",
            f"the rule that should win declares {contributed!r} for this knob but "
            f"the element computes {knob.current!r}, so something not captured "
            f"here (an inline style, a UA sheet, an unreadable stylesheet) is in "
            f"charge; edit that instead",
        )
    return Winner(rule, props, "high")


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
    conclusion about the edit, not the absence of one."""

    literal: str
    value: str
    confidence: str
    reason: str = ""


def locate(span: str | None, prop: str) -> Located:
    """Whether ``prop``'s declaration is findable in the author's text, and how
    sure. The confidence is produced HERE, by the branch that actually looked,
    and no caller can upgrade it (F-850)."""
    found = author_declaration(span, prop) if span else None
    if found is None:
        return Located(
            "",
            "",
            "low",
            "the author's text for this rule is not readable, so no find literal "
            "is offered; open the rule at source_ref and edit it there",
        )
    literal, value, occurrences = found
    if occurrences == 1:
        return Located(literal, value, "high")
    return Located(
        literal,
        value,
        "medium",
        "this declaration repeats in the rule; match by position",
    )


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
    if prop == knob.longhand:
        # The knob's own longhand: the token is this animation's item of the
        # comma list. Taking the whole list would report "2s, 3s" as one
        # animation's duration and rewrite both.
        start, end = item_span(value, knob.list_index)
    else:
        item_start, item_end = item_span(value, knob.list_index)
        inside = token_verdict(knob, value[item_start:item_end])
        if not inside.found:
            recipe["confidence"] = inside.confidence
            recipe["note"] = inside.reason
            return recipe
        start, end = item_start + inside.start, item_start + inside.end
    recipe["find_unique_in_rule"] = located.confidence == "high"
    recipe.update(_swap(literal, value, start, end))
    return recipe


def keyframe_recipe(
    source: Record | None, block: str | None, prop: str, value: str
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
    recipe.update(_swap(located.literal, located.value, 0, len(located.value)))
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
