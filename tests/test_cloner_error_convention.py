"""F-858: the cloner subsystem joins the ONE error convention.

RED-FIRST condition (on ``main`` at bb8b5ce, before the sweep): every test in
:class:`TestEngineAspectsRaise`, :class:`TestProgressiveRaises`,
:class:`TestToFilePropagates` and :class:`TestNoSecondErrorShape` FAILS, because
the cloner engine, the progressive adapter and the to-file adapter all reported
failure by RETURNING ``{"error": ...}`` — the shape CLAUDE.md convention 2 and
`DESIGN §9 <../DESIGN.md>`_ ban outside a *named* KEEP contract. The
``pytest.raises`` blocks fail with ``DID NOT RAISE``; the AST guard fails with a
29-entry offender list.

What the sweep must NOT change is pinned here too, in
:class:`TestKeptEmbeddedFailureRecords` — those tests are GREEN before and after
and exist so the sweep cannot quietly take the isolation with it:

* **per-aspect isolation** (F-851 / the ``gather`` composition site): one aspect
  failing must still leave the other five populated, with the failure embedded
  as ``result[aspect]["error"]``. Raising aspects reach that record through the
  ``return_exceptions=True`` gather that was already there — the isolation is
  the SAME one home, not a second one;
* **sub-field degradation** inside ``extract_complete_element_cdp``: the private
  ``_get_element_html`` helper answers a per-node CDP failure with an embedded
  ``{"error": ...}`` exactly as its siblings answer with ``{}`` / ``[]``. It is a
  payload field, never a tool return, so it stays.
"""

import ast
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import nodriver.cdp.dom as cdp_dom
import pytest

from fakes import FakeStorage, FakeTab, fake_element
from stealth_chrome_devtools_mcp.embedded import cdp_element_cloner as _cdc
from stealth_chrome_devtools_mcp.embedded import file_based_element_cloner as _fbc
from stealth_chrome_devtools_mcp.embedded import progressive_element_cloner as _pec
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

# ---------------------------------------------------------------------------
# Canned seams (kept local: this file pins the ERROR convention, not the schema,
# which stays pinned in test_cloner_schemas.py).
# ---------------------------------------------------------------------------


def _cdp_responses():
    ns = SimpleNamespace
    return {
        "enable": None,
        "get_document": fake_element(node_id=1),
        "query_selector_all": [cdp_dom.NodeId(2)],
        "describe_node": ns(
            tag_name="div",
            node_name="DIV",
            local_name="div",
            node_value=None,
            attributes=["id", "demo"],
            children=None,
        ),
        "get_outer_html": '<div id="demo">hi</div>',
        "get_computed_style_for_node": [ns(name="color", value="rgb(0, 0, 0)")],
        "get_matched_styles_for_node": [None, None, [], [], []],
        "resolve_node": ns(object_id=None),
        "request_child_nodes": None,
    }


#: A value carrying ``exception_details`` — what ``Tab.evaluate`` hands back
#: when the injected extraction script threw (nodriver returns the record in the
#: value's place rather than raising).
JS_THREW = SimpleNamespace(exception_details="ReferenceError: x is not defined")


