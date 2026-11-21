from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claudeshift.cli import app
from claudeshift.config.paths import resolve_runtime_paths
from claudeshift.core.vault import MAX_ROLLING_BACKUPS, VaultEngine, VaultError
from claudeshift.licensing.entitlements import get_entitlements
from claudeshift.licensing.gumroad import (
    build_signed_record,
    load_verified_license,
    save_license_record,
)
from claudeshift.telemetry import pricing as pricing_mod
from claudeshift.telemetry.analytics import compute_usage_report
from claudeshift.telemetry.parser import TelemetryScanner, UsageEvent

runner = CliRunner()


def _init_env(tmp_path: Path, monkeypatch, pro: bool = True) -> tuple[Path, Path, VaultEngine]:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    home.mkdir()
    monkeypatch.setenv("CLAUDESHIFT_TARGET_HOME", str(home))
    monkeypatch.setenv("CLAUDESHIFT_VAULT_DIR", str(vault))
    paths = resolve_runtime_paths()
    if pro:
        save_license_record(
            paths.config_dir,
            build_signed_record("STRESS-KEY-0001", "prod_stress", "qa@claudeshift.dev"),
        )
    return home, vault, VaultEngine(paths)


def _write_claude(home: Path, email: str, secret_token: str = "sk-ant-oat01-SUPERSECRET999") -> None:
    cdir = home / ".claude"
    cdir.mkdir(exist_ok=True)
    (cdir / "settings.json").write_text(
        json.dumps({"model": "claude-opus-5-5", "theme": "dark"}), encoding="utf-8"
    )
    (cdir / ".credentials.json").write_text(
        json.dumps({"accessToken": secret_token, "refreshToken": "rt-NEVER-PRINT-ME"}),
        encoding="utf-8",
    )
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {"emailAddress": email, "organizationName": "HardeningOrg"},
                "apiKey": secret_token,
                "mcpServers": {"postgres": {"command": "npx"}},
            }
        ),
        encoding="utf-8",
    )


def test_crash_mid_swap_triggers_full_rollback(tmp_path: Path, monkeypatch) -> None:
    """Simulate an OS lock/crash mid-swap and prove live files remain 100% untouched."""
    home, _, engine = _init_env(tmp_path, monkeypatch, pro=True)

    _write_claude(home, "profile-a@corp.com")
    engine.save_profile("alpha")

    _write_claude(home, "profile-b@corp.com")
    (home / ".claude" / "important_note.txt").write_text("DO-NOT-LOSE", encoding="utf-8")
    engine.save_profile("beta")

    # Inject failure when renaming the 2nd staged target into live home
    orig_rename = Path.rename
    stage_moves = {"count": 0}

    def flaky_rename(self: Path, target: Path):
        if ".claudeshift_stage_" in str(self.parent):
            stage_moves["count"] += 1
            if stage_moves["count"] == 2:
                raise PermissionError("Simulated Windows file lock by running Claude process")
        return orig_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)

    with pytest.raises(VaultError, match="safely rolled back"):
        engine.use_profile("alpha", sync_current=False)

    # Restore real rename and verify beta's live files are completely intact
    monkeypatch.setattr(Path, "rename", orig_rename)
    live_cfg = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert live_cfg["oauthAccount"]["emailAddress"] == "profile-b@corp.com"
    assert (home / ".claude" / "important_note.txt").read_text(encoding="utf-8") == "DO-NOT-LOSE"
    assert engine.get_active_profile("claude") == "beta"
    # Ensure no leftover stage/rollback junk folders remain
    leftovers = [p.name for p in home.iterdir() if p.name.startswith(".claudeshift_")]
    assert leftovers == []


def test_zero_secret_leak_across_cli_commands(tmp_path: Path, monkeypatch) -> None:
    """Ensure OAuth tokens, refresh tokens, and API keys NEVER appear in CLI stdout."""
    home, _, _ = _init_env(tmp_path, monkeypatch, pro=True)
    secret = "sk-ant-oat01-SUPERSECRET999"
    _write_claude(home, "secure@corp.com", secret_token=secret)

    for cmd in (
        ["doctor"],
        ["save", "sec1"],
        ["save", "sec2"],
        ["list"],
        ["diff", "sec1", "sec2"],
        ["current"],
    ):
        res = runner.invoke(app, cmd)
        assert res.exit_code == 0, f"Command {cmd} failed: {res.output}"
        assert secret not in res.output
        assert "rt-NEVER-PRINT-ME" not in res.output


@pytest.mark.parametrize(
    "bad_name",
    ["../escape", "a/b", "a\\b", "CON", "nul", "COM1", "LPT9", "con.txt", "default", "backup", ""],
)
def test_profile_name_sanitization_blocks_traversal_and_windows_reserved(
    tmp_path: Path, monkeypatch, bad_name: str
) -> None:
    home, _, engine = _init_env(tmp_path, monkeypatch, pro=True)
    _write_claude(home, "safe@corp.com")
    with pytest.raises(VaultError):
        engine.save_profile(bad_name)


