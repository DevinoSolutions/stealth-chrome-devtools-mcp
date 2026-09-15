"""Cloner output characterization net (M6-3) — the M5a/M5b gate.

Drives the REAL extraction logic of all five cloner modules against the canned
``fake_tab`` (JS-eval + CDP seams), pinning the CURRENT output *schema* — not
value-level fidelity, which needs real Chrome (integration). Two tiers per the
approved design:

* **(a) hard structural assertions** — invariant top-level key sets, the
  failure shape (a raised ``ToolError`` since F-858; it was a returned
  ``{"error": ...}`` dict up to 2.1.1), and the F-140 nesting divergence that
  distinguishes the three "complete element" engines (flat vs
  flat+selector/url/timestamp vs nested-under-``element``).
* **(b) soft golden JSON per engine** (``tests/goldens/``) captured from this
  tree — a consolidation PR (M5b) diffs and updates these deliberately.

Volatile fields (wall-clock timestamps, absolute paths) are normalised to fixed
sentinels at BOTH capture and compare (see ``fakes.normalize_golden``), so a
golden never embeds a real time/path — that would be a flake/portability bug.
"""

import inspect
import re
from pathlib import Path
from types import SimpleNamespace

import nodriver.cdp.dom as cdp_dom
import pytest

from fakes import (
    FakeStorage,
    FakeTab,
    animation_evaluate_map,
    as_jsonable,
    fake_element,
    js_aspect_answer,
    load_or_capture_golden,
    normalize_golden,
)
from stealth_chrome_devtools_mcp.embedded import cdp_element_cloner as _cdc
from stealth_chrome_devtools_mcp.embedded import file_based_element_cloner as _fbc
from stealth_chrome_devtools_mcp.embedded import progressive_element_cloner as _pec
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

GOLDENS_DIR = Path(__file__).resolve().parent / "goldens"
_JS_DIR = Path(_cdc.__file__).resolve().parent / "js"

# The scripts the ENGINE evaluates, read out of the engine's own source rather
# than listed here: a sixth or seventh aspect joins the F-872 pin below the day
# its filename appears in ``cdp_element_cloner.py``, with no edit to this file.
# ``extract_styles.js`` / ``comprehensive_element_extractor.js`` are packaged but
# never evaluated by the engine (styles is the CDP path), so they are absent by
# construction, not by exception.
EVALUATED_JS = sorted(
    set(
        re.findall(
            r'"(extract_\w+\.js)"',
            Path(_cdc.__file__).read_text(encoding="utf-8"),
        )
    )
)


def _bidi_nodes(obj: object, path: str = "$") -> list[str]:
    """Every path in ``obj`` still holding a BiDi ``RemoteValue`` node (F-872).

    A ``{"type": …, "value": …}`` dict is what ``Tab.evaluate``'s deep
    serialization emits for a JS value; finding one in an aspect's RESULT means
    the transport's encoding leaked into the payload a caller reads.
    """
    if isinstance(obj, dict):
        if "type" in obj and set(obj) <= {
            "type",
            "value",
            "objectId",
            "weakLocalObjectReference",
        }:
            return [path]
        return [p for k, v in obj.items() for p in _bidi_nodes(v, f"{path}.{k}")]
    if isinstance(obj, list):
        return [p for i, v in enumerate(obj) for p in _bidi_nodes(v, f"{path}[{i}]")]
    return []


# --- Canned tab responses (test data; the FakeTab MECHANISM lives in fakes.py) --

# What the comprehensive/JS engines' ``tab.evaluate`` returns (a deserialised
# element extraction result).
CANNED_JS_ELEMENT = {
    "html": {
        "outerHTML": '<div id="demo">hi</div>',
        "tagName": "DIV",
        "id": "demo",
        "className": "box",
        "attributes": [{"name": "id", "value": "demo"}],
    },
    "computedStyles": {"color": "rgb(0, 0, 0)", "display": "block"},
    "eventListeners": [{"type": "click", "source": "inline", "handler": "f()"}],
    "cssRules": [],
    "children": [],
}


