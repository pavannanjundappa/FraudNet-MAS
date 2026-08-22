#!/usr/bin/env python3
"""Test database operations directly"""
import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "fraud_decisions.db"

# Test data
test_policy_id = "TEST_POL_001"
test_result = {
    "decision": "REVIEW",
    "composite_score": 65.5,
    "flagged_by": ["Policy Agent", "Claims Agent"],
    "verdicts": [
        {"agent": "Policy Agent", "verdict": "SUSPICIOUS"},
        {"agent": "Claims Agent", "verdict": "NORMAL"}
    ]
}
test_summary = "Test case for verification"

print("=" * 60)
print("DATABASE SAVE TEST")
print("=" * 60)

try:
    # Ensure database is created
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("""
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
        """)
        conn.commit()
    print("✅ Database table created/verified")

    # Test INSERT
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT INTO decisions (policy_id, decision, composite_score, flagged_by, verdicts_json, summary, review_status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                test_policy_id,
                test_result.get("decision"),
                test_result.get("composite_score"),
                json.dumps(test_result.get("flagged_by", [])),
                json.dumps(test_result.get("verdicts", [])),
                test_summary
            )
        )
        conn.commit()
    print("✅ Record inserted successfully")

    # Verify the data was saved
    with sqlite3.connect(str(DB_PATH)) as conn:
        cursor = conn.execute("SELECT * FROM decisions WHERE policy_id = ?", (test_policy_id,))
        row = cursor.fetchone()
        if row:
            print("✅ Record verified in database")
            print(f"   Policy ID: {row[1]}")
            print(f"   Decision: {row[2]}")
            print(f"   Score: {row[3]}")
            print(f"   Status: {row[9]}")
        else:
            print("❌ Record NOT found in database after insert")

    # Check total records
    with sqlite3.connect(str(DB_PATH)) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM decisions")
        count = cursor.fetchone()[0]
    print(f"✅ Total records in database: {count}")

    print("\n✅ Database test PASSED!")

except Exception as e:
    print(f"❌ ERROR: {str(e)}")
    import traceback
    traceback.print_exc()

print("=" * 60)
