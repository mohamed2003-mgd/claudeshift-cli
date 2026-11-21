from __future__ import annotations

import csv
import json
import os
import uuid
from dataclasses import asdict
from pathlib import Path

from claudeshift.telemetry.analytics import UsageReport
from claudeshift.telemetry.parser import UsageEvent

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_csv_cell(value: str) -> str:
    """Neutralize Excel/Sheets CSV formula injection on untrusted text fields."""
    if value and value[0] in _FORMULA_PREFIXES:
        return f"'{value}"
    return value


def export_csv(events: list[UsageEvent], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "timestamp",
        "provider",
        "project",
        "session_id",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_write_tokens",
        "cache_read_tokens",
        "total_tokens",
        "cost_usd",
    ]
    tmp_path = output_path.with_suffix(f".{uuid.uuid4().hex[:6]}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for ev in events:
                writer.writerow(
                    {
                        "timestamp": _sanitize_csv_cell(ev.timestamp),
                        "provider": _sanitize_csv_cell(ev.provider),
                        "project": _sanitize_csv_cell(ev.project),
                        "session_id": _sanitize_csv_cell(ev.session_id),
                        "model": _sanitize_csv_cell(ev.model),
                        "input_tokens": ev.input_tokens,
                        "output_tokens": ev.output_tokens,
                        "cache_write_tokens": ev.cache_write_tokens,
                        "cache_read_tokens": ev.cache_read_tokens,
                        "total_tokens": ev.total_tokens,
                        "cost_usd": f"{ev.cost_usd:.6f}",
                    }
                )
        os.replace(tmp_path, output_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return output_path.resolve()


def export_json(report: UsageReport, events: list[UsageEvent], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "summary": {
            "generated_at": report.generated_at,
            "plan": report.plan.id,
            "plan_source": report.plan_source,
            "budget_usd": report.budget_usd,
            "window_cost_usd": report.window_cost_usd,
            "window_tokens": report.window_tokens,
            "window_pct": report.window_pct,
            "alert_level": report.alert_level,
            "burn_usd_per_hour": report.burn_usd_per_hour,
            "today_cost_usd": report.today_cost_usd,
            "total_cost_usd": report.total_cost_usd,
        },
        "models": [asdict(m) for m in report.models],
        "projects": [asdict(p) for p in report.projects],
        "events": [e.to_dict() for e in events],
    }
    tmp_path = output_path.with_suffix(f".{uuid.uuid4().hex[:6]}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, output_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return output_path.resolve()