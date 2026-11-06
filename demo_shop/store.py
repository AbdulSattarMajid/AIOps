"""In-memory fake shop data and business logic."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class User:
    user_id: int
    username: str
    email: str


@dataclass
class Product:
    product_id: int
    name: str
    price: int | None
    description: str


def get_profile_email(user: User) -> str:
    return user.email


def cart_total(
    product_ids: list[int], products: dict[int, Product]
) -> int:
    return sum(products[product_id].price for product_id in product_ids)


class ShopStore:
    def __init__(self) -> None:
        self.users = {
            1: User(1, "alice", "alice@example.test"),
            2: User(2, "ben", "ben@example.test"),
        }
        self.products = {
            1: Product(1, "Canvas Tote", 1800, "A sturdy everyday bag."),
            2: Product(2, "Pocket Notebook", 700, "A small ruled notebook."),
            3: Product(
                3,
                "Classic Desk Lamp",
                None,
                "A customer favorite, now discontinued.",
            ),
        }
        self.carts = {1: [], 2: [3]}

    def find_user(self, username: str) -> User | None:
        normalized = username.strip().casefold()
        return next(
            (user for user in self.users.values() if user.username == normalized),
            None,
        )

    def get_user(self, user_id: int) -> User | None:
        return self.users.get(user_id)

    def profile_email(self, user_id: int) -> str:
        user = self.get_user(user_id)
        return get_profile_email(user)

    def delete_user(self, user_id: int) -> None:
        self.users.pop(user_id, None)
        self.carts.pop(user_id, None)

    def add_to_cart(self, user_id: int, product_id: int) -> None:
        if product_id not in self.products:
            raise LookupError("That product is no longer available.")
        self.carts.setdefault(user_id, []).append(product_id)

    def cart_products(self, user_id: int) -> list[Product]:
        return [self.products[item] for item in self.carts.get(user_id, [])]

    def total(self, user_id: int) -> int:
        return cart_total(self.carts.get(user_id, []), self.products)
