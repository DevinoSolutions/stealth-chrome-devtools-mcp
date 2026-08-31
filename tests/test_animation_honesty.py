"""Derived fields that were confidently wrong (F-851: review findings R3-R11).

Each class here pins one way the schema stated something it had not established.
They share a shape: the payload was not silent (which a weak model reads as a
reason to be careful) but assertive, and assertive-and-wrong is the failure mode
the whole animations feature exists to avoid.

The fixtures are deliberately NOT built from CSSOM-serialized text, and the
inputs are the exotic ones — a reversed animation, a negative delay, a
descendant-scoped selector, a ``no-preference`` media query — because every one
of these defects survived a fixture that only exercised the common case.
"""

import inspect

import pytest

from fakes import FakeTab, animation_evaluate_map
from stealth_chrome_devtools_mcp.embedded import animation_analysis
from stealth_chrome_devtools_mcp.embedded import cdp_element_cloner as _cdc
from test_animation_schema_v2 import computed, extract, facts


def waapi_entry(name, target_selector, keyframes=2, **over):
    """One live-animation fact record, small enough to multiply by the hundred."""
    entry = {
        "kind": "Animation",
        "animation_name": name,
        "author_id": "",
        "play_state": "running",
        "target": {"relation": "descendant", "selector": target_selector, "pseudo": ""},
        "computed_timing": {
            "delay": 0,
            "duration": 1000,
            "iterations": 1,
            "direction": "normal",
            "fill": "none",
            "easing": "linear",
        },
        "keyframes": [
            {"computedOffset": index / max(keyframes - 1, 1), "opacity": str(index)}
            for index in range(keyframes)
        ],
    }
    entry.update(over)
    return entry


# ===========================================================================
# R3 — the caps the owner's Q2 ruling requires to be SURFACED when hit
# ===========================================================================


class TestCapsAreEnforcedAndReported:
    async def test_a_huge_subtree_is_capped_and_says_so(self):
        """``include_subtree`` defaults ON, so one call on a page section can
        pull in hundreds of live animations. ``build_waapi`` iterated all of
        them with no cap at all, while ``caps.truncated`` reported ``false``."""
        payload = await extract(
            facts(
                waapi=[
                    waapi_entry(f"anim-{index}", f"#row-{index}")
                    for index in range(animation_analysis.ANIMATION_CAP + 25)
                ]
            )
        )
        assert len(payload["animations"]) <= animation_analysis.ANIMATION_CAP
        assert payload["caps"]["truncated"]["animations"] is True
        codes = [w["code"] for w in payload["warnings"]]
        assert "animation_cap_reached" in codes

    async def test_keyframes_are_never_cut_silently(self):
        """``_waapi_keyframes`` truncated with ``records[:KEYFRAME_CAP]`` and
        emitted nothing, while ``build_waapi`` hardcoded ``warnings: []`` — so
        240 of 300 keyframes vanished with the payload claiming completeness."""
        payload = await extract(
            facts(
                waapi=[
                    waapi_entry(
                        "long", "#deep", keyframes=animation_analysis.KEYFRAME_CAP + 40
                    )
                ]
            )
        )
        animation = payload["animations"][0]
        assert len(animation["keyframes"]) == animation_analysis.KEYFRAME_CAP
        assert "keyframe_cap_reached" in [w["code"] for w in animation["warnings"]]
        assert payload["caps"]["truncated"]["keyframes"] is True

    async def test_a_live_animation_within_the_caps_reports_no_truncation(self):
        payload = await extract(facts(waapi=[waapi_entry("small", "#one")]))
        assert payload["caps"]["truncated"] == {
            "animations": False,
            "keyframes": False,
        }


# ===========================================================================
# R5 — checkpoints bound offsets to wall-clock time ignoring the direction
# ===========================================================================


