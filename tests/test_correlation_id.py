"""Pinning tests for M3-5: correlation id via the section_tool chokepoint
(F-308), including the MANDATORY tools/list schema-snapshot pin.

This is the plan's highest-uncertainty step (risk #5): ``section_tool``'s
wrapper must set/reset ``correlation_id_var`` around every one of the 96
registered tool calls without FastMCP losing any tool's JSON schema. 91 of
the 96 registered functions are ``async def`` and 5 are plain ``def``
(``get_hook_documentation`` et al.), so the wrapper must preserve both.

``TestToolsListSchemaSnapshot`` pins the exact ``inputSchema``/``name`` FastMCP
produces for one representative tool per section, captured from the REAL
pre-change tree (``git log`` show this commit's parent for the capture
script) before ``section_tool`` was touched. If this test goes red, the
fallback in plan_M3 risk #5 is ``wrapper.__signature__ =
inspect.signature(func)``.
"""

import asyncio
import json
import logging

import pytest

from fakes import live_tools
from stealth_chrome_devtools_mcp.embedded.logging_setup import (
    CorrelationIdFilter,
    correlation_id_var,
    with_correlation_id,
)

# Captured from the pre-change tree: one representative tool's real FastMCP
# name + inputSchema per section (11 sections). See M3-5's commit message for
# the capture method. A structural change here means section_tool's wrapper
# altered what a real MCP client sees in tools/list.
#
# F-896 adds ONE property, ``session``, to spawn_browser — a deliberate surface
# change landing in the same PR as the parameter, per CONTRIBUTING's golden
# discipline, and the same one property that moved in the HARD
# ``tests/goldens/tool_surface.json``. Nothing else in this snapshot moves,
# which is the thing it exists to say about a change of this shape.
#
# F-897 adds ONE more, ``seed_from``, on exactly the same terms and for the
# same reason: a new session may be copied from an existing one. Again nothing
# else in this snapshot moves, and again it is the same single property that
# moved in the HARD golden.
#
# F-943 drops every ``"title"`` key. fastmcp 2.14 prunes the per-parameter titles
# pydantic generates from the parameter name ("Block Resources" for
# ``block_resources``), so this is the dependency bump, not the wrapper. Names,
# types, defaults and ``required`` are unchanged here and in the HARD golden,
# which lost exactly its 333 titles and nothing else (measured: both sides
# compared with string-valued ``title`` keys removed, 94 tools, zero diffs).
#
# F-946 adds two keys, both fastmcp 3's and both additive. Each parameter's
# ``description`` is now its line from the docstring's ``Args:`` section, which
# the tool description still carries too, and the top level says
# ``"additionalProperties": false``, which pydantic already enforced on every
# call. Stripping those two gives this snapshot as it was, and the HARD golden
# as it was across all 94 tools (264 descriptions, 94 flags, zero other diffs).
_GOLDEN_SCHEMA_JSON = r"""
{
  "browser-management": {
    "name": "spawn_browser",
    "inputSchema": {
      "additionalProperties": false,
      "properties": {
        "block_resources": {"default": null, "description": "List of resource types to block (e.g., ['image', 'font', 'stylesheet']).", "items": {"type": "string"}, "type": "array"},
        "browser_args": {"default": null, "description": "Additional browser launch args.", "items": {"type": "string"}, "type": "array"},
        "extra_headers": {"additionalProperties": {"type": "string"}, "default": null, "description": "Additional HTTP headers.", "type": "object"},
        "headless": {"default": false, "description": "Run in headless mode.", "type": "boolean"},
        "idle_timeout_seconds": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": null, "description": "Idle timeout override in seconds for automatic instance cleanup."},
        "proxy": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "description": "Proxy server URL."},
        "sandbox": {"anyOf": [{}, {"type": "null"}], "default": null, "description": "Enable browser sandbox. Accepts bool, string ('true'/'false'), int (1/0), or None for auto-detect."},
        "seed_from": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "description": "The NAME of an existing session to copy when\n``session`` names one that does not exist yet \u2014 so a new session\nstarts with that session's cookies and logins instead of the\nshared ``default`` session's. Leave UNSET for normal use; unset\nmeans ``default``, which is exactly what every session has always\nbeen seeded from.\nIt applies ONLY at creation: passing it with a ``session`` that\nalready exists is an ERROR naming where that session was actually\nseeded from, never a silent no-op and never a re-seed \u2014 opening a\nsession keeps what it holds, and overwriting a login a human typed\nby hand is not something a flag should be able to do by accident.\nIt needs a ``session`` of its own, so it is also an error with no\n``session`` or with ``session=\"default\"``.\nThe source must EXIST. It MAY be open in a browser, but only one\nTHIS backend drives \u2014 a session you spawned through this tool, the\ncommon case after ``spawn --session work --headed`` and a hand\nlogin. Then its COOKIES are read out of the running browser over\nCDP and written into the new session, and the answer says so:\n``seeded_via: \"cdp-cookies\"`` plus counts. A source held by a\nbrowser this backend does not drive (another backend's, or a Chrome\nnobody here launched) is refused BY NAME, because there is no\nconnection of ours to ask for its cookies and a file copy of a live\nprofile carries none at all \u2014 the jar is held open and skipped, and\nnothing can say afterwards what was lost.\n``default`` \u2014 the value an UNSET ``seed_from`` means \u2014 has its own\nthree outcomes, because it is the session a human logs in to and\nits window is normally still open. The copy always comes from the\nseed, a separate closed copy of it, and never from the live\ndirectory. If this backend is driving that window, the live jar is\nhanded over on top of that copy, exactly as for a named source\n(``seeded_via: \"cdp-cookies\"``) \u2014 which matters because the seed is\nNOT refreshed while ``default`` is open, so the copy alone can be\ndays old. If a Chrome we do not drive holds it, the copy happens\nanyway and nothing is refused; the answer's ``seed_changed_since``\nsays the seed is behind. The only refusal is a machine with no seed\nyet AND ``default`` open, where the only available copy would be of\nthe live directory: nothing is created, and closing that window\nonce writes the seed.\nWHAT A HAND-OFF CARRIES IS COOKIES AND NOTHING ELSE: every kind\n(session, persistent, HttpOnly, Secure, SameSite=None and\nPartitioned), and the WHOLE jar \u2014 every site that session is logged\ninto, not just the one you had in mind. It does NOT carry\n``localStorage``, ``sessionStorage``, IndexedDB, Cache Storage, any\nservice-worker registration, or saved passwords/autofill. A site\nthat keeps its token in ``localStorage`` will NOT be logged in.\nThose stores come across only from a source that is CLOSED, where\nthe file copy can read them \u2014 with the one exception that a session\ncookie is never on disk at all and so only ever arrives this way.\nIf the hand-off fails the session is still created and still works;\nthe answer then says ``seeded_via: \"copy\"`` with\n``cookie_handoff_error``.\nThe other exception is ``default`` itself, whose copyable form the\nproduct maintains separately, so seeding from it works whether or\nnot it is open (that copy can be as old as the last time ``default``\nwas closed, which the answer reports as ``seed_changed_since``).\nWhat the new session records is the source's NAME:\n``spawn_diagnostics[\"profile_selection\"][\"seeded_from\"]``, a word\nyou can pass straight back as ``session=``."},
        "session": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "description": "The NAME of a persistent browser session \u2014 THE\none documented way to ask for a profile, and the only one to use.\nLeave UNSET for normal use: an unnamed spawn gets a disposable copy\nof the shared ``default`` session and deletes it as soon as the\nbrowser closes, so you never manage or clean up sessions. Set it\nonly when the user has EXPLICITLY asked to keep a login: a named\nsession is NOT auto-cleaned and persists on disk indefinitely, so\ntreat creating one as a deliberate, space-consuming action, and do\nnot invent names. ``session=\"default\"`` opens the SHARED session\nitself \u2014 the profile every new session is seeded from and the one a\nhuman logs in to; it is reserved and is never a session of your own.\nA session is a NAME, not a path: pass ``user_data_dir`` to open a\ndirectory by path.\nA named session is never deleted by close_instance, by the clone GC,\nby `cleanup --apply` or by `kill-orphans`, and since F-888 its\nBROWSER survives the backend too: a\nbackend that stops, restarts, heals or crashes leaves such a browser\nRUNNING, and it is RE-ATTACHED to over CDP rather than replaced, so a\nhuman's logged-in session is not lost. Two paths reach it and you need\nneither by name: a new backend adopts the browsers it finds recorded\nat its own startup (same instance_id as before), and spawning with a\nsession a live browser still holds re-attaches to THAT browser\ninstead of walking to a sibling directory \u2014 and since F-931 so does a\nspawn naming NOTHING, which lands on ``default``. Either way the answer\ncarries ``spawn_diagnostics[\"reattached\"]: true`` plus the holder's\npid, and the page is the one that was already open \u2014 not a fresh tab\non the same cookies. A spawn naming nothing is never handed a browser\nTHIS backend already drives, though: it asked for a browser of its\nown, so it gets a new session copied from the seed, with that\nbrowser's live cookies handed over. So to recover a logged-in browser whose backend\ndied, just spawn with the same session. On that path the\narguments that describe a LAUNCH cannot apply to a browser already\nrunning: headless, user_agent, viewport, proxy, browser_args,\ntimezone_id and extra_headers are IGNORED rather than refused, and the\nones you passed are listed in\n``spawn_diagnostics[\"ignored_spawn_args\"]`` \u2014 refusing over a viewport\nwould send the spawn to a different directory and lose the login.\n``block_resources`` IS applied. What the dead backend held and nobody\ncan read back off a running browser is named in\n``spawn_diagnostics[\"not_restored\"]``. The one case that refuses is a\nbrowser this backend does not DRIVE \u2014 another backend's Chrome, or\nthe human's own \u2014 because its cookies can only come over a CDP\nconnection of ours and there is none: a copy of a profile Chrome is\nwriting to carries none of them. Since F-915 that REFUSES the whole\nspawn rather than starting a new browser beside it. Nothing is\ncreated, that browser is left running and untouched, and the error\nnames the holder and both ways on \u2014 close it (``stealthy close``, or\nstop the backend that owns it: RUNBOOK, \"Recover a stranded login\")\nand spawn again, or pass a free ``session`` name for a new session\nof your own."},
        "timezone_id": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "description": "IANA timezone ID applied via CDP timezone override."},
        "user_agent": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "description": "Custom user agent string."},
        "user_data_dir": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "description": "DEPRECATED, and the ONE thing it still\nbuys you is an absolute PATH, which ``session`` refuses. For a name\nit resolves to exactly the same profile ``session`` does \u2014 it is the\nsame argument under the older word, not a second one \u2014 so passing\nboth with DIFFERENT values is an error rather than a precedence you\ncannot see. Everything said about ``session`` above applies to it."},
        "viewport_height": {"default": 1080, "description": "Requested browser WINDOW height in pixels, same\nbest-effort clamping as viewport_width.", "type": "integer"},
        "viewport_width": {"default": 1920, "description": "Requested browser WINDOW width in pixels (outer, not\nthe CSS viewport). Best-effort: a headed window is clamped to the work\narea of the LAUNCHING context's desktop \u2014 the user's monitor only when\nthe backend runs on it (F-808), not the caller's screen \u2014 so a request\nlarger than that desktop lands smaller.", "type": "integer"}
      },
      "type": "object"
    }
  },
  "cdp-functions": {
    "name": "list_cdp_commands",
    "inputSchema": {
      "additionalProperties": false,
      "properties": {},
      "type": "object"
    }
  },
  "cookies-storage": {
    "name": "get_cookies",
    "inputSchema": {
      "additionalProperties": false,
      "properties": {
        "instance_id": {"description": "Browser instance ID.", "type": "string"},
        "urls": {"anyOf": [{"items": {"type": "string"}, "type": "array"}, {"type": "null"}], "default": null, "description": "Optional list of URLs to get cookies for."}
      },
      "required": ["instance_id"],
      "type": "object"
    }
  },
  "debugging": {
    "name": "get_debug_view",
    "inputSchema": {
      "additionalProperties": false,
      "properties": {
        "include_all": {"default": false, "description": "Include all logs regardless of limits (default: False).", "type": "boolean"},
        "max_errors": {"default": 50, "description": "Maximum number of errors to include (default: 50).", "type": "integer"},
        "max_info": {"default": 50, "description": "Maximum number of info logs to include (default: 50).", "type": "integer"},
        "max_warnings": {"default": 50, "description": "Maximum number of warnings to include (default: 50).", "type": "integer"}
      },
      "type": "object"
    }
  },
  "dynamic-hooks": {
    "name": "create_dynamic_hook",
    "inputSchema": {
      "additionalProperties": false,
      "properties": {
        "function_code": {"description": "Python function code that processes requests (must define process_request(request))", "type": "string"},
        "instance_ids": {"anyOf": [{"items": {"type": "string"}, "type": "array"}, {"type": "null"}], "default": null, "description": "Browser instances to apply hook to (all if None)"},
        "name": {"description": "Human-readable hook name", "type": "string"},
        "priority": {"default": 100, "description": "Hook priority (lower = higher priority)", "type": "integer"},
        "requirements": {"additionalProperties": true, "description": "Matching criteria (url_pattern, method, resource_type, custom_condition)", "type": "object"}
      },
      "required": ["name", "requirements", "function_code"],
      "type": "object"
    }
  },
  "element-extraction": {
    "name": "extract_element_styles",
    "inputSchema": {
      "additionalProperties": false,
      "properties": {
        "include_computed": {"default": true, "description": "Include computed styles.", "type": "boolean"},
        "include_css_rules": {"default": true, "description": "Include matching CSS rules.", "type": "boolean"},
        "include_inheritance": {"default": false, "description": "Include style inheritance chain.", "type": "boolean"},
        "include_pseudo": {"default": true, "description": "Include pseudo-element styles (::before, ::after).", "type": "boolean"},
        "instance_id": {"description": "Browser instance ID.", "type": "string"},
        "selector": {"description": "CSS selector for the element.", "type": "string"}
      },
      "required": ["instance_id", "selector"],
      "type": "object"
    }
  },
  "element-interaction": {
    "name": "query_elements",
    "inputSchema": {
      "additionalProperties": false,
      "properties": {
        "instance_id": {"description": "Browser instance ID.", "type": "string"},
        "limit": {"anyOf": [{}, {"type": "null"}], "default": null, "description": "Maximum number of elements to return."},
        "selector": {"description": "CSS selector or XPath (starts with '//').", "type": "string"},
        "text_filter": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "description": "Filter by text content."},
        "visible_only": {"default": true, "description": "Only return visible elements.", "type": "boolean"}
      },
      "required": ["instance_id", "selector"],
      "type": "object"
    }
  },
  "file-extraction": {
    "name": "clone_element_to_file",
    "inputSchema": {
      "additionalProperties": false,
      "properties": {
        "extraction_options": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "description": "JSON string with extraction options."},
        "instance_id": {"description": "Browser instance ID.", "type": "string"},
        "selector": {"description": "CSS selector for the element.", "type": "string"}
      },
      "required": ["instance_id", "selector"],
      "type": "object"
    }
  },
  "network-debugging": {
    "name": "list_network_requests",
    "inputSchema": {
      "additionalProperties": false,
      "properties": {
        "filter_type": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "description": "Filter by CDP resource type, matched\ncase-insensitively (e.g., 'document', 'image', 'script', 'xhr')."},
        "instance_id": {"description": "Browser instance ID.", "type": "string"}
      },
      "required": ["instance_id"],
      "type": "object"
    }
  },
  "progressive-cloning": {
    "name": "clone_element_progressive",
    "inputSchema": {
      "additionalProperties": false,
      "properties": {
        "include_children": {"default": true, "description": "Whether to extract child elements.", "type": "boolean"},
        "instance_id": {"description": "Browser instance ID.", "type": "string"},
        "selector": {"description": "CSS selector for the element.", "type": "string"}
      },
      "required": ["instance_id", "selector"],
      "type": "object"
    }
  },
  "tabs": {
    "name": "list_tabs",
    "inputSchema": {
      "additionalProperties": false,
      "properties": {
        "instance_id": {"description": "Browser instance ID.", "type": "string"}
      },
      "required": ["instance_id"],
      "type": "object"
    }
  }
}
"""


