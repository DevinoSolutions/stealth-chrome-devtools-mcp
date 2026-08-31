"""The confidence invariant for the animations schema (F-850).

Ten separately-reported defects shared ONE root cause: confidence was stamped
after the fact. ``_enrich`` wrote ``easing_confidence: "high"`` onto whatever a
heuristic returned — including a fall-through branch that had decided nothing —
``_recipe`` defaulted to ``"high"``, and ``build_waapi`` hardcoded
``warnings: []``. The spec's M11 rule says every derived field carries a
confidence or is omitted; the code said it and then routed around it.

The fix is structural: a derivation returns its value AND its confidence welded
together (``animation_facts.Derived``), so a branch that decided nothing cannot
inherit a caller's optimism. These tests enforce that end to end, which is what
makes them worth more than any single defect fix:

* the payload walk pins the invariant on the OUTPUT, so a future field that
  forgets its confidence reds here even though nobody wrote a test for it;
* the AST scan pins it on the INPUT side, so a confidence literal cannot be
  reintroduced at a plumbing site that never decided anything.
"""

import ast
from pathlib import Path

import pytest

from stealth_chrome_devtools_mcp.embedded import animation_analysis, animation_facts
from test_animation_schema_v2 import TWO_ANIMATIONS, computed, extract, facts

PACKAGE = Path(animation_facts.__file__).parent
MODULES = (
    "animation_facts.py",
    "animation_advice.py",
    "animation_analysis.py",
)


# ---------------------------------------------------------------------------
# The payload walk
# ---------------------------------------------------------------------------


