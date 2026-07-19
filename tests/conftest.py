import os

import pytest

# Modules such as app.mcp_server read settings at import time, so the required
# secrets must exist in the environment before any app module is imported.
os.environ.setdefault("GITHUB_TOKEN", "test-github-token")
os.environ.setdefault("PUBLIC_BASE_URL", "https://gateway.test")
os.environ.setdefault("MCP_OAUTH_CLIENT_ID", "test-client")
os.environ.setdefault("MCP_OAUTH_CLIENT_SECRET", "test-client-secret-123")
os.environ.setdefault("MCP_LOGIN_PASSWORD", "test-login-password")
os.environ.setdefault("JWT_SECRET", "x" * 64)

from app.config import Settings  # noqa: E402


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        github_token="test-github-token",
        github_api_url="https://api.github.test",
        public_base_url="https://gateway.test",
        mcp_oauth_client_id="test-client",
        mcp_oauth_client_secret="test-client-secret-123",
        mcp_login_password="test-login-password",
        jwt_secret="x" * 64,
        require_expected_head=True,
    )
