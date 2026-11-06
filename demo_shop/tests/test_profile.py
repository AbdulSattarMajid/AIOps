from __future__ import annotations

import pytest

from demo_shop.store import ShopStore


def test_deleted_user_profile_has_a_clear_error():
    shop = ShopStore()
    shop.delete_user(1)

    with pytest.raises(LookupError, match="user"):
        shop.profile_email(1)
