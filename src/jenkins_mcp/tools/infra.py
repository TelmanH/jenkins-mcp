"""Infrastructure: node_health, get_node, get_queue."""

from typing import Annotated, Any
from urllib.parse import quote

from fastmcp.exceptions import ToolError
from pydantic import Field

from .. import config
from ..app import READ_ONLY, InstanceName, mcp
from ..client import Instance
from ..config import get_instance
from ..settings import JsonDict
from ..text import clip, fmt_age, mask_env_text, short_class, url_to_full_name
from ..xmlutil import parse_xml, text_of

GB = 1024 ** 3
_LAUNCHER_FIELDS = ("host", "port", "credentialsId", "webSocket", "tunnel")


def _monitors(md: JsonDict, disk_warn_gb: float) -> tuple[JsonDict, list[str]]:
    """Disk, temp, memory, response time, clock and architecture from monitorData, plus warnings."""
    item: JsonDict = {}
    warn: list[str] = []
    disk = (md.get("hudson.node_monitors.DiskSpaceMonitor") or {}).get("size")
    tmp = (md.get("hudson.node_monitors.TemporarySpaceMonitor") or {}).get("size")
    mem = md.get("hudson.node_monitors.SwapSpaceMonitor") or {}
    rt = (md.get("hudson.node_monitors.ResponseTimeMonitor") or {}).get("average")
    clock = (md.get("hudson.node_monitors.ClockMonitor") or {}).get("diff")
    if disk is not None and disk < disk_warn_gb * GB:
        warn.append(f"low disk {disk / GB:.1f} GB")
    if tmp is not None and tmp < 1 * GB:
        warn.append(f"low temp space {tmp / GB:.1f} GB")
    if rt is not None and rt > 5000:
        warn.append(f"slow response {rt} ms")
    if clock is not None and abs(clock) > 60000:
        warn.append(f"clock drift {clock / 1000:.0f}s")
    if disk is not None:
        item["disk_free_gb"] = round(disk / GB, 1)
    if mem.get("availablePhysicalMemory") is not None:
        item["mem_free_gb"] = round(mem["availablePhysicalMemory"] / GB, 1)
    if mem.get("availableSwapSpace") is not None:
        item["swap_free_gb"] = round(mem["availableSwapSpace"] / GB, 1)
    if rt is not None:
        item["response_ms"] = rt
    if md.get("hudson.node_monitors.ArchitectureMonitor"):
        item["arch"] = md["hudson.node_monitors.ArchitectureMonitor"]
    return item, warn


def _labels(c: JsonDict) -> list[str]:
    """Assigned labels without the node's own name (every node carries itself as a label)."""
    own = str(c.get("displayName") or c.get("name") or "")
    return [str(lb.get("name")) for lb in c.get("assignedLabels") or []
            if isinstance(lb, dict) and lb.get("name") and lb.get("name") != own]


@mcp.tool(annotations=READ_ONLY)
def node_health(instance: InstanceName = "", only_problems: bool = False) -> JsonDict:
    """Controller and agent status: online/offline (with reason), labels, executors, idle, free disk and
    temp space, free memory/swap, response time, clock drift, plus warnings. Static agents only;
    ephemeral Kubernetes agents appear only while they exist. Details of one node: get_node."""
    inst = get_instance(instance)
    disk_warn_gb = config.current().disk_warn_gb
    d = inst.json("/computer/api/json") or {}
    nodes, all_ok = [], True
    for c in d.get("computer") or []:
        offline, temp = bool(c.get("offline")), bool(c.get("temporarilyOffline"))
        monitors, warn = _monitors(c.get("monitorData") or {}, disk_warn_gb)
        if offline and not temp:
            warn.insert(0, "OFFLINE")
            all_ok = False
        elif temp:
            warn.insert(0, "temporarily offline (maintenance)")
        item: JsonDict = {"name": c.get("displayName"), "online": not offline,
                          "executors": c.get("numExecutors"), "idle": c.get("idle")}
        if labels := _labels(c):
            item["labels"] = labels
        if c.get("offlineCauseReason"):
            item["offline_reason"] = clip(c["offlineCauseReason"], 200)
        item.update(monitors)
        if warn:
            item["warnings"] = warn
        if not only_problems or warn:
            nodes.append(item)
    return {"instance": inst.name, "healthy": all_ok,
            "executors": f"{d.get('busyExecutors', '?')} busy / {d.get('totalExecutors', '?')} total",
            "nodes": nodes}


def _find_node(inst: Instance, name: str) -> JsonDict:
    d = inst.json("/computer/api/json", tree="computer[displayName]") or {}
    comps = [c for c in d.get("computer") or [] if isinstance(c, dict) and c.get("displayName")]
    key = name.strip().lower()
    exact = [c for c in comps if str(c["displayName"]).lower() == key]
    match = exact or [c for c in comps if key and key in str(c["displayName"]).lower()]
    if len(match) == 1:
        return match[0]
    names = ", ".join(str(c["displayName"]) for c in comps[:60])
    raise ToolError(f"[{inst.name}] {'Ambiguous' if match else 'Unknown'} node '{name}'. Nodes: {names}")


def _executor_rows(execs: list[Any], one_off: bool = False) -> list[str]:
    rows = []
    for i, e in enumerate(execs or []):
        ce = (e or {}).get("currentExecutable") if isinstance(e, dict) else None
        if ce:
            job = url_to_full_name(str(ce.get("url", ""))) or str(ce.get("fullDisplayName", "?"))
            prefix = "one-off" if one_off else f"#{i} busy"
            rows.append(f"{prefix}: {job} #{ce.get('number', '?')} (started {fmt_age(ce.get('timestamp'))})")
        elif not one_off:
            rows.append(f"#{i} idle")
    return rows


