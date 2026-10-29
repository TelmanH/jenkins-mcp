"""Loading knowledge base entries (Markdown with YAML front matter), reloaded when files change."""

import fnmatch
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .. import config
from ..settings import JsonDict

FLAGS = re.IGNORECASE | re.MULTILINE
_ID = re.compile(r"\bKB-(\d{3,})\b", re.I)
_SOLUTION = ("solution", "fix", "resolution", "workaround")


def as_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    return [str(v) for v in (value if isinstance(value, list) else [value]) if str(v).strip()]


@dataclass
class Entry:
    id: str
    title: str
    path: Path
    meta: JsonDict
    body: str
    draft: bool
    instances: list[str]
    jobs: list[str]
    match: list[re.Pattern[str]]
    red_herrings: list[re.Pattern[str]]
    consequences: list[re.Pattern[str]]

    @property
    def status(self) -> str:
        return str(self.meta.get("status") or ("draft" if self.draft else "open"))

    @property
    def category(self) -> str:
        return str(self.meta.get("category") or "")

    def in_scope(self, instance: str, job: str) -> bool:
        """Empty instance/job = unknown -> not used as a filter."""
        if self.instances and instance and instance.lower() not in [i.lower() for i in self.instances]:
            return False
        if self.jobs and job and not any(fnmatch.fnmatchcase(job.lower(), g.lower()) for g in self.jobs):
            return False
        return True

    def scope(self) -> str:
        parts = [",".join(self.instances) or "all instances", ",".join(self.jobs) or "all jobs"]
        return " / ".join(parts)

    def section(self, *names: str) -> str:
        """Text of the first '## heading' whose title contains one of `names`."""
        out: list[str] = []
        inside = False
        for line in self.body.splitlines():
            if line.startswith("## "):
                if inside:
                    break
                inside = any(n in line.lower() for n in names)
                continue
            if inside:
                out.append(line)
        return "\n".join(out).strip()

    def solution_hint(self) -> str:
        for line in self.section(*_SOLUTION).splitlines():
            line = line.strip().lstrip("-*0123456789. ").strip()
            if line and not line.startswith("<!--"):
                return line
        return ""

    def row(self) -> str:
        flag = " DRAFT" if self.draft else ""
        return f"{self.id} | {self.status}{flag} | {self.category or '-'} | {self.scope()} | {self.title}"


@dataclass
class KnowledgeBase:
    dir: Path
    entries: list[Entry]
    drafts: list[Entry]
    errors: list[str]
    signature: tuple

    def find(self, kb_id: str) -> Entry | None:
        key = kb_id.strip().lower()
        for e in self.entries + self.drafts:
            if e.id.lower() == key or e.path.stem.lower() == key:
                return e
        return None

    def next_id(self) -> str:
        nums = [0]
        for p in list(self.dir.glob("*.md")) + list((self.dir / "drafts").glob("*.md")):
            m = _ID.search(p.name)
            if m:
                nums.append(int(m.group(1)))
        for e in self.entries + self.drafts:
            m = _ID.search(e.id)
            if m:
                nums.append(int(m.group(1)))
        return f"KB-{max(nums) + 1:04d}"


def _compile(values: Any, where: str, errors: list[str]) -> list[re.Pattern[str]]:
    out = []
    for i, v in enumerate(as_list(values)):
        try:
            out.append(re.compile(v, FLAGS))
        except re.error as e:
            errors.append(f"{where}[{i}]: invalid regex {v!r}: {e}")
    return out


def parse_file(path: Path, draft: bool) -> tuple[Entry | None, list[str]]:
    errors: list[str] = []
    name = path.name if not draft else f"drafts/{path.name}"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, [f"{name}: cannot read: {e}"]
    if not text.startswith("---"):
        return None, [f"{name}: must start with a '---' front matter block"]
    end = text.find("\n---", 3)
    if end < 0:
        return None, [f"{name}: front matter is not closed with '---'"]
    try:
        meta = yaml.safe_load(text[3:end]) or {}
        if not isinstance(meta, dict):
            raise ValueError("front matter must be a mapping")
    except Exception as e:
        return None, [f"{name}: invalid front matter: {e}"]
    body = text[end + 4:].split("\n", 1)[1] if "\n" in text[end + 4:] else ""
    m = _ID.search(str(meta.get("id") or "")) or _ID.search(path.stem)
    kb_id = m.group(0).upper() if m else path.stem
    entry = Entry(
        id=kb_id, title=str(meta.get("title") or path.stem), path=path, meta=meta, body=body.strip(),
        draft=draft, instances=as_list(meta.get("instance")), jobs=as_list(meta.get("job")),
        match=_compile(meta.get("match"), f"{name}: match", errors),
        red_herrings=_compile(meta.get("red_herrings"), f"{name}: red_herrings", errors),
        consequences=_compile(meta.get("consequences"), f"{name}: consequences", errors),
    )
    if not entry.match and not entry.red_herrings and not draft:
        errors.append(f"{name}: has neither 'match' nor 'red_herrings' - it can never be found automatically")
    return entry, errors


def _files(kb_dir: Path) -> tuple[list[Path], list[Path]]:
    if not kb_dir.is_dir():
        return [], []
    entries = sorted(p for p in kb_dir.glob("*.md") if _ID.match(p.name))
    drafts = sorted((kb_dir / "drafts").glob("*.md")) if (kb_dir / "drafts").is_dir() else []
    return entries, drafts


def _signature(kb_dir: Path, files: list[Path]) -> tuple:
    sig: list[Any] = [str(kb_dir)]
    for p in files:
        try:
            st = p.stat()
            sig.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    return tuple(sig)


_lock = threading.Lock()
_cached: KnowledgeBase | None = None


def get() -> KnowledgeBase:
    """Current knowledge base; re-read automatically when a file is added, changed or removed."""
    global _cached
    kb_dir = config.current().kb_dir
    entry_files, draft_files = _files(kb_dir)
    sig = _signature(kb_dir, entry_files + draft_files)
    with _lock:
        if _cached is not None and _cached.signature == sig:
            return _cached
        errors: list[str] = []
        if not kb_dir.is_dir():
            errors.append(f"knowledge folder {kb_dir} does not exist")
        entries: list[Entry] = []
        drafts: list[Entry] = []
        for files, draft, bucket in ((entry_files, False, entries), (draft_files, True, drafts)):
            for p in files:
                e, errs = parse_file(p, draft)
                errors += errs
                if e:
                    bucket.append(e)
        seen: dict[str, str] = {}
        for e in entries:
            if e.id in seen:
                errors.append(f"duplicate id {e.id}: {seen[e.id]} and {e.path.name}")
            seen[e.id] = e.path.name
        _cached = KnowledgeBase(kb_dir, entries, drafts, errors, sig)
        return _cached
