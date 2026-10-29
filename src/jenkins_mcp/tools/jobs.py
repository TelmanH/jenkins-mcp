"""Jobs: list_jobs, failures_overview, recent_builds, find_jobs_by_scm, get_job."""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from pydantic import Field

from .. import config
from ..app import READ_ONLY, InstanceName, Job, mcp
from ..client import Instance
from ..config import get_instance
from ..settings import JOB_CACHE_S, JsonDict
from ..targets import job_path, resolve
from ..text import clip, fmt_age, fmt_duration, is_secret_name, mask, short_class

# lastStableBuild (not lastSuccessfulBuild): Jenkins counts UNSTABLE builds as "successful"
_JOB_FIELDS = ("name,_class,color,lastBuild[number,result,timestamp,building,duration,estimatedDuration],"
               "lastStableBuild[number,timestamp]")
_BUILD_FIELDS = "number,result,timestamp,duration,building,estimatedDuration,culprits[fullName]"
LONG_RUNNING = 1.5  # a running build is flagged above this multiple of its estimated duration


def _jobs_tree(depth: int, builds: int = 0, scm: bool = False) -> str:
    fields = _JOB_FIELDS.replace("estimatedDuration]", "estimatedDuration,actions[remoteUrls]]") if scm else _JOB_FIELDS
    fields += f",builds[{_BUILD_FIELDS}]{{0,{builds}}}" if builds else ""
    tree = f"jobs[{fields}]"
    for _ in range(max(1, depth) - 1):
        tree = f"jobs[{fields},{tree}]"
    return tree


def _is_folder(item: JsonDict) -> bool:
    cls = str(item.get("_class", ""))
    return any(m in cls for m in ("Folder", "MultiBranchProject", "OrganizationFolder")) or (
        "jobs" in item and "color" not in item)


def _job_status(item: JsonDict) -> str:
    color = str(item.get("color") or "")
    lb = item.get("lastBuild") or {}
    if color.startswith("disabled"):
        return "DISABLED"
    if not lb:
        return "NOT_BUILT"
    if lb.get("building"):
        return "BUILDING"
    return str(lb.get("result") or "UNKNOWN")


_TREE_CACHE: dict[tuple[str, str, str, str], tuple[float, Any]] = {}
_TREE_LOCK = threading.Lock()


def _cached_tree(inst: Instance, path: str, tree: str) -> Any:
    """Folder tree JSON, reused for JOB_CACHE_S seconds: list_jobs, failures_overview & co. in one
    conversation do not walk a large instance again and again."""
    key = (inst.name, inst.url, path, tree)
    now = time.monotonic()
    with _TREE_LOCK:
        hit = _TREE_CACHE.get(key)
        if hit and now - hit[0] < JOB_CACHE_S:
            return hit[1]
    data = inst.json(path, tree=tree, allow_404=True)
    if JOB_CACHE_S > 0:
        with _TREE_LOCK:
            _TREE_CACHE[key] = (now, data)
            for k in [k for k, (ts, _) in _TREE_CACHE.items() if now - ts >= JOB_CACHE_S]:
                del _TREE_CACHE[k]
    return data


def walk_jobs(inst: Instance, folder_parts: list[str], depth: int = 5,
              max_requests: int = 150, builds: int = 0, scm: bool = False) -> tuple[list[JsonDict], bool]:
    """All jobs below a folder with ~1 request per instance (nested `tree`); folders deeper than
    `depth` are fetched separately. builds > 0 also returns the newest `builds` builds per job,
    scm=True the repository URLs the last build checked out. Returns (rows, truncated)."""
    rows: list[JsonDict] = []
    state: dict[str, int] = {"requests": 0, "truncated": 0}

    def visit(items: list[JsonDict], parts: list[str]) -> None:
        for it in items or []:
            name = it.get("name")
            if not isinstance(name, str) or not name:
                continue
            p = [*parts, name]
            full = "/".join(p)
            if name in inst.exclude_folders or full in inst.exclude_folders:
                continue
            if _is_folder(it):
                if "jobs" in it:
                    visit(it.get("jobs") or [], p)
                else:
                    fetch(p)
                continue
            lb = it.get("lastBuild") or {}
            stable = it.get("lastStableBuild") or {}
            rows.append({"job": full, "status": _job_status(it), "number": lb.get("number"),
                         "ts": lb.get("timestamp"), "duration": lb.get("duration"),
                         "est": lb.get("estimatedDuration"), "stable": stable.get("number"),
                         "stable_ts": stable.get("timestamp"),
                         "builds": [b for b in it.get("builds") or [] if isinstance(b, dict)],
                         "repos": [str(u) for a in lb.get("actions") or [] if isinstance(a, dict)
                                   for u in a.get("remoteUrls") or []]})

    def fetch(parts: list[str], top: bool = False) -> None:
        state["requests"] += 1
        if state["requests"] > max_requests:
            state["truncated"] = 1
            return
        d = _cached_tree(inst, f"{job_path(parts)}/api/json", _jobs_tree(depth, builds, scm))
        if d is None:
            if top:
                raise ToolError(f"[{inst.name}] Folder '{'/'.join(parts)}' not found.")
            return
        visit(d.get("jobs") or [], parts)

    fetch(folder_parts, top=True)
    return rows, bool(state["truncated"])


