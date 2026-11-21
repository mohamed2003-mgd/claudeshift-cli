from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from claudeshift.core.models import ProfileIdentity


def _safe_load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_email_from_jwt(jwt_token: str) -> str:
    """Read-only extraction of email claim from JWT id_token. Never prints or stores tokens."""
    parts = jwt_token.split(".")
    if len(parts) < 2:
        return ""
    segment = parts[1]
    padding = "=" * (-len(segment) % 4)
    try:
        raw = base64.urlsafe_b64decode((segment + padding).encode("ascii"))
        claims = json.loads(raw.decode("utf-8", errors="replace"))
        if isinstance(claims, dict):
            return str(claims.get("email") or "")
    except Exception:
        pass
    return ""


def inspect_identity(provider: str, root_dir: Path) -> ProfileIdentity:
    """Extract non-sensitive metadata (account email, org, model, MCP server names)."""
    if provider == "claude":
        cfg = _safe_load_json(root_dir / ".claude.json")
        settings = _safe_load_json(root_dir / ".claude" / "settings.json")

        oauth = cfg.get("oauthAccount") or {}
        if not isinstance(oauth, dict):
            oauth = {}

        account = str(oauth.get("emailAddress") or oauth.get("accountUuid") or "")
        org = str(oauth.get("organizationName") or "")
        model = str(settings.get("model") or "")

        mcp_names: set[str] = set()
        for source in (cfg.get("mcpServers"), settings.get("mcpServers")):
            if isinstance(source, dict):
                mcp_names.update(str(k) for k in source)

        return ProfileIdentity(
            account=account,
            organization=org,
            model=model,
            mcp_servers=tuple(sorted(mcp_names)),
        )

    if provider == "claude-desktop":
        candidates = (
            root_dir / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json",
            root_dir / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
            root_dir / ".config" / "Claude" / "claude_desktop_config.json",
        )
        cfg = {}
        for c in candidates:
            if c.is_file():
                cfg = _safe_load_json(c)
                break
        mcp = cfg.get("mcpServers")
        mcp_names = tuple(sorted(str(k) for k in mcp)) if isinstance(mcp, dict) else ()
        return ProfileIdentity(account="Claude Desktop Profile", mcp_servers=mcp_names)

    if provider == "codex":
        auth = _safe_load_json(root_dir / ".codex" / "auth.json")
        account = str(auth.get("account") or auth.get("email") or "")
        if not account and isinstance(auth.get("tokens"), dict):
            id_tok = auth["tokens"].get("id_token")
            if isinstance(id_tok, str) and "." in id_tok:
                account = _extract_email_from_jwt(id_tok)

        model = ""
        toml_path = root_dir / ".codex" / "config.toml"
        if toml_path.is_file():
            text = toml_path.read_text(encoding="utf-8", errors="replace")
            match = re.search(r'^\s*model\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
            if match:
                model = match.group(1)
        return ProfileIdentity(account=account, model=model)

    if provider == "gemini":
        settings = _safe_load_json(root_dir / ".gemini" / "settings.json")
        account = str(
            settings.get("selectedAuthType")
            or (settings.get("security") or {}).get("auth", {}).get("selectedType", "")
        )
        mcp = settings.get("mcpServers")
        mcp_names = tuple(sorted(str(k) for k in mcp)) if isinstance(mcp, dict) else ()
        return ProfileIdentity(account=account, mcp_servers=mcp_names)

    if provider == "cursor":
        mcp_cfg = _safe_load_json(root_dir / ".cursor" / "mcp.json")
        mcp = mcp_cfg.get("mcpServers")
        mcp_names = tuple(sorted(str(k) for k in mcp)) if isinstance(mcp, dict) else ()
        return ProfileIdentity(mcp_servers=mcp_names)

    return ProfileIdentity()