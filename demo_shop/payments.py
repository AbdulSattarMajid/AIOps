"""Fake checkout configuration for the demo shop."""
from __future__ import annotations

import os


def load_payment_config() -> dict[str, str]:
    return {"payment_key": os.environ["SHOP_PAYMENT_KEY"]}