def _overdue(ts: Any, est: Any) -> str:
    """'running 3m 0s, usual 20s - longer than usual' for a build running past LONG_RUNNING x its estimate."""
    if not ts or not est or est <= 0:
        return ""
    running = time.time() * 1000 - ts
    if running <= LONG_RUNNING * est:
        return ""
    return f"running {fmt_duration(running)}, usual {fmt_duration(est)} - longer than usual"


def _row_str(r: JsonDict) -> str:
    out = f"{r['job']} | {r['status']}"
    if r.get("number"):
        out += f" #{r['number']} | {fmt_age(r.get('ts'))}"
    if r["status"] in ("FAILURE", "UNSTABLE"):
        out += (f" | last green #{r['stable']} {fmt_age(r.get('stable_ts'))}" if r.get("stable")
                else " | no green build on record")
    elif r["status"] == "BUILDING" and (late := _overdue(r.get("ts"), r.get("est"))):
        out += f" | {late}"
    return out


def _targets(instance: str) -> list[Instance]:
    if instance.strip().lower() in ("", "all", "*"):
        return list(config.current().instances.values())
    return [get_instance(instance)]


@mcp.tool(annotations=READ_ONLY)
def list_jobs(instance: InstanceName = "", folder: str = "", status: str = "",
              name_contains: str = "", limit: int = 100) -> JsonDict:
    """List jobs recursively (folders and multibranch projects are expanded).
    folder: start below this folder, e.g. 'cd' or 'release'. Empty = whole instance.
    status: comma list filter on the last build: SUCCESS, FAILURE, UNSTABLE, ABORTED, NOT_BUILT, BUILDING, DISABLED.
    name_contains: case-insensitive substring filter on the full job name.
    Each row: 'full/job/name | STATUS #lastBuild | age'. Uses few API calls (no per-job requests)."""
    inst = get_instance(instance)
    folder_parts = [p for p in folder.split("/") if p]
    rows, truncated = walk_jobs(inst, folder_parts)
    wanted = {s.strip().upper() for s in status.split(",") if s.strip()}
    needle = name_contains.lower().strip()
    hits = [r for r in rows if (not wanted or r["status"] in wanted) and (not needle or needle in r["job"].lower())]
    limit = max(1, min(limit, 500))
    result: JsonDict = {"instance": inst.name, "folder": folder or "/", "matched": len(hits),
                        "jobs": [_row_str(r) for r in hits[:limit]]}
    if len(hits) > limit:
        result["note"] = f"{len(hits) - limit} more - use folder/status/name_contains to narrow down"
    if truncated:
        result["warning"] = "folder walk stopped early (too many folders); pass a narrower folder"
    return result


@mcp.tool(annotations=READ_ONLY)
def failures_overview(instance: Annotated[str, Field(description="Instance name or 'all'")] = "all",
                      folder: str = "", hours: int = 24, include_unstable: bool = True,
                      limit_per_instance: int = 30) -> JsonDict:
    """Jobs whose LAST build failed (and optionally is unstable) within the last `hours`,
    across one or all instances in parallel. Good for a morning check or 'what is red right now'."""
    targets = _targets(instance)
    wanted = {"FAILURE"} | ({"UNSTABLE"} if include_unstable else set())
    since_ms = (time.time() - hours * 3600) * 1000
    folder_parts = [p for p in folder.split("/") if p]

    def scan(inst: Instance) -> tuple[str, JsonDict]:
        try:
            rows, truncated = walk_jobs(inst, folder_parts)
        except ToolError as e:
            return inst.name, {"error": str(e)}
        bad = sorted((r for r in rows if r["status"] in wanted and (r.get("ts") or 0) >= since_ms),
                     key=lambda r: -(r.get("ts") or 0))
        res: JsonDict = {"count": len(bad), "jobs": [_row_str(r) for r in bad[:limit_per_instance]]}
        if truncated:
            res["warning"] = "partial scan"
        return inst.name, res

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(pool.map(scan, targets))
    return {"window_hours": hours, "statuses": sorted(wanted), "instances": results}


