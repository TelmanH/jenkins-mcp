# Jenkins MCP - User Guide

Installation: company (Windows + VS Code Copilot) [setup/windows/README.md](../setup/windows/README.md), local testing (macOS + Claude Code) [tests/README.md](../tests/README.md).

## 1. First check

Ask the agent (Claude Code, or Copilot Chat in **Agent** mode):

> list_instances with check_connection

Expected: every instance shows `status: ok, Jenkins <version>`, and there is no `config_errors` field.

Per instance you also see:
- `token`: where the token comes from: `from the environment` (VS Code `inputs`, the MCP client config
  or the shell) or `from config/.env`. The token itself is never shown. A token from the environment
  always wins over `config/.env`, including after `reload_config=true`.
- `user`: the Jenkins user the token belongs to, plus its groups (`whoAmI`).
- `admin_rights` (only when it applies): the token could administer Jenkins. This is information, not an
  error. The server still sends only GET/HEAD requests.
- `quieting_down` (only when it applies): Jenkins is preparing for a shutdown and new builds do not start.

| Problem | Meaning / fix |
|---|---|
| `JENKINS_X_TOKEN is not set` | Windows/VS Code: run `setup.ps1` again and restart the server, VS Code asks for the token. Otherwise add it to `config/.env`, then `list_instances reload_config=true` |
| `Authentication failed (401)` | Wrong user or token. Use an API token, not the password |
| `TLS verification failed` | Internal CA: set `ca_bundle: certs/internal-ca.pem` for the instance |
| `Cannot connect` | VPN, proxy or DNS. Is the URL reachable from this machine? Per instance `proxy:` (or `proxy: none`) in the yaml |
| `has no build '5' ... removed by the build discarder` | The build was deleted by log rotation; the message names the oldest kept build |
| `config_errors: ...` | The message names the broken entry in `config/jenkins_instances.yaml` and why |

## 2. Analysing a failed build

Paste the build URL from the browser; the instance, job and build are detected automatically:

> Why did https://jenkins.example.com/job/deploy/job/app-install/57/ fail?

Or use the prompt: Copilot `/mcp.jenkins.analyse_build`, Claude Code `/mcp__jenkins__analyse_build`.

The agent starts with `get_build_summary`. If the build only failed because a build it started with the
`build` step failed, the summary lists it under `downstream` and the first next step is to analyse that
build. Running builds show `waiting_for_input` when an `input` step waits for approval. Their log
is fetched incrementally.

The agent starts with `get_build_summary`, which returns:
- result, duration, trigger, parameters (secrets masked), agent node, commits;
- stages and the failed stage;
- `known_issues`: matching knowledge base entries with their documented fix;
- `likely_root_cause`: the most probable error lines with line numbers;
- `known_noise`: errors the knowledge base marks as harmless or as consequences.

Useful follow-up questions:

| Question | Tool |
|---|---|
| "Show the log of the failed stage" | `get_stage_log` |
| "All errors in this build" | `get_log_errors` |
| "Search for 'timeout' with 3 lines of context" | `search_log` |
| "Show lines 1200-1300" | `get_log_lines` |
| "Which tests failed and since when?" | `get_test_failures` |
| "Show the Deploy stage exactly, it runs in parallel" | `get_stage_log` with `exact` |
| "What is this running build waiting for?" | `get_pending_inputs` |
| "What changed since the last green build?" | `compare_builds` |
| "It works on build-agent-01 but fails on build-agent-03" | `compare_builds` with `good_on_node` / `bad_on_node` |
| "It works on staging but fails on prod" | `compare_builds` with `other_instance` |
| "Show env.log" / "read report.txt" | `get_env_log` / `read_artifact` |
| "Which Jenkinsfile and library versions did this build use?" | `get_pipeline_info` |
| "Show the Jenkinsfile that actually ran" | `get_pipeline_info` with `show_script` |

