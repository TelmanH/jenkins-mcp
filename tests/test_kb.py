"""Unit tests: knowledge base matching on a real-world style log, config validation, read-only guard."""

from pathlib import Path

import pytest

from jenkins_mcp import config
from jenkins_mcp.kb import match as kbm
from jenkins_mcp.kb import store
from jenkins_mcp.logs.parse import ParsedLog, clean_line
from jenkins_mcp.tools.builds import ranked_causes
from jenkins_mcp.tools.knowledge import _slug

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def db_log() -> ParsedLog:
    raw = (FIXTURES / "db_migration_failure.log").read_text(encoding="utf-8")
    return ParsedLog([clean_line(x) for x in raw.splitlines()], len(raw), False)


def test_db_log_matches_kb_0001(db_log):
    a = kbm.analyse(db_log, "main", "deploy/app-install")
    assert [h.entry.id for h in a.hits] == ["KB-0001"]
    causes = ranked_causes(db_log.groups, a, "Deploy Application")
    assert "Unpack failed" in causes[0].text or "drop/create" in causes[0].text, "DB/installer error ranked first"
    assert not any("cygpath" in g.text or "WFLYPRT0053" in g.text for g in causes), \
        "tool warnings and app server error are not root causes"
    assert "known noise (KB-0001)" in a.notes.values(), "cygpath marked known noise"
    assert "consequence of KB-0001" in a.notes.values(), "app server error marked consequence"


def test_kb_entry_scoped_to_instance(db_log):
    assert kbm.analyse(db_log, "alpha", "deploy/x").hits == []


def test_solution_hint_from_english_heading():
    entry, problems = store.parse_file(FIXTURES / "knowledge" / "KB-0001-db-migration-failure.md", False)
    assert entry is not None and not problems
    assert entry.solution_hint().startswith("Check `/home/deploy/work/app/logs/db/drop_create.log`")


def test_broken_rules_skipped_one_by_one():
    errs: list[str] = []
    rules = config.compile_rules([{"pattern": "abc"}, {"regex": "("}, {"category": "x"}, "oops",
                                  {"regex": "a", "severity": "fatal"}], "extra", errs)
    assert len(rules) == 1 and len(errs) == 4
    assert "extra[1]: invalid regex" in errs[0] and "extra[2]: missing 'regex'" in errs[1]


def test_read_only_session_blocks_post():
    with pytest.raises(Exception, match="read-only"):
        config.get_instance("alpha").session.post(config.get_instance("alpha").url + "/job/x/build")


def test_slug_is_ascii():
    assert _slug("Naïve café -> Résumé") == "naive-cafe-resume"


@pytest.mark.parametrize("folder", [PROJECT / "knowledge", FIXTURES / "knowledge"])
def test_knowledge_folders_parse_cleanly(folder: Path):
    files = sorted(folder.glob("KB-*.md"))
    for f in files:
        entry, problems = store.parse_file(f, False)
        assert entry is not None and not problems, f"{f.name}: {problems}"
    assert not store._ID.match("TEMPLATE.md"), "the template is never loaded as an entry"


# ----------------------------------------------------------------------------- store helpers
def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


@pytest.mark.parametrize("text, problem", [
    ("no front matter", "must start with a '---'"),
    ("---\nid: KB-1\n", "not closed"),
    ("---\n- a list\n---\n", "must be a mapping"),
    ("---\nid: KB-0005\nmatch: ['(']\n---\n", "invalid regex"),
    ("---\nid: KB-0006\ntitle: t\n---\n", "neither 'match' nor 'red_herrings'"),
])
def test_parse_file_reports_problems(tmp_path, text, problem):
    _, problems = store.parse_file(_write(tmp_path, "KB-0009-x.md", text), False)
    assert any(problem in p for p in problems), problems


def test_parse_file_id_from_name_and_lists(tmp_path):
    e, problems = store.parse_file(_write(tmp_path, "KB-0042-x.md", "---\nmatch: 'one line'\ninstance: [a, B]\n---\nbody"), False)
    assert not problems and e.id == "KB-0042" and e.instances == ["a", "B"] and e.body == "body"
    assert store.as_list(None) == [] and store.as_list("x") == ["x"] and store.as_list(["a", " "]) == ["a"]


def test_in_scope_instance_and_job_glob(tmp_path):
    e, _ = store.parse_file(_write(tmp_path, "KB-0001-x.md",
                                   "---\nmatch: 'abc def'\ninstance: Main\njob: deploy/*\n---\n"), False)
    assert e.in_scope("main", "deploy/eu/app-install"), "case-insensitive; * spans folders"
    assert not e.in_scope("other", "deploy/x")
    assert not e.in_scope("main", "build/x")
    assert e.in_scope("", ""), "unknown instance/job is not a filter"


def test_section_and_solution_hint_skip_comments(tmp_path):
    e, _ = store.parse_file(_write(tmp_path, "KB-0002-x.md", "---\nmatch: 'abc def'\n---\n## Symptoms\ns\n"
                                   "## Solution\n<!-- TODO -->\n- 1. Restart the agent\n## How it was found\nh\n"), False)
    assert e.section("symptoms") == "s"
    assert e.solution_hint() == "Restart the agent"


def test_next_id_counts_entries_and_drafts(tmp_path):
    (tmp_path / "drafts").mkdir()
    _write(tmp_path, "KB-0003-a.md", "x")
    _write(tmp_path / "drafts", "KB-0010-b.md", "x")
    kb = store.KnowledgeBase(tmp_path, [], [], [], ())
    assert kb.next_id() == "KB-0011"


def test_match_text_by_regex_then_keywords():
    hits = kbm.match_text("InstallerException: Unpack failed (xml line 53)")
    assert hits and hits[0].entry.id == "KB-0001" and hits[0].matched[0][0] == 0
    fuzzy = kbm.match_text("the installer aborted while unpacking, schema drop/create problem")
    assert fuzzy and fuzzy[0].entry.id == "KB-0001" and fuzzy[0].matched[0][1].startswith("keywords:")
    assert kbm.match_text("completely unrelated words xyz") == []