class TestEngineAspectsRaise:
    """Every public aspect on the ONE engine reports failure by raising, with
    the message text it used to put in the dict preserved verbatim."""

    async def test_styles_unresolved_element_raises(self):
        tab = FakeTab(cdp_responses=_cdp_responses(), select_result=None)
        with pytest.raises(ToolError, match=r"^Element not found$"):
            await _cdc.cdp_element_cloner.extract_element_styles(tab, selector="#demo")

    async def test_styles_operational_failure_keeps_its_prefix(self):
        """The CDP-path failure message names the transport that failed — the
        prefix is the whole diagnostic value, so re-wrapping must not eat it."""

        def _boom(_name):
            raise RuntimeError("socket closed")

        tab = FakeTab(cdp_responses={"enable": None, "get_document": _boom})
        tab._cdp_responses["get_computed_style_for_node"] = _boom
        with pytest.raises(ToolError, match=r"CDP extraction failed: socket closed"):
            await _cdc.cdp_element_cloner.extract_element_styles(
                tab, element=fake_element(node_id=2)
            )

    @pytest.mark.parametrize(
        "method",
        [
            "extract_element_structure",
            "extract_element_events",
            "extract_element_animations",
            "extract_element_assets",
        ],
    )
    async def test_missing_selector_raises(self, method):
        tab = FakeTab(evaluate_result={})
        with pytest.raises(ToolError, match=r"^Selector is required$"):
            await getattr(_cdc.cdp_element_cloner, method)(tab, selector=None)

    @pytest.mark.parametrize(
        "method",
        [
            "extract_element_structure",
            "extract_element_events",
            "extract_element_animations",
            "extract_element_assets",
            "extract_related_files",
        ],
    )
    async def test_a_script_that_threw_raises(self, method):
        tab = FakeTab(evaluate_result=JS_THREW)
        with pytest.raises(ToolError, match=r"JavaScript error: ReferenceError"):
            await getattr(_cdc.cdp_element_cloner, method)(tab, selector="#demo")

    @pytest.mark.parametrize(
        "method",
        [
            "extract_element_structure",
            "extract_element_events",
            "extract_element_assets",
            "extract_related_files",
        ],
    )
    async def test_an_unexpected_return_type_raises_and_still_shows_the_payload(
        self, method
    ):
        """The old dict carried the offending value in a ``raw_data`` key. A
        raise has one field, so the payload folds into the message — dropping it
        would leave "unexpected type" with nothing to debug from."""
        tab = FakeTab(evaluate_result=42)
        with pytest.raises(ToolError, match=r"Unexpected return type: .*int.*42"):
            await getattr(_cdc.cdp_element_cloner, method)(tab, selector="#demo")

    async def test_animations_unexpected_return_type_raises(self):
        # The animations collector alone must answer with a JSON *string*
        # (F-846), so its non-str branch is the same defect, differently typed.
        tab = FakeTab(evaluate_result={"animations": []})
        with pytest.raises(ToolError, match=r"Unexpected return type"):
            await _cdc.cdp_element_cloner.extract_element_animations(
                tab, selector="#demo"
            )

    async def test_animations_collector_error_is_re_raised_not_passed_through(self):
        """The collector reports its own miss inside the JSON it returns. That
        payload used to be handed back verbatim as the aspect's result — Q4's
        "parity with the other five". The five now raise, so parity means this
        raises too, carrying the collector's own words."""
        tab = FakeTab(evaluate_result=json.dumps({"error": "Element not found"}))
        with pytest.raises(ToolError, match=r"^Element not found$"):
            await _cdc.cdp_element_cloner.extract_element_animations(
                tab, selector="#hero"
            )

    async def test_animations_non_json_answer_raises(self):
        tab = FakeTab(evaluate_result="<html>not json</html>")
        with pytest.raises(ToolError):
            await _cdc.cdp_element_cloner.extract_element_animations(
                tab, selector="#hero"
            )

    async def test_complete_element_cdp_missing_element_raises(self):
        tab = FakeTab(cdp_responses={**_cdp_responses(), "query_selector_all": []})
        with pytest.raises(ToolError, match=r"^Element not found: #missing$"):
            await _cdc.CDPElementCloner().extract_complete_element_cdp(tab, "#missing")

    async def test_complete_element_cdp_not_found_is_not_relabelled(self):
        """The outer handler prefixes ``CDP extraction failed:``. A not-found
        selector is not a transport failure, so it must reach the caller as
        itself rather than wrapped in a cause it does not have."""
        tab = FakeTab(cdp_responses={**_cdp_responses(), "query_selector_all": []})
        with pytest.raises(ToolError) as exc:
            await _cdc.CDPElementCloner().extract_complete_element_cdp(tab, "#missing")
        assert "CDP extraction failed" not in str(exc.value)


