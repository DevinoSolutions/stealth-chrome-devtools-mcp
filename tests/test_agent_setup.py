"""``agent-setup/prompt.md`` — the file an AI coding agent fetches and executes.

The README's "Onboard your agent" sentence points an agent at ONE raw URL; what it
finds there installs the server, registers it in that agent and verifies the
backend. Two failure modes, two classes:

- **Hermetic**: the file exists where the sentence says, carries no template
  placeholder left over from the fleet spec, teaches both console-script names
  the package actually installs (``stealth-chrome-devtools-mcp`` and
  ``stealthy``), and every link in it is an https URL.
- **Network** (``integration``, deselected from the hermetic lane): every URL the
  prompt names answers 200, so an agent following it never lands on a dead page.
  A GitHub page that still answers 5xx after every retry skips it with the reason
  (T-16103): that is GitHub's outage, not a dead link. Its logic is pinned
  hermetically by ``TestAGitHub5xxIsRetriedThenSkipped``.
  The README's own raw-URL is NOT fetched here: it resolves only once the file is
  on ``main``, which on a PR it is not yet — the post-merge check is a ``curl``
  from a clean shell, as the release notes say.
"""

import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PROMPT = REPO / "agent-setup" / "prompt.md"
README = REPO / "README.md"
RAW_URL = (
    "https://raw.githubusercontent.com/DevinoSolutions/stealth-chrome-devtools-mcp"
    "/main/agent-setup/prompt.md"
)
SENTENCE = (
    "Fetch and execute the appropriate instructions to set me up for "
    f"Stealth Chrome DevTools MCP from {RAW_URL}"
)
# Fleet-spec template tokens that must never reach the shipped file. `<user>`,
# `<agent>` and `<absolute path>` are deliberate: they are the agent's fill-ins.
LEFTOVER_PLACEHOLDERS = (
    "<App>",
    "<docs-host>",
    "<app-domain>",
    "<skills-repo>",
    "TODO",
    "TBD",
)
URL_RE = re.compile(r"https?://[^\s)>`\"]+")


def _prompt() -> str:
    return PROMPT.read_text(encoding="utf-8")


def _urls(text: str) -> list[str]:
    return sorted({u.rstrip(".,") for u in URL_RE.findall(text)})


class TestThePromptIsWhatTheSentencePromises:
    def test_the_file_exists_at_the_path_the_raw_url_names(self):
        assert PROMPT.is_file()
        assert RAW_URL.endswith("/main/" + PROMPT.relative_to(REPO).as_posix())

    def test_the_readme_carries_the_exact_sentence(self):
        text = README.read_text(encoding="utf-8")
        assert text.count(SENTENCE) == 1, (
            "the copyable sentence must appear exactly once"
        )
        assert "[`agent-setup/prompt.md`](agent-setup/prompt.md)" in text

    def test_no_template_placeholder_survived(self):
        text = _prompt()
        for token in LEFTOVER_PLACEHOLDERS:
            assert token not in text, (
                f"fleet-spec placeholder {token!r} left in prompt.md"
            )

    def test_it_teaches_both_console_scripts_the_package_installs(self):
        scripts = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["scripts"]
        assert {"stealth-chrome-devtools-mcp", "stealthy"} <= set(scripts)
        text = _prompt()
        assert "uv tool install stealth-chrome-devtools-mcp" in text
        assert "stealthy status" in text
        assert "stealthy doctor" in text

    def test_every_link_is_https(self):
        for url in _urls(_prompt()):
            assert url.startswith("https://"), url

    def test_it_never_asks_the_agent_to_sign_in(self):
        """The one step that is the user's: credentials and CAPTCHAs are human-only."""
        text = _prompt()
        assert "Do **not** sign in" in text
        assert "CAPTCHA" in text

    def test_each_named_agent_has_a_section(self):
        text = _prompt()
        for heading in ("### Claude Code", "### Codex", "### Cursor"):
            assert heading in text
        assert "claude mcp add --scope user stealth-chrome-devtools-mcp --" in text
        assert "codex mcp add stealth-chrome-devtools-mcp --" in text
        assert "~/.cursor/mcp.json" in text


#: A github.com file page, as the owner/repo/ref-and-path it names.
_BLOB = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/blob/(.+)$")


#: Seconds to wait before each round of re-fetching the pages that answered 5xx.
_RETRY_DELAYS = (2.0, 5.0, 10.0)

#: One fetch's socket timeout. GitHub answers in well under a second; this only
#: bounds a page that hangs. It is urllib's per-operation timeout, not a cap on
#: the whole fetch: a server trickling bytes slower than it can run past it, and
#: then the lane's ``--timeout=300`` fails the run, as it did before T-16103.
_FETCH_TIMEOUT_S = 15.0

#: Wall-clock seconds the first fetches, every retry round and the raw fallback
#: together may take, each fetch counted at its full timeout. A round starts only
#: when its wait, one timeout per page it retries and one per raw fetch a blob
#: page would still owe all fit, so the check ends inside the budget, under the
#: integration lane's ``--timeout=300``.
_BUDGET_S = 240.0

