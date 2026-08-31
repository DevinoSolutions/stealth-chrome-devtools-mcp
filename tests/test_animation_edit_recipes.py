"""Edit recipes must name the TOKEN and the WINNING rule (F-852: R1, R2).

Two defects that compound, and both are worse than a missing recipe:

* **R1** — every timing knob got the same ``find``: the whole declaration, each
  at ``confidence: "high"``, differing only in ``current``. A model told "find
  this, replace it with the new duration" turns
  ``.card { animation: fade 2s ease; }`` into ``.card { 3s; }``. That is a
  file-corrupting instruction delivered at the highest confidence we have.
* **R2** — the recipe pointed at the LAST document-order rule declaring anything
  ``animation*``, ignoring specificity and ``!important``, while ``current`` came
  from computed style. So it could report ``current: "5s"`` with a ``find``
  inside a rule whose edit cannot change the rendered 5s.

Every fixture here is written the way an author writes CSS, not the way Chrome
re-serializes it: name first in the shorthand, bare decimals, tight
``cubic-bezier`` spacing. A fixture built from CSSOM output cannot fail these.
"""

import re

import pytest

from test_animation_schema_v2 import computed, extract, facts

PLACEHOLDER = "{{NEW_VALUE}}"

# One rule, one shorthand, five knobs inside it. This is the shape the live
# probe hit: all five recipes carried this entire string as `find`.
TORTURE_SOURCE = """
#torture {
  color: red;
  animation: torture-pulse 2.4s cubic-bezier(.68,-0.55,.27,1.55) 0.3s infinite alternate both;
}
@keyframes torture-pulse {
  0%,50% { transform: scale(1) rotate(0deg); opacity: .8 }
  100%   { transform: scale(.7) rotate(-10deg); opacity: 1 }
}
"""


def rule(selector, declares, source_ref="src-0", **over):
    record = {
        "source_ref": source_ref,
        "selector_text": selector,
        "css_text": f"{selector} {{ ... }}",
        "declares": declares,
        "important": [],
        "order": {name: index for index, name in enumerate(declares)},
        "matches_now": True,
        "matches_base": True,
        "at_rule_context": [],
    }
    record.update(over)
    return record


def source(source_id, selector=None, name=None, kind="rule", index=0):
    return {
        "id": source_id,
        "kind": kind,
        "stylesheet": {"index": index, "href": None, "kind": "style"},
        "source_text_available": True,
        "rule_path": [0],
        "at_rule_context": [],
        "name": name,
        "selector_text": selector,
        "computed_css_text": "",
    }


TORTURE = facts(
    selector="#torture",
    raw_sources={"0": TORTURE_SOURCE},
    computed=computed(
        animation_name="torture-pulse",
        animation_duration="2.4s",
        animation_delay="0.3s",
        # Chrome's form of the author's tight cubic-bezier.
        animation_timing_function="cubic-bezier(0.68, -0.55, 0.27, 1.55)",
        animation_iteration_count="infinite",
        animation_direction="alternate",
        animation_fill_mode="both",
    ),
    keyframe_rules=[
        {
            "name": "torture-pulse",
            "source_ref": "src-1",
            "keyframes": [
                {
                    "key_text": "0%, 50%",
                    "css_text": "transform: scale(1) rotate(0deg); opacity: 0.8;",
                    "easing": "",
                    "composite": "",
                },
                {
                    "key_text": "100%",
                    "css_text": "transform: scale(0.7) rotate(-10deg); opacity: 1;",
                    "easing": "",
                    "composite": "",
                },
            ],
        }
    ],
    matched_rules=[
        # What the CSSOM really reports for a rule written as a shorthand: the
        # shorthand AND every longhand it set. The longhands are authoritative
        # for the values, but only the shorthand exists in the file, so a recipe
        # has to read one and address the other.
        rule(
            "#torture",
            {
                "animation": (
                    "2.4s cubic-bezier(0.68, -0.55, 0.27, 1.55) 0.3s infinite "
                    "alternate both running torture-pulse"
                ),
                "animation-name": "torture-pulse",
                "animation-duration": "2.4s",
                "animation-delay": "0.3s",
                "animation-timing-function": "cubic-bezier(0.68, -0.55, 0.27, 1.55)",
                "animation-iteration-count": "infinite",
            },
        )
    ],
    sources=[
        source("src-0", selector="#torture"),
        source("src-1", name="torture-pulse", kind="keyframes"),
    ],
)


