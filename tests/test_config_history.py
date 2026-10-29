"""Job Config History: list of changes with the JCasC/JobDSL hint, semantic + text diff of two versions
(or a version against the live config), plugin missing, and Jenkinsfile commits in compare_builds."""

import json

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="module")

JOB = "cd/master/app-deploy"


async def call(c, tool: str, **args) -> tuple[str, bool]:
    res = await c.call_tool(tool, args, raise_on_error=False)
    return "\n".join(getattr(b, "text", "") for b in res.content), res.is_error


async def test_history_list(client):
    t, err = await call(client, "get_job_config_history", job=JOB)
    assert not err, t
    d = json.loads(t)
    assert d["instance"] == "alpha" and d["count"] == 3
    assert d["versions"][0] == "2026-10-21_09-00-00 | Changed | SYSTEM", "newest first"
    assert d["versions"][1] == "2026-10-20_08-15-30 | Changed | Dev User (dev)"
    assert "JCasC" in d["hint"] and "Git" in d["hint"], "SYSTEM changes come from config as code"


async def test_diff_latest_two_versions(client):
    t, err = await call(client, "diff_job_config", job=JOB)
    assert not err, t
    d = json.loads(t)
    assert d["older"] == "2026-10-20_08-15-30" and d["newer"] == "2026-10-21_09-00-00"
    assert "triggers: cron: H 3 * * *; after upstream: cd/master/app-build -> cron: H 2 * * *; after upstream: " \
           "cd/master/app-build" in d["changes"]
    assert "keep_builds: 10 builds -> 30 builds" in d["changes"]
    assert "-        <hudson.triggers.TimerTrigger><spec>H 3 * * *</spec></hudson.triggers.TimerTrigger>" in d["diff"]
    assert "+        <hudson.triggers.TimerTrigger><spec>H 2 * * *</spec></hudson.triggers.TimerTrigger>" in d["diff"]


async def test_diff_against_current_and_masking(client):
    t, err = await call(client, "diff_job_config", job=JOB, older="2026-10-01_10-00-00", newer="current")
    assert not err, t
    d = json.loads(t)
    assert d["newer"] == "current (live config.xml)"
    assert any(c.startswith("type: Pipeline (inline script) -> Pipeline (Jenkinsfile from SCM)") for c in d["changes"])
    assert "early1" not in t, "secrets masked in old versions too"


async def test_history_errors(client):
    t, err = await call(client, "diff_job_config", job=JOB, older="1999-01-01_00-00-00")
    assert err and "2026-10-20_08-15-30" in t, "unknown version lists the available ones"
    t, err = await call(client, "diff_job_config", job="cd/master/app-build")
    assert err and "only one version" in t and "get_job_config" in t, "nothing older to compare with"
    t, err = await call(client, "get_job_config_history", job="core-build", instance="beta")
    assert err and "Job Config History plugin" in t, "plugin missing explained"


async def test_compare_builds_flags_jenkinsfile_commits(client):
    t, _ = await call(client, "compare_builds", job=JOB)
    changes = json.loads(t)["pipeline_changes"]
    assert changes == ["#42 feedface Ops User: Raise helm timeout (ci/Jenkinsfile.deploy)"], changes
