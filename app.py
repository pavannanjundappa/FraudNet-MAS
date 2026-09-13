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
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
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
    finally:
        conn.close()


def _sync_csv_from_db():
    rows = []
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM decisions ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()

    fieldnames = [
        "id",
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
    csv_rows = [dict(row) for row in rows]
    with CSV_PATH.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)


def save_decision_record(policy_id: str, result: dict, summary: str | None = None):
    if not policy_id:
        raise ValueError("policy_id is required")

    ensure_decision_store()
    summary_text = (summary or build_human_summary(policy_id, result) or "No summary").strip()

    clean_policy_id = str(policy_id).strip()
    clean_decision = str(result.get("decision", "UNKNOWN")).upper()
    clean_score = float(result.get("composite_score", 0.0) or 0.0)
    clean_flagged = json.dumps(result.get("flagged_by", []) or [], ensure_ascii=False)
    clean_verdicts = json.dumps(result.get("verdicts", []) or [], ensure_ascii=False)

    conn = sqlite3.connect(str(DB_PATH))
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM decisions WHERE policy_id = ? ORDER BY id DESC LIMIT 1",
            (clean_policy_id,),
        )
        existing = cursor.fetchone()

        if existing is not None:
            cursor.execute(
                """
                UPDATE decisions
                SET decision = ?, composite_score = ?, flagged_by = ?, verdicts_json = ?,
                    summary = ?, human_decision = NULL, reviewer_notes = '', review_status = 'pending',
                    created_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    clean_decision,
                    clean_score,
                    clean_flagged,
                    clean_verdicts,
                    summary_text,
                    existing[0],
                ),
            )
        else:
            cursor.execute(
                """
                INSERT INTO decisions (
                    policy_id, decision, composite_score, flagged_by, verdicts_json,
                    summary, human_decision, reviewer_notes, review_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_policy_id,
                    clean_decision,
                    clean_score,
                    clean_flagged,
                    clean_verdicts,
                    summary_text,
                    None,
                    "",
                    "pending",
                ),
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"❌ DB ERROR in save_decision_record: {exc}")
        raise exc
    finally:
        conn.close()

    _sync_csv_from_db()
    if "human_review_queue" in st.session_state:
        st.session_state["human_review_queue"] = []
    ensure_review_queue(force=True)
    return True


def update_decision_record(policy_id: str, human_decision: str, notes: str = ""):
    ensure_decision_store()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM decisions WHERE policy_id = ? ORDER BY id DESC LIMIT 1",
            (str(policy_id).strip(),),
        )
        row = cursor.fetchone()
        if row is not None:
            cursor.execute(
                """
                UPDATE decisions
                SET human_decision = ?, reviewer_notes = ?, review_status = 'resolved'
                WHERE id = ?
                """,
                (str(human_decision).strip(), str(notes).strip(), row[0]),
            )
            conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"❌ DB ERROR in update_decision_record: {exc}")
        raise exc
    finally:
        conn.close()

    _sync_csv_from_db()


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
# Fusion logic
# ---------------------------------------------------------------------------
def _parse_verdict(raw_text: str, agent_name_fallback: str) -> dict:
    text = raw_text.strip()
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
    chat_client = build_chat_client()
    workflow = build_investigation_workflow(chat_client)
    result = await workflow.run(
        f"Investigate policy_id: {policy_id}. Look up whatever records you "
        f"need using your tools and return your verdict."
    )
    outputs = result.get_outputs()
    if not outputs:
        return {"composite_score": 0.0, "decision": "ERROR", "flagged_by": [], "verdicts": []}

    raw = outputs[0]
    if isinstance(raw, str):
        return json.loads(raw)
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
REVIEWER_USERNAME = "reviewer"
REVIEWER_PASSWORD = "reviewer"
VALID_LOGIN_PAIRS = {
    ADMIN_USERNAME: ADMIN_PASSWORD,
    REVIEWER_USERNAME: REVIEWER_PASSWORD,
}


def is_valid_login(username: str | None, password: str | None) -> bool:
    if not username or not password:
        return False
    username = username.strip()
    return VALID_LOGIN_PAIRS.get(username) == password


def get_role_for_user(username: str | None) -> str | None:
    if not username:
        return None
    username = username.strip()
    if username == ADMIN_USERNAME:
        return "admin"
    if username == REVIEWER_USERNAME:
        return "reviewer"
    return None


