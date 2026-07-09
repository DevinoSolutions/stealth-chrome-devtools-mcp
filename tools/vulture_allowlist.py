"""Vulture allowlist — every entry owner-tagged per 2.5-gates spec.

Vulture at min_confidence=80 finds only entries in wholesale plan-owned files.
False positives at lower confidence (60) are dominated by pydantic model
fields, enum members, and methods called dynamically via server.py's
section_tool dispatch — all correctly suppressed by ignore_decorators.
"""

# ── plan_M5b (cloner consolidation) ────────────────────────────────────────
base_url  # plan_M5b: element_cloner.py:499 unused in current branch

# ── plan_M4ph1 (server.py god-file split) ──────────────────────────────────
ignore_cache  # plan_M4ph1: server.py:1662 destructured but unused
full_page  # plan_M4ph1: server.py:2067 destructured but unused

# ── plan_M7 (close_instance offload) ──────────────────────────────────────
_close_proxy_forwarder  # plan_M7: Phase 1 pops forwarder; method kept for future callers
close_kill_timeout  # plan_M7: pydantic Settings field read via get_settings()

# ── FALSE-POSITIVE (pydantic Settings — vulture can't see pydantic field access) ─
model_config  # FALSE-POSITIVE(pydantic SettingsConfigDict descriptor)
_reject_unknown_prefixed_env  # FALSE-POSITIVE(pydantic model_validator)
