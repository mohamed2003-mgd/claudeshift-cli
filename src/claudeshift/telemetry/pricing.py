from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)


@dataclass(frozen=True)
class ModelRate:
    """USD per 1,000,000 tokens."""

    input_per_m: float
    output_per_m: float
    cache_write_per_m: float
    cache_read_per_m: float
    estimated: bool = False


@dataclass(frozen=True)
class PlanQuota:
    id: str
    label: str
    window_hours: int
    window_cost_budget_usd: float
    window_token_budget: int


# Ordered version-agnostic family + tier rules (most specific first).
# Handles minor/point releases automatically (e.g. gemini-3.1-pro, gemini-3.8-flash, claude-opus-5.5, gpt-6.2-mini).
MODEL_PATTERNS: tuple[tuple[re.Pattern[str], ModelRate], ...] = (
    # --- Google Gemini 3.x family (3.0, 3.1, 3.5, 3.8, previews, etc.) ---
    (re.compile(r"gemini-3(?:\.\d+)?(?:[-_.].*)?flash[-_.]?lite"), ModelRate(0.10, 0.40, 0.10, 0.025)),
    (re.compile(r"gemini-3(?:\.\d+)?(?:[-_.].*)?flash"), ModelRate(0.30, 1.20, 0.30, 0.075)),
    (re.compile(r"gemini-3(?:\.\d+)?(?:[-_.].*)?ultra"), ModelRate(5.00, 25.00, 5.00, 1.25)),
    (re.compile(r"gemini-3(?:\.\d+)?(?:[-_.].*)?pro"), ModelRate(2.50, 15.00, 2.50, 0.625)),
    (re.compile(r"gemini-3(?:\.\d+)?\b"), ModelRate(1.50, 10.00, 1.50, 0.375)),
    # --- Google Gemini 2.5 family ---
    (re.compile(r"gemini-2\.5(?:[-_.].*)?flash[-_.]?lite"), ModelRate(0.075, 0.30, 0.075, 0.02)),
    (re.compile(r"gemini-2\.5(?:[-_.].*)?flash"), ModelRate(0.15, 0.60, 0.15, 0.0375)),
    (re.compile(r"gemini-2\.5(?:[-_.].*)?pro"), ModelRate(1.25, 10.00, 1.25, 0.31)),
    # --- Anthropic Claude (5.x, 4.x, 3.x & Fable) ---
    (re.compile(r"claude.*fable|\bfable(?:[-_.]\d+(?:\.\d+)?)?"), ModelRate(5.00, 25.00, 6.25, 0.50)),
    (re.compile(r"claude.*opus"), ModelRate(15.00, 75.00, 18.75, 1.50)),
    (re.compile(r"claude.*sonnet"), ModelRate(3.00, 15.00, 3.75, 0.30)),
    (re.compile(r"claude.*haiku"), ModelRate(0.80, 4.00, 1.00, 0.08)),
    # --- OpenAI GPT-6.x / Astra / Luna ---
    (re.compile(r"gpt-6(?:\.\d+)?.*astra|\bastra\b"), ModelRate(5.00, 20.00, 5.00, 1.25)),
    (re.compile(r"gpt-6(?:\.\d+)?.*luna|\bluna\b"), ModelRate(1.50, 6.00, 1.50, 0.375)),
    (re.compile(r"gpt-6(?:\.\d+)?.*nano"), ModelRate(0.15, 0.60, 0.15, 0.0375)),
    (re.compile(r"gpt-6(?:\.\d+)?.*mini"), ModelRate(0.60, 2.40, 0.60, 0.15)),
    (re.compile(r"gpt-6(?:\.\d+)?.*pro"), ModelRate(10.00, 40.00, 10.00, 2.50)),
    (re.compile(r"gpt-6(?:\.\d+)?"), ModelRate(3.00, 12.00, 3.00, 0.75)),
    # --- OpenAI GPT-5.x / Codex / o-series / GPT-4.1 ---
    (re.compile(r"gpt-5(?:\.\d+)?.*nano"), ModelRate(0.05, 0.40, 0.05, 0.005)),
    (re.compile(r"gpt-5(?:\.\d+)?.*mini"), ModelRate(0.25, 2.00, 0.25, 0.025)),
    (re.compile(r"gpt-5(?:\.\d+)?.*codex"), ModelRate(1.50, 10.00, 1.50, 0.15)),
    (re.compile(r"gpt-5\.4"), ModelRate(2.50, 10.00, 2.50, 0.25)),
    (re.compile(r"gpt-5\.3"), ModelRate(2.00, 10.00, 2.00, 0.20)),
    (re.compile(r"gpt-5\.2"), ModelRate(1.75, 10.00, 1.75, 0.175)),
    (re.compile(r"gpt-5\.1"), ModelRate(1.50, 10.00, 1.50, 0.15)),
    (re.compile(r"gpt-5\b"), ModelRate(1.25, 10.00, 1.25, 0.125)),
    (re.compile(r"codex-mini"), ModelRate(1.50, 6.00, 1.50, 0.375)),
    (re.compile(r"\bo4-mini\b"), ModelRate(1.10, 4.40, 1.10, 0.275)),
    (re.compile(r"\bo3-pro\b"), ModelRate(20.00, 80.00, 20.00, 5.00)),
    (re.compile(r"\bo3-mini\b"), ModelRate(1.10, 4.40, 1.10, 0.55)),
    (re.compile(r"\bo3\b"), ModelRate(2.00, 8.00, 2.00, 0.50)),
    (re.compile(r"gpt-4\.1.*mini"), ModelRate(0.40, 1.60, 0.40, 0.10)),
    (re.compile(r"gpt-4\.1"), ModelRate(2.00, 8.00, 2.00, 0.50)),
)

