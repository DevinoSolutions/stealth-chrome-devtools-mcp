"""Animation aspect schema v2 (F-846 / F-847 / F-848).

Drives the REAL engine + the REAL Python derivation against a fact payload
delivered exactly the way the browser delivers it — a **JSON string** (F-846).
The browser half (``embedded/js/extract_animations.js``) is a fact collector
only, so everything asserted here is hermetically testable with no Chrome.

Two regression nets are load-bearing and must never be relaxed:

* **D1/F-846** — keyframes arrive as REAL parsed structures. If the transport
  regresses to CDP deep-serialization, ``keyframes`` becomes a
  ``{"type": "object", "value": [...]}`` blob and these tests red.
* **D2/F-847** — keyframe resolution works with 2+ animations on one element.
  The v1 code compared the whole ``"pulse, spin"`` list against
  ``CSSKeyframesRule.name``, so keyframes came back empty EXACTLY when there was
  more than one animation — the case the feature exists for.
"""

import json
from pathlib import Path

import pytest

from fakes import ANIMATION_JS_MARKER, FakeTab, animation_evaluate_map
from stealth_chrome_devtools_mcp.embedded import cdp_element_cloner as _cdc

# ---------------------------------------------------------------------------
# Fact payloads (test DATA; the transport MECHANISM lives in fakes.py)
# ---------------------------------------------------------------------------


def computed(**over):
    """A full computed block — every key the collector reads, defaults inert."""
    base = {
        "animation_name": "none",
        "animation_duration": "0s",
        "animation_delay": "0s",
        "animation_timing_function": "ease",
        "animation_iteration_count": "1",
        "animation_direction": "normal",
        "animation_fill_mode": "none",
        "animation_play_state": "running",
        "animation_composition": "replace",
        "animation_timeline": "auto",
        "animation_range_start": "normal",
        "animation_range_end": "normal",
        "transition_property": "all",
        "transition_duration": "0s",
        "transition_delay": "0s",
        "transition_timing_function": "ease",
        "transition_behavior": "normal",
    }
    base.update(over)
    return base


def facts(**over):
    base = {
        "facts_version": 1,
        "selector": "#hero",
        "url": "https://fake.test/anim",
        "captured_at_ms": 1234.5,
        "element": {
            "tag": "div",
            "id": "hero",
            "classes": ["hero"],
            "inline_properties": [],
            "is_canvas": False,
        },
        "computed": computed(),
        "transforms": {"transform": "none", "transform_origin": "50% 50%"},
        "keyframe_rules": [],
        "waapi": [],
        "matched_rules": [],
        "candidate_rules": [],
        "sources": [],
        "raw_sources": {},
        "warnings": [],
        "caps_hit": {},
    }
    base.update(over)
    return base


# The AUTHOR'S bytes for TWO_ANIMATIONS, deliberately NOT in Chrome's CSSOM
# serialization (F-849): the animation NAME comes first in the shorthand, the
# decimals are bare (`.2s`, `.5`), the cubic-bezier is tightly spaced, and there
# is no `running` keyword. A fixture written in CSSOM shape could not fail the
# find-literal assertions, because the fixture and the assertion would share one
# serialization -- which is exactly how this defect survived the first round.
AUTHOR_SOURCE = """
.hero {
  animation: pulse 2s cubic-bezier(.34,1.56,.64,1) .2s infinite alternate both,
             spin 3s linear .2s 2 normal both;
  animation-duration: 2s, 3s;
  transition: opacity .3s ease-in-out;
}
@keyframes pulse {
  0%,50% { transform: scale(1) }
  100%   { transform: scale(1.08); opacity: .5 }
}
@keyframes spin {
  from { transform: rotate(0deg) }
  to   { transform: rotate(360deg) }
}
"""


# Two CSS animations on ONE element with per-animation lists of DIFFERENT
# lengths, so the CSS list-cycling rule is actually exercised: 2 names, 2
# durations, 1 delay (cycles), 2 iteration counts.
TWO_ANIMATIONS = facts(
    raw_sources={"0": AUTHOR_SOURCE},
    computed=computed(
        animation_name="pulse, spin",
        animation_duration="2s, 3s",
        animation_delay="0.2s",
        animation_timing_function="cubic-bezier(0.34, 1.56, 0.64, 1), linear",
        animation_iteration_count="infinite, 2",
        animation_direction="alternate, normal",
        animation_fill_mode="both",
        transition_property="opacity",
        transition_duration="0.3s",
        transition_timing_function="ease-in-out",
    ),
    keyframe_rules=[
        {
            "name": "pulse",
            "source_ref": "src-1",
            "keyframes": [
                {
                    "key_text": "0%, 50%",
                    "css_text": "transform: scale(1);",
                    "easing": "ease-out",
                    "composite": "",
                },
                {
                    "key_text": "100%",
                    "css_text": "transform: scale(1.08); opacity: 0.5;",
                    "easing": "",
                    "composite": "",
                },
            ],
        },
        {
            "name": "spin",
            "source_ref": "src-2",
            "keyframes": [
                {
                    "key_text": "from",
                    "css_text": "transform: rotate(0deg);",
                    "easing": "",
                    "composite": "",
                },
                {
                    "key_text": "to",
                    "css_text": "transform: rotate(360deg);",
                    "easing": "",
                    "composite": "",
                },
            ],
        },
    ],
    matched_rules=[
        {
            "source_ref": "src-0",
            "selector_text": ".hero",
            "css_text": (
                ".hero { animation: pulse 2s cubic-bezier(0.34, 1.56, 0.64, 1) "
                "0.2s infinite alternate both, spin 3s linear 0.2s 2 normal both; "
                "animation-duration: 2s, 3s; "
                "transition: opacity 0.3s ease-in-out; }"
            ),
            # What ``style.getPropertyValue`` reports for the rule: the
            # shorthand in Chrome's serialization (name last, decimals expanded,
            # `running` injected) ALONGSIDE the longhands the author also wrote.
            # The shorthand has to be here for the same reason the defect
            # existed: it is the only declaration carrying easing/delay/
            # iterations, and a recipe for those has to address it.
            "declares": {
                "animation": (
                    "2s cubic-bezier(0.34, 1.56, 0.64, 1) 0.2s infinite alternate "
                    "both running pulse, 3s linear 0.2s 2 normal both running spin"
                ),
                "animation-name": "pulse, spin",
                "animation-duration": "2s, 3s",
                "transition": "opacity 0.3s ease-in-out",
            },
            "important": [],
            "matches_now": True,
            "matches_base": True,
            "at_rule_context": [],
        }
    ],
    sources=[
        {
            "id": "src-0",
            "kind": "rule",
            "stylesheet": {
                "index": 0,
                "href": None,
                "kind": "style",
                "origin": "author",
                "disabled": False,
            },
            "source_text_available": True,
            "rule_path": [3],
            "at_rule_context": [],
            "name": None,
            "selector_text": ".hero",
            "css_text": (
                ".hero { animation: pulse 2s cubic-bezier(0.34, 1.56, 0.64, 1) "
                "0.2s infinite alternate both, spin 3s linear 0.2s 2 normal both; "
                "animation-duration: 2s, 3s; "
                "transition: opacity 0.3s ease-in-out; }"
            ),
        },
        {
            "id": "src-1",
            "kind": "keyframes",
            "stylesheet": {
                "index": 0,
                "href": None,
                "kind": "style",
                "origin": "author",
                "disabled": False,
            },
            "source_text_available": True,
            "rule_path": [4],
            "at_rule_context": [],
            "name": "pulse",
            "selector_text": None,
            "css_text": (
                "@keyframes pulse { 0%, 50% { transform: scale(1); } "
                "100% { transform: scale(1.08); opacity: 0.5; } }"
            ),
        },
        {
            "id": "src-2",
            "kind": "keyframes",
            "stylesheet": {
                "index": 0,
                "href": None,
                "kind": "style",
                "origin": "author",
                "disabled": False,
            },
            "source_text_available": True,
            "rule_path": [5],
            "at_rule_context": [],
            "name": "spin",
            "selector_text": None,
            "css_text": "@keyframes spin { from { transform: rotate(0deg) } }",
        },
    ],
)


