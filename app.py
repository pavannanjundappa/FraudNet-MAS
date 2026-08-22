import asyncio
import csv
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import cast

import streamlit as st
from dotenv import load_dotenv

from agent_framework import Message, AgentResponse
from agent_framework.foundry import FoundryChatClient
from agent_framework.orchestrations import ConcurrentBuilder
from agent_framework._workflows._agent_executor import AgentExecutorResponse
from azure.identity import AzureCliCredential

import data
from tools import (
    lookup_policy,
    lookup_payments,
    lookup_ghost_broking,
    lookup_claims,
    lookup_agent_for_policy,
)

load_dotenv()

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "fraud_decisions.db"
CSV_PATH = PROJECT_DIR / "fraud_decisions.csv"


def ensure_decision_store():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                policy_id TEXT,
                decision TEXT,
                composite_score REAL,
                flagged_by TEXT,
                verdicts_json TEXT,
                summary TEXT,
                human_decision TEXT,
                reviewer_notes TEXT,
                review_status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def save_decision_record(policy_id: str, result: dict, summary: str | None = None):
    try:
        ensure_decision_store()
        summary_text = summary or build_human_summary(policy_id, result)
        record = {
            "policy_id": policy_id,
            "decision": result.get("decision", "UNKNOWN"),
            "composite_score": float(result.get("composite_score", 0.0)),
            "flagged_by": json.dumps(result.get("flagged_by", [])),
            "verdicts_json": json.dumps(result.get("verdicts", [])),
            "summary": summary_text,
            "human_decision": None,
            "reviewer_notes": "",
            "review_status": "pending",
        }

        with sqlite3.connect(str(DB_PATH)) as conn:
            # Check if record exists
            existing = conn.execute(
                "SELECT id FROM decisions WHERE policy_id = ? ORDER BY id DESC LIMIT 1",
                (policy_id,),
            ).fetchone()
            
            if existing is not None:
                # Update existing record
                conn.execute(
                    """
                    UPDATE decisions
                    SET decision = ?, composite_score = ?, flagged_by = ?, verdicts_json = ?,
                        summary = ?, human_decision = NULL, reviewer_notes = '', review_status = 'pending'
                    WHERE id = ?
                    """,
                    (
                        record["decision"],
                        record["composite_score"],
                        record["flagged_by"],
                        record["verdicts_json"],
                        record["summary"],
                        existing[0],
                    ),
                )
            else:
                # Insert new record
                conn.execute(
                    """
                    INSERT INTO decisions (
                        policy_id, decision, composite_score, flagged_by, verdicts_json,
                        summary, human_decision, reviewer_notes, review_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["policy_id"],
                        record["decision"],
                        record["composite_score"],
                        record["flagged_by"],
                        record["verdicts_json"],
                        record["summary"],
                        record["human_decision"],
                        record["reviewer_notes"],
                        record["review_status"],
                    ),
                )
            conn.commit()
        
        print(f"✅ DEBUG: Record saved for policy {policy_id} to {DB_PATH}")
    except Exception as e:
        print(f"❌ ERROR saving decision record for {policy_id}: {str(e)}")
        import traceback
        traceback.print_exc()
        raise

    csv_header = [
        "policy_id",
        "decision",
        "composite_score",
        "flagged_by",
        "verdicts_json",
        "summary",
        "human_decision",
        "reviewer_notes",
        "review_status",
        "created_at",
    ]
    csv_rows = []
    if CSV_PATH.exists():
        with CSV_PATH.open("r", newline="", encoding="utf-8") as csv_file:
            csv_rows = list(csv.DictReader(csv_file))

    updated = False
    for row in csv_rows:
        if row.get("policy_id") == policy_id:
            row.update(
                {
                    "policy_id": record["policy_id"],
                    "decision": record["decision"],
                    "composite_score": record["composite_score"],
                    "flagged_by": record["flagged_by"],
                    "verdicts_json": record["verdicts_json"],
                    "summary": record["summary"],
                    "human_decision": record["human_decision"],
                    "reviewer_notes": record["reviewer_notes"],
                    "review_status": record["review_status"],
                    "created_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            updated = True
            break

    if not updated:
        csv_rows.append(
            {
                "policy_id": record["policy_id"],
                "decision": record["decision"],
                "composite_score": record["composite_score"],
                "flagged_by": record["flagged_by"],
                "verdicts_json": record["verdicts_json"],
                "summary": record["summary"],
                "human_decision": record["human_decision"],
                "reviewer_notes": record["reviewer_notes"],
                "review_status": record["review_status"],
                "created_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    with CSV_PATH.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=csv_header)
        writer.writeheader()
        writer.writerows(csv_rows)


def update_decision_record(policy_id: str, human_decision: str, notes: str = ""):
    try:
        ensure_decision_store()
        with sqlite3.connect(str(DB_PATH)) as conn:
            row = conn.execute(
                "SELECT id FROM decisions WHERE policy_id = ? ORDER BY id DESC LIMIT 1",
                (policy_id,),
            ).fetchone()
            if row is not None:
                conn.execute(
                    """
                    UPDATE decisions
                    SET human_decision = ?, reviewer_notes = ?, review_status = 'resolved'
                    WHERE id = ?
                    """,
                    (human_decision, notes, row[0]),
                )
                conn.commit()
                print(f"✅ DEBUG: Decision updated for policy {policy_id} in {DB_PATH}")
            else:
                print(f"❌ WARNING: No record found for policy {policy_id}")
    except Exception as e:
        print(f"❌ ERROR updating decision record for {policy_id}: {str(e)}")
        import traceback
        traceback.print_exc()
        raise

    if CSV_PATH.exists():
        with CSV_PATH.open("r", newline="", encoding="utf-8") as csv_file:
            rows = list(csv.DictReader(csv_file))

        updated_rows = []
        for row_dict in rows:
            if row_dict.get("policy_id") == policy_id:
                row_dict["human_decision"] = human_decision
                row_dict["reviewer_notes"] = notes
                row_dict["review_status"] = "resolved"
            updated_rows.append(row_dict)

        fieldnames = list(rows[0].keys()) if rows else [
            "policy_id", "decision", "composite_score", "flagged_by", "verdicts_json",
            "summary", "human_decision", "reviewer_notes", "review_status", "created_at",
        ]
        with CSV_PATH.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(updated_rows)


# ---------------------------------------------------------------------------
# JSON contract every specialist agent must follow
# ---------------------------------------------------------------------------
RESPONSE_CONTRACT = """
Respond with ONLY a single JSON object, no extra prose, no markdown fences:
{"agent": "<agent-name>", "fraud_flag": <true|false>, "fraud_type": "<string or null>",
 "confidence": <float 0.0-1.0>, "evidence": "<one short sentence citing the specific
 field/value that drove your verdict>"}
"""

POLICY_INSTRUCTIONS = f"""
You are the Policy Agent in a multi-agent insurance fraud detection system.
You inspect POLICY records only. Always call lookup_policy(policy_id) first —
never answer from assumptions.

Look for these fraud patterns:
- Free-Look Period Abuse: policy cancelled inside the free-look window, often
  right after a claim was filed.
- Backdated Policy Issuance: policy dates appear manipulated to predate a loss.
- Fake Policy - No Premium Collected: premium_actually_collected is False.
- Fronting / Straw Policyholder: policy structure suggests the named insured
  is not the real risk owner (e.g. mismatched channel/state patterns, unusually
  high sum_insured for the premium, Lapsed status shortly after issuance).

If none of these patterns are present, return fraud_flag: false, fraud_type: null.
{RESPONSE_CONTRACT}
"""

PAYMENTS_INSTRUCTIONS = f"""
You are the Payments Agent in a multi-agent insurance fraud detection system.
You inspect PAYMENT records only. Always call lookup_payments(policy_id) first.

Look for these fraud patterns:
- Premium Diversion by Agent: premium_remitted_to_insurer is False, or
  remittance_delay_days is unusually high (>30 days).
- Bounced Cheque Used for Coverage: payment_status is "Bounced" but coverage
  was still active.
- Fake Payment Receipt Submitted: is_duplicate_receipt is True.
- Duplicate/Unauthorized Refund Claimed: payment_status is "Reversed" without
  a clear matching cancellation reason.

If none of these patterns are present, return fraud_flag: false, fraud_type: null.
{RESPONSE_CONTRACT}
"""

BROKER_INSTRUCTIONS = f"""
You are the Broker Integrity Agent in a multi-agent insurance fraud detection
system. You inspect AGENT/BROKER licensing records only.
First call lookup_agent_for_policy(policy_id) to resolve the agent_id, then
call lookup_ghost_broking(agent_id).

Look for these fraud patterns:
- Unlicensed Selling: license_status is not "Valid".
- Expired License Sales: license_status is "Expired".
- Fake/Cloned Agent Identity: license_status is "Fake/Cloned".
- Premium Pocketing - No Remittance: premium_deposited_with_insurer is False.
Also weigh customer_aware_of_agent_status (False is an aggravating factor) and
complaint_count (higher is an aggravating factor).

If none of these patterns are present, return fraud_flag: false, fraud_type: null.
{RESPONSE_CONTRACT}
"""

CLAIMS_INSTRUCTIONS = f"""
You are the Claims Agent in a multi-agent insurance fraud detection system.
You inspect CLAIM records only. Always call lookup_claims(policy_id) first.

Look for these fraud patterns:
- Early Claim Fraud: days_policy_to_incident is less than 30.
- Staged Accident: suspicious combination of low witness_count and
  police_report_filed being False for an incident that should typically
  have witnesses/a report.
- Exaggerated Claim Amount: approved_amount is far lower than claim_amount
  (e.g. less than 40% of it approved).
- Multiple/Duplicate Claims: num_claims_filed_by_customer is 2 or more.
- Fake Supporting Documents: documents_altered_flag is True.

If none of these patterns are present, return fraud_flag: false, fraud_type: null.
{RESPONSE_CONTRACT}
"""


def build_chat_client() -> FoundryChatClient:
    credential = AzureCliCredential()
    return FoundryChatClient(
        credential=credential,
        project_endpoint=os.getenv("AZURE_AI_PROJECT_ENDPOINT"),
        model=os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME"),
    )


def build_specialist_agents(chat_client: FoundryChatClient):
    policy_agent = chat_client.as_agent(
        name="policy-agent",
        instructions=POLICY_INSTRUCTIONS,
        tools=[lookup_policy],
    )
    payments_agent = chat_client.as_agent(
        name="payments-agent",
        instructions=PAYMENTS_INSTRUCTIONS,
        tools=[lookup_payments],
    )
    broker_agent = chat_client.as_agent(
        name="broker-integrity-agent",
        instructions=BROKER_INSTRUCTIONS,
        tools=[lookup_agent_for_policy, lookup_ghost_broking],
    )
    claims_agent = chat_client.as_agent(
        name="claims-agent",
        instructions=CLAIMS_INSTRUCTIONS,
        tools=[lookup_claims],
    )
    return policy_agent, payments_agent, broker_agent, claims_agent


# ---------------------------------------------------------------------------
# Fusion logic — the core design decision, kept explicit and testable
# ---------------------------------------------------------------------------
def _parse_verdict(raw_text: str, agent_name_fallback: str) -> dict:
    """Best-effort parse of a specialist agent's JSON verdict. Never raises —
    a malformed response is treated as a non-flagging, zero-confidence verdict
    so one bad agent response can't crash the whole investigation."""
    text = raw_text.strip()
    # strip accidental markdown fences
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
        return {
            "agent": parsed.get("agent", agent_name_fallback),
            "fraud_flag": bool(parsed.get("fraud_flag", False)),
            "fraud_type": parsed.get("fraud_type"),
            "confidence": float(parsed.get("confidence", 0.0)),
            "evidence": parsed.get("evidence", ""),
        }
    except (json.JSONDecodeError, TypeError, ValueError):
        return {
            "agent": agent_name_fallback,
            "fraud_flag": False,
            "fraud_type": None,
            "confidence": 0.0,
            "evidence": "agent returned an unparseable response",
        }


def fuse_verdicts(responses: list[AgentExecutorResponse]) -> str:
    """Aggregator passed to ConcurrentBuilder.with_aggregator(). Receives one
    AgentExecutorResponse per specialist agent, parses each verdict, and
    applies the fusion rule. Returns a JSON string (becomes the workflow's
    single output message)."""
    verdicts = []
    for r in responses:
        agent_name = r.executor_id
        text = r.agent_response.text if r.agent_response is not None else ""
        verdicts.append(_parse_verdict(text, agent_name))

    flagging = [v for v in verdicts if v["fraud_flag"]]
    composite_score = (
        sum(v["confidence"] for v in flagging) / len(flagging) if flagging else 0.0
    )

    high_conf_flags = [v for v in flagging if v["confidence"] >= 0.7]
    if len(high_conf_flags) >= 2:
        decision = "ESCALATE"
    elif len(flagging) == 1 and flagging[0]["confidence"] >= 0.8:
        decision = "REVIEW"
    else:
        decision = "APPROVE"

    result = {
        "composite_score": round(composite_score, 3),
        "decision": decision,
        "flagged_by": [v["agent"] for v in flagging],
        "verdicts": verdicts,
    }
    return json.dumps(result, indent=2)


def build_investigation_workflow(chat_client: FoundryChatClient):
    policy_agent, payments_agent, broker_agent, claims_agent = build_specialist_agents(chat_client)
    workflow = (
        ConcurrentBuilder(participants=[policy_agent, payments_agent, broker_agent, claims_agent])
        .with_aggregator(fuse_verdicts)
        .build()
    )
    return workflow


async def investigate(policy_id: str) -> dict:
    """Run all four specialist agents concurrently against a case and return
    the fused decision as a dict."""
    chat_client = build_chat_client()
    workflow = build_investigation_workflow(chat_client)
    result = await workflow.run(
        f"Investigate policy_id: {policy_id}. Look up whatever records you "
        f"need using your tools and return your verdict."
    )
    outputs = result.get_outputs()
    if not outputs:
        return {"composite_score": 0.0, "decision": "ERROR", "flagged_by": [], "verdicts": []}

    # the aggregator returns a plain JSON string as the workflow output
    raw = outputs[0]
    if isinstance(raw, str):
        return json.loads(raw)
    # fallback in case the SDK wraps it in a Message/AgentResponse instead
    text = getattr(raw, "text", None) or str(raw)
    return json.loads(text)


async def main():
    policy_id = sys.argv[1] if len(sys.argv) > 1 else "POL000914"
    print(f"Investigating {policy_id} ...\n")
    result = await investigate(policy_id)

    print("=" * 60)
    print(f"DECISION: {result['decision']}  (composite score: {result['composite_score']})")
    print(f"Flagged by: {', '.join(result['flagged_by']) or 'no agents'}")
    print("=" * 60)
    for v in result["verdicts"]:
        status = "FRAUD" if v["fraud_flag"] else "clear"
        print(f"[{v['agent']}] {status} (confidence={v['confidence']:.2f}) "
              f"type={v['fraud_type']}\n    evidence: {v['evidence']}")


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin"


def _get_theme_settings():
    theme_name = st.session_state.get("app_theme", "Dark")
    if theme_name == "Custom":
        bg = st.session_state.get("theme_bg", "#f4f7fb")
        panel = st.session_state.get("theme_panel", "#ffffff")
        card = st.session_state.get("theme_card", "#f0f4f9")
        text = st.session_state.get("theme_text", "#102030")
        accent = st.session_state.get("theme_accent", "#2f74ff")
    elif theme_name == "Light":
        bg = "#f4f7fb"
        panel = "#ffffff"
        card = "#edf3fb"
        text = "#102030"
        accent = "#2f74ff"
    else:
        bg = "#0b1220"
        panel = "#111c2d"
        card = "#16253d"
        text = "#eaf2ff"
        accent = "#5dade2"

    return {
        "bg": bg,
        "panel": panel,
        "card": card,
        "muted": "#5b6d87" if text == "#102030" else "#98a9c4",
        "text": text,
        "green": "#2ecc71",
        "red": "#e74c3c",
        "amber": "#f39c12",
        "blue": accent,
    }


def apply_app_styles():
    theme = _get_theme_settings()
    st.markdown(
        f"""
        <style>
            :root {{
                --bg: {theme['bg']};
                --panel: {theme['panel']};
                --card: {theme['card']};
                --muted: {theme['muted']};
                --text: {theme['text']};
                --green: {theme['green']};
                --red: {theme['red']};
                --amber: {theme['amber']};
                --blue: {theme['blue']};
            }}
            .stApp {{
                background: linear-gradient(135deg, var(--bg) 0%, color-mix(in srgb, var(--bg) 80%, white) 40%, var(--card) 100%);
                color: var(--text);
            }}
            .stSidebar {{
                background: var(--panel);
                border-right: 1px solid rgba(255,255,255,0.08);
            }}
            .css-1d391kg, .css-18ni7ap {{
                background: transparent;
            }}
            .stDataFrame, .stTable {{
                background: var(--card);
                border-radius: 10px;
            }}
            .metric-container {{
                background: var(--card);
                border: 1px solid rgba(255,255,255,0.06);
                border-radius: 12px;
                padding: 0.8rem;
            }}
            .summary-card {{
                background: var(--card);
                border: 1px solid rgba(255,255,255,0.06);
                border-radius: 12px;
                padding: 1rem;
                margin-top: 0.75rem;
            }}
            div[data-testid="stMetric"] {{
                background: var(--card);
                border: 1px solid rgba(255,255,255,0.06);
                border-radius: 12px;
                padding: 0.9rem;
            }}
            h1, h2, h3, h4, p, label, .st-emotion-cache-10trblm, .st-emotion-cache-1inwzcr {{
                color: var(--text) !important;
            }}
            .stTextInput > div > div > input,
            .stTextInput input,
            .stSelectbox select,
            .stButton > button {{
                border-radius: 10px;
            }}
            .stButton > button {{
                background: linear-gradient(90deg, var(--blue), color-mix(in srgb, var(--blue) 75%, white));
                color: white;
                border: none;
                font-weight: 600;
            }}
            .stButton > button:hover {{
                background: linear-gradient(90deg, color-mix(in srgb, var(--blue) 85%, black), var(--blue));
            }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _get_history():
    if "investigation_history" not in st.session_state:
        st.session_state["investigation_history"] = []
    return st.session_state["investigation_history"]


def get_dashboard_decision(item: dict) -> str:
    value = item.get("human_decision") or item.get("decision") or "UNKNOWN"
    value = str(value).upper()
    if value == "ALLOW":
        return "ALLOW"
    return value


def load_csv_decision_rows() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open("r", newline="", encoding="utf-8") as csv_file:
        rows = list(csv.DictReader(csv_file))
    return rows


def get_decision_store_rows() -> list[dict]:
    ensure_decision_store()
    csv_rows = load_csv_decision_rows()
    if csv_rows:
        return csv_rows
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM decisions ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def compute_dashboard_stats() -> dict:
    csv_rows = load_csv_decision_rows()
    rows = csv_rows if csv_rows else get_decision_store_rows()
    if rows:
        total_cases = len(rows)
        escalations = sum(1 for item in rows if get_dashboard_decision(item) == "ESCALATE")
        reviews = sum(1 for item in rows if get_dashboard_decision(item) == "REVIEW")
        approvals = sum(1 for item in rows if get_dashboard_decision(item) in {"APPROVE", "ALLOW"})
        avg_score = round(
            sum(float(item.get("composite_score", 0.0)) for item in rows) / total_cases,
            2,
        )
        agent_counts: dict[str, int] = {}
        for item in rows:
            verdicts = json.loads(item.get("verdicts_json") or item.get("verdicts") or "[]")
            for verdict in verdicts:
                if verdict.get("fraud_flag"):
                    agent = verdict.get("agent", "Unknown")
                    agent_counts[agent] = agent_counts.get(agent, 0) + 1
        return {
            "total_cases": total_cases,
            "escalations": escalations,
            "reviews": reviews,
            "approvals": approvals,
            "avg_score": avg_score,
            "agent_counts": agent_counts,
            "source": "csv",
        }

    history = _get_history()
    if history:
        total_cases = len(history)
        escalations = sum(1 for item in history if get_dashboard_decision(item) == "ESCALATE")
        reviews = sum(1 for item in history if get_dashboard_decision(item) == "REVIEW")
        approvals = sum(1 for item in history if get_dashboard_decision(item) in {"APPROVE", "ALLOW"})
        avg_score = round(sum(float(item.get("composite_score", 0.0)) for item in history) / total_cases, 2)
        agent_counts = {}
        for item in history:
            for verdict in item.get("verdicts", []):
                if verdict.get("fraud_flag"):
                    agent = verdict.get("agent", "Unknown")
                    agent_counts[agent] = agent_counts.get(agent, 0) + 1
        return {
            "total_cases": total_cases,
            "escalations": escalations,
            "reviews": reviews,
            "approvals": approvals,
            "avg_score": avg_score,
            "agent_counts": agent_counts,
            "source": "history",
        }

    data._load()
    policies = data._policies
    total_cases = len(policies) if policies is not None else 0
    flagged_cases = int((policies["fraud_flag"].fillna(False) == True).sum()) if policies is not None else 0
    approvals = total_cases - flagged_cases
    return {
        "total_cases": total_cases,
        "escalations": flagged_cases,
        "reviews": 0,
        "approvals": approvals,
        "avg_score": round((flagged_cases / total_cases) * 100, 2) if total_cases else 0.0,
        "agent_counts": {},
        "source": "dataset",
    }


def build_human_summary(policy_id: str, result: dict) -> str:
    decision = str(result.get("decision", "UNKNOWN")).upper()
    verdicts = result.get("verdicts", [])
    high_risk = [v for v in verdicts if v.get("fraud_flag")]

    if decision == "ESCALATE":
        action = "This policy should be escalated to senior review and investigation."
    elif decision == "REVIEW":
        action = "This policy needs human review before a final decision is made."
    else:
        action = "This policy can be cleared unless more evidence appears during manual review."

    if high_risk:
        risk_text = ", ".join(
            f"{v.get('agent', 'Agent')} flagged {v.get('fraud_type') or 'fraud risk'} with {float(v.get('confidence', 0.0)):.2f} confidence"
            for v in high_risk
        )
    else:
        risk_text = "No high-confidence fraud pattern was detected by the specialist agents."

    summary = (
        f"Policy {policy_id} was reviewed by the fraud detection agents. "
        f"The system returned a {decision.lower()} decision. "
        f"{risk_text}. {action}"
    )
    return summary


def render_result(result: dict, policy_id: str | None = None):
    if not result:
        st.warning("No investigation result was returned.")
        return

    decision = result.get("decision", "UNKNOWN")
    composite_score = result.get("composite_score", 0.0)
    verdicts = result.get("verdicts", [])

    if policy_id:
        st.caption(f"Policy ID: {policy_id}")

    human_summary = build_human_summary(policy_id or "unknown", result)
    st.subheader("Agents summary")
    st.write(human_summary)

    st.subheader("Investigation summary")
    col1, col2 = st.columns(2)
    with col1:
        if decision.upper() in {"APPROVE", "CLEAR", "ALLOW"}:
            st.markdown(f"<h3 style='color: #2ecc71;'>Decision: {decision}</h3>", unsafe_allow_html=True)
        else:
            st.markdown(f"<h3 style='color: #e74c3c;'>Decision: {decision}</h3>", unsafe_allow_html=True)
    with col2:
        st.metric("Composite score", f"{composite_score:.2f}")

    if verdicts:
        table_rows = []
        for verdict in verdicts:
            table_rows.append(
                {
                    "Agent": verdict.get("agent", "Unknown"),
                    "Status": "Fraud" if verdict.get("fraud_flag") else "Clear",
                    "Type": verdict.get("fraud_type") or "None",
                    "Confidence": float(verdict.get("confidence", 0.0)),
                    "Evidence": verdict.get("evidence", "No evidence provided"),
                }
            )

        st.dataframe(
            table_rows,
            use_container_width=True,
            hide_index=True,
            column_order=["Agent", "Status", "Type", "Confidence", "Evidence"],
        )
    else:
        st.info("No specialist verdicts were returned.")

    if policy_id:
        review_key = f"human_review_{policy_id}"
        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button("✅ Send to Human Review", key=review_key, use_container_width=True):
                save_decision_record(policy_id, result, human_summary)
                add_case_to_review(policy_id, result)
                st.session_state["last_review_summary"] = human_summary
                st.success(f"✅ Policy {policy_id} has been saved and queued for human review!")
                st.rerun()


def dashboard_page():
    st.title("Fraud dashboard")
    stats = compute_dashboard_stats()

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total cases", stats["total_cases"])
    with col2:
        st.metric("Escalate", stats["escalations"])
    with col3:
        st.metric("Review", stats["reviews"])
    with col4:
        st.metric("Approve", stats["approvals"])

    st.caption(f"Average score: {stats['avg_score']:.2f}")

    # Tabs for different views
    tab1, tab2 = st.tabs(["Recent Decisions", "Pending Reviews"])

    with tab1:
        store_rows = get_decision_store_rows()
        if store_rows:
            recent_rows = []
            for item in store_rows[:15]:
                ai_decision = item.get("decision", "UNKNOWN")
                human_decision = item.get("human_decision") or "—"
                review_status = item.get("review_status", "pending")
                
                # Show human decision if available, otherwise show AI decision
                final_decision = human_decision if human_decision != "—" else ai_decision
                
                recent_rows.append(
                    {
                        "Policy ID": item.get("policy_id", "--"),
                        "AI Decision": ai_decision,
                        "Final Decision": final_decision,
                        "Score": float(item.get("composite_score", 0.0)),
                        "Status": "✅ Resolved" if review_status == "resolved" else "⏳ Pending",
                    }
                )
            st.dataframe(recent_rows, use_container_width=True, hide_index=True)
        else:
            st.info("Recent investigation history will appear here as soon as you run a case.")

    with tab2:
        store_rows = get_decision_store_rows()
        pending_rows = [item for item in store_rows if item.get("review_status") == "pending"]
        
        if pending_rows:
            pending_table = []
            for item in pending_rows:
                pending_table.append(
                    {
                        "Policy ID": item.get("policy_id", "--"),
                        "AI Decision": item.get("decision", "UNKNOWN"),
                        "Score": float(item.get("composite_score", 0.0)),
                        "Summary": item.get("summary", "No summary")[:80] + "..." if item.get("summary") else "—",
                    }
                )
            st.dataframe(pending_table, use_container_width=True, hide_index=True)
        else:
            st.info("No pending reviews. All cases have been resolved!")

    if stats.get("agent_counts"):
        st.subheader("Fraud signals by agent")
        agent_df = [{"Agent": k, "Fraud signals": v} for k, v in stats["agent_counts"].items()]
        st.dataframe(agent_df, use_container_width=True, hide_index=True)
    else:
        st.info("No agent fraud signals recorded yet. Run an investigation to populate this dashboard.")


def ensure_review_queue(force: bool = False):
    if "human_review_queue" not in st.session_state or st.session_state["human_review_queue"] is None:
        st.session_state["human_review_queue"] = []

    if force:
        queued = []
        ensure_decision_store()
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT policy_id, decision, composite_score, flagged_by, verdicts_json, summary, human_decision, reviewer_notes, review_status FROM decisions WHERE review_status = 'pending' ORDER BY created_at DESC"
            ).fetchall()

        for row in rows:
            verdicts = json.loads(row["verdicts_json"] or "[]")
            queued.append(
                {
                    "policy_id": row["policy_id"],
                    "decision": row["decision"],
                    "composite_score": float(row["composite_score"] or 0.0),
                    "flagged_by": json.loads(row["flagged_by"] or "[]"),
                    "verdicts": verdicts,
                    "human_decision": row["human_decision"],
                    "notes": row["reviewer_notes"] or "",
                    "summary": row["summary"] or build_human_summary(row["policy_id"], {"decision": row["decision"], "composite_score": row["composite_score"], "verdicts": verdicts}),
                }
            )

        if not queued and CSV_PATH.exists():
            with CSV_PATH.open("r", newline="", encoding="utf-8") as csv_file:
                csv_rows = list(csv.DictReader(csv_file))
            for row in csv_rows:
                if row.get("review_status") == "pending":
                    verdicts = json.loads(row.get("verdicts_json") or "[]")
                    queued.append(
                        {
                            "policy_id": row.get("policy_id"),
                            "decision": row.get("decision", "UNKNOWN"),
                            "composite_score": float(row.get("composite_score", 0.0) or 0.0),
                            "flagged_by": json.loads(row.get("flagged_by") or "[]"),
                            "verdicts": verdicts,
                            "human_decision": row.get("human_decision"),
                            "notes": row.get("reviewer_notes") or "",
                            "summary": row.get("summary") or build_human_summary(row.get("policy_id") or "unknown", {"decision": row.get("decision"), "composite_score": row.get("composite_score", 0.0), "verdicts": verdicts}),
                        }
                    )

        st.session_state["human_review_queue"] = queued


def add_case_to_review(policy_id: str, result: dict):
    # Reload from database to get the latest
    ensure_review_queue(force=True)
    
    queue = st.session_state["human_review_queue"]
    # Check if this policy is already in the queue
    if not any(item["policy_id"] == policy_id for item in queue):
        # Don't add manually, let the database be the source of truth
        pass
    
    # Always reload from DB after any changes
    ensure_review_queue(force=True)


def human_review_page():
    # Always reload from database to ensure we have the latest pending records
    ensure_review_queue(force=True)
    st.title("Human in the loop review")
    st.subheader("Insurance agent final decision")

    queue = st.session_state["human_review_queue"]
    
    # Show status
    col1, col2 = st.columns([1, 3])
    with col1:
        st.metric("Cases Queued", len(queue))
    with col2:
        if len(queue) > 0:
            st.success(f"✅ {len(queue)} case(s) awaiting your review")
        else:
            st.info("No pending cases")
    
    # Debug: Show database path and record count
    with st.expander("🔧 Debug Info", expanded=False):
        st.write(f"**Database Path:** `{DB_PATH}`")
        st.write(f"**Database Exists:** {DB_PATH.exists()}")
        try:
            with sqlite3.connect(str(DB_PATH)) as conn:
                total_records = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
                pending_records = conn.execute("SELECT COUNT(*) FROM decisions WHERE review_status='pending'").fetchone()[0]
                resolved_records = conn.execute("SELECT COUNT(*) FROM decisions WHERE review_status='resolved'").fetchone()[0]
            st.write(f"**Total Records in DB:** {total_records}")
            st.write(f"**Pending:** {pending_records} | **Resolved:** {resolved_records}")
            
            # Show all records
            with sqlite3.connect(str(DB_PATH)) as conn:
                conn.row_factory = sqlite3.Row
                all_rows = conn.execute("SELECT policy_id, decision, review_status, created_at FROM decisions ORDER BY created_at DESC LIMIT 20").fetchall()
            if all_rows:
                st.write("**Recent Records:**")
                for row in all_rows:
                    st.write(f"  - {row['policy_id']}: {row['decision']} ({row['review_status']}) - {row['created_at']}")
        except Exception as e:
            st.error(f"Error reading database: {str(e)}")
    
    if not queue:
        st.warning("No cases are currently queued for human review.")
        st.info("**How to queue a case:**\n1. Go to 'Policy Investigation'\n2. Enter a Policy ID\n3. Click 'Investigate policy'\n4. Review the results\n5. Click '✅ Send to Human Review'")
        return

    selected_policy = st.selectbox("Select policy for review", [item["policy_id"] for item in queue])
    case = next(item for item in queue if item["policy_id"] == selected_policy)

    st.markdown(
        f"<div class='summary-card'><strong>Policy:</strong> {case['policy_id']}<br>"
        f"<strong>AI decision:</strong> {case['decision']}<br>"
        f"<strong>Composite score:</strong> {float(case['composite_score']):.2f}<br>"
        f"<strong>Recommendation:</strong> {case.get('summary', '')}</div>",
        unsafe_allow_html=True,
    )

    st.subheader("Case summary")
    st.write(case.get("summary", "No summary available."))

    # Fetch and display related policy data
    policy_data = data.get_policy_record(selected_policy)
    if policy_data:
        with st.expander("📋 Policy Details", expanded=True):
            col1, col2 = st.columns(2)
            with col1:
                st.write(f"**Policy ID:** {policy_data.get('policy_id', 'N/A')}")
                st.write(f"**Insured Name:** {policy_data.get('insured_name', 'N/A')}")
                st.write(f"**Issue Date:** {policy_data.get('issue_date', 'N/A')}")
                st.write(f"**Premium:** {policy_data.get('premium', 'N/A')}")
                st.write(f"**Sum Insured:** {policy_data.get('sum_insured', 'N/A')}")
            with col2:
                st.write(f"**Status:** {policy_data.get('policy_status', 'N/A')}")
                st.write(f"**Channel:** {policy_data.get('channel', 'N/A')}")
                st.write(f"**State:** {policy_data.get('state', 'N/A')}")
                st.write(f"**Agent ID:** {policy_data.get('agent_id', 'N/A')}")
                st.write(f"**Fraud Flag (Ground Truth):** {'Yes' if policy_data.get('fraud_flag') else 'No'}")

    # Fetch and display payment records
    payment_records = data.get_payment_records(selected_policy)
    if payment_records:
        with st.expander("💳 Payment Records", expanded=False):
            payment_table = []
            for payment in payment_records:
                payment_table.append({
                    "Payment ID": payment.get("payment_id", "N/A"),
                    "Amount": payment.get("amount", 0),
                    "Status": payment.get("payment_status", "N/A"),
                    "Remitted to Insurer": payment.get("premium_remitted_to_insurer", False),
                    "Delay Days": payment.get("remittance_delay_days", 0),
                })
            st.dataframe(payment_table, use_container_width=True, hide_index=True)
    else:
        with st.expander("💳 Payment Records", expanded=False):
            st.info("No payment records found for this policy.")

    # Fetch and display claim records
    claim_records = data.get_claim_records(selected_policy)
    if claim_records:
        with st.expander("📄 Claims History", expanded=False):
            claim_table = []
            for claim in claim_records:
                claim_table.append({
                    "Claim ID": claim.get("claim_id", "N/A"),
                    "Claim Amount": claim.get("claim_amount", 0),
                    "Approved Amount": claim.get("approved_amount", 0),
                    "Status": claim.get("claim_status", "N/A"),
                    "Days to Incident": claim.get("days_policy_to_incident", 0),
                })
            st.dataframe(claim_table, use_container_width=True, hide_index=True)
    else:
        with st.expander("📄 Claims History", expanded=False):
            st.info("No claims found for this policy.")

    # Fetch and display broker/agent integrity info
    agent_id = data.get_agent_id_for_policy(selected_policy)
    if agent_id:
        broker_records = data.get_ghost_broking_records(agent_id)
        if broker_records:
            with st.expander("🔍 Broker Integrity Info", expanded=False):
                broker_table = []
                for broker in broker_records:
                    broker_table.append({
                        "Agent ID": broker.get("agent_id", "N/A"),
                        "License Status": broker.get("license_status", "N/A"),
                        "Complaints": broker.get("complaint_count", 0),
                        "Premium Deposited": broker.get("premium_deposited_with_insurer", False),
                        "Customer Aware": broker.get("customer_aware_of_agent_status", False),
                    })
                st.dataframe(broker_table, use_container_width=True, hide_index=True)

    if case.get("verdicts"):
        st.subheader("Agent evidence table")
        st.dataframe(
            [
                {
                    "Agent": v.get("agent", "Unknown"),
                    "Status": "Fraud" if v.get("fraud_flag") else "Clear",
                    "Type": v.get("fraud_type") or "None",
                    "Confidence": float(v.get("confidence", 0.0)),
                    "Evidence": v.get("evidence", "No evidence provided"),
                }
                for v in case["verdicts"]
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.divider()
    st.subheader("Your Final Decision")
    decision = st.radio(
        "Select action",
        ["ALLOW", "REJECT", "REVIEW", "ESCALATE"],
        index=0,
        horizontal=True,
    )
    notes = st.text_area("Reviewer notes", value=case.get("notes", ""), height=100)

    if st.button("✅ Submit final decision", use_container_width=True):
        update_decision_record(selected_policy, decision, notes)
        for item in queue:
            if item["policy_id"] == selected_policy:
                item["human_decision"] = decision
                item["notes"] = notes
                break
        st.session_state["human_review_queue"] = [item for item in queue if item["policy_id"] != selected_policy]
        st.success(f"✅ Final decision saved for policy {selected_policy}: **{decision}**")
        st.rerun()

    st.divider()
    st.subheader("Queued review list")
    review_table = []
    for item in queue:
        review_table.append(
            {
                "Policy ID": item["policy_id"],
                "AI Decision": item["decision"],
                "Score": float(item.get("composite_score", 0.0)),
                "Human Decision": item.get("human_decision") or "⏳ Pending",
            }
        )
    st.dataframe(review_table, use_container_width=True, hide_index=True)


def login_page():
    st.title("FraudNet MAS")
    st.subheader("Login")

    username = st.text_input("Username")
    password = st.text_input("Password", type="password")

    if st.button("Login"):
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            st.session_state["authenticated"] = True
            st.session_state["username"] = username
            st.rerun()
        else:
            st.error("Invalid username or password.")


def policy_search_page():
    st.title("FraudNet MAS")
    st.caption(f"Logged in as: {st.session_state.get('username', 'admin')}")

    policy_id = st.text_input("Policy ID", placeholder="POL000914")

    if st.button("Investigate policy") and policy_id.strip():
        with st.spinner(f"Investigating policy {policy_id}..."):
            result = asyncio.run(investigate(policy_id.strip()))

        history = _get_history()
        history.insert(
            0,
            {
                "policy_id": policy_id.strip(),
                "decision": result.get("decision", "UNKNOWN"),
                "composite_score": result.get("composite_score", 0.0),
                "flagged_by": result.get("flagged_by", []),
                "verdicts": result.get("verdicts", []),
            },
        )
        if len(history) > 10:
            history.pop()

        render_result(result, policy_id.strip())
    else:
        st.info("Enter a policy ID and click 'Investigate policy' to review the fraud risk assessment.")


def streamlit_app():
    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False

    if "investigation_history" not in st.session_state:
        st.session_state["investigation_history"] = []

    if "app_theme" not in st.session_state:
        st.session_state["app_theme"] = "Dark"
    if "theme_bg" not in st.session_state:
        st.session_state["theme_bg"] = "#f4f7fb"
    if "theme_panel" not in st.session_state:
        st.session_state["theme_panel"] = "#ffffff"
    if "theme_card" not in st.session_state:
        st.session_state["theme_card"] = "#edf3fb"
    if "theme_text" not in st.session_state:
        st.session_state["theme_text"] = "#102030"
    if "theme_accent" not in st.session_state:
        st.session_state["theme_accent"] = "#2f74ff"

    ensure_review_queue()

    if not st.session_state["authenticated"]:
        apply_app_styles()
        login_page()
        return

    st.sidebar.title("FraudNet MAS")
    st.session_state["app_theme"] = st.sidebar.selectbox(
        "Theme",
        ["Dark", "Light", "Custom"],
        index=["Dark", "Light", "Custom"].index(st.session_state.get("app_theme", "Dark")),
    )
    if st.session_state["app_theme"] == "Custom":
        st.session_state["theme_bg"] = st.sidebar.color_picker("Background", st.session_state.get("theme_bg", "#f4f7fb"))
        st.session_state["theme_panel"] = st.sidebar.color_picker("Sidebar", st.session_state.get("theme_panel", "#ffffff"))
        st.session_state["theme_card"] = st.sidebar.color_picker("Cards", st.session_state.get("theme_card", "#edf3fb"))
        st.session_state["theme_text"] = st.sidebar.color_picker("Text", st.session_state.get("theme_text", "#102030"))
        st.session_state["theme_accent"] = st.sidebar.color_picker("Accent", st.session_state.get("theme_accent", "#2f74ff"))
    apply_app_styles()
    st.sidebar.caption(f"Queued for review: {len(st.session_state['human_review_queue'])}")
    page = st.sidebar.radio(
        "Navigation",
        ["Dashboard", "Policy Investigation", "Human Review"],
        index=1,
    )

    if st.sidebar.button("Logout"):
        st.session_state["authenticated"] = False
        st.session_state.pop("username", None)
        st.rerun()

    if page == "Dashboard":
        dashboard_page()
    elif page == "Human Review":
        human_review_page()
    else:
        policy_search_page()


if __name__ == "__main__":
    streamlit_app()
