from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import platform
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

GUMROAD_VERIFY_URL = "https://api.gumroad.com/v2/licenses/verify"
PLACEHOLDER_PRODUCT_ID = "REPLACE_WITH_GUMROAD_PRODUCT_ID"
DEFAULT_GUMROAD_PRODUCT_ID = "xmBRCXkC5mrDkN2y5_EOyw=="
GUMROAD_PRODUCT_URL = "https://mgm15.gumroad.com/l/claudeshift-cli"
OFFLINE_GRACE_DAYS = 14
_SIGNING_SALT = "claudeshift::pro::v1::hmac-guard"


class LicenseError(RuntimeError):
    """Raised when license activation or verification fails."""


@dataclass(frozen=True)
class LicenseRecord:
    status: str  # active | revoked | expired
    key_masked: str
    key_hash: str
    product_id: str
    email: str
    product_name: str
    uses: int
    activated_at: str
    last_verified_at: str
    grace_until: str
    machine_id: str
    signature: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LicenseRecord:
        return cls(
            status=str(d["status"]),
            key_masked=str(d["key_masked"]),
            key_hash=str(d["key_hash"]),
            product_id=str(d["product_id"]),
            email=str(d.get("email", "")),
            product_name=str(d.get("product_name", "claudeshift Pro")),
            uses=int(d.get("uses", 1)),
            activated_at=str(d["activated_at"]),
            last_verified_at=str(d["last_verified_at"]),
            grace_until=str(d["grace_until"]),
            machine_id=str(d["machine_id"]),
            signature=str(d["signature"]),
        )


def is_production_product_pinned() -> bool:
    """True once DEFAULT_GUMROAD_PRODUCT_ID is configured to a real Gumroad product."""
    return bool(DEFAULT_GUMROAD_PRODUCT_ID and DEFAULT_GUMROAD_PRODUCT_ID != PLACEHOLDER_PRODUCT_ID)


def get_gumroad_product_id() -> str:
    """
    Return the authoritative Gumroad Product ID.
    Once pinned for production, environment overrides are strictly ignored to prevent
    attackers from verifying keys against their own $0 Gumroad products.
    """
    if is_production_product_pinned():
        return DEFAULT_GUMROAD_PRODUCT_ID
    return os.getenv("CLAUDESHIFT_GUMROAD_PRODUCT_ID", "").strip() or PLACEHOLDER_PRODUCT_ID


def mask_license_key(key: str) -> str:
    clean = key.strip()
    if len(clean) <= 8:
        return "****"
    return f"{clean[:4]}-****-****-{clean[-4:]}"


def get_machine_fingerprint() -> str:
    raw = f"{platform.system()}|{platform.node()}|{getpass.getuser()}|{platform.machine()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _compute_signature(
    status: str,
    key_hash: str,
    product_id: str,
    email: str,
    grace_until: str,
    machine_id: str,
) -> str:
    payload = f"{status}|{key_hash}|{product_id}|{email}|{grace_until}|{machine_id}"
    secret = f"{_SIGNING_SALT}:{machine_id}".encode()
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def build_signed_record(
    license_key: str,
    product_id: str,
    email: str,
    product_name: str = "claudeshift Pro",
    uses: int = 1,
    now_utc: datetime | None = None,
) -> LicenseRecord:
    now = now_utc or datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    grace_iso = (now + timedelta(days=OFFLINE_GRACE_DAYS)).isoformat(timespec="seconds")
    key_hash = hashlib.sha256(license_key.strip().encode()).hexdigest()
    machine_id = get_machine_fingerprint()
    sig = _compute_signature("active", key_hash, product_id, email, grace_iso, machine_id)

    return LicenseRecord(
        status="active",
        key_masked=mask_license_key(license_key),
        key_hash=key_hash,
        product_id=product_id,
        email=email,
        product_name=product_name,
        uses=uses,
        activated_at=now_iso,
        last_verified_at=now_iso,
        grace_until=grace_iso,
        machine_id=machine_id,
        signature=sig,
    )


