"""Workspace files that were not archived: list_workspace, read_workspace_file.

Freestyle (and other classic) jobs have one job workspace (/job/x/ws/). A Pipeline build has one workspace
per `node` block, linked from the build's "Workspaces" page (/job/x/<n>/ws/ -> execution/node/<id>/ws/).
Either way it is the CURRENT content on the agent, not a snapshot of the build.
"""

import html
import re
from typing import Annotated
from urllib.parse import quote

import requests
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..app import READ_ONLY, Build, InstanceName, Job, mcp
from ..settings import JsonDict
from ..targets import Target, build_info, resolve
from .artifacts import _ARTIFACT_CAP, render_text, stream_bytes, text_lines

# <li><a href=".../execution/node/3/ws/"><code>/path</code></a> on agent</li> (Jenkins 2.5xx)
_NODE_LINK = re.compile(r'href="[^"]*execution/node/(\d+)/ws/?"[^>]*>(.*?)</li>', re.S)
_TAG = re.compile(r"<[^>]+>")
CURRENT_STATE = ("this is the CURRENT content of the workspace on the agent; a later build may have changed or "
                 "cleaned it. Files kept with this exact build: list_artifacts")
NodeId = Annotated[str, Field(description="Pipeline only: workspace of which node block (node_id from "
                                          "list_workspace); empty = the only one")]


def _get(t: Target, url: str, what: str, stream: bool = False) -> requests.Response:
    try:
        r = t.inst.get(url, allow_404=True, stream=stream)
    except ToolError as e:
        if "(403)" in str(e):
            raise ToolError(f"[{t.inst.name}] {what} is not readable (403): the token's user needs the "
                            "Job/Workspace permission.") from None
        raise
    if r is None:
        raise ToolError(f"[{t.inst.name}] {what} not found: the agent may be offline or removed (ephemeral agent), "
                        "the workspace may have been cleaned (cleanWs / deleteDir), or the path does not exist.")
    return r


def _node_blocks(t: Target, number: int) -> list[tuple[str, str]]:
    r = _get(t, f"{t.path}/{number}/ws/", f"Workspaces of build #{number}")
    return [(nid, " ".join(html.unescape(_TAG.sub(" ", desc)).split())) for nid, desc in _NODE_LINK.findall(r.text)]


def _workspace(t: Target, node_id: str) -> tuple[str | None, JsonDict]:
    """(base path of the workspace or None when a node block must be chosen, context for the answer)."""
    cls = str((t.inst.json(f"{t.path}/api/json", tree="_class") or {}).get("_class", ""))
    ctx: JsonDict = {"instance": t.inst.name, "job": t.full_name}
    if "WorkflowJob" not in cls:
        return f"{t.path}/ws", ctx
    number = int(build_info(t, light=True)["number"])
    ctx["build"] = number
    blocks = _node_blocks(t, number)
    rows = [f"node_id={nid}: {desc}" for nid, desc in blocks]
    if not blocks:
        raise ToolError(f"[{t.inst.name}] Build #{number} lists no workspace (it never ran on an agent, or the "
                        "workspace page has an unknown format).")
    if node_id.strip():
        if node_id.strip() not in [nid for nid, _ in blocks]:
            raise ToolError(f"Unknown node_id '{node_id}'. Workspaces of build #{number}: {'; '.join(rows)}")
        chosen = node_id.strip()
    elif len(blocks) == 1:
        chosen = blocks[0][0]
    else:
        ctx["workspaces"] = rows
        return None, ctx
    ctx["node_id"] = chosen
    return f"{t.path}/{number}/execution/node/{chosen}/ws", ctx


@mcp.tool(annotations=READ_ONLY)
def list_workspace(job: Job, build: Build = "", instance: InstanceName = "", node_id: NodeId = "",
                   path: Annotated[str, Field(description="Folder inside the workspace; empty = root")] = "") -> JsonDict:
    """Files in a job's workspace (folders end with '/'), for files that were not archived (e.g. app-queue.log).
    Pipeline: the workspace of a `node` block of the build; with several blocks it lists them first.
    Shows the CURRENT state on the agent. Needs the Job/Workspace permission and a reachable agent."""
    t = resolve(job, build, instance)
    base, out = _workspace(t, node_id)
    if base is None:
        out["next_step"] = "several node blocks - call again with node_id=..."
        return out
    folder = path.strip("/")
    r = _get(t, f"{base}/{quote(folder + '/', safe='/') if folder else ''}*plain*",
             f"Workspace folder '{folder or '/'}'")
    entries = [x for x in r.text.splitlines() if x.strip()]
    out.update({"path": folder or "/", "count": len(entries), "entries": entries[:300], "note": CURRENT_STATE})
    if len(entries) > 300:
        out["more"] = f"{len(entries) - 300} more entries - list a subfolder"
    return out


@mcp.tool(annotations=READ_ONLY)
def read_workspace_file(job: Job, path: Annotated[str, Field(description="File path inside the workspace")],
                        build: Build = "", instance: InstanceName = "", node_id: NodeId = "", pattern: str = "",
                        start: int = 1, count: int = 200) -> str:
    """Read a text file from a job's workspace (e.g. a log the build did not archive). With `pattern` it
    greps (regex, case-insensitive); otherwise `count` lines from `start` (0 = tail). UTF-8, UTF-16 and
    cp1252 are decoded, binary files refused, secrets masked. Shows the CURRENT file on the agent."""
    t = resolve(job, build, instance)
    base, ctx = _workspace(t, node_id)
    if base is None:
        raise ToolError(f"Build #{ctx['build']} has several node blocks, pass node_id: {'; '.join(ctx['workspaces'])}")
    clean = path.strip("/")
    r = _get(t, f"{base}/{quote(clean, safe='/')}", f"Workspace file '{clean}'", stream=True)
    is_html = "text/html" in r.headers.get("Content-Type", "")
    data, truncated = stream_bytes(r, _ARTIFACT_CAP)
    if is_html and b"*zip*" in data[:65536]:
        raise ToolError(f"'{clean}' is a directory - use list_workspace(path='{clean}').")
    lines, encoding = text_lines(data, f"Workspace file '{clean}'")
    where = f"workspace of {t.full_name} on {t.inst.name}" + (
        f" build #{ctx['build']} node_id={ctx['node_id']}" if "build" in ctx else "")
    return render_text(clean, lines, f"{where} (current state on the agent)", truncated, encoding, pattern, start, count)
