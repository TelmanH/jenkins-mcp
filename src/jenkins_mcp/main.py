"""Assembles the server: importing the tool modules registers their tools on `mcp`."""

from . import prompts, tools  # noqa: F401  (registration side effects)
from .app import mcp


def run() -> None:
    mcp.run(show_banner=False)  # keep stderr clean in MCP client logs
