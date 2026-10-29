"""Job configuration history (Job Config History plugin): get_job_config_history, diff_job_config."""

import difflib
from typing import Annotated, Any
from urllib.parse import quote

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..app import READ_ONLY, InstanceName, Job, mcp
from ..settings import UNTRUSTED, JsonDict
from ..targets import Target, resolve
from ..text import budget, clip, mask
from ..xmlutil import parse_xml
from .jobconfig import fetch_config_xml, summarize_config

CURRENT = "current"
_CONFIG_AS_CODE_USERS = {"system"}


def _history(t: Target) -> list[JsonDict]:
    """Versions newest first: {'date': '2026-10-21_09-00-00', 'operation', 'user', 'userID'}."""
    try:
        d = t.inst.json(f"{t.path}/jobConfigHistory/api/json", allow_404=True)
    except ToolError as e:
        if "(403)" in str(e):
            raise ToolError(f"[{t.inst.name}] Config history of '{t.full_name}' is not readable (403): the token's "
                            "user needs Job/ExtendedRead or Job/Configure.") from None
        raise
    if d is None:
        raise ToolError(f"[{t.inst.name}] No config history for '{t.full_name}': the Job Config History plugin is "
                        "not installed on this instance, or it has no history for this job. get_job_config shows "
                        "the current configuration.")
    entries = d.get("jobConfigHistory") or d.get("configs") or []
    rows = [e for e in entries if isinstance(e, dict) and e.get("date")]
    return sorted(rows, key=lambda e: str(e["date"]), reverse=True)


def _row(e: JsonDict) -> str:
    user, uid = str(e.get("user") or "?"), str(e.get("userID") or "")
    who = f"{user} ({uid})" if uid and uid != user else user
    return f"{e['date']} | {e.get('operation') or '?'} | {who}"


def _version_xml(t: Target, stamp: str) -> str:
    if stamp == CURRENT:
        return fetch_config_xml(t)
    r = t.inst.get(f"{t.path}/jobConfigHistory/configOutput?type=xml&timestamp={quote(stamp, safe='')}",
                   allow_404=True)
    if r is None:
        raise ToolError(f"[{t.inst.name}] Version {stamp} of '{t.full_name}' could not be read.")
    return r.text


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, list):
        return clip("; ".join(str(v) for v in value), 300)
    return clip(value, 300)


@mcp.tool(annotations=READ_ONLY)
def get_job_config_history(job: Job, instance: InstanceName = "", limit: int = 20) -> JsonDict:
    """Who changed a job's configuration and when (Job Config History plugin), newest first:
    'timestamp | operation | user'. Use the timestamps with diff_job_config. Changes by SYSTEM usually come
    from JCasC / JobDSL seed jobs: then the source of truth is Git and UI changes get overwritten."""
    t = resolve(job, "", instance)
    hist = _history(t)
    out: JsonDict = {"instance": t.inst.name, "job": t.full_name, "count": len(hist),
                     "versions": [_row(e) for e in hist[: max(1, min(limit, 200))]]}
    if any(str(e.get("userID") or e.get("user") or "").lower() in _CONFIG_AS_CODE_USERS for e in hist):
        out["hint"] = ("changes by SYSTEM come from JCasC or a JobDSL seed job (then the source of truth is in Git "
                       "and UI changes get overwritten), or from a Declarative Pipeline saving its own "
                       "parameters/triggers/options during a build - diff_job_config shows which")
    if len(hist) >= 2:
        out["next_steps"] = [f"diff_job_config(older='{hist[1]['date']}', newer='{hist[0]['date']}') for the last "
                             "change, newer='current' to compare with the live config"]
    return out


@mcp.tool(annotations=READ_ONLY)
def diff_job_config(job: Job,
                    older: Annotated[str, Field(description="Timestamp from get_job_config_history; empty = the "
                                                            "version before `newer`")] = "",
                    newer: Annotated[str, Field(description="Timestamp, or 'current' for the live config.xml; empty "
                                                            "= the newest version")] = "",
                    instance: InstanceName = "") -> JsonDict:
    """What changed between two configuration versions of a job: a semantic list ('triggers: cron: H 3 * * *
    -> cron: H 2 * * *', parameters, SCM, script path, retention...) plus a unified diff of the XML.
    Secrets are masked in both. Default: the newest version against the one before it."""
    t = resolve(job, "", instance)
    stamps = [str(e["date"]) for e in _history(t)]
    if not stamps:
        raise ToolError(f"[{t.inst.name}] '{t.full_name}' has no config history versions.")
    if len(stamps) == 1 and not older.strip() and newer.strip() in ("", stamps[0]):
        raise ToolError(f"[{t.inst.name}] '{t.full_name}' has only one version in its history ({stamps[0]}): the "
                        "plugin records changes from the time it was installed, so there is nothing older to compare "
                        "with. get_job_config shows the current configuration.")
    newer = newer.strip() or stamps[0]
    if newer == CURRENT:
        older = older.strip() or stamps[0]
    else:
        idx = stamps.index(newer) if newer in stamps else -1
        older = older.strip() or (stamps[idx + 1] if 0 <= idx < len(stamps) - 1 else "")
    for stamp in (older, newer):
        if stamp != CURRENT and stamp not in stamps:
            raise ToolError(f"Unknown version '{stamp or '(none before the oldest)'}'. Available: "
                            f"{', '.join(stamps[:15])}{' ...' if len(stamps) > 15 else ''}, or 'current'.")
    a, b = _version_xml(t, older), _version_xml(t, newer)
    sa, sb = summarize_config(parse_xml(a)), summarize_config(parse_xml(b))
    changes = [f"{k}: {_fmt(sa.get(k))} -> {_fmt(sb.get(k))}"
               for k in dict.fromkeys([*sa, *sb]) if sa.get(k) != sb.get(k)]
    diff = list(difflib.unified_diff([mask(x) for x in a.splitlines()], [mask(x) for x in b.splitlines()],
                                     fromfile=older, tofile=newer, lineterm="", n=2))
    return {"instance": t.inst.name, "job": t.full_name, "older": older,
            "newer": "current (live config.xml)" if newer == CURRENT else newer,
            "changes": changes or ["no difference in the summarized fields (see diff)"],
            "diff": budget("\n".join(diff), 6000) if diff else "identical",
            "note": UNTRUSTED}
