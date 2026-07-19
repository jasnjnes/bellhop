from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.config import get_settings
from app.dependencies import get_github
from app.models import FileChange


_settings = get_settings()

mcp = FastMCP(
    "Bellhop",
    instructions=(
        "GitHub is the only source of truth. Never impose a standard repository layout. "
        "Use whatever paths and structure the user and agent decide for that specific project. "
        "Create private repositories by default. Before modifying an existing branch, read its "
        "current commit SHA and pass it as expected_head. Group related file changes into one "
        "atomic commit. If the branch moved since you read it but your edits touch different "
        "files, the commit is re-applied automatically onto the new head (the result reports "
        "rebased_onto_current_head=true) — mention this to the user. If your edits touch the "
        "same files that changed, the commit is refused with conflicting_paths; re-read those "
        "files at current_head and try again. Never manufacture v1/v2 folders for versioning; "
        "Git commits, branches, tags, releases, and pull requests are the version system."
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_settings.mcp_allowed_hosts,
        allowed_origins=_settings.mcp_allowed_origins,
    ),
)


@mcp.tool()
async def github_connection_status() -> dict[str, Any]:
    """Verify the Render gateway's GitHub credential and show the authenticated account."""
    return await get_github().authenticated_user()


@mcp.tool()
async def list_repositories(
    visibility: str = "all",
    limit: int = 50,
) -> dict[str, Any]:
    """List repositories visible to the gateway, newest activity first."""
    return {"repositories": await get_github().list_repositories(visibility=visibility, limit=limit)}


@mcp.tool()
async def create_repository(
    name: str,
    description: str = "",
    private: bool = True,
    owner: str | None = None,
    auto_init: bool = False,
) -> dict[str, Any]:
    """
    Create a GitHub repository without imposing any folder structure.

    The default leaves the repository empty so the gateway does not force even a README.
    Call initialize_repository_files next to create the first branch and exactly the
    structure the user and agent chose. Set auto_init=true only when a placeholder README
    is explicitly acceptable.
    """
    return await get_github().create_repository(
        name=name,
        description=description,
        private=private,
        owner=owner,
        auto_init=auto_init,
    )


@mcp.tool()
async def initialize_repository_files(
    owner: str,
    repository: str,
    branch: str,
    message: str,
    files: list[FileChange],
) -> dict[str, Any]:
    """
    Create the first commit and branch in an empty repository.

    The paths are completely project-defined. Use this immediately after
    create_repository when auto_init=false.
    """
    return await get_github().initialize_repository(
        owner,
        repository,
        branch=branch,
        message=message,
        files=files,
    )


@mcp.tool()
async def get_repository(owner: str, repository: str) -> dict[str, Any]:
    """Get repository metadata, permissions, default branch, and current ownership."""
    return await get_github().repository(owner, repository)


@mcp.tool()
async def get_branch_head(
    owner: str,
    repository: str,
    branch: str | None = None,
) -> dict[str, Any]:
    """Return the current commit SHA for a branch. Use this SHA as expected_head when writing."""
    return await get_github().branch_head(owner, repository, branch)


@mcp.tool()
async def list_repository_files(
    owner: str,
    repository: str,
    ref: str | None = None,
    prefix: str | None = None,
    include_directories: bool = False,
) -> dict[str, Any]:
    """List the repository tree at any branch, tag, or commit, optionally under an arbitrary prefix."""
    return await get_github().list_tree(
        owner,
        repository,
        ref=ref,
        prefix=prefix,
        include_directories=include_directories,
    )


@mcp.tool()
async def read_repository_file(
    owner: str,
    repository: str,
    path: str,
    ref: str | None = None,
    include_binary_content: bool = False,
) -> dict[str, Any]:
    """Read a file and return its content, blob SHA, and exact repository commit SHA."""
    return await get_github().read_file(
        owner,
        repository,
        path=path,
        ref=ref,
        include_binary_content=include_binary_content,
    )


