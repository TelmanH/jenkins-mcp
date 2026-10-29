"""Pipeline stages: wfapi (Pipeline Stage View plugin) with a console-marker fallback."""

import re

from fastmcp.exceptions import ToolError

from ..settings import JsonDict
from ..targets import Target
from ..text import clip, fmt_duration, short_class
from .errors import root_cause_candidates
from .parse import ParsedLog

FAILED_STATES = ("FAILED", "UNSTABLE", "ABORTED")


def pipeline_stages(t: Target, number: int, pl: ParsedLog | None) -> tuple[list[JsonDict], str]:
    stages: list[JsonDict] = []
    source = "wfapi"
    try:
        d = t.inst.json(f"{t.path}/{number}/wfapi/describe", allow_404=True)
    except ToolError:
        d = None
    if d and isinstance(d.get("stages"), list):
        for s in d["stages"]:
            item: JsonDict = {"name": s.get("name"), "status": s.get("status"),
                              "duration": fmt_duration(s.get("durationMillis"))}
            err = s.get("error") or {}
            if err:
                item["error"] = clip(f"{short_class(err.get('type'))}: {err.get('message', '')}", 300)
            stages.append(item)
    elif pl is not None:
        source = "console"
        seen = set()
        for path in pl.spans:
            top = path.split(" > ")[0]
            if top not in seen:
                seen.add(top)
                stages.append({"name": top, "status": "?"})
        for line in pl.lines:  # mark skipped stages
            m = re.search(r'Stage "(.+?)" skipped due to', line)
            if m:
                for s in stages:
                    if s["name"] == m.group(1):
                        s["status"] = "SKIPPED"
    if pl is not None:
        for s in stages:
            rng = pl.stage_range(str(s["name"]))
            if rng:
                s["lines"] = f"{rng[0] + 1}-{rng[1] + 1}"
    return stages, source


def stage_str(s: JsonDict) -> str:
    out = f"{s['name']}: {s.get('status', '?')}"
    if s.get("duration"):
        out += f" {s['duration']}"
    if s.get("lines"):
        out += f" [L{s['lines']}]"
    if s.get("error"):
        out += f" - {s['error']}"
    return out


def pick_failed_stage(stages: list[JsonDict], pl: ParsedLog) -> str | None:
    for state in FAILED_STATES:
        for s in stages:
            if s.get("status") == state:
                return str(s["name"])
    for g in root_cause_candidates(pl.groups, 1):
        if g.stage:
            return g.stage.split(" > ")[0]
    return None
