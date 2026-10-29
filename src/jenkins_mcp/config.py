"""Configuration: config/jenkins_instances.yaml + config/.env -> Settings (instances, log rules, knowledge base).

Errors in the config never crash the server: each problem is collected in Settings.errors
(reported by list_instances) and only the broken entry is skipped.
"""

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import urllib3
import yaml
from dotenv import dotenv_values
from fastmcp.exceptions import ToolError

from .client import Instance
from .settings import CONFIG_PATH, DEFAULT_KB_DIR, ENV_FILE, PROCESS_ENV, JsonDict

RULES_FILE = Path(__file__).parent / "rules" / "default_rules.yaml"
SEVERITIES = ("high", "medium", "low", "warn")


@dataclass
class Rule:
    category: str
    severity: str
    rx: re.Pattern[str]


@dataclass
class Settings:
    instances: dict[str, Instance]
    default: str | None
    errors: list[str]
    rules: list[Rule]
    ignore: list[re.Pattern[str]]
    noise: list[re.Pattern[str]]
    disk_warn_gb: float
    source: str
    kb_dir: Path

    @property
    def error(self) -> str | None:
        return "; ".join(self.errors) if self.errors else None


# ----------------------------------------------------------------------------- helpers
def _env_name(name: str) -> str:
    return re.sub(r"\W", "_", name).upper()


def token_source(var: str) -> str:
    """Where a credential variable comes from - never its value."""
    if var in PROCESS_ENV and os.getenv(var):
        return "from the environment"  # MCP client config (VS Code inputs, claude mcp add -e) or shell
    if dotenv_values(ENV_FILE).get(var):
        return "from config/.env"
    return "from the environment" if os.getenv(var) else "missing"


def _verify_setting(cfg: JsonDict, base: Path) -> bool | str:
    if cfg.get("ca_bundle"):
        p = Path(os.path.expandvars(str(cfg["ca_bundle"]))).expanduser()
        return str(p if p.is_absolute() else base / p)
    if cfg.get("verify_tls") is False:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return False
    return True


def compile_rules(entries: Any, where: str, errors: list[str]) -> list[Rule]:
    """Compile rule dicts; a broken entry is reported with its position and skipped."""
    rules: list[Rule] = []
    if entries in (None, ""):
        return rules
    if not isinstance(entries, list):
        errors.append(f"{where} must be a list of rules")
        return rules
    for i, r in enumerate(entries):
        pos = f"{where}[{i}]"
        if not isinstance(r, dict):
            errors.append(f"{pos}: expected 'category/severity/regex', got {r!r}")
            continue
        rx = r.get("regex", r.get("pattern"))  # 'pattern' accepted as an alias
        if not rx:
            errors.append(f"{pos}: missing 'regex' (keys found: {', '.join(map(str, r)) or 'none'})")
            continue
        severity = str(r.get("severity", "high")).lower()
        if severity not in SEVERITIES:
            errors.append(f"{pos}: severity '{severity}' unknown, use one of {', '.join(SEVERITIES)}")
            continue
        try:
            rules.append(Rule(str(r.get("category", "custom")), severity, re.compile(str(rx))))
        except re.error as e:
            errors.append(f"{pos}: invalid regex {str(rx)!r}: {e}")
    return rules


def compile_patterns(patterns: Any, where: str, errors: list[str]) -> list[re.Pattern[str]]:
    out: list[re.Pattern[str]] = []
    if patterns in (None, ""):
        return out
    if not isinstance(patterns, list):
        errors.append(f"{where} must be a list of regex strings")
        return out
    for i, p in enumerate(patterns):
        try:
            out.append(re.compile(str(p)))
        except re.error as e:
            errors.append(f"{where}[{i}]: invalid regex {str(p)!r}: {e}")
    return out


def _defaults(errors: list[str]) -> JsonDict:
    try:
        return yaml.safe_load(RULES_FILE.read_text(encoding="utf-8")) or {}
    except Exception as e:  # packaging problem - report, keep running without built-in rules
        errors.append(f"cannot read built-in rules {RULES_FILE}: {e}")
        return {}


