from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from claudeshift.config.paths import RuntimePaths
from claudeshift.core.inspector import inspect_identity
from claudeshift.core.models import (
    BackupInfo,
    ProfileDiff,
    ProfileManifest,
    SwitchResult,
)
from claudeshift.licensing.entitlements import (
    ProFeatureRequiredError,
    get_entitlements,
)
from claudeshift.providers.base import PROVIDERS, ProviderSpec

MANIFEST_SCHEMA_VERSION = 1
MAX_ROLLING_BACKUPS = 10
PROFILE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,47}$")
BACKUP_ID_RE = re.compile(r"^\d{8}_\d{6}_[a-f0-9]{6}$")

# Exclude heavy regenerable caches, lock files, and marketplace bundles from profile snapshots
IGNORED_PATTERNS = (
    "*.lock",
    "*.tmp",
    "LOCK",
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
    "shell-snapshots",
    "__pycache__",
    "cache",
    "Cache",
    "Cache_Data",
    "Code Cache",
    "GPUCache",
    "DawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "GrShaderCache",
    "ShaderCache",
    "Service Worker",
    "blob_storage",
    "Crashpad",
    "marketplaces",
    "node_modules",
    "logs",
    "Logs",
    "tmp",
    "temp",
)

WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class VaultError(RuntimeError):
    """Base error for vault operations."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _win_long_path(path: Path) -> str:
    """Bypass Windows 260-char MAX_PATH limit using extended-length path prefix."""
    s = str(path.resolve())
    if sys.platform == "win32" and not s.startswith("\\\\?\\"):
        if s.startswith("\\\\"):
            return "\\\\?\\UNC\\" + s[2:]
        return "\\\\?\\" + s
    return s


def _validate_profile_name(name: str) -> str:
    cleaned = name.strip()
    if not PROFILE_NAME_RE.match(cleaned):
        raise VaultError(
            f"Invalid profile name '{name}'. Use 1-48 chars: letters, numbers, '.', '_', '-'."
        )
    base_name = cleaned.split(".")[0].lower()
    if (
        cleaned.lower() in {"default", "all", "none", "backup", "backups"}
        or base_name in WINDOWS_RESERVED_NAMES
    ):
        raise VaultError(
            f"Profile name '{cleaned}' is reserved. Choose a descriptive name like 'work' or 'personal'."
        )
    return cleaned


def _validate_backup_id(backup_id: str) -> str:
    cleaned = backup_id.strip()
    if not BACKUP_ID_RE.match(cleaned):
        raise VaultError(f"Invalid backup ID '{backup_id}'.")
    return cleaned


def _assert_within_root(root: Path, candidate: Path, label: str) -> Path:
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    if not resolved_candidate.is_relative_to(resolved_root):
        raise VaultError(f"Security violation: {label} escapes allowed directory.")
    return resolved_candidate


def _validate_no_escaping_symlinks(src: Path, allowed_root: Path) -> None:
    """Reject any symlink inside src whose resolved target escapes allowed_root."""
    resolved_root = allowed_root.resolve()

    def _check(p: Path) -> None:
        if p.is_symlink():
            target = p.resolve()
            if not target.is_relative_to(resolved_root):
                raise VaultError(
                    f"Security violation: symlink '{p.name}' escapes target home directory."
                )

    _check(src)
    if src.is_dir() and not src.is_symlink():
        for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
            # Skip ignored cache directories during symlink traversal as well
            dirnames[:] = [d for d in dirnames if d not in IGNORED_PATTERNS]
            base = Path(dirpath)
            for name in dirnames:
                _check(base / name)
            for name in filenames:
                _check(base / name)


def _get_provider(provider_id: str) -> ProviderSpec:
    pid = provider_id.strip().lower()
    if pid not in PROVIDERS:
        valid = ", ".join(PROVIDERS.keys())
        raise VaultError(f"Unknown provider '{provider_id}'. Supported providers: {valid}")
    return PROVIDERS[pid]


def _handle_remove_readonly(func, path, exc_info) -> None:
    with contextlib.suppress(Exception):
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)


def _force_rmtree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_file() or path.is_symlink():
        with contextlib.suppress(Exception):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        path.unlink(missing_ok=True)
        return
    shutil.rmtree(_win_long_path(path), onerror=_handle_remove_readonly)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, path)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(_win_long_path(path), "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_entry(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(
            _win_long_path(src),
            _win_long_path(dst),
            symlinks=True,
            ignore=shutil.ignore_patterns(*IGNORED_PATTERNS),
            dirs_exist_ok=False,
        )
    else:
        shutil.copy2(_win_long_path(src), _win_long_path(dst))


def _scan_payload(payload_dir: Path) -> tuple[int, int, dict[str, str]]:
    """Return (file_count, total_bytes, checksums_map) for all regular files under payload_dir."""
    file_count = 0
    total_bytes = 0
    checksums: dict[str, str] = {}
    if not payload_dir.exists():
        return 0, 0, {}

    for p in sorted(payload_dir.rglob("*")):
        if p.is_file() and not p.is_symlink():
            rel = p.relative_to(payload_dir).as_posix()
            size = p.stat().st_size
            file_count += 1
            total_bytes += size
            checksums[rel] = _sha256_file(p) if size <= 5 * 1024 * 1024 else f"size:{size}"
    return file_count, total_bytes, checksums


def _verify_checksums(payload_dir: Path, checksums: dict[str, str], label: str) -> None:
    for rel, expected in checksums.items():
        if not isinstance(rel, str) or not rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise VaultError(f"Security violation: illegal path '{rel}' inside {label} manifest.")
        fpath = _assert_within_root(payload_dir, payload_dir / rel, f"{label} checksum path '{rel}'")
        if not fpath.is_file():
            raise VaultError(f"Integrity check failed: missing file '{rel}' in {label}.")
        if expected.startswith("size:"):
            if f"size:{fpath.stat().st_size}" != expected:
                raise VaultError(f"Integrity check failed: size mismatch for '{rel}' in {label}.")
        else:
            actual = _sha256_file(fpath)
            if actual != expected:
                raise VaultError(f"Integrity check failed: SHA-256 mismatch for '{rel}' in {label}.")


class VaultEngine:
    """Atomic, crash-safe, multi-provider profile vault."""

    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths
        self.vault_root = paths.vault_dir
        self.profiles_root = self.vault_root / "profiles"
        self.backups_root = self.vault_root / "backups"
        self.state_file = self.vault_root / "state.json"
        self.lock_file = self.vault_root / ".vault.lock"

    @contextmanager
    def _locked(self, timeout_sec: float = 5.0) -> Iterator[None]:
        self.vault_root.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout_sec
        while True:
            try:
                fd = os.open(str(self.lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"pid={os.getpid()} ts={_utc_now()}\n".encode())
                os.close(fd)
                break
            except FileExistsError:
                try:
                    age = time.time() - self.lock_file.stat().st_mtime
                    if age > 60:
                        self.lock_file.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise VaultError(
                        "Vault is currently locked by another claudeshift process. Try again in a moment."
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            self.lock_file.unlink(missing_ok=True)

    def _read_state(self) -> dict:
        if not self.state_file.is_file():
            return {"active": {}}
        with contextlib.suppress(Exception):
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("active"), dict):
                return data
        return {"active": {}}

    def _write_state(self, state: dict) -> None:
        _atomic_write_json(self.state_file, state)

    def get_active_profile(self, provider: str = "claude") -> str | None:
        spec = _get_provider(provider)
        state = self._read_state()
        return state.get("active", {}).get(spec.id)

    def _profile_dir(self, provider_id: str, name: str) -> Path:
        return self.profiles_root / provider_id / name

    def list_profiles(self, provider: str = "claude") -> list[ProfileManifest]:
        spec = _get_provider(provider)
        pdir = self.profiles_root / spec.id
        if not pdir.is_dir():
            return []
        manifests: list[ProfileManifest] = []
        for child in sorted(pdir.iterdir()):
            mfile = child / "manifest.json"
            if child.is_dir() and mfile.is_file():
                with contextlib.suppress(Exception):
                    data = json.loads(mfile.read_text(encoding="utf-8"))
                    manifests.append(ProfileManifest.from_dict(data))
        return manifests

    def _verify_profile_integrity(self, profile_dir: Path) -> ProfileManifest:
        mfile = profile_dir / "manifest.json"
        payload_dir = profile_dir / "payload"
        if not mfile.is_file() or not payload_dir.is_dir():
            raise VaultError(f"Profile storage at '{profile_dir.name}' is corrupted or incomplete.")

        manifest = ProfileManifest.from_dict(json.loads(mfile.read_text(encoding="utf-8")))
        _verify_checksums(payload_dir, manifest.checksums, f"profile '{manifest.name}'")
        return manifest

    def _capture_live_to_dir(
        self,
        spec: ProviderSpec,
        dest_payload: Path,
        name: str,
        note: str = "",
        existing_created_at: str | None = None,
    ) -> ProfileManifest:
        found_targets: list[str] = []
        dest_payload.mkdir(parents=True, exist_ok=True)

        for rel in spec.targets:
            src = self.paths.target_home / rel
            if src.exists() or src.is_symlink():
                _validate_no_escaping_symlinks(src, self.paths.target_home)
                _copy_entry(src, dest_payload / rel)
                found_targets.append(rel)

        if not found_targets:
            raise VaultError(
                f"No {spec.display_name} files found in '{self.paths.target_home}'. "
                f"Expected one of: {', '.join(spec.targets)}"
            )

        file_count, total_bytes, checksums = _scan_payload(dest_payload)
        identity = inspect_identity(spec.id, dest_payload)
        now = _utc_now()
        return ProfileManifest(
            schema_version=MANIFEST_SCHEMA_VERSION,
            name=name,
            provider=spec.id,
            created_at=existing_created_at or now,
            updated_at=now,
            targets=found_targets,
            file_count=file_count,
            total_bytes=total_bytes,
            checksums=checksums,
            identity=identity,
            note=note,
        )

    def save_profile(
        self,
        name: str,
        provider: str = "claude",
        overwrite: bool = False,
        note: str = "",
        set_active: bool = True,
    ) -> ProfileManifest:
        spec = _get_provider(provider)
        clean_name = _validate_profile_name(name)
        entitlements = get_entitlements(self.paths.config_dir)

        with self._locked():
            existing_profiles = self.list_profiles(spec.id)
            target_dir = self._profile_dir(spec.id, clean_name)
            already_exists = target_dir.exists()

            if already_exists and not overwrite:
                raise VaultError(
                    f"Profile '{clean_name}' already exists for {spec.id}. Use --overwrite to update it."
                )

            if (
                not already_exists
                and entitlements.max_profiles_per_provider is not None
                and len(existing_profiles) >= entitlements.max_profiles_per_provider
            ):
                raise ProFeatureRequiredError(
                    feature="unlimited_profiles",
                    detail=(
                        f"Free tier supports up to {entitlements.max_profiles_per_provider} profiles "
                        f"per provider. Upgrade to claudeshift Pro for unlimited profiles, rolling backups, "
                        f"and live quota alerts."
                    ),
                )

            created_at: str | None = None
            if already_exists and (target_dir / "manifest.json").is_file():
                with contextlib.suppress(Exception):
                    prev = json.loads((target_dir / "manifest.json").read_text(encoding="utf-8"))
                    created_at = prev.get("created_at")

            tmp_profile_dir = self.profiles_root / spec.id / f".tmp_{clean_name}_{uuid.uuid4().hex[:8]}"
            try:
                manifest = self._capture_live_to_dir(
                    spec=spec,
                    dest_payload=tmp_profile_dir / "payload",
                    name=clean_name,
                    note=note,
                    existing_created_at=created_at,
                )
                _atomic_write_json(tmp_profile_dir / "manifest.json", manifest.to_dict())

                if target_dir.exists():
                    _force_rmtree(target_dir)
                target_dir.parent.mkdir(parents=True, exist_ok=True)
                tmp_profile_dir.rename(target_dir)
            except Exception:
                _force_rmtree(tmp_profile_dir)
                raise

            if set_active:
                state = self._read_state()
                state.setdefault("active", {})[spec.id] = clean_name
                self._write_state(state)

            return manifest

    def _create_backup_snapshot(self, spec: ProviderSpec, reason: str) -> BackupInfo | None:
        has_live = any((self.paths.target_home / rel).exists() for rel in spec.targets)
        if not has_live:
            return None

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_id = f"{stamp}_{uuid.uuid4().hex[:6]}"
        bdir = self.backups_root / spec.id / backup_id
        payload_dir = bdir / "payload"
        payload_dir.mkdir(parents=True, exist_ok=True)

        targets_copied: list[str] = []
        for rel in spec.targets:
            src = self.paths.target_home / rel
            if src.exists() or src.is_symlink():
                _validate_no_escaping_symlinks(src, self.paths.target_home)
                _copy_entry(src, payload_dir / rel)
                targets_copied.append(rel)

        _, total_bytes, checksums = _scan_payload(payload_dir)
        meta = {
            "backup_id": backup_id,
            "provider": spec.id,
            "created_at": _utc_now(),
            "reason": reason,
            "targets": targets_copied,
            "total_bytes": total_bytes,
            "checksums": checksums,
        }
        _atomic_write_json(bdir / "backup.json", meta)

        provider_backups = sorted(
            [p for p in (self.backups_root / spec.id).iterdir() if p.is_dir()],
            key=lambda p: p.name,
            reverse=True,
        )
        for stale in provider_backups[MAX_ROLLING_BACKUPS:]:
            _force_rmtree(stale)

        return BackupInfo(
            backup_id=backup_id,
            provider=spec.id,
            created_at=meta["created_at"],
            reason=reason,
            total_bytes=total_bytes,
            path=str(bdir),
        )

    def _atomic_apply_payload(self, spec: ProviderSpec, payload_dir: Path) -> None:
        """
        Transactional swap on target_home (supports both top-level and nested targets like AppData/Roaming/Claude):
        1. Copy payload to staging folder on target_home (same filesystem).
        2. Move current live targets into rollback folder.
        3. Move staged targets into live positions.
        4. On ANY error (e.g., locked file on Windows), restore rollback automatically.
        """
        token = uuid.uuid4().hex[:8]
        stage_dir = self.paths.target_home / f".claudeshift_stage_{token}"
        rollback_dir = self.paths.target_home / f".claudeshift_rollback_{token}"

        moved_to_rollback: list[str] = []
        applied_live: list[str] = []

        try:
            stage_dir.mkdir(parents=True, exist_ok=True)
            rollback_dir.mkdir(parents=True, exist_ok=True)

            staged_targets: list[str] = []
            for rel in spec.targets:
                src = payload_dir / rel
                if src.exists():
                    _validate_no_escaping_symlinks(src, payload_dir)
                    _copy_entry(src, stage_dir / rel)
                    staged_targets.append(rel)

            if not staged_targets:
                raise VaultError("Target profile payload contains no files to restore.")

            for rel in spec.targets:
                live = self.paths.target_home / rel
                if live.exists():
                    rb_dest = rollback_dir / rel
                    rb_dest.parent.mkdir(parents=True, exist_ok=True)
                    live.rename(rb_dest)
                    moved_to_rollback.append(rel)

            for rel in staged_targets:
                live_dest = self.paths.target_home / rel
                live_dest.parent.mkdir(parents=True, exist_ok=True)
                (stage_dir / rel).rename(live_dest)
                applied_live.append(rel)

        except Exception as exc:
            for rel in applied_live:
                live = self.paths.target_home / rel
                if live.exists():
                    _force_rmtree(live)
            for rel in moved_to_rollback:
                rb = rollback_dir / rel
                if rb.exists():
                    with contextlib.suppress(Exception):
                        live_dest = self.paths.target_home / rel
                        live_dest.parent.mkdir(parents=True, exist_ok=True)
                        rb.rename(live_dest)
            raise VaultError(
                f"Atomic swap aborted and safely rolled back (your files are untouched). "
                f"Close {spec.display_name} if it is running and retry. Details: {exc}"
            ) from exc
        finally:
            _force_rmtree(stage_dir)
            _force_rmtree(rollback_dir)

    def use_profile(
        self,
        name: str,
        provider: str = "claude",
        sync_current: bool = True,
    ) -> SwitchResult:
        t0 = time.perf_counter()
        spec = _get_provider(provider)
        clean_name = _validate_profile_name(name)
        entitlements = get_entitlements(self.paths.config_dir)

        with self._locked():
            target_dir = self._profile_dir(spec.id, clean_name)
            if not target_dir.is_dir():
                raise VaultError(f"Profile '{clean_name}' not found for {spec.id}.")

            self._verify_profile_integrity(target_dir)

            state = self._read_state()
            prev_active = state.get("active", {}).get(spec.id)
            synced_previous = False

            if sync_current and prev_active and prev_active != clean_name:
                prev_dir = self._profile_dir(spec.id, prev_active)
                if prev_dir.is_dir() and any((self.paths.target_home / r).exists() for r in spec.targets):
                    tmp_sync = self.profiles_root / spec.id / f".tmp_sync_{prev_active}_{uuid.uuid4().hex[:6]}"
                    try:
                        prev_meta = json.loads((prev_dir / "manifest.json").read_text(encoding="utf-8"))
                        updated_manifest = self._capture_live_to_dir(
                            spec=spec,
                            dest_payload=tmp_sync / "payload",
                            name=prev_active,
                            note=prev_meta.get("note", ""),
                            existing_created_at=prev_meta.get("created_at"),
                        )
                        _atomic_write_json(tmp_sync / "manifest.json", updated_manifest.to_dict())
                        _force_rmtree(prev_dir)
                        tmp_sync.rename(prev_dir)
                        synced_previous = True
                    except Exception:
                        _force_rmtree(tmp_sync)
                        raise

            backup_id: str | None = None
            if entitlements.rolling_backups:
                binfo = self._create_backup_snapshot(spec, reason=f"pre-switch -> {clean_name}")
                if binfo:
                    backup_id = binfo.backup_id

            self._atomic_apply_payload(spec, target_dir / "payload")

            state.setdefault("active", {})[spec.id] = clean_name
            self._write_state(state)

            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return SwitchResult(
                provider=spec.id,
                previous_profile=prev_active,
                active_profile=clean_name,
                synced_previous=synced_previous,
                backup_id=backup_id,
                elapsed_ms=round(elapsed_ms, 2),
            )

    def remove_profile(self, name: str, provider: str = "claude") -> None:
        spec = _get_provider(provider)
        clean_name = _validate_profile_name(name)
        with self._locked():
            target_dir = self._profile_dir(spec.id, clean_name)
            if not target_dir.is_dir():
                raise VaultError(f"Profile '{clean_name}' not found for {spec.id}.")
            _force_rmtree(target_dir)

            state = self._read_state()
            if state.get("active", {}).get(spec.id) == clean_name:
                state["active"].pop(spec.id, None)
                self._write_state(state)

    def list_backups(self, provider: str = "claude") -> list[BackupInfo]:
        spec = _get_provider(provider)
        entitlements = get_entitlements(self.paths.config_dir)
        if not entitlements.rolling_backups:
            raise ProFeatureRequiredError(
                feature="rolling_backups",
                detail="Rolling safety backups & time-machine restore require claudeshift Pro.",
            )

        broot = self.backups_root / spec.id
        if not broot.is_dir():
            return []

        items: list[BackupInfo] = []
        for child in sorted(broot.iterdir(), key=lambda p: p.name, reverse=True):
            meta_file = child / "backup.json"
            if child.is_dir() and meta_file.is_file():
                with contextlib.suppress(Exception):
                    m = json.loads(meta_file.read_text(encoding="utf-8"))
                    items.append(
                        BackupInfo(
                            backup_id=m["backup_id"],
                            provider=m["provider"],
                            created_at=m["created_at"],
                            reason=m.get("reason", ""),
                            total_bytes=int(m.get("total_bytes", 0)),
                            path=str(child),
                        )
                    )
        return items

    def restore_backup(self, backup_id: str, provider: str = "claude") -> BackupInfo:
        spec = _get_provider(provider)
        clean_bid = _validate_backup_id(backup_id)
        entitlements = get_entitlements(self.paths.config_dir)
        if not entitlements.rolling_backups:
            raise ProFeatureRequiredError(
                feature="rolling_backups",
                detail="Backup restore requires claudeshift Pro.",
            )

        with self._locked():
            broot = self.backups_root / spec.id
            bdir = _assert_within_root(broot, broot / clean_bid, "backup_id")
            meta_file = bdir / "backup.json"
            payload_dir = bdir / "payload"
            if not meta_file.is_file() or not payload_dir.is_dir():
                raise VaultError(f"Backup '{clean_bid}' not found for {spec.id}.")

            m = json.loads(meta_file.read_text(encoding="utf-8"))
            checksums = m.get("checksums")
            if isinstance(checksums, dict):
                _verify_checksums(payload_dir, checksums, f"backup '{clean_bid}'")

            self._atomic_apply_payload(spec, payload_dir)
            return BackupInfo(
                backup_id=m["backup_id"],
                provider=m["provider"],
                created_at=m["created_at"],
                reason=m.get("reason", ""),
                total_bytes=int(m.get("total_bytes", 0)),
                path=str(bdir),
            )

    def diff_profiles(self, profile_a: str, profile_b: str, provider: str = "claude") -> ProfileDiff:
        spec = _get_provider(provider)
        entitlements = get_entitlements(self.paths.config_dir)
        if not entitlements.profile_diff:
            raise ProFeatureRequiredError(
                feature="profile_diff",
                detail="Profile & MCP diffing requires claudeshift Pro.",
            )

        a_dir = self._profile_dir(spec.id, _validate_profile_name(profile_a))
        b_dir = self._profile_dir(spec.id, _validate_profile_name(profile_b))
        if not a_dir.is_dir():
            raise VaultError(f"Profile '{profile_a}' not found.")
        if not b_dir.is_dir():
            raise VaultError(f"Profile '{profile_b}' not found.")

        ma = self._verify_profile_integrity(a_dir)
        mb = self._verify_profile_integrity(b_dir)

        mcp_a = set(ma.identity.mcp_servers)
        mcp_b = set(mb.identity.mcp_servers)

        files_a = set(ma.checksums.keys())
        files_b = set(mb.checksums.keys())
        shared_files = files_a & files_b
        changed = sorted(f for f in shared_files if ma.checksums[f] != mb.checksums[f])

        return ProfileDiff(
            provider=spec.id,
            profile_a=ma.name,
            profile_b=mb.name,
            only_in_a_mcp=tuple(sorted(mcp_a - mcp_b)),
            only_in_b_mcp=tuple(sorted(mcp_b - mcp_a)),
            shared_mcp=tuple(sorted(mcp_a & mcp_b)),
            changed_files=tuple(changed),
            only_in_a_files=tuple(sorted(files_a - files_b)),
            only_in_b_files=tuple(sorted(files_b - files_a)),
        )