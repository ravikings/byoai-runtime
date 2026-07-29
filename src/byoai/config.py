"""Dict-based configuration → adapter instances.

Lets applications configure the runtime declaratively (12-factor style, easy to
load from env/JSON/YAML) while power users construct adapter objects directly.

    Runtime(
        llm={"provider": "openai", "model": "gpt-4o",
             "fallback": {"provider": "ollama", "model": "llama3.1"}},
        cache={"provider": "redis", "url": "redis://redis.internal:6379"},
        vector_store={"provider": "pgvector", "dsn": "...", "table": "embeddings"},
    )
"""

from __future__ import annotations

import os
from typing import Any

from .cache.base import CacheStore
from .cache.memory import MemoryCache
from .errors import ConfigurationError
from .providers.base import LLMProvider
from .vector.base import VectorStore


def _load_plugin(group: str, name: str, config: dict[str, Any]) -> Any:
    """Resolve an unknown provider name via entry points (``pip install
    byoai-<something>`` packages register factories under ``byoai.*`` groups).

    Returns the built instance, or None if no plugin claims the name.
    """
    from importlib.metadata import entry_points

    for entry_point in entry_points(group=group):
        if entry_point.name == name:
            factory = entry_point.load()
            return factory(config)
    return None


def build_cache(config: dict[str, Any]) -> CacheStore:
    config = dict(config)
    provider = config.pop("provider", "memory")
    if provider in ("redis", "valkey"):
        from .cache.redis import RedisCache

        return RedisCache(**config)
    if provider in ("memory", "inmemory"):
        config.pop("url", None)
        return MemoryCache(**config)
    plugin = _load_plugin("byoai.caches", provider, config)
    if plugin is not None:
        return plugin
    raise ConfigurationError(f"unknown cache provider {provider!r}")


def build_vector_store(config: dict[str, Any]) -> VectorStore:
    config = dict(config)
    provider = config.pop("provider", None)
    if not provider:
        raise ConfigurationError("vector_store config requires a 'provider' key")
    if provider in ("pgvector", "postgres", "postgresql"):
        from .vector.pgvector import PgVectorStore

        return PgVectorStore(**config)
    if provider == "qdrant":
        from .vector.qdrant import QdrantVectorStore

        return QdrantVectorStore(**config)
    if provider == "pinecone":
        from .vector.pinecone import PineconeVectorStore

        return PineconeVectorStore(**config)
    plugin = _load_plugin("byoai.vector_stores", provider, config)
    if plugin is not None:
        return plugin
    raise ConfigurationError(f"unknown vector provider {provider!r}")


def build_semantic_cache(config: dict[str, Any]) -> Any:
    config = dict(config)
    config.pop("threshold", None)  # consumed by the stage, not the store
    provider = config.pop("provider", "memory")
    if provider in ("memory", "inmemory"):
        from .cache.semantic import MemorySemanticCache

        return MemorySemanticCache(**config)
    plugin = _load_plugin("byoai.semantic_caches", provider, config)
    if plugin is not None:
        return plugin
    raise ConfigurationError(f"unknown semantic cache provider {provider!r}")


def _apply_openai_compat_defaults(provider: str, config: dict[str, Any]) -> bool:
    """Shared endpoint defaults for the OpenAI-compatible family. Returns
    True when ``provider`` belongs to the family (config mutated in place)."""
    if provider == "openai":
        return True
    if provider == "ollama":
        config.setdefault("base_url", "http://localhost:11434/v1")
        config.setdefault("api_key", "ollama")
        return True
    if provider in ("openai_compatible", "vllm", "litellm"):
        if "base_url" not in config:
            raise ConfigurationError(f"{provider} requires 'base_url'")
        return True
    return False


def build_embedder(config: dict[str, Any]) -> Any:
    config = dict(config)
    provider = config.pop("provider", "openai")
    if _apply_openai_compat_defaults(provider, config):
        from .providers.embeddings import OpenAICompatEmbedder

        return OpenAICompatEmbedder(name=provider, **config)
    plugin = _load_plugin("byoai.embedders", provider, config)
    if plugin is not None:
        return plugin
    raise ConfigurationError(f"unknown embedder provider {provider!r}")