`get_pipeline_info` reads the Jenkinsfile source and the Shared Library versions from the log ("Loading
library shared-lib@rel-2.3"), and the git checkouts from the build. If a library branch carries a version
(e.g. `rel-2.3`) that matches none of the build's `*VERSION*` parameters (e.g. `REL_VERSION=2.4`), it adds a
hint. Replayed or restarted builds are flagged, because their script can differ from Git. `show_script`
needs the Run/Replay permission. Without it you get a clear message and the rest of the answer.

`good_on_node` / `bad_on_node` pick the newest successful or failed build that ran on that agent. For
Pipeline jobs, the agent is only in the log, so up to 10 logs are read (cached). `other_instance` takes
the good build of the same job from another Jenkins instance. The commit range is skipped then, because
commits cannot be compared across instances.

### Comparing artifacts

> Which artifacts differ between the last green and the last failed build?  (`compare_artifacts`)

> Show the diff of logs/env.log between #41 and #42.  (`compare_artifacts path=logs/env.log good=41 bad=42`)

Without `path` you get the artifacts that exist in only one build, and the common ones that changed.
Changes are detected by the Jenkins fingerprint (MD5, no download) if the job records fingerprints,
otherwise by size; small files (up to 1 MB) are compared byte by byte. With `path` you get a unified diff
of a text artifact (secrets masked), or the SHA-256 and size of a binary. Binary files such as jars,
zips, PDFs or Excel files (e.g. a release record) are never sent to the model: `list_artifacts` gives the
download URL and, with `sizes=true`, their sizes.

### Files that were not archived (workspace)

> Read app-queue.log from the workspace of build 42 and search for ERROR.  (`list_workspace`, `read_workspace_file`)

When a file is not an artifact, it may still be in the job's workspace on the agent:
- Freestyle jobs have one workspace per job.
- A Pipeline build has one workspace per `node` block. If there are several, `list_workspace` lists them
  (`node_id=...`) and you pick one.
- The workspace is the **current** state on the agent. A later build may have changed or cleaned it.
  The files of exactly this build are only in the artifacts.
- It needs the **Job/Workspace** permission and a reachable agent. A cleaned workspace (`cleanWs`), an offline
  agent or a removed Kubernetes pod gives a clear message.

Text files in UTF-8, UTF-16 (with byte order mark, typical for Windows tools) and cp1252 (German Windows
tools) are decoded, for artifacts too. Binary files are refused.

## 3. Daily overview

For the stand-up or a Build Sheriff round, run the prompt `morning_report` (Copilot `/mcp.jenkins.morning_report`,
Claude Code `/mcp__jenkins__morning_report`). It checks every instance (reachable? quieting down?), red
jobs with "failing since", long-running builds, offline agents, low disk and the queue, and it summarises the
three most important failures in one line each.

> What failed in the last 24 hours?  (`failures_overview`)

> Show every build of today, including the green ones. Which builds run longer than usual?  (`recent_builds`)

Failing jobs show `last green #N <age>`, the last build with result SUCCESS. UNSTABLE builds do not count
as green, so "failing since" is accurate. `no green build on record` means none is kept (new job, or old
builds removed by log rotation). A running build is flagged `longer than usual` when it runs more than
1.5 times its estimated duration. Failed builds list the `culprits` (authors of the commits in the build).

> Are the agents healthy? Is anything stuck in the queue?  (`node_health`, `get_queue`)

> What runs on build-agent-01 right now, which labels does it have, which Java and Maven does it use?  (`get_node`)

`get_node` combines the live state (labels, executors with the running build, monitors) with the node's
`config.xml` (remote root, mode, launch method, node environment variables with secrets masked, tool
locations). If your token lacks Computer/ExtendedRead, only the live part is shown and `config` says why.
The Java version appears only if a JVM version monitor exists (versioncolumn plugin).

> Which jobs build https://git.example.com/scm/team/app.git? Which jobs load shared-lib?  (`find_jobs_by_scm`)

`find_jobs_by_scm` looks at the repositories each job's last build checked out, across all instances.
https, ssh (`git@host:org/repo`, `ssh://...:7999/...`), Bitbucket `/scm/` URLs and `.git` endings are
treated as the same repository, and a part like `team/app` is enough. Jobs that were never built are
not found this way.

## 4. Job configuration

> How is cd/master/app-deploy configured? Which repo, branch, Jenkinsfile, triggers and managed files does it use?  (`get_job_config`)

> Show the Maven settings file maven-settings-nexus.  (`read_managed_file`)

`get_job_config` reads `config.xml` and returns a summary. Credentials appear only as IDs, and
Jenkins-encrypted values (`{AQAA...}`) are never shown. `raw=true` returns the whole XML, masked and
size-limited. It needs **Job/ExtendedRead** (or Job/Configure); without it you get a clear 403 message.

Config File Provider files appear under `managed_files`:
- files from a build wrapper or the Maven `settingsConfigId` are listed by `get_job_config`;
- files a Jenkinsfile provides at runtime (`configFileProvider([...])`) appear in the build log, so
  `get_pipeline_info` lists them.

`read_managed_file` shows the content with passwords masked. Pass `folder=` for files that are defined in
a folder. Note: this reads the page Jenkins shows under *Manage Jenkins > Managed files*. It has not yet been
verified against your Jenkins versions, and if the page looks different you get a message with the URL.

### Who changed the job?

> Who changed the configuration of cd/master/app-deploy, and what changed last time?  (`get_job_config_history`, `diff_job_config`)

> Compare the version from 2026-10-01 with the live configuration.  (`diff_job_config older=... newer=current`)

This needs the **Job Config History** plugin. Without it you get a clear message, and `get_job_config`
still shows the current state. `diff_job_config` lists the changed fields (triggers, parameters, SCM,
script path, retention...) and an XML diff, with secrets masked. Changes by `SYSTEM` usually come from JCasC
or a JobDSL seed job: then the source of truth is in Git, and a change made in the UI will be overwritten.

Changes to the Jenkinsfile itself live in Git. `compare_builds` lists them under `pipeline_changes`, as
the commits between the two builds that touch a `Jenkinsfile*` or `*.groovy` file.

### Recurring errors

> Which errors of cd/master/app-deploy repeat over the last 10 builds, and which also appear in green builds?  (`recurring_errors`)

Each error group shows in how many failed and green builds it occurs, the first and last build, and a
verdict: `new since #N` (a regression; start there) or `also in green builds` (probably not the cause:
a candidate for `red_herrings` in a knowledge base entry). It reads one log per build (max 20, cached).

## 5. Documenting a solved problem

When the root cause is confirmed, run the `document_session` prompt or say:

> Document this in the knowledge base.

The agent checks for an existing entry and writes a **draft** to `knowledge/drafts/`. Review it:
- Is the root cause right?
- Are the `match` lines specific? Copy distinctive sentences from the log, never just `ERROR`.
- Are harmless errors listed under `red_herrings` and follow-up errors under `consequences`?

If an entry already exists, the agent writes an **update proposal** with `propose_kb_update` instead of a
duplicate: a copy of the entry with the new build, extra regexes and an `## Update proposal` section. If
it is right, replace the original file with it.

`list_known_issues` shows `matched_since_start`: how many builds each entry matched since the server
started. Entries that never match may have outdated regexes.

Then move the file to `knowledge/` and commit it to your team's private knowledge repo. From that moment every analysis of a matching
build shows the entry. Field reference: [knowledge/README.md](../knowledge/README.md).

## 6. Custom error rules

If your logs contain product-specific error lines, teach the server to recognise them in
`config/jenkins_instances.yaml`:

```yaml
log_analysis:
  extra_error_patterns:
    - category: installer
      severity: high          # high = can be a root cause, medium = ranked after high, low = hidden by default
      regex: 'Installation FAILED|InstallerException'
  ignore_patterns:            # lines that look like errors but are not
    - 'Retrying in \d+ seconds'
  noise_patterns:             # hidden from summaries, still searchable
    - '^\[INFO\] Scanning'
```

Apply them with `list_instances reload_config=true`.

Lines that start like an error (`Error: ...`, `fatal: ...`, `npm ERR! ...`, `FAILURE: ...`) but match no
specific rule are still reported, as category `generic` with severity `medium`: visible in `get_log_errors`
and ranked as root cause only when no specific (`high`) error exists. Write your own `high` rule when such a
line is a typical root cause in your builds.
