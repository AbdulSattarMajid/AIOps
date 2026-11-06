"""FastAPI application for the in-memory Corner Shop demo."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from aiops_sdk import install
from demo_shop import pages
from demo_shop.payments import load_payment_config
from demo_shop.store import ShopStore


def create_app() -> FastAPI:
    shop = ShopStore()
    app = FastAPI(title="Corner Shop Demo")

    @app.exception_handler(Exception)
    async def friendly_server_error(_request: Request, _exc: Exception) -> HTMLResponse:
        return HTMLResponse(pages.error_page(), status_code=500)

    @app.get("/", response_class=HTMLResponse)
    async def home() -> HTMLResponse:
        return HTMLResponse(pages.home_page(shop.products.values()))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/login")
    async def login(request: Request) -> Response:
        form = await _form_values(request)
        user = shop.find_user(form.get("username", ""))
        if user is None:
            return HTMLResponse(
                pages.login_page("Please use one of the demo usernames listed in the shop guide."),
                status_code=400,
            )
        return RedirectResponse(f"/profile/{user.user_id}", status_code=303)

    @app.get("/profile/{user_id}", response_class=HTMLResponse)
    async def profile(user_id: int) -> HTMLResponse:
        email = shop.profile_email(user_id)
        user = shop.get_user(user_id)
        if user is None:
            return HTMLResponse(pages.error_page(), status_code=500)
        return HTMLResponse(pages.profile_page(user, email))

    @app.get("/cart", response_class=HTMLResponse)
    async def cart(user_id: int = 1) -> HTMLResponse:
        return HTMLResponse(
            pages.cart_page(shop.cart_products(user_id), shop.total(user_id), user_id)
        )

    @app.post("/cart/add")
    async def add_to_cart(request: Request) -> Response:
        form = await _form_values(request)
        try:
            product_id = int(form.get("product_id", ""))
            user_id = int(form.get("user_id", "1"))
            shop.add_to_cart(user_id, product_id)
        except (ValueError, LookupError):
            return HTMLResponse(
                pages.message_page("Unavailable", "That item could not be added."),
                status_code=400,
            )
        return RedirectResponse(f"/cart?user_id={user_id}", status_code=303)

    @app.get("/checkout", response_class=HTMLResponse)
    async def checkout(user_id: int = 1) -> HTMLResponse:
        return HTMLResponse(pages.checkout_page(user_id))

    @app.post("/checkout")
    async def place_order(request: Request) -> HTMLResponse:
        form = await _form_values(request)
        int(form.get("user_id", "1"))
        load_payment_config()
        return HTMLResponse(
            pages.message_page("Order placed", "This demo did not process a payment.")
        )

    @app.post("/account/delete", response_class=HTMLResponse)
    async def delete_account(request: Request) -> HTMLResponse:
        form = await _form_values(request)
        try:
            user_id = int(form.get("user_id", ""))
        except ValueError:
            return HTMLResponse(
                pages.message_page("Account not found", "Please sign in again."),
                status_code=400,
            )
        shop.delete_user(user_id)
        return HTMLResponse(pages.confirmation_page(user_id))

    api_url = os.getenv("AIOPS_API_URL")
    api_key = os.getenv("AIOPS_API_KEY")
    if api_url and api_key:
        install(
            app,
            api_url=api_url,
            api_key=api_key,
            service_name="shop-service",
            app_root=Path(__file__).resolve().parent.parent,
        )
    return app


async def _form_values(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    return {key: values[-1] for key, values in parse_qs(body).items() if values}


app = create_app()
