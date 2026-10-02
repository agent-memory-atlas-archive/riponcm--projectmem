from __future__ import annotations

import os
import re
import shutil
import stat
import sys
from pathlib import Path

import typer

from projectmem.storage import MEM_DIR


# Marker used to identify projectmem's section in git hooks
HOOK_MARKER_START = "# >>> projectmem auto-capture >>>"
HOOK_MARKER_END = "# <<< projectmem auto-capture <<<"

# /bin/sh, not bash. Git for Windows maps /bin/sh to its bundled shell, while
# `/usr/bin/env bash` depends on what is on PATH — and under GitHub Desktop it
# often is not there, which makes git abort the commit outright rather than
# skip the hook. The snippet body uses only [ ], command -v and $( ), so there
# is nothing here that needs bash.
HOOK_SHEBANG = "#!/bin/sh\n"


# ── L-047: bake the absolute path to `pjm` into the hook ────────────────
#
# Git invokes hooks via a non-interactive bash (no `.zshrc`/`.bashrc` runs),
# so conda / pyenv / venv PATH modifications are absent. The old snippet
# relied on `command -v pjm`, which silently returns false in that context
# and made the hook a no-op for every conda user. We now resolve the
# absolute path at install time and write it into the hook, with a runtime
# fallback to `command -v` for the rare case where the install-time binary
# was moved.

def _shell_path(path: str) -> str:
    """A path safe to embed in a POSIX shell script.

    Git hooks run under a shell even on Windows — Git for Windows carries its
    own POSIX runtime. A native Windows path goes in as
    ``C:\\Users\\ripon\\...`` and the shell reads every backslash as an
    escape, so ``PJM_BIN`` becomes ``C:\\Usersipon\\...``: the ``-x`` test
    then fails and the hook silently falls through to its PATH lookup. Forward
    slashes are understood by Git's shell and by Windows itself.
    """
    return path.replace("\\", "/")


def _is_windows() -> bool:
    return os.name == "nt"


def _resolve_pjm_binary() -> str:
    """Find an absolute path to a working pjm-equivalent CLI.

    Preference order:
      1. The entry point next to the interpreter that imported this module —
         ``<prefix>/bin/pjm`` on POSIX, ``<prefix>/Scripts/pjm.exe`` on
         Windows (then the ``projectmem`` alias). Looking only in ``bin/``
         meant this branch could never match on Windows. It goes first
         because it is the install that is actually running: with
         ``/some/venv/bin/pjm init`` and that venv off PATH, asking PATH first
         baked a different install's pjm (an anaconda one, say) into the hook.
      2. ``pjm`` on PATH, then the ``projectmem`` alias
      3. Bare ``"pjm"`` as a last resort — preserves prior behaviour and
         the runtime fallback in the snippet can still find it.

    The result is always shell-safe: see ``_shell_path``.
    """
    if _is_windows():
        candidates = [Path(sys.prefix) / "Scripts" / "pjm.exe",
                      Path(sys.prefix) / "Scripts" / "projectmem.exe"]
    else:
        candidates = [Path(sys.prefix) / "bin" / "pjm",
                      Path(sys.prefix) / "bin" / "projectmem"]
    for guess in candidates:
        if guess.is_file() and (_is_windows() or os.access(guess, os.X_OK)):
            return _shell_path(str(guess))
    found = shutil.which("pjm") or shutil.which("projectmem")
    if found:
        return _shell_path(found)
    return "pjm"


def _auto_capture_snippet(pjm_path: str, capture_arg: str) -> str:
    """Auto-capture hook snippet with a baked absolute path + runtime fallback.

    Redirects BOTH stdout and stderr to /dev/null so the backgrounded
    capture process never prints over the user's shell prompt after
    `git commit` returns (L-050). Users can verify capture with `pjm show`.
    """
    return (
        f"{HOOK_MARKER_START}\n"
        "# Automatically captures development events into projectmem.\n"
        "# Installed by: pjm hooks install (or pjm init)\n"
        "# Remove with:  pjm hooks uninstall\n"
        f'PJM_BIN="{pjm_path}"\n'
        'if [ ! -x "$PJM_BIN" ]; then\n'
        '    PJM_BIN="$(command -v pjm 2>/dev/null || command -v projectmem 2>/dev/null)"\n'
        'fi\n'
        'if [ -d ".projectmem" ] && [ -n "$PJM_BIN" ]; then\n'
        f'    "$PJM_BIN" _auto-capture "{capture_arg}" >/dev/null 2>&1 &\n'
        'fi\n'
        f"{HOOK_MARKER_END}\n"
    )


