"""VS Code mcp.json generator (one password input per instance -> tokens in VS Code's secure storage)
and the environment rules around it: an input left empty must not hide config/.env."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from jenkins_mcp import vscode_config

YAML = """
default: alpha
instances:
  alpha: {url: 'https://alpha.example.com/'}
  gamma-2: {url: 'https://gamma.example.com/', token_env: MY_GAMMA_TOKEN}
"""


def _home(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "jenkins_instances.yaml").write_text(YAML, encoding="utf-8")
    return tmp_path


def test_generated_config_has_one_password_input_per_instance(tmp_path):
    out = tmp_path / ".vscode" / "mcp.json"
    assert vscode_config.main(["--home", str(_home(tmp_path)), "--output", str(out), "--platform", "windows"]) == 0
    cfg = json.loads(out.read_text(encoding="utf-8"))
    assert [i["id"] for i in cfg["inputs"]] == ["jenkins-alpha-token", "jenkins-gamma-2-token"]
    assert all(i["type"] == "promptString" and i["password"] is True for i in cfg["inputs"])
    assert "alpha.example.com" not in out.read_text(encoding="utf-8"), "no company URLs in the file"
    server = cfg["servers"]["jenkins"]
    assert server["command"] == "${workspaceFolder}/.venv/Scripts/jenkins-mcp.exe"
    assert server["env"] == {"JENKINS_MCP_HOME": "${workspaceFolder}",
                             "JENKINS_ALPHA_TOKEN": "${input:jenkins-alpha-token}",
                             "MY_GAMMA_TOKEN": "${input:jenkins-gamma-2-token}"}, "token_env of the yaml is respected"


def test_without_inputs_and_backup(tmp_path):
    out = tmp_path / "mcp.json"
    out.write_text('{"servers": {"mine": {}}}', encoding="utf-8")
    vscode_config.main(["--home", str(_home(tmp_path)), "--output", str(out), "--platform", "posix", "--no-inputs"])
    cfg = json.loads(out.read_text(encoding="utf-8"))
    assert "inputs" not in cfg and cfg["servers"]["jenkins"]["command"] == "${workspaceFolder}/.venv/bin/jenkins-mcp"
    assert json.loads((tmp_path / "mcp.json.bak").read_text(encoding="utf-8")) == {"servers": {"mine": {}}}, \
        "an existing different file is kept as .bak"


def test_empty_vscode_input_does_not_hide_dotenv(tmp_path):
    home = _home(tmp_path)
    (home / "config" / ".env").write_text("JENKINS_ALPHA_TOKEN=from-dotenv\n", encoding="utf-8")
    code = textwrap.dedent("""
        import os
        from jenkins_mcp import config
        print(os.environ.get("JENKINS_ALPHA_TOKEN"), config.token_source("JENKINS_ALPHA_TOKEN"))
    """)
    env = {**os.environ, "JENKINS_MCP_HOME": str(home), "JENKINS_ALPHA_TOKEN": ""}
    res = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert res.stdout.split() == ["from-dotenv", "from", "config/.env"]
