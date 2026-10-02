"""Staleness must judge the same history the same way on every machine, and
must not judge projectmem's own files or its own commit records.

Timezone (0.3.3 regression, commit 306df47): ``commit_times`` returned git's
``%cI`` strings, which carry the committer's offset, and ``find_stale_events``
compared them as strings against Zulu event timestamps. Five events followed by
4,3,2,1,0 later commits at threshold 3 flagged 0 under America/Denver, 2 under
UTC (correct) and 5 under Asia/Dhaka.

Repair: before #20 auto-capture located most commits at
``.projectmem/summary.md``, and the log is append-only, so those events are
still in every existing user's log. Rather than rewrite history, the read side
stops asking about memory files and about auto-captured commit records.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from projectmem.cli import app
from projectmem.commands.precheck import _analyze_files
from projectmem.models import Event
from projectmem.staleness import (
    commit_times,
    commits_touching_since,
    find_stale_events,
    is_memory_path,
    location_file,
)
from projectmem.storage import initialize

runner = CliRunner()


def _git(repo: Path, *args: str, when: str | None = None) -> str:
    env = None
    if when:
        env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True, env=env).stdout


def _touch(repo: Path, rel: str, text: str, message: str, when: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-qm", message, when=when)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _touch(r, "src/f.py", "v0\n", "init", "2026-01-01T10:00:00Z")
    return r


def _event(eid: str, ts: str, loc: str, etype: str = "decision", **kw) -> Event:
    return Event(id=eid, type=etype, summary=f"claim {eid}", timestamp=ts,
                 location=loc, **kw)


# ── Timezone ────────────────────────────────────────────────────────────────

# The event is logged at noon UTC. Two commits land before it, written by a
# committer at +06:00 whose wall clock reads 15:00 and 16:00 — digits that sort
# AFTER "12:00". Three land after it, written at -06:00 where the clock reads
# 07:00-09:00 — digits that sort BEFORE "12:00". A string comparison gets every
# one of them wrong, in both directions.
EVENT_AT = "2026-05-01T12:00:00Z"
BEFORE = ("2026-05-01T15:00:00+06:00", "2026-05-01T16:00:00+06:00")   # 09Z, 10Z
AFTER = ("2026-05-01T07:00:00-06:00", "2026-05-01T08:00:00-06:00",   # 13Z, 14Z
         "2026-05-01T09:00:00-06:00")                                 # 15Z


@pytest.fixture
def offset_repo(repo: Path) -> Path:
    for i, when in enumerate(BEFORE + AFTER, start=1):
        _touch(repo, "src/f.py", f"v{i}\n", f"edit {i}", when=when)
    return repo


@pytest.mark.parametrize("tz", ["UTC", "America/Denver", "Asia/Dhaka"])
def test_commit_counts_do_not_depend_on_committer_or_machine_offsets(
    offset_repo, monkeypatch, tz
):
    monkeypatch.setenv("TZ", tz)

    assert commits_touching_since("src/f.py", EVENT_AT, offset_repo) == len(AFTER)

    flagged = find_stale_events([_event("d", EVENT_AT, "src/f.py")], offset_repo)
    assert [(x["file"], x["commits_since"]) for x in flagged] == [("src/f.py", 3)]


def test_commit_times_are_canonical_zulu(offset_repo):
    """So that every caller can keep comparing them as strings."""
    times = commit_times("src/f.py", offset_repo)
    assert times is not None
    assert all(t.endswith("Z") and "+" not in t for t in times)
    assert times[:3] == ["2026-05-01T15:00:00Z", "2026-05-01T14:00:00Z",
                         "2026-05-01T13:00:00Z"]


# ── Memory files are never judged ───────────────────────────────────────────

def test_an_old_event_located_at_summary_md_is_not_flagged(repo):
    """The fixture is what pre-#20 auto-capture wrote, and what every existing
    log still contains. Whether the file is tracked and churning or was never
    committed at all, there is nothing to judge."""
    initialize(repo)
    for i in range(4):
        _touch(repo, ".projectmem/summary.md", f"# v{i}\n", f"regen {i}",
               when=f"2026-02-0{i + 1}T10:00:00Z")
    tracked = _event("old", "2026-01-15T10:00:00Z", ".projectmem/summary.md",
                     etype="fix")
    never_committed = _event("gone", "2026-01-15T10:00:00Z",
                             ".projectmem/issues/0001-login.md")

    assert location_file(tracked) is None
    assert find_stale_events([tracked, never_committed], repo) == []


def test_precheck_skips_a_staged_summary_md(repo, monkeypatch):
    """Staging the regenerated summary reported HIGH CHURN about the memory
    layer's own write pattern. Nothing projectmem writes is up for review."""
    initialize(repo)
    # Dated inside precheck's 30-day churn window, so the old code did warn.
    for i in range(5):
        when = (datetime.now(timezone.utc) - timedelta(days=5 - i)).isoformat()
        _touch(repo, ".projectmem/summary.md", f"# v{i}\n", f"regen {i}", when=when)
    events = [Event(type="fix", summary=f"regen {i}", git_commit=f"c{i}",
                    files=[".projectmem/summary.md"]) for i in range(5)]

    assert _analyze_files([".projectmem/summary.md"], events, root=repo) == []
    # However the path is spelled, including the absolute form the MCP
    # precheck_file tool accepts.
    for spelled in ("./.projectmem/summary.md", str(repo / ".projectmem/summary.md"),
                    ".projectmem\\summary.md"):
        assert _analyze_files([spelled], events, root=repo) == []

    (repo / ".projectmem" / "summary.md").write_text("# staged\n", encoding="utf-8")
    _git(repo, "add", ".projectmem/summary.md")
    monkeypatch.chdir(repo)
    result = runner.invoke(app, ["precheck"], catch_exceptions=False)
    assert "HIGH CHURN" not in result.output
    assert "no warnings" in result.output


