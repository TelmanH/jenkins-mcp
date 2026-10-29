"""Finding error lines with the configured rules and grouping identical ones."""

import re
from dataclasses import dataclass

from .. import config
from ..text import NORMALIZE, clip, mask
from .parse import ParsedLog, strip_ts

_STACK_LINE = re.compile(r"^\s+(?:at |\.\.\. \d+ (?:more|common frames omitted))")
_WARN_PREFIX = re.compile(r"^\s*(?:\[?WARN(?:ING)?\]?[\s:]|W\d{4} )", re.I)


@dataclass
class ErrorGroup:
    category: str
    severity: str  # high | medium | warn | low (ranking order)
    first_line: int  # 1-based
    last_line: int
    count: int
    text: str
    stage: str
    stack: list[str]
    stack_more: int
    key: tuple[str, str]

    def render(self, note: str = "") -> str:
        head = f"[{self.category}{'/warn' if self.severity == 'warn' else ''}] L{self.first_line}"
        if self.count > 1:
            head += f" (x{self.count}, last L{self.last_line})"
        if self.stage:
            head += f" stage: {self.stage}"
        if note:
            head += f"  <- {note}"
        out = [head, "    " + mask(self.text)]
        out += ["      " + clip(s.strip(), 200) for s in self.stack]
        if self.stack_more:
            out.append(f"      ... +{self.stack_more} more stack lines")
        return "\n".join(out)


def match_rule(s: str) -> tuple[str, str] | None:
    for rule in config.current().rules:
        if rule.rx.search(s):
            # a matched line that is explicitly logged as a warning is context, not a root cause
            sev = "warn" if rule.severity in ("high", "medium") and _WARN_PREFIX.match(s) else rule.severity
            return rule.category, sev
    return None


def find_errors(pl: ParsedLog) -> list[ErrorGroup]:
    ignore = config.current().ignore
    groups: dict[tuple[str, str], ErrorGroup] = {}
    lines, n, i = pl.lines, len(pl.lines), 0
    stage_of = pl.stage_of
    while i < n:
        s = strip_ts(lines[i])
        if not s or s.startswith(("+ ", "[Pipeline]")) or any(p.search(s) for p in ignore):
            i += 1
            continue
        rule = match_rule(s)
        if rule is None:
            i += 1
            continue
        j, stack = i + 1, []
        while j < n and _STACK_LINE.match(strip_ts(lines[j])):
            stack.append(strip_ts(lines[j]))
            j += 1
        key = (rule[0], re.sub(r"\s+", " ", NORMALIZE.sub("#", s)).strip()[:200])
        g = groups.get(key)
        if g:
            g.count += 1
            g.last_line = i + 1
        else:
            groups[key] = ErrorGroup(rule[0], rule[1], i + 1, i + 1, 1, clip(s.strip(), 300),
                                     stage_of[i], stack[:4], max(0, len(stack) - 4), key)
        i = j if stack else i + 1
    return sorted(groups.values(), key=lambda g: g.first_line)


def root_cause_candidates(groups: list[ErrorGroup], k: int = 3, stage: str | None = None,
                          exclude: set[tuple[str, str]] | None = None) -> list[ErrorGroup]:
    """Groups of the most severe non-empty tier (high, then medium = generic error lines, then warn,
    then everything), those inside the failed stage first, in order of appearance.
    `exclude` = group keys the knowledge base marked as known noise / consequences."""
    pool = [g for g in groups if not exclude or g.key not in exclude] or groups
    tier = next((t for sev in ("high", "medium", "warn") if (t := [g for g in pool if g.severity == sev])), pool)
    if stage:
        inside = [g for g in tier if stage in [c.strip() for c in g.stage.split(" > ")]]
        tier = inside + [g for g in tier if g not in inside]
    return tier[:k]
