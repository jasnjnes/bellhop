from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.errors import AuthenticationError
from app.oauth import TokenService


class MCPBearerAuthMiddleware:
    """Require gateway-issued OAuth access tokens on the mounted MCP endpoint."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        headers = Headers(scope=scope)
        authorization = headers.get("authorization", "")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""

        try:
            if not token:
                raise AuthenticationError("Bearer token required.")
            payload = TokenService(settings).decode(token, token_type="access_token")
            if payload.get("aud") != settings.mcp_resource:
                raise AuthenticationError("The access token audience is invalid.")
            scope.setdefault("state", {})
            scope["state"]["principal"] = payload.get("sub")
        except AuthenticationError:
            metadata = f"{settings.public_base_url}/.well-known/oauth-protected-resource/mcp"
            response = JSONResponse(
                {"error": "unauthorized", "message": "Connect this MCP server using OAuth."},
                status_code=401,
                headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{metadata}"',
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
