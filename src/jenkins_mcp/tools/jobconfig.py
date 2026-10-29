"""Job configuration: get_job_config (config.xml as a summary), read_managed_file (Config File Provider).

Only credential IDs are shown, never values; Jenkins-encrypted secrets ({AQAA...}) and secret-looking
XML elements are masked even in the raw view.
"""

import html
import re
import xml.etree.ElementTree as ET
from typing import Annotated
from urllib.parse import quote

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..app import READ_ONLY, InstanceName, Job, mcp
from ..config import get_instance
from ..settings import UNTRUSTED, JsonDict
from ..targets import Target, resolve
from ..text import budget, clip, is_secret_name, mask, short_class
from ..xmlutil import parse_xml, text_of

_TYPES = {
    "project": "Freestyle", "matrix-project": "Matrix", "maven2-moduleset": "Maven",
    "org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject": "Multibranch Pipeline",
    "com.cloudbees.hudson.plugins.folder.Folder": "Folder",
    "jenkins.branch.OrganizationFolder": "Organization folder",
}
_MANAGED_CONTENT = re.compile(r'<textarea[^>]*\bname="config\.content"[^>]*>(.*?)</textarea>|<pre[^>]*>(.*?)</pre>', re.S)


def fetch_config_xml(t: Target) -> str:
    """config.xml of a job or folder; 403 explained (needs Job/ExtendedRead or Job/Configure)."""
    try:
        r = t.inst.get(f"{t.path}/config.xml", allow_404=True)
    except ToolError as e:
        if "(403)" in str(e):
            raise ToolError(f"[{t.inst.name}] config.xml of '{t.full_name}' is not readable (403): the token's "
                            "user needs Job/ExtendedRead (Extended Read Permission plugin) or Job/Configure.") from None
        raise
    if r is None:
        raise ToolError(t.inst.http_error(404, f"{t.inst.url}{t.path}/"))
    return r.text


def _job_type(root: ET.Element) -> str:
    if root.tag == "flow-definition":
        definition = root.find("definition")
        cls = definition.get("class", "") if definition is not None else ""
        if "CpsScmFlowDefinition" in cls:
            return "Pipeline (Jenkinsfile from SCM)"
        return "Pipeline (inline script)" if "CpsFlowDefinition" in cls else "Pipeline"
    return _TYPES.get(root.tag, root.tag)


def _scm(el: ET.Element) -> str:
    cls = el.get("class", "")
    creds = sorted({text_of(c, ".") for c in el.iter("credentialsId") if text_of(c, ".")})
    tail = f" (credentials: {', '.join(creds)})" if creds else ""
    if "GitSCM" in cls and "Source" not in cls:
        urls = [text_of(u, ".") for u in el.iter("url")]
        branches = [text_of(b, "name") for b in el.iter("hudson.plugins.git.BranchSpec")]
        return f"git {', '.join(urls) or '?'} branches {', '.join(branches) or '?'}{tail}"
    if "GitSCMSource" in cls:
        return f"git {text_of(el, 'remote') or '?'}{tail}"
    if "BitbucketSCMSource" in cls:
        return (f"bitbucket {text_of(el, 'serverUrl')}/{text_of(el, 'repoOwner')}/{text_of(el, 'repository')}"
                f"{tail}")
    return short_class(cls) + tail


def _parameters(root: ET.Element) -> list[str]:
    rows = []
    for pd in root.iter():
        if not pd.tag.endswith("ParameterDefinition") or pd.find("name") is None:
            continue
        name = text_of(pd, "name")
        kind = short_class(pd.tag).replace("ParameterDefinition", "")
        row = f"{name} ({kind})"
        choices = [text_of(c, ".") for c in pd.iter("string")] if pd.find("choices") is not None else []
        if not choices and text_of(pd, "choices"):
            choices = text_of(pd, "choices").splitlines()
        default = text_of(pd, "defaultValue")
        if choices:
            row += f" choices={clip(', '.join(choices), 150)}"
        elif default:
            secret = kind == "Password" or is_secret_name(name) or default.startswith("{AQAA")
            row += f" default={'****' if secret else clip(mask(default), 80)}"
        rows.append(row)
    return rows


