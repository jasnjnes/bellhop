import time

import pytest

from app.errors import AuthenticationError, ValidationError
from app.uploads import UploadTicketService


@pytest.fixture()
def service(settings) -> UploadTicketService:
    return UploadTicketService(settings)


def test_issued_ticket_is_scoped_to_one_repo_and_path(service):
    ticket = service.issue(
        owner="jason",
        repo="demo",
        path="artifacts/report.pdf",
        branch="main",
        message="Add report",
    )

    claims = service.inspect(ticket.token)
    assert claims["owner"] == "jason"
    assert claims["repo"] == "demo"
    assert claims["path"] == "artifacts/report.pdf"
    assert claims["branch"] == "main"
    assert claims["message"] == "Add report"


def test_issued_ticket_url_points_at_the_public_upload_endpoint(service, settings):
    ticket = service.issue(
        owner="jason", repo="demo", path="a.bin", branch="main", message="m"
    )

    assert ticket.upload_url == f"{settings.public_base_url}/upload/{ticket.token}"
    assert ticket.expires_in == settings.upload_ticket_ttl_seconds
    assert ticket.max_bytes == settings.max_upload_bytes


def test_ticket_path_is_normalized_at_issue_time(service):
    with pytest.raises(ValidationError):
        service.issue(
            owner="jason", repo="demo", path="../../etc/passwd", branch="main", message="m"
        )


def test_redeeming_a_ticket_returns_its_claims(service):
    ticket = service.issue(
        owner="jason", repo="demo", path="a.bin", branch="main", message="Add a"
    )

    claims = service.redeem(ticket.token)

    assert claims["path"] == "a.bin"
    assert claims["repo"] == "demo"


def test_a_ticket_cannot_be_redeemed_twice(service):
    ticket = service.issue(
        owner="jason", repo="demo", path="a.bin", branch="main", message="Add a"
    )
    service.redeem(ticket.token)

    with pytest.raises(AuthenticationError):
        service.redeem(ticket.token)


def test_an_expired_ticket_is_rejected(settings):
    settings.upload_ticket_ttl_seconds = 1
    service = UploadTicketService(settings)
    ticket = service.issue(
        owner="jason", repo="demo", path="a.bin", branch="main", message="Add a"
    )

    # Rather than sleeping, mint the ticket in the past.
    past = int(time.time()) - 10
    expired = service._encode({**service.inspect(ticket.token), "exp": past})

    with pytest.raises(AuthenticationError):
        service.redeem(expired)


def test_a_tampered_ticket_is_rejected(service):
    ticket = service.issue(
        owner="jason", repo="demo", path="a.bin", branch="main", message="Add a"
    )
    header, payload, signature = ticket.token.split(".")
    forged = f"{header}.{payload}x.{signature}"

    with pytest.raises(AuthenticationError):
        service.redeem(forged)


def test_a_ticket_signed_with_another_secret_is_rejected(settings, service):
    other = settings.model_copy(update={"jwt_secret": "y" * 64})
    foreign = UploadTicketService(other).issue(
        owner="jason", repo="demo", path="a.bin", branch="main", message="Add a"
    )

    with pytest.raises(AuthenticationError):
        service.redeem(foreign.token)


def test_an_access_token_cannot_be_used_as_an_upload_ticket(settings, service):
    from app.oauth import TokenService

    access = TokenService(settings).access_token(client_id="test-client", scope="repo")

    with pytest.raises(AuthenticationError):
        service.redeem(access)


def test_an_upload_ticket_cannot_be_used_as_an_access_token(settings, service):
    from app.oauth import TokenService

    ticket = service.issue(
        owner="jason", repo="demo", path="a.bin", branch="main", message="m"
    )

    with pytest.raises(AuthenticationError):
        TokenService(settings).decode(ticket.token, token_type="access_token")


def test_a_payload_over_max_bytes_is_rejected(service, settings):
    settings.max_upload_bytes = 16
    ticket = service.issue(
        owner="jason", repo="demo", path="a.bin", branch="main", message="m"
    )

    with pytest.raises(ValidationError):
        service.enforce_size(ticket.token, b"x" * 17)


def test_a_payload_within_max_bytes_is_accepted(service, settings):
    settings.max_upload_bytes = 16
    ticket = service.issue(
        owner="jason", repo="demo", path="a.bin", branch="main", message="m"
    )

    service.enforce_size(ticket.token, b"x" * 16)


def test_an_empty_payload_is_rejected(service):
    ticket = service.issue(
        owner="jason", repo="demo", path="a.bin", branch="main", message="m"
    )

    with pytest.raises(ValidationError):
        service.enforce_size(ticket.token, b"")


def test_create_upload_ticket_tool_returns_a_usable_url():
    """The MCP tool an agent calls to get an upload URL."""
    import asyncio

    from app.mcp_server import create_upload_ticket

    result = asyncio.run(
        create_upload_ticket(
            owner="jason",
            repository="demo",
            path="artifacts/out.bin",
            branch="main",
            message="Add output",
        )
    )

    assert result["upload_url"].endswith(f"/upload/{result['ticket']}")
    assert result["path"] == "artifacts/out.bin"
    assert result["expires_in"] > 0
    assert result["max_bytes"] > 0
    assert result["method"] == "POST"
