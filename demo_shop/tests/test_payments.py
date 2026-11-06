from __future__ import annotations

import pytest

from demo_shop.payments import load_payment_config


def test_missing_payment_key_has_a_clear_error(monkeypatch):
    monkeypatch.delenv("SHOP_PAYMENT_KEY", raising=False)

    with pytest.raises(RuntimeError, match="payment"):
        load_payment_config()
