"""Tool-level edge cases of the log tools (search_log, get_log_lines, get_stage_log, get_log_errors)
through the MCP protocol, against build #42 of the fake Jenkins (failing helm deploy, ~120k lines)."""

import re

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="module")

JOB = "cd/master/app-deploy"


async def call(c, name: str, **args) -> tuple[str, bool]:
    res = await c.call_tool(name, {"job": JOB, "build": 42, **args}, raise_on_error=False)
    return "\n".join(getattr(b, "text", "") for b in res.content), res.is_error


def shown_lines(text: str) -> list[int]:
    return [int(m.group(1)) for m in re.finditer(r"^[> ]\s*(\d+)\| ", text, re.M)]


# ----------------------------------------------------------------------------- search_log
async def test_search_marks_hits_and_context(client):
    t, err = await call(client, "search_log", pattern="UPGRADE FAILED", context=2)
    assert not err and t.startswith("1 matching lines")
    hit = [ln for ln in t.splitlines() if ln.startswith(">")]
    assert len(hit) == 1 and "UPGRADE FAILED" in hit[0]
    assert len(shown_lines(t)) == 5, "hit + 2 lines before + 2 after"


async def test_search_context_zero(client):
    t, _ = await call(client, "search_log", pattern="CrashLoopBackOff", context=0)
    assert len(shown_lines(t)) == 1


async def test_search_ignore_case(client):
    t, _ = await call(client, "search_log", pattern="upgrade failed")
    assert "1 matching lines" in t
    t, _ = await call(client, "search_log", pattern="upgrade failed", ignore_case=False)
    assert t.startswith("No match")


async def test_search_invalid_regex_is_searched_literally(client):
    t, err = await call(client, "search_log", pattern="--timeout 5m (")
    assert not err and t.startswith("No match")
    t, err = await call(client, "search_log", pattern="./chart --wait")
    assert not err and "1 matching lines" in t


async def test_search_restricted_to_stage(client):
    t, _ = await call(client, "search_log", pattern="helm upgrade", stage="Deploy")
    assert "1 matching lines" in t
    t, _ = await call(client, "search_log", pattern="helm upgrade", stage="Build")
    assert t.startswith("No match")
    t, _ = await call(client, "search_log", pattern="x", stage="Nope")
    assert t.startswith("Stage 'Nope' not found") and "Deploy" in t


async def test_search_limits_and_reports_total(client):
    t, _ = await call(client, "search_log", pattern="Compiling module", max_matches=1000, context=0)
    assert t.startswith("3000 matching lines") and "showing 100" in t, "max_matches is capped at 100"
    assert "first unshown at L" in t


async def test_search_output_masks_secrets_and_is_marked_untrusted(client):
    t, _ = await call(client, "search_log", pattern="Bearer|password=")
    assert "abcdef0123456789" not in t and "SuperSecret123" not in t and "****" in t
    assert "untrusted data" in t


# ----------------------------------------------------------------------------- get_log_lines
async def test_log_lines_window(client):
    t, _ = await call(client, "get_log_lines", start=10, count=5)
    assert shown_lines(t) == [10, 11, 12, 13, 14] and t.startswith("Lines 10-14 of")


async def test_log_lines_tail_and_clamps(client):
    t, _ = await call(client, "get_log_lines", start=0, count=3)
    assert "Finished: FAILURE" in t.splitlines()[-1]
    t, _ = await call(client, "get_log_lines", start=10_000_000, count=5)
    assert len(shown_lines(t)) == 1, "start past the end shows the last line"
    t, _ = await call(client, "get_log_lines", start=1, count=5000)
    assert len(shown_lines(t)) <= 400


async def test_log_lines_hide_noise(client):
    t, _ = await call(client, "get_log_lines", start=1, count=12, hide_noise=True)
    assert "[Pipeline]" not in t and "Started by user" in t


# ----------------------------------------------------------------------------- get_stage_log / get_log_errors
async def test_stage_log_named_and_unknown(client):
    t, _ = await call(client, "get_stage_log", stage="Test")
    assert "Stage 'Test'" in t and "Tests run: 120" in t and "Errors in this stage" not in t
    t, _ = await call(client, "get_stage_log", stage="Nope")
    assert t.startswith("Stage 'Nope' not found")


async def test_log_errors_filters(client):
    t, _ = await call(client, "get_log_errors", category="helm")
    assert "[helm]" in t and "[java" not in t
    t, _ = await call(client, "get_log_errors", include_generic=True)
    assert "script returned exit code 1" in t
    t, _ = await call(client, "get_log_errors", category="does-not-exist")
    assert t.startswith("No error lines detected")