def only(items, **match):
    """The one item matching ``match`` — asserts rather than raising
    StopIteration, so a red test names what was missing instead of surfacing as
    "coroutine raised StopIteration"."""
    found = [i for i in items if all(i.get(k) == v for k, v in match.items())]
    assert len(found) == 1, f"expected exactly one {match}, got {list(items)}"
    return found[0]


def anim_tab(payload):
    """A FakeTab answering the animations script with ``payload`` as a JSON string."""
    return FakeTab(evaluate_map=animation_evaluate_map(payload))


async def extract(payload, **kwargs):
    return await _cdc.cdp_element_cloner.extract_element_animations(
        anim_tab(payload), selector="#hero", **kwargs
    )


# ===========================================================================
# D1 / F-846 — the transport
# ===========================================================================


class TestTransport:
    async def test_json_string_is_parsed_into_a_real_dict(self):
        result = await extract(TWO_ANIMATIONS)
        assert isinstance(result, dict)
        assert result["schema_version"] == 2
        # Not the raw string, and not a CDP deep-serialization blob.
        assert "type" not in result
        assert result["selector"] == "#hero"

    async def test_keyframes_are_real_parsed_json_not_a_serialization_blob(self):
        """The D1 regression net. Under the v1 transport this arrived as
        ``{"type": "object", "value": [["pulse", {...}]]}`` — unusable."""
        result = await extract(TWO_ANIMATIONS)
        pulse = only(result["animations"], name="pulse")
        assert isinstance(pulse["keyframes"], list)
        first = pulse["keyframes"][0]
        assert isinstance(first, dict)
        assert isinstance(first["offset"], float)
        assert isinstance(first["properties"], dict)
        assert first["properties"]["transform"] == "scale(1)"
        # A deep-serialization blob would carry these instead of real fields.
        assert "type" not in first and "value" not in first

    async def test_the_script_really_is_asked_for_a_string(self):
        """Fidelity guard: the fake answers the marker in the real script, so
        this reds if the collector ever stops carrying it."""
        tab = anim_tab(TWO_ANIMATIONS)
        await _cdc.cdp_element_cloner.extract_element_animations(tab, selector="#hero")
        assert tab.evaluate_calls
        assert ANIMATION_JS_MARKER in tab.evaluate_calls[0]
        assert "JSON.stringify(facts)" in tab.evaluate_calls[0]

    async def test_non_json_answer_is_an_honest_error_not_a_crash(self):
        tab = FakeTab(evaluate_result="<html>not json</html>")
        result = await _cdc.cdp_element_cloner.extract_element_animations(
            tab, selector="#hero"
        )
        assert "error" in result

    async def test_element_not_found_keeps_the_aspect_error_shape(self):
        # Q4: this aspect keeps parity with the other five — a dict, not a raise.
        result = await extract({"error": "Element not found"})
        assert result == {"error": "Element not found"}

    async def test_selector_is_required(self):
        tab = anim_tab(TWO_ANIMATIONS)
        result = await _cdc.cdp_element_cloner.extract_element_animations(
            tab, selector=None
        )
        assert result == {"error": "Selector is required"}


# ===========================================================================
# D2 / F-847 — per-animation records and the comma-split keyframe lookup
# ===========================================================================


class TestPerAnimationRecords:
    async def test_two_animations_become_two_records(self):
        result = await extract(TWO_ANIMATIONS)
        assert [a["name"] for a in result["animations"]] == ["pulse", "spin"]
        assert result["has_motion"] is True

    async def test_keyframes_resolve_for_both_animations(self):
        """The D2 regression net: v1 matched ``"pulse, spin"`` against each
        ``CSSKeyframesRule.name``, so BOTH came back empty."""
        result = await extract(TWO_ANIMATIONS)
        by_name = {a["name"]: a for a in result["animations"]}
        assert by_name["pulse"]["keyframes"], "pulse keyframes must resolve"
        assert by_name["spin"]["keyframes"], "spin keyframes must resolve"

    async def test_shorter_lists_cycle_per_the_css_rule(self):
        """2 names, 1 delay: the delay list cycles independently."""
        result = await extract(TWO_ANIMATIONS)
        pulse, spin = result["animations"]
        assert pulse["timing"]["duration_ms"] == 2000
        assert spin["timing"]["duration_ms"] == 3000
        assert pulse["timing"]["delay_ms"] == 200
        assert spin["timing"]["delay_ms"] == 200  # cycled, not dropped
        assert pulse["timing"]["iterations"] == "infinite"
        assert spin["timing"]["iterations"] == 2

    async def test_numeric_and_raw_live_side_by_side(self):
        result = await extract(TWO_ANIMATIONS)
        pulse = result["animations"][0]
        assert pulse["timing"]["duration_ms"] == 2000
        assert pulse["timing"]["duration_raw"] == "2s"

    async def test_comma_offset_keyframe_expands_to_one_record_per_offset(self):
        result = await extract(TWO_ANIMATIONS)
        pulse = result["animations"][0]
        offsets = [k["offset"] for k in pulse["keyframes"]]
        assert offsets == [0.0, 0.5, 1.0]  # "0%, 50%" expanded, then 100%

    async def test_from_and_to_are_offsets_not_text(self):
        result = await extract(TWO_ANIMATIONS)
        spin = result["animations"][1]
        assert [k["offset"] for k in spin["keyframes"]] == [0.0, 1.0]


# ===========================================================================
# D3 — default-transition noise suppression (M8)
# ===========================================================================


class TestNoiseSuppression:
    async def test_zero_duration_zero_delay_transition_is_dropped(self):
        # The UA default `all 0s ease 0s` is not information.
        result = await extract(facts())
        assert result["transitions"] == []
        assert result["has_motion"] is False

    async def test_a_real_transition_survives(self):
        result = await extract(TWO_ANIMATIONS)
        assert [t["property"] for t in result["transitions"]] == ["opacity"]
        assert result["transitions"][0]["duration_ms"] == 300

    async def test_nonzero_delay_alone_is_kept(self):
        payload = facts(
            computed=computed(
                transition_property="opacity",
                transition_duration="0s",
                transition_delay="0.5s",
            )
        )
        result = await extract(payload)
        assert len(result["transitions"]) == 1
        assert result["transitions"][0]["delay_ms"] == 500

    async def test_v1_keys_are_gone(self):
        # Q1: clean schema break.
        result = await extract(TWO_ANIMATIONS)
        for dead in ("css_animations", "css_transitions", "keyframe_rules"):
            assert dead not in result