# ── Auto-captured commit records are facts, not claims ─────────────────────

@pytest.fixture
def worked_on_repo(repo: Path) -> Path:
    """A file committed three more times after an event was logged about it."""
    for i in range(1, 4):
        _touch(repo, "src/f.py", f"v{i}\n", f"edit {i}", when=f"2026-03-0{i}T10:00:00Z")
    return repo


@pytest.mark.parametrize("etype", ["fix", "note"])
def test_an_auto_captured_fix_or_note_is_not_flagged_as_stale(worked_on_repo, etype):
    captured = _event("auto", "2026-02-01T10:00:00Z", "src/f.py", etype=etype,
                      auto_captured=True, capture_source="git_post_commit",
                      git_commit="abc1234")

    assert find_stale_events([captured], worked_on_repo) == []


def test_an_auto_captured_decision_is_still_a_claim_and_is_flagged(worked_on_repo):
    """"Breaking change:" / "Refactor:" assert the file's shape; three more
    commits can make that wrong exactly like a hand-written decision."""
    captured = _event("auto", "2026-02-01T10:00:00Z", "src/f.py", etype="decision",
                      auto_captured=True, capture_source="git_post_commit",
                      git_commit="abc1234")

    flagged = find_stale_events([captured], worked_on_repo)
    assert [(x["event"].id, x["commits_since"]) for x in flagged] == [("auto", 3)]


# ── One root-anchored rule for "is this a memory file" ──────────────────────

def test_memory_path_rule_is_anchored_at_the_project_root(tmp_path):
    root = tmp_path / "proj"
    (root / ".projectmem").mkdir(parents=True)
    ours = [".projectmem/summary.md", "./.projectmem/summary.md",
            ".projectmem\\issues\\0001.md", str(root / ".projectmem" / "summary.md")]
    not_ours = ["sub/.projectmem/x.md", "tests/fixtures/.projectmem/events.jsonl",
                "src/app.py", str(tmp_path / "other" / ".projectmem" / "summary.md")]

    assert all(is_memory_path(p, root) for p in ours), ours
    assert not any(is_memory_path(p, root) for p in not_ours), not_ours


def test_precheck_and_staleness_agree_on_a_nested_projectmem_dir(repo):
    """A fixture directory named `.projectmem` is an ordinary file to both."""
    nested = "tests/fixtures/.projectmem/events.jsonl"
    _touch(repo, nested, "{}\n", "add fixture", when="2026-02-01T10:00:00Z")
    events = [Event(type="attempt", summary="tried hand-editing the fixture",
                    outcome="failed", location=nested)]

    assert location_file(_event("e", "2026-01-01T10:00:00Z", nested), repo) == nested
    warnings = _analyze_files([nested], events, root=repo)
    assert [w["type"] for w in warnings] == ["failed_attempts"]


def test_a_manual_decision_in_the_same_spot_is_still_flagged(worked_on_repo):
    """The repair must not switch the feature off."""
    manual = _event("human", "2026-02-01T10:00:00Z", "src/f.py")

    flagged = find_stale_events([manual], worked_on_repo)
    assert [(x["event"].id, x["commits_since"]) for x in flagged] == [("human", 3)]
