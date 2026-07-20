"""Tests for the ticketed upload endpoint.

The endpoint exists so an agent running in a sandbox can send file bytes
directly to the gateway instead of inlining them as base64 through the model's
context. It is deliberately unauthenticated apart from the ticket itself: the
ticket is a capability, minted over the authenticated MCP connection, scoped to
one path in one repository, and valid for minutes.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.dependencies import get_github
from app.github import GitHubClient
from app.main import app
from app.uploads import UploadTicketService, reset_consumed_tickets


@pytest.fixture(scope="module")
def client():
    """A client that does not enter the app lifespan.

    The lifespan exists only to start the MCP session manager, which can be
    started once per process and is irrelevant to the upload endpoint. Entering
    it here would collide with the other suites that mount the MCP app.
    """
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_tickets():
    reset_consumed_tickets()
    yield
    reset_consumed_tickets()


@pytest.fixture()
def committed():
    """Override the GitHub client with a mock transport; record what was sent."""
    recorded: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "head123"}})
        if request.method == "GET" and path.endswith("/git/commits/head123"):
            return httpx.Response(200, json={"tree": {"sha": "tree123"}})
        if request.method == "POST" and path.endswith("/git/blobs"):
            recorded["blob"] = request.read()
            return httpx.Response(201, json={"sha": "blob123"})
        if request.method == "POST" and path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "tree456"})
        if request.method == "POST" and path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "commit456"})
        if request.method == "PATCH" and path.endswith("/git/refs/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "commit456"}})
        return httpx.Response(500, json={"message": f"Unexpected: {request.method} {path}"})

    client_with_mock = GitHubClient(get_settings(), transport=httpx.MockTransport(handler))
    app.dependency_overrides[get_github] = lambda: client_with_mock
    yield recorded
    app.dependency_overrides.pop(get_github, None)


def issue(**overrides) -> str:
    params = {
        "owner": "jason",
        "repo": "demo",
        "path": "artifacts/report.bin",
        "branch": "main",
        "message": "Add report",
    }
    params.update(overrides)
    return UploadTicketService(get_settings()).issue(**params).token


def test_upload_with_a_valid_ticket_commits_the_bytes(client, committed):
    response = client.post(f"/upload/{issue()}", content=b"binary-payload")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["commit_sha"] == "commit456"
    assert body["path"] == "artifacts/report.bin"
    assert b"binary-payload" not in committed["blob"], "bytes should be sent base64-encoded"


def test_a_ticket_works_only_once(client, committed):
    token = issue()

    first = client.post(f"/upload/{token}", content=b"payload")
    second = client.post(f"/upload/{token}", content=b"payload")

    assert first.status_code == 200
    assert second.status_code == 401
    assert "already been used" in second.text


def test_a_garbage_ticket_is_rejected(client, committed):
    response = client.post("/upload/not-a-real-ticket", content=b"payload")

    assert response.status_code == 401


def test_an_empty_body_is_rejected(client, committed):
    response = client.post(f"/upload/{issue()}", content=b"")

    assert response.status_code == 422


def test_an_oversized_body_is_rejected(client, committed, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "8")
    get_settings.cache_clear()
    try:
        token = issue()
        response = client.post(f"/upload/{token}", content=b"x" * 9)
        assert response.status_code == 422
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_an_upload_may_exceed_the_inline_base64_limit(client, committed):
    """max_binary_input_bytes caps bytes crossing the model's context via MCP.

    Upload-ticket bytes never enter context, so that cap must not apply here —
    only the larger max_upload_bytes does. Without this the feature is pointless:
    anything worth uploading is bigger than the inline limit.
    """
    settings = get_settings()
    oversized_for_inline = b"x" * (settings.max_binary_input_bytes + 1024)
    assert len(oversized_for_inline) < settings.max_upload_bytes

    response = client.post(f"/upload/{issue()}", content=oversized_for_inline)

    assert response.status_code == 200, response.text


def test_the_upload_endpoint_does_not_require_an_oauth_bearer_token(client, committed):
    """The ticket is the credential; requiring a bearer too would defeat the design."""
    response = client.post(f"/upload/{issue()}", content=b"payload")

    assert response.status_code == 200


@pytest.fixture()
def empty_repository():
    """Mock a repository that was just created and has no commits or branch yet.

    GitHub answers the branch-ref lookup with 404 until a first commit exists, so
    the upload must bootstrap the branch instead of failing. Records the blob PUT.
    """
    recorded: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/git/ref/heads/main"):
            return httpx.Response(404, json={"message": "Not Found"})
        # Empty repositories reject the Git Data endpoints with 409 "empty", which
        # routes initialization through the Contents API.
        if request.method == "POST" and path.endswith("/git/blobs"):
            return httpx.Response(409, json={"message": "Git Repository is empty."})
        if request.method == "PUT" and "/contents/" in path:
            recorded["contents"] = request.read()
            return httpx.Response(201, json={"commit": {"sha": "seedcommit"}})
        return httpx.Response(500, json={"message": f"Unexpected: {request.method} {path}"})

    client_with_mock = GitHubClient(get_settings(), transport=httpx.MockTransport(handler))
    app.dependency_overrides[get_github] = lambda: client_with_mock
    yield recorded
    app.dependency_overrides.pop(get_github, None)


def test_first_upload_to_a_freshly_created_empty_repo_bootstraps_the_branch(client, empty_repository):
    """Uploading the first document right after create_repository must not 404.

    The branch ref does not exist yet in an empty repository, so the redeem path
    has to create the first commit rather than assume an existing head.
    """
    response = client.post(f"/upload/{issue()}", content=b"pdf-bytes")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["commit_sha"] == "seedcommit"
    assert body["path"] == "artifacts/report.bin"
    assert body["bytes_written"] == len(b"pdf-bytes")
    recorded_bytes = empty_repository.get("contents")
    assert recorded_bytes is not None, "the seed file should have been written"
    assert b"pdf-bytes" not in recorded_bytes, "bytes should be sent base64-encoded"
