"""Small Streamlit app that exercises the deliberately faulty sample service."""
from __future__ import annotations

import os
from pathlib import Path
import sys

import streamlit as st

from demo_ui.client import configured_client
from demo_ui.crash_reporter import report_crash


st.set_page_config(page_title="AIOps target app")
st.title("Target app (the app that crashes)")
st.caption("Fake demo data only")

client, key_configured = configured_client()
health = client.health()
st.sidebar.write(
    "API reachable: " + ("yes" if health.ok else health.message)
)
st.sidebar.write(f"key configured: {'yes' if key_configured else 'no'}")

sample_path = Path(__file__).resolve().parents[1] / "mock_services" / "sample_app"
sys.path.insert(0, str(sample_path))


def _report(service_name, operation):
    try:
        return operation()
    except Exception as error:
        result = report_crash(client, service_name, error)
        if result.ok:
            st.error(f"crashed, reported to AIOps (status {result.status_code})")
        else:
            st.error(result.message)
        return None


def _load_missing_user():
    import users

    return users.get_user_email(999)


def _load_payment_config():
    import config

    existed = "STRIPE_API_KEY" in os.environ
    old_value = os.environ.pop("STRIPE_API_KEY", None)
    try:
        return config.load_config()
    finally:
        if existed and old_value is not None:
            os.environ["STRIPE_API_KEY"] = old_value


def _get_db_connection():
    import db

    class TimeoutPool:
        def acquire(self, timeout):
            raise TimeoutError(f"pool timed out after {timeout} seconds")

    return db.OrderRepository(TimeoutPool()).get_connection()


for label, service, operation in (
    ("Load user 999", "user-service", _load_missing_user),
    ("Load payment config", "payment-service", _load_payment_config),
    ("Get DB connection", "order-service", _get_db_connection),
):
    if st.button(label):
        _report(service, operation)

repeat_count = st.slider("Crash x N", min_value=1, max_value=500, value=1)
if st.button("Run crash storm"):
    progress = st.progress(0)
    accepted = 0
    not_accepted = 0
    for index in range(repeat_count):
        try:
            _load_missing_user()
        except Exception as error:
            result = report_crash(client, "user-service", error)
            if result.ok:
                accepted += 1
            else:
                not_accepted += 1
                st.warning(result.message)
                if result.status_code == 429:
                    break
        progress.progress((index + 1) / repeat_count)
    st.write(f"accepted: {accepted}; not accepted: {not_accepted}")