# ===========================================================================
# T1 — sources (M3) and edit recipes (M10)
# ===========================================================================


class TestSourcesAndRecipes:
    async def test_every_animation_points_at_its_sources(self):
        result = await extract(TWO_ANIMATIONS)
        pulse = result["animations"][0]
        assert "src-1" in pulse["source_refs"]  # its @keyframes block
        assert "src-0" in pulse["source_refs"]  # the rule that applied it
        by_id = {s["id"]: s for s in result["sources"]}
        assert by_id["src-1"]["kind"] == "keyframes"
        assert by_id["src-1"]["stylesheet"]["kind"] == "style"
        assert by_id["src-1"]["rule_path"] == [4]
        # The author's own rule text, sliced out of the sheet's raw source.
        assert "transform: scale(1.08)" in by_id["src-1"]["source_text"]

    async def test_duration_recipe_is_a_verified_find_literal(self):
        """F-849: verified against the AUTHOR's bytes, not Chrome's cssText."""
        result = await extract(TWO_ANIMATIONS)
        pulse = result["animations"][0]
        duration = only(pulse["edits"], knob="duration")
        # The longhand IS a comma list covering both animations; this record is
        # animation 0, so its current value is the first item, not "2s, 3s".
        assert duration["current"] == "2s"
        assert duration["file"] == "<style> #0"
        assert duration["source_ref"] == "src-0"
        # The find literal must occur in what the author actually wrote.
        assert duration["find"] in AUTHOR_SOURCE
        assert duration["find_unique_in_rule"] is True

    async def test_a_shorthand_only_knob_points_at_the_authored_shorthand(self):
        """`easing` has no longhand here, so the recipe addresses the shorthand
        the author wrote -- name first, bare decimals, tight bezier -- and names
        the one token inside it that this knob owns (F-852)."""
        result = await extract(TWO_ANIMATIONS)
        easing = only(result["animations"][0]["edits"], knob="easing")
        assert easing["find"] in AUTHOR_SOURCE
        assert "pulse 2s cubic-bezier(.34,1.56,.64,1)" in easing["find"]
        assert "running" not in easing["find"]
        # Superseded the prose "change the easing component within find": a
        # sentence is not something a model can apply mechanically, and the four
        # other knobs carried the identical sentence with the identical find.
        assert easing["token"] == "cubic-bezier(.34,1.56,.64,1)"
        assert easing["replace"].count("{{NEW_VALUE}}") == 1
        assert "pulse 2s {{NEW_VALUE}}" in easing["replace"]

    async def test_a_linked_sheet_degrades_to_a_pointer(self):
        """No author bytes (we do not re-fetch, Q5) means no find literal --
        never a CSSOM literal dressed up as one."""
        payload = dict(TWO_ANIMATIONS)
        payload["raw_sources"] = {}
        result = await extract(payload)
        for recipe in result["animations"][0]["edits"]:
            assert "find" not in recipe
            assert recipe["confidence"] == "low"
            assert recipe["source_ref"]
            assert recipe["note"]

    async def test_a_selector_declared_twice_is_ambiguous_not_guessed(self):
        """Two `.hero` blocks is ordinary CSS, and we cannot tell which one the
        CSSOM record came from. Picking the first would be a coin flip dressed
        as a verified address."""
        payload = dict(TWO_ANIMATIONS)
        payload["raw_sources"] = {"0": AUTHOR_SOURCE + "\n.hero { color: red }"}
        result = await extract(payload)
        timing_edits = [
            e
            for e in result["animations"][0]["edits"]
            if not e["knob"].startswith("keyframe[")
        ]
        assert timing_edits
        assert all("find" not in e for e in timing_edits)

    async def test_keyframe_declarations_get_their_own_recipes(self):
        result = await extract(TWO_ANIMATIONS)
        pulse = result["animations"][0]
        knobs = [e["knob"] for e in pulse["edits"]]
        assert "keyframe[1.0].transform" in knobs
        recipe = only(pulse["edits"], knob="keyframe[1.0].transform")
        assert recipe["current"] == "scale(1.08)"
        assert recipe["find"] == "transform: scale(1.08)"
        assert recipe["source_ref"] == "src-1"

    async def test_a_find_that_is_not_unique_says_so(self):
        """The same declaration twice inside ONE keyframe block: the model must
        be warned off a blind replace rather than handed a risky literal."""
        payload = facts(
            computed=computed(animation_name="dup", animation_duration="1s"),
            keyframe_rules=[
                {
                    "name": "dup",
                    "source_ref": "src-1",
                    "keyframes": [
                        {
                            "key_text": "0%",
                            "css_text": "opacity: 1;",
                            "easing": "",
                            "composite": "",
                        },
                        {
                            "key_text": "100%",
                            "css_text": "opacity: 1;",
                            "easing": "",
                            "composite": "",
                        },
                    ],
                }
            ],
            sources=[
                {
                    "id": "src-1",
                    "kind": "keyframes",
                    "stylesheet": {"index": 0, "href": None, "kind": "style"},
                    "rule_path": [1],
                    "at_rule_context": [],
                    "name": "dup",
                    "selector_text": None,
                    "source_text_available": True,
                    "css_text": "@keyframes dup { 0% { opacity: 1 } 100% { opacity: 1 } }",
                }
            ],
            raw_sources={
                "0": "@keyframes dup { 0% { opacity: 1; opacity: 1 } 100% { opacity: 1 } }"
            },
        )
        result = await extract(payload)
        edits = result["animations"][0]["edits"]
        # The 0% block declares opacity TWICE, so a blind replace is unsafe.
        risky = only(edits, knob="keyframe[0.0].opacity")
        assert risky["find_unique_in_rule"] is False
        assert risky["confidence"] == "medium"
        assert "position" in risky["note"]
        # The 100% block declares it once. Scoping per keyframe block is what
        # keeps this one safe: a body-wide search would see both and call it
        # ambiguous, or worse, hand back the 0% block's declaration.
        safe = only(edits, knob="keyframe[1.0].opacity")
        assert safe["find_unique_in_rule"] is True
        assert safe["confidence"] == "high"

    async def test_no_css_origin_is_reported_as_not_editable(self):
        """The negative case (M10): editing CSS that has no effect on a running
        element.animate() is the wrong edit, so say so explicitly."""
        payload = facts(
            waapi=[
                {
                    "kind": "Animation",
                    "animation_name": None,
                    "author_id": "js-fade",
                    "play_state": "running",
                    "target": {"relation": "self", "selector": "#hero", "pseudo": ""},
                    "computed_timing": {
                        "delay": 0,
                        "duration": 500,
                        "iterations": 1,
                        "direction": "normal",
                        "fill": "none",
                        "easing": "linear",
                    },
                    "keyframes": [
                        {"offset": 0, "opacity": "0"},
                        {"offset": 1, "opacity": "1"},
                    ],
                }
            ]
        )
        result = await extract(payload)
        waapi = only(result["animations"], kind="waapi")
        assert waapi["edits"] == []
        assert waapi["editable"] is False
        assert "element.animate()" in waapi["not_editable_reason"]

    async def test_cross_origin_warning_names_the_href(self):
        # Q5: name it, never re-fetch it.
        payload = facts(
            warnings=[
                {
                    "code": "cross_origin_stylesheet",
                    "message": "Stylesheet rules unreadable (CORS)",
                    "detail": {"index": 4, "href": "https://cdn.example/vendor.css"},
                }
            ]
        )
        result = await extract(payload)
        warning = result["warnings"][0]
        assert warning["code"] == "cross_origin_stylesheet"
        assert warning["detail"]["href"] == "https://cdn.example/vendor.css"

    async def test_missing_keyframes_is_a_named_warning_not_silence(self):
        """M7: empty keyframes with no warning reads as 'no keyframes exist' and
        makes a model ADD a duplicate @keyframes block."""
        payload = facts(
            computed=computed(animation_name="ghost", animation_duration="1s")
        )
        result = await extract(payload)
        ghost = result["animations"][0]
        assert ghost["keyframes"] == []
        assert any(w["code"] == "keyframes_not_found" for w in ghost["warnings"])


