"""ByoAI Runtime — the AI execution runtime for Python.

Bring your own infrastructure; ByoAI brings the runtime.
"""

import logging as _logging

from ._version import __version__
from .context import RequestContext
from .errors import (
    AllProvidersFailed,
    AllProvidersFailedError,
    ByoAIError,
    CacheError,
    ConfigurationError,
    FilterError,
    MiddlewareError,
    PipelineError,
    PipelineNotFound,
    PipelineNotFoundError,
    ProviderError,
    RateLimitError,
    VectorStoreError,
)
from .middleware import Middleware
from .pipeline import FunctionStage, Pipeline, PipelineStage
from .runtime import Runtime
from .types import Document, ExecutionResult, Message, ProviderResponse, StreamChunk, Usage

__all__ = [
    "__version__",
    "Runtime",
    "RequestContext",
    "Pipeline",
    "PipelineStage",
    "FunctionStage",
    "Middleware",
    "Message",
    "Document",
    "Usage",
    "ExecutionResult",
    "ProviderResponse",
    "StreamChunk",
    "ByoAIError",
    "ConfigurationError",
    "PipelineError",
    "PipelineNotFoundError",
    "PipelineNotFound",  # deprecated alias
    "MiddlewareError",
    "ProviderError",
    "RateLimitError",
    "AllProvidersFailedError",
    "AllProvidersFailed",  # deprecated alias
    "CacheError",
    "VectorStoreError",
    "FilterError",
]

# Library convention: never configure logging for the application, but make
# sure "no handler" noise can't appear either. Applications opt in with
# logging.getLogger("byoai").setLevel(...).
_logging.getLogger(__name__).addHandler(_logging.NullHandler())
