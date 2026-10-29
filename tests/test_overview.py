"""Overview across instances: recent_builds (time window), 'failing since' (last stable build),
long-running builds, culprits, and one unreachable instance not breaking an overview."""

import json

import pytest
from fastmcp.exceptions import ToolError

from jenkins_mcp.tools import jobs

pytestmark = pytest.mark.asyncio(loop_scope="module")

JOB = "cd/master/app-deploy"


async def call(c, name: str, **args) -> tuple[str, bool]:
    res = await c.call_tool(name, args, raise_on_error=False)
    return "\n".join(getattr(b, "text", "") for b in res.content), res.is_error


async def test_recent_builds_all_instances(client):
    t, err = await call(client, "recent_builds", hours=24)
    assert not err, t
    d = json.loads(t)
    alpha, beta = d["instances"]["alpha"], d["instances"]["beta"]
    rows = "\n".join(alpha["builds"])
    assert f"{JOB} #42 FAILURE" in rows and f"{JOB} #41 SUCCESS" in rows, "every build in the window, not only the last"
    assert "cd/master/app-build #6" not in rows, "builds older than the window are left out"
    assert alpha["builds"][0].startswith("BuildManagement/branch-create #9 BUILDING"), "newest first"
    assert alpha["by_result"]["FAILURE"] == 1 and alpha["by_result"]["SUCCESS"] == 2
    assert any(r.startswith("core-build #100 FAILURE") for r in beta["builds"]), "second instance scanned"


async def test_recent_builds_filters_and_culprits(client):
    t, _ = await call(client, "recent_builds", instance="alpha", hours=1, status="FAILURE,UNSTABLE")
    rows = json.loads(t)["instances"]["alpha"]["builds"]
    assert rows and all(" FAILURE " in r or " UNSTABLE " in r for r in rows), rows
    assert not any("#41 " in r for r in rows), "older than 1 h and green"
    assert any("#42 FAILURE" in r and "culprits: Dev User" in r for r in rows), "who broke the build"


async def test_long_running_build_flagged(client):
    t, _ = await call(client, "recent_builds", instance="alpha", status="BUILDING")
    row = json.loads(t)["instances"]["alpha"]["builds"][0]
    assert "branch-create #9 BUILDING" in row and "longer than usual" in row, row
    t, _ = await call(client, "list_jobs", status="BUILDING")
    assert "longer than usual" in t, "also visible in list_jobs"


async def test_failing_since_in_job_lists(client):
    t, _ = await call(client, "list_jobs", status="FAILURE,UNSTABLE")
    assert f"{JOB} | FAILURE #42 | 30m ago | last green #41 2h ago" in t, t
    assert "release/release%2F2.4 | UNSTABLE #3 | 10m ago | last green #1 3d ago" in t, "UNSTABLE is not green"
    t, _ = await call(client, "failures_overview")
    assert "last green #41" in t
    assert "core-build | FAILURE #100 | 1h ago | no green build on record" in t


async def test_build_summary_culprits(client):
    t, _ = await call(client, "get_build_summary", job=JOB, build=42)
    assert json.loads(t)["culprits"] == ["Dev User"]
    t, _ = await call(client, "get_build_summary", job=JOB, build=41)
    assert "culprits" not in json.loads(t)


async def test_one_dead_instance_does_not_break_the_overview(client, monkeypatch):
    real = jobs.walk_jobs

    def flaky(inst, *a, **kw):
        if inst.name == "beta":
            raise ToolError("[beta] Cannot connect to https://jenkins-beta.example.com (VPN/proxy/DNS?).")
        return real(inst, *a, **kw)

    monkeypatch.setattr(jobs, "walk_jobs", flaky)
    t, err = await call(client, "recent_builds")
    assert not err
    d = json.loads(t)["instances"]
    assert "Cannot connect" in d["beta"]["error"] and d["alpha"]["builds"]