# What the animations collector returns — a FACT payload, delivered as the JSON
# string a real tab delivers (F-846). Deliberately small: this file pins the
# COMPOSED clone's shape; the animations schema itself is pinned in
# test_animation_schema_v2.py.
CANNED_ANIMATION_FACTS = {
    "facts_version": 1,
    "selector": "#demo",
    "url": "https://fake.test/page",
    "captured_at_ms": 100.0,
    "element": {
        "tag": "div",
        "id": "demo",
        "classes": ["box"],
        "inline_properties": [],
        "is_canvas": False,
    },
    "computed": {
        "animation_name": "pulse",
        "animation_duration": "2s",
        "animation_delay": "0s",
        "animation_timing_function": "ease-in-out",
        "animation_iteration_count": "infinite",
        "animation_direction": "alternate",
        "animation_fill_mode": "both",
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
    },
    "transforms": {"transform": "none", "transform_origin": "50% 50%"},
    "keyframe_rules": [
        {
            "name": "pulse",
            "source_ref": "src-0",
            "keyframes": [
                {
                    "key_text": "0%",
                    "css_text": "transform: scale(1);",
                    "easing": "",
                    "composite": "",
                },
                {
                    "key_text": "100%",
                    "css_text": "transform: scale(1.08);",
                    "easing": "",
                    "composite": "",
                },
            ],
        }
    ],
    "waapi": [],
    "matched_rules": [],
    "candidate_rules": [],
    "sources": [],
    "warnings": [],
    "caps_hit": {},
}


def _cdp_responses():
    """Canned CDP command→response map (keyed by generator ``co_name``). Helper
    methods that don't get a rich response fall back gracefully, so the nested
    result schema is produced either way."""
    ns = SimpleNamespace
    return {
        "enable": None,
        # Real NodeIds, not bare ints: every by-node CDP command serialises with
        # node_id.to_json(), so a bare int is a response Chrome could not send.
        "get_document": fake_element(node_id=1),
        "query_selector_all": [cdp_dom.NodeId(2)],
        "describe_node": ns(
            tag_name="div",
            node_name="DIV",
            local_name="div",
            node_value=None,
            attributes=["id", "demo", "class", "box"],
            children=None,
        ),
        "get_outer_html": '<div id="demo" class="box">hi</div>',
        "get_computed_style_for_node": [
            ns(name="color", value="rgb(0, 0, 0)"),
            ns(name="display", value="block"),
        ],
        "get_matched_styles_for_node": [None, None, [], [], []],
        "resolve_node": ns(object_id=None),
        "request_child_nodes": None,
    }


def _assert_golden(name, obj, volatile=("timestamp",)):
    """Tier-(b): compare ``obj`` to the committed golden (captured on first run),
    both normalised + jsonable so the comparison is byte-consistent."""
    normalized = as_jsonable(normalize_golden(obj, volatile))
    golden = load_or_capture_golden(GOLDENS_DIR / f"{name}.json", normalized)
    assert normalized == golden


# ===========================================================================
# The three disagreeing "complete element" engines (F-140).
# ===========================================================================


class TestCompleteElementEngines:
    async def test_cdp_is_nested_under_element(self):
        tab = FakeTab(cdp_responses=_cdp_responses())
        result = await _cdc.CDPElementCloner().extract_complete_element_cdp(
            tab, "#demo", include_children=True
        )
        # Nested: the element data lives under a top-level "element" block.
        assert result["extraction_method"] == "CDP"
        assert set(result["element"]) == {
            "html",
            "computed_styles",
            "matched_styles",
            "event_listeners",
            "children",
        }
        assert {"extraction_stats", "selector", "url", "timestamp"} <= set(result)
        _assert_golden("cdp_complete_element", result)

    async def test_cdp_raises_when_element_missing(self):
        # query_selector_all → [] → the F-140 error contract, which F-858 moved
        # onto the ONE convention: the message text is byte-preserved, only the
        # transport changed (returned dict → raised ToolError).
        tab = FakeTab(cdp_responses={**_cdp_responses(), "query_selector_all": []})
        with pytest.raises(ToolError, match=r"^Element not found: #missing$"):
            await _cdc.CDPElementCloner().extract_complete_element_cdp(tab, "#missing")


# ===========================================================================
# ProgressiveElementCloner.expand_* + list_stored_elements (in-memory store).
# ===========================================================================


ELEMENT_ID = "elem_fixedtest01"
# The ONE canonical aspect-keyed shape produced by
# cdp_element_cloner.extract_complete_element (M5b-3b re-point). The old nested
# ``{"element": {...}}`` dual-schema fallback (F-143) is deleted; progressive
# now reads styles.computed_styles / events.event_listeners / structure.children.
STORED_FULL_DATA = {
    "styles": {
        "method": "cdp_direct",
        "computed_styles": {"color": "red", "display": "block"},
        "css_rules": [],
    },
    "structure": {"tag_name": "DIV", "attributes": {}, "children": []},
    "events": {"event_listeners": [{"type": "click"}]},
    "animations": {},
    "assets": {"fonts": {}},
    "related_files": {},
}


