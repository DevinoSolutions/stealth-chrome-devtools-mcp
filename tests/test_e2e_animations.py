"""E2E: the animations aspect against REAL Chrome and a real animated page.

The hermetic tier (``test_animation_schema_v2.py``) drives the Python derivation
from a captured fact payload, which proves every conclusion but proves nothing
about the browser half. This file is the other half: it runs the actual
``js/extract_animations.js`` collector in a real Chrome against
``fixture_app/animations.html`` and asserts the schema-v2 payload that comes back
over the real transport.

The page is built to cover, on one element or its neighbours: 2+ CSS animations,
a comma keyframe selector, a per-keyframe easing, a ``::before`` animation, an
``element.animate()`` with ``composite`` and infinite iterations, a ``view()``
timeline, a transition and a uniform stagger.
"""

from __future__ import annotations

import json

import pytest

from e2e_helpers import (
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    sandbox_kwargs,
    warmup_once,
)

pytestmark = integration_pytestmark()


@pytest.fixture(autouse=True)
async def _warm():
    await warmup_once()


async def _extract(iid, base, selector, **kwargs):
    extract = get_fn("extract_element_animations")
    await navigate_and_settle(iid, f"{base}/animations.html")
    return await extract(instance_id=iid, selector=selector, **kwargs)


def _by_name(payload, name):
    found = [a for a in payload["animations"] if a["name"] == name]
    assert len(found) == 1, (
        f"expected one '{name}', got {[a['name'] for a in payload['animations']]}"
    )
    return found[0]


async def test_schema_v2_end_to_end_over_the_real_transport(fixture_app_server):
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        payload = await _extract(iid, base, "#hero")

        # --- the payload really arrived, parsed (D1/F-846 over real CDP) ---
        assert payload["schema_version"] == 2
        assert payload["has_motion"] is True
        assert "error" not in payload
        # A CDP deep-serialization blob would carry these instead of real keys.
        assert "type" not in payload and "value" not in payload

        # --- D2/F-847: BOTH animations resolve their keyframes ---
        assert {"hero-pulse", "hero-spin"} <= {a["name"] for a in payload["animations"]}
        pulse = _by_name(payload, "hero-pulse")
        spin = _by_name(payload, "hero-spin")
        assert pulse["keyframes"], "hero-pulse keyframes must resolve"
        assert spin["keyframes"], "hero-spin keyframes must resolve (the v1 bug)"

        # --- keyframes are REAL parsed JSON, not text to re-parse ---
        first = pulse["keyframes"][0]
        assert isinstance(first["offset"], float)
        assert isinstance(first["properties"], dict)
        # "0%, 50%" expanded into one record per offset.
        assert [k["offset"] for k in pulse["keyframes"]] == [0.0, 0.5, 1.0]
        assert "scale(1)" in first["properties"]["transform"]
        # per-keyframe easing survived
        assert pulse["keyframes"][0]["easing"] == "ease-out"

        # --- M5: numbers for reasoning, the token for write-back ---
        assert pulse["timing"]["duration_ms"] == 2000
        assert pulse["timing"]["duration_raw"] == "2s"
        assert spin["timing"]["duration_ms"] == 3000
        # the SHORTER delay list cycled onto both animations
        assert pulse["timing"]["delay_ms"] == 200
        assert spin["timing"]["delay_ms"] == 200
        assert pulse["timing"]["iterations"] == "infinite"

        # --- M9 derived timing, precomputed ---
        assert pulse["derived"]["cycle_ms"] == 4000  # alternate -> 2 iterations
        assert pulse["derived"]["total_ms"] == "infinite"
        assert spin["derived"]["total_ms"] == 6200  # 200 + 2 x 3000

        # --- semantics + prose ---
        assert pulse["semantics"]["easing_class"] == "overshoot"
        assert spin["semantics"]["motion_kind"] == "rotate"
        assert "2s" in pulse["summary"] and "infinite" in pulse["summary"]
        assert "animation" in payload["overview"]

        # --- checkpoints never interpolate ---
        exact = [c for c in pulse["checkpoints"] if c["offset"] == 0.5]
        assert exact and exact[0]["exact"] is True
        bracketed = [c for c in pulse["checkpoints"] if c["offset"] == 0.75]
        assert bracketed and bracketed[0]["exact"] is False
        assert "values" not in bracketed[0]

        # --- M3 sources: a real href and a real rule path ---
        keyframe_sources = [s for s in payload["sources"] if s["kind"] == "keyframes"]
        assert any(s["name"] == "hero-pulse" for s in keyframe_sources)
        assert any(
            str(s["stylesheet"]["href"]).endswith("animations.css")
            for s in keyframe_sources
        )
        assert all(isinstance(s["rule_path"], list) for s in payload["sources"])

        # --- M10 edit recipes point at literals that really exist ---
        recipes = [e for e in pulse["edits"] if e.get("find")]
        assert recipes, "at least one verified find literal"
        keyframe_recipe = [
            e for e in pulse["edits"] if e["knob"].startswith("keyframe[")
        ]
        assert keyframe_recipe
        assert all(str(e["file"]).endswith("animations.css") for e in keyframe_recipe)

        # --- D3/M8: the UA default transition is gone, the real one survives ---
        assert [t["property"] for t in payload["transitions"]] == ["opacity"]
        assert payload["transitions"][0]["duration_ms"] == 300
        assert "all" not in json.dumps(payload["transitions"])

        # --- v1 keys are gone (Q1 clean break) ---
        for dead in ("css_animations", "css_transitions", "keyframe_rules"):
            assert dead not in payload
    finally:
        await close(instance_id=iid)


