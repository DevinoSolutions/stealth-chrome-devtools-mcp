"""What a Sentry event may carry off this machine, and what it may not (2.0.4).

`observability.py` ships errors to a hardcoded DSN, on by default. That was
written for a "local, single-user tool, 0 external users" — and on 2026-08-02 it
stopped being true: third-party installs from PyPI were reporting into the
maintainer's Sentry, carrying their **Windows usernames** in stacktrace frame
paths and exception messages, and their **machine names** as `server_name`. The
human ruling kept the reporting (it caught two real bugs on machines nobody here
owns) and required the identifying parts to be scrubbed for everyone, with no
maintainer-only carve-out.

This file exercises the scrubber **directly as a function**. Nothing here
initializes Sentry, opens a socket, or reads the environment: `before_send` is a
pure `event -> event` transform, so that is how it is tested. (`conftest.py` sets
`STEALTH_MCP_NO_ERROR_REPORTING=1` for the whole suite anyway — a test that
needed a live `sentry_init` would have to unset it, and none of these do.)

Two properties pull against each other and both are asserted:

* **nothing identifying survives** — the home-directory segment is replaced under
  *both* path flavors regardless of which OS is running the test. A Windows
  maintainer receives Linux users' events, so `C:\\Users\\<name>`, `/home/<name>`
  and `/Users/<name>` are all handled by string rules, never by `os.path`;
* **everything diagnostic survives** — release, environment, correlation ids, the
  exception type and mechanism, the module path *after* the home segment. A
  scrubber that eats the error class makes the report worthless, which is a
  slower way of turning reporting off.

And the contract the module has had since #55: it **never raises**. A crash
inside the scrubber must not lose the event, so the fallback still returns
something, and that something still has no `server_name`.
"""

from __future__ import annotations

import pytest

from stealth_chrome_devtools_mcp import observability

# The host username these fixtures plant. Never a real one: a test that scrubbed
# the machine's actual user would pass for the wrong reason on that machine only.
USER = "jdoe"


def _event_with_frames(*paths: str) -> dict[str, object]:
    """A minimally-shaped Sentry event whose frames carry ``paths``."""
    return {
        "server_name": "DESKTOP-ABC123",
        "release": "2.0.4",
        "environment": "production",
        "exception": {
            "values": [
                {
                    "type": "FileNotFoundError",
                    "value": "boom",
                    "mechanism": {"type": "excepthook", "handled": False},
                    "stacktrace": {
                        "frames": [
                            {"abs_path": p, "filename": p, "lineno": 12} for p in paths
                        ]
                    },
                }
            ]
        },
    }


def _frames(event: object) -> list[dict[str, object]]:
    assert isinstance(event, dict)
    exception = event["exception"]
    assert isinstance(exception, dict)
    values = exception["values"]
    assert isinstance(values, list)
    return values[0]["stacktrace"]["frames"]


# ===========================================================================
# server_name — the machine's own name, on every single event
# ===========================================================================
def test_the_machine_name_never_leaves_the_machine():
    out = observability._scrub_event(_event_with_frames("/home/jdoe/a.py"))

    assert isinstance(out, dict)
    assert "server_name" not in out
    assert "DESKTOP-ABC123" not in str(out)


def test_an_event_that_never_had_a_server_name_is_still_accepted():
    out = observability._scrub_event({"release": "2.0.4"})

    assert out == {"release": "2.0.4"}


