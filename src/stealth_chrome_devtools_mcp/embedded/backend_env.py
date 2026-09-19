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
:func:`scrub_process_env` applies the SAME table to our own environment, once, at
the top of the one entrypoint every path goes through. The composer keeps its own
call because ``restart_backend`` reaches it from the ops CLI without passing
through that entrypoint at all.

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


def scrub(env: MutableMapping[str, str]) -> list[str]:
    """Remove from ``env`` every name the backend must not inherit, in place;
    return the names removed, sorted.

    A ``MutableMapping`` and not a ``dict``, because it has two callers with two
    different mappings: the child-env copy ``_start_server_process`` owns, and
    ``os.environ`` itself through :func:`scrub_process_env`.

    Case-folded, because the lookup on the other side is: ``fastmcp``'s
    ``model_config`` sets ``case_sensitive=False``, and Windows folds env-var
    case in the OS as well — so ``fastmcp_port``, ``FastMCP_Port`` and
    ``FASTMCP_PORT`` are one variable and all three must go. The same fold is
    applied to :data:`NO_AUTO_RECOVERY`, which pydantic-settings reads
    case-insensitively for exactly the same reason.
    """
    removed = sorted(
        name
        for name in env
        if name.upper().startswith(FASTMCP_PREFIX) or name.upper() == NO_AUTO_RECOVERY
    )
    for name in removed:
        del env[name]  # on ``os.environ`` this is a real ``unsetenv``
    if removed:
        # Names only. Never a value (F-869's discipline): this reaches the
        # durable proxy log, and an environment is where tokens live.
        _logger.info(
            "backend env: dropped %d inherited variable(s): %s",
            len(removed),
            ", ".join(removed),
        )
    return removed


def scrub_process_env() -> list[str]:
    """:func:`scrub`, applied to THIS process's own environment (F-890 M5).

    Called once, from ``server.main()``, before any path through that entrypoint
    can import ``fastmcp`` — which on the ``runpy`` fallthrough happens in this
    very process. The same table, applied to a different mapping: a second list
    of names here is exactly the drift convention 4 is about, so there is one
    ``scrub`` and this is a two-line application of it.

    Deleting from ``os.environ`` is a real ``unsetenv``, so the removal also
    covers anything this process later spawns — which is why the composer's own
    call is belt-and-braces rather than the only defence.
    """
    return scrub(os.environ)  # noqa: TID251  PERMANENT(F-890 M5: this deletes a THIRD PARTY's names before a library parses them; it reads no STEALTH_MCP_* configuration, which is what settings.py is the one home for - argued in this module's docstring)