# ----------------------------------------------------------------------------- loading
def load_settings() -> Settings:
    instances: dict[str, Instance] = {}
    default: str | None = None
    errors: list[str] = []
    raw: JsonDict = {}
    source = ""

    if CONFIG_PATH.exists():
        source = str(CONFIG_PATH)
        try:
            raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise ValueError("top level must be a mapping")
        except Exception as e:
            errors.append(f"Cannot read {CONFIG_PATH.name}: {e}")
            raw = {}
        shared = raw.get("defaults") or {}
        for name, cfg in (raw.get("instances") or {}).items():
            cfg = {**shared, **(cfg or {})}
            if not cfg.get("url"):
                errors.append(f"instances.{name}: no url")
                continue
            user_env = cfg.get("user_env") or f"JENKINS_{_env_name(str(name))}_USER"
            token_env = cfg.get("token_env") or f"JENKINS_{_env_name(str(name))}_TOKEN"
            instances[str(name)] = Instance(
                name=str(name), url=str(cfg["url"]),
                user=os.getenv(user_env, ""), token=os.getenv(token_env, ""),
                user_env=user_env, token_env=token_env,
                verify=_verify_setting(cfg, CONFIG_PATH.parent),
                timeout=float(cfg.get("timeout", 60)),
                description=str(cfg.get("description", "")),
                exclude_folders=tuple(cfg.get("exclude_folders") or ()),
                proxy=str(cfg.get("proxy") or ""),
            )
        default = raw.get("default")
        if default and default not in instances:
            errors.append(f"default instance '{default}' is not defined")
            default = None
    else:
        errors.append(f"No configuration found at {CONFIG_PATH}. Copy config/jenkins_instances.example.yaml "
                      "to config/jenkins_instances.yaml (or set JENKINS_MCP_HOME / JENKINS_MCP_CONFIG).")

    la = raw.get("log_analysis") or {}
    nh = raw.get("node_health") or {}
    kb = raw.get("knowledge") or {}
    builtin = _defaults(errors)

    # user rules first: the first matching rule wins
    rules = compile_rules(la.get("extra_error_patterns"), "log_analysis.extra_error_patterns", errors)
    rules += compile_rules(builtin.get("rules"), "built-in rules", errors)
    ignore = compile_patterns(builtin.get("ignore"), "built-in ignore", errors)
    ignore += compile_patterns(la.get("ignore_patterns"), "log_analysis.ignore_patterns", errors)
    noise = compile_patterns(builtin.get("noise"), "built-in noise", errors)
    noise += compile_patterns(la.get("noise_patterns"), "log_analysis.noise_patterns", errors)

    kb_dir = Path(os.getenv("JENKINS_MCP_KB_DIR") or kb.get("dir") or DEFAULT_KB_DIR).expanduser()
    if not kb_dir.is_absolute():
        kb_dir = (CONFIG_PATH.parent / kb_dir).resolve()

    return Settings(instances, default, errors, rules, ignore, noise,
                    float(nh.get("disk_warn_gb", 10)), source, kb_dir)


_current: Settings | None = None
_lock = threading.Lock()  # tools run in worker threads


def current() -> Settings:
    global _current
    if _current is None:
        with _lock:
            if _current is None:
                _current = load_settings()
    return _current


def reload() -> Settings:
    global _current
    with _lock:
        # edited .env values apply, but never over variables the MCP client set (e.g. VS Code inputs)
        for key, value in dotenv_values(ENV_FILE).items():
            if key not in PROCESS_ENV and value is not None:
                os.environ[key] = value
        _current = load_settings()
    return _current


# ----------------------------------------------------------------------------- instance lookup
def get_instance(name: str = "") -> Instance:
    s = current()
    if not s.instances:
        raise ToolError(s.error or "No Jenkins instances configured.")
    names = list(s.instances)
    if not name or not name.strip():
        if s.default:
            return s.instances[s.default]
        if len(names) == 1:
            return s.instances[names[0]]
        raise ToolError(f"Several Jenkins instances are configured, pass instance= one of: {', '.join(names)}")
    key = name.strip().lower()
    exact = [n for n in names if n.lower() == key]
    partial = [n for n in names if key in n.lower()]
    match = exact or partial
    if len(match) == 1:
        return s.instances[match[0]]
    raise ToolError(f"Unknown or ambiguous instance '{name}'. Available: {', '.join(names)}")


def instance_for_url(url: str) -> Instance:
    instances = current().instances.values()
    owners = [i for i in instances if i.owns(url)]
    if not owners:
        known = ", ".join(f"{i.name}={i.url}" for i in instances)
        raise ToolError(f"URL {url} does not belong to any configured instance ({known}).")
    return max(owners, key=lambda i: len(i.url))