@mcp.tool(annotations=READ_ONLY)
def recent_builds(instance: Annotated[str, Field(description="Instance name or 'all'")] = "all",
                  hours: int = 24, folder: str = "",
                  status: Annotated[str, Field(description="Comma list: SUCCESS, FAILURE, UNSTABLE, ABORTED, "
                                                           "NOT_BUILT, BUILDING. Empty = all")] = "",
                  limit_per_instance: int = 50) -> JsonDict:
    """Every build (not only the last one per job) started in the last `hours`, across one or all
    instances in parallel, newest first: 'job #n RESULT duration age | culprits | long-running note'.
    Counts per result. Uses ~1 request per instance (up to 10 newest builds per job are checked).
    Good for 'what ran/failed today', a morning check or a stand-up."""
    targets = _targets(instance)
    wanted = {s.strip().upper() for s in status.split(",") if s.strip()}
    since_ms = (time.time() - max(1, hours) * 3600) * 1000
    folder_parts = [p for p in folder.split("/") if p]
    limit = max(1, min(limit_per_instance, 300))

    def scan(inst: Instance) -> tuple[str, JsonDict]:
        try:
            rows, truncated = walk_jobs(inst, folder_parts, builds=10)
        except ToolError as e:
            return inst.name, {"error": str(e)}
        found = []
        for r in rows:
            for b in r["builds"]:
                result = "BUILDING" if b.get("building") else str(b.get("result") or "UNKNOWN")
                if (b.get("timestamp") or 0) < since_ms and result != "BUILDING":
                    continue
                if wanted and result not in wanted:
                    continue
                found.append((b.get("timestamp") or 0, result, r["job"], b))
        found.sort(key=lambda x: -x[0])
        by_result: dict[str, int] = {}
        out: list[str] = []
        for ts, result, job, b in found:
            by_result[result] = by_result.get(result, 0) + 1
            if len(out) >= limit:
                continue
            row = f"{job} #{b.get('number')} {result}"
            row += (f" {fmt_duration(b.get('duration'))}" if result != "BUILDING" else "") + f" {fmt_age(ts)}"
            names = [str(c["fullName"]) for c in b.get("culprits") or [] if isinstance(c, dict) and c.get("fullName")]
            if names and result in ("FAILURE", "UNSTABLE"):
                row += f" | culprits: {', '.join(names[:5])}"
            if result == "BUILDING" and (late := _overdue(ts, b.get("estimatedDuration"))):
                row += f" | {late}"
            out.append(row)
        res: JsonDict = {"count": len(found), "by_result": by_result, "builds": out}
        if len(found) > limit:
            res["note"] = f"{len(found) - limit} more - narrow with folder/status/hours"
        if truncated:
            res["warning"] = "partial scan"
        return inst.name, res

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(pool.map(scan, targets))
    return {"window_hours": hours, "statuses": sorted(wanted) or "all", "instances": results}


def normalize_repo(url: str) -> str:
    """'host/org/repo' for https, scp-like ssh (git@host:org/repo), ssh://...:port, Bitbucket /scm/ URLs."""
    u = url.strip().lower()
    u = re.sub(r"^[a-z+]+://", "", u)              # scheme
    u = re.sub(r"^[^@/]+@", "", u)                 # user[:password]@
    u = re.sub(r"^([^/:]+):(\d+)/", r"\1/", u)     # host:port/
    u = re.sub(r"^([^/:]+):(?!\d+/)", r"\1/", u)   # scp-like host:org/repo
    u = re.sub(r"^([^/]+)/scm/", r"\1/", u)        # Bitbucket Server clone URLs
    return re.sub(r"(\.git)?/*$", "", u)


