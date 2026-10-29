"""Writes .vscode/mcp.json for VS Code Copilot with one password input per Jenkins instance: VS Code asks
for each API token once and keeps it in its secure storage, so config/.env needs no tokens.

    python -m jenkins_mcp.vscode_config [--no-inputs] [--home DIR] [--output FILE] [--platform windows|posix]

Called by setup/windows/setup.ps1. The file contains instance names but no URLs or tokens; it is
git-ignored because it is specific to your configuration.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from .config import _env_name
from .settings import HOME

COMMANDS = {"windows": "${workspaceFolder}/.venv/Scripts/jenkins-mcp.exe",
            "posix": "${workspaceFolder}/.venv/bin/jenkins-mcp"}


def _instances(home: Path) -> list[tuple[str, str]]:
    """(instance name, token variable) from config/jenkins_instances.yaml (or the example)."""
    cfg_dir = home / "config"
    path = next((p for p in (cfg_dir / "jenkins_instances.yaml", cfg_dir / "jenkins_instances.example.yaml")
                 if p.is_file()), None)
    if path is None:
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    shared = raw.get("defaults") or {}
    out = []
    for name, cfg in (raw.get("instances") or {}).items():
        cfg = {**shared, **(cfg or {})}
        out.append((str(name), str(cfg.get("token_env") or f"JENKINS_{_env_name(str(name))}_TOKEN")))
    return out


def build(instances: list[tuple[str, str]], platform: str, inputs: bool = True) -> dict[str, Any]:
    env = {"JENKINS_MCP_HOME": "${workspaceFolder}"}
    entries = []
    for name, token_env in instances if inputs else []:
        input_id = f"jenkins-{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}-token"
        entries.append({"type": "promptString", "id": input_id, "password": True,
                        "description": f"Jenkins API token for instance '{name}' (stored securely by VS Code)"})
        env[token_env] = f"${{input:{input_id}}}"
    cfg: dict[str, Any] = {"inputs": entries} if entries else {}
    cfg["servers"] = {"jenkins": {"type": "stdio", "command": COMMANDS[platform], "env": env}}
    return cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", default=str(HOME), help="project folder with config/ (default: auto-detected)")
    ap.add_argument("--output", default="", help="default: <home>/.vscode/mcp.json")
    ap.add_argument("--platform", choices=sorted(COMMANDS), default="windows" if os.name == "nt" else "posix")
    ap.add_argument("--no-inputs", action="store_true", help="tokens stay in config/.env (no VS Code prompts)")
    args = ap.parse_args(argv)
    home = Path(args.home)
    out = Path(args.output) if args.output else home / ".vscode" / "mcp.json"
    instances = _instances(home)
    text = json.dumps(build(instances, args.platform, not args.no_inputs), indent=2) + "\n"
    if out.exists() and out.read_text(encoding="utf-8") != text:
        out.with_name(out.name + ".bak").write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    mode = "tokens in config/.env" if args.no_inputs else f"{len(instances)} token prompt(s)"
    print(f"wrote {out} ({mode})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
