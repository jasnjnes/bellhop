from __future__ import annotations

import base64
import binascii
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings
from app.errors import ConflictError, GitHubAPIError, NotFoundError, ValidationError
from app.models import FileChange


def normalize_path(value: str) -> str:
    value = value.replace("\\", "/").strip()
    path = PurePosixPath(value)
    if not value or path.is_absolute():
        raise ValidationError("Repository paths must be non-empty and relative.")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValidationError("Repository paths cannot contain empty, dot, or parent segments.")
    if path.parts[0] == ".git":
        raise ValidationError("The .git directory cannot be modified.")
    return str(path)


def _is_empty_repository_error(exc: GitHubAPIError) -> bool:
    """GitHub signals a commit-less repository as 409 'Git Repository is empty.'"""
    return exc.github_status == 409 and "empty" in exc.message.lower()


class GitHubClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._transport = transport
        self._login: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.settings.github_token}",
            "X-GitHub-Api-Version": self.settings.github_api_version,
            "User-Agent": "bellhop",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        accept_statuses: set[int] | None = None,
    ) -> Any:
        url = f"{self.settings.github_api_url}{path}"
        async with httpx.AsyncClient(
            headers=self.headers,
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=self._transport,
        ) as client:
            response = await client.request(method, url, params=params, json=json)

        valid = accept_statuses or {200, 201, 202, 204}
        if response.status_code not in valid:
            try:
                details = response.json()
            except ValueError:
                details = {"body": response.text[:2000]}
            message = details.get("message", "GitHub API request failed.") if isinstance(details, dict) else "GitHub API request failed."
            raise GitHubAPIError(
                message,
                github_status=response.status_code,
                details={"path": path, "response": details},
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def authenticated_user(self) -> dict[str, Any]:
        user = await self._request("GET", "/user")
        self._login = user["login"]
        return {
            "login": user["login"],
            "name": user.get("name"),
            "html_url": user["html_url"],
        }

    async def default_owner(self) -> str:
        if self.settings.github_default_owner:
            return self.settings.github_default_owner
        if not self._login:
            await self.authenticated_user()
        assert self._login
        return self._login

    async def repository(self, owner: str, repo: str) -> dict[str, Any]:
        data = await self._request("GET", f"/repos/{owner}/{repo}")
        return {
            "owner": data["owner"]["login"],
            "name": data["name"],
            "full_name": data["full_name"],
            "private": data["private"],
            "description": data.get("description"),
            "default_branch": data["default_branch"],
            "html_url": data["html_url"],
            "updated_at": data["updated_at"],
            "permissions": data.get("permissions"),
        }

    async def list_repositories(
        self,
        *,
        visibility: str = "all",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        data = await self._request(
            "GET",
            "/user/repos",
            params={
                "visibility": visibility,
                "affiliation": "owner,collaborator,organization_member",
                "sort": "updated",
                "direction": "desc",
                "per_page": limit,
            },
        )
        return [
            {
                "full_name": item["full_name"],
                "private": item["private"],
                "description": item.get("description"),
                "default_branch": item["default_branch"],
                "html_url": item["html_url"],
                "updated_at": item["updated_at"],
                "permissions": item.get("permissions"),
            }
            for item in data
        ]

    async def create_repository(
        self,
        *,
        name: str,
        description: str = "",
        private: bool = True,
        owner: str | None = None,
        auto_init: bool = False,
        has_issues: bool = True,
        has_projects: bool = True,
        has_wiki: bool = False,
    ) -> dict[str, Any]:
        target_owner = owner or await self.default_owner()
        current_user = (await self.authenticated_user())["login"]
        endpoint = "/user/repos" if target_owner.casefold() == current_user.casefold() else f"/orgs/{target_owner}/repos"
        data = await self._request(
            "POST",
            endpoint,
            json={
                "name": name,
                "description": description,
                "private": private,
                "auto_init": auto_init,
                "has_issues": has_issues,
                "has_projects": has_projects,
                "has_wiki": has_wiki,
            },
        )
        return {
            "owner": data["owner"]["login"],
            "name": data["name"],
            "full_name": data["full_name"],
            "private": data["private"],
            "default_branch": data["default_branch"],
            "html_url": data["html_url"],
            "clone_url": data["clone_url"],
            "initial_commit_created": auto_init,
            "next_step": (
                "Call initialize_repository_files to create the first branch and project-defined files."
                if not auto_init
                else "Repository is ready for normal commits."
            ),
        }

    async def initialize_repository(
        self,
        owner: str,
        repo: str,
        *,
        branch: str,
        message: str,
        files: list[FileChange],
        binary_limit_bytes: int | None = None,
    ) -> dict[str, Any]:
        if not files:
            raise ValidationError("At least one initial file is required.")
        if len(files) > self.settings.max_commit_files:
            raise ValidationError(
                f"An initial commit can contain at most {self.settings.max_commit_files} files."
            )

        tree_entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        normalized: list[FileChange] = []

        for change in files:
            if change.delete:
                raise ValidationError("The initial commit cannot contain deletions.")
            safe = normalize_path(change.path)
            if safe in seen:
                raise ValidationError(f"Duplicate path in initial commit: {safe}")
            seen.add(safe)
            normalized.append(change.model_copy(update={"path": safe}))

        for change in normalized:
            self._validated_base64(change, binary_limit_bytes)

        try:
            return await self._initialize_via_git_data(
                owner, repo, branch=branch, message=message, files=normalized
            )
        except GitHubAPIError as exc:
            if not _is_empty_repository_error(exc):
                raise

        # A repository with no commits rejects every Git Data endpoint with
        # 409 "Git Repository is empty." The Contents API is the only write
        # path GitHub accepts before a first commit exists, so bootstrap
        # through it and let the Git Data path handle any remaining files.
        return await self._initialize_via_contents_api(
            owner, repo, branch=branch, message=message, files=normalized
        )

    def _validated_base64(self, change: FileChange, limit: int | None = None) -> bytes | None:
        if change.content_base64 is None:
            return None
        try:
            raw = base64.b64decode(change.content_base64, validate=True)
        except binascii.Error as exc:
            raise ValidationError(f"Invalid base64 content for {change.path}.") from exc
        # The default limit caps binary that an agent inlines through the model's
        # context. Upload tickets stream bytes straight to the gateway and never
        # put them in context, so that path passes its own, larger limit.
        effective = self.settings.max_binary_input_bytes if limit is None else limit
        if len(raw) > effective:
            raise ValidationError(f"Binary content for {change.path} exceeds {effective} bytes.")
        return raw

    async def _initialize_via_git_data(
        self,
        owner: str,
        repo: str,
        *,
        branch: str,
        message: str,
        files: list[FileChange],
    ) -> dict[str, Any]:
        tree_entries: list[dict[str, Any]] = []
        for change in files:
            if change.content_base64 is not None:
                blob_payload = {"content": change.content_base64, "encoding": "base64"}
            else:
                assert change.content is not None
                blob_payload = {"content": change.content, "encoding": "utf-8"}

            blob = await self._request(
                "POST",
                f"/repos/{owner}/{repo}/git/blobs",
                json=blob_payload,
            )
            tree_entries.append(
                {
                    "path": change.path,
                    "mode": change.mode,
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )

        tree = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/git/trees",
            json={"tree": tree_entries},
        )
        commit = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/git/commits",
            json={
                "message": message,
                "tree": tree["sha"],
                "parents": [],
            },
        )
        await self._request(
            "POST",
            f"/repos/{owner}/{repo}/git/refs",
            json={
                "ref": f"refs/heads/{branch}",
                "sha": commit["sha"],
            },
        )
        return {
            "owner": owner,
            "repository": repo,
            "branch": branch,
            "commit_sha": commit["sha"],
            "commit_url": f"https://github.com/{owner}/{repo}/commit/{commit['sha']}",
            "changed_paths": [change.path for change in files],
        }

    async def _initialize_via_contents_api(
        self,
        owner: str,
        repo: str,
        *,
        branch: str,
        message: str,
        files: list[FileChange],
    ) -> dict[str, Any]:
        seed, remaining = files[0], files[1:]
        if seed.content_base64 is not None:
            seed_content = seed.content_base64
        else:
            assert seed.content is not None
            seed_content = base64.b64encode(seed.content.encode()).decode()

        created = await self._request(
            "PUT",
            f"/repos/{owner}/{repo}/contents/{quote(seed.path, safe='/')}",
            json={
                "message": message,
                "content": seed_content,
                "branch": branch,
            },
        )
        commit_sha = created["commit"]["sha"]

        # The Contents API writes one file per commit, so anything beyond the
        # seed goes in a second atomic commit now that a head exists.
        if remaining:
            follow_up = await self.commit_files(
                owner,
                repo,
                branch=branch,
                message=message,
                changes=remaining,
                expected_head=commit_sha,
            )
            commit_sha = follow_up["commit_sha"]

        return {
            "owner": owner,
            "repository": repo,
            "branch": branch,
            "commit_sha": commit_sha,
            "commit_url": f"https://github.com/{owner}/{repo}/commit/{commit_sha}",
            "changed_paths": [change.path for change in files],
            "bootstrapped_via_contents_api": True,
        }

    async def resolve_commit(
        self,
        owner: str,
        repo: str,
        ref: str | None = None,
    ) -> dict[str, str]:
        if not ref:
            ref = (await self.repository(owner, repo))["default_branch"]
        data = await self._request("GET", f"/repos/{owner}/{repo}/commits/{quote(ref, safe='')}")
        return {
            "ref": ref,
            "commit_sha": data["sha"],
            "tree_sha": data["commit"]["tree"]["sha"],
            "html_url": data["html_url"],
        }

    async def _changed_paths_between(self, owner: str, repo: str, base: str, head: str) -> set[str]:
        """Return the set of file paths that differ between two commits.

        Used to decide whether a stale-head commit can be safely rebased: if the
        incoming changes do not touch any path that moved on the branch since the
        caller last read it, the edits are independent and can be re-applied.
        """
        data = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/compare/{quote(base, safe='')}...{quote(head, safe='')}",
        )
        return {entry["filename"] for entry in data.get("files", [])}

    async def branch_head(self, owner: str, repo: str, branch: str | None = None) -> dict[str, str]:
        if not branch:
            branch = (await self.repository(owner, repo))["default_branch"]
        data = await self._request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{quote(branch, safe='/')}")
        return {
            "branch": branch,
            "commit_sha": data["object"]["sha"],
        }

    async def list_tree(
        self,
        owner: str,
        repo: str,
        *,
        ref: str | None = None,
        prefix: str | None = None,
        include_directories: bool = False,
    ) -> dict[str, Any]:
        resolved = await self.resolve_commit(owner, repo, ref)
        data = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/git/trees/{resolved['tree_sha']}",
            params={"recursive": "1"},
        )
        safe_prefix = normalize_path(prefix) if prefix else ""
        entries = []
        for entry in data.get("tree", []):
            path = entry["path"]
            if safe_prefix and not (path == safe_prefix or path.startswith(f"{safe_prefix}/")):
                continue
            if not include_directories and entry["type"] != "blob":
                continue
            entries.append(
                {
                    "path": path,
                    "type": entry["type"],
                    "sha": entry["sha"],
                    "size": entry.get("size"),
                }
            )
        return {
            "owner": owner,
            "repository": repo,
            "ref": resolved["ref"],
            "commit_sha": resolved["commit_sha"],
            "truncated": data.get("truncated", False),
            "entries": entries,
        }

    async def _blob_for_path(
        self,
        owner: str,
        repo: str,
        *,
        path: str,
        ref: str | None = None,
    ) -> dict[str, Any]:
        safe_path = normalize_path(path)
        tree = await self.list_tree(owner, repo, ref=ref)
        match = next((item for item in tree["entries"] if item["path"] == safe_path), None)
        if not match:
            raise NotFoundError(f"'{safe_path}' does not exist in {owner}/{repo}.")
        blob = await self._request("GET", f"/repos/{owner}/{repo}/git/blobs/{match['sha']}")
        try:
            raw = base64.b64decode(blob["content"].replace("\n", ""), validate=True)
        except (binascii.Error, KeyError) as exc:
            raise GitHubAPIError("GitHub returned an invalid blob response.") from exc
        return {
            "path": safe_path,
            "blob_sha": match["sha"],
            "size": len(raw),
            "bytes": raw,
            "commit_sha": tree["commit_sha"],
            "ref": tree["ref"],
        }

    async def read_file(
        self,
        owner: str,
        repo: str,
        *,
        path: str,
        ref: str | None = None,
        include_binary_content: bool = False,
    ) -> dict[str, Any]:
        blob = await self._blob_for_path(owner, repo, path=path, ref=ref)
        raw = blob.pop("bytes")
        result = {
            "owner": owner,
            "repository": repo,
            **blob,
            "html_url": f"https://github.com/{owner}/{repo}/blob/{blob['commit_sha']}/{blob['path']}",
        }
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {
                **result,
                "is_binary": True,
                "content": None,
                "content_base64": (
                    base64.b64encode(raw).decode("ascii")
                    if include_binary_content and len(raw) <= self.settings.max_binary_input_bytes
                    else None
                ),
            }

        if len(raw) > self.settings.max_text_result_bytes:
            return {
                **result,
                "is_binary": False,
                "content": None,
                "content_truncated": True,
                "message": "The text file is too large for an MCP tool result. Read a smaller file or use GitHub directly.",
            }
        return {
            **result,
            "is_binary": False,
            "content": text,
            "content_truncated": False,
        }

    async def commit_files(
        self,
        owner: str,
        repo: str,
        *,
        branch: str,
        message: str,
        changes: list[FileChange],
        expected_head: str | None,
        auto_rebase: bool = True,
        binary_limit_bytes: int | None = None,
    ) -> dict[str, Any]:
        if not changes:
            raise ValidationError("At least one file change is required.")
        if len(changes) > self.settings.max_commit_files:
            raise ValidationError(
                f"A commit can contain at most {self.settings.max_commit_files} files."
            )

        normalized: list[FileChange] = []
        seen: set[str] = set()
        for change in changes:
            safe = normalize_path(change.path)
            if safe in seen:
                raise ValidationError(f"Duplicate path in commit: {safe}")
            seen.add(safe)
            normalized.append(change.model_copy(update={"path": safe}))

        head = await self.branch_head(owner, repo, branch)
        current_head = head["commit_sha"]
        if self.settings.require_expected_head and not expected_head:
            raise ConflictError(
                "expected_head is required. Read the repository or branch head before committing.",
                details={"current_head": current_head, "branch": branch},
            )

        rebased_onto_current_head = False
        if expected_head and expected_head != current_head:
            # The branch moved since the caller last read it. If none of the
            # incoming paths overlap what changed on the branch, the edits are
            # independent and we re-apply them on top of the new head. If they
            # do overlap, this is a real conflict and we refuse rather than
            # silently clobber the newer work.
            if not auto_rebase:
                raise ConflictError(
                    "The branch changed after the agent last read it.",
                    details={
                        "expected_head": expected_head,
                        "current_head": current_head,
                        "branch": branch,
                    },
                )
            changed_on_branch = await self._changed_paths_between(
                owner, repo, expected_head, current_head
            )
            incoming_paths = {change.path for change in normalized}
            conflicting = sorted(incoming_paths & changed_on_branch)
            if conflicting:
                raise ConflictError(
                    "The branch changed and your edits touch the same files.",
                    details={
                        "expected_head": expected_head,
                        "current_head": current_head,
                        "branch": branch,
                        "conflicting_paths": conflicting,
                        "hint": (
                            "Re-read these files at current_head, reapply your intent, "
                            "then commit again with expected_head set to current_head."
                        ),
                    },
                )
            rebased_onto_current_head = True

        parent = await self._request("GET", f"/repos/{owner}/{repo}/git/commits/{current_head}")
        base_tree_sha = parent["tree"]["sha"]
        tree_entries: list[dict[str, Any]] = []

        for change in normalized:
            if change.delete:
                tree_entries.append(
                    {
                        "path": change.path,
                        "mode": change.mode,
                        "type": "blob",
                        "sha": None,
                    }
                )
                continue

            if change.content_base64 is not None:
                self._validated_base64(change, binary_limit_bytes)
                blob_payload = {
                    "content": change.content_base64,
                    "encoding": "base64",
                }
            else:
                assert change.content is not None
                blob_payload = {
                    "content": change.content,
                    "encoding": "utf-8",
                }

            blob = await self._request(
                "POST",
                f"/repos/{owner}/{repo}/git/blobs",
                json=blob_payload,
            )
            tree_entries.append(
                {
                    "path": change.path,
                    "mode": change.mode,
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )

        tree = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/git/trees",
            json={
                "base_tree": base_tree_sha,
                "tree": tree_entries,
            },
        )
        commit = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/git/commits",
            json={
                "message": message,
                "tree": tree["sha"],
                "parents": [current_head],
            },
        )
        try:
            await self._request(
                "PATCH",
                f"/repos/{owner}/{repo}/git/refs/heads/{quote(branch, safe='/')}",
                json={
                    "sha": commit["sha"],
                    "force": False,
                },
            )
        except GitHubAPIError as exc:
            if exc.github_status in {409, 422}:
                raise ConflictError(
                    "GitHub rejected the branch update because the branch moved or a rule blocked it.",
                    details={
                        "previous_head": current_head,
                        "proposed_commit": commit["sha"],
                        "github": exc.details,
                    },
                ) from exc
            raise

        return {
            "owner": owner,
            "repository": repo,
            "branch": branch,
            "previous_head": current_head,
            "commit_sha": commit["sha"],
            "commit_url": f"https://github.com/{owner}/{repo}/commit/{commit['sha']}",
            "changed_paths": [change.path for change in normalized],
            "rebased_onto_current_head": rebased_onto_current_head,
            "expected_head": expected_head,
        }

    async def commit_upload(
        self,
        owner: str,
        repo: str,
        *,
        branch: str,
        message: str,
        path: str,
        content_base64: str,
        max_bytes: int,
    ) -> dict[str, Any]:
        """Commit one uploaded file, initializing the branch if it has no commits yet.

        Upload tickets are commonly redeemed against a repository that was just
        created with auto_init=false, so it has no commits and the target branch
        ref does not exist. A plain commit needs an existing head and would fail
        with 404; here we detect that and create the branch's first commit
        instead, so the very first document upload after create_repository lands
        instead of getting stuck.
        """
        change = FileChange(path=path, content_base64=content_base64)

        try:
            head = await self.branch_head(owner, repo, branch)
        except GitHubAPIError as exc:
            if exc.github_status != 404:
                raise
            head = None

        if head is None:
            return await self.initialize_repository(
                owner,
                repo,
                branch=branch,
                message=message,
                files=[change],
                binary_limit_bytes=max_bytes,
            )

        return await self.commit_files(
            owner,
            repo,
            branch=branch,
            message=message,
            changes=[change],
            expected_head=head["commit_sha"],
            binary_limit_bytes=max_bytes,
        )

    async def move_file(
        self,
        owner: str,
        repo: str,
        *,
        branch: str,
        source_path: str,
        destination_path: str,
        message: str,
        expected_head: str,
    ) -> dict[str, Any]:
        source = normalize_path(source_path)
        destination = normalize_path(destination_path)
        blob = await self._blob_for_path(owner, repo, path=source, ref=branch)
        encoded = base64.b64encode(blob["bytes"]).decode("ascii")
        return await self.commit_files(
            owner,
            repo,
            branch=branch,
            message=message,
            expected_head=expected_head,
            changes=[
                FileChange(path=destination, content_base64=encoded),
                FileChange(path=source, delete=True),
            ],
        )

    async def create_branch(
        self,
        owner: str,
        repo: str,
        *,
        branch: str,
        from_ref: str | None = None,
    ) -> dict[str, Any]:
        resolved = await self.resolve_commit(owner, repo, from_ref)
        await self._request(
            "POST",
            f"/repos/{owner}/{repo}/git/refs",
            json={
                "ref": f"refs/heads/{branch}",
                "sha": resolved["commit_sha"],
            },
        )
        return {
            "owner": owner,
            "repository": repo,
            "branch": branch,
            "commit_sha": resolved["commit_sha"],
            "html_url": f"https://github.com/{owner}/{repo}/tree/{branch}",
        }

    async def history(
        self,
        owner: str,
        repo: str,
        *,
        ref: str | None = None,
        path: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"per_page": max(1, min(limit, 100))}
        if ref:
            params["sha"] = ref
        if path:
            params["path"] = normalize_path(path)
        commits = await self._request("GET", f"/repos/{owner}/{repo}/commits", params=params)
        return {
            "owner": owner,
            "repository": repo,
            "ref": ref,
            "path": path,
            "commits": [
                {
                    "sha": item["sha"],
                    "message": item["commit"]["message"],
                    "author": item["commit"]["author"],
                    "html_url": item["html_url"],
                }
                for item in commits
            ],
        }

    async def compare(
        self,
        owner: str,
        repo: str,
        *,
        base: str,
        head: str,
    ) -> dict[str, Any]:
        data = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/compare/{quote(base, safe='')}...{quote(head, safe='')}",
        )
        return {
            "owner": owner,
            "repository": repo,
            "status": data["status"],
            "ahead_by": data["ahead_by"],
            "behind_by": data["behind_by"],
            "total_commits": data["total_commits"],
            "commits": [
                {
                    "sha": item["sha"],
                    "message": item["commit"]["message"],
                    "html_url": item["html_url"],
                }
                for item in data.get("commits", [])
            ],
            "files": [
                {
                    "filename": item["filename"],
                    "status": item["status"],
                    "additions": item["additions"],
                    "deletions": item["deletions"],
                    "changes": item["changes"],
                    "patch": item.get("patch"),
                }
                for item in data.get("files", [])
            ],
            "html_url": data["html_url"],
        }

    async def create_release(
        self,
        owner: str,
        repo: str,
        *,
        tag_name: str,
        target: str,
        name: str | None = None,
        body: str = "",
        draft: bool = False,
        prerelease: bool = False,
    ) -> dict[str, Any]:
        data = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/releases",
            json={
                "tag_name": tag_name,
                "target_commitish": target,
                "name": name or tag_name,
                "body": body,
                "draft": draft,
                "prerelease": prerelease,
                "generate_release_notes": not bool(body),
            },
        )
        return {
            "owner": owner,
            "repository": repo,
            "tag_name": data["tag_name"],
            "name": data["name"],
            "draft": data["draft"],
            "prerelease": data["prerelease"],
            "html_url": data["html_url"],
        }

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        head: str,
        base: str,
        body: str = "",
        draft: bool = False,
    ) -> dict[str, Any]:
        data = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={
                "title": title,
                "head": head,
                "base": base,
                "body": body,
                "draft": draft,
            },
        )
        return {
            "number": data["number"],
            "title": data["title"],
            "state": data["state"],
            "draft": data["draft"],
            "html_url": data["html_url"],
        }

    async def search_code(
        self,
        owner: str,
        repo: str,
        *,
        query: str,
        path: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        qualifiers = [query, f"repo:{owner}/{repo}"]
        if path:
            qualifiers.append(f"path:{normalize_path(path)}")
        data = await self._request(
            "GET",
            "/search/code",
            params={
                "q": " ".join(qualifiers),
                "per_page": max(1, min(limit, 100)),
            },
        )
        return {
            "owner": owner,
            "repository": repo,
            "query": query,
            "total_count": data["total_count"],
            "items": [
                {
                    "name": item["name"],
                    "path": item["path"],
                    "sha": item["sha"],
                    "html_url": item["html_url"],
                }
                for item in data.get("items", [])
            ],
        }