# ===========================================================================
# Home directories — both flavors, on whichever OS is running this test
# ===========================================================================
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Windows, the flavor that actually leaked.
        (rf"C:\Users\{USER}\AppData\Local\app.py", r"C:\Users\~\AppData\Local\app.py"),
        # Windows with forward slashes — what `pathlib` and CDP hand back.
        (f"C:/Users/{USER}/AppData/Local/app.py", "C:/Users/~/AppData/Local/app.py"),
        # Windows, repr-escaped: `str(OSError)` doubles every separator.
        (rf"C:\\Users\\{USER}\\app.py", r"C:\\Users\\~\\app.py"),
        # A drive letter that is not C:, lowercased.
        (rf"d:\users\{USER}\app.py", r"d:\users\~\app.py"),
        # Linux.
        (f"/home/{USER}/src/app.py", "/home/~/src/app.py"),
        # macOS.
        (f"/Users/{USER}/src/app.py", "/Users/~/src/app.py"),
        # A bare home directory with nothing under it.
        (f"/home/{USER}", "/home/~"),
        # A file: URL, which is still a local path.
        (f"file:///home/{USER}/src/app.py", "file:///home/~/src/app.py"),
        # A Windows account name with a space — the common display-name folder.
        (r"C:\Users\John Doe\app.py", r"C:\Users\~\app.py"),
        (r"C:\\Users\\John Doe\\app.py", r"C:\\Users\\~\\app.py"),
        ("/Users/John Appleseed/x.py", "/Users/~/x.py"),
        # …and one whose name only ends at a closing quote.
        (r"'C:\\Users\\John Doe'", r"'C:\\Users\\~'"),
        # A UNC share: the home is on a file server, the account is still theirs.
        (r"\\fileserver\Users\jdoe\app.py", r"\\fileserver\Users\~\app.py"),
        # Repeated separators are legal and must not be a way through.
        (f"/home//{USER}/app.py", "/home//~/app.py"),
        (f"//home/{USER}/app.py", "//home/~/app.py"),
        # Silverblue and the Solaris-style layouts put $HOME elsewhere.
        (f"/var/home/{USER}/app.py", "/var/home/~/app.py"),
        (f"/usr/home/{USER}/app.py", "/usr/home/~/app.py"),
        (f"/export/home/{USER}/app.py", "/export/home/~/app.py"),
    ],
)
def test_every_home_flavor_is_anonymized_whatever_the_host_os_is(raw, expected):
    """Windows receives Linux users' events and vice versa — no `os.path` here."""
    out = observability._scrub_event(_event_with_frames(raw))

    frame = _frames(out)[0]
    assert frame["abs_path"] == expected
    assert frame["filename"] == expected
    assert USER not in str(out)


@pytest.mark.parametrize(
    "path",
    [
        "/usr/lib/python3.11/asyncio/runners.py",
        r"C:\Python311\Lib\asyncio\runners.py",
        "/opt/venv/lib/site-packages/sentry_sdk/client.py",
        # A URL path segment that merely spells "Users" is not a home directory.
        "https://example.test/Users/api/v1",
    ],
)
def test_a_path_outside_a_home_directory_is_left_exactly_as_it_was(path):
    """Over-scrubbing costs diagnostics; the rule is anchored, not a wildcard."""
    out = observability._scrub_event(_event_with_frames(path))

    assert _frames(out)[0]["abs_path"] == path


@pytest.mark.parametrize(
    ("prose", "expected"),
    [
        (
            f"cwd was /home/{USER} and then the spawn failed",
            "cwd was /home/~ and then the spawn failed",
        ),
        (
            f"cwd was /home/{USER} and then it read /etc/hosts",
            "cwd was /home/~ and then it read /etc/hosts",
        ),
        (
            rf"profile C:\Users\{USER} was locked by another process",
            r"profile C:\Users\~ was locked by another process",
        ),
    ],
)
def test_a_home_path_in_prose_loses_the_name_and_keeps_the_sentence(prose, expected):
    """The space-tolerant rule must not eat the words after the username.

    This is the reason the space-tolerant alternative demands a following
    separator or quote and is never anchored on end-of-string: a sentence ends
    there too, and `/home/jdoe and then` would have been read as one very
    unusual account name.
    """
    out = observability._scrub_event({"message": prose})

    assert out["message"] == expected


def test_every_frame_is_reached_not_just_the_first():
    out = observability._scrub_event(
        _event_with_frames(
            f"/home/{USER}/a.py",
            f"/home/{USER}/b.py",
            "/usr/lib/python3.11/runpy.py",
            rf"C:\Users\{USER}\c.py",
        )
    )

    assert [f["abs_path"] for f in _frames(out)] == [
        "/home/~/a.py",
        "/home/~/b.py",
        "/usr/lib/python3.11/runpy.py",
        r"C:\Users\~\c.py",
    ]


def test_an_exception_message_carrying_a_home_path_is_anonymized():
    event = _event_with_frames("/usr/lib/python3.11/pathlib.py")
    event["exception"]["values"][0]["value"] = (
        rf"[Errno 2] No such file or directory: 'C:\\Users\\{USER}\\capture.json'"
    )

    out = observability._scrub_event(event)

    assert _frames(out)[0]["abs_path"] == "/usr/lib/python3.11/pathlib.py"
    assert out["exception"]["values"][0]["value"] == (
        r"[Errno 2] No such file or directory: 'C:\\Users\\~\\capture.json'"
    )