def walk(node, path="$"):
    """Every (path, dict) pair in the payload, depth first."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")


def claims(payload):
    """Every (path, field, body) for a registered claim field in the payload."""
    for path, node in walk(payload):
        for field in animation_analysis.CLAIM_FIELDS:
            if field in node:
                yield f"{path}.{field}", field, node[field]


class TestTheInvariantHoldsOnThePayload:
    async def test_every_claim_field_carries_the_confidence_it_was_derived_with(self):
        payload = await extract(TWO_ANIMATIONS)
        found = list(claims(payload))
        assert found, "no claim fields in the payload at all — registry drifted?"
        for path, _field, body in found:
            assert isinstance(body, dict), f"{path} is not a claim object: {body!r}"
            assert "value" in body, f"{path} has no value"
            assert body.get("confidence") in animation_facts.CONFIDENCE_LEVELS, (
                f"{path} carries confidence={body.get('confidence')!r}"
            )

    async def test_every_judgement_block_carries_a_confidence(self):
        payload = await extract(TWO_ANIMATIONS)
        seen = 0
        for path, node in walk(payload):
            for field in animation_analysis.JUDGEMENT_BLOCKS:
                block = node.get(field)
                items = block if isinstance(block, list) else [block]
                for index, item in enumerate(items):
                    if not isinstance(item, dict):
                        continue
                    seen += 1
                    assert (
                        item.get("confidence") in animation_facts.CONFIDENCE_LEVELS
                    ), f"{path}.{field}[{index}] has no confidence: {item!r}"
        assert seen, "no judgement blocks in the payload at all"

    async def test_no_confidence_anywhere_is_outside_the_closed_set(self):
        """A typo'd or invented level is as bad as a missing one: a weak model
        reads 'certain' or 'probably' as a stronger claim than 'high'."""
        payload = await extract(TWO_ANIMATIONS)
        for path, node in walk(payload):
            if "confidence" in node:
                assert node["confidence"] in animation_facts.CONFIDENCE_LEVELS, (
                    f"{path} carries confidence={node['confidence']!r}"
                )

    async def test_a_claim_is_omitted_rather_than_hedged_when_undecidable(self):
        """M11 / the owner's ruling: absence tells a weak model to be careful,
        while a hedged guess is quoted verbatim anyway."""
        payload = await extract(
            facts(
                computed=computed(
                    animation_name="mystery",
                    animation_duration="1s",
                    # Neither ease-in nor ease-out nor overshoot: fast at BOTH
                    # ends. There is no honest class for it.
                    animation_timing_function="cubic-bezier(0.1, 0.9, 0.9, 0.1)",
                )
            )
        )
        semantics = payload["animations"][0].get("semantics", {})
        assert "easing_class" not in semantics, (
            f"an unclassifiable curve was still classified: {semantics!r}"
        )


# ---------------------------------------------------------------------------
# The derivations themselves
# ---------------------------------------------------------------------------

EXOTIC_EASINGS = [
    "",
    "   ",
    "linear",
    "ease",
    "ease-in-out",
    "step-start",
    "steps(4, end)",
    "linear(0, 0.25 75%, 1)",
    "cubic-bezier(0.25, 0.25, 0.75, 0.75)",
    "cubic-bezier(0.34, 1.56, 0.64, 1)",
    "cubic-bezier(0.1, 0.9, 0.9, 0.1)",
    "cubic-bezier(0.42, 0, 1, 1)",
    "cubic-bezier(nonsense)",
    "cubic-bezier(1, 2)",
    "spring(1 100 10 0)",
    "var(--my-easing)",
    "CUBIC-BEZIER(0.4, 0, 0.2, 1)",
]


class TestDerivationsCarryTheirOwnConfidence:
    @pytest.mark.parametrize("easing", EXOTIC_EASINGS)
    def test_easing_class_never_returns_a_value_without_a_confidence(self, easing):
        result = animation_facts.easing_class(easing)
        assert isinstance(result, animation_facts.Derived)
        if result.value is None:
            assert result.confidence == "", (
                f"{easing!r} decided nothing but claims {result.confidence!r}"
            )
        else:
            assert result.confidence in animation_facts.CONFIDENCE_LEVELS

    def test_a_diagonal_bezier_really_is_linear_and_says_so_confidently(self):
        result = animation_facts.easing_class("cubic-bezier(0.25, 0.25, 0.75, 0.75)")
        assert result.value == "linear"
        assert result.confidence == "high"

    def test_a_curve_that_is_fast_at_both_ends_is_not_called_linear(self):
        """R4: the old ``_bezier_class`` tested only ``y1<x1`` / ``y2>x2``, so
        this curve fell through to ``return "linear"`` and was then stamped
        ``easing_confidence: "high"`` — a lie at the highest confidence."""
        result = animation_facts.easing_class("cubic-bezier(0.1, 0.9, 0.9, 0.1)")
        assert result.value is None
        assert result.confidence == ""

    @pytest.mark.parametrize(
        "frames",
        [
            [],
            [{"properties": {}}],
            [{"properties": {"opacity": "0"}}],
            [
                {
                    "properties": {
                        "transform": "matrix3d(1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1)"
                    }
                }
            ],
            [{"properties": {"transform": "translateX(10px) scale(1.2)"}}],
            [{"properties": {"-webkit-mystery": "7"}}],
        ],
    )
    def test_motion_kind_never_returns_a_value_without_a_confidence(self, frames):
        result = animation_facts.motion_kind(frames)
        assert isinstance(result, animation_facts.Derived)
        if result.value is None:
            assert result.confidence == ""
        else:
            assert result.confidence in animation_facts.CONFIDENCE_LEVELS

    def test_a_compound_transform_reports_every_family_it_touches(self):
        """R10: first-match-wins substring scanning classified
        ``translateX(10px) scale(1.2)`` as "scale" alone, and a compound
        transform could therefore never come out as "mixed"."""
        result = animation_facts.motion_kind(
            [{"properties": {"transform": "translateX(10px) scale(1.2)"}}]
        )
        assert result.value == "mixed"

    def test_a_matrix_transform_is_not_asserted_to_be_a_translate(self):
        """R10: ``matrix3d(...)`` contains "matrix", which the old table mapped
        to "translate". A matrix encodes translate, scale, rotate and skew at
        once; naming one of them is a guess."""
        result = animation_facts.motion_kind(
            [{"properties": {"transform": "matrix3d(1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1)"}}]
        )
        assert result.value is None, f"a matrix was decoded as {result.value!r}"


# ---------------------------------------------------------------------------
# The AST scan — no confidence literal outside a deciding branch
# ---------------------------------------------------------------------------

# Functions that genuinely DECIDE a confidence: each is the branch that reached
# the conclusion, so the literal belongs with it. Everything else is plumbing,
# and plumbing that writes a confidence is the defect this whole slice exists to
# make impossible. Adding a name here is a deliberate act, reviewable on its own.
DECIDERS = frozenset(
    {
        "_bezier_class",
        "easing_class",
        "motion_kind",
        "_conflict",
        "trigger_from_rules",
        "build_pending",
        "_attach_trigger",
        "locate",
        "stagger_group",
    }
)

CONFIDENCE_KEYS = {"confidence", "easing_confidence", "motion_confidence"}


def confidence_literals(path):
    """(function, line) for every constant written to a confidence-ish key."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                owner[id(child)] = node.name
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            pairs = zip(node.keys, node.values, strict=True)
        elif isinstance(node, ast.Assign) and isinstance(
            node.targets[0], ast.Subscript
        ):
            sub = node.targets[0]
            pairs = [(sub.slice, node.value)]
        else:
            continue
        for key, value in pairs:
            if not isinstance(key, ast.Constant) or key.value not in CONFIDENCE_KEYS:
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                found.append((owner.get(id(node), "<module>"), node.lineno))
    return found


