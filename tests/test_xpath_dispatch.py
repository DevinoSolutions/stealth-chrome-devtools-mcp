"""Pins for F-831 (GitHub #15): XPath dispatch lives in element_resolution.

Seven element-interaction tools advertise "CSS selector or XPath", but only
``query_elements`` honoured one -- via its own ``selector.startswith("//")``
branch calling ``tab.xpath`` directly, INSIDE the tool path. That branch was a
second way to resolve a selector: it bypassed the one home
(``element_resolution``) and therefore the -32000/handler-race recovery every
CSS selector gets, and the other six tools silently resolved an XPath as CSS
and failed.

These pin the fix: the detection contract, that both resolution shapes dispatch
to the nodriver/CDP XPath path, that the recovery wraps it exactly like CSS,
that CSS is untouched, and that ``query_elements``' returned shape is unchanged.
Hermetic (fake Tab/Element), so the fast unit lane -- no real Chrome.
"""

import pytest
from nodriver.core.connection import ProtocolException

from stealth_chrome_devtools_mcp.embedded import element_resolution
from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler
from stealth_chrome_devtools_mcp.embedded.element_resolution import (
    _MAX_RESOLVES,
    is_xpath,
    query_selector_all,
    resolve_element,
    resolve_elements,
    xpath_expression,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch):
    # Zero the recovery backoff so the unit lane stays fast while the real
    # sleep(0) code path still runs. Mirrors tests/test_element_resolution.py.
    monkeypatch.setattr(element_resolution, "_SETTLE_SECONDS", 0.0)


def _stale():
    return ProtocolException(
        {"message": "Could not find node with given id", "code": -32000}
    )


def _pop(effects):
    effect = effects.pop(0)
    if isinstance(effect, Exception):
        raise effect
    return effect


class _FakeElement:
    """Minimal nodriver-Element stand-in for the tools' post-resolve work."""

    def __init__(self, tag="div", text="hello", attrs=None):
        self.tag_name = tag
        self.text = text
        self.text_all = text
        self.attrs = attrs or {}
        self.children = []

    async def update(self):
        return None

    async def get_position(self):
        return None

    async def scroll_into_view(self):
        return None

    async def mouse_click(self):
        return None

    async def click(self):
        return None


class _FakeTab:
    """Records which resolution surface a selector reached.

    ``xpath``/``select``/``select_all``/``send`` each replay an effects list
    (raise it if it is an exception, else return it), the idiom used by
    tests/test_element_resolution.py.
    """

    def __init__(self, *, xpath=None, select=None, select_all=None, send=None):
        self._xpath = list(xpath or [])
        self._select = list(select or [])
        self._select_all = list(select_all or [])
        self._send = list(send or [])
        self.xpath_calls = []
        self.select_calls = []
        self.select_all_calls = []
        self.sent = []

    async def xpath(self, expression, timeout=None):
        self.xpath_calls.append(expression)
        return _pop(self._xpath)

    async def select(self, selector, timeout=None):
        self.select_calls.append(selector)
        return _pop(self._select)

    async def select_all(self, selector):
        self.select_all_calls.append(selector)
        return _pop(self._select_all)

    async def send(self, cmd):
        self.sent.append(cmd)
        return _pop(self._send)


# --- (f) the detection contract ---------------------------------------------

XPATH_SELECTORS = [
    "//a",
    "/html/body",
    "(//div)[1]",
    "xpath=//button[@id='go']",
    "//script[not(@src)]",
    "  //a  ",
]

CSS_SELECTORS = [
    "#id",
    ".cls",
    "div > a",
    "a[href]",
    "input[type='file']",
    "button:not(.x)",
    "*",
]


@pytest.mark.parametrize("selector", XPATH_SELECTORS)
def test_xpath_selectors_are_detected_as_xpath(selector):
    assert is_xpath(selector) is True


@pytest.mark.parametrize("selector", CSS_SELECTORS)
def test_css_selectors_are_not_detected_as_xpath(selector):
    assert is_xpath(selector) is False
    assert xpath_expression(selector) is None


def test_the_legacy_double_slash_form_still_reaches_the_xpath_path():
    # The deleted query_elements branch keyed on exactly this. Whatever it
    # accepted, the shared contract must keep accepting.
    assert xpath_expression("//div[@class='x']") == "//div[@class='x']"


def test_the_explicit_prefix_is_stripped_from_the_expression():
    assert xpath_expression("xpath=//a") == "//a"
    assert xpath_expression("XPath= //a ") == "//a"


def test_surrounding_whitespace_is_stripped_from_the_expression():
    assert xpath_expression("  //a  ") == "//a"


