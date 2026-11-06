"""Small server-rendered HTML pages for the demo shop."""
from __future__ import annotations

from collections.abc import Iterable
from html import escape

from demo_shop.store import Product, User

_STYLE = """
<style>
:root { color-scheme: light; font-family: system-ui, sans-serif; color: #233044;
  background: #f5f7fb; }
* { box-sizing: border-box; }
body { margin: 0; }
header { background: #183b56; color: white; padding: 1.2rem max(1rem, calc((100% - 900px) / 2)); }
header a { color: white; text-decoration: none; font-weight: 700; font-size: 1.3rem; }
main { width: min(900px, calc(100% - 2rem)); margin: 2rem auto; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 1rem; }
.card, .panel { background: white; border: 1px solid #dfe5ec; border-radius: 12px;
  padding: 1.2rem; box-shadow: 0 3px 12px #2030400a; }
h1 { margin-top: 0; } h2 { margin-top: .2rem; }
p { line-height: 1.55; }
a { color: #176b87; }
button { border: 0; border-radius: 7px; background: #176b87; color: white;
  padding: .7rem 1rem; font: inherit; cursor: pointer; }
button:hover { background: #10536b; }
input { display: block; width: 100%; max-width: 24rem; margin: .5rem 0 1rem;
  padding: .7rem; border: 1px solid #aeb9c5; border-radius: 6px; font: inherit; }
.muted { color: #627184; }
.actions { display: flex; flex-wrap: wrap; gap: .8rem; align-items: center; margin-top: 1rem; }
@media (max-width: 520px) { main { margin: 1rem auto; } header { padding: 1rem; } }
</style>
"""


def _layout(title: str, content: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{escape(title)} | Corner Shop</title>{_STYLE}</head>"
        f"<body><header><a href=\"/\">Corner Shop</a></header>"
        f"<main>{content}</main></body></html>"
    )


def home_page(products: Iterable[Product]) -> str:
    cards = []
    for product in products:
        if product.price is None:
            price = "No longer available"
            cart_action = '<p class="muted">Discontinued</p>'
        else:
            price = f"${product.price / 100:.2f}"
            cart_action = (
                '<form method="post" action="/cart/add">'
                f'<input type="hidden" name="product_id" value="{product.product_id}">'
                '<input type="hidden" name="user_id" value="1">'
                '<button type="submit">Add to cart</button></form>'
            )
        cards.append(
            '<article class="card">'
            f"<h2>{escape(product.name)}</h2>"
            f'<p class="muted">{escape(product.description)}</p><p><strong>{price}</strong></p>'
            f"{cart_action}</article>"
        )
    content = (
        "<h1>Thoughtful things for everyday life</h1>"
        '<p class="muted">A small collection, shipped with care.</p>'
        f'<section class="grid">{"".join(cards)}</section>'
        '<section class="panel" style="margin-top:1rem">'
        "<h2>Your account</h2><form method=\"post\" action=\"/login\">"
        '<label for="username">Username</label><input id="username" name="username" '
        'autocomplete="username" required><button type="submit">Sign in</button></form>'
        '<p><a href="/cart?user_id=1">View your cart</a></p></section>'
    )
    return _layout("Home", content)


def login_page(message: str = "Sign in with a demo username.") -> str:
    return _layout(
        "Sign in",
        f'<section class="panel"><h1>Welcome back</h1><p>{escape(message)}</p>'
        '<form method="post" action="/login"><label for="username">Username</label>'
        '<input id="username" name="username" autocomplete="username" required>'
        '<button type="submit">Sign in</button></form></section>',
    )


def profile_page(user: User, email: str) -> str:
    return _layout(
        "Your profile",
        f'<section class="panel"><h1>Hello, {escape(user.username)}</h1>'
        f"<p>Email: {escape(email)}</p><div class=\"actions\">"
        f'<a href="/cart?user_id={user.user_id}">View your cart</a>'
        '<form method="post" action="/account/delete">'
        f'<input type="hidden" name="user_id" value="{user.user_id}">'
        '<button type="submit">Delete my account</button></form></div></section>',
    )


def cart_page(products: Iterable[Product], total: int, user_id: int) -> str:
    rows = "".join(
        f'<li>{escape(product.name)} — '
        + ("Unavailable" if product.price is None else f"${product.price / 100:.2f}")
        + "</li>"
        for product in products
    )
    if not rows:
        rows = "<li>Your cart is empty.</li>"
    return _layout(
        "Your cart",
        f'<section class="panel"><h1>Your cart</h1><ul>{rows}</ul>'
        f'<p><strong>Total: ${total / 100:.2f}</strong></p>'
        f'<a href="/checkout?user_id={user_id}">Continue to checkout</a></section>',
    )


def checkout_page(user_id: int) -> str:
    return _layout(
        "Checkout",
        f'<section class="panel"><h1>Checkout</h1>'
        '<p>This demo does not process payments.</p>'
        '<form method="post" action="/checkout">'
        f'<input type="hidden" name="user_id" value="{user_id}">'
        '<button type="submit">Place demo order</button></form></section>',
    )


def error_page() -> str:
    return _layout(
        "Something went wrong",
        '<section class="panel"><h1>We hit a snag</h1>'
        "<p>We couldn't complete that request. Please try again later.</p>"
        '<a href="/">Return to the shop</a></section>',
    )


def confirmation_page(user_id: int) -> str:
    return _layout(
        "Account deleted",
        '<section class="panel"><h1>Account deleted</h1>'
        '<p>Your demo account has been removed.</p>'
        f'<a href="/profile/{user_id}">View your profile</a></section>',
    )


def message_page(title: str, message: str) -> str:
    return _layout(
        title,
        f'<section class="panel"><h1>{escape(title)}</h1>'
        f"<p>{escape(message)}</p><a href=\"/\">Return to the shop</a></section>",
    )
