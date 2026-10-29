"""Build analysis: get_build_summary, get_test_failures, compare_builds."""

import re
from typing import Annotated

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..app import READ_ONLY, Build, InstanceName, Job, mcp
from ..kb import match as kb_match
from ..logs import (ParsedLog, load_log, meaningful_tail, pick_failed_stage, pipeline_stages,
                    root_cause_candidates, stage_str)
from ..logs.errors import ErrorGroup
from ..logs.pipeline import downstream_builds
from ..settings import UNTRUSTED, JsonDict
from ..targets import Target, build_info, resolve
from ..text import NORMALIZE, clip, fmt_age, fmt_duration, fmt_time, is_secret_name, mask
from .artifacts import env_log_of


_PIPELINE_FILE = re.compile(r"(?:^|/)Jenkinsfile[^/]*$|\.groovy$")


def build_params(info: JsonDict) -> dict[str, str]:
    out = {}
    for a in info.get("actions") or []:
        for p in (a or {}).get("parameters") or []:
            name = str(p.get("name"))
            out[name] = "****" if is_secret_name(name) else clip(p.get("value"), 120)
    return out


_MAX_DOWNSTREAM = 5


def _downstream(t: Target, pl: ParsedLog, check_results: bool) -> tuple[list[str], str]:
    """Rows 'job #n RESULT' of builds started by the `build` step, and a hint for the first failed one."""
    rows, hint = [], ""
    for name, n in downstream_builds(pl)[:_MAX_DOWNSTREAM]:
        if not check_results:
            rows.append(f"{name} #{n}")
            continue
        try:
            child = build_info(resolve(name, n, t.inst.name), light=True)
        except ToolError:
            rows.append(f"{name} #{n} (not found by this name)")
            continue
        result = "BUILDING" if child.get("building") else str(child.get("result"))
        rows.append(f"{name} #{n} {result}")
        if not hint and result in ("FAILURE", "UNSTABLE", "ABORTED"):
            hint = (f"get_build_summary(job='{name}', build={n}) - the failure is in this downstream build; "
                    "analyse it first")
    return rows, hint


def pending_inputs(t: Target, number: int) -> list[JsonDict]:
    try:
        d = t.inst.json(f"{t.path}/{number}/wfapi/pendingInputActions", allow_404=True)
    except ToolError:
        return []
    return [x for x in d or [] if isinstance(x, dict)]


def ranked_causes(groups: list[ErrorGroup], kb: kb_match.Analysis, stage: str | None, k: int = 3) -> list[ErrorGroup]:
    """Known-issue lines first, then rule-based candidates without known noise/consequences."""
    first = kb.known_issue_groups(groups)
    rest = root_cause_candidates(groups, k, stage, exclude=kb.not_root_cause | {g.key for g in first})
    if first:  # a known issue explains the build: only add other real (high) errors, no warn/generic filler
        rest = [g for g in rest if g.severity == "high" and g.key not in kb.not_root_cause]
    return (first + [g for g in rest if g not in first])[:k]


