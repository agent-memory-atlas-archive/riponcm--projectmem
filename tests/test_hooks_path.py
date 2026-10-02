"""Regression test for L-047 — the conda/venv hook silently-no-op bug.

Before the fix, ``install_hooks`` wrote hook snippets that relied on
``command -v pjm`` at runtime. Under conda/pyenv/venv the interactive
shell's PATH isn't inherited by git's non-interactive hook bash, so the
lookup returned nothing and the hook silently did nothing — including
the pre-commit warning, projectmem's headline feature.

The fix bakes the install-time absolute path to ``pjm`` into the hook,
with a runtime ``command -v`` fallback for the case where the binary
was moved. This test simulates a stripped-PATH non-interactive bash
(approximating git's hook environment) and confirms the hook still
finds and invokes ``pjm``.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from projectmem.commands.hooks import (
    HOOK_MARKER_END,
    HOOK_MARKER_START,
    _auto_capture_snippet,
    _precheck_snippet,
    _resolve_pjm_binary,
    install_hooks,
)


def test_resolve_pjm_binary_returns_absolute_path_or_bare_name() -> None:
    """The resolver must return *something* — never raise."""
    found = _resolve_pjm_binary()
    assert isinstance(found, str) and found
    # Either an absolute path (the normal case) or the literal "pjm" fallback.
    assert found == "pjm" or os.path.isabs(found)


def test_precheck_snippet_bakes_path_into_script(tmp_path: Path) -> None:
    snippet = _precheck_snippet("/opt/example/bin/pjm")
    assert 'PJM_BIN="/opt/example/bin/pjm"' in snippet
    # Markers preserved so uninstall still works.
    assert HOOK_MARKER_START in snippet
    assert HOOK_MARKER_END in snippet
    # The runtime fallback is there too.
    assert "command -v pjm" in snippet


def test_auto_capture_snippet_bakes_path_and_capture_arg() -> None:
    snippet = _auto_capture_snippet("/opt/example/bin/pjm", "commit")
    assert 'PJM_BIN="/opt/example/bin/pjm"' in snippet
    assert '"$PJM_BIN" _auto-capture "commit"' in snippet
    # No leftover `$1` placeholder.
    assert "$1" not in snippet
    # L-050: both stdout AND stderr must be silenced, or the
    # backgrounded capture output prints over the user's shell prompt
    # after `git commit` returns. The old `2>/dev/null` (stderr only)
    # form is forbidden.
    assert ">/dev/null 2>&1 &" in snippet
    assert "2>/dev/null &" not in snippet


@pytest.fixture
def fake_pjm(tmp_path: Path) -> Path:
    """Create a fake `pjm` binary that records its invocation arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "pjm.log"
    pjm = bin_dir / "pjm"
    pjm.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log_path}"\n'
        "exit 0\n"
    )
    os.chmod(pjm, 0o755)
    return pjm


