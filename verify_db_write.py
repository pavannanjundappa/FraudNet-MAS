import sqlite3
import os

os.chdir(r'D:\iit_bhalai\fourthsemister\FraudNet-MAS')
import app

print('DB_PATH=', app.DB_PATH)
print('exists_before=', app.DB_PATH.exists())
policy_id = 'TEST-123'
result = {
    'decision': 'REVIEW',
    'composite_score': 0.81,
    'flagged_by': ['policy-agent'],
    'verdicts': [{
        'agent': 'policy-agent',
        'fraud_flag': True,
        'fraud_type': 'Backdated Policy Issuance',
        'confidence': 0.81,
        'evidence': 'policy_inception_date'
    }]
}
app.save_decision_record(policy_id, result, 'test summary')
rows = sqlite3.connect(str(app.DB_PATH)).execute(
    'SELECT policy_id, decision, review_status, summary FROM decisions WHERE policy_id=? ORDER BY id DESC LIMIT 10',
    (policy_id,),
).fetchall()
print('rows=', rows)
print('exists_after=', app.DB_PATH.exists())
