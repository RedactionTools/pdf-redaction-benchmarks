"""Tool adapters: the base contract, the two transports, and the registry."""

from .api import ApiTool, Throttle
from .base import Handle, RunResult, Tool, parse_tool_id, sha256
from .manual import ManualTool
from .registry import (
    ENTRY_POINT_GROUP,
    available,
    create,
    get,
    load_entry_points,
    register,
    snapshot,
    surfaces_of,
    unregister,
    vendors,
)

__all__ = [
    "ApiTool",
    "ENTRY_POINT_GROUP",
    "Handle",
    "ManualTool",
    "RunResult",
    "Throttle",
    "Tool",
    "available",
    "create",
    "get",
    "load_entry_points",
    "parse_tool_id",
    "register",
    "sha256",
    "snapshot",
    "surfaces_of",
    "unregister",
    "vendors",
]