@pytest.fixture()
def seeded_progressive_store(monkeypatch):
    """Isolate the shared ``in_memory_storage`` singleton: swap in a FakeStorage
    seeded with one stored element under a FIXED id (deterministic — no volatile
    uuid/timestamp), restored automatically at teardown (registry-mutation
    safety)."""
    store = FakeStorage()
    monkeypatch.setattr(_pec, "in_memory_storage", store)
    _pec.progressive_element_cloner._save_store(
        {
            ELEMENT_ID: {
                "full_data": STORED_FULL_DATA,
                "url": "https://fake.test/page",
                "selector": "#demo",
                "timestamp": 111.0,
                "include_children": True,
            }
        }
    )
    return _pec.progressive_element_cloner


class TestProgressiveCloner:
    def test_expand_styles_schema(self, seeded_progressive_store):
        result = seeded_progressive_store.expand_styles(ELEMENT_ID)
        assert set(result) == {
            "element_id",
            "data_type",
            "styles",
            "total_available",
            "returned_count",
        }
        assert result["data_type"] == "styles"
        assert result["styles"] == {"color": "red", "display": "block"}
        _assert_golden("progressive_expand_styles", result, volatile=())

    def test_expand_events_schema(self, seeded_progressive_store):
        result = seeded_progressive_store.expand_events(ELEMENT_ID)
        assert result["data_type"] == "events"
        assert result["event_listeners"] == [{"type": "click"}]

    def test_list_stored_elements_schema(self, seeded_progressive_store):
        result = seeded_progressive_store.list_stored_elements()
        assert set(result) == {"stored_elements", "total_count"}
        assert result["total_count"] == 1
        assert result["stored_elements"][0]["element_id"] == ELEMENT_ID
        _assert_golden("progressive_list_stored_elements", result)

    def test_expand_missing_element_raises(self, seeded_progressive_store):
        with pytest.raises(ToolError, match=r"^Element nope not found$"):
            seeded_progressive_store.expand_styles("nope")


# ===========================================================================
# FileBasedElementCloner to-file summary shape (F-141).
# ===========================================================================


class TestFileBasedCloner:
    async def test_structure_to_file_summary_shape(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_fbc.file_based_element_cloner, "output_dir", tmp_path)
        tab = FakeTab(
            evaluate_result=js_aspect_answer(
                {
                    "tag_name": "DIV",
                    "attributes": {"id": "demo"},
                    "data_attributes": {},
                    "children": [],
                    "dom_path": "html>body>div",
                }
            )
        )
        result = await _fbc.file_based_element_cloner.extract_element_structure_to_file(
            tab, selector="#demo"
        )
        # The one unified to-file contract (F-141): every *_to_file method now
        # returns exactly {file_path, extraction_type, summary} via the shared
        # _extract_and_save helper; selector re-homes into summary.
        assert set(result) == {"file_path", "extraction_type", "summary"}
        assert result["extraction_type"] == "structure"
        assert result["summary"]["selector"] == "#demo"
        assert isinstance(result["summary"], dict)
        # The file really landed under the patched (temp) output dir.
        assert Path(result["file_path"]).exists()
        assert not inspect.iscoroutine(result)
        _assert_golden("file_based_structure_to_file", result, volatile=("file_path",))

    async def test_structure_to_file_propagates_a_delegated_failure(
        self, tmp_path, monkeypatch
    ):
        """SOFT-GOLDEN UPDATE (F-858, deliberate): the unified to-file contract
        (F-141) used to SWALLOW a delegated extractor error — it wrote the
        engine's ``{"error": ...}`` payload to disk and answered with the normal
        ``{file_path, extraction_type, summary}`` shape and an all-empty summary
        (``tag_name`` None). F-141 chose that for CONSISTENCY across the 8 copies
        (7 swallowed, clone_complete propagated), not because a caller wanted it,
        and the result is the F-795/F-802 defect class: a payload whose shape
        says the clone worked, with an empty summary no caller can tell from a
        genuinely empty element. The engine raises now, so the failure reaches
        the caller and no file claims to be a clone that does not exist."""
        monkeypatch.setattr(_fbc.file_based_element_cloner, "output_dir", tmp_path)
        tab = FakeTab(evaluate_result=js_aspect_answer(CANNED_JS_ELEMENT))
        with pytest.raises(ToolError, match=r"^Selector is required$"):
            await _fbc.file_based_element_cloner.extract_element_structure_to_file(
                tab, selector=None
            )
        assert list(tmp_path.glob("*.json")) == []


