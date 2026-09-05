"""Adversarial audit of schema v2: the inputs the merged suite does not write.

Every case here is CSS an author really writes and the merged tests never fed
in. They split into two kinds, and the distinction is the point:

* **A recipe that lies.** The payload emits ``confidence: "high"`` plus a
  ``find``/``replace`` pair that either rewrites the wrong thing or cannot
  change what renders. These are worse than a missing recipe, which is the
  premise ``animation_edits`` is built on, so they are the tests that matter.
* **A recipe that degrades.** No ``find``, a note saying why. That is the
  contract working, and it is pinned here so a later "improvement" cannot
  quietly start guessing.

The fixtures are written the way an author writes CSS, never the way Chrome
re-serializes it — bare decimals, name-first shorthand, ``!important`` where an
author would put it. A fixture in CSSOM shape shares a serializer with the code
under test and therefore cannot fail (the trap this repo has already been caught
by once).
"""

from __future__ import annotations

from stealth_chrome_devtools_mcp.embedded.animation_edits import EDIT_CAP
from test_animation_edit_recipes import recipes, rule, source
from test_animation_schema_v2 import computed, extract, facts


def _timing(payload, name):
    """The timing block of the one animation called ``name``."""
    found = [a for a in payload["animations"] if a["name"] == name]
    assert len(found) == 1, (
        f"expected one {name!r}, got {[a['name'] for a in payload['animations']]}"
    )
    return found[0]


class TestAnimationNameNoneKeepsEveryOtherListInStep:
    """``animation-name: fade, none, spin`` is legal CSS, and every other
    ``animation-*`` list stays THREE items long and positional.

    Dropping the ``none`` from the name list without keeping its slot shifts
    every list index after it by one, so ``spin`` inherits the timing of the
    switched-off slot. This is the F-847 list-cycling defect returning by a side
    door: the cycling rule is applied correctly, to the wrong index.

    It is the worst shape a defect can take here — the numbers are not missing,
    they are confidently wrong, and ``derived.total_ms`` is computed from them.
    """

    THREE_SLOTS = facts(
        selector="#hero",
        computed=computed(
            animation_name="fade, none, spin",
            animation_duration="1s, 2s, 3s",
            animation_delay="0s, 0s, 5s",
            animation_iteration_count="1, 1, infinite",
            animation_timing_function="linear, linear, ease-in",
            animation_direction="normal, normal, alternate",
        ),
    )

    async def test_the_animation_after_a_none_slot_keeps_its_own_timing(self):
        payload = await extract(self.THREE_SLOTS)
        spin = _timing(payload, "spin")["timing"]
        assert spin["duration_raw"] == "3s", (
            "spin sits in slot 2, so its duration is the third item of "
            "'1s, 2s, 3s'; reading the second means the `none` slot was dropped "
            "without keeping its index"
        )
        assert spin["delay_raw"] == "5s"
        assert spin["iterations"] == "infinite"
        assert spin["easing"] == "ease-in"
        assert spin["direction"] == "alternate"

    async def test_the_animation_before_a_none_slot_is_untouched(self):
        payload = await extract(self.THREE_SLOTS)
        fade = _timing(payload, "fade")["timing"]
        assert fade["duration_raw"] == "1s"
        assert fade["delay_raw"] == "0s"

    async def test_the_switched_off_slot_produces_no_record(self):
        payload = await extract(self.THREE_SLOTS)
        assert [a["name"] for a in payload["animations"]] == ["fade", "spin"]

    async def test_the_ids_stay_dense_so_a_live_record_cannot_collide(self):
        """``build_waapi`` numbers its records from ``len(animations)``. If a
        skipped slot left a hole in the CSS ids, the first live record would be
        handed an id that already exists."""
        payload = await extract(self.THREE_SLOTS)
        ids = [a["id"] for a in payload["animations"]]
        assert ids == ["anim-0", "anim-1"]
        assert len(ids) == len(set(ids))

    async def test_the_edit_recipe_addresses_the_right_comma_item(self):
        """The recipe's ``list_index`` is the same index the timing used, so a
        wrong index here rewrites a DIFFERENT animation's duration in the
        author's file — at ``confidence: high``."""
        payload = facts(
            selector="#hero",
            raw_sources={
                "0": "#hero {\n"
                "  animation-name: fade, none, spin;\n"
                "  animation-duration: 1s, 2s, 3s;\n"
                "}\n"
            },
            computed=computed(
                animation_name="fade, none, spin",
                animation_duration="1s, 2s, 3s",
            ),
            matched_rules=[
                rule(
                    "#hero",
                    {
                        "animation-name": "fade, none, spin",
                        "animation-duration": "1s, 2s, 3s",
                    },
                )
            ],
            sources=[source("src-0", selector="#hero")],
        )
        found = await recipes(payload, name="spin")
        assert found["duration"]["token"] == "3s", (
            "spin's duration token is the third comma item; handing back '2s' "
            "would retime the wrong animation"
        )
        assert (
            found["duration"]["replace"] == "animation-duration: 1s, 2s, {{NEW_VALUE}}"
        )


