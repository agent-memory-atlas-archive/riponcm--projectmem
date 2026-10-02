"""Staleness must judge the same history the same way on every machine.

Timezone (0.3.3 regression, commit 306df47): ``commit_times`` returned git's
``%cI`` strings, which carry the committer's offset, and ``find_stale_events``
compared them as strings against Zulu event timestamps. Five events followed by
4,3,2,1,0 later commits at threshold 3 flagged 0 under America/Denver, 2 under
UTC (correct) and 5 under Asia/Dhaka.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from projectmem.models import Event
from projectmem.staleness import (
    commit_times,
    commits_touching_since,
    find_stale_events,
)


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
