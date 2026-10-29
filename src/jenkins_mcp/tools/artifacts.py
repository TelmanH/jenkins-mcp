"""Artifacts: list_artifacts, read_artifact, get_env_log, compare_artifacts (+ helpers used by compare_builds
and the workspace tools)."""

import difflib
import hashlib
from typing import Annotated, Any
from urllib.parse import quote

import requests
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..app import READ_ONLY, Build, InstanceName, Job, mcp
from ..logs.parse import clean_line
from ..settings import UNTRUSTED, JsonDict
from ..targets import Target, build_info, resolve
from ..text import budget, clip, decode_text, fmt_size, looks_binary, mask, mask_env_text, safe_regex


def artifacts_of(t: Target, number: Any) -> list[JsonDict]:
    d = t.inst.json(f"{t.path}/{number}/api/json", tree="artifacts[fileName,relativePath]") or {}
    return [a for a in d.get("artifacts") or [] if isinstance(a, dict)]


def env_log_of(t: Target, number: Any) -> list[str] | None:
    for a in artifacts_of(t, number):
        if a.get("fileName") == "env.log":
            r = t.inst.get(f"{t.path}/{number}/artifact/{quote(str(a.get('relativePath')), safe='/')}")
            return mask_env_text(r.text) if r is not None else None
    return None


_ARTIFACT_CAP = 20 * 1024 * 1024


def stream_bytes(r: requests.Response, cap: int) -> tuple[bytes, bool]:
    """Body of a streamed response, at most `cap` bytes; (data, cut_off)."""
    data, truncated = bytearray(), False
    try:
        for chunk in r.iter_content(1 << 16):
            data += chunk
            if len(data) > cap:
                truncated = True
                del data[cap:]
                break
    finally:
        r.close()
    return bytes(data), truncated


def text_lines(data: bytes, label: str) -> tuple[list[str], str]:
    """Cleaned lines and the detected encoding; binary content is refused."""
    if looks_binary(data):
        raise ToolError(f"{label} looks binary ({len(data)} bytes); only text files can be read.")
    text, encoding = decode_text(data)
    return [clean_line(x) for x in text.split("\n")], encoding


def _read_text_artifact(t: Target, number: Any, path: str) -> tuple[list[str], bool, str]:
    """Lines of a text artifact (first 20 MB), whether it was cut off, and its encoding."""
    r = t.inst.get(f"{t.path}/{number}/artifact/{quote(path.lstrip('/'), safe='/')}", stream=True, allow_404=True)
    if r is None:
        raise ToolError(f"Artifact '{path}' not found in build #{number}. Use list_artifacts.")
    data, truncated = stream_bytes(r, _ARTIFACT_CAP)
    lines, encoding = text_lines(data, f"Artifact '{path}'")
    return lines, truncated, encoding


def masked_lines(path: str, lines: list[str]) -> list[str]:
    return mask_env_text("\n".join(lines)) if path.endswith((".env", "env.log")) else [mask(x) for x in lines]


def render_text(path: str, lines: list[str], where: str, truncated: bool, encoding: str,
                pattern: str, start: int, count: int) -> str:
    """Masked text file view: grep with 1 line context (pattern) or a window of `count` lines from `start`
    (0 = tail). `where` names the source, e.g. 'build #42'."""
    lines = masked_lines(path, lines)
    info = f"{len(lines)} lines" + (f", {encoding}" if encoding != "utf-8" else "")
    info += ", cut off after the first 20 MB" if truncated else ""
    head = [f"{path} ({info}), {where}", UNTRUSTED]
    if pattern:
        rx = safe_regex(pattern, 2)  # re.IGNORECASE
        hits = [i for i, x in enumerate(lines) if rx.search(x)]
        body: list[str] = []
        for h in hits[:50]:
            for k in range(max(0, h - 1), min(len(lines), h + 2)):
                body.append(f"{'>' if k == h else ' '}{k + 1:>6}| {clip(lines[k], 400)}")
            body.append("   ...")
        return budget("\n".join(head + [f"{len(hits)} matches"] + body))
    count = max(1, min(count, 500))
    a = max(0, len(lines) - count) if start <= 0 else start - 1
    return budget("\n".join(head + [f"{k + 1:>6}| {clip(lines[k], 400)}" for k in range(a, min(len(lines), a + count))]))


