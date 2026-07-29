"""Shared helpers for the benchmark scripts."""

from __future__ import annotations


def percentile(sorted_values: list[float], q: float) -> float:
    """Percentile of an already-sorted list (nearest-rank)."""
    if not sorted_values:
        raise ValueError("no values")
    return sorted_values[min(int(len(sorted_values) * q), len(sorted_values) - 1)]
