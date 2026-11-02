"""A fake GitHub API for tests: canned responses by path, every request recorded."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from fixed_since.github import GitHubClient

REPO = "acme/widget"

GraphQLHandler = Callable[[str, dict[str, Any]], dict[str, Any]]


class FakeGitHub:
    """A route's value is the JSON to return, or a function of the request that builds it."""

    def __init__(self, routes: dict[str, Any], graphql: GraphQLHandler | None = None) -> None:
        self.routes = routes
        self.graphql = graphql
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/graphql" and self.graphql is not None:
            body = json.loads(request.content)
            return httpx.Response(200, json=self.graphql(body["query"], body["variables"]))
        if path not in self.routes:
            return httpx.Response(404, json={"message": f"no route for {path}"})
        body = self.routes[path]
        if callable(body):
            body = body(request)
        if isinstance(body, list) and request.url.params.get("page", "1") != "1":
            body = []
        return httpx.Response(200, json=body)

    def client(self) -> GitHubClient:
        return GitHubClient(REPO, token="t", transport=httpx.MockTransport(self))

    def searched(self) -> bool:
        return any(r.url.path == "/search/issues" for r in self.requests)

    def search_queries(self) -> list[str]:
        return [r.url.params["q"] for r in self.requests if r.url.path == "/search/issues"]
