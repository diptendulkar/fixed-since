"""Command line entry point."""

from __future__ import annotations

import argparse
import sys

from fixed_since.draft import render_report
from fixed_since.finder import find_candidates
from fixed_since.github import GitHubClient, GitHubError, resolve_token


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fixed-since",
        description=(
            "Find open issues that a later merged pull request already fixed. "
            "Read-only: prints a report with draft replies and never posts anything."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="check one issue")
    check.add_argument("repo", help="OWNER/NAME")
    check.add_argument("issue", type=int, help="issue number")

    scan = commands.add_parser("scan", help="check the oldest open issues")
    scan.add_argument("repo", help="OWNER/NAME")
    scan.add_argument("--limit", type=int, default=20, help="issues to check (default 20)")

    for command in (check, scan):
        command.add_argument(
            "--no-search",
            action="store_true",
            help="only use pull requests that link to the issue; skip the noisier title search",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with GitHubClient(args.repo, token=resolve_token()) as client:
            if args.command == "check":
                issues = [client.get_issue(args.issue)]
            else:
                issues = client.list_open_issues(args.limit)
            results = [
                (issue, find_candidates(client, issue, search=not args.no_search))
                for issue in issues
            ]
    except (GitHubError, ValueError) as exc:
        print(f"fixed-since: {exc}", file=sys.stderr)
        return 1
    print(render_report(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