@pytest.fixture()
def captured_backend_records():
    records = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("stealth.backend")
    handler = _ListHandler()
    handler.addFilter(CorrelationIdFilter())
    logger.addHandler(handler)
    prior_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)


class TestWithCorrelationIdUnit:
    """Direct unit tests of the wrapper itself, independent of FastMCP."""

    async def test_async_function_gets_id_and_resets_after(self):
        assert correlation_id_var.get() == "-"

        @with_correlation_id
        async def _tool():
            return correlation_id_var.get()

        result = await _tool()
        assert result != "-"
        assert correlation_id_var.get() == "-"

    def test_sync_function_gets_id_and_resets_after(self):
        assert correlation_id_var.get() == "-"

        @with_correlation_id
        def _tool():
            return correlation_id_var.get()

        result = _tool()
        assert result != "-"
        assert correlation_id_var.get() == "-"

    async def test_concurrent_async_calls_get_distinct_ids(self):
        # ContextVar isolation across asyncio tasks (plan_M3 risk #6).
        @with_correlation_id
        async def _tool():
            await asyncio.sleep(0.02)
            return correlation_id_var.get()

        id_a, id_b = await asyncio.gather(_tool(), _tool())
        assert id_a != "-"
        assert id_b != "-"
        assert id_a != id_b

    def test_wraps_preserves_name_doc_and_signature(self):
        import inspect

        def _original(a: int, b: str = "x") -> str:
            """Original docstring."""
            return b * a

        wrapped = with_correlation_id(_original)
        assert wrapped.__name__ == "_original"
        assert wrapped.__doc__ == "Original docstring."
        assert inspect.signature(wrapped) == inspect.signature(_original)

    async def test_async_wraps_preserves_name_doc_and_signature(self):
        import inspect

        async def _original(a: int, b: str = "x") -> str:
            """Original async docstring."""
            return b * a

        wrapped = with_correlation_id(_original)
        assert wrapped.__name__ == "_original"
        assert wrapped.__doc__ == "Original async docstring."
        assert inspect.signature(wrapped) == inspect.signature(_original)