def _triggers(root: ET.Element) -> list[str]:
    rows = []
    for group in root.iter("triggers"):
        for trig in group:
            name = short_class(trig.tag)
            if name == "TimerTrigger":
                rows.append(f"cron: {text_of(trig, 'spec')}")
            elif name == "SCMTrigger":
                rows.append(f"poll SCM: {text_of(trig, 'spec')}")
            elif name == "ReverseBuildTrigger":
                rows.append(f"after upstream: {text_of(trig, 'upstreamProjects')}")
            else:
                rows.append(name)
    return rows


def _keep_builds(root: ET.Element) -> str:
    rotator = next((s for s in root.iter("strategy") if "LogRotator" in s.get("class", "")), None)
    if rotator is None:
        return ""
    parts = []
    if (n := text_of(rotator, "numToKeep")) and n != "-1":
        parts.append(f"{n} builds")
    if (d := text_of(rotator, "daysToKeep")) and d != "-1":
        parts.append(f"{d} days")
    return ", ".join(parts)


def _steps(container: ET.Element | None) -> list[str]:
    rows = []
    for step in container if container is not None else []:
        name = short_class(step.tag)
        detail = text_of(step, "command") or text_of(step, "targets")
        rows.append(f"{name}: {clip(mask(' '.join(detail.split())), 150)}" if detail else name)
    return rows


def _managed_files(root: ET.Element) -> list[str]:
    rows = []
    for mf in root.iter("org.jenkinsci.plugins.configfiles.buildwrapper.ManagedFile"):
        row = text_of(mf, "fileId")
        if target := text_of(mf, "targetLocation"):
            row += f" -> {target}"
        if var := text_of(mf, "variable"):
            row += f" (variable {var})"
        rows.append(row)
    for el in root.iter():
        if (fid := text_of(el, "settingsConfigId")) and fid != "":
            kind = "Maven global settings" if "Global" in el.get("class", "") else "Maven settings"
            rows.append(f"{fid} ({kind})")
    return rows


def _folder_libraries(root: ET.Element) -> list[str]:
    rows = []
    for lib in root.iter("org.jenkinsci.plugins.workflow.libs.LibraryConfiguration"):
        row = f"{text_of(lib, 'name')} default={text_of(lib, 'defaultVersion') or '-'}"
        if text_of(lib, "implicit") == "true":
            row += " implicit"
        rows.append(row)
    return rows


def summarize_config(root: ET.Element) -> JsonDict:
    out: JsonDict = {"type": _job_type(root)}
    if desc := text_of(root, "description"):
        out["description"] = clip(mask(desc), 300)
    if text_of(root, "disabled") == "true":
        out["disabled"] = True
    if label := text_of(root, "assignedNode"):
        out["label"] = label
    scm = [_scm(el) for el in root.iter("scm") if "NullSCM" not in el.get("class", "")]
    scm += [_scm(el) for el in root.iter("source") if el.get("class")]
    if scm:
        out["scm"] = scm
    definition = root.find("definition")
    script_path = text_of(definition, "scriptPath") or text_of(root.find("factory"), "scriptPath")
    if script_path:
        out["script_path"] = script_path
    if definition is not None and text_of(definition, "lightweight"):
        out["lightweight_checkout"] = text_of(definition, "lightweight") == "true"
    if definition is not None and (script := text_of(definition, "script")):
        out["script"] = budget(mask(script), 3000)
    if params := _parameters(root):
        out["parameters"] = params
    if triggers := _triggers(root):
        out["triggers"] = triggers
    if keep := _keep_builds(root):
        out["keep_builds"] = keep
    if root.find(".//org.jenkinsci.plugins.workflow.job.properties.DisableConcurrentBuildsJobProperty") is not None:
        out["concurrent_builds"] = False
    elif text_of(root, "concurrentBuild"):
        out["concurrent_builds"] = text_of(root, "concurrentBuild") == "true"
    if builders := _steps(root.find("builders")):
        out["builders"] = builders
    pub = root.find("publishers")
    if publishers := [short_class(p.tag) for p in (pub if pub is not None else [])]:
        out["publishers"] = publishers
    if files := _managed_files(root):
        out["managed_files"] = files
    if libs := _folder_libraries(root):
        out["libraries"] = libs
    creds = sorted({text_of(c, ".") for c in root.iter("credentialsId") if text_of(c, ".")})
    if creds:
        out["credential_ids"] = creds
    return out


