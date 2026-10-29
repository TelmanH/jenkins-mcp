"""list_instances: which Jenkins instances are configured and reachable."""

from concurrent.futures import ThreadPoolExecutor

from fastmcp.exceptions import ToolError

from .. import config
from ..app import READ_ONLY, mcp
from ..client import Instance
from ..kb import store as kb
from ..settings import JsonDict
from ..text import mask


def _connection(inst: Instance, item: JsonDict) -> None:
    """Version, quiet-down mode, the token's user/groups and whether it has admin rights."""
    try:
        r = inst.get("/api/json", params={"tree": "mode,quietingDown"})
    except ToolError as e:
        item["status"] = f"error: {e}"
        return
    item["status"] = f"ok, Jenkins {r.headers.get('X-Jenkins', '?')}" if r is not None else "?"
    try:
        root = r.json() if r is not None else {}
    except ValueError:
        root = {}
    if root.get("quietingDown"):
        item["quieting_down"] = "yes - Jenkins is preparing for shutdown, new builds do not start"
    try:
        me = inst.json("/whoAmI/api/json", allow_404=True)
    except ToolError:
        me = None
    if me:
        groups = [str(a) for a in me.get("authorities") or [] if a != "authenticated"]
        item["user"] = str(me.get("name")) + (f" (groups: {', '.join(groups[:10])})" if groups else "")
    else:
        item["user"] = "unknown (whoAmI not available)"
    if inst.probe("/manage/") == 200:
        item["admin_rights"] = ("this token can administer Jenkins; this server still sends GET/HEAD only "
                                "and cannot change anything")


@mcp.tool(annotations=READ_ONLY)
def list_instances(check_connection: bool = False, reload_config: bool = False) -> JsonDict:
    """List configured Jenkins instances (name, url, description, default, where the token comes from).
    check_connection=True also pings each instance and reports the Jenkins version or the error, the
    token's user and groups, whether it has admin rights, and quiet-down (shutdown) mode.
    reload_config=True re-reads config/jenkins_instances.yaml and config/.env without restarting the server.
    Also reports configuration problems and the state of the knowledge base."""
    settings = config.reload() if reload_config else config.current()

    def describe(inst: Instance) -> JsonDict:
        item: JsonDict = {"name": inst.name, "url": inst.url, "default": inst.name == settings.default,
                          "token": config.token_source(inst.token_env)}
        if inst.description:
            item["description"] = inst.description
        if inst.proxy:
            item["proxy"] = "direct (no proxy)" if inst.proxy.lower() == "none" else mask(inst.proxy)
        if not inst.token:
            item["warning"] = f"{inst.token_env} is not set"
        if check_connection:
            _connection(inst, item)
        return item

    with ThreadPoolExecutor(max_workers=8) as pool:
        out = list(pool.map(describe, settings.instances.values()))
    result: JsonDict = {"config": settings.source, "instances": out}
    if settings.errors:
        result["config_errors"] = settings.errors
    base = kb.get()
    result["knowledge_base"] = {"dir": str(base.dir), "entries": len(base.entries), "drafts": len(base.drafts)}
    if base.errors:
        result["knowledge_base"]["errors"] = base.errors
    return result
