from __future__ import annotations

import pytest

from claudeshift.licensing import gumroad as gumroad_mod


@pytest.fixture(autouse=True)
def _unpin_product_id_for_unit_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep production builds pinned while letting isolated unit tests use mock product IDs."""
    monkeypatch.setattr(
        gumroad_mod,
        "DEFAULT_GUMROAD_PRODUCT_ID",
        gumroad_mod.PLACEHOLDER_PRODUCT_ID,
    )