# Plan quotas across Claude Code, OpenAI Codex, and Gemini CLI
PLAN_QUOTAS: dict[str, PlanQuota] = {
    "claude_free": PlanQuota(
        id="claude_free",
        label="Claude Free / Unverified",
        window_hours=5,
        window_cost_budget_usd=5.0,
        window_token_budget=5_000_000,
    ),
    "pro": PlanQuota(
        id="pro",
        label="Claude Pro ($20/mo)",
        window_hours=5,
        window_cost_budget_usd=18.0,
        window_token_budget=19_000_000,
    ),
    "max5": PlanQuota(
        id="max5",
        label="Claude Max 5x ($100/mo)",
        window_hours=5,
        window_cost_budget_usd=35.0,
        window_token_budget=88_000_000,
    ),
    "max20": PlanQuota(
        id="max20",
        label="Claude Max 20x ($200/mo)",
        window_hours=5,
        window_cost_budget_usd=140.0,
        window_token_budget=220_000_000,
    ),
    "claude_team": PlanQuota(
        id="claude_team",
        label="Claude Team",
        window_hours=5,
        window_cost_budget_usd=40.0,
        window_token_budget=95_000_000,
    ),
    "claude_enterprise": PlanQuota(
        id="claude_enterprise",
        label="Claude Enterprise",
        window_hours=5,
        window_cost_budget_usd=100.0,
        window_token_budget=180_000_000,
    ),
    "codex_free": PlanQuota(
        id="codex_free",
        label="ChatGPT Free / Unverified",
        window_hours=5,
        window_cost_budget_usd=5.0,
        window_token_budget=5_000_000,
    ),
    "codex_go": PlanQuota(
        id="codex_go",
        label="ChatGPT Go / Codex",
        window_hours=5,
        window_cost_budget_usd=10.0,
        window_token_budget=12_000_000,
    ),
    "codex_plus": PlanQuota(
        id="codex_plus",
        label="ChatGPT Plus / Codex ($20/mo)",
        window_hours=5,
        window_cost_budget_usd=20.0,
        window_token_budget=25_000_000,
    ),
    "codex_pro": PlanQuota(
        id="codex_pro",
        label="ChatGPT Pro / Codex ($200/mo)",
        window_hours=5,
        window_cost_budget_usd=140.0,
        window_token_budget=220_000_000,
    ),
    "codex_team": PlanQuota(
        id="codex_team",
        label="ChatGPT Team / Codex",
        window_hours=5,
        window_cost_budget_usd=50.0,
        window_token_budget=90_000_000,
    ),
    "codex_enterprise": PlanQuota(
        id="codex_enterprise",
        label="ChatGPT Enterprise / Codex",
        window_hours=5,
        window_cost_budget_usd=120.0,
        window_token_budget=200_000_000,
    ),
    "gemini_free": PlanQuota(
        id="gemini_free",
        label="Gemini Personal (Free OAuth)",
        window_hours=5,
        window_cost_budget_usd=5.0,
        window_token_budget=10_000_000,
    ),
    "api": PlanQuota(
        id="api",
        label="API Key (Pay-as-you-go)",
        window_hours=5,
        window_cost_budget_usd=50.0,
        window_token_budget=100_000_000,
    ),
}

