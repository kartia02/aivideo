"""Service-layer exceptions, mapped to HTTP responses in main.py."""

from __future__ import annotations


class ServiceError(Exception):
    """Base class for recoverable service failures."""

    status_code = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConfigurationError(ServiceError):
    """A required API key or setting is missing."""

    status_code = 503


class UpstreamError(ServiceError):
    """An upstream provider returned an error or an unusable payload."""

    status_code = 502


class TaskNotFoundError(ServiceError):
    status_code = 404


class TaskNotReadyError(ServiceError):
    """The video was requested before the task finished."""

    status_code = 409
