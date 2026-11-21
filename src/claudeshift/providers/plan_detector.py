from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claudeshift.telemetry.pricing import PLAN_QUOTAS, PlanQuota


@dataclass(frozen=True)
class DetectedPlan:
    plan: PlanQuota
    provider: str
    source: str
    raw_tier: str = ""


def _safe_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _decode_jwt_payload_readonly(jwt_token: str) -> dict[str, Any]:
    """Read-only base64url decode of non-sensitive JWT claims (plan type only). Never stored/logged."""
    parts = jwt_token.split(".")
    if len(parts) < 2:
        return {}
    segment = parts[1]
    padding = "=" * (-len(segment) % 4)
    try:
        raw = base64.urlsafe_b64decode((segment + padding).encode("ascii"))
        data = json.loads(raw.decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _detect_claude_plan(target_home: Path) -> DetectedPlan | None:
    creds = _safe_json(target_home / ".claude" / ".credentials.json")
    oauth = creds.get("claudeAiOauth")
    if isinstance(oauth, dict):
        sub_type = str(oauth.get("subscriptionType") or "").lower()
        tier = str(oauth.get("rateLimitTier") or "").lower()
        combined = f"{sub_type} {tier}".strip()

        if "20x" in combined or "max_20" in combined or "max20" in combined:
            return DetectedPlan(PLAN_QUOTAS["max20"], "claude", "claude-credentials", combined)
        if "5x" in combined or "max_5" in combined or "max5" in combined or "max" in sub_type:
            return DetectedPlan(PLAN_QUOTAS["max5"], "claude", "claude-credentials", combined)
        if "team" in combined:
            return DetectedPlan(PLAN_QUOTAS["claude_team"], "claude", "claude-credentials", combined)
        if "enterprise" in combined:
            return DetectedPlan(PLAN_QUOTAS["claude_enterprise"], "claude", "claude-credentials", combined)
        if "pro" in combined:
            return DetectedPlan(PLAN_QUOTAS["pro"], "claude", "claude-credentials", combined)
        if "free" in combined:
            return DetectedPlan(PLAN_QUOTAS["claude_free"], "claude", "claude-credentials", combined)

    cfg = _safe_json(target_home / ".claude.json")
    acct = cfg.get("oauthAccount")
    if isinstance(acct, dict):
        billing = str(acct.get("billingType") or "").lower()
        org_type = str(acct.get("organizationType") or "").lower()
        combined = f"{billing} {org_type}".strip()
        if "max_20" in combined or "20x" in combined:
            return DetectedPlan(PLAN_QUOTAS["max20"], "claude", "claude-config", combined)
        if "max" in combined:
            return DetectedPlan(PLAN_QUOTAS["max5"], "claude", "claude-config", combined)
        if "pro" in combined:
            return DetectedPlan(PLAN_QUOTAS["pro"], "claude", "claude-config", combined)
        if "team" in combined:
            return DetectedPlan(PLAN_QUOTAS["claude_team"], "claude", "claude-config", combined)
        if "free" in combined:
            return DetectedPlan(PLAN_QUOTAS["claude_free"], "claude", "claude-config", combined)

    if cfg.get("primaryApiKey") or os.getenv("ANTHROPIC_API_KEY"):
        return DetectedPlan(PLAN_QUOTAS["api"], "claude", "api-key", "api")

    return None


def _map_codex_plan_name(raw_plan: str, source: str) -> DetectedPlan | None:
    low = raw_plan.strip().lower()
    if not low:
        return None
    if low == "go" or "chatgpt_go" in low or low.endswith("_go") or low.startswith("go_"):
        return DetectedPlan(PLAN_QUOTAS["codex_go"], "codex", source, low)
    if "pro" in low:
        return DetectedPlan(PLAN_QUOTAS["codex_pro"], "codex", source, low)
    if "plus" in low:
        return DetectedPlan(PLAN_QUOTAS["codex_plus"], "codex", source, low)
    if "team" in low:
        return DetectedPlan(PLAN_QUOTAS["codex_team"], "codex", source, low)
    if "enterprise" in low or "business" in low:
        return DetectedPlan(PLAN_QUOTAS["codex_enterprise"], "codex", source, low)
    if "free" in low:
        return DetectedPlan(PLAN_QUOTAS["codex_free"], "codex", source, low)
    if "api" in low:
        return DetectedPlan(PLAN_QUOTAS["api"], "codex", source, low)

    # Dynamic passthrough: never discard a newly introduced provider tier name!
    clean_title = raw_plan.strip().replace("_", " ").title()
    dynamic_plan = PlanQuota(
        id=f"codex_{low}",
        label=f"ChatGPT {clean_title} / Codex",
        window_hours=5,
        window_cost_budget_usd=15.0,
        window_token_budget=15_000_000,
    )
    return DetectedPlan(dynamic_plan, "codex", source, low)


def _peek_latest_codex_session_plan(target_home: Path) -> str:
    """Quickly inspect the newest Codex JSONL log for rate_limits.plan_type (read-only)."""
    base = target_home / ".codex" / "sessions"
    if not base.is_dir():
        return ""
    try:
        candidates = sorted(
            (p for p in base.rglob("*.jsonl") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return ""

    for fpath in candidates[:3]:
        found = ""
        try:
            with fpath.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "plan_type" not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = obj.get("payload") if isinstance(obj, dict) else None
                    rl = payload.get("rate_limits") if isinstance(payload, dict) else None
                    if isinstance(rl, dict) and isinstance(rl.get("plan_type"), str):
                        val = rl["plan_type"].strip()
                        if val:
                            found = val
        except OSError:
            continue
        if found:
            return found
    return ""


def _detect_codex_plan(target_home: Path, session_hint: str = "") -> DetectedPlan | None:
    # 1. Inspect ~/.codex/auth.json first (direct keys + JWT chatgpt_plan_type claim)
    auth = _safe_json(target_home / ".codex" / "auth.json")
    if auth:
        for key in ("plan", "plan_type", "subscription", "tier", "chatgpt_plan_type"):
            if isinstance(auth.get(key), str):
                mapped = _map_codex_plan_name(auth[key], "codex-auth")
                if mapped:
                    return mapped

        tokens = auth.get("tokens")
        if isinstance(tokens, dict):
            for tok_key in ("id_token", "access_token"):
                tok_val = tokens.get(tok_key)
                if isinstance(tok_val, str) and "." in tok_val:
                    claims = _decode_jwt_payload_readonly(tok_val)
                    auth_ns = claims.get("https://api.openai.com/auth")
                    if isinstance(auth_ns, dict):
                        plan_type = str(auth_ns.get("chatgpt_plan_type") or "")
                        mapped = _map_codex_plan_name(plan_type, "codex-auth-claim")
                        if mapped:
                            return mapped
                    for top_key in ("chatgpt_plan_type", "plan_type"):
                        if isinstance(claims.get(top_key), str):
                            mapped = _map_codex_plan_name(claims[top_key], "codex-auth-claim")
                            if mapped:
                                return mapped

        if auth.get("OPENAI_API_KEY") and not auth.get("tokens"):
            return DetectedPlan(PLAN_QUOTAS["api"], "codex", "codex-api-key", "apikey")

    # 2. Check session log hint (either passed from scanner or peeked for 'doctor')
    hint = session_hint or _peek_latest_codex_session_plan(target_home)
    if hint:
        mapped = _map_codex_plan_name(hint, "codex-session-log")
        if mapped:
            return mapped

    if os.getenv("OPENAI_API_KEY") and not (target_home / ".codex" / "auth.json").is_file():
        return DetectedPlan(PLAN_QUOTAS["api"], "codex", "env-api-key", "apikey")

    return None


def _detect_gemini_plan(target_home: Path) -> DetectedPlan | None:
    settings = _safe_json(target_home / ".gemini" / "settings.json")
    auth_type = str(
        settings.get("selectedAuthType")
        or (settings.get("security") or {}).get("auth", {}).get("selectedType", "")
    ).lower()
    if "api-key" in auth_type or "gemini-api-key" in auth_type:
        return DetectedPlan(PLAN_QUOTAS["api"], "gemini", "gemini-settings", auth_type)
    if "oauth" in auth_type:
        return DetectedPlan(PLAN_QUOTAS["gemini_free"], "gemini", "gemini-settings", auth_type)
    if os.getenv("GEMINI_API_KEY"):
        return DetectedPlan(PLAN_QUOTAS["api"], "gemini", "env-api-key", "api")
    return None


def detect_active_plan(
    target_home: Path,
    provider: str = "all",
    dominant_provider: str | None = None,
    session_plan_hint: str = "",
) -> DetectedPlan:
    """Auto-detect the real subscription tier. Never fabricates a paid plan if unverified."""
    preferred = dominant_provider or (provider if provider != "all" else "claude")

    if preferred == "codex":
        found = _detect_codex_plan(target_home, session_hint=session_plan_hint)
        if found:
            return found
        return DetectedPlan(PLAN_QUOTAS["codex_free"], "codex", "unverified", "free")

    if preferred == "claude":
        found = _detect_claude_plan(target_home)
        if found:
            return found
        if provider == "all":
            found_codex = _detect_codex_plan(target_home, session_hint=session_plan_hint)
            if found_codex:
                return found_codex
        return DetectedPlan(PLAN_QUOTAS["claude_free"], "claude", "unverified", "free")

    if preferred == "gemini":
        found = _detect_gemini_plan(target_home)
        if found:
            return found
        return DetectedPlan(PLAN_QUOTAS["gemini_free"], "gemini", "unverified", "free")

    return DetectedPlan(PLAN_QUOTAS["api"], preferred, "unverified", "api")