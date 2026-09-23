"""Exchange error types."""

from __future__ import annotations


class IndodaxError(Exception):
    """Base class for all Indodax client errors."""


class IndodaxAPIError(IndodaxError):
    """The API answered with an error payload or a non-retryable HTTP status."""

    def __init__(self, message: str, *, status: int | None = None, code: str | int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.code is not None:
            parts.append(f"code={self.code}")
        return " ".join(parts)


class IndodaxRateLimitError(IndodaxAPIError):
    """HTTP 429 / too_many_requests after retries were exhausted."""


class IndodaxNetworkError(IndodaxError):
    """Transport-level failure (timeout, DNS, connection reset) after retries."""


class IndodaxResponseFormatError(IndodaxError):
    """The response did not have the shape documented by Indodax."""
