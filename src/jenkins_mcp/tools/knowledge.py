"""Knowledge base tools: find_known_issues, get_known_issue, list_known_issues, draft_kb_entry,
propose_kb_update.

Only draft_kb_entry and propose_kb_update write, and only a NEW local file under knowledge/drafts/.
Drafts are not used for matching until a human reviews them and moves them into knowledge/
(human in the loop).
"""

import re
import unicodedata
from datetime import date
from typing import Annotated

import yaml
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..app import LOCAL_WRITE, READ_ONLY, Build, InstanceName, mcp
from ..kb import match as kb_match
from ..kb import store
from ..kb.store import FLAGS, as_list
from ..logs import load_log
from ..settings import HOME, JsonDict
from ..targets import build_info, resolve
from ..text import budget, clip, mask

OptionalJob = Annotated[str, Field(description="Job full name or Jenkins URL. Empty = not tied to a build.")]
Regexes = Annotated[list[str], Field(description="Python regexes (case-insensitive). Use specific phrases "
                                                 "copied from the log, not generic words like 'ERROR'.")]


@mcp.tool(annotations=READ_ONLY)
def find_known_issues(job: OptionalJob = "", build: Build = "", instance: InstanceName = "",
                      text: Annotated[str, Field(description="Error text to look up instead of a build")] = "",
                      include_drafts: bool = False) -> JsonDict:
    """Look up the team knowledge base. With job (+build) the build log is matched against every
    entry in scope (instance/job); with text, a pasted error message is matched. Returns matching
    entries with the first matching log line and the documented fix. get_build_summary already does
    this automatically - use this tool for a pasted error or before writing a new entry."""
    base = store.get()
    out: JsonDict = {"knowledge_base": f"{len(base.entries)} entries, {len(base.drafts)} drafts"}
    if base.errors:
        out["kb_errors"] = base.errors[:10]
    if job.strip():
        t = resolve(job, build, instance)
        info = build_info(t, light=True)
        pl = load_log(t, info)
        kb = kb_match.analyse(pl, t.inst.name, t.full_name)
        out.update({"instance": t.inst.name, "job": t.full_name, "build": info["number"]})
        out["matches"] = [h.render() for h in kb.hits] or ["no known issue matches this build"]
        noise = sorted(set(kb.notes.values()))
        if noise:
            out["annotated_error_groups"] = noise
        return out
    if not text.strip():
        raise ToolError("Pass either job (+build) or text.")
    hits = kb_match.match_text(text, instance, "", include_drafts)
    out["matches"] = [h.render() for h in hits] or ["no known issue matches this text"]
    return out


@mcp.tool(annotations=READ_ONLY)
def get_known_issue(id: Annotated[str, Field(description="Entry id like KB-0001 (drafts too)")]) -> str:
    """Full text of one knowledge base entry: scope, symptoms, root cause, fix, how it was found,
    red herrings. Content is written by the team (reviewed) - drafts are marked as unreviewed."""
    base = store.get()
    e = base.find(id)
    if e is None:
        known = ", ".join(x.id for x in base.entries[:30]) or "none"
        raise ToolError(f"No knowledge base entry '{id}'. Known: {known}. Use list_known_issues.")
    head = f"{e.id} - {e.title}\nfile: {e.path.relative_to(base.dir.parent) if base.dir.parent in e.path.parents else e.path}"
    if e.draft:
        head += "\nDRAFT - not reviewed yet, treat with care"
    return budget(head + "\n\n" + e.path.read_text(encoding="utf-8"))


@mcp.tool(annotations=READ_ONLY)
def list_known_issues(query: str = "", instance: InstanceName = "", category: str = "",
                      include_drafts: bool = False) -> JsonDict:
    """List knowledge base entries: 'id | status | category | scope | title', plus how many builds each
    entry matched since the server started. query = case-insensitive text filter on title and content."""
    base = store.get()
    pool = base.entries + (base.drafts if include_drafts else [])
    q = query.lower().strip()
    listed = [e for e in pool
              if (not q or q in f"{e.title}\n{e.body}".lower())
              and (not category or e.category.lower() == category.lower().strip())
              and e.in_scope(instance.strip(), "")]
    rows = [e.row() for e in listed]
    out: JsonDict = {"dir": str(base.dir), "count": len(rows), "entries": rows[:100]}
    ids = {e.id for e in listed}
    if stats := {k: v for k, v in kb_match.hit_stats().items() if k in ids}:
        out["matched_since_start"] = stats
        out["stats_note"] = "builds each entry matched since the server started (in memory, not persisted)"
    if base.drafts and not include_drafts:
        out["drafts_waiting_for_review"] = [d.path.name for d in base.drafts[:20]]
    if base.errors:
        out["errors"] = base.errors[:10]
    return out