def _precheck_snippet(pjm_path: str) -> str:
    """Pre-commit precheck snippet with a baked absolute path + runtime fallback."""
    return (
        f"{HOOK_MARKER_START}\n"
        "# Pre-commit warning check against project memory.\n"
        "# Installed by: pjm hooks install (or pjm init)\n"
        "# Remove with:  pjm hooks uninstall\n"
        "# Bypass once:  git commit --no-verify\n"
        f'PJM_BIN="{pjm_path}"\n'
        'if [ ! -x "$PJM_BIN" ]; then\n'
        '    PJM_BIN="$(command -v pjm 2>/dev/null || command -v projectmem 2>/dev/null)"\n'
        'fi\n'
        'if [ -d ".projectmem" ] && [ -n "$PJM_BIN" ]; then\n'
        '    "$PJM_BIN" precheck --level warn || true\n'
        'fi\n'
        f"{HOOK_MARKER_END}\n"
    )


# Back-compat aliases for code/tests that imported the old constants. These
# resolve the binary at import time, which matches the old semantics for
# anyone who imported the snippet directly.
HOOK_SNIPPET = _auto_capture_snippet(_resolve_pjm_binary(), '$1')
PRECHECK_SNIPPET = _precheck_snippet(_resolve_pjm_binary())

# Hook types we install, with the argument passed to _auto-capture
HOOK_CONFIGS = {
    "post-commit": "commit",
    "post-merge": "merge",
}


def run(action: str = "install", root: Path | None = None) -> None:
    root_path = root or Path.cwd()
    hooks_dir = root_path / ".git" / "hooks"

    if not hooks_dir.exists():
        typer.echo(
            "Error: .git/hooks directory not found. Is this a git repository?",
            err=True,
        )
        return

    if action == "install":
        install_hooks(hooks_dir)
    elif action == "uninstall":
        uninstall_hooks(hooks_dir)
    else:
        typer.echo(f"Unknown action: {action}")


# Shebangs earlier releases wrote when they created a hook file of their own
# (0.1.1 through 0.3.2). HOOK_SHEBANG replaced it in 0.3.3 (#16).
_OLD_HOOK_SHEBANGS = ("#!/usr/bin/env bash",)


def _sync_hook(hook_path: Path, snippet: str) -> str:
    """Put `snippet` into one git hook, touching nothing that is not ours.

    Returns what happened: ``installed`` (new block, or new file), ``refreshed``
    (our existing block replaced because it differed), ``current`` (already the
    same) or ``skipped`` (a block with no end marker — see below).

    An existing block used to be skipped on sight, so no fix to the hook body
    ever reached a project that had installed hooks: the baked pjm path, the
    ``#!/bin/sh`` shebang and the forward-slash paths of 0.3.3 all stayed as the
    old release wrote them. The block between the markers is projectmem's, so
    it is replaced in place; everything outside the markers is left as it was.
    """
    if not hook_path.exists():
        hook_path.write_text(HOOK_SHEBANG + snippet, encoding="utf-8")
        _make_executable(hook_path)
        return "installed"

    content = hook_path.read_text(encoding="utf-8")
    start = content.find(HOOK_MARKER_START)
    if start < 0:
        hook_path.write_text(
            content.rstrip("\n") + "\n\n" + snippet, encoding="utf-8"
        )
        _make_executable(hook_path)
        return "installed"

    end = content.find(HOOK_MARKER_END, start)
    if end < 0:
        # A hand-edited hook that lost our end marker. Where the block stops is
        # a guess, and a wrong guess deletes the user's own lines.
        return "skipped"
    end += len(HOOK_MARKER_END)
    if content[end:end + 1] == "\n":
        end += 1

    updated = content[:start] + snippet + content[end:]

    # The shebang is ours to change only when the file is nothing but ours: an
    # old projectmem shebang plus marked blocks. Any other content means the
    # user owns the file, and so the interpreter line.
    first, _, rest = updated.partition("\n")
    if first in _OLD_HOOK_SHEBANGS:
        outside = re.sub(
            re.escape(HOOK_MARKER_START) + r".*?" + re.escape(HOOK_MARKER_END),
            "", rest, flags=re.DOTALL,
        )
        if not outside.strip():
            updated = HOOK_SHEBANG + rest

    if updated == content:
        return "current"
    hook_path.write_text(updated, encoding="utf-8")
    _make_executable(hook_path)
    return "refreshed"


