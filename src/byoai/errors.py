"""Typed error hierarchy for the ByoAI runtime.

Every error raised by the runtime derives from :class:`ByoAIError` so applications
can catch runtime failures without depending on provider SDK exception types.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from byoai.recorder.mandate import Deny


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


class CoriqoIdentityError(ByoAIError):
    """The Coriqo identity on this host could not be resolved or used."""


class EnforcementIdentityUnavailableError(CoriqoIdentityError):
    """An enforcement-capable identity was required, and none is configured.

    Raised when the only Coriqo identity available is a static API key (or
    nothing at all). Mandate enforcement decides what an agent may do, so it
    has to authenticate with the device key rather than a bearer secret that
    lives in the agent's own environment.
    """


class MandateDeniedError(ByoAIError):
    """A tool call was outside the agent's approved mandate, and did not run.

    Terminal and non-retryable **by construction**, not by convention. If a
    denial reaches the model as an ordinary tool error — worse, one naming the
    tool and the scope it missed — the model does the competent thing with a
    failure: rephrases, tries an adjacent tool, and routes around the control.
    So:

    * ``str(exc)`` is the fixed :data:`~byoai.recorder.mandate.MODEL_MESSAGE`
      and nothing else, because ``str(exc)`` is what agent frameworks put back
      into the model's context;
    * :attr:`operator_detail` — which tool, which mandate version, how stale,
      why — is for logs and never for the model;
    * :attr:`retryable` is ``False`` and there is no ``retry_after``, so the
      error cannot be mistaken for a :class:`ProviderError`-shaped transient.

    Retrying is pointless anyway: the same action against the same snapshot
    denies again.
    """

    #: Mirrors :attr:`ProviderError.retryable` so a router that branches on the
    #: attribute rather than the type sees the answer, not an ``AttributeError``.
    retryable = False

    #: False here, True on :class:`MandateRunHaltedError`. Present on both so a
    #: supervisor can ask "is the run over?" without importing the subclass.
    halted = False

    def __init__(self, verdict: Deny) -> None:
        # The *only* argument is the fixed sentence, so str(), repr() and any
        # framework that formats the exception all stay model-safe.
        super().__init__(verdict.model_message)
        self.verdict = verdict

    @property
    def model_message(self) -> str:
        """The one sentence this denial may put in front of a model."""
        return self.verdict.model_message

    @property
    def tool(self) -> str | None:
        return self.verdict.tool

    @property
    def operator_detail(self) -> str:
        """Everything an operator needs, and the model must never see."""
        parts = [f"reason={self.verdict.reason}", f"tool={self.verdict.tool!r}"]
        if self.verdict.mandate_version_id is not None:
            parts.append(f"mandate_version_id={self.verdict.mandate_version_id}")
        if self.verdict.snapshot_age_s is not None:
            parts.append(f"snapshot_age_s={self.verdict.snapshot_age_s:.1f}")
        parts.append(f"posture={self.verdict.posture}")
        if self.verdict.detail:
            parts.append(f"detail={self.verdict.detail}")
        return " ".join(parts)


class MandateRunHaltedError(MandateDeniedError):
    """The run was halted after repeated attempts at an already-denied tool.

    A subclass of :class:`MandateDeniedError` so every ``except
    MandateDeniedError`` already written keeps stopping the call, and so
    ``str(exc)`` is still the one fixed sentence — a model that has been
    grinding against a control learns nothing new at the moment it is cut off.

    The distinction is for the supervising loop, not the model. ``isinstance``
    (or :attr:`halted`) separates *this tool is refused, try something else*
    from *this run is over, stop scheduling turns for it*. :attr:`attempts`
    says how many times the tool was tried, and :attr:`run_id` which run was
    halted, so the caller can say so in a finding rather than in prose.
    """

    #: True on every instance. A caller branching on the attribute rather than
    #: the type sees the answer on an ordinary denial too, where it is False.
    halted = True

    def __init__(self, verdict: Deny, *, run_id: str, attempts: int) -> None:
        super().__init__(verdict)
        self.run_id = run_id
        self.attempts = attempts


# Pre-0.1 names, kept as aliases so existing `except PipelineNotFound` /
# `except AllProvidersFailed` code keeps working. New code should use the
# PEP 8 ``*Error`` names.
PipelineNotFound = PipelineNotFoundError
AllProvidersFailed = AllProvidersFailedError