def _slug(title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    return s[:50].rstrip("-") or "entry"


def _validate(patterns: list[str], field: str) -> list[str]:
    clean = []
    for p in patterns or []:
        p = str(p).strip()
        if not p:
            continue
        try:
            rx = re.compile(p, FLAGS)
        except re.error as e:
            raise ToolError(f"{field}: invalid regex {p!r}: {e}") from None
        if len(p) < 6 or rx.search(""):
            raise ToolError(f"{field}: {p!r} is too generic - use a specific phrase from the log.")
        clean.append(p)
    return clean


@mcp.tool(annotations=LOCAL_WRITE)
def draft_kb_entry(
    title: Annotated[str, Field(description="Short title: symptom -> cause")],
    root_cause: Annotated[str, Field(description="The confirmed root cause")],
    match: Regexes,
    solution: Annotated[str, Field(description="How it was / is fixed. Empty if not known yet.")] = "",
    symptoms: str = "",
    how_found: Annotated[str, Field(description="How the cause was identified (searches, comparisons)")] = "",
    red_herrings: Annotated[list[str], Field(description="Regexes of errors that appeared but were NOT the cause "
                                                         "(e.g. also present in green builds)")] = [],  # noqa: B006
    consequences: Annotated[list[str], Field(description="Regexes of follow-up errors caused by this issue")] = [],  # noqa: B006
    job: OptionalJob = "", build: Build = "", instance: InstanceName = "",
    version: Annotated[str, Field(description="Product/application version of the build, if relevant")] = "",
    stage: str = "", category: str = "",
    status: Annotated[str, Field(description="open | solved | workaround")] = "open",
) -> JsonDict:
    """Write a NEW knowledge base draft (Markdown) to knowledge/drafts/ for human review.
    Call this at the end of a debugging session once the user confirmed the root cause.
    It never touches Jenkins and never overwrites a file. Drafts are not used for matching
    until a human reviews them and moves them into knowledge/ (then commit to Git)."""
    if not title.strip() or not root_cause.strip():
        raise ToolError("title and root_cause are required.")
    match_p = _validate(match, "match")
    if not match_p:
        raise ToolError("At least one 'match' regex is required, otherwise the entry can never be found.")
    red_p = _validate(red_herrings, "red_herrings")
    cons_p = _validate(consequences, "consequences")

    warnings: list[str] = []
    builds: list[str] = []
    job_scope = job.strip()
    inst_scope = instance.strip()
    if job_scope:
        try:
            t = resolve(job_scope, build, instance)
            job_scope, inst_scope = t.full_name, t.inst.name
            if str(build).strip() or job.startswith("http"):
                builds.append(t.build_url(build_info(t, light=True)["number"]))
        except ToolError as e:
            warnings.append(f"build not resolved, kept as given: {clip(e, 200)}")

    base = store.get()
    new_rx = [re.compile(p, FLAGS) for p in match_p]
    for entry in base.entries + base.drafts:
        if any(rx.search(p) for rx in entry.match for p in match_p) or \
                any(rx.search(old.pattern) for rx in new_rx for old in entry.match):
            warnings.append(f"possible duplicate of {entry.id} ({entry.title}) - consider updating that entry instead")

    kb_id = base.next_id()
    meta: JsonDict = {"id": kb_id, "title": mask(title.strip()), "status": status.strip() or "open"}
    if inst_scope:
        meta["instance"] = inst_scope
    if job_scope:
        meta["job"] = job_scope
    for key, val in (("version", version), ("stage", stage), ("category", category)):
        if str(val).strip():
            meta[key] = str(val).strip()
    meta["match"] = match_p
    if red_p:
        meta["red_herrings"] = red_p
    if cons_p:
        meta["consequences"] = cons_p
    if builds:
        meta["builds"] = builds
    meta["date"] = date.today().isoformat()
    meta["source"] = "draft_kb_entry (review before use)"

    def section(name: str, text: str, placeholder: str) -> str:
        return f"## {name}\n{mask(text.strip()) if text.strip() else placeholder}\n"

    body = "\n".join([
        section("Symptoms", symptoms, "<!-- TODO -->"),
        section("Root cause", root_cause, "<!-- TODO -->"),
        section("Solution", solution, "<!-- TODO: solution not documented yet -->"),
        section("How it was found", how_found, "<!-- TODO -->"),
    ])
    content = "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000) + "---\n\n" + body

    drafts = base.dir / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    path = drafts / f"{kb_id}-{_slug(title)}.md"
    with open(path, "x", encoding="utf-8") as f:  # 'x' = never overwrite
        f.write(content)
    shown = path.relative_to(HOME) if HOME in path.parents else path
    out: JsonDict = {"draft": str(shown), "id": kb_id,
                     "next": (f"Review {shown}; when correct move it to {base.dir.name}/ and commit. "
                              "Drafts are not used for matching until then.")}
    if warnings:
        out["warnings"] = warnings
    return out


@mcp.tool(annotations=LOCAL_WRITE)
def propose_kb_update(
    id: Annotated[str, Field(description="Reviewed entry to update, e.g. KB-0001")],
    job: OptionalJob = "", build: Build = "", instance: InstanceName = "",
    add_match: Regexes = [],  # noqa: B006
    add_red_herrings: Regexes = [],  # noqa: B006
    add_consequences: Regexes = [],  # noqa: B006
    note: Annotated[str, Field(description="What is new: another occurrence, a better fix, a wrong regex...")] = "",
) -> JsonDict:
    """Propose an update of an EXISTING knowledge base entry instead of writing a duplicate: writes a NEW
    draft next to it in knowledge/drafts/ with the build URL added to `builds`, the extra regexes merged in
    and an 'Update proposal' section. Never touches the entry itself or Jenkins, never overwrites a file.
    Reports whether the entry matches the given build."""
    base = store.get()
    entry = base.find(id)
    if entry is None or entry.draft:
        known = ", ".join(x.id for x in base.entries[:30]) or "none"
        raise ToolError(f"No reviewed knowledge base entry '{id}'. Known: {known}. For a new problem use draft_kb_entry.")
    added = {"match": _validate(add_match, "add_match"), "red_herrings": _validate(add_red_herrings, "add_red_herrings"),
             "consequences": _validate(add_consequences, "add_consequences")}
    if not job.strip() and not any(added.values()) and not note.strip():
        raise ToolError("Nothing to propose: pass job/build, add_* regexes and/or a note.")
    meta: JsonDict = dict(entry.meta)
    for key, values in added.items():
        old = as_list(meta.get(key))
        if new := [v for v in values if v not in old]:
            meta[key] = old + new
    out: JsonDict = {"id": entry.id}
    lines = [f"- date: {date.today().isoformat()}"]
    if job.strip():
        t = resolve(job, build, instance)
        info = build_info(t, light=True)
        url = t.build_url(info["number"])
        matched = any(h.entry.id == entry.id
                      for h in kb_match.analyse(load_log(t, info), t.inst.name, t.full_name).hits)
        out["matches_this_build"] = matched
        meta["builds"] = as_list(meta.get("builds")) + ([url] if url not in as_list(meta.get("builds")) else [])
        lines.append(f"- build: {url} (entry matches it: {'yes' if matched else 'no'})")
        if not matched and not added["match"]:
            out["warnings"] = ["the entry does not match this build - add a match regex (add_match), "
                               "otherwise it will not be found next time"]
    for key, values in added.items():
        if values:
            lines.append(f"- added {key}: " + ", ".join(f"`{v}`" for v in values))
    if note.strip():
        lines.append(f"- note: {mask(note.strip())}")
    meta["source"] = "propose_kb_update (review before use)"
    content = ("---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000) + "---\n\n"
               + entry.body.rstrip() + "\n\n## Update proposal\n" + "\n".join(lines) + "\n")
    drafts = base.dir / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    stem = f"{entry.id}-update-{date.today().isoformat()}"
    path = drafts / f"{stem}.md"
    n = 2
    while path.exists():
        path, n = drafts / f"{stem}-{n}.md", n + 1
    with open(path, "x", encoding="utf-8") as f:  # 'x' = never overwrite
        f.write(content)
    shown = path.relative_to(HOME) if HOME in path.parents else path
    out["draft"] = str(shown)
    out["next"] = (f"Review {shown}; when correct replace {entry.path.name} with it (keep the file name) and commit. "
                   "Drafts are not used for matching until then.")
    return out
