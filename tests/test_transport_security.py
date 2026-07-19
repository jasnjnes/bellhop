"""Tests for the MCP transport's DNS-rebinding settings.

Two failures shaped these tests, both of which broke the deployed connector:

1. FastMCP defaults to a loopback-only Host allowlist, so a gateway deployed
   behind a real hostname answered every MCP request with 421.
2. With protection enabled, any Origin not in the allowlist gets a 403. Claude
   is served from both claude.ai and claude.com and desktop builds may send
   another origin again, so enumerating client origins is not workable. OAuth
   succeeded and then every MCP call 403'd, which surfaces as "not connected".

The protection therefore defaults to off: the OAuth bearer requirement on
/mcp is the actual access control, and a browser-based attacker cannot mint a
token. The allowlists remain correct for anyone who turns it back on.
"""

import base64
import hashlib
import re
import secrets

import pytest
from fastapi.testclient import TestClient

from app.config import Settings


def make_settings(public_base_url: str, **overrides) -> Settings:
    return Settings(
        github_token="test-github-token",
        public_base_url=public_base_url,
        mcp_oauth_client_secret="test-client-secret-123",
        mcp_login_password="test-login-password",
        jwt_secret="x" * 64,
        **overrides,
    )


def test_protection_is_off_by_default():
    """Off by default so an unlisted client Origin cannot 403 the connector."""
    assert make_settings("https://gateway.test").mcp_dns_rebinding_protection is False


def test_allowed_hosts_include_public_hostname():
    settings = make_settings("https://gateway.onrender.com")
    assert "gateway.onrender.com" in settings.mcp_allowed_hosts
    assert "gateway.onrender.com:*" in settings.mcp_allowed_hosts


def test_allowed_hosts_include_localhost_for_local_development():
    settings = make_settings("http://localhost:8000")
    assert "localhost:*" in settings.mcp_allowed_hosts
    assert "127.0.0.1:*" in settings.mcp_allowed_hosts


def test_allowed_hosts_preserve_non_default_port():
    settings = make_settings("https://gateway.example.com:8443")
    assert "gateway.example.com:8443" in settings.mcp_allowed_hosts


@pytest.mark.parametrize(
    "origin",
    ["https://claude.ai", "https://claude.com", "https://www.claude.com"],
)
def test_allowed_origins_cover_both_claude_domains(origin):
    """Claude is served from claude.ai and claude.com; both must be listed."""
    assert origin in make_settings("https://gateway.test").mcp_allowed_origins


def test_extra_allowed_origins_are_configurable():
    settings = make_settings(
        "https://gateway.test",
        mcp_extra_allowed_origins="https://example.test, https://other.test",
    )
    assert "https://example.test" in settings.mcp_allowed_origins
    assert "https://other.test" in settings.mcp_allowed_origins


@pytest.fixture(scope="module")
def client() -> TestClient:
    """One client for the module.

    mcp.session_manager.run() can only be entered once per process, so each test
    must not open its own TestClient context.
    """
    from app.main import app

    with TestClient(app, base_url="https://gateway.test") as test_client:
        yield test_client


def _access_token(client: TestClient) -> str:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    params = {
        "response_type": "code",
        "client_id": "test-client",
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "x",
    }
    redirect = client.post(
        "/authorize",
        data={**params, "password": "test-login-password"},
        follow_redirects=False,
    )
    code = re.search(r"code=([^&]+)", redirect.headers["location"]).group(1)
    token_response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": params["redirect_uri"],
            "code_verifier": verifier,
            "client_id": "test-client",
            "client_secret": "test-client-secret-123",
        },
    )
    return token_response.json()["access_token"]


@pytest.mark.parametrize(
    "origin",
    [None, "https://claude.ai", "https://claude.com", "https://desktop.claude.ai", "app://claude"],
)
def test_authenticated_mcp_request_succeeds_for_any_client_origin(client, origin):
    """Regression: an unlisted Origin used to 403 after OAuth had succeeded."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {_access_token(client)}",
    }
    if origin:
        headers["Origin"] = origin

    response = client.post(
        "/mcp/",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
    )

    assert response.status_code == 200, f"Origin {origin!r} rejected: {response.text[:200]}"


def test_mcp_endpoint_still_requires_a_bearer_token(client):
    """Disabling transport security must not weaken the actual access control."""
    response = client.post(
        "/mcp/",
        headers={"Content-Type": "application/json", "Origin": "https://evil.test"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers
