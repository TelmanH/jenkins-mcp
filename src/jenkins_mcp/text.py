"""Secret masking, size budget and formatting helpers."""

import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse

from .settings import MAX_CHARS

_SECRET_NAME = re.compile(r"pass(word|wd)?|token|secret|api[_-]?key|private[_-]?key|credential|auth|cookie", re.I)
_NOT_SECRET = {"PWD", "OLDPWD"}
_INLINE_SECRETS = [
    (re.compile(r"(?i)\b(authorization:\s*(?:basic|bearer|token)\s+)\S+"), r"\1****"),
    (re.compile(r"(?i)\b((?:password|passwd|token|secret|api[_-]?key)\s*[=:]\s*)(?!\*\*\*)[^\s,;'\"]+"), r"\1****"),
    (re.compile(r"(https?://[^/\s:@]+:)[^@\s/]+@"), r"\1****@"),
    # Pipeline password parameter: password(name: 'X', defaultValue: 'secret')
    (re.compile(r"(?i)(\bpassword\s*\([^)]*?\bdefaultValue\s*:\s*(['\"]))(?:\\.|(?!\2).)*\2"), r"\1****\2"),
    # XML elements (settings.xml, config.xml) and Jenkins-encrypted secrets
    (re.compile(r"(?i)(<(password|passwd|passphrase|token|secret|privateKey|apiKey|api_key)>)[^<]*(</\2>)"),
     r"\1****\3"),
    (re.compile(r"\{AQAAAB[A-Za-z0-9+/=]*\}"), "{****}"),
]
NORMALIZE = re.compile(r"0x[0-9a-fA-F]+|\b[0-9a-f]{7,}\b|\d+")


def mask(text: str) -> str:
    for pattern, repl in _INLINE_SECRETS:
        text = pattern.sub(repl, text)
    return text


_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
_ASCII = bytes(range(128))


def looks_binary(data: bytes) -> bool:
    """NUL bytes near the start = binary, unless it is UTF-16 text with a byte order mark."""
    return not data.startswith(_UTF16_BOMS) and b"\x00" in data[:8192]


def decode_text(data: bytes) -> tuple[str, str]:
    """(text, encoding): UTF-16 with BOM, UTF-8 (with or without BOM), else cp1252 (German Windows tools)."""
    if data.startswith(_UTF16_BOMS):
        return data.decode("utf-16", errors="replace").lstrip("\ufeff"), "utf-16"
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as e:
        text = data.decode("utf-8", errors="replace")
        if e.start >= len(data) - 3:  # only a multi-byte character cut off by a size limit
            return text, "utf-8"
        # mostly valid UTF-8 with a few broken bytes stays UTF-8; isolated high bytes mean cp1252
        bad = text.count("\ufffd")
        high = len(data.translate(None, _ASCII))  # non-ASCII bytes (ASCII deleted, in C)
        if high - bad >= 2 * bad:
            return text, "utf-8"
        return data.decode("cp1252", errors="replace"), "cp1252"


def is_secret_name(name: str) -> bool:
    return name.upper() not in _NOT_SECRET and bool(_SECRET_NAME.search(name))


def mask_env_text(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_][\w.]*)=(.*)$", line)
        if m and is_secret_name(m.group(1)) and m.group(2):
            out.append(f"{m.group(1)}=****")
        else:
            out.append(mask(line))
    return out


def clip(s: Any, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[:n] + f"...[+{len(s) - n} chars]"


def budget(text: str, limit: int = MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    cut = cut if cut > limit // 2 else limit
    return text[:cut] + (f"\n[output clipped at {limit} chars - narrow the request "
                         "(pattern, stage, start/count) to see more]")


def safe_regex(pattern: str, flags: int = 0) -> re.Pattern[str]:
    """User/agent supplied search pattern: invalid regex is treated as literal text."""
    try:
        return re.compile(pattern, flags)
    except re.error:
        return re.compile(re.escape(pattern), flags)


def fmt_size(n: int | None) -> str:
    if n is None:
        return "size ?"
    for unit, factor in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= factor:
            return f"{n / factor:.1f} {unit}"
    return f"{n} B"


def fmt_duration(ms: Any) -> str:
    s = int((ms or 0) / 1000)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")


def fmt_age(ts_ms: Any) -> str:
    if not ts_ms:
        return "?"
    d = max(0, time.time() - ts_ms / 1000)
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


def fmt_time(ts_ms: Any) -> str:
    if not ts_ms:
        return "?"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")


def short_class(cls: Any) -> str:
    return str(cls or "").rsplit(".", 1)[-1].rsplit("$", 1)[-1]


def url_to_full_name(url: str) -> str:
    segs = [unquote(s) for s in urlparse(url or "").path.split("/") if s]
    return "/".join(segs[i + 1] for i in range(len(segs) - 1) if segs[i] == "job")
