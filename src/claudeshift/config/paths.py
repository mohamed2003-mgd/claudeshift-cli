from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    target_home: Path
    is_sandbox: bool
    vault_dir: Path
    config_dir: Path


def get_target_home() -> tuple[Path, bool]:
    """Return (target_home, is_sandbox). Never touches real home if sandbox env is set."""
    override = os.getenv("CLAUDESHIFT_TARGET_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve(), True
    return Path.home().resolve(), False


def get_vault_dir() -> Path:
    """Resolve XDG-compliant / Windows-native vault storage directory."""
    override = os.getenv("CLAUDESHIFT_VAULT_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    xdg_data = os.getenv("XDG_DATA_HOME", "").strip()
    if xdg_data:
        return Path(xdg_data).expanduser().resolve() / "claudeshift"

    if sys.platform == "win32":
        local_appdata = os.getenv("LOCALAPPDATA", "").strip()
        if local_appdata:
            return Path(local_appdata).resolve() / "claudeshift"

    return Path.home().resolve() / ".local" / "share" / "claudeshift"


def get_config_dir(is_sandbox: bool = False, vault_dir: Path | None = None) -> Path:
    """Resolve XDG-compliant / Windows-native config directory (isolated in sandbox)."""
    override = os.getenv("CLAUDESHIFT_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    if is_sandbox and vault_dir is not None:
        return vault_dir / "_config"

    xdg_config = os.getenv("XDG_CONFIG_HOME", "").strip()
    if xdg_config:
        return Path(xdg_config).expanduser().resolve() / "claudeshift"

    if sys.platform == "win32":
        appdata = os.getenv("APPDATA", "").strip()
        if appdata:
            return Path(appdata).resolve() / "claudeshift"

    return Path.home().resolve() / ".config" / "claudeshift"


def resolve_runtime_paths() -> RuntimePaths:
    target_home, is_sandbox = get_target_home()
    vault_dir = get_vault_dir()
    config_dir = get_config_dir(is_sandbox=is_sandbox, vault_dir=vault_dir)
    return RuntimePaths(
        target_home=target_home,
        is_sandbox=is_sandbox,
        vault_dir=vault_dir,
        config_dir=config_dir,
    )