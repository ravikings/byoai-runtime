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


def build_cache(config: dict[str, Any]) -> CacheStore:
    config = dict(config)
    provider = config.pop("provider", "memory")
    if provider in ("redis", "valkey"):
        from .cache.redis import RedisCache

        return RedisCache(**config)
    if provider in ("memory", "inmemory"):
        config.pop("url", None)
        return MemoryCache(**config)
    raise ConfigurationError(f"unknown cache provider {provider!r}")


def build_vector_store(config: dict[str, Any]) -> VectorStore:
    config = dict(config)
    provider = config.pop("provider", None)
    if not provider:
        raise ConfigurationError("vector_store config requires a 'provider' key")
    if provider in ("pgvector", "postgres", "postgresql"):
        from .vector.pgvector import PgVectorStore

        return PgVectorStore(**config)
    raise ConfigurationError(
        f"unknown vector provider {provider!r} (MVP supports: pgvector; see ROADMAP Phase 6)"
    )


def build_provider(config: dict[str, Any]) -> LLMProvider:
    config = dict(config)
    config.pop("fallback", None)
    provider = config.pop("provider", None)
    if not provider:
        raise ConfigurationError("llm config requires a 'provider' key")

    if provider == "anthropic":
        from .providers.anthropic import AnthropicProvider

        return AnthropicProvider(name=provider, **config)

    from .providers.openai_compat import OpenAICompatProvider

    if provider == "openai":
        return OpenAICompatProvider(name="openai", **config)
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
    if provider == "ollama":
        config.setdefault("base_url", "http://localhost:11434/v1")
        config.setdefault("api_key", "ollama")
        return OpenAICompatProvider(name="ollama", **config)
    if provider == "openrouter":
        config.setdefault("base_url", "https://openrouter.ai/api/v1")
        config.setdefault("api_key", os.environ.get("OPENROUTER_API_KEY"))
        return OpenAICompatProvider(name="openrouter", **config)
    if provider in ("openai_compatible", "vllm", "litellm"):
        if "base_url" not in config:
            raise ConfigurationError(f"{provider} requires 'base_url'")
        return OpenAICompatProvider(name=provider, **config)

    raise ConfigurationError(f"unknown llm provider {provider!r}")


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
