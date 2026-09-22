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
  The README's own raw-URL is NOT fetched here: it resolves only once the file is
  on ``main``, which on a PR it is not yet — the post-merge check is a ``curl``
  from a clean shell, as the release notes say.
"""

import re
import tomllib
import urllib.error
import urllib.request
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


def _status(url: str) -> int:
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
        dead = {url: _status(url) for url in _urls(_prompt())}
        dead = {url: code for url, code in dead.items() if code != 200}
        assert not dead, f"dead links in prompt.md: {dead}"
