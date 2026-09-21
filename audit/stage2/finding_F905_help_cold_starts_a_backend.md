# F-905 — asking for help started a backend

**Severity** MED · **Status** FIXED on `fix/F903-suite-can-reach-real-state-dir`
· **Found by** F-903 round 2, while writing a pin that was supposed to be the
safe way to smoke-test the entrypoint · **Related** F-904 (the `__main__.py`
guard, same entry point), F-903 (the suite fence, whose census found both)

## 1. The claim

`python -m stealth_chrome_devtools_mcp --help` does not print help. It
cold-starts a real backend into `~/.stealth-mcp` and then serves an stdio proxy
against it, until the operator notices and interrupts it.

The two spellings a person would try are `--help` and `-h`, and both did it.

## 2. How it was found

F-903's round-2 brief asked for a pin that `python -m … --help` exits 0 — the
ordinary "did the packaging survive" smoke test, and a reasonable thing to ask
for. Before running it I read `server.main` to confirm it was safe, found that
it was not, and did not run it. The pin shipped as `--transport http --help`
instead, with the bare spelling pinned by TRIPWIRING the cold start rather than
performing one. This finding is what that tripwire is about.

Nothing was cold-started to prove it. The evidence is the code path plus the
tripwire node, which fails if the request reaches `ensure_server_running`.

## 3. Mechanism

`src/stealth_chrome_devtools_mcp/server.py`'s `main()` is a SHIM. It decides
exactly one thing — run the stdio proxy here, or `runpy` the real backend
(`embedded/server.py`) into this process — and it decides it from three flags:

```python
parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--transport", default="stdio")
parser.add_argument("--standalone", action="store_true")
parser.add_argument("--singleton-port", type=int, default=DEFAULT_PORT)
known, extra = parser.parse_known_args()

if known.transport == "stdio" and not known.standalone:
    …
    port = ensure_server_running(port=known.singleton_port)   # ← --help got here
    if port is not None:
        run_stdio_proxy(port)
        return

runpy.run_path(str(EMBEDDED_DIR / "server.py"), run_name="__main__")
```

**`add_help=False` and `parse_known_args` are both deliberate, and both are
right.** Every argument other than those three belongs to
`embedded/server.py`'s `build_arg_parser()` — the real one, with `--transport`,
`--host`, `--port`, `--sections`, `--xpool-safe` and the rest — which is reached
through the `runpy` load and re-reads `sys.argv` for itself. `parse_known_args`
is what lets an unknown argument pass through to it. And `add_help=True` here
would be worse than the bug in one respect: `--help` would print the SHIM's
three-flag usage and exit, hiding the real interface behind a summary of an
implementation detail.

So the pass-through is the design. The defect is what the help request passed
through INTO: with the default `--transport stdio` and no `--standalone`, the
condition above is true, and a help request became a proxy start.

The blast radius is not confined to a curious operator. `ensure_server_running`
adopts or cold-starts a backend for this display context, which on a machine
with a stale record can also EVICT one (F-886's rule spares a backend owning
live browsers; an idle one is evicted). "I typed `--help`" is not consent for
any of that.

## 4. The fix

One predicate, keyed on the request and on nothing else:

```python
_ANSWER_AND_EXIT = frozenset({"-h", "--help", "--list-sections"})
…
wants_answer = _ANSWER_AND_EXIT.intersection(extra)

if known.transport == "stdio" and not known.standalone and not wants_answer:
```

A question now takes the branch that can ANSWER it.

**The set is "answer and exit", not "help"** — and it took a review to get the
membership right. The first version of this fix shipped `{-h, --help}` and left
the sibling behind: `--list-sections`, whose own help text is *"List all
available tool sections and exit"*, is unknown to the shim in exactly the same
way and reached `ensure_server_running` in exactly the same way (measured with
the same tripwire, before the widening). The rule is what
`build_arg_parser()` handles by PRINTING and exiting before a port is bound.

`--minimal`, `--debug` and `--xpool-safe` are deliberately NOT in it. They
CONFIGURE a backend that then serves, so a caller passing one with the default
stdio transport still means "start a backend"; routing them here would turn a
working invocation into a printout. The line between the two sets is "does the
flag end the process with an answer", and it is stated at the constant. The `runpy` load reaches
`build_arg_parser()`, whose own `--help` prints the real usage and exits 0 before
anything binds a port, spawns a browser or touches the record.

**Why route it rather than answer it here.** Answering here means a second
parser that knows the real one's flags, which is the second-way defect and would
drift the day someone adds an option to the backend. Routing has one more
property worth stating: the shim prints nothing at all, so the usage the
operator reads is the one the program actually has.

**Why it cannot move any other argv.** The condition gained one conjunct and
that conjunct is empty unless `-h` or `--help` is present in `extra` — i.e.
unless the caller asked a question this shim was never going to answer. An
ordinary `stdio` start, an `--transport http` start, a `--standalone` start and
the backend's own `-m …` argv are byte-identical to 2.1.12's. The pin
`test_a_real_transport_argument_is_untouched` exists for exactly this, because
"route help to runpy" has a lazy implementation ("always runpy") that would
delete the stdio proxy.

## 5. RED first

`tests/test_package_entrypoints.py::TestAskingAQuestionStartsNothing`, against
the unfixed shim — first the two help spellings, then `--list-sections` against
the half-fixed one:

```
AssertionError: `--help` reached ensure_server_running -- asking for help
cold-starts a backend (F-905)
AssertionError: `--list-sections` reached ensure_server_running -- …
```

2 RED → 9 GREEN for the file; then 1 RED → 13 GREEN for the sibling. Neither node starts anything: `ensure_server_running`
is replaced with a tripwire and `runpy.run_path` with a recorder, so the node
observes WHICH branch was taken without either branch running.

The end-to-end evidence is a real child with a tmp `HOME`:

```
$ uv run python -m stealth_chrome_devtools_mcp --help
usage: server.py [-h] [--transport {stdio,http}] …
exit 0
```

`server.py` in that usage line is `embedded/server.py` — the real parser
answering, which is the whole point of the fix. The sibling, same shape:

```
$ uv run python -m stealth_chrome_devtools_mcp --list-sections
Available tool sections:
  browser-management: Core browser operations (8 tools)
  …
exit 0
```

with no new Python process on the machine before and after.

## 6. Residuals

1. **The usage line says `server.py`, not the command the operator typed.**
   `build_arg_parser()` takes argparse's default `prog` from `sys.argv[0]`,
   which under the `runpy` load is the embedded file. Cosmetic, pre-existing on
   every path that reaches that parser, and fixing it means giving the backend
   parser a `prog=` that knows about its callers — left alone.
2. **`-h`/`--help` are matched as literal strings**, not by asking argparse. The
   shim cannot ask: its own parser has no help action, and the real parser is
   not loaded yet. An abbreviation argparse would accept (`--hel`) is not
   matched and falls through to the old behaviour. Naming it rather than
   widening it: a prefix match here would start guessing at arguments that
   belong to the other parser.
3. **A help request with `--standalone` or `--transport http`** already reached
   the `runpy` branch and is unchanged. Only the default path was broken.
4. **The set is a LIST and has to be kept in step with `build_arg_parser()`.**
   Nothing derives it — the shim may not import the backend's parser, which is
   the whole reason it has three flags of its own — so a future flag that prints
   and exits has to be added here by hand. That is the cost of the shim's
   independence and it is why the constant states the rule rather than only the
   members. `tests/test_package_entrypoints.py` parametrises every member, so a
   flag added to the set without the behaviour fails.