def reversible(direction, **over):
    return facts(
        computed=computed(
            animation_name="slide",
            animation_duration="1s",
            animation_direction=direction,
            **over,
        ),
        keyframe_rules=[
            {
                "name": "slide",
                "source_ref": "src-1",
                "keyframes": [
                    {
                        "key_text": "0%",
                        "css_text": "opacity: 0;",
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
    )


def checkpoint(payload, offset):
    found = [
        c for c in payload["animations"][0]["checkpoints"] if c["offset"] == offset
    ]
    assert len(found) == 1, f"no checkpoint at {offset}"
    return found[0]


class TestCheckpointsRespectDirection:
    async def test_forward_playback_is_unchanged(self):
        payload = await extract(reversible("normal"))
        assert checkpoint(payload, 0.0)["time_ms"] == 0.0
        assert checkpoint(payload, 1.0)["time_ms"] == 1000.0

    async def test_a_reversed_animation_does_not_claim_zero_renders_at_zero(self):
        """R5: with ``animation-direction: reverse`` the element renders the
        100% keyframe at t=0. The old code emitted
        ``{offset: 0.0, time_ms: 0.0, values: {opacity: "0"}}`` — measured-looking
        and simply wrong, with no confidence marker anywhere near it."""
        payload = await extract(reversible("reverse"))
        assert checkpoint(payload, 0.0)["time_ms"] == 1000.0
        assert checkpoint(payload, 1.0)["time_ms"] == 0.0

    async def test_alternate_reverse_starts_reversed_too(self):
        payload = await extract(reversible("alternate-reverse"))
        assert checkpoint(payload, 0.0)["time_ms"] == 1000.0

    async def test_alternate_plays_its_first_iteration_forwards(self):
        payload = await extract(reversible("alternate"))
        assert checkpoint(payload, 0.0)["time_ms"] == 0.0

    async def test_a_non_zero_iteration_start_omits_the_time_and_says_why(self):
        """A first iteration that begins mid-animation reaches its offsets in an
        order we are not going to reconstruct — so no ``time_ms`` at all."""
        payload = await extract(
            facts(
                waapi=[
                    waapi_entry(
                        "shifted",
                        "#self",
                        computed_timing={
                            "delay": 0,
                            "duration": 1000,
                            "iterations": 1,
                            "iteration_start": 0.35,
                            "direction": "normal",
                            "fill": "none",
                            "easing": "linear",
                        },
                    )
                ]
            )
        )
        animation = payload["animations"][0]
        for point in animation["checkpoints"]:
            assert "time_ms" not in point, point
        assert "checkpoint_time_not_decidable" in [
            w["code"] for w in animation["warnings"]
        ]


# ===========================================================================
# R7 — a negative delay is not a wait
# ===========================================================================

NEGATIVE_DELAY = facts(
    computed=computed(
        animation_name="jump",
        animation_duration="2s",
        animation_delay="-0.5s",
    )
)


class TestNegativeDelay:
    async def test_the_active_window_starts_at_zero_not_in_the_past(self):
        """``active_start_ms: -500`` said the animation begins half a second
        before the page did. It begins immediately, already 500ms in."""
        derived = (await extract(NEGATIVE_DELAY))["animations"][0]["derived"]
        assert derived["active_start_ms"] == 0.0
        assert derived["starts_at_progress_ms"] == 500.0

    async def test_the_summary_does_not_narrate_it_as_a_wait(self):
        """``if as_number(timing.get("delay_ms")):`` is truthy for negatives, so
        the prose read "...after a -0.5s delay"."""
        summary = (await extract(NEGATIVE_DELAY))["animations"][0]["summary"]
        assert "-0.5s delay" not in summary, summary
        assert "500" in summary and "into the animation" in summary

    async def test_a_positive_delay_still_reads_as_a_wait(self):
        payload = await extract(
            facts(
                computed=computed(
                    animation_name="jump",
                    animation_duration="2s",
                    animation_delay="0.5s",
                )
            )
        )
        assert "after a 0.5s delay" in payload["animations"][0]["summary"]
        assert payload["animations"][0]["derived"]["active_start_ms"] == 500.0
        assert "starts_at_progress_ms" not in payload["animations"][0]["derived"]


# ===========================================================================
# R8 — pending_animations told the model to add an ANCESTOR's class
# ===========================================================================


def pending_facts(selector, classes):
    return facts(
        element={
            "tag": "div",
            "id": "",
            "classes": classes,
            "inline_properties": [],
            "is_canvas": False,
        },
        candidate_rules=[
            {
                "source_ref": "src-0",
                "selector_text": selector,
                "css_text": f"{selector} {{ animation: reveal 1s ease; }}",
                "declares": {"animation-name": "reveal"},
                "important": [],
                "matches_now": False,
                "matches_base": False,
                "at_rule_context": [],
            }
        ],
    )


class TestPendingAnimationsAdviseTheRightThing:
    async def test_a_class_on_the_elements_own_compound_is_actionable(self):
        payload = await extract(pending_facts(".card.is-open", ["card"]))
        pending = payload["pending_animations"][0]
        assert pending["trigger"]["kind"] == "class-toggle"
        assert pending["trigger"]["detail"]["class"] == "is-open"
        assert "is-open" in pending["summary"]

    async def test_an_ancestors_class_is_not_offered_as_one_to_add(self):
        """R8: ``.gallery .card`` on a ``.card`` outside any ``.gallery`` used to
        say "add the 'gallery' class to run it". Adding it to the element cannot
        make that rule match."""
        payload = await extract(pending_facts(".gallery .card", ["card"]))
        pending = payload["pending_animations"][0]
        assert "add the 'gallery' class" not in pending["summary"], pending["summary"]
        assert pending["trigger"]["kind"] != "class-toggle"
        assert "gallery" in pending["summary"]

    async def test_a_sibling_combinator_is_not_a_class_to_add_either(self):
        payload = await extract(pending_facts(".open ~ .card", ["card"]))
        pending = payload["pending_animations"][0]
        assert pending["trigger"]["kind"] != "class-toggle"


# ===========================================================================
# R9 — reduced_motion_override fired on `no-preference`
# ===========================================================================


def media_facts(condition):
    return facts(
        computed=computed(animation_name="fade", animation_duration="1s"),
        matched_rules=[
            {
                "source_ref": "src-0",
                "selector_text": ".card",
                "css_text": ".card { animation: fade 1s; }",
                "declares": {"animation": "fade 1s"},
                "important": [],
                "matches_now": True,
                "matches_base": True,
                "at_rule_context": [condition],
            }
        ],
    )


def codes(payload):
    return [i["code"] for i in payload["interactions"]]


class TestReducedMotionReadsTheFeatureValue:
    async def test_reduce_fires_the_warning(self):
        payload = await extract(media_facts("@media (prefers-reduced-motion: reduce)"))
        assert "reduced_motion_override" in codes(payload)

    async def test_a_bare_feature_query_fires_it_too(self):
        payload = await extract(media_facts("@media (prefers-reduced-motion)"))
        assert "reduced_motion_override" in codes(payload)

    async def test_no_preference_does_not_fire_it(self):
        """R9: the check was a substring match on the feature NAME, so a block
        that applies only when motion IS allowed produced a warning whose remedy
        is backwards for that input."""
        payload = await extract(
            media_facts("@media (prefers-reduced-motion: no-preference)")
        )
        assert "reduced_motion_override" not in codes(payload)


# ===========================================================================
# R10 — staggers that are not staggers, and records with no verdict
# ===========================================================================


def sibling(name, delay_ms, index):
    entry = waapi_entry(name, f"#item-{index}")
    entry["computed_timing"]["delay"] = delay_ms
    return entry


def stagger_of(*delays):
    return facts(waapi=[sibling("slide", d, i) for i, d in enumerate(delays)])


def group_of(payload):
    return payload["animations"][0].get("derived", {}).get("stagger_group")


class TestStaggerGroups:
    async def test_a_real_stagger_is_reported(self):
        payload = await extract(stagger_of(0, 100, 200))
        group = group_of(payload)
        assert group["uniform"] is True
        assert group["delta_ms"] == 100.0
        assert group["delays_ms"] == [0.0, 100.0, 200.0]

    async def test_identical_delays_are_not_a_stagger(self):
        """R10: three siblings starting together is a chorus, not a stagger, and
        ``{uniform: true, delta_ms: 0.0}`` invites a model to "fix" the spacing
        of something that was never staggered."""
        assert group_of(await extract(stagger_of(200, 200, 200))) is None

    async def test_an_unreadable_delay_does_not_become_a_uniform_stagger(self):
        """The delay was coerced to 0.0 before differencing, so a member whose
        delay we could not read produced a confident, invented spacing."""
        payload = await extract(
            facts(
                waapi=[
                    sibling("slide", 0, 0),
                    sibling("slide", 100, 1),
                    waapi_entry(
                        "slide",
                        "#item-2",
                        computed_timing={
                            "duration": 1000,
                            "iterations": 1,
                            "direction": "normal",
                            "fill": "none",
                            "easing": "linear",
                        },
                    ),
                ]
            )
        )
        assert group_of(payload) is None


class TestEveryRecordDeclaresWhetherItIsEditable:
    async def test_a_transition_says_so_when_it_offers_no_recipes(self):
        """R10: absence reads as "editable". ``editable: false`` was set only
        for ``kind == "waapi"``, so every transition came back with no edits, no
        verdict and no reason."""
        payload = await extract(
            facts(
                computed=computed(
                    transition_property="opacity",
                    transition_duration="0.3s",
                )
            )
        )
        transition = payload["transitions"][0]
        assert "editable" in transition
        if transition["editable"] is False:
            assert transition["not_editable_reason"]

    async def test_a_css_animation_with_an_unreadable_rule_says_so(self):
        payload = await extract(
            facts(computed=computed(animation_name="ghost", animation_duration="1s"))
        )
        animation = payload["animations"][0]
        assert animation["editable"] is False
        assert animation["not_editable_reason"]


# ===========================================================================
# R11 — a retired option must degrade, not detonate the whole clone
# ===========================================================================


CANNED_JS = {"html": {"outerHTML": "<div id='demo'></div>"}, "cssRules": []}

MINIMAL_FACTS = facts(selector="#demo", computed=computed())


def complete_tab():
    return FakeTab(
        evaluate_result=dict(CANNED_JS),
        evaluate_map=animation_evaluate_map(MINIMAL_FACTS),
        select_result=None,
    )


class TestRetiredOptionsDegrade:
    async def test_a_retired_option_does_not_destroy_five_other_aspects(self):
        """R11: ``analyze_keyframes`` was a real v1 option. Passing it now bound
        against the new signature, raised ``TypeError`` at the call site — before
        any coroutine existed, so ``gather``'s isolation never saw it — and the
        outer handler turned ONE stale string into an error payload for the
        entire clone. The repo's "0 external users" premise is false; someone
        else's outdated call must not lose structure, styles, events, assets and
        related_files."""
        result = await _cdc.cdp_element_cloner.extract_complete_element(
            complete_tab(),
            selector="#demo",
            extraction_options={"animations": {"analyze_keyframes": True}},
        )
        assert "error" not in result
        assert {
            "styles",
            "structure",
            "events",
            "animations",
            "assets",
            "related_files",
        } <= set(result)
        assert result["animations"].get("schema_version") == 2

    async def test_the_retired_option_is_named_in_the_aspects_warnings(self):
        result = await _cdc.cdp_element_cloner.extract_complete_element(
            complete_tab(),
            selector="#demo",
            extraction_options={"animations": {"analyze_keyframes": True}},
        )
        warnings = result["animations"]["warnings"]
        retired = [w for w in warnings if w["code"] == "retired_option_ignored"]
        assert len(retired) == 1
        assert retired[0]["detail"]["option"] == "analyze_keyframes"
        # Naming the replacement is the whole value of the warning.
        assert retired[0]["message"]

    async def test_an_unknown_option_nobody_ever_shipped_also_degrades(self):
        result = await _cdc.cdp_element_cloner.extract_complete_element(
            complete_tab(),
            selector="#demo",
            extraction_options={"animations": {"totally_made_up": 7}},
        )
        assert "error" not in result
        assert "retired_option_ignored" in [
            w["code"] for w in result["animations"]["warnings"]
        ]

    async def test_a_live_option_is_still_honoured(self):
        """The tolerance is specific: it must not swallow options that work."""
        result = await _cdc.cdp_element_cloner.extract_complete_element(
            complete_tab(),
            selector="#demo",
            extraction_options={"animations": {"include_waapi": False}},
        )
        assert result["animations"]["options"]["include_waapi"] is False

    async def test_an_aspect_that_raises_does_not_zero_out_its_siblings(
        self, monkeypatch
    ):
        async def boom(*args, **kwargs):
            raise RuntimeError("aspect exploded")

        monkeypatch.setattr(_cdc.cdp_element_cloner, "extract_element_events", boom)
        result = await _cdc.cdp_element_cloner.extract_complete_element(
            complete_tab(), selector="#demo"
        )
        assert "error" not in result
        assert "aspect exploded" in result["events"]["error"]
        assert result["animations"].get("schema_version") == 2

    def test_the_retired_option_table_names_options_that_really_are_gone(self):
        """A stale entry would advertise a removal that never happened."""
        from stealth_chrome_devtools_mcp.embedded import aspect_options

        live = set(
            inspect.signature(
                _cdc.cdp_element_cloner.extract_element_animations
            ).parameters
        )
        for option in aspect_options.RETIRED["animations"]:
            assert option not in live, f"{option} is still a real parameter"


@pytest.mark.parametrize(
    "aspect",
    ["styles", "structure", "events", "animations", "assets", "related_files"],
)
async def test_every_aspect_tolerates_a_bogus_option(aspect):
    result = await _cdc.cdp_element_cloner.extract_complete_element(
        complete_tab(),
        selector="#demo",
        extraction_options={aspect: {"no_such_option_anywhere": True}},
    )
    assert "error" not in result
    assert aspect in result
