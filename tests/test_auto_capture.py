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


# ── Review follow-ups: rank by what the file IS, with code as a positive test ──

def test_manifests_and_build_files_never_beat_source(tmp_path):
    """`Dockerfile`, `Makefile`, `package.json`, `pyproject.toml` all sort
    before `src/` and all used to win: nothing said they were not code."""
    root = _repo(tmp_path)
    for manifest, code in (
        ("package.json", "src/app.py"),
        ("Dockerfile", "src/app.py"),
        ("pyproject.toml", "src/app.py"),
        ("Makefile", "src/main.c"),
        ("requirements.txt", "src/app.py"),
        ("CMakeLists.txt", "src/main.c"),
    ):
        _commit(root, f"fix: {manifest}", {manifest: "x\n", code: f"# {manifest}\n"})
        assert _last_event(root).location == code, manifest


def test_first_commit_with_manifests_is_located_at_the_code(tmp_path):
    root = _repo(tmp_path, initial_commit=False)
    _commit(root, "fix: initial import", {
        "Dockerfile": "FROM x\n",
        "package.json": "{}\n",
        "pyproject.toml": "[project]\n",
        "src/app.py": "print('hi')\n",
    })

    assert _last_event(root).location == "src/app.py"


def test_source_outranks_tests_which_outrank_other_files(tmp_path):
    root = _repo(tmp_path)
    _commit(root, "fix: ui and its test", {
        "tests/test_app.py": "def test(): pass\n",
        "web/ui.js": "x\n",
    })
    assert _last_event(root).location == "web/ui.js"

    _commit(root, "fix: test and a data file", {
        "src/data/words.txt": "a\n",          # .txt is not a doc, not code
        "tests/test_words.py": "def test(): pass\n",
    })
    assert _last_event(root).location == "tests/test_words.py"

    _commit(root, "fix: sphinx config", {
        "docs/conf.py": "x = 1\n",            # code, even inside docs/
        "docs/guide.md": "# g\n",
    })
    assert _last_event(root).location == "docs/conf.py"


def test_only_a_top_level_dot_dir_or_dot_file_counts_as_config(tmp_path):
    """`src/.well-known/foo.ts` is code; `.github/ci.yml` and `.gitignore` are not."""
    root = _repo(tmp_path)
    _commit(root, "fix: hidden module and docs", {
        "README.md": "# r\n",
        "src/.hidden/x.py": "x = 1\n",
    })
    assert _last_event(root).location == "src/.hidden/x.py"

    _commit(root, "fix: well-known and plain", {
        "src/.well-known/foo.ts": "x\n",
        "src/zzz.ts": "x\n",
    })
    assert _last_event(root).location == "src/.well-known/foo.ts"  # tie: git order

    _commit(root, "fix: ci, dotfile and code", {
        ".github/workflows/ci.yml": "on: push\n",
        ".env.example": "X=1\n",
        "src/app.py": "x\n",
    })
    assert _last_event(root).location == "src/app.py"


def test_a_merge_commit_records_the_files_it_brought_in(tmp_path):
    """`git diff-tree HEAD` prints nothing for a merge; diff against the first
    parent gives the merge exactly the files it landed, not the whole branch."""
    root = _repo(tmp_path)
    _commit(root, "add: base", {"src/base.py": "b\n"})
    _git(root, "checkout", "-qb", "feature")
    _commit(root, "add: feature", {"src/feature.py": "f\n"})
    _git(root, "checkout", "-q", "-")
    _commit(root, "add: mainline", {"src/other.py": "o\n"})
    _git(root, "merge", "-q", "--no-ff", "-m", "fix: merge the feature branch", "feature")

    event = _last_event(root)

    assert event.files == ["src/feature.py"]
    assert event.location == "src/feature.py"


def test_memory_files_come_last_so_the_cap_keeps_the_code(tmp_path):
    """Twelve memory files sort ahead of `src/`; `files[:10]` used to hold
    only them, so the location was not even in the event's file list."""
    root = _repo(tmp_path)
    files = {f".projectmem/issues/{i:04d}-x.md": "x\n" for i in range(12)}
    files["src/app.py"] = "x\n"
    _commit(root, "fix: lots of issues and one file", files)

    event = _last_event(root)

    assert event.location == "src/app.py"
    assert event.files[0] == "src/app.py"
    assert len(event.files) == 10


def test_the_location_is_always_first_in_files(tmp_path):
    """Fourteen docs ahead of `z/code.py` in git order: memory-last sorting
    alone still capped `files` to the docs and left the location out."""
    root = _repo(tmp_path)
    files = {f"a{i:02d}.md": "x\n" for i in range(14)}
    files["z/code.py"] = "x\n"
    _commit(root, "fix: docs and the code", files)

    event = _last_event(root)

    assert event.location == "z/code.py"
    assert event.files[0] == "z/code.py"
    assert len(event.files) == 10


# ── Names are matched whole, never as prefixes ──────────────────────────────

def test_a_code_file_whose_name_starts_like_a_doc_or_manifest_is_code(tmp_path):
    """`readme`, `requirements`, `tsconfig` were prefix matches, so
    `src/requirements_checker.py` lost to `README.md` and `tsconfig_utils.ts`
    to `package.json`."""
    root = _repo(tmp_path)
    for code, other in (
        ("src/requirements_checker.py", "README.md"),
        ("src/tsconfig_utils.ts", "package.json"),
        ("src/NoticeService.java", "CHANGELOG.md"),
        ("src/license_check.py", "docs/guide.md"),
        ("src/readme_gen.py", "LICENSE"),
        ("src/dockerfile_parser.py", "Dockerfile"),
    ):
        _commit(root, f"fix: {code}", {code: "x\n", other: "# x\n"})
        assert _last_event(root).location == code, code


def test_explicit_build_code_files_are_still_demoted(tmp_path):
    root = _repo(tmp_path)
    _commit(root, "fix: packaging and app", {
        "setup.py": "x\n",
        "src/app.py": "x\n",
        "vite.config.ts": "x\n",
        "conftest.py": "x\n",
    })
    assert _last_event(root).location == "src/app.py"

    _commit(root, "fix: docs and a bundler config", {
        "README.md": "# r\n",
        "webpack.config.js": "x\n",
    })
    assert _last_event(root).location == "README.md"


def test_binary_assets_do_not_beat_docs(tmp_path):
    root = _repo(tmp_path)
    _commit(root, "fix: logo and readme", {
        "README.md": "# r\n",
        "assets/logo.png": "\x89PNG\n",
    })
    assert _last_event(root).location == "README.md"
