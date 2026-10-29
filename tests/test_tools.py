"""End-to-end test of every MCP tool through the MCP protocol, against the fake Jenkins.

Tests in this module share one client and run in file order (the knowledge base tests write a draft).
"""

import json
from pathlib import Path

import pytest

from fake_jenkins import ALPHA, CALLS, LOGS
from jenkins_mcp.settings import MAX_CHARS

pytestmark = pytest.mark.asyncio(loop_scope="module")

JOB = "cd/master/app-deploy"


async def call(c, name: str, **args) -> tuple[str, bool]:
    res = await c.call_tool(name, args, raise_on_error=False)
    return "\n".join(getattr(b, "text", "") for b in res.content), res.is_error


async def test_only_kb_draft_tools_write(client):
    tools = await client.list_tools()
    assert len(tools) == 34
    writers = [t.name for t in tools if not (t.annotations and t.annotations.read_only_hint)]
    assert writers == ["draft_kb_entry", "propose_kb_update"]
    for name in writers:
        a = next(t for t in tools if t.name == name).annotations
        assert a.destructive_hint is False and a.open_world_hint is False, "local, non-destructive write only"


async def test_list_instances(client):
    t, _ = await call(client, "list_instances", check_connection=True)
    assert "2.462.3" in t and "2.479.1" in t, "both instances reachable"
    li = json.loads(t)
    assert "config_errors" not in li, "no config errors ('pattern' alias accepted)"
    assert li["knowledge_base"]["entries"] == 3 and "errors" not in li["knowledge_base"]


async def test_list_jobs(client):
    t, _ = await call(client, "list_jobs")
    assert "release/release%2F2.4 | UNSTABLE" in t, "multibranch branch listed with encoded name"
    assert "BuildManagement/branch-create | BUILDING" in t, "depth-limited folder fetched separately"
    assert "BuildManagement/nexus-cleanup | DISABLED" in t, "disabled job detected"

    t, _ = await call(client, "list_jobs", status="FAILURE,UNSTABLE")
    assert '"matched":2' in t.replace(" ", ""), "status filter"

    t, _ = await call(client, "list_jobs", instance="beta")
    assert "core-build" in t, "switch instance by name"


async def test_failures_overview(client):
    t, _ = await call(client, "failures_overview", hours=24)
    assert "cd/master/app-deploy" in t and "core-build" in t, "overview across all instances"


async def test_get_job(client):
    t, _ = await call(client, "get_job", job=JOB)
    params = json.loads(t)["parameters"]
    assert "****" in params[1] or "default=" not in params[1], "password parameter default masked"


async def test_build_summary(client):
    t, _ = await call(client, "get_build_summary", job=JOB, build="lastFailedBuild")
    s = json.loads(t)
    assert s["failed_stage"] == "Deploy"
    assert any("UPGRADE FAILED" in x for x in s["errors"]["likely_root_cause"]), "helm error is a root-cause candidate"
    assert s["known_issues"][0].startswith("KB-9001") and "values.yaml" in s["known_issues"][0], \
        "known issue from KB with fix line"
    assert not any("KB-9002" in x for x in s["known_issues"]), "KB entry of another instance ignored"
    assert not any("CrashLoopBackOff" in x or "SYSTEM:" in x for x in s["errors"]["likely_root_cause"]), \
        "consequence and red herring removed from root causes"
    assert any("known noise (KB-9001)" in x for x in s["errors"].get("known_noise", []))
    assert s["next_steps"][0].startswith("get_known_issue('KB-9001')")
    assert s["parameters"]["NEXUS_PASSWORD"] == "****", "secret parameter masked"
    assert s["agents"] == ["build-agent-02"]
    assert len(t) < 8000, f"summary small ({len(t)} chars vs {len(LOGS[42])} raw)"
    assert "\x1b" not in t and "ha:////" not in t, "ANSI codes and console notes removed"
    assert not any("SocketTimeout" in x for x in s["errors"]["likely_root_cause"]), "WARN retries not a root cause"
    assert "similar lines" in s["failed_stage_tail"] + s["log_end"] or "Compiling module" not in t


async def test_build_summary_from_url(client):
    t, _ = await call(client, "get_build_summary", job=f"{ALPHA}/job/cd/job/master/job/app-deploy/42/console")
    assert json.loads(t)["build"] == 42, "URL input resolves instance, job and build"


async def test_stage_log(client):
    t, _ = await call(client, "get_stage_log", job=JOB, build=42)
    assert "Stage 'Deploy' (FAILED)" in t and "CrashLoopBackOff" in t


async def test_log_errors(client):
    t, _ = await call(client, "get_log_errors", job=JOB, build=42)
    assert "[java/warn]" in t and "x40" in t, "40 identical retry exceptions collapsed into one group"
    assert "more stack lines" in t, "stack traces shortened"
    assert "abcdef0123456789" not in t and "SuperSecret123" not in t, "secrets masked in log output"
    assert "script returned exit code" not in t, "generic wrapper errors hidden by default"


