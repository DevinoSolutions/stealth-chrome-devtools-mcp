# F-786 — no local artifact write is atomic

**Status:** OPEN — characterized by plan_RELEASE W15, not fixed (W15 is zero-`src/`).
**Severity:** LOW. A crash mid-write leaves a partial file; nothing silently
corrupts a good one.
**Surface:** `tools/canary_repro.py`, `tools/release_evidence.py`, and every
write site under `src/`.
**Found by:** plan_RELEASE §2.15 (W15), "atomic local writes".

## The behavior

§2.15 asks W15 to assert "atomic local writes". There are none. A sweep for the
write-then-rename idiom (`tempfile` + `os.replace`/`os.rename`, or `fsync`) finds
exactly one `os.replace` in the tree — `embedded/clone_storage.py`, where it is a
*move-to-trash* in the GC sweep, not an artifact write. Every artifact producer
uses a plain in-place `write_text` / `open("w")`:

* `tools/canary_repro.py` — `target.write_text(...)`
* `tools/release_evidence.py` — `out.write_text(...)` for both `emit` and `aggregate`
* `embedded/response_handler.py`, `embedded/network_interceptor.py`,
  `embedded/debug_logger.py`, `embedded/singleton.py`

So a process death partway through any of them leaves a truncated file that
parses as invalid JSON. Nothing detects or repairs that.

## The guarantee that *does* exist, and is weaker than atomicity

`tools/canary_repro.py` reaches the practically important half by a different
route — **every check precedes all I/O**:

```python
class RefusedError(Exception):
    """The helper refused to write. Never a partial write: checks precede I/O."""
```

`build_record(...)` and `resolve_destination(...)` both run to completion before
`destination.mkdir(parents=True)` and `target.write_text(...)`. A *refusal*
therefore leaves no directory and no partial file. That covers the failure mode
W15 actually cares about (a rejected capture must not litter), but it is not
atomicity: it says nothing about a crash *during* the write.

Already pinned by `tests/test_canary_repro.py::test_a_refusal_leaves_no_directory_behind`.
W15 does **not** restate it (a second way is a defect) and pins only the absence
of atomicity.

## A related ordering wart, recorded not pinned

`tools/release_evidence.py` `_cmd_emit` / `_cmd_aggregate` call `validate_record`
**after** `out.write_text(...)`. An invalid record is written to disk first and
reported second, so a failed emit still leaves a file behind. Not W15's surface
(it belongs to W5's ledger tooling) and no pin is added here; noted so it is not
rediscovered as new.

## Pin

`tests/test_observability.py::TestBoundedCapture::test_no_local_artifact_write_is_atomic`
(`@pytest.mark.characterization`, `route:F-786`).

## Contract limitation wording (for W5 §Limitations)

> Local diagnostic and evidence artifacts are written in place, not via
> write-then-rename. A process killed during a write leaves a truncated file.
> The repro helper validates every input before creating anything, so a *refused*
> capture leaves nothing behind, but an interrupted *accepted* capture is not
> protected.