def _require_launchable_bash() -> str:
    """Return a bash executable path, or skip when this host cannot run one."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is not available")

    try:
        result = subprocess.run(
            [bash, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"bash is not launchable: {exc}")

    if result.returncode != 0:
        reason = _bash_failure_reason(result)
        pytest.skip(f"bash is not launchable: {reason[:120]}")

    return bash


def _bash_failure_reason(result: subprocess.CompletedProcess[str]) -> str:
    """Normalize Windows bash/WSL startup errors for readable skip messages."""
    return (
        ((result.stderr or "") + "\n" + (result.stdout or ""))
        .replace("\x00", "")
        .strip()
    )


def test_hook_runs_under_stripped_path(tmp_path: Path, fake_pjm: Path) -> None:
    """Simulate git's non-interactive hook environment: PATH = /usr/bin only.

    The hook must still find pjm via the baked absolute path, NOT via
    `command -v`. This is the exact failure mode L-047 fixes.
    """
    bash = _require_launchable_bash()

    # Lay out a repo with .projectmem so the hook's directory check passes.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".projectmem").mkdir()

    # Build the snippet that install_hooks would write — but with our fake
    # pjm path baked in instead of the real one.
    snippet = _precheck_snippet(str(fake_pjm))
    hook = repo / "pre-commit"
    hook.write_text("#!/usr/bin/env bash\n" + snippet)
    os.chmod(hook, 0o755)

    # Strip PATH to a minimal git-like environment — NO conda/venv bin dir.
    minimal_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
    }
    # Resolve bash before stripping PATH; the stripped PATH is the condition
    # being tested inside the hook process.
    result = subprocess.run(
        [bash, str(hook)],
        cwd=repo,
        env=minimal_env,
        capture_output=True,
        text=True,
        timeout=10,
        stdin=subprocess.DEVNULL,
    )
    log_path = fake_pjm.parent.parent / "pjm.log"
    if result.returncode != 0:
        reason = _bash_failure_reason(result)
        lowered = reason.lower()
        if "wsl" in lowered or "bash/" in lowered:
            pytest.skip(f"bash is not launchable for hook scripts: {reason[:120]}")
        assert result.returncode == 0, f"hook errored: {reason}"
    assert log_path.exists(), (
        "hook did not invoke pjm — this is the L-047 regression "
        f"(stripped PATH = {minimal_env['PATH']!r}). stderr: {result.stderr!r}"
    )
    invocation = log_path.read_text().strip()
    assert invocation.startswith("precheck"), f"unexpected args: {invocation!r}"


def test_install_hooks_uses_resolved_path(tmp_path: Path, monkeypatch) -> None:
    """Smoke: install_hooks writes a hook whose PJM_BIN is the resolved binary."""
    # An empty prefix, so PATH is what answers: the interpreter-adjacent pjm
    # takes precedence over it, and is exercised by its own tests below.
    monkeypatch.setattr("sys.prefix", str(tmp_path / "no-venv"))
    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True)
    install_hooks(hooks_dir)
    precommit = hooks_dir / "pre-commit"
    assert precommit.exists()
    content = precommit.read_text()
    # The hook must contain a PJM_BIN line with a path; we don't pin which
    # specific path, just that it isn't the bare "pjm" fallback when a real
    # binary was discoverable.
    match = re.search(r'PJM_BIN="([^"]+)"', content)
    assert match, "hook missing PJM_BIN line"
    baked = match.group(1)
    found = shutil.which("pjm") or shutil.which("projectmem")
    if found:
        assert baked == found, f"baked path {baked!r} != resolved {found!r}"


# ── the pjm that is running is the pjm the hook should call ─────────────────
#
# `/some/venv/bin/pjm init` with that venv off PATH used to bake whatever
# `pjm` PATH did find (an anaconda one, say), so commits were then captured by
# a different install than the one that wrote the hook.

def _fake_prefix(tmp_path: Path, exe: str, subdir: str) -> Path:
    prefix = tmp_path / "venv"
    (prefix / subdir).mkdir(parents=True)
    binary = prefix / subdir / exe
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return binary


def test_resolver_prefers_the_pjm_next_to_the_interpreter(tmp_path, monkeypatch) -> None:
    import projectmem.commands.hooks as hooks

    binary = _fake_prefix(tmp_path, "pjm", "bin")
    monkeypatch.setattr(hooks, "_is_windows", lambda: False)
    monkeypatch.setattr(hooks.sys, "prefix", str(binary.parent.parent))
    monkeypatch.setattr(hooks.shutil, "which", lambda name: f"/opt/anaconda3/bin/{name}")

    assert _resolve_pjm_binary() == binary.as_posix()


def test_resolver_falls_back_to_path_without_an_adjacent_pjm(tmp_path, monkeypatch) -> None:
    import projectmem.commands.hooks as hooks

    (tmp_path / "venv" / "bin").mkdir(parents=True)
    monkeypatch.setattr(hooks, "_is_windows", lambda: False)
    monkeypatch.setattr(hooks.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(hooks.shutil, "which", lambda name: "/usr/local/bin/pjm" if name == "pjm" else None)

    assert _resolve_pjm_binary() == "/usr/local/bin/pjm"


def test_resolver_skips_an_adjacent_pjm_that_is_not_executable(tmp_path, monkeypatch) -> None:
    import projectmem.commands.hooks as hooks

    binary = _fake_prefix(tmp_path, "pjm", "bin")
    binary.chmod(0o644)
    monkeypatch.setattr(hooks, "_is_windows", lambda: False)
    monkeypatch.setattr(hooks.sys, "prefix", str(binary.parent.parent))
    monkeypatch.setattr(hooks.shutil, "which", lambda name: "/usr/local/bin/pjm" if name == "pjm" else None)

    assert _resolve_pjm_binary() == "/usr/local/bin/pjm"


def test_resolver_finds_scripts_pjm_exe_on_windows(tmp_path, monkeypatch) -> None:
    import projectmem.commands.hooks as hooks

    binary = _fake_prefix(tmp_path, "pjm.exe", "Scripts")
    monkeypatch.setattr(hooks, "_is_windows", lambda: True)
    monkeypatch.setattr(hooks.sys, "prefix", str(binary.parent.parent))
    monkeypatch.setattr(hooks.shutil, "which", lambda name: r"C:\Anaconda3\Scripts\pjm.exe")

    assert _resolve_pjm_binary() == binary.as_posix()
    assert "\\" not in _resolve_pjm_binary()


def test_resolver_with_nothing_found_is_the_bare_name(tmp_path, monkeypatch) -> None:
    import projectmem.commands.hooks as hooks

    (tmp_path / "venv").mkdir()
    monkeypatch.setattr(hooks, "_is_windows", lambda: False)
    monkeypatch.setattr(hooks.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(hooks.shutil, "which", lambda name: None)

    assert _resolve_pjm_binary() == "pjm"


# ── a hook installed by an older release must pick up the current one ───────
#
# install_hooks() used to skip any hook that already had our marker, so the
# baked path, the #!/bin/sh shebang and the forward-slash paths of 0.3.3 never
# reached a project that had installed hooks earlier. The fixtures below are
# the text 0.3.2 wrote, byte for byte, with the pjm of a different install
# (anaconda) baked in.

OLD_SHEBANG = "#!/usr/bin/env bash\n"
OLD_POST_COMMIT = (
    '# >>> projectmem auto-capture >>>\n'
    '# Automatically captures development events into projectmem.\n'
    '# Installed by: pjm hooks install (or pjm init)\n'
    '# Remove with:  pjm hooks uninstall\n'
    'PJM_BIN="/opt/anaconda3/bin/pjm"\n'
    'if [ ! -x "$PJM_BIN" ]; then\n'
    '    PJM_BIN="$(command -v pjm 2>/dev/null || command -v projectmem 2>/dev/null)"\n'
    'fi\n'
    'if [ -d ".projectmem" ] && [ -n "$PJM_BIN" ]; then\n'
    '    "$PJM_BIN" _auto-capture "commit" >/dev/null 2>&1 &\n'
    'fi\n'
    '# <<< projectmem auto-capture <<<\n'
)
OLD_PRE_COMMIT = (
    '# >>> projectmem auto-capture >>>\n'
    '# Pre-commit warning check against project memory.\n'
    '# Installed by: pjm hooks install (or pjm init)\n'
    '# Remove with:  pjm hooks uninstall\n'
    '# Bypass once:  git commit --no-verify\n'
    'PJM_BIN="/opt/anaconda3/bin/pjm"\n'
    'if [ ! -x "$PJM_BIN" ]; then\n'
    '    PJM_BIN="$(command -v pjm 2>/dev/null || command -v projectmem 2>/dev/null)"\n'
    'fi\n'
    'if [ -d ".projectmem" ] && [ -n "$PJM_BIN" ]; then\n'
    '    "$PJM_BIN" precheck --level warn || true\n'
    'fi\n'
    '# <<< projectmem auto-capture <<<\n'
)


def _venv_pjm(tmp_path: Path, monkeypatch) -> str:
    import projectmem.commands.hooks as hooks

    binary = _fake_prefix(tmp_path, "pjm", "bin")
    monkeypatch.setattr(hooks, "_is_windows", lambda: False)
    monkeypatch.setattr(hooks.sys, "prefix", str(binary.parent.parent))
    return binary.as_posix()


def _hooks_dir(tmp_path: Path) -> Path:
    hooks_dir = tmp_path / "repo" / ".git" / "hooks"
    hooks_dir.mkdir(parents=True)
    return hooks_dir


def test_a_hook_baked_by_an_old_release_gets_the_new_path_and_shebang(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from projectmem.commands.hooks import HOOK_SHEBANG

    pjm = _venv_pjm(tmp_path, monkeypatch)
    hooks_dir = _hooks_dir(tmp_path)
    (hooks_dir / "post-commit").write_text(OLD_SHEBANG + OLD_POST_COMMIT, encoding="utf-8")
    (hooks_dir / "post-merge").write_text(
        OLD_SHEBANG + OLD_POST_COMMIT.replace('"commit"', '"merge"'), encoding="utf-8"
    )
    (hooks_dir / "pre-commit").write_text(OLD_SHEBANG + OLD_PRE_COMMIT, encoding="utf-8")

    install_hooks(hooks_dir)

    for name in ("post-commit", "post-merge", "pre-commit"):
        text = (hooks_dir / name).read_text(encoding="utf-8")
        assert text.startswith(HOOK_SHEBANG), f"{name} kept the old shebang"
        assert f'PJM_BIN="{pjm}"' in text
        assert "anaconda" not in text
        assert text.count(HOOK_MARKER_START) == 1
        assert os.access(hooks_dir / name, os.X_OK)
    assert 'precheck --level warn' in (hooks_dir / "pre-commit").read_text(encoding="utf-8")
    assert '_auto-capture "merge"' in (hooks_dir / "post-merge").read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "projectmem git hooks refreshed: post-commit, post-merge, pre-commit" in out
    assert "installed:" not in out


def test_a_users_own_lines_and_shebang_survive_a_refresh_byte_for_byte(
    tmp_path: Path, monkeypatch
) -> None:
    pjm = _venv_pjm(tmp_path, monkeypatch)
    hooks_dir = _hooks_dir(tmp_path)
    before = "#!/usr/bin/env bash\nset -e\necho 'lint first'\n\n"
    after = "\n# then tests\npytest -q  \n\ttrailing\n"
    (hooks_dir / "pre-commit").write_text(before + OLD_PRE_COMMIT + after, encoding="utf-8")

    install_hooks(hooks_dir)

    text = (hooks_dir / "pre-commit").read_text(encoding="utf-8")
    assert text.startswith(before), "the user's shebang or lines before the block changed"
    assert text.endswith(after), "the user's lines after the block changed"
    assert f'PJM_BIN="{pjm}"' in text
    assert text.count(HOOK_MARKER_START) == 1


def test_a_second_install_changes_nothing_and_says_so(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _venv_pjm(tmp_path, monkeypatch)
    hooks_dir = _hooks_dir(tmp_path)
    (hooks_dir / "post-commit").write_text(OLD_SHEBANG + OLD_POST_COMMIT, encoding="utf-8")
    (hooks_dir / "pre-commit").write_text("#!/bin/sh\necho mine\n" + OLD_PRE_COMMIT, encoding="utf-8")

    install_hooks(hooks_dir)
    first = {p.name: p.read_bytes() for p in hooks_dir.iterdir()}
    capsys.readouterr()
    install_hooks(hooks_dir)

    assert {p.name: p.read_bytes() for p in hooks_dir.iterdir()} == first
    assert "projectmem git hooks already installed." in capsys.readouterr().out


def test_a_block_with_no_end_marker_is_left_alone_with_a_warning(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _venv_pjm(tmp_path, monkeypatch)
    hooks_dir = _hooks_dir(tmp_path)
    broken = (
        OLD_SHEBANG + "echo mine\n"
        + OLD_PRE_COMMIT.replace("# <<< projectmem auto-capture <<<\n", "")
        + "echo also mine\n"
    )
    (hooks_dir / "pre-commit").write_text(broken, encoding="utf-8")

    install_hooks(hooks_dir)

    assert (hooks_dir / "pre-commit").read_text(encoding="utf-8") == broken
    captured = capsys.readouterr()
    assert "pre-commit" in captured.err and "end marker" in captured.err
    assert "already installed" not in captured.out
