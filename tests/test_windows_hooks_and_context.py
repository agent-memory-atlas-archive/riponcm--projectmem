"""Regressions for 0.3.3 — three bugs reported against 0.3.2.

All three had the same shape: something broke on Windows, or reported a crash
where there was none, and the failure was silent or misleading.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import set_fake_home

WIN_PJM = r"C:\Users\ripon\AppData\Local\Programs\Python\Python312\Scripts\pjm.exe"


# ── #16: git hooks blocked commits from GitHub Desktop ──────────────────────

def test_hook_shebang_is_posix_sh_not_bash():
    """Git for Windows maps /bin/sh to its bundled shell.

    `/usr/bin/env bash` depends on PATH, and under GitHub Desktop bash often is
    not there — git then aborts the commit rather than skipping the hook. The
    snippet body uses only [ ], command -v and $( ), so nothing needs bash.
    """
    from projectmem.commands import hooks

    assert hooks.HOOK_SHEBANG == "#!/bin/sh\n"


def test_baked_binary_path_has_no_backslashes(monkeypatch, tmp_path):
    """A Windows path in a shell string is eaten by escape processing.

    `C:\\Users\\ripon\\...` reaches the shell as `C:\\Usersipon\\...` — the -x
    test then fails and the hook silently falls through to its PATH lookup, so
    it only ever worked by accident.
    """
    from projectmem.commands import hooks

    # An empty prefix, so PATH is what answers: the interpreter-adjacent pjm
    # (the venv this suite runs in) would otherwise win, correctly.
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda name: WIN_PJM)
    resolved = hooks._resolve_pjm_binary()

    assert "\\" not in resolved
    assert resolved == "C:/Users/ripon/AppData/Local/Programs/Python/Python312/Scripts/pjm.exe"


def test_baked_path_survives_a_real_shell(tmp_path, monkeypatch):
    """The end the user actually feels: what the hook reads back."""
    from projectmem.commands import hooks

    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda name: WIN_PJM)
    script = tmp_path / "h.sh"
    script.write_text(f'PJM_BIN="{hooks._resolve_pjm_binary()}"\nprintf %s "$PJM_BIN"\n')
    out = subprocess.run(["sh", str(script)], capture_output=True, text=True).stdout

    assert out == "C:/Users/ripon/AppData/Local/Programs/Python/Python312/Scripts/pjm.exe"
    assert "Usersipon" not in out, "backslash escapes ate part of the path"


def test_windows_falls_back_to_scripts_not_bin(monkeypatch, tmp_path):
    """`<prefix>/bin/pjm` can never exist on Windows; it is Scripts/pjm.exe."""
    from projectmem.commands import hooks

    import types

    monkeypatch.setattr(shutil, "which", lambda name: None)
    # Patch only the module's view of os.name. Setting the real os.name makes
    # pathlib try to build a WindowsPath on a POSIX host, which fails for
    # reasons that have nothing to do with what is being tested.
    monkeypatch.setattr(hooks, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "pjm.exe").write_text("")

    assert hooks._resolve_pjm_binary() == f"{tmp_path.as_posix()}/Scripts/pjm.exe"


def test_installed_hook_does_not_block_a_commit(tmp_path, monkeypatch):
    """The reported symptom, end to end."""
    from projectmem.commands.hooks import install_hooks

    set_fake_home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "pm"))
    repo = tmp_path / "repo"
    (repo / ".projectmem").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    install_hooks(repo / ".git" / "hooks")

    hook = repo / ".git" / "hooks" / "pre-commit"
    assert hook.read_text().startswith("#!/bin/sh")

    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    r = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-m", "test"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, f"hook blocked the commit: {r.stderr}"


# ── #15: get_context read the agent harness directory ───────────────────────

def test_get_context_uses_the_resolved_project_not_cwd(tmp_path, monkeypatch):
    """generate_context defaults root to Path.cwd().

    For an MCP server cwd is wherever the client launched it — the agent
    harness directory. The events came from the right project but the git
    status and architecture came from somewhere else entirely.
    """
    from projectmem import mcp_server
    from projectmem.commands import context as context_mod

    seen = {}

    def spy(events, token_budget=2000, focus=None, recent_days=30, root=None):
        seen["root"] = root
        return {"markdown": "ok"}

    monkeypatch.setattr(context_mod, "generate_context", spy)
    monkeypatch.setattr(mcp_server, "_root_for", lambda project: tmp_path / "realproj")
    monkeypatch.setattr(mcp_server, "read_events", lambda root: [])

    fn = mcp_server.get_context.fn if hasattr(mcp_server.get_context, "fn") else mcp_server.get_context
    fn(project="realproj")

    assert seen["root"] == tmp_path / "realproj", "root must be the resolved project"


def test_precheck_file_uses_the_resolved_project_not_cwd(tmp_path, monkeypatch):
    """The same defect as get_context, one tool over.

    precheck_file resolved the root to READ events, then called
    _analyze_files without it. Staleness then asked "does this file
    exist?" relative to the server's cwd, so every cited file in every
    project but the server's own looked deleted — and the churn count
    came from whatever git repo the server happened to sit in.
    """
    from projectmem import mcp_server
    from projectmem.commands import precheck as precheck_mod

    seen = {}

    def spy(files, events, root=None):
        seen["root"] = root
        return []

    monkeypatch.setattr(precheck_mod, "_analyze_files", spy)
    monkeypatch.setattr(mcp_server, "_root_for", lambda project: tmp_path / "realproj")
    monkeypatch.setattr(mcp_server, "read_events", lambda root: [])

    fn = (mcp_server.precheck_file.fn
          if hasattr(mcp_server.precheck_file, "fn") else mcp_server.precheck_file)
    fn(file_path="src/a.py", project="realproj")

    assert seen["root"] == tmp_path / "realproj", "root must be the resolved project"

def test_precheck_churn_counts_the_projects_git_not_the_servers(tmp_path, monkeypatch):
    """The churn number came from whatever repo the process sat in.

    _analyze_files received root but called _git_recent_changes without
    it, so `git log` ran in the server's cwd. Outside a repo git fails and
    the count silently fell back to the event log, inventing churn the
    project does not have.
    """
    from projectmem.commands import precheck as precheck_mod
    from projectmem.models import Event

    seen = {}

    def spy(file_path, days, root=None):
        seen["root"] = root
        return 0

    monkeypatch.setattr(precheck_mod, "_git_recent_changes", spy)
    monkeypatch.chdir(tmp_path)

    events = [Event(id="evt_1", type="note", timestamp="2099-01-01T00:00:00Z",
                    summary="a note", location="src/a.py:1")]
    precheck_mod._analyze_files(["src/a.py"], events, root=tmp_path / "realproj")

    assert seen["root"] == tmp_path / "realproj", "churn must be counted in the project"

# ── expected errors were reported as crashes ────────────────────────────────

def test_an_expected_error_prints_no_traceback(tmp_path):
    """`pjm fix` with no open issue is a normal condition, not a crash."""
    env = {**os.environ,
           "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
           "PROJECTMEM_HOME": str(tmp_path / "pm"),
           "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")}
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([sys.executable, "-m", "projectmem.cli", "init"],
                   cwd=repo, env=env, capture_output=True)
    r = subprocess.run([sys.executable, "-m", "projectmem.cli", "fix", "nothing open"],
                       cwd=repo, env=env, capture_output=True, text=True)

    combined = r.stdout + r.stderr
    assert "No open issue found" in combined
    assert "Traceback" not in combined, f"expected error printed a traceback:\n{combined}"
    assert r.returncode == 1
