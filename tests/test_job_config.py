"""get_job_config (config.xml summary incl. Config File Provider files and credential IDs) and
read_managed_file (Config File Provider content, masked). Edge cases: XML 1.1 with control-character
references, 403 without Job/ExtendedRead, unknown managed file, secrets in XML elements."""

import json

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="module")

DEPLOY = "cd/master/app-deploy"
BUILD = "cd/master/app-build"


async def call(c, tool: str, **args) -> tuple[str, bool]:
    res = await c.call_tool(tool, args, raise_on_error=False)
    return "\n".join(getattr(b, "text", "") for b in res.content), res.is_error


async def test_pipeline_job_config(client):
    t, err = await call(client, "get_job_config", job=DEPLOY)
    assert not err, t
    d = json.loads(t)
    assert d["instance"] == "alpha" and d["type"] == "Pipeline (Jenkinsfile from SCM)"
    assert d["description"] == "Deploys the app [1mfast[0m", "XML 1.1 control-character references survive parsing"
    assert d["scm"] == ["git https://git.example.com/app.git branches */master (credentials: git-readonly)"]
    assert d["script_path"] == "ci/Jenkinsfile.deploy" and d["lightweight_checkout"] is True
    assert "NEXUS_PASSWORD (Password) default=****" in d["parameters"]
    assert "REL_VERSION (String) default=2.4" in d["parameters"]
    assert "TARGET_ENV (Choice) choices=dev, test" in d["parameters"]
    assert d["triggers"] == ["cron: H 2 * * *", "after upstream: cd/master/app-build"]
    assert d["keep_builds"] == "30 builds" and d["concurrent_builds"] is False
    assert d["credential_ids"] == ["git-readonly"]
    assert "AQAAABAAAAAQ" not in t, "encrypted secrets never shown"


async def test_freestyle_job_config_with_managed_files(client):
    t, err = await call(client, "get_job_config", job=BUILD)
    assert not err, t
    d = json.loads(t)
    assert d["type"] == "Freestyle" and d["disabled"] is True and d["label"] == "linux && docker"
    assert d["managed_files"] == ["maven-settings-nexus -> settings.xml (variable MVN_SETTINGS)",
                                  "maven-settings-nexus (Maven settings)"]
    assert any(b.startswith("Shell: mvn -s $MVN_SETTINGS") for b in d["builders"])
    assert "s3cr3t" not in t, "inline secret in a build step masked"
    assert d["publishers"] == ["ArtifactArchiver"]
    assert "read_managed_file" in " ".join(d["next_steps"])


async def test_job_config_raw_and_forbidden(client):
    t, err = await call(client, "get_job_config", job=BUILD, raw=True)
    assert not err and "<builders>" in t and "s3cr3t" not in t, "raw XML is masked"
    t, err = await call(client, "get_job_config", job="core-build", instance="beta")
    assert err and "ExtendedRead" in t, "403 explained"


async def test_read_managed_file(client):
    t, err = await call(client, "read_managed_file", id="maven-settings-nexus")
    assert not err, t
    assert "<username>deployer</username>" in t and "<password>****</password>" in t
    assert "nexusPass1" not in t
    t, err = await call(client, "read_managed_file", id="nope")
    assert err and "nope" in t and "get_job_config" in t


async def test_managed_files_from_the_log(client):
    t, _ = await call(client, "get_pipeline_info", job=DEPLOY, build=42)
    assert json.loads(t)["managed_files"] == ["Nexus settings"], "configFileProvider in a Jenkinsfile shows up in the log"