class TestNoConfidenceIsWrittenByPlumbing:
    @pytest.mark.parametrize("module", MODULES)
    def test_only_a_deciding_branch_writes_a_confidence_literal(self, module):
        offenders = [
            (function, line)
            for function, line in confidence_literals(PACKAGE / module)
            if function not in DECIDERS
        ]
        assert not offenders, (
            f"{module} writes a confidence it did not derive at "
            + ", ".join(f"{function}():{line}" for function, line in offenders)
        )

    def test_the_decider_allowlist_names_real_functions(self):
        """A stale allowlist entry would silently widen the rule."""
        defined = set()
        for module in MODULES:
            tree = ast.parse((PACKAGE / module).read_text(encoding="utf-8"))
            defined |= {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
        assert defined >= DECIDERS, f"allowlist names nothing: {DECIDERS - defined}"


# ---------------------------------------------------------------------------
# R6 — the animation-level easing must not speak for the keyframes
# ---------------------------------------------------------------------------


PER_KEYFRAME_EASING = facts(
    computed=computed(
        animation_name="segmented",
        animation_duration="2s",
        # The animation-level function is linear...
        animation_timing_function="linear",
    ),
    keyframe_rules=[
        {
            "name": "segmented",
            "source_ref": "src-1",
            "keyframes": [
                # ...but every segment overrides it, so "linear" describes
                # nothing that actually renders.
                {
                    "key_text": "0%",
                    "css_text": "opacity: 0;",
                    "easing": "ease-in",
                    "composite": "",
                },
                {
                    "key_text": "50%",
                    "css_text": "opacity: 1;",
                    "easing": "cubic-bezier(0.34, 1.56, 0.64, 1)",
                    "composite": "",
                },
                {
                    "key_text": "100%",
                    "css_text": "opacity: 0;",
                    "easing": "",
                    "composite": "",
                },
            ],
        }
    ],
)


class TestPerKeyframeEasing:
    async def test_the_payload_does_not_contradict_its_own_checkpoints(self):
        """R6: ``semantics.easing_class`` read only ``timing.easing`` while
        ``checkpoints[].between.segment_easing`` correctly reported per-segment
        curves. The confident field is the one a model quotes."""
        payload = await extract(PER_KEYFRAME_EASING)
        animation = payload["animations"][0]
        klass = animation["semantics"]["easing_class"]
        assert klass["value"] == "per-keyframe", (
            f"segments disagree but the payload says {klass!r}"
        )
        assert "reason" in klass

    async def test_a_single_agreed_easing_is_still_reported_plainly(self):
        payload = await extract(TWO_ANIMATIONS)
        spin = next(a for a in payload["animations"] if a["name"] == "spin")
        assert spin["semantics"]["easing_class"]["value"] == "linear"

    async def test_the_summary_does_not_quote_an_overridden_easing(self):
        payload = await extract(PER_KEYFRAME_EASING)
        summary = payload["animations"][0]["summary"]
        assert "linear easing" not in summary, summary
        assert "per-keyframe" in summary
