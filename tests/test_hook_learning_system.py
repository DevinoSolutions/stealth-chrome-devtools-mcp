"""Behavioral tests for HookLearningSystem — the AI-facing hook docs + validator.

The documentation getters are contract surfaces the MCP tools return verbatim,
so they must stay well-formed. validate_hook_function is real AST analysis: it
must flag a missing entrypoint, a wrong signature, and dangerous calls, while
letting a well-formed hook through.
"""

import ast

import pytest

from stealth_chrome_devtools_mcp.embedded import hook_learning_system as hls
from stealth_chrome_devtools_mcp.embedded.hook_learning_system import (
    HookLearningSystem,
    hook_learning_system,
)


class TestDocumentationGetters:
    def test_request_object_documentation_shape(self):
        doc = HookLearningSystem.get_request_object_documentation()
        assert "url" in doc["request_object"]["fields"]
        assert set(doc["hook_action"]["actions"]) >= {
            "continue",
            "block",
            "redirect",
            "modify",
            "fulfill",
        }

    def test_hook_examples_are_wellformed(self):
        examples = HookLearningSystem.get_hook_examples()
        assert len(examples) >= 10
        for ex in examples:
            assert ex["name"] and ex["description"]
            assert "def process_request(request)" in ex["function"]

    def test_requirements_documentation_shape(self):
        doc = HookLearningSystem.get_requirements_documentation()
        assert "url_pattern" in doc["requirements"]["fields"]
        assert isinstance(doc["best_practices"], list) and doc["best_practices"]

    def test_common_patterns_shape(self):
        patterns = HookLearningSystem.get_common_patterns()
        assert patterns and all("action" in p and "requirements" in p for p in patterns)

    def test_global_instance_exposed(self):
        assert isinstance(hook_learning_system, HookLearningSystem)


class TestValidateHookFunction:
    def test_valid_function_passes(self):
        result = HookLearningSystem.validate_hook_function(
            "def process_request(request):\n    return None\n"
        )
        assert result["valid"] is True
        assert result["issues"] == []

    def test_missing_entrypoint_is_issue(self):
        result = HookLearningSystem.validate_hook_function(
            "def other(x):\n    return x\n"
        )
        assert result["valid"] is False
        assert any("process_request" in i for i in result["issues"])

    def test_wrong_parameter_count_is_issue(self):
        result = HookLearningSystem.validate_hook_function(
            "def process_request(a, b):\n    return None\n"
        )
        assert result["valid"] is False
        assert any("exactly one parameter" in i for i in result["issues"])

    def test_misnamed_parameter_is_warning_not_issue(self):
        result = HookLearningSystem.validate_hook_function(
            "def process_request(req):\n    return None\n"
        )
        assert result["valid"] is True
        assert any("named 'request'" in w for w in result["warnings"])

    def test_dangerous_call_is_issue(self):
        result = HookLearningSystem.validate_hook_function(
            "def process_request(request):\n    return eval('1+1')\n"
        )
        assert result["valid"] is False
        assert any("Dangerous function call: eval" in i for i in result["issues"])

    def test_import_is_warning(self):
        result = HookLearningSystem.validate_hook_function(
            "import os\ndef process_request(request):\n    return None\n"
        )
        assert result["valid"] is True
        assert any("Imports may not work" in w for w in result["warnings"])

    def test_syntax_error_is_reported(self):
        result = HookLearningSystem.validate_hook_function(
            "def process_request(request)\n    return None\n"  # missing colon
        )
        assert result["valid"] is False
        assert any("Syntax error" in i for i in result["issues"])

    @pytest.mark.parametrize(
        "code",
        [
            pytest.param("x = 1" + chr(0), id="nul"),
            pytest.param("x = '" + chr(0xD800) + "'", id="lone-surrogate"),
            pytest.param("-" * 100_000 + "1", id="unary-minus-chain"),
            pytest.param("not " * 100_000 + "1", id="not-chain"),
            pytest.param("+".join(["1"] * 100_000), id="long-binop-chain"),
            pytest.param("x" + ".a" * 100_000, id="long-attribute-chain"),
            pytest.param("(" * 300, id="nested-parens"),
        ],
    )
    def test_text_the_parser_cannot_take_is_reported_not_raised(self, code):
        # T-9431: measured on 3.11-3.14, the caller's text alone makes ast.parse
        # raise SyntaxError, ValueError, MemoryError or RecursionError.
        result = HookLearningSystem.validate_hook_function(code)
        assert result["valid"] is False
        assert result["warnings"] == []
        assert len(result["issues"]) == 1
        assert result["issues"][0].startswith("Syntax error: ")
        assert len(result["issues"][0]) > len("Syntax error: ")

    @pytest.mark.parametrize("error", [ValueError, RecursionError, MemoryError])
    def test_the_parse_guard_does_not_reach_the_walk(self, monkeypatch, error):
        # The caller-text types are caught around the parse ONLY; the same types
        # raised by the analysis after it are our bug.
        class BrokenAst:
            def __getattr__(self, name):
                return getattr(ast, name)

            def walk(self, _tree):
                raise error("walk bug")

        monkeypatch.setattr(hls, "ast", BrokenAst())

        with pytest.raises(error, match="walk bug"):
            HookLearningSystem.validate_hook_function(
                "def process_request(request):\n    return None\n"
            )

    def test_a_failure_in_our_own_analysis_propagates(self, monkeypatch):
        # T-9431: only the caller's unparseable text may become an issue. A bug in
        # the validator itself must surface, not read as an issue (a CallerError
        # Sentry drops on purpose).
        class BrokenAst:
            # Only this module's name is rebound: patching the real ``ast.parse``
            # would also break pytest's and traceback's own parsing.
            def __getattr__(self, name):
                return getattr(ast, name)

            def parse(self, _code):
                raise RuntimeError("validator bug")

        monkeypatch.setattr(hls, "ast", BrokenAst())

        with pytest.raises(RuntimeError, match="validator bug"):
            HookLearningSystem.validate_hook_function(
                "def process_request(request):\n    return None\n"
            )
