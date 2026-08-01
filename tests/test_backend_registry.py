"""Pins for the server.json record's one home.

Grows in plan_F808 Task 3, which adds the schema-v2 per-display-context record.
"""

import inspect

from stealth_chrome_devtools_mcp.embedded import backend_registry


def _public_functions():
    for name, obj in vars(backend_registry).items():
        if name.startswith("_") or not inspect.isfunction(obj):
            continue
        if obj.__module__ == backend_registry.__name__:
            yield name, obj


class TestNoDefaultPaths:
    def test_no_public_function_defaults_its_path_parameter(self):
        """The module docstring's corollary, enforced: the caller's binding is
        what selects the file. A default would bind this module's own
        SERVER_STATE_FILE at def-time and silently ignore the redirection the
        hermetic fixtures rely on - which is the only thing keeping a test run
        off the developer's live ~/.stealth-mcp record.
        """
        offenders = [
            f"{name}({param})"
            for name, func in _public_functions()
            for param in inspect.signature(func).parameters.values()
            if param.name in ("path", "paths")
            and param.default is not inspect.Parameter.empty
        ]
        assert offenders == [], (
            f"path parameters must stay required, but {offenders} default theirs"
        )


class TestWriteRecord:
    def test_write_record_creates_the_missing_parent_dir(self, tmp_path):
        """The one line that is not a verbatim move: write_record makes the
        record's OWN parent rather than calling singleton's _ensure_state_dir,
        so a redirected path lands where the caller asked instead of forcing
        the real STATE_DIR into existence.
        """
        record = tmp_path / "sub" / "server.json"

        backend_registry.write_record(
            record, port=19222, version="2.0.3", pid=4242, source_fingerprint="fp"
        )

        assert backend_registry.read_record(record) == {
            "port": 19222,
            "version": "2.0.3",
            "pid": 4242,
            "source_fingerprint": "fp",
        }
