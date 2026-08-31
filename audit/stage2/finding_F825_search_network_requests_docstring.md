# F-825 (SMALL) — `search_network_requests` did not name `url_pattern` where a caller looks

**Status:** FIXED on `fix/F821-F825-F836-small-fixes`.

## What was wrong

The tool's summary line was:

> Search network requests with advanced filters and pagination.

"advanced filters" names nothing. The `Args:` block below it did list
`url_pattern`, but the summary is the line a client shows first (and the only
line some clients show at all), so an AI caller reaching for the obvious filter
guesses `url=` or `pattern=` — which is a validation error, not a filtered
result.

The per-parameter text was also vaguer than the code: `search_requests` matches
`url_pattern`, `response_contains`, `payload_contains` and `resource_type` as
**case-insensitive substrings**, and `method` whole-but-case-insensitively —
none of which the docstring said. "Filter by resource type" in particular reads
as an exact match.

## Fix

Reworded inside the existing 18-line docstring budget (`server.py` sits at its
exact LOC cap, 3411/3411 — net delta 0):

* the summary now reads *"Search network requests; ``url_pattern`` filters the
  URL (substring, not glob)."* — the parameter is named in the first line;
* each filter now states its actual matching rule (case-insensitive substring
  vs. whole-value vs. exact int).

## Test

`tests/test_server_network_tools.py::TestSearchDescriptionNamesItsFilters`:

* the registered tool's **first description line** names `url_pattern`;
* the description names **every** parameter in the tool's signature — derived
  from `inspect.signature`, so a future parameter that goes undocumented fails
  the pin rather than shipping unnamed.

RED evidence (pre-fix):
`AssertionError: Search network requests with advanced filters and pagination.
assert 'url_pattern' in 'Search network requests with advanced filters and pagination.'`
