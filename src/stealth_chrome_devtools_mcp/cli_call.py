"""THE one home for the CLI verbs that drive the backend's TOOL surface (F-891)
— ``tools``, ``call``, ``ls``, ``spawn``, ``nav``, ``close``.

Why these six are not in :mod:`cli` beside ``status`` and ``doctor``, when the
whole point of F-891 is that there is ONE CLI and not two: they answer a
different question with a different collaborator. The ops verbs drive the
backend's LIFECYCLE from outside it and must work when there is no backend at
all — that is why every one of them reaches for ``singleton`` and none of them
speaks MCP. These six drive the backend's TOOL surface over MCP and are
meaningless without a live one. ``cli.py`` stays the verb and argparse home, and
its 1000-LOC budget ratchets down only, so the bodies live here and the parser
stays there: the same extraction ``dom_handler`` made for ``text_entry`` and
``control_state``, for the same reason.

**Backend selection is not decided here.** :func:`backend_url` binds
``singleton._probe_backend_status`` — THE one selection ``status``, ``doctor``,
``stop`` and ``kill-orphans`` already make — and ``singleton.ensure_server_
running``, the same startup path the stdio proxy takes, so the cold-start lock,
F-886's step-aside and F-889's adopt-forward rule all apply to a ``stealthy``
invocation exactly as they do to a Claude Code session. It is a thin binding for
the reason every binding in ``singleton`` is one: the suite patches those names,
so they resolve at CALL time. Selected ONCE per command and passed down, which
is F-868's rule at the one place it had not reached yet.

**There is no per-tool argparse mirror.** ``call`` takes a tool name and
key/value pairs, and the tool's own schema on the backend is the validation, so
a 95th tool is reachable from the shell the day it is registered and no release
is needed to teach this file about it. The tool count stays derived, never typed.

Output: JSON when stdout is not a terminal or ``--json`` is given, a table
otherwise (:func:`wants_json`). Errors go to stderr, ONE LINE each: the exit
codes below are a closed set, :func:`_verdict` maps every exception there is
onto one of them, and no invocation leaves a raw traceback paired with Python's
own exit 1 — which is the code that means "the tool said no".
No message here adds anything to what the tool itself
returned — a tool's payload is the caller's own data (cookies, page text, a
profile path naming the operating user), and this file neither logs it nor ships
it anywhere; what the backend already records, it records.

What a TEXT answer looks like is :mod:`cli_render`'s (F-897) — the three tables
and their clipping rule, moved out when this file stood at exactly its
1000-LOC budget and ``spawn`` needed a flag. The line is consequence: an exit
code and the JSON-or-text decision (:func:`wants_json`) are things a script
depends on and stay here; the shape of a table is explicitly not a contract.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp import cli_render

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterable, Sequence

    #: What ``parser.add_subparsers()`` hands back. The name is private to
    #: ``argparse`` and there is no public spelling of it, so it is written
    #: ONCE here rather than at the one function that takes it.
    SubParsers = argparse._SubParsersAction[argparse.ArgumentParser]

    #: What a verb hands :func:`_run`: a coroutine function taking the ONE
    #: selected backend url. Typed rather than `object`, so a verb whose body
    #: takes the wrong shape is a type error and not a runtime one.
    VerbBody = Callable[[str], Coroutine[object, object, None]]

#: Exit codes, and the set is CLOSED: :func:`_verdict` maps every exception
#: there is onto one of them, so no invocation can leave a raw traceback and an
#: exit status a script cannot tell from a tool's refusal (F-891 review M1).
#:
#: 2 is argparse's own, so a usage error this file detects and one argparse
#: detects leave the same trace — and `cli.main`'s "no subcommand" branch
#: returns it too, because printing help is a usage condition. 130 is the
#: shell's convention for SIGINT (128 + 2) and is what a ``Ctrl-C`` costs. 70 is
#: ``EX_SOFTWARE`` from ``sysexits.h`` and means OUR bug, deliberately not 1:
#: exit 1 says "the tool answered and said no", and a script branching on it
#: must not be handed a crash in the CLI wearing the backend's answer.
#:
#: 141 is ``128 + SIGPIPE`` and means THE READER WENT AWAY — `stealthy ls | head
#: -1`, `stealthy tools | less` with `q` pressed early (F-891 review M2). It is
#: its own code because the alternatives are both false statements: a
#: `BrokenPipeError` is an `OSError`, so without this row it reached the
#: transport row and said "could not reach the backend" about a round trip that
#: had already succeeded, and :data:`EXIT_OK` would claim a complete answer for
#: output that was truncated. 141 is also what a coreutils program dying of
#: SIGPIPE reports, so `set -o pipefail` sees from `stealthy` exactly what it
#: sees from `ls | head`. It prints NOTHING: the operator's `head` did what they
#: asked, and a diagnostic on the terminal for the commonest idiom in the shell
#: would be noise.
EXIT_OK = 0
EXIT_TOOL_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_BACKEND = 3
EXIT_INTERNAL = 70
EXIT_INTERRUPTED = 130
EXIT_BROKEN_PIPE = 141


class UsageError(Exception):
    """The caller's invocation is wrong, and it is wrong before any backend was
    asked anything. Exits 2, like argparse's own refusals."""