# ===========================================================================
# M5b canonical engine surface — CDPElementCloner grows the ONE home the five
# engines converge onto (additive; nothing re-pointed/deleted yet in M5b-1).
# ===========================================================================


class TestCanonicalEngine:
    def test_singleton_exists(self):
        # F-144: module-level singleton, mirroring the sibling cloners.
        assert isinstance(_cdc.cdp_element_cloner, _cdc.CDPElementCloner)

    async def test_styles_uses_cdp_direct_schema(self):
        tab = FakeTab(
            cdp_responses=_cdp_responses(), select_result=fake_element(node_id=2)
        )
        result = await _cdc.cdp_element_cloner.extract_element_styles(
            tab, selector="#demo"
        )
        assert result["method"] == "cdp_direct"
        assert result["computed_styles"] == {
            "color": "rgb(0, 0, 0)",
            "display": "block",
        }
        assert result["css_rules"] == []
        # REUSES the element_cloner styles golden on purpose: the engine's CDP
        # styles path must be byte-identical to the one it replaces (dedup, no
        # schema change). A drift in either implementation reds this.
        _assert_golden("extract_element_styles", result)

    async def test_styles_raises_when_unresolved(self):
        tab = FakeTab(cdp_responses=_cdp_responses(), select_result=None)
        with pytest.raises(ToolError, match=r"^Element not found$"):
            await _cdc.cdp_element_cloner.extract_element_styles(tab, selector="#demo")

    @pytest.mark.parametrize(
        "method",
        [
            "extract_element_structure",
            "extract_element_events",
            "extract_element_assets",
            "extract_related_files",
        ],
    )
    async def test_js_aspect_parses_the_one_json_string(self, method):
        # Structure/events/assets/related_files stay on JS-eval (§2.1 +
        # 2026-07-18 structure ruling) — zero capability loss vs the retired
        # ElementCloner.
        #
        # SOFT-GOLDEN/FIXTURE UPDATE (F-872, deliberate): this used to feed
        # ``evaluate_result=dict(...)`` and assert the dict passed through. A
        # real ``Tab.evaluate`` NEVER hands back a dict — it always requests deep
        # serialization, so an object literal arrives as BiDi ``RemoteValue``
        # nodes. The old fixture was the shape the bug needed to stay invisible
        # (measured: ``children``/``class_list``/``images``/``stylesheets`` came
        # back as ``{'type': 'object', 'value': [[…]]}`` from real Chrome).
        # ``extract_element_animations`` reached this contract first in F-846;
        # all six aspects share it now.
        tab = FakeTab(evaluate_result=js_aspect_answer(CANNED_JS_ELEMENT))
        result = await getattr(_cdc.cdp_element_cloner, method)(tab, selector="#demo")
        assert result == CANNED_JS_ELEMENT
        assert tab.evaluate_calls  # JS-eval path exercised
        assert not tab.send_calls  # and NOT the CDP path

    async def test_structure_requires_selector(self):
        tab = FakeTab(evaluate_result=js_aspect_answer(CANNED_JS_ELEMENT))
        with pytest.raises(ToolError, match=r"^Selector is required$"):
            await _cdc.cdp_element_cloner.extract_element_structure(tab, selector=None)

    @pytest.mark.parametrize("script", EVALUATED_JS)
    def test_every_aspect_script_returns_one_json_string(self, script):
        """F-872 ROOT-CAUSE PIN: no aspect script may return a bare object.

        ``nodriver``'s ``Tab.evaluate`` sends
        ``SerializationOptions(serialization="deep", max_depth=10)`` on every
        call and hands back ``deep_serialized_value.value`` verbatim
        (``nodriver/core/tab.py``; ``cdp/runtime.py`` keeps ``json["value"]``
        as-is), so a returned OBJECT arrives as BiDi ``RemoteValue`` nodes at
        every depth and ``return_by_value`` cannot undo it. A string is the one
        shape the transport leaves alone. Pinning the SCRIPTS rather than one
        conversion helper is what keeps a seventh aspect from reintroducing the
        defect the day it is added."""
        src = (_JS_DIR / script).read_text(encoding="utf-8")
        assert "return JSON.stringify(" in src, (
            f"{script} must hand back ONE JSON string (F-872)"
        )
        assert not re.search(r"\breturn result;", src), (
            f"{script} still returns a bare object — deep serialization corrupts "
            "every nested array/object in it (F-872)"
        )

    async def test_nested_containers_survive_the_transport(self):
        """F-872 REGRESSION PIN, payload measured from real headless Chrome.

        Before the fix the engine's ``_convert_nodriver_result`` unwrapped the
        TOP level only: ``children`` stayed a list of
        ``{'type': 'object', 'value': [['tag_name', {...}], …]}`` nodes,
        ``class_list`` a list of ``{'type': 'string', …}`` nodes, and an empty
        JS object (``framework_handlers``) decayed into an empty LIST. Nothing
        raised, so every caller read corrupted values as real ones."""
        payload = {
            "tag_name": "div",
            "class_list": ["box", "outer"],
            "children": [
                {"tag_name": "span", "id": None, "class_name": "child a"},
                {"tag_name": "span", "id": None, "class_name": "child b"},
            ],
            "dimensions": {"width": 732, "height": 70},
            "framework_handlers": {},
        }
        tab = FakeTab(evaluate_result=js_aspect_answer(payload))
        result = await _cdc.cdp_element_cloner.extract_element_structure(
            tab, selector="#target"
        )
        assert result == payload
        assert result["children"][0]["tag_name"] == "span"
        assert result["class_list"] == ["box", "outer"]
        assert result["framework_handlers"] == {}  # an object, not a list
        assert _bidi_nodes(result) == []

    async def test_transport_split_styles_cdp_others_js(self):
        """Pins the §2.1 transport decision deterministically (no timing): styles
        takes the CDP (``.send``) path; the JS aspects take ``.evaluate``."""
        styles_tab = FakeTab(
            cdp_responses=_cdp_responses(), select_result=fake_element(node_id=2)
        )
        await _cdc.cdp_element_cloner.extract_element_styles(
            styles_tab, selector="#demo"
        )
        assert styles_tab.send_calls and not styles_tab.evaluate_calls

        js_tab = FakeTab(evaluate_result=js_aspect_answer(CANNED_JS_ELEMENT))
        await _cdc.cdp_element_cloner.extract_element_structure(
            js_tab, selector="#demo"
        )
        assert js_tab.evaluate_calls and not js_tab.send_calls

    async def test_resolve_node_id_variants(self):
        tab = FakeTab(cdp_responses=_cdp_responses())
        assert (
            await _cdc.cdp_element_cloner._resolve_node_id(
                tab, element=fake_element(node_id=7)
            )
            == 7
        )
        unresolved = FakeTab(select_result=None)
        assert (
            await _cdc.cdp_element_cloner._resolve_node_id(unresolved, selector="#x")
            is None
        )

    async def test_complete_composes_all_six_aspects(self):
        tab = FakeTab(
            evaluate_result=js_aspect_answer(CANNED_JS_ELEMENT),
            # The animations script answers with its OWN JSON string, so the
            # composed clone carries a real schema-v2 block rather than the
            # generic canned element every other JS aspect gets. (Every aspect
            # answers with a string since F-872; only the CONTENT differs.)
            evaluate_map=animation_evaluate_map(CANNED_ANIMATION_FACTS),
            cdp_responses=_cdp_responses(),
            select_result=fake_element(node_id=2),
        )
        result = await _cdc.cdp_element_cloner.extract_complete_element(
            tab, selector="#demo"
        )
        # ONE canonical flat schema (F-140 3->1): NOT nested under "element".
        assert "element" not in result
        assert {"url", "timestamp", "selector", "extraction_options"} <= set(result)
        assert {
            "styles",
            "structure",
            "events",
            "animations",
            "assets",
            "related_files",
        } <= set(result)
        # F-142 fixed: every aspect populates (selector forwarded), unlike the
        # retired clone_element_complete where the JS ones erred "Selector ...".
        assert result["styles"]["method"] == "cdp_direct"
        assert result["structure"] == CANNED_JS_ELEMENT
        assert result["events"] == CANNED_JS_ELEMENT
        assert result["assets"] == CANNED_JS_ELEMENT
        # The animations aspect is the one with its own schema (v2, F-848).
        assert result["animations"]["schema_version"] == 2
        assert result["animations"]["animations"][0]["name"] == "pulse"
        _assert_golden("canonical_engine", result)
