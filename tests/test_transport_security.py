"""Regression tests for the MCP transport's DNS-rebinding host allowlist.

FastMCP auto-enables DNS rebinding protection with a localhost-only allowlist
whenever its `host` setting is a loopback address. Deployed behind Render the
inbound Host header is the public hostname, so an unconfigured gateway answers
every MCP request with 421 and the connector can never finish handshaking.
"""

from app.config import Settings


def make_settings(public_base_url: str) -> Settings:
    return Settings(
        github_token="test-github-token",
        public_base_url=public_base_url,
        mcp_oauth_client_secret="test-client-secret-123",
        mcp_login_password="test-login-password",
        jwt_secret="x" * 64,
    )


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


def test_allowed_origins_include_claude_and_self():
    settings = make_settings("https://gateway.onrender.com")
    assert "https://claude.ai" in settings.mcp_allowed_origins
    assert "https://gateway.onrender.com" in settings.mcp_allowed_origins


def test_mcp_transport_security_is_configured_for_the_public_host():
    """The FastMCP instance must carry our allowlist, not FastMCP's localhost default."""
    from app.config import get_settings
    from app.mcp_server import mcp

    security = mcp.settings.transport_security
    assert security is not None, "transport_security must be set explicitly"
    assert security.enable_dns_rebinding_protection
    assert security.allowed_hosts == get_settings().mcp_allowed_hosts
    assert security.allowed_origins == get_settings().mcp_allowed_origins