def _artifact_url(t: Target, number: Any, path: str) -> str:
    return f"{t.path}/{number}/artifact/{quote(path.lstrip('/'), safe='/')}"


_MAX_SIZES = 50


@mcp.tool(annotations=READ_ONLY)
def list_artifacts(job: Job, build: Build = "", instance: InstanceName = "", limit: int = 200,
                   sizes: Annotated[bool, Field(description="Also show file sizes (one HEAD request per file, "
                                                            f"first {_MAX_SIZES})")] = False) -> JsonDict:
    """Archived artifacts of a build (relative paths, optionally with sizes) and the download base URL.
    Read text artifacts with read_artifact; binaries (jar, zip, pdf, xlsx...) are never sent to the model -
    use the download URL."""
    t = resolve(job, build, instance)
    number = build_info(t, light=True)["number"]
    arts = [str(a.get("relativePath")) for a in artifacts_of(t, number)][: max(1, min(limit, 1000))]
    if sizes:
        arts = [f"{p} ({fmt_size(t.inst.size(_artifact_url(t, number, p)))})" if i < _MAX_SIZES else p
                for i, p in enumerate(arts)]
    return {"instance": t.inst.name, "build": number, "count": len(arts),
            "base_url": f"{t.build_url(number)}artifact/", "artifacts": arts}


def _fingerprints(t: Target, number: Any) -> dict[str, str]:
    """relativePath or file name -> MD5, if the build recorded fingerprints."""
    d = t.inst.json(f"{t.path}/{number}/api/json", tree="fingerprint[fileName,hash]") or {}
    return {str(f["fileName"]): str(f["hash"]) for f in d.get("fingerprint") or []
            if isinstance(f, dict) and f.get("fileName") and f.get("hash")}


def _fingerprint_of(fps: dict[str, str], path: str) -> str | None:
    if path in fps:
        return fps[path]
    base = path.rsplit("/", 1)[-1]
    hits = [h for name, h in fps.items() if name.rsplit("/", 1)[-1] == base]
    return hits[0] if len(hits) == 1 else None


def _download(t: Target, number: Any, path: str) -> tuple[bytes, bool]:
    r = t.inst.get(_artifact_url(t, number, path), stream=True, allow_404=True)
    if r is None:
        raise ToolError(f"Artifact '{path}' could not be downloaded from build #{number}.")
    return stream_bytes(r, _ARTIFACT_CAP)


_SMALL = 1024 * 1024  # files up to this size are downloaded and compared byte-wise
_MAX_DOWNLOADS = 20
_MAX_COMPARED = 100


def _compare_one(tg: Target, ng: Any, tb: Target, nb: Any, path: str) -> JsonDict:
    a, cut_a = _download(tg, ng, path)
    b, cut_b = _download(tb, nb, path)
    out: JsonDict = {"path": path}
    if looks_binary(a) or looks_binary(b):
        ha, hb = hashlib.sha256(a).hexdigest(), hashlib.sha256(b).hexdigest()
        out["binary"] = True
        out["result"] = (f"identical (sha256 {ha[:16]}, {fmt_size(len(a))})" if ha == hb and not (cut_a or cut_b) else
                         f"different: sha256 {ha[:16]} ({fmt_size(len(a))}) -> {hb[:16]} ({fmt_size(len(b))})")
        if cut_a or cut_b:
            out["note"] = "compared only the first 20 MB"
        return out
    la = masked_lines(path, text_lines(a, f"Artifact '{path}'")[0])
    lb = masked_lines(path, text_lines(b, f"Artifact '{path}'")[0])
    diff = list(difflib.unified_diff(la, lb, fromfile=f"#{ng}/{path}", tofile=f"#{nb}/{path}", lineterm="", n=2))
    out["diff"] = budget("\n".join(diff), 8000) if diff else "identical"
    out["note"] = UNTRUSTED
    return out


