# Hotspot Agreement (dual independent Haiku runs A & B)

Rule: **both found → deep dive**; **one found & Critical/High → deep dive**; **one found & Medium/Low → Unverified appendix**.
A nominated 15, B nominated 14. High concordance on the top tier.

## AGREED (both runs) → deep-dive eligible
| Subsystem | A severity | B severity | Deep dive |
|---|---|---|---|
| server.py god object (eval, ~22 broad excepts, 66 tools, import-time wiring) | Critical | Critical | **DD-1** |
| Singleton + import-time globals + version-awareness bypass + no thread-safety | Critical | High | **DD-2** |
| Element cloner — 5 overlapping variants | High | High | **DD-3** |
| Dynamic hook system — exec/eval, AI-generated code, shared state | High | High | **DD-4** |
| cdp_function_executor — getattr/injection surface | Low | High | **DD-4** (standoff on severity → resolve in dive) |
| process_cleanup.py — zombie/silent-failure semantics | High | Medium | **DD-5** |
| browser_manager.py — no context manager / no close timeout / leak | Medium | High | **DD-5** |
| Pervasive broad/silent exception handlers (server, cleanup, network, dom) | Medium | Medium | **DD-5** (cross-cutting) |
| Persistent storage — in-memory, no durability, no version isolation | Medium | Medium | **DD-2** |
| Network/DOM handlers — coupling + monolithic functions | Medium | Medium | **DD-1** (handlers server orchestrates) |
| Test coverage / testability of async+global modules | Medium | Medium | reported by every DD; aggregated at synthesis |

## SINGLE-run, High/Critical → deep-dive eligible
| Subsystem | Run | Severity | Deep dive |
|---|---|---|---|
| hook_learning_system.py — 309-line monolith `get_hook_examples()` | B | High | **DD-4** |

## SINGLE-run, Medium/Low → Unverified appendix (cross-covered by empirical agents)
- Proxy layer orchestration / hook-before-after-proxy ordering (A, Medium) — DD-4 will note ordering.
- Response handler file fallback: no quota/cleanup/monitoring (A, Medium) — DD-5 touches disk/resource.
- Debug logger singleton: no per-session level (A, Low).
- cli.py: no error recovery (B, Medium) — covered by incident + onboarding agents.
- CI/CD pipeline limited (B, Medium) — covered by tooling.json + onboarding.
- CDPFunctionExecutor single executor / head-of-line blocking (A, Low) — DD-4 notes.

## Deep-dive assignments (5 Sonnet agents, non-overlapping, F-1xx ranges)
- **DD-1** server.py god object + handler coupling → F-101..F-119
- **DD-2** singleton / global state / version-awareness / persistent storage durability → F-120..F-139
- **DD-3** element cloner 5-variant duplication → F-140..F-159
- **DD-4** dynamic code-execution surface (dynamic_hook_system + cdp_function_executor + hook_learning_system; note proxy/CDP-queue) → F-160..F-179
- **DD-5** lifecycle/cleanup/error-handling (browser_manager + process_cleanup + pervasive except + disk fallback) → F-180..F-199
