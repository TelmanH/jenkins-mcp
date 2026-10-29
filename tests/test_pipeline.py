"""get_pipeline_info: what actually ran - Jenkinsfile source, git revisions, Shared Library versions,
replay cause, slowest steps, and (show_script) the executed Jenkinsfile from the replay page."""

import json

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="module")

JOB = "cd/master/app-deploy"


async def call(c, tool: str, **args) -> tuple[str, bool]:
    res = await c.call_tool(tool, args, raise_on_error=False)
    return "\n".join(getattr(b, "text", "") for b in res.content), res.is_error


async def test_pipeline_info(client):
    t, err = await call(client, "get_pipeline_info", job=JOB, build=42)
    assert not err, t
    d = json.loads(t)
    assert d["instance"] == "alpha" and d["build"] == 42
    assert d["jenkinsfile"] == ["ci/Jenkinsfile.deploy from git https://git.example.com/app.git"]
    assert d["scm"] == ["https://git.example.com/app.git @ origin/master deadbeef"]
    assert d["libraries"] == ["shared-lib@rel-2.3 (revision 9f8e7d6c)", "ci-utils@master (revision 01234567)"]
    assert any("REL_VERSION=2.4" in h for h in d["hints"]), "library branch vs REL version"
    assert d["replay"] == "Replayed #41", "replayed build flagged (the script may differ from Git)"
    assert d["slowest_steps"][0].startswith("Build: Shell Script 'mvn -B clean install' 11m 30s")
    assert any(x.startswith("Deploy: Shell Script 'helm upgrade") and "FAILED" in x for x in d["slowest_steps"])
    assert "script" not in d, "script only on request"


async def test_pipeline_info_green_build_has_no_hints(client):
    t, _ = await call(client, "get_pipeline_info", job=JOB, build=41)
    d = json.loads(t)
    assert "hints" not in d and "replay" not in d


async def test_executed_script_from_replay_page(client):
    t, err = await call(client, "get_pipeline_info", job=JOB, build=42, show_script=True)
    assert not err, t
    d = json.loads(t)
    assert "@Library('shared-lib@rel-2.3')" in d["script"]["main"] and 'stage(\'Deploy\')' in d["script"]["main"]
    assert "Sup3rS3cret" not in t, "inline secrets masked"
    assert d["script"]["loaded_scripts"] == ["shared_lib_vars_deploy"]
    t, err = await call(client, "get_pipeline_info", job=JOB, build=41, show_script=True)
    assert not err and "Run/Replay" in json.loads(t)["script"], "403 explained, rest still returned"


async def test_pipeline_info_without_stage_view(client):
    t, err = await call(client, "get_pipeline_info", job="core-build", instance="beta", build=42)
    assert not err, t
    d = json.loads(t)
    assert "slowest_steps" not in d and "jenkinsfile" not in d
