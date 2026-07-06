"""The direct `python -m ... server --transport http` entrypoint must bind to
loopback by default.

The backend has NO authentication and drives real browsers holding logged-in
profiles. Defaulting the HTTP host to 0.0.0.0 silently exposes that unauthenticated
control surface to every host on the network. The singleton always passes
--host 127.0.0.1 explicitly, but a direct/manual/container invocation that omits
--host must not fall through to all-interfaces. Loopback is the safe default;
operators who genuinely need a public bind pass --host 0.0.0.0 deliberately.
"""

import server


def test_http_host_defaults_to_loopback():
    args = server.build_arg_parser().parse_args(["--transport", "http"])
    assert args.host == "127.0.0.1", (
        "unauthenticated HTTP backend must default to loopback, not all interfaces"
    )


def test_http_host_is_still_overridable():
    # The safe default must not remove the ability to bind publicly on purpose.
    args = server.build_arg_parser().parse_args(
        ["--transport", "http", "--host", "0.0.0.0"]
    )
    assert args.host == "0.0.0.0"
