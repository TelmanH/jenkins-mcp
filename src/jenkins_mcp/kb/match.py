"""Matching a build log (or a pasted error text) against the knowledge base."""

import re
import threading
from dataclasses import dataclass

from ..logs.errors import ErrorGroup
from ..logs.parse import ParsedLog, strip_ts
from ..text import clip, mask
from . import store
from .store import Entry


@dataclass
class Hit:
    entry: Entry
    matched: list[tuple[int, str]]  # (1-based line, text) - one per matching `match` regex
    total: int

    @property
    def score(self) -> float:
        return len(self.matched) / max(1, self.total)

    def render(self) -> str:
        e = self.entry
        line, text = self.matched[0]
        where = f"L{line}: " if line else ""
        fix = e.solution_hint() or "no solution documented yet"
        return (f"{e.id} [{e.status}{', DRAFT' if e.draft else ''}] {e.title} | "
                f"matched {len(self.matched)}/{self.total} patterns, first {where}{clip(mask(text), 160)} | "
                f"fix: {clip(fix, 200)} | details: get_known_issue('{e.id}')")


@dataclass
class Analysis:
    hits: list[Hit]
    notes: dict[tuple[str, str], str]  # error group key -> "known noise (KB-x)" / "consequence of KB-x" / "known issue KB-x"

    @property
    def not_root_cause(self) -> set[tuple[str, str]]:
        return {k for k, v in self.notes.items() if not v.startswith("known issue")}

    def known_issue_groups(self, groups: list[ErrorGroup]) -> list[ErrorGroup]:
        return [g for g in groups if self.notes.get(g.key, "").startswith("known issue")]


def _sort(hits: list[Hit]) -> list[Hit]:
    return sorted(hits, key=lambda h: (-h.score, -len(h.matched), h.entry.id))


# How often each entry matched a build since the server started (in memory; one count per build).
_STATS: dict[str, int] = {}
_SEEN: set[tuple] = set()
_STATS_LOCK = threading.Lock()


def _count(hits: list[Hit], pl: ParsedLog, instance: str, job: str) -> None:
    build = (instance, job, len(pl.lines), pl.size_bytes)
    with _STATS_LOCK:
        if len(_SEEN) > 20000:
            _SEEN.clear()
        for h in hits:
            key = (h.entry.id, *build)
            if key not in _SEEN:
                _SEEN.add(key)
                _STATS[h.entry.id] = _STATS.get(h.entry.id, 0) + 1


def hit_stats() -> dict[str, int]:
    with _STATS_LOCK:
        return dict(sorted(_STATS.items(), key=lambda kv: (-kv[1], kv[0])))


def analyse(pl: ParsedLog, instance: str, job: str) -> Analysis:
    """Entries in scope (instance/job) whose `match` regexes occur in the log, plus notes for
    error groups that are red herrings (scope-wide) or consequences of a matched entry."""
    scoped = [e for e in store.get().entries if e.in_scope(instance, job)]
    hits: list[Hit] = []
    for e in scoped:
        found = []
        for rx in e.match:
            m = rx.search(pl.text)
            if m:
                i = pl.line_of_offset(m.start())
                found.append((i + 1, strip_ts(pl.lines[i]).strip()))
        if found:
            hits.append(Hit(e, found, len(e.match)))
    hits = _sort(hits)
    _count(hits, pl, instance, job)

    notes: dict[tuple[str, str], str] = {}
    matched = [h.entry for h in hits]
    for g in pl.groups:
        for e in matched:
            if any(rx.search(g.text) for rx in e.match):
                notes[g.key] = f"known issue {e.id}"
                break
        if g.key in notes:
            continue
        for e in scoped:
            if any(rx.search(g.text) for rx in e.red_herrings):
                notes[g.key] = f"known noise ({e.id})"
                break
            if e in matched and any(rx.search(g.text) for rx in e.consequences):
                notes[g.key] = f"consequence of {e.id}"
                break
    return Analysis(hits, notes)


_WORD = re.compile(r"[^\W\d][\w./-]{3,}")  # words of 4+ chars, any alphabet


def match_text(text: str, instance: str = "", job: str = "", include_drafts: bool = False) -> list[Hit]:
    """Entries whose regexes match a pasted error text; falls back to keyword overlap."""
    base = store.get()
    pool = base.entries + (base.drafts if include_drafts else [])
    pool = [e for e in pool if e.in_scope(instance, job)]
    hits: list[Hit] = []
    for e in pool:
        found = []
        for rx in e.match:
            m = rx.search(text)
            if m:
                found.append((0, m.group(0)))
        if found:
            hits.append(Hit(e, found, len(e.match)))
    if hits:
        return _sort(hits)
    words = {w.lower() for w in _WORD.findall(text)}
    scored = []
    for e in pool:
        hay = f"{e.title}\n{e.body}".lower()
        common = [w for w in words if w in hay]
        if len(common) >= 2:
            scored.append((len(common), e, common))
    scored.sort(key=lambda x: (-x[0], x[1].id))
    return [Hit(e, [(0, "keywords: " + ", ".join(sorted(c)[:6]))], max(1, len(words))) for _, e, c in scored[:5]]
