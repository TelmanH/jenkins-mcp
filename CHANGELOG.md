# Changelog

## Unreleased

- Minimum versions raised: fastmcp 4.1.3, requests 2.34.2; dev: pytest 9.1.1, pytest-asyncio 1.4.0,
  responses 0.26.3. CI uses actions/checkout and actions/setup-python v7. Package status is Beta until the
  first real Windows/VS Code Copilot run.
- `list_instances`: shows where each token comes from (environment or `config/.env`, never the value);
  `check_connection` also shows the token's user and groups (`whoAmI`), admin rights (as information)
  and quiet-down mode.
- Fix: `reload_config=true` no longer lets `config/.env` overwrite a token set by the MCP client
  (e.g. VS Code `inputs`).
- New tool `recent_builds`: every build in a time window across all instances, with culprits and a
  "longer than usual" flag for running builds. One unreachable instance shows an error entry and does not fail the call.
- `list_jobs` / `failures_overview`: failing jobs show the last green build (`lastStableBuild`; UNSTABLE is not green).
- `get_build_summary`: `culprits` for failed and unstable builds.
- New tool `get_node`: live executors (which build runs where), labels, Java version and the node's config.xml
  (remote root, launcher, env vars masked, tool locations); `node_health` shows labels.
- Jenkins config.xml files (XML 1.1) are parsed with a shared helper (`xmlutil.py`).
- New tool `get_pipeline_info`: Jenkinsfile source, git checkouts, Shared Library versions with a version-mismatch
  hint, replay/restart cause, slowest steps (wfapi), and optionally the executed script (replay page, masked).
- New tools `get_job_config` (config.xml summary incl. Config File Provider files and credential IDs; `raw`
  masked XML) and `read_managed_file` (managed file content, masked). `get_pipeline_info` lists managed files
  provisioned by the build.
- Masking also covers secret XML elements (`<password>`, `<token>`, `<privateKey>`...) and Jenkins-encrypted values.
- New tools `get_job_config_history` and `diff_job_config` (Job Config History plugin; JCasC/JobDSL hint for
  changes by SYSTEM; semantic + masked XML diff, also against the live config).
- `compare_builds`: `pipeline_changes` lists commits that touch a Jenkinsfile or `*.groovy` file.
- New tools `list_workspace` and `read_workspace_file`: files that were not archived (freestyle job workspace or
  the workspace of a Pipeline node block; current state on the agent).
- Text files are decoded as UTF-16 (BOM), UTF-8 or cp1252; UTF-16 files were wrongly refused as binary before.
- New tool `compare_artifacts` (lists, fingerprint/size/byte comparison, text diff, binary checksum);
  `list_artifacts` shows sizes (`sizes=true`) and the instance.
- `get_build_summary`: `downstream` builds (from `Starting building: ...`) with their result and a hint to analyse a
  failed child first; `waiting_for_input` for running builds.
- New tool `get_pending_inputs` (read only); `get_stage_log exact=true` reads a stage step by step from wfapi.
- Running builds: the log is fetched incrementally (`progressiveText`), not downloaded again on every call.
- Logs larger than `JENKINS_MCP_MAX_LOG_MB` are cached too (they were downloaded again on every call).
- Console logs: UTF-8 is kept when only a few bytes are broken; logs in cp1252 are decoded as cp1252.
- `compare_builds`: `good_on_node` / `bad_on_node` (newest good/failed build on an agent) and `other_instance`
  (same job on another instance).
- New tool `recurring_errors`: error groups across the newest builds (regression vs. also in green builds).
- New tool `propose_kb_update` (local write, like `draft_kb_entry`): an update proposal for an existing entry as a
  new draft. `document_session` uses it when an entry already exists.
- `list_known_issues`: `matched_since_start` hit statistics (in memory).
- New tool `find_jobs_by_scm`: jobs whose last build checked out a repository, URL spellings normalized.
- New prompt `morning_report` (stand-up / Build Sheriff check across all instances).
- Windows: `setup.ps1` writes `.vscode/mcp.json` with one VS Code password prompt per instance (tokens in VS Code's
  secure storage, `-TokensInEnvFile` for the old way; `python -m jenkins_mcp.vscode_config`). `.vscode/mcp.json` is now
  git-ignored (template: `.vscode/mcp.example.json`). An empty or unresolved prompt does not hide `config/.env`.
- Per-instance `proxy:` (`none` = direct).
- Job lists are reused for 60 s (`JENKINS_MCP_JOB_CACHE_S`), so large instances are not walked again and again.
- A build removed by log rotation gets a clear message with the oldest/newest kept build.
- Every Jenkins answer names its instance (also the text tools and `get_test_failures`); tests guard this and the
  size of the tool list.
- Verified against a real Jenkins (2.580.1, live Docker test, 129 checks, 0 failed). Fixes found there:
  - `list_workspace` / `read_workspace_file`: the Pipeline "Workspaces" page wraps the path in `<code>` and puts the
    node after the link; such builds were reported as having no workspace.
  - Masking: the default of a Pipeline `password(name: ..., defaultValue: '...')` parameter in an inline script
    (`get_job_config`, replay script) was shown.
  - `read_managed_file`: an unknown id (Jenkins answers HTTP 500) now gives "not found" instead of the raw error.
  - `diff_job_config`: a job with only one recorded version gets a clear message.
  - The SYSTEM hint in `get_job_config_history` also names Declarative Pipelines, which save their own
    parameters/triggers as SYSTEM after a build.
- Live test: `casc.yaml` lives outside the Jenkins volume, so a rebuilt image always applies it; the seeder's config
  change works with Jenkins' `<description></description>`; the test waits for the input build and uses a 30-day window.

## 1.0.0 - 2026-10-25

First public release.

- 20 read-only MCP tools: build summaries with root-cause ranking, stage logs, error groups, log search,
  test failures, build comparison, artifacts, node health, queue, and a team knowledge base.
- Server-side log analysis with a size budget per answer; logs up to 200 MB (larger ones keep head and tail).
- Knowledge base of known issues (Markdown) with automatic matching, red herrings and consequences;
  drafts written by the assistant for human review.
- Read-only by construction (GET/HEAD only), credentials only sent to configured hosts, secret masking.
- Setup for Windows + VS Code Copilot, local testing on macOS + Claude Code and Windows + VS Code.
- Offline test suite (fake Jenkins) and a live test against a real Jenkins in Docker.