class TestImportantSurvivesTheReplaceTemplate:
    """``!important`` is a property of the DECLARATION, not of any comma item.

    ``animation_edits``' whole promise is that ``replace`` carries the rest of
    the declaration so nothing can be lost. A token span that swallows the
    priority breaks exactly that promise, and it breaks it on the most
    authoritative rule in the file — ``winning_rule`` RANKS ``!important``
    first, so this is the rule the recipe is most likely to point at.
    """

    IMPORTANT = facts(
        selector="#hero",
        raw_sources={
            "0": "#hero {\n"
            "  animation-name: fade;\n"
            "  animation-duration: 2s !important;\n"
            "}\n"
        },
        computed=computed(animation_name="fade", animation_duration="2s"),
        matched_rules=[
            rule(
                "#hero",
                {"animation-name": "fade", "animation-duration": "2s"},
                important=["animation-duration"],
            )
        ],
        sources=[source("src-0", selector="#hero")],
    )

    async def test_the_token_is_the_value_not_the_priority(self):
        found = await recipes(self.IMPORTANT, name="fade")
        assert found["duration"]["token"] == "2s", (
            "the token a model swaps is the duration; including '!important' "
            "means substituting '3s' silently drops the priority"
        )

    async def test_the_replacement_still_carries_the_priority(self):
        found = await recipes(self.IMPORTANT, name="fade")
        replace = found["duration"]["replace"]
        assert "!important" in replace, (
            "applying this recipe must not change the cascade; a replacement "
            "that drops '!important' rewrites which rule wins"
        )
        # The mechanical check: apply the recipe the way EDIT_PROTOCOL says to.
        assert replace.replace("{{NEW_VALUE}}", "3s") == (
            "animation-duration: 3s !important"
        )

    async def test_the_find_literal_is_still_the_authors_whole_declaration(self):
        found = await recipes(self.IMPORTANT, name="fade")
        assert found["duration"]["find"] == "animation-duration: 2s !important"
        assert found["duration"]["find"] in self.IMPORTANT["raw_sources"]["0"]


