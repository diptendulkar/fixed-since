"""Minimal read-only GitHub REST client for the data fixed-since needs."""

from __future__ import annotations

import os
import shutil
import ssl
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import truststore

API_URL = "https://api.github.com"


class GitHubError(RuntimeError):
    """A GitHub API call failed or returned something unusable."""


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    created_at: datetime
    url: str


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str
    url: str
    merged_at: datetime
    merge_commit_sha: str | None


@dataclass(frozen=True)
class Release:
    tag: str
    url: str
    published_at: datetime


@dataclass(frozen=True)
class ChangedFile:
    path: str
    # None when GitHub omits the patch: binary files and very large diffs.
    patch: str | None


def tls_context() -> ssl.SSLContext:
    """The OS trust store, so a corporate TLS-inspecting proxy's CA is honoured
    the same way the browser honours it."""
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def resolve_token() -> str | None:
    """Use GITHUB_TOKEN / GH_TOKEN, falling back to the GitHub CLI's login."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    gh = shutil.which("gh")
    if gh is None:
        return None
    result = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, check=False)
    return result.stdout.strip() or None


def _issue(data: dict[str, Any]) -> Issue:
    return Issue(
        number=data["number"],
        title=data["title"],
        body=data.get("body") or "",
        created_at=parse_time(data["created_at"]),
        url=data["html_url"],
    )


class GitHubClient:
    """Read-only access to one repository. Never writes to GitHub."""

    def __init__(
        self,
        repo: str,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        owner, separator, name = repo.partition("/")
        if not separator or not owner or not name or "/" in name:
            raise ValueError(f"repository must look like OWNER/NAME, got {repo!r}")
        self.repo = repo
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(
            base_url=API_URL,
            headers=headers,
            timeout=30.0,
            transport=transport,
            verify=tls_context(),
        )
        self._releases: list[Release] | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise GitHubError(f"GET {path} failed: {exc}") from exc
        if response.status_code >= 400:
            raise GitHubError(f"GET {path} -> HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    def _paginate(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        items: list[Any] = []
        page = 1
        while True:
            batch = self._get(path, {**(params or {}), "per_page": 100, "page": page})
            if not batch:
                return items
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    def get_issue(self, number: int) -> Issue:
        data = self._get(f"/repos/{self.repo}/issues/{number}")
        if "pull_request" in data:
            raise GitHubError(f"#{number} is a pull request, not an issue")
        return _issue(data)

    def list_open_issues(self, limit: int) -> list[Issue]:
        """Oldest open issues first: those are the most likely to be stale."""
        issues: list[Issue] = []
        page = 1
        while len(issues) < limit:
            batch = self._get(
                f"/repos/{self.repo}/issues",
                {
                    "state": "open",
                    "sort": "created",
                    "direction": "asc",
                    "per_page": 100,
                    "page": page,
                },
            )
            if not batch:
                break
            issues.extend(_issue(item) for item in batch if "pull_request" not in item)
            page += 1
        return issues[:limit]

    def timeline(self, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{self.repo}/issues/{number}/timeline")

    def get_merged_pull(self, number: int) -> PullRequest | None:
        data = self._get(f"/repos/{self.repo}/pulls/{number}")
        if not data.get("merged_at"):
            return None
        return PullRequest(
            number=data["number"],
            title=data["title"],
            body=data.get("body") or "",
            url=data["html_url"],
            merged_at=parse_time(data["merged_at"]),
            merge_commit_sha=data.get("merge_commit_sha"),
        )

    def pull_files(self, number: int) -> list[ChangedFile]:
        return [
            ChangedFile(path=item["filename"], patch=item.get("patch"))
            for item in self._paginate(f"/repos/{self.repo}/pulls/{number}/files")
        ]

    def search_merged_prs(
        self, terms: list[str], merged_after: datetime, limit: int = 5
    ) -> list[PullRequest]:
        query = " ".join(
            [
                f"repo:{self.repo}",
                "is:pr",
                "is:merged",
                f"merged:>{merged_after.date().isoformat()}",
                *terms,
            ]
        )
        data = self._get("/search/issues", {"q": query, "per_page": limit})
        pulls = (self.get_merged_pull(item["number"]) for item in data.get("items", []))
        return [pull for pull in pulls if pull is not None]

    def releases(self) -> list[Release]:
        """Published, non-prerelease releases, oldest first."""
        if self._releases is None:
            releases = [
                Release(
                    tag=item["tag_name"],
                    url=item["html_url"],
                    published_at=parse_time(item["published_at"]),
                )
                for item in self._paginate(f"/repos/{self.repo}/releases")
                if not item.get("draft") and not item.get("prerelease") and item.get("published_at")
            ]
            self._releases = sorted(releases, key=lambda release: release.published_at)
        return self._releases

    def release_contains(self, tag: str, sha: str) -> bool:
        data = self._get(f"/repos/{self.repo}/compare/{sha}...{tag}")
        return data.get("status") in ("ahead", "identical")
