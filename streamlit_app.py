"""
streamlit_app.py — minimal demo UI for FraudMesh.
Run with: streamlit run streamlit_app.py
"""
import asyncio

import pandas as pd
import streamlit as st

import data
from app import investigate

st.set_page_config(page_title="FraudMesh", page_icon="🕵️", layout="centered")
st.title("🕵️ FraudMesh — Multi-Agent Insurance Fraud Detection")
st.caption("Policy · Payments · Broker Integrity · Claims — 4 specialist agents, one fused verdict")

policy_id = st.text_input("Policy ID", value="POL000914")

DECISION_COLOR = {"APPROVE": "green", "REVIEW": "orange", "ESCALATE": "red", "ERROR": "gray"}

if st.button("Run Investigation", type="primary"):
    with st.spinner("Running 4 specialist agents concurrently..."):
        try:
            result = asyncio.run(investigate(policy_id))
        except Exception as e:  # noqa: BLE001 — surface any Azure/auth error to the UI
            st.error(f"Investigation failed: {e}")
            st.stop()

    color = DECISION_COLOR.get(result["decision"], "gray")
    st.markdown(
        f"### Decision: :{color}[{result['decision']}]  "
        f"&nbsp;&nbsp; Composite score: `{result['composite_score']}`"
    )
    if result["flagged_by"]:
        st.write(f"Flagged by: **{', '.join(result['flagged_by'])}**")

    st.subheader("Specialist verdicts")
    verdict_df = pd.DataFrame(result["verdicts"])
    st.dataframe(verdict_df, use_container_width=True, hide_index=True)

    with st.expander("Raw case data (all 4 tables)"):
        st.write("**Policy**")
        st.json(data.get_policy_record(policy_id))
        st.write("**Payments**")
        st.json(data.get_payment_records(policy_id))
        agent_id = data.get_agent_id_for_policy(policy_id)
        st.write("**Ghost broking**")
        st.json(data.get_ghost_broking_records(agent_id) if agent_id else [])
        st.write("**Claims**")
        st.json(data.get_claim_records(policy_id))