class TestALayerMakesTheWinnerUndecidable:
    """``@layer`` reverses the rule ``winning_rule`` ranks by.

    An UNLAYERED normal declaration beats a layered one no matter how specific
    the layered selector is. ``winning_rule`` ranks ``!important``, then
    specificity, then document order, and never reads ``at_rule_context`` —
    which the recipe itself carries. So a layered ``#hero`` outranks an
    unlayered ``.hero`` in our ordering and loses in the browser's.

    The computed-value cross-check cannot save this one: when both rules declare
    the SAME value there is no disagreement to notice, and the recipe goes out
    at ``confidence: "high"`` pointing at a rule whose edit changes nothing.

    Layer ORDER is not recoverable from the captured facts either — a bare
    ``@layer a, b;`` statement rule has no ``cssRules`` and the collector's walk
    skips it — so the honest verdict is the one the module already has a
    vocabulary for: undecidable, degrade, say why.
    """

    LAYERED = facts(
        selector="#hero",
        raw_sources={
            "0": "@layer base {\n"
            "  #hero { animation-name: fade; animation-duration: 2s; }\n"
            "}\n"
            ".hero { animation-name: fade; animation-duration: 2s; }\n"
        },
        computed=computed(animation_name="fade", animation_duration="2s"),
        matched_rules=[
            rule(
                "#hero",
                {"animation-name": "fade", "animation-duration": "2s"},
                at_rule_context=["@layer base"],
            ),
            rule(
                ".hero",
                {"animation-name": "fade", "animation-duration": "2s"},
                source_ref="src-1",
            ),
        ],
        sources=[
            source("src-0", selector="#hero"),
            source("src-1", selector=".hero"),
        ],
    )

    async def test_no_find_is_offered_when_a_layer_decides_the_cascade(self):
        found = await recipes(self.LAYERED, name="fade")
        assert "find" not in found["duration"], (
            "an unlayered .hero beats a layered #hero, so a find inside the "
            "layered rule is an edit that cannot change what renders"
        )

    async def test_the_note_names_the_layer_as_the_reason(self):
        found = await recipes(self.LAYERED, name="fade")
        assert found["duration"]["confidence"] == "low"
        assert "layer" in found["duration"]["note"].lower()

    async def test_the_record_says_it_is_not_editable(self):
        payload = await extract(self.LAYERED)
        fade = _timing(payload, "fade")
        assert fade["editable"] is False
        assert fade["not_editable_reason"]

    async def test_one_layer_alone_still_yields_a_usable_recipe(self):
        """The degradation is about DISAGREEMENT between candidates. A single
        layered rule is as decidable as a single unlayered one, and turning
        every ``@layer`` user's payload into pointers would be a worse defect
        than the one being fixed."""
        payload = facts(
            selector="#hero",
            raw_sources={
                "0": "@layer base {\n"
                "  #hero { animation-name: fade; animation-duration: 2s; }\n"
                "}\n"
            },
            computed=computed(animation_name="fade", animation_duration="2s"),
            matched_rules=[
                rule(
                    "#hero",
                    {"animation-name": "fade", "animation-duration": "2s"},
                    at_rule_context=["@layer base"],
                )
            ],
            sources=[source("src-0", selector="#hero")],
        )
        found = await recipes(payload, name="fade")
        assert found["duration"]["token"] == "2s"
        assert found["duration"]["confidence"] == "high"

    async def test_two_rules_in_the_same_layer_are_still_decidable(self):
        payload = facts(
            selector="#hero",
            raw_sources={
                "0": "@layer base {\n"
                "  .hero { animation-name: fade; animation-duration: 9s; }\n"
                "  #hero { animation-name: fade; animation-duration: 2s; }\n"
                "}\n"
            },
            computed=computed(animation_name="fade", animation_duration="2s"),
            matched_rules=[
                rule(
                    ".hero",
                    {"animation-name": "fade", "animation-duration": "9s"},
                    at_rule_context=["@layer base"],
                ),
                rule(
                    "#hero",
                    {"animation-name": "fade", "animation-duration": "2s"},
                    source_ref="src-1",
                    at_rule_context=["@layer base"],
                ),
            ],
            sources=[
                source("src-0", selector=".hero"),
                source("src-1", selector="#hero"),
            ],
        )
        found = await recipes(payload, name="fade")
        assert found["duration"]["rule_selector"] == "#hero"
        assert found["duration"]["token"] == "2s"


class TestTruncatedEditsAnnounceThemselves:
    """``EDIT_CAP`` cuts the recipe list at 20 and used to say nothing.

    The caps discipline this batch landed (F-853/F-855) is that a bound the
    reader cannot see is a bound that lies: the payload's ``caps.truncated``
    block reports only animations and keyframes, so a model handed 20 of 38
    recipes has no way to learn the other 18 exist. It will conclude the
    keyframes it cannot find a recipe for are not editable.
    """

    @staticmethod
    def _many_keyframes():
        frames = [
            {
                "key_text": f"{i * 10}%",
                "css_text": "opacity: 0.5; transform: scale(1); color: red;",
                "easing": "",
                "composite": "",
            }
            for i in range(11)
        ]
        return facts(
            selector="#hero",
            raw_sources={"0": "#hero { animation: fade 2s ease; }\n"},
            computed=computed(animation_name="fade", animation_duration="2s"),
            matched_rules=[
                rule(
                    "#hero",
                    {"animation": "fade 2s ease", "animation-duration": "2s"},
                )
            ],
            keyframe_rules=[
                {"name": "fade", "source_ref": "src-1", "keyframes": frames}
            ],
            sources=[
                source("src-0", selector="#hero"),
                source("src-1", name="fade", kind="keyframes"),
            ],
        )

    async def test_the_cut_really_happens(self):
        payload = await extract(self._many_keyframes())
        fade = _timing(payload, "fade")
        assert len(fade["edits"]) == EDIT_CAP
        # 11 keyframes x 3 declarations + 5 timing knobs is well past the cap.
        assert len(fade["keyframes"]) * 3 + 5 > EDIT_CAP

    async def test_the_record_warns_that_recipes_were_dropped(self):
        payload = await extract(self._many_keyframes())
        fade = _timing(payload, "fade")
        codes = [w["code"] for w in fade["warnings"]]
        assert "edit_cap_reached" in codes, (
            f"edits were cut from {len(fade['keyframes']) * 3 + 5} to "
            f"{len(fade['edits'])} with no warning; silent truncation is the "
            f"exact failure F-853/F-855 named"
        )

    async def test_the_warning_names_a_remedy_that_works(self):
        """F-855's rule: a truncation message must not name a lever the reader
        does not have. ``EDIT_CAP`` is not caller-settable at all, so the
        message must not invite anyone to raise it."""
        payload = await extract(self._many_keyframes())
        fade = _timing(payload, "fade")
        message = next(
            w["message"] for w in fade["warnings"] if w["code"] == "edit_cap_reached"
        )
        assert "source_ref" in message or "narrower selector" in message
        assert "max_edits" not in message

    async def test_an_uncut_record_carries_no_such_warning(self):
        payload = facts(
            selector="#hero",
            raw_sources={"0": "#hero { animation: fade 2s ease; }\n"},
            computed=computed(animation_name="fade", animation_duration="2s"),
            matched_rules=[
                rule("#hero", {"animation": "fade 2s ease", "animation-duration": "2s"})
            ],
        )
        result = await extract(payload)
        fade = _timing(result, "fade")
        assert "edit_cap_reached" not in [w["code"] for w in fade["warnings"]]