async def recipes(payload_facts, name="torture-pulse"):
    payload = await extract(payload_facts)
    animation = next(a for a in payload["animations"] if a["name"] == name)
    return {edit["knob"]: edit for edit in animation["edits"]}


def apply(recipe, new_value, text):
    """Apply a recipe the way a weak model is told to: find the literal, put the
    replacement in its place with the placeholder filled in."""
    assert recipe["find"] in text, (
        f"find literal is not in the source: {recipe['find']}"
    )
    return text.replace(
        recipe["find"], recipe["replace"].replace(PLACEHOLDER, new_value)
    )


# ===========================================================================
# R1 — which TOKEN, not just which declaration
# ===========================================================================


class TestARecipeNamesTheTokenToChange:
    async def test_the_five_timing_knobs_do_not_share_one_replacement(self):
        """The defect in one line: duration/delay/easing/iterations/name each
        carried the identical whole-declaration literal, so applying any of them
        as instructed replaces the whole declaration with a single value."""
        found = await recipes(TORTURE)
        knobs = ["duration", "delay", "easing", "iterations", "name"]
        replacements = {found[knob]["replace"] for knob in knobs}
        assert len(replacements) == len(knobs), (
            "two knobs produce the same edit, so at least one of them is wrong"
        )

    @pytest.mark.parametrize(
        "knob,token",
        [
            ("duration", "2.4s"),
            # The SECOND <time> in the shorthand is the delay. Reading it as the
            # first is the single most likely way to break a working animation.
            ("delay", "0.3s"),
            ("easing", "cubic-bezier(.68,-0.55,.27,1.55)"),
            ("iterations", "infinite"),
            ("name", "torture-pulse"),
        ],
    )
    async def test_each_knob_points_at_its_own_token(self, knob, token):
        found = await recipes(TORTURE)
        assert found[knob]["token"] == token

    async def test_the_tokens_are_the_authors_spelling_not_chromes(self):
        """The author wrote ``cubic-bezier(.68,-0.55,.27,1.55)``; Chrome reports
        ``cubic-bezier(0.68, -0.55, 0.27, 1.55)``. A token that cannot be found
        in the file is no better than a whole declaration that cannot."""
        found = await recipes(TORTURE)
        for edit in found.values():
            if "token" in edit:
                assert edit["token"] in TORTURE_SOURCE, edit["token"]

    async def test_applying_a_recipe_keeps_the_rest_of_the_declaration(self):
        """The property the whole redesign exists to guarantee: it must be
        impossible to apply a recipe in a way that drops the other components."""
        found = await recipes(TORTURE)
        updated = apply(found["duration"], "3s", TORTURE_SOURCE)
        assert "animation: torture-pulse 3s" in updated
        for survivor in (
            "cubic-bezier(.68,-0.55,.27,1.55)",
            "0.3s",
            "infinite",
            "alternate",
            "both",
        ):
            assert survivor in updated, f"applying the duration edit lost {survivor}"

    async def test_applying_the_delay_recipe_does_not_touch_the_duration(self):
        found = await recipes(TORTURE)
        updated = apply(found["delay"], "1s", TORTURE_SOURCE)
        assert "torture-pulse 2.4s" in updated
        assert "1.55) 1s infinite" in updated

    async def test_the_placeholder_is_visible_in_place_and_named_once(self):
        """A model must not have to infer the marker it is meant to substitute —
        but it does not need to be told forty times either (R12). The marker
        appears inside every `replace`, so it is discoverable where it is used;
        the instruction for using it is stated once in `edit_protocol`."""
        payload = await extract(TORTURE)
        assert payload["edit_protocol"]["placeholder"] == PLACEHOLDER
        found = {e["knob"]: e for e in payload["animations"][0]["edits"]}
        for edit in found.values():
            if "replace" in edit:
                assert edit["replace"].count(PLACEHOLDER) == 1

    async def test_a_keyframe_declaration_is_addressed_whole(self):
        """A keyframe declaration's value IS the thing being changed, so there
        is no sub-token to isolate — but it still must be the right block."""
        found = await recipes(TORTURE)
        opacity = found["keyframe[1.0].opacity"]
        assert opacity["find"] == "opacity: 1"
        assert opacity["token"] == "1"


