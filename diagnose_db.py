#!/usr/bin/env python3
"""Diagnostic script to verify fraud_decisions.db and data storage"""
import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "fraud_decisions.db"

print("=" * 70)
print("🔍 FRAUDNET DATABASE DIAGNOSTICS")
print("=" * 70)

# 1. Check if database file exists
print(f"\n1. Database File Check:")
print(f"   Path: {DB_PATH}")
print(f"   Exists: {'✅ YES' if DB_PATH.exists() else '❌ NO'}")
if DB_PATH.exists():
    size_kb = DB_PATH.stat().st_size / 1024
    print(f"   Size: {size_kb:.2f} KB")

# 2. Try to connect and check table
print(f"\n2. Table Check:")
try:
    with sqlite3.connect(str(DB_PATH)) as conn:
        # Create table if it doesn't exist
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
        
        # Check if table exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='decisions'"
        )
        if cursor.fetchone():
            print("   ✅ 'decisions' table exists")
            
            # Get record count
            cursor = conn.execute("SELECT COUNT(*) FROM decisions")
            count = cursor.fetchone()[0]
            print(f"   Total records: {count}")
            
            # Get pending count
            cursor = conn.execute("SELECT COUNT(*) FROM decisions WHERE review_status='pending'")
            pending = cursor.fetchone()[0]
            print(f"   Pending records: {pending}")
            
            # Get resolved count
            cursor = conn.execute("SELECT COUNT(*) FROM decisions WHERE review_status='resolved'")
            resolved = cursor.fetchone()[0]
            print(f"   Resolved records: {resolved}")
            
            # Show recent records
            if count > 0:
                print(f"\n3. Recent Records (Last 5):")
                cursor = conn.execute(
                    "SELECT id, policy_id, decision, review_status, created_at FROM decisions ORDER BY created_at DESC LIMIT 5"
                )
                for row in cursor.fetchall():
                    print(f"   ID:{row[0]} | Policy:{row[1]} | Decision:{row[2]} | Status:{row[3]} | Time:{row[4]}")
            else:
                print("\n3. Recent Records: NONE - Database is empty")
        else:
            print("   ❌ 'decisions' table NOT found")
except Exception as e:
    print(f"   ❌ ERROR: {str(e)}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 70)
print("✅ Diagnostics complete!")
print("=" * 70)