# ===========================================================================
# T1 — derived timing (M9), semantics, summaries, checkpoints
# ===========================================================================


class TestDerivedTiming:
    async def test_alternate_doubles_the_cycle(self):
        result = await extract(TWO_ANIMATIONS)
        pulse = result["animations"][0]
        derived = pulse["derived"]
        assert derived["iteration_ms"] == 2000
        # direction: alternate — one full there-and-back cycle is 2 iterations.
        assert derived["cycle_ms"] == 4000
        assert derived["active_start_ms"] == 200

    async def test_infinite_stays_the_documented_string(self):
        """JSON.stringify turns Infinity into null, which reads as UNKNOWN
        rather than forever — the collector normalizes it to "infinite" and
        Python never re-interprets."""
        result = await extract(TWO_ANIMATIONS)
        derived = result["animations"][0]["derived"]
        assert derived["active_end_ms"] == "infinite"
        assert derived["total_ms"] == "infinite"

    async def test_finite_animation_gets_real_numbers(self):
        result = await extract(TWO_ANIMATIONS)
        spin = result["animations"][1]["derived"]
        assert spin["iteration_ms"] == 3000
        assert spin["cycle_ms"] == 3000  # direction: normal
        assert spin["active_start_ms"] == 200
        assert spin["active_end_ms"] == 6200  # 200 delay + 2 x 3000
        assert spin["total_ms"] == 6200

    async def test_fractional_iterations_are_not_rounded_away(self):
        payload = facts(
            computed=computed(
                animation_name="a",
                animation_duration="1s",
                animation_iteration_count="2.5",
            )
        )
        result = await extract(payload)
        assert result["animations"][0]["derived"]["active_end_ms"] == 2500

    async def test_iteration_start_shifts_nothing_it_cannot_decide(self):
        """iteration_start changes WHERE in the cycle playback begins, not the
        active duration — asserting it does would be a lie."""
        payload = facts(
            computed=computed(
                animation_name="a", animation_duration="1s", animation_delay="0s"
            )
        )
        result = await extract(payload)
        assert result["animations"][0]["derived"]["active_end_ms"] == 1000


class TestSemantics:
    @pytest.mark.parametrize(
        "easing,expected",
        [
            ("cubic-bezier(0.34, 1.56, 0.64, 1)", "overshoot"),
            ("cubic-bezier(0.4, 0, 0.2, 1)", "ease-in-out"),
            ("linear", "linear"),
            ("ease-in", "ease-in"),
            ("ease-out", "ease-out"),
            ("steps(4, end)", "stepped"),
            ("linear(0, 0.25 30%, 1)", "custom"),
        ],
    )
    async def test_easing_class_is_read_off_the_curve(self, easing, expected):
        payload = facts(
            computed=computed(
                animation_name="a",
                animation_duration="1s",
                animation_timing_function=easing,
            )
        )
        result = await extract(payload)
        semantics = result["animations"][0]["semantics"]
        # A derived field is a CLAIM: the value never travels without the
        # confidence the derivation produced for it (F-850).
        assert semantics["easing_class"]["value"] == expected
        assert semantics["easing_class"]["confidence"] in ("high", "medium", "low")
        # The raw string always stays alongside the classification.
        assert result["animations"][0]["timing"]["easing"] == easing

    async def test_unknown_easing_is_omitted_not_guessed(self):
        payload = facts(
            computed=computed(
                animation_name="a",
                animation_duration="1s",
                animation_timing_function="some-future-easing()",
            )
        )
        result = await extract(payload)
        assert "easing_class" not in result["animations"][0].get("semantics", {})

    async def test_motion_kind_comes_from_the_properties_touched(self):
        result = await extract(TWO_ANIMATIONS)
        by_name = {a["name"]: a for a in result["animations"]}
        # pulse touches transform: scale + opacity -> two families -> mixed
        assert by_name["pulse"]["semantics"]["motion_kind"]["value"] == "mixed"
        assert by_name["spin"]["semantics"]["motion_kind"]["value"] == "rotate"

    async def test_motion_kind_omitted_when_no_keyframes_are_readable(self):
        payload = facts(
            computed=computed(animation_name="ghost", animation_duration="1s")
        )
        result = await extract(payload)
        assert "motion_kind" not in result["animations"][0].get("semantics", {})


class TestSummaries:
    async def test_every_animation_gets_a_quotable_summary(self):
        result = await extract(TWO_ANIMATIONS)
        for animation in result["animations"]:
            assert isinstance(animation["summary"], str)
            assert animation["summary"]

    async def test_the_summary_never_contradicts_the_fields(self):
        """§3.1: template-generated FROM the payload, so it cannot drift."""
        result = await extract(TWO_ANIMATIONS)
        pulse = result["animations"][0]
        assert "2s" in pulse["summary"]
        assert "infinite" in pulse["summary"]
        assert "alternate" in pulse["summary"]

    async def test_overview_is_present_and_counts_correctly(self):
        result = await extract(TWO_ANIMATIONS)
        assert "2 animations" in result["overview"]
        assert "1 transition" in result["overview"]

    async def test_overview_says_so_when_nothing_moves(self):
        result = await extract(facts())
        assert result["has_motion"] is False
        assert "no" in result["overview"].lower()