def test_a_prefix_with_no_expression_is_a_tool_error():
    # 'xpath=' alone is not a CSS selector either; say so instead of resolving
    # the literal string and reporting a confusing not-found.
    with pytest.raises(ToolError, match="XPath"):
        xpath_expression("xpath=   ")


def test_a_relative_dot_path_stays_css():
    # '.foo' is a class selector far more often than a relative XPath; the
    # explicit prefix is how a caller asks for the latter.
    assert is_xpath("./div") is False
    assert is_xpath("xpath=./div") is True


# --- (a) an XPath resolves through the shared path --------------------------


@pytest.mark.asyncio
async def test_resolve_elements_dispatches_xpath_to_the_xpath_path():
    nodes = [_FakeElement(), _FakeElement()]
    tab = _FakeTab(xpath=[nodes])
    assert await resolve_elements(tab, "//div") == nodes
    assert tab.xpath_calls == ["//div"]
    assert tab.select_all_calls == []  # never resolved as CSS


@pytest.mark.asyncio
async def test_resolve_element_returns_the_first_xpath_match():
    first = _FakeElement(tag="a")
    tab = _FakeTab(xpath=[[first, _FakeElement()]])
    assert await resolve_element(tab, "xpath=(//a)[1]") is first
    assert tab.xpath_calls == ["(//a)[1]"]  # prefix stripped before dispatch
    assert tab.select_calls == []


@pytest.mark.asyncio
async def test_resolve_element_returns_none_on_a_genuine_xpath_zero_match():
    tab = _FakeTab(xpath=[[]])
    assert await resolve_element(tab, "//nope") is None


@pytest.mark.asyncio
async def test_nodriver_optional_nones_are_dropped_from_xpath_matches():
    # nodriver types Tab.xpath as List[Optional[Element]]; callers of this
    # module must never receive a None inside the match list.
    kept = _FakeElement()
    tab = _FakeTab(xpath=[[None, kept, None]])
    assert await resolve_elements(tab, "//div") == [kept]


@pytest.mark.asyncio
async def test_query_selector_all_dispatches_xpath_to_the_cdp_search_path():
    # DOM.performSearch + DOM.getSearchResults is the CDP-native XPath node-id
    # query (what nodriver's own Tab.xpath is built on). Sends: get_document,
    # perform_search -> (id, count), get_search_results, discard_search_results.
    tab = _FakeTab(send=[object(), ("search-1", 2), [10, 11], None])
    assert await query_selector_all(tab, "//div") == [10, 11]
    assert len(tab.sent) == 4


@pytest.mark.asyncio
async def test_query_selector_all_xpath_zero_match_returns_empty():
    tab = _FakeTab(send=[object(), ("search-1", 0), None])
    assert await query_selector_all(tab, "//nope") == []


# --- (e) the race classifier wraps the xpath path too -----------------------


@pytest.mark.asyncio
async def test_a_stale_node_on_the_xpath_path_re_resolves():
    nodes = [_FakeElement()]
    tab = _FakeTab(xpath=[_stale(), nodes])
    assert await resolve_elements(tab, "//div") == nodes
    assert tab.xpath_calls == ["//div", "//div"]  # re-resolved on a fresh doc


@pytest.mark.asyncio
async def test_a_persistent_stale_node_on_the_xpath_path_is_bounded():
    tab = _FakeTab(xpath=[_stale() for _ in range(_MAX_RESOLVES)])
    with pytest.raises(ProtocolException, match="Could not find node with given id"):
        await resolve_element(tab, "//div")
    assert len(tab.xpath_calls) == _MAX_RESOLVES  # same bound as the CSS path


@pytest.mark.asyncio
async def test_an_unrelated_error_on_the_xpath_path_is_never_retried():
    tab = _FakeTab(xpath=[RuntimeError("boom")])
    with pytest.raises(RuntimeError, match="boom"):
        await resolve_elements(tab, "//div")
    assert len(tab.xpath_calls) == 1


# --- (d) CSS is unaffected ---------------------------------------------------


@pytest.mark.asyncio
async def test_css_still_resolves_through_select_and_select_all():
    sentinel = _FakeElement()
    tab = _FakeTab(select=[sentinel], select_all=[[sentinel]])
    assert await resolve_element(tab, "#btn") is sentinel
    assert await resolve_elements(tab, ".row") == [sentinel]
    assert tab.xpath_calls == []


@pytest.mark.asyncio
async def test_css_query_selector_all_still_uses_query_selector_all():
    class _Doc:
        node_id = 1

    tab = _FakeTab(send=[_Doc(), [7]])
    assert await query_selector_all(tab, ".x") == [7]
    assert len(tab.sent) == 2  # get_document + query_selector_all, no search