def test_log_messages_and_breadcrumbs_are_anonymized_too():
    """The LoggingIntegration turns our own log lines into event content."""
    out = observability._scrub_event(
        {
            "server_name": "DESKTOP-ABC123",
            "logentry": {
                "message": "resolved log dir to %s",
                "params": [f"/home/{USER}/.stealth-mcp/logs"],
            },
            "breadcrumbs": {
                "values": [
                    {
                        "category": "stealth.backend",
                        "message": rf"spawn --user-data-dir=C:\Users\{USER}\profile",
                    }
                ]
            },
        }
    )

    assert USER not in str(out)
    assert out["logentry"]["message"] == "resolved log dir to %s"
    assert out["logentry"]["params"] == ["/home/~/.stealth-mcp/logs"]
    assert out["breadcrumbs"]["values"][0]["category"] == "stealth.backend"
    assert out["breadcrumbs"]["values"][0]["message"] == (
        r"spawn --user-data-dir=C:\Users\~\profile"
    )


def test_an_event_with_no_stacktrace_at_all_still_scrubs():
    out = observability._scrub_event(
        {"server_name": "DESKTOP-ABC123", "message": f"cwd was /home/{USER}/proj"}
    )

    assert out == {"message": "cwd was /home/~/proj"}


# ===========================================================================
# …and the diagnostics that must survive, or the report is worthless
# ===========================================================================
def test_the_diagnostic_value_of_the_event_is_untouched():
    event = _event_with_frames(rf"C:\Users\{USER}\src\browser_manager.py")
    event["tags"] = {"correlation_id": "a1b2c3d4e5f6"}
    event["contexts"] = {"trace": {"trace_id": "0123456789abcdef"}}

    out = observability._scrub_event(event)

    assert out["release"] == "2.0.4"
    assert out["environment"] == "production"
    assert out["tags"] == {"correlation_id": "a1b2c3d4e5f6"}
    assert out["contexts"] == {"trace": {"trace_id": "0123456789abcdef"}}
    value = out["exception"]["values"][0]
    assert value["type"] == "FileNotFoundError"
    assert value["mechanism"] == {"type": "excepthook", "handled": False}
    assert _frames(out)[0]["lineno"] == 12
    # The module path AFTER the home segment is the whole point of a stacktrace.
    assert _frames(out)[0]["abs_path"].endswith(r"src\browser_manager.py")


def test_scrubbing_an_already_scrubbed_event_changes_nothing():
    once = observability._scrub_event(_event_with_frames(f"/home/{USER}/a.py"))
    twice = observability._scrub_event(once)

    assert twice == once


def test_the_caller_s_own_event_object_is_not_mutated():
    """`before_send` returning a copy keeps the SDK's own bookkeeping honest."""
    event = _event_with_frames(f"/home/{USER}/a.py")

    observability._scrub_event(event)

    assert event["server_name"] == "DESKTOP-ABC123"
    assert _frames(event)[0]["abs_path"] == f"/home/{USER}/a.py"


# ===========================================================================
# The never-raises contract (#55) — a scrubber crash must not lose the event
# ===========================================================================
@pytest.mark.parametrize(
    "malformed",
    [
        None,
        "not an event at all",
        ["a", "list"],
        42,
        {"exception": None},
        {"exception": {"values": "not a list"}},
        {"exception": {"values": [{"stacktrace": {"frames": None}}]}},
        {"server_name": object()},
    ],
)
def test_a_malformed_event_returns_instead_of_raising(malformed):
    out = observability._scrub_event(malformed)

    if isinstance(out, dict):
        assert "server_name" not in out


def test_a_self_referential_event_terminates_instead_of_recursing_forever():
    event: dict[str, object] = {"server_name": "DESKTOP-ABC123"}
    event["self"] = event

    out = observability._scrub_event(event)

    assert isinstance(out, dict)
    assert "server_name" not in out


def test_an_internal_crash_still_strips_the_server_name():
    """The fallback: the unscrubbed event, minus the one field we can always drop.

    Losing the event entirely would be worse — that is the failure mode the
    module's never-raises contract exists to prevent — so the scrubber degrades
    to the least it can guarantee rather than to nothing.
    """

    class _Hostile(dict):
        def items(self):
            raise RuntimeError("event walk exploded")

    hostile = _Hostile(server_name="DESKTOP-ABC123", release="2.0.4")

    out = observability._scrub_event(hostile)

    assert "server_name" not in out
    assert out["release"] == "2.0.4"
    # …and the failure path copies too. The SDK still holds this object; a
    # scrubber that reaches back into its caller's event is a second way to
    # change an event, and the one that nobody can see.
    assert hostile["server_name"] == "DESKTOP-ABC123"


def test_the_scrubber_accepts_the_hint_sentry_passes_positionally():
    """`before_send(event, hint)` — a one-argument callable would crash the SDK."""
    out = observability._scrub_event(
        _event_with_frames(f"/home/{USER}/a.py"), {"exc_info": (None, None, None)}
    )

    assert _frames(out)[0]["abs_path"] == "/home/~/a.py"