class TestValuesBehindACustomPropertyDegrade:
    """``animation: fade var(--dur) ease`` has no ``<time>`` token to address.

    Chrome resolves the duration to ``0.4s`` in computed style while the
    author's bytes say ``var(--dur)``. A recipe that pointed at the resolved
    value would be addressing text that is not in the file; one that swapped
    ``var(--dur)`` would delete the indirection the author wanted.

    Degrading is the right answer, and this pins it so a later pass cannot
    start substituting the resolved value.
    """

    VAR_SHORTHAND = facts(
        selector="#hero",
        raw_sources={"0": "#hero { animation: fade var(--dur) ease; }\n"},
        computed=computed(animation_name="fade", animation_duration="0.4s"),
        matched_rules=[rule("#hero", {"animation": "fade var(--dur) ease"})],
        sources=[source("src-0", selector="#hero")],
    )

    async def test_no_find_literal_is_offered_for_the_duration(self):
        found = await recipes(self.VAR_SHORTHAND, name="fade")
        assert "find" not in found["duration"]
        assert found["duration"]["confidence"] == "low"

    async def test_the_resolved_value_is_never_presented_as_the_authors_text(self):
        """``0.4s`` appears nowhere in the file, so it must appear in no
        ``find``, ``token`` or ``replace`` on any recipe for this record."""
        payload = await extract(self.VAR_SHORTHAND)
        fade = _timing(payload, "fade")
        for edit in fade["edits"]:
            for field in ("find", "token", "replace"):
                text = edit.get(field, "")
                assert "0.4s" not in text, (
                    f"{edit['knob']}.{field} offers {text!r}, which is Chrome's "
                    f"resolved value and is not in the author's stylesheet"
                )

    async def test_the_declared_duration_is_still_reported(self):
        """Degrading the RECIPE must not degrade the FACT: the model still
        needs to know the animation runs for 400ms."""
        payload = await extract(self.VAR_SHORTHAND)
        assert _timing(payload, "fade")["timing"]["duration_ms"] == 400.0

    async def test_the_note_names_the_custom_property_it_defers_to(self):
        """D6. RED before this PR: the note read "something not captured here
        (an inline style, a UA sheet, an unreadable stylesheet) is in charge;
        edit that instead" — three wrong places, while the rule that IS in
        charge is right there in ``matched_rules`` and names ``--dur`` itself.

        A weak model reading that goes looking for a stylesheet it will not
        find. The custom property is the ONE thing to change, and the payload
        already carried it.
        """
        found = await recipes(self.VAR_SHORTHAND, name="fade")
        note = found["duration"]["note"]
        assert "--dur" in note, note
        assert "cross-origin" not in note, note
        assert "inline style" not in note, note

    async def test_the_note_never_invites_an_edit_to_the_resolved_value(self):
        found = await recipes(self.VAR_SHORTHAND, name="fade")
        assert "0.4s" not in found["duration"]["note"]


