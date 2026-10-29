"""Cleaning console lines and the ParsedLog model (stages, nodes, noise, rendering)."""

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .. import config
from ..text import NORMALIZE, clip, mask

if TYPE_CHECKING:
    from .errors import ErrorGroup

_CONSOLE_NOTE = re.compile(r"\x1b\[8mha:[^\x1b]*\x1b\[0m")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# exactly one separator space: the rest (e.g. the tab of '\tat ...' stack lines) belongs to the line
_TIMESTAMP = re.compile(r"^(?:\[\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z?\]|\d\d:\d\d:\d\d(?:\.\d+)?) ")
_STAGE_OPEN = re.compile(r"^\[Pipeline\] \{ \((.*)\)\s*$")
_NODE_LINE = re.compile(r"Running on (.+?) in (\S+)")


def clean_line(line: str) -> str:
    if "\x1b" in line:
        line = _CONSOLE_NOTE.sub("", line)
    if "\r" in line:
        line = line.rstrip("\r")
        if "\r" in line:  # progress bars: keep what was finally visible
            line = line.rsplit("\r", 1)[-1]
    if "\x1b" in line:
        line = _ANSI.sub("", line)
    return line.rstrip()


def strip_ts(line: str) -> str:
    return _TIMESTAMP.sub("", line, count=1)


@dataclass
class ParsedLog:
    # Shared between worker threads via the LRU. The lazy fields below may be computed twice
    # concurrently; both computations give the same result, so the race is harmless.
    lines: list[str]
    size_bytes: int
    truncated: bool
    _stage_of: list[str] | None = None
    _spans: "OrderedDict[str, list[int]] | None" = None
    _groups: "list[ErrorGroup] | None" = None
    _groups_for: int = -1  # id() of the Settings the groups were computed with
    _text: str | None = field(default=None, repr=False)

    # -- stages from console markers (works without the pipeline-stage-view plugin)
    def _parse_stages(self) -> None:
        stack: list[str | None] = []
        stage_of: list[str] = []
        spans: OrderedDict[str, list[int]] = OrderedDict()
        for i, raw in enumerate(self.lines):
            s = strip_ts(raw)
            if s.startswith("[Pipeline] {"):
                m = _STAGE_OPEN.match(s)
                name = m.group(1) if m else None
                if name and name.startswith("Branch: "):
                    name = name[len("Branch: "):]
                stack.append(name)
            cur = " > ".join(n for n in stack if n)
            stage_of.append(cur)
            if cur:
                spans.setdefault(cur, [i, i])[1] = i
            if s.startswith("[Pipeline] }") and stack:
                stack.pop()
        self._stage_of, self._spans = stage_of, spans

    @property
    def stage_of(self) -> list[str]:
        if self._stage_of is None:
            self._parse_stages()
        return self._stage_of  # type: ignore[return-value]

    @property
    def spans(self) -> "OrderedDict[str, list[int]]":
        if self._spans is None:
            self._parse_stages()
        return self._spans  # type: ignore[return-value]

    @property
    def text(self) -> str:
        """Whole cleaned log without timestamps as one string (fast regex search; '^' = start of what
        the user sees). Line count is unchanged, so line_of_offset stays valid."""
        if self._text is None:
            self._text = "\n".join(strip_ts(x) for x in self.lines)
        return self._text

    def line_of_offset(self, offset: int) -> int:
        """0-based line index of a character offset in `text`."""
        return self.text.count("\n", 0, offset)

    def stage_range(self, name: str) -> tuple[int, int] | None:
        """0-based inclusive line range of a stage; matches any path component."""
        hits = [span for path, span in self.spans.items()
                if name == path or name in [c.strip() for c in path.split(" > ")]]
        if not hits:
            low = name.lower()
            hits = [span for path, span in self.spans.items() if low in path.lower()]
        if not hits:
            return None
        return min(h[0] for h in hits), max(h[1] for h in hits)

    def is_noise(self, i: int) -> bool:
        s = strip_ts(self.lines[i])
        return any(p.search(s) for p in config.current().noise)

    @property
    def nodes(self) -> list[str]:
        seen: list[str] = []
        for line in self.lines:
            m = _NODE_LINE.search(line)
            if m and m.group(1) not in seen:
                seen.append(m.group(1))
        return seen

    @property
    def groups(self) -> "list[ErrorGroup]":
        settings = config.current()
        if self._groups is None or self._groups_for != id(settings):  # recompute after reload_config
            from .errors import find_errors
            self._groups, self._groups_for = find_errors(self), id(settings)
        return self._groups


# ----------------------------------------------------------------------------- rendering
def fmt_line(pl: ParsedLog, i: int, mark: bool = False) -> str:
    return f"{'>' if mark else ' '}{i + 1:>6}| {clip(mask(strip_ts(pl.lines[i])), 400)}"


def _shape(line: str) -> str:
    return NORMALIZE.sub("#", strip_ts(line))


def render_collapsed(pl: ParsedLog, idx: list[int]) -> list[str]:
    """Render lines; runs of >=3 lines that differ only in numbers/hashes become one line + a counter."""
    out: list[str] = []
    k = 0
    while k < len(idx):
        j = k
        while j + 1 < len(idx) and _shape(pl.lines[idx[j + 1]]) == _shape(pl.lines[idx[k]]):
            j += 1
        if j - k >= 2:
            out.append(fmt_line(pl, idx[k]))
            out.append(f"        ... x{j - k} similar lines (L{idx[k + 1] + 1}-L{idx[j] + 1})")
        else:
            out += [fmt_line(pl, i) for i in idx[k:j + 1]]
        k = j + 1
    return out


def meaningful_tail(pl: ParsedLog, count: int, lo: int = 0, hi: int | None = None) -> list[str]:
    """Last `count` rendered rows of non-noise lines in [lo, hi], repetitive runs collapsed."""
    hi = len(pl.lines) - 1 if hi is None else hi
    picked: list[int] = []
    i = hi
    while i >= lo and len(picked) < count * 8:
        if not pl.is_noise(i):
            picked.append(i)
        i -= 1
    picked.reverse()
    rows = render_collapsed(pl, picked)
    rows = rows[-count:]
    if rows and rows[0].lstrip().startswith("..."):
        rows = rows[1:]
    return rows
