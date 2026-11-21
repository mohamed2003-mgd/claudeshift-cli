from __future__ import annotations

import csv
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claudeshift.cli import app
from claudeshift.config.paths import resolve_runtime_paths
from claudeshift.core.vault import VaultEngine, VaultError, _validate_no_escaping_symlinks
from claudeshift.licensing import gumroad as gumroad_mod
from claudeshift.telemetry import pricing as pricing_mod
from claudeshift.telemetry.exporter import export_csv
from claudeshift.telemetry.parser import TelemetryScanner, UsageEvent

runner = CliRunner()


def _make_pro_engine(tmp_path: Path, monkeypatch) -> tuple[Path, Path, VaultEngine]:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    home.mkdir()
    monkeypatch.setenv("CLAUDESHIFT_TARGET_HOME", str(home))
    monkeypatch.setenv("CLAUDESHIFT_VAULT_DIR", str(vault))
    paths = resolve_runtime_paths()
    rec = gumroad_mod.build_signed_record("REDTEAM-PRO-KEY", "prod_rt", "audit@claudeshift.dev")
    gumroad_mod.save_license_record(paths.config_dir, rec)
    return home, vault, VaultEngine(paths)


def _seed_claude(home: Path, email: str) -> None:
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"model": "claude-opus-5.5"}), encoding="utf-8")
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": email}}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 1. AUTH & LICENSING RED-TEAM ATTACKS
# ---------------------------------------------------------------------------


def test_pinned_production_product_id_blocks_spoofing_and_env_override(
    tmp_path: Path, monkeypatch
) -> None:
    """Once pinned in production, attacker CANNOT bypass licensing via env var or $0 product."""
    monkeypatch.setattr(gumroad_mod, "DEFAULT_GUMROAD_PRODUCT_ID", "prod_OFFICIAL_CLAUDESHIFT")
    monkeypatch.setenv("CLAUDESHIFT_GUMROAD_PRODUCT_ID", "prod_ATTACKER_FREE_0_USD")

    # Authoritative ID must ignore attacker's env override
    assert gumroad_mod.get_gumroad_product_id() == "prod_OFFICIAL_CLAUDESHIFT"

    # A locally signed license for attacker's product ID MUST be rejected
    forged_rec = gumroad_mod.build_signed_record(
        "ATTACKER-KEY-0000", "prod_ATTACKER_FREE_0_USD", "attacker@evil.com"
    )
    gumroad_mod.save_license_record(tmp_path, forged_rec)

    loaded, reason = gumroad_mod.load_verified_license(tmp_path)
    assert loaded is None
    assert "different product id" in reason.lower()


def test_cli_license_activate_rejects_removed_product_id_flag() -> None:
    """--product-id flag must not exist on CLI to prevent activation spoofing."""
    res = runner.invoke(app, ["license", "activate", "SOME-KEY", "--product-id", "evil"])
    assert res.exit_code != 0


# ---------------------------------------------------------------------------
# 2. PATH TRAVERSAL & MANIFEST POISONING ATTACKS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "evil_backup_id",
    [
        "../../profiles/claude/work",
        "..\\..\\profiles\\claude\\work",
        "/etc/passwd",
        "C:\\Windows\\System32",
        "20261121_120000_abcdef/../../evil",
        "",
        "not-a-valid-id",
    ],
)
def test_backup_restore_blocks_path_traversal(tmp_path: Path, monkeypatch, evil_backup_id: str) -> None:
    _, _, engine = _make_pro_engine(tmp_path, monkeypatch)
    with pytest.raises(VaultError, match="Invalid backup ID"):
        engine.restore_backup(evil_backup_id, provider="claude")


def test_manifest_checksum_path_traversal_poisoning_blocked(tmp_path: Path, monkeypatch) -> None:
    """Poisoning manifest.json checksum keys with '../' must be blocked before reading outside vault."""
    home, _, engine = _make_pro_engine(tmp_path, monkeypatch)
    _seed_claude(home, "victim@corp.com")
    engine.save_profile("work")

    # Plant external file outside payload dir
    secret_outside = engine.vault_root / "outside_secret.txt"
    secret_outside.write_text("TOP_SECRET", encoding="utf-8")

    # Poison manifest.json with traversal path
    mfile = engine.profiles_root / "claude" / "work" / "manifest.json"
    data = json.loads(mfile.read_text(encoding="utf-8"))
    data["checksums"]["../../../../outside_secret.txt"] = "deadbeef"
    mfile.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(VaultError, match="Security violation"):
        engine.use_profile("work", sync_current=False)