@mcp.tool(annotations=READ_ONLY)
def find_jobs_by_scm(url: Annotated[str, Field(description="Repository URL in any spelling (https, ssh, with .git) "
                                                           "or a part of it like 'org/repo'")],
                     instance: Annotated[str, Field(description="Instance name or 'all'")] = "all",
                     folder: str = "") -> JsonDict:
    """Jobs whose last build checked out a repository (app repo or Shared Library), across instances:
    'job | STATUS #n | repo URL'. URL spellings are normalized (https / ssh / port / Bitbucket /scm/ / .git).
    ~1 request per instance. Jobs that were never built cannot be found this way."""
    targets = _targets(instance)
    wanted = normalize_repo(url)
    folder_parts = [p for p in folder.split("/") if p]

    def scan(inst: Instance) -> tuple[str, JsonDict]:
        try:
            rows, truncated = walk_jobs(inst, folder_parts, scm=True)
        except ToolError as e:
            return inst.name, {"error": str(e)}
        hits = []
        for r in rows:
            match = next((u for u in r["repos"] if wanted and wanted in normalize_repo(u)), None)
            if match:
                hits.append(f"{r['job']} | {r['status']}" + (f" #{r['number']}" if r.get("number") else "")
                            + f" | {mask(match)}")
        res: JsonDict = {"jobs": hits}
        if truncated:
            res["warning"] = "partial scan"
        return inst.name, res

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(pool.map(scan, targets))
    out: JsonDict = {"repository": wanted, "instances": results}
    if not any(r.get("jobs") for r in results.values()):
        out["note"] = ("no job's last build checked out this repository; jobs that were never built (or whose "
                       "last build failed before the checkout) are not found this way")
    return out


@mcp.tool(annotations=READ_ONLY)
def get_job(job: Job, instance: InstanceName = "", recent_builds: int = 10) -> JsonDict:
    """Job details: type, health, permalinks (last/lastFailed/lastSuccessful build numbers),
    recent builds, parameters it accepts, and children if it is a folder/multibranch project."""
    t = resolve(job, "", instance)
    n = max(1, min(recent_builds, 50))
    tree = ("name,fullName,_class,buildable,color,description,healthReport[score,description],"
            "lastBuild[number],lastSuccessfulBuild[number],lastFailedBuild[number],lastCompletedBuild[number],"
            f"builds[number,result,duration,timestamp,building]{{0,{n}}},jobs[name,_class,color],"
            "property[parameterDefinitions[name,type,defaultParameterValue[value]]]")
    d = t.inst.json(f"{t.path}/api/json", tree=tree, allow_404=True)
    if d is None:
        raise ToolError(t.inst.http_error(404, f"{t.inst.url}{t.path}/"))
    out: JsonDict = {"instance": t.inst.name, "job": d.get("fullName") or t.full_name,
                     "url": f"{t.inst.url}{t.path}/", "type": short_class(d.get("_class"))}
    if d.get("description"):
        out["description"] = clip(d["description"], 300)
    if d.get("healthReport"):
        out["health"] = [f"{h.get('score')}%: {h.get('description')}" for h in d["healthReport"]]
    links = {k: (d.get(k) or {}).get("number") for k in
             ("lastBuild", "lastCompletedBuild", "lastSuccessfulBuild", "lastFailedBuild")}
    if any(links.values()):
        out["permalinks"] = links
    if d.get("builds"):
        out["recent_builds"] = [
            f"#{b.get('number')} {'BUILDING' if b.get('building') else b.get('result')} "
            f"{fmt_duration(b.get('duration'))} {fmt_age(b.get('timestamp'))}" for b in d["builds"]]
    params = []
    for prop in d.get("property") or []:
        for p in (prop or {}).get("parameterDefinitions") or []:
            default = (p.get("defaultParameterValue") or {}).get("value")
            if is_secret_name(str(p.get("name"))) or "Password" in str(p.get("type")):
                default = "****" if default else default
            params.append(f"{p.get('name')} ({short_class(p.get('type')).replace('ParameterDefinition', '')})"
                          + (f" default={clip(default, 80)}" if default not in (None, "") else ""))
    if params:
        out["parameters"] = params
    if d.get("jobs") is not None:
        kids = d.get("jobs") or []
        out["children"] = [f"{c.get('name')} ({short_class(c.get('_class'))}, {c.get('color', '-')})" for c in kids[:100]]
        if len(kids) > 100:
            out["children_note"] = f"{len(kids) - 100} more - use list_jobs(folder=...)"
    return out
