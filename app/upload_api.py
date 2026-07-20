from __future__ import annotations

import base64
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

from app.config import Settings, get_settings
from app.dependencies import get_github
from app.github import GitHubClient
from app.uploads import UploadTicketService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/upload/{token}")
async def redeem_upload_ticket(
    token: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    github: GitHubClient = Depends(get_github),
) -> dict[str, Any]:
    """Accept raw bytes for a ticketed path and commit them to GitHub.

    Deliberately not behind the OAuth bearer requirement: the ticket is the
    credential. It is minted over the authenticated MCP connection, authorizes
    exactly one path in one repository, expires in minutes, and is burned on
    first use.
    """
    service = UploadTicketService(settings)
    body = await request.body()

    # Size and signature are checked before the ticket is burned so a rejected
    # upload can be retried with the same ticket.
    service.enforce_size(token, body)
    claims = service.redeem(token)

    owner = claims["owner"]
    repo = claims["repo"]
    path = claims["path"]
    branch = claims["branch"]

    # commit_upload creates the branch's first commit when the repository was just
    # created and has no commits yet, so the first upload after create_repository
    # does not fail on a missing branch ref.
    result = await github.commit_upload(
        owner,
        repo,
        branch=branch,
        message=claims["message"],
        path=path,
        content_base64=base64.b64encode(body).decode("ascii"),
        max_bytes=int(claims["max_bytes"]),
    )

    logger.info(
        "Upload ticket redeemed",
        extra={"repo": f"{owner}/{repo}", "path": path, "bytes": len(body)},
    )
    return {**result, "path": path, "bytes_written": len(body)}
