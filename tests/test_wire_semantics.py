"""plan_RELEASE W13 (MQ-138…144) — what the product does on the WIRE.

Every other lane asks whether a tool computes the right answer. This one asks
whether the **protocol** around that answer holds up: does an independent client
speak it, does a response stay glued to its request when several are in flight,
does a cancellation actually end a wait, and does a client that walks away leave
a clean process table behind.

Four properties make a node here evidence rather than a smoke test.

*It is the real wire.* Every node drives the **absolute installed console
launcher** over stdio JSON-RPC — the same launcher W1 canonicalizes — inside a
throwaway HOME with its own ``--singleton-port``. Nothing imports
``embedded/server.py``; the in-process ``.fn`` seam the E2E suite uses cannot
answer a single question in this file, because framing, ids and disconnects do
not exist there.

*The client owns the ids.* :class:`release_gate_harness.RawStdioWire` writes the
frames by hand, so ``exactly one response for request id N`` is asserted over
actual stdout frames rather than over an SDK's bookkeeping. MQ-127 (W10) says in
words that it makes no such claim; this is where the claim is made.

*Every wait is bounded twice, and no sleep is a barrier.* The product's own
deadline is set strictly inside :data:`OUTER_BOUND`, so a request that never
answers fails a node by name instead of hanging pytest. "In flight" always means
W7's fixture said the request **arrived** (``/fault/arm`` → poll
``/fault/status`` for ``entered`` → ``/fault/release``); a cancellation or a
disconnect is therefore injected into an operation that has demonstrably
started.

*Teardown is owned.* :func:`release_gate_harness.gate_workspace` terminates the
detached backend recorded in the isolated ``server.json`` and asserts no child
process of this pytest run survives the block.

What the wire actually found
----------------------------
The framing, correlation, disconnect and shutdown contracts hold. Six things do
not, and each of them was found by asking a question only a wire lane can ask:

* **Cancellation works, and works promptly** — a confirmed in-flight navigation
  answers within milliseconds of ``notifications/cancelled``, exactly once, and
  the SERVER stays usable. Two things are wrong with it: the answer is a
  JSON-RPC error with ``code: 0`` (F-791), which no client can classify by code;
  and the cancelled INSTANCE is left wedged (F-794) — its next call burns the
  full CDP budget and reports that the browser may have crashed.
* **Malformed input is answered with silence** (F-792). A non-JSON line and a
  method-less request both vanish: no ``-32700``, no ``-32600``, no frame at
  all. The session survives — which is the half that matters most — but a client
  that sends a bad frame waits forever for a reply that is never coming.
* **A second concurrent auto-clone spawn blocked on the client** (F-790, now
  FIXED). While one instance holds the master profile, a second
  ``spawn_browser`` with no ``user_data_dir`` sends a ``roots/list`` request *to
  the client* and awaits it. That await had no deadline, so a client that does
  not implement MCP ``roots`` — which the protocol permits — got no result, no
  error and no timeout, ever. Only a wire lane could have found this, because
  the defect IS a frame. The await is now bounded by
  ``STEALTH_MCP_CLIENT_ROOTS_TIMEOUT_SECONDS`` and falls back to a local clone
  seed, and the node below is the regression oracle: a client that advertises
  ``roots`` and then answers nothing still gets its spawn reply. Nodes here that
  need two instances still name their profiles — that is cheaper, not required.
* **One instance serializes its calls** (F-793). Short calls run together
  happily, but a call issued behind a *parked* operation on the same instance
  does not queue — it times out and blames a crash. MQ-140's reversed-completion
  node is cross-instance for exactly this reason, stated rather than dodged.
* **A thrown script was reported as a success** (F-795), found incidentally when
  this module's first probe script used an illegal ``return``. FIXED in 2.0.1:
  the eval path now raises, and the node that found it asserts the failure.

MQ binding. The ids below are bound to runtime evidence by the ``--mq`` flags on
the ``transport`` cell's ``release_evidence.py emit`` step in
``.github/workflows/release-gate.yml`` — the ``transport`` cell rather than the
``integration`` one because this module is ``transport``-marked, and the
integration cell deselects ``transport`` on macOS (F-773). That ledger, not this
docstring, is what W8 resolves against.

===========  ================================================================
MQ           node
===========  ================================================================
``MQ-138``   ``test_the_official_mcp_sdk_client_initializes_lists_calls_and_closes``
``MQ-139``   ``test_concurrent_calls_on_one_instance_keep_their_own_answers``
             and ``test_two_named_instances_stay_isolated_under_interleaved_calls``
``MQ-140``   ``test_reversed_completion_keeps_each_result_on_its_own_request``
             and ``test_duplicate_looking_payloads_are_told_apart_only_by_id``
``MQ-142``   ``test_client_disconnect_with_a_request_in_flight_has_one_outcome``
``MQ-144``   ``test_shutdown_with_an_in_flight_call_leaves_no_orphan``
===========  ================================================================

``MQ-141`` (cancellation) and ``MQ-143`` (framing/backpressure/malformed input)
are **planned**, not satisfied, and deliberately so. Their measurements were
taken and each found a real defect in the half the step names — cancellation's
typed outcome and instance recovery (F-791, F-794) and malformed input's
protocol reply (F-792). All of these are characterization-pinned and routed,
never fixed: ``src/`` edits are a plan_RELEASE non-goal (§1.2) and a
characterization can never satisfy a step (§0.2). Their ids are NOT bound to any
``--mq`` flag. F-790, F-793 and F-795 own no step; they narrow MQ-139/MQ-140 and
are pinned inside them. Two of those three have since been FIXED for 2.0.1 —
F-790 (the unbounded ``roots/list`` await; its node here was flipped from a
characterization to the regression oracle in that same change) and F-795 (its
node asserts the fix rather than pinning the defect). F-793 remains
characterization-pinned.

HTTP parity, stated exactly: ``RELEASE_CONTRACT.md`` puts HTTP under
*"HTTP (described, not qualified)"*. plan_RELEASE §2.13 asks for HTTP parity
**only where HTTP is contract-qualified**, so this module runs no HTTP node and
copies no stdio evidence into an HTTP column. That exclusion is itself asserted
by ``test_the_http_column_is_out_of_scope_because_http_is_not_qualified`` so it
cannot silently stop being true.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import time
import uuid
from pathlib import Path

import pytest
import requests

from e2e_helpers import CAN_RUN
from release_gate_harness import (
    REGISTRY_TOOL_COUNT,
    SERVER_NAME,
    RawStdioWire,
    gate_work_dir,
    gate_workspace,
    resolve_launcher,
    workspace_backend_logs,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.transport,
    # The gate runs this module in two cells with different per-test ceilings
    # (`integration` at --timeout=180, `transport` at --timeout=300). Every wait
    # in this file is already bounded by its OWN deadline below, all of which are
    # well inside 300; pinning the pytest ceiling here makes the two cells agree
    # and stops the stricter one cutting a node off BEFORE its own bound can
    # report what went wrong (the shape of F-780).
    pytest.mark.timeout(300),
]

if not CAN_RUN:
    pytestmark.append(pytest.mark.skip("Chrome not available or server failed to load"))

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── Bounds ──────────────────────────────────────────────────────────────────
# The harness bound. Never a product deadline: if THIS fires, the wire did not
# answer, and the node fails by name rather than hanging the suite.
OUTER_BOUND = 90.0
HANDSHAKE_BOUND = 130.0  # first backend-bound call — covers backend cold start
SPAWN_BOUND = 120.0  # a real Chrome launch, cold
BARRIER_BOUND = 25.0  # how long the fixture may take to say "request arrived"
EXIT_BOUND = 45.0  # how long a disconnected proxy may take to exit

# The product deadlines, strictly inside OUTER_BOUND.
NAV_TIMEOUT_MS = 20_000
HELD_NAV_TIMEOUT_MS = 40_000  # a route held open on purpose; released by the test

HTTP_TIMEOUT = 10

# F-790's control bound. The working (master-profile) spawn path answers in
# under a second on the same machine and in the same session, so a control spawn
# slower than 30x that means the machine cannot decide the node at all.
CLONE_HANG_BOUND = 30.0
# F-790's product deadline, pinned into the node's workspace via
# STEALTH_MCP_CLIENT_ROOTS_TIMEOUT_SECONDS: how long the auto-clone path may wait
# on the CLIENT's roots/list reply before falling back to a local clone seed.
# Small on purpose — the node measures the bound, not the default's patience.
ROOTS_BOUND = 2.0

# M6-pinned bytes. Preserved verbatim; a fix must turn these red deliberately.
INSTANCE_NOT_FOUND = "Instance not found: {instance_id}"
CANCELLED_MESSAGE = "Request cancelled"
# The tail of F-783's CDP-timeout message. The leading "…after {t:.0f}s (instance
# {id})." varies per call, so the invariant half is matched exactly and the
# variable half by prefix.
CDP_TIMEOUT_TAIL = (
    "The browser may have crashed or the connection dropped. "
    "Try closing the instance with close_instance and spawning a new one."
)


def _echo(value: str) -> str:
    """A JS **expression** that evaluates to ``value``.

    ``execute_script`` evaluates an expression, not a function body: a
    ``return`` statement raises ``SyntaxError: Illegal return statement``. That
    used to be reported as ``success: true`` (F-795, now fixed), which made
    getting it wrong silent. Every script in this module goes through here.
    """
    return json.dumps(value)


# ── Fixture-barrier helpers (W7's controllers, driven over plain HTTP) ───────
def _fault(base: str, verb: str, token: str) -> dict:
    response = requests.get(
        f"{base}/fault/{verb}", params={"token": token}, timeout=HTTP_TIMEOUT
    )
    response.raise_for_status()
    return response.json()


async def _arm(base: str, token: str) -> dict:
    return await asyncio.to_thread(_fault, base, "arm", token)


async def _release(base: str, token: str) -> dict:
    return await asyncio.to_thread(_fault, base, "release", token)


async def _await_entered(base: str, token: str, timeout: float = BARRIER_BOUND) -> dict:
    """Block until the fixture confirms the request really arrived.

    This — never a sleep — is what makes "in flight" a fact. The status route is
    served from a different thread than the parked handler, so polling it cannot
    deadlock against the request being held.
    """
    deadline = time.monotonic() + timeout
    snapshot: dict = {}
    while time.monotonic() < deadline:
        snapshot = await asyncio.to_thread(_fault, base, "status", token)
        if snapshot.get("entered"):
            return snapshot
        await asyncio.sleep(0.05)
    raise AssertionError(f"fixture never entered for {token!r}: {snapshot}")


def _token(what: str) -> str:
    return f"w13-{what}-{uuid.uuid4().hex[:8]}"


# ── Frame readers ───────────────────────────────────────────────────────────
def _tool_result(frame: dict) -> dict:
    """The ``tools/call`` result of a frame that must not be a protocol error."""
    assert "error" not in frame, f"protocol error frame: {frame}"
    return frame["result"]


def _tool_text(frame: dict) -> str:
    result = _tool_result(frame)
    content = result.get("content") or []
    return content[0]["text"] if content else ""


def _tool_payload(frame: dict):
    """A tool's JSON payload, whether it arrived structured or as text."""
    result = _tool_result(frame)
    if "structuredContent" in result:
        structured = result["structuredContent"]
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    return json.loads(_tool_text(frame))