# ===========================================================================
# R2 — which RULE actually wins the cascade
# ===========================================================================

CASCADE_SOURCE = """
.card { animation: fade 2s ease; }
#hero { animation-duration: 5s; }
"""

CASCADE = facts(
    selector="#hero",
    raw_sources={"0": CASCADE_SOURCE},
    computed=computed(
        animation_name="fade",
        # The ID rule wins the duration; the class rule supplies the name.
        animation_duration="5s",
        animation_timing_function="ease",
    ),
    matched_rules=[
        rule(".card", {"animation": "2s ease 0s 1 normal none running fade"}),
        rule("#hero", {"animation-duration": "5s"}, source_ref="src-1"),
    ],
    sources=[source("src-0", selector=".card"), source("src-1", selector="#hero")],
)


class TestTheRecipePointsAtTheRuleThatWins:
    async def test_a_higher_specificity_longhand_beats_the_shorthand(self):
        """R2: the old selection took the LAST matched rule declaring anything
        ``animation*`` and used it for every knob, so this emitted
        ``current: "5s"`` with a ``find`` inside ``.card`` — an edit that cannot
        change the rendered 5s."""
        found = await recipes(CASCADE, name="fade")
        assert found["duration"]["source_ref"] == "src-1"
        assert found["duration"]["rule_selector"] == "#hero"
        assert found["duration"]["current"] == "5s"
        assert "5s" in found["duration"]["find"]

    async def test_the_knob_only_the_shorthand_declares_still_points_at_it(self):
        found = await recipes(CASCADE, name="fade")
        assert found["name"]["rule_selector"] == ".card"
        assert found["easing"]["rule_selector"] == ".card"

    async def test_important_beats_specificity(self):
        payload = facts(
            selector="#hero",
            raw_sources={
                "0": ".card { animation-duration: 2s !important; }\n"
                "#hero { animation-duration: 5s; }\n"
            },
            computed=computed(animation_name="fade", animation_duration="2s"),
            matched_rules=[
                rule(
                    ".card",
                    {"animation-duration": "2s"},
                    important=["animation-duration"],
                ),
                rule("#hero", {"animation-duration": "5s"}, source_ref="src-1"),
            ],
            sources=[
                source("src-0", selector=".card"),
                source("src-1", selector="#hero"),
            ],
        )
        found = await recipes(payload, name="fade")
        assert found["duration"]["rule_selector"] == ".card"

    async def test_the_recipe_carries_the_rule_that_scopes_its_uniqueness_claim(self):
        """``find_unique_in_rule`` is a rule-scoped claim, but the only locator
        offered was file-wide. The rule has to travel with it."""
        found = await recipes(TORTURE)
        assert found["duration"]["rule_selector"] == "#torture"
        assert found["duration"]["find_unique_in_rule"] is True

    async def test_an_at_rule_context_travels_with_the_recipe(self):
        payload = facts(
            selector="#hero",
            raw_sources={
                "0": "@media (min-width: 600px) { #hero { animation: fade 2s ease; } }\n"
            },
            computed=computed(animation_name="fade", animation_duration="2s"),
            matched_rules=[
                rule(
                    "#hero",
                    {"animation": "2s ease 0s 1 normal none running fade"},
                    at_rule_context=["@media (min-width: 600px)"],
                )
            ],
            sources=[source("src-0", selector="#hero")],
        )
        found = await recipes(payload, name="fade")
        assert found["duration"]["at_rule_context"] == ["@media (min-width: 600px)"]

    async def test_an_undecidable_selector_degrades_instead_of_guessing(self):
        """``:is()`` takes the specificity of its most specific argument, which
        we did not compute. With two candidates and no way to order them, the
        recipe must not claim to know which one renders."""
        payload = facts(
            selector="#hero",
            raw_sources={
                "0": ":is(.card, #shell) { animation-duration: 2s; }\n"
                ".card { animation-duration: 5s; }\n"
            },
            computed=computed(animation_name="fade", animation_duration="5s"),
            matched_rules=[
                rule(":is(.card, #shell)", {"animation-duration": "2s"}),
                rule(".card", {"animation-duration": "5s"}, source_ref="src-1"),
            ],
            sources=[
                source("src-0", selector=":is(.card, #shell)"),
                source("src-1", selector=".card"),
            ],
        )
        found = await recipes(payload, name="fade")
        assert found["duration"]["confidence"] == "low"
        assert "find" not in found["duration"]
        assert found["duration"]["note"]

    async def test_a_nested_rule_does_not_hand_the_win_to_the_wrong_selector(self):
        """`&` resolves to the parent rule's selector, so its specificity cannot
        be computed from this selector's text at all. Counting it best-effort
        made `& .card` tie with `.card` at (0,1,0), and document order then gave
        the win to `.card` — which does NOT render, because the nested rule
        carries the parent's id.

        Both rules declare the SAME duration on purpose, and the two selectors
        are deliberately DISTINCT text. That combination is what makes this the
        dangerous shape: the computed-value cross-check passes (both say 2s) and
        the duplicate-header guard never fires (the headers differ), so nothing
        else catches it and the recipe confidently addresses a declaration whose
        edit changes nothing on the page. R2 through a side door.
        """
        payload = facts(
            selector="#shell .card",
            raw_sources={
                "0": "#shell { & .card { animation-duration: 2s; } }\n"
                ".promo { animation-duration: 2s; }\n"
            },
            computed=computed(animation_name="fade", animation_duration="2s"),
            matched_rules=[
                rule("& .card", {"animation-duration": "2s"}),
                rule(".promo", {"animation-duration": "2s"}, source_ref="src-1"),
            ],
            sources=[
                source("src-0", selector="& .card"),
                source("src-1", selector=".promo"),
            ],
        )
        found = await recipes(payload, name="fade")
        assert found["duration"]["confidence"] == "low"
        assert "find" not in found["duration"]
        assert found["duration"]["note"]

    async def test_a_nth_child_of_selector_degrades_for_the_same_reason(self):
        """`:nth-child(2 of .promoted)` takes the specificity of its `of`
        argument. Plain `:nth-child(2)` does not and stays decidable."""
        payload = facts(
            selector=".card",
            raw_sources={
                "0": ":nth-child(2 of .promoted) { animation-duration: 2s; }\n"
                ".card { animation-duration: 2s; }\n"
            },
            computed=computed(animation_name="fade", animation_duration="2s"),
            matched_rules=[
                rule(":nth-child(2 of .promoted)", {"animation-duration": "2s"}),
                rule(".card", {"animation-duration": "2s"}, source_ref="src-1"),
            ],
            sources=[
                source("src-0", selector=":nth-child(2 of .promoted)"),
                source("src-1", selector=".card"),
            ],
        )
        found = await recipes(payload, name="fade")
        assert found["duration"]["confidence"] == "low"
        assert "find" not in found["duration"]

    async def test_a_lone_nested_rule_is_still_actionable(self):
        """Undecidable specificity only matters when it has to be COMPARED. One
        rule declaring the knob has no cascade to resolve, and the value
        cross-check still guards it, so degrading here would lose a real answer
        for no gain."""
        payload = facts(
            selector="#shell .card",
            raw_sources={"0": "#shell { & .card { animation-duration: 2s; } }\n"},
            computed=computed(animation_name="fade", animation_duration="2s"),
            matched_rules=[rule("& .card", {"animation-duration": "2s"})],
            sources=[source("src-0", selector="& .card")],
        )
        found = await recipes(payload, name="fade")
        assert found["duration"]["find"] == "animation-duration: 2s"
        assert found["duration"]["confidence"] == "high"

    async def test_plain_nth_child_still_computes_a_specificity(self):
        payload = facts(
            selector=".card",
            raw_sources={
                "0": "#shell :nth-child(2) { animation-duration: 5s; }\n"
                ".card { animation-duration: 2s; }\n"
            },
            computed=computed(animation_name="fade", animation_duration="5s"),
            matched_rules=[
                rule("#shell :nth-child(2)", {"animation-duration": "5s"}),
                rule(".card", {"animation-duration": "2s"}, source_ref="src-1"),
            ],
            sources=[
                source("src-0", selector="#shell :nth-child(2)"),
                source("src-1", selector=".card"),
            ],
        )
        found = await recipes(payload, name="fade")
        assert found["duration"]["rule_selector"] == "#shell :nth-child(2)"
        assert found["duration"]["confidence"] == "high"

    async def test_a_winner_that_disagrees_with_the_computed_value_degrades(self):
        """If the rule we picked does not produce what the browser computed,
        something we never saw is winning — an inline style, a UA sheet, a
        stylesheet we could not read. Emitting a confident find there points the
        model at CSS that is not in charge."""
        payload = facts(
            selector="#hero",
            raw_sources={"0": "#hero { animation-duration: 2s; }\n"},
            computed=computed(animation_name="fade", animation_duration="9s"),
            matched_rules=[rule("#hero", {"animation-duration": "2s"})],
            sources=[source("src-0", selector="#hero")],
        )
        found = await recipes(payload, name="fade")
        assert found["duration"]["confidence"] == "low"
        assert "find" not in found["duration"]
        assert "9s" in found["duration"]["note"] or "2s" in found["duration"]["note"]


