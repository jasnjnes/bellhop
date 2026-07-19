import pytest

from app.config import Settings


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
