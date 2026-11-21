from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from claudeshift import __version__
from claudeshift.cli import app

runner = CliRunner()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_doctor_sandbox_isolation(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_vault = tmp_path / "vault"
    fake_home.mkdir()
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("CLAUDESHIFT_TARGET_HOME", str(fake_home))
    monkeypatch.setenv("CLAUDESHIFT_VAULT_DIR", str(fake_vault))

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "SANDBOX MODE" in result.output
    assert "Claude Code" in result.output
    assert "DETECTED" in result.output


def test_doctor_empty_sandbox_shows_not_found(tmp_path: Path, monkeypatch) -> None:
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    monkeypatch.setenv("CLAUDESHIFT_TARGET_HOME", str(empty_home))
    monkeypatch.setenv("CLAUDESHIFT_VAULT_DIR", str(tmp_path / "vault"))

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "DETECTED" not in result.output