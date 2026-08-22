"""
data.py — loads the four fraud datasets and exposes lookup functions used by
the @tool wrappers in tools.py.
"""
from __future__ import annotations

import os
from functools import lru_cache

import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

_policies: pd.DataFrame | None = None
_payments: pd.DataFrame | None = None
_ghost_broking: pd.DataFrame | None = None
_claims: pd.DataFrame | None = None


def _load() -> None:
    """Lazily load all four CSVs into module-level DataFrames."""
    global _policies, _payments, _ghost_broking, _claims
    if _policies is not None:
        return
    _policies = pd.read_csv(os.path.join(DATA_DIR, "policies_free_insurance.csv"))
    _payments = pd.read_csv(os.path.join(DATA_DIR, "payments_bad_payments.csv"))
    _ghost_broking = pd.read_csv(os.path.join(DATA_DIR, "ghost_broking.csv"))
    _claims = pd.read_csv(os.path.join(DATA_DIR, "claims.csv"))


def get_policy_record(policy_id: str) -> dict | None:
    """Return the policy row for policy_id, or None if not found."""
    _load()
    match = _policies[_policies["policy_id"] == policy_id]
    if match.empty:
        return None
    return match.iloc[0].to_dict()


def get_payment_records(policy_id: str) -> list[dict]:
    """Return all payment rows for policy_id."""
    _load()
    match = _payments[_payments["policy_id"] == policy_id]
    return match.to_dict("records")


def get_ghost_broking_records(agent_id: str) -> list[dict]:
    """Return all ghost-broking records for agent_id."""
    _load()
    match = _ghost_broking[_ghost_broking["agent_id"] == agent_id]
    return match.to_dict("records")


def get_claim_records(policy_id: str) -> list[dict]:
    """Return all claim rows for policy_id."""
    _load()
    match = _claims[_claims["policy_id"] == policy_id]
    return match.to_dict("records")


@lru_cache(maxsize=1)
def all_policy_ids() -> tuple[str, ...]:
    """All policy_ids in the policies dataset — used by evaluate.py."""
    _load()
    return tuple(_policies["policy_id"].tolist())


def get_agent_id_for_policy(policy_id: str) -> str | None:
    """Convenience helper: look up the selling agent_id for a policy."""
    record = get_policy_record(policy_id)
    return record["agent_id"] if record else None


def get_ground_truth_fraud_flag(policy_id: str) -> bool:
    """Ground-truth label for evaluation: True if the policy row itself is
    flagged as fraud in the source dataset."""
    record = get_policy_record(policy_id)
    return bool(record["fraud_flag"]) if record else False


if __name__ == "__main__":
    # quick self-test
    _load()
    print(f"policies={len(_policies)} payments={len(_payments)} "
          f"ghost_broking={len(_ghost_broking)} claims={len(_claims)}")
    test_id = "POL000914"
    print("policy:", get_policy_record(test_id))
    print("payments:", len(get_payment_records(test_id)))
    print("claims:", len(get_claim_records(test_id)))
    agent_id = get_agent_id_for_policy(test_id)
    print("agent_id:", agent_id, "ghost_broking:", len(get_ghost_broking_records(agent_id)))
