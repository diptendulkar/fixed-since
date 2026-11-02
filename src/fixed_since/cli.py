"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import sys

from fixed_since.draft import render_report
from fixed_since.finder import find_candidates
from fixed_since.github import GitHubClient, GitHubError, resolve_token
from fixed_since.verify import (
    AnthropicVerifier,
    Model,
    OpenAICompatibleVerifier,
    PromptPrinter,
    VerifierError,
    verify_candidates,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fixed-since",
        description=(
            "Find open issues that a later merged pull request already fixed. "
            "Read-only: prints a report with draft replies and never posts anything."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="check one issue or discussion")
    check.add_argument("repo", help="OWNER/NAME")
    check.add_argument("issue", type=int, help="issue or discussion number")
    check.add_argument(
        "--discussion", action="store_true", help="the number is a discussion, not an issue"
    )

    scan = commands.add_parser("scan", help="check the oldest open issues")
    scan.add_argument("repo", help="OWNER/NAME")
    scan.add_argument("--limit", type=int, default=20, help="how many to check (default 20)")
    scan.add_argument(
        "--discussions",
        action="store_true",
        help="scan unanswered Q&A discussions instead of open issues",
    )

    for command in (check, scan):
        command.add_argument(
            "--no-search",
            action="store_true",
            help="only use pull requests that link to the issue; skip the noisier title search",
        )
        command.add_argument(
            "--verify",
            choices=("openai", "anthropic"),
            help=(
                "have a model suggest title searches and read each promising pull "
                "request's diff. 'openai' is any Chat Completions API (OPENAI_API_KEY, "
                "OPENAI_BASE_URL for Ollama, OpenRouter, vLLM...); 'anthropic' uses "
                "ANTHROPIC_API_KEY"
            ),
        )
        command.add_argument("--model", help="model name for --verify")
        command.add_argument(
            "--show-prompt",
            action="store_true",
            help="print what would be sent to the model, to stderr, without calling one",
        )
    return parser


def _model(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Model | None:
    if args.show_prompt:
        return PromptPrinter()
    if args.verify is None:
        if args.model:
            parser.error("--model needs --verify")
        return None
    if not args.model:
        parser.error("--verify needs --model")
    if args.verify == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            parser.error("--verify anthropic needs ANTHROPIC_API_KEY")
        return AnthropicVerifier(args.model, api_key)
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    return OpenAICompatibleVerifier(args.model, os.environ.get("OPENAI_API_KEY"), base_url)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    model = _model(parser, args)
    try:
        with GitHubClient(args.repo, token=resolve_token()) as client:
            if args.command == "check" and args.discussion:
                issues = [client.get_discussion(args.issue)]
            elif args.command == "check":
                issues = [client.get_issue(args.issue)]
            elif args.discussions:
                issues = client.list_unanswered_discussions(args.limit)
            else:
                issues = client.list_open_issues(args.limit)
            results = []
            for issue in issues:
                candidates = find_candidates(
                    client, issue, search=not args.no_search, planner=model
                )
                if model is not None:
                    candidates = verify_candidates(client, issue, candidates, model)
                results.append((issue, candidates))
    except (GitHubError, VerifierError, ValueError) as exc:
        print(f"fixed-since: {exc}", file=sys.stderr)
        return 1
    print(render_report(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
