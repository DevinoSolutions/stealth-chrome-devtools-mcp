"""M14 S8 — doc-claim accuracy harness (plan_M14 §4 / §5.3 / §5.4).

The single failure mode of the M14 docs is *docs that lie about the tree*. This
harness is the guard: it fails loudly if a root doc names a module, env var, CLI
verb, or load-bearing symbol the tree does not have, if a tombstoned module comes
back, or if the documented tool count drifts from the live registry. It keeps
DESIGN / CLAUDE / RUNBOOK / CONTRIBUTING + README honest against the code, in CI.

It deliberately does NOT assert the F-403 "uv run fails on the &-path" claim: that
is true only in a checkout whose path contains spaces/`&` (the dev checkout), not on
CI's clean path, so it is verified by the Stage-4 executor in the real checkout, not
pinned here (pinning it would fail on CI).
"""

import re
from pathlib import Path
from typing import ClassVar

import pytest

from stealth_chrome_devtools_mcp import cli
from stealth_chrome_devtools_mcp.embedded import server, tool_registry
from stealth_chrome_devtools_mcp.settings import Settings

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "src" / "stealth_chrome_devtools_mcp"
DOCS = ["README.md", "DESIGN.md", "CLAUDE.md", "RUNBOOK.md", "CONTRIBUTING.md"]


def _doc_text() -> str:
    return "\n".join((REPO / d).read_text(encoding="utf-8") for d in DOCS)


class TestDocFilesPresent:
    def test_all_root_docs_exist(self):
        for d in DOCS:
            assert (REPO / d).is_file(), f"{d} is missing"


class TestDocumentedEnvVars:
    # STEALTH_MCP_SESSION_STORAGE_CAP_GB is intentionally documented as the
    # RETIRED pre-A1 name (the README/RUNBOOK migration notes); it is the one
    # STEALTH_MCP_* token the docs may name that is no longer a live field.
    RETIRED: ClassVar[set[str]] = {"STEALTH_MCP_SESSION_STORAGE_CAP_GB"}

    def test_every_documented_stealth_env_var_is_real(self):
        known = Settings._known_env_names()  # upper-cased, incl. legacy aliases
        mentioned = set(re.findall(r"STEALTH_MCP_[A-Z0-9_]+", _doc_text()))
        assert mentioned, "expected the docs to mention STEALTH_MCP_* env vars"
        for name in sorted(mentioned):
            assert name in known or name in self.RETIRED, (
                f"{name} is documented but is not a Settings env var "
                "(rename drift? add a typed field or fix the doc)"
            )

    def test_a1_renamed_cap_var_is_documented(self):
        text = _doc_text()
        assert "STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB" in text
        # the new name must be a live field
        assert (
            "STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB" in Settings._known_env_names()
        )


class TestDocumentedCliVerbs:
    VERBS: ClassVar[list[str]] = [
        "status",
        "profiles",
        "cleanup",
        "doctor",
        "stop",
        "restart",
        "kill-orphans",
        "serve",
    ]

    def test_verbs_exist_and_are_documented(self):
        text = _doc_text()
        for verb in self.VERBS:
            assert verb in cli._DISPATCH, f"{verb} not in cli dispatch"
            assert verb in text, f"{verb} not documented in the root docs"

    def test_renamed_flag_present_old_absent_in_cli(self):
        parser = cli.build_parser()
        ns = parser.parse_args(["cleanup", "--browser-session-cap-gb", "1.0"])
        assert ns.browser_session_cap_gb == 1.0
        with pytest.raises(SystemExit):  # old flag no longer recognized
            parser.parse_args(["cleanup", "--session-cap-gb", "1.0"])


