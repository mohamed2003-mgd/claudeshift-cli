from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    display_name: str
    targets: tuple[str, ...]
    executables: tuple[str, ...] = ()
    supports_usage_logs: bool = False
    usage_glob: str = ""


@dataclass(frozen=True)
class ProviderStatus:
    spec: ProviderSpec
    detected: bool
    installed: bool
    has_data: bool
    found_paths: tuple[Path, ...]


def _claude_desktop_targets() -> tuple[str, ...]:
    if sys.platform == "win32":
        targets = ["AppData/Roaming/Claude"]
        # Support Windows Microsoft Store (MSIX) sandboxed Claude Desktop path automatically
        pkgs = Path.home() / "AppData" / "Local" / "Packages"
        if pkgs.is_dir():
            for pkg in sorted(pkgs.glob("Claude_*")):
                roaming = pkg / "LocalCache" / "Roaming" / "Claude"
                if roaming.exists():
                    rel = roaming.relative_to(Path.home()).as_posix()
                    targets.append(rel)
                    break
        return tuple(targets)
    if sys.platform == "darwin":
        return ("Library/Application Support/Claude",)
    return (".config/Claude",)


PROVIDERS: dict[str, ProviderSpec] = {
    "claude": ProviderSpec(
        id="claude",
        display_name="Claude Code CLI",
        targets=(".claude", ".claude.json"),
        executables=("claude",),
        supports_usage_logs=True,
        usage_glob=".claude/projects/**/*.jsonl",
    ),
    "claude-desktop": ProviderSpec(
        id="claude-desktop",
        display_name="Claude Desktop App",
        targets=_claude_desktop_targets(),
        executables=("claude-desktop",),
        supports_usage_logs=False,
    ),
    "codex": ProviderSpec(
        id="codex",
        display_name="OpenAI Codex CLI",
        targets=(".codex",),
        executables=("codex",),
        supports_usage_logs=True,
        usage_glob=".codex/sessions/**/*.jsonl",
    ),
    "gemini": ProviderSpec(
        id="gemini",
        display_name="Gemini CLI",
        targets=(".gemini",),
        executables=("gemini",),
        supports_usage_logs=False,
    ),
    "cursor": ProviderSpec(
        id="cursor",
        display_name="Cursor (MCP / CLI Config)",
        targets=(".cursor",),
        executables=("cursor",),
        supports_usage_logs=False,
    ),
}


def _is_desktop_app_installed(provider_id: str) -> bool:
    if provider_id == "claude-desktop":
        if sys.platform == "win32":
            local_app = Path(os.getenv("LOCALAPPDATA", ""))
            if local_app.is_dir():
                if (local_app / "AnthropicClaude" / "claude.exe").is_file():
                    return True
                if (local_app / "Programs" / "claude" / "Claude.exe").is_file():
                    return True
                pkgs = local_app / "Packages"
                if pkgs.is_dir() and any(pkgs.glob("Claude_*")):
                    return True
        elif sys.platform == "darwin":
            return Path("/Applications/Claude.app").exists()
    elif provider_id == "cursor":
        if sys.platform == "win32":
            local_app = Path(os.getenv("LOCALAPPDATA", ""))
            if (local_app / "Programs" / "cursor" / "Cursor.exe").is_file():
                return True
        elif sys.platform == "darwin":
            return Path("/Applications/Cursor.app").exists()
    return False


def _is_provider_installed(spec: ProviderSpec) -> bool:
    if any(shutil.which(exe) is not None for exe in spec.executables):
        return True
    return _is_desktop_app_installed(spec.id)


def inspect_provider(
    spec: ProviderSpec,
    target_home: Path,
    is_sandbox: bool = False,
) -> ProviderStatus:
    found = tuple(target_home / rel for rel in spec.targets if (target_home / rel).exists())
    has_data = len(found) > 0
    installed = False if is_sandbox else _is_provider_installed(spec)
    return ProviderStatus(
        spec=spec,
        detected=has_data or installed,
        installed=installed,
        has_data=has_data,
        found_paths=found,
    )


def inspect_all_providers(target_home: Path, is_sandbox: bool = False) -> list[ProviderStatus]:
    return [inspect_provider(spec, target_home, is_sandbox=is_sandbox) for spec in PROVIDERS.values()]