_CUSTOM_RATES: dict[str, ModelRate] = {}
_SYNCED_RATES: dict[str, ModelRate] = {}


def _safe_non_negative_rate(val: object, default: float) -> float | None:
    try:
        num = float(val if val is not None else default)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num) or num < 0.0 or num > 10_000.0:
        return None
    return num


def _load_rate_map_file(path: Path, target_dict: dict[str, ModelRate]) -> None:
    if not path.is_file():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = raw.get("models", raw) if isinstance(raw, dict) else {}
        if isinstance(entries, dict):
            for prefix, vals in entries.items():
                if not isinstance(prefix, str) or not prefix.strip() or not isinstance(vals, dict):
                    continue
                in_m = _safe_non_negative_rate(vals.get("input_per_m"), 3.0)
                out_m = _safe_non_negative_rate(vals.get("output_per_m"), 15.0)
                cw_m = _safe_non_negative_rate(vals.get("cache_write_per_m"), 3.75)
                cr_m = _safe_non_negative_rate(vals.get("cache_read_per_m"), 0.30)
                if None in (in_m, out_m, cw_m, cr_m):
                    continue
                target_dict[prefix.strip().lower()] = ModelRate(
                    input_per_m=in_m,  # type: ignore[arg-type]
                    output_per_m=out_m,  # type: ignore[arg-type]
                    cache_write_per_m=cw_m,  # type: ignore[arg-type]
                    cache_read_per_m=cr_m,  # type: ignore[arg-type]
                    estimated=False,
                )
    except Exception:
        pass


def load_pricing_overrides(config_dir: Path) -> None:
    """Load synced pricing cache + user pricing_overrides.json (overrides win)."""
    _SYNCED_RATES.clear()
    _CUSTOM_RATES.clear()
    _load_rate_map_file(config_dir / "pricing_cache.json", _SYNCED_RATES)
    _load_rate_map_file(config_dir / "pricing_overrides.json", _CUSTOM_RATES)


