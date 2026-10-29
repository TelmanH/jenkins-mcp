"""get_node: node configuration (config.xml) + live state (labels, executors, monitors), and labels in
node_health. Edge cases: XML 1.1 config, 403 on config.xml, built-in node, unknown or ambiguous names."""

import json

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def call(c, tool: str, **args) -> tuple[str, bool]:
    res = await c.call_tool(tool, args, raise_on_error=False)
    return "\n".join(getattr(b, "text", "") for b in res.content), res.is_error


async def test_get_node_config_and_live_state(client):
    t, err = await call(client, "get_node", name="build-agent-01")
    assert not err, t
    d = json.loads(t)
    assert d["instance"] == "alpha" and d["online"] is True
    assert d["labels"] == ["linux", "docker"], "own name is not listed as a label"
    assert d["executors"][0].startswith("#0 busy: cd/master/app-deploy #42"), "which build runs where"
    assert d["executors"][1] == "#1 idle"
    assert d["java"] == "17.0.9" and d["disk_free_gb"] == 80.0
    cfg = d["config"]
    assert cfg["remote_root"] == "/home/jenkins" and cfg["mode"] == "EXCLUSIVE (only jobs with matching labels)"
    assert cfg["launcher"] == "SSHLauncher host=build01.example.com port=22 credentialsId=jenkins-ssh-key"
    assert "JAVA_HOME=/opt/jdk17" in cfg["env"] and "NEXUS_PASSWORD=****" in cfg["env"]
    assert "hunter2node" not in t, "node env secrets masked"
    assert cfg["tools"] == ["maven-3.9: /opt/maven"]


async def test_get_node_without_config_permission(client):
    t, err = await call(client, "get_node", name="agent-02")  # partial name
    assert not err, t
    d = json.loads(t)
    assert d["name"] == "build-agent-02" and d["labels"] == ["linux"], "API part still shown"
    assert "ExtendedRead" in d["config"], "403 on config.xml explained"
    assert "versioncolumn" in d["java"], "JVM not exposed: says why"


async def test_get_node_built_in_and_errors(client):
    t, err = await call(client, "get_node", name="built-in")
    assert not err and json.loads(t)["name"] == "Built-In Node", t
    t, err = await call(client, "get_node", name="nope")
    assert err and "build-agent-01" in t, "unknown node lists the nodes"
    t, err = await call(client, "get_node", name="agent")
    assert err and "ambiguous" in t.lower()


async def test_node_health_shows_labels(client):
    t, _ = await call(client, "node_health")
    nodes = {n["name"]: n for n in json.loads(t)["nodes"]}
    assert nodes["build-agent-01"]["labels"] == ["linux", "docker"]
