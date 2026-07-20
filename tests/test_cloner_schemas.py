"""Cloner output characterization net (M6-3) — the M5a/M5b gate.

Drives the REAL extraction logic of all five cloner modules against the canned
``fake_tab`` (JS-eval + CDP seams), pinning the CURRENT output *schema* — not
value-level fidelity, which needs real Chrome (integration). Two tiers per the
approved design:

* **(a) hard structural assertions** — invariant top-level key sets, the
  ``{"error": ...}`` shape, and the F-140 nesting divergence that distinguishes
  the three "complete element" engines (flat vs flat+selector/url/timestamp vs
  nested-under-``element``).
* **(b) soft golden JSON per engine** (``tests/goldens/``) captured from this
  tree — a consolidation PR (M5b) diffs and updates these deliberately.

Volatile fields (wall-clock timestamps, absolute paths) are normalised to fixed
sentinels at BOTH capture and compare (see ``fakes.normalize_golden``), so a
golden never embeds a real time/path — that would be a flake/portability bug.
"""

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import (
    FakeStorage,
    FakeTab,
    as_jsonable,
    load_or_capture_golden,
    normalize_golden,
)
from stealth_chrome_devtools_mcp.embedded import cdp_element_cloner as _cdc
from stealth_chrome_devtools_mcp.embedded import element_cloner as _ec
from stealth_chrome_devtools_mcp.embedded import file_based_element_cloner as _fbc
from stealth_chrome_devtools_mcp.embedded import progressive_element_cloner as _pec

GOLDENS_DIR = Path(__file__).resolve().parent / "goldens"

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


