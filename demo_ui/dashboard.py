"""Read-only incident dashboard for the local AIOps API."""
from __future__ import annotations

from datetime import datetime, timezone
import os
import time

import streamlit as st

from demo_ui.client import configured_client
from demo_ui.views import (
    delivery_button_enabled,
    format_analysis,
)
from demo_ui.views import (
    api_error_message,
    empty_state,
    incident_detail,
    queue_status,
    shape_incidents,
    summarize_redaction,
)


st.set_page_config(page_title="AIOps dashboard", layout="wide")
st.title("AIOps incident dashboard")
client, key_configured = configured_client()
st.sidebar.write(f"key configured: {'yes' if key_configured else 'no'}")
auto_refresh = st.sidebar.checkbox("Auto-refresh every 5 s", value=False)
refresh = st.button("Refresh")

if not key_configured:
    st.error("key not configured")
    st.stop()

health = client.health()
stats_result = client.queue_stats()
stats = queue_status(stats_result)
columns = st.columns(4)
columns[0].metric("API reachable", "yes" if health.ok else "no")
columns[1].metric(
    "Queue length",
    stats["stream_length"] if stats["stream_length"] is not None else "—",
)
columns[2].metric(
    "Pending",
    stats["pending_count"] if stats["pending_count"] is not None else "—",
)
columns[3].metric(
    "Dead-letter",
    stats["dead_letter_count"]
    if stats["dead_letter_count"] is not None else "—",
)
if not health.ok:
    st.error(api_error_message(health))
if stats["message"]:
    st.error(stats["message"])

incidents_result = client.list_incidents()
if not incidents_result.ok:
    st.error(api_error_message(incidents_result))
    st.stop()

incidents = shape_incidents(incidents_result.data)
if not incidents:
    st.info(empty_state(incidents))
else:
    st.subheader("Incidents")
    st.dataframe(incidents, use_container_width=True, hide_index=True)
    labels = {
        f"#{item.get('id')} · {item.get('service_name', 'unknown')} · "
        f"{item.get('error_class', 'error')}": item
        for item in incidents
    }
    selected_label = st.selectbox("Incident detail", list(labels))
    selected = incident_detail(labels[selected_label])
    st.subheader("Incident detail")
    st.json({
        key: value for key, value in selected.items()
        if key != "sample_trace" and key != "redaction"
    })
    if selected.get("redaction"):
        st.caption("Redacted values: " + summarize_redaction(
            selected["redaction"]
        ))
    if selected.get("sample_trace"):
        st.code(selected["sample_trace"], language="text")

    st.subheader("Analysis and delivery")
    incident_id = int(selected["id"])
    if st.button("Analyze", key=f"analyze-{incident_id}"):
        result = client.analyze(incident_id, deliver=False)
        if result.ok and isinstance(result.data, dict):
            st.session_state[f"analysis-{incident_id}"] = result.data
        else:
            st.error(api_error_message(result))

    analysis = st.session_state.get(f"analysis-{incident_id}")
    if isinstance(analysis, dict):
        formatted = format_analysis(analysis)
        st.write(f"Agent status: {formatted['status']}")
        st.write(
            "Verification status: "
            + (formatted["verification_status"] or "not available")
        )
        st.write(f"Attempts: {formatted['attempts']}")
        st.write(f"Tokens used: {formatted['tokens_used']}")
        if formatted["reason"]:
            st.info(formatted["reason"])
        if formatted["rejected_attempts"]:
            st.write("Rejected attempts")
            st.dataframe(
                formatted["rejected_attempts"],
                hide_index=True,
                use_container_width=True,
            )
        if formatted["diff"]:
            st.code(formatted["diff"], language="diff")
        st.write("Test evidence")
        st.json(formatted["evidence"])

        verified = delivery_button_enabled(analysis)
        configured_repo = os.getenv("GITHUB_REPO", "").strip()
        response_repo = formatted.get("target_repo", "")
        target_repo = response_repo or configured_repo or "not configured"
        st.info(f"Draft PR target repository: {target_repo}")
        confirmation = st.checkbox(
            "I confirm opening a draft pull request",
            key=f"confirm-delivery-{incident_id}",
            disabled=not verified,
        )
        if st.button(
            "Open draft PR",
            key=f"deliver-{incident_id}",
            disabled=not (verified and confirmation),
        ):
            result = client.analyze(incident_id, deliver=True)
            if not result.ok:
                st.error(api_error_message(result))
            elif isinstance(result.data, dict):
                delivery = result.data.get("delivery")
                if isinstance(delivery, dict):
                    st.write(
                        f"Delivery status: {delivery.get('status', 'unknown')}"
                    )
                    if delivery.get("pr_url"):
                        st.link_button("Open pull request", delivery["pr_url"])
                    if delivery.get("message"):
                        st.info(delivery["message"])
                else:
                    st.error("Delivery status was not returned.")

st.subheader("Storm simulator")
storm_count = st.slider(
    "Identical events", min_value=100, max_value=2000, value=1000, step=100
)
if st.button("Send storm"):
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "service_name": "demo-storm-service",
        "error_class": "RuntimeError",
        "message": "AIOps Streamlit demo storm event",
        "stack_trace": (
            "Traceback (most recent call last):\n"
            '  File "/app/demo.py", line 1, in run_storm\n'
            "RuntimeError: AIOps Streamlit demo storm event"
        ),
    }
    progress = st.progress(0)
    accepted = 0
    not_accepted = 0
    for index in range(storm_count):
        result = client.send_log(payload)
        if result.ok:
            accepted += 1
        else:
            not_accepted += 1
            st.warning(result.message)
            if result.status_code == 429:
                break
        progress.progress((index + 1) / storm_count)
    st.write(f"accepted: {accepted}; not accepted: {not_accepted}")
    st.info("Press Refresh after the worker drains the queue to view incident counts.")

if refresh:
    st.rerun()
if auto_refresh:
    time.sleep(5)
    st.rerun()
