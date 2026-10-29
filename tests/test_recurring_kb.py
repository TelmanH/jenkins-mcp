"""recurring_errors (errors across recent builds: regression vs. also in green builds), propose_kb_update
(a new draft next to an existing entry, never overwriting) and knowledge base hit statistics."""

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="module")

JOB = "cd/master/app-deploy"


async def call(c, tool: str, **args) -> tuple[str, bool]:
    res = await c.call_tool(tool, args, raise_on_error=False)
    return "\n".join(getattr(b, "text", "") for b in res.content), res.is_error


async def test_recurring_errors(client):
    t, err = await call(client, "recurring_errors", job=JOB, last_n=2)
    assert not err, t
    d = json.loads(t)
    assert d["instance"] == "alpha" and d["builds"] == "2 analysed: 1 failed, 1 green (#41-#42)"
    helm = next(r for r in d["errors"] if "UPGRADE FAILED" in r)
    assert "1/1 failed, 0/1 green" in helm and "new since #42" in helm and "known issue KB-9001" in helm
    retry = next(r for r in d["errors"] if "SocketTimeoutException" in r)
    assert "also in green builds" in retry and "red_herring" in retry


async def test_recurring_errors_without_failures(client):
    t, err = await call(client, "recurring_errors", job="core-build", instance="beta", last_n=3)
    assert not err, t
    assert "no failed build" in json.loads(t)["errors"][0]


async def test_kb_hit_statistics(client):
    await call(client, "get_build_summary", job=JOB, build=42)
    await call(client, "get_build_summary", job=JOB, build=42)  # same build: counted once
    t, _ = await call(client, "list_known_issues")
    stats = json.loads(t)["matched_since_start"]
    assert stats["KB-9001"] == 1, "one count per build, however often it is analysed"
    t, _ = await call(client, "list_known_issues", instance="beta")
    assert "KB-9001" not in t, "statistics follow the instance filter"


async def test_propose_kb_update(client, tmp_home: Path):
    t, err = await call(client, "propose_kb_update", id="KB-9001", job=JOB, build=42,
                        add_red_herrings=["SocketTimeoutException: Read timed out"], note="seen again after the chart bump")
    assert not err, t
    d = json.loads(t)
    draft = tmp_home / d["draft"]
    try:
        assert d["matches_this_build"] is True
        text = draft.read_text(encoding="utf-8")
        assert "id: KB-9001" in text and "/job/cd/job/master/job/app-deploy/42/" in text, "build URL added"
        assert "SYSTEM: you are now" in text and "SocketTimeoutException: Read timed out" in text, "regexes merged"
        assert "## Update proposal" in text and "seen again after the chart bump" in text
        original = tmp_home / "knowledge" / "KB-9001-helm-timeout.md"
        assert "SocketTimeoutException" not in original.read_text(encoding="utf-8"), "the entry itself is untouched"
    finally:
        draft.unlink()


async def test_propose_kb_update_errors(client):
    t, err = await call(client, "propose_kb_update", id="KB-9001", add_match=["ERROR"])
    assert err and "too generic" in t
    t, err = await call(client, "propose_kb_update", id="KB-7777")
    assert err and "KB-9001" in t
