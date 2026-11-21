from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from claudeshift.providers.plan_detector import detect_active_plan
from claudeshift.telemetry.parser import TelemetryScanner
from claudeshift.telemetry.pricing import (
    estimate_event_cost_usd,
    load_pricing_overrides,
    resolve_model_rate,
    sync_live_pricing_catalog,
)


def _make_fake_jwt(claims: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{header}.{body}.fakesig"


def test_codex_stateful_model_project_and_dedup(tmp_path: Path) -> None:
    """Codex turn_context sets real model & cwd; duplicate token_count heartbeats are ignored."""
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    sdir = home / ".codex" / "sessions" / "2026" / "11" / "21"
    sdir.mkdir(parents=True)

    now = datetime.now(timezone.utc).isoformat()
    lines = [
        json.dumps(
            {
                "timestamp": now,
                "type": "session_meta",
                "payload": {"id": "sess-astra-1", "cwd": "D:\\projects\\my-awesome-api"},
            }
        ),
        json.dumps(
            {
                "timestamp": now,
                "type": "turn_context",
                "payload": {"model": "gpt-6-astra", "cwd": "D:\\projects\\my-awesome-api"},
            }
        ),
        json.dumps(
            {
                "timestamp": now,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {"plan_type": "go"},
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 10000,
                            "cached_input_tokens": 4000,
                            "output_tokens": 1000,
                        },
                        "last_token_usage": {
                            "input_tokens": 10000,
                            "cached_input_tokens": 4000,
                            "output_tokens": 1000,
                        },
                    },
                },
            }
        ),
        json.dumps(
            {
                "timestamp": now,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 10000,
                            "cached_input_tokens": 4000,
                            "output_tokens": 1000,
                        },
                        "last_token_usage": {
                            "input_tokens": 10000,
                            "cached_input_tokens": 4000,
                            "output_tokens": 1000,
                        },
                    },
                },
            }
        ),
    ]
    (sdir / "rollout-001.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    scanner = TelemetryScanner(home, vault)
    events, stats = scanner.scan(provider="codex")

    assert len(events) == 1
    assert events[0].model == "gpt-6-astra"
    assert events[0].project == "my-awesome-api"
    assert events[0].input_tokens == 6000
    assert events[0].cache_read_tokens == 4000
    assert stats["plan_hint"] == "go"


def test_plan_autodetection_for_claude_codex_go_and_honest_free(tmp_path: Path) -> None:
    # 1. Empty home -> MUST honestly report Free / Unverified (never fabricate $100 Max!)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert detect_active_plan(empty, provider="claude").plan.id == "claude_free"
    assert detect_active_plan(empty, provider="codex").plan.id == "codex_free"

    # 2. Claude Max 20x detection
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x",
                }
            }
        ),
        encoding="utf-8",
    )
    det_claude = detect_active_plan(home, provider="claude")
    assert det_claude.plan.id == "max20"
    assert det_claude.source == "claude-credentials"

    # 3. ChatGPT Go detection via JWT claim
    (home / ".codex").mkdir(parents=True)
    jwt_go = _make_fake_jwt({"https://api.openai.com/auth": {"chatgpt_plan_type": "go"}})
    (home / ".codex" / "auth.json").write_text(
        json.dumps({"tokens": {"id_token": jwt_go}}), encoding="utf-8"
    )
    det_go = detect_active_plan(home, provider="codex")
    assert det_go.plan.id == "codex_go"
    assert det_go.source == "codex-auth-claim"

    # 4. Future unknown tier name is passed through dynamically (not discarded!)
    jwt_future = _make_fake_jwt({"https://api.openai.com/auth": {"chatgpt_plan_type": "spark"}})
    (home / ".codex" / "auth.json").write_text(
        json.dumps({"tokens": {"id_token": jwt_future}}), encoding="utf-8"
    )
    det_future = detect_active_plan(home, provider="codex")
    assert "Spark" in det_future.plan.label


def test_modern_2026_models_and_live_pricing_sync(tmp_path: Path, monkeypatch) -> None:
    assert estimate_event_cost_usd("gpt-6-astra", 1_000_000, 0, 0, 0) == 5.0
    assert estimate_event_cost_usd("gpt-6-luna", 1_000_000, 0, 0, 0) == 1.5
    assert estimate_event_cost_usd("gpt-6.2", 1_000_000, 0, 0, 0) == 3.0
    assert estimate_event_cost_usd("gemini-3-pro", 1_000_000, 0, 0, 0) == 2.5

    brand_new = resolve_model_rate("gpt-7-hyper-mini")
    assert brand_new.estimated is True
    assert brand_new.input_per_m == 0.60

    class _FakeResp:
        def read(self) -> bytes:
            return json.dumps(
                {
                    "gpt-7-hyper-mini": {
                        "input_cost_per_token": 0.0000004,
                        "output_cost_per_token": 0.0000016,
                    }
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=15.0: _FakeResp())
    count, path = sync_live_pricing_catalog(tmp_path)
    assert count == 1
    assert path.is_file()

    load_pricing_overrides(tmp_path)
    exact = resolve_model_rate("gpt-7-hyper-mini")
    assert exact.estimated is False
    assert exact.input_per_m == 0.4