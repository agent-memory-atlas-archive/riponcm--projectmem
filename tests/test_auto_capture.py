"""Auto-capture must tag a commit with the code it touched, not with memory.

#20: `location` was `files[0]`. git lists changed paths sorted, `.projectmem/`
sorts ahead of most paths because of the leading dot, and summary.md is
regenerated on every event — so most commits were located at
`.projectmem/summary.md`. The staleness check then counted every later commit
to summary.md against them and flagged nearly all as possibly stale.

0.3.4: `files[0]` after that filter was still git's first path, which is the
alphabetically first one — a deleted file, the old half of a rename, an
uppercase `README.md`, a `.github/` workflow — none of which is where the
commit's code lives.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from projectmem.commands.auto_capture import _capture_commit
from projectmem.storage import initialize, read_events


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repo(tmp_path: Path, initial_commit: bool = True) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    initialize(tmp_path)
    if initial_commit:
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


def _last_event(root: Path):
    _capture_commit(root)
    return read_events(root)[-1]


def test_a_deleted_file_is_never_the_location(tmp_path):
    """`src/aaa_old.py` sorted first and was deleted — so every later precheck
    flagged the event as citing a file that no longer exists."""
    root = _repo(tmp_path)
    _commit(root, "add: old module", {"src/aaa_old.py": "old\n"})
    (root / "src" / "aaa_old.py").unlink()
    _git(root, "add", "-A")
    _commit(root, "fix: guard the login submit", {"src/login.py": "print('login')\n"})

    event = _last_event(root)

    assert event.location == "src/login.py"
    # The deletion is still part of the record of what the commit touched.
    assert "src/aaa_old.py" in event.files


def test_a_rename_is_located_at_the_new_path(tmp_path):
    root = _repo(tmp_path)
    _commit(root, "add: handler", {"src/old_name.py": "x = 1\n"})
    _git(root, "mv", "src/old_name.py", "src/new_name.py")
    _git(root, "commit", "-q", "-m", "fix: name the handler for what it does")

    event = _last_event(root)

    assert event.location == "src/new_name.py"
    assert event.files == ["src/new_name.py"]


def test_the_repos_first_commit_lists_its_files(tmp_path):
    """`git diff-tree HEAD` prints nothing for a root commit without `--root`."""
    root = _repo(tmp_path, initial_commit=False)
    _commit(root, "fix: first cut of the app", {"src/app.py": "print('hi')\n"})

    event = _last_event(root)

    assert event.location == "src/app.py"
    assert "src/app.py" in event.files


def test_a_non_ascii_path_is_stored_as_written(tmp_path):
    """Without `-z` git C-quotes the path: `"src/caf\\303\\251.py"`, which
    matches no file on disk, so the event read as citing a deleted file."""
    root = _repo(tmp_path)
    _commit(root, "fix: accent handling", {"src/café.py": "x = 1\n"})

    event = _last_event(root)

    assert event.location == "src/café.py"
    assert event.files == ["src/café.py"]


def test_code_outranks_docs_and_ci_config(tmp_path):
    root = _repo(tmp_path)
    _commit(root, "fix: validate the token before use", {
        ".github/workflows/ci.yml": "on: push\n",
        "README.md": "# app\n",
        "src/app.py": "print('hi')\n",
    })

    event = _last_event(root)

    assert event.location == "src/app.py"
    assert set(event.files) == {".github/workflows/ci.yml", "README.md", "src/app.py"}


def test_docs_outrank_lockfiles_and_a_tie_keeps_git_order(tmp_path):
    root = _repo(tmp_path)
    _commit(root, "fix: document the upgrade path", {
        "Cargo.lock": "[[package]]\n",   # uppercase: git lists it first
        "docs/upgrade.md": "# upgrade\n",
    })
    assert _last_event(root).location == "docs/upgrade.md"

    _commit(root, "fix: both handlers", {
        "src/a.py": "a = 1\n",
        "src/b.py": "b = 1\n",
    })
    assert _last_event(root).location == "src/a.py"