def _node_config(xml_text: str) -> JsonDict:
    root = parse_xml(xml_text)
    cfg: JsonDict = {}
    if remote := text_of(root, "remoteFS"):
        cfg["remote_root"] = remote
    if mode := text_of(root, "mode"):
        cfg["mode"] = {"NORMAL": "NORMAL (any job)",
                       "EXCLUSIVE": "EXCLUSIVE (only jobs with matching labels)"}.get(mode, mode)
    if (n := text_of(root, "numExecutors")):
        cfg["executors"] = n
    launcher = root.find("launcher")
    if launcher is not None:
        parts = [short_class(launcher.get("class"))]
        parts += [f"{f}={text_of(launcher, f)}" for f in _LAUNCHER_FIELDS if text_of(launcher, f)]
        cfg["launcher"] = " ".join(parts)
    retention = root.find("retentionStrategy")
    if retention is not None and retention.get("class"):
        cfg["availability"] = short_class(retention.get("class"))
    env: list[str] = []
    for prop in root.iter("hudson.slaves.EnvironmentVariablesNodeProperty"):
        values = [(s.text or "") for s in prop.iter("string")]
        env += mask_env_text("\n".join(f"{k}={v}" for k, v in zip(values[::2], values[1::2])))
    if env:
        cfg["env"] = env
    tools = [f"{text_of(loc, 'name')}: {text_of(loc, 'home')}"
             for loc in root.iter("hudson.tools.ToolLocationNodeProperty_-ToolLocation")]
    if tools:
        cfg["tools"] = tools
    return cfg


@mcp.tool(annotations=READ_ONLY)
def get_node(name: Annotated[str, Field(description="Node name as shown by node_health; a unique part is "
                                                    "enough, 'built-in' = the controller")],
             instance: InstanceName = "") -> JsonDict:
    """One node in detail. Live: online/offline (reason), labels, every executor with the build running
    on it, Java version (if a monitor exposes it), disk/memory/clock. Configuration (config.xml):
    remote root, mode (any job / matching labels only), launch method (e.g. SSH host), node environment
    variables (secrets masked) and tool locations. Without Computer/ExtendedRead permission only the
    live part is shown."""
    inst = get_instance(instance)
    c = _find_node(inst, name)
    display = str(c["displayName"])
    builtin = "built-in node" in display.lower() or display.lower() in ("master", "built-in")
    seg = "(built-in)" if builtin else quote(display, safe="")
    d = inst.json(f"/computer/{seg}/api/json?depth=1", allow_404=True)
    if d is None and builtin:  # Jenkins before 2.307 calls the controller "(master)"
        seg = "(master)"
        d = inst.json(f"/computer/{seg}/api/json?depth=1", allow_404=True)
    if d is None:
        raise ToolError(f"[{inst.name}] Node '{display}' disappeared (ephemeral agent?).")
    offline = bool(d.get("offline"))
    out: JsonDict = {"instance": inst.name, "name": d.get("displayName") or display, "online": not offline}
    if d.get("description"):
        out["description"] = clip(d["description"], 200)
    if d.get("temporarilyOffline"):
        out["temporarily_offline"] = True
    if d.get("offlineCauseReason"):
        out["offline_reason"] = clip(d["offlineCauseReason"], 300)
    out["labels"] = _labels(d)
    out["executors"] = _executor_rows(d.get("executors") or []) + _executor_rows(d.get("oneOffExecutors") or [], True)
    md = d.get("monitorData") or {}
    java = next((str(v) for k, v in md.items() if "jvmversion" in k.lower() and v), "")
    out["java"] = java or ("not exposed - needs the versioncolumn plugin (JVM version monitor), "
                           "or check Manage Jenkins > Nodes as admin")
    monitors, warn = _monitors(md, config.current().disk_warn_gb)
    out.update(monitors)
    if warn:
        out["warnings"] = warn
    if builtin:
        out["config"] = "the built-in node has no node config.xml (see the controller's system settings)"
        return out
    try:
        r = inst.get(f"/computer/{seg}/config.xml", allow_404=True)
        out["config"] = _node_config(r.text) if r is not None else "no config.xml (cloud/ephemeral agent?)"
    except ToolError as e:
        out["config"] = ("not readable: needs Computer/ExtendedRead (or Configure) permission - labels, executors "
                         "and monitors above come from the API" if "(403)" in str(e) else f"not readable: {clip(e, 200)}")
    return out


@mcp.tool(annotations=READ_ONLY)
def get_queue(instance: InstanceName = "", limit: int = 30) -> JsonDict:
    """Build queue: what is waiting, for how long, and why (e.g. 'Waiting for next available executor
    on label X'). Stuck items are flagged. Useful when builds 'hang' before starting."""
    inst = get_instance(instance)
    d = inst.json("/queue/api/json", tree="items[id,why,stuck,blocked,buildable,inQueueSince,task[name,url]]") or {}
    items = sorted(d.get("items") or [], key=lambda x: x.get("inQueueSince") or 0)
    rows = []
    for it in items[: max(1, min(limit, 200))]:
        task = it.get("task") or {}
        rows.append({"job": url_to_full_name(task.get("url", "")) or task.get("name"),
                     "waiting": fmt_age(it.get("inQueueSince")).replace(" ago", ""),
                     "why": clip(it.get("why") or "", 200), **({"stuck": True} if it.get("stuck") else {})})
    return {"instance": inst.name, "queued": len(items), "items": rows}