class NoBackendError(Exception):
    """There is no backend to talk to, and none was started. Exits 3."""


# ── selection ────────────────────────────────────────────────────────────────


def backend_url(*, start: bool) -> tuple[str, bool]:
    """``(url, started)`` for the backend THIS shell would be served by.

    One probe, through the one selection binding, exactly as the ``status``
    block does it (F-868). A ``responsive`` answer is used as-is; anything else
    — no record, a dead record, a wedged backend — means there is nothing to
    drive, and with ``start`` the existing startup path is asked for one.

    **That first step is also why this CLI never evicts a LIVE backend** (F-891
    review S1), and it is a property of the walk rather than a rule added here:
    ``backend_liveness.probe_recorded`` asks ``adoption_candidates`` and the
    liveness ladder and NOTHING about identity, so a backend built from a
    different source tree answers, is adopted, and the only path that can evict
    — ``ensure_server_running`` — is never reached. A fingerprint mismatch is
    the proxy REUSE GATE's business, not this CLI's: a proxy is about to serve a
    whole session off that backend, while a one-shot command only borrows the
    socket, and the thing it would replace is somebody's live session.

    **What the second step still costs, stated rather than hidden.** When
    nothing answers, ``ensure_server_running`` is the proxy's own cold start and
    it CAN evict: a WEDGED backend of a foreign identity on this display
    context, owning no live browser, is terminated and replaced exactly as a
    proxy start from this checkout would. It is not narrowed here, because a
    second startup path is a second way to start a backend (convention 4) and
    this one carries the cold-start lock every caller depends on. ``--no-start``
    is the opt-out, ``backend_eviction.protected`` spares anything still holding
    a browser, and RUNBOOK says so where ``--no-start`` is documented.

    Widening the first step to "any recorded backend that answers, whatever its
    display context" was built and REVERTED: it prevents no eviction — the entry
    it newly reaches is on another desktop, so the cold start targets a
    different port and terminates nothing — while a ``spawn --headed`` driven
    through it opens a window on a desktop the operator is not watching, and the
    CLI silently stops agreeing with its own ``status``.

    ``started`` is what the caller waits on, and it is the whole reason this
    answers a PAIR. ``ensure_server_running`` returns immediately (the spawn
    runs under the cold-start lock on a daemon thread), so a backend we asked
    for has to be waited out; a backend that just answered ``responsive`` has
    not, and probing it again would spend the cold-start deadline re-proving
    what the selection had established one line earlier.

    Blocking calls on the event loop are fine here and nowhere else in this
    tree: this is a one-shot process with nothing else to schedule.
    """
    from stealth_chrome_devtools_mcp.embedded import singleton

    status, port = singleton._probe_backend_status()
    if status == "responsive" and port is not None:
        return singleton._backend_http_url(port), False
    if not start:
        raise NoBackendError(
            f"no backend is serving this shell (state: {status}) and --no-start "
            "was given; drop --no-start to have one started"
        )
    return singleton._backend_http_url(singleton.ensure_server_running()), True


async def await_ready(url: str) -> None:
    """Wait out a cold start on ``singleton``'s own deadline, or raise."""
    from stealth_chrome_devtools_mcp.embedded import singleton

    if not await singleton._await_backend_http(url):
        raise NoBackendError(f"the backend at {url} did not become ready")


# ── argument parsing ─────────────────────────────────────────────────────────


def parse_arguments(pairs: Sequence[str], blob: str | None) -> dict[str, object]:
    """A tool's arguments, from ``--json`` and ``--arg key=value`` pairs.

    Each ``--arg`` value is JSON when it parses as JSON and the literal string
    otherwise, which is what makes ``headless=false`` a bool, ``viewport_width=
    1200`` an int and ``browser_args=["--x"]`` a list, while
    ``user_data_dir=seller-central`` and ``C:\\Users\\me\\profile`` stay strings
    instead of failing. The split is on the FIRST ``=`` only, so a url's query
    string survives.

    ``--arg`` wins per key, because it is the more specific thing to type: a
    caller pasting a saved ``--json`` object and overriding one field means the
    override.
    """
    import json

    arguments: dict[str, object] = {}
    if blob is not None:
        try:
            parsed = json.loads(blob)
        except ValueError as exc:
            raise UsageError(f"--json is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise UsageError(
                f"--json must be a JSON object, got {type(parsed).__name__}"
            )
        arguments.update(parsed)
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise UsageError(f"--arg {pair!r} is not key=value")
        arguments[key] = _scalar(raw)
    return arguments


