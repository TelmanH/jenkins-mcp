"""Parsing Jenkins XML (config.xml of jobs and nodes, config history versions).

Jenkins writes these files as XML 1.1, which Python's parser refuses, and 1.1 allows character
references to control characters (e.g. &#x1b; from ANSI text in a description) that 1.0 forbids.
"""

import re
import xml.etree.ElementTree as ET

from fastmcp.exceptions import ToolError

_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>")
_CHAR_REF = re.compile(r"&#(x[0-9a-fA-F]+|\d+);")


def _keep_xml10_ref(m: re.Match[str]) -> str:
    ref = m.group(1)
    code = int(ref[1:], 16) if ref.startswith("x") else int(ref)
    return "" if code < 0x20 and code not in (0x9, 0xA, 0xD) else m.group(0)


def parse_xml(text: str) -> ET.Element:
    text = _DECLARATION.sub("", text.lstrip("﻿"), count=1)
    try:
        return ET.fromstring(_CHAR_REF.sub(_keep_xml10_ref, text))
    except ET.ParseError as e:
        raise ToolError(f"Cannot parse the XML returned by Jenkins: {e}") from None


def text_of(el: ET.Element | None, path: str) -> str:
    """Stripped text of the first element at `path` below `el`, '' if missing."""
    if el is None:
        return ""
    found = el.find(path)
    return (found.text or "").strip() if found is not None else ""
