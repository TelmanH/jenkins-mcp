"""Cross-cutting checks: every Jenkins answer names its instance (D3), the tool list stays small (it is
sent to the model with every request), the job tree is cached briefly, and builds removed by log rotation
get a clear message."""

import json

import pytest

from fake_jenkins import ALPHA, CALLS

pytestmark = pytest.mark.asyncio(loop_scope="module")

JOB = "cd/master/app-deploy"
TOOL_LIST_BUDGET = 45000  # characters of names, descriptions and schemas of all tools


async def call(c, tool: str, **args) -> tuple[str, bool]:
    res = await c.call_tool(tool, args, raise_on_error=False)
    return "\n".join(getattr(b, "text", "") for b in res.content), res.is_error


CALLS_WITH_INSTANCE = {
    "list_jobs": {}, "failures_overview": {}, "recent_builds": {}, "find_jobs_by_scm": {"url": "app"},
    "get_job": {"job": JOB}, "get_build_summary": {"job": JOB, "build": 42},
    "get_stage_log": {"job": JOB, "build": 42}, "get_log_errors": {"job": JOB, "build": 42},
    "search_log": {"job": JOB, "build": 42, "pattern": "helm"}, "get_log_lines": {"job": JOB, "build": 42},
    "get_test_failures": {"job": JOB, "build": 42}, "compare_builds": {"job": JOB},
    "list_artifacts": {"job": JOB, "build": 42}, "read_artifact": {"job": JOB, "build": 42, "path": "logs/env.log"},
    "get_env_log": {"job": JOB, "build": 42}, "compare_artifacts": {"job": JOB},
    "node_health": {}, "get_node": {"name": "build-agent-01"}, "get_queue": {},
    "get_pipeline_info": {"job": JOB, "build": 42}, "get_pending_inputs": {"job": JOB, "build": 42},
    "get_job_config": {"job": JOB}, "get_job_config_history": {"job": JOB}, "diff_job_config": {"job": JOB},
    "read_managed_file": {"id": "maven-settings-nexus"},
    "list_workspace": {"job": JOB, "build": 42, "node_id": "5"},
    "read_workspace_file": {"job": JOB, "build": 42, "node_id": "5", "path": "pom.xml"},
    "recurring_errors": {"job": JOB, "last_n": 2}, "find_known_issues": {"job": JOB, "build": 42},
}
NO_INSTANCE = {"list_instances", "get_known_issue", "list_known_issues", "draft_kb_entry", "propose_kb_update"}


async def test_every_jenkins_answer_names_its_instance(client):
    names = {t.name for t in await client.list_tools()}
    assert names == set(CALLS_WITH_INSTANCE) | NO_INSTANCE, "new tool: add it to CALLS_WITH_INSTANCE"
    for tool, args in CALLS_WITH_INSTANCE.items():
        t, err = await call(client, tool, **args)
        assert not err, f"{tool}: {t[:300]}"
        assert "alpha" in t, f"{tool} does not name the instance: {t[:200]}"


async def test_tool_list_stays_small(client):
    tools = await client.list_tools()
    size = sum(len(t.name) + len(t.description or "") + len(json.dumps(t.input_schema)) for t in tools)
    assert size < TOOL_LIST_BUDGET, f"tool list is {size} characters - every request pays for it"


async def test_job_tree_cached_briefly(client):
    before = sum(c == f"GET {ALPHA}/api/json?tree=" or c.startswith(f"GET {ALPHA}/api/json?tree=jobs") for c in CALLS)
    await call(client, "list_jobs")
    await call(client, "list_jobs", status="FAILURE")
    await call(client, "failures_overview", instance="alpha")
    after = sum(c.startswith(f"GET {ALPHA}/api/json?tree=jobs") for c in CALLS)
    assert after - before <= 1, "the same job tree is fetched once within the cache time"


async def test_build_removed_by_log_rotation(client):
    t, err = await call(client, "get_build_summary", job=JOB, build=5)
    assert err and "has no build '5'" in t and "removed" in t and "#41" in t, t
    t, err = await call(client, "get_build_summary", job=JOB, build=999)
    assert err and "newest is #42" in t