class TestSectionToolIntegration:
    """A real registered tool, called end to end through FastMCP's own Tool
    object — not just the bare wrapper in isolation."""

    async def test_real_tool_call_stamps_correlation_id_on_log_lines(
        self, captured_backend_records
    ):
        from stealth_chrome_devtools_mcp.embedded import server

        tools = await live_tools(server.mcp)
        # Sync, static-documentation tool - no browser instance needed.
        tool = tools["get_hook_documentation"]

        assert correlation_id_var.get() == "-"
        await tool.run({})
        assert correlation_id_var.get() == "-"  # reset after the call

        ids = {
            record.correlation_id
            for record in captured_backend_records
            if getattr(record, "correlation_id", "-") != "-"
        }
        assert ids, (
            "expected at least one stealth.backend record stamped with a "
            "real (non-default) correlation id during the tool call"
        )


class TestToolsListSchemaSnapshot:
    """MANDATORY per plan risk #5: section_tool's wrapper must not change
    what a real MCP client sees in tools/list for any of the 96 tools."""

    async def test_representative_tool_schema_unchanged_per_section(self):
        golden = json.loads(_GOLDEN_SCHEMA_JSON)

        from stealth_chrome_devtools_mcp.embedded import server

        tools = await live_tools(server.mcp)

        for section, expected in sorted(golden.items()):
            name = expected["name"]
            assert name in tools, f"{section}: tool {name!r} no longer registered"
            mcp_tool = tools[name].to_mcp_tool(name=name)
            assert mcp_tool.name == expected["name"], section
            assert mcp_tool.inputSchema == expected["inputSchema"], (
                f"{section}: inputSchema for {name!r} changed - the "
                f"section_tool wrapper must preserve FastMCP's schema exactly"
            )
