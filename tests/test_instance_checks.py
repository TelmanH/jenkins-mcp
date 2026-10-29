"""list_instances(check_connection=True): who the token belongs to, admin rights, quiet-down mode and
where each token comes from (process environment, e.g. VS Code `inputs`, or config/.env)."""

import json

import pytest


pytestmark = pytest.mark.asyncio(loop_scope="module")


async def call(c, name: str, **args) -> tuple[str, bool]:
    res = await c.call_tool(name, args, raise_on_error=False)
    return "\n".join(getattr(b, "text", "") for b in res.content), res.is_error


async def test_connection_check_shows_user_rights_and_quiet_down(client):
    t, err = await call(client, "list_instances", check_connection=True)
    assert not err, t
    by_name = {i["name"]: i for i in json.loads(t)["instances"]}
    alpha, beta = by_name["alpha"], by_name["beta"]

    assert alpha["user"].startswith("tester") and "jenkins-devs" in alpha["user"], "whoAmI user + groups"
    assert "admin_rights" not in alpha, "normal user: /manage/ is 403"
    assert "quieting_down" not in alpha

    assert "admin_rights" in beta and "GET/HEAD only" in beta["admin_rights"], "admin token reported as info"
    assert "whoAmI" in beta["user"], "old Jenkins without whoAmI: says it is unknown"
    assert beta["quieting_down"].startswith("yes"), "quiet-down mode shown"


async def test_token_source_shown_without_the_token(client):
    t, _ = await call(client, "list_instances")
    li = json.loads(t)
    assert all(i["token"] == "from the environment" for i in li["instances"]), li["instances"]
    assert "t0ken" not in t, "the token itself is never shown"
