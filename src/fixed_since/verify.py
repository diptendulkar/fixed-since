"""Ask a language model whether a pull request's diff addresses an issue."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol, TextIO

import httpx

from fixed_since.github import ChangedFile, Issue, PullRequest, tls_context

if TYPE_CHECKING:
    from fixed_since.finder import Candidate
    from fixed_since.github import GitHubClient

Outcome = Literal["addresses", "partial", "unrelated", "unsure"]
OUTCOMES: tuple[Outcome, ...] = ("addresses", "partial", "unrelated", "unsure")

MAX_ISSUE_CHARS = 6_000
MAX_PULL_BODY_CHARS = 4_000
MAX_DIFF_CHARS = 30_000
MIN_TRUNCATED_PATCH_CHARS = 1_000

# Only these are worth paying a model to check; mentions and pull requests
# superseded by a reopen are already ruled out.
VERIFIABLE = ("claims-fix", "partial", "title-search")

SYSTEM_PROMPT = """\
You check whether a merged pull request fixes a reported GitHub issue.
You are given the issue, the pull request's description, and its diff.
Judge from the diff, not the description: descriptions overclaim.

Answer with one JSON object and nothing else:
{"outcome": "addresses" | "partial" | "unrelated" | "unsure",
 "reason": "<one or two sentences naming the specific change>",
 "files": ["<changed file paths your reason relies on>"]}

- "addresses": the diff changes the behaviour the issue reports.
- "partial": the diff fixes part of it; say what is still missing.
- "unrelated": the diff does not touch what the issue reports.
- "unsure": you cannot tell, for example because the relevant code is not in the diff.
  Prefer this to guessing.
Only list files that appear in the diff."""


class VerifierError(RuntimeError):
    """The model could not be reached or refused the request."""


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    reason: str
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerifyRequest:
    issue: Issue
    pull: PullRequest
    files: list[ChangedFile]

    @property
    def changed_paths(self) -> set[str]:
        return {changed.path for changed in self.files}


class Verifier(Protocol):
    def verify(self, request: VerifyRequest) -> Verdict: ...


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}\n[... truncated]"


_LOCK_OR_GENERATED = re.compile(
    r"(\.lock|-lock\.json|-lock\.yaml|\.snap|\.min\.js|\.map)$|(^|/)(dist|build|vendor)/"
)
_TEST = re.compile(r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*$|_test\.\w+$|\.(test|spec)\.\w+$")
_DOC = re.compile(r"\.(md|mdx|rst|txt)$|(^|/)(docs?|designs?)/")


def diff_priority(path: str) -> int:
    """Order changed files by how much they say about behaviour.

    The diff budget runs out on large pull requests, so the code that
    changes behaviour goes first and docs, tests and lockfiles after it.
    """
    lowered = path.lower()
    if _LOCK_OR_GENERATED.search(lowered):
        return 3
    if _DOC.search(lowered):
        return 2
    if _TEST.search(lowered):
        return 1
    return 0


def build_prompt(request: VerifyRequest) -> str:
    issue, pull = request.issue, request.pull
    diff: list[str] = []
    omitted: list[str] = []
    budget = MAX_DIFF_CHARS
    # Smallest first within each group, so the most files fit whole and only
    # the largest is cut short.
    ordered = sorted(request.files, key=lambda f: (diff_priority(f.path), len(f.patch or "")))
    for changed in ordered:
        if changed.patch is None:
            omitted.append(f"{changed.path} (no patch: binary or too large)")
            continue
        block = f"--- {changed.path}\n{changed.patch}"
        if len(block) > budget:
            if diff_priority(changed.path) == 0 and budget >= MIN_TRUNCATED_PATCH_CHARS:
                # Part of a behaviour-changing file says more than any whole doc.
                diff.append(f"{block[:budget]}\n[... patch truncated]")
                budget = 0
            else:
                omitted.append(f"{changed.path} (left out to stay within the size limit)")
            continue
        diff.append(block)
        budget -= len(block)
    if omitted:
        diff.append("Changed files not shown:\n" + "\n".join(f"- {line}" for line in omitted))

    return "\n\n".join(
        [
            f"## Issue #{issue.number}: {issue.title}",
            _clip(issue.body, MAX_ISSUE_CHARS) or "(no description)",
            f"## Pull request #{pull.number}: {pull.title}",
            _clip(pull.body, MAX_PULL_BODY_CHARS) or "(no description)",
            "## Diff",
            "\n\n".join(diff) or "(no changes shown)",
        ]
    )


def parse_verdict(text: str, changed_paths: set[str]) -> Verdict:
    """Read the model's answer, treating anything unsupported as "unsure"."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return Verdict("unsure", "model gave no JSON answer")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return Verdict("unsure", "model gave malformed JSON")
    if not isinstance(data, dict):
        return Verdict("unsure", "model gave malformed JSON")

    outcome = data.get("outcome")
    reason = data.get("reason")
    files = data.get("files", [])
    if outcome not in OUTCOMES or not isinstance(reason, str) or not reason.strip():
        return Verdict("unsure", "model answer was missing an outcome or reason")
    if not isinstance(files, list) or not all(isinstance(path, str) for path in files):
        return Verdict("unsure", "model answer listed files in the wrong shape")

    invented = sorted(set(files) - changed_paths)
    if invented:
        return Verdict(
            "unsure", f"model cited files that are not in the diff: {', '.join(invented)}"
        )
    if outcome in ("addresses", "partial") and not files:
        return Verdict("unsure", "model claimed a fix without citing a changed file")
    return Verdict(outcome, reason.strip(), tuple(files))