class TestCheckpoints:
    async def test_a_checkpoint_on_a_keyframe_is_exact(self):
        result = await extract(TWO_ANIMATIONS)
        pulse = result["animations"][0]
        exact = only(pulse["checkpoints"], offset=0.5)
        assert exact["exact"] is True
        assert exact["values"]["transform"] == "scale(1)"
        assert exact["time_ms"] == 1000

    async def test_a_checkpoint_between_keyframes_brackets_and_never_interpolates(self):
        result = await extract(TWO_ANIMATIONS)
        pulse = result["animations"][0]
        between = only(pulse["checkpoints"], offset=0.75)
        assert between["exact"] is False
        # No computed value: the bracketing keyframes and the segment easing.
        assert "values" not in between
        assert between["between"]["from_offset"] == 0.5
        assert between["between"]["to_offset"] == 1.0
        assert between["from"]["transform"] == "scale(1)"
        assert between["to"]["transform"] == "scale(1.08)"

    async def test_no_checkpoints_without_keyframes(self):
        payload = facts(
            computed=computed(animation_name="ghost", animation_duration="1s")
        )
        result = await extract(payload)
        assert result["animations"][0]["checkpoints"] == []


# ===========================================================================
# T2 — WAAPI/subtree (M4/S2), stagger (§3.3), interactions (§3.6)
# ===========================================================================


def waapi_entry(**over):
    base = {
        "kind": "Animation",
        "animation_name": "fade",
        "author_id": "",
        "play_state": "running",
        "target": {"relation": "self", "selector": "#hero", "pseudo": ""},
        "computed_timing": {
            "delay": 0,
            "duration": 500,
            "iterations": 1,
            "direction": "normal",
            "fill": "none",
            "easing": "linear",
        },
        "keyframes": [{"offset": 0, "opacity": "0"}, {"offset": 1, "opacity": "1"}],
    }
    base.update(over)
    return base


class TestWaapiAndSubtree:
    async def test_a_running_element_animate_is_reported_at_all(self):
        """v1 returned NOTHING for element.animate() — the model was told a
        visibly moving element was static."""
        result = await extract(facts(waapi=[waapi_entry()]))
        assert result["has_motion"] is True
        animation = only(result["animations"], kind="waapi")
        assert animation["timing"]["duration_ms"] == 500
        assert animation["animated_properties"] == ["opacity"]

    async def test_camel_case_waapi_properties_become_css_names(self):
        entry = waapi_entry(
            keyframes=[
                {"offset": 0, "backgroundColor": "red"},
                {"offset": 1, "backgroundColor": "blue"},
            ]
        )
        result = await extract(facts(waapi=[entry]))
        animation = only(result["animations"], kind="waapi")
        assert animation["animated_properties"] == ["background-color"]

    async def test_a_pseudo_element_animation_is_labelled(self):
        entry = waapi_entry(
            kind="CSSAnimation",
            animation_name="reveal",
            target={"relation": "self", "selector": "#hero", "pseudo": "::before"},
        )
        result = await extract(facts(waapi=[entry]))
        animation = only(result["animations"], name="reveal")
        assert animation["target"]["relation"] == "pseudo"
        assert animation["target"]["pseudo_element"] == "::before"

    async def test_a_descendant_animation_carries_its_own_selector(self):
        entry = waapi_entry(
            animation_name="slide",
            target={"relation": "descendant", "selector": "#hero > .row", "pseudo": ""},
        )
        result = await extract(facts(waapi=[entry]))
        animation = only(result["animations"], name="slide")
        assert animation["target"]["relation"] == "descendant"
        assert animation["target"]["selector"] == "#hero > .row"

    async def test_a_css_animation_is_not_duplicated_by_its_waapi_twin(self):
        """The same animation must not appear twice: getAnimations() reports the
        CSSAnimation the computed style already described."""
        entry = waapi_entry(kind="CSSAnimation", animation_name="pulse")
        payload = dict(TWO_ANIMATIONS)
        payload["waapi"] = [entry]
        result = await extract(payload)
        assert [a["name"] for a in result["animations"]].count("pulse") == 1

    async def test_composite_is_carried_through(self):
        entry = waapi_entry(composite="add")
        result = await extract(facts(waapi=[entry]))
        assert (
            only(result["animations"], kind="waapi")["timing"]["composition"] == "add"
        )

    async def test_infinite_iterations_survive_as_the_string(self):
        entry = waapi_entry(
            computed_timing={
                "delay": 0,
                "duration": 500,
                "iterations": "infinite",
                "direction": "normal",
                "fill": "none",
                "easing": "linear",
            }
        )
        result = await extract(facts(waapi=[entry]))
        animation = only(result["animations"], kind="waapi")
        assert animation["timing"]["iterations"] == "infinite"
        assert animation["derived"]["total_ms"] == "infinite"


class TestStagger:
    def _member(self, index, delay_ms):
        return waapi_entry(
            kind="CSSAnimation",
            animation_name="rise",
            target={
                "relation": "descendant",
                "selector": f"#hero > li:nth-of-type({index + 1})",
                "pseudo": "",
            },
            computed_timing={
                "delay": delay_ms,
                "duration": 400,
                "iterations": 1,
                "direction": "normal",
                "fill": "both",
                "easing": "ease",
            },
        )

    async def test_a_uniform_stagger_reports_one_delta(self):
        entries = [self._member(i, i * 80) for i in range(5)]
        result = await extract(facts(waapi=entries))
        first = only(result["animations"], id="anim-0")
        group = first["derived"]["stagger_group"]
        assert group["members"] == 5
        assert group["uniform"] is True
        assert group["delta_ms"] == 80
        assert group["delays_ms"] == [0, 80, 160, 240, 320]

    async def test_an_uneven_stagger_never_reports_an_averaged_delta(self):
        """Honesty (§3.3): an averaged delta would be a number we invented, and
        an off-by-one stagger is a visible bug."""
        entries = [self._member(i, d) for i, d in enumerate([0, 80, 300])]
        result = await extract(facts(waapi=entries))
        group = only(result["animations"], id="anim-0")["derived"]["stagger_group"]
        assert group["uniform"] is False
        assert "delta_ms" not in group
        assert group["delays_ms"] == [0, 80, 300]

    async def test_a_lone_animation_is_not_a_stagger_group(self):
        result = await extract(facts(waapi=[self._member(0, 0)]))
        assert "stagger_group" not in result["animations"][0]["derived"]


class TestInteractions:
    async def test_two_animations_writing_one_property_is_flagged(self):
        result = await extract(TWO_ANIMATIONS)
        conflict = only(result["interactions"], code="same_property_multi_writer")
        assert set(conflict["involves"]) == {"anim-0", "anim-1"}
        assert "transform" in conflict["message"]
        assert conflict["remedy"]

    async def test_a_paused_animation_is_flagged(self):
        payload = facts(
            computed=computed(
                animation_name="a",
                animation_duration="1s",
                animation_play_state="paused",
            )
        )
        result = await extract(payload)
        assert only(result["interactions"], code="paused_play_state")

    async def test_a_zero_duration_animation_is_flagged(self):
        payload = facts(computed=computed(animation_name="a", animation_duration="0s"))
        result = await extract(payload)
        assert only(result["interactions"], code="zero_duration_animation")

    async def test_fill_none_before_a_delay_explains_the_dead_time(self):
        payload = facts(
            computed=computed(
                animation_name="a",
                animation_duration="1s",
                animation_delay="0.3s",
                animation_fill_mode="none",
            )
        )
        result = await extract(payload)
        conflict = only(result["interactions"], code="fill_none_before_delay")
        assert "300" in conflict["message"]

    async def test_an_inline_style_override_names_the_property(self):
        payload = facts(
            element={
                "tag": "div",
                "id": "hero",
                "classes": ["hero"],
                "inline_properties": ["transform"],
                "is_canvas": False,
            },
            computed=computed(animation_name="spin", animation_duration="1s"),
            keyframe_rules=[
                {
                    "name": "spin",
                    "source_ref": "src-2",
                    "keyframes": [
                        {
                            "key_text": "0%",
                            "css_text": "transform: rotate(0deg);",
                            "easing": "",
                            "composite": "",
                        }
                    ],
                }
            ],
        )
        result = await extract(payload)
        conflict = only(result["interactions"], code="inline_style_overrides")
        assert "transform" in conflict["message"]

    async def test_interactions_do_not_fire_on_absent_data(self):
        """Sizing note for T2: an interaction that fires on missing facts is
        worse than one that never fires."""
        result = await extract(facts())
        assert result["interactions"] == []