# ===========================================================================
# Degradation still works the way F-849 established
# ===========================================================================


class TestDegradationIsUnchanged:
    async def test_a_linked_sheet_still_offers_a_pointer_and_no_find(self):
        payload = facts(
            selector="#hero",
            raw_sources={"0": None},
            computed=computed(animation_name="fade", animation_duration="2s"),
            matched_rules=[rule("#hero", {"animation-duration": "2s"})],
            sources=[
                {
                    **source("src-0", selector="#hero"),
                    "source_text_available": False,
                    "stylesheet": {
                        "index": 0,
                        "href": "https://cdn.test/app.css",
                        "kind": "link",
                    },
                }
            ],
        )
        found = await recipes(payload, name="fade")
        assert "find" not in found["duration"]
        assert found["duration"]["confidence"] == "low"
        assert found["duration"]["file"] == "https://cdn.test/app.css"

    async def test_no_recipe_ever_offers_chromes_serialization_as_a_find(self):
        """The F-849 invariant, restated as a property over every recipe: a find
        literal that is not in the author's text must not exist."""
        for payload_facts, name, text in (
            (TORTURE, "torture-pulse", TORTURE_SOURCE),
            (CASCADE, "fade", CASCADE_SOURCE),
        ):
            for edit in (await recipes(payload_facts, name)).values():
                if "find" in edit:
                    assert edit["find"] in text, f"{edit['knob']}: {edit['find']!r}"
                if "replace" in edit:
                    rebuilt = edit["replace"].replace(PLACEHOLDER, edit["token"])
                    assert rebuilt == edit["find"], (
                        f"{edit['knob']}: replace does not rebuild find"
                    )