def _assert_ok(frame: dict, request_id) -> dict:
    assert frame["id"] == request_id, (
        f"response id {frame['id']} != request {request_id}"
    )
    result = _tool_result(frame)
    assert result.get("isError") is not True, f"tool failed: {result}"
    return result


# ── Call helpers ────────────────────────────────────────────────────────────
async def _call(
    wire: RawStdioWire, name: str, args: dict, timeout: float = OUTER_BOUND
):
    request_id = await wire.call_tool(name, args)
    frame = await wire.response(request_id, timeout)
    _assert_ok(frame, request_id)
    return frame


async def _spawn(wire: RawStdioWire, *, profile: str | None = None) -> str:
    """One headless instance. ``profile`` names it (see F-790: the unnamed
    auto-clone path cannot produce a SECOND concurrent instance)."""
    args: dict = {"headless": True, "sandbox": False}
    if profile is not None:
        args["user_data_dir"] = profile
    frame = await _call(wire, "spawn_browser", args, timeout=SPAWN_BOUND)
    return _tool_payload(frame)["instance_id"]


async def _close(wire: RawStdioWire, instance_id: str) -> None:
    await _call(
        wire, "close_instance", {"instance_id": instance_id}, timeout=OUTER_BOUND
    )


async def _navigate(wire: RawStdioWire, instance_id: str, url: str, **extra):
    return await _call(
        wire,
        "navigate",
        {"instance_id": instance_id, "url": url, "timeout": NAV_TIMEOUT_MS, **extra},
    )


