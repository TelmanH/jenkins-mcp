"""Unit tests: console log cleaning, stage detection, rendering, error grouping and log download/cache."""

import os

import pytest
import responses

from jenkins_mcp.client import Instance
from jenkins_mcp.logs import cache
from jenkins_mcp.logs.errors import find_errors, root_cause_candidates
from jenkins_mcp.logs.parse import ParsedLog, clean_line, meaningful_tail, render_collapsed, strip_ts
from jenkins_mcp.logs.stages import pick_failed_stage, stage_str
from jenkins_mcp.targets import Target


def log(*lines: str) -> ParsedLog:
    cleaned = [clean_line(x) for x in lines]
    return ParsedLog(cleaned, sum(len(x) for x in cleaned), False)


# ----------------------------------------------------------------------------- cleaning
@pytest.mark.parametrize("raw, expected", [
    ("\x1b[8mha:////4FAKE+/=\x1b[0m[Pipeline] stage", "[Pipeline] stage"),       # hidden console note
    ("\x1b[1;34mINFO\x1b[0m done", "INFO done"),                                   # ANSI colours
    ("progress 10%\r progress 50%\r progress 100%", " progress 100%"),            # progress bar: last state
    ("windows line\r", "windows line"),                                           # CRLF
    ("trailing   ", "trailing"),
])
def test_clean_line(raw, expected):
    assert clean_line(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("[2026-10-20T08:00:00.000Z] hello", "hello"),
    ("16:32:03 hello", "hello"),
    ("08:00:01 \tat a.B(C.java:1)", "\tat a.B(C.java:1)"),    # only ONE separator space is removed
    ("no timestamp", "no timestamp"),
])
def test_strip_ts(raw, expected):
    assert strip_ts(raw) == expected


# ----------------------------------------------------------------------------- stages
PIPELINE = (
    "Started by user Test",
    "Running on agent-1 in /ws/app",
    "[Pipeline] {",
    "[Pipeline] stage",
    "[Pipeline] { (Build)",
    "compile ok",
    "[Pipeline] }",
    "[Pipeline] stage",
    "[Pipeline] { (Deploy)",
    "[Pipeline] parallel",
    "[Pipeline] { (Branch: eu)",
    "deploy eu",
    "[Pipeline] }",
    "[Pipeline] { (Branch: us)",
    "Running on agent-2 in /ws/app",
    "deploy us",
    "[Pipeline] }",
    "[Pipeline] }",
    "[Pipeline] }",
)


def test_stages_nested_and_parallel():
    pl = log(*PIPELINE)
    assert pl.stage_of[5] == "Build"
    assert pl.stage_of[11] == "Deploy > eu" and pl.stage_of[15] == "Deploy > us"
    assert list(pl.spans) == ["Build", "Deploy", "Deploy > eu", "Deploy > us"]
    assert pl.stage_range("Build") == (4, 6)
    assert pl.stage_range("eu") == (10, 12), "a parallel branch is found by its own name"
    assert pl.stage_range("Deploy") == (8, 17), "a stage covers its branches"
    assert pl.stage_range("deplo") == (8, 17), "case-insensitive substring fallback"
    assert pl.stage_range("nope") is None


def test_nodes_in_order_of_appearance():
    assert log(*PIPELINE).nodes == ["agent-1", "agent-2"]


def test_pick_failed_stage_prefers_status_then_errors():
    pl = log(*PIPELINE)
    stages = [{"name": "Build", "status": "SUCCESS"}, {"name": "Deploy", "status": "FAILED"}]
    assert pick_failed_stage(stages, pl) == "Deploy"
    assert pick_failed_stage([{"name": "A", "status": "UNSTABLE"}, {"name": "B", "status": "ABORTED"}], pl) == "A"
    failing = log("[Pipeline] { (Package)", "Error response from daemon: no space left on device", "[Pipeline] }")
    assert pick_failed_stage([{"name": "Package", "status": "?"}], failing) == "Package", "fallback: stage of first error"


def test_stage_str():
    s = {"name": "Deploy", "status": "FAILED", "duration": "4m 2s", "lines": "10-20", "error": "AbortException: x"}
    assert stage_str(s) == "Deploy: FAILED 4m 2s [L10-20] - AbortException: x"


