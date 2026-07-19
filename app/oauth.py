from __future__ import annotations

import base64
import hashlib
import hmac
import html
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import jwt
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import Settings, get_settings
from app.errors import AuthenticationError, ValidationError
from app.ratelimit import get_login_throttle


SCOPES = "github:read github:write repo:create release:write"


def _b64url_sha256(value: str) -> str:
    digest = hashlib.sha256(value.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_pkce(verifier: str, challenge: str) -> bool:
    return hmac.compare_digest(_b64url_sha256(verifier), challenge)


def redirect_uri_allowed(uri: str, settings: Settings) -> bool:
    if uri == "https://claude.ai/api/mcp/auth_callback":
        return True

    parsed = urlparse(uri)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
        return False
    return parsed.path == "/callback"


@dataclass
class TokenService:
    settings: Settings

    def encode(self, payload: dict[str, Any], *, ttl: int, token_type: str) -> str:
        now = int(time.time())
        body = {
            **payload,
            "iss": self.settings.issuer,
            "iat": now,
            "exp": now + ttl,
            "jti": secrets.token_urlsafe(16),
            "typ": token_type,
        }
        return jwt.encode(body, self.settings.jwt_secret, algorithm="HS256")

    def decode(self, token: str, *, token_type: str) -> dict[str, Any]:
        try:
            payload = jwt.decode(
                token,
                self.settings.jwt_secret,
                algorithms=["HS256"],
                issuer=self.settings.issuer,
                options={"require": ["exp", "iat", "iss", "typ"], "verify_aud": False},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError("The OAuth token is invalid or expired.") from exc

        if payload.get("typ") != token_type:
            raise AuthenticationError("The OAuth token type is invalid.")
        return payload

    def authorization_code(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        scope: str,
    ) -> str:
        return self.encode(
            {
                "sub": "gateway-owner",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "scope": scope,
            },
            ttl=self.settings.auth_code_ttl_seconds,
            token_type="authorization_code",
        )

    def access_token(self, *, client_id: str, scope: str) -> str:
        return self.encode(
            {
                "sub": "gateway-owner",
                "client_id": client_id,
                "scope": scope,
                "aud": self.settings.mcp_resource,
            },
            ttl=self.settings.access_token_ttl_seconds,
            token_type="access_token",
        )

    def refresh_token(self, *, client_id: str, scope: str) -> str:
        return self.encode(
            {
                "sub": "gateway-owner",
                "client_id": client_id,
                "scope": scope,
            },
            ttl=self.settings.refresh_token_ttl_seconds,
            token_type="refresh_token",
        )


def _validate_authorize_request(
    settings: Settings,
    *,
    response_type: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
) -> None:
    if response_type != "code":
        raise ValidationError("Only response_type=code is supported.")
    if not hmac.compare_digest(client_id, settings.mcp_oauth_client_id):
        raise AuthenticationError("Unknown OAuth client.")
    if not redirect_uri_allowed(redirect_uri, settings):
        raise ValidationError("The OAuth redirect URI is not allowed.")
    if code_challenge_method != "S256" or not code_challenge:
        raise ValidationError("PKCE using code_challenge_method=S256 is required.")


def _form_page(fields: dict[str, str], error: str = "") -> str:
    hidden = "\n".join(
        f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}">'
        for key, value in fields.items()
    )
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Authorize Bellhop</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background:#f6f7f9; margin:0; padding:32px; }}
    main {{ max-width:520px; margin:8vh auto; background:white; padding:28px; border-radius:16px;
            box-shadow:0 8px 30px rgba(0,0,0,.08); }}
    h1 {{ margin-top:0; }}
    label {{ display:block; font-weight:650; margin:22px 0 8px; }}
    input[type=password] {{ box-sizing:border-box; width:100%; padding:12px; border:1px solid #bbb;
                           border-radius:8px; font-size:16px; }}
    button {{ margin-top:20px; width:100%; padding:12px; border:0; border-radius:8px;
              font-size:16px; font-weight:700; cursor:pointer; }}
    .error {{ color:#a40000; }}
    .note {{ color:#555; line-height:1.45; }}
  </style>
</head>
<body>
<main>
  <h1>Connect Claude</h1>
  <p class="note">This authorizes Claude to use your private GitHub gateway. The GitHub token stays
  on Render and is never sent to Claude.</p>
  {error_html}
  <form method="post" action="/authorize">
    {hidden}
    <label for="password">Gateway password</label>
    <input id="password" name="password" type="password" required autofocus>
    <button type="submit">Authorize</button>
  </form>
</main>
</body>
</html>"""


router = APIRouter()


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource_metadata(settings: Settings = Depends(get_settings)):
    return {
        "resource": settings.mcp_resource,
        "authorization_servers": [settings.issuer],
        "scopes_supported": SCOPES.split(),
        "bearer_methods_supported": ["header"],
    }


@router.get("/.well-known/oauth-authorization-server")
def authorization_server_metadata(settings: Settings = Depends(get_settings)):
    return {
        "issuer": settings.issuer,
        "authorization_endpoint": f"{settings.public_base_url}/authorize",
        "token_endpoint": f"{settings.public_base_url}/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "scopes_supported": SCOPES.split(),
    }


@router.get("/authorize", response_class=HTMLResponse)
def authorize_get(
    response_type: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
    state: str = "",
    scope: str = SCOPES,
    resource: str = "",
    settings: Settings = Depends(get_settings),
):
    _validate_authorize_request(
        settings,
        response_type=response_type,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )
    return _form_page(
        {
            "response_type": response_type,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "state": state,
            "scope": scope,
            "resource": resource,
        }
    )


@router.post("/authorize", response_class=HTMLResponse)
def authorize_post(
    password: str = Form(...),
    response_type: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    code_challenge: str = Form(...),
    code_challenge_method: str = Form(...),
    state: str = Form(""),
    scope: str = Form(SCOPES),
    resource: str = Form(""),
    settings: Settings = Depends(get_settings),
):
    _validate_authorize_request(
        settings,
        response_type=response_type,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )
    fields = {
        "response_type": response_type,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "state": state,
        "scope": scope,
        "resource": resource,
    }
    throttle = get_login_throttle(
        settings.max_login_attempts, settings.login_attempt_window_seconds
    )
    retry_after = throttle.retry_after_seconds()
    if retry_after:
        return HTMLResponse(
            _form_page(fields, "Too many attempts. Wait a few minutes and try again."),
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    if not hmac.compare_digest(password, settings.mcp_login_password):
        throttle.record_failure()
        return HTMLResponse(_form_page(fields, "Incorrect gateway password."), status_code=401)
    throttle.reset()

    code = TokenService(settings).authorization_code(
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        scope=scope,
    )
    query = {"code": code}
    if state:
        query["state"] = state
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{separator}{urlencode(query)}", status_code=302)


def _client_credentials(request: Request, client_id: str, client_secret: str) -> tuple[str, str]:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(authorization[6:]).decode("utf-8")
            basic_id, basic_secret = decoded.split(":", 1)
            return basic_id, basic_secret
        except (ValueError, UnicodeDecodeError):
            raise AuthenticationError("Malformed OAuth Basic authentication.")
    return client_id, client_secret


@router.post("/token")
def token(
    request: Request,
    grant_type: str = Form(...),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    code: str = Form(""),
    redirect_uri: str = Form(""),
    code_verifier: str = Form(""),
    refresh_token: str = Form(""),
    settings: Settings = Depends(get_settings),
):
    supplied_id, supplied_secret = _client_credentials(request, client_id, client_secret)
    if not hmac.compare_digest(supplied_id, settings.mcp_oauth_client_id):
        return JSONResponse({"error": "invalid_client"}, status_code=401)
    if not hmac.compare_digest(supplied_secret, settings.mcp_oauth_client_secret):
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    service = TokenService(settings)

    if grant_type == "authorization_code":
        if not code or not redirect_uri or not code_verifier:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        try:
            payload = service.decode(code, token_type="authorization_code")
        except AuthenticationError:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if payload.get("client_id") != supplied_id or payload.get("redirect_uri") != redirect_uri:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if not verify_pkce(code_verifier, payload.get("code_challenge", "")):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        scope = payload.get("scope", SCOPES)

    elif grant_type == "refresh_token":
        if not refresh_token:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        try:
            payload = service.decode(refresh_token, token_type="refresh_token")
        except AuthenticationError:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if payload.get("client_id") != supplied_id:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        scope = payload.get("scope", SCOPES)

    else:
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    return {
        "access_token": service.access_token(client_id=supplied_id, scope=scope),
        "token_type": "Bearer",
        "expires_in": settings.access_token_ttl_seconds,
        "refresh_token": service.refresh_token(client_id=supplied_id, scope=scope),
        "scope": scope,
    }