def _get_theme_settings():
    theme_name = st.session_state.get("app_theme", "Slate Enterprise")
    if theme_name == "Corporate Light":
        return {
            "bg": "#F8FAFC",
            "panel": "#FFFFFF",
            "card": "#FFFFFF",
            "card_border": "#E2E8F0",
            "muted": "#64748B",
            "text": "#0F172A",
            "heading": "#0F172A",
            "accent": "#2563EB",
            "green": "#059669",
            "green_bg": "#ECFDF5",
            "red": "#DC2626",
            "red_bg": "#FEF2F2",
            "amber": "#D97706",
            "amber_bg": "#FFFBEB",
        }
    elif theme_name == "Custom":
        bg = st.session_state.get("theme_bg", "#0B0F17")
        panel = st.session_state.get("theme_panel", "#111827")
        card = st.session_state.get("theme_card", "#1F2937")
        text = st.session_state.get("theme_text", "#F9FAFB")
        accent = st.session_state.get("theme_accent", "#3B82F6")
        return {
            "bg": bg,
            "panel": panel,
            "card": card,
            "card_border": "rgba(255,255,255,0.08)",
            "muted": "#94A3B8",
            "text": text,
            "heading": "#FFFFFF",
            "accent": accent,
            "green": "#10B981",
            "green_bg": "rgba(16, 185, 129, 0.12)",
            "red": "#EF4444",
            "red_bg": "rgba(239, 68, 68, 0.12)",
            "amber": "#F59E0B",
            "amber_bg": "rgba(245, 158, 11, 0.12)",
        }
    else:  # Slate Enterprise (Dark Default)
        return {
            "bg": "#0B0F17",
            "panel": "#111827",
            "card": "#161F30",
            "card_border": "#1F293D",
            "muted": "#94A3B8",
            "text": "#E2E8F0",
            "heading": "#F8FAFC",
            "accent": "#3B82F6",
            "green": "#10B981",
            "green_bg": "rgba(16, 185, 129, 0.12)",
            "red": "#EF4444",
            "red_bg": "rgba(239, 68, 68, 0.12)",
            "amber": "#F59E0B",
            "amber_bg": "rgba(245, 158, 11, 0.12)",
        }