def _post(http: httpx.Client, url: str, body: dict[str, Any]) -> Any:
    try:
        response = http.post(url, json=body)
    except httpx.HTTPError as exc:
        raise VerifierError(f"POST {url} failed: {exc}") from exc
    if response.status_code >= 400:
        raise VerifierError(f"POST {url} -> HTTP {response.status_code}: {response.text[:200]}")
    return response.json()


class OpenAICompatibleVerifier:
    """Any Chat Completions API: OpenAI, OpenRouter, Ollama, LM Studio, vLLM."""

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.model = model
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._http = httpx.Client(
            headers=headers, timeout=120.0, transport=transport, verify=tls_context()
        )

    def verify(self, request: VerifyRequest) -> Verdict:
        payload = _post(
            self._http,
            self._url,
            {
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_prompt(request)},
                ],
            },
        )
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return Verdict("unsure", "model response had no message content")
        return parse_verdict(str(text), request.changed_paths)


class AnthropicVerifier:
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self._url = f"{base_url.rstrip('/')}/v1/messages"
        self._http = httpx.Client(
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            timeout=120.0,
            transport=transport,
            verify=tls_context(),
        )

    def verify(self, request: VerifyRequest) -> Verdict:
        payload = _post(
            self._http,
            self._url,
            {
                "model": self.model,
                "max_tokens": 500,
                "temperature": 0,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": build_prompt(request)}],
            },
        )
        try:
            text = "".join(
                block["text"] for block in payload["content"] if block.get("type") == "text"
            )
        except (KeyError, TypeError, AttributeError):
            return Verdict("unsure", "model response had no text content")
        return parse_verdict(text, request.changed_paths)


class PromptPrinter:
    """Prints what would be sent instead of calling a model."""

    def __init__(self, stream: TextIO = sys.stderr) -> None:
        self._stream = stream

    def verify(self, request: VerifyRequest) -> Verdict:
        self._stream.write(
            f"\n===== prompt for issue #{request.issue.number}, pull #{request.pull.number} "
            f"=====\n[system]\n{SYSTEM_PROMPT}\n\n[user]\n{build_prompt(request)}\n"
        )
        return Verdict("unsure", "not checked: --show-prompt printed the prompt instead")


def verify_candidates(
    client: GitHubClient,
    issue: Issue,
    candidates: list[Candidate],
    verifier: Verifier,
    *,
    max_per_issue: int = 3,
) -> list[Candidate]:
    """Attach a verdict to the strongest few candidates worth checking."""
    checked = 0
    result: list[Candidate] = []
    for candidate in candidates:
        if candidate.evidence in VERIFIABLE and checked < max_per_issue:
            checked += 1
            request = VerifyRequest(issue, candidate.pull, client.pull_files(candidate.pull.number))
            candidate = replace(candidate, verdict=verifier.verify(request))
        result.append(candidate)
    return result