class TestProgressiveRaises:
    """The progressive adapter's store misses join the convention: an
    ``element_id`` naming nothing is the ``InstanceNotFoundError`` shape one
    level down, not a value the caller destructures."""

    @pytest.fixture()
    def cloner(self, monkeypatch):
        monkeypatch.setattr(_pec, "in_memory_storage", FakeStorage())
        return _pec.progressive_element_cloner

    @pytest.mark.parametrize(
        "method",
        [
            "expand_styles",
            "expand_events",
            "expand_children",
            "expand_css_rules",
            "expand_pseudo_elements",
            "expand_animations",
            "clear_stored_element",
        ],
    )
    def test_unknown_element_id_raises(self, cloner, method):
        with pytest.raises(ToolError, match=r"^Element nope not found$"):
            getattr(cloner, method)("nope")

    async def test_a_failed_extraction_propagates_the_real_cause(self, cloner):
        """The adapter used to flatten every engine failure into the single
        string "Element not found or extraction failed", which named neither the
        element nor the failure. Delegating to the engine's own raise is what
        makes the answer actionable."""
        tab = FakeTab(evaluate_result={}, cdp_responses=_cdp_responses())

        async def boom(*args, **kwargs):
            raise ToolError("the tab went away mid-clone")

        cloner_engine = _pec.cdp_element_cloner
        original = cloner_engine.extract_complete_element
        try:
            cloner_engine.extract_complete_element = boom
            with pytest.raises(ToolError, match=r"the tab went away mid-clone"):
                await cloner.clone_element_progressive(tab, "#demo")
        finally:
            cloner_engine.extract_complete_element = original


