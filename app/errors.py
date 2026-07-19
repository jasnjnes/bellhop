from __future__ import annotations


class GatewayError(Exception):
    status_code = 400
    code = "gateway_error"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(GatewayError):
    status_code = 422
    code = "validation_error"


class AuthenticationError(GatewayError):
    status_code = 401
    code = "authentication_error"


class AuthorizationError(GatewayError):
    status_code = 403
    code = "authorization_error"


class NotFoundError(GatewayError):
    status_code = 404
    code = "not_found"


class ConflictError(GatewayError):
    status_code = 409
    code = "conflict"


class GitHubAPIError(GatewayError):
    status_code = 502
    code = "github_api_error"

    def __init__(
        self,
        message: str,
        *,
        github_status: int | None = None,
        details: dict | None = None,
    ):
        super().__init__(message, details=details)
        self.github_status = github_status
        if github_status in {401, 403}:
            self.status_code = github_status
        elif github_status == 404:
            self.status_code = 404
        elif github_status in {409, 422}:
            self.status_code = 409