# ----------------------------------------------------------------------------- rendering
def test_render_collapsed_folds_repetitive_lines():
    pl = log("start", *[f"Downloading lib{i}.jar ({i} kB)" for i in range(1, 6)], "end")
    rows = render_collapsed(pl, list(range(len(pl.lines))))
    assert len(rows) == 4
    assert "Downloading lib1.jar" in rows[1]
    assert rows[2].strip() == "... x4 similar lines (L3-L6)"


def test_render_keeps_short_runs():
    pl = log("a 1", "a 2", "b")
    assert len(render_collapsed(pl, [0, 1, 2])) == 3, "runs shorter than 3 are not folded"


def test_meaningful_tail_skips_noise_and_masks():
    pl = log("real 1", "[Pipeline] }", "", "password=hunter2", "[Pipeline] // stage", "Finished: FAILURE")
    rows = meaningful_tail(pl, 3)
    assert [r.split("| ", 1)[1] for r in rows] == ["real 1", "password=****", "Finished: FAILURE"]
    assert rows[0].lstrip().startswith("1|"), "original line numbers are kept"


# ----------------------------------------------------------------------------- error detection
ERROR_LOG = (
    "[Pipeline] { (Build)",
    "java.lang.IllegalStateException: boom",
    *[f"\tat a.b.C{i}.run(C.java:{i})" for i in range(6)],
    "Connection refused to host 10.0.0.1",
    "Connection refused to host 10.0.0.2",
    "WARN: java.net.SocketTimeoutException: Read timed out",
    "[INFO] Tests run: 5, Failures: 0, Errors: 0",
    "+ echo ERROR something",
    "[Pipeline] }",
    "[Pipeline] { (Deploy)",
    "Error: UPGRADE FAILED: timed out waiting for the condition",
    "[Pipeline] }",
    "ERROR: script returned exit code 1",
)


@pytest.fixture(scope="module")
def groups():
    return find_errors(log(*ERROR_LOG))


def test_find_errors_categories_and_severity(groups):
    by_text = {g.text: g for g in groups}
    assert by_text["java.lang.IllegalStateException: boom"].category == "java"
    assert by_text["WARN: java.net.SocketTimeoutException: Read timed out"].severity == "warn", \
        "an error logged as WARN is downgraded"
    assert by_text["ERROR: script returned exit code 1"].severity == "low", "generic Jenkins wrapper"
    assert by_text["Error: UPGRADE FAILED: timed out waiting for the condition"].category == "helm"


def test_find_errors_groups_lines_that_differ_only_in_numbers(groups):
    net = [g for g in groups if g.category == "network"]
    assert len(net) == 1 and net[0].count == 2 and (net[0].first_line, net[0].last_line) == (9, 10)


def test_find_errors_shortens_stack_traces(groups):
    g = next(g for g in groups if "IllegalState" in g.text)
    assert len(g.stack) == 4 and g.stack_more == 2 and g.stage == "Build"
    assert "+2 more stack lines" in g.render()


def test_find_errors_skips_ignored_and_shell_echo(groups):
    assert not any("Tests run" in g.text or "echo ERROR" in g.text for g in groups)


def test_root_cause_candidates(groups):
    first = root_cause_candidates(groups, 1)[0]
    assert "IllegalState" in first.text, "high severity, in order of appearance"
    in_stage = root_cause_candidates(groups, 1, stage="Deploy")[0]
    assert "UPGRADE FAILED" in in_stage.text, "errors inside the failed stage come first"
    excluded = root_cause_candidates(groups, 5, exclude={first.key})
    assert first not in excluded
    assert all(g.severity == "high" for g in root_cause_candidates(groups, 10))


# ----------------------------------------------------------------------------- download + cache
BIG = "https://big.example.com"


def _target(name: str) -> Target:
    return Target(Instance(name=name, url=BIG, user="", token="", user_env="U", token_env="T"), ["job"], "1")


def test_download_truncates_huge_logs_in_the_middle(monkeypatch):
    monkeypatch.setattr(cache, "MAX_LOG_MB", 1)
    body = "".join(f"line {i:07d} {'x' * 40}\n" for i in range(60_000))   # ~3 MB
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, f"{BIG}/job/job/1/consoleText", body=body)
        raw, truncated = cache._download_log(_target("big"), 1)
    text = raw.decode()
    assert truncated and len(raw) < 1.1 * 1024 * 1024
    assert text.startswith("line 0000000") and text.rstrip().endswith(f"line 0059999 {'x' * 40}")
    assert "[jenkins-mcp: log larger than 1 MB" in text


