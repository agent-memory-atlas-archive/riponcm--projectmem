"""Auto-capture must tag a commit with the code it touched, not with memory.

#20: `location` was `files[0]`. git lists changed paths sorted, `.projectmem/`
sorts ahead of most paths because of the leading dot, and summary.md is
regenerated on every event — so most commits were located at
`.projectmem/summary.md`. The staleness check then counted every later commit
to summary.md against them and flagged nearly all as possibly stale.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from projectmem.commands.auto_capture import _capture_commit
from projectmem.storage import initialize, read_events


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    initialize(tmp_path)
    # A first commit, so the commits under test have a parent: `git diff-tree`
    # lists no files for a root commit.
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "initial")
    return tmp_path


def _commit(root: Path, message: str, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _git(root, "add", rel)
    _git(root, "commit", "-q", "-m", message)


def test_location_skips_projectmem_files(tmp_path):
    root = _repo(tmp_path)
    _commit(root, "fix: guard the login submit", {
        ".projectmem/summary.md": "# regenerated\n",
        "src/login.py": "print('login')\n",
    })

    _capture_commit(root)

    event = read_events(root)[-1]
    assert event.type == "fix"
    assert event.location == "src/login.py"
    # Only the location moves; the event still records every file it touched.
    assert ".projectmem/summary.md" in event.files


def test_a_memory_only_commit_has_no_location(tmp_path):
    """Nothing to point at is better than pointing at summary.md."""
    root = _repo(tmp_path)
    _commit(root, "fix: tidy the summary", {
        ".projectmem/summary.md": "# regenerated\n",
    })

    _capture_commit(root)

    event = read_events(root)[-1]
    assert event.type == "fix"
    assert event.location is None