def save_license_record(config_dir: Path, record: LicenseRecord) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / "license.json"
    tmp = target.with_suffix(f".{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target


def verify_local_record(record: LicenseRecord, now_utc: datetime | None = None) -> tuple[bool, str]:
    """Validate product ID pinning, HMAC signature, machine binding, status, and grace window."""
    if record.status != "active":
        return False, f"License status is '{record.status}'."

    expected_pid = get_gumroad_product_id()
    if expected_pid != PLACEHOLDER_PRODUCT_ID and record.product_id != expected_pid:
        return False, "License belongs to a different product ID."

    current_machine = get_machine_fingerprint()
    if record.machine_id != current_machine:
        return False, "License cache belongs to a different machine. Re-activate with your Gumroad key."

    expected_sig = _compute_signature(
        record.status,
        record.key_hash,
        record.product_id,
        record.email,
        record.grace_until,
        record.machine_id,
    )
    if not hmac.compare_digest(expected_sig, record.signature):
        return False, "Local license signature is invalid or tampered."

    now = now_utc or datetime.now(timezone.utc)
    try:
        grace_dt = datetime.fromisoformat(record.grace_until.replace("Z", "+00:00"))
        if grace_dt.tzinfo is None:
            grace_dt = grace_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return False, "Invalid license expiration timestamp."

    if now > grace_dt:
        return False, "Offline grace period (14 days) expired. Re-run 'claudeshift license activate <KEY>' online."

    return True, "valid"


def load_verified_license(config_dir: Path) -> tuple[LicenseRecord | None, str]:
    license_file = config_dir / "license.json"
    if not license_file.is_file():
        return None, "not_activated"
    try:
        raw = json.loads(license_file.read_text(encoding="utf-8"))
        record = LicenseRecord.from_dict(raw)
    except Exception:
        return None, "corrupted_license_file"

    ok, reason = verify_local_record(record)
    if not ok:
        return None, reason
    return record, "valid"


def deactivate_license(config_dir: Path) -> bool:
    license_file = config_dir / "license.json"
    if license_file.is_file():
        license_file.unlink(missing_ok=True)
        return True
    return False


def verify_with_gumroad(
    license_key: str,
    product_id: str | None = None,
    increment_uses: bool = True,
    timeout_sec: float = 12.0,
) -> dict[str, Any]:
    """Verify a license key directly against Gumroad Licenses API v2."""
    clean_key = license_key.strip()
    if not clean_key:
        raise LicenseError("License key cannot be empty.")

    pid = DEFAULT_GUMROAD_PRODUCT_ID if is_production_product_pinned() else (product_id or get_gumroad_product_id()).strip()
    if not pid or pid == PLACEHOLDER_PRODUCT_ID:
        raise LicenseError(
            "Gumroad Product ID is not configured yet. "
            "Set CLAUDESHIFT_GUMROAD_PRODUCT_ID or configure DEFAULT_GUMROAD_PRODUCT_ID."
        )

    body = urllib.parse.urlencode(
        {
            "product_id": pid,
            "license_key": clean_key,
            "increment_uses_count": "true" if increment_uses else "false",
        }
    ).encode()

    req = urllib.request.Request(
        GUMROAD_VERIFY_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "claudeshift-cli/0.1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        msg = f"HTTP {err.code}"
        try:
            err_body = json.loads(err.read().decode("utf-8"))
            if isinstance(err_body, dict) and err_body.get("message"):
                msg = str(err_body["message"])
        except Exception:
            pass
        raise LicenseError(f"Gumroad rejected license key: {msg}") from err
    except urllib.error.URLError as err:
        raise LicenseError(f"Network error contacting Gumroad: {err.reason}") from err
    except Exception as err:
        raise LicenseError(f"Unexpected verification error: {err}") from err

    if not data.get("success"):
        raise LicenseError(str(data.get("message") or "Invalid license key."))

    purchase = data.get("purchase") or {}
    if purchase.get("refunded"):
        raise LicenseError("This license has been refunded and is no longer active.")
    if purchase.get("chargebacked") or purchase.get("disputed"):
        raise LicenseError("This license is suspended due to a payment dispute/chargeback.")

    return data


def activate_gumroad_license(
    config_dir: Path,
    license_key: str,
    product_id: str | None = None,
) -> LicenseRecord:
    pid = DEFAULT_GUMROAD_PRODUCT_ID if is_production_product_pinned() else (product_id or get_gumroad_product_id()).strip()
    resp = verify_with_gumroad(license_key=license_key, product_id=pid, increment_uses=True)
    purchase = resp.get("purchase") or {}

    record = build_signed_record(
        license_key=license_key,
        product_id=pid,
        email=str(purchase.get("email") or ""),
        product_name=str(purchase.get("product_name") or "claudeshift Pro"),
        uses=int(resp.get("uses") or 1),
    )
    save_license_record(config_dir, record)
    return record