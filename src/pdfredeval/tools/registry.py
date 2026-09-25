"""Adapter registry, keyed `vendor:surface`.

A vendor's web UI and API routinely run different pipelines, defaults and model versions,
so they are separate entries and their scores are never merged. Out-of-tree adapters are
discovered through entry points, so a vendor can ship and maintain their own without a PR
here. See docs/adapters.md.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import metadata
from typing import Any, TypeVar

from ..errors import DuplicateToolError, UnknownToolError
from ..types import Surface, Transport
from .base import Tool, parse_tool_id

ENTRY_POINT_GROUP = "pdfredeval.tools"

_REGISTRY: dict[str, type[Tool]] = {}
_PLUGINS_LOADED = False

T = TypeVar("T", bound=type[Tool])


def register(cls: T | None = None, *, replace: bool = False) -> T | Callable[[T], T]:
    """Class decorator. Reads the id from `cls.tool_id`.

    Usable bare (`@register`) or called (`@register(replace=True)`).
    """

    def _apply(target: T) -> T:
        tool_id = getattr(target, "tool_id", "")
        parse_tool_id(tool_id)  # validates, raises InvalidToolIdError
        existing = _REGISTRY.get(tool_id)
        if existing is not None and not replace:
            raise DuplicateToolError(
                f"{tool_id!r} is already registered to "
                f"{existing.__module__}.{existing.__qualname__}. Pass replace=True to "
                f"override deliberately."
            )
        _REGISTRY[tool_id] = target
        return target

    return _apply if cls is None else _apply(cls)


def unregister(tool_id: str) -> None:
    _REGISTRY.pop(tool_id, None)


def get(tool_id: str, *, load_plugins: bool = True) -> type[Tool]:
    """The adapter class for an id."""
    if tool_id not in _REGISTRY and load_plugins:
        load_entry_points()
    try:
        return _REGISTRY[tool_id]
    except KeyError:
        known = ", ".join(available()) or "none registered"
        raise UnknownToolError(f"no adapter for {tool_id!r}. Known: {known}") from None


def create(tool_id: str, **kwargs: Any) -> Tool:
    """Instantiate an adapter by id."""
    return get(tool_id)(**kwargs)


def available(
    *,
    vendor: str | None = None,
    surface: Surface | str | None = None,
    transport: Transport | str | None = None,
) -> list[str]:
    """Registered ids, optionally filtered. Sorted, so output is stable."""
    if surface is not None:
        surface = Surface(surface)
    if transport is not None:
        transport = Transport(transport)
    out = []
    for tool_id, cls in _REGISTRY.items():
        this_vendor, this_surface = parse_tool_id(tool_id)
        if vendor is not None and this_vendor != vendor:
            continue
        if surface is not None and this_surface is not surface:
            continue
        if transport is not None and cls.transport is not transport:
            continue
        out.append(tool_id)
    return sorted(out)


def vendors() -> list[str]:
    return sorted({parse_tool_id(t)[0] for t in _REGISTRY})


def surfaces_of(vendor: str) -> list[str]:
    """Every surface registered for one vendor.

    More than one means their scores must be reported separately - and if they agree,
    that is itself a finding.
    """
    return available(vendor=vendor)


def load_entry_points(*, force: bool = False) -> list[str]:
    """Import out-of-tree adapters advertised under the entry-point group.

    A plugin that fails to import is skipped rather than fatal: one broken third-party
    adapter must not take down a benchmark run over the others.
    """
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED and not force:
        return []
    _PLUGINS_LOADED = True
    loaded: list[str] = []
    try:
        entries = metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # pragma: no cover - importlib backport differences
        return loaded
    for entry in entries:
        try:
            obj = entry.load()
        except Exception:
            continue
        if isinstance(obj, type) and issubclass(obj, Tool):
            try:
                register(obj)
            except DuplicateToolError:
                pass
        loaded.append(entry.name)
    return loaded


def snapshot() -> dict[str, str]:
    """id -> implementing class path. For a run report, so results name their adapter."""
    return {
        tool_id: f"{cls.__module__}.{cls.__qualname__}"
        for tool_id, cls in sorted(_REGISTRY.items())
    }
