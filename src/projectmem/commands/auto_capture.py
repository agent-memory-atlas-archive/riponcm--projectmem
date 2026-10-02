"""Internal auto-capture command called by git hooks.

This is NOT a user-facing command.  Git hooks invoke it as:
    pjm _auto-capture commit
    pjm _auto-capture merge

It reads the latest git state, classifies the event, and appends an
auto-captured event to events.jsonl.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import typer

from projectmem.models import Event
from projectmem.staleness import is_memory_path
from projectmem.storage import (
    MEM_DIR,
    append_event,
    get_git_commit,
    read_events,
)
from projectmem.summary import regenerate_summary


# ── Classification Patterns ──────────────────────────────────────────
# Order matters — first match wins. Patterns are tested case-insensitively
# against the full commit message.

COMMIT_PATTERNS: list[dict[str, Any]] = [
    {
        "name": "revert",
        "pattern": re.compile(r"^revert\b|^Revert\b", re.IGNORECASE),
        "event_type": "attempt",
        "outcome": "failed",
        "prefix": "Reverted",
        "confidence": "high",
        "capture_source": "git_post_commit",
    },
    {
        "name": "fix",
        "pattern": re.compile(
            r"^fix[\s(:]|^hotfix[\s(:]|^bugfix[\s(:]|^patch[\s(:]", re.IGNORECASE
        ),
        "event_type": "fix",
        "outcome": None,
        "prefix": "Fix",
        "confidence": "high",
        "capture_source": "git_post_commit",
    },
    {
        "name": "breaking",
        "pattern": re.compile(r"BREAKING[\s_-]?CHANGE|^break[\s(:]", re.IGNORECASE),
        "event_type": "decision",
        "outcome": None,
        "prefix": "Breaking change",
        "confidence": "high",
        "capture_source": "git_post_commit",
    },
    {
        "name": "feature",
        "pattern": re.compile(r"^feat[\s(:]|^feature[\s(:]|^add[\s(:]", re.IGNORECASE),
        "event_type": "note",
        "outcome": None,
        "prefix": "New feature",
        "confidence": "medium",
        "capture_source": "git_post_commit",
    },
    {
        "name": "refactor",
        "pattern": re.compile(
            r"^refactor[\s(:]|^cleanup[\s(:]|^reorganize|^restructure",
            re.IGNORECASE,
        ),
        "event_type": "decision",
        "outcome": None,
        "prefix": "Refactor",
        "confidence": "medium",
        "capture_source": "git_post_commit",
    },
    {
        "name": "docs",
        "pattern": re.compile(r"^docs?[\s(:]|^readme|^changelog", re.IGNORECASE),
        "event_type": "note",
        "outcome": None,
        "prefix": "Documentation",
        "confidence": "low",
        "capture_source": "git_post_commit",
    },
    {
        "name": "test",
        "pattern": re.compile(r"^test[\s(:]|^tests?[\s(:]|^spec[\s(:]", re.IGNORECASE),
        "event_type": "note",
        "outcome": None,
        "prefix": "Tests",
        "confidence": "low",
        "capture_source": "git_post_commit",
    },
]

# Minimum confidence to actually log (skip "low" by default to reduce noise)
MIN_CONFIDENCE = "medium"
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def run(trigger: str = "commit", root: Path | None = None) -> None:
    """Classify the latest git action and log it as an auto-captured event."""
    root_path = root or Path.cwd()

    # Guard: only run if .projectmem exists
    if not (root_path / MEM_DIR).exists():
        return

    # Check auto-capture config
    config_path = root_path / MEM_DIR / "config.toml"
    if config_path.exists():
        config_text = config_path.read_text(encoding="utf-8")
        if "auto_capture = false" in config_text:
            return

    if trigger == "commit":
        _capture_commit(root_path)
    elif trigger == "merge":
        _capture_merge(root_path)


def _capture_commit(root: Path) -> None:
    """Classify and capture a git commit."""
    msg = _git_last_message(root)
    if not msg:
        return

    changes = _git_last_changes(root)
    location = _pick_location(changes)
    # The location first, then the rest with memory files last, so the
    # 10-file cap never drops the code and the location is always in `files`.
    rest = sorted((p for _, p in changes if p != location), key=is_memory_path)
    files = ([location] if location else []) + rest
    commit_hash = get_git_commit(root)

    # Deduplicate: don't re-log if this commit is already captured
    try:
        existing = read_events(root)
        existing_commits = {e.git_commit for e in existing if e.git_commit}
        if commit_hash and commit_hash in existing_commits:
            return
    except Exception:
        pass  # If events can't be read, proceed anyway

    # Classify
    matched = _classify_message(msg)
    if not matched:
        return

    # Check confidence threshold
    if CONFIDENCE_RANK.get(matched["confidence"], 0) < CONFIDENCE_RANK.get(
        MIN_CONFIDENCE, 2
    ):
        return

    # Build summary
    first_line = msg.strip().split("\n")[0][:120]
    summary = f"{matched['prefix']}: {first_line}"

    event = Event(
        type=matched["event_type"],
        summary=summary,
        outcome=matched["outcome"],
        files=files[:10],  # Cap at 10 files
        git_commit=commit_hash,
        location=location,
        auto_captured=True,
        capture_source=matched["capture_source"],
        capture_confidence=matched["confidence"],
        git_message=first_line,
        command="auto-capture",
    )

    try:
        append_event(event, root)
        regenerate_summary(root)
        # Color output for terminal feedback
        colors = {
            "attempt": "\033[0;33m",  # yellow for reverts
            "fix": "\033[0;32m",      # green for fixes
            "decision": "\033[0;31m", # red for breaking/decisions
            "note": "\033[0;36m",     # cyan for features/notes
        }
        color = colors.get(event.type, "\033[0;37m")
        typer.echo(
            f"{color}[projectmem] Auto-captured: {summary}\033[0m"
        )
    except Exception:
        pass  # Silent failure — never block the developer's workflow


def _capture_merge(root: Path) -> None:
    """Capture a branch merge event."""
    msg = _git_last_message(root)
    if not msg:
        return

    commit_hash = get_git_commit(root)
    first_line = msg.strip().split("\n")[0][:120]

    # Deduplicate
    try:
        existing = read_events(root)
        existing_commits = {e.git_commit for e in existing if e.git_commit}
        if commit_hash and commit_hash in existing_commits:
            return
    except Exception:
        pass

    event = Event(
        type="note",
        summary=f"Merge: {first_line}",
        git_commit=commit_hash,
        location=None,
        auto_captured=True,
        capture_source="git_post_merge",
        capture_confidence="high",
        git_message=first_line,
        command="auto-capture",
    )

    try:
        append_event(event, root)
        regenerate_summary(root)
        typer.echo(
            f"\033[0;35m[projectmem] Auto-captured merge: {first_line}\033[0m"
        )
    except Exception:
        pass


def _classify_message(message: str) -> dict[str, Any] | None:
    """Match a commit message against classification patterns."""
    for pattern in COMMIT_PATTERNS:
        if pattern["pattern"].search(message):
            return pattern
    return None


def _git_last_message(root: Path) -> str | None:
    """Get the most recent commit message."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--pretty=%B"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _git_last_changes(root: Path) -> list[tuple[str, str]]:
    """``(status, path)`` for every file the most recent commit touched.

    ``status`` is git's one-letter kind — A(dded), M(odified), D(eleted),
    R(enamed), C(opied), T(ype changed) — with the similarity score dropped.
    For a rename or copy ``path`` is the NEW path: the old one no longer
    exists, so an event located there would be flagged "no longer exists"
    forever and ``pjm precheck`` on the surviving file would show nothing.

    Diffing ``HEAD^..HEAD`` explicitly gives a merge commit the files it
    brought onto its first parent (``diff-tree HEAD`` prints nothing for a
    merge, and ``-m`` concatenates the diff against every parent). The repo's
    first commit has no ``HEAD^``, so it falls back to ``--root``. ``-l1000``
    caps rename detection so a huge commit cannot run into the 5 s timeout
    and silently lose its files; ``-z`` keeps a non-ASCII path as bytes
    instead of a C-quoted ``"src/caf\\303\\251.py"`` that matches no file.
    """
    common = ["-r", "-M", "-l1000", "-z", "--name-status"]
    attempts = (
        ["git", "diff-tree", *common, "HEAD^", "HEAD"],
        ["git", "diff-tree", "--root", "--no-commit-id", *common, "HEAD"],
    )
    result = None
    for cmd in attempts:
        try:
            result = subprocess.run(
                cmd, cwd=root, check=True, capture_output=True, timeout=5
            )
            break
        except subprocess.CalledProcessError:
            continue  # no parent: try the root form
        except (OSError, subprocess.TimeoutExpired):
            return []
    if result is None:
        return []
    # Not `text=True`: a hook runs under whatever locale git gave it, often C,
    # and a non-UTF-8 default encoding would turn café into a crash.
    fields = result.stdout.decode("utf-8", errors="replace").split("\0")
    changes: list[tuple[str, str]] = []
    i = 0
    while i + 1 < len(fields) and fields[i]:
        status, path = fields[i][0], fields[i + 1]
        i += 2
        if status in ("R", "C"):  # one extra field: old path, then new path
            if i >= len(fields):
                break
            path = fields[i]
            i += 1
        changes.append((status, path))
    return changes


