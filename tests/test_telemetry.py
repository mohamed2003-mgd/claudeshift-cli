from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner

from claudeshift.cli import app
from claudeshift.config.paths import resolve_runtime_paths
from claudeshift.licensing.gumroad import build_signed_record, save_license_record
from claudeshift.telemetry.analytics import compute_usage_report
from claudeshift.telemetry.parser import TelemetryScanner

runner = CliRunner()


def _create_mock_logs(home: Path, now: datetime) -> Path:
    proj_dir = home / ".claude" / "projects" / "client-billing-api"
    proj_dir.mkdir(parents=True, exist_ok=True)
    log_file = proj_dir / "sess-001.jsonl"

    lines = [
        # Corrupt line should be skipped gracefully
        "{this is broken json",
        # Streamed duplicate message ID: first partial (200 output), then final (100000 output)
        json.dumps(
            {
                "timestamp": (now - timedelta(minutes=20)).isoformat(),
                "sessionId": "sess-001",
                "message": {
                    "id": "msg_dup_1",
                    "model": "claude-opus-5-5",
                    "usage": {
                        "input_tokens": 50000,
                        "output_tokens": 200,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            }
        ),
        json.dumps(
            {
                "timestamp": (now - timedelta(minutes=19)).isoformat(),
                "sessionId": "sess-001",
                "message": {
                    "id": "msg_dup_1",
                    "model": "claude-opus-5-5",
                    "usage": {
                        "input_tokens": 50000,
                        "output_tokens": 100000,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            }
        ),
        # Older event outside 5h rolling window (8 hours ago)
        json.dumps(
            {
                "timestamp": (now - timedelta(hours=8)).isoformat(),
                "sessionId": "sess-old",
                "message": {
                    "id": "msg_old_1",
                    "model": "claude-sonnet-5-5",
                    "usage": {
                        "input_tokens": 10000,
                        "output_tokens": 2000,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 5000,
                    },
                },
            }
        ),
    ]
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_file


def test_dedup_cache_and_rolling_window(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    now = datetime.now(timezone.utc)
    _create_mock_logs(home, now)

    scanner = TelemetryScanner(home, vault)
    events1, stats1 = scanner.scan(provider="claude")

    # 3 valid lines, 2 share msg_dup_1 -> must deduplicate to 2 events!
    assert len(events1) == 2
    assert stats1["parsed_fresh"] == 1
    assert stats1["cache_hits"] == 0

    # Second scan without file changes -> must hit incremental cache!
    events2, stats2 = scanner.scan(provider="claude")
    assert len(events2) == 2
    assert stats2["cache_hits"] == 1
    assert stats2["parsed_fresh"] == 0

    # Report check: Opus 50K in + 100K out = $0.75 + $7.50 = $8.25 in 5h window
    report = compute_usage_report(events1, plan_id="pro", custom_budget_usd=9.0, now_utc=now)
    assert report.window_requests == 1
    assert report.total_requests == 2
    assert report.window_cost_usd == 8.25
    assert report.alert_level == "CRITICAL"  # 8.25 / 9.0 = 91.7% >= 90%
    assert report.burn_usd_per_hour > 0
    assert report.eta_minutes is not None


def test_cli_usage_free_and_pro_gating(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    _create_mock_logs(home, datetime.now(timezone.utc))

    monkeypatch.setenv("CLAUDESHIFT_TARGET_HOME", str(home))
    monkeypatch.setenv("CLAUDESHIFT_VAULT_DIR", str(vault))

    # Free: usage works
    res_free = runner.invoke(app, ["usage"])
    assert res_free.exit_code == 0
    assert "Local Token & Quota Summary" in res_free.output

    # Free: monitor & export blocked with exit code 2
    res_mon_blocked = runner.invoke(app, ["monitor", "--once"])
    assert res_mon_blocked.exit_code == 2
    assert "PRO Feature" in res_mon_blocked.output

    out_csv = tmp_path / "report.csv"
    res_exp_blocked = runner.invoke(app, ["export", str(out_csv)])
    assert res_exp_blocked.exit_code == 2

    # Pro: activate signed license cache -> monitor --once & CSV/JSON export succeed
    paths = resolve_runtime_paths()
    save_license_record(
        paths.config_dir,
        build_signed_record("TEST-PRO-KEY-8888", "prod_test", "pro@example.com"),
    )

    res_mon_pro = runner.invoke(app, ["monitor", "--once"])
    assert res_mon_pro.exit_code == 0
    assert "Live Quota & Burn-Rate Radar" in res_mon_pro.output

    res_csv = runner.invoke(app, ["export", str(out_csv)])
    assert res_csv.exit_code == 0
    assert out_csv.is_file()
    assert "client-billing-api" in out_csv.read_text(encoding="utf-8")

    out_json = tmp_path / "report.json"
    res_json = runner.invoke(app, ["export", str(out_json)])
    assert res_json.exit_code == 0
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert len(data["events"]) == 2