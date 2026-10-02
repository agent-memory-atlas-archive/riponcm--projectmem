"""Stale-memory detection (0.1.4) — judgment, not decay.

Other memory tools silently down-rank or delete old memories; projectmem
never deletes. Instead it cross-references each decision/fix/note against
the git history of the file it cites: if the file has changed substantially
since the event was logged, the event is *flagged* as possibly stale and a
human decides — confirm it still holds, or retire it with
``pjm decision "..." --supersedes <id>``.

Detection is deliberately cheap and deterministic: one ``git log`` count per
distinct (file, oldest-event) pair, no embeddings, no daemon. Events whose
referenced file no longer exists are flagged too (strongest staleness
signal of all).
"""
from __future__ import annotations

import posixpath
import re
import subprocess
from pathlib import Path

from projectmem.models import (
    Event,
    location_to_file,
    normalize_timestamp,
    superseded_ids,
)
from projectmem.storage import MEM_DIR

# A memory is "possibly stale" once its file changed in this many commits
# after the event was logged. 3 tracks the precheck block threshold — one
# rewrite is normal drift, three separate changes mean the file moved on.
STALE_COMMIT_THRESHOLD = 3

# ``Path("C:/x").is_absolute()`` is False on POSIX; an MCP client on Windows
# can still hand us that spelling.
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:/")

# Event types that assert something durable about a file. Attempts are
# excluded: a failed attempt is a historical fact, not a claim about the
# file's current shape — it cannot go stale.
_STALE_CHECKED_TYPES = ("decision", "fix", "note")

# Auto-captured event types that are likewise records of a commit rather
# than claims: "Fix: ..." and "New feature: ..." say what a commit did. An
# auto-captured decision ("Breaking change:", "Refactor:") does assert the
# file's shape, so it stays checked (see ``find_stale_events``).
_AUTO_CAPTURED_FACT_TYPES = ("fix", "note")


def location_file(event: Event, root: Path | None = None) -> str | None:
    """File part of an event's location (``src/auth.py:42`` -> ``src/auth.py``)."""
    if not event.location:
        return None
    file_part = location_to_file(event.location) or ""
    # Locations like "class AuthHandler" or "deploy pipeline" aren't paths.
    if not file_part or ("/" not in file_part and "." not in file_part):
        return None
    # Memory files are not code. summary.md is regenerated on every event, so
    # any event located there would be flagged within three commits; before
    # #20 auto-capture located most commits there, and the log is append-only,
    # so those events are still in users' logs. Judge them never, not forever.
    if is_memory_path(file_part, root):
        return None
    return file_part


def is_memory_path(path: str, root: Path | None = None) -> bool:
    """True for a path inside the project's own ``.projectmem/``.

    One rule for every reader (staleness, precheck, the MCP tool): the path,
    made project-relative, has ``.projectmem`` as its FIRST component.
    ``sub/.projectmem/x`` and ``tests/fixtures/.projectmem/x`` are ordinary
    files — only the memory directory at the root is projectmem's — and so is
    anything outside the project.

    A relative path is project-relative (git output, event locations). One
    that climbs out of the root (``../.projectmem/summary.md``) was typed from
    a subdirectory — an MCP client's cwd, say — and is anchored there instead.
    ``src/../.projectmem/summary.md`` and ``./`` fold through ``normpath``.
    """
    root_posix = (root or Path.cwd()).resolve().as_posix()
    normalized = path.replace("\\", "/")
    if _WINDOWS_DRIVE.match(normalized) and not _WINDOWS_DRIVE.match(root_posix):
        # A Windows path handed to a POSIX server cannot be anchored to the
        # root at all. "Safe to modify" is the costly mistake, so err towards
        # "ours" when the path names a `.projectmem` directory anywhere.
        return MEM_DIR in normalized.split("/")
    is_abs = posixpath.isabs(normalized) or bool(_WINDOWS_DRIVE.match(normalized))
    bases = [None] if is_abs else [root_posix, _cwd_posix()]
    for base in bases:
        candidate = posixpath.normpath(
            normalized if base is None else posixpath.join(base, normalized)
        )
        # The root is resolved, so resolve an absolute candidate too: on macOS
        # `/tmp/proj/.projectmem/x` is `/private/tmp/proj/...` once resolved,
        # and any project reached through a symlinked folder has the same gap.
        spellings = [candidate]
        if base is None:
            spellings.append(_resolved_posix(candidate))
        for spelling in spellings:
            try:
                rel = Path(spelling).relative_to(root_posix)
            except ValueError:
                continue  # outside the project from this anchor
            return bool(rel.parts) and rel.parts[0] == MEM_DIR
    return False


def _resolved_posix(path: str) -> str:
    try:
        return Path(path).resolve().as_posix()
    except (OSError, RuntimeError):  # unreadable, or a symlink loop
        return path


def _cwd_posix() -> str:
    try:
        return Path.cwd().resolve().as_posix()
    except OSError:  # cwd deleted under us
        return "/"


def commits_touching_since(
    file_path: str, since_iso: str, root: Path | None = None
) -> int | None:
    """Count commits that touched `file_path` after `since_iso`.

    Returns None when git is unavailable / not a repo — callers must treat
    that as "cannot judge", never as "stale".

    Kept for callers that need a single answer. ``find_stale_events`` uses
    ``commit_times`` instead: one git call per file answers every event that
    cites it, rather than one call per event.
    """
    times = commit_times(file_path, root)
    if times is None:
        return None
    since = _zulu(since_iso)
    return sum(1 for t in times if t > since)