async def _handshake(wire: RawStdioWire, *, capabilities: dict | None = None) -> dict:
    """``initialize`` + ``notifications/initialized`` + one ``tools/list``.

    ``capabilities`` is forwarded verbatim; the F-790 node uses it to advertise
    MCP ``roots`` and then never answer a single ``roots/list``.
    """
    init = await wire.initialize(capabilities=capabilities)
    listed = await wire.request("tools/list")
    tools = await wire.response(listed, HANDSHAKE_BOUND)
    return {"init": init, "tools": _tool_result(tools)["tools"]}


# ── Fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def launcher():
    return resolve_launcher()


@pytest.fixture(scope="module")
def space(tmp_path_factory):
    """ONE isolated backend for the whole module: its cold start is paid once
    and every node's teardown is still owned by the block.

    ``gate_work_dir`` prefers ``RUNNER_TEMP`` on CI (W1's rule, for W1's reason),
    and a directory it mints there is ours to remove — pytest only cleans the
    ``tmp_path_factory`` fallback.
    """
    fallback = tmp_path_factory.mktemp("w13")
    work_dir = gate_work_dir(fallback)
    try:
        with gate_workspace(work_dir) as workspace:
            yield workspace
    finally:
        if work_dir != fallback:
            shutil.rmtree(work_dir, ignore_errors=True)
    assert not workspace["leftover_children"], (
        f"W13 left child processes behind: {workspace['leftover_children']}"
    )


@pytest.fixture(scope="module", autouse=True)
def primed_master(launcher, space):
    """Create the master profile (and pay the backend's cold start) once.

    Every node below names its profile, and a NAMED profile is cloned from the
    master snapshot — which cannot happen before a master exists. One unnamed
    spawn, immediately closed, is what creates it; doing it here rather than
    inside a node keeps F-790 (the unnamed path cannot produce a *second* live
    instance) out of every other node's way.

    Deliberately synchronous: it needs no interaction with a node's event loop,
    and a module-scoped *async* fixture would force a matching pytest-asyncio
    loop scope on every node in the file for no gain.
    """

    async def _prime():
        client = RawStdioWire(launcher=launcher, env=space["env"], port=space["port"])
        await client.start()
        try:
            await _handshake(client)
            instance_id = await _spawn(client)
            await _close(client, instance_id)
        finally:
            await client.aclose()

    asyncio.run(_prime())


@pytest.fixture()
async def wire(launcher, space):
    """A fresh handshaken client against the module's shared backend."""
    client = RawStdioWire(launcher=launcher, env=space["env"], port=space["port"])
    await client.start()
    try:
        await _handshake(client)
    except Exception as exc:  # noqa: BLE001  PERMANENT(augment with backend logs)
        await client.aclose()
        pytest.fail(
            f"W13 handshake failed: {type(exc).__name__}: {exc}\n"
            f"--- backend logs (capped) ---\n{workspace_backend_logs(space)[-4000:]}"
        )
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture()
async def named_instance(wire, request):
    """One named-profile instance, closed by the node's own assertion where the
    node cares, and by this net where it does not."""
    profile = f"w13-{request.node.name[:40]}"
    instance_id = await _spawn(wire, profile=profile)
    try:
        yield instance_id
    finally:
        with contextlib.suppress(Exception):
            await _close(wire, instance_id)