def _scalar(raw: str) -> object:
    import json

    try:
        return json.loads(raw)
    except ValueError:
        return raw


def resolve_instance(records: Iterable[dict[str, object]], given: str) -> str:
    """An instance id from a full id or a unique prefix of one.

    An exact match wins outright, so an id that happens to be a prefix of a
    longer one is still reachable by typing it in full. An ambiguous prefix
    names every match rather than picking one — picking would eventually close
    the wrong browser.
    """
    ids = [str(record.get("instance_id")) for record in records]
    if given in ids:
        return given
    matches = [candidate for candidate in ids if candidate.startswith(given)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise UsageError(
            f"no instance matches {given!r} (have: {', '.join(ids) or 'none'})"
        )
    raise UsageError(f"{given!r} is ambiguous — matches {', '.join(sorted(matches))}")


# ── output ───────────────────────────────────────────────────────────────────


def wants_json(stream: object, *, explicit: bool) -> bool:
    """JSON when asked, and JSON whenever stdout is not a terminal.

    A table that reaches a pipe is a table something downstream has to parse,
    and the shape of a table is not a contract. A stream that cannot say whether
    it is a terminal is treated as one that is not.
    """
    if explicit:
        return True
    isatty = getattr(stream, "isatty", None)
    return not (callable(isatty) and isatty())


def _emit_json(payload: object) -> None:
    import json

    print(json.dumps(payload, indent=2, default=str))


# ── the six verbs ────────────────────────────────────────────────────────────


def _unwrapped(exc: BaseException) -> BaseException:
    """The one exception inside a singly-nested ``ExceptionGroup``, else ``exc``.

    The transport runs under ``anyio.create_task_group``, which raises a GROUP,
    so without this every transport failure would arrive as the one shape
    :func:`_verdict` cannot classify and would be reported as our own bug. Only
    a group carrying exactly ONE leaf is unwrapped: a group of several is a
    genuinely composite failure and gets the internal verdict, because picking
    one of them to report would be picking which half of the truth to tell.
    """
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


def _verdict(exc: BaseException) -> tuple[int, str]:
    """``(exit code, one line for stderr)`` for anything that escaped a verb.

    The set is CLOSED — every branch returns and the last one is the catch-all —
    because a CLI that lets an exception through hands a script exit 1 and a
    traceback, which is byte-indistinguishable from "the tool said no" (F-891
    review M1).

    The judgements, in the order they are asked:

    * ``KeyboardInterrupt`` is not a failure to describe — the operator already
      knows what they pressed — so it costs one word and
      :data:`EXIT_INTERRUPTED`. It is handled at all because the alternative is
      Python's own traceback and exit 1, the TOOL-error code, for a key that
      was meant. ``asyncio.CancelledError`` shares the row: it is a
      ``BaseException`` that is neither an ``Exception`` nor a
      ``KeyboardInterrupt`` nor a group, so a cancellation arriving from
      anywhere but the SIGINT ``asyncio.run`` already converts was the one
      remaining shape that could leave a set advertised as closed;
    * our own three named refusals, unchanged;
    * the READER WENT AWAY — :func:`_reader_gone` — **and it must be asked
      BEFORE the transport row, because it is an ``OSError`` and that row would
      otherwise swallow it** (F-891 review M2). It comes from OUR OWN ``print``,
      after a round trip that worked, so "could not reach the backend" is a
      false statement about the one thing this function exists to keep
      straight. Empty message, by design: see :data:`EXIT_BROKEN_PIPE`. Keyed
      on the ERRNO and not only the type, because the same closed pipe is
      ``BrokenPipeError`` on POSIX and a bare ``OSError(EINVAL)`` on Windows
      (round 4, measured through a real pipe: the type-keyed row passed every
      double and answered 3 on Windows) — the one judgement that reads an
      attribute, which is why it stands in front of the table rather than in
      it. **Keyed on the error and not on the SITE, and that has a residual
      worth naming** (delta review S3): ``httpx`` writing to a backend socket
      the peer closed also raises ``BrokenPipeError``, and it is reported here
      as a broken pipe — silent, 141 — where the truthful answer is 3. "It
      comes from our own ``print``" is the overwhelmingly common case, not a
      guarantee. Narrowing it to the site would mean a flag set around every
      emit and read here, i.e. a second way to know where an exception came
      from; the residual is the cheaper side of that trade and is stated rather
      than hidden;
    * a TRANSPORT failure — ``httpx`` could not reach it, the socket died, the
      read budget expired — is :data:`EXIT_NO_BACKEND` and not a tool error:
      nothing on the backend ever saw the request, so there is no answer to
      report and re-running is the remedy. ``OSError`` covers the socket layer
      and is the base of ``ConnectionError``; ``httpx.HTTPError`` is the base of
      every connect/read/protocol failure the client raises; ``TimeoutError``
      is what the read budget becomes;
    * ``McpError`` — the backend answered, at the protocol level, that it will
      not do this (an unknown tool is the everyday one) — is a TOOL error,
      because the round trip worked and the fix is in what was asked;
    * anything else is :data:`EXIT_INTERNAL`, a bug here.

    A TABLE walked in order rather than a ladder of ``if``\\ s, because the order
    IS the policy and a table cannot grow a branch that silently precedes the
    ones above it. It is built inside the function because two of its types are
    lazy imports this file must not pay for on an ops verb.
    """
    import asyncio

    import httpx
    from mcp.shared.exceptions import McpError

    from stealth_chrome_devtools_mcp.embedded.backend_client import BackendCallError

    judgements: tuple[tuple[type | tuple[type, ...], int, Callable[..., str]], ...] = (
        (
            (KeyboardInterrupt, asyncio.CancelledError),
            EXIT_INTERRUPTED,
            lambda _exc: "interrupted",
        ),
        (UsageError, EXIT_USAGE, lambda exc: f"error: {exc}"),
        (NoBackendError, EXIT_NO_BACKEND, lambda exc: f"error: {exc}"),
        (BackendCallError, EXIT_TOOL_ERROR, str),
        (
            (httpx.HTTPError, OSError, TimeoutError),
            EXIT_NO_BACKEND,
            lambda exc: (
                f"error: could not reach the backend ({type(exc).__name__}: {exc})"
            ),
        ),
        (McpError, EXIT_TOOL_ERROR, lambda exc: f"error: the backend refused: {exc}"),
    )

    exc = _unwrapped(exc)
    # In front of the table and not a row of it: the one judgement keyed on an
    # errno, and it has to precede the transport row's `OSError` (review M2).
    if _reader_gone(exc):
        return EXIT_BROKEN_PIPE, ""
    for kinds, code, message in judgements:
        if isinstance(exc, kinds):
            return code, message(exc)
    return EXIT_INTERNAL, (
        f"internal error in stealthy ({type(exc).__name__}: {exc}) — "
        "re-run with --traceback for the full stack"
    )


#: A name and not an inline test so the suite can pin BOTH arms on every cell.
_WINDOWS = sys.platform == "win32"


def _reader_gone(exc: BaseException) -> bool:
    """Is ``exc`` a write to a stdout whose reader has left? Two measured
    spellings of ONE event: ``BrokenPipeError`` (EPIPE) everywhere, and a bare
    ``OSError(EINVAL)`` on WINDOWS ONLY (round 4: ``tools --json`` into a real
    closed pipe answered 3 under the type-keyed row). EINVAL is a broad errno —
    a cold start's own ``OSError(22)`` would read as 141 — so the arm is scoped
    to the one platform it was measured on; finding §6.14 owns what is left."""
    import errno

    return isinstance(exc, BrokenPipeError) or (
        isinstance(exc, OSError)
        and (exc.errno == errno.EPIPE or (_WINDOWS and exc.errno == errno.EINVAL))
    )


def _abandon_stdout() -> None:
    """Point this process's stdout at the void, once the reader has gone.

    Returning :data:`EXIT_BROKEN_PIPE` is not enough on its own. Whatever
    ``print`` had buffered is still flushed when the interpreter finalises, that
    write fails again OUTSIDE every handler there is, and CPython answers with
    ``Exception ignored in: <_io.TextIOWrapper name='<stdout>'>`` on stderr and
    exit **120** — a code outside the set this file advertises, produced after
    the set had been honoured. Re-pointing fd 1 at the null device makes that
    final flush a no-op, so the code we chose is the code the shell sees.

    Tolerating a missing ``fileno`` is the whole mechanism and not laziness: a
    captured or replaced ``sys.stdout`` has no descriptor, and a stream that
    owns none cannot fail to flush one.

    Called from TWO places — the verdict in :func:`_run` when a pipe error
    surfaced inside a tool verb's body, and the ONE flush that ends
    ``cli.main`` for every verb of both tables (F-891 delta review M1, S2). It
    is idempotent and costs one ``open``.

    **Nothing in this body may raise.** Both callers sit inside a handler, so
    an escape here reaches ``sys.excepthook`` — traceback, exit 1, a Sentry
    ship — from the function whose job is to keep the set closed (round-4 M1:
    ``os.open`` sat unguarded between two guarded calls and EMFILE took it).
    """
    import os

    try:
        target = sys.stdout.fileno()
    except (OSError, ValueError, AttributeError):
        # A captured or replaced stdout owns no descriptor, so there is no
        # final flush of one to fail — asked FIRST so the null device is never
        # opened for a stream that cannot use it (review nit: the old single
        # statement opened the fd and then abandoned it when `fileno` raised).
        return
    try:
        spare = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        # Descriptor exhaustion. The exit flush may then still fail and cost
        # us 120, but that is the interpreter's code and not an escape of ours.
        return
    try:
        os.dup2(spare, target)
    except OSError:
        # A `dup2` that fails leaves fd 1 exactly as it was — the redirect did
        # not happen, nothing was half-done — so the worst outcome is the 120
        # this function exists to prevent, never a traceback out of `main`.
        pass
    finally:
        with contextlib.suppress(OSError):
            os.close(spare)


def _run(args: argparse.Namespace, body: VerbBody) -> int:
    """Select the backend ONCE, hand the url to ``body``, and turn whatever
    escapes into an exit code.

    Two rules live here and nowhere else. The selection is made once per command
    and passed down — F-868's rule, arriving at the one surface it had not
    reached — so every call a verb makes goes to the backend the command
    reported. And this is the ONE place an exception becomes a status, so no
    verb body carries its own error policy and no two of them can disagree about
    what exit 1 means. A verb that refuses its own arguments raises
    :class:`UsageError` from inside ``body``: the selection in front of it is a
    local probe, never a backend round trip, so a bad invocation still costs the
    backend nothing.

    The ``except`` clause names four types and not ``BaseException``, and each
    is load-bearing: ``Exception`` is the ordinary case and covers
    ``ExceptionGroup``; ``KeyboardInterrupt`` is not an ``Exception``;
    ``asyncio.CancelledError`` is neither, and is safe to catch HERE precisely
    because this is not inside a task — ``asyncio.run`` has already torn the
    loop down, so nothing is being deprived of its cancellation (delta review
    nit); and a ``BaseExceptionGroup`` that is not an ``ExceptionGroup`` is the
    shape an interrupt takes when it reaches us through the transport's own
    task group. ``SystemExit`` is deliberately outside all four — argparse
    raising it is how exit 2 already leaves this process, and catching it here
    would turn a refusal into a status this function invented.

    ``--traceback`` PRINTS the stack after the one line, and deliberately does
    NOT re-raise (F-891 review S1). Re-raising looked equivalent and was not:
    the exception then left ``main``, past ``cli.sentry_init()``, and
    ``sys.excepthook`` shipped it — carrying a ``BackendCallError`` built from
    the tool's own words, which is exactly what this module's docstring promises
    it "neither logs nor ships anywhere". ``BackendCallError`` is not a
    ``tool_errors.ToolError``, so ``expected_events``' convention rule would not
    have dropped it either. The operator gets the same stack on stderr, the
    process still exits on the verdict's code, and nothing leaves the machine.
    """
    import asyncio

    async def drive() -> None:
        url, started = backend_url(start=not args.no_start)
        if started:
            await await_ready(url)
        await body(url)

    try:
        asyncio.run(drive())
    except (
        # BLE001 fired the moment `--traceback` stopped re-raising (review S1),
        # and the blind catch is the whole contract rather than an oversight:
        # this is THE one place an exception becomes a status, and an exception
        # that escaped would be the traceback-plus-exit-1 that F-891 review M1
        # exists to remove. `_verdict` is where the breadth is paid back — it
        # names every kind it can and calls the rest OUR bug, code 70.
        Exception,  # noqa: BLE001  PERMANENT(F-891 — the closed exit-code set)
        KeyboardInterrupt,
        asyncio.CancelledError,
        BaseExceptionGroup,
    ) as exc:
        code, line = _verdict(exc)
        if line:
            # The reader of STDERR can be gone too (`... 2>&1 | head -1`), and
            # this print is inside the handler, so its own BrokenPipeError
            # would leave `_run`, leave `main`, and reach `sys.excepthook` —
            # the traceback-plus-exit-1 (and the Sentry ship) that this whole
            # function exists to prevent, produced by the line reporting it.
            # The message is already lost either way; the CODE is what a script
            # reads (review S2).
            with contextlib.suppress(OSError):
                print(line, file=sys.stderr)
        if getattr(args, "traceback", False):
            import traceback

            # Same closed-stderr door as the one-line report above, one line
            # below it (round-4 S1) — and `--traceback` is exactly when an
            # operator is piping output around.
            with contextlib.suppress(OSError):
                traceback.print_exc()
        if code == EXIT_BROKEN_PIPE:
            _abandon_stdout()
        return code
    # The SUCCESS path's >8 KB tail (review M1) is flushed by `cli._delivered`,
    # ONCE, after whichever dispatch table answered — deliberately not here,
    # because the eight ops verbs had the same hole (round-4 S2).
    return EXIT_OK


def _timeout(args: argparse.Namespace) -> float:
    from stealth_chrome_devtools_mcp.embedded import backend_client

    given = getattr(args, "timeout", None)
    return backend_client.DEFAULT_TIMEOUT_SECONDS if given is None else float(given)


async def _call(
    url: str, args: argparse.Namespace, name: str, arguments: dict[str, object]
) -> object:
    from stealth_chrome_devtools_mcp.embedded import backend_client

    return await backend_client.call_tool(
        url, name, arguments, budget_seconds=_timeout(args)
    )


def cmd_call(args: argparse.Namespace) -> int:
    """`call <tool> [--arg k=v ...] [--json '<object>']` — THE core verb.

    Always prints JSON: the answer is a tool's structured result and there is no
    human rendering of 94 different shapes to choose between, which is also why
    ``--json`` on THIS verb means the arguments object rather than an output
    mode. Nothing else in the CLI spells it that way, and the help text says so.
    """

    async def body(url: str) -> None:
        arguments = parse_arguments(args.arg or [], args.json)
        _emit_json(await _call(url, args, args.tool, arguments))

    return _run(args, body)


def cmd_ls(args: argparse.Namespace) -> int:
    """`ls` — ``list_instances`` as a table, or as its own record under JSON.

    The table deliberately has no headed/headless and no profile column:
    ``list_instances`` does not report either (F-874's three record shapes), and
    a column filled from somewhere else would be this CLI asserting something
    the backend never said. ``get_instance_state`` is where those live, one
    ``stealthy call`` away.
    """

    async def body(url: str) -> None:
        records = await _call(url, args, "list_instances", {})
        rows = records if isinstance(records, list) else []
        if wants_json(sys.stdout, explicit=args.json):
            _emit_json(records)
            return
        if not rows:
            print("no browser instances.")
            return
        for row in cli_render.instance_rows(rows):
            print(row)

    return _run(args, body)


def _spawn_arguments(args: argparse.Namespace) -> dict[str, object]:
    """``spawn_browser``'s arguments, from the sugar flags.

    ``--session`` goes STRAIGHT THROUGH as ``session``, ``--from`` as
    ``seed_from`` and ``--profile`` as ``user_data_dir``, uninterpreted: what a
    name resolves to is the resolver's answer and the verb PRINTS it back, so
    the CLI never claims a profile the backend did not pick. **``--from``
    carries no opinion of its own** (F-897) — whether that session exists, is
    open, is the shared one, or names a session that already exists is decided
    by ``profile_seed`` on the backend, which is the only place that can see
    any of it, and a CLI-side pre-check would be a second answer that goes
    stale between the check and the spawn. ``--profile`` is DEPRECATED and
    undocumented (F-896),
    accepted for one release with a stderr line naming its replacement; the
    path door is ``stealthy call spawn_browser --arg user_data_dir=<path>``,
    which is what ``call`` is for, and a second sugar flag for one tool
    argument is convention 4's defect. **Both flags at once are left for the
    BACKEND to refuse** — that rule lives in ``profile_seed.profile_request``
    and nowhere else, so the two surfaces cannot come to disagree.

    Neither ``--headed`` nor ``--headless`` sends no ``headless`` at all, so the
    tool's own default decides rather than this CLI holding a second opinion.
    """
    arguments: dict[str, object] = {}
    if args.session:
        arguments["session"] = args.session
    if args.seed_from:
        arguments["seed_from"] = args.seed_from
    if args.profile:
        print(
            "note: --profile is deprecated — use --session NAME, or `stealthy "
            "call spawn_browser --arg user_data_dir=<path>` for a path.",
            file=sys.stderr,
        )
        arguments["user_data_dir"] = args.profile
    if args.headed:
        arguments["headless"] = False
    elif args.headless:
        arguments["headless"] = True
    return arguments


def cmd_spawn(args: argparse.Namespace) -> int:
    """`spawn` — sugar over ``spawn_browser``, plus ``--url`` as a navigation.

    ``spawn_browser`` takes no url (it opens a blank tab), so ``--url`` is a
    second call rather than an argument. Both run under the ONE selection this
    command made.

    **The spawn's answer is emitted BEFORE the navigation is attempted** (F-891
    review M3). The browser exists the moment the first call returns and the
    second call can fail — a url that will not load, a challenge page, an
    expired budget — so printing at the end meant a failed navigation took the
    instance id down with it and left a running browser, on a headed spawn a
    visible window, that the caller could not name to ``nav`` or ``close``.
    Order is the whole fix: report what EXISTS, then do the thing that might not
    work. Nothing is swallowed to buy it — the navigation's failure still
    reaches :func:`_run` and still exits 1, so the caller gets the id AND the
    error, which is what it takes to act.
    """

    async def body(url: str) -> None:
        result = await _call(url, args, "spawn_browser", _spawn_arguments(args))
        record = result if isinstance(result, dict) else {}
        if wants_json(sys.stdout, explicit=args.json):
            _emit_json(result)
        else:
            for line in cli_render.spawn_lines(record):
                print(line)
        if args.url and record.get("instance_id"):
            await _call(
                url,
                args,
                "navigate",
                {"instance_id": record["instance_id"], "url": args.url},
            )

    return _run(args, body)


async def _resolved(url: str, args: argparse.Namespace, given: str) -> str:
    """The instance the caller meant, from a full id or a unique prefix.

    The listing is what makes a PREFIX work at all, and it is also what turns
    "there is no such instance" into a refusal before anything is navigated or
    closed.
    """
    records = await _call(url, args, "list_instances", {})
    return resolve_instance(records if isinstance(records, list) else [], given)


def cmd_nav(args: argparse.Namespace) -> int:
    """`nav <instance> <url> [--wait ...]` — thin sugar over ``navigate``."""

    async def body(url: str) -> None:
        instance_id = await _resolved(url, args, args.instance)
        arguments: dict[str, object] = {"instance_id": instance_id, "url": args.url}
        if args.wait:
            arguments["wait_until"] = args.wait
        result = await _call(url, args, "navigate", arguments)
        if wants_json(sys.stdout, explicit=args.json):
            _emit_json(result)
            return
        record = result if isinstance(result, dict) else {}
        print(f"{instance_id} -> {record.get('url', args.url)}")
        if record.get("title"):
            print(f"title: {record['title']}")

    return _run(args, body)


def cmd_close(args: argparse.Namespace) -> int:
    """`close <instance>` — thin sugar over ``close_instance``."""

    async def body(url: str) -> None:
        instance_id = await _resolved(url, args, args.instance)
        result = await _call(url, args, "close_instance", {"instance_id": instance_id})
        if wants_json(sys.stdout, explicit=args.json):
            _emit_json(result)
            return
        # F-910: the tool answers a RECORD (`closed` + `seed_refreshed`), and a
        # record is always truthy — so `if result` would have printed "closed"
        # for a close that failed. A bare bool is still accepted because this
        # CLI adopts whichever backend answers, including an older build's.
        record = result if isinstance(result, dict) else {"closed": result}
        print(
            f"closed {instance_id}"
            if record.get("closed")
            else f"{instance_id} was not closed"
        )
        if record.get("seed_refreshed") is False:
            # The seed is what every new session is copied from, so a refusal
            # here is the caller's business even though the close succeeded.
            print(f"seed not refreshed: {record.get('seed_error')}")

    return _run(args, body)


def _sections() -> dict[str, str]:
    """tool name -> section, from the INSTALLED build's registry.

    ``SECTION_TOOLS`` is filled by ``embedded/server.py``'s binding loop, so
    reaching it means importing the server module — which is why this is only
    paid by ``tools`` and by no other verb here. ``cli._server()` is the door,
    the same one ``doctor`` and ``stop`` use, so the read-only import semantics
    (``STEALTH_MCP_NO_AUTO_RECOVERY=1``, F-890's env scrub) are not re-spelled.
    """
    from stealth_chrome_devtools_mcp import cli
    from stealth_chrome_devtools_mcp.embedded import tool_registry

    cli._server()
    return {
        name: section
        for section, names in tool_registry.SECTION_TOOLS.items()
        for name in names
    }


def cmd_tools(args: argparse.Namespace) -> int:
    """`tools [--section X] [--json]` — the LIVE tool surface of the backend.

    The live list is the truth about the RUNNING backend; the registry is the
    truth about the INSTALLED build. They are normally the same and when they
    are not, that fact is the answer — the shell and the backend are different
    builds — so the count line states both rather than presenting either as the
    number.
    """

    async def body(url: str) -> None:
        from stealth_chrome_devtools_mcp.embedded import backend_client

        tools = await backend_client.list_tools(url, budget_seconds=_timeout(args))
        sections = _sections()
        if args.section:
            tools = [
                tool
                for tool in tools
                if sections.get(str(tool.get("name"))) == args.section
            ]
        if wants_json(sys.stdout, explicit=args.json):
            _emit_json(tools)
            return
        print(
            f"{len(tools)} tools on the live backend; the installed build's "
            f"registry lists {len(sections)}"
        )
        for line in cli_render.tool_lines(tools, sections):
            print(line)

    return _run(args, body)


# ── the six verbs' own parser surface ────────────────────────────────────────


def _backend_flags() -> argparse.ArgumentParser:
    """The flags every tool-driving verb shares (F-891).

    A parent parser rather than six copies: ``--no-start``, ``--timeout`` and
    ``--traceback`` mean the same thing for all six, and six declarations are
    six places for one of them to drift. ``--json`` is deliberately NOT here —
    on ``call`` it names the arguments object, not an output mode.

    ``--traceback`` is the one way to see a stack from these verbs, because
    :func:`_run` turns every exception into one stderr line and a closed exit
    code (F-891 review M1). A flag and not an env var: this package reads its
    environment in ``settings.py`` and nowhere else. It PRINTS the stack and
    never re-raises — an exception leaving ``main`` goes past `sentry_init()`
    and ships the tool's own payload off the machine (review S1).

    ``--no-start``'s help names the consequence it prevents, not merely what it
    switches off (F-891 review S1): a responsive backend is always adopted,
    whatever build it is, but a cold start is the PROXY's cold start and can
    evict a wedged one. That is the whole reason an operator would reach for
    this flag, and a help string saying only "do not start one" leaves them to
    discover it.
    """
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--no-start",
        action="store_true",
        help=(
            "fail instead of starting a backend when none is running; a live "
            "backend is always used as-is, but starting one can evict a wedged "
            "backend of another build"
        ),
    )
    shared.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="per-call budget in seconds (default: the client's)",
    )
    shared.add_argument(
        "--traceback",
        action="store_true",
        help="also print the full stack, in addition to the one-line message",
    )
    return shared