# ── Location ranking ─────────────────────────────────────────────────────
# When a commit touches code, the location is the code. Git lists paths in
# sorted order, so without a ranking `Dockerfile`, `README.md` or
# `.github/ci.yml` won every time they appeared next to `src/`.
#
# Rank 0 is a positive test. `models._SOURCE_SUFFIXES` is the wrong table for
# it: that set answers "does this look like a file path" and so includes
# md, txt, json, toml and lock.
RANK_SOURCE, RANK_TEST, RANK_OTHER, RANK_DOC, RANK_BUILD, RANK_LOCK = range(6)

_CODE_SUFFIXES = frozenset(
    "py pyi js jsx ts tsx mjs cjs rs go rb php java kt kts swift c h cc cpp hpp "
    "cs m mm sh bash zsh ps1 sql html css scss sass vue svelte gradle tf proto "
    "graphql ex exs erl hs scala clj lua r jl dart zig nim".split()
)
_TEST_DIRS = frozenset({"tests", "test", "__tests__", "spec"})
_TEST_NAME = re.compile(r"(^test_.*|.*_test\.[^.]+|.*\.(spec|test)\.[^.]+)$")
_DOC_SUFFIXES = (".md", ".rst", ".adoc")
_DOC_DIRS = frozenset({"docs", "doc"})
# Matched against the whole stem (before the first dot), never as a prefix:
# `README`, `LICENSE.txt` — but `readme_gen.py` and `NoticeService.java` are code.
_DOC_STEMS = frozenset({"readme", "changelog", "changes", "notice", "license",
                        "licence", "copying", "authors", "contributing",
                        "code_of_conduct", "security"})
