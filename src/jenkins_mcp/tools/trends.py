"""Errors across builds: recurring_errors (regressions vs. errors that also occur in green builds)."""

from fastmcp.exceptions import ToolError

from ..app import READ_ONLY, InstanceName, Job, mcp
from ..kb import match as kb_match
from ..logs import load_log
from ..logs.errors import ErrorGroup
from ..settings import UNTRUSTED, JsonDict
from ..targets import resolve
from ..text import clip, mask

_FINISHED = ("SUCCESS", "FAILURE", "UNSTABLE")
_MAX_BUILDS = 20
_MAX_ROWS = 25


def _row(g: ErrorGroup, failed: set[int], green: set[int], nf: int, ng: int, analysed: list[int],
         failed_builds: list[int], note: str) -> str:
    occurs = sorted(failed | green)
    tags = []
    if green:
        tags.append("also in green builds -> probably not the cause (red_herring candidate)")
    elif any(n < occurs[0] for n in analysed):
        since = [n for n in failed_builds if n >= occurs[0]]
        tags.append(f"new since #{occurs[0]}" + (" (in every failed build since)" if set(since) <= failed else ""))
    elif len(failed) == nf:
        tags.append("in every analysed failed build")
    row = (f"{len(failed)}/{nf} failed, {len(green)}/{ng} green | first #{occurs[0]} last #{occurs[-1]} | "
           f"{'; '.join(tags) or '-'} | [{g.category}] {clip(mask(g.text), 160)}")
    return row + (f"  <- {note}" if note else "")


@mcp.tool(annotations=READ_ONLY)
def recurring_errors(job: Job, instance: InstanceName = "", last_n: int = 8) -> JsonDict:
    """Error groups across the newest finished builds of one job (max 20): in how many failed and green
    builds each occurs, first/last build, and a verdict - 'new since #N' (regression) or 'also in green
    builds' (red_herring candidate for the knowledge base). Knowledge base notes are attached.
    Cost: one log per build (cached afterwards)."""
    t = resolve(job, "", instance)
    n = max(2, min(last_n, _MAX_BUILDS))
    d = t.inst.json(f"{t.path}/api/json", tree=f"builds[number,result,building]{{0,{n + 5}}}") or {}
    builds = [b for b in d.get("builds") or [] if not b.get("building") and b.get("result") in _FINISHED][:n]
    if not builds:
        raise ToolError(f"[{t.inst.name}] '{t.full_name}' has no finished builds.")
    failed_builds = sorted(int(b["number"]) for b in builds if b["result"] != "SUCCESS")
    analysed = sorted(int(b["number"]) for b in builds)
    nf, ng = len(failed_builds), len(builds) - len(failed_builds)
    out: JsonDict = {"instance": t.inst.name, "job": t.full_name,
                     "builds": f"{len(builds)} analysed: {nf} failed, {ng} green (#{analysed[0]}-#{analysed[-1]})"}
    if not failed_builds:
        out["errors"] = [f"no failed build among the last {len(builds)} finished builds"]
        return out
    seen: dict[tuple[str, str], JsonDict] = {}
    for b in builds:
        number = int(b["number"])
        bt = resolve(t.full_name, number, t.inst.name)
        pl = load_log(bt, {"number": number, "building": False})
        kb = kb_match.analyse(pl, t.inst.name, t.full_name)
        for g in pl.groups:
            if g.severity == "low":
                continue
            e = seen.setdefault(g.key, {"group": g, "failed": set(), "green": set(), "note": ""})
            e["green" if b["result"] == "SUCCESS" else "failed"].add(number)
            e["note"] = e["note"] or kb.notes.get(g.key, "")
    rows = sorted((e for e in seen.values() if e["failed"]),
                  key=lambda e: (-len(e["failed"]), len(e["green"]), e["group"].first_line))
    out["errors"] = [_row(e["group"], e["failed"], e["green"], nf, ng, analysed, failed_builds, e["note"])
                     for e in rows[:_MAX_ROWS]]
    if len(rows) > _MAX_ROWS:
        out["more"] = f"{len(rows) - _MAX_ROWS} more error groups"
    out["note"] = UNTRUSTED
    return out
