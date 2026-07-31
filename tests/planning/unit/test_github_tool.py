from __future__ import annotations

import asyncio

import httpx
import pytest

from changepilot.planning.adapters.tools.github import (
    GitHubPullRequestInput,
    GitHubPullRequestTool,
    parse_github_pull_request_url,
)


def test_github_url_parser_rejects_non_github_hosts() -> None:
    with pytest.raises(ValueError, match="github.com"):
        parse_github_pull_request_url(
            "https://example.com/acme/orders/pull/7"
        )


def test_github_tool_is_bound_to_submitted_pull_request() -> None:
    tool = GitHubPullRequestTool(
        allowed_pull_request_url=(
            "https://github.com/acme/orders/pull/7"
        )
    )

    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(
            tool.execute(
                GitHubPullRequestInput(
                    pull_request_url=(
                        "https://github.com/acme/orders/pull/8"
                    )
                )
            )
        )


def test_github_tool_reads_metadata_files_and_checks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(
                200,
                json={
                    "html_url": "https://github.com/acme/orders/pull/7",
                    "title": "Add schema migration",
                    "state": "open",
                    "draft": False,
                    "body": "Upgrade orders safely.",
                    "user": {"login": "developer"},
                    "base": {"ref": "master"},
                    "head": {"ref": "feature/migration", "sha": "abc123"},
                },
            )
        if request.url.path.endswith("/pulls/7/files"):
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "migrations/v2.sql",
                        "status": "added",
                        "additions": 20,
                        "deletions": 0,
                    }
                ],
            )
        if request.url.path.endswith("/commits/abc123/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "name": "tests",
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    async def inspect() -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            tool = GitHubPullRequestTool(client=client)
            return dict(
                await tool.execute(
                    GitHubPullRequestInput(
                        pull_request_url=(
                            "https://github.com/acme/orders/pull/7"
                        )
                    )
                )
            )

    result = asyncio.run(inspect())

    assert result["repository"] == "acme/orders"
    assert result["changed_files"][0]["filename"] == "migrations/v2.sql"
    assert result["checks"][0]["conclusion"] == "success"


def test_github_api_failure_is_sanitized() -> None:
    async def inspect() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(403)
            ),
        ) as client:
            tool = GitHubPullRequestTool(client=client)
            await tool.execute(
                GitHubPullRequestInput(
                    pull_request_url=(
                        "https://github.com/acme/orders/pull/7"
                    )
                )
            )

    with pytest.raises(ValueError, match="GitHub API request failed"):
        asyncio.run(inspect())