# ===========================================================================
# T3 — timeline typing (M12) and trigger attribution (§3.5)
# ===========================================================================


class TestTimeline:
    async def test_a_time_animation_is_typed_time(self):
        result = await extract(TWO_ANIMATIONS)
        assert result["animations"][0]["timeline"]["type"] == "time"

    async def test_a_view_timeline_is_typed_and_duration_is_not_faked(self):
        """M12: a view() animation reports duration "auto" and iterations 1. Read
        as a time animation that is broken, a model will 'repair' correct code."""
        entry = waapi_entry(
            kind="CSSAnimation",
            animation_name="reveal",
            timeline={
                "type": "ViewTimeline",
                "axis": "block",
                "subject_selector": "#hero",
            },
            computed_timing={
                "delay": 0,
                "duration": "auto",
                "iterations": 1,
                "direction": "normal",
                "fill": "both",
                "easing": "linear",
            },
            range_start="cover 0%",
            range_end="cover 100%",
        )
        result = await extract(facts(waapi=[entry]))
        animation = only(result["animations"], name="reveal")
        assert animation["timeline"]["type"] == "view"
        assert animation["timeline"]["axis"] == "block"
        assert animation["timeline"]["range_start"] == "cover 0%"
        # duration_ms must be OMITTED, never coerced to 0.
        assert "duration_ms" not in animation["timing"]
        assert animation["timing"]["duration_raw"] == "auto"

    async def test_a_scroll_timeline_warns_that_duration_edits_are_a_noop(self):
        entry = waapi_entry(
            kind="CSSAnimation",
            animation_name="progress",
            timeline={
                "type": "ScrollTimeline",
                "axis": "y",
                "subject_selector": "#doc",
            },
            computed_timing={
                "delay": 0,
                "duration": "auto",
                "iterations": 1,
                "direction": "normal",
                "fill": "both",
                "easing": "linear",
            },
        )
        result = await extract(facts(waapi=[entry]))
        assert (
            only(result["animations"], name="progress")["timeline"]["type"] == "scroll"
        )
        conflict = only(result["interactions"], code="scroll_timeline_duration_noop")
        assert conflict["severity"] == "high"
        assert "animation-range" in conflict["remedy"]


class TestTriggers:
    async def test_a_matching_rule_with_no_interaction_pseudo_reads_as_load(self):
        result = await extract(TWO_ANIMATIONS)
        trigger = result["animations"][0]["trigger"]
        assert trigger["kind"] == "load"
        assert trigger["confidence"] == "medium"

    async def test_a_hover_rule_is_attributed_to_hover(self):
        payload = facts(
            computed=computed(
                transition_property="opacity",
                transition_duration="0.3s",
            ),
            matched_rules=[
                {
                    "source_ref": "src-0",
                    "selector_text": ".hero",
                    "css_text": ".hero { transition: opacity 0.3s; }",
                    "declares": {"transition": "opacity 0.3s"},
                    "important": [],
                    "matches_now": True,
                    "matches_base": True,
                    "at_rule_context": [],
                }
            ],
            candidate_rules=[
                {
                    "source_ref": "src-1",
                    "selector_text": ".hero:hover",
                    "css_text": ".hero:hover { opacity: 0.5; }",
                    "declares": {},
                    "important": [],
                    "matches_now": False,
                    "matches_base": True,
                    "at_rule_context": [],
                }
            ],
        )
        result = await extract(payload)
        trigger = result["transitions"][0]["trigger"]
        assert trigger["kind"] == "hover"
        assert trigger["confidence"] == "high"
        assert trigger["detail"]["rule_selector"] == ".hero:hover"

    async def test_a_class_toggle_names_the_class_to_toggle(self):
        payload = facts(
            candidate_rules=[
                {
                    "source_ref": "src-3",
                    "selector_text": ".hero.is-open",
                    "css_text": ".hero.is-open { animation: reveal 1s; }",
                    "declares": {"animation-name": "reveal"},
                    "important": [],
                    "matches_now": False,
                    "matches_base": False,
                    "at_rule_context": [],
                }
            ],
        )
        result = await extract(payload)
        pending = only(result["pending_animations"], name="reveal")
        assert pending["trigger"]["kind"] == "class-toggle"
        assert pending["trigger"]["confidence"] == "medium"
        assert pending["trigger"]["detail"]["class"] == "is-open"

    async def test_a_scroll_driven_animation_is_triggered_by_scroll(self):
        entry = waapi_entry(
            kind="CSSAnimation",
            animation_name="progress",
            timeline={"type": "ScrollTimeline", "axis": "y"},
        )
        result = await extract(facts(waapi=[entry]))
        trigger = only(result["animations"], name="progress")["trigger"]
        assert trigger["kind"] == "scroll"
        assert trigger["confidence"] == "high"

    async def test_a_bare_waapi_animation_is_triggered_by_js(self):
        result = await extract(facts(waapi=[waapi_entry()]))
        trigger = only(result["animations"], kind="waapi")["trigger"]
        assert trigger["kind"] == "js"
        assert trigger["confidence"] == "high"


# ===========================================================================
# End-to-end shape: one payload covering everything fixture_app/animations.html
# covers, driven through the ENGINE over the real JSON-string transport.
# ===========================================================================