class TestDuplicateKeyframesResolveToTheLastBlock:
    """Two ``@keyframes fade`` blocks is ordinary CSS and the LAST one wins.

    Resolving to the first would describe motion the page does not perform, and
    the recipe must not point into either block: with two identical headers in
    the file there is no way to tell which one the CSSOM record came from.
    """

    @staticmethod
    def _block(start, end, source_ref):
        return {
            "name": "fade",
            "source_ref": source_ref,
            "keyframes": [
                {
                    "key_text": "from",
                    "css_text": f"opacity: {start};",
                    "easing": "",
                    "composite": "",
                },
                {
                    "key_text": "to",
                    "css_text": f"opacity: {end};",
                    "easing": "",
                    "composite": "",
                },
            ],
        }

    DUPLICATE = facts(
        selector="#hero",
        raw_sources={
            "0": "#hero { animation: fade 2s ease; }\n"
            "@keyframes fade { from { opacity: 0 } to { opacity: 1 } }\n"
            "@keyframes fade { from { opacity: 1 } to { opacity: .25 } }\n"
        },
        computed=computed(animation_name="fade", animation_duration="2s"),
        matched_rules=[
            rule("#hero", {"animation": "fade 2s ease", "animation-duration": "2s"})
        ],
        sources=[
            source("src-0", selector="#hero"),
            source("src-1", name="fade", kind="keyframes"),
            source("src-2", name="fade", kind="keyframes"),
        ],
    )

    def _facts(self):
        payload = dict(self.DUPLICATE)
        payload["keyframe_rules"] = [
            self._block("0", "1", "src-1"),
            self._block("1", "0.25", "src-2"),
        ]
        return payload

    async def test_the_last_block_is_the_one_reported(self):
        payload = await extract(self._facts())
        fade = _timing(payload, "fade")
        values = [k["properties"]["opacity"] for k in fade["keyframes"]]
        assert values == ["1", "0.25"], (
            "the cascade gives the last @keyframes block of a name; reporting "
            "0 -> 1 describes motion this page does not perform"
        )

    async def test_no_keyframe_recipe_guesses_which_block_to_edit(self):
        payload = await extract(self._facts())
        fade = _timing(payload, "fade")
        for edit in fade["edits"]:
            if edit["knob"].startswith("keyframe["):
                assert "find" not in edit, (
                    "two identical @keyframes headers are in this file; "
                    "picking one is a coin flip dressed as a verified address"
                )


class TestAnInlineAnimationIsNotBlamedOnACrossOriginSheet:
    """An ``animation`` in the element's ``style=""`` has no rule to edit.

    Degrading is right. But the collector already captured
    ``element.inline_properties``, so the payload KNOWS where the declaration
    is, and telling the reader its stylesheet is "likely cross-origin" sends a
    weak model looking for a file that is not involved.

    The safe half (no ``find``) was pinned when this file was written; the
    misdirection was recorded as D6 and is fixed here — the reason now names the
    ``style=""`` attribute the collector actually saw.
    """

    INLINE = facts(
        selector="#hero",
        element={
            "tag": "div",
            "id": "hero",
            "classes": [],
            "inline_properties": ["animation-name", "animation-duration"],
            "is_canvas": False,
        },
        computed=computed(animation_name="fade", animation_duration="2s"),
    )

    async def test_no_recipe_points_at_a_stylesheet_that_declares_nothing(self):
        payload = await extract(self.INLINE)
        fade = _timing(payload, "fade")
        assert fade["editable"] is False
        assert all("find" not in edit for edit in fade["edits"])

    async def test_the_timing_is_still_read_from_computed_style(self):
        payload = await extract(self.INLINE)
        assert _timing(payload, "fade")["timing"]["duration_ms"] == 2000.0

    async def test_the_reason_names_the_style_attribute_the_collector_saw(self):
        """D6. RED before this PR: ``not_editable_reason`` said "its stylesheet
        is likely cross-origin", which is a guess, and a wrong one — no
        stylesheet is involved at all and ``element.inline_properties`` says
        so."""
        payload = await extract(self.INLINE)
        reason = _timing(payload, "fade")["not_editable_reason"]
        assert 'style=""' in reason, reason
        assert "cross-origin" not in reason, reason

    async def test_the_reason_names_the_properties_that_are_actually_inline(self):
        payload = await extract(self.INLINE)
        reason = _timing(payload, "fade")["not_editable_reason"]
        assert "animation-name" in reason and "animation-duration" in reason