def apply_app_styles():
    t = _get_theme_settings()
    st.markdown(
        f"""
        <style>
            @import url('[https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap](https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap)');

            html, body, [class*="css"] {{
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            }}

            :root {{
                --bg: {t['bg']};
                --panel: {t['panel']};
                --card: {t['card']};
                --card-border: {t['card_border']};
                --muted: {t['muted']};
                --text: {t['text']};
                --heading: {t['heading']};
                --accent: {t['accent']};
                --green: {t['green']};
                --green-bg: {t['green_bg']};
                --red: {t['red']};
                --red-bg: {t['red_bg']};
                --amber: {t['amber']};
                --amber-bg: {t['amber_bg']};
            }}

            .stApp {{
                background-color: var(--bg);
                color: var(--text);
            }}

            /* Hide Streamlit Deploy button and standard header actions */
            .stDeployButton,
            div[data-testid="stToolbarActions"],
            div[data-testid="stStatusWidget"],
            #MainMenu {{
                display: none !important;
                visibility: hidden !important;
            }}

            header[data-testid="stHeader"] {{
                display: none !important;
            }}

            .block-container {{
                padding-top: 2rem !important;
                padding-bottom: 2rem !important;
                max-width: 1240px;
            }}

            /* Sidebar Styling */
            section[data-testid="stSidebar"] {{
                background-color: var(--panel) !important;
                border-right: 1px solid var(--card-border) !important;
            }}
            section[data-testid="stSidebar"] h1, 
            section[data-testid="stSidebar"] h2, 
            section[data-testid="stSidebar"] h3 {{
                color: var(--heading) !important;
                font-weight: 700;
                letter-spacing: -0.02em;
            }}

            /* Enterprise Metric Containers */
            div[data-testid="stMetric"] {{
                background-color: var(--card) !important;
                border: 1px solid var(--card-border) !important;
                border-radius: 8px !important;
                padding: 1.1rem 1.25rem !important;
                box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
            }}
            div[data-testid="stMetric"] label {{
                color: var(--muted) !important;
                font-size: 0.8125rem !important;
                font-weight: 500 !important;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }}
            div[data-testid="stMetric"] div[data-testid="stMetricValue"] {{
                color: var(--heading) !important;
                font-weight: 700 !important;
                font-size: 1.75rem !important;
            }}

            /* Custom Enterprise Cards */
            .ent-card {{
                background-color: var(--card);
                border: 1px solid var(--card-border);
                border-radius: 8px;
                padding: 1.25rem 1.5rem;
                margin-top: 0.75rem;
                margin-bottom: 1.25rem;
                box-shadow: 0 1px 3px rgba(0,0,0,0.06);
            }}

            .ent-header {{
                font-size: 0.75rem;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.08em;
                color: var(--muted);
                margin-bottom: 0.35rem;
            }}

            /* Badges & Status Pills */
            .ent-pill {{
                display: inline-flex;
                align-items: center;
                gap: 0.375rem;
                padding: 0.25rem 0.625rem;
                font-size: 0.75rem;
                font-weight: 600;
                border-radius: 9999px;
                line-height: 1;
            }}
            .ent-pill-green {{
                background-color: var(--green-bg);
                color: var(--green);
                border: 1px solid var(--green);
            }}
            .ent-pill-red {{
                background-color: var(--red-bg);
                color: var(--red);
                border: 1px solid var(--red);
            }}
            .ent-pill-amber {{
                background-color: var(--amber-bg);
                color: var(--amber);
                border: 1px solid var(--amber);
            }}

            /* Tables & DataFrames */
            .stDataFrame, .stTable {{
                border-radius: 8px;
                overflow: hidden;
                border: 1px solid var(--card-border);
            }}

            /* Forms, Inputs, and Select Boxes */
            .stTextInput > div > div > input, 
            .stSelectbox > div > div, 
            .stTextArea textarea {{
                background-color: var(--card) !important;
                color: var(--text) !important;
                border: 1px solid var(--card-border) !important;
                border-radius: 6px !important;
                font-size: 0.875rem !important;
            }}
            .stTextInput > div > div > input:focus,
            .stTextArea textarea:focus {{
                border-color: var(--accent) !important;
                box-shadow: 0 0 0 1px var(--accent) !important;
            }}

            /* Buttons */
            .stButton > button {{
                background-color: var(--accent) !important;
                color: #FFFFFF !important;
                border: 1px solid transparent !important;
                border-radius: 6px !important;
                padding: 0.45rem 1rem !important;
                font-weight: 600 !important;
                font-size: 0.875rem !important;
                box-shadow: 0 1px 2px rgba(0,0,0,0.1);
                transition: all 0.15s ease-in-out;
            }}
            .stButton > button:hover {{
                opacity: 0.92;
                transform: translateY(-1px);
                box-shadow: 0 2px 4px rgba(0,0,0,0.15);
            }}

            /* Headings */
            h1, h2, h3, h4 {{
                color: var(--heading) !important;
                font-weight: 700 !important;
                letter-spacing: -0.02em !important;
            }}
            p, label, span {{
                color: var(--text);
            }}
            .text-muted {{
                color: var(--muted) !important;
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
    if value in {"ALLOW", "APPROVE"}:
        return "ALLOW"
    if value in {"REJECT", "REJECTED"}:
        return "REJECT"
    if value == "ESCALATE":
        return "ESCALATE"
    if value == "REVIEW":
        return "REVIEW"
    return value


def load_csv_decision_rows() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open("r", newline="", encoding="utf-8") as csv_file:
        rows = list(csv.DictReader(csv_file))
    return rows


def get_decision_store_rows() -> list[dict]:
    ensure_decision_store()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM decisions ORDER BY created_at DESC"
        ).fetchall()
        db_rows = [dict(row) for row in rows]
    finally:
        conn.close()

    if db_rows:
        return db_rows
    csv_rows = load_csv_decision_rows()
    if csv_rows:
        return csv_rows
    return []


def compute_dashboard_stats() -> dict:
    rows = get_decision_store_rows()
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

    decision = result.get("decision", "UNKNOWN").upper()
    composite_score = float(result.get("composite_score", 0.0))
    verdicts = result.get("verdicts", [])

    human_summary = build_human_summary(policy_id or "unknown", result)

    pill_class = "ent-pill-green" if decision in {"APPROVE", "CLEAR", "ALLOW"} else ("ent-pill-amber" if decision == "REVIEW" else "ent-pill-red")

    st.markdown(
        f"""
        <div class="ent-card">
            <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 0.75rem;">
                <div>
                    <div class="ent-header">INVESTIGATION OUTCOME</div>
                    <div style="display: flex; align-items: center; gap: 0.75rem;">
                        <h2 style="margin: 0; font-size: 1.5rem;">Policy {policy_id or '—'}</h2>
                        <span class="ent-pill {pill_class}">{decision}</span>
                    </div>
                </div>
                <div style="text-align: right;">
                    <div class="ent-header">COMPOSITE RISK</div>
                    <div style="font-size: 1.5rem; font-weight: 700;">{composite_score:.2f}</div>
                </div>
            </div>
            <div style="font-size: 0.9375rem; line-height: 1.5; color: var(--text);">
                {human_summary}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if verdicts:
        st.markdown("<div class='ent-header'>SPECIALIST AGENT FINDINGS</div>", unsafe_allow_html=True)
        table_rows = [
            {
                "Agent": v.get("agent", "Unknown"),
                "Status": "⚠️ Flagged" if v.get("fraud_flag") else "✓ Clear",
                "Anomaly Type": v.get("fraud_type") or "None",
                "Confidence": f"{float(v.get('confidence', 0.0)):.2f}",
                "Evidence": v.get("evidence", "No evidence provided"),
            }
            for v in verdicts
        ]
        st.dataframe(
            table_rows,
            use_container_width=True,
            hide_index=True,
            column_order=["Agent", "Status", "Anomaly Type", "Confidence", "Evidence"],
        )
    else:
        st.info("No specialist verdicts were returned.")

    if policy_id:
        review_key = f"send_review_{policy_id}"
        col1, col2 = st.columns([1, 2])
        with col1:
            if st.button("📋 Route to Human Review Queue", key=review_key, use_container_width=True):
                try:
                    save_decision_record(policy_id, result, human_summary)
                    add_case_to_review(policy_id, result)
                    st.session_state["last_review_summary"] = human_summary
                    st.toast(f"Policy {policy_id} queued for review", icon="✅")
                    st.success(f"Policy {policy_id} has been transferred to the review queue.")
                except Exception as e:
                    st.error(f"Failed to queue review: {e}")


def dashboard_page():
    st.markdown("<h2>Fraud Analytics & Operations</h2>", unsafe_allow_html=True)
    st.markdown("<div class='text-muted' style='margin-bottom: 1.5rem;'>High-level summary of policy verifications and specialist agent fraud flags.</div>", unsafe_allow_html=True)

    stats = compute_dashboard_stats()

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Cases", stats["total_cases"])
    with col2:
        st.metric("Escalations", stats["escalations"])
    with col3:
        st.metric("Pending Review", stats["reviews"])
    with col4:
        st.metric("Approved", stats["approvals"])

    st.caption(f"Portfolio Average Composite Score: {stats['avg_score']:.2f}")

    tab1, tab2, tab3 = st.tabs(["Recent Activity", "Review Backlog", "Resolved Audit Log"])

    with tab1:
        store_rows = get_decision_store_rows()
        if store_rows:
            recent_rows = []
            for item in store_rows[:15]:
                ai_decision = item.get("decision", "UNKNOWN")
                human_decision = item.get("human_decision") or "—"
                review_status = item.get("review_status", "pending")
                final_decision = human_decision if human_decision != "—" else ai_decision

                recent_rows.append(
                    {
                        "Policy ID": item.get("policy_id", "--"),
                        "AI Decision": ai_decision,
                        "Final Outcome": final_decision,
                        "Composite Risk": f"{float(item.get('composite_score', 0.0)):.2f}",
                        "Workflow Status": "Resolved" if review_status == "resolved" else "Pending Review",
                    }
                )
            st.dataframe(recent_rows, use_container_width=True, hide_index=True)
        else:
            st.info("No investigation activity recorded yet. Run a policy evaluation to populate metrics.")

    with tab2:
        store_rows = get_decision_store_rows()
        pending_rows = [item for item in store_rows if item.get("review_status") == "pending"]

        if pending_rows:
            pending_table = []
            for item in pending_rows:
                pending_table.append(
                    {
                        "Policy ID": item.get("policy_id", "--"),
                        "AI Determination": item.get("decision", "UNKNOWN"),
                        "Risk Score": f"{float(item.get('composite_score', 0.0)):.2f}",
                        "Summary": item.get("summary", "No summary")[:90] + "..." if item.get("summary") else "—",
                    }
                )
            st.dataframe(pending_table, use_container_width=True, hide_index=True)
        else:
            st.info("The review queue is empty. All policies have been evaluated.")

    with tab3:
        store_rows = get_decision_store_rows()
        resolved_rows = [item for item in store_rows if item.get("review_status") == "resolved"]

        if resolved_rows:
            resolved_table = []
            for item in resolved_rows:
                resolved_table.append(
                    {
                        "Policy ID": item.get("policy_id", "--"),
                        "AI Verdict": item.get("decision", "UNKNOWN"),
                        "Sign-off Decision": item.get("human_decision", "—"),
                        "Auditor Notes": (item.get("reviewer_notes") or "—")[:90],
                        "Risk Score": f"{float(item.get('composite_score', 0.0)):.2f}",
                    }
                )
            st.dataframe(resolved_table, use_container_width=True, hide_index=True)
        else:
            st.info("No resolved determinations present in the audit log.")

    if stats.get("agent_counts"):
        st.markdown("<br><div class='ent-header'>SPECIALIST AGENT DETECTION FREQUENCY</div>", unsafe_allow_html=True)
        agent_df = [{"Specialist Unit": k, "Detections Triggered": v} for k, v in stats["agent_counts"].items()]
        st.dataframe(agent_df, use_container_width=True, hide_index=True)


def ensure_review_queue(force: bool = False):
    if "human_review_queue" not in st.session_state or st.session_state["human_review_queue"] is None:
        st.session_state["human_review_queue"] = []

    if force or True:
        queued = []
        ensure_decision_store()
        conn = sqlite3.connect(str(DB_PATH))
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT policy_id, decision, composite_score, flagged_by, verdicts_json, summary,
                       human_decision, reviewer_notes, review_status
                FROM decisions
                WHERE review_status IS NULL OR review_status = 'pending'
                ORDER BY created_at DESC
                """
            ).fetchall()
        finally:
            conn.close()

        for row in rows:
            try:
                verdicts = json.loads(row["verdicts_json"] or "[]")
            except Exception:
                verdicts = []
            try:
                flagged_by = json.loads(row["flagged_by"] or "[]")
            except Exception:
                flagged_by = []
            queued.append(
                {
                    "policy_id": row["policy_id"],
                    "decision": row["decision"],
                    "composite_score": float(row["composite_score"] or 0.0),
                    "flagged_by": flagged_by,
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
                if row.get("review_status") in (None, "", "pending"):
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
    ensure_review_queue(force=True)
    queue = st.session_state["human_review_queue"]
    if not any(item["policy_id"] == policy_id for item in queue):
        pass
    ensure_review_queue(force=True)


def human_review_page():
    if not st.session_state.get("authenticated", False):
        st.warning("Please sign in to access the Human Review workspace.")
        login_page()
        return

    role = st.session_state.get("role")
    if role != "reviewer":
        st.warning("Access restricted: Reviewer authorization required.")
        st.session_state["authenticated"] = False
        st.session_state.pop("username", None)
        st.session_state.pop("role", None)
        login_page()
        return

    ensure_review_queue(force=True)
    st.markdown("<h2>Human-in-the-Loop Review Station</h2>", unsafe_allow_html=True)
    st.markdown("<div class='text-muted' style='margin-bottom: 1.5rem;'>Examine automated multi-agent determinations and submit binding human approvals or rejections.</div>", unsafe_allow_html=True)

    queue = st.session_state["human_review_queue"]

    col1, col2 = st.columns([1, 3])
    with col1:
        st.metric("Pending Adjudications", len(queue))
    with col2:
        if len(queue) > 0:
            st.info(f"⚡ {len(queue)} policy case(s) waiting for operational decision.")
        else:
            st.success("All assigned cases have been processed.")

    with st.expander("System Audit & Storage Info", expanded=False):
        st.caption(f"Store Location: `{DB_PATH}`")
        try:
            conn = sqlite3.connect(str(DB_PATH))
            try:
                total_records = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
                pending_records = conn.execute("SELECT COUNT(*) FROM decisions WHERE review_status='pending'").fetchone()[0]
                resolved_records = conn.execute("SELECT COUNT(*) FROM decisions WHERE review_status='resolved'").fetchone()[0]
            finally:
                conn.close()

            st.write(f"**Total Records:** {total_records} | **Pending:** {pending_records} | **Resolved:** {resolved_records}")
        except Exception as e:
            st.error(f"Error reading database: {str(e)}")

    if not queue:
        st.info("No policy records currently requiring human intervention.")
        return

    selected_policy = st.selectbox("Select Target Policy", [item["policy_id"] for item in queue])
    case = next(item for item in queue if item["policy_id"] == selected_policy)

    dec_val = case['decision'].upper()
    pill_class = "ent-pill-green" if dec_val in {"APPROVE", "CLEAR", "ALLOW"} else ("ent-pill-amber" if dec_val == "REVIEW" else "ent-pill-red")

    st.markdown(
        f"""
        <div class="ent-card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
                <span class="ent-header">CASE OVERVIEW &bull; {case['policy_id']}</span>
                <span class="ent-pill {pill_class}">{dec_val}</span>
            </div>
            <div style="font-size: 0.9375rem; line-height: 1.5;">
                {case.get('summary', 'No overview generated.')}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    policy_data = data.get_policy_record(selected_policy)
    if policy_data:
        with st.expander("Policy Metadata Details", expanded=True):
            col1, col2 = st.columns(2)
            with col1:
                st.write(f"**Policy ID:** {policy_data.get('policy_id', 'N/A')}")
                st.write(f"**Insured Entity:** {policy_data.get('insured_name', 'N/A')}")
                st.write(f"**Effective Date:** {policy_data.get('issue_date', 'N/A')}")
                st.write(f"**Total Premium:** {policy_data.get('premium', 'N/A')}")
            with col2:
                st.write(f"**Policy Status:** {policy_data.get('policy_status', 'N/A')}")
                st.write(f"**Underwriting Channel:** {policy_data.get('channel', 'N/A')}")
                st.write(f"**State / Region:** {policy_data.get('state', 'N/A')}")
                st.write(f"**Agent Reference:** {policy_data.get('agent_id', 'N/A')}")

    payment_records = data.get_payment_records(selected_policy)
    if payment_records:
        with st.expander("Transaction & Premium Ledgers", expanded=False):
            payment_table = []
            for payment in payment_records:
                payment_table.append({
                    "Payment Ref": payment.get("payment_id", "N/A"),
                    "Amount": payment.get("amount", 0),
                    "Clearing Status": payment.get("payment_status", "N/A"),
                    "Remitted to Insurer": "Yes" if payment.get("premium_remitted_to_insurer") else "No",
                    "Delay (Days)": payment.get("remittance_delay_days", 0),
                })
            st.dataframe(payment_table, use_container_width=True, hide_index=True)

    claim_records = data.get_claim_records(selected_policy)
    if claim_records:
        with st.expander("Claims History & Adjudication", expanded=False):
            claim_table = []
            for claim in claim_records:
                claim_table.append({
                    "Claim Ref": claim.get("claim_id", "N/A"),
                    "Claimed Value": claim.get("claim_amount", 0),
                    "Approved Amount": claim.get("approved_amount", 0),
                    "Status": claim.get("claim_status", "N/A"),
                    "Policy-to-Incident (Days)": claim.get("days_policy_to_incident", 0),
                })
            st.dataframe(claim_table, use_container_width=True, hide_index=True)

    agent_id = data.get_agent_id_for_policy(selected_policy)
    if agent_id:
        broker_records = data.get_ghost_broking_records(agent_id)
        if broker_records:
            with st.expander("Broker Integrity & Regulatory Clearance", expanded=False):
                broker_table = []
                for broker in broker_records:
                    broker_table.append({
                        "Agent Ref": broker.get("agent_id", "N/A"),
                        "License Health": broker.get("license_status", "N/A"),
                        "Complaints Recorded": broker.get("complaint_count", 0),
                        "Premium Deposited": "Yes" if broker.get("premium_deposited_with_insurer") else "No",
                        "Customer Aware": "Yes" if broker.get("customer_aware_of_agent_status") else "No",
                    })
                st.dataframe(broker_table, use_container_width=True, hide_index=True)

    if case.get("verdicts"):
        st.markdown("<br><div class='ent-header'>SPECIALIST AGENT FINDINGS</div>", unsafe_allow_html=True)
        st.dataframe(
            [
                {
                    "Agent": v.get("agent", "Unknown"),
                    "Status": "⚠️ Flagged" if v.get("fraud_flag") else "✓ Clear",
                    "Pattern Identified": v.get("fraud_type") or "None",
                    "Confidence": f"{float(v.get('confidence', 0.0)):.2f}",
                    "Audit Evidence": v.get("evidence", "No evidence provided"),
                }
                for v in case["verdicts"]
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("<hr style='border: none; border-top: 1px solid var(--card-border); margin: 2rem 0 1rem 0;'>", unsafe_allow_html=True)
    st.markdown("<h3>Adjudication Verdict</h3>", unsafe_allow_html=True)
    decision = st.radio(
        "Final Decision",
        ["ALLOW", "REJECT", "REVIEW", "ESCALATE"],
        index=0,
        horizontal=True,
        format_func=lambda value: {
            "ALLOW": "Approve Policy",
            "REJECT": "Reject & Flag",
            "REVIEW": "Escalate to Field",
            "ESCALATE": "Refer to SIU",
        }.get(value, value),
    )
    notes = st.text_area("Audit Justification / Notes", value=case.get("notes", ""), placeholder="Enter operational justification for this decision...", height=100)

    if st.button("Commit Final Determination", use_container_width=True):
        update_decision_record(selected_policy, decision, notes)
        for item in queue:
            if item["policy_id"] == selected_policy:
                item["human_decision"] = decision
                item["notes"] = notes
                break
        st.session_state["human_review_queue"] = [item for item in queue if item["policy_id"] != selected_policy]
        st.success(f"Determination committed for policy {selected_policy}: {decision}")
        st.rerun()


def login_page():
    st.markdown(
        """
        <div style="text-align: center; margin-top: 2.5rem; margin-bottom: 2rem;">
            <h1 style="font-size: 2.25rem; font-weight: 800; letter-spacing: -0.03em; margin-bottom: 0.25rem;">FraudNet MAS</h1>
            <p class="text-muted" style="font-size: 0.9375rem; margin-top: 0;">Enterprise Multi-Agent Insurance Fraud Detection Platform</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns([1, 1.3, 1])
    with col2:
        with st.container(border=True):
            st.markdown("<div class='ent-header' style='margin-bottom: 1rem;'>SECURE SIGN IN</div>", unsafe_allow_html=True)
            username = st.text_input("Username", placeholder="admin or reviewer")
            password = st.text_input("Password", type="password", placeholder="••••••••")

            st.markdown("<div style='margin-top: 0.5rem;'></div>", unsafe_allow_html=True)
            if st.button("Sign In", use_container_width=True):
                if is_valid_login(username, password):
                    username = username.strip()
                    st.session_state["authenticated"] = True
                    st.session_state["username"] = username
                    st.session_state["role"] = get_role_for_user(username)
                    st.rerun()
                else:
                    st.session_state["authenticated"] = False
                    st.session_state.pop("username", None)
                    st.session_state.pop("role", None)
                    st.error("Invalid credentials. Demo accounts: admin/admin, reviewer/reviewer")


def policy_search_page():
    st.markdown("<h2>Policy Fraud Investigation</h2>", unsafe_allow_html=True)
    st.markdown("<div class='text-muted' style='margin-bottom: 1.5rem;'>Trigger concurrent verification across policy, payment, ghost-broking, and claims detection agents.</div>", unsafe_allow_html=True)

    col1, col2 = st.columns([3, 1])
    with col1:
        policy_id = st.text_input("Target Policy Identifier", value="POL000914", label_visibility="collapsed")
    with col2:
        run_btn = st.button("Run Inspection", use_container_width=True)

    if run_btn and policy_id.strip():
        pid = policy_id.strip()
        with st.spinner(f"Orchestrating specialist agents for policy {pid}..."):
            result = asyncio.run(investigate(pid))

        st.session_state["current_investigation"] = {
            "policy_id": pid,
            "result": result,
        }

        history = _get_history()
        history.insert(
            0,
            {
                "policy_id": pid,
                "decision": result.get("decision", "UNKNOWN"),
                "composite_score": result.get("composite_score", 0.0),
                "flagged_by": result.get("flagged_by", []),
                "verdicts": result.get("verdicts", []),
            },
        )
        if len(history) > 10:
            history.pop()

    current = st.session_state.get("current_investigation")
    if current:
        render_result(current["result"], current["policy_id"])
    else:
        st.info("Specify a policy ID and select 'Run Inspection' to execute multi-agent analysis.")


def streamlit_app():
    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False
    if "username" not in st.session_state:
        st.session_state["username"] = ""
    if "role" not in st.session_state:
        st.session_state["role"] = None

    if "investigation_history" not in st.session_state:
        st.session_state["investigation_history"] = []

    if st.session_state.get("authenticated"):
        current_user = str(st.session_state.get("username", "")).strip()
        expected_role = get_role_for_user(current_user)
        if expected_role is None:
            st.session_state["authenticated"] = False
            st.session_state.pop("username", None)
            st.session_state.pop("role", None)
        else:
            st.session_state["role"] = expected_role

    if "app_theme" not in st.session_state:
        st.session_state["app_theme"] = "Slate Enterprise"

    apply_app_styles()

    if not st.session_state["authenticated"]:
        login_page()
        return

    # Sidebar setup
    st.sidebar.markdown(
        """
        <div style="padding-bottom: 1rem; border-bottom: 1px solid rgba(255,255,255,0.08); margin-bottom: 1rem;">
            <div style="font-size: 1.125rem; font-weight: 800; letter-spacing: -0.02em;">FraudNet MAS</div>
            <div style="font-size: 0.75rem; color: #94A3B8; text-transform: uppercase; letter-spacing: 0.06em;">Enterprise Edition</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.session_state["app_theme"] = st.sidebar.selectbox(
        "Theme Palette",
        ["Slate Enterprise", "Corporate Light", "Custom"],
        index=["Slate Enterprise", "Corporate Light", "Custom"].index(st.session_state.get("app_theme", "Slate Enterprise")),
    )
    if st.session_state["app_theme"] == "Custom":
        st.session_state["theme_bg"] = st.sidebar.color_picker("Background", st.session_state.get("theme_bg", "#0B0F17"))
        st.session_state["theme_panel"] = st.sidebar.color_picker("Sidebar", st.session_state.get("theme_panel", "#111827"))
        st.session_state["theme_card"] = st.sidebar.color_picker("Cards", st.session_state.get("theme_card", "#1F2937"))
        st.session_state["theme_text"] = st.sidebar.color_picker("Text", st.session_state.get("theme_text", "#F9FAFB"))
        st.session_state["theme_accent"] = st.sidebar.color_picker("Accent", st.session_state.get("theme_accent", "#3B82F6"))
    apply_app_styles()

    role = st.session_state.get("role")
    st.sidebar.markdown(f"<div style='font-size: 0.8125rem; margin-top: 1rem;'><span style='color: var(--muted);'>User:</span> <strong>{st.session_state.get('username')}</strong> ({role})</div>", unsafe_allow_html=True)

    if st.sidebar.button("Sign Out"):
        st.session_state["authenticated"] = False
        st.session_state.pop("username", None)
        st.session_state.pop("role", None)
        st.session_state.pop("current_investigation", None)
        st.rerun()

    st.sidebar.markdown("<hr style='border: none; border-top: 1px solid var(--card-border); margin: 1rem 0;'>", unsafe_allow_html=True)

    if role == "admin":
        page = st.sidebar.radio("Navigation", ["Dashboard", "Policy Investigation"], index=1)
        if page == "Dashboard":
            dashboard_page()
        else:
            policy_search_page()
        return

    if role == "reviewer":
        ensure_review_queue(force=True)
        human_review_page()
        return

    st.session_state["authenticated"] = False
    st.session_state.pop("username", None)
    st.session_state.pop("role", None)
    login_page()


if __name__ == "__main__":
    streamlit_app()