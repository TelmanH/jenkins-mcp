---
id: KB-0001
title: Database drop/create fails -> installer "Unpack failed" -> app server does not start
status: open                     # open | solved | workaround
instance: main
job: deploy/*                    # glob; * also covers sub-folders
stage: Deploy Application
category: database
version: "1.4.2"                 # version of the first build where it was seen
match:                           # searched in the log lines (case-insensitive)
  - 'Database drop/create failed'
  - 'Errors occurred during execution'
  - 'InstallerException: Unpack failed'
consequences:                    # errors CAUSED by this issue - not the root cause
  - 'WFLYPRT0053'
  - 'controller is not available at localhost:9990'
  - 'Errors occurred during the installation'
red_herrings:                    # also seen in green builds of these jobs - not the root cause
  - 'cygpath: No such file or directory'
  - '/bin/apppwd'
  - '/bin/appuname'
builds:
  - https://jenkins.example.com/job/deploy/job/app-install/5/
date: 2026-10-20
author: Example Author
---

## Symptoms
The deployment job fails after ~40 minutes in stage "Deploy Application".
The end of the log shows an app server connection error (`WFLYPRT0053`, `localhost:9990 connection refused`),
so at first sight it looks like an app server problem.

## Root cause
The database drop/create step of the installation fails:

```
WARNING: Errors occurred during execution.
com.example.installer.InstallerException: Unpack failed (xml line 53)
[ERROR] - Database drop/create failed on deploy-agent-01:
```

The installer aborts because of it; the app server error is only a consequence.
The real error message is not in the Jenkins log but in `drop_create.log` on the agent.

## Solution
Check `/home/deploy/work/app/logs/db/drop_create.log` on the agent (which schema / which SQL step failed).

## How it was found
Searched the log for "error", found the installer/DB lines. The `cygpath` error also appears in a green
pipeline, so it was ruled out. The first analysis blamed `cygpath`, the locale and the app server - wrong.