# ===========================================================================
# Transitions get the same treatment
# ===========================================================================

TRANSITION_SOURCE = "#fader { transition: transform .6s ease-out .1s; }\n"

TRANSITION = facts(
    selector="#fader",
    raw_sources={"0": TRANSITION_SOURCE},
    computed=computed(
        transition_property="transform",
        transition_duration="0.6s",
        transition_delay="0.1s",
        transition_timing_function="ease-out",
    ),
    matched_rules=[rule("#fader", {"transition": "transform 0.6s ease-out 0.1s"})],
    sources=[source("src-0", selector="#fader")],
)


class TestTransitionRecipes:
    async def test_a_transition_knob_names_its_own_token(self):
        payload = await extract(TRANSITION)
        edits = {e["knob"]: e for e in payload["transitions"][0]["edits"]}
        assert edits["duration"]["token"] == ".6s"
        assert edits["delay"]["token"] == ".1s"
        assert edits["easing"]["token"] == "ease-out"

    async def test_applying_a_transition_edit_keeps_the_property(self):
        payload = await extract(TRANSITION)
        edits = {e["knob"]: e for e in payload["transitions"][0]["edits"]}
        updated = apply(edits["duration"], "1s", TRANSITION_SOURCE)
        assert "transition: transform 1s ease-out .1s" in updated


