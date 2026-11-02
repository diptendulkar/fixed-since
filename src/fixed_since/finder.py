"""Find merged pull requests that may have fixed an open issue."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from fixed_since.github import GitHubClient, Issue, PullRequest, Release, parse_time
from fixed_since.verify import Verdict

Evidence = Literal["claims-fix", "partial", "title-search", "reopened-after", "mention"]

# Strongest first; candidates are sorted by this.
EVIDENCE_ORDER: tuple[Evidence, ...] = (
    "claims-fix",
    "partial",
    "title-search",
    "reopened-after",
    "mention",
)

_FIX_WORDS = r"(?:fix|fixes|fixed|close|closes|closed|resolve|resolves|resolved)"
_PARTIAL_WORDS = (
    r"(?:part of|related to|relates to|towards?|addresses|address|implements?|"
    r"refs?|follow[- ]up (?:to|for)|progress on)"
)

_STOPWORDS = frozenset(
    """
    the and for with when not does doesn don can cannot isn are was were has have
    from into after before should would could this that its how why what which
    error errors issue bug question fails failed failing broken support using use
    get gets make makes work working works new add feature request still always
    """.split()
)


@dataclass(frozen=True)
class Candidate:
    pull: PullRequest
    evidence: Evidence
    release: Release | None
    # Set when a model has read the diff; None when unchecked.
    verdict: Verdict | None = None


def classify_reference(text: str, issue_number: int) -> Evidence:
    """How a pull request's title and description refer to an issue.

    A pull request that links to an issue usually only mentions it: as an
    example in a table, or as "adjacent work". Only wording that says it fixes
    the issue, or is part of fixing it, counts as more than a mention.
    """
    ref = rf"#{issue_number}(?!\d)"
    strongest: Evidence = "mention"
    for line in text.lower().splitlines():
        if not re.search(ref, line):
            continue
        if re.search(rf"\b{_FIX_WORDS}\b:?\s+(?:issue\s+)?{ref}", line):
            return "claims-fix"
        if re.search(rf"\b{_PARTIAL_WORDS}\b[^#\n]{{0,20}}{ref}", line):
            strongest = "partial"
    return strongest


def linked_pull_numbers(events: list[dict[str, Any]], repo: str) -> list[int]:
    """Pull requests in ``repo`` whose description or commits reference the issue."""
    numbers: list[int] = []
    for event in events:
        if event.get("event") != "cross-referenced":
            continue
        source = (event.get("source") or {}).get("issue") or {}
        same_repo = (source.get("repository") or {}).get("full_name") == repo
        if "pull_request" in source and same_repo and source["number"] not in numbers:
            numbers.append(source["number"])
    return numbers


def last_reopened(events: list[dict[str, Any]]) -> datetime | None:
    times = [parse_time(e["created_at"]) for e in events if e.get("event") == "reopened"]
    return max(times, default=None)


def salient_terms(title: str, max_terms: int = 3) -> list[str]:
    """The most specific words in an issue title, for a pull-request search.

    GitHub search ANDs every term, so a few long, distinctive words find more
    than the whole title does.
    """
    words: list[str] = []
    for word in re.findall(r"[a-z][a-z0-9_\-]{2,}", title.lower()):
        if word not in _STOPWORDS and word not in words:
            words.append(word)
    return sorted(words, key=len, reverse=True)[:max_terms]


def first_release_containing(client: GitHubClient, pull: PullRequest) -> Release | None:
    if not pull.merge_commit_sha:
        return None
    for release in client.releases():
        if release.published_at < pull.merged_at:
            continue
        if client.release_contains(release.tag, pull.merge_commit_sha):
            return release
    return None


def find_candidates(client: GitHubClient, issue: Issue, *, search: bool = True) -> list[Candidate]:
    """Merged pull requests that may fix ``issue``, strongest evidence first.

    Pull requests that link to the issue are checked first. A title search
    runs only when none of them claims to fix or advance it, because a search
    match is much noisier. A pull request merged before the issue was last
    reopened is known not to have been enough, whatever it claims.
    """
    events = client.timeline(issue.number)
    reopened = last_reopened(events)
    found: list[tuple[PullRequest, Evidence]] = []
    for number in linked_pull_numbers(events, client.repo):
        pull = client.get_merged_pull(number)
        if pull is None or pull.merged_at <= issue.created_at:
            continue
        if reopened is not None and pull.merged_at <= reopened:
            found.append((pull, "reopened-after"))
        else:
            found.append((pull, classify_reference(f"{pull.title}\n{pull.body}", issue.number)))

    if search and not any(evidence in ("claims-fix", "partial") for _, evidence in found):
        terms = salient_terms(issue.title)
        known = {pull.number for pull, _ in found}
        if terms:
            found.extend(
                (pull, "title-search")
                for pull in client.search_merged_prs(terms, issue.created_at)
                if pull.number not in known
            )

    found.sort(key=lambda item: EVIDENCE_ORDER.index(item[1]))
    return [
        Candidate(pull=pull, evidence=evidence, release=first_release_containing(client, pull))
        for pull, evidence in found
    ]