#: Hosts whose 5xx, once the retries are spent, is GitHub's outage, not our link.
_GITHUB_HOSTS = ("github.com", "raw.githubusercontent.com")


def _is_5xx(code: int) -> bool:
    return 500 <= code < 600


def _statuses(
    pages: list[str],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, int]:
    """Each page's status. Every page is fetched once, then the pages answering
    5xx are fetched again together after each of :data:`_RETRY_DELAYS`, while
    the round fits :data:`_BUDGET_S`. Any other answer is final at once: a 404
    is not retried.

    A github.com file page still answering 5xx is then decided by the same file
    on raw.githubusercontent.com: GitHub's blob pages have answered 503 for EVERY
    repo at once while githubstatus read "operational" (2026-10-09, 2026-10-10),
    and then answer 503 for a file that does not exist too, so the 5xx says
    nothing about the link. raw answers 200 for the file and 404 for a missing
    one, so a dead link still fails here."""
    start = clock()
    codes = {page: _fetch(page) for page in pages}
    for delay in _RETRY_DELAYS:
        pending = [page for page, code in codes.items() if _is_5xx(code)]
        # The round's fetches, plus the raw fetch each blob page still owes after.
        owed = len(pending) + sum(1 for page in pending if _BLOB.match(page))
        cost = delay + _FETCH_TIMEOUT_S * owed
        if not pending or clock() - start + cost > _BUDGET_S:
            break
        sleep(delay)
        for page in pending:
            codes[page] = _fetch(page)
    for page, code in codes.items():
        blob = _BLOB.match(page)
        if _is_5xx(code) and blob:
            owner, repo, rest = blob.groups()
            codes[page] = _fetch(
                f"https://raw.githubusercontent.com/{owner}/{repo}/{rest}"
            )
    return codes


def _github_outage(url: str, code: int) -> bool:
    return _is_5xx(code) and urllib.parse.urlsplit(url).hostname in _GITHUB_HOSTS


def _check_links(
    urls: list[str], statuses: Callable[[list[str]], dict[str, int]] = _statuses
) -> None:
    """Fail on a dead link; skip, naming every URL, when the only problem is
    GitHub still answering 5xx once the retries are spent.

    The repo page and ``/issues`` have no second source the way a blob page has
    raw: on 2026-11-09 they answered 504 to the CI runner on two runs in a row
    while githubstatus read "operational" and the same fetch answered 200 here
    (T-16103). That says nothing about the link, so it skips with the reason.
    Anything else that is not 200 (a 404, or a 5xx from another host) fails,
    and a dead link fails even while GitHub is down. Anchors of one page share
    one fetch.
    """
    by_page = statuses(sorted({url.split("#", 1)[0] for url in urls}))
    codes = {url: by_page[url.split("#", 1)[0]] for url in urls}
    outage = {url: code for url, code in codes.items() if _github_outage(url, code)}
    dead = {
        url: code for url, code in codes.items() if code != 200 and url not in outage
    }
    assert not dead, f"dead links in prompt.md: {dead}"
    if outage:
        pytest.skip(
            f"GitHub still answered 5xx after the retries for {outage}: a GitHub "
            "outage, not a dead link (every other link answered 200, and a 404 "
            "would still fail)"
        )