# ═══════════════════════════════════════════════════════════════════════════
# MQ-138 — an INDEPENDENT client, not ours
# ═══════════════════════════════════════════════════════════════════════════
async def test_the_official_mcp_sdk_client_initializes_lists_calls_and_closes(
    launcher, space
):
    """MQ-138: the official ``mcp`` SDK — a different client library from the
    ``fastmcp`` one W1 uses and the hand-written frames every other node here
    uses — drives the installed launcher end to end.

    Interoperability is the whole point, so this node imports **only** the
    official SDK for the protocol. Nothing from ``fastmcp``, nothing from
    ``embedded/server.py``: if the wire only answered our own client, this node
    could not pass.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=str(launcher),
        args=["--singleton-port", str(space["port"])],
        env=space["env"],
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await asyncio.wait_for(session.initialize(), HANDSHAKE_BOUND)
            assert init.serverInfo.name == SERVER_NAME
            assert init.serverInfo.version
            assert init.protocolVersion

            listed = await asyncio.wait_for(session.list_tools(), HANDSHAKE_BOUND)
            assert len(listed.tools) == REGISTRY_TOOL_COUNT
            # A schema, not just a name: an interoperable client must be able to
            # build a call from what it was told.
            by_name = {tool.name: tool for tool in listed.tools}
            assert "list_instances" in by_name
            assert by_name["spawn_browser"].inputSchema["type"] == "object"

            ok = await asyncio.wait_for(
                session.call_tool("list_instances", {}), OUTER_BOUND
            )
            assert ok.isError is False

            missing = "no-such-instance-mq138"
            failed = await asyncio.wait_for(
                session.call_tool("get_page_content", {"instance_id": missing}),
                OUTER_BOUND,
            )
            assert failed.isError is True
            # M6-pinned bytes, verbatim, as the independent client receives them.
            assert failed.content[0].text.endswith(
                INSTANCE_NOT_FOUND.format(instance_id=missing)
            )

    # The SDK's own teardown closed the child; the workspace fixture proves no
    # process from it survives the module.


# ═══════════════════════════════════════════════════════════════════════════
# MQ-139 — concurrency on one instance, isolation across two
# ═══════════════════════════════════════════════════════════════════════════
async def test_concurrent_calls_on_one_instance_keep_their_own_answers(
    wire, named_instance, fixture_app_server
):
    """MQ-139 (one instance): four ``tools/call`` requests in flight at once,
    each carrying a value only that request can produce.

    Distinguishable payloads are the point. Four identical calls would pass even
    if the server answered every one of them with the first result; these cannot.
    """
    await _navigate(wire, named_instance, f"{fixture_app_server}/index.html")

    markers = [f"mq139-{index}-{uuid.uuid4().hex[:6]}" for index in range(4)]
    ids = [
        await wire.call_tool(
            "execute_script",
            {"instance_id": named_instance, "script": _echo(marker)},
        )
        for marker in markers
    ]
    frames = await asyncio.gather(*(wire.response(i, OUTER_BOUND) for i in ids))

    for marker, request_id, frame in zip(markers, ids, frames, strict=True):
        _assert_ok(frame, request_id)
        payload = _tool_payload(frame)
        assert payload["result"] == marker, (
            f"request {request_id} expected {marker!r}, got {payload['result']!r}"
        )
        assert len(wire.frames_for(request_id)) == 1, "exactly one response per id"

    assert wire.non_frame_stdout == [], wire.non_frame_stdout


async def test_two_named_instances_stay_isolated_under_interleaved_calls(
    wire, fixture_app_server
):
    """MQ-139 (two instances): two live instances, calls interleaved, and each
    answer belongs to its own browser.

    Isolation is asserted twice — a different page per instance (so a swapped
    response is visible in the content) and a ``localStorage`` write in A that B
    must not be able to see (so shared browser state is visible even if the
    responses are correctly routed).

    Both profiles are NAMED. The unnamed auto-clone path cannot produce a second
    concurrent instance at all (F-790), which is pinned separately below.
    """
    alpha = await _spawn(wire, profile="w13-iso-alpha")
    beta = await _spawn(wire, profile="w13-iso-beta")
    try:
        await _navigate(wire, alpha, f"{fixture_app_server}/index.html")
        await _navigate(wire, beta, f"{fixture_app_server}/cookies.html")

        secret = f"mq139-alpha-only-{uuid.uuid4().hex[:8]}"
        await _call(
            wire,
            "execute_script",
            {
                "instance_id": alpha,
                "script": f"(localStorage.setItem('mq139', {secret!r}), 'set')",
            },
        )

        # Interleaved and concurrent: A, B, A, B — all four in flight together.
        wanted = [
            (alpha, "fixture-index-page"),
            (beta, "fixture-cookies-page"),
            (alpha, "fixture-index-page"),
            (beta, "fixture-cookies-page"),
        ]
        ids = [
            await wire.call_tool("get_page_content", {"instance_id": iid})
            for iid, _ in wanted
        ]
        frames = await asyncio.gather(*(wire.response(i, OUTER_BOUND) for i in ids))
        for (_, sentinel), request_id, frame in zip(wanted, ids, frames, strict=True):
            _assert_ok(frame, request_id)
            assert sentinel in _tool_text(frame)
            assert len(wire.frames_for(request_id)) == 1

        # The other instance never saw alpha's write.
        beta_read = await _call(
            wire,
            "execute_script",
            {"instance_id": beta, "script": "localStorage.getItem('mq139')"},
        )
        assert secret not in _tool_text(beta_read), (
            "instance B can read instance A's localStorage — profiles are not isolated"
        )

        listed = await _call(wire, "list_instances", {})
        live = {entry["instance_id"] for entry in _tool_payload(listed)}
        assert {alpha, beta} <= live
    finally:
        for instance_id in (alpha, beta):
            with contextlib.suppress(Exception):
                await _close(wire, instance_id)

    listed = await _call(wire, "list_instances", {})
    remaining = {entry["instance_id"] for entry in _tool_payload(listed)}
    assert not ({alpha, beta} & remaining), "closed instances still listed"


async def test_execute_script_reports_failure_for_a_script_that_threw(
    wire, named_instance, fixture_app_server
):
    """F-795 (FIXED): a script that raises is an ERROR on the wire.

    Found incidentally: the first draft of MQ-139 above used ``return 'x';``,
    which is a ``SyntaxError: Illegal return statement`` for an expression
    evaluator — and the tool used to report it as a success whose ``result``
    happened to be an exception record, with ``success: true``, ``error: null``
    and ``isError`` false.

    ``nodriver``'s ``Tab.evaluate`` RETURNS the CDP ``ExceptionDetails`` in the
    value's place instead of raising, so the fix is one guard
    (``tool_errors._require_js_value``) on the eval path. This node is the wire
    half of that fix: what a caller sees is ``isError: true`` carrying the
    exception text — and the SAME tab still runs a valid script afterwards, so
    the failure is reported without wedging anything.
    """
    await _navigate(wire, named_instance, f"{fixture_app_server}/index.html")

    request_id = await wire.call_tool(
        "execute_script",
        {"instance_id": named_instance, "script": "return 'illegal-here';"},
    )
    frame = await wire.response(request_id, OUTER_BOUND)
    result = _tool_result(frame)
    assert result.get("isError") is True, (
        f"a throwing script still reports success — F-795 is open again: {result}"
    )
    text = result["content"][0]["text"]
    assert text.startswith(
        "Error calling tool 'execute_script': Script raised an exception: "
    ), text
    assert "SyntaxError: Illegal return statement" in text, text

    # The tab is not wedged by its own script's exception.
    ok = await _call(
        wire,
        "execute_script",
        {"instance_id": named_instance, "script": _echo("after-the-throw")},
    )
    assert _tool_payload(ok)["result"] == "after-the-throw"


# ═══════════════════════════════════════════════════════════════════════════
# MQ-140 — correlation when completion order is not issue order
# ═══════════════════════════════════════════════════════════════════════════
async def test_reversed_completion_keeps_each_result_on_its_own_request(
    wire, named_instance, fixture_app_server
):
    """MQ-140: a request issued FIRST is deliberately made to finish LAST.

    The fixture holds the navigation open until this test releases it, so the
    reversal is caused rather than hoped for: the barrier proves the slow
    request arrived at the server, the fast request is issued and answered while
    it is still parked, and only then is the slow one let go. A server that
    answered in issue order, or that handed the second answer to the first id,
    fails on the recorded frame order.

    The fast request runs on a **second** instance, and that is not incidental:
    a parked navigation blocks every other CDP call on the SAME instance
    (F-793), so the honest cross-request-correlation shape here is cross-
    instance. The same-instance limitation is pinned in its own node below
    rather than papered over by choosing a call that happens to dodge it.
    """
    token = _token("reversed")
    fast_instance = await _spawn(wire, profile="w13-reversed-fast")
    try:
        await _arm(fixture_app_server, token)
        slow_id = await wire.call_tool(
            "navigate",
            {
                "instance_id": named_instance,
                "url": f"{fixture_app_server}/fault/slow?token={token}",
                "timeout": HELD_NAV_TIMEOUT_MS,
            },
        )
        await _await_entered(fixture_app_server, token)
        assert not wire.answered(slow_id), "the held navigation answered before release"

        marker = f"mq140-fast-{uuid.uuid4().hex[:6]}"
        fast_id = await wire.call_tool(
            "execute_script",
            {"instance_id": fast_instance, "script": _echo(marker)},
        )
        fast_frame = await wire.response(fast_id, OUTER_BOUND)
        _assert_ok(fast_frame, fast_id)
        assert _tool_payload(fast_frame)["result"] == marker
        assert not wire.answered(slow_id), "the held navigation answered out of turn"

        await _release(fixture_app_server, token)
        slow_frame = await wire.response(slow_id, OUTER_BOUND)
        _assert_ok(slow_frame, slow_id)

        responses = wire.frames_for(slow_id) + wire.frames_for(fast_id)
        order = [
            f["id"]
            for f in wire.frames
            if any(f is candidate for candidate in responses)
        ]
        assert order == [fast_id, slow_id], (
            f"completion order was not reversed relative to issue order: {order}"
        )
        assert len(wire.frames_for(slow_id)) == 1
        assert len(wire.frames_for(fast_id)) == 1
        assert wire.non_frame_stdout == []
    finally:
        await _release(fixture_app_server, token)
        with contextlib.suppress(Exception):
            await _close(wire, fast_instance)


@pytest.mark.characterization
async def test_a_parked_navigation_blocks_every_call_on_the_same_instance(
    wire, named_instance, fixture_app_server
):
    """F-793: while one navigation is parked, a second call on the SAME instance
    does not queue behind it — it times out.

    Found while building the reversed-completion node above, which originally
    used one instance and failed here. The instance's single CDP connection is
    serialized, so the second call waits out its own ``CDP operation timed out``
    budget and fails; a client issuing two calls against one instance gets a
    spurious "the browser may have crashed" for the second.

    Two controls keep this a measurement:

    * the parked navigation is confirmed in flight by the fixture barrier, so
      the blocker demonstrably exists when the second call is issued;
    * the same call shape succeeds on a DIFFERENT instance at the same moment
      (``test_reversed_completion_keeps_each_result_on_its_own_request``), so
      the failure is per-instance serialization, not a broken call.

    Pinned in the direction that makes a fix red: the blocked call must FAIL.
    """
    token = _token("same-instance")
    await _arm(fixture_app_server, token)
    parked_id = None
    try:
        parked_id = await wire.call_tool(
            "navigate",
            {
                "instance_id": named_instance,
                "url": f"{fixture_app_server}/fault/slow?token={token}",
                "timeout": HELD_NAV_TIMEOUT_MS,
            },
        )
        await _await_entered(fixture_app_server, token)

        blocked_id = await wire.call_tool(
            "execute_script",
            {"instance_id": named_instance, "script": _echo("mq140-blocked")},
        )
        blocked = await wire.response(blocked_id, OUTER_BOUND)
        result = _tool_result(blocked)
        assert result.get("isError") is True, (
            "a call issued behind a parked navigation SUCCEEDED — F-793 is closed "
            "and MQ-139's same-instance claim can be widened"
        )
        text = result["content"][0]["text"]
        # M6-pinned bytes (F-783's timeout message), verbatim.
        assert text.startswith(
            "Error calling tool 'execute_script': CDP operation timed out after "
        )
        assert text.endswith(CDP_TIMEOUT_TAIL)
    finally:
        await _release(fixture_app_server, token)
        if parked_id is not None:
            with contextlib.suppress(Exception):
                await wire.response(parked_id, OUTER_BOUND)


async def test_duplicate_looking_payloads_are_told_apart_only_by_id(
    wire, named_instance, fixture_app_server
):
    """MQ-140: two in-flight requests whose method AND arguments are byte-
    identical. The request id is the only thing that distinguishes them, so this
    is the narrowest possible test of correlation: exactly two responses, one
    per id, neither dropped and neither duplicated.
    """
    await _navigate(wire, named_instance, f"{fixture_app_server}/index.html")

    arguments = {"instance_id": named_instance, "include_frames": False}
    first = await wire.call_tool("get_page_content", arguments)
    second = await wire.call_tool("get_page_content", arguments)
    assert first != second, "the wire must mint distinct ids"

    frames = await asyncio.gather(
        wire.response(first, OUTER_BOUND), wire.response(second, OUTER_BOUND)
    )
    for request_id, frame in zip((first, second), frames, strict=True):
        _assert_ok(frame, request_id)
        assert "fixture-index-page" in _tool_text(frame)
        assert len(wire.frames_for(request_id)) == 1, (
            f"id {request_id} got {len(wire.frames_for(request_id))} responses"
        )
    assert wire.non_frame_stdout == []


# ═══════════════════════════════════════════════════════════════════════════
# MQ-141 — protocol cancellation (planned: F-791)
# ═══════════════════════════════════════════════════════════════════════════
async def test_cancellation_control_the_same_route_completes_when_released(
    wire, named_instance, fixture_app_server
):
    """MQ-141 (control): the SAME held route, released instead of cancelled,
    COMPLETES inside the product deadline.

    Without this, "the cancelled request stopped waiting" is equally consistent
    with "this route never works", and the cancellation node below would prove
    nothing.
    """
    token = _token("cancel-control")
    await _arm(fixture_app_server, token)
    request_id = await wire.call_tool(
        "navigate",
        {
            "instance_id": named_instance,
            "url": f"{fixture_app_server}/fault/slow?token={token}",
            "timeout": HELD_NAV_TIMEOUT_MS,
        },
    )
    await _await_entered(fixture_app_server, token)
    await _release(fixture_app_server, token)

    frame = await wire.response(request_id, OUTER_BOUND)
    _assert_ok(frame, request_id)
    assert _tool_payload(frame)["success"] is True


@pytest.mark.characterization
async def test_cancelling_a_confirmed_in_flight_request_ends_it_with_code_zero(
    wire, named_instance, fixture_app_server
):
    """MQ-141 (planned, F-791 + F-794): ``notifications/cancelled`` for a
    request the fixture has confirmed is in flight.

    The good half: the wait ENDS, promptly, exactly once, no duplicate frame
    arrives when the route is later released, and the SERVER is still usable —
    cancellation is genuinely supported, which is more than the step assumed it
    might find.

    Two halves keep MQ-141 `planned`, and both are pinned here:

    * **F-791** — the terminal outcome is a JSON-RPC error whose ``code`` is
      ``0``. Zero is neither a reserved JSON-RPC code nor a documented product
      code, so a client can only recognise a cancellation by matching the
      message string.
    * **F-794** — the cancelled instance is left **wedged**. The next navigation
      on it does not work; it burns its full CDP budget and returns the
      "browser may have crashed" timeout. Recovery means a NEW instance, not the
      cancelled one, which is the same shape F-788 records for a navigation
      timeout.

    Both are pinned in the direction that makes a fix red: a typed code, or an
    instance that survives its own cancellation, turns this node red and forces
    MQ-141 to be promoted deliberately.
    """
    token = _token("cancel")
    await _arm(fixture_app_server, token)
    request_id = await wire.call_tool(
        "navigate",
        {
            "instance_id": named_instance,
            "url": f"{fixture_app_server}/fault/hang-before-headers?token={token}",
            "timeout": HELD_NAV_TIMEOUT_MS,
        },
    )
    await _await_entered(fixture_app_server, token)
    assert not wire.answered(request_id)

    started = time.monotonic()
    await wire.notify(
        "notifications/cancelled",
        {"requestId": request_id, "reason": "plan_RELEASE W13 MQ-141"},
    )
    frame = await wire.response(request_id, OUTER_BOUND)
    elapsed = time.monotonic() - started

    # The wait ended, and it ended because of the cancellation: the route is
    # still parked (nothing has released it yet), so nothing else could have.
    assert elapsed < HELD_NAV_TIMEOUT_MS / 1000, (
        f"cancellation took {elapsed:.1f}s — that is the navigation deadline, not a cancel"
    )
    assert frame["id"] == request_id
    assert "error" in frame, f"expected a protocol error frame, got {frame}"
    assert frame["error"]["message"] == CANCELLED_MESSAGE
    # F-791: pinned as-is. A typed code makes this red.
    assert frame["error"]["code"] == 0

    # The session survives a cancellation.
    listed = await _call(wire, "list_instances", {})
    assert any(e["instance_id"] == named_instance for e in _tool_payload(listed))

    # Releasing the route afterwards produces NO second frame for that id.
    await _release(fixture_app_server, token)
    await asyncio.sleep(2.0)  # not a barrier: a window in which a duplicate could land
    assert len(wire.frames_for(request_id)) == 1, (
        f"a cancelled id received {len(wire.frames_for(request_id))} frames"
    )

    # F-794: the cancelled INSTANCE does not recover. The next navigation on it
    # burns its whole CDP budget and returns the crash-or-dropped-connection
    # timeout. Pinned as-is; an instance that survives cancellation makes it red.
    wedged_id = await wire.call_tool(
        "navigate",
        {
            "instance_id": named_instance,
            "url": f"{fixture_app_server}/index.html",
            "timeout": NAV_TIMEOUT_MS,
        },
    )
    wedged = _tool_result(await wire.response(wedged_id, OUTER_BOUND))
    assert wedged.get("isError") is True, (
        "the cancelled instance navigated again — F-794 is closed and MQ-141's "
        "recovery half can be claimed"
    )
    text = wedged["content"][0]["text"]
    # M6-pinned bytes (F-783's timeout message), verbatim.
    assert text.startswith(
        "Error calling tool 'navigate': CDP operation timed out after "
    )
    assert text.endswith(CDP_TIMEOUT_TAIL)

    # Recovery is at the SERVER level, not the instance level: a fresh instance
    # navigates and closes cleanly, so a cancellation costs one browser, not the
    # session.
    fresh = await _spawn(wire, profile="w13-cancel-recovery")
    try:
        await _navigate(wire, fresh, f"{fixture_app_server}/index.html")
    finally:
        with contextlib.suppress(Exception):
            await _close(wire, fresh)
    assert wire.non_frame_stdout == []


# ═══════════════════════════════════════════════════════════════════════════
# MQ-142 — the client walks away mid-request
# ═══════════════════════════════════════════════════════════════════════════
async def test_client_disconnect_with_a_request_in_flight_has_one_outcome(
    launcher, space, fixture_app_server
):
    """MQ-142: close stdin while a confirmed in-flight navigation is parked.

    The oracle is *exactly one terminal outcome per request id*: either one
    response frame arrived before the stream ended, or none did and the stream
    ended — never two frames, never a half-frame, never a process that stays
    alive waiting for an answer nobody will read. Then a FRESH client must be
    able to drive the same backend, which is what makes the disconnect a
    recoverable event rather than a wedge.

    This node owns its own client (it destroys it) but shares the module's
    backend on purpose: surviving a client disconnect is precisely the property
    under test.
    """
    dying = RawStdioWire(launcher=launcher, env=space["env"], port=space["port"])
    await dying.start()
    instance_id = None
    token = _token("disconnect")
    try:
        await _handshake(dying)
        instance_id = await _spawn(dying, profile="w13-disconnect")
        await _arm(fixture_app_server, token)
        request_id = await dying.call_tool(
            "navigate",
            {
                "instance_id": instance_id,
                "url": f"{fixture_app_server}/fault/hang-before-headers?token={token}",
                "timeout": HELD_NAV_TIMEOUT_MS,
            },
        )
        await _await_entered(fixture_app_server, token)

        await dying.close_stdin()
        exit_code = await dying.wait_exit(EXIT_BOUND)
        assert exit_code is not None, (
            f"the proxy did not exit within {EXIT_BOUND}s of stdin closing"
        )

        outcomes = dying.frames_for(request_id)
        assert len(outcomes) <= 1, f"more than one terminal outcome: {outcomes}"
        assert dying.stdout_eof, "stdout did not reach EOF after the child exited"
        assert dying.non_frame_stdout == [], dying.non_frame_stdout
    finally:
        await _release(fixture_app_server, token)
        await dying.aclose()

    # Recovery: a fresh client drives the same backend, and the orphaned
    # instance is still there to be cleaned up through the normal tool.
    fresh = RawStdioWire(launcher=launcher, env=space["env"], port=space["port"])
    await fresh.start()
    try:
        handshake = await _handshake(fresh)
        assert len(handshake["tools"]) == REGISTRY_TOOL_COUNT
        if instance_id is not None:
            with contextlib.suppress(Exception):
                await _close(fresh, instance_id)
    finally:
        await fresh.aclose()


# ═══════════════════════════════════════════════════════════════════════════
# MQ-143 — framing, malformed input, large results, backpressure
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.characterization
async def test_malformed_input_is_dropped_without_any_protocol_reply(
    wire, fixture_app_server
):
    """MQ-143 (planned, F-792): two kinds of bad input, and what comes back.

    A non-JSON line and a syntactically valid frame with no ``method`` both get
    **no reply at all** — no ``-32700`` parse error, no ``-32600`` invalid
    request, nothing. The session survives both, which is the property that
    stops a bad frame from taking down a session; but a client that sent one
    waits forever, and that is why MQ-143 is `planned`.

    Pinned in the direction that makes a fix red: the moment either input earns
    a reply, ``no_reply`` stops holding and this node must be updated.
    """
    for payload in (
        "this is not json at all {{{",
        json.dumps({"jsonrpc": "2.0", "id": 424242}),
    ):
        before = len(wire.frames)
        await wire.send_raw(payload)
        await asyncio.sleep(2.0)  # a window for a reply, not a barrier
        no_reply = wire.frames[before:]
        assert no_reply == [], f"input {payload!r} was answered with {no_reply}"

        # …and the session is still alive.
        alive = await _call(wire, "list_instances", {})
        assert _tool_result(alive).get("isError") is not True

    assert wire.non_frame_stdout == [], wire.non_frame_stdout


async def test_a_large_bounded_result_is_one_parseable_frame_under_a_slow_reader(
    wire, named_instance, fixture_app_server
):
    """MQ-143: a large bounded result, delivered while the client is deliberately
    not reading, and a second request issued into that stalled pipe.

    A screenshot is a real large payload (base64 PNG, tens of KB), which is what
    makes the framing question non-trivial: it must arrive as ONE newline-
    delimited JSON object, not a truncated or split frame. Pausing the reader
    first fills the pipe, so the server hits genuine OS backpressure rather than
    a client that merely happens to be quick. Nothing may deadlock, no byte of
    diagnostics may appear on stdout, and both answers must survive intact.

    Two halves of plan_RELEASE §2.13's MQ-143 wording are deliberately NOT
    claimed here, and are recorded as scope in ``tests/MANUAL_QA_PROTOCOL.md``
    rather than dropped: *simultaneous stderr diagnostics* could not be induced
    (the launcher writes ZERO bytes to stderr — the proxy and backend both log
    to files under the isolated ``HOME``), so the boundedness assertion below
    measures a channel that stays empty rather than one under load; and W9's
    memory ceiling does not exist yet, so no memory claim is made.
    """
    await _navigate(wire, named_instance, f"{fixture_app_server}/index.html")

    wire.pause_reader()
    big_id = await wire.call_tool("take_screenshot", {"instance_id": named_instance})
    small_id = await wire.call_tool("list_instances", {})
    await asyncio.sleep(3.0)  # a window in which the stalled pipe must not break
    assert not wire.answered(big_id), "the paused reader still consumed a frame"

    wire.resume_reader()
    big = await wire.response(big_id, OUTER_BOUND)
    small = await wire.response(small_id, OUTER_BOUND)

    _assert_ok(big, big_id)
    _assert_ok(small, small_id)
    shot = _tool_payload(big)
    encoded = (
        shot["data"] if isinstance(shot, dict) and "data" in shot else _tool_text(big)
    )
    assert len(encoded) > 10_000, f"screenshot payload was only {len(encoded)} chars"
    assert len(wire.frames_for(big_id)) == 1
    assert len(wire.frames_for(small_id)) == 1

    # Framing purity and bounded diagnostics, measured over the whole session.
    assert wire.non_frame_stdout == [], wire.non_frame_stdout
    assert wire.stderr_total_bytes <= wire.stderr_cap, (
        f"stderr exceeded {wire.stderr_cap} bytes ({wire.stderr_total_bytes} seen)"
    )
    assert wire.stderr_truncated is False


# ═══════════════════════════════════════════════════════════════════════════
# MQ-144 — shutdown with work in flight, and the HTTP column
# ═══════════════════════════════════════════════════════════════════════════
async def test_shutdown_with_an_in_flight_call_leaves_no_orphan(
    launcher, space, fixture_app_server
):
    """MQ-144: shut the client down while a call is parked, and account for
    every process afterwards.

    Bounded shutdown is the claim: the proxy exits inside :data:`EXIT_BOUND`
    rather than blocking on an answer that will never come, the shared backend
    is still healthy (a fresh client lists the full registry), and the browser
    the dying session owned is closable through the normal tool — no orphan is
    left for the workspace teardown to discover. The workspace fixture asserts
    that last part globally.
    """
    closing = RawStdioWire(launcher=launcher, env=space["env"], port=space["port"])
    await closing.start()
    token = _token("shutdown")
    instance_id = None
    try:
        await _handshake(closing)
        instance_id = await _spawn(closing, profile="w13-shutdown")
        await _arm(fixture_app_server, token)
        await closing.call_tool(
            "navigate",
            {
                "instance_id": instance_id,
                "url": f"{fixture_app_server}/fault/hang-before-headers?token={token}",
                "timeout": HELD_NAV_TIMEOUT_MS,
            },
        )
        await _await_entered(fixture_app_server, token)

        started = time.monotonic()
        exit_code = await closing.aclose(timeout=EXIT_BOUND)
        assert exit_code is not None, "shutdown did not terminate the proxy"
        assert time.monotonic() - started < EXIT_BOUND, "shutdown was not bounded"
    finally:
        await _release(fixture_app_server, token)

    survivor = RawStdioWire(launcher=launcher, env=space["env"], port=space["port"])
    await survivor.start()
    try:
        handshake = await _handshake(survivor)
        assert len(handshake["tools"]) == REGISTRY_TOOL_COUNT
        if instance_id is not None:
            await _close(survivor, instance_id)
            listed = await _call(survivor, "list_instances", {})
            assert instance_id not in {
                entry["instance_id"] for entry in _tool_payload(listed)
            }
    finally:
        await survivor.aclose()


def test_the_http_column_is_out_of_scope_because_http_is_not_qualified():
    """MQ-144 (scope): the reason this module runs no HTTP node, asserted.

    plan_RELEASE §2.13 asks for HTTP parity *"where HTTP is contract-qualified"*.
    It is not: ``RELEASE_CONTRACT.md`` files HTTP under "described, not
    qualified". Recording that as an assertion rather than as prose means the
    exclusion cannot quietly become false — if HTTP is ever qualified, this node
    goes red and W13's HTTP column has to be filled in rather than forgotten.

    The intentional transport differences, listed explicitly:

    1. HTTP has no stdin, so MQ-142's "close stdin mid-request" has no HTTP
       analogue; the nearest equivalent is dropping the TCP connection, which is
       a different mechanism and would need its own oracle.
    2. HTTP has no private stdout pipe, so MQ-143's "no diagnostic byte reaches
       the framing channel" is not the same property.
    3. The stdio path is a proxy in front of the same backend, so HTTP evidence
       would exercise strictly less machinery — which is exactly why stdio
       evidence must never be copied into an HTTP column, nor the reverse.
    """
    contract = (REPO_ROOT / "RELEASE_CONTRACT.md").read_text(encoding="utf-8")
    assert "### HTTP (described, not qualified)" in contract, (
        "RELEASE_CONTRACT.md no longer files HTTP as described-not-qualified; "
        "W13's HTTP parity column must be revisited"
    )


# ═══════════════════════════════════════════════════════════════════════════
# F-790 — the auto-clone spawn path is bounded against a silent client
# ═══════════════════════════════════════════════════════════════════════════
async def test_a_second_unnamed_spawn_is_bounded_when_roots_list_is_never_answered(
    launcher, tmp_path
):
    """F-790 (RESOLVED): with one instance already holding the master profile, a
    second ``spawn_browser`` that names no ``user_data_dir`` must still ANSWER —
    even though it asks this client a question this client never replies to.

    The mechanism is a frame, which is why only a wire lane can assert it. The
    auto-clone branch of ``clone_storage.resolve_profile_selection`` derives its
    clone name from ``_client_session_seed()``, which awaits
    ``context.list_roots()`` — a **server→client** ``roots/list`` request. MCP
    ``roots`` is OPTIONAL, so a conforming client may never answer; before the
    fix nothing bounded that await and the tool parked forever (measured by hand
    to 420 s: no result, no error, no timeout; the predecessor characterization
    of this node asserted ``answered is False`` at 30 s and passed).

    The client written here is the worst conforming case, not merely a lazy one:
    it **advertises** the ``roots`` capability at ``initialize`` and then never
    answers a single ``roots/list``. A server that trusted the advertisement
    would wait the longest exactly here.

    Four things make this a regression oracle rather than a smoke test:

    * **The reply is the assertion.** ``spawn_browser`` answers inside
      :data:`SPAWN_BOUND` — a full cold-Chrome budget, and a fraction of the
      420 s the unbounded path was measured at.
    * **Exactly one outcome.** One response frame carries that id, so a bounded
      answer is not a duplicate or a stray inbound request being miscounted.
    * **The named mechanism ran.** ``roots/list`` must actually be on the wire
      and must still be unanswered when the reply lands — otherwise the spawn
      took some other route and this node would pass for the wrong reason. The
      backend's own log must carry the ``_client_session_seed`` fallback
      warning, which is what proves the deadline (not luck) released it.
    * **A sensitivity control in the same node.** The FIRST spawn — master
      profile, no round trip — is measured on the same machine seconds earlier,
      and must not itself have asked for roots.

    ``STEALTH_MCP_CLIENT_ROOTS_TIMEOUT_SECONDS`` is pinned small in this
    workspace so the node measures the *bound* rather than the default's
    patience. Own workspace on purpose: the backend that fields the abandoned
    round trip is shared with nothing else.
    """
    with gate_workspace(gate_work_dir(tmp_path)) as space:
        env = {
            **space["env"],
            "STEALTH_MCP_CLIENT_ROOTS_TIMEOUT_SECONDS": str(ROOTS_BOUND),
        }
        wire = RawStdioWire(launcher=launcher, env=env, port=space["port"])
        await wire.start()
        try:
            # Advertise roots, then answer nothing: the F-790 client.
            await _handshake(wire, capabilities={"roots": {"listChanged": False}})

            control_started = time.monotonic()
            first_id = await wire.call_tool(
                "spawn_browser", {"headless": True, "sandbox": False}
            )
            first = await wire.response(first_id, SPAWN_BOUND)
            control_seconds = time.monotonic() - control_started
            _assert_ok(first, first_id)
            instance_id = _tool_payload(first)["instance_id"]
            assert control_seconds < CLONE_HANG_BOUND, (
                f"the control spawn itself took {control_seconds:.1f}s — this "
                "machine cannot decide F-790"
            )
            assert not [f for f in wire.frames if f.get("method") == "roots/list"], (
                "the master-profile spawn asked for roots; F-790's precondition "
                "(only the auto-clone branch does) no longer holds"
            )

            # The regression: the auto-clone spawn answers, bounded, without us
            # ever replying to the roots/list it is about to send.
            second_id = await wire.call_tool(
                "spawn_browser", {"headless": True, "sandbox": False}
            )
            second = await wire.response(second_id, SPAWN_BOUND)
            assert len(wire.frames_for(second_id)) == 1, wire.frames_for(second_id)

            asked = [f for f in wire.frames if f.get("method") == "roots/list"]
            assert asked, (
                "the second spawn never asked the client for roots — F-790's "
                "mechanism has changed and this pin must be re-derived"
            )
            assert asked[0].get("id") is not None, (
                f"roots/list arrived as a notification, not a request: {asked[0]}"
            )
            logs = workspace_backend_logs(space)
            assert "_client_session_seed" in logs, (
                "the bounded await did not log its fallback; the reply may have "
                f"come from somewhere other than the F-790 deadline:\n{logs}"
            )

            # Whichever way the clone spawn landed, it landed TYPED and bounded.
            # A failure here is Chrome's, not the protocol's (see the finding's
            # "What is NOT claimed"); the hang is what this node owns.
            if _tool_result(second).get("isError") is True:
                assert _tool_text(second).strip(), "errored with an empty message"
            else:
                await _close(wire, _tool_payload(second)["instance_id"])

            # The pipe was never the question, and still is not.
            await _call(wire, "list_instances", {})
            await _close(wire, instance_id)
        finally:
            await wire.aclose()
    assert not space["leftover_children"], space["leftover_children"]
