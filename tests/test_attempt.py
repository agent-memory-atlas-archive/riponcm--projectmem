"""`pjm attempt` / `pjm fix` must never write against an issue that does not exist.

An attempt appended to a mistyped id left an issue group with no `issue`
event, and the next summary rebuild raised StopIteration — so every later
write failed. A stale `.current_issue` marker led to the same orphan writes.
"""
from __future__ import annotations

from typer.testing import CliRunner

from projectmem.cli import app
from projectmem.storage import read_events


def test_attempt_on_an_unknown_issue_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["init"], catch_exceptions=False)
    runner.invoke(app, ["log", "real issue"], catch_exceptions=False)

    result = runner.invoke(app, ["attempt", "--issue", "0099", "--failed", "typo id"])
    assert result.exit_code != 0
    assert "#0099 was not found" in str(result.exception)

    # Nothing was appended, and the project still works afterwards.
    assert not any(e.issue_id == "0099" for e in read_events(tmp_path))
    result = runner.invoke(app, ["note", "still works"])
    assert result.exit_code == 0, result.output


def test_attempt_issue_id_is_normalised_like_fix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["init"], catch_exceptions=False)
    runner.invoke(app, ["log", "real issue"], catch_exceptions=False)

    result = runner.invoke(app, ["attempt", "--issue", "1", "--failed", "short id"])
    assert result.exit_code == 0, result.output
    assert "#0001" in result.output


def test_a_stale_current_issue_marker_is_ignored(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["init"], catch_exceptions=False)
    (tmp_path / ".projectmem" / ".current_issue").write_text("0042", encoding="utf-8")

    result = runner.invoke(app, ["attempt", "--failed", "no real issue yet"])

    # The marker names an issue that does not exist, so it is ignored and the
    # usual "no active issue" guidance applies instead of an orphan attempt.
    assert result.exit_code != 0
    assert "No active issue" in str(result.exception)
    assert not any(e.issue_id == "0042" for e in read_events(tmp_path))


def test_fix_ignores_and_clears_a_stale_marker(tmp_path, monkeypatch):
    """The marker guard has to cover `fix` too: a marker naming a missing
    issue used to be consumed by the next `pjm fix`, recording a fix for an
    issue that never existed while the real open issue stayed open."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["init"], catch_exceptions=False)
    runner.invoke(app, ["log", "real issue"], catch_exceptions=False)
    marker = tmp_path / ".projectmem" / ".current_issue"
    marker.write_text("0777", encoding="utf-8")

    result = runner.invoke(app, ["fix", "fixed the real one"])

    assert result.exit_code == 0, result.output
    assert "#0001" in result.output
    assert not any(e.issue_id == "0777" for e in read_events(tmp_path))
    assert not marker.exists()


def test_a_bare_hash_is_reported_readably(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["init"], catch_exceptions=False)

    result = runner.invoke(app, ["attempt", "--issue", "#", "--failed", "x"])

    assert result.exit_code != 0
    assert "##" not in str(result.exception)