def _fetch(url: str) -> int:
    # Only https ever reaches here: ``test_every_link_is_https`` refuses anything
    # else, and the scheme is re-checked so the S310 waiver below stays honest.
    if not url.startswith("https://"):
        raise ValueError(f"refusing to fetch a non-https url: {url}")
    request = urllib.request.Request(  # noqa: S310  PERMANENT(https-only, guarded above)
        url.split("#", 1)[0],
        headers={"User-Agent": "stealth-chrome-devtools-mcp-tests"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_S) as response:  # noqa: S310  PERMANENT(https-only, guarded above)
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.mark.integration
class TestEveryUrlAnswers:
    def test_every_url_in_the_prompt_returns_200(self):
        _check_links(_urls(_prompt()))


class _Pages:
    """``_fetch``'s double on a fake clock: each URL answers its scripted codes
    in turn, then repeats the last one, ``latency`` seconds after it was asked.
    Every fetch is recorded, and so is every wait."""

    def __init__(self, script: dict[str, list[int]], latency: float) -> None:
        self.script = {url: list(codes) for url, codes in script.items()}
        self.latency = latency
        self.now = 0.0
        self.fetched: list[str] = []
        self.slept: list[float] = []

    def fetch(self, url: str) -> int:
        self.fetched.append(url)
        self.now += self.latency
        codes = self.script[url]
        return codes.pop(0) if len(codes) > 1 else codes[0]

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def statuses(self, pages: list[str]) -> dict[str, int]:
        return _statuses(pages, sleep=self.sleep, clock=lambda: self.now)


@pytest.fixture
def pages(monkeypatch):
    def install(script, latency=0.1):
        fake = _Pages(script, latency)
        monkeypatch.setattr(sys.modules[__name__], "_fetch", fake.fetch)
        return fake

    return install


def _judged(urls: list[str], fake: _Pages) -> None:
    """``_check_links`` where a skip is NOT the expected outcome: a skip here
    would leave the test green while the behaviour it pins is broken."""
    try:
        _check_links(urls, fake.statuses)
    except pytest.skip.Exception as skip:
        pytest.fail(f"unexpected skip: {skip}")


_REPO_PAGE = "https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp"
_PYPI = "https://pypi.org/project/stealth-chrome-devtools-mcp/"


class TestAGitHub5xxIsRetriedThenSkipped:
    """T-16103, hermetic: the 5xx path of the network test above."""

    def test_a_5xx_is_retried_with_backoff_until_the_page_answers(self, pages):
        fake = pages({_REPO_PAGE: [504, 503, 200]})

        assert fake.statuses([_REPO_PAGE]) == {_REPO_PAGE: 200}

        assert fake.slept == [2.0, 5.0]
        assert len(fake.fetched) == 3

    def test_a_404_is_final_at_once(self, pages):
        fake = pages({_REPO_PAGE: [404, 200]})

        assert fake.statuses([_REPO_PAGE]) == {_REPO_PAGE: 404}

        assert fake.slept == []

    def test_a_github_page_still_5xx_after_every_retry_skips_with_the_reason(
        self, pages
    ):
        fake = pages({_REPO_PAGE: [504], _PYPI: [200]})
        urls = [f"{_REPO_PAGE}#cli", f"{_REPO_PAGE}#readme", _PYPI]

        with pytest.raises(
            pytest.skip.Exception, match=r"GitHub still answered 5xx"
        ) as skip:
            _check_links(urls, fake.statuses)

        assert f"'{_REPO_PAGE}#cli': 504" in str(skip.value)
        assert fake.slept == list(_RETRY_DELAYS)
        # One fetch per round for the page, not one per anchor; pypi once.
        assert fake.fetched.count(_REPO_PAGE) == len(_RETRY_DELAYS) + 1
        assert fake.fetched.count(_PYPI) == 1

    def test_a_blob_page_still_falls_back_to_raw_after_the_retries(self, pages):
        blob = f"{_REPO_PAGE}/blob/main/RUNBOOK.md"
        raw = (
            "https://raw.githubusercontent.com/DevinoSolutions/"
            "stealth-chrome-devtools-mcp/main/RUNBOOK.md"
        )
        fake = pages({blob: [503], raw: [200]})

        _judged([blob], fake)

        assert fake.fetched[-1] == raw

    def test_a_dead_link_still_fails_while_github_is_down(self, pages):
        fake = pages({_REPO_PAGE: [504], _PYPI: [404]})

        with pytest.raises(AssertionError, match=r"pypi\.org.*404"):
            _judged([_REPO_PAGE, _PYPI], fake)

    def test_a_5xx_from_another_host_still_fails(self, pages):
        fake = pages({_PYPI: [503]})

        with pytest.raises(AssertionError, match=r"pypi\.org.*503"):
            _judged([_PYPI], fake)

    def test_a_github_status_outside_5xx_is_not_an_outage(self, pages):
        fake = pages({_REPO_PAGE: [600]})

        with pytest.raises(AssertionError, match=r"600"):
            _judged([_REPO_PAGE], fake)

        assert fake.slept == []

    def test_every_page_hanging_to_its_timeout_still_ends_inside_the_budget(
        self, pages
    ):
        """Codex review of 08cf888, MEDIUM: retries per page had no overall
        bound, so slow 504s could outlast the lane's ``--timeout=300`` before
        the skip was ever reached."""
        many = [f"{_REPO_PAGE}/page-{n}" for n in range(7)]
        fake = pages({page: [504] for page in many}, latency=_FETCH_TIMEOUT_S)

        with pytest.raises(pytest.skip.Exception):
            _check_links(many, fake.statuses)

        assert fake.now <= _BUDGET_S
        assert fake.now == 7 * _FETCH_TIMEOUT_S + 2.0 + 7 * _FETCH_TIMEOUT_S  # 212 s
        assert fake.slept == [2.0]  # the round that fit ran; the next did not

    def test_the_raw_fallback_is_inside_the_budget_too(self, pages):
        """Codex review of f636af3, LOW: a round fitted the budget without the
        raw.githubusercontent fetch each blob page still owes after it, so three
        blob pages among seven hanging ones ended at 257 s, past the budget."""
        repo = [f"{_REPO_PAGE}/page-{n}" for n in range(4)]
        blobs = [f"{_REPO_PAGE}/blob/main/doc-{n}.md" for n in range(3)]
        raws = [
            "https://raw.githubusercontent.com/DevinoSolutions/"
            f"stealth-chrome-devtools-mcp/main/doc-{n}.md"
            for n in range(3)
        ]
        fake = pages(
            {page: [504] for page in repo + blobs + raws}, latency=_FETCH_TIMEOUT_S
        )

        with pytest.raises(pytest.skip.Exception):
            _check_links(repo + blobs, fake.statuses)

        assert fake.now <= _BUDGET_S
        assert fake.now == 7 * _FETCH_TIMEOUT_S + 3 * _FETCH_TIMEOUT_S  # 150 s
        assert fake.fetched[-3:] == raws
