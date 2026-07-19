from __future__ import annotations

import os
from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Bellhop"
    environment: str = "production"

    github_token: str = Field(min_length=1)
    github_default_owner: str = ""
    github_api_url: str = "https://api.github.com"
    github_api_version: str = "2026-03-10"

    public_base_url: str = ""
    render_external_url: str = ""

    mcp_oauth_client_id: str = "bellhop"
    mcp_oauth_client_secret: str = Field(min_length=16)
    mcp_login_password: str = Field(min_length=12)
    jwt_secret: str = Field(min_length=32)

    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_seconds: int = 604_800  # 7 days. Shrinks the window a leaked token grants.
    auth_code_ttl_seconds: int = 300

    # Brute-force protection for the /authorize password form. Global (single-user
    # gateway): after this many failures inside the window, the form returns 429
    # until the window clears. In-memory and best-effort on a single instance.
    max_login_attempts: int = 5
    login_attempt_window_seconds: int = 900

    # DNS-rebinding protection validates the Host and Origin headers on MCP
    # requests. It exists to stop a malicious web page from reaching an MCP
    # server bound to loopback. This gateway is public and requires an OAuth
    # bearer token on every MCP call, so a browser-based attacker cannot make an
    # authenticated request regardless. Leaving it on only risks 403-ing real
    # clients whose Origin is not in the allowlist, so it defaults to off.
    mcp_dns_rebinding_protection: bool = False
    # Comma-separated extra origins, used only when the protection is enabled.
    mcp_extra_allowed_origins: str = ""

    require_expected_head: bool = True
    max_text_result_bytes: int = 120_000
    max_binary_input_bytes: int = 2_500_000
    max_commit_files: int = 100

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_urls(self):
        base = self.public_base_url or self.render_external_url or os.getenv("RENDER_EXTERNAL_URL", "")
        if not base:
            base = "http://localhost:8000"
        self.public_base_url = base.rstrip("/")
        self.github_api_url = self.github_api_url.rstrip("/")
        return self

    @property
    def issuer(self) -> str:
        return self.public_base_url

    @property
    def mcp_resource(self) -> str:
        return f"{self.public_base_url}/mcp"

    @property
    def mcp_allowed_hosts(self) -> list[str]:
        """Host header values the MCP transport accepts when protection is enabled.

        FastMCP defaults to a loopback-only allowlist, which rejects every request
        once the gateway is deployed behind a real hostname. The public host is
        added so the deployed service answers normally.
        """
        hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
        parsed = urlparse(self.public_base_url)
        if parsed.hostname:
            hosts.append(parsed.netloc)
            hosts.append(f"{parsed.hostname}:*")
        return hosts

    @property
    def mcp_allowed_origins(self) -> list[str]:
        """Origin header values the MCP transport accepts when protection is enabled.

        Client origins cannot be reliably enumerated: Claude is served from both
        claude.ai and claude.com, and desktop builds may send another origin
        again. Anything missing here is rejected with 403, which is why
        mcp_dns_rebinding_protection defaults to off.
        """
        origins = [
            "https://claude.ai",
            "https://www.claude.ai",
            "https://claude.com",
            "https://www.claude.com",
            "https://api.claude.com",
            self.public_base_url,
            "http://localhost:*",
            "http://127.0.0.1:*",
        ]
        extra = [origin.strip() for origin in self.mcp_extra_allowed_origins.split(",") if origin.strip()]
        return origins + extra

    @property
    def allowed_redirect_uris(self) -> set[str]:
        return {
            "https://claude.ai/api/mcp/auth_callback",
            "http://localhost/callback",
            "http://127.0.0.1/callback",
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