def test_load_log_caches_finished_builds_only():
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, f"{BIG}/job/job/7/consoleText", body="a\nb\n")
        t = _target("cachetest")
        first = cache.load_log(t, {"number": 7, "building": False})
        second = cache.load_log(t, {"number": 7, "building": False})
        assert first is second and len(rsps.calls) == 1 and first.lines == ["a", "b"]
    if os.name != "nt":
        assert (cache.CACHE_DIR / "cachetest").stat().st_mode & 0o077 == 0, "cache folder only readable by the owner"


# ----------------------------------------------------------------------------- generic catch-all rule
@pytest.mark.parametrize("line", [
    "Error: deploy target unreachable",
    "error: pathspec 'main2' did not match any file(s) known to git",
    "fatal: repository 'https://git.example.com/x.git/' not found",
    "FATAL: missing credentials for registry",
    "error[E0308]: mismatched types",
    "Error - could not lock state file",
    "  Error: indented output of a tool",
    "npm ERR! code ERESOLVE",
    "FAILURE: Build failed with an exception.",
    "BUILD FAILED in 12s",
])
def test_generic_error_lines_are_detected_as_medium(line):
    groups = find_errors(log(line))
    assert [(g.category, g.severity) for g in groups] == [("generic", "medium")], line


@pytest.mark.parametrize("line", [
    "no error found",
    "errors: 0",                                    # 'errors' is not 'error'
    "Errors were checked, everything fine",
    "terror: a word that contains error",
    "Error:",                                       # nothing after the colon
    "+ echo Error: from a shell trace",             # shell trace lines are skipped
])
def test_generic_rule_ignores_prose(line):
    assert find_errors(log(line)) == []


@pytest.mark.parametrize("line, category, severity", [
    ("Error: UPGRADE FAILED: timed out", "helm", "high"),                 # specific rules win
    ("error: unable to recognize deployment.yaml", "k8s", "high"),
    ("ERROR: script returned exit code 1", "jenkins", "low"),           # Jenkins' own wrapper stays low
    ("WARNING: Error: retrying in 5s", None, None),                      # does not start with error
    ("WARN Error: cache miss", None, None),
])
def test_generic_rule_does_not_steal_specific_matches(line, category, severity):
    groups = find_errors(log(line))
    assert [(g.category, g.severity) for g in groups] == ([(category, severity)] if category else [])


def test_ranking_high_beats_medium_beats_warn_and_low():
    groups = find_errors(log(
        "ERROR: script returned exit code 1",                    # low
        "WARN: java.net.SocketTimeoutException: Read timed out",  # warn
        "Error: deploy target unreachable",                      # medium
        "Permission denied (publickey)",                         # high
    ))
    order = [g.severity for g in root_cause_candidates(groups, 4)]
    assert order == ["high"], "only the most severe tier is returned"
    without_high = [g for g in groups if g.severity != "high"]
    assert [g.severity for g in root_cause_candidates(without_high, 4)] == ["medium"]
    only_warn_low = [g for g in groups if g.severity in ("warn", "low")]
    assert [g.severity for g in root_cause_candidates(only_warn_low, 4)] == ["warn"]


# ----------------------------------------------------------------------------- pipeline facts from the log
def test_jenkinsfile_source_and_libraries():
    from jenkins_mcp.logs.pipeline import jenkinsfile_sources, libraries
    pl = log("[2026-10-20T08:00:00.000Z] Obtained Jenkinsfile from git https://git.example.com/a.git",
             "Loading library shared-lib@rel-2.2",
             "Attempting to resolve rel-2.2 from remote references...",
             "Found match: refs/heads/rel-2.2 revision 1111111111aaaaaaaaaa2222222222bbbbbbbbbb",
             "Loading library other@1.0",
             "[Pipeline] stage",
             "Checking out Revision 3333333333cccccccccc4444444444dddddddddd (main)",  # app checkout, not the lib
             "Obtained Jenkinsfile from 5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f")   # multibranch: a commit
    assert jenkinsfile_sources(pl) == ["Jenkinsfile from git https://git.example.com/a.git",
                                       "Jenkinsfile from 5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f"]
    assert libraries(pl) == ["shared-lib@rel-2.2 (revision 11111111)", "other@1.0"], \
        "revision taken only from the lines that belong to the library"


