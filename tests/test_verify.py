from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from fixed_since import verify
from fixed_since.cli import main
from fixed_since.draft import draft_kind, draft_reply, render_report
from fixed_since.finder import Candidate
from fixed_since.github import ChangedFile, GitHubClient, Issue, PullRequest
from fixed_since.verify import (
    AnthropicVerifier,
    OpenAICompatibleVerifier,
    PromptPrinter,
    Verdict,
    VerifierError,
    VerifyRequest,
    build_prompt,
    parse_verdict,
    verify_candidates,
)

WHEN = datetime(2026, 8, 20, tzinfo=timezone.utc)
ISSUE = Issue(1, "Host forgets its login after idle", "Comes back logged out.", WHEN, "u")
PULL = PullRequest(10, "Refresh host tokens", "Fixes #1.", "u", WHEN, "sha")
FILES = [
    ChangedFile("omnigent/cli_auth.py", "+def refresh_stored_token(): ..."),
    ChangedFile("docs/logo.png", None),
]
REQUEST = VerifyRequest(ISSUE, PULL, FILES)


def _answer(outcome: str = "addresses", files: Any = ("omnigent/cli_auth.py",)) -> str:
    return json.dumps({"outcome": outcome, "reason": "Adds token refresh.", "files": list(files)})


def _candidate(evidence: str, outcome: str | None = None) -> Candidate:
    verdict = Verdict(outcome, "because", ("f",)) if outcome else None  # type: ignore[arg-type]
    return Candidate(PULL, evidence, None, verdict)  # type: ignore[arg-type]


class StubVerifier:
    def __init__(self, outcome: str = "addresses") -> None:
        self.outcome = outcome
        self.requests: list[VerifyRequest] = []

    def verify(self, request: VerifyRequest) -> Verdict:
        self.requests.append(request)
        return Verdict(self.outcome, "stub", ())  # type: ignore[arg-type]


def test_prompt_carries_the_issue_the_pull_and_its_diff() -> None:
    prompt = build_prompt(REQUEST)
    assert "## Issue #1: Host forgets its login after idle" in prompt
    assert "Comes back logged out." in prompt
    assert "## Pull request #10: Refresh host tokens" in prompt
    assert "--- omnigent/cli_auth.py\n+def refresh_stored_token(): ..." in prompt
    assert "docs/logo.png (no patch: binary or too large)" in prompt


def test_prompt_lists_files_left_out_to_stay_within_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify, "MAX_DIFF_CHARS", 60)
    files = [ChangedFile("small.py", "+x"), ChangedFile("big.py", "+" + "y" * 500)]
    prompt = build_prompt(VerifyRequest(ISSUE, PULL, files))
    assert "--- small.py" in prompt
    assert "--- big.py" not in prompt
    assert "big.py (left out to stay within the size limit)" in prompt


@pytest.mark.parametrize(
    ("path", "priority"),
    [
        ("web/src/pages/settings/SettingsCustomAgentsSection.tsx", 0),
        ("omnigent/server/feature_flags.py", 0),
        ("tests/server/test_feature_flags.py", 1),
        ("web/src/lib/capabilities.test.ts", 1),
        ("pkg/thing_test.go", 1),
        ("designs/CUSTOM_AGENTS_SETTINGS_UI.md", 2),
        ("docs/guide/setup.rst", 2),
        ("uv.lock", 3),
        ("web/pnpm-lock.yaml", 3),
        ("web/dist/app.min.js", 3),
    ],
)
def test_diff_priority(path: str, priority: int) -> None:
    assert verify.diff_priority(path) == priority


def test_source_code_gets_the_diff_budget_before_docs_and_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify, "MAX_DIFF_CHARS", 120)  # room for two of the three files
    files = [
        ChangedFile("designs/PLAN.md", "+" + "d" * 30),
        ChangedFile("tests/test_ui.py", "+" + "t" * 30),
        ChangedFile("web/src/Settings.tsx", "+" + "s" * 30),
    ]
    prompt = build_prompt(VerifyRequest(ISSUE, PULL, files))
    assert "--- web/src/Settings.tsx" in prompt
    assert "--- tests/test_ui.py" in prompt
    assert "designs/PLAN.md (left out to stay within the size limit)" in prompt