# Build/manifest/CI files, by whole name or anchored pattern. These are the
# only names that demote a file carrying a code suffix (`setup.py`,
# `vite.config.ts`): they configure the build rather than being the program.
_BUILD_NAMES = frozenset({
    "makefile", "cmakelists.txt", "justfile", "package.json", "pyproject.toml",
    "setup.py", "setup.cfg", "tox.ini", "noxfile.py", "conftest.py", "manifest.in",
    "cargo.toml", "go.mod", "gemfile", "rakefile", "pipfile", "composer.json",
    "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
    "pom.xml", "mix.exs", "deno.json", "procfile", "vagrantfile",
})
_BUILD_RE = re.compile(
    r"^(dockerfile(\..+)?|.+\.dockerfile|docker-compose.*\.ya?ml|requirements.*\.txt"
    r"|constraints.*\.txt|tsconfig.*\.json|jsconfig.*\.json|.+\.config\.(js|cjs|mjs|ts))$"
)
# Binary assets rank with build files: a logo is not what a commit is about.
_ASSET_SUFFIXES = frozenset(
    "png jpg jpeg gif webp svg ico bmp tiff psd ttf otf woff woff2 eot "
    "mp3 wav ogg flac mp4 webm mov avi pdf zip gz tgz tar bz2 xz 7z rar jar "
    "whl bin dll so dylib exe".split()
)
_LOCK_NAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock",
    "pipfile.lock", "cargo.lock", "gemfile.lock", "composer.lock", "go.sum",
})


def _location_rank(path: str) -> int:
    """Lower wins: source, tests, other, docs, build/manifest/CI, lockfiles."""
    parts = path.split("/")
    lower = parts[-1].lower()
    stem, _, rest = lower.partition(".")
    suffix = rest.rsplit(".", 1)[-1] if rest else ""
    if lower in _LOCK_NAMES or suffix == "lock":
        return RANK_LOCK
    # Only the top-level directory and the file name count as "dot" config:
    # `.github/`, `.husky/`, `.gitignore`, `.env.example`, `.eslintrc.json`.
    # `src/.well-known/foo.ts` is code.
    if parts[0].startswith(".") or lower.startswith(".") or lower in _BUILD_NAMES \
            or _BUILD_RE.match(lower) or suffix in _ASSET_SUFFIXES:
        return RANK_BUILD
    if suffix in _CODE_SUFFIXES:  # before docs: `docs/conf.py`, `license_check.py`
        in_test_dir = any(p.lower() in _TEST_DIRS for p in parts[:-1])
        return RANK_TEST if in_test_dir or _TEST_NAME.match(lower) else RANK_SOURCE
    if lower.endswith(_DOC_SUFFIXES) or stem in _DOC_STEMS \
            or parts[0].lower() in _DOC_DIRS:
        return RANK_DOC
    return RANK_OTHER


def _pick_location(changes: list[tuple[str, str]]) -> str | None:
    """The one file this commit is about, or None.

    Candidates are the paths that still exist after the commit — a deleted
    file would be flagged "no longer exists" on every later precheck — minus
    memory files: summary.md is regenerated on every event, so it is in most
    commits, and `.projectmem/` sorts ahead of most paths (#20). Among those
    the lowest ``_location_rank`` wins; a tie keeps git's order.
    """
    live = [
        path for status, path in changes
        if status != "D" and not is_memory_path(path)
    ]
    if not live:
        return None
    return min(live, key=_location_rank)  # min() is stable: first of a tie wins
