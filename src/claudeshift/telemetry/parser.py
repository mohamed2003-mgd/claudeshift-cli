from __future__ import annotations

import contextlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claudeshift.telemetry.pricing import estimate_event_cost_usd

CACHE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class UsageEvent:
    provider: str
    timestamp: str  # ISO-8601 UTC
    epoch: float
    session_id: str
    project: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    cost_usd: float

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_write_tokens
            + self.cache_read_tokens
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UsageEvent:
        return cls(
            provider=str(d["provider"]),
            timestamp=str(d["timestamp"]),
            epoch=float(d["epoch"]),
            session_id=str(d["session_id"]),
            project=str(d["project"]),
            model=str(d["model"]),
            input_tokens=int(d["input_tokens"]),
            output_tokens=int(d["output_tokens"]),
            cache_write_tokens=int(d["cache_write_tokens"]),
            cache_read_tokens=int(d["cache_read_tokens"]),
            cost_usd=float(d["cost_usd"]),
        )


def _safe_non_neg_int(val: Any) -> int:
    if isinstance(val, bool) or val is None:
        return 0
    try:
        num = int(val)
    except (TypeError, ValueError):
        return 0
    return max(0, num)


def _parse_iso_timestamp(raw: Any, fallback_epoch: float) -> tuple[str, float]:
    if isinstance(raw, int | float) and not isinstance(raw, bool) and raw > 0:
        with contextlib.suppress(OSError, OverflowError, ValueError):
            dt = datetime.fromtimestamp(float(raw), tz=timezone.utc)
            return dt.isoformat(timespec="seconds"), dt.timestamp()
    if isinstance(raw, str) and raw.strip():
        s = raw.strip().replace("Z", "+00:00")
        with contextlib.suppress(ValueError):
            dt = datetime.fromisoformat(s)
            dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
            return dt.isoformat(timespec="seconds"), dt.timestamp()
    dt = datetime.fromtimestamp(fallback_epoch, tz=timezone.utc)
    return dt.isoformat(timespec="seconds"), dt.timestamp()


def _extract_project_name(file_path: Path, provider: str) -> str:
    parent = file_path.parent.name
    if provider == "claude":
        cleaned = parent.strip("-")
        if "--" in cleaned:
            parts = [p for p in cleaned.split("--") if p]
            return parts[-1] if parts else cleaned
        return cleaned or "default"
    return parent or "default"


def _read_codex_default_model(target_home: Path) -> str:
    """Read configured model from ~/.codex/config.toml as a last-resort session fallback."""
    toml_path = target_home / ".codex" / "config.toml"
    if not toml_path.is_file():
        return ""
    with contextlib.suppress(OSError):
        text = toml_path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r'^\s*model\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        if match:
            return match.group(1).strip()
    return ""


