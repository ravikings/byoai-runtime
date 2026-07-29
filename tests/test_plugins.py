from __future__ import annotations

import pytest

from byoai.config import build_cache, build_provider, build_vector_store
from byoai.errors import ConfigurationError


class FakeEntryPoint:
    def __init__(self, name, factory):
        self.name = name
        self._factory = factory

    def load(self):
        return self._factory


def test_unknown_provider_without_plugin_raises(monkeypatch):
    # isolate from any plugins actually installed in the environment
    monkeypatch.setattr("importlib.metadata.entry_points", lambda *, group: [])
    with pytest.raises(ConfigurationError):
        build_provider({"provider": "no-such-thing", "model": "m"})
    with pytest.raises(ConfigurationError):
        build_cache({"provider": "no-such-thing"})
    with pytest.raises(ConfigurationError):
        build_vector_store({"provider": "no-such-thing"})


def test_plugin_entry_point_resolves(monkeypatch):
    built = {}

    class PluginProvider:
        name = "custom"
        model = "custom-1"

        async def complete(self, messages, **options): ...
        async def stream(self, messages, **options): ...
        async def close(self): ...

    def factory(config):
        built.update(config)
        return PluginProvider()

    def fake_entry_points(*, group):
        if group == "byoai.providers":
            return [FakeEntryPoint("my_custom_llm", factory)]
        return []

    import byoai.config as config_module

    monkeypatch.setattr(
        "importlib.metadata.entry_points", fake_entry_points
    )
    provider = config_module.build_provider(
        {"provider": "my_custom_llm", "model": "custom-1", "region": "eu"}
    )
    assert isinstance(provider, PluginProvider)
    assert built == {"model": "custom-1", "region": "eu"}