@mcp.tool(annotations=READ_ONLY)
def compare_artifacts(job: Job, good: Build = "lastSuccessfulBuild", bad: Build = "lastFailedBuild",
                      path: Annotated[str, Field(description="One artifact to diff; empty = compare the lists")] = "",
                      instance: InstanceName = "") -> JsonDict:
    """Compare the archived artifacts of two builds. Without path: artifacts only in one build, and which
    common ones changed (by Jenkins fingerprint, else by size, small files byte-wise). With path: a unified
    diff of a text artifact (secrets masked), or the sha256/size of a binary one."""
    tg, tb = resolve(job, good, instance), resolve(job, bad, instance)
    ng, nb = build_info(tg, light=True)["number"], build_info(tb, light=True)["number"]
    ag = [str(a.get("relativePath")) for a in artifacts_of(tg, ng)]
    ab = [str(a.get("relativePath")) for a in artifacts_of(tb, nb)]
    out: JsonDict = {"instance": tg.inst.name, "job": tg.full_name, "good": f"#{ng}", "bad": f"#{nb}"}
    if path.strip():
        p = path.strip().lstrip("/")
        for n, arts in ((ng, ag), (nb, ab)):
            if p not in arts:
                raise ToolError(f"Artifact '{p}' is not in build #{n}. Use compare_artifacts without path or "
                                "list_artifacts.")
        out.update(_compare_one(tg, ng, tb, nb, p))
        return out
    out["only_in_good"] = sorted(set(ag) - set(ab))
    out["only_in_bad"] = sorted(set(ab) - set(ag))
    common = [p for p in ag if p in set(ab)]
    fg, fb = _fingerprints(tg, ng), _fingerprints(tb, nb)
    changed: list[str] = []
    identical: list[str] = []
    unchecked: list[str] = []
    downloads = 0
    for p in common[:_MAX_COMPARED]:
        ha, hb = _fingerprint_of(fg, p), _fingerprint_of(fb, p)
        if ha and hb:
            (identical.append(p) if ha == hb else changed.append(f"{p}: content differs (fingerprint)"))
            continue
        sa, sb = tg.inst.size(_artifact_url(tg, ng, p)), tb.inst.size(_artifact_url(tb, nb, p))
        if sa is not None and sb is not None and sa != sb:
            changed.append(f"{p}: size {fmt_size(sa)} -> {fmt_size(sb)}")
        elif downloads < _MAX_DOWNLOADS and (sa or 0) <= _SMALL and (sb or 0) <= _SMALL:
            downloads += 1
            if _download(tg, ng, p)[0] == _download(tb, nb, p)[0]:
                identical.append(p)
            else:
                changed.append(f"{p}: content differs ({fmt_size(sa)} -> {fmt_size(sb)})")
        else:
            unchecked.append(f"{p}: same size {fmt_size(sa)}, content not compared")
    out["changed"] = changed
    out["identical"] = identical
    if unchecked:
        out["not_compared"] = unchecked
    if len(common) > _MAX_COMPARED:
        out["note"] = f"compared the first {_MAX_COMPARED} of {len(common)} common artifacts"
    if changed:
        out["next_step"] = f"compare_artifacts(path='{changed[0].split(':')[0]}') for the diff of one file"
    return out


@mcp.tool(annotations=READ_ONLY)
def read_artifact(job: Job, path: Annotated[str, Field(description="relativePath from list_artifacts")],
                  build: Build = "", instance: InstanceName = "", pattern: str = "",
                  start: int = 1, count: int = 200) -> str:
    """Read a text artifact (reports, logs, yaml, json...). With `pattern` it greps (regex, case-insensitive,
    1 line context, max 50 matches); otherwise returns `count` lines from `start` (0 = tail). Secrets are masked.
    UTF-8, UTF-16 (with BOM) and cp1252 files are decoded; binary files are refused."""
    t = resolve(job, build, instance)
    number = build_info(t, light=True)["number"]
    lines, truncated, encoding = _read_text_artifact(t, number, path)
    return render_text(path, lines, f"build #{number} on {t.inst.name}", truncated, encoding, pattern, start, count)


@mcp.tool(annotations=READ_ONLY)
def get_env_log(job: Job, build: Build = "", instance: InstanceName = "", pattern: str = "") -> str:
    """The env.log artifact (environment variables dumped by the pipeline) of a build, with
    secret-looking values masked. Optional regex `pattern` filters the lines (e.g. 'JAVA|MAVEN|NODE')."""
    t = resolve(job, build, instance)
    number = build_info(t, light=True)["number"]
    lines = env_log_of(t, number)
    if lines is None:
        return f"Build #{number} on {t.inst.name} has no env.log artifact."
    if pattern:
        rx = safe_regex(pattern, 2)
        lines = [x for x in lines if rx.search(x)]
    return budget("\n".join([f"env.log of build #{number} on {t.inst.name} ({len(lines)} lines)"] + sorted(lines)))
