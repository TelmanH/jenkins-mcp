"""Live test: every MCP tool against the seeded local Jenkins (see README.md).

Usage:  python tests/live/live_test.py [http://localhost:8080]
Needs:  setup_jobs.py was run (writes tests/live/.token). The instance is named 'main' on purpose,
        so the example knowledge-base entry KB-0001 (instance main, job deploy/*) applies to the
        seeded deploy/app-install job.
The example knowledge base is copied to a temp folder: draft_kb_entry never touches your real knowledge/.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import requests

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")
HERE = Path(__file__).parent
TMP = Path(tempfile.mkdtemp(prefix="jenkins-mcp-live-"))
token = (HERE / ".token").read_text(encoding="utf-8").strip()

(TMP / "config").mkdir(parents=True)
shutil.copytree(HERE.parent / "fixtures" / "knowledge", TMP / "knowledge")
(TMP / "config" / "jenkins_instances.yaml").write_text(f"""
default: main
instances:
  main:
    url: {BASE}/
knowledge:
  dir: ../knowledge
""", encoding="utf-8")
os.environ.update({"JENKINS_MCP_HOME": str(TMP),
                   "JENKINS_MCP_CACHE_DIR": str(TMP / "cache"),
                   "JENKINS_MAIN_USER": "admin", "JENKINS_MAIN_TOKEN": token})
from jenkins_mcp import config  # noqa: E402
from jenkins_mcp.main import mcp  # noqa: E402
from jenkins_mcp.settings import MAX_CHARS  # noqa: E402
from fastmcp import Client  # noqa: E402

DEP, INSTALL, BUILD, TEST, LONG = ("cd/master/app-deploy", "deploy/app-install", "cd/master/app-build",
                              "cd/master/app-test", "cd/master/long-log")
TRAIN, CONFIG, APPROVAL = "cd/master/release-train", "cd/master/uses-config", "cd/master/needs-approval"
failures: list[str] = []
tool_ok: dict[str, bool] = {}
current = ""


def check(cond: bool, msg: str) -> None:
    print(("   ok   " if cond else "   FAIL ") + msg)
    if not cond:
        failures.append(f"{current}: {msg}")
        tool_ok[current] = False


async def call(c: Client, tool: str, **args):  # not `name`: get_node takes name=
    global current
    current = tool
    tool_ok.setdefault(tool, True)
    res = await c.call_tool(tool, args, raise_on_error=False)
    text = "\n".join(getattr(b, "text", "") for b in res.content)
    print(f"\n=== {tool} {args} -> {len(text)} chars, error={res.is_error}")
    print("   " + text[:300].replace("\n", "\n   ") + ("..." if len(text) > 300 else ""))
    check(len(text) <= MAX_CHARS + 1500, f"output size {len(text)} within budget")
    return text, res.is_error


def build_numbers() -> dict[str, int]:
    out = {}
    for j in (DEP, INSTALL, BUILD, TEST, LONG):
        path = "/job/" + "/job/".join(j.split("/"))
        r = requests.get(f"{BASE}{path}/api/json?tree=lastBuild[number]", auth=("admin", token), timeout=30)
        out[j] = (r.json().get("lastBuild") or {}).get("number", 0)
    return out


def wait_running(job: str, timeout: int = 60) -> None:
    """The seeder starts needs-approval without waiting; it first sits in the quiet period."""
    path = "/job/" + "/job/".join(job.split("/"))
    for _ in range(timeout // 2):
        r = requests.get(f"{BASE}{path}/api/json?tree=lastBuild[building]", auth=("admin", token), timeout=30)
        if (r.json().get("lastBuild") or {}).get("building"):
            return
        time.sleep(2)


async def main() -> None:
    before = build_numbers()
    wait_running(APPROVAL)
    check(all(v >= 1 for v in before.values()), f"seeded jobs have builds {before}")
    async with Client(mcp) as c:
        tools = {t.name: t for t in await c.list_tools()}
        print("tools:", ", ".join(tools))
        writers = [n for n, t in tools.items() if not (t.annotations and t.annotations.readOnlyHint)]
        check(writers == ["draft_kb_entry", "propose_kb_update"],
              f"only the knowledge base draft tools are not read-only ({writers})")

        t, e = await call(c, "list_instances", check_connection=True)
        li = json.loads(t)
        check(not e and "config_errors" not in li, "no config errors")
        check("main" in t and ("Jenkins" in t or "2." in t), "main reachable, version shown")
        check(li["knowledge_base"]["entries"] >= 1, "knowledge base loaded")
        main = li["instances"][0]
        check(main.get("user", "").startswith("admin"), "whoAmI: token user shown")
        check("admin_rights" in main, "admin token reported as information")

        t, e = await call(c, "list_jobs")
        check(all(x in t for x in (DEP, INSTALL, BUILD, TEST, LONG, "cd/master/queued-job")), "all seeded jobs listed")
        check("FAILURE" in t and "SUCCESS" in t, "statuses shown")
        t, _ = await call(c, "list_jobs", status="FAILURE")
        check(DEP in t and BUILD not in t, "status filter FAILURE")
        t, _ = await call(c, "list_jobs", folder="deploy")
        check(INSTALL in t and DEP not in t, "folder filter")
        t, _ = await call(c, "list_jobs", name_contains="LONG")
        check(LONG in t and DEP not in t, "name_contains is case-insensitive")

        t, _ = await call(c, "failures_overview", instance="all", hours=720)
        check(DEP in t and INSTALL in t and BUILD not in t, "failures_overview: red jobs only")

        t, _ = await call(c, "get_job", job=DEP)
        check("TARGET_ENV" in t, "parameters listed")
        check("hunter2secret" not in t, "password parameter default not leaked")
        t, _ = await call(c, "get_job", job="cd/master/does-not-exist")
        check("404" in t or "not" in t.lower(), "helpful error for unknown job")

        t, e = await call(c, "get_build_summary", job=DEP, build="lastFailedBuild")
        check(not e and "Deploy" in t, "failed stage = Deploy")
        check("UPGRADE FAILED" in t, "root cause line found")
        check("SuperSecret123" not in t and "hunter2secret" not in t, "secrets masked")
        t, _ = await call(c, "get_build_summary", job=DEP, build="lastSuccessfulBuild")
        check("SUCCESS" in t, "green build summarised")
        t, _ = await call(c, "get_build_summary", job=f"{BASE}/job/cd/job/master/job/app-deploy/2/")
        check("UPGRADE FAILED" in t, "pasted build URL works")

        t, _ = await call(c, "get_build_summary", job=INSTALL, build=1)
        check("KB-0001" in t, "KB-0001 recognised for app-install")
        check("Unpack failed" in t or "drop/create" in t, "installer/DB errors are the causes")

        t, _ = await call(c, "get_stage_log", job=DEP, build=2)
        check("UPGRADE FAILED" in t, "default stage = failed stage")
        t, _ = await call(c, "get_stage_log", job=DEP, build=2, stage="Build")
        check("BUILD SUCCESS" in t, "named stage")

        t, _ = await call(c, "get_log_errors", job=DEP, build=2)
        check("UPGRADE FAILED" in t, "error groups")
        t, _ = await call(c, "get_log_errors", job=LONG, build=1, category="maven")
        check("Compilation failure" in t, "maven category in 30k-line log")

        t, _ = await call(c, "search_log", job=DEP, build=2, pattern=r"retry \d/3")
        check("retry 1/3" in t, "search_log")
        t, _ = await call(c, "get_log_lines", job=LONG, build=1, start=1, count=15)  # ~10 Jenkins header lines first
        check("processing item 1 " in t, "log lines from start")
        t, _ = await call(c, "get_log_lines", job=LONG, build=1, start=0, count=5)
        check("Compilation failure" in t or "exit" in t.lower() or "ERROR" in t, "tail of log")

        t, _ = await call(c, "get_test_failures", job=TEST)
        check("createPolicy" in t and "Connection refused" in t, "failed JUnit cases")

        t, _ = await call(c, "compare_builds", job=DEP)
        check("FAIL" in t, "env/param difference FAIL=true vs false shown")
        check("hunter2secret" not in t, "no secret in diff")

        t, _ = await call(c, "list_artifacts", job=BUILD)
        check("logs/env.log" in t and "target/app.jar" in t, "artifacts listed")
        t, _ = await call(c, "read_artifact", job=BUILD, path="logs/env.log", pattern="^JENKINS_URL")
        check("JENKINS_URL" in t, "text artifact read")
        t, _ = await call(c, "read_artifact", job=BUILD, path="target/app.jar")
        check("PK" not in t[:20], "binary artifact not dumped")
        t, _ = await call(c, "get_env_log", job=DEP, build=2)
        check("FAIL=true" in t and "hunter2secret" not in t, "env.log: content ok, secret masked")

        t, _ = await call(c, "node_health")
        check("Built-In" in t or "built-in" in t.lower() or "master" in t.lower(), "built-in node listed")
        for _ in range(15):  # a freshly queued item first sits in the quiet period, then waits for the label
            t, _ = await call(c, "get_queue")
            if "no-such-agent" in t:
                break
            await asyncio.sleep(2)
        check("queued-job" in t, "queued job visible")
        check("no-such-agent" in t or "label" in t.lower(), "queue reason shown")

        t, _ = await call(c, "find_known_issues", job=INSTALL, build=1)
        check("KB-0001" in t, "find_known_issues by build")
        t, _ = await call(c, "find_known_issues", text="com.example.installer.InstallerException: Unpack failed (xml line 53)")
        check("KB-0001" in t, "find_known_issues by pasted text")
        t, _ = await call(c, "list_known_issues")
        check("KB-0001" in t, "list_known_issues")
        t, _ = await call(c, "get_known_issue", id="KB-0001")
        check("drop_create.log" in t, "get_known_issue")

        t, e = await call(c, "draft_kb_entry", title="Helm timeout -> crash loop", root_cause="memory limit too low",
                          match=["UPGRADE FAILED: timed out waiting"], job=DEP, build=2)
        check(not e and list((TMP / "knowledge" / "drafts").glob("KB-*.md")), "draft written to the TEMP knowledge dir")

        t, e = await call(c, "propose_kb_update", id="KB-0001", job=INSTALL, build=1, note="live test")
        check(not e and json.loads(t).get("matches_this_build") is True, "update proposal for KB-0001")

        # ---------------------------------------------------------------- overview, nodes
        t, _ = await call(c, "recent_builds", hours=720)
        check(DEP in t and "#2 FAILURE" in t and "#1 SUCCESS" in t, "recent_builds: every build in the window")
        t, _ = await call(c, "recent_builds", status="BUILDING", hours=720)
        check(APPROVAL in t, "recent_builds: running build listed")
        t, _ = await call(c, "list_jobs", status="FAILURE")
        check("last green #1" in t, "failing since: last green build")
        t, _ = await call(c, "get_node", name="built-in")
        check('"labels"' in t and '"executors"' in t, "get_node: built-in node")
        t, _ = await call(c, "get_node", name="offline-agent")
        check('"online":false' in t.replace(" ", "") or "Unknown node" in t, "get_node: offline agent (if the seeder created it)")
        check("linux" in t or "Unknown node" in t, "get_node: labels from config")

        # ---------------------------------------------------------------- pipeline, config, history
        t, e = await call(c, "get_pipeline_info", job=DEP, build=2, show_script=True)
        check(not e and "helm upgrade" in t, "get_pipeline_info: executed script from the replay page")
        check("hunter2secret" not in t, "replay script: secrets masked")
        t, _ = await call(c, "get_pipeline_info", job=CONFIG, build=1)
        check("Live settings" in t, "managed file provisioned by the build (from the log)")
        t, e = await call(c, "get_job_config", job=DEP)
        check(not e and "TARGET_ENV" in t and "Pipeline (inline script)" in t, "get_job_config summary")
        check("hunter2secret" not in t, "get_job_config: no secret")
        t, e = await call(c, "read_managed_file", id="live-settings")
        check(not e and "user=deployer" in t, "read_managed_file: content found on the page")
        check("liveSecret1" not in t, "read_managed_file: secret masked")
        t, e = await call(c, "get_job_config_history", job=BUILD)
        check(not e and json.loads(t).get("count", 0) >= 2, "Job Config History: versions listed")
        t, e = await call(c, "diff_job_config", job=BUILD)
        check(not e and "changed by setup_jobs" in t, "diff_job_config: the seeded change")

        # ---------------------------------------------------------------- workspace, artifacts
        t, e = await call(c, "list_workspace", job=BUILD, build=2)
        check(not e and "logs/" in t, "list_workspace: Pipeline node-block workspace found")
        t, e = await call(c, "read_workspace_file", job=BUILD, build=2, path="logs/env.log", pattern="^JENKINS_URL")
        check(not e and "JENKINS_URL" in t, "read_workspace_file")
        t, e = await call(c, "compare_artifacts", job=BUILD, good=1, bad=2)
        check(not e and "logs/env.log" in t, "compare_artifacts: env.log differs between builds")
        t, _ = await call(c, "list_artifacts", job=BUILD, sizes=True)
        check(" B)" in t or "KB)" in t, "artifact sizes from HEAD")

        # ---------------------------------------------------------------- logs, comparisons, trends
        t, _ = await call(c, "get_build_summary", job=TRAIN, build=1)
        check("child-fail #1 FAILURE" in t, "downstream build found with its result")
        t, _ = await call(c, "get_pending_inputs", job=APPROVAL)
        check("Deploy to prod?" in t, "pending input step")
        t, _ = await call(c, "get_stage_log", job=DEP, build=2, stage="Deploy", exact=True)
        check("UPGRADE FAILED" in t and "per-step log from wfapi" in t, "exact per-step stage log")
        t, _ = await call(c, "get_build_summary", job=DEP, build=1)
        agent = (json.loads(t).get("agents") or ["?"])[0]  # the built-in node logs as "Running on Jenkins in ..."
        t, e = await call(c, "compare_builds", job=DEP, good_on_node=agent, bad_on_node=agent)
        check(not e and '"good"' in t and '"bad"' in t, f"compare_builds by node ({agent})")
        t, e = await call(c, "recurring_errors", job=DEP, last_n=5)
        check(not e and "UPGRADE FAILED" in t, "recurring_errors")
        t, e = await call(c, "find_jobs_by_scm", url="example.invalid/none")
        check(not e and "never built" in t, "find_jobs_by_scm: no match explained (seeded jobs use inline scripts)")

        prompts = [p.name for p in await c.list_prompts()]
        check({"analyse_build", "document_session", "morning_report"} <= set(prompts), f"prompts {prompts}")

    # read-only guarantees
    try:
        config.get_instance("main").session.post(BASE + "/job/x/build")
        check(False, "POST blocked")
    except Exception as ex:  # noqa: BLE001
        check("read-only" in str(ex).lower(), "POST blocked by ReadOnlySession")
    after = build_numbers()
    check(after == before, f"Jenkins unchanged by the whole run (build numbers {after})")

    print("\n================ SUMMARY ================")
    for n in sorted(tool_ok):
        print(f"  {'OK  ' if tool_ok[n] else 'FAIL'} {n}")
    missing = set(tools) - set(tool_ok)
    print("  not exercised:", sorted(missing) or "-")
    print(f"\n{len(failures)} failed")
    for f in failures:
        print("  -", f)
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if failures else 0)


asyncio.run(main())
