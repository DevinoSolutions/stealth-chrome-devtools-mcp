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
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Coroutine, Iterable, Sequence

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
EXIT_BROKEN_PIPE = 141
EXIT_INTERRUPTED = 130

#: The mark a table puts in front of a url or title that is the LAST KNOWN one
#: rather than the current one. F-874 is the whole reason it exists: a `partial`
#: or `stored` record deliberately carries NO `current_url`, and a table that
#: filled that column from `last_navigated_url` would report a login page for an
#: instance sitting on a feed — the exact defect that finding closed.
LAST_KNOWN_MARK = "~"

#: Where `tools` files a live tool the installed registry has never heard of.
#: Named rather than inline because it is the row that MEANS something: the
#: shell and the backend are different builds.
UNKNOWN_SECTION = "(unknown section)"

#: Table column widths. A url is unbounded and a title is page-authored, so both
#: are clipped; the full values are one `--json` away.
_URL_WIDTH = 52
_TITLE_WIDTH = 28
_ID_WIDTH = 38


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


def _clip(value: object, width: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def instance_rows(records: Sequence[dict[str, object]]) -> list[str]:
    """The `ls` table: one row per instance, and never a stale url in a column
    headed as the current one — see :data:`LAST_KNOWN_MARK`."""
    rows = [f"{'ID':<{_ID_WIDTH}} {'STATE':<18} {'SRC':<7} {'URL':<{_URL_WIDTH}} TITLE"]
    for record in records:
        live = record.get("source") == "active" and not record.get("partial")
        mark = "" if live else LAST_KNOWN_MARK
        url = record.get("current_url") if live else record.get("last_navigated_url")
        title = record.get("title") if live else record.get("last_navigated_title")
        shown_url = _clip(mark + _clip(url, _URL_WIDTH), _URL_WIDTH)
        shown_title = _clip(mark + _clip(title, _TITLE_WIDTH), _TITLE_WIDTH)
        rows.append(
            f"{_clip(record.get('instance_id'), _ID_WIDTH):<{_ID_WIDTH}} "
            f"{_clip(record.get('state'), 18):<18} "
            f"{_clip(record.get('source'), 7):<7} "
            f"{shown_url:<{_URL_WIDTH}} {shown_title}"
        )
    rows.append(
        f"({LAST_KNOWN_MARK} = last known, not current: this instance's live tab "
        "could not be read)"
    )
    return rows


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
      was meant;
    * our own three named refusals, unchanged;
    * ``BrokenPipeError`` — **and it must sit ABOVE the transport row, because
      it is an ``OSError`` and that row would otherwise swallow it** (F-891
      review M2). It comes from OUR OWN ``print``, after a round trip that
      worked, so "could not reach the backend" is a false statement about the
      one thing this function exists to keep straight. Empty message, by
      design: see :data:`EXIT_BROKEN_PIPE`;
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
    import httpx
    from mcp.shared.exceptions import McpError

    from stealth_chrome_devtools_mcp.embedded.backend_client import BackendCallError

    judgements: tuple[tuple[type | tuple[type, ...], int, Callable[..., str]], ...] = (
        (KeyboardInterrupt, EXIT_INTERRUPTED, lambda _exc: "interrupted"),
        (UsageError, EXIT_USAGE, lambda exc: f"error: {exc}"),
        (NoBackendError, EXIT_NO_BACKEND, lambda exc: f"error: {exc}"),
        (BackendCallError, EXIT_TOOL_ERROR, str),
        (BrokenPipeError, EXIT_BROKEN_PIPE, lambda _exc: ""),
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
    for kinds, code, message in judgements:
        if isinstance(exc, kinds):
            return code, message(exc)
    return EXIT_INTERNAL, (
        f"internal error in stealthy ({type(exc).__name__}: {exc}) — "
        "re-run with --traceback for the full stack"
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

    The suppression is the whole mechanism and not laziness: a captured or
    replaced ``sys.stdout`` has no ``fileno``, and a stream that owns no file
    descriptor cannot fail to flush one.
    """
    import contextlib
    import os

    with contextlib.suppress(OSError, ValueError, AttributeError):
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


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

    The ``except`` clause names three types and not ``BaseException``, and each
    is load-bearing: ``Exception`` is the ordinary case and covers
    ``ExceptionGroup``; ``KeyboardInterrupt`` is not an ``Exception``; and a
    ``BaseExceptionGroup`` that is not an ``ExceptionGroup`` is the shape an
    interrupt takes when it reaches us through the transport's own task group.
    ``SystemExit`` is deliberately outside all three — argparse raising it is
    how exit 2 already leaves this process, and catching it here would turn a
    refusal into a status this function invented.

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
        BaseExceptionGroup,
    ) as exc:
        code, line = _verdict(exc)
        if line:
            print(line, file=sys.stderr)
        if getattr(args, "traceback", False):
            import traceback

            traceback.print_exc()
        if code == EXIT_BROKEN_PIPE:
            _abandon_stdout()
        return code
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
        for row in instance_rows(rows):
            print(row)

    return _run(args, body)


def _spawn_arguments(args: argparse.Namespace) -> dict[str, object]:
    """``spawn_browser``'s arguments, from the three sugar flags.

    ``--profile`` is passed STRAIGHT THROUGH as ``user_data_dir`` — a name or an
    absolute path, exactly as the tool takes it, with no interpretation here.
    That is the whole of profile selection at this layer, deliberately: what a
    name resolves to is ``clone_storage.resolve_profile_selection``'s answer and
    the verb PRINTS it back (role + directory), so the CLI can never claim a
    profile the backend did not actually pick.

    **There is no `--master`.** It was built and removed before shipping: an
    unnamed spawn reaches the master profile only while master is FREE, so the
    flag had to name master's DIRECTORY to keep its promise — and naming a
    directory is what ``--profile`` already does. Worse, `master` as a bare NAME
    resolves to ``sessions/master``, a different profile entirely (F-894), so the
    flag would have taught a spelling that is a trap one character away. The
    session vocabulary replacing it (``--session NAME`` + ``--from <session>``,
    with ``default`` reserved for what is today the master profile) is where that
    question belongs; shipping ``--master`` would have meant renaming it in the
    next release.

    Neither ``--headed`` nor ``--headless`` sends no ``headless`` argument at
    all, so the tool's own default decides; sending one either way would be a
    second answer to a question ``spawn_browser`` already answers.
    """
    arguments: dict[str, object] = {}
    if args.profile:
        arguments["user_data_dir"] = args.profile
    if args.headed:
        arguments["headless"] = False
    elif args.headless:
        arguments["headless"] = True
    return arguments


def _spawn_lines(result: dict[str, object]) -> list[str]:
    """What a human needs to see about the browser they just got.

    ``reattached`` first and unmissable: it is the difference between a fresh
    Chrome and the one that still holds the login the caller came for, and F-888
    put that fact in ``spawn_diagnostics``, where nobody reads it.
    """
    diagnostics = result.get("spawn_diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    selection = diagnostics.get("profile_selection")
    selection = selection if isinstance(selection, dict) else {}
    lines = [f"instance   : {result.get('instance_id')}"]
    if diagnostics.get("reattached"):
        lines.append(
            f"REATTACHED : yes — this is the browser that was already running "
            f"(pid {diagnostics.get('reattached_pid')})"
        )
    if diagnostics.get("reattach_declined"):
        lines.append(f"reattach   : declined — {diagnostics['reattach_declined']}")
    lines.append(f"role       : {selection.get('profile_role', '-')}")
    lines.append(f"profile    : {selection.get('user_data_dir', '-')}")
    if selection.get("walked_to"):
        lines.append(
            f"walked to  : {selection['walked_to']} ({selection.get('walk_reason')})"
        )
    return lines


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
            for line in _spawn_lines(record):
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
        print(f"closed {instance_id}" if result else f"{instance_id} was not closed")

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


def _tool_lines(
    tools: Sequence[dict[str, object]], sections: dict[str, str]
) -> list[str]:
    """The live surface, grouped by the section the installed registry knows it
    by. A live tool the registry does not know goes under ``(unknown section)``
    rather than being dropped — the LIVE list is the truth about the running
    backend, and a name this build has never heard of is the most interesting
    row on the page."""
    grouped: dict[str, list[dict[str, object]]] = {}
    for tool in tools:
        section = sections.get(str(tool.get("name")), UNKNOWN_SECTION)
        grouped.setdefault(section, []).append(tool)
    lines: list[str] = []
    for section in sorted(grouped):
        lines.append(f"\n{section} ({len(grouped[section])})")
        for tool in grouped[section]:
            summary = str(tool.get("description") or "").strip().splitlines()
            first = summary[0] if summary else ""
            lines.append(f"  {tool.get('name')!s:<34} {_clip(first, 60)}")
    return lines


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
        for line in _tool_lines(tools, sections):
            print(line)

    return _run(args, body)