def build_provider(config: dict[str, Any]) -> LLMProvider:
    config = dict(config)
    config.pop("fallback", None)
    provider = config.pop("provider", None)
    if not provider:
        raise ConfigurationError("llm config requires a 'provider' key")

    if provider == "anthropic":
        from .providers.anthropic import AnthropicProvider

        return AnthropicProvider(name=provider, **config)
    if provider == "gemini":
        from .providers.gemini import GeminiProvider

        return GeminiProvider(name=provider, **config)

    from .providers.openai_compat import OpenAICompatProvider

    if _apply_openai_compat_defaults(provider, config):
        return OpenAICompatProvider(name=provider, **config)
    if provider == "azure_openai":
        # Azure's deployment-scoped URL; api key travels in the api-key header.
        deployment = config.pop("deployment", None) or config.get("model")
        endpoint = config.pop("endpoint", None) or os.environ.get("AZURE_OPENAI_ENDPOINT")
        api_version = config.pop("api_version", "2024-06-01")
        if not endpoint or not deployment:
            raise ConfigurationError("azure_openai requires 'endpoint' and 'deployment'")
        api_key = config.pop("api_key", None) or os.environ.get("AZURE_OPENAI_API_KEY", "")
        config.setdefault("model", deployment)
        return OpenAICompatProvider(
            name="azure_openai",
            base_url=f"{endpoint.rstrip('/')}/openai/deployments/{deployment}"
            f"?api-version={api_version}",
            api_key=None,
            default_headers={"api-key": api_key},
            **config,
        )
    if provider == "openrouter":
        config.setdefault("base_url", "https://openrouter.ai/api/v1")
        config.setdefault("api_key", os.environ.get("OPENROUTER_API_KEY"))
        return OpenAICompatProvider(name="openrouter", **config)

    plugin = _load_plugin("byoai.providers", provider, config)
    if plugin is not None:
        return plugin
    raise ConfigurationError(f"unknown llm provider {provider!r}")


_UNSET = object()


def configure_telemetry(runtime: Any, config: Any) -> Any:
    """Wire tracing onto a runtime from declarative config.

    ``{"provider": "opentelemetry", "endpoint": "...", "service_name": "..."}``
    creates an OTLP exporter to that collector; omit ``endpoint`` to use the
    process's globally configured OpenTelemetry SDK. A non-dict value is
    treated as an already-built TracerProvider (mirroring how ``cache=`` and
    ``vector_store=`` accept pre-built instances).

    Returns the TracerProvider *this call created* (for the runtime to shut
    down on close), or None when the caller owns the provider's lifecycle.
    """
    try:
        from .telemetry.otel import configure_otlp, instrument
    except ImportError as exc:
        raise ConfigurationError(
            "telemetry requires OpenTelemetry: pip install 'byoai-runtime[otel]'"
        ) from exc

    if not isinstance(config, dict):
        instrument(runtime, tracer_provider=config)
        return None

    config = dict(config)
    provider = config.pop("provider", "opentelemetry")
    if provider not in ("opentelemetry", "otel"):
        raise ConfigurationError(f"unknown telemetry provider {provider!r}")
    tracer_provider = config.pop("tracer_provider", None)
    endpoint = config.pop("endpoint", _UNSET)
    owned_provider = None
    if tracer_provider is None and endpoint is not _UNSET:
        if not endpoint:
            raise ConfigurationError(
                "telemetry 'endpoint' is empty — set a collector URL or omit the key "
                "to use the globally configured OpenTelemetry SDK"
            )
        tracer_provider = owned_provider = configure_otlp(endpoint=endpoint, **config)
    instrument(runtime, tracer_provider=tracer_provider)
    return owned_provider


def build_router(config: dict[str, Any]) -> list[LLMProvider]:
    """Flatten a primary + nested ``fallback`` chain into an ordered provider list."""
    providers: list[LLMProvider] = []
    current: dict[str, Any] | None = config
    seen = 0
    while current is not None:
        providers.append(build_provider(current))
        current = current.get("fallback")
        seen += 1
        if seen > 10:
            raise ConfigurationError("llm fallback chain too deep (>10)")
    return providers
