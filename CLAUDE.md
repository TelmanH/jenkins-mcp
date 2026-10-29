# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A read-only, multi-instance Jenkins MCP server (FastMCP 4, stdio transport, Python ≥3.10). It analyses
large build logs server-side and returns small excerpts, and matches builds against a Markdown team
knowledge base. Entry point: `jenkins-mcp` → `jenkins_mcp.main:run`.

## Commands

Setup on macOS (creates `.venv`, installs `-e ".[dev]"`, registers the server with `claude mcp add`):
`tests/macos/setup.sh`. On Windows, run `setup\windows\setup.ps1 -Dev`.

```bash
.venv/bin/pytest -q                                  # all offline tests (~2 s, no network)
.venv/bin/pytest -q tests/test_log_tools.py          # one file
.venv/bin/pytest -q tests/test_tools.py::test_name   # one test
.venv/bin/pytest -q -k search                        # by name
.venv/bin/mypy src --ignore-missing-imports          # type check (as CI runs it)
tests/live/run_live.sh                               # real Jenkins in Docker + live check of every tool
.venv/bin/python -m jenkins_mcp.vscode_config --output /tmp/mcp.json   # preview the VS Code config setup.ps1 writes
docker compose -f tests/live/docker-compose.yml down -v   # remove the live Jenkins
```

CI (`.github/workflows/tests.yml`) runs pytest and mypy on Ubuntu, Windows and macOS with Python 3.10 and 3.13,
plus gitleaks. There is no linter configured.

## Hard rules

- **Never write to Jenkins.** `ReadOnlySession` (`client.py`) rejects every method except GET and HEAD, and
  `Instance.abs_url` refuses hosts outside the configured instance, so credentials are never sent elsewhere.
  New Jenkins tools use `@mcp.tool(annotations=READ_ONLY)` and only `Instance.get` / `json` / `probe` / `size`.
  The only tools that write are `draft_kb_entry` and `propose_kb_update` (`LOCAL_WRITE`). Each creates a new file
  in `knowledge/drafts/` (`open(..., "x")`) and never overwrites one.
- **stdout is the MCP transport.** Never `print()`; use `settings.log` (stderr).
- Tool output that contains log or artifact content is prefixed with `settings.UNTRUSTED`, passed through
  `text.mask` (secret masking, also XML `<password>` elements and `{AQAA...}` values) and kept within
  `text.budget` (`JENKINS_MCP_MAX_CHARS`). Configuration tools show credential IDs, never values.
- Every answer from a Jenkins tool names its instance (`"instance"` field, or "build #42 on alpha" in text output).
- Test data must be synthetic (`example.com`, made-up names). Never commit real Jenkins URLs, logs, tokens or
  `knowledge/KB-*.md` entries (`.gitignore` excludes them and `config/jenkins_instances.yaml`/`.env`).

## Architecture

- `app.py` holds the `FastMCP` object, the server `INSTRUCTIONS` (the analysis workflow given to the AI
  client), the `READ_ONLY`/`LOCAL_WRITE` annotations and the shared parameter types `Job`, `Build` and `InstanceName`.
  Tools register by import side effect: `main.py` imports `tools` and `prompts`, and `tools/__init__.py`
  imports each tool module. A new tool module must be added there.
- `settings.py` resolves `HOME` (`JENKINS_MCP_HOME`, otherwise the nearest parent dir with
  `config/jenkins_instances(.example).yaml`), loads `config/.env` and reads env tunables **at import time**.
  `PROCESS_ENV` is a snapshot of the environment before `.env` is loaded. Variables set by the MCP client
  (e.g. VS Code `inputs`) always win over `.env`, also in `config.reload()`.
- `config.py` loads the YAML and `.env` into a `Settings` object (instances, compiled log rules, KB dir) held
  in a module-level singleton (`current()`, `reload()`, behind a lock because tools run in worker threads). Config
  errors are collected and reported through `list_instances` instead of being raised. `list_instances(reload_config=true)`
  reloads without a restart.
- `xmlutil.parse_xml` must be used for every Jenkins XML (job/node `config.xml`, config history). Jenkins writes
  XML 1.1, which `xml.etree` rejects without it.
- `targets.py` turns `job` (a `folder/sub/job` path, a pasted `job/a/job/b` path or a full URL, which also selects the
  instance) plus `build` (a number or a permalink) into a `Target`.
- The log pipeline is `logs/`. `cache.load_log` downloads a finished build's log once, keeps it in a disk cache
  (including truncated logs, which keep their marker) and an in-memory LRU of 6, and parses it into a `ParsedLog`.
  Running builds are fetched incrementally (`progressiveText` from the last offset, `_RUNNING`). A log over
  `MAX_LOG_MB` keeps the first 20 % and the end. Bytes are decoded with `text.decode_text` (UTF-16 BOM, UTF-8, cp1252).
  `stages.py` detects pipeline stages and the failed stage. `errors.py` classifies lines with user rules
  (`log_analysis.extra_error_patterns`, checked first) and then `rules/default_rules.yaml` (first match wins,
  ranked by severity high > medium > warn > low), and groups identical lines.
  `pipeline.py` reads the Jenkinsfile source and Shared Library versions from the log, because the REST API
  doesn't export a build's libraries.
