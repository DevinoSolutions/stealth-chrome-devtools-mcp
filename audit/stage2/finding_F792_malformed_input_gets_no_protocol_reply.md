# F-792 — malformed stdin input is dropped without any protocol reply

**Status:** OPEN — characterized by plan_RELEASE W13, not fixed (W13 is zero-`src/`).
**Severity:** LOW-MEDIUM. Nothing breaks; a client that sends a bad frame waits
forever for an answer that is never coming.
**Surface:** the stdio wire of the installed launcher —
`embedded/singleton.py::_proxy_streams`'s `pump_client`, which reads the client
stream and does `if isinstance(msg, Exception): continue`.
**Found by:** plan_RELEASE §2.13 (W13), MQ-143.

## The behavior

Two kinds of bad input were written to the launcher's stdin, each followed by a
two-second window and then a normal call:

| input | reply | session afterwards |
|---|---|---|
| `this is not json at all {{{` | **none** — no frame of any kind | still answers `list_instances` |
| `{"jsonrpc":"2.0","id":424242}` (valid JSON, no `method`) | **none** | still answers `list_instances` |

JSON-RPC 2.0 specifies `-32700 Parse error` for the first and
`-32600 Invalid Request` for the second, both with `"id": null` where the id
cannot be determined. Neither is produced. The proxy's client pump receives the
decode failure as an `Exception` item on the stream and `continue`s past it.

## Why it is worth a finding

The **good** half is the half that matters most and it holds: a malformed frame
cannot take down a live session. That is asserted, not assumed.

The bad half is that silence is indistinguishable from "still working". A client
with a serialization bug — or one talking to the wrong process — gets no signal
at all and blocks on a response that will never arrive, with no bounded failure
and nothing in the frame stream to debug from. Every other error surface in this
product answers; this one does not.

Related and deliberately in scope for the same node: **stdout stays pure**. Across
every W13 session, not one non-JSON line reached stdout and stderr stayed at
zero bytes (the proxy logs to files, not to the terminal). The framing channel is
clean — which is why the missing error frame is a gap in the protocol answer, not
a contamination problem.

## Evidence

`tests/test_wire_semantics.py::test_malformed_input_is_dropped_without_any_protocol_reply`
(`@pytest.mark.characterization`, route:F-792). It asserts BOTH halves: no reply
frame is produced, and the session still answers a real call afterwards. Pinned
in the direction that makes a fix red — the moment either input earns a
`-32700`/`-32600`, the node fails and MQ-143 can be promoted.

`tests/test_wire_semantics.py::test_a_large_bounded_result_is_one_parseable_frame_under_a_slow_reader`
carries the rest of MQ-143's surface (a ~88 KB screenshot as ONE frame, a
deliberately stalled reader, no deadlock, `non_frame_stdout == []`, stderr under
its cap).

## Contract limitation wording (for W5 §Limitations)

> Input that is not a valid JSON-RPC request receives no reply. The session is
> unaffected and continues to serve well-formed requests, but no `-32700` or
> `-32600` error frame is emitted, so a client that sends a malformed frame has
> no protocol-level signal that it did.

## Routing

- MQ-143 in `tests/MANUAL_QA_PROTOCOL.md` is `planned`, with these nodes recorded
  as current support (non-acceptance).
- No `--mq` id in `release-gate.yml` is bound to the pins.
- Related: F-791 (cancellation answers with `code: 0`) is the other half of
  "the wire's error vocabulary is thinner than the protocol's".
