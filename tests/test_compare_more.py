"""compare_builds across nodes (latest good/failed build that ran on a node) and across instances."""

import json

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="module")

JOB = "cd/master/app-deploy"


async def call(c, tool: str, **args) -> tuple[str, bool]:
    res = await c.call_tool(tool, args, raise_on_error=False)
    return "\n".join(getattr(b, "text", "") for b in res.content), res.is_error


async def test_good_build_picked_by_node(client):
    t, err = await call(client, "compare_builds", job=JOB, good_on_node="build-agent-01", bad_on_node="agent-02")
    assert not err, t
    d = json.loads(t)
    assert d["good"].startswith("#41 SUCCESS") and "on build-agent-01" in d["good"]
    assert d["bad"].startswith("#42 FAILURE") and "on build-agent-02" in d["bad"]
    assert d["agents"] == {"good": ["build-agent-01"], "bad": ["build-agent-02"]}


async def test_no_build_on_that_node(client):
    t, err = await call(client, "compare_builds", job=JOB, good_on_node="build-agent-09")
    assert err and "build-agent-09" in t and "no successful build" in t.lower()


async def test_same_job_on_another_instance(client):
    t, err = await call(client, "compare_builds", job=JOB, other_instance="beta")
    assert not err, t
    d = json.loads(t)
    assert d["good"].startswith("beta #41 SUCCESS") and d["bad"].startswith("alpha #42 FAILURE")
    assert "commits_between" not in d, "commit ranges make no sense across instances"
    assert "JAVA_HOME: /opt/jdk17 -> /opt/jdk21" in t, "env.log differences across instances"
    assert d["errors"]["new_in_bad"] > 0