def test_vault_lock_contention_and_stale_lock_recovery(tmp_path: Path, monkeypatch) -> None:
    home, _, engine = _init_env(tmp_path, monkeypatch, pro=True)
    _write_claude(home, "lock@corp.com")
    engine.vault_root.mkdir(parents=True, exist_ok=True)

    # 1. Fresh lock held by another process -> must reject cleanly
    engine.lock_file.write_text("pid=99999", encoding="utf-8")
    with pytest.raises(VaultError, match="locked by another claudeshift process"), engine._locked(
        timeout_sec=0.15
    ):
        pass

    # 2. Stale lock (> 60s old) -> must auto-recover and succeed
    old_ts = time.time() - 120
    os.utime(engine.lock_file, (old_ts, old_ts))
    manifest = engine.save_profile("recovered")
    assert manifest.name == "recovered"
    assert not engine.lock_file.exists()


def test_multi_provider_isolation_and_per_provider_free_limit(tmp_path: Path, monkeypatch) -> None:
    """Switching codex/gemini must never disturb claude; Free limit applies per provider."""
    home, _, engine = _init_env(tmp_path, monkeypatch, pro=False)

    _write_claude(home, "c1@corp.com")
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text('model = "o3"\n', encoding="utf-8")
    (home / ".codex" / "auth.json").write_text(json.dumps({"account": "codex1@corp.com"}), encoding="utf-8")

    (home / ".gemini").mkdir()
    (home / ".gemini" / "settings.json").write_text(
        json.dumps({"selectedAuthType": "oauth-work"}), encoding="utf-8"
    )

    # Save 2 profiles in claude AND 2 profiles in codex under FREE tier (must succeed!)
    engine.save_profile("c1", provider="claude")
    engine.save_profile("c2", provider="claude")
    engine.save_profile("x1", provider="codex")
    (home / ".codex" / "auth.json").write_text(json.dumps({"account": "codex2@corp.com"}), encoding="utf-8")
    engine.save_profile("x2", provider="codex")
    engine.save_profile("g1", provider="gemini")

    # Switch codex back to x1 -> claude active profile & live files must be untouched
    engine.use_profile("x1", provider="codex")
    assert engine.get_active_profile("claude") == "c2"
    assert engine.get_active_profile("codex") == "x1"
    codex_live = json.loads((home / ".codex" / "auth.json").read_text(encoding="utf-8"))
    assert codex_live["account"] == "codex1@corp.com"


def test_rolling_backups_prune_to_max_limit(tmp_path: Path, monkeypatch) -> None:
    home, _, engine = _init_env(tmp_path, monkeypatch, pro=True)
    _write_claude(home, "a@corp.com")
    engine.save_profile("a")
    _write_claude(home, "b@corp.com")
    engine.save_profile("b")

    # Perform 12 switches -> should keep only MAX_ROLLING_BACKUPS (10)
    for i in range(12):
        engine.use_profile("a" if i % 2 == 0 else "b", sync_current=False)

    backups = engine.list_backups("claude")
    assert len(backups) == MAX_ROLLING_BACKUPS


def test_telemetry_5000_events_benchmark_and_incremental_cache(tmp_path: Path) -> None:
    """Stress-test parser with 20 files x 250 events = 5,000 events and verify incremental cache."""
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    now = datetime.now(timezone.utc)

    for f_idx in range(20):
        pdir = home / ".claude" / "projects" / f"D--workspace--service-{f_idx % 4}"
        pdir.mkdir(parents=True, exist_ok=True)
        lines = []
        for e_idx in range(250):
            ts = (now - timedelta(minutes=e_idx % 180)).isoformat()
            lines.append(
                json.dumps(
                    {
                        "timestamp": ts,
                        "sessionId": f"sess-{f_idx}",
                        "message": {
                            "id": f"m-{f_idx}-{e_idx}",
                            "model": "claude-sonnet-5-5" if e_idx % 2 else "claude-fable-5-2",
                            "usage": {
                                "input_tokens": 1000,
                                "output_tokens": 300,
                                "cache_creation_input_tokens": 0,
                                "cache_read_input_tokens": 2000,
                            },
                        },
                    }
                )
            )
        (pdir / f"log_{f_idx:02d}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    scanner = TelemetryScanner(home, vault)

    # Cold scan (5,000 events)
    t0 = time.perf_counter()
    events_cold, stats_cold = scanner.scan(provider="claude")
    cold_ms = (time.perf_counter() - t0) * 1000
    assert len(events_cold) == 5000
    assert stats_cold["parsed_fresh"] == 20
    assert stats_cold["cache_hits"] == 0
    assert cold_ms < 4000, f"Cold scan too slow: {cold_ms:.1f}ms"

    # Warm scan (0 files changed)
    t1 = time.perf_counter()
    events_warm, stats_warm = scanner.scan(provider="claude")
    warm_ms = (time.perf_counter() - t1) * 1000
    assert len(events_warm) == 5000
    assert stats_warm["cache_hits"] == 20
    assert stats_warm["parsed_fresh"] == 0
    assert warm_ms < 1000, f"Warm cache scan too slow: {warm_ms:.1f}ms"

    # Mutate ONLY 1 file -> must reparse exactly 1 file and reuse 19 from cache!
    one_file = home / ".claude" / "projects" / "D--workspace--service-0" / "log_00.jsonl"
    with one_file.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "timestamp": now.isoformat(),
                    "sessionId": "sess-0",
                    "message": {
                        "id": "m-extra-new",
                        "model": "claude-opus-5-5",
                        "usage": {"input_tokens": 500, "output_tokens": 250},
                    },
                }
            )
            + "\n"
        )

    events_inc, stats_inc = scanner.scan(provider="claude")
    assert len(events_inc) == 5001
    assert stats_inc["parsed_fresh"] == 1
    assert stats_inc["cache_hits"] == 19