@mcp.tool(annotations=READ_ONLY)
def get_job_config(job: Job, instance: InstanceName = "",
                   raw: Annotated[bool, Field(description="Return the whole config.xml (masked, size-limited) "
                                                          "instead of the summary")] = False) -> JsonDict:
    """A job's (or folder's) configuration from config.xml as a compact summary: type, SCM repo/branches,
    Jenkinsfile path or inline script, parameters, triggers (cron, SCM polling, upstream), build
    retention, concurrency, label, freestyle build steps, Config File Provider files (managed_files),
    folder Shared Libraries and the credential IDs used (IDs only, never values). Needs Job/ExtendedRead.
    Jobs managed by JCasC/JobDSL: this is the live state; the source of truth is in Git."""
    t = resolve(job, "", instance)
    xml_text = fetch_config_xml(t)
    out: JsonDict = {"instance": t.inst.name, "job": t.full_name, "url": f"{t.inst.url}{t.path}/"}
    if raw:
        out["config_xml"] = budget(mask(xml_text))
        out["note"] = UNTRUSTED
        return out
    out.update(summarize_config(parse_xml(xml_text)))
    hints = []
    if out.get("managed_files"):
        first = str(out["managed_files"][0]).split(" ")[0]
        hints.append(f"read_managed_file(id='{first}') for the content of a managed file")
    hints.append("get_job_config_history for who changed the configuration and when")
    out["next_steps"] = hints
    return out


@mcp.tool(annotations=READ_ONLY)
def read_managed_file(id: Annotated[str, Field(description="Managed file id (fileId / settingsConfigId) as shown by "
                                                           "get_job_config")],
                      folder: Annotated[str, Field(description="Folder that defines the file; empty = global "
                                                               "(Manage Jenkins > Managed files)")] = "",
                      instance: InstanceName = "") -> str:
    """Content of a Config File Provider managed file (settings.xml, .npmrc, properties...) with secrets
    masked. Global files by default; pass folder= for files defined in a folder."""
    if folder.strip():
        t = resolve(folder, "", instance)
        inst, bases, where = t.inst, [t.path], f"folder '{t.full_name}'"
    else:
        inst = get_instance(instance)
        bases, where = ["/manage", ""], "the global managed files"
    for base in bases:
        url = f"{base}/configfiles/show?id={quote(id.strip(), safe='')}"
        try:
            r = inst.get(url, allow_404=True)
        except ToolError as e:
            if "(403)" in str(e):
                raise ToolError(f"[{inst.name}] Managed file '{id}' is not readable (403): the token's user lacks "
                                "the permission to view managed files.") from None
            if "HTTP 500" in str(e):  # config-file-provider answers an unknown id with a NullPointerException
                continue
            raise
        if r is None:
            continue
        m = _MANAGED_CONTENT.search(r.text)
        if not m:
            raise ToolError(f"[{inst.name}] Found {inst.url}{url} but could not recognise the file content on the "
                            "page (unknown Config File Provider version). Open it in the browser.")
        content = html.unescape(m.group(1) if m.group(1) is not None else m.group(2))
        lines = [mask(x) for x in content.splitlines()]
        return budget("\n".join([f"Managed file '{id}' from {where} ({len(lines)} lines), instance {inst.name}",
                                 UNTRUSTED] + lines))
    raise ToolError(f"[{inst.name}] Managed file '{id}' not found in {where}. Use get_job_config (managed_files) "
                    "for the ids, and pass folder= for files defined in a folder.")
