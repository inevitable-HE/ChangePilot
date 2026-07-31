from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, field_validator

from changepilot.planning.ports.models import ChatToolDefinition


class GitHubPullRequestInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pull_request_url: str

    @field_validator("pull_request_url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        parse_github_pull_request_url(value)
        return value


class GitHubPullRequestTool:
    input_model = GitHubPullRequestInput
    definition = ChatToolDefinition(
        name="github.inspect_pull_request",
        description=(
            "Read GitHub pull request metadata, changed files, and CI checks. "
            "Use this when a change request contains a GitHub pull request URL."
        ),
        input_schema=GitHubPullRequestInput.model_json_schema(),
    )

    def __init__(
        self,
        *,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 15.0,
        max_files: int = 50,
        allowed_pull_request_url: str | None = None,
    ) -> None:
        self._token = token or os.getenv("CHANGEPILOT_GITHUB_TOKEN")
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._max_files = max_files
        self._allowed_pull_request = (
            None
            if allowed_pull_request_url is None
            else parse_github_pull_request_url(allowed_pull_request_url)
        )

    async def execute(
        self,
        arguments: BaseModel,
    ) -> Mapping[str, Any]:
        if not isinstance(arguments, GitHubPullRequestInput):
            raise ValueError("invalid GitHub pull request tool input")
        owner, repository, number = parse_github_pull_request_url(
            arguments.pull_request_url
        )
        if (
            self._allowed_pull_request is not None
            and (owner, repository, number) != self._allowed_pull_request
        ):
            raise ValueError(
                "tool call URL does not match the submitted pull request"
            )
        try:
            if self._client is not None:
                return await self._inspect(
                    self._client,
                    owner=owner,
                    repository=repository,
                    number=number,
                )
            async with httpx.AsyncClient(
                base_url="https://api.github.com",
                headers=self._headers(),
                timeout=self._timeout_seconds,
                follow_redirects=False,
            ) as client:
                return await self._inspect(
                    client,
                    owner=owner,
                    repository=repository,
                    number=number,
                )
        except httpx.HTTPError as exc:
            raise ValueError("GitHub API request failed") from exc

    async def _inspect(
        self,
        client: httpx.AsyncClient,
        *,
        owner: str,
        repository: str,
        number: int,
    ) -> Mapping[str, Any]:
        prefix = f"/repos/{owner}/{repository}"
        pull_response = await client.get(f"{prefix}/pulls/{number}")
        pull_response.raise_for_status()
        pull = pull_response.json()

        files_response = await client.get(
            f"{prefix}/pulls/{number}/files",
            params={"per_page": min(100, self._max_files)},
        )
        files_response.raise_for_status()
        files = files_response.json()

        head_sha = str(pull.get("head", {}).get("sha", ""))
        checks: list[dict[str, Any]] = []
        if head_sha:
            checks_response = await client.get(
                f"{prefix}/commits/{head_sha}/check-runs",
                params={"per_page": 100},
                headers={
                    **self._headers(),
                    "Accept": "application/vnd.github+json",
                },
            )
            checks_response.raise_for_status()
            checks = [
                {
                    "name": str(item.get("name", "")),
                    "status": str(item.get("status", "")),
                    "conclusion": item.get("conclusion"),
                }
                for item in checks_response.json().get("check_runs", ())
            ]

        return {
            "repository": f"{owner}/{repository}",
            "number": number,
            "url": str(pull.get("html_url", "")),
            "title": str(pull.get("title", "")),
            "state": str(pull.get("state", "")),
            "draft": bool(pull.get("draft", False)),
            "author": str(pull.get("user", {}).get("login", "")),
            "base_ref": str(pull.get("base", {}).get("ref", "")),
            "head_ref": str(pull.get("head", {}).get("ref", "")),
            "head_sha": head_sha,
            "body_excerpt": " ".join(
                str(pull.get("body") or "").split()
            )[:1_000],
            "changed_files": [
                {
                    "filename": str(item.get("filename", "")),
                    "status": str(item.get("status", "")),
                    "additions": int(item.get("additions", 0)),
                    "deletions": int(item.get("deletions", 0)),
                }
                for item in files[: self._max_files]
            ],
            "files_truncated": len(files) >= self._max_files,
            "checks": checks,
        }

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ChangePilot",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers


def parse_github_pull_request_url(value: str) -> tuple[str, str, int]:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise ValueError("pull request URL must use https://github.com")
    parts = tuple(part for part in parsed.path.split("/") if part)
    if len(parts) != 4 or parts[2] != "pull":
        raise ValueError(
            "pull request URL must match "
            "https://github.com/{owner}/{repository}/pull/{number}"
        )
    owner, repository, _, raw_number = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository:
        raise ValueError("pull request owner and repository are required")
    try:
        number = int(raw_number)
    except ValueError as exc:
        raise ValueError("pull request number must be an integer") from exc
    if number <= 0:
        raise ValueError("pull request number must be positive")
    return owner, repository, number
