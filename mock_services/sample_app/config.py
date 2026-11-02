"""Sample payment-service config loader with a deliberate bug."""
import os


def load_config():
    key = os.environ["STRIPE_API_KEY"]
    return {"stripe_key": key}
