"""Regression tests for the review fixes and tool paths the first suites did not reach:
commit window of compare_builds, timestamp-independent search/KB matching, URLs copied from views,
console-only stage detection (no Stage View plugin), artifacts, prompts."""

import json

import pytest

from fake_jenkins import ALPHA
from jenkins_mcp.tools import artifacts

pytestmark = pytest.mark.asyncio(loop_scope="module")

JOB = "cd/master/app-deploy"


async def call(c, name: str, **args) -> tuple[str, bool]:
    res = await c.call_tool(name, args, raise_on_error=False)
    return "\n".join(getattr(b, "text", "") for b in res.content), res.is_error


# ----------------------------------------------------------------------------- compare_builds commit window
async def test_compare_builds_finds_commits_far_from_the_newest_build(client):
    # beta/core-build has builds 1..100; the builds{a,b} range of the API counts from the newest build
    t, err = await call(client, "compare_builds", job="core-build", instance="beta", good=40, bad=42)
    assert not err, t
    commits = json.loads(t)["commits_between"]
    assert any("#41" in c and "change 41" in c for c in commits), commits
    assert any("#42" in c for c in commits) and not any("#40 " in c or "#43 " in c for c in commits)


# ----------------------------------------------------------------------------- console-only stages
async def test_stages_from_console_when_stage_view_plugin_missing(client):
    t, err = await call(client, "get_build_summary", job="core-build", instance="beta", build=42)
    assert not err, t
    s = json.loads(t)
    assert s["stage_source"] == "console"
    assert any(x.startswith("Publish: SKIPPED") for x in s["stages"])
    assert s["failed_stage"] == "Deploy", "derived from the first real error"
    causes = s["errors"]["likely_root_cause"]
    assert "[generic] L11 stage: Deploy" in causes[0] and "deploy target unreachable" in causes[0], \
        "a generic error line outranks 'script returned exit code 1'"


async def test_no_test_report(client):
    t, _ = await call(client, "get_test_failures", job="core-build", instance="beta", build=42)
    assert json.loads(t)["has_test_report"] is False


# ----------------------------------------------------------------------------- timestamps are invisible to search
async def test_search_anchor_matches_what_the_user_sees(client):
    # every line of build 42 starts with a [timestamp]; '^' must anchor at the visible text
    t, _ = await call(client, "search_log", job=JOB, build=42, pattern=r"^Error: UPGRADE FAILED")
    assert t.startswith("1 matching lines"), t[:200]
    t, _ = await call(client, "search_log", job="core-build", instance="beta", build=42, pattern=r"^Error: deploy")
    assert t.startswith("1 matching lines"), t[:200]


async def test_find_known_issues_by_build(client):
    t, err = await call(client, "find_known_issues", job=JOB, build=42)
    assert not err
    d = json.loads(t)
    assert d["matches"][0].startswith("KB-9001") and "first L" in d["matches"][0]
    assert "known noise (KB-9001)" in d["annotated_error_groups"]
    t, _ = await call(client, "find_known_issues", job="core-build", instance="beta", build=42)
    assert "no known issue matches this build" in t
    t, err = await call(client, "find_known_issues")
    assert err and "either job" in t


# ----------------------------------------------------------------------------- URLs from views
@pytest.mark.parametrize("url", [
    f"{ALPHA}/view/Deploy/job/cd/job/master/job/app-deploy/42/",
    f"{ALPHA}/view/All/view/Nested/job/cd/job/master/job/app-deploy/42/console",
])
async def test_url_copied_from_a_view(client, url):
    t, err = await call(client, "get_build_summary", job=url)
    assert not err, t
    assert json.loads(t)["build"] == 42


# ----------------------------------------------------------------------------- artifacts
async def test_read_text_artifact_window_tail_and_grep(client):
    t, err = await call(client, "read_artifact", job=JOB, build=42, path="logs/env.log", start=1, count=2)
    assert not err and t.startswith("logs/env.log (") and "     1| " in t and "     3| " not in t
    t, _ = await call(client, "read_artifact", job=JOB, build=42, path="logs/env.log", start=0, count=1)
    assert len([ln for ln in t.splitlines() if "| " in ln]) == 1, "start=0 -> tail"
    t, _ = await call(client, "read_artifact", job=JOB, build=42, path="logs/env.log", pattern="java_home")
    assert "1 matches" in t and "/opt/jdk21" in t, "grep is case-insensitive"
    t, _ = await call(client, "read_artifact", job=JOB, build=42, path="logs/env.log", pattern="TOKEN")
    assert "abc123" not in t and "****" in t, "env-style artifacts are masked"


async def test_read_artifact_missing(client):
    t, err = await call(client, "read_artifact", job=JOB, build=42, path="nope.txt")
    assert err and "not found" in t and "list_artifacts" in t


async def test_read_artifact_reports_cut_off(client, monkeypatch):
    monkeypatch.setattr(artifacts, "_ARTIFACT_CAP", 20)
    t, err = await call(client, "read_artifact", job=JOB, build=42, path="logs/env.log")
    assert not err and "cut off after the first 20 MB" in t.splitlines()[0]


async def test_env_log_filter_and_missing(client):
    t, _ = await call(client, "get_env_log", job=JOB, build=42, pattern="JAVA")
    assert "JAVA_HOME=/opt/jdk21" in t and "PWD=" not in t
    t, _ = await call(client, "get_env_log", job="core-build", instance="beta", build=42)
    assert t == "Build #42 on beta has no env.log artifact."


# ----------------------------------------------------------------------------- prompts
async def test_prompts_render(client):
    r = await client.get_prompt("analyse_build", {"build_url": "https://jenkins.example.com/job/x/1/"})
    text = r.messages[0].content.text
    assert "https://jenkins.example.com/job/x/1/" in text and "get_build_summary" in text and "untrusted" in text
    r = await client.get_prompt("document_session", {})
    assert "draft_kb_entry" in r.messages[0].content.text
    assert "propose_kb_update" in r.messages[0].content.text, "existing entries are updated, not duplicated"
    r = await client.get_prompt("morning_report", {"hours": "12"})
    text = r.messages[0].content.text
    for tool in ("list_instances", "failures_overview", "recent_builds", "node_health", "get_queue"):
        assert tool in text, tool
    assert "last 12 hours" in text and "every instance" in text
