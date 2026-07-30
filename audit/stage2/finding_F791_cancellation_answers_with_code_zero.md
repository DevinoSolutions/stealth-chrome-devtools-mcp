# F-791 — a cancelled request is answered with JSON-RPC error `code: 0`

**Status:** OPEN — characterized by plan_RELEASE W13, not fixed (W13 is zero-`src/`).
**Severity:** MEDIUM. Cancellation works; a client cannot recognise it by code.
**Surface:** the stdio wire of the installed launcher — `embedded/singleton.py`
(`_proxy_streams`, which forwards `notifications/cancelled` to the backend
verbatim) in front of the FastMCP/`mcp` request responder that produces the
frame. The product ships this behavior whichever layer emits it.
**Found by:** plan_RELEASE §2.13 (W13), MQ-141.

## The behavior

Protocol cancellation is **supported and prompt** — which the step did not
assume it would find. With a navigation confirmed in flight by W7's fixture
barrier, sending

```json
{"jsonrpc":"2.0","method":"notifications/cancelled",
 "params":{"requestId":11,"reason":"…"}}
```

ends the wait in **milliseconds**, exactly once, and leaves the session usable.
Releasing the still-parked route afterwards produces no second frame for that id.

The defect is the shape of the terminal frame:

```json
{"jsonrpc": "2.0", "id": 11, "error": {"code": 0, "message": "Request cancelled"}}
```

`code: 0` carries no information. JSON-RPC 2.0 reserves `-32768…-32000` and
expects an implementation to use a code outside that range *that means
something*; zero is the absence of a choice. A client that wants to distinguish
"I cancelled this" from "the server failed" has only the English string
`"Request cancelled"` to match on — the same class of problem F-783 records for
the timeout path, one layer further out.

Two related observations, recorded so the absence is deliberate rather than
assumed:

* **Whether the underlying work stops is not observable at the wire.** The
  cancellation ends the *response*; nothing in the frame says whether the
  navigation was actually abandoned. The node asserts the recoverable half (the
  instance still navigates and closes normally) because that is what can be
  measured from outside.
* **The MCP specification says a receiver SHOULD NOT respond to a cancelled
  request at all.** Answering is arguably friendlier to a waiting client than
  silence — but it is a deviation, and if it is ever "fixed" to silence, every
  client that currently unblocks on this frame will hang instead. Whoever
  changes it must change it deliberately; that is what the pin is for.

## Evidence

`tests/test_wire_semantics.py::test_cancelling_a_confirmed_in_flight_request_ends_it_with_code_zero`
(`@pytest.mark.characterization`, route:F-791). It pins `error["code"] == 0` and
the exact message bytes, so giving cancellation a real code turns the node red.

Its sensitivity control is a separate node —
`::test_cancellation_control_the_same_route_completes_when_released` — which
drives the SAME held route to a normal success by releasing it instead of
cancelling. Without that control, "the cancelled call stopped waiting" would be
equally consistent with "this route never completes".

## Contract limitation wording (for W5 §Limitations)

> Protocol cancellation is honoured: a cancelled `tools/call` is terminated
> promptly and answered exactly once. The answer is a JSON-RPC error with
> `code: 0` and the message `Request cancelled`; there is no typed cancellation
> code, so a client must match the message text. Whether the cancelled operation
> itself stops is not reported.

## Routing

- MQ-141 in `tests/MANUAL_QA_PROTOCOL.md` is `planned`, with this node recorded
  as current support (non-acceptance).
- No `--mq` id in `release-gate.yml` is bound to the pin.
- Related: F-783 (the timeout path raises a bare `Exception` and cancellation is
  never converted in-process) is the same gap seen from the tool boundary; this
  note is what the client actually receives.