# Mirrors tests/fixture_app/animations.html + animations.css. The integration
# twin (tests/test_e2e_animations.py) asserts the same facts against real Chrome;
# this one asserts the derivation hermetically, in milliseconds.
FIXTURE_PAGE = facts(
    selector="#hero",
    url="https://fake.test/animations.html",
    element={
        "tag": "div",
        "id": "hero",
        "classes": [],
        "inline_properties": [],
        "is_canvas": False,
    },
    computed=computed(
        animation_name="hero-pulse, hero-spin",
        animation_duration="2s, 3s",
        animation_delay="0.2s",
        animation_timing_function="cubic-bezier(0.34, 1.56, 0.64, 1), linear",
        animation_iteration_count="infinite, 2",
        animation_direction="alternate, normal",
        animation_fill_mode="both",
        transition_property="opacity",
        transition_duration="0.3s",
        transition_timing_function="ease-in-out",
    ),
    keyframe_rules=[
        {
            "name": "hero-pulse",
            "source_ref": "src-1",
            "keyframes": [
                {
                    "key_text": "0%, 50%",
                    "css_text": "transform: scale(1);",
                    "easing": "ease-out",
                    "composite": "",
                },
                {
                    "key_text": "100%",
                    "css_text": "transform: scale(1.08); opacity: 0.5;",
                    "easing": "",
                    "composite": "",
                },
            ],
        },
        {
            "name": "hero-spin",
            "source_ref": "src-2",
            "keyframes": [
                {
                    "key_text": "from",
                    "css_text": "transform: rotate(0deg);",
                    "easing": "",
                    "composite": "",
                },
                {
                    "key_text": "to",
                    "css_text": "transform: rotate(360deg);",
                    "easing": "",
                    "composite": "",
                },
            ],
        },
    ],
    waapi=[
        # the ::before animation
        {
            "kind": "CSSAnimation",
            "animation_name": "hero-reveal",
            "author_id": "",
            "play_state": "running",
            "target": {"relation": "self", "selector": "#hero", "pseudo": "::before"},
            "computed_timing": {
                "delay": 0,
                "duration": 1500,
                "iterations": 1,
                "direction": "normal",
                "fill": "forwards",
                "easing": "ease-out",
            },
            "keyframes": [
                {"offset": 0, "computedOffset": 0, "opacity": "0"},
                {"offset": 1, "computedOffset": 1, "opacity": "1"},
            ],
        },
        # element.animate() with composite + infinite iterations
        {
            "kind": "Animation",
            "animation_name": None,
            "author_id": "fixture-waapi-fade",
            "play_state": "running",
            "composite": "add",
            "target": {"relation": "descendant", "selector": "#waapi", "pseudo": ""},
            "computed_timing": {
                "delay": 0,
                "duration": 750,
                "iterations": "infinite",
                "direction": "normal",
                "fill": "both",
                "easing": "ease-in-out",
            },
            "keyframes": [
                {"offset": 0, "computedOffset": 0, "opacity": "0"},
                {"offset": 1, "computedOffset": 1, "opacity": "1"},
            ],
        },
        # the view()-driven animation
        {
            "kind": "CSSAnimation",
            "animation_name": "hero-progress",
            "author_id": "",
            "play_state": "running",
            "timeline": {
                "type": "ViewTimeline",
                "axis": "block",
                "subject_selector": "#scroll-bar",
            },
            "target": {
                "relation": "descendant",
                "selector": "#scroll-bar",
                "pseudo": "",
            },
            "computed_timing": {
                "delay": 0,
                "duration": "auto",
                "iterations": 1,
                "direction": "normal",
                "fill": "both",
                "easing": "linear",
            },
            "keyframes": [
                {"offset": 0, "computedOffset": 0, "transform": "scaleX(0)"},
                {"offset": 1, "computedOffset": 1, "transform": "scaleX(1)"},
            ],
            "range_start": "cover 0%",
            "range_end": "cover 100%",
        },
    ],
    matched_rules=TWO_ANIMATIONS["matched_rules"],
    sources=TWO_ANIMATIONS["sources"],
)


class TestFixturePageEndToEnd:
    """Everything the animated fixture page covers, in one payload."""

    async def test_the_whole_v2_shape_is_present(self):
        result = await extract(FIXTURE_PAGE)
        assert result["schema_version"] == 2
        assert result["has_motion"] is True
        assert set(result) == {
            "schema_version",
            "selector",
            "url",
            "captured_at_ms",
            "has_motion",
            "overview",
            "animations",
            "transitions",
            "pending_animations",
            "interactions",
            "transforms",
            "sources",
            "warnings",
            "caps",
            "options",
            # R12: both are stated ONCE here instead of per record. edit_protocol
            # replaces a how-to sentence that was repeated in every recipe (36%
            # of one measured record's edits block); detail_note explains what a
            # summary record elided and how to ask for the full one.
            "edit_protocol",
            "detail_note",
        }

    async def test_every_declared_and_live_animation_appears_exactly_once(self):
        result = await extract(FIXTURE_PAGE)
        names = [a["name"] for a in result["animations"]]
        assert names.count("hero-pulse") == 1
        assert names.count("hero-spin") == 1
        assert names.count("hero-reveal") == 1  # the ::before
        assert names.count("hero-progress") == 1  # the view() timeline
        assert "fixture-waapi-fade" in names  # element.animate()

    async def test_keyframes_arrive_as_real_parsed_json(self):
        """D1/F-846 regression net, on the full payload.

        Scoped to full-detail records: a descendant the caller did not select
        carries no keyframes at all now (R12), and says so with
        ``detail_level: "summary"`` rather than an empty list.
        """
        result = await extract(FIXTURE_PAGE)
        full = [a for a in result["animations"] if "detail_level" not in a]
        assert full, "no full-detail records to check"
        for animation in full:
            for frame in animation["keyframes"]:
                assert isinstance(frame, dict)
                assert isinstance(frame["offset"], float)
                assert isinstance(frame["properties"], dict)
                assert "type" not in frame and "value" not in frame

    async def test_keyframe_resolution_works_with_two_animations(self):
        """D2/F-847 regression net, on the full payload."""
        result = await extract(FIXTURE_PAGE)
        pulse = only(result["animations"], name="hero-pulse")
        spin = only(result["animations"], name="hero-spin")
        assert [k["offset"] for k in pulse["keyframes"]] == [0.0, 0.5, 1.0]
        assert [k["offset"] for k in spin["keyframes"]] == [0.0, 1.0]
        assert pulse["keyframes"][0]["easing"] == "ease-out"

    async def test_each_animation_carries_the_whole_actionable_block(self):
        result = await extract(FIXTURE_PAGE)
        pulse = only(result["animations"], name="hero-pulse")
        assert pulse["summary"]
        # R6: the animation-level curve is an overshoot bezier, but EVERY
        # segment of hero-pulse declares its own animation-timing-function
        # (ease-out), so the declared curve never renders. The old code quoted
        # the declared one confidently; this fixture was itself an instance of
        # the defect.
        assert pulse["semantics"]["easing_class"]["value"] == "ease-out"
        assert pulse["timeline"]["type"] == "time"
        assert pulse["timing"]["duration_ms"] == 2000
        assert pulse["derived"]["cycle_ms"] == 4000
        assert pulse["trigger"]["kind"] == "load"
        assert pulse["checkpoints"]
        assert pulse["edits"]
        assert pulse["source_refs"]

    async def test_field_order_is_summary_first_and_bulk_last(self):
        result = await extract(FIXTURE_PAGE)
        keys = list(only(result["animations"], name="hero-pulse"))
        assert keys.index("summary") < keys.index("keyframes")
        assert keys.index("timing") < keys.index("keyframes")
        assert keys.index("keyframes") < keys.index("warnings")

    async def test_the_view_timeline_is_typed_and_flagged(self):
        result = await extract(FIXTURE_PAGE)
        progress = only(result["animations"], name="hero-progress")
        assert progress["timeline"]["type"] == "view"
        assert progress["timeline"]["range_start"] == "cover 0%"
        assert "duration_ms" not in progress["timing"]
        assert progress["derived"] == {}  # nothing decidable, nothing invented
        assert only(result["interactions"], code="scroll_timeline_duration_noop")

    async def test_the_payload_is_json_serializable(self):
        """It has to survive the MCP transport on the way out, too."""
        result = await extract(FIXTURE_PAGE)
        assert json.loads(json.dumps(result))["schema_version"] == 2