@mcp.tool()
async def commit_repository_files(
    owner: str,
    repository: str,
    branch: str,
    message: str,
    changes: list[FileChange],
    expected_head: str | None,
    auto_rebase: bool = True,
) -> dict[str, Any]:
    """
    Create, update, and delete arbitrary paths in one atomic Git commit.

    Each change uses exactly one of:
    - content: UTF-8 text
    - content_base64: a small binary file
    - delete: true

    expected_head must be the current branch SHA returned by get_branch_head or a
    prior write.

    Concurrency: if the branch moved since expected_head but your changes touch
    different files, the commit is safely re-applied onto the new head and the
    result reports rebased_onto_current_head=true. If your changes touch the same
    files that moved, the commit is refused with conflicting_paths so you can
    re-read and retry. Set auto_rebase=false to require an exact expected_head
    match and reject any movement (stricter, more manual).
    """
    return await get_github().commit_files(
        owner,
        repository,
        branch=branch,
        message=message,
        changes=changes,
        expected_head=expected_head,
        auto_rebase=auto_rebase,
    )


@mcp.tool()
async def move_repository_file(
    owner: str,
    repository: str,
    branch: str,
    source_path: str,
    destination_path: str,
    message: str,
    expected_head: str,
) -> dict[str, Any]:
    """Move or rename one file in a single commit while retaining Git history."""
    return await get_github().move_file(
        owner,
        repository,
        branch=branch,
        source_path=source_path,
        destination_path=destination_path,
        message=message,
        expected_head=expected_head,
    )


@mcp.tool()
async def delete_repository_files(
    owner: str,
    repository: str,
    branch: str,
    paths: list[str],
    message: str,
    expected_head: str,
) -> dict[str, Any]:
    """Delete one or more files in a single recoverable Git commit. This never deletes the repository."""
    changes = [FileChange(path=path, delete=True) for path in paths]
    return await get_github().commit_files(
        owner,
        repository,
        branch=branch,
        message=message,
        changes=changes,
        expected_head=expected_head,
    )


@mcp.tool()
async def create_repository_branch(
    owner: str,
    repository: str,
    branch: str,
    from_ref: str | None = None,
) -> dict[str, Any]:
    """Create a branch from a branch, tag, or commit. Defaults to the repository's default branch."""
    return await get_github().create_branch(
        owner,
        repository,
        branch=branch,
        from_ref=from_ref,
    )


@mcp.tool()
async def repository_history(
    owner: str,
    repository: str,
    ref: str | None = None,
    path: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Return commit history for a repository, branch, or one arbitrary path."""
    return await get_github().history(
        owner,
        repository,
        ref=ref,
        path=path,
        limit=limit,
    )


@mcp.tool()
async def compare_repository_versions(
    owner: str,
    repository: str,
    base: str,
    head: str,
) -> dict[str, Any]:
    """Compare branches, tags, or commit SHAs and return commits and file patches."""
    return await get_github().compare(
        owner,
        repository,
        base=base,
        head=head,
    )


@mcp.tool()
async def create_repository_release(
    owner: str,
    repository: str,
    tag_name: str,
    target: str,
    name: str | None = None,
    body: str = "",
    draft: bool = False,
    prerelease: bool = False,
) -> dict[str, Any]:
    """Create a GitHub release and tag for a polished or published project version."""
    return await get_github().create_release(
        owner,
        repository,
        tag_name=tag_name,
        target=target,
        name=name,
        body=body,
        draft=draft,
        prerelease=prerelease,
    )


@mcp.tool()
async def create_repository_pull_request(
    owner: str,
    repository: str,
    title: str,
    head: str,
    base: str,
    body: str = "",
    draft: bool = False,
) -> dict[str, Any]:
    """Open a pull request when the user prefers review rather than direct commits to the main branch."""
    return await get_github().create_pull_request(
        owner,
        repository,
        title=title,
        head=head,
        base=base,
        body=body,
        draft=draft,
    )


@mcp.tool()
async def search_repository_code(
    owner: str,
    repository: str,
    query: str,
    path: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search indexed text across a repository, optionally under any project-defined path."""
    return await get_github().search_code(
        owner,
        repository,
        query=query,
        path=path,
        limit=limit,
    )