# ===========================================================================
# The documented promise must be one the payload keeps
# ===========================================================================


class TestTheToolDocstringDoesNotOversell:
    def test_the_tools_do_not_promise_a_bare_verified_unique_find(self):
        """The server docstring sold ``find`` as "verified unique in its rule".
        That claim is true only for the recipes that carry
        ``find_unique_in_rule: true``, and a docstring is what a model reads
        before it ever sees a payload."""
        from stealth_chrome_devtools_mcp.embedded import server as _server

        for tool in (
            "extract_element_animations",
            "extract_element_animations_to_file",
        ):
            doc = getattr(_server, tool).fn.__doc__ or ""
            claim = re.search(r"verified unique", doc)
            assert claim is None or "find_unique_in_rule" in doc, (
                f"{tool} promises uniqueness without naming the field that reports it"
            )


class TestATruncationMessageCanBeFollowedWithoutFailing:
    """R14: the R12 truncation message told the reader to pass ``max_animations``
    "to this tool", and neither ``extract_element_animations`` nor its
    ``_to_file`` twin accepts it -- so the payload confidently instructed a
    model to make a call that would be rejected.

    A truncated payload is exactly when a model is most motivated to act on the
    remedy, which makes this the same honesty class the whole review removed,
    arriving through the fix for it. The message is produced by a leaf that
    cannot see the tool signatures, so nothing but a test can hold the two
    together; this one reads the REAL signatures rather than a copy of them, so
    it also fires the day someone exposes the parameters and leaves the message
    saying they are unavailable.
    """

    ANIMATIONS_TOOLS = (
        "extract_element_animations",
        "extract_element_animations_to_file",
    )

    def _accepted(self) -> set[str]:
        import inspect

        from stealth_chrome_devtools_mcp.embedded import server as _server

        return set.intersection(
            *(
                set(inspect.signature(getattr(_server, tool).fn).parameters)
                for tool in self.ANIMATIONS_TOOLS
            )
        )

    @pytest.mark.parametrize(
        ("noun", "cap", "option"),
        [("animations", 25, "max_animations"), ("keyframes", 20, "max_keyframes")],
    )
    def test_it_never_tells_a_tool_caller_to_pass_an_option_the_tool_rejects(
        self, noun, cap, option
    ):
        from stealth_chrome_devtools_mcp.embedded import animation_facts

        message = animation_facts.cap_message(noun, cap, option)
        if option in self._accepted():
            return  # the parameter exists; telling the caller to pass it is true
        assert "this tool" not in message, (
            f"the message tells the reader to pass {option} 'to this tool', but "
            f"neither {' nor '.join(self.ANIMATIONS_TOOLS)} accepts it"
        )
        assert option not in message or "clone_element_complete" in message, (
            f"{option} is named without the path it is actually settable on, so "
            "a reader following it has nowhere to go"
        )

    def test_it_always_offers_the_remedy_a_direct_tool_caller_can_actually_use(self):
        """Narrowing the selector is the ONLY lever the two direct tools give a
        caller. It has to be there whatever the parameter situation is."""
        from stealth_chrome_devtools_mcp.embedded import animation_facts

        message = animation_facts.cap_message("animations", 25, "max_animations")
        assert "selector" in message, "no remedy the direct tool caller can act on"
