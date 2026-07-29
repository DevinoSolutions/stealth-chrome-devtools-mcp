# F-778 — `get_cookies` returns nodriver `Cookie` dataclasses, not the declared `list[dict]`

**Status: OPEN.** Opened by RELEASE-5 (W5) from the measurements the
real-transport cookie node (PR #52) produced.
**Severity: LOW / cosmetic** — the wire shape a client receives is correct. The
mismatch is between the annotation and the runtime object, plus one confusing
client-side reconstruction.

---

## The finding

`get_cookies` is declared `-> list[dict[str, Any]]` but returns nodriver
`cdp.network.Cookie` **dataclasses**. What each consumer sees:

| Consumer | What it gets | Correct? |
|---|---|---|
| the MCP wire (`structuredContent`) | proper JSON cookie objects — pydantic serializes the dataclasses | **yes** |
| a client reading `structured_content` | real cookie dicts with `name`/`value`/`domain`/`path` | **yes** — this is what the qualified test asserts |
| fastmcp's `result.data` reconstruction | an opaque `[Root()]` | no, but it is a client-side artifact |
| the type annotation | claims `dict`, delivers a dataclass | no |

No user impact has been measured: the served path returns the right data in the
right shape. The risk is one of *reading*, not of behaviour — an
`.data`-based assertion looks like a product failure when it is only the
reconstruction, which is why
`tests/test_e2e_transport_cookies.py::test_real_transport_cookie_round_trip`
deliberately asserts through `structured_content` and says so in its docstring.

## What closing it requires

Either the annotation matches what is returned, or the tool converts the
dataclasses to plain dicts before returning. Both are `src/` changes, so neither
belongs to W5 (production edits are a hard non-goal). Whichever is chosen, the
transport node above already pins the observable wire shape, so a change that
altered it would fail loudly.

## Routing

Recorded here and in the generated contract's limitations register, so the
mismatch lives somewhere rather than nowhere. Low priority by severity, but it
sits on a now-qualified tool, which is why it is written down rather than
carried in a review comment.
