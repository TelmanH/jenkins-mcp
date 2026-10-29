"""find_jobs_by_scm: jobs whose last build checked out a repository, across instances, with URL
normalization (https / scp-like ssh / ssh with port / Bitbucket /scm/ prefix / credentials / .git)."""

import json

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def call(c, tool: str, **args) -> tuple[str, bool]:
    res = await c.call_tool(tool, args, raise_on_error=False)
    return "\n".join(getattr(b, "text", "") for b in res.content), res.is_error


async def test_find_by_full_url(client):
    t, err = await call(client, "find_jobs_by_scm", url="https://git.example.com/app.git")
    assert not err, t
    d = json.loads(t)["instances"]
    assert d["alpha"]["jobs"] == ["cd/master/app-deploy | FAILURE #42 | https://git.example.com/app.git"]
    assert d["beta"]["jobs"] == []


@pytest.mark.parametrize("url, instance, job", [
    ("https://git.example.com/team/app-build", "alpha", "cd/master/app-build"),       # scp-like ssh in Jenkins
    ("https://git.example.com/scm/lib/shared-lib.git", "alpha", "cd/master/app-deploy"),  # library checkout
    ("ssh://git@git.example.com:7999/core/core.git", "beta", "core-build"),          # credentials + /scm/ in Jenkins
    ("core/core", "beta", "core-build"),                                               # a part of the path
])
async def test_url_spellings(client, url, instance, job):
    t, err = await call(client, "find_jobs_by_scm", url=url)
    assert not err, t
    assert any(r.startswith(f"{job} |") for r in json.loads(t)["instances"][instance]["jobs"]), t


async def test_no_match_and_credentials_hidden(client):
    t, err = await call(client, "find_jobs_by_scm", url="nothing/here")
    assert not err and "never built" in json.loads(t)["note"]
    t, _ = await call(client, "find_jobs_by_scm", url="core/core")
    assert "pw@" not in t, "credentials in repo URLs are masked"
