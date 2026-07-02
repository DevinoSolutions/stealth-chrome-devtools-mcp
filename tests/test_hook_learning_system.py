"""Behavioral tests for HookLearningSystem — the AI-facing hook docs + validator.

The documentation getters are contract surfaces the MCP tools return verbatim,
so they must stay well-formed. validate_hook_function is real AST analysis: it
must flag a missing entrypoint, a wrong signature, and dangerous calls, while
letting a well-formed hook through.
"""

from hook_learning_system import HookLearningSystem, hook_learning_system


class TestDocumentationGetters:
    def test_request_object_documentation_shape(self):
        doc = HookLearningSystem.get_request_object_documentation()
        assert "url" in doc["request_object"]["fields"]
        assert set(doc["hook_action"]["actions"]) >= {"continue", "block", "redirect",
                                                       "modify", "fulfill"}

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
