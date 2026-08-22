import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "fraud_decisions.db"

print(f"Database path: {DB_PATH}")
print(f"Exists: {DB_PATH.exists()}")

if DB_PATH.exists():
    try:
        conn = sqlite3.connect(str(DB_PATH))
        print("Connected to database")
        
        # List all tables
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = cursor.fetchall()
        print(f"Tables: {tables}")
        
        # Check decisions table
        try:
            cursor.execute("SELECT COUNT(*) FROM decisions")
            count = cursor.fetchone()[0]
            print(f"Records in decisions table: {count}")
        except:
            print("decisions table not found")
        
        conn.close()
        print("Database check complete")
    except Exception as e:
        print(f"Error: {e}")