def test_tampered_backup_payload_blocked_by_sha256_on_restore(tmp_path: Path, monkeypatch) -> None:
    """If someone tampers with a stored safety backup, restore MUST abort and leave live files intact."""
    home, _, engine = _make_pro_engine(tmp_path, monkeypatch)
    _seed_claude(home, "v1@corp.com")
    engine.save_profile("p1")
    _seed_claude(home, "v2@corp.com")
    engine.save_profile("p2")

    res = engine.use_profile("p1", sync_current=False)
    assert res.backup_id is not None

    # Tamper with backup payload
    tampered = engine.backups_root / "claude" / res.backup_id / "payload" / ".claude.json"
    tampered.write_text('{"oauthAccount": {"emailAddress": "hacked@evil.com"}}', encoding="utf-8")

    with pytest.raises(VaultError, match="SHA-256 mismatch"):
        engine.restore_backup(res.backup_id, provider="claude")

    # Live home MUST still hold v1 untouched
    live = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert live["oauthAccount"]["emailAddress"] == "v1@corp.com"


# ---------------------------------------------------------------------------
# 3. SYMLINK ESCAPE ATTACK
# ---------------------------------------------------------------------------


def test_symlink_escape_attack_blocked(tmp_path: Path, monkeypatch) -> None:
    """A symlink inside .claude pointing outside target_home must NEVER be exfiltrated into vault."""
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_secret = outside_dir / "id_rsa"
    outside_secret.write_text("PRIVATE-KEY-DO-NOT-COPY", encoding="utf-8")

    home, _, engine = _make_pro_engine(tmp_path, monkeypatch)
    _seed_claude(home, "user@corp.com")

    evil_link = home / ".claude" / "stolen_key"
    try:
        evil_link.symlink_to(outside_secret)
    except OSError:
        # Windows without Developer Mode / Admin blocks creating real symlinks -> verify guard via mock
        fake_file = home / ".claude" / "stolen_key"
        fake_file.write_text("placeholder", encoding="utf-8")
        orig_is_symlink = Path.is_symlink
        orig_resolve = Path.resolve

        monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "stolen_key" or orig_is_symlink(self))
        monkeypatch.setattr(
            Path,
            "resolve",
            lambda self, strict=False: outside_secret if self.name == "stolen_key" else orig_resolve(self, strict=strict),
        )
        with pytest.raises(VaultError, match="symlink.*escapes"):
            _validate_no_escaping_symlinks(home / ".claude", home)
        return

    with pytest.raises(VaultError, match="symlink.*escapes"):
        engine.save_profile("compromised")


# ---------------------------------------------------------------------------
# 4. CSV FORMULA INJECTION & ATOMIC EXPORT
# ---------------------------------------------------------------------------


def test_csv_export_neutralizes_excel_formula_injection(tmp_path: Path) -> None:
    malicious_events = [
        UsageEvent(
            provider="claude",
            timestamp="2026-11-21T12:00:00+00:00",
            epoch=1795176000.0,
            session_id="+SUM(1,2)",
            project='=CMD|\' /C calc\'!A0',
            model="@malicious-model",
            input_tokens=1000,
            output_tokens=500,
            cache_write_tokens=0,
            cache_read_tokens=0,
            cost_usd=0.01,
        ),
        UsageEvent(
            provider="claude",
            timestamp="2026-11-21T12:01:00+00:00",
            epoch=1795176060.0,
            session_id="-2+3",
            project="safe-project",
            model="gemini-3.8-flash",
            input_tokens=1000,
            output_tokens=500,
            cache_write_tokens=0,
            cache_read_tokens=0,
            cost_usd=0.001,
        ),
    ]
    out_csv = tmp_path / "billing.csv"
    export_csv(malicious_events, out_csv)

    with out_csv.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))

    assert rows[0]["project"].startswith("'=")
    assert rows[0]["session_id"].startswith("'+")
    assert rows[0]["model"].startswith("'@")
    assert rows[1]["session_id"].startswith("'-")
    # Normal text must NOT be altered
    assert rows[1]["project"] == "safe-project"
    assert rows[1]["model"] == "gemini-3.8-flash"


# ---------------------------------------------------------------------------
# 5. GEMINI 3.1 / 3.8 & VERSION-AGNOSTIC PRICING + POISONING REJECTION
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected_in_per_m", "expected_out_per_m"),
    [
        ("gemini-3.1-pro", 2.50, 15.00),
        ("gemini-3.1-pro-preview-05-14", 2.50, 15.00),
        ("gemini-3.8-flash", 0.30, 1.20),
        ("gemini-3.8-flash-lite", 0.10, 0.40),
        ("gemini-3.5-ultra", 5.00, 25.00),
        ("claude-opus-5.5", 15.00, 75.00),
        ("claude-sonnet-5.5", 3.00, 15.00),
        ("claude-fable-5.2", 5.00, 25.00),
        ("gpt-6-astra", 5.00, 20.00),
        ("gpt-6-luna", 1.50, 6.00),
        ("gpt-6.2-mini", 0.60, 2.40),
        ("gpt-6.2", 3.00, 12.00),
    ],
)
def test_versioned_2026_models_price_accurately(
    model: str, expected_in_per_m: float, expected_out_per_m: float
) -> None:
    rate = pricing_mod.resolve_model_rate(model)
    assert rate.estimated is False
    assert rate.input_per_m == expected_in_per_m
    assert rate.output_per_m == expected_out_per_m