# ===========================================================================
# The thin adapters that READ the aspect payload
# ===========================================================================


class TestAdaptersReadTheV2Shape:
    """Every consumer of the animations aspect, pinned hermetically.

    These exist because the v2 break was first caught by the (slow) integration
    lane: three adapter summaries read v1 keys, one of them raising
    ``'list' object has no attribute 'get'`` on a real page. An adapter that
    reads a shape the engine does not produce must red HERE, in milliseconds.
    """

    async def test_to_file_summary_counts_records(self, tmp_path, monkeypatch):
        from stealth_chrome_devtools_mcp.embedded import (
            file_based_element_cloner as _fbc,
        )

        monkeypatch.setattr(_fbc.file_based_element_cloner, "output_dir", tmp_path)
        result = (
            await _fbc.file_based_element_cloner.extract_element_animations_to_file(
                anim_tab(FIXTURE_PAGE), selector="#hero"
            )
        )
        assert set(result) == {"file_path", "extraction_type", "summary"}
        summary = result["summary"]
        assert summary["has_motion"] is True
        assert summary["animations_count"] >= 2
        assert summary["keyframes_count"] > 0
        # The payload really landed on disk as schema v2.
        written = json.loads(Path(result["file_path"]).read_text(encoding="utf-8"))
        assert written["schema_version"] == 2

    async def test_complete_clone_summary_counts_records(self, tmp_path, monkeypatch):
        """The site that raised on a real page: ``len(d["animations"])`` over a
        dict counted the aspect's 15 keys, and the nested read hit a list."""
        from stealth_chrome_devtools_mcp.embedded import (
            file_based_element_cloner as _fbc,
        )

        monkeypatch.setattr(_fbc.file_based_element_cloner, "output_dir", tmp_path)
        tab = FakeTab(
            evaluate_result={},
            evaluate_map=animation_evaluate_map(FIXTURE_PAGE),
        )
        result = await _fbc.file_based_element_cloner.clone_element_complete_to_file(
            tab, selector="#hero"
        )
        assert "error" not in result
        animations = result["summary"]["components"]["animations"]
        assert animations["has_animations"] is True
        assert animations["animations_count"] == len(
            FIXTURE_PAGE["keyframe_rules"]
        ) + len(FIXTURE_PAGE["waapi"])
        assert animations["keyframes_count"] > 0

    async def test_complete_to_file_summary_counts_records(self, tmp_path, monkeypatch):
        """The other summary that counted a dict's keys instead of records."""
        from stealth_chrome_devtools_mcp.embedded import (
            file_based_element_cloner as _fbc,
        )

        monkeypatch.setattr(_fbc.file_based_element_cloner, "output_dir", tmp_path)
        tab = FakeTab(
            evaluate_result={},
            evaluate_map=animation_evaluate_map(FIXTURE_PAGE),
        )
        result = await _fbc.file_based_element_cloner.extract_complete_element_to_file(
            tab, selector="#hero"
        )
        assert result["summary"]["animations_count"] == len(
            FIXTURE_PAGE["keyframe_rules"]
        ) + len(FIXTURE_PAGE["waapi"])

    def test_progressive_expand_passes_the_whole_v2_object_through(self):
        from stealth_chrome_devtools_mcp.embedded import (
            progressive_element_cloner as _pec,
        )
        from stealth_chrome_devtools_mcp.embedded.animation_analysis import analyze

        payload = analyze(FIXTURE_PAGE)
        cloner = _pec.progressive_element_cloner
        cloner._save_store(
            {
                "elem_anim_v2": {
                    "full_data": {"animations": payload, "assets": {"fonts": {}}},
                    "url": "https://fake.test/animations.html",
                    "selector": "#hero",
                    "timestamp": 1.0,
                    "include_children": False,
                }
            }
        )
        try:
            expanded = cloner.expand_animations("elem_anim_v2")
            assert expanded["animations"]["schema_version"] == 2
            assert expanded["animations"]["animations"][0]["name"] == "hero-pulse"
        finally:
            cloner._save_store({})


# ===========================================================================
# Derivation unit tests — the exotic inputs where a derived field could LIE
# ===========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [("2s", 2000.0), ("200ms", 200.0), ("0.3s", 300.0), (".5s", 500.0), ("0s", 0.0)],
)
def test_duration_tokens_round_to_ms(raw, expected):
    from stealth_chrome_devtools_mcp.embedded.animation_analysis import duration_ms

    assert duration_ms(raw) == expected


@pytest.mark.parametrize("raw", ["auto", "", "fast", None, "2x"])
def test_non_time_tokens_are_none_never_zero(raw):
    from stealth_chrome_devtools_mcp.embedded.animation_analysis import duration_ms

    assert duration_ms(raw) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("a, b", ["a", "b"]),
        (
            "cubic-bezier(0.1, 0.2, 0.3, 0.4), linear",
            ["cubic-bezier(0.1, 0.2, 0.3, 0.4)", "linear"],
        ),
        ("steps(4, end)", ["steps(4, end)"]),
        ("", []),
    ],
)
def test_top_level_comma_split(value, expected):
    from stealth_chrome_devtools_mcp.embedded.animation_analysis import split_css_list

    assert split_css_list(value) == expected


@pytest.mark.parametrize(
    "direction,iterations,duration,expected_cycle",
    [
        ("normal", 1, 1000, 1000),
        ("alternate", 1, 1000, 2000),
        ("alternate-reverse", 1, 1000, 2000),
        ("reverse", 1, 1000, 1000),
    ],
)
def test_cycle_arithmetic_over_directions(
    direction, iterations, duration, expected_cycle
):
    from stealth_chrome_devtools_mcp.embedded.animation_analysis import derive_timing

    derived = derive_timing(
        {
            "duration_ms": duration,
            "delay_ms": 0,
            "iterations": iterations,
            "direction": direction,
        }
    )
    assert derived["cycle_ms"] == expected_cycle


def test_derived_timing_omits_what_it_cannot_decide():
    """A scroll/view animation has no duration in ms; inventing one is the lie
    class M11 forbids."""
    from stealth_chrome_devtools_mcp.embedded.animation_analysis import derive_timing

    derived = derive_timing({"duration_raw": "auto", "iterations": 1})
    assert "iteration_ms" not in derived
    assert "cycle_ms" not in derived
    assert "total_ms" not in derived


def test_facts_payload_round_trips_as_a_string():
    """Sanity: the harness really hands over a string, like a real tab does."""
    encoded = animation_evaluate_map(TWO_ANIMATIONS)[ANIMATION_JS_MARKER]
    assert isinstance(encoded, str)
    assert json.loads(encoded)["computed"]["animation_name"] == "pulse, spin"
