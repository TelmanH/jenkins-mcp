"""Downloading console logs once, with a disk cache (finished builds) and a small in-memory LRU.
Running builds are fetched incrementally (progressiveText from the last offset)."""

import hashlib
import re
import threading
from collections import OrderedDict

from fastmcp.exceptions import ToolError

from ..settings import CACHE_DIR, CACHE_MAX_MB, MAX_LOG_MB, JsonDict, log
from ..targets import Target
from ..text import decode_text
from .parse import ParsedLog, clean_line

_LRU: "OrderedDict[str, ParsedLog]" = OrderedDict()
_LRU_LOCK = threading.Lock()
_LRU_SIZE = 6
_RUNNING: "OrderedDict[str, tuple[bytes, int]]" = OrderedDict()  # running build -> (log so far, next offset)
_RUNNING_SIZE = 10
_TRUNCATED_MARK = b"\n[jenkins-mcp: log larger than "


def _prune_cache() -> None:
    try:
        files = sorted(CACHE_DIR.rglob("*.log"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        while files and total > CACHE_MAX_MB * 1024 * 1024:
            f = files.pop(0)
            total -= f.stat().st_size
            f.unlink(missing_ok=True)
    except OSError as e:
        log.warning("cache prune failed: %s", e)


def _download_log(t: Target, number: int) -> tuple[bytes, bool]:
    """Stream consoleText; logs above MAX_LOG_MB keep the first 20 % and the end."""
    r = t.inst.get(f"{t.path}/{number}/consoleText", stream=True)
    if r is None:
        raise ToolError(f"[{t.inst.name}] No console log for build #{number}.")
    cap = MAX_LOG_MB * 1024 * 1024
    head_cap, tail_cap = cap // 5, cap - cap // 5
    head, tail, total = bytearray(), bytearray(), 0
    try:
        for chunk in r.iter_content(1 << 16):
            total += len(chunk)
            if len(head) < head_cap:
                take = head_cap - len(head)
                head += chunk[:take]
                chunk = chunk[take:]
            if chunk:
                tail += chunk
                if len(tail) > 2 * tail_cap:
                    del tail[: len(tail) - tail_cap]
    finally:
        r.close()
    if total <= len(head) + len(tail):
        return bytes(head + tail), False
    tail = tail[-tail_cap:]
    tail = tail[tail.find(b"\n") + 1:]
    omitted = (total - len(head) - len(tail)) // (1024 * 1024)
    marker = _TRUNCATED_MARK + f"{MAX_LOG_MB} MB, ~{omitted} MB omitted here]\n".encode()
    return bytes(head + marker + tail), True


def _progressive(t: Target, number: int, key: str) -> tuple[bytes, bool]:
    """Log of a running build: only the text after the last offset is downloaded."""
    with _LRU_LOCK:
        known, offset = _RUNNING.get(key, (b"", 0))
    r = t.inst.get(f"{t.path}/{number}/logText/progressiveText", params={"start": offset})
    if r is None:
        raise ToolError(f"[{t.inst.name}] No console log for build #{number}.")
    chunk = r.content
    size = r.headers.get("X-Text-Size", "")
    new_offset = int(size) if size.isdigit() else offset + len(chunk)
    if new_offset < offset:  # log shrank (build restarted / log rewritten): start again
        with _LRU_LOCK:
            _RUNNING.pop(key, None)
        return _progressive(t, number, key) if offset else (chunk, False)
    raw = known + chunk
    if len(raw) > MAX_LOG_MB * 1024 * 1024:  # too big to keep growing in memory
        with _LRU_LOCK:
            _RUNNING.pop(key, None)
        return _download_log(t, number)
    with _LRU_LOCK:
        current = _RUNNING.get(key, (b"", 0))
        if current[1] == offset:  # nobody else appended in the meantime
            _RUNNING[key] = (raw, new_offset)
            _RUNNING.move_to_end(key)
            while len(_RUNNING) > _RUNNING_SIZE:
                _RUNNING.popitem(last=False)
        elif current[1] > offset:
            raw = current[0]
    return raw, False


def load_log(t: Target, info: JsonDict) -> ParsedLog:
    number = info.get("number")
    building = bool(info.get("building"))
    key = f"{t.inst.name}|{t.inst.url}|{t.full_name}|{number}"  # url: re-pointed instance -> no stale log
    if not building:
        with _LRU_LOCK:
            if key in _LRU:
                _LRU.move_to_end(key)
                return _LRU[key]

    cache_file = CACHE_DIR / re.sub(r"\W", "_", t.inst.name) / (hashlib.sha1(key.encode()).hexdigest()[:24] + ".log")
    truncated = False
    if building:
        raw, truncated = _progressive(t, int(number), key)  # type: ignore[arg-type]
    elif cache_file.exists():
        raw = cache_file.read_bytes()
        truncated = _TRUNCATED_MARK in raw
    else:
        with _LRU_LOCK:
            _RUNNING.pop(key, None)  # the build finished: its partial log is no longer needed
        raw, truncated = _download_log(t, int(number))  # type: ignore[arg-type]
        try:  # finished builds never change -> cache (a truncated log keeps its marker)
            cache_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)  # logs may contain secrets
            tmp = cache_file.with_suffix(".tmp")
            tmp.write_bytes(raw)
            tmp.replace(cache_file)
            _prune_cache()
        except OSError as e:
            log.warning("cannot write log cache: %s", e)

    lines = [clean_line(x) for x in decode_text(raw)[0].split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    pl = ParsedLog(lines=lines, size_bytes=len(raw), truncated=truncated)
    if not building:
        with _LRU_LOCK:
            _LRU[key] = pl
            while len(_LRU) > _LRU_SIZE:
                _LRU.popitem(last=False)
    return pl
