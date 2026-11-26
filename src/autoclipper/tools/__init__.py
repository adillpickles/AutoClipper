"""Managed external tool discovery and installation for AutoClipper."""

from autoclipper.tools.discovery import (
    TOOL_COMMANDS,
    TOOL_CONFIG_VARS,
    ConfiguredToolError,
    ResolvedTool,
    ToolDiscoveryError,
    ToolName,
    ToolSource,
    effective_tools_dir,
    executable_name,
    managed_bin_dir,
    managed_binary_path,
    resolve_all_tools,
    resolve_tool,
)

__all__ = [
    "TOOL_COMMANDS",
    "TOOL_CONFIG_VARS",
    "ConfiguredToolError",
    "ResolvedTool",
    "ToolDiscoveryError",
    "ToolName",
    "ToolSource",
    "effective_tools_dir",
    "executable_name",
    "managed_bin_dir",
    "managed_binary_path",
    "resolve_all_tools",
    "resolve_tool",
]
