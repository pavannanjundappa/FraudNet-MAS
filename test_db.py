"""Quick test to verify database operations"""
import sqlite3
from pathlib import Path
import sys

# Add the project to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

DB_PATH = Path(__file__).resolve().parent / "fraud_decisions.db"

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

def test_database():
    # Check if DB exists
    print(f"Database path: {DB_PATH}")
    print(f"Database exists: {DB_PATH.exists()}")
    
    # Ensure table exists
    ensure_decision_store()
    print("✅ Table initialized")
    
    # Try to connect and check table schema
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='decisions'"
            )
            table_exists = cursor.fetchone() is not None
            print(f"✅ Table 'decisions' exists: {table_exists}")
            
            if table_exists:
                # Get table schema
                cursor = conn.execute("PRAGMA table_info(decisions)")
                columns = cursor.fetchall()
                print("\nTable columns:")
                for col in columns:
                    print(f"  - {col[1]} ({col[2]})")
                
                # Count records
                cursor = conn.execute("SELECT COUNT(*) FROM decisions")
                count = cursor.fetchone()[0]
                print(f"\n✅ Total records in database: {count}")
                
                # List pending records
                cursor = conn.execute(
                    "SELECT policy_id, decision, review_status FROM decisions WHERE review_status = 'pending' ORDER BY created_at DESC"
                )
                pending = cursor.fetchall()
                print(f"✅ Pending records: {len(pending)}")
                if pending:
                    for row in pending:
                        print(f"  - Policy: {row[0]}, Decision: {row[1]}, Status: {row[2]}")
                else:
                    print("  (No pending records)")
    except Exception as e:
        import traceback
        print(f"❌ Error: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    test_database()
