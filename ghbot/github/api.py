"""Typed wrappers for the GitHub endpoints used by the bot."""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

from ghbot.github.client import GitHubClient, GitHubError, GitHubNotFound, Page

# Fields accepted by PATCH /user (verified against docs.github.com).
PROFILE_FIELDS = ("name", "email", "blog", "twitter_username", "company", "location", "hireable", "bio")


def _r(full_name: str) -> str:
    owner, _, name = full_name.partition("/")
    return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"


class GitHubAPI:
    def __init__(self, client: GitHubClient, username: str) -> None:
        self.client = client
        self.username = username

    # ---- account
    async def get_user(self) -> dict[str, Any]:
        return await self.client.get("/user")

    async def update_user(self, **fields: Any) -> dict[str, Any]:
        unknown = set(fields) - set(PROFILE_FIELDS)
        if unknown:
            raise ValueError(f"Unsupported profile fields: {unknown}")
        response = await self.client.request("PATCH", "/user", json=fields)
        return response.json()

    async def list_social_accounts(self) -> list[dict[str, Any]]:
        return await self.client.paginate("/user/social_accounts")

    async def add_social_accounts(self, urls: list[str]) -> None:
        await self.client.request("POST", "/user/social_accounts", json={"account_urls": urls})

    async def delete_social_accounts(self, urls: list[str]) -> None:
        await self.client.request("DELETE", "/user/social_accounts", json={"account_urls": urls})

    async def get_status(self) -> dict[str, Any] | None:
        data = await self.client.graphql(
            "query { viewer { status { message emoji indicatesLimitedAvailability expiresAt } } }", {}
        )
        return data["viewer"]["status"]

    async def set_status(self, message: str | None, emoji: str | None, busy: bool = False) -> None:
        await self.client.graphql(
            "mutation($input: ChangeUserStatusInput!) { changeUserStatus(input: $input) { status { message } } }",
            {"input": {"message": message, "emoji": emoji, "limitedAvailability": busy}},
        )

    async def rate_limit(self) -> dict[str, Any]:
        return await self.client.get("/rate_limit")

    # ---- repositories
    async def list_repos(self, page: int, per_page: int, *, sort: str = "pushed", public_only: bool = True) -> Page:
        return await self.client.page(
            "/user/repos", page=page, per_page=per_page, affiliation="owner", sort=sort, direction="desc",
            visibility="public" if public_only else "all",
        )

    async def all_repos(self, *, public_only: bool = True, max_items: int = 1000) -> list[dict[str, Any]]:
        return await self.client.paginate(
            "/user/repos", max_items=max_items, affiliation="owner", sort="pushed", direction="desc",
            visibility="public" if public_only else "all",
        )

    async def get_repo(self, full_name: str) -> dict[str, Any]:
        return await self.client.get(_r(full_name))

    async def repo_exists(self, full_name: str) -> bool:
        try:
            await self.get_repo(full_name)
            return True
        except GitHubNotFound:
            return False

    async def update_repo(self, full_name: str, **fields: Any) -> dict[str, Any]:
        response = await self.client.request("PATCH", _r(full_name), json=fields)
        return response.json()

    async def delete_repo(self, full_name: str) -> None:
        await self.client.request("DELETE", _r(full_name))

    async def create_repo(self, name: str, *, private: bool, description: str | None = None,
                          homepage: str | None = None, has_issues: bool = True, has_wiki: bool = True,
                          has_projects: bool = True) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": name, "private": private, "auto_init": False,
            "has_issues": has_issues, "has_wiki": has_wiki, "has_projects": has_projects,
        }
        if description:
            body["description"] = description[:350]
        if homepage:
            body["homepage"] = homepage
        response = await self.client.request("POST", "/user/repos", json=body)
        return response.json()

    async def replace_topics(self, full_name: str, topics: list[str]) -> None:
        await self.client.request("PUT", f"{_r(full_name)}/topics", json={"names": topics})

    async def list_branches(self, full_name: str, page: int = 1, per_page: int = 30, protected: bool | None = None) -> Page:
        params: dict[str, Any] = {}
        if protected is not None:
            params["protected"] = str(protected).lower()
        return await self.client.page(f"{_r(full_name)}/branches", page=page, per_page=per_page, **params)

    async def list_commits(self, full_name: str, page: int = 1, per_page: int = 10, sha: str | None = None,
                           since: str | None = None, until: str | None = None) -> Page:
        params = {k: v for k, v in {"sha": sha, "since": since, "until": until}.items() if v}
        return await self.client.page(f"{_r(full_name)}/commits", page=page, per_page=per_page, **params)

    async def get_commit(self, full_name: str, ref: str) -> dict[str, Any]:
        return await self.client.get(f"{_r(full_name)}/commits/{quote(ref, safe='')}")

    async def compare(self, full_name: str, base: str, head: str) -> dict[str, Any]:
        basehead = f"{quote(base, safe='')}...{quote(head, safe='')}"
        return await self.client.get(f"{_r(full_name)}/compare/{basehead}", per_page=10)

    async def count(self, path: str, **params: Any) -> int:
        """Total item count using the per_page=1 / last-page trick."""
        page = await self.client.page(path, page=1, per_page=1, **params)
        if not page.items:
            return 0
        return page.last_page or 1

    async def count_commits(self, full_name: str) -> int:
        return await self.count(f"{_r(full_name)}/commits")

    async def list_tags(self, full_name: str, page: int = 1, per_page: int = 15) -> Page:
        return await self.client.page(f"{_r(full_name)}/tags", page=page, per_page=per_page)

    async def list_releases(self, full_name: str, page: int = 1, per_page: int = 8) -> Page:
        return await self.client.page(f"{_r(full_name)}/releases", page=page, per_page=per_page)

    async def list_issues(self, full_name: str, page: int = 1, per_page: int = 10) -> Page:
        """Open issues only (the issues endpoint also returns pull requests; they are removed)."""
        result = await self.client.page(f"{_r(full_name)}/issues", page=page, per_page=per_page, state="open")
        result.items = [i for i in result.items if "pull_request" not in i]
        return result

    async def list_pulls(self, full_name: str, page: int = 1, per_page: int = 10) -> Page:
        return await self.client.page(f"{_r(full_name)}/pulls", page=page, per_page=per_page, state="open")

    async def languages(self, full_name: str) -> dict[str, int]:
        return await self.client.get(f"{_r(full_name)}/languages")

    async def list_contributors(self, full_name: str, page: int = 1, per_page: int = 15) -> Page:
        return await self.client.page(f"{_r(full_name)}/contributors", page=page, per_page=per_page)

    async def user_events(self, page: int = 1, per_page: int = 15) -> Page:
        return await self.client.page(f"/users/{quote(self.username, safe='')}/events", page=page, per_page=per_page)

    async def notifications(self, page: int = 1, per_page: int = 15) -> Page:
        return await self.client.page("/notifications", page=page, per_page=per_page)

    async def count_open_pulls(self, full_name: str) -> int:
        page = await self.client.page(f"{_r(full_name)}/pulls", page=1, per_page=1, state="open")
        if not page.items:
            return 0
        return page.last_page or 1

    # ---- contents
    async def get_file(self, full_name: str, path: str) -> tuple[str, str] | None:
        """Return (decoded_text, blob_sha) or None when the file does not exist."""
        try:
            data = await self.client.get(f"{_r(full_name)}/contents/{quote(path)}")
        except GitHubNotFound:
            return None
        return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]

    async def put_file_on_branch(self, full_name: str, path: str, content: str, message: str,
                                 sha: str | None, branch: str) -> dict[str, Any]:
        body: dict[str, Any] = {"message": message, "branch": branch,
                                "content": base64.b64encode(content.encode()).decode()}
        if sha:
            body["sha"] = sha
        response = await self.client.request("PUT", f"{_r(full_name)}/contents/{quote(path)}", json=body)
        return response.json()

    async def put_file(self, full_name: str, path: str, content: str, message: str, sha: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"message": message, "content": base64.b64encode(content.encode()).decode()}
        if sha:
            body["sha"] = sha  # optimistic concurrency: fails with 409 if README changed meanwhile
        response = await self.client.request("PUT", f"{_r(full_name)}/contents/{quote(path)}", json=body)
        return response.json()

    # ---- git refs, pull requests and checks
    async def get_branch(self, full_name: str, branch: str) -> dict[str, Any]:
        return await self.client.get(f"{_r(full_name)}/branches/{quote(branch, safe='')}")

    async def branch_protection(self, full_name: str, branch: str) -> dict[str, Any] | None:
        """Protection rules, or None when the branch is unprotected or not readable by this token."""
        try:
            return await self.client.get(f"{_r(full_name)}/branches/{quote(branch, safe='')}/protection")
        except GitHubError as exc:
            if exc.status in (403, 404):  # unprotected, or missing Administration:read
                return None
            raise

    async def create_ref(self, full_name: str, ref: str, sha: str) -> dict[str, Any]:
        response = await self.client.request("POST", f"{_r(full_name)}/git/refs", json={"ref": ref, "sha": sha})
        return response.json()

    async def delete_ref(self, full_name: str, ref: str) -> None:
        await self.client.request("DELETE", f"{_r(full_name)}/git/refs/{quote(ref, safe='/')}")

    async def create_pull(self, full_name: str, *, title: str, head: str, base: str, body: str,
                          draft: bool = False) -> dict[str, Any]:
        response = await self.client.request(
            "POST", f"{_r(full_name)}/pulls",
            json={"title": title, "head": head, "base": base, "body": body, "draft": draft},
        )
        return response.json()

    async def get_pull(self, full_name: str, number: int) -> dict[str, Any]:
        return await self.client.get(f"{_r(full_name)}/pulls/{int(number)}")

    async def list_reviews(self, full_name: str, number: int) -> list[dict[str, Any]]:
        return await self.client.paginate(f"{_r(full_name)}/pulls/{int(number)}/reviews", max_items=100)

    async def merge_pull(self, full_name: str, number: int, *, method: str, title: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"merge_method": method}
        if title:
            body["commit_title"] = title
        response = await self.client.request("PUT", f"{_r(full_name)}/pulls/{int(number)}/merge", json=body)
        return response.json()

    async def check_runs(self, full_name: str, ref: str) -> dict[str, Any]:
        """Check runs for a commit. Needs the fine-grained 'Checks: read' permission."""
        try:
            return await self.client.get(f"{_r(full_name)}/commits/{quote(ref, safe='')}/check-runs")
        except GitHubError as exc:
            if exc.status == 403:
                return {"total_count": 0, "check_runs": [], "forbidden": True}
            raise

    async def combined_status(self, full_name: str, ref: str) -> dict[str, Any]:
        """Legacy commit statuses. Needs the fine-grained 'Commit statuses: read' permission."""
        try:
            return await self.client.get(f"{_r(full_name)}/commits/{quote(ref, safe='')}/status")
        except GitHubError as exc:
            if exc.status == 403:
                return {"state": "unknown", "statuses": [], "forbidden": True}
            raise

    async def runs_for_sha(self, full_name: str, sha: str, per_page: int = 5) -> list[dict[str, Any]]:
        page = await self.client.page(f"{_r(full_name)}/actions/runs", page=1, per_page=per_page, head_sha=sha)
        return page.items

    async def run_jobs(self, full_name: str, run_id: int) -> list[dict[str, Any]]:
        return await self.client.paginate(f"{_r(full_name)}/actions/runs/{int(run_id)}/jobs", max_items=30)

    async def job_logs(self, full_name: str, job_id: int) -> str:
        """Plain-text log of one job (Actions: read). Empty string when logs are gone."""
        try:
            response = await self.client.request("GET", f"{_r(full_name)}/actions/jobs/{int(job_id)}/logs")
        except GitHubError:
            return ""
        return response.text

    async def check_run_annotations(self, full_name: str, check_run_id: int) -> list[dict[str, Any]]:
        """File/line annotations of a check run (Checks: read)."""
        try:
            return await self.client.paginate(f"{_r(full_name)}/check-runs/{int(check_run_id)}/annotations",
                                              max_items=50)
        except GitHubError:
            return []

    async def search_count(self, query: str) -> int:
        data = await self.client.get("/search/issues", q=query, per_page=1, advanced_search="true")
        return int(data.get("total_count", 0))

    # ---- actions
    async def list_workflows(self, full_name: str) -> list[dict[str, Any]]:
        return await self.client.paginate(f"{_r(full_name)}/actions/workflows", max_items=100)

    async def list_runs(self, full_name: str, page: int = 1, per_page: int = 10, status: str | None = None) -> Page:
        params = {"status": status} if status else {}
        return await self.client.page(f"{_r(full_name)}/actions/runs", page=page, per_page=per_page, **params)

    async def get_run(self, full_name: str, run_id: int) -> dict[str, Any]:
        return await self.client.get(f"{_r(full_name)}/actions/runs/{int(run_id)}")

    async def rerun(self, full_name: str, run_id: int) -> None:
        await self.client.request("POST", f"{_r(full_name)}/actions/runs/{int(run_id)}/rerun")

    async def rerun_failed(self, full_name: str, run_id: int) -> None:
        await self.client.request("POST", f"{_r(full_name)}/actions/runs/{int(run_id)}/rerun-failed-jobs")

    async def cancel_run(self, full_name: str, run_id: int) -> None:
        await self.client.request("POST", f"{_r(full_name)}/actions/runs/{int(run_id)}/cancel")