def sync_live_pricing_catalog(
    config_dir: Path,
    source_url: str = LITELLM_PRICING_URL,
    timeout_sec: float = 15.0,
) -> tuple[int, Path]:
    """Fetch latest community model pricing catalog so brand-new models get exact prices."""
    req = urllib.request.Request(source_url, headers={"User-Agent": "claudeshift-cli/0.1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Failed to sync live pricing catalog: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuntimeError("Unexpected pricing catalog format.")

    extracted: dict[str, dict[str, float]] = {}
    relevant_prefixes = ("claude", "gpt-", "o1", "o3", "o4", "codex", "gemini", "fable", "astra", "luna")

    for raw_name, meta in raw.items():
        if not isinstance(meta, dict):
            continue
        clean_name = raw_name.split("/")[-1].lower().strip()
        if not clean_name.startswith(relevant_prefixes):
            continue
        in_tok = meta.get("input_cost_per_token")
        out_tok = meta.get("output_cost_per_token")
        if not isinstance(in_tok, int | float) or not isinstance(out_tok, int | float):
            continue
        if not (math.isfinite(in_tok) and math.isfinite(out_tok)) or in_tok < 0 or out_tok < 0:
            continue
        if in_tok == 0 and out_tok == 0:
            continue

        cw_tok = meta.get("cache_creation_input_token_cost")
        cr_tok = meta.get("cache_read_input_token_cost")

        in_m = round(float(in_tok) * 1_000_000.0, 4)
        out_m = round(float(out_tok) * 1_000_000.0, 4)
        cw_m = (
            round(float(cw_tok) * 1_000_000.0, 4)
            if isinstance(cw_tok, int | float) and math.isfinite(cw_tok) and cw_tok >= 0
            else in_m
        )
        cr_m = (
            round(float(cr_tok) * 1_000_000.0, 4)
            if isinstance(cr_tok, int | float) and math.isfinite(cr_tok) and cr_tok >= 0
            else round(in_m * 0.25, 4)
        )
        extracted[clean_name] = {
            "input_per_m": in_m,
            "output_per_m": out_m,
            "cache_write_per_m": cw_m,
            "cache_read_per_m": cr_m,
        }

    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / "pricing_cache.json"
    tmp = target.with_suffix(f".{uuid.uuid4().hex[:6]}.tmp")
    payload = {
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source_url,
        "count": len(extracted),
        "models": extracted,
    }
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return len(extracted), target


def _infer_heuristic_rate(low_model: str) -> ModelRate:
    """Smart tier fallback when encountering a brand-new unseen model."""
    if any(k in low_model for k in ("nano", "lite")):
        return ModelRate(0.10, 0.40, 0.10, 0.025, estimated=True)
    if any(k in low_model for k in ("mini", "flash", "haiku", "fast")):
        return ModelRate(0.60, 2.40, 0.60, 0.15, estimated=True)
    if any(k in low_model for k in ("opus", "ultra", "pro-max")):
        return ModelRate(15.0, 75.0, 18.75, 1.50, estimated=True)
    if any(k in low_model for k in ("astra", "fable", "max")):
        return ModelRate(5.0, 20.0, 5.0, 1.00, estimated=True)
    return ModelRate(3.0, 12.0, 3.0, 0.50, estimated=True)


def resolve_model_rate(model_name: str) -> ModelRate:
    low = (model_name or "").strip().lower()
    if not low:
        return _infer_heuristic_rate("")

    # 1. User custom overrides (exact or longest substring)
    if low in _CUSTOM_RATES:
        return _CUSTOM_RATES[low]
    for prefix in sorted(_CUSTOM_RATES, key=len, reverse=True):
        if prefix in low:
            return _CUSTOM_RATES[prefix]

    # 2. Live synced catalog (exact match first, then longest prefix)
    if low in _SYNCED_RATES:
        return _SYNCED_RATES[low]
    for prefix in sorted(_SYNCED_RATES, key=len, reverse=True):
        if low.startswith(prefix):
            return _SYNCED_RATES[prefix]

    # 3. Version-agnostic 2026+ regex table
    for pattern, rate in MODEL_PATTERNS:
        if pattern.search(low):
            return rate

    # 4. Heuristic tier fallback for brand-new unseen models
    return _infer_heuristic_rate(low)


def estimate_event_cost_usd(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int,
    cache_read_tokens: int,
) -> float:
    rate = resolve_model_rate(model_name)
    in_t = max(0, int(input_tokens))
    out_t = max(0, int(output_tokens))
    cw_t = max(0, int(cache_write_tokens))
    cr_t = max(0, int(cache_read_tokens))
    cost = (
        (in_t / 1_000_000.0) * rate.input_per_m
        + (out_t / 1_000_000.0) * rate.output_per_m
        + (cw_t / 1_000_000.0) * rate.cache_write_per_m
        + (cr_t / 1_000_000.0) * rate.cache_read_per_m
    )
    return round(cost, 6)