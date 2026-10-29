"""Unit tests: configuration loading/validation (config.py), home discovery (settings.py) and the
network error messages of the HTTP client (client.py)."""

import os
from pathlib import Path

import pytest
import requests
import responses
from fastmcp.exceptions import ToolError

from jenkins_mcp import config, settings
from jenkins_mcp.client import Instance


def load(monkeypatch, tmp_path: Path, yaml_text: str | None) -> config.Settings:
    cfg = tmp_path / "config" / "jenkins_instances.yaml"
    cfg.parent.mkdir(exist_ok=True)
    if yaml_text is not None:
        cfg.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    return config.load_settings()


def test_valid_config_with_defaults_and_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_USER", "u1")
    monkeypatch.setenv("MY_TOKEN", "t1")
    s = load(monkeypatch, tmp_path, """
default: a
defaults: {timeout: 15, verify_tls: false}
instances:
  a: {url: 'https://a.example.com/', exclude_folders: [archive], user_env: MY_USER, token_env: MY_TOKEN}
  b-2: {url: 'https://b.example.com', ca_bundle: certs/ca.pem, timeout: 5}
knowledge: {dir: ../kb}
node_health: {disk_warn_gb: 3}
""")
    assert not s.errors
    a, b = s.instances["a"], s.instances["b-2"]
    assert (a.url, a.user, a.token, a.timeout, a.verify) == ("https://a.example.com", "u1", "t1", 15.0, False)
    assert a.exclude_folders == ("archive",)
    assert b.token_env == "JENKINS_B_2_TOKEN", "non-alphanumeric characters become '_'"
    assert b.verify == str(tmp_path / "config" / "certs" / "ca.pem"), "ca_bundle relative to the config file"
    assert b.timeout == 5.0
    assert s.kb_dir == (tmp_path / "kb").resolve() and s.disk_warn_gb == 3.0


@pytest.mark.parametrize("yaml_text, error", [
    (None, "No configuration found"),
    ("instances: [unclosed", "Cannot read jenkins_instances.yaml"),
    ("- just a list", "top level must be a mapping"),
    ("instances: {a: {description: no url}}", "instances.a: no url"),
    ("default: z\ninstances: {a: {url: 'https://a.example.com'}}", "default instance 'z' is not defined"),
    ("instances: {}\nlog_analysis: {extra_error_patterns: {not: a list}}", "must be a list of rules"),
    ("instances: {}\nlog_analysis: {ignore_patterns: ['(']}", "log_analysis.ignore_patterns[0]: invalid regex"),
])
def test_config_errors_are_reported_not_raised(monkeypatch, tmp_path, yaml_text, error):
    s = load(monkeypatch, tmp_path, yaml_text)
    assert any(error in e for e in s.errors), s.errors
    assert s.rules, "built-in rules still loaded"


def test_get_instance_without_default(monkeypatch, tmp_path):
    s = load(monkeypatch, tmp_path, "instances: {a: {url: 'https://a.example.com'}, b: {url: 'https://b.example.com'}}")
    monkeypatch.setattr(config, "_current", s)
    with pytest.raises(ToolError, match="pass instance="):
        config.get_instance("")
    monkeypatch.setattr(config, "_current", load(monkeypatch, tmp_path, "instances: {only: {url: 'https://o.example.com'}}"))
    assert config.get_instance("").name == "only", "a single instance needs no default"


# ----------------------------------------------------------------------------- home discovery
def test_home_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("JENKINS_MCP_HOME", str(tmp_path))
    assert settings._find_home() == tmp_path.resolve()


def test_home_found_from_a_subfolder(monkeypatch, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "jenkins_instances.example.yaml").write_text("")
    (tmp_path / "src" / "deep").mkdir(parents=True)
    monkeypatch.delenv("JENKINS_MCP_HOME")
    monkeypatch.chdir(tmp_path / "src" / "deep")
    assert settings._find_home() == tmp_path.resolve()


def test_unrelated_config_folder_is_ignored(monkeypatch, tmp_path):
    (tmp_path / "config").mkdir()                    # e.g. ~/config of another tool
    (tmp_path / "work").mkdir()
    monkeypatch.delenv("JENKINS_MCP_HOME")
    monkeypatch.chdir(tmp_path / "work")
    assert settings._find_home() == (tmp_path / "work").resolve()


# ----------------------------------------------------------------------------- network errors
URL = "https://net.example.com"


def _inst() -> Instance:
    return Instance(name="net", url=URL, user="u", token="t", user_env="U", token_env="T", timeout=3)


@pytest.mark.parametrize("exc, message", [
    (requests.exceptions.SSLError("cert"), "TLS verification failed"),
    (requests.exceptions.ConnectTimeout("slow"), "Timeout after 3"),
    (requests.exceptions.ConnectionError("dns"), "Cannot connect"),
])
def test_network_errors_become_clear_tool_errors(exc, message):
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, f"{URL}/api/json", body=exc)
        with pytest.raises(ToolError, match=message):
            _inst().get("/api/json")


def test_html_instead_of_json():
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, f"{URL}/api/json", body="<html>login</html>", content_type="text/html")
        with pytest.raises(ToolError, match="Expected JSON"):
            _inst().json("/api/json")


def test_404_allowed_returns_none():
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, f"{URL}/x/api/json", status=404)
        assert _inst().json("/x/api/json", allow_404=True) is None



# ----------------------------------------------------------------------------- token sources
def test_token_source_dotenv_and_missing(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("JENKINS_FILE_TOKEN=abc\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env)
    monkeypatch.setattr(config, "PROCESS_ENV", frozenset())
    monkeypatch.setenv("JENKINS_FILE_TOKEN", "abc")
    monkeypatch.delenv("JENKINS_NONE_TOKEN", raising=False)
    assert config.token_source("JENKINS_FILE_TOKEN") == "from config/.env"
    assert config.token_source("JENKINS_NONE_TOKEN") == "missing"


def test_reload_keeps_tokens_from_the_process_environment(monkeypatch, tmp_path):
    """VS Code `inputs` arrive as process environment variables; config/.env must not overwrite them."""
    env = tmp_path / ".env"
    env.write_text("JENKINS_VS_TOKEN=from-dotenv\nJENKINS_NEW_TOKEN=new-value\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env)
    monkeypatch.setenv("JENKINS_VS_TOKEN", "from-vscode")
    monkeypatch.setattr(config, "PROCESS_ENV", frozenset({"JENKINS_VS_TOKEN"}))
    monkeypatch.setattr(config, "_current", None)
    try:
        config.reload()
        assert os.environ["JENKINS_VS_TOKEN"] == "from-vscode", "process environment wins"
        assert os.environ["JENKINS_NEW_TOKEN"] == "new-value", "values only in .env are still loaded"
    finally:
        os.environ.pop("JENKINS_NEW_TOKEN", None)
        monkeypatch.setattr(config, "_current", None)



# ----------------------------------------------------------------------------- per-instance proxy
def test_proxy_per_instance(monkeypatch, tmp_path):
    s = load(monkeypatch, tmp_path, """
defaults: {proxy: 'http://proxy.example.com:80'}
instances:
  outside: {url: 'https://a.example.com'}
  inside: {url: 'https://b.example.com', proxy: none}
""")
    out, inside = s.instances["outside"], s.instances["inside"]
    assert out.session.proxies == {"http": "http://proxy.example.com:80", "https": "http://proxy.example.com:80"}
    assert out.proxy == "http://proxy.example.com:80"
    assert inside.session.trust_env is False and inside.session.proxies == {}, "direct: HTTPS_PROXY is ignored"
    assert inside.proxy == "none"