def test_a_source_file_too_big_for_the_budget_is_truncated_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify, "MAX_DIFF_CHARS", 100)
    monkeypatch.setattr(verify, "MIN_TRUNCATED_PATCH_CHARS", 50)
    files = [ChangedFile("src/big.py", "+" + "b" * 500), ChangedFile("README.md", "+doc")]
    prompt = build_prompt(VerifyRequest(ISSUE, PULL, files))
    assert "--- src/big.py" in prompt and "[... patch truncated]" in prompt
    assert "README.md (left out to stay within the size limit)" in prompt


def test_small_source_files_fit_before_a_large_one_is_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify, "MAX_DIFF_CHARS", 200)
    monkeypatch.setattr(verify, "MIN_TRUNCATED_PATCH_CHARS", 50)
    files = [ChangedFile("src/big.py", "+" + "b" * 500), ChangedFile("src/nav.tsx", "+nav")]
    prompt = build_prompt(VerifyRequest(ISSUE, PULL, files))
    assert "--- src/nav.tsx\n+nav" in prompt
    assert prompt.index("--- src/nav.tsx") < prompt.index("--- src/big.py")
    assert "[... patch truncated]" in prompt


def test_parse_reads_a_supported_answer() -> None:
    verdict = parse_verdict(_answer(), REQUEST.changed_paths)
    assert verdict == Verdict("addresses", "Adds token refresh.", ("omnigent/cli_auth.py",))


def test_parse_finds_json_wrapped_in_a_code_fence() -> None:
    assert parse_verdict(f"```json\n{_answer()}\n```", REQUEST.changed_paths).outcome == "addresses"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("I think it is fixed.", "no JSON"),
        ("{not json}", "malformed JSON"),
        (json.dumps({"outcome": "definitely", "reason": "x"}), "missing an outcome"),
        (json.dumps({"outcome": "addresses", "reason": ""}), "missing an outcome"),
        (_answer(files=["omnigent/server.py"]), "not in the diff: omnigent/server.py"),
        (_answer(files=[]), "without citing a changed file"),
    ],
)
def test_parse_treats_anything_unsupported_as_unsure(text: str, reason: str) -> None:
    verdict = parse_verdict(text, REQUEST.changed_paths)
    assert verdict.outcome == "unsure"
    assert reason in verdict.reason


def test_unrelated_needs_no_files() -> None:
    assert parse_verdict(_answer("unrelated", files=[]), REQUEST.changed_paths).outcome == (
        "unrelated"
    )


def test_openai_compatible_request_and_response() -> None:
    sent: list[httpx.Request] = []

    def server(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": _answer()}}]})

    verifier = OpenAICompatibleVerifier(
        "local-model", "k", "http://localhost:11434/v1/", transport=httpx.MockTransport(server)
    )
    assert verifier.verify(REQUEST).outcome == "addresses"
    (request,) = sent
    body = json.loads(request.content)
    assert str(request.url) == "http://localhost:11434/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer k"
    assert body["model"] == "local-model" and body["temperature"] == 0
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_openai_compatible_works_without_a_key_for_local_servers() -> None:
    sent: list[httpx.Request] = []

    def server(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": _answer()}}]})

    OpenAICompatibleVerifier("m", None, transport=httpx.MockTransport(server)).verify(REQUEST)
    assert "authorization" not in sent[0].headers


def test_anthropic_request_and_response() -> None:
    sent: list[httpx.Request] = []

    def server(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"content": [{"type": "text", "text": _answer()}]})

    verifier = AnthropicVerifier("claude-x", "k", transport=httpx.MockTransport(server))
    assert verifier.verify(REQUEST).outcome == "addresses"
    (request,) = sent
    body = json.loads(request.content)
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "k"
    assert body["system"] == verify.SYSTEM_PROMPT and body["temperature"] == 0


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_both_clients_plan_searches_with_the_query_prompt(provider: str) -> None:
    sent: list[dict] = []
    answer = '["refresh credential", "repo:evil/x token"]'

    def server(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if provider == "openai":
            return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})
        return httpx.Response(200, json={"content": [{"type": "text", "text": answer}]})

    transport = httpx.MockTransport(server)
    model = (
        OpenAICompatibleVerifier("m", "k", transport=transport)
        if provider == "openai"
        else AnthropicVerifier("m", "k", transport=transport)
    )
    assert model.suggest_queries(ISSUE) == ["refresh credential", "token"]
    (body,) = sent
    system = body["messages"][0]["content"] if provider == "openai" else body["system"]
    assert system == verify.QUERY_SYSTEM_PROMPT
    assert ISSUE.title in json.dumps(body)


