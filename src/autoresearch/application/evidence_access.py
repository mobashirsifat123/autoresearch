"""Mapping/attribute access for evidence records.

The repository returns sqlite rows as ``dict`` objects, while tests and some
call sites use dataclasses. The publication gate must read the same fields
from both shapes; using ``getattr`` on a dict silently returns defaults and
can turn a well-powered real study into "no baselines / no power".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def field(value: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a mapping or an object, never raising on absence."""
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)
