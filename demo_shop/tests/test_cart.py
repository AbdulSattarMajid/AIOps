from __future__ import annotations

from demo_shop.store import ShopStore


def test_discontinued_product_is_skipped_in_cart_total():
    shop = ShopStore()

    assert shop.total(2) == 0