class TestAnUnreachableRuleIsNotCalledCrossOriginWithoutEvidence:
    """ "Likely cross-origin" was asserted for every unreadable declaration.

    The third of D6's three causes: an adopted constructed sheet
    (``new CSSStyleSheet()`` + ``replaceSync``) is not in ``document.styleSheets``
    at all, so the collector enumerates every sheet, reads every one of them
    successfully, and finds no rule. "Its stylesheet is likely cross-origin" is
    then a claim contradicted by the payload's own warnings — there is no
    ``cross_origin_stylesheet`` warning, because nothing was blocked.

    The discriminating fact is already there: whether a CORS failure was
    actually WITNESSED. When it was, the message may say so, and may name the
    href it was witnessed on; when it was not, it may not.
    """

    UNREACHABLE = facts(
        selector="#hero",
        computed=computed(animation_name="adopted", animation_duration="2.5s"),
    )

    BLOCKED = facts(
        selector="#hero",
        computed=computed(animation_name="adopted", animation_duration="2.5s"),
        warnings=[
            {
                "code": "cross_origin_stylesheet",
                "message": "Stylesheet rules unreadable (CORS)",
                "detail": {"index": 0, "href": "https://cdn.test/site.css"},
            }
        ],
    )

    async def test_no_cors_failure_was_seen_so_none_is_alleged(self):
        payload = await extract(self.UNREACHABLE)
        reason = _timing(payload, "adopted")["not_editable_reason"]
        assert "cross-origin" not in reason, reason

    async def test_the_reason_names_the_causes_that_are_actually_left(self):
        """Every enumerable sheet WAS read, so the declaration is not in a
        document stylesheet: a constructed/adopted sheet, a shadow-root
        ``<style>``, or JS. Naming those is the difference between a pointer
        and a shrug."""
        payload = await extract(self.UNREACHABLE)
        reason = _timing(payload, "adopted")["not_editable_reason"]
        assert "constructed" in reason.lower(), reason

    async def test_a_witnessed_cors_failure_is_named_with_its_href(self):
        payload = await extract(self.BLOCKED)
        reason = _timing(payload, "adopted")["not_editable_reason"]
        assert "cross-origin" in reason, reason
        assert "https://cdn.test/site.css" in reason, reason


class TestStepsEasingAndNegativeDelayStayMechanical:
    """Two shapes a weak model gets wrong on its own, both fully decidable."""

    STEPPED = facts(
        selector="#hero",
        raw_sources={
            "0": "#hero { animation: tick 2s steps(4, jump-end) -0.5s infinite; }\n"
        },
        computed=computed(
            animation_name="tick",
            animation_duration="2s",
            animation_delay="-0.5s",
            animation_timing_function="steps(4, jump-end)",
            animation_iteration_count="infinite",
        ),
        matched_rules=[
            rule(
                "#hero",
                {"animation": "tick 2s steps(4, jump-end) -0.5s infinite"},
            )
        ],
        sources=[source("src-0", selector="#hero")],
    )

    async def test_a_negative_delay_starts_now_partway_in(self):
        payload = await extract(self.STEPPED)
        derived = _timing(payload, "tick")["derived"]
        assert derived["active_start_ms"] == 0.0
        assert derived["starts_at_progress_ms"] == 500.0

    async def test_the_steps_easing_classifies_as_stepped(self):
        payload = await extract(self.STEPPED)
        semantics = _timing(payload, "tick")["semantics"]
        assert semantics["easing_class"]["value"] == "stepped"

    async def test_the_easing_recipe_addresses_the_whole_steps_function(self):
        """``steps(4, jump-end)`` has a comma inside it. A token split that did
        not keep the function whole would hand back ``steps(4,`` — a find that
        matches and a replace that produces invalid CSS."""
        found = await recipes(self.STEPPED, name="tick")
        assert found["easing"]["token"] == "steps(4, jump-end)"
        assert found["easing"]["replace"] == (
            "animation: tick 2s {{NEW_VALUE}} -0.5s infinite"
        )

    async def test_the_delay_recipe_takes_the_second_time_not_the_first(self):
        found = await recipes(self.STEPPED, name="tick")
        assert found["delay"]["token"] == "-0.5s"
        assert found["duration"]["token"] == "2s"


