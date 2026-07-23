"""RELEASE-FIX-A C2/C3 regression tests for the CDP element cloner engine.

These pin two Tier-A defects that made a legal call return a generic
``{"error": ...}`` failure:

* **C2 (A5)** — ``extract_element_styles`` bound ``matched_styles`` only inside
  ``if include_css_rules:``, so the legal combo
  ``include_css_rules=False, include_pseudo=True`` raised ``NameError`` (caught
  and reported as ``CDP extraction failed: …``).
* **C3 (A6)** — the ``$SELECTOR`` placeholder was substituted raw into JS
  templates that wrap it in quotes, so a selector like ``input[name="email"]``
  produced invalid JS. The fix JSON-encodes the placeholder at the Python
  substitution site.

Uses the hermetic ``fakes.FakeTab`` harness (``evaluate``/``send`` are canned;
no real Chrome).
"""

import json
from types import SimpleNamespace

from fakes import FakeTab
from stealth_chrome_devtools_mcp.embedded.cdp_element_cloner import (
    cdp_element_cloner,
)


def _pseudo_matched_styles():
    """A ``get_matched_styles_for_node`` tuple with a populated pseudo entry
    (index 3) so the pseudo branch has something to emit."""
    pseudo_match = SimpleNamespace(
        pseudo_type=SimpleNamespace(value="before"),
        matches=[object(), object()],
    )
    # Positional CDP tuple: inline, attributes, matched rules, pseudo, inherited.
    return [None, None, [], [pseudo_match], []]


def _styles_cdp_responses():
    return {
        "enable": None,
        "get_computed_style_for_node": [
            SimpleNamespace(name="color", value="rgb(0, 0, 0)"),
        ],
        "get_matched_styles_for_node": _pseudo_matched_styles(),
    }


class TestExtractStylesPseudoWithoutCssRules:
    async def test_extract_styles_pseudo_without_css_rules(self):
        # The legal combo the defect crashed on: pseudo requested, css_rules not.
        tab = FakeTab(
            cdp_responses=_styles_cdp_responses(),
            select_result=SimpleNamespace(node_id=2),
        )
        result = await cdp_element_cloner.extract_element_styles(
            tab,
            selector="#demo",
            include_css_rules=False,
            include_pseudo=True,
        )
        # Before the fix the pseudo tuple was never fetched, so the broad except
        # reported a generic CDP-extraction failure instead of the pseudo data.
        assert "error" not in result
        assert result["method"] == "cdp_direct"
        assert result["pseudo_elements"] == {"before": {"matches": 2}}
        # css_rules were NOT requested, so that key must be absent.
        assert "css_rules" not in result


class TestSelectorSubstitutionEncoding:
    def test_selector_with_double_quote_produces_valid_js(self):
        # A quoted-attribute selector must survive substitution into the
        # double-quoted template `const selector = "$SELECTOR$";`. Before the
        # fix the raw selector was embedded, producing invalid JS
        # (`const selector = "input[name="email"]";`).
        selector = 'input[name="email"]'
        js = cdp_element_cloner._load_js_file("extract_styles.js", selector, {})
        expected = f"const selector = {json.dumps(selector)};"
        assert expected in js

    def test_single_quoted_template_selector_is_encoded(self):
        # extract_assets.js wraps the placeholder in single quotes
        # (`})('$SELECTOR', {`); json.dumps supplies its own double quotes.
        selector = "a[href='/x']"
        js = cdp_element_cloner._encode_into("})('$SELECTOR', {", "SELECTOR", selector)
        assert js == "})(" + json.dumps(selector) + ", {"

    def test_plain_selector_is_byte_identical_to_prior_output(self):
        # For a plain selector json.dumps yields the same double-quoted literal
        # the template already had, so the emitted JS is unchanged.
        js = cdp_element_cloner._encode_into(
            'const selector = "$SELECTOR$";', "SELECTOR", "div.foo"
        )
        assert js == 'const selector = "div.foo";'
