"""MCP tool modules and the annotations they share.

Every tool calls Grubhub, so all are open-world. Per the MCP spec,
destructiveHint marks tools that may modify or remove existing state; tools
that only add things are marked non-destructive.
"""

from mcp.types import ToolAnnotations

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
ADDITIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)