class TestARecipeSaysWhereToOpenTheFileNotJustWhatToFind:
    """The audit's closing recommendation: the addressing stopped one step short.

    ``file`` was ``"<style> #0"`` or an href — neither of which an editor can
    open at the right place — while ``rule_span`` had already computed the byte
    offset of the rule inside the sheet and thrown it away. Joining the two is
    what makes a recipe mechanically applicable rather than a search hint.

    Two things are pinned here, and they are pinned separately on purpose:

    * the OFFSET is verified by slicing the sheet's own bytes at it, never by
      recomputing the number the way the product does;
    * the offset's FRAME OF REFERENCE is stated once, on the source, because
      offsets into a ``<style>`` element's text are not offsets into the HTML
      document that contains it, and a reader who assumes otherwise lands in the
      wrong place with no error.
    """

    SHEET = "#hero {\n  animation-name: fade;\n  animation-duration: 2s;\n}\n"

    LOCATED = facts(
        selector="#hero",
        raw_sources={"0": SHEET},
        computed=computed(animation_name="fade", animation_duration="2s"),
        matched_rules=[
            rule("#hero", {"animation-name": "fade", "animation-duration": "2s"})
        ],
        sources=[source("src-0", selector="#hero")],
    )

    LINKED = facts(
        selector="#hero",
        computed=computed(animation_name="fade", animation_duration="2s"),
        matched_rules=[
            rule("#hero", {"animation-name": "fade", "animation-duration": "2s"})
        ],
        sources=[
            {
                **source("src-0", selector="#hero"),
                "stylesheet": {
                    "index": 0,
                    "href": "https://cdn.test/site.css",
                    "kind": "link",
                },
                "source_text_available": False,
            }
        ],
    )

    async def test_the_offset_lands_on_the_find_literal_in_the_sheets_own_bytes(self):
        edit = (await recipes(self.LOCATED, name="fade"))["duration"]
        start = edit["char_offset"]
        assert self.SHEET[start : start + len(edit["find"])] == edit["find"], (
            f"char_offset {start} points at "
            f"{self.SHEET[start : start + len(edit['find'])]!r}, not at the find "
            f"literal — an offset that is off by anything is worse than none"
        )

    async def test_the_line_and_column_are_one_based_like_an_editor(self):
        edit = (await recipes(self.LOCATED, name="fade"))["duration"]
        assert (edit["line"], edit["column"]) == (3, 3)

    async def test_the_source_says_which_document_the_offsets_are_measured_in(self):
        payload = await extract(self.LOCATED)
        opened = payload["sources"][0]["open"]
        assert opened["url"] == payload["url"], (
            "a <style> block is reached by opening the DOCUMENT; the sheet has "
            "no url of its own"
        )
        assert opened["offsets_in"] == "style_element_text"

    async def test_the_frame_of_reference_is_explained_once_in_the_protocol(self):
        """The R12 rule: an instruction repeated per recipe is a payload tax.
        ``edit_protocol`` is where the one copy lives."""
        payload = await extract(self.LOCATED)
        protocol = payload["edit_protocol"]["open"]
        assert "char_offset" in protocol and "style_element_text" in protocol
        for edit in _timing(payload, "fade")["edits"]:
            assert "style_element_text" not in str(edit)

    async def test_a_linked_sheet_names_its_own_url_and_promises_no_offsets(self):
        """Author text for a linked sheet is not readable (owner ruling Q5: name
        the href and stop), so there is no offset to give — and claiming a frame
        of reference for offsets that do not exist would be the same defect in
        the other direction."""
        payload = await extract(self.LINKED)
        opened = payload["sources"][0]["open"]
        assert opened["url"] == "https://cdn.test/site.css"
        assert "offsets_in" not in opened
        for edit in _timing(payload, "fade")["edits"]:
            assert "char_offset" not in edit and "line" not in edit


