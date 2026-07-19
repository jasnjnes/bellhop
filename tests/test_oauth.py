import base64
import hashlib

from app.oauth import TokenService, verify_pkce


def challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")


def test_pkce_and_tokens(settings):
    verifier = "a" * 64
    assert verify_pkce(verifier, challenge(verifier))
    assert not verify_pkce("wrong", challenge(verifier))

    service = TokenService(settings)
    code = service.authorization_code(
        client_id=settings.mcp_oauth_client_id,
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        code_challenge=challenge(verifier),
        scope="github:read github:write",
    )
    payload = service.decode(code, token_type="authorization_code")
    assert payload["client_id"] == settings.mcp_oauth_client_id

    access = service.access_token(
        client_id=settings.mcp_oauth_client_id,
        scope="github:read",
    )
    access_payload = service.decode(access, token_type="access_token")
    assert access_payload["aud"] == settings.mcp_resource
