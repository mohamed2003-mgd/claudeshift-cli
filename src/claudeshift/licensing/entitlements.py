from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from claudeshift.licensing.gumroad import load_verified_license

FREE_MAX_PROFILES_PER_PROVIDER = 2


class ProFeatureRequiredError(RuntimeError):
    """Raised when a user invokes a Pro-only capability without an active license."""

    def __init__(self, feature: str, detail: str) -> None:
        self.feature = feature
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class Entitlements:
    is_pro: bool
    tier_name: str
    max_profiles_per_provider: int | None
    rolling_backups: bool
    profile_diff: bool
    live_dashboard: bool
    csv_export: bool
    license_source: str
    license_email: str = ""
    grace_until: str = ""


def get_entitlements(config_dir: Path) -> Entitlements:
    """Resolve current Free vs Pro entitlements using HMAC-verified local license."""
    record, reason = load_verified_license(config_dir)
    if record is not None:
        return Entitlements(
            is_pro=True,
            tier_name="PRO",
            max_profiles_per_provider=None,
            rolling_backups=True,
            profile_diff=True,
            live_dashboard=True,
            csv_export=True,
            license_source="gumroad-verified",
            license_email=record.email,
            grace_until=record.grace_until,
        )

    return Entitlements(
        is_pro=False,
        tier_name="FREE",
        max_profiles_per_provider=FREE_MAX_PROFILES_PER_PROVIDER,
        rolling_backups=False,
        profile_diff=False,
        live_dashboard=False,
        csv_export=False,
        license_source=reason,
    )