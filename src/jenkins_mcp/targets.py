"""Addressing jobs and builds: 'folder/sub/job', pasted URLs, numbers and permalinks."""

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlparse

from fastmcp.exceptions import ToolError

from .client import Instance
from .config import get_instance, instance_for_url
from .settings import PERMALINKS, JsonDict


def job_path(parts: list[str]) -> str:
    return "/" + "/".join(f"job/{quote(p, safe='')}" for p in parts) if parts else ""


@dataclass
class Target:
    inst: Instance
    parts: list[str]
    build: str  # number or permalink

    @property
    def path(self) -> str:
        return job_path(self.parts)

    @property
    def full_name(self) -> str:
        return "/".join(self.parts)

    def build_url(self, number: Any) -> str:
        return f"{self.inst.url}{self.path}/{number}/"


def normalize_build(build: Any, fallback: str) -> str:
    b = str(build).strip().lstrip("#") if build not in (None, "") else fallback
    if b.isdigit():
        return b
    if b.lower() in PERMALINKS:
        return PERMALINKS[b.lower()]
    raise ToolError(f"Invalid build '{build}'. Use a number or one of: {', '.join(PERMALINKS.values())}")


def resolve(job: str, build: Any = "", instance: str = "") -> Target:
    job = (job or "").strip()
    if not job:
        raise ToolError("job must not be empty (e.g. 'cd/master/my-app' or a Jenkins URL).")
    url_build = None
    if job.startswith(("http://", "https://")):
        inst = instance_for_url(job)
        rest = urlparse(job).path[len(urlparse(inst.url).path):]
        segs = [unquote(s) for s in rest.split("/") if s]
        parts: list[str] = []
        i = 0
        while i + 1 < len(segs) and segs[i] == "view":  # URL copied from a (nested) view
            i += 2
        while i + 1 < len(segs) and segs[i] == "job":
            parts.append(segs[i + 1])
            i += 2
        if i < len(segs) and (segs[i].isdigit() or segs[i].lower() in PERMALINKS):
            url_build = segs[i]
    else:
        inst = get_instance(instance)
        segs = [s for s in job.split("/") if s]
        if len(segs) >= 2 and segs[0] == "job":  # pasted path like job/a/job/b
            segs = [segs[k + 1] for k in range(0, len(segs) - 1, 2) if segs[k] == "job"]
        parts = segs
    if not parts:
        raise ToolError(f"Could not find a job path in '{job}'.")
    return Target(inst, parts, normalize_build(build, url_build or "lastBuild"))


BUILD_TREE = (
    "number,url,result,building,duration,estimatedDuration,timestamp,displayName,description,"
    "actions[_class,causes[shortDescription],parameters[name,value],failCount,skipCount,totalCount],"
    "changeSets[items[msg,commitId,author[fullName]]],culprits[fullName]"
)
BUILD_TREE_LIGHT = "number,result,building,duration,timestamp"


def build_info(t: Target, light: bool = False) -> JsonDict:
    data = t.inst.json(f"{t.path}/{t.build}/api/json", tree=BUILD_TREE_LIGHT if light else BUILD_TREE,
                       allow_404=True)
    if data is None:
        job = t.inst.json(f"{t.path}/api/json", tree="name,firstBuild[number],lastBuild[number]", allow_404=True)
        if job is None:
            raise ToolError(t.inst.http_error(404, f"{t.inst.url}{t.path}/"))
        raise ToolError(_missing_build(t, job))
    return data


def _missing_build(t: Target, job: JsonDict) -> str:
    first = (job.get("firstBuild") or {}).get("number")
    last = (job.get("lastBuild") or {}).get("number")
    msg = f"[{t.inst.name}] Job '{t.full_name}' has no build '{t.build}'."
    if not last:
        msg += " The job has no builds (yet)."
    elif not t.build.isdigit():
        msg += " No build matches this permalink (none kept, e.g. never successful, or removed by log rotation)."
    elif first and int(t.build) < int(first):
        msg += (f" It was removed by the build discarder (log rotation): the oldest kept build is #{first}, "
                f"the newest is #{last}.")
    elif int(t.build) > int(last):
        msg += f" The newest is #{last}."
    else:
        msg += f" Kept builds: #{first}-#{last}; this one was deleted."
    return msg + " Use get_job to see recent builds."
