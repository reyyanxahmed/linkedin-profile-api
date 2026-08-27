"""Error taxonomy for the LinkedIn Profile API.

Single responsibility: define the error classes and stable codes mapped to HTTP status.
Every error carries a stable `code` string so clients can branch on it, not on message
text. ConfigError names the exact key in queries.yaml that is unset.
"""

from __future__ import annotations


class AppError(Exception):
    """Base for all app errors. Subclasses set `code` and `status_code`."""

    code: str = "INTERNAL"
    status_code: int = 500

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message or self.__class__.__name__

    def as_dict(self, request_id: str | None = None) -> dict:
        body: dict = {"error": {"code": self.code, "message": self.message}}
        if request_id:
            body["error"]["request_id"] = request_id
        return body


class InvalidUrlError(AppError):
    code = "INVALID_URL"
    status_code = 400


class ProfileNotFoundError(AppError):
    code = "PROFILE_NOT_FOUND"
    status_code = 404


class ProfilePrivateError(AppError):
    code = "PROFILE_PRIVATE"
    status_code = 403


class UpstreamChallengeError(AppError):
    code = "UPSTREAM_CHALLENGE"
    status_code = 502


class NoSessionsError(AppError):
    code = "ALL_SESSIONS_COOLING"
    status_code = 503


class RateLimitedError(AppError):
    code = "RATE_LIMITED"
    status_code = 429


class ConfigError(AppError):
    """A required config value (e.g. queryId placeholder) is still unset.

    Message must name the exact key in queries.yaml and state how to obtain it.
    """

    code = "MISSING_QUERY_ID"
    status_code = 500


class UnauthorizedError(AppError):
    code = "UNAUTHORIZED"
    status_code = 401


class InternalError(AppError):
    code = "INTERNAL"
    status_code = 500