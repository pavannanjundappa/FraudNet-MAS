# FraudMesh — Multi-Agent Insurance Fraud Detection

Built on Microsoft Agent Framework (`FoundryChatClient` + `ConcurrentBuilder`).
Four specialist agents (Policy, Payments, Broker Integrity, Claims) run in
parallel against the same case, and a custom aggregator (`fuse_verdicts` in
`app.py`) combines their verdicts into a single decision.

## Files
- `data.py` — loads the 4 CSVs from `./data`, exposes lookup functions
- `tools.py` — `@tool`-wrapped versions of those lookups the agents can call
- `app.py` — builds the 4 agents, the concurrent workflow, fusion logic, and
  a CLI entry point (`python app.py <policy_id>`)
- `streamlit_app.py` — browser demo UI
- `evaluate.py` — runs the full 1000-policy dataset through the system and
  reports precision/recall/F1 against ground truth, plus a single-agent
  baseline for comparison
- `.env.example` — copy to `.env` and fill in your Foundry project endpoint

## Setup
```bash
pip install -r requirements.txt
az login                          # AzureCliCredential needs an active session
cp .env.example .env              # then edit .env with your project details
```

## Run
```bash
python app.py POL000914           # single case, CLI output
streamlit run streamlit_app.py    # browser demo
python evaluate.py --limit 50     # quick evaluation sample
python evaluate.py                # full 1000-policy evaluation (slower, costs tokens)
```

## Fusion rule (app.py: fuse_verdicts)
- `composite_score` = average confidence across agents that flagged fraud
- `>= 2 agents` flag with confidence `>= 0.7` → **ESCALATE**
- exactly `1 agent` flags with confidence `>= 0.8` → **REVIEW**
- otherwise → **APPROVE**

This threshold is deliberately explicit rather than left to an LLM to decide,
so it's defensible and tunable — adjust the constants in `fuse_verdicts` and
re-run `evaluate.py` to see the precision/recall tradeoff.

## Known test case
`POL000914` is a real, fully cross-linked fraud case in the dataset (agent
`AGT00077`): flagged for Fronting in policies, Premium Diversion + Bounced
Cheque in payments, Expired/Suspended License in ghost broking, and Multiple
Claims in claims. A correct run should land on **ESCALATE** with 3-4 agents
flagging it — use this to sanity-check your Foundry connection before running
the full evaluation.