@mcp.tool(annotations=READ_ONLY)
def get_build_summary(job: Job, build: Build = "", instance: InstanceName = "") -> JsonDict:
    """START HERE for any build question. Compact summary of one build: result, duration,
    trigger, parameters, agent node, commits, culprits (failed builds), downstream builds started by the
    `build` step (with their result), pending input steps (running builds), stages (with failed stage and its error),
    test counts, log size, known issues from the team knowledge base, likely root-cause
    error lines with line numbers, and the last meaningful log lines.
    Typical size: 1-3k tokens even for 30k+ line logs."""
    t = resolve(job, build, instance)
    info = build_info(t)
    number = int(info["number"])
    pl = load_log(t, info)
    stages, stage_source = pipeline_stages(t, number, pl)
    kb = kb_match.analyse(pl, t.inst.name, t.full_name)

    out: JsonDict = {
        "instance": t.inst.name, "job": t.full_name, "build": number, "url": t.build_url(number),
        "result": "BUILDING" if info.get("building") else info.get("result"),
        "duration": fmt_duration(info.get("duration")) if not info.get("building")
        else f"running, est. {fmt_duration(info.get('estimatedDuration'))}",
        "started": f"{fmt_time(info.get('timestamp'))} ({fmt_age(info.get('timestamp'))})",
    }
    if kb.hits:
        out["known_issues"] = [h.render() for h in kb.hits[:3]]
    causes: list[str] = []
    tests = None
    for a in info.get("actions") or []:
        if not isinstance(a, dict):
            continue
        causes += [c.get("shortDescription") for c in a.get("causes") or [] if c.get("shortDescription")]
        if "failCount" in a and "totalCount" in a:
            tests = {"failed": a.get("failCount"), "skipped": a.get("skipCount"), "total": a.get("totalCount")}
    if causes:
        out["trigger"] = causes[:3]
    params = build_params(info)
    if params:
        out["parameters"] = params
    if pl.nodes:
        out["agents"] = pl.nodes[:4]
    commits = [f"{str(i.get('commitId', ''))[:8]} {((i.get('author') or {}).get('fullName') or '?')}: "
               f"{clip((i.get('msg') or '').strip(), 100)}"
               for cs in info.get("changeSets") or [] for i in cs.get("items") or []]
    if commits:
        out["commits"] = commits[:5] + ([f"... +{len(commits) - 5} more"] if len(commits) > 5 else [])
    if info.get("building") and (waiting := pending_inputs(t, number)):
        out["waiting_for_input"] = [clip(str(w.get("message") or w.get("id")), 120) for w in waiting]
    down, down_hint = _downstream(t, pl, info.get("result") not in (None, "SUCCESS"))
    if down:
        out["downstream"] = down
    culprits = [c.get("fullName") for c in info.get("culprits") or [] if isinstance(c, dict) and c.get("fullName")]
    if culprits and info.get("result") in ("FAILURE", "UNSTABLE"):
        out["culprits"] = culprits[:10]
    if stages:
        out["stages"] = [stage_str(s) for s in stages]
        out["stage_source"] = stage_source
    failed = pick_failed_stage(stages, pl) if info.get("result") not in (None, "SUCCESS") else None
    if failed:
        out["failed_stage"] = failed
    if tests:
        out["tests"] = tests
    out["log"] = {"lines": len(pl.lines), "size_kb": pl.size_bytes // 1024, "truncated": pl.truncated}

    groups = pl.groups
    by_cat: dict[str, int] = {}
    for g in groups:
        by_cat[g.category] = by_cat.get(g.category, 0) + g.count
    errors: JsonDict = {
        "distinct": len(groups), "by_category": by_cat,
        "likely_root_cause": [g.render(kb.notes.get(g.key, "")) for g in ranked_causes(groups, kb, failed)],
    }
    noise = [g for g in groups if g.key in kb.not_root_cause]
    if noise:
        errors["known_noise"] = [f"L{g.first_line} {clip(mask(g.text), 120)}  <- {kb.notes[g.key]}" for g in noise[:6]]
    out["errors"] = errors

    rng = pl.stage_range(failed) if failed else None
    if rng:
        out["failed_stage_tail"] = "\n".join([UNTRUSTED] + meaningful_tail(pl, 15, rng[0], rng[1]))
        out["log_end"] = "\n".join(meaningful_tail(pl, 6))
    else:
        out["tail"] = "\n".join([UNTRUSTED] + meaningful_tail(pl, 20))

    hints = [down_hint] if down_hint else []
    if kb.hits:
        hints.append(f"get_known_issue('{kb.hits[0].entry.id}') - verified cause and fix from the team knowledge base")
    if failed:
        hints.append(f"get_stage_log(stage='{failed}') for the failed stage")
    if tests and tests.get("failed"):
        hints.append("get_test_failures for failing test cases")
    if len(groups) > 3:
        hints.append("get_log_errors for all error groups")
    hints.append("search_log(pattern=...) / get_log_lines(start=...) to drill down")
    if info.get("result") in ("FAILURE", "UNSTABLE"):
        hints.append("compare_builds to diff against the last successful build")
        if not kb.hits:
            hints.append("once the root cause is confirmed: draft_kb_entry to document it for the team")
    out["next_steps"] = hints
    return out


@mcp.tool(annotations=READ_ONLY)
def get_test_failures(job: Job, build: Build = "", instance: InstanceName = "", max_cases: int = 20) -> JsonDict:
    """Failed test cases from the JUnit test report (not the log): class, test, error message,
    shortened stack, and 'age' = number of consecutive builds this test has been failing."""
    t = resolve(job, build, instance)
    info = build_info(t, light=True)
    number = info["number"]
    d = t.inst.json(f"{t.path}/{number}/testReport/api/json",
                    tree="failCount,passCount,skipCount,suites[cases[className,name,status,errorDetails,errorStackTrace,age]]",
                    allow_404=True)
    if d is None:
        return {"instance": t.inst.name, "build": number, "has_test_report": False,
                "hint": "No JUnit report published. Use get_log_errors(category='tests') or search_log."}
    failed = [c for s in d.get("suites") or [] for c in s.get("cases") or []
              if c.get("status") in ("FAILED", "REGRESSION")]
    signatures: dict[str, int] = {}
    for c in failed:
        sig = NORMALIZE.sub("#", str(c.get("errorDetails") or "")).strip()[:120]
        signatures[sig] = signatures.get(sig, 0) + 1
    cases = []
    for c in failed[: max(1, min(max_cases, 100))]:
        stack = [ln.strip() for ln in str(c.get("errorStackTrace") or "").splitlines() if ln.strip()][:6]
        cases.append({"test": f"{c.get('className')}.{c.get('name')}", "status": c.get("status"),
                      "failing_for_builds": c.get("age"), "error": clip(mask(str(c.get("errorDetails") or "")), 400),
                      "stack": stack})
    return {"instance": t.inst.name, "build": number, "failed": d.get("failCount"), "passed": d.get("passCount"),
            "skipped": d.get("skipCount"),
            "common_errors": sorted(([v, k] for k, v in signatures.items()), reverse=True)[:5],
            "cases": cases, "note": UNTRUSTED}


_VOLATILE_ENV = re.compile(r"BUILD_(?:ID|NUMBER|URL|TAG|DISPLAY_NAME)|RUN_|JOB_URL|_TIMESTAMP|HUDSON_COOKIE|"
                           r"JENKINS_SERVER_COOKIE|WORKSPACE_TMP")


_NODE_SCAN = 30  # newest builds checked for good_on_node / bad_on_node
_NODE_LOGS = 10  # Pipeline builds: logs read at most to find the node


def build_on_node(t: Target, node: str, good: bool) -> str:
    """Number of the newest successful (good) or failed/unstable build that ran on `node` (substring)."""
    wanted = ("SUCCESS",) if good else ("FAILURE", "UNSTABLE")
    d = t.inst.json(f"{t.path}/api/json", tree=f"builds[number,result,building,builtOn]{{0,{_NODE_SCAN}}}") or {}
    key, logs_read = node.strip().lower(), 0
    for b in d.get("builds") or []:
        if b.get("building") or b.get("result") not in wanted:
            continue
        if b.get("builtOn"):  # freestyle & co.
            if key in str(b["builtOn"]).lower():
                return str(b["number"])
            continue
        if logs_read >= _NODE_LOGS:  # Pipeline: the node is only in the log
            break
        logs_read += 1
        cand = resolve(t.full_name, b["number"], t.inst.name)
        if any(key in n.lower() for n in load_log(cand, build_info(cand, light=True)).nodes):
            return str(b["number"])
    kind = "successful" if good else "failed or unstable"
    raise ToolError(f"[{t.inst.name}] No {kind} build of '{t.full_name}' ran on a node matching '{node}' among the "
                    f"last {_NODE_SCAN} builds (read {logs_read} Pipeline logs). Check the name with node_health.")


@mcp.tool(annotations=READ_ONLY)
def compare_builds(job: Job, good: Build = "lastSuccessfulBuild", bad: Build = "lastFailedBuild",
                   instance: InstanceName = "",
                   good_on_node: Annotated[str, Field(description="Pick the newest successful build that ran on this "
                                                                  "node (overrides good)")] = "",
                   bad_on_node: Annotated[str, Field(description="Pick the newest failed/unstable build that ran on "
                                                                 "this node (overrides bad)")] = "",
                   other_instance: Annotated[str, Field(description="Take the GOOD build of the same job from this "
                                                                    "instance (cross-instance comparison)")] = "",
                   ) -> JsonDict:
    """What changed between a good and a bad build: result, duration, agent node, parameters,
    stage status/durations, commits in between (pipeline_changes = commits touching a Jenkinsfile or
    *.groovy), error groups that are new in the bad build,
    and env.log differences (if both builds archived env.log). Ideal for 'it worked yesterday',
    'why is node A slower than node B' and for checking whether an error also occurs in green builds.
    good_on_node / bad_on_node pick builds by agent ('works on agent-01, fails on agent-03'; Pipeline jobs
    need up to 10 log reads); other_instance compares the same job across instances (no commit range)."""
    tb = resolve(job, bad, instance)
    tg = resolve(tb.full_name, good, other_instance) if other_instance.strip() else resolve(job, good, instance)
    if good_on_node.strip():
        tg = resolve(tg.full_name, build_on_node(tg, good_on_node, True), tg.inst.name)
    if bad_on_node.strip():
        tb = resolve(tb.full_name, build_on_node(tb, bad_on_node, False), tb.inst.name)
    ig, ib = build_info(tg), build_info(tb)
    ng, nb = int(ig["number"]), int(ib["number"])
    lg, lb = load_log(tg, ig), load_log(tb, ib)
    same_job = tg.inst is tb.inst

    def label(t: Target, n: int, info: JsonDict, pl: ParsedLog) -> str:
        text = f"{'' if same_job else t.inst.name + ' '}#{n} {info.get('result')} {fmt_duration(info.get('duration'))} "
        text += fmt_age(info.get("timestamp"))
        return text + (f" on {', '.join(pl.nodes[:2])}" if pl.nodes else "")

    out: JsonDict = {"instance": tb.inst.name if same_job else f"{tg.inst.name} (good) vs {tb.inst.name} (bad)",
                     "job": tb.full_name, "good": label(tg, ng, ig, lg), "bad": label(tb, nb, ib, lb)}
    if lg.nodes != lb.nodes:
        out["agents"] = {"good": lg.nodes[:4], "bad": lb.nodes[:4]}
    pg, pb = build_params(ig), build_params(ib)
    pdiff = {k: f"{pg.get(k)} -> {pb.get(k)}" for k in sorted(set(pg) | set(pb)) if pg.get(k) != pb.get(k)}
    if pdiff:
        out["parameter_changes"] = pdiff
    sg, _ = pipeline_stages(tg, ng, lg)
    sb, _ = pipeline_stages(tb, nb, lb)
    gmap = {s["name"]: s for s in sg}
    sdiff = []
    for s in sb:
        g = gmap.get(s["name"])
        if not g:
            sdiff.append(f"{s['name']}: only in bad ({s.get('status')})")
        elif g.get("status") != s.get("status") or g.get("duration") != s.get("duration"):
            sdiff.append(f"{s['name']}: {g.get('status')} {g.get('duration', '')} -> {s.get('status')} {s.get('duration', '')}"
                         + (f" - {s['error']}" if s.get("error") else ""))
    if sdiff:
        out["stage_changes"] = sdiff
    if same_job and nb > ng:
        # builds{a,b} indexes the newest-first build list -> offset from the newest build;
        # the margin of 5 covers deleted builds that shift the list
        newest = ((tg.inst.json(f"{tg.path}/api/json", tree="lastBuild[number]") or {}).get("lastBuild") or {}).get("number")
        lo = max(0, int(newest or nb) - nb - 5)
        hi = min(lo + 100, int(newest or nb) - ng + 5)
        d = tg.inst.json(f"{tg.path}/api/json",
                         tree=f"builds[number,changeSets[items[msg,commitId,author[fullName],affectedPaths]]]"
                              f"{{{lo},{hi}}}") or {}
        commits, pipeline = [], []
        for b in d.get("builds") or []:
            if not ng < (b.get("number") or 0) <= nb:
                continue
            for i in (i for cs in b.get("changeSets") or [] for i in cs.get("items") or []):
                row = (f"#{b['number']} {str(i.get('commitId', ''))[:8]} "
                       f"{((i.get('author') or {}).get('fullName') or '?')}: {clip((i.get('msg') or '').strip(), 90)}")
                commits.append(row)
                paths = [str(x) for x in i.get("affectedPaths") or [] if _PIPELINE_FILE.search(str(x))]
                if paths:
                    pipeline.append(f"{row} ({', '.join(paths[:3])})")
        out["commits_between"] = commits[:20] or ["none recorded"]
        if pipeline:
            out["pipeline_changes"] = pipeline[:10]
    keys_g = {g.key for g in lg.groups}
    new = [g for g in lb.groups if g.key not in keys_g]
    also_in_good = [g for g in lb.groups if g.key in keys_g and g.severity == "high"]
    kb = kb_match.analyse(lb, tb.inst.name, tb.full_name)
    out["errors"] = {"good_distinct": len(lg.groups), "bad_distinct": len(lb.groups), "new_in_bad": len(new),
                     "new_in_bad_first": [g.render(kb.notes.get(g.key, ""))
                                          for g in ranked_causes(new, kb, None, 6)]}
    if also_in_good:
        out["errors"]["also_in_good_build"] = [f"L{g.first_line} {clip(mask(g.text), 120)}" for g in also_in_good[:8]]
        out["errors"]["hint"] = ("errors that also occur in the good build are probably not the cause; "
                                 "consider adding them as red_herrings to a knowledge base entry")
    try:
        eg, eb = env_log_of(tg, ng), env_log_of(tb, nb)
    except ToolError:
        eg = eb = None
    if eg is not None and eb is not None:
        def kv(lines: list[str]) -> dict[str, str]:
            return dict(ln.split("=", 1) for ln in lines if "=" in ln)
        a, b = kv(eg), kv(eb)
        diff = [f"{k}: {clip(a.get(k), 80)} -> {clip(b.get(k), 80)}" for k in sorted(set(a) | set(b))
                if a.get(k) != b.get(k) and not _VOLATILE_ENV.search(k)]
        out["env_changes"] = diff[:40] + ([f"... +{len(diff) - 40} more"] if len(diff) > 40 else [])
    out["note"] = UNTRUSTED
    return out
