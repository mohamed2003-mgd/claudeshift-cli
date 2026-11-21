from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from claudeshift.telemetry.parser import UsageEvent
from claudeshift.telemetry.pricing import PLAN_QUOTAS, PlanQuota, resolve_model_rate


@dataclass(frozen=True)
class ModelBreakdown:
    model: str
    requests: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_tokens: int
    cost_usd: float
    rate_estimated: bool = False


@dataclass(frozen=True)
class ProjectBreakdown:
    project: str
    sessions: int
    requests: int
    total_tokens: int
    cost_usd: float


@dataclass(frozen=True)
class UsageReport:
    generated_at: str
    plan: PlanQuota
    plan_source: str
    budget_usd: float
    # Active 5-hour rolling window metrics
    window_requests: int
    window_tokens: int
    window_cost_usd: float
    window_pct: float
    alert_level: str  # OK | WARNING | CRITICAL
    # Burn-rate & ETA (based on last 60 minutes)
    burn_tokens_per_min: float
    burn_usd_per_hour: float
    eta_minutes: float | None
    window_reset_in_minutes: float | None
    # Today (UTC day) & All-time totals
    today_requests: int
    today_tokens: int
    today_cost_usd: float
    total_requests: int
    total_tokens: int
    total_cost_usd: float
    # Breakdowns
    models: tuple[ModelBreakdown, ...]
    projects: tuple[ProjectBreakdown, ...]
    # Scanner perf stats
    files_scanned: int
    cache_hits: int
    parsed_fresh: int


def compute_usage_report(
    events: list[UsageEvent],
    plan_id: str = "max5",
    custom_budget_usd: float | None = None,
    now_utc: datetime | None = None,
    scan_stats: dict[str, Any] | None = None,
    resolved_plan: PlanQuota | None = None,
    plan_source: str = "manual",
) -> UsageReport:
    now = now_utc or datetime.now(timezone.utc)
    now_epoch = now.timestamp()
    plan = resolved_plan or PLAN_QUOTAS.get(plan_id.lower(), PLAN_QUOTAS["max5"])
    budget_usd = (
        custom_budget_usd
        if (custom_budget_usd and custom_budget_usd > 0)
        else plan.window_cost_budget_usd
    )

    window_cutoff = (now - timedelta(hours=plan.window_hours)).timestamp()
    burn_cutoff = (now - timedelta(minutes=60)).timestamp()
    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp()

    window_events = [e for e in events if e.epoch >= window_cutoff]
    burn_events = [e for e in events if e.epoch >= burn_cutoff]
    today_events = [e for e in events if e.epoch >= today_start]

    window_tokens = sum(e.total_tokens for e in window_events)
    window_cost = round(sum(e.cost_usd for e in window_events), 4)
    window_pct = min(999.9, round((window_cost / budget_usd) * 100.0, 1)) if budget_usd > 0 else 0.0

    if window_pct >= 90.0:
        alert_level = "CRITICAL"
    elif window_pct >= 75.0:
        alert_level = "WARNING"
    else:
        alert_level = "OK"

    if burn_events:
        span_min = max(5.0, (now_epoch - burn_events[0].epoch) / 60.0)
        span_min = min(60.0, span_min)
        burn_tok = sum(e.total_tokens for e in burn_events)
        burn_usd = sum(e.cost_usd for e in burn_events)
        burn_tokens_per_min = round(burn_tok / span_min, 1)
        burn_usd_per_hour = round((burn_usd / span_min) * 60.0, 2)
    else:
        burn_tokens_per_min = 0.0
        burn_usd_per_hour = 0.0

    remaining_usd = max(0.0, budget_usd - window_cost)
    if burn_usd_per_hour > 0.01 and remaining_usd > 0:
        eta_minutes: float | None = round((remaining_usd / burn_usd_per_hour) * 60.0, 1)
    elif remaining_usd <= 0 and window_events:
        eta_minutes = 0.0
    else:
        eta_minutes = None

    if window_events:
        oldest_in_window = window_events[0].epoch
        reset_epoch = oldest_in_window + (plan.window_hours * 3600)
        window_reset_in_minutes: float | None = max(0.0, round((reset_epoch - now_epoch) / 60.0, 1))
    else:
        window_reset_in_minutes = None

    by_model: dict[str, list[UsageEvent]] = defaultdict(list)
    by_project: dict[str, list[UsageEvent]] = defaultdict(list)
    for e in events:
        by_model[e.model].append(e)
        by_project[e.project].append(e)

    models = tuple(
        sorted(
            (
                ModelBreakdown(
                    model=m,
                    requests=len(evs),
                    input_tokens=sum(x.input_tokens for x in evs),
                    output_tokens=sum(x.output_tokens for x in evs),
                    cache_read_tokens=sum(x.cache_read_tokens for x in evs),
                    cache_write_tokens=sum(x.cache_write_tokens for x in evs),
                    total_tokens=sum(x.total_tokens for x in evs),
                    cost_usd=round(sum(x.cost_usd for x in evs), 4),
                    rate_estimated=resolve_model_rate(m).estimated,
                )
                for m, evs in by_model.items()
            ),
            key=lambda x: x.cost_usd,
            reverse=True,
        )
    )

    projects = tuple(
        sorted(
            (
                ProjectBreakdown(
                    project=p,
                    sessions=len({x.session_id for x in evs}),
                    requests=len(evs),
                    total_tokens=sum(x.total_tokens for x in evs),
                    cost_usd=round(sum(x.cost_usd for x in evs), 4),
                )
                for p, evs in by_project.items()
            ),
            key=lambda x: x.cost_usd,
            reverse=True,
        )
    )

    stats = scan_stats or {}
    return UsageReport(
        generated_at=now.isoformat(timespec="seconds"),
        plan=plan,
        plan_source=plan_source,
        budget_usd=budget_usd,
        window_requests=len(window_events),
        window_tokens=window_tokens,
        window_cost_usd=window_cost,
        window_pct=window_pct,
        alert_level=alert_level,
        burn_tokens_per_min=burn_tokens_per_min,
        burn_usd_per_hour=burn_usd_per_hour,
        eta_minutes=eta_minutes,
        window_reset_in_minutes=window_reset_in_minutes,
        today_requests=len(today_events),
        today_tokens=sum(e.total_tokens for e in today_events),
        today_cost_usd=round(sum(e.cost_usd for e in today_events), 4),
        total_requests=len(events),
        total_tokens=sum(e.total_tokens for e in events),
        total_cost_usd=round(sum(e.cost_usd for e in events), 4),
        models=models,
        projects=projects,
        files_scanned=int(stats.get("files_scanned", 0)),
        cache_hits=int(stats.get("cache_hits", 0)),
        parsed_fresh=int(stats.get("parsed_fresh", 0)),
    )