def test_prompt_printer_shows_the_query_prompt_and_suggests_nothing() -> None:
    stream = io.StringIO()
    assert PromptPrinter(stream).suggest_queries(ISSUE) == []
    assert ISSUE.title in stream.getvalue()


def test_a_refused_request_is_a_verifier_error() -> None:
    def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    verifier = AnthropicVerifier("m", "k", transport=httpx.MockTransport(server))
    with pytest.raises(VerifierError, match="HTTP 401"):
        verifier.verify(REQUEST)


def test_a_response_without_content_is_unsure() -> None:
    def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    verifier = OpenAICompatibleVerifier("m", "k", transport=httpx.MockTransport(server))
    assert verifier.verify(REQUEST).outcome == "unsure"


def test_prompt_printer_shows_the_prompt_and_calls_nothing() -> None:
    stream = io.StringIO()
    verdict = PromptPrinter(stream).verify(REQUEST)
    assert verdict.outcome == "unsure"
    assert "[system]" in stream.getvalue() and "## Issue #1" in stream.getvalue()


def test_checks_are_capped_per_kind_of_evidence() -> None:
    def files(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"filename": "a.py", "patch": "+a"}])

    client = GitHubClient("acme/widget", transport=httpx.MockTransport(files))
    candidates = [
        _candidate("claims-fix"),
        _candidate("title-search"),
        _candidate("title-search"),
        _candidate("title-search"),  # third of its kind: over the cap
        _candidate("model-search"),  # still checked despite the noisy title searches
        _candidate("reopened-after"),
        _candidate("mention"),
    ]
    stub = StubVerifier()
    checked = verify_candidates(client, ISSUE, candidates, stub, max_per_evidence=2)
    assert [c.verdict is not None for c in checked] == [
        True,
        True,
        True,
        False,
        True,
        False,
        False,
    ]
    assert stub.requests[0].files == [ChangedFile("a.py", "+a")]


@pytest.mark.parametrize(
    ("evidence", "outcome", "expected"),
    [
        ("claims-fix", None, "fixed"),
        ("claims-fix", "addresses", "fixed"),
        ("claims-fix", "unsure", "fixed"),
        ("claims-fix", "partial", "ask"),
        ("claims-fix", "unrelated", None),
        ("partial", "addresses", "ask"),
        ("partial", "unrelated", None),
        ("title-search", None, None),
        ("title-search", "unsure", None),
        ("title-search", "addresses", "ask"),
        ("mention", "addresses", None),
        ("reopened-after", "addresses", None),
    ],
)
def test_a_model_can_lower_a_draft_but_never_raise_it_to_fixed(
    evidence: str, outcome: str | None, expected: str | None
) -> None:
    assert draft_kind(_candidate(evidence, outcome)) == expected


def test_a_checked_title_search_match_gets_a_question_not_a_claim() -> None:
    reply = draft_reply(_candidate("title-search", "addresses")) or ""
    assert "may cover it" in reply and "probably be closed" not in reply


def test_report_shows_the_diff_check_and_drafts_from_the_first_usable_candidate() -> None:
    rejected = _candidate("claims-fix", "unrelated")
    usable = Candidate(
        PullRequest(11, "Other", "", "u", WHEN, "s"), "partial", None, Verdict("partial", "x", ())
    )
    report = render_report([(ISSUE, [rejected, usable])])
    assert "Diff check: **unrelated**. because" in report
    assert "> Work toward this landed in #11" in report


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--verify", "openai"], "--verify needs --model"),
        (["--model", "m"], "--model needs --verify"),
    ],
)
def test_cli_rejects_incomplete_verify_options(
    args: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["check", "acme/widget", "1", *args])
    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err


def test_cli_requires_an_anthropic_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        main(["check", "acme/widget", "1", "--verify", "anthropic", "--model", "m"])
    assert "needs ANTHROPIC_API_KEY" in capsys.readouterr().err
