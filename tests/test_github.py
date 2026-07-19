import json

import httpx
import pytest

from app.errors import ConflictError, ValidationError
from app.github import GitHubClient, normalize_path
from app.models import FileChange


def test_path_validation():
    assert normalize_path("src/app.py") == "src/app.py"
    with pytest.raises(ValidationError):
        normalize_path("../secret")
    with pytest.raises(ValidationError):
        normalize_path("/absolute")


@pytest.mark.asyncio
async def test_atomic_commit(settings):
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        path = request.url.path
        if request.method == "GET" and path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "head123"}})
        if request.method == "GET" and path.endswith("/git/commits/head123"):
            return httpx.Response(200, json={"tree": {"sha": "tree123"}})
        if request.method == "POST" and path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "blob123"})
        if request.method == "POST" and path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "tree456"})
        if request.method == "POST" and path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "commit456"})
        if request.method == "PATCH" and path.endswith("/git/refs/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "commit456"}})
        return httpx.Response(500, json={"message": f"Unexpected request: {request.method} {path}"})

    client = GitHubClient(settings, transport=httpx.MockTransport(handler))
    result = await client.commit_files(
        "jason",
        "demo",
        branch="main",
        message="Add application",
        changes=[FileChange(path="src/main.py", content="print('hello')\n")],
        expected_head="head123",
    )

    assert result["previous_head"] == "head123"
    assert result["commit_sha"] == "commit456"
    assert result["changed_paths"] == ["src/main.py"]
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_stale_head_rejected_when_auto_rebase_disabled(settings):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": {"sha": "new-head"}})

    client = GitHubClient(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(ConflictError):
        await client.commit_files(
            "jason",
            "demo",
            branch="main",
            message="Stale write",
            changes=[FileChange(path="README.md", content="stale")],
            expected_head="old-head",
            auto_rebase=False,
        )


@pytest.mark.asyncio
async def test_auto_rebase_nonoverlapping_edit_succeeds(settings):
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "new-head"}})
        if request.method == "GET" and "/compare/" in path:
            # The branch moved by changing a different file.
            return httpx.Response(200, json={"files": [{"filename": "docs/other.md"}]})
        if request.method == "GET" and path.endswith("/git/commits/new-head"):
            return httpx.Response(200, json={"tree": {"sha": "tree-new"}})
        if request.method == "POST" and path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "blob123"})
        if request.method == "POST" and path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "tree456"})
        if request.method == "POST" and path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "commit789"})
        if request.method == "PATCH" and path.endswith("/git/refs/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "commit789"}})
        return httpx.Response(500, json={"message": f"Unexpected: {request.method} {path}"})

    client = GitHubClient(settings, transport=httpx.MockTransport(handler))
    result = await client.commit_files(
        "jason",
        "demo",
        branch="main",
        message="Independent edit",
        changes=[FileChange(path="README.md", content="new intro\n")],
        expected_head="old-head",
    )
    assert result["rebased_onto_current_head"] is True
    assert result["expected_head"] == "old-head"
    assert result["previous_head"] == "new-head"
    assert result["commit_sha"] == "commit789"


@pytest.mark.asyncio
async def test_auto_rebase_overlapping_edit_conflicts(settings):
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "new-head"}})
        if request.method == "GET" and "/compare/" in path:
            # The branch moved by changing the same file we are editing.
            return httpx.Response(200, json={"files": [{"filename": "README.md"}]})
        return httpx.Response(500, json={"message": f"Unexpected: {request.method} {path}"})

    client = GitHubClient(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(ConflictError) as exc_info:
        await client.commit_files(
            "jason",
            "demo",
            branch="main",
            message="Colliding edit",
            changes=[FileChange(path="README.md", content="mine\n")],
            expected_head="old-head",
        )
    assert exc_info.value.details["conflicting_paths"] == ["README.md"]


@pytest.mark.asyncio
async def test_initialize_empty_repository(settings):
    blob_counter = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal blob_counter
        path = request.url.path
        if request.method == "POST" and path.endswith("/git/blobs"):
            blob_counter += 1
            return httpx.Response(201, json={"sha": f"blob{blob_counter}"})
        if request.method == "POST" and path.endswith("/git/trees"):
            body = json.loads(request.content)
            assert "base_tree" not in body
            return httpx.Response(201, json={"sha": "initial-tree"})
        if request.method == "POST" and path.endswith("/git/commits"):
            body = json.loads(request.content)
            assert body["parents"] == []
            return httpx.Response(201, json={"sha": "initial-commit"})
        if request.method == "POST" and path.endswith("/git/refs"):
            return httpx.Response(201, json={"ref": "refs/heads/main"})
        return httpx.Response(500, json={"message": "Unexpected request"})

    client = GitHubClient(settings, transport=httpx.MockTransport(handler))
    result = await client.initialize_repository(
        "jason",
        "demo",
        branch="main",
        message="Initial project",
        files=[
            FileChange(path="custom/location/spec.md", content="# Spec\n"),
            FileChange(path="application.py", content="print('ok')\n"),
        ],
    )
    assert result["commit_sha"] == "initial-commit"
    assert result["changed_paths"] == ["custom/location/spec.md", "application.py"]
