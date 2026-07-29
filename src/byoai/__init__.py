"""ByoAI Runtime — the AI execution runtime for Python.

Bring your own infrastructure; ByoAI brings the runtime.
"""

from .context import RequestContext
from .errors import (
    AllProvidersFailed,
    ByoAIError,
    CacheError,
    ConfigurationError,
    FilterError,
    MiddlewareError,
    PipelineError,
    PipelineNotFound,
    ProviderError,
    RateLimitError,
    VectorStoreError,
)
from .middleware import Middleware
from .pipeline import FunctionStage, Pipeline, PipelineStage
from .runtime import Runtime
from .types import Document, ExecutionResult, Message, ProviderResponse, StreamChunk, Usage

__version__ = "0.1.0a1"

__all__ = [
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
    "PipelineNotFound",
    "MiddlewareError",
    "ProviderError",
    "RateLimitError",
    "AllProvidersFailed",
    "CacheError",
    "VectorStoreError",
    "FilterError",
]
