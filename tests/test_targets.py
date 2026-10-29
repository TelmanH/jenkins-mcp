"""Unit tests: job/build addressing (targets.py) and the URL safety of the HTTP client (client.py)."""

import pytest
from fastmcp.exceptions import ToolError

from fake_jenkins import ALPHA, BETA
from jenkins_mcp.client import Instance
from jenkins_mcp.config import get_instance
from jenkins_mcp.targets import job_path, normalize_build, resolve


def test_job_path_encodes_each_segment():
    assert job_path(["a", "b c", "release/1"]) == "/job/a/job/b%20c/job/release%2F1"
    assert job_path([]) == ""


@pytest.mark.parametrize("build, expected", [
    ("#42", "42"), (42, "42"), ("lastfailedbuild", "lastFailedBuild"), ("LASTBUILD", "lastBuild"),
    ("", "lastBuild"), (None, "lastBuild"),
])
def test_normalize_build(build, expected):
    assert normalize_build(build, "lastBuild") == expected


def test_normalize_build_rejects_garbage():
    with pytest.raises(ToolError, match="Invalid build 'abc'"):
        normalize_build("abc", "lastBuild")


def test_resolve_plain_name_uses_default_instance():
    t = resolve("cd/master/app-deploy")
    assert (t.inst.name, t.parts, t.build) == ("alpha", ["cd", "master", "app-deploy"], "lastBuild")
    assert t.path == "/job/cd/job/master/job/app-deploy"
    assert t.build_url(7) == f"{ALPHA}/job/cd/job/master/job/app-deploy/7/"


def test_resolve_instance_by_exact_or_partial_name():
    assert resolve("x", instance="beta").inst.name == "beta"
    assert resolve("x", instance="ALP").inst.name == "alpha"


def test_resolve_pasted_url_detects_instance_job_and_build():
    t = resolve(f"{ALPHA}/job/cd/job/master/job/app-deploy/42/console")
    assert (t.inst.name, t.full_name, t.build) == ("alpha", "cd/master/app-deploy", "42")
    assert resolve(f"{BETA}/job/core-build/lastFailedBuild/").build == "lastFailedBuild"
    assert resolve(f"{ALPHA}/job/a/job/b/42/", build=40).build == "40", "explicit build wins over URL"


def test_resolve_pasted_relative_path():
    assert resolve("job/cd/job/master/job/app").parts == ["cd", "master", "app"]


@pytest.mark.parametrize("job, message", [
    ("", "must not be empty"),
    ("https://evil.example.com/job/x/1/", "does not belong"),
    (f"{ALPHA}/view/all/", "Could not find a job path"),
])
def test_resolve_errors(job, message):
    with pytest.raises(ToolError, match=message):
        resolve(job)


def test_unknown_instance_lists_alternatives():
    with pytest.raises(ToolError, match="Available: alpha, beta"):
        get_instance("nope")


def _inst(url: str) -> Instance:
    return Instance(name="x", url=url, user="u", token="t", user_env="U", token_env="T")


def test_instance_owns_only_its_own_urls():
    i = _inst("https://h.example.com/jenkins/")
    assert i.url == "https://h.example.com/jenkins"
    assert i.owns("https://h.example.com/jenkins/job/a")
    assert i.owns("HTTPS://H.EXAMPLE.COM/jenkins")
    assert not i.owns("https://h.example.com/jenkinsfoo/job/a"), "prefix must end at a path boundary"
    assert not i.owns("http://h.example.com/jenkins/job/a"), "scheme must match"
    assert not i.owns("https://other.example.com/jenkins/job/a")


def test_abs_url_never_sends_credentials_elsewhere():
    i = _inst("https://h.example.com")
    assert i.abs_url("/api/json") == "https://h.example.com/api/json"
    assert i.abs_url("api/json") == "https://h.example.com/api/json"
    with pytest.raises(ToolError, match="Credentials are only sent"):
        i.abs_url("https://evil.example.com/api/json")


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_session_blocks_every_write_method(method):
    with pytest.raises(ToolError, match="read-only"):
        _inst("https://h.example.com").session.request(method, "https://h.example.com/job/x/build")


def test_http_error_messages_are_actionable():
    i = _inst("https://h.example.com")
    assert "U / T in config/.env" in i.http_error(401, "u") and "API token" in i.http_error(401, "u")
    assert "Overall/Read" in i.http_error(403, "u")
    assert "%2F" in i.http_error(404, "u")
    assert i.http_error(500, "u") == "[x] HTTP 500 from u."
