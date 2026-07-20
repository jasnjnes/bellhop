from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

import jwt

from app.config import Settings
from app.errors import AuthenticationError, ValidationError
from app.github import normalize_path


TOKEN_TYPE = "upload_ticket"

# Redeemed ticket IDs, held for the lifetime of the process. A ticket is a
# capability rather than a credential, so the only replay window is its own TTL;
# a restart clears this set and a ticket could be redeemed a second time inside
# that window. That is accepted deliberately: this gateway is single-owner and
# runs as one instance with no datastore. A shared deployment needs Redis or
# Postgres here instead.
_consumed: dict[str, int] = {}


def _forget_expired(now: int) -> None:
    for jti, expires_at in list(_consumed.items()):
        if expires_at <= now:
            del _consumed[jti]


def reset_consumed_tickets() -> None:
    """Clear redeemed-ticket state. For tests."""
    _consumed.clear()


@dataclass(frozen=True)
class UploadTicket:
    token: str
    upload_url: str
    expires_in: int
    max_bytes: int
    path: str


class UploadTicketService:
    """Issues and redeems single-use tickets that authorize one file upload.

    The ticket authorizes exactly one path, on one branch, in one repository, for
    a few minutes. It carries no GitHub credential, so it grants nothing beyond
    that single write even if it leaks.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def _encode(self, claims: dict[str, Any]) -> str:
        return jwt.encode(claims, self.settings.jwt_secret, algorithm="HS256")

    def inspect(self, token: str) -> dict[str, Any]:
        """Decode and validate a ticket without consuming it."""
        try:
            claims = jwt.decode(
                token,
                self.settings.jwt_secret,
                algorithms=["HS256"],
                issuer=self.settings.issuer,
                options={"require": ["exp", "iat", "iss", "typ", "jti"], "verify_aud": False},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError("The upload ticket is invalid or expired.") from exc

        if claims.get("typ") != TOKEN_TYPE:
            raise AuthenticationError("That token is not an upload ticket.")
        return claims

    def issue(
        self,
        *,
        owner: str,
        repo: str,
        path: str,
        branch: str,
        message: str,
    ) -> UploadTicket:
        safe_path = normalize_path(path)
        now = int(time.time())
        ttl = self.settings.upload_ticket_ttl_seconds
        claims = {
            "owner": owner,
            "repo": repo,
            "path": safe_path,
            "branch": branch,
            "message": message,
            "max_bytes": self.settings.max_upload_bytes,
            "iss": self.settings.issuer,
            "iat": now,
            "exp": now + ttl,
            "jti": secrets.token_urlsafe(16),
            "typ": TOKEN_TYPE,
        }
        token = self._encode(claims)
        return UploadTicket(
            token=token,
            upload_url=f"{self.settings.public_base_url}/upload/{token}",
            expires_in=ttl,
            max_bytes=self.settings.max_upload_bytes,
            path=safe_path,
        )

    def redeem(self, token: str) -> dict[str, Any]:
        """Validate a ticket and burn it. Raises if already used."""
        claims = self.inspect(token)
        now = int(time.time())
        _forget_expired(now)

        jti = claims["jti"]
        if jti in _consumed:
            raise AuthenticationError("This upload ticket has already been used.")
        _consumed[jti] = int(claims["exp"])
        return claims

    def enforce_size(self, token: str, body: bytes) -> None:
        claims = self.inspect(token)
        if not body:
            raise ValidationError("The upload body is empty.")
        max_bytes = int(claims["max_bytes"])
        if len(body) > max_bytes:
            raise ValidationError(
                f"The upload is {len(body)} bytes, which exceeds the {max_bytes} byte limit."
            )