class TestToFilePropagates:
    """F-141's to-file layer stops swallowing a delegated extraction failure.

    Writing a file whose CONTENT is ``{"error": ...}`` and answering the caller
    with the normal ``{file_path, extraction_type, summary}`` shape is the
    F-795/F-802 defect class in the cloner: every Python-side step succeeded, so
    the payload claimed a clone that does not exist. The summary was all-empty,
    which no caller can tell from a genuinely empty element."""

    async def test_a_delegated_failure_is_not_written_and_not_reported_as_a_save(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(_fbc.file_based_element_cloner, "output_dir", tmp_path)
        tab = FakeTab(evaluate_result={"tag_name": "DIV"})
        with pytest.raises(ToolError, match=r"^Selector is required$"):
            await _fbc.file_based_element_cloner.extract_element_structure_to_file(
                tab, selector=None
            )
        assert list(tmp_path.glob("*.json")) == [], (
            "a clone that failed must leave no file claiming to be one"
        )

    async def test_a_successful_save_is_unchanged(self, tmp_path, monkeypatch):
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
        assert set(result) == {"file_path", "extraction_type", "summary"}
        assert Path(result["file_path"]).exists()


class TestKeptEmbeddedFailureRecords:
    """GREEN before and after the sweep. The convention is about what a TOOL
    returns; a failure recorded as a FIELD of a payload the caller asked for is
    a different thing, and both survivors are named here so a future sweep does
    not read them as leftovers."""

    async def test_one_raising_aspect_still_leaves_its_five_siblings_populated(
        self, monkeypatch
    ):
        async def boom(*args, **kwargs):
            raise ToolError("aspect exploded")

        monkeypatch.setattr(_cdc.cdp_element_cloner, "extract_element_events", boom)
        tab = FakeTab(
            evaluate_result={"tag_name": "DIV"},
            cdp_responses=_cdp_responses(),
            select_result=fake_element(node_id=2),
        )
        result = await _cdc.cdp_element_cloner.extract_complete_element(
            tab, selector="#demo"
        )
        assert "error" not in result
        assert result["events"]["error"] == "aspect exploded"
        assert result["styles"]["method"] == "cdp_direct"
        assert result["structure"] == {"tag_name": "DIV"}

    async def test_a_per_node_cdp_failure_degrades_its_field_not_the_clone(self):
        """``_get_element_html`` sits beside ``_get_computed_styles_cdp`` (``{}``
        on failure) and ``_get_event_listeners_cdp`` (``[]``). All three answer a
        per-node CDP miss with an empty-ish field so the surrounding clone still
        lands; only this one has a shape that can SAY what went wrong."""

        def _describe_boom(_name):
            raise RuntimeError("node id is stale")

        responses = {**_cdp_responses(), "describe_node": _describe_boom}
        result = await _cdc.CDPElementCloner().extract_complete_element_cdp(
            responses_tab := FakeTab(cdp_responses=responses),
            "#demo",
            include_children=False,
        )
        assert responses_tab.send_calls
        assert result["element"]["html"] == {"error": "node id is stale"}
        assert result["extraction_method"] == "CDP"


# ---------------------------------------------------------------------------
# The static gate — "a second way is a defect" (CLAUDE.md convention 4).
# ---------------------------------------------------------------------------

#: ``qualified name -> why this dict survives the sweep``. Anything else that
#: returns a dict carrying an ``error`` key in these three modules is a second
#: error convention and fails the gate.
KEEP_EMBEDDED_ERROR_RECORDS = {
    "CDPElementCloner._get_element_html": (
        "sub-field degradation: the record lands in the payload's element.html, "
        "beside siblings that degrade to {} and []"
    ),
    "CDPElementCloner.extract_complete_element": (
        "per-aspect isolation (F-851): the gather composition site embeds a "
        "failed aspect so the other five still reach the caller"
    ),
}

CLONER_MODULES = (
    "cdp_element_cloner.py",
    "progressive_element_cloner.py",
    "file_based_element_cloner.py",
)


def _qualified_functions(tree: ast.AST) -> dict[str, ast.AST]:
    """``Class.func`` -> node, for every function in *tree*. Walking the module
    and each class separately would report the same function twice (once bare,
    once qualified) and let a bare name slip past the qualified allowlist."""
    found: dict[str, ast.AST] = {}

    def _visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                _visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found[f"{prefix}{child.name}"] = child

    _visit(tree, "")
    return found


def _error_dict_returns(path: Path) -> list[str]:
    """Every ``return {... "error": ... }`` in *path*, as ``Class.func:lineno``."""
    offenders: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for name, func in _qualified_functions(tree).items():
        if name in KEEP_EMBEDDED_ERROR_RECORDS:
            continue
        for node in ast.walk(func):
            if not (isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)):
                continue
            keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
            if "error" in keys:
                offenders.append(f"{path.name}::{name}:{node.lineno}")
    return offenders


class TestNoSecondErrorShape:
    def test_the_cloner_subsystem_has_no_unnamed_error_dict_return(self):
        root = Path(inspect.getfile(_cdc)).parent
        offenders = sorted(
            o for m in CLONER_MODULES for o in _error_dict_returns(root / m)
        )
        assert offenders == [], (
            "The cloner subsystem reports failure by raising ToolError "
            "(CLAUDE.md convention 2). A returned {'error': ...} is a second "
            "convention unless it is an embedded payload record named in "
            f"KEEP_EMBEDDED_ERROR_RECORDS. Offenders: {offenders}"
        )

    def test_the_keep_list_still_describes_real_functions(self):
        """A stale allowlist entry would silently re-open the hole it names."""
        root = Path(inspect.getfile(_cdc)).parent
        seen: set[str] = set()
        for module in CLONER_MODULES:
            tree = ast.parse((root / module).read_text(encoding="utf-8"))
            seen |= set(_qualified_functions(tree))
        assert set(KEEP_EMBEDDED_ERROR_RECORDS) <= seen