def test_pricing_overrides_reject_nan_inf_and_negative_poison(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "pricing_overrides.json").write_text(
        json.dumps(
            {
                "bad-negative": {"input_per_m": -50.0, "output_per_m": 10.0},
                "bad-string": {"input_per_m": "free", "output_per_m": 10.0},
                "bad-huge": {"input_per_m": 9999999.0, "output_per_m": 10.0},
                "valid-custom": {"input_per_m": 1.25, "output_per_m": 5.0, "cache_write_per_m": 1.25, "cache_read_per_m": 0.25},
            }
        ),
        encoding="utf-8",
    )
    try:
        pricing_mod.load_pricing_overrides(cfg)
        assert "bad-negative" not in pricing_mod._CUSTOM_RATES
        assert "bad-string" not in pricing_mod._CUSTOM_RATES
        assert "bad-huge" not in pricing_mod._CUSTOM_RATES
        assert pricing_mod.resolve_model_rate("valid-custom").input_per_m == 1.25
    finally:
        pricing_mod._CUSTOM_RATES.clear()


# ---------------------------------------------------------------------------
# 6. PARSER FUZZING, UNICODE / ARABIC PATHS, AND CORRUPTION RESILIENCE
# ---------------------------------------------------------------------------


def test_parser_survives_fuzzed_garbage_bom_nulls_and_arabic_projects(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    now = datetime.now(timezone.utc).isoformat()

    pdir = home / ".claude" / "projects" / "مشروع_التجربة_2026"
    pdir.mkdir(parents=True)

    fuzzed_lines = [
        "\ufeff" + json.dumps({"timestamp": now, "message": {"id": "ok1", "model": "gemini-3.8-flash", "usage": {"input_tokens": 1000, "output_tokens": 500}}}),
        "",
        "   ",
        "\x00\x01\xff\xfe binary garbage",
        "[]",
        '"just a json string"',
        json.dumps({"message": {"id": "bad_types", "model": "claude-opus-5.5", "usage": {"input_tokens": "not-a-number", "output_tokens": -9999, "cache_read_input_tokens": True}}}),
        json.dumps({"timestamp": 999999999999999999, "message": {"id": "overflow_ts", "model": "claude-sonnet-5.5", "usage": {"input_tokens": 500, "output_tokens": 200}}}),
    ]
    (pdir / "chaos.jsonl").write_text("\n".join(fuzzed_lines) + "\n", encoding="utf-8")

    scanner = TelemetryScanner(home, vault)
    events, stats = scanner.scan(provider="claude")

    # Only the 2 valid non-zero events should survive; zero crashes!
    assert stats["files_scanned"] == 1
    assert len(events) == 2
    assert all(e.project == "مشروع_التجربة_2026" for e in events)
    assert all(e.cost_usd >= 0 for e in events)


# ---------------------------------------------------------------------------
# 7. CONCURRENCY TORTURE TEST (MULTI-THREADED VAULT CONTENTION)
# ---------------------------------------------------------------------------


def test_concurrent_threads_never_corrupt_vault_or_state(tmp_path: Path, monkeypatch) -> None:
    home, _, engine = _make_pro_engine(tmp_path, monkeypatch)
    _seed_claude(home, "a@corp.com")
    engine.save_profile("alpha")
    _seed_claude(home, "b@corp.com")
    engine.save_profile("beta")

    errors: list[Exception] = []

    def worker(idx: int) -> None:
        target = "alpha" if idx % 2 == 0 else "beta"
        try:
            engine.use_profile(target, sync_current=False)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Unexpected concurrency failures: {errors}"
    active = engine.get_active_profile("claude")
    assert active in {"alpha", "beta"}

    # Live file must be valid JSON matching the active profile
    live = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    expected_email = "a@corp.com" if active == "alpha" else "b@corp.com"
    assert live["oauthAccount"]["emailAddress"] == expected_email
    # Zero leftover temp/stage/lock files
    assert not engine.lock_file.exists()
    leftovers = [p.name for p in home.iterdir() if p.name.startswith(".claudeshift_")]
    assert leftovers == []