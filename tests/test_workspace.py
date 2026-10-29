"""Workspace access (files that were not archived, e.g. app-queue.log): Pipeline node blocks, freestyle
workspace, directory listing, text decoding (UTF-16 with BOM, cp1252), binary files, cleaned workspaces."""

import json

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="module")

JOB = "cd/master/app-deploy"
FREESTYLE = "cd/master/app-build"


async def call(c, tool: str, **args) -> tuple[str, bool]:
    res = await c.call_tool(tool, args, raise_on_error=False)
    return "\n".join(getattr(b, "text", "") for b in res.content), res.is_error


async def test_pipeline_with_several_node_blocks_asks_for_one(client):
    t, err = await call(client, "list_workspace", job=JOB, build=42)
    assert not err, t
    d = json.loads(t)
    assert d["workspaces"] == ["node_id=5: /home/jenkins/workspace/cd_master_app-deploy on build-agent-02",
                               "node_id=50: /home/jenkins/workspace/cd_master_app-deploy@2 on build-agent-02"]
    assert "node_id" in d["next_step"]


async def test_list_pipeline_workspace(client):
    t, err = await call(client, "list_workspace", job=JOB, build=42, node_id="5")
    assert not err, t
    d = json.loads(t)
    assert d["instance"] == "alpha" and d["entries"] == ["logs/", "app-queue.log", "pom.xml", "app.jar"]
    assert "CURRENT" in d["note"], "the workspace is the agent's current state, not a snapshot of the build"
    t, _ = await call(client, "list_workspace", job=JOB, build=42, node_id="5", path="logs")
    assert json.loads(t)["entries"] == ["app.log"]


async def test_read_utf16_workspace_file(client):
    t, err = await call(client, "read_workspace_file", job=JOB, build=42, node_id="5", path="app-queue.log")
    assert not err, t
    assert "ERROR QUEUE-1234 queue full" in t and "\x00" not in t, "UTF-16 with BOM decoded, not refused as binary"
    assert "queueSecret" not in t and "password=****" in t
    assert "utf-16" in t.splitlines()[0], "encoding reported"
    t, _ = await call(client, "read_workspace_file", job=JOB, build=42, node_id="5", path="app-queue.log", pattern="queue full")
    assert "1 matches" in t


async def test_workspace_edge_cases(client):
    t, err = await call(client, "read_workspace_file", job=JOB, build=42, node_id="5", path="logs")
    assert err and "directory" in t and "list_workspace" in t
    t, err = await call(client, "read_workspace_file", job=JOB, build=42, node_id="5", path="app.jar")
    assert err and "binary" in t
    t, err = await call(client, "list_workspace", job=JOB, build=41)
    assert err and "cleanWs" in t and "offline" in t, "cleaned workspace / agent gone explained"
    t, err = await call(client, "list_workspace", job=JOB, build=42, node_id="99")
    assert err and "node_id=5" in t, "unknown node block lists the valid ones"


async def test_freestyle_workspace_and_cp1252(client):
    t, err = await call(client, "list_workspace", job=FREESTYLE)
    assert not err, t
    assert json.loads(t)["entries"] == ["settings.xml", "build.log"]
    t, err = await call(client, "read_workspace_file", job=FREESTYLE, path="build.log")
    assert not err and "Fehler: Übersetzung fehlgeschlagen" in t, "German Windows encoding decoded"


async def test_utf16_artifact(client):
    t, err = await call(client, "read_artifact", job=JOB, build=42, path="logs/app-queue.log")
    assert not err and "QUEUE-1234" in t and "queueSecret" not in t, "app-queue.log as artifact is readable too"
