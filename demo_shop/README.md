# Corner Shop demo

A tiny server-rendered shop with fake in-memory users and products. It has no
payment integration; the checkout form only demonstrates a fake order flow.

Run it from the repository root with `.\run_shop.ps1`. The seeded usernames are
`alice` (an empty cart) and `ben` (a cart containing a discontinued lamp). Sign
in from the home page to reach a profile and cart.

If both `AIOPS_API_URL` and `AIOPS_API_KEY` are set, unhandled route exceptions
are reported by `aiops_sdk` while the browser receives the shop's friendly 500
page. With neither setting, the shop runs without reporting.

The intentional failure scenarios and tests documenting their desired fixes
are in `demo_shop/tests/`.
