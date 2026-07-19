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
        """Host header values the MCP transport accepts.

        FastMCP turns on DNS-rebinding protection with a loopback-only allowlist,
        which rejects every request once the gateway is deployed behind a real
        hostname. The public host is added here so the deployed service answers
        normally while the protection itself stays enabled.
        """
        hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
        parsed = urlparse(self.public_base_url)
        if parsed.hostname:
            hosts.append(parsed.netloc)
            hosts.append(f"{parsed.hostname}:*")
        return hosts

    @property
    def mcp_allowed_origins(self) -> list[str]:
        """Origin header values the MCP transport accepts.

        Claude's connector surfaces send an Origin of https://claude.ai when they
        send one at all; requests without an Origin are treated as same-origin.
        """
        return [
            "https://claude.ai",
            "https://www.claude.ai",
            self.public_base_url,
            "http://localhost:*",
            "http://127.0.0.1:*",
        ]

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
