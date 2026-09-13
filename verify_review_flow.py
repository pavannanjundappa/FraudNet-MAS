import sqlite3
from pathlib import Path

import app


db = Path(app.DB_PATH)
csv_path = Path(app.CSV_PATH)
if db.exists():
    db.unlink()
if csv_path.exists():
    csv_path.unlink()

app.ensure_decision_store()
policy_id = 'POL-TEST-123'
result = {
    'decision': 'FRAUD',
    'composite_score': 0.91,
    'flagged_by': ['Policy Agent'],
    'verdicts': [{'agent': 'Policy Agent', 'fraud_flag': True, 'fraud_type': 'Free-Look Abuse', 'confidence': 0.9, 'evidence': 'sample'}],
}

app.save_decision_record(policy_id, result, 'Sample summary')
with sqlite3.connect(db) as conn:
    row = conn.execute(
        'SELECT policy_id, review_status, human_decision, reviewer_notes FROM decisions WHERE policy_id=? ORDER BY id DESC LIMIT 1',
        (policy_id,),
    ).fetchone()
    print('DB_ROW', row)

app.ensure_review_queue(force=True)
q = app.st.session_state.get('human_review_queue', [])
print('QUEUE_LEN', len(q))
print('QUEUE_IDS', [x['policy_id'] for x in q])
