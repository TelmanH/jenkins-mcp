"""Unit tests: src/jenkins_mcp/text.py - secret masking, output budget, regex safety, formatting."""

import time

import pytest

from jenkins_mcp.text import (budget, clip, fmt_age, fmt_duration, is_secret_name, mask, mask_env_text,
                              safe_regex, short_class, url_to_full_name)
from jenkins_mcp.xmlutil import parse_xml


@pytest.mark.parametrize("raw, expected", [
    ("Authorization: Bearer abc123def", "Authorization: Bearer ****"),
    ("authorization: basic dXNlcjpwYXNz", "authorization: basic ****"),
    ("password=hunter2 next", "password=**** next"),
    ("api-key: 0123456789", "api-key: ****"),
    ("clone https://user:s3cret@git.example.com/repo.git", "clone https://user:****@git.example.com/repo.git"),
    ("password=*** already masked", "password=*** already masked"),
    ("nothing secret here", "nothing secret here"),
    # Pipeline password parameter in an inline script (config.xml, replay page)
    ("password(name: 'NEXUS_PASSWORD', defaultValue: 'hunter2secret', description: 'x')",
     "password(name: 'NEXUS_PASSWORD', defaultValue: '****', description: 'x')"),
    ('password(defaultValue: "p@ss word", name: "PW")', 'password(defaultValue: "****", name: "PW")'),
])
def test_mask_inline_secrets(raw, expected):
    assert mask(raw) == expected


@pytest.mark.parametrize("name, secret", [
    ("NEXUS_TOKEN", True), ("DB_PASSWORD", True), ("db_passwd", True), ("AWS_SECRET_ACCESS_KEY", True),
    ("API_KEY", True), ("SESSION_COOKIE", True),
    ("PWD", False), ("OLDPWD", False), ("PATH", False), ("JAVA_HOME", False),
    ("GIT_AUTHOR_NAME", True),  # known over-masking: 'auth' matches; safe side, documented here
])
def test_is_secret_name(name, secret):
    assert is_secret_name(name) is secret


def test_mask_env_text():
    out = mask_env_text("API_KEY=abc\nEMPTY_TOKEN=\nPWD=/home/jenkins\nREPO=https://u:p@h.example.com/x\nPATH=/usr/bin")
    assert out == ["API_KEY=****", "EMPTY_TOKEN=", "PWD=/home/jenkins", "REPO=https://u:****@h.example.com/x",
                   "PATH=/usr/bin"]


def test_clip():
    assert clip("abcdef", 10) == "abcdef"
    assert clip("abcdef", 3) == "abc...[+3 chars]"
    assert clip(12345, 2) == "12...[+3 chars]"


def test_budget_cuts_at_line_end_and_explains():
    text = "\n".join(f"line {i:04d}" for i in range(1000))
    out = budget(text, 500)
    body, marker = out.rsplit("\n[output clipped", 1)
    assert len(body) <= 500 and body.endswith(("0", "1", "2", "3", "4", "5", "6", "7", "8", "9"))
    assert "narrow the request" in marker
    assert budget("short", 500) == "short"


def test_safe_regex_invalid_pattern_is_literal():
    rx = safe_regex("value(")
    assert rx.search("a value( here") and not rx.search("value")
    assert safe_regex(r"retry \d/3").search("retry 2/3")


@pytest.mark.parametrize("ms, expected", [(None, "0s"), (0, "0s"), (59_000, "59s"), (65_000, "1m 5s"),
                                          (3_660_000, "1h 1m")])
def test_fmt_duration(ms, expected):
    assert fmt_duration(ms) == expected


def test_fmt_age():
    now = time.time() * 1000
    assert fmt_age(None) == "?"
    assert fmt_age(now - 120_000) == "2m ago"
    assert fmt_age(now - 2 * 3_600_000) == "2h ago"
    assert fmt_age(now - 3 * 86_400_000) == "3d ago"
    assert fmt_age(now + 60_000) == "0m ago", "clock skew never gives negative ages"


def test_short_class_and_url_to_full_name():
    assert short_class("hudson.model.FreeStyleProject") == "FreeStyleProject"
    assert short_class("a.b.Outer$Inner") == "Inner"
    assert short_class(None) == ""
    assert url_to_full_name("https://j.example.com/job/a/job/b/12/") == "a/b"
    assert url_to_full_name("https://j.example.com/job/rel/job/release%2F2.4/") == "rel/release/2.4"


# ----------------------------------------------------------------------------- Jenkins XML
def test_xml_1_1_is_parsed():
    root = parse_xml("<?xml version='1.1' encoding='UTF-8'?>\n<slave><remoteFS>/w</remoteFS></slave>")
    assert root.findtext("remoteFS") == "/w"


@pytest.mark.parametrize("raw, expected", [
    ("<password>nexusPass1</password>", "<password>****</password>"),
    ("<server><privateKey>-----BEGIN</privateKey></server>", "<server><privateKey>****</privateKey></server>"),
    ("<passphrase>x y</passphrase><token>t</token>", "<passphrase>****</passphrase><token>****</token>"),
    ("<username>deployer</username>", "<username>deployer</username>"),
])
def test_mask_xml_elements(raw, expected):
    assert mask(raw) == expected


@pytest.mark.parametrize("data, text, encoding", [
    ("﻿äöü".encode("utf-16-le"), "äöü", "utf-16"),
    ("äöü".encode("utf-16-be")[0:0] + b"\xfe\xff" + "äöü".encode("utf-16-be"), "äöü", "utf-16"),
    ("﻿plain".encode("utf-8"), "plain", "utf-8"),
    ("Größe".encode("utf-8"), "Größe", "utf-8"),
    ("Übersetzung".encode("cp1252"), "Übersetzung", "cp1252"),
    ("Fehler: äöü ß".encode("cp1252"), "Fehler: äöü ß", "cp1252"),
    ("Größe ".encode("utf-8") + b"\xfc", "Größe \ufffd", "utf-8"),        # a stray byte does not switch to cp1252
])
def test_decode_text(data, text, encoding):
    from jenkins_mcp.text import decode_text
    assert decode_text(data) == (text, encoding)


def test_binary_detection():
    from jenkins_mcp.text import looks_binary
    assert looks_binary(b"PK\x03\x04\x00\x00")
    assert not looks_binary("﻿x".encode("utf-16-le")), "UTF-16 text has NUL bytes but is not binary"
    assert not looks_binary(b"plain text")