async def test_pseudo_element_and_waapi_are_reported(fixture_app_server):
    """The two blind spots of v1: a ``::before`` animation and a running
    ``element.animate()``, which returned literally nothing."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        hero = await _extract(iid, base, "#hero")
        pseudo = [
            a
            for a in hero["animations"]
            if a.get("target", {}).get("relation") == "pseudo"
        ]
        assert pseudo, "the ::before animation must be reported"
        assert pseudo[0]["target"]["pseudo_element"] == "::before"

        waapi = await _extract(iid, base, "#waapi")
        assert waapi["has_motion"] is True
        live = [a for a in waapi["animations"] if a["kind"] == "waapi"]
        assert live, "element.animate() must be reported at all"
        animation = live[0]
        assert animation["timing"]["duration_ms"] == 750
        # Infinity survived the JSON boundary as the documented string, NOT the
        # null JSON.stringify would have produced.
        assert animation["timing"]["iterations"] == "infinite"
        assert animation["derived"]["total_ms"] == "infinite"
        assert animation["timing"]["composition"] == "add"
        assert animation["author_id"] == "fixture-waapi-fade"
        assert sorted(animation["animated_properties"]) == ["opacity", "transform"]
        # M10's negative case: there is no CSS to edit here.
        assert animation["editable"] is False
        assert animation["edits"] == []
        assert animation["trigger"]["kind"] == "js"
    finally:
        await close(instance_id=iid)


async def test_view_timeline_is_typed_and_flagged(fixture_app_server):
    """M12: without this the model sees duration "auto" / iterations 1 and
    "repairs" a correct scroll-driven animation."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        payload = await _extract(iid, base, "#scroll-bar")
        scroll_driven = [
            a
            for a in payload["animations"]
            if a.get("timeline", {}).get("type") in {"scroll", "view"}
        ]
        if not scroll_driven:
            pytest.skip("this Chrome build does not support animation-timeline")
        animation = scroll_driven[0]
        assert animation["timeline"]["type"] == "view"
        # duration_ms is OMITTED rather than coerced to 0.
        assert "duration_ms" not in animation["timing"]
        assert animation["trigger"]["kind"] == "view"
        noop = [
            i
            for i in payload["interactions"]
            if i["code"] == "scroll_timeline_duration_noop"
        ]
        assert noop and "animation-range" in noop[0]["remedy"]
    finally:
        await close(instance_id=iid)


async def test_stagger_group_is_computed_across_siblings(fixture_app_server):
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        payload = await _extract(iid, base, "#stagger")
        grouped = [
            a for a in payload["animations"] if "stagger_group" in a.get("derived", {})
        ]
        assert grouped, "three siblings sharing one animation name is a stagger"
        group = grouped[0]["derived"]["stagger_group"]
        assert group["members"] == 3
        assert group["uniform"] is True
        assert group["delta_ms"] == 80
        assert group["delays_ms"] == [0, 80, 160]
    finally:
        await close(instance_id=iid)


async def test_to_file_variant_writes_the_same_schema(fixture_app_server, tmp_path):
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    to_file = get_fn("extract_element_animations_to_file")
    close = get_fn("close_instance")
    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/animations.html")
        written = await to_file(instance_id=iid, selector="#hero")
        assert set(written) == {"file_path", "extraction_type", "summary"}
        summary = written["summary"]
        assert summary["has_motion"] is True
        assert summary["animations_count"] >= 2
        assert summary["keyframes_count"] > 0
    finally:
        await close(instance_id=iid)