def add_parsers(sub: SubParsers) -> None:
    """Contribute the six verbs' subparsers to ``cli``'s ONE parser tree.

    Parsers and bodies in one file because they are one question. They were
    split — declarations in ``cli.py``, meaning here — and the split showed:
    this module's docstring explained what ``--session`` MEANS while the
    ``add_argument`` offering it lived in another file, so adding a flag to
    ``spawn`` meant editing two homes and either could drift (convention 4,
    reached from the side where the second way is a second FILE). ``cli.py``
    still owns the parser TREE, both script names and the ops verbs.

    ``call`` has no per-tool mirror on purpose — the tool's own schema on the
    backend is the validation, so a 95th tool is reachable the day it is
    registered.
    """
    shared = _backend_flags()

    tools = sub.add_parser(
        "tools", parents=[shared], help="list the live backend's tools"
    )
    tools.add_argument("--section", default=None, help="only tools in this section")
    tools.add_argument("--json", action="store_true", help="JSON output")

    call = sub.add_parser(
        "call",
        parents=[shared],
        help="call ANY tool on the live backend (the core verb)",
    )
    call.add_argument("tool", help="tool name, e.g. spawn_browser")
    call.add_argument(
        "--arg",
        action="append",
        metavar="KEY=VALUE",
        help="one argument; the value is JSON when it parses, else a string",
    )
    call.add_argument(
        "--json",
        default=None,
        metavar="OBJECT",
        help="the whole arguments object as JSON (--arg wins per key). On THIS "
        "verb --json is the arguments, not an output mode: `call` always "
        "prints the tool's structured result as JSON.",
    )

    listing = sub.add_parser("ls", parents=[shared], help="list browser instances")
    listing.add_argument("--json", action="store_true", help="JSON output")

    spawn = sub.add_parser("spawn", parents=[shared], help="spawn a browser")
    spawn.add_argument(
        "--session",
        default=None,
        metavar="NAME",
        help="persistent session NAME, not a path: keeps its cookies and "
        "logins. `default` is the shared session new ones are copied from",
    )
    spawn.add_argument(
        "--from",
        dest="seed_from",
        default=None,
        metavar="SESSION",
        help="when --session NAMES a session that does not exist yet, copy it "
        "from this one instead of `default`; the source must exist and, "
        "unless it is `default`, must not be open",
    )
    # F-896: accepted for one release, undocumented, and it says so on stderr.
    spawn.add_argument("--profile", default=None, help=argparse.SUPPRESS)
    headed = spawn.add_mutually_exclusive_group()
    headed.add_argument("--headed", action="store_true", help="show a window")
    headed.add_argument("--headless", action="store_true", help="no window")
    spawn.add_argument("--url", default=None, help="navigate here after spawning")
    spawn.add_argument("--json", action="store_true", help="JSON output")

    nav = sub.add_parser("nav", parents=[shared], help="navigate an instance")
    nav.add_argument("instance", help="instance id, or a unique prefix of one")
    nav.add_argument("url")
    nav.add_argument(
        "--wait",
        default=None,
        choices=("load", "domcontentloaded", "networkidle"),
        help="milestone to wait for (default: the tool's)",
    )
    nav.add_argument("--json", action="store_true", help="JSON output")

    close = sub.add_parser("close", parents=[shared], help="close an instance")
    close.add_argument("instance", help="instance id, or a unique prefix of one")
    close.add_argument("--json", action="store_true", help="JSON output")


#: Verb name -> body, for ``cli.main``'s dispatch. Here rather than as six
#: one-line shims in ``cli.py``: the shims restated this mapping in a second
#: place, and a verb added here but forgotten there is a parser that reaches no
#: body. ``cli.py`` keeps its own table for the ops verbs and consults this one
#: for everything it does not recognise.
DISPATCH: dict[str, Callable[[argparse.Namespace], int]] = {
    "tools": cmd_tools,
    "call": cmd_call,
    "ls": cmd_ls,
    "spawn": cmd_spawn,
    "nav": cmd_nav,
    "close": cmd_close,
}