def install_hooks(hooks_dir: Path) -> None:
    """Install projectmem auto-capture into git hooks.

    Safe for existing hooks — adds a clearly-marked snippet rather than
    overwriting the file, and re-running refreshes that snippet in place. The
    pjm binary path is resolved at install time and baked into the hook
    (L-047), so the hook works under conda / pyenv / venv where the
    interactive-shell PATH isn't inherited by git.
    """
    pjm_path = _resolve_pjm_binary()

    # Auto-capture hooks (post-commit, post-merge), then the pre-commit
    # precheck warning.
    snippets = {
        name: _auto_capture_snippet(pjm_path, arg) for name, arg in HOOK_CONFIGS.items()
    }
    snippets["pre-commit"] = _precheck_snippet(pjm_path)

    outcomes = {
        name: _sync_hook(hooks_dir / name, snippet) for name, snippet in snippets.items()
    }
    installed = [n for n, o in outcomes.items() if o == "installed"]
    refreshed = [n for n, o in outcomes.items() if o == "refreshed"]
    skipped = [n for n, o in outcomes.items() if o == "skipped"]

    for name in skipped:
        typer.echo(
            f"Warning: .git/hooks/{name} has a projectmem start marker but no end "
            "marker, so it was left alone. Fix or delete that block, then re-run "
            "`pjm hooks install`.",
            err=True,
        )
    if installed:
        typer.echo(
            f"projectmem git hooks installed: {', '.join(installed)}\n"
            "  Auto-captures: commits, reverts, merges, fixes, features, breaking changes.\n"
            "  Pre-commit: warns about repeating failed approaches and high-churn files."
        )
    if refreshed:
        typer.echo(f"projectmem git hooks refreshed: {', '.join(refreshed)}")
    if not (installed or refreshed or skipped):
        typer.echo("projectmem git hooks already installed.")


def uninstall_hooks(hooks_dir: Path) -> None:
    """Remove projectmem's snippet from git hooks without touching other content."""
    removed: list[str] = []

    # All hook names we may have touched
    all_hooks = list(HOOK_CONFIGS) + ["pre-commit"]

    for hook_name in all_hooks:
        hook_path = hooks_dir / hook_name
        if not hook_path.exists():
            continue
        content = hook_path.read_text(encoding="utf-8")
        if HOOK_MARKER_START not in content:
            continue

        # Remove the projectmem snippet
        lines = content.split("\n")
        new_lines: list[str] = []
        skip = False
        for line in lines:
            if HOOK_MARKER_START in line:
                skip = True
                continue
            if HOOK_MARKER_END in line:
                skip = False
                continue
            if not skip:
                new_lines.append(line)

        remaining = "\n".join(new_lines).strip()
        if remaining in ("", "#!/usr/bin/env bash", "#!/bin/sh"):
            # Hook file is now empty — remove it
            hook_path.unlink()
        else:
            hook_path.write_text(remaining + "\n", encoding="utf-8")
        removed.append(hook_name)

    if removed:
        typer.echo(f"projectmem hooks removed from: {', '.join(removed)}")
    else:
        typer.echo("No projectmem hooks found to remove.")


def _make_executable(path: Path) -> None:
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