# --- (b) query_elements' returned shape is unchanged ------------------------

ELEMENT_INFO_KEYS = {
    "selector",
    "tag_name",
    "text",
    "attributes",
    "is_visible",
    "is_clickable",
    "bounding_box",
    "children_count",
}


@pytest.mark.asyncio
async def test_query_elements_xpath_keeps_the_css_result_shape():
    element = _FakeElement(tag="a", text="Go", attrs={"href": "/x"})
    css_tab = _FakeTab(select_all=[[element]])
    xpath_tab = _FakeTab(xpath=[[element]])

    css = await DOMHandler.query_elements(css_tab, "a.link", visible_only=False)
    xpath = await DOMHandler.query_elements(xpath_tab, "//a", visible_only=False)

    assert len(xpath) == len(css) == 1
    css_dict = css[0].model_dump()
    xpath_dict = xpath[0].model_dump()
    assert set(xpath_dict) == set(css_dict) == ELEMENT_INFO_KEYS
    # Identical but for the echoed selector, which is the caller's own string.
    assert css_dict.pop("selector") == "a.link"
    assert xpath_dict.pop("selector") == "//a"
    assert xpath_dict == css_dict


@pytest.mark.asyncio
async def test_query_elements_xpath_goes_through_element_resolution():
    # The deleted branch called tab.xpath from inside dom_handler; it must now
    # arrive via element_resolution, so the recovery applies.
    tab = _FakeTab(xpath=[_stale(), [_FakeElement()]])
    results = await DOMHandler.query_elements(tab, "//div", visible_only=False)
    assert len(results) == 1
    assert tab.xpath_calls == ["//div", "//div"]


@pytest.mark.asyncio
async def test_query_elements_honours_limit_and_text_filter_on_xpath():
    elements = [_FakeElement(text="keep"), _FakeElement(text="drop")]
    tab = _FakeTab(xpath=[elements])
    results = await DOMHandler.query_elements(
        tab, "//div", text_filter="keep", visible_only=False
    )
    assert [r.text for r in results] == ["keep"]


# --- (c) the other advertising tools inherit it -----------------------------


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda tab, sel: DOMHandler.get_element_state(tab, sel),
            id="get_element_state",
        ),
        pytest.param(
            lambda tab, sel: DOMHandler.wait_for_element(
                tab, sel, timeout=1000, visible=False
            ),
            id="wait_for_element",
        ),
        pytest.param(
            lambda tab, sel: DOMHandler.click_element(tab, sel),
            id="click_element",
        ),
    ],
)
@pytest.mark.parametrize("selector", ["//button", "xpath=//button"])
@pytest.mark.asyncio
async def test_advertising_tools_resolve_xpath_through_the_shared_path(call, selector):
    tab = _FakeTab(xpath=[[_FakeElement(tag="button")]])
    await call(tab, selector)
    assert tab.xpath_calls == ["//button"]
    assert tab.select_calls == []  # not silently resolved as CSS


@pytest.mark.asyncio
async def test_the_issue_15_repro_selector_clicks():
    # GitHub #15, verbatim: query_elements accepted this text() XPath and
    # click_element answered -32000 "DOM Error while querying" for the SAME
    # string, because click resolved it as CSS. One grammar, both tools.
    selector = "//*[contains(text(),'University of Ottawa')]"
    query_tab = _FakeTab(xpath=[[_FakeElement()]])
    click_tab = _FakeTab(xpath=[[_FakeElement()]])

    assert len(await DOMHandler.query_elements(query_tab, selector, visible_only=False))
    assert await DOMHandler.click_element(click_tab, selector) is True
    assert query_tab.xpath_calls == click_tab.xpath_calls == [selector]


@pytest.mark.parametrize("kwargs", [{"value": "b"}, {"index": 1}])
@pytest.mark.asyncio
async def test_select_option_acts_on_the_resolved_element_not_a_second_lookup(kwargs):
    # select_option does not advertise XPath, but it resolves through the same
    # shared path -- so its value/index arms must act on the element already
    # resolved, not re-run `document.querySelector(selector)`, which cannot
    # express an XPath and would silently no-op while still returning True.
    applied = []

    class _Select(_FakeElement):
        async def apply(self, js):
            applied.append(js)

    tab = _FakeTab(xpath=[[_Select(tag="select")]])
    assert await DOMHandler.select_option(tab, "//select", **kwargs) is True
    assert len(applied) == 1
    assert "querySelector" not in applied[0]
