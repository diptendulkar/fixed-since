from __future__ import annotations

from typing import Any

import httpx
import pytest
from fakes import REPO, FakeGitHub

from fixed_since.draft import draft_kind, render_report
from fixed_since.finder import find_candidates
from fixed_since.github import GitHubError, Issue
from fixed_since.verify import parse_queries

DISCUSSION = {
    "number": 1566,
    "title": "Long-running worker auth",
    "body": "The worker needs `omnigent login` again after a few idle hours.",
    "createdAt": "2026-06-29T00:00:00Z",
    "url": f"https://github.com/{REPO}/discussions/1566",
}


def _pull(number: int, merged_at: str = "2026-08-21T00:00:00Z") -> dict[str, Any]:
    return {
        "number": number,
        "title": f"change {number}",
        "body": "",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "merged_at": merged_at,
        "merge_commit_sha": f"sha{number}",
    }


def _graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    if "discussionCategories" in query:
        nodes = [{"id": "QA", "isAnswerable": True}, {"id": "NEWS", "isAnswerable": False}]
        return {"data": {"repository": {"discussionCategories": {"nodes": nodes}}}}
    if "discussions(" in query:
        assert variables["category"] == "QA", "only answerable categories are scanned"
        newer = {**DISCUSSION, "number": 2000, "createdAt": "2026-07-01T00:00:00Z"}
        nodes = [newer, DISCUSSION]
        return {"data": {"repository": {"discussions": {"nodes": nodes}}}}
    found = DISCUSSION if variables["number"] == 1566 else None
    return {"data": {"repository": {"discussion": found}}}


def _fake(search: dict[str, list[int]] | None = None, **pulls: dict[str, Any]) -> FakeGitHub:
    """``search`` answers each title search by the first phrase its query contains."""
    results = search or {}

    def search_route(request: httpx.Request) -> dict[str, Any]:
        query = request.url.params["q"]
        numbers = next((v for phrase, v in results.items() if phrase in query), [])
        return {"items": [{"number": n} for n in numbers]}

    routes = {
        f"/repos/{REPO}/releases": [],
        "/search/issues": search_route,
        **{f"/repos/{REPO}/pulls/{n}": p for n, p in pulls.items()},
    }
    return FakeGitHub(routes, graphql=_graphql)


class Planner:
    def __init__(self, queries: list[str]) -> None:
        self.queries = queries
        self.asked: list[Issue] = []

    def suggest_queries(self, issue: Issue) -> list[str]:
        self.asked.append(issue)
        return self.queries


def test_get_discussion_reads_it_through_graphql() -> None:
    with _fake().client() as client:
        discussion = client.get_discussion(1566)
    assert (discussion.number, discussion.kind) == (1566, "discussion")
    assert discussion.url.endswith("/discussions/1566")


def test_a_missing_discussion_is_an_error() -> None:
    with _fake().client() as client, pytest.raises(GitHubError, match="not a discussion"):
        client.get_discussion(7)


def test_graphql_errors_are_reported() -> None:
    fake = FakeGitHub({}, graphql=lambda q, v: {"errors": [{"message": "rate limited"}]})
    with fake.client() as client, pytest.raises(GitHubError, match="rate limited"):
        client.get_discussion(1566)


def test_scan_reads_only_answerable_categories_oldest_first() -> None:
    with _fake().client() as client:
        found = client.list_unanswered_discussions(limit=1)
    assert [d.number for d in found] == [1566]


def test_a_discussion_is_searched_and_never_has_its_timeline_read() -> None:
    fake = _fake(search={"long-running": [4743]}, **{"4743": _pull(4743)})
    with fake.client() as client:
        candidates = find_candidates(client, client.get_discussion(1566))
    assert [(c.pull.number, c.evidence) for c in candidates] == [(4743, "title-search")]
    assert not any("timeline" in r.url.path for r in fake.requests)
    assert "merged:>2026-06-29" in fake.search_queries()[0]


def test_model_suggested_searches_add_what_the_title_search_missed() -> None:
    fake = _fake(
        search={"long-running": [11289], "refresh credential": [4743], "refresh grant": [11289]},
        **{"11289": _pull(11289), "4743": _pull(4743)},
    )
    planner = Planner(["refresh credential", "refresh grant"])
    with fake.client() as client:
        candidates = find_candidates(client, client.get_discussion(1566), planner=planner)
    assert [(c.pull.number, c.evidence) for c in candidates] == [
        (11289, "title-search"),
        (4743, "model-search"),
    ]
    assert len(fake.search_queries()) == 3


def test_the_planner_is_not_asked_when_a_linked_pull_claims_the_fix() -> None:
    issue_routes = {
        f"/repos/{REPO}/issues/1": {
            "number": 1,
            "title": "Broken",
            "body": "",
            "created_at": "2026-06-01T00:00:00Z",
            "html_url": "u",
        },
        f"/repos/{REPO}/issues/1/timeline": [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {"number": 9, "pull_request": {}, "repository": {"full_name": REPO}}
                },
            }
        ],
        f"/repos/{REPO}/pulls/9": {**_pull(9), "body": "Fixes #1"},
        f"/repos/{REPO}/releases": [],
    }
    planner = Planner(["anything"])
    with FakeGitHub(issue_routes).client() as client:
        find_candidates(client, client.get_issue(1), planner=planner)
    assert planner.asked == []


def test_an_unverified_discussion_match_gets_no_draft() -> None:
    fake = _fake(search={"long-running": [4743]}, **{"4743": _pull(4743)})
    with fake.client() as client:
        discussion = client.get_discussion(1566)
        candidates = find_candidates(client, discussion)
    assert draft_kind(candidates[0]) is None
    report = render_report([(discussion, candidates)])
    assert report.startswith("# fixed-since: 0 of 1 discussions have a likely fix")
    assert "Weak matches, no draft" in report


def test_the_report_lists_what_was_checked_and_found_nothing() -> None:
    with _fake().client() as client:
        discussion = client.get_discussion(1566)
        report = render_report([(discussion, find_candidates(client, discussion))])
    assert "## Nothing found" in report and "#1566" in report


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '["refresh host credential", "token expiry"]',
            ["refresh host credential", "token expiry"],
        ),
        ('```json\n["a b"]\n```', ["a b"]),
        ('["refresh token repo:other/repo is:open"]', ["refresh token"]),
        ('["one two three four five"]', ["one two three"]),
        ('["Token Expiry", "token expiry", ""]', ["token expiry"]),
        ('["a", "b", "c", "d"]', ["a", "b", "c"]),
        ("no array here", []),
        ('{"queries": "nope"}', []),
        ("[1, 2]", []),
    ],
)
def test_parse_queries_keeps_only_plain_words(text: str, expected: list[str]) -> None:
    assert parse_queries(text) == expected