async def test_search_log(client):
    t, _ = await call(client, "search_log", job=JOB, build=42, pattern=r"helm|kubectl", context=1)
    assert "matching lines" in t
    t, _ = await call(client, "search_log", job=JOB, build=42, pattern="Downloading from", max_matches=3)
    assert "24000 matching" in t and "more matches" in t, "search reports total, shows few"


async def test_log_lines_budget(client):
    t, err = await call(client, "get_log_lines", job=JOB, build=42, start=0, count=15, hide_noise=True)
    assert not err and "meaningful lines" in t
    t, _ = await call(client, "get_log_lines", job=JOB, build=42, start=1, count=400)
    assert len(t) <= MAX_CHARS + 200, "output budget enforced"


async def test_test_failures(client):
    t, _ = await call(client, "get_test_failures", job=JOB, build=42)
    assert "deletePolicy" in t and '"failing_for_builds":3' in t.replace(" ", "")


async def test_compare_builds(client):
    t, _ = await call(client, "compare_builds", job=JOB)
    assert "build-agent-01" in t and "build-agent-02" in t, "agent change detected"
    assert "JAVA_HOME: /opt/jdk17 -> /opt/jdk21" in t, "env.log diff"
    assert "abc123" not in t, "env secrets masked in diff"
    assert "deadbeef" in t, "commits between builds"


async def test_artifacts(client):
    t, _ = await call(client, "get_env_log", job=JOB, build=42)
    assert "NEXUS_TOKEN=****" in t and "PWD=/home/jenkins" in t, "env.log masking (PWD kept)"
    t, err = await call(client, "list_artifacts", job=JOB, build=42)
    assert not err
    t, err = await call(client, "read_artifact", job=JOB, build=42, path="target/app.jar")
    assert err and "binary" in t, "binary artifact refused"


async def test_infra(client):
    t, _ = await call(client, "node_health")
    assert json.loads(t)["healthy"] is False and "low disk" in t and "clock drift" in t
    t, _ = await call(client, "get_queue")
    assert "cd/master/app-build" in t and "stuck" in t


async def test_knowledge_base_tools(client, tmp_home: Path):
    t, _ = await call(client, "get_log_errors", job=JOB, build=42)
    assert "consequence of KB-9001" in t and "known issue KB-9001" in t, "error groups annotated by KB"

    t, _ = await call(client, "find_known_issues", text="[ERROR] - Database drop/create failed on deploy-agent-01:")
    assert "KB-0001" in t, "pasted error text finds KB-0001"

    t, _ = await call(client, "get_known_issue", id="kb-0001")
    assert "drop_create.log" in t, "full KB entry readable"

    t, _ = await call(client, "list_known_issues", instance="main")
    assert "KB-0001" in t and "KB-9001" not in t, "list filtered by instance"


async def test_draft_kb_entry(client, tmp_home: Path):
    t, err = await call(client, "draft_kb_entry", title="x", root_cause="y", match=["ERROR"])
    assert err and "too generic" in t, "generic match regex rejected"

    t, err = await call(client, "draft_kb_entry", title="Helm timeout again", root_cause="memory",
                        match=["UPGRADE FAILED: timed out"], job=JOB, build=42, version="2.4.0",
                        red_herrings=["SocketTimeoutException: Read timed out"])
    assert not err, t
    d = json.loads(t)
    draft = tmp_home / d["draft"] if not Path(d["draft"]).is_absolute() else Path(d["draft"])
    assert d["id"] == "KB-9003" and draft.exists(), "draft written with next free id"
    assert "possible duplicate of KB-9001" in t
    text = draft.read_text(encoding="utf-8")
    assert "/42/" in text, "build URL recorded in draft"
    assert "## Root cause" in text and "version: 2.4.0" in text, "English sections and version field"

    t, _ = await call(client, "get_build_summary", job=JOB, build=42)
    assert "KB-9003" not in t, "drafts are not used for matching"
    t, _ = await call(client, "list_instances")
    assert json.loads(t)["knowledge_base"]["drafts"] == 1, "draft counted"


async def test_error_messages(client):
    t, err = await call(client, "get_build_summary", job="cd/master/does-not-exist")
    assert err and "404" in t and "list_jobs" in t, "helpful 404 message"
    t, err = await call(client, "get_build_summary", job=JOB, build=999)
    assert err and "has no build '999'" in t, "missing build vs missing job distinguished"
    t, err = await call(client, "list_jobs", instance="nope")
    assert err and "Available: alpha, beta" in t, "unknown instance lists alternatives"
    t, err = await call(client, "get_build_summary", job="https://evil.example.com/job/x/1/")
    assert err and "does not belong" in t, "foreign URL rejected (no credential leak)"


async def test_log_cached_and_no_writes(client):
    downloads = sum("app-deploy/42/consoleText" in x for x in CALLS)
    assert downloads == 1, f"log downloaded once and cached ({downloads} downloads)"
    assert not any(x.startswith("POST") for x in CALLS), "no write request ever sent"
