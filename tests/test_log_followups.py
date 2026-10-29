"""Downstream builds in the summary, pending input steps, running builds (incremental log) and the exact
per-step stage log from wfapi (parallel branches do not interleave)."""

import json

import pytest

from fake_jenkins import CALLS

pytestmark = pytest.mark.asyncio(loop_scope="module")

RUNNING = "BuildManagement/branch-create"


async def call(c, tool: str, **args) -> tuple[str, bool]:
    res = await c.call_tool(tool, args, raise_on_error=False)
    return "\n".join(getattr(b, "text", "") for b in res.content), res.is_error


async def test_summary_follows_downstream_builds(client):
    t, err = await call(client, "get_build_summary", job="cd/master/release-train", build=5)
    assert not err, t
    s = json.loads(t)
    assert s["downstream"] == ["cd/master/app-deploy #42 FAILURE", "Legacy Name #3 (not found by this name)"]
    assert s["next_steps"][0].startswith("get_build_summary(job='cd/master/app-deploy', build=42)"), \
        "the failure is in the child: analyse it first"


async def test_pending_input(client):
    t, err = await call(client, "get_pending_inputs", job=RUNNING)
    assert not err, t
    d = json.loads(t)
    assert d["instance"] == "alpha" and d["build"] == 9
    assert d["waiting_for"] == ["'Deploy to prod?' (id Approve, button 'Deploy', inputs: TICKET; submitter: release-managers)"]
    assert "UI" in d["note"], "approving stays a human action in Jenkins"
    t, _ = await call(client, "get_pending_inputs", job="cd/master/app-deploy", build=42)
    assert "not running" in json.loads(t)["waiting_for"][0]


async def test_running_build_summary_is_incremental_and_shows_input(client):
    t, err = await call(client, "get_build_summary", job=RUNNING)
    assert not err, t
    s = json.loads(t)
    assert s["result"] == "BUILDING" and s["waiting_for_input"] == ["Deploy to prod?"]
    await call(client, "get_build_summary", job=RUNNING)
    starts = [c.split("start=")[1] for c in CALLS if "branch-create/9/logText/progressiveText" in c]
    assert starts[-2:] == ["0", str(len("Started by user Dev\nWaiting for approval\n"))], starts


async def test_exact_stage_log_from_wfapi(client):
    t, err = await call(client, "get_stage_log", job="cd/master/app-deploy", build=42, stage="Deploy", exact=True)
    assert not err, t
    assert "per-step log from wfapi" in t and "Shell Script 'helm upgrade" in t
    assert "Error: UPGRADE FAILED: timed out waiting for the condition" in t and "<span" not in t, "HTML stripped"
    t, err = await call(client, "get_stage_log", job="core-build", instance="beta", build=42, stage="Deploy", exact=True)
    assert not err and "console markers" in t, "without Stage View: falls back to the console"