def _cdp_responses():
    """Canned CDP command→response map (keyed by generator ``co_name``). Helper
    methods that don't get a rich response fall back gracefully, so the nested
    result schema is produced either way."""
    ns = SimpleNamespace
    return {
        "enable": None,
        "get_document": ns(node_id=1),
        "query_selector_all": [2],
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

    async def test_cdp_returns_error_shape_when_element_missing(self):
        # query_selector_all → [] → the F-140 error contract.
        tab = FakeTab(cdp_responses={**_cdp_responses(), "query_selector_all": []})
        result = await _cdc.CDPElementCloner().extract_complete_element_cdp(
            tab, "#missing"
        )
        assert result == {"error": "Element not found: #missing"}

    @pytest.mark.characterization
    async def test_element_cloner_clone_is_flat_multi_key(self):
        """PINS CURRENT BEHAVIOR incl. known quirks F-140 (3 divergent schemas)
        and F-142 (selector not forwarded to the JS sub-extractors); M5b/M5a will
        intentionally change this — update the golden/assertion when that fix
        lands. ``clone_element_complete`` resolves the element via ``tab.select``
        then gathers the 5 ``extract_*`` — but forwards ``element`` positionally
        (not ``selector``), so the JS sub-extractors error with
        ``"Selector is required"`` while CDP ``styles`` succeeds."""
        tab = FakeTab(
            evaluate_result=dict(CANNED_JS_ELEMENT),
            cdp_responses=_cdp_responses(),
            select_result=SimpleNamespace(node_id=2),
        )
        result = await _ec.element_cloner.clone_element_complete(tab, selector="#demo")
        # Flat multi-key: the 6 extraction names at the top level + metadata.
        assert {
            "styles",
            "structure",
            "events",
            "animations",
            "assets",
            "related_files",
        } <= set(result)
        assert {"url", "timestamp", "selector", "extraction_options"} <= set(result)
        assert "element" not in result
        # The selector-not-forwarded quirk: styles (CDP) works, JS ones error.
        assert result["styles"]["method"] == "cdp_direct"
        assert result["structure"] == {"error": "Selector is required"}
        _assert_golden("element_cloner_clone_complete", result)


# ===========================================================================
# The 5 ElementCloner.extract_* methods.
# ===========================================================================


class TestExtractMethods:
    async def test_styles_uses_cdp_direct_schema(self):
        tab = FakeTab(
            cdp_responses=_cdp_responses(), select_result=SimpleNamespace(node_id=2)
        )
        result = await _ec.element_cloner.extract_element_styles(tab, selector="#demo")
        assert result["method"] == "cdp_direct"
        assert result["computed_styles"] == {
            "color": "rgb(0, 0, 0)",
            "display": "block",
        }
        assert result["css_rules"] == []
        _assert_golden("extract_element_styles", result)

    async def test_styles_error_shape_when_element_unresolved(self):
        # tab.select → None → the styles "Element not found" contract.
        tab = FakeTab(cdp_responses=_cdp_responses(), select_result=None)
        result = await _ec.element_cloner.extract_element_styles(tab, selector="#demo")
        assert result == {"error": "Element not found"}

    @pytest.mark.characterization
    @pytest.mark.parametrize(
        "method",
        [
            "extract_element_structure",
            "extract_element_events",
            "extract_element_animations",
            "extract_element_assets",
        ],
    )
    async def test_js_extract_passes_dict_through(self, method):
        """PINS CURRENT BEHAVIOR incl. known quirk F-142 (4 of 5 extract_* still on
        the JS-eval path); M5a will intentionally move these to CDP — update when
        that fix lands. Each JS-path method returns a ``dict`` result from
        ``tab.evaluate`` unchanged (passthrough)."""
        tab = FakeTab(evaluate_result=dict(CANNED_JS_ELEMENT))
        result = await getattr(_ec.element_cloner, method)(tab, selector="#demo")
        assert result == CANNED_JS_ELEMENT
        assert tab.evaluate_calls  # the JS-eval path was actually exercised

    async def test_structure_requires_selector(self):
        tab = FakeTab(evaluate_result=dict(CANNED_JS_ELEMENT))
        result = await _ec.element_cloner.extract_element_structure(tab, selector=None)
        assert result == {"error": "Selector is required"}

    async def test_structure_converts_nodriver_array_result(self):
        # nodriver's [[key, {type,value}], ...] array format → dict via converter.
        tab = FakeTab(
            evaluate_result=[["tag_name", {"type": "string", "value": "DIV"}]]
        )
        result = await _ec.element_cloner.extract_element_structure(
            tab, selector="#demo"
        )
        assert result == {"tag_name": "DIV"}
        _assert_golden("extract_element_structure_list_convert", result, volatile=())


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

    def test_expand_missing_element_error_shape(self, seeded_progressive_store):
        assert seeded_progressive_store.expand_styles("nope") == {
            "error": "Element nope not found"
        }


# ===========================================================================
# FileBasedElementCloner to-file summary shape (F-141).
# ===========================================================================


class TestFileBasedCloner:
    async def test_structure_to_file_summary_shape(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_fbc.file_based_element_cloner, "output_dir", tmp_path)
        tab = FakeTab(
            evaluate_result={
                "tag_name": "DIV",
                "attributes": {"id": "demo"},
                "data_attributes": {},
                "children": [],
                "dom_path": "html>body>div",
            }
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

    async def test_structure_to_file_swallows_delegated_error(
        self, tmp_path, monkeypatch
    ):
        """The unified to-file contract (F-141) swallows a delegated extractor
        error rather than propagating it: when the engine returns
        ``{"error": ...}`` (here: no selector), ``_extract_and_save`` still writes
        that payload to disk and returns the normal
        ``{file_path, extraction_type, summary}`` shape with an all-empty summary
        (``tag_name`` None). This is now the deliberate one-contract behavior —
        previously only 7 of 8 copies swallowed; clone_complete propagated."""
        monkeypatch.setattr(_fbc.file_based_element_cloner, "output_dir", tmp_path)
        tab = FakeTab(evaluate_result=dict(CANNED_JS_ELEMENT))
        result = await _fbc.file_based_element_cloner.extract_element_structure_to_file(
            tab, selector=None
        )
        assert set(result) == {"file_path", "extraction_type", "summary"}
        assert "error" not in result
        assert result["summary"]["tag_name"] is None


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
            cdp_responses=_cdp_responses(), select_result=SimpleNamespace(node_id=2)
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

    async def test_styles_error_shape_when_unresolved(self):
        tab = FakeTab(cdp_responses=_cdp_responses(), select_result=None)
        result = await _cdc.cdp_element_cloner.extract_element_styles(
            tab, selector="#demo"
        )
        assert result == {"error": "Element not found"}

    @pytest.mark.parametrize(
        "method",
        [
            "extract_element_structure",
            "extract_element_events",
            "extract_element_animations",
            "extract_element_assets",
        ],
    )
    async def test_js_aspect_passes_dict_through(self, method):
        # Structure/events/animations/assets stay on JS-eval (§2.1 + 2026-07-18
        # structure ruling) — zero capability loss vs the retired ElementCloner.
        tab = FakeTab(evaluate_result=dict(CANNED_JS_ELEMENT))
        result = await getattr(_cdc.cdp_element_cloner, method)(tab, selector="#demo")
        assert result == CANNED_JS_ELEMENT
        assert tab.evaluate_calls  # JS-eval path exercised
        assert not tab.send_calls  # and NOT the CDP path

    async def test_transport_split_styles_cdp_others_js(self):
        """Pins the §2.1 transport decision deterministically (no timing): styles
        takes the CDP (``.send``) path; the JS aspects take ``.evaluate``."""
        styles_tab = FakeTab(
            cdp_responses=_cdp_responses(), select_result=SimpleNamespace(node_id=2)
        )
        await _cdc.cdp_element_cloner.extract_element_styles(
            styles_tab, selector="#demo"
        )
        assert styles_tab.send_calls and not styles_tab.evaluate_calls

        js_tab = FakeTab(evaluate_result=dict(CANNED_JS_ELEMENT))
        await _cdc.cdp_element_cloner.extract_element_structure(
            js_tab, selector="#demo"
        )
        assert js_tab.evaluate_calls and not js_tab.send_calls

    async def test_resolve_node_id_variants(self):
        tab = FakeTab(cdp_responses=_cdp_responses())
        assert (
            await _cdc.cdp_element_cloner._resolve_node_id(
                tab, element=SimpleNamespace(node_id=7)
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
            evaluate_result=dict(CANNED_JS_ELEMENT),
            cdp_responses=_cdp_responses(),
            select_result=SimpleNamespace(node_id=2),
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
        assert result["animations"] == CANNED_JS_ELEMENT
        assert result["assets"] == CANNED_JS_ELEMENT
        _assert_golden("canonical_engine", result)