def test_library_version_mismatch_hint():
    from jenkins_mcp.logs.pipeline import version_hints
    params = {"REL_VERSION": "2.4", "TARGET_ENV": "test"}
    assert version_hints(["shared-lib@rel-2.3 (revision 9f8e7d6c)"], params) == [
        "library shared-lib@rel-2.3 is version 2.3, but parameter REL_VERSION=2.4 - is the right library branch loaded?"]
    assert version_hints(["shared-lib@rel-2.4"], params) == []
    assert version_hints(["shared-lib@master"], params) == [], "unversioned library: no guess"
    assert version_hints(["shared-lib@rel-2.3"], {"TARGET_ENV": "x"}) == [], "no version parameter: no guess"


# ----------------------------------------------------------------------------- running builds, truncated cache, encoding
def test_running_build_fetches_only_new_log_text():
    url = f"{BIG}/job/job/8/logText/progressiveText"
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, url, body="a\nb\n", headers={"X-Text-Size": "4", "X-More-Data": "true"},
                 match=[responses.matchers.query_param_matcher({"start": "0"})])
        rsps.add(responses.GET, url, body="c\n", headers={"X-Text-Size": "6", "X-More-Data": "true"},
                 match=[responses.matchers.query_param_matcher({"start": "4"})])
        t = _target("running")
        assert cache.load_log(t, {"number": 8, "building": True}).lines == ["a", "b"]
        assert cache.load_log(t, {"number": 8, "building": True}).lines == ["a", "b", "c"], "new text appended"
        assert [c.request.params["start"] for c in rsps.calls] == ["0", "4"], "only the new part is downloaded"


def test_running_build_log_restarted_from_scratch_when_it_shrinks():
    url = f"{BIG}/job/job/9/logText/progressiveText"
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, url, body="old line\n", headers={"X-Text-Size": "9"},
                 match=[responses.matchers.query_param_matcher({"start": "0"})])
        t = _target("shrink")
        cache.load_log(t, {"number": 9, "building": True})
        rsps.replace(responses.GET, url, body="", headers={"X-Text-Size": "3"},
                     match=[responses.matchers.query_param_matcher({"start": "9"})])
        rsps.add(responses.GET, url, body="new\n", headers={"X-Text-Size": "4"},
                 match=[responses.matchers.query_param_matcher({"start": "0"})])
        assert cache.load_log(t, {"number": 9, "building": True}).lines == ["new"]


def test_truncated_logs_are_cached_too(monkeypatch):
    monkeypatch.setattr(cache, "MAX_LOG_MB", 1)
    body = "".join(f"line {i:07d} {'x' * 40}\n" for i in range(60_000))
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, f"{BIG}/job/job/3/consoleText", body=body)
        t = _target("bigcache")
        first = cache.load_log(t, {"number": 3, "building": False})
        cache._LRU.clear()  # force the disk cache
        second = cache.load_log(t, {"number": 3, "building": False})
        assert len(rsps.calls) == 1, "a truncated log is not downloaded again"
        assert first.truncated and second.truncated, "the truncated flag survives the disk cache"


def test_log_encoding_mostly_utf8_with_a_stray_byte():
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, f"{BIG}/job/job/4/consoleText", body="Größe ok\n".encode() + b"bad \xfc byte\n")
        rsps.add(responses.GET, f"{BIG}/job/job/5/consoleText", body="Fehler: Übersetzung\n".encode("cp1252"))
        t = _target("enc")
        assert cache.load_log(t, {"number": 4, "building": False}).lines[0] == "Größe ok", "UTF-8 kept"
        assert cache.load_log(t, {"number": 5, "building": False}).lines[0] == "Fehler: Übersetzung", "cp1252 log"


# ----------------------------------------------------------------------------- downstream builds
def test_downstream_builds_from_the_log():
    from jenkins_mcp.logs.pipeline import downstream_builds
    pl = log("Scheduling project: cd » master » app-deploy",
             "[2026-10-20T08:00:00.000Z] Starting building: cd » master » app-deploy #42",
             "Starting building: Legacy Name #3",
             "Starting building: cd » master » app-deploy #42")
    assert downstream_builds(pl) == [("cd/master/app-deploy", 42), ("Legacy Name", 3)]
