from __future__ import annotations

from typing import Any

import httpx
import pytest
from fakes import REPO, FakeGitHub

from fixed_since.draft import draft_reply, render_report
from fixed_since.finder import classify_reference, find_candidates, salient_terms
from fixed_since.github import GitHubClient, GitHubError

ISSUE_OPENED = "2026-06-01T00:00:00Z"
MERGED = "2026-08-20T00:00:00Z"


def _issue(number: int = 1, title: str = "Worker auth expires after idle") -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "body": "",
        "created_at": ISSUE_OPENED,
        "html_url": f"https://github.com/{REPO}/issues/{number}",
    }


def _pull(
    number: int, merged_at: str | None = MERGED, *, body: str = "Fixes #1.", sha: str = "fix"
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"change {number}",
        "body": body,
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "merged_at": merged_at,
        "merge_commit_sha": sha,
    }


def _cross_ref(number: int, repo: str = REPO) -> dict[str, Any]:
    return {
        "event": "cross-referenced",
        "source": {
            "issue": {"number": number, "pull_request": {}, "repository": {"full_name": repo}}
        },
    }


def _release(tag: str, published_at: str, *, prerelease: bool = False) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
        "published_at": published_at,
        "draft": False,
        "prerelease": prerelease,
    }


def _routes(search: list[int] | None = None, **extra: Any) -> dict[str, Any]:
    base = f"/repos/{REPO}"
    return {
        f"{base}/issues/1": _issue(),
        f"{base}/issues/1/timeline": [],
        f"{base}/releases": [],
        "/search/issues": {"items": [{"number": n} for n in search or []]},
        **{f"{base}/{path}": body for path, body in extra.items()},
    }


def _linked(*pulls: dict[str, Any], **extra: Any) -> FakeGitHub:
    """A repository where each given pull request links to issue #1."""
    return FakeGitHub(
        _routes(
            **{
                "issues/1/timeline": [_cross_ref(p["number"]) for p in pulls],
                **{f"pulls/{p['number']}": p for p in pulls},
                **extra,
            }
        )
    )


def _candidates(fake: FakeGitHub, *, search: bool = True) -> list[Any]:
    with fake.client() as client:
        return find_candidates(client, client.get_issue(1), search=search)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Fixes #1.", "claims-fix"),
        ("This closes issue #1", "claims-fix"),
        ("Resolves: #1", "claims-fix"),
        ("Part of #1.", "partial"),
        ("Related to #1 and OMNI-13099.", "partial"),
        ("| #1 pin input box (cosmetic) | P3 | P3 |", "mention"),
        ("#3 and #1 (skill sources) touch adjacent areas.", "mention"),
        ("Fixes #12, part of #1", "partial"),
    ],
)
def test_classify_reference(text: str, expected: str) -> None:
    assert classify_reference(text, 1) == expected


def test_a_longer_issue_number_is_not_a_reference() -> None:
    assert classify_reference("Fixes #12", 1) == "mention"


def test_salient_terms_prefers_long_specific_words() -> None:
    assert salient_terms("[Question] Long-running worker auth fails when idle") == [
        "long-running",
        "worker",
        "auth",
    ]


def test_salient_terms_drops_stopwords() -> None:
    assert salient_terms("How does it work") == []


def test_client_rejects_a_malformed_repository() -> None:
    with pytest.raises(ValueError, match="OWNER/NAME"):
        GitHubClient("just-a-name")


def test_get_issue_refuses_a_pull_request() -> None:
    fake = FakeGitHub(_routes(**{"issues/1": {**_issue(), "pull_request": {}}}))
    with fake.client() as client, pytest.raises(GitHubError, match="pull request"):
        client.get_issue(1)


def test_a_network_failure_is_reported_as_a_github_error() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("certificate verify failed", request=request)

    client = GitHubClient(REPO, transport=httpx.MockTransport(unreachable))
    with client, pytest.raises(GitHubError, match="certificate verify failed"):
        client.get_issue(1)


def test_pulls_that_cannot_be_the_fix_are_dropped() -> None:
    fake = FakeGitHub(
        _routes(
            **{
                "issues/1/timeline": [
                    _cross_ref(11),  # merged before the issue was opened
                    _cross_ref(12),  # never merged
                    _cross_ref(13, repo="someone/fork"),  # another repository
                ],
                "pulls/11": _pull(11, "2026-05-01T00:00:00Z"),
                "pulls/12": _pull(12, None),
            }
        )
    )
    assert _candidates(fake, search=False) == []


def test_candidates_are_ordered_strongest_evidence_first() -> None:
    fake = _linked(
        _pull(10, body="| #1 cosmetic | P3 |"),
        _pull(11, body="Part of #1."),
        _pull(12, body="Fixes #1."),
    )
    assert [(c.pull.number, c.evidence) for c in _candidates(fake)] == [
        (12, "claims-fix"),
        (11, "partial"),
        (10, "mention"),
    ]


