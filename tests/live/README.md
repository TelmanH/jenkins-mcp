# Live test against a real (local) Jenkins

Throw-away Jenkins (LTS, JDK 21) in Docker with seeded jobs. Everything stays on your machine.
This is a development check, not part of the server. Run it on the macOS dev machine (Docker Desktop).

## Run

Prerequisites: Docker Desktop running, and the developer setup (`tests/macos/setup.sh` or `setup\windows\setup.ps1 -Dev`).
Behind a proxy: set it in Docker Desktop (Settings -> Resources -> Proxies); the image build downloads
plugins from `updates.jenkins.io`.

```bash
tests/live/run_live.sh        # macOS/Linux: docker compose up, setup_jobs.py, live_test.py (uses .venv)
```
```powershell
powershell -ExecutionPolicy Bypass -File tests\live\run_live.ps1   # Windows (after setup\windows\setup.ps1 -Dev)
```

Jenkins UI: http://localhost:8080 (admin / admin). Stop: `docker compose -f tests/live/docker-compose.yml down -v`.
Without Docker: any local Jenkins with the plugins in `plugins.txt`, user `admin`/`admin`;
then `.venv/bin/python tests/live/setup_jobs.py <url>` and `.venv/bin/python tests/live/live_test.py <url>`.

## Seeded jobs (instance is called `main` so the example KB-0001 applies)

| Job | Builds | Purpose |
|---|---|---|
| `cd/master/app-build` | #1, #2 green | artifacts `logs/env.log`, `target/app.jar` (binary) |
| `cd/master/app-deploy` | #1 green, #2 **red** (`FAIL=true`) | failed stage `Deploy`, WARN retries, masked secrets, prompt-injection line, param diff |
| `cd/master/app-test` | #1 unstable | 2 failing JUnit cases |
| `cd/master/long-log` | #1 red | 30 000-line log + Maven compile error |
| `cd/master/queued-job` | queued | waits for label `no-such-agent` |
| `deploy/app-install` | #1 red | DB migration / installer failure log (`tests/fixtures`) -> must match KB-0001 |
| `cd/master/child-fail` | #1 red | fails on purpose (downstream of release-train) |
| `cd/master/release-train` | #1 red | `build job: 'child-fail'` -> `downstream` in the summary |
| `cd/master/uses-config` | #1 green | `configFileProvider` with the managed file `live-settings` (JCasC) |
| `cd/master/needs-approval` | #1 running | waits at an `input` step -> `get_pending_inputs`, `recent_builds status=BUILDING` |
| node `offline-agent` | offline | inbound agent that never connects (created by the seeder; skipped if creation fails) |

`app-build` gets one config change from the seeder, so Job Config History has two versions. Plugins added for
the new tools: `jobConfigHistory`, `config-file-provider` (`plugins.txt`). Not seeded: a multibranch job with
`release%2F2.4` (needs a Git server) - covered by the offline tests only.

## Tool checklist (automatic = `live_test.py`; manual = ask Copilot in Agent mode with the real MCP)

| Tool | Automatic check | Manual prompt / expected |
|---|---|---|
| `list_instances` | reachable, version, no config errors, KB loaded | "list_instances check_connection" |
| `list_jobs` | all jobs, `status`, `folder`, `name_contains` filters | "which jobs failed in cd?" -> app-deploy, long-log, app-install |
| `failures_overview` | red jobs listed, green not | "what is red today?" |
| `get_job` | parameters listed, password default hidden | "show app-deploy" |
| `get_build_summary` | failed stage `Deploy`, cause found, secrets masked, URL input works, KB-0001 on app-install | "why did app-deploy #2 fail?" |
| `get_stage_log` | failed stage by default, named stage | "log of stage Build" |
| `get_log_errors` | groups; `category=maven` in 30k lines | "errors of long-log" |
| `search_log` | `retry \d/3` | "search 'retry' in app-deploy #2" |
| `get_log_lines` | head and tail | "last 20 lines of long-log" |
| `get_test_failures` | createPolicy / deletePolicy | "failed tests of app-test" |
| `compare_builds` | FAIL param/env differs | "compare app-deploy good vs bad" |
| `list_artifacts` / `read_artifact` | text read, binary refused | "show env.log of app-build" |
| `get_env_log` | `FAIL=true`, password masked | "env of app-deploy #2" |
| `node_health` | built-in node listed | UI: Manage Jenkins -> Nodes -> Built-In Node -> mark offline; then `node_health only_problems` must show it. Mark online again |
| `get_queue` | queued-job + reason | "what is in the queue?" |
| `find_known_issues` | by build and by pasted text | paste the installer error text |
| `list_known_issues` / `get_known_issue` | KB-0001 | "show KB-0001" |
| `draft_kb_entry` | writes a draft into a TEMP copy of the example KB | after a debugging chat: `/document_session`; check `knowledge/drafts/` |
| `recent_builds` | every build of the window, the running needs-approval build | "what ran today?" |
| `get_node` | built-in node, offline-agent (labels from config) | "details of offline-agent" |
| `get_pipeline_info` | executed script from the **replay page**, managed file from the log | "which Jenkinsfile ran in app-deploy #2?" |
| `get_job_config` | summary of app-deploy | "how is app-deploy configured?" |
| `read_managed_file` | content of `live-settings` from the **managed files page**, secret masked | "show the managed file live-settings" |
| `get_job_config_history` / `diff_job_config` | two versions of app-build, the seeded change | "who changed app-build?" |
| `list_workspace` / `read_workspace_file` | the **Workspaces page** of a Pipeline build, env.log in it | "show the workspace of app-build #2" |
| `compare_artifacts`, `list_artifacts sizes` | env.log differs between app-build #1 and #2 | "compare the artifacts of app-build 1 and 2" |
| `get_pending_inputs` | needs-approval waits for "Deploy to prod?" | "what is needs-approval waiting for?" |
| `get_stage_log exact` | per-step log of Deploy | "exact log of stage Deploy" |
| `compare_builds` by node | node taken from the summary | "compare app-deploy on node X" |
| `recurring_errors` | UPGRADE FAILED in app-deploy | "which errors repeat in app-deploy?" |
| `find_jobs_by_scm` | no match explained (seeded jobs have inline scripts) | try it on your real Jenkins |
| `propose_kb_update` | update proposal for KB-0001 in the TEMP knowledge dir | "add app-install #1 to KB-0001" |
| prompts | `analyse_build`, `document_session`, `morning_report` exist | type `/` in chat |

Safety checks in `live_test.py`: only `draft_kb_entry` and `propose_kb_update` are not read-only; POST is blocked by `ReadOnlySession`;
no build number changed after the run (the test triggered nothing).

## Using this Jenkins by hand (Claude Code or Copilot)

Add an instance to your `config/jenkins_instances.yaml`:
```yaml
  local:
    url: http://localhost:8080/
```
and `config/.env`: `JENKINS_LOCAL_USER=admin`, `JENKINS_LOCAL_TOKEN=<content of tests/live/.token>`.
Then `list_instances reload_config=true`. The example KB-0001 lives in `tests/fixtures/knowledge/` and is scoped to
instance `main`; it is not in your real `knowledge/` folder.
