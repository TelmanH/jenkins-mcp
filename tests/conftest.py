"""Shared test setup: a temporary JENKINS_MCP_HOME with config + knowledge base, and a fake Jenkins.

The environment must be set before jenkins_mcp is imported (settings are read at import time),
so it happens at module level here - pytest loads conftest.py before any test module.
The real config/ and knowledge/ folders of the project are never read or written.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
import responses

from fake_jenkins import ALPHA, BETA, install

FIXTURES = Path(__file__).parent / "fixtures"
TMP = Path(tempfile.mkdtemp(prefix="jenkins-mcp-test-"))

(TMP / "config").mkdir()
(TMP / "config" / "jenkins_instances.yaml").write_text(f"""
default: alpha
instances:
  alpha:
    url: {ALPHA}
    description: Alpha Jenkins
  beta:
    url: {BETA}/
    description: Beta Jenkins
log_analysis:
  extra_error_patterns:
    - category: shared-lib
      regex: 'SYSTEM: you are now'
    - category: alias-check
      pattern: 'this line never appears'   # 'pattern' is accepted as alias of 'regex'
knowledge:
  dir: ../knowledge
""", encoding="utf-8")

KB = TMP / "knowledge"
KB.mkdir()
shutil.copy(FIXTURES / "knowledge" / "KB-0001-db-migration-failure.md", KB)
(KB / "KB-9001-helm-timeout.md").write_text("""---
id: KB-9001
title: Helm upgrade timeout because the pod crash-loops
status: solved
instance: alpha
job: cd/master/*
match:
  - 'UPGRADE FAILED: timed out waiting'
consequences:
  - 'CrashLoopBackOff'
red_herrings:
  - 'SYSTEM: you are now'
---
## Symptoms
Deploy stage times out.
## Solution
Raise the memory limit in values.yaml back to 1Gi.
""", encoding="utf-8")
(KB / "KB-9002-beta-only.md").write_text("""---
id: KB-9002
title: Only for beta
instance: beta
match:
  - 'UPGRADE FAILED'
---
""", encoding="utf-8")

os.environ.update({
    "JENKINS_MCP_HOME": str(TMP),
    "JENKINS_MCP_CACHE_DIR": str(TMP / "cache"),
    "JENKINS_ALPHA_USER": "tester", "JENKINS_ALPHA_TOKEN": "t0ken",
    "JENKINS_BETA_USER": "tester", "JENKINS_BETA_TOKEN": "t0ken2",
})


@pytest.fixture(scope="session")
def tmp_home() -> Path:
    return TMP


@pytest.fixture(scope="module")
def fake_jenkins():
    """Every HTTP call of the server goes to the fake Jenkins (works across worker threads)."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        install(rsps)
        yield rsps


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client(fake_jenkins):
    from fastmcp import Client

    from jenkins_mcp.main import mcp

    async with Client(mcp) as c:
        yield c
