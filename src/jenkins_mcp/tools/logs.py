"""Log tools: get_stage_log, get_log_errors, search_log, get_log_lines."""

import html
import re
from typing import Annotated

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..app import READ_ONLY, Build, InstanceName, Job, mcp
from ..kb import match as kb_match
from ..logs import fmt_line, load_log, meaningful_tail, pick_failed_stage, pipeline_stages, stage_str
from ..logs.parse import clean_line, strip_ts
from ..settings import MAX_CHARS, UNTRUSTED
from ..targets import Target, build_info, resolve
from ..text import budget, clip, fmt_duration, mask, safe_regex


_TAG = re.compile(r"<[^>]+>")
_MAX_STEPS = 30


def _exact_stage_log(t: Target, number: int, name: str, max_lines: int) -> str | None:
    """Per-step log of one stage from wfapi (no interleaving of parallel branches); None without wfapi."""
    try:
        d = t.inst.json(f"{t.path}/{number}/wfapi/describe", allow_404=True)
    except ToolError:
        return None
    stage = next((s for s in (d or {}).get("stages") or [] if str(s.get("name", "")).lower() == name.lower()), None)
    if not stage or not stage.get("id"):
        return None
    nd = t.inst.json(f"{t.path}/{number}/execution/node/{stage['id']}/wfapi/describe", allow_404=True) or {}
    out = [f"Stage '{stage.get('name')}' ({stage.get('status')}), build #{number} on {t.inst.name} - per-step log "
           "from wfapi", UNTRUSTED]
    per_step = max(5, min(max_lines, 300) // max(1, len(nd.get("stageFlowNodes") or [1])))
    for step in (nd.get("stageFlowNodes") or [])[:_MAX_STEPS]:
        desc = f" '{clip(mask(str(step['parameterDescription'])), 80)}'" if step.get("parameterDescription") else ""
        out.append(f"\n--- {step.get('name')}{desc} ({step.get('status')}, {fmt_duration(step.get('durationMillis'))})")
        try:
            lg = t.inst.json(f"{t.path}/{number}/execution/node/{step.get('id')}/wfapi/log", allow_404=True) or {}
        except ToolError:
            lg = {}
        text = html.unescape(_TAG.sub("", str(lg.get("text") or "")))
        lines = [ln for ln in (clean_line(x) for x in text.split("\n")) if ln]
        out += [f"    {clip(mask(ln), 400)}" for ln in lines[-per_step:]]
        if lg.get("hasMore"):
            out.append("    ... (step log continues - use search_log)")
    return budget("\n".join(out))


@mcp.tool(annotations=READ_ONLY)
def get_stage_log(job: Job, build: Build = "", instance: InstanceName = "",
                  stage: Annotated[str, Field(description="Stage name; empty = the failed stage")] = "",
                  max_lines: int = 80,
                  exact: Annotated[bool, Field(description="Per-step log from the Stage View API (wfapi): exact "
                                                           "for parallel branches; more requests")] = False) -> str:
    """Errors and the last meaningful lines of ONE pipeline stage (default: the failed stage).
    Much cheaper than the full log. Lines of parallel branches can interleave in the console; exact=True
    reads the stage step by step from wfapi instead (falls back to the console without the plugin)."""
    t = resolve(job, build, instance)
    info = build_info(t, light=True)
    number = int(info["number"])
    pl = load_log(t, info)
    stages, _ = pipeline_stages(t, number, pl)
    name = stage.strip() or pick_failed_stage(stages, pl)
    if not name:
        if stages:
            return "No failed stage detected. Stages: " + "; ".join(stage_str(s) for s in stages)
        return "No pipeline stages found in this log (freestyle job?). Use get_log_errors or search_log."
    note = ""
    if exact:
        if (text := _exact_stage_log(t, number, name, max_lines)) is not None:
            return text
        note = "(no per-step data from wfapi - Stage View plugin missing? - showing the console markers instead)"
    rng = pl.stage_range(name)
    if rng is None:
        known = ", ".join(pl.spans.keys()) or "none"
        return f"Stage '{name}' not found in the console log. Stages seen: {clip(known, 1500)}"
    lo, hi = rng
    kb = kb_match.analyse(pl, t.inst.name, t.full_name)
    status = next((s.get("status") for s in stages if s.get("name") == name), "?")
    head = [f"Stage '{name}' ({status}) lines {lo + 1}-{hi + 1} of {len(pl.lines)}, build #{number} on {t.inst.name} "
            f"{note}".rstrip(),
            UNTRUSTED]
    errs = [g for g in pl.groups if lo < g.first_line <= hi + 1]
    if errs:
        head.append(f"\nErrors in this stage ({len(errs)} distinct, first 8):")
        head += [g.render(kb.notes.get(g.key, "")) for g in errs[:8]]
    head.append(f"\nLast {max_lines} meaningful lines of the stage:")
    head += meaningful_tail(pl, max(10, min(max_lines, 300)), lo, hi)
    return budget("\n".join(head))


@mcp.tool(annotations=READ_ONLY)
def get_log_errors(job: Job, build: Build = "", instance: InstanceName = "",
                   category: Annotated[str, Field(description="Optional filter: maven, tests, java, helm, k8s, "
                                                              "ansible, docker, network, nexus, system, jenkins, generic "
                                                              "or a custom category")] = "",
                   max_groups: int = 20, include_generic: bool = False) -> str:
    """All detected error lines of a build, de-duplicated into groups (same message = one group
    with a count), in order of first appearance, with line number, stage, severity and a shortened
    stack trace. Severity 'warn' = error text inside a line logged as WARN (often retries, rarely the cause);
    category 'generic' = an 'Error:'/'FATAL:' line no specific rule knows (ranked after specific errors).
    Groups the knowledge base knows are annotated ('known issue', 'known noise', 'consequence of').
    Generic Jenkins wrapper messages ('script returned exit code 1') are hidden unless include_generic."""
    t = resolve(job, build, instance)
    info = build_info(t, light=True)
    pl = load_log(t, info)
    groups = pl.groups
    if not include_generic:
        groups = [g for g in groups if g.severity != "low"] or groups
    if category:
        groups = [g for g in groups if g.category.lower() == category.lower().strip()]
    if not groups:
        return f"No error lines detected in build #{info['number']} on {t.inst.name} ({len(pl.lines)} lines). Try search_log."
    kb = kb_match.analyse(pl, t.inst.name, t.full_name)
    shown = groups[: max(1, min(max_groups, 60))]
    out = [f"Build #{info['number']} on {t.inst.name}: {len(groups)} distinct error groups, showing {len(shown)}",
           UNTRUSTED]
    out += [g.render(kb.notes.get(g.key, "")) for g in shown]
    return budget("\n".join(out))


@mcp.tool(annotations=READ_ONLY)
def search_log(job: Job, pattern: Annotated[str, Field(description="Python regex (invalid regex is treated as literal text)")],
               build: Build = "", instance: InstanceName = "", context: int = 2, max_matches: int = 25,
               ignore_case: bool = True, stage: str = "") -> str:
    """grep -C for a build log: matching lines with `context` lines around them and line numbers.
    Optionally restricted to one stage. Reports the total number of matches even if only
    max_matches are shown."""
    t = resolve(job, build, instance)
    info = build_info(t, light=True)
    pl = load_log(t, info)
    rx = safe_regex(pattern, re.IGNORECASE if ignore_case else 0)
    lo, hi = 0, len(pl.lines) - 1
    if stage:
        rng = pl.stage_range(stage)
        if rng is None:
            return f"Stage '{stage}' not found. Stages: {clip(', '.join(pl.spans.keys()), 1500)}"
        lo, hi = rng
    hits = [i for i in range(lo, hi + 1) if rx.search(strip_ts(pl.lines[i]))]  # match what is shown
    if not hits:
        return f"No match for /{pattern}/ in build #{info['number']} on {t.inst.name} ({hi - lo + 1} lines searched)."
    ctx = max(0, min(context, 10))
    shown = hits[: max(1, min(max_matches, 100))]
    hitset = set(shown)
    out = [f"{len(hits)} matching lines for /{pattern}/ in build #{info['number']} on {t.inst.name}, "
           f"showing {len(shown)}", UNTRUSTED]
    last = -2
    for h in shown:
        a, b = max(lo, h - ctx), min(hi, h + ctx)
        if a > last + 1 and last >= 0:
            out.append("   ...")
        for k in range(max(a, last + 1), b + 1):
            out.append(fmt_line(pl, k, mark=k in hitset))
        last = b
    if len(hits) > len(shown):
        out.append(f"[+{len(hits) - len(shown)} more matches; first unshown at L{hits[len(shown)] + 1}]")
    return budget("\n".join(out))


@mcp.tool(annotations=READ_ONLY)
def get_log_lines(job: Job, build: Build = "", instance: InstanceName = "",
                  start: Annotated[int, Field(description="1-based first line; 0 or negative = tail of the log")] = 0,
                  count: int = 150, hide_noise: bool = False) -> str:
    """A window of the cleaned console log with line numbers (max 400 lines per call).
    Use line numbers from the other tools. hide_noise drops [Pipeline]/download/progress lines."""
    t = resolve(job, build, instance)
    info = build_info(t, light=True)
    pl = load_log(t, info)
    n = len(pl.lines)
    count = max(1, min(count, 400))
    if start <= 0:
        if hide_noise:
            body = meaningful_tail(pl, count)
            return budget("\n".join([f"Last {len(body)} meaningful lines of {n}, build #{info['number']} on {t.inst.name}",
                                      UNTRUSTED] + body))
        a = max(0, n - count)
    else:
        a = min(start - 1, max(0, n - 1))
    idx = [i for i in range(a, min(n, a + count)) if not (hide_noise and pl.is_noise(i))]
    head = f"Lines {a + 1}-{min(n, a + count)} of {n}, build #{info['number']} on {t.inst.name}"
    if pl.truncated:
        head += " (log was truncated in the middle because of its size)"
    return budget("\n".join([head, UNTRUSTED] + [fmt_line(pl, i) for i in idx]), MAX_CHARS)
