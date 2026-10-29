# Knowledge base

The team's memory of solved build problems. Each known issue is one Markdown file named
`KB-0001-short-name.md`. `get_build_summary` matches every build against these files automatically
and lists matching entries under `known_issues`.

This folder is data, not code. Entries describe your builds, hosts and log lines, so keep them in a
**private** repository: in the public project, `.gitignore` excludes `KB-*.md` and `drafts/`. Point the
server to your private checkout with `knowledge.dir` in `config/jenkins_instances.yaml` or with the
`JENKINS_MCP_KB_DIR` environment variable.

## Folders

| Location | Meaning |
|---|---|
| `KB-*.md` | Reviewed entries. **Used** for matching. |
| `drafts/` | Drafts written by the agent via `draft_kb_entry` (new entries) or `propose_kb_update` (`KB-0001-update-<date>.md`, changes to an existing entry). **Not used.** |
| `TEMPLATE.md` | Copy this to start an entry by hand. Not loaded (its name does not start with `KB-`). |

Draft workflow (human in the loop):
1. At the end of a debugging session the agent writes a draft to `drafts/` with `draft_kb_entry`.
2. You review it: is the root cause correct, are the `match` regexes specific enough?
3. Move the file up into the knowledge folder and commit it (private repo). Only then is it used for matching.
4. An update proposal (`KB-0001-update-<date>.md`) is a full copy of the entry plus an `## Update proposal`
   section. If it is right, replace the original file with it (keep the original file name) and remove that section.

The server re-reads the files automatically when they change; no restart needed.

## Fields (front matter)

| Field | Required | Description |
|---|---|---|
| `id` | yes | Unique, in the form `KB-0001` |
| `title` | yes | "symptom -> cause" |
| `status` | | `open`, `solved`, `workaround` |
| `instance` | | A single instance name or a list. Empty = all instances |
| `job` | | Glob, e.g. `deploy/*`. Empty = all jobs |
| `match` | yes* | Log lines that identify this issue (regex, case-insensitive). If even one matches, the entry is suggested |
| `consequences` | | Errors that are a **result** of this issue. Removed from the root-cause list when the entry matches |
| `red_herrings` | | Errors that are **always** harmless in this scope (instance/job), e.g. ones that also appear in green builds. Removed from the root-cause list even if the entry itself does not match |
| `stage`, `category`, `version`, `builds`, `date`, `author` | | Informational |

\* An entry with only `red_herrings` is also valid (e.g. "this job always prints a harmless locale warning").

## Regex tips

- Use single quotes: `'Unpack failed \(xml line \d+\)'` - backslashes stay as they are.
- Escape characters such as `( ) [ ] . * + ?` with `\`.
- Do not use generic words (`ERROR`, `Exception`); copy a distinctive sentence from the log.

## Sections

`## Symptoms`, `## Root cause`, `## Solution`, `## How it was found`.
The first line of `## Solution` is shown as "fix:" in the `get_build_summary` output.
