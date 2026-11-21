from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProfileIdentity:
    account: str = ""
    organization: str = ""
    model: str = ""
    mcp_servers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "organization": self.organization,
            "model": self.model,
            "mcp_servers": list(self.mcp_servers),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProfileIdentity:
        return cls(
            account=str(data.get("account", "")),
            organization=str(data.get("organization", "")),
            model=str(data.get("model", "")),
            mcp_servers=tuple(data.get("mcp_servers", [])),
        )


@dataclass
class ProfileManifest:
    schema_version: int
    name: str
    provider: str
    created_at: str
    updated_at: str
    targets: list[str]
    file_count: int
    total_bytes: int
    checksums: dict[str, str]
    identity: ProfileIdentity = field(default_factory=ProfileIdentity)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["identity"] = self.identity.to_dict()
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProfileManifest:
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            name=str(data["name"]),
            provider=str(data["provider"]),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            targets=list(data.get("targets", [])),
            file_count=int(data.get("file_count", 0)),
            total_bytes=int(data.get("total_bytes", 0)),
            checksums=dict(data.get("checksums", {})),
            identity=ProfileIdentity.from_dict(data.get("identity", {})),
            note=str(data.get("note", "")),
        )


@dataclass(frozen=True)
class BackupInfo:
    backup_id: str
    provider: str
    created_at: str
    reason: str
    total_bytes: int
    path: str


@dataclass(frozen=True)
class SwitchResult:
    provider: str
    previous_profile: str | None
    active_profile: str
    synced_previous: bool
    backup_id: str | None
    elapsed_ms: float


@dataclass(frozen=True)
class ProfileDiff:
    provider: str
    profile_a: str
    profile_b: str
    only_in_a_mcp: tuple[str, ...]
    only_in_b_mcp: tuple[str, ...]
    shared_mcp: tuple[str, ...]
    changed_files: tuple[str, ...]
    only_in_a_files: tuple[str, ...]
    only_in_b_files: tuple[str, ...]