def test_combined_claude_and_codex_all_provider_scan(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    now = datetime.now(timezone.utc).isoformat()

    cdir = home / ".claude" / "projects" / "proj-a"
    cdir.mkdir(parents=True)
    (cdir / "c.jsonl").write_text(
        json.dumps(
            {
                "timestamp": now,
                "message": {"id": "c1", "model": "claude-sonnet-5-5", "usage": {"input_tokens": 1000, "output_tokens": 500}},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    xdir = home / ".codex" / "sessions" / "2026"
    xdir.mkdir(parents=True)
    (xdir / "x.jsonl").write_text(
        json.dumps(
            {
                "timestamp": now,
                "session_id": "cx-1",
                "payload": {"model": "o3", "usage": {"input_tokens": 2000, "output_tokens": 800}},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    scanner = TelemetryScanner(home, vault)
    events, stats = scanner.scan(provider="all")
    assert stats["files_scanned"] == 2
    assert {e.provider for e in events} == {"claude", "codex"}


@pytest.mark.parametrize(
    ("cost", "budget", "expected_alert"),
    [
        (7.49, 10.0, "OK"),
        (7.50, 10.0, "WARNING"),
        (8.99, 10.0, "WARNING"),
        (9.00, 10.0, "CRITICAL"),
        (15.0, 10.0, "CRITICAL"),
    ],
)
def test_alert_threshold_boundaries(cost: float, budget: float, expected_alert: str) -> None:
    now = datetime.now(timezone.utc)
    ev = UsageEvent(
        provider="claude",
        timestamp=now.isoformat(timespec="seconds"),
        epoch=now.timestamp(),
        session_id="s1",
        project="p1",
        model="claude-opus-5-5",
        input_tokens=1000,
        output_tokens=1000,
        cache_write_tokens=0,
        cache_read_tokens=0,
        cost_usd=cost,
    )
    report = compute_usage_report([ev], plan_id="pro", custom_budget_usd=budget, now_utc=now)
    assert report.alert_level == expected_alert


def test_custom_pricing_overrides_future_models(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "pricing_overrides.json").write_text(
        json.dumps(
            {
                "claude-nova-6": {
                    "input_per_m": 20.0,
                    "output_per_m": 100.0,
                    "cache_write_per_m": 25.0,
                    "cache_read_per_m": 2.0,
                }
            }
        ),
        encoding="utf-8",
    )
    try:
        pricing_mod.load_pricing_overrides(cfg)
        cost = pricing_mod.estimate_event_cost_usd("claude-nova-6-preview", 500_000, 100_000, 0, 0)
        assert cost == 20.0  # $10 input + $10 output
    finally:
        pricing_mod._CUSTOM_RATES.clear()


def test_corrupted_license_file_and_foreign_machine_graceful_fallback(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()

    # 1. Corrupted JSON -> gracefully degrades to FREE, never crashes
    (cfg / "license.json").write_text("{corrupt!!", encoding="utf-8")
    ent = get_entitlements(cfg)
    assert ent.is_pro is False
    assert ent.license_source == "corrupted_license_file"

    # 2. License copied from another machine -> rejected by machine binding
    rec = build_signed_record("KEY-FOREIGN-1234", "prod_1", "copy@test.com")
    data = rec.to_dict()
    data["machine_id"] = "other-machine-fingerprint"
    (cfg / "license.json").write_text(json.dumps(data), encoding="utf-8")
    loaded, reason = load_verified_license(cfg)
    assert loaded is None
    assert "different machine" in reason.lower()