class TestEditableSaysWhichPARTSAreEditable:
    """D7: ``editable`` was a whole-record verdict, and its absence read as yes.

    A record whose five timing knobs are all pointers but whose keyframe
    declarations are applicable emitted NO ``editable`` field at all — so a
    reader that stops at the flag concludes it can retime the animation, which
    is exactly the false all-clear the flag exists to prevent.

    The verdict is now granular: ``editable`` says whether ANY recipe here can
    be applied, and ``not_editable`` names the ones that cannot, so the two
    together cannot be read as a promise about a knob that is a pointer.
    """

    MIXED = facts(
        selector="#hero",
        raw_sources={
            "0": "@layer base {\n"
            "  #hero { animation-name: fade; animation-duration: 2s; }\n"
            "}\n"
            ".hero { animation-name: fade; animation-duration: 2s; }\n"
            "@keyframes fade { from { opacity: 0 } to { opacity: 1 } }\n"
        },
        computed=computed(animation_name="fade", animation_duration="2s"),
        matched_rules=[
            rule(
                "#hero",
                {"animation-name": "fade", "animation-duration": "2s"},
                at_rule_context=["@layer base"],
            ),
            rule(
                ".hero",
                {"animation-name": "fade", "animation-duration": "2s"},
                source_ref="src-1",
            ),
        ],
        keyframe_rules=[
            {
                "name": "fade",
                "source_ref": "src-2",
                "keyframes": [
                    {
                        "key_text": "from",
                        "css_text": "opacity: 0;",
                        "easing": "",
                        "composite": "",
                    },
                    {
                        "key_text": "to",
                        "css_text": "opacity: 1;",
                        "easing": "",
                        "composite": "",
                    },
                ],
            }
        ],
        sources=[
            source("src-0", selector="#hero"),
            source("src-1", selector=".hero"),
            source("src-2", name="fade", kind="keyframes"),
        ],
    )

    # Every knob spelled out in the shorthand. A bare ``animation: fade 2s ease``
    # is NOT fully applicable and never was: the delay and iteration count come
    # from the initial values, so no rule declares them and both degrade to
    # pointers — which is itself half of why D7 matters.
    PLAIN = facts(
        selector="#hero",
        raw_sources={
            "0": "#hero { animation: fade 2s ease 0s 1; }\n"
            "@keyframes fade { from { opacity: 0 } to { opacity: 1 } }\n"
        },
        computed=computed(animation_name="fade", animation_duration="2s"),
        matched_rules=[
            rule(
                "#hero",
                {"animation": "fade 2s ease 0s 1", "animation-duration": "2s"},
            )
        ],
        keyframe_rules=[
            {
                "name": "fade",
                "source_ref": "src-1",
                "keyframes": [
                    {
                        "key_text": "from",
                        "css_text": "opacity: 0;",
                        "easing": "",
                        "composite": "",
                    },
                    {
                        "key_text": "to",
                        "css_text": "opacity: 1;",
                        "easing": "",
                        "composite": "",
                    },
                ],
            }
        ],
        sources=[
            source("src-0", selector="#hero"),
            source("src-1", name="fade", kind="keyframes"),
        ],
    )

    async def test_the_mixed_record_really_is_mixed(self):
        """Asserted first: if the keyframe recipes were pointers too this class
        would pass for the wrong reason."""
        payload = await extract(self.MIXED)
        found = {e["knob"]: e for e in _timing(payload, "fade")["edits"]}
        assert all(
            "find" not in found[knob]
            for knob in ("duration", "delay", "easing", "iterations", "name")
        )
        assert any(
            "find" in edit
            for knob, edit in found.items()
            if knob.startswith("keyframe[")
        )

    async def test_a_record_whose_timing_is_all_pointers_says_so(self):
        """RED before this PR: no ``editable`` key at all on this record."""
        payload = await extract(self.MIXED)
        fade = _timing(payload, "fade")
        assert fade["editable"] is True
        assert set(fade["not_editable"]) == {
            "duration",
            "delay",
            "easing",
            "iterations",
            "name",
        }
        assert fade["not_editable_reason"]

    async def test_a_fully_applicable_record_carries_no_pointer_list(self):
        """The list exists to break a false all-clear. Where every recipe
        applies there is no all-clear to break, and an empty list would be
        noise on every healthy record."""
        payload = await extract(self.PLAIN)
        fade = _timing(payload, "fade")
        assert fade["editable"] is True
        assert "not_editable" not in fade
        assert "not_editable_reason" not in fade

    async def test_a_record_with_nothing_applicable_still_lists_its_parts(self):
        payload = await extract(
            facts(
                selector="#hero",
                computed=computed(animation_name="fade", animation_duration="2s"),
            )
        )
        fade = _timing(payload, "fade")
        assert fade["editable"] is False
        assert "duration" in fade["not_editable"]
        assert fade["not_editable_reason"]