def _extract_model_from_obj(obj: dict[str, Any]) -> str:
    for key in ("model", "model_name", "modelName"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    payload = obj.get("payload")
    if isinstance(payload, dict):
        for key in ("model", "model_name", "modelName"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        meta = payload.get("metadata")
        if isinstance(meta, dict) and isinstance(meta.get("model"), str):
            return meta["model"].strip()
        collab = payload.get("collaboration_mode")
        if isinstance(collab, dict):
            settings = collab.get("settings")
            if isinstance(settings, dict) and isinstance(settings.get("model"), str):
                return settings["model"].strip()
    return ""


def _parse_claude_file(file_path: Path) -> tuple[list[UsageEvent], str]:
    fallback_epoch = file_path.stat().st_mtime
    project = _extract_project_name(file_path, "claude")
    dedup: dict[str, UsageEvent] = {}
    ordered_keys: list[str] = []

    with file_path.open("r", encoding="utf-8-sig", errors="replace") as fh:
        for line_no, raw_line in enumerate(fh, 1):
            line = raw_line.strip()
            if not line or '"usage"' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            msg = obj.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
                usage = msg["usage"]
                model = str(msg.get("model") or obj.get("model") or "unknown")
                msg_id = str(msg.get("id") or obj.get("requestId") or f"line:{line_no}")
            elif isinstance(obj.get("usage"), dict):
                usage = obj["usage"]
                model = str(obj.get("model") or "unknown")
                msg_id = str(obj.get("requestId") or f"line:{line_no}")
            else:
                continue

            in_tok = _safe_non_neg_int(usage.get("input_tokens"))
            out_tok = _safe_non_neg_int(usage.get("output_tokens"))
            cw_tok = _safe_non_neg_int(usage.get("cache_creation_input_tokens"))
            cr_tok = _safe_non_neg_int(usage.get("cache_read_input_tokens"))
            if (in_tok + out_tok + cw_tok + cr_tok) <= 0:
                continue

            ts_iso, epoch = _parse_iso_timestamp(obj.get("timestamp"), fallback_epoch)
            session_id = str(obj.get("sessionId") or file_path.stem)
            cost = estimate_event_cost_usd(model, in_tok, out_tok, cw_tok, cr_tok)

            event = UsageEvent(
                provider="claude",
                timestamp=ts_iso,
                epoch=epoch,
                session_id=session_id,
                project=project,
                model=model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cache_write_tokens=cw_tok,
                cache_read_tokens=cr_tok,
                cost_usd=cost,
            )

            if msg_id not in dedup:
                ordered_keys.append(msg_id)
                dedup[msg_id] = event
            elif event.total_tokens >= dedup[msg_id].total_tokens:
                dedup[msg_id] = event

    return [dedup[k] for k in ordered_keys], ""


def _parse_codex_file(
    file_path: Path,
    configured_model: str = "",
) -> tuple[list[UsageEvent], str]:
    fallback_epoch = file_path.stat().st_mtime
    project = _extract_project_name(file_path, "codex")
    session_id = file_path.stem
    current_model = configured_model or ""
    plan_hint = ""
    last_cumulative_sig: tuple[int, int, int] | None = None
    events: list[UsageEvent] = []

    with file_path.open("r", encoding="utf-8-sig", errors="replace") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj

            found_model = _extract_model_from_obj(obj)
            if found_model:
                current_model = found_model

            cwd = payload.get("cwd") or obj.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                cwd_name = Path(cwd.replace("\\", "/")).name
                if cwd_name:
                    project = cwd_name

            if isinstance(payload.get("id"), str) and obj.get("type") == "session_meta":
                session_id = payload["id"]

            rl = payload.get("rate_limits")
            if isinstance(rl, dict):
                pt = rl.get("plan_type") or rl.get("limit_name")
                if isinstance(pt, str) and pt.strip():
                    plan_hint = pt.strip()

            usage: dict[str, Any] | None = None
            info = payload.get("info")
            if isinstance(info, dict):
                total_u = info.get("total_token_usage")
                last_u = info.get("last_token_usage")
                if isinstance(total_u, dict):
                    sig = (
                        _safe_non_neg_int(total_u.get("input_tokens")),
                        _safe_non_neg_int(total_u.get("output_tokens")),
                        _safe_non_neg_int(total_u.get("cached_input_tokens")),
                    )
                    if sig == last_cumulative_sig:
                        continue
                    last_cumulative_sig = sig
                if isinstance(last_u, dict):
                    usage = last_u
                elif isinstance(total_u, dict):
                    usage = total_u
            elif isinstance(payload.get("usage"), dict):
                usage = payload["usage"]

            if not isinstance(usage, dict):
                continue

            raw_in = _safe_non_neg_int(usage.get("input_tokens") or usage.get("prompt_tokens"))
            out_tok = _safe_non_neg_int(usage.get("output_tokens") or usage.get("completion_tokens"))
            cr_tok = _safe_non_neg_int(usage.get("cached_input_tokens"))
            in_tok = max(0, raw_in - cr_tok) if cr_tok > 0 and raw_in >= cr_tok else raw_in

            if (in_tok + out_tok + cr_tok) <= 0:
                continue

            resolved_model = found_model or current_model or "codex-unknown"
            ts_iso, epoch = _parse_iso_timestamp(obj.get("timestamp"), fallback_epoch)
            cost = estimate_event_cost_usd(resolved_model, in_tok, out_tok, 0, cr_tok)

            events.append(
                UsageEvent(
                    provider="codex",
                    timestamp=ts_iso,
                    epoch=epoch,
                    session_id=str(obj.get("session_id") or session_id),
                    project=project,
                    model=resolved_model,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cache_write_tokens=0,
                    cache_read_tokens=cr_tok,
                    cost_usd=cost,
                )
            )
    return events, plan_hint


def discover_log_files(target_home: Path, provider: str = "claude") -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    providers = ["claude", "codex"] if provider == "all" else [provider]

    for pid in providers:
        if pid == "claude":
            base = target_home / ".claude" / "projects"
            if base.is_dir():
                for p in base.rglob("*.jsonl"):
                    if p.is_file():
                        files.append(("claude", p))
        elif pid == "codex":
            base = target_home / ".codex" / "sessions"
            if base.is_dir():
                for p in base.rglob("*.jsonl"):
                    if p.is_file():
                        files.append(("codex", p))
    return files


class TelemetryScanner:
    """High-speed incremental JSONL scanner backed by mtime+size caching."""

    def __init__(self, target_home: Path, vault_dir: Path) -> None:
        self.target_home = target_home
        self.cache_file = vault_dir / "_telemetry_cache.json"

    def _load_cache(self) -> dict[str, Any]:
        if not self.cache_file.is_file():
            return {"version": CACHE_SCHEMA_VERSION, "files": {}}
        with contextlib.suppress(Exception):
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if data.get("version") == CACHE_SCHEMA_VERSION and isinstance(data.get("files"), dict):
                return data
        return {"version": CACHE_SCHEMA_VERSION, "files": {}}

    def _save_cache(self, cache: dict[str, Any]) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_file.with_suffix(f".{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, self.cache_file)

    def scan(
        self, provider: str = "claude", use_cache: bool = True
    ) -> tuple[list[UsageEvent], dict[str, Any]]:
        cache = self._load_cache() if use_cache else {"version": CACHE_SCHEMA_VERSION, "files": {}}
        cached_files: dict[str, Any] = cache.get("files", {})

        discovered = discover_log_files(self.target_home, provider=provider)
        codex_default_model = _read_codex_default_model(self.target_home)

        all_events: list[UsageEvent] = []
        cache_hits = 0
        parsed_fresh = 0
        dirty = False
        latest_plan_hint = ""
        latest_hint_mtime = -1

        for pid, fpath in discovered:
            try:
                st = fpath.stat()
            except OSError:
                continue

            key = str(fpath.resolve())
            sig = [st.st_mtime_ns, st.st_size]
            entry = cached_files.get(key)

            if use_cache and entry and entry.get("sig") == sig:
                cache_hits += 1
                file_hint = str(entry.get("plan_hint") or "")
                for raw_ev in entry.get("events", []):
                    all_events.append(UsageEvent.from_dict(raw_ev))
            else:
                parsed_fresh += 1
                if pid == "claude":
                    events, file_hint = _parse_claude_file(fpath)
                else:
                    events, file_hint = _parse_codex_file(fpath, configured_model=codex_default_model)
                all_events.extend(events)
                if use_cache:
                    cached_files[key] = {
                        "sig": sig,
                        "plan_hint": file_hint,
                        "events": [e.to_dict() for e in events],
                    }
                    dirty = True

            if file_hint and st.st_mtime_ns >= latest_hint_mtime:
                latest_hint_mtime = st.st_mtime_ns
                latest_plan_hint = file_hint

        if use_cache and dirty:
            cache["files"] = cached_files
            with contextlib.suppress(OSError):
                self._save_cache(cache)

        all_events.sort(key=lambda e: e.epoch)
        stats: dict[str, Any] = {
            "files_scanned": len(discovered),
            "cache_hits": cache_hits,
            "parsed_fresh": parsed_fresh,
            "plan_hint": latest_plan_hint,
        }
        return all_events, stats