def test_a_fix_merged_before_the_issue_was_reopened_was_not_enough() -> None:
    fake = _linked(
        _pull(10, "2026-08-14T00:00:00Z"),
        _pull(11, "2026-09-01T00:00:00Z"),
    )
    fake.routes[f"/repos/{REPO}/issues/1/timeline"].append(
        {"event": "reopened", "created_at": "2026-08-20T00:00:00Z"}
    )
    candidates = _candidates(fake, search=False)
    assert [(c.pull.number, c.evidence) for c in candidates] == [
        (11, "claims-fix"),
        (10, "reopened-after"),
    ]
    assert draft_reply(candidates[1]) is None


def test_a_pull_claiming_the_fix_skips_the_title_search() -> None:
    fake = _linked(_pull(10))
    _candidates(fake)
    assert not fake.searched()


def test_a_mere_mention_still_triggers_the_title_search() -> None:
    fake = _linked(
        _pull(10, body="#1 touches adjacent areas."),
        **{"pulls/20": _pull(20, "2026-08-25T00:00:00Z", body="")},
    )
    fake.routes["/search/issues"] = {"items": [{"number": 20}, {"number": 10}]}
    found = [(c.pull.number, c.evidence) for c in _candidates(fake)]
    assert found == [(20, "title-search"), (10, "mention")]
    (query,) = fake.search_queries()
    assert f"repo:{REPO}" in query and "merged:>2026-06-01" in query and "is:merged" in query
    assert "in:title" in query


def test_release_is_the_first_published_after_the_merge_that_contains_it() -> None:
    fake = _linked(
        _pull(10),
        releases=[
            _release("v0.10.0", "2026-08-01T00:00:00Z"),
            _release("v0.11.0rc1", "2026-08-21T00:00:00Z", prerelease=True),
            _release("v0.11.0", "2026-08-22T00:00:00Z"),
            _release("v0.12.0", "2026-09-01T00:00:00Z"),
        ],
        **{
            "compare/fix...v0.11.0": {"status": "ahead"},
            "compare/fix...v0.12.0": {"status": "ahead"},
        },
    )
    (candidate,) = _candidates(fake)
    assert candidate.release is not None and candidate.release.tag == "v0.11.0"
    compared = [r.url.path for r in fake.requests if "/compare/" in r.url.path]
    assert compared == [f"/repos/{REPO}/compare/fix...v0.11.0"]


def test_a_merge_missing_from_every_release_is_unreleased() -> None:
    fake = _linked(
        _pull(10),
        releases=[_release("v0.11.0", "2026-08-22T00:00:00Z")],
        **{"compare/fix...v0.11.0": {"status": "diverged"}},
    )
    (candidate,) = _candidates(fake)
    assert candidate.release is None
    assert "not in a release yet" in (draft_reply(candidate) or "")


def test_a_partial_fix_asks_what_is_still_missing() -> None:
    (candidate,) = _candidates(_linked(_pull(10, body="Part of #1.")))
    reply = draft_reply(candidate) or ""
    assert reply.startswith("Work toward this landed in #10")
    assert "is something still missing?" in reply


def test_weak_evidence_gets_no_draft() -> None:
    (candidate,) = _candidates(_linked(_pull(10, body="See #1.")), search=False)
    assert candidate.evidence == "mention"
    assert draft_reply(candidate) is None


def test_report_drafts_only_for_likely_fixes() -> None:
    fake = _linked(
        _pull(10),
        releases=[_release("v0.11.0", "2026-08-22T00:00:00Z")],
        **{"compare/fix...v0.11.0": {"status": "ahead"}},
    )
    with fake.client() as client:
        issue = client.get_issue(1)
        report = render_report([(issue, find_candidates(client, issue))])
    assert "1 of 1 issues have a likely fix" in report
    assert "> This looks like it was fixed by #10" in report
    assert "[v0.11.0](https://github.com/acme/widget/releases/tag/v0.11.0)" in report
    assert "Weak matches" not in report

    mention_only = _linked(_pull(10, body="See #1."))
    with mention_only.client() as client:
        issue = client.get_issue(1)
        report = render_report([(issue, find_candidates(client, issue, search=False))])
    assert "0 of 1 issues have a likely fix" in report
    assert "## Weak matches, no draft" in report
    assert "Draft reply" not in report


def test_the_client_only_ever_reads() -> None:
    fake = _linked(_pull(10, body="See #1."), **{"pulls/20": _pull(20, body="")})
    fake.routes["/search/issues"] = {"items": [{"number": 20}]}
    _candidates(fake)
    assert {r.method for r in fake.requests} == {"GET"}
