"""MCP tools, one module per topic. Importing this package registers all of them.

To add a tool: create a module here, decorate functions with @mcp.tool(annotations=READ_ONLY),
import the module below, add a test in tests/test_tools.py.
"""

from . import (artifacts, builds, config_history, infra, instances, jobconfig, jobs, knowledge,  # noqa: F401
               logs, pipeline, trends, workspace)
