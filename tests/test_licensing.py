from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claudeshift.cli import app
from claudeshift.licensing.entitlements import get_entitlements
from claudeshift.licensing.gumroad import (
    LicenseError,
    activate_gumroad_license,
    build_signed_record,
    load_verified_license,
    save_license_record,
    verify_local_record,
)
from claudeshift.telemetry.pricing import estimate_event_cost_usd

runner = CliRunner()


class _FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_gumroad_activation_and_cli_lifecycle(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    home.mkdir()
    monkeypatch.setenv("CLAUDESHIFT_TARGET_HOME", str(home))
    monkeypatch.setenv("CLAUDESHIFT_VAULT_DIR", str(vault))
    monkeypatch.setenv("CLAUDESHIFT_GUMROAD_PRODUCT_ID", "prod_live_123")

    def fake_urlopen(req, timeout=12.0):
        return _FakeHTTPResponse(
            {
                "success": True,
                "uses": 1,
                "purchase": {
                    "email": "buyer@startup.io",
                    "product_name": "claudeshift Pro",
                    "refunded": False,
                    "chargebacked": False,
                },
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    # Initially FREE
    res_status_free = runner.invoke(app, ["license", "status"])
    assert res_status_free.exit_code == 0
    assert "FREE" in res_status_free.output

    # Activate via CLI
    res_act = runner.invoke(app, ["license", "activate", "ABCD-1234-5678-WXYZ"])
    assert res_act.exit_code == 0
    assert "PRO Activated" in res_act.output
    assert "ABCD-****-****-WXYZ" in res_act.output

    # Status now PRO
    res_status_pro = runner.invoke(app, ["license", "status"])
    assert res_status_pro.exit_code == 0
    assert "PRO (Verified)" in res_status_pro.output

    # Deactivate reverts to FREE
    res_deact = runner.invoke(app, ["license", "deactivate"])
    assert res_deact.exit_code == 0
    assert get_entitlements(vault / "_config").is_pro is False


def test_gumroad_rejects_refunded_and_http_404(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDESHIFT_GUMROAD_PRODUCT_ID", "prod_live_123")

    # Refunded purchase
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=12.0: _FakeHTTPResponse(
            {"success": True, "uses": 1, "purchase": {"email": "x@y.com", "refunded": True}}
        ),
    )
    with pytest.raises(LicenseError, match="refunded"):
        activate_gumroad_license(tmp_path, "KEY-REFUND-1234")

    # HTTP 404 invalid key from Gumroad
    def fake_404(req, timeout=12.0):
        raise urllib.error.HTTPError(
            url="https://api.gumroad.com/v2/licenses/verify",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(b'{"success": false, "message": "That license does not exist."}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_404)
    with pytest.raises(LicenseError, match="does not exist"):
        activate_gumroad_license(tmp_path, "FAKE-KEY-0000")


def test_tampered_license_json_is_rejected(tmp_path: Path) -> None:
    rec = build_signed_record("REAL-KEY-1111", "prod_1", "honest@dev.com")
    lpath = save_license_record(tmp_path, rec)

    # Valid before tampering
    loaded, reason = load_verified_license(tmp_path)
    assert loaded is not None and reason == "valid"

    # Tamper with grace_until date to try extending forever
    data = json.loads(lpath.read_text(encoding="utf-8"))
    data["grace_until"] = "2099-12-31T23:59:59+00:00"
    lpath.write_text(json.dumps(data), encoding="utf-8")

    loaded_after, reason_after = load_verified_license(tmp_path)
    assert loaded_after is None
    assert "signature" in reason_after.lower()


def test_offline_grace_expiration() -> None:
    old_time = datetime.now(timezone.utc) - timedelta(days=20)
    rec = build_signed_record("OLD-KEY-2222", "prod_1", "user@dev.com", now_utc=old_time)
    ok, reason = verify_local_record(rec)
    assert ok is False
    assert "expired" in reason.lower()


def test_claude_5_5_and_fable_pricing_accuracy() -> None:
    # 1M input tokens check for modern models
    assert estimate_event_cost_usd("claude-opus-5-5", 1_000_000, 0, 0, 0) == 15.0
    assert estimate_event_cost_usd("claude-sonnet-5-5", 1_000_000, 0, 0, 0) == 3.0
    assert estimate_event_cost_usd("claude-fable-5-2", 1_000_000, 0, 0, 0) == 5.0