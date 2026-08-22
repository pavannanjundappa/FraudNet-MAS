"""Test inserting and retrieving a record from fraud_decisions.db"""
import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "fraud_decisions.db"

# Create table
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
    print("✅ Table created/verified")

# Insert a test record
with sqlite3.connect(DB_PATH) as conn:
    conn.execute(
        """
        INSERT INTO decisions (
            policy_id, decision, composite_score, flagged_by, verdicts_json,
            summary, human_decision, reviewer_notes, review_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "TEST001",
            "ESCALATE",
            0.85,
            json.dumps(["policy-agent", "claims-agent"]),
            json.dumps([
                {"agent": "policy-agent", "fraud_flag": True, "confidence": 0.9},
                {"agent": "claims-agent", "fraud_flag": True, "confidence": 0.8}
            ]),
            "Test summary for manual insertion",
            None,
            "",
            "pending"
        )
    )
    conn.commit()
    print("✅ Test record inserted")

# Query the record
with sqlite3.connect(DB_PATH) as conn:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM decisions WHERE review_status = 'pending' ORDER BY created_at DESC"
    ).fetchall()
    
    print(f"\n✅ Found {len(rows)} pending records:")
    for row in rows:
        print(f"  - Policy: {row['policy_id']}, Decision: {row['decision']}, Status: {row['review_status']}")