class TestNavMapModules:
    # Every embedded module the CLAUDE.md nav map points at must exist.
    LIVE_EMBEDDED: ClassVar[list[str]] = [
        "browser_manager",
        "singleton",
        "tool_registry",
        "tool_errors",
        "logging_setup",
        "process_cleanup",
        "models",
        "platform_utils",
        "cdp_element_cloner",
        "file_based_element_cloner",
        "progressive_element_cloner",
        "clone_storage",
        "network_interceptor",
        "dynamic_hook_system",
        "dynamic_hook_ai_interface",
        "hook_learning_system",
        "cdp_function_executor",
        "response_handler",
        "in_memory_storage",
        "debug_logger",
        "element_resolution",
    ]
    LIVE_TOPLEVEL: ClassVar[list[str]] = [
        "cli",
        "server",
        "settings",
        "observability",
        "__main__",
    ]
    # Tombstones: the docs say these are GONE; if one comes back the tombstone lies.
    TOMBSTONES: ClassVar[list[str]] = [
        "embedded/element_cloner.py",
        "embedded/comprehensive_element_cloner.py",
        "embedded/persistent_storage.py",
        "embedded/response_stage_hooks.py",
        "env_utils.py",
    ]

    def test_live_modules_exist(self):
        for name in self.LIVE_EMBEDDED:
            assert (PKG / "embedded" / f"{name}.py").is_file(), name
        for name in self.LIVE_TOPLEVEL:
            assert (PKG / f"{name}.py").is_file(), name
        # the browser-side JS payload dir the cloner engine loads from
        assert (PKG / "embedded" / "js").is_dir()

    def test_tombstoned_modules_are_gone(self):
        for rel in self.TOMBSTONES:
            assert not (PKG / rel).exists(), f"{rel} is tombstoned in docs but exists"
            assert not (PKG / "embedded" / rel).exists()


class TestLoadBearingSymbols:
    """The specific symbols the docs lean on, by the module/attr the doc names."""

    def test_symbols_resolve(self):
        import importlib

        expected = {
            "embedded.singleton": [
                "_backend_http_ready",
                "_probe_backend_status",
                "_source_fingerprint",
                "_select_backend_port",
                "DEFAULT_PORT",
                "run_stdio_proxy",
                "stop_backend",
                "restart_backend",
            ],
            "embedded.tool_registry": ["SECTION_TOOLS", "ToolRegistry"],
            "embedded.tool_errors": [
                "ToolError",
                "InstanceNotFoundError",
                "_require_tab",
                "_require_browser",
            ],
            "embedded.logging_setup": [
                "resolve_log_dir",
                "with_correlation_id",
                "CorrelationIdFilter",
            ],
            "embedded.cdp_element_cloner": ["CDPElementCloner", "cdp_element_cloner"],
            "embedded.file_based_element_cloner": ["FileBasedElementCloner"],
            "embedded.clone_storage": ["browser_session_storage_cap_bytes"],
            "embedded.network_interceptor": ["NetworkInterceptor"],
            "embedded.process_cleanup": ["ProcessCleanup"],
            "embedded.in_memory_storage": ["InMemoryStorage", "in_memory_storage"],
            "settings": ["Settings", "get_settings"],
        }
        for mod_suffix, attrs in expected.items():
            mod = importlib.import_module(f"stealth_chrome_devtools_mcp.{mod_suffix}")
            for attr in attrs:
                assert hasattr(mod, attr), (
                    f"{mod_suffix}.{attr} named in docs but missing"
                )

    def test_process_cleanup_activation_seam(self):
        from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup

        assert hasattr(ProcessCleanup, "activate")
        assert hasattr(ProcessCleanup, "recover_orphans")


class TestDocumentedToolCount:
    def test_docs_say_94_and_registry_agrees(self):
        registry_total = sum(len(v) for v in server.SECTION_TOOLS.values())
        assert registry_total == 94
        assert tool_registry.SECTION_TOOLS is server.SECTION_TOOLS
        # every root doc that cites a tool count cites 94 (no 90/96/97/99 left)
        text = _doc_text()
        for stale in ("90 tools", "96 tools", "97 tools", "99 tools"):
            assert stale not in text, f"stale tool count '{stale}' still in docs"
        assert "94 tools" in text
