from __future__ import annotations

import json
from pathlib import Path

import pytest

from claudeshift.config.paths import resolve_runtime_paths
from claudeshift.core.vault import VaultEngine, VaultError
from claudeshift.licensing.entitlements import ProFeatureRequiredError
from claudeshift.licensing.gumroad import build_signed_record, save_license_record


def _setup_sandbox(tmp_path: Path, monkeypatch, pro: bool = False) -> tuple[Path, VaultEngine]:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    home.mkdir()
    monkeypatch.setenv("CLAUDESHIFT_TARGET_HOME", str(home))
    monkeypatch.setenv("CLAUDESHIFT_VAULT_DIR", str(vault))

    paths = resolve_runtime_paths()
    if pro:
        rec = build_signed_record("TEST-PRO-KEY-9999", "prod_test", "pro@example.com")
        save_license_record(paths.config_dir, rec)
    return home, VaultEngine(paths)


def _write_claude_state(home: Path, email: str, model: str, mcps: list[str]) -> None:
    cdir = home / ".claude"
    cdir.mkdir(exist_ok=True)
    (cdir / "settings.json").write_text(json.dumps({"model": model}), encoding="utf-8")
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {"emailAddress": email, "organizationName": "TestOrg"},
                "mcpServers": {m: {"command": "npx"} for m in mcps},
            }
        ),
        encoding="utf-8",
    )


def test_save_switch_and_autosync_credentials(tmp_path: Path, monkeypatch) -> None:
    home, engine = _setup_sandbox(tmp_path, monkeypatch, pro=False)

    # Save 'work'
    _write_claude_state(home, "work@corp.com", "claude-opus-5-5", ["github", "postgres"])
    m_work = engine.save_profile("work", provider="claude")
    assert m_work.identity.account == "work@corp.com"
    assert m_work.identity.mcp_servers == ("github", "postgres")

    # Simulate switching to 'personal' manually and saving it
    _write_claude_state(home, "me@gmail.com", "claude-sonnet-5-5", ["filesystem"])
    engine.save_profile("personal", provider="claude")
    assert engine.get_active_profile("claude") == "personal"

    # Simulate OAuth refresh modifying personal's live file BEFORE switching back to work
    (home / ".claude" / "refreshed_token.txt").write_text("fresh-token-v2", encoding="utf-8")

    res = engine.use_profile("work", provider="claude")
    assert res.active_profile == "work"
    assert res.synced_previous is True
    live_cfg = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert live_cfg["oauthAccount"]["emailAddress"] == "work@corp.com"
    assert not (home / ".claude" / "refreshed_token.txt").exists()

    # Switch back to personal — refreshed token MUST still exist thanks to auto-sync!
    engine.use_profile("personal", provider="claude")
    assert (home / ".claude" / "refreshed_token.txt").read_text(encoding="utf-8") == "fresh-token-v2"


def test_free_tier_enforces_two_profile_limit(tmp_path: Path, monkeypatch) -> None:
    home, engine = _setup_sandbox(tmp_path, monkeypatch, pro=False)
    _write_claude_state(home, "a@test.com", "sonnet", [])
    engine.save_profile("p1")
    engine.save_profile("p2")

    with pytest.raises(ProFeatureRequiredError):
        engine.save_profile("p3")


def test_pro_unlimited_backups_restore_and_diff(tmp_path: Path, monkeypatch) -> None:
    home, engine = _setup_sandbox(tmp_path, monkeypatch, pro=True)

    _write_claude_state(home, "work@corp.com", "opus", ["github", "postgres"])
    engine.save_profile("work")

    _write_claude_state(home, "home@me.com", "sonnet", ["github", "obsidian"])
    engine.save_profile("personal")

    _write_claude_state(home, "client@agency.io", "sonnet", ["stripe"])
    engine.save_profile("client")  # 3rd profile allowed in Pro!

    # Switch triggers rolling backup
    res = engine.use_profile("work")
    assert res.backup_id is not None

    backups = engine.list_backups("claude")
    assert len(backups) == 1

    # Diff work vs personal
    d = engine.diff_profiles("work", "personal")
    assert d.only_in_a_mcp == ("postgres",)
    assert d.only_in_b_mcp == ("obsidian",)
    assert d.shared_mcp == ("github",)

    # Restore pre-switch backup (which was 'client')
    engine.restore_backup(backups[0].backup_id)
    restored = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert restored["oauthAccount"]["emailAddress"] == "client@agency.io"


def test_sha256_integrity_blocks_corrupted_profile(tmp_path: Path, monkeypatch) -> None:
    home, engine = _setup_sandbox(tmp_path, monkeypatch, pro=False)
    _write_claude_state(home, "safe@test.com", "sonnet", [])
    engine.save_profile("safe")

    # Tamper with stored payload file inside vault
    tampered = engine.profiles_root / "claude" / "safe" / "payload" / ".claude.json"
    tampered.write_text('{"corrupted": true}', encoding="utf-8")

    with pytest.raises(VaultError, match="SHA-256 mismatch"):
        engine.use_profile("safe")