- Knowledge base: `kb/`. `store.py` loads `KB-*.md` files (front matter + sections) into a cached `KnowledgeBase`
  that reloads when a file's signature changes. `drafts/` entries are never used for matching.
  `match.analyse` applies `match` / `consequences` / `red_herrings` regexes, scoped by `instance` and the
  `job` glob, to a log. `tools/builds.py:ranked_causes` uses that to drop noise from the root-cause candidates.
- `tools/builds.py:get_build_summary` is the main tool and combines all of the above (plus downstream builds and
  pending inputs). `compare_builds` compares a good and a bad build, also by node or across instances. Other modules
  in `tools/`:
  - `jobs.py`: job tree walks with a 60 s cache (`_cached_tree`, `JENKINS_MCP_JOB_CACHE_S`)
  - `jobconfig.py`: `summarize_config`, reused by `config_history.py`
  - `pipeline.py`: what ran, pending inputs
  - `workspace.py`
  - `artifacts.py`: shared text reading via `stream_bytes` / `text_lines` / `render_text`, also used by the workspace tools
  - `trends.py`: `recurring_errors`
  - `infra.py`: nodes
- `vscode_config.py` generates `.vscode/mcp.json` for `setup.ps1`, with one `promptString` input (`password: true`)
  per instance. That file is git-ignored; `.vscode/mcp.example.json` is the committed template.
- Some tools parse Jenkins HTML pages, whose markup can change between versions: the managed-files page
  (`read_managed_file`; an unknown id gives HTTP 500, not 404), the Pipeline "Workspaces" page (`/<n>/ws/`,
  path in `<code>`, node after the link), the replay page. They and the Job Config History JSON, `wfapi` step logs
  and `pendingInputActions` passed the live test on Jenkins 2.580.1. Keep `tests/fake_jenkins.py` in the real
  markup and rerun `tests/live/run_live.sh` after changing them.
- A Declarative Pipeline saves its own config as user SYSTEM after a build, so SYSTEM in the config history does not
  always mean JCasC/JobDSL.

Known design debt (from the README roadmap): the module-level singletons (config, KB cache, log LRU) and the
global config read inside `logs/` are planned to move into one application context object.

## Tests

- `tests/conftest.py` creates a temporary `JENKINS_MCP_HOME` (config + KB) and sets the env vars **at module
  level, before `jenkins_mcp` is imported**, because settings are read at import time. The real `config/` and
  `knowledge/` are never touched.
- `tests/test_robustness.py` requires every Jenkins tool to name its instance (add new tools to
  `CALLS_WITH_INSTANCE`) and the tool list to stay under `TOOL_LIST_BUDGET` characters, because it is sent with every request.
- `tests/fake_jenkins.py` fakes two instances (`alpha`, `beta`) with `responses`. `CALLS` records requests, which is
  how the tests check that only GET/HEAD is sent; HEAD returns headers without a body, like a real server. New
  endpoints go there; add them without changing existing data that tests depend on. The `client` fixture is a
  module-scoped in-process `fastmcp.Client`, one per test module. Module-level state (log LRU, job tree cache,
  KB hit statistics) is shared across modules, so tests must not rely on it being empty. A test that writes a KB
  draft deletes it again, except the one in `test_tools.py`.
- Sync unit tests go into modules without the asyncio `pytestmark` (`test_text.py`, `test_config.py`,
  `test_log_parsing.py`, `test_windows_setup.py`).
- `tests/test_tools.py` calls every tool through the MCP protocol, **in file order**: a later test depends on the
  draft that an earlier test writes. It also asserts the exact tool count, with `draft_kb_entry` and `propose_kb_update` as the only writers.
  Update that assertion when you add a tool.
- `tests/live/` is a standalone script (pytest skips it); it needs Docker. Behaviour that depends on a real
  Jenkins also gets a check in `tests/live/live_test.py`.

## Status and next steps

- Verified by the live Docker test and a manual Windows / VS Code Copilot run against real instances.
- Planned next (details in the README roadmap): the 1.1.0 release (package status back to Stable, CHANGELOG,
  tag) and read-only plugin tools, which need a check that the users' tokens can read `/pluginManager/api/json`.
- `main` is protected: every change goes through a branch and a PR with green Ubuntu/Windows/gitleaks checks.

## Docs to keep in sync

- README.md has the tool table, the env var table and the tool count.
- `docs/USER_GUIDE.md` is the user guide.
- `setup/windows/README.md` is the company setup guide, with the VS Code token prompts.
- `tests/live/README.md` lists the seeded jobs and the tool checklist.
- `knowledge/README.md` documents the KB front-matter fields.
- `CHANGELOG.md` records changes in the `Unreleased` section.
