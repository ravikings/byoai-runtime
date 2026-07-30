"""Typed error hierarchy for the ByoAI runtime.

Every error raised by the runtime derives from :class:`ByoAIError` so applications
can catch runtime failures without depending on provider SDK exception types.
"""

from __future__ import annotations


class ByoAIError(Exception):
    """Base class for all runtime errors."""


class ConfigurationError(ByoAIError):
    """Invalid or missing runtime configuration."""


class PipelineError(ByoAIError):
    """A pipeline stage failed while executing."""

    def __init__(self, message: str, *, stage: str | None = None) -> None:
        super().__init__(message)
        self.stage = stage


class PipelineNotFoundError(ByoAIError, LookupError):
    """A named pipeline was requested but never registered."""


class MiddlewareError(ByoAIError):
    """A middleware failed outside of normal short-circuiting."""


class ProviderError(ByoAIError):
    """Normalized provider failure.

    Attributes:
        provider: name of the provider adapter that raised.
        status_code: HTTP status from the provider, if any.
        retryable: whether the router may retry this call.
        retry_after: server-suggested delay in seconds, if the provider sent one.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


class RateLimitError(ProviderError):
    def __init__(self, message: str, *, provider: str, retry_after: float | None = None) -> None:
        super().__init__(
            message,
            provider=provider,
            status_code=429,
            retryable=True,
            retry_after=retry_after,
        )


class AllProvidersFailedError(ByoAIError):
    """Every provider in the fallback chain failed."""

    def __init__(self, message: str, errors: list[ProviderError]) -> None:
        super().__init__(message)
        self.errors = errors


class CacheError(ByoAIError):
    """A cache adapter failed. Cache failures should generally be non-fatal."""


class VectorStoreError(ByoAIError):
    """A vector store adapter failed."""


class FilterError(ByoAIError):
    """An AST filter expression is malformed or unsupported by the target dialect."""


# Pre-0.1 names, kept as aliases so existing `except PipelineNotFound` /
# `except AllProvidersFailed` code keeps working. New code should use the
# PEP 8 ``*Error`` names.
PipelineNotFound = PipelineNotFoundError
AllProvidersFailed = AllProvidersFailedError
