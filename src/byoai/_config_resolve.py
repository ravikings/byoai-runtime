"""Shared resolver for ``metric=``/``selection=``-style config knobs: a
preset name looked up in a registry, or a bare callable used as-is.

Callers check ``callable(value)`` themselves before falling back to this —
what to do with a bare callable differs per knob (e.g. pairing it with a
fixed normalize step) — so this only covers the "resolve a preset name, or
raise a consistently-worded ConfigurationError" half shared by every one of
these knobs.
"""

from __future__ import annotations

from typing import TypeVar

from .errors import ConfigurationError

T = TypeVar("T")


def resolve_preset(value: str, presets: dict[str, T], *, kind: str, callable_signature: str) -> T:
    if isinstance(value, str) and value in presets:
        return presets[value]
    raise ConfigurationError(
        f"unknown {kind} {value!r} (expected one of {sorted(presets)} "
        f"or a callable{callable_signature})"
    )
