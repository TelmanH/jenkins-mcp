"""Artifact sizes and compare_artifacts: added/removed files, changed files via fingerprint (no download),
small-file byte comparison, text diff (masked) and binary checksum comparison."""

import json

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="module")

JOB = "cd/master/app-deploy"


async def call(c, tool: str, **args) -> tuple[str, bool]:
    res = await c.call_tool(tool, args, raise_on_error=False)
    return "\n".join(getattr(b, "text", "") for b in res.content), res.is_error


async def test_list_artifacts_with_sizes(client):
    t, err = await call(client, "list_artifacts", job=JOB, build=42, sizes=True)
    assert not err, t
    d = json.loads(t)
    assert d["instance"] == "alpha"
    assert "logs/env.log (117 B)" in d["artifacts"] and "target/app.jar (12 B)" in d["artifacts"]


async def test_compare_artifact_lists(client):
    t, err = await call(client, "compare_artifacts", job=JOB)
    assert not err, t
    d = json.loads(t)
    assert d["good"] == "#41" and d["bad"] == "#42"
    assert d["only_in_good"] == ["release-notes.txt"] and d["only_in_bad"] == ["report/new-check.txt"]
    assert "target/app.jar: content differs (fingerprint)" in d["changed"], "binary compared without download"
    assert "logs/env.log: content differs (117 B -> 117 B)" in d["changed"], "small text files compared byte-wise"
    assert d["identical"] == ["logs/app-queue.log"]


async def test_compare_one_text_artifact(client):
    t, err = await call(client, "compare_artifacts", job=JOB, path="logs/env.log")
    assert not err, t
    diff = json.loads(t)["diff"]
    assert "-JAVA_HOME=/opt/jdk17" in diff and "+JAVA_HOME=/opt/jdk21" in diff
    assert "abc123" not in t, "secrets masked in the diff"


async def test_compare_binary_and_missing(client):
    t, err = await call(client, "compare_artifacts", job=JOB, path="target/app.jar")
    assert not err, t
    d = json.loads(t)
    assert d["binary"] is True and d["result"].startswith("identical"), d
    t, err = await call(client, "compare_artifacts", job=JOB, path="release-notes.txt")
    assert err and "not in build #42" in t
