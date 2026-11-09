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


#: Seconds to wait before each re-fetch of a page that answered 5xx.
_RETRY_DELAYS = (2.0, 5.0, 10.0)

#: Hosts whose 5xx, once every retry is spent, is GitHub's outage, not our link.
_GITHUB_HOSTS = ("github.com", "raw.githubusercontent.com")


def _answer(url: str, sleep: Callable[[float], None]) -> int:
    """``url``'s status, re-fetched after each of :data:`_RETRY_DELAYS` while it
    answers 5xx. Any other answer is final at once: a 404 is not retried."""
    code = _fetch(url)
    for delay in _RETRY_DELAYS:
        if code < 500:
            break
        sleep(delay)
        code = _fetch(url)
    return code


def _status(url: str, sleep: Callable[[float], None] = time.sleep) -> int:
    """The link's status. A github.com file page that answers 5xx is decided by
    the same file on raw.githubusercontent.com: GitHub's blob pages have answered
    503 for EVERY repo at once while githubstatus read "operational" (2026-10-09,
    2026-10-10), and then answer 503 for a file that does not exist too, so the
    5xx says nothing about the link. raw answers 200 for the file and 404 for a
    missing one, so a dead link still fails here."""
    code = _answer(url, sleep)
    blob = _BLOB.match(url.split("#", 1)[0])
    if code >= 500 and blob:
        owner, repo, rest = blob.groups()
        return _answer(
            f"https://raw.githubusercontent.com/{owner}/{repo}/{rest}", sleep
        )
    return code


def _github_outage(url: str, code: int) -> bool:
    return code >= 500 and urllib.parse.urlsplit(url).hostname in _GITHUB_HOSTS


def _check_links(urls: list[str], status: Callable[[str], int] = _status) -> None:
    """Fail on a dead link; skip, naming every page, when the only problem is
    GitHub answering 5xx after every retry.

    The repo page and ``/issues`` have no second source the way a blob page has
    raw: on 2026-11-09 they answered 504 to the CI runner on two runs in a row
    while githubstatus read "operational" and the same fetch answered 200 here
    (T-16103). That says nothing about the link, so it skips with the reason.
    Anything else that is not 200 (a 404, or a 5xx from another host) fails,
    and a dead link fails even while GitHub is down.
    """
    pages: dict[str, int] = {}
    for url in urls:
        page = url.split("#", 1)[0]
        if page not in pages:  # anchors of one page share one fetch
            pages[page] = status(page)
    codes = {url: pages[url.split("#", 1)[0]] for url in urls}
    outage = {url: code for url, code in codes.items() if _github_outage(url, code)}
    dead = {
        url: code for url, code in codes.items() if code != 200 and url not in outage
    }
    assert not dead, f"dead links in prompt.md: {dead}"
    if outage:
        pytest.skip(
            f"GitHub answered 5xx after {len(_RETRY_DELAYS) + 1} tries for "
            f"{outage}: a GitHub outage, not a dead link (every other link answered "
            "200, and a 404 would still fail)"
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
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310  PERMANENT(https-only, guarded above)
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.mark.integration
class TestEveryUrlAnswers:
    def test_every_url_in_the_prompt_returns_200(self):
        _check_links(_urls(_prompt()))


class _Pages:
    """``_fetch``'s double: each URL answers its scripted codes in turn, then
    repeats the last one. Every fetch is recorded, and so is every wait."""

    def __init__(self, script: dict[str, list[int]]) -> None:
        self.script = {url: list(codes) for url, codes in script.items()}
        self.fetched: list[str] = []
        self.slept: list[float] = []

    def fetch(self, url: str) -> int:
        self.fetched.append(url)
        codes = self.script[url]
        return codes.pop(0) if len(codes) > 1 else codes[0]


@pytest.fixture
def pages(monkeypatch):
    def install(script):
        fake = _Pages(script)
        monkeypatch.setattr(sys.modules[__name__], "_fetch", fake.fetch)
        return fake

    return install


_REPO_PAGE = "https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp"
_PYPI = "https://pypi.org/project/stealth-chrome-devtools-mcp/"


class TestAGitHub5xxIsRetriedThenSkipped:
    """T-16103, hermetic: the 5xx path of the network test above."""

    def test_a_5xx_is_retried_with_backoff_until_the_page_answers(self, pages):
        fake = pages({_REPO_PAGE: [504, 503, 200]})

        assert _status(_REPO_PAGE, sleep=fake.slept.append) == 200

        assert fake.slept == [2.0, 5.0]
        assert len(fake.fetched) == 3

    def test_a_404_is_final_at_once(self, pages):
        fake = pages({_REPO_PAGE: [404, 200]})

        assert _status(_REPO_PAGE, sleep=fake.slept.append) == 404

        assert fake.slept == []

    def test_a_github_page_still_5xx_after_every_retry_skips_with_the_reason(
        self, pages
    ):
        fake = pages({_REPO_PAGE: [504], _PYPI: [200]})
        urls = [f"{_REPO_PAGE}#cli", f"{_REPO_PAGE}#readme", _PYPI]

        def status(url):
            return _status(url, sleep=fake.slept.append)

        with pytest.raises(pytest.skip.Exception, match=r"GitHub answered 5xx") as skip:
            _check_links(urls, status)

        assert f"'{_REPO_PAGE}#cli': 504" in str(skip.value)
        assert fake.slept == list(_RETRY_DELAYS)
        assert (
            fake.fetched.count(_REPO_PAGE) == len(_RETRY_DELAYS) + 1
        )  # not per anchor

    def test_a_blob_page_still_falls_back_to_raw_after_the_retries(self, pages):
        blob = f"{_REPO_PAGE}/blob/main/RUNBOOK.md"
        raw = (
            "https://raw.githubusercontent.com/DevinoSolutions/"
            "stealth-chrome-devtools-mcp/main/RUNBOOK.md"
        )
        fake = pages({blob: [503], raw: [200]})

        _check_links([blob], lambda url: _status(url, sleep=fake.slept.append))

        assert fake.fetched[-1] == raw

    def test_a_dead_link_still_fails_while_github_is_down(self, pages):
        fake = pages({_REPO_PAGE: [504], _PYPI: [404]})

        with pytest.raises(AssertionError, match=r"pypi\.org.*404"):
            _check_links(
                [_REPO_PAGE, _PYPI], lambda url: _status(url, sleep=fake.slept.append)
            )

    def test_a_5xx_from_another_host_still_fails(self, pages):
        fake = pages({_PYPI: [503]})

        with pytest.raises(AssertionError, match=r"pypi\.org.*503"):
            _check_links([_PYPI], lambda url: _status(url, sleep=fake.slept.append))
