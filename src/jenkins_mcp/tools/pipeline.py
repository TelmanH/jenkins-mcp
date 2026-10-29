"""Pipeline facts of one build: get_pipeline_info (what actually ran), get_pending_inputs."""

import html
import re
from typing import Annotated

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..app import READ_ONLY, Build, InstanceName, Job, mcp
from ..logs import load_log
from ..logs.pipeline import jenkinsfile_sources, libraries, managed_files, version_hints
from ..settings import UNTRUSTED, JsonDict
from ..targets import Target, build_info, resolve
from ..text import budget, clip, fmt_duration, mask
from .builds import build_params, pending_inputs

_SCM_TREE = "actions[_class,causes[_class,shortDescription],remoteUrls,lastBuiltRevision[SHA1,branch[name]]]"
_TEXTAREA = re.compile(r'<textarea[^>]*\bname="_\.([^"]+)"[^>]*>(.*?)</textarea>', re.S)
_MAX_STAGE_REQUESTS = 4


def _scm_rows(actions: list[JsonDict]) -> list[str]:
    rows: list[str] = []
    for a in actions:
        rev = a.get("lastBuiltRevision") if isinstance(a, dict) else None
        if not rev:
            continue
        url = ", ".join(a.get("remoteUrls") or []) or "?"
        branches = [re.sub(r"^refs/remotes/", "", str(b.get("name"))) for b in rev.get("branch") or [] if b.get("name")]
        row = f"{url} @ {', '.join(branches) or '?'} {str(rev.get('SHA1', ''))[:8]}"
        if row not in rows:
            rows.append(row)
    return rows


def _replay_cause(actions: list[JsonDict]) -> str:
    for a in actions:
        for c in (a or {}).get("causes") or []:
            cls = str(c.get("_class", ""))
            if "ReplayCause" in cls or "RestartDeclarativePipelineCause" in cls:
                return str(c.get("shortDescription") or cls)
    return ""


def _slowest_steps(t: Target, number: int) -> list[str]:
    """Slowest steps of the failed stage(s) and the two slowest stages (one wfapi request per stage)."""
    try:
        d = t.inst.json(f"{t.path}/{number}/wfapi/describe", allow_404=True)
    except ToolError:
        return []
    stages = [s for s in (d or {}).get("stages") or [] if isinstance(s, dict) and s.get("id")]
    picked = [s for s in stages if s.get("status") in ("FAILED", "UNSTABLE")]
    picked += [s for s in sorted(stages, key=lambda s: -(s.get("durationMillis") or 0)) if s not in picked][:2]
    steps: list[tuple[int, str]] = []
    for s in picked[:_MAX_STAGE_REQUESTS]:
        try:
            nd = t.inst.json(f"{t.path}/{number}/execution/node/{s['id']}/wfapi/describe", allow_404=True) or {}
        except ToolError:
            continue
        for n in nd.get("stageFlowNodes") or []:
            ms = int(n.get("durationMillis") or 0)
            desc = f" '{clip(mask(str(n['parameterDescription'])), 60)}'" if n.get("parameterDescription") else ""
            status = f" {n.get('status')}" if n.get("status") not in (None, "SUCCESS") else ""
            steps.append((ms, f"{s.get('name')}: {n.get('name')}{desc} {fmt_duration(ms)}{status}"))
    return [row for _, row in sorted(steps, key=lambda x: -x[0])[:6]]


def _executed_script(t: Target, number: int) -> JsonDict | str:
    try:
        r = t.inst.get(f"{t.path}/{number}/replay/", allow_404=True)
    except ToolError as e:
        if "(403)" in str(e):
            return "not readable: the token's user needs the Run/Replay permission on this job"
        return f"not readable: {clip(e, 200)}"
    if r is None:
        return "no replay page (not a Pipeline build, or Jenkins cannot replay it)"
    scripts = {name: html.unescape(body) for name, body in _TEXTAREA.findall(r.text)}
    if "mainScript" not in scripts:
        return "replay page has no script (the build may be too old or still running)"
    return {"main": budget(mask(scripts.pop("mainScript")), 6000), "loaded_scripts": sorted(scripts)}


@mcp.tool(annotations=READ_ONLY)
def get_pipeline_info(job: Job, build: Build = "", instance: InstanceName = "",
                      show_script: Annotated[bool, Field(description="Also return the Jenkinsfile that actually "
                                                                     "ran (from the replay page)")] = False) -> JsonDict:
    """What actually ran in a Pipeline build: where the Jenkinsfile came from, the git checkouts (repo,
    branch, commit), the Shared Library versions that were loaded (with a hint when a library branch does
    not match a *VERSION* parameter, e.g. rel-2.3 vs REL_VERSION=2.4), replay / restart-from-stage, and the
    slowest steps, and the Config File Provider files it provisioned. show_script=True adds the executed
    script (needs the Run/Replay permission)."""
    t = resolve(job, build, instance)
    info = build_info(t)
    number = int(info["number"])
    pl = load_log(t, info)
    actions = [a for a in (t.inst.json(f"{t.path}/{number}/api/json", tree=_SCM_TREE) or {}).get("actions") or []
               if isinstance(a, dict)]
    out: JsonDict = {"instance": t.inst.name, "job": t.full_name, "build": number, "url": t.build_url(number)}
    if src := jenkinsfile_sources(pl):
        out["jenkinsfile"] = src
    if scm := _scm_rows(actions):
        out["scm"] = scm
    libs = libraries(pl)
    if libs:
        out["libraries"] = libs
    if hints := version_hints(libs, build_params(info)):
        out["hints"] = hints
    if files := managed_files(pl):
        out["managed_files"] = files
    if replay := _replay_cause(actions):
        out["replay"] = replay
        out["replay_note"] = "replayed/restarted builds can run a script that differs from Git (show_script=true)"
    if steps := _slowest_steps(t, number):
        out["slowest_steps"] = steps
    if show_script:
        out["script"] = _executed_script(t, number)
        out["note"] = UNTRUSTED
    return out


@mcp.tool(annotations=READ_ONLY)
def get_pending_inputs(job: Job, build: Build = "", instance: InstanceName = "") -> JsonDict:
    """What a running Pipeline build is waiting for (`input` steps): message, id, button, requested inputs
    and who may approve. Read only - approving or aborting stays a human action in the Jenkins UI."""
    t = resolve(job, build, instance)
    info = build_info(t, light=True)
    number = int(info["number"])
    out: JsonDict = {"instance": t.inst.name, "job": t.full_name, "build": number, "url": t.build_url(number)}
    if not info.get("building"):
        out["waiting_for"] = [f"nothing - the build is not running (result {info.get('result')})"]
        return out
    rows = []
    for w in pending_inputs(t, number):
        names = [str(i.get("name")) for i in w.get("inputs") or [] if isinstance(i, dict) and i.get("name")]
        row = f"'{clip(w.get('message') or '', 150)}' (id {w.get('id')}, button '{w.get('proceedText') or 'Proceed'}'"
        row += f", inputs: {', '.join(names)}" if names else ""
        row += f"; submitter: {w.get('submitter')}" if w.get("submitter") else ""
        rows.append(row + ")")
    out["waiting_for"] = rows or ["no pending input step (or the Pipeline Stage View plugin is missing)"]
    out["note"] = f"approve or abort in the Jenkins UI: {t.build_url(number)}input/"
    return out
