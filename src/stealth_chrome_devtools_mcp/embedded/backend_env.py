"""THE one home for "what the environment we hand the backend must NOT carry"
(F-890).

The backend is a child of the stdio proxy, and ``singleton._start_server_process``
copies ``os.environ`` whole. That environment belongs to the MCP CLIENT — Claude
Code — not to us, and everything in it reaches the backend, including names that
belong to a THIRD party.

**The incident.** ``embedded/server.py`` imports ``fastmcp`` at module scope, and
``fastmcp`` (2.11.2) builds a ``pydantic_settings.BaseSettings`` AT IMPORT whose
``env_prefixes`` are ``["FASTMCP_", "FASTMCP_SERVER_"]``. An inherited
``FASTMCP_PORT`` is therefore parsed into ``port: int`` before one line of our
code runs — before ``build_arg_parser``, before ``--port`` exists as a concept.
An empty value is not "unset" to pydantic: it is the string ``""``, and
``int("")`` fails validation. Measured on the installed stack::

    FASTMCP_PORT=""  ->  ValidationError: port — Input should be a valid
                         integer, unable to parse string as an integer
                         [type=int_parsing, input_value='', input_type=str]
    port=""/PORT=""  ->  no effect (port=8000)

The import raises and the process dies before ``configure_logging`` has installed
anything, so the only trace is ``backend-boot.log`` (F-303) — which is why
2026-09-18 04:26-05:33 shows sixty-six minutes of launches that each died the
same way while the proxy above them retried, healed and gave up.

**Why the whole family, and not the field that bit us.** The narrow fix is
"override ``FASTMCP_PORT`` with our own port", and it is the wrong fix for the
reason convention 4 exists: there would then be two places that decide what port
the backend listens on, and the second one is a string in someone else's
environment. Our backend's configuration comes from ONE place and always has —
the argv ``_server_process_cmd`` builds, consumed by ``build_arg_parser`` and
handed to ``mcp.run(transport="http", host=…, port=…)``. Every ``FASTMCP_*`` name
is an input to a decision we have already made: ``port``/``host`` can make the
backend unstartable or bind it where the record does not name, ``log_level``
fights ``configure_logging``, ``json_response``/``stateless_http`` change the
transport the proxy is written against, ``streamable_http_path`` moves the URL
``_backend_http_url`` hardcodes. None has a legitimate reading for this process.

Scrubbing by PREFIX rather than by field name is also deliberate. Deriving the
names from ``fastmcp.settings.Settings.model_fields`` reads more precise and is
worse: it requires importing ``fastmcp`` to compute, and **the stdio proxy must
never import** ``fastmcp`` — it reaches ``backend_launch`` today without touching
the MCP server stack at all (``desktop_launch`` carries the same paragraph about
why its nodriver import is lazy: ≈175 ms warm to reach a seam, down from ≈470).
One prefix also covers all of ``FASTMCP_``, ``FASTMCP_SERVER_`` and the nested
``FASTMCP_EXPERIMENTAL…`` block, so there is one constant and not three. The
constant is ours; that it still COVERS the installed library is pinned against
the library's own ``model_config`` in ``tests/test_backend_env_scrub.py``.

**It also owns the removal that was already there.** ``STEALTH_MCP_NO_AUTO_RECOVERY``
has been popped from the child env since M8-2 — a spawned backend must always
reap its own orphaned browsers even when the CLI-invoking parent set the flag to
skip its own recovery-on-import (``cli.py``'s ``os.environ.setdefault``). That is
the same sentence as the one above ("a name in the parent's environment must not
reach a decision the backend makes for itself"), so it lives here rather than as
a second removal site two lines away from this one.

**Two call sites, one rule** (F-890 review M5). The child-env composer is not
every path by which this package imports ``fastmcp``: ``server.main()``'s
``runpy`` fallthrough runs ``embedded/server.py`` IN THIS PROCESS — that is what
``--transport http`` does — and ``stealth-chrome-devtools serve --http`` reaches
the same line through ``cli._cmd_serve``. An operator with a stray
``FASTMCP_PORT=""`` in their shell gets the identical import-time
``ValidationError`` there, with the identical absence of a log line. So
:func:`scrub_process_env` applies the THIRD PARTY's half of the table to our own
environment, at the top of ``server.main()`` and again at ``cli._server()``, the
two doors this package has onto an ``import fastmcp``. The composer keeps its own
call because ``restart_backend`` reaches it from the ops CLI without passing
through either.

**The two tables are deliberately not the same table** (F-889 review N1). The
child's is one name wider: it also drops ``STEALTH_MCP_NO_AUTO_RECOVERY``, below.
Applied to OUR OWN environment that removal is a bug — the flag is the operator's
own answer, which ``cli.py`` sets with ``os.environ.setdefault`` precisely so that
``doctor`` and ``status`` stay read-only, and deleting it re-enables the orphan
reaping a read-only verb exists not to do. One shared ``_inherited_fastmcp`` holds
the half they do share, so the prefix still lives in exactly one place; what
differs is one clause, and it differs because the two environments belong to two
different parties. **Nothing here ever deletes a** ``STEALTH_MCP_*`` **name from
our own process.**

That second function READS ``os.environ``, which the repo otherwise confines to
``settings.py``. Deliberate, and narrow: that rule is about CONFIGURATION — every
``STEALTH_MCP_*`` knob is a typed field in one home, and a second reader of one
is a second answer. This reads no configuration and produces no value; it deletes
a THIRD PARTY's names from our own process before a library parses them, and the
table it deletes by is stated here, once. Moving the call to ``settings.py``
would put a ``FASTMCP_`` prefix in the ``STEALTH_MCP_*`` home and make ``settings``
an importer of ``embedded/``; splitting the table from its application would put
the rule in two files. Neither is better than one named exception.

A leaf: stdlib only. :func:`scrub` never touches ``os.environ`` — it mutates the
dict copy ``_start_server_process`` owns — so the two functions stay honest about
which environment each one changes. The removed NAMES are logged, never their
values: an environment variable is a place secrets live and this line reaches the
durable log.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import MutableMapping

# One stream: which variables a proxy declined to pass on is part of that
# proxy's story, so this goes to the log ``configure_logging("proxy")`` already
# owns — the same logger ``singleton`` uses, from which this moved.
_logger = logging.getLogger("stealth.proxy")

#: Every third-party name whose value would configure the backend behind our
#: argv's back. One prefix, because ``FASTMCP_SERVER_`` and the experimental
#: block both start with it.
FASTMCP_PREFIX = "FASTMCP_"

#: The flag a CLI-invoking parent sets to skip ITS OWN recovery-on-import, which
#: a spawned backend must never inherit (M8-2).
NO_AUTO_RECOVERY = "STEALTH_MCP_NO_AUTO_RECOVERY"


def _inherited_fastmcp(env: MutableMapping[str, str]) -> list[str]:
    """The third party's names in ``env``, sorted — the half BOTH scrubs share.

    Case-folded, because the lookup on the other side is: ``fastmcp``'s
    ``model_config`` sets ``case_sensitive=False``, and Windows folds env-var
    case in the OS as well — so ``fastmcp_port``, ``FastMCP_Port`` and
    ``FASTMCP_PORT`` are one variable and all three must go.
    """
    return sorted(name for name in env if name.upper().startswith(FASTMCP_PREFIX))


def _remove(
    env: MutableMapping[str, str], names: list[str], *, level: int
) -> list[str]:
    """Delete ``names`` from ``env`` in place and report them, once.

    The LEVEL is the caller's because the two callers are heard by different
    ears (F-889 review N4). :func:`scrub` runs inside a proxy whose logging is
    already configured, so its line lands in the durable log at INFO.
    :func:`scrub_process_env` runs BEFORE ``configure_logging`` — that is the
    whole point of where it sits — and Python's last-resort handler emits at
    WARNING and above, so an INFO line there would be written to nothing at all.
    """
    for name in names:
        del env[name]  # on ``os.environ`` this is a real ``unsetenv``
    if names:
        # Names only. Never a value (F-869's discipline): this reaches the
        # durable proxy log, and an environment is where tokens live.
        _logger.log(
            level,
            "backend env: dropped %d inherited variable(s): %s",
            len(names),
            ", ".join(names),
        )
    return names


def scrub(env: MutableMapping[str, str]) -> list[str]:
    """Remove from ``env`` every name the BACKEND must not inherit, in place;
    return the names removed, sorted.

    A ``MutableMapping`` and not a ``dict`` because ``os.environ`` is one too —
    but this is the CHILD's table, and it is one name wider than the process
    table below. :data:`NO_AUTO_RECOVERY` is folded in the same way, which
    pydantic-settings reads case-insensitively for the same reason the prefix is.
    """
    ours = [name for name in env if name.upper() == NO_AUTO_RECOVERY]
    return _remove(env, sorted(_inherited_fastmcp(env) + ours), level=logging.INFO)


def scrub_process_env() -> list[str]:
    """The FASTMCP half of :func:`scrub`, applied to THIS process's own
    environment (F-890 M5), and **never a** ``STEALTH_MCP_*`` **name**.

    Called once, from ``server.main()``, before any path through that entrypoint
    can import ``fastmcp`` — which on the ``runpy`` fallthrough happens in this
    very process.

    **Two mappings, two tables, and the difference is the whole point** (F-889
    review N1). The child-env table also drops :data:`NO_AUTO_RECOVERY`, because
    a SPAWNED backend must reap its own orphans whatever its parent decided for
    itself. Applied here that name says the opposite thing: it is the operator's
    own answer — ``cli.py`` sets it with ``os.environ.setdefault`` so that
    ``doctor`` and ``status`` stay read-only — and deleting it from our own
    environment silently re-enables the reaping those verbs exist not to do.
    Every name this removes belongs to a third party; not one of them is ours.

    Deleting from ``os.environ`` is a real ``unsetenv``, so the removal also
    covers anything this process later spawns — which is why the composer's own
    call is belt-and-braces rather than the only defence.
    """
    env = os.environ  # noqa: TID251  PERMANENT(F-890 M5: this deletes a THIRD PARTY's names before a library parses them; it reads no STEALTH_MCP_* configuration, which is what settings.py is the one home for - argued in this module's docstring)
    return _remove(env, _inherited_fastmcp(env), level=logging.WARNING)
