---
id: KB-0000                      # next free number; file name: KB-0000-short-name.md
title: <symptom> -> <cause>
status: open                     # open | solved | workaround
instance:                        # instance name(s) from config, empty = all
job:                             # glob such as 'deploy/*', empty = all jobs
stage:
category:
version:                         # version of the first build where it was seen
match:                           # 1-3 distinctive log lines (regex, case-insensitive)
  - '<distinctive line copied from the log>'
consequences: []                 # follow-up errors caused by this issue - not root causes
red_herrings: []                 # errors that also appear in green builds - not root causes
builds: []                       # build URLs where it was seen
date:
author:
---

## Symptoms
What the user sees (failed stage, misleading last error lines).

## Root cause
The verified cause, with the key log lines.

## Solution
First line is shown as "fix:" in build summaries.

## How it was found
Searches, comparisons, what was ruled out and why.
