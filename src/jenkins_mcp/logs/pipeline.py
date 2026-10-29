"""Pipeline facts from the console log: where the Jenkinsfile came from and which Shared Library
versions were loaded (the REST API does not export the libraries of a build)."""

import re

from .parse import ParsedLog, strip_ts

_OBTAINED = re.compile(r"^Obtained (\S+ from .+?)\s*$")
_LIBRARY = re.compile(r"^Loading library ([^@\s]+@\S+)")
_REVISION = re.compile(r"^(?:Found match: \S+ revision|Checking out Revision) ([0-9a-f]{7,40})\b")
_VERSION = re.compile(r"(\d+)\.(\d+)")
_MANAGED_FILE = re.compile(r"^copy managed file \[(.+?)\] to ")
_DOWNSTREAM = re.compile(r"^Starting building: (.+?) #(\d+)\s*$")
_LIB_WINDOW = 80  # lines after 'Loading library' that can belong to its checkout


def jenkinsfile_sources(pl: ParsedLog) -> list[str]:
    """'ci/Jenkinsfile from git https://...' (or 'from <commit>' in multibranch jobs)."""
    out: list[str] = []
    for line in pl.lines:
        s = strip_ts(line)
        if s.startswith("Obtained ") and (m := _OBTAINED.match(s)) and m.group(1) not in out:
            out.append(m.group(1))
    return out


def libraries(pl: ParsedLog) -> list[str]:
    """'name@version (revision abcd1234)'; the revision is only taken from the lines that belong to
    the library checkout (up to the next library, the next [Pipeline] step or _LIB_WINDOW lines)."""
    out: list[str] = []
    lines = pl.lines
    for i, line in enumerate(lines):
        s = strip_ts(line)
        if not s.startswith("Loading library ") or not (m := _LIBRARY.match(s)):
            continue
        lib, rev = m.group(1), ""
        for nxt in lines[i + 1: i + 1 + _LIB_WINDOW]:
            n = strip_ts(nxt)
            if n.startswith(("Loading library ", "[Pipeline]")):
                break
            if r := _REVISION.match(n):
                rev = r.group(1)
                break
        row = f"{lib} (revision {rev[:8]})" if rev else lib
        if row not in out:
            out.append(row)
    return out


def version_hints(libs: list[str], params: dict[str, str]) -> list[str]:
    """Flag a library whose branch carries a major.minor version (e.g. rel-2.3) that matches none of the
    build's *VERSION* parameters (e.g. REL_VERSION=2.4)."""
    vparams = [(k, v) for k, v in params.items() if "VERSION" in k.upper() and _VERSION.search(str(v))]
    if not vparams:
        return []
    wanted = {_VERSION.search(str(v)).group(1, 2) for _, v in vparams}  # type: ignore[union-attr]
    hints = []
    for lib in libs:
        name_ver = lib.split(" ")[0]
        m = _VERSION.search(name_ver.split("@", 1)[1])
        if m and m.group(1, 2) not in wanted:
            k, v = vparams[0]
            hints.append(f"library {name_ver} is version {m.group(1)}.{m.group(2)}, but parameter {k}={v} "
                         "- is the right library branch loaded?")
    return hints


def managed_files(pl: ParsedLog) -> list[str]:
    """Config File Provider files the build provisioned ('copy managed file [Nexus settings] to ...')."""
    out: list[str] = []
    for line in pl.lines:
        s = strip_ts(line)
        if s.startswith("copy managed file") and (m := _MANAGED_FILE.match(s)) and m.group(1) not in out:
            out.append(m.group(1))
    return out


def downstream_builds(pl: ParsedLog) -> list[tuple[str, int]]:
    """Builds started by the `build` step: ('folder/job', number). The console shows display names
    joined with ' » '; they equal the job names unless a display name was set."""
    out: list[tuple[str, int]] = []
    for line in pl.lines:
        s = strip_ts(line)
        if s.startswith("Starting building: ") and (m := _DOWNSTREAM.match(s)):
            item = ("/".join(part.strip() for part in m.group(1).split(" » ")), int(m.group(2)))
            if item not in out:
                out.append(item)
    return out
