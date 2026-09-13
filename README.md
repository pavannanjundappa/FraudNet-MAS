# FraudNet-MAS

FraudNet-MAS is a multi-agent insurance fraud detection application built with Python and Streamlit. It combines specialist policy, payment, broker, and claims checks into a single fraud risk assessment and supports a human-in-the-loop review flow for escalated cases.

## Overview

The project analyzes a policy and runs several specialist agents to gather evidence from related records, then fuses the results into a composite decision. Cases that need manual intervention can be queued for human review, where a reviewer can approve, reject, or escalate the decision.

## Current workflow

1. Admin logs in to the system.
2. Admin investigates a policy using the Policy Investigation screen.
3. The system runs the specialist agents and produces a fraud decision and composite score.
4. If the case needs manual review, the admin clicks the "Send to Human Review" button.
5. The case is written to the SQLite database as a pending review item.
6. Reviewer logs in and sees only the queued cases assigned for human review.
7. Reviewer evaluates the case and submits a final decision.
8. The database record is marked resolved, and the dashboard reflects the final outcome.

## Role-based access

- Admin: dashboard and policy investigation access
- Reviewer: human review access only

Demo credentials:

- Admin: `admin` / `admin`
- Reviewer: `reviewer` / `reviewer`

## Main project files

- `FraudNet-MAS/app.py` — main Streamlit app, multi-agent workflow, authentication, review queue, dashboard
- `FraudNet-MAS/data.py` — policy, payments, broker, and claims data access
- `FraudNet-MAS/tools.py` — lookup helpers used by agents
- `FraudNet-MAS/fraud_decisions.db` — SQLite source of truth for queued and resolved decisions
- `FraudNet-MAS/fraud_decisions.csv` — CSV mirror synced from the database
- `FraudNet-MAS/streamlit_app.py` — minimal demo UI for investigation-only use

## Database behavior

The application stores human-review workflow records in the `decisions` table inside `fraud_decisions.db`.

Fields include:

- `policy_id`
- `decision`
- `composite_score`
- `flagged_by`
- `verdicts_json`
- `summary`
- `human_decision`
- `reviewer_notes`
- `review_status`
- `created_at`

The review queue loads pending records from the database using `review_status = 'pending'`, so the reviewer sees the same records that were queued by the admin.

## Dashboard and review queue

The dashboard shows:

- recent decisions
- pending review cases
- resolved decisions
- aggregate averages and agent-level fraud signal counts

The human review screen displays the selected policy with supporting records and a final decision form for the reviewer.

## Running the app

From the project root:

```bash
cd FraudNet-MAS
python -m streamlit run app.py
```

If using the project virtual environment:

```bash
cd FraudNet-MAS
.venv\Scripts\python.exe -m streamlit run app.py
```

## Notes

This project follows the pattern below:

- AI-generated investigation results feed into a structured review record.
- Human review records are persisted in SQLite rather than only in temporary session state.
- Reviewer decisions update the same source record and are reflected in the dashboard.
- CSV files are treated as a synced mirror of the database, not the primary workflow storage.