def _zulu(ts: str | None) -> str:
    """Canonical UTC form of an event timestamp; "" (no timestamp) stays ""."""
    return normalize_timestamp(ts) if ts else ""


def commit_times(
    file_path: str, root: Path | None = None, since_iso: str | None = None
) -> list[str] | None:
    """Every commit time that touched `file_path`, newest first, as UTC Zulu.

    Times are returned in the same canonical form events are stored in
    (``2026-10-02T18:41:22Z``), so callers may compare them as strings. Git's
    ``%cI`` carries the committer's own offset (``12:41:32-06:00``), and a
    string comparison between that and a Zulu event timestamp is a comparison
    of wall-clock digits in two different zones: the same five events and the
    same history were flagged 0, 2 or 5 times depending on the machine's TZ
    (0.3.3 regression).

    One ``git log`` answers any number of "how many commits since T?"
    questions by counting in memory, because the answer for a later T is
    always a suffix of the answer for an earlier one.

    This replaces one subprocess per event. The old memoisation keyed on
    ``(file, event.timestamp)`` and looked correct, but real events carry
    distinct timestamps, so it never hit: a 1,200-event project spawned 1,201
    git processes and took 26 seconds to answer a question about one file.

    ``since_iso`` bounds the walk. Nothing older than the oldest event citing
    the file can change any count, so passing that timestamp keeps one call
    doing no more work than the calls it replaces — and keeps it inside the
    same 5s budget every other git helper here uses.

    Returns None when git cannot answer — never an empty list, which would
    read as "nothing has changed" and is the opposite of "cannot judge".
    """
    cmd = ["git", "log", "--format=%cI"]
    if since_iso:
        cmd.append(f"--since={since_iso}")
    cmd += ["--", file_path]
    try:
        result = subprocess.run(
            cmd,
            cwd=root or Path.cwd(),
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return [
        normalize_timestamp(line.strip())
        for line in result.stdout.splitlines()
        if line.strip()
    ]


def find_stale_events(
    events: list[Event],
    root: Path | None = None,
    threshold: int = STALE_COMMIT_THRESHOLD,
    only_files: set[str] | None = None,
) -> list[dict]:
    """Flag live decisions/fixes/notes whose cited file has moved on.

    Returns dicts: ``{event, file, commits_since}`` — ``commits_since`` is
    -1 when the cited file no longer exists (deleted/renamed), which is
    reported as its own, stronger staleness reason. Superseded events are
    skipped: they are already retired, flagging them again is noise.

    ``only_files`` restricts the check to events citing those paths. It is how
    ``pjm precheck`` asks about the handful of files being committed instead of
    the whole log — on a 1,200-event project that is 95% less work for exactly
    the same answer about those files. Callers that genuinely want the project
    view (``pjm brief``, the dashboard) leave it None.

    Cost is one ``git log`` per distinct file, not one per event.
    """
    root_path = root or Path.cwd()
    retired = superseded_ids(events)

    # Pass one: decide what to ask about, before running any git.
    candidates: list[tuple[Event, str]] = []
    for event in events:
        if event.type not in _STALE_CHECKED_TYPES or event.id in retired:
            continue
        # An auto-captured fix or note is a record of a commit, not a human's
        # claim about the file — the same reason attempts are excluded. Three
        # more commits to the file are the file being worked on, not the
        # record going out of date; flagging it only taught users to ignore
        # the warning. An auto-captured decision is a claim, and stays.
        if event.auto_captured and event.type in _AUTO_CAPTURED_FACT_TYPES:
            continue
        file_path = location_file(event, root_path)
        if not file_path:
            continue
        if only_files is not None and file_path not in only_files:
            continue
        candidates.append((event, file_path))

    # Pass two: one git call per distinct file, bounded by the oldest event
    # that cites it — commits before that cannot affect any count.
    oldest: dict[str, str] = {}
    for event, file_path in candidates:
        ts = _zulu(event.timestamp)
        if file_path not in oldest or ts < oldest[file_path]:
            oldest[file_path] = ts

    history: dict[str, list[str] | None] = {}
    for _, file_path in candidates:
        if file_path in history:
            continue
        if not (root_path / file_path).exists():
            history[file_path] = None  # gone — handled below, no git needed
            continue
        history[file_path] = commit_times(
            file_path, root_path, since_iso=oldest.get(file_path) or None
        )

    # Pass three: every count is a comparison against times already in hand.
    flagged: list[dict] = []
    for event, file_path in candidates:
        if not (root_path / file_path).exists():
            flagged.append({"event": event, "file": file_path, "commits_since": -1})
            continue
        times = history.get(file_path)
        if times is None:
            continue  # git could not answer; "cannot judge" is not "stale"
        # Both sides are canonical Zulu (see ``commit_times``), so the string
        # order is the time order.
        logged = _zulu(event.timestamp)
        count = sum(1 for t in times if t > logged)
        if count >= threshold:
            flagged.append({"event": event, "file": file_path, "commits_since": count})
    return flagged


def stale_label(item: dict) -> str:
    """One-line human description of a stale flag."""
    event: Event = item["event"]
    if item["commits_since"] == -1:
        reason = f"cited file {item['file']} no longer exists"
    else:
        reason = f"predates {item['commits_since']} commits to {item['file']}"
    return (
        f"{event.type} [{event.id}] \"{event.summary[:70]}\" — {reason}. "
        f"Confirm, or retire with: pjm decision \"...\" --supersedes {event.id}"
    )
