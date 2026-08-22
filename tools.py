"""
tools.py — @tool-wrapped functions each specialist agent can call to ground
its verdict in the actual dataset rows, instead of guessing.
"""
from __future__ import annotations

import json
from typing import Annotated

from agent_framework import tool
from pydantic import Field

import data


@tool
def lookup_policy(
    policy_id: Annotated[str, Field(description="The policy_id to look up, e.g. POL000914")]
) -> str:
    """Look up a policy record (type, premium, status, free-look/backdating
    flags) from the policies dataset by policy_id. Returns JSON."""
    record = data.get_policy_record(policy_id)
    if record is None:
        return json.dumps({"error": f"No policy found for policy_id={policy_id}"})
    return json.dumps(record, default=str)


@tool
def lookup_payments(
    policy_id: Annotated[str, Field(description="The policy_id whose payment history to look up")]
) -> str:
    """Look up all payment records (mode, status, remittance behaviour) for a
    given policy_id from the payments dataset. Returns a JSON list."""
    records = data.get_payment_records(policy_id)
    if not records:
        return json.dumps({"error": f"No payments found for policy_id={policy_id}"})
    return json.dumps(records, default=str)


@tool
def lookup_ghost_broking(
    agent_id: Annotated[str, Field(description="The agent_id whose license/broker record to look up")]
) -> str:
    """Look up license status, complaint history, and premium-remittance
    behaviour for an agent_id from the ghost broking dataset. Returns a JSON list."""
    records = data.get_ghost_broking_records(agent_id)
    if not records:
        return json.dumps({"error": f"No ghost-broking records found for agent_id={agent_id}"})
    return json.dumps(records, default=str)


@tool
def lookup_claims(
    policy_id: Annotated[str, Field(description="The policy_id whose claim history to look up")]
) -> str:
    """Look up all claim records (amounts, timing, approval status) for a
    given policy_id from the claims dataset. Returns a JSON list."""
    records = data.get_claim_records(policy_id)
    if not records:
        return json.dumps({"error": f"No claims found for policy_id={policy_id}"})
    return json.dumps(records, default=str)


@tool
def lookup_agent_for_policy(
    policy_id: Annotated[str, Field(description="The policy_id to resolve to its selling agent_id")]
) -> str:
    """Resolve a policy_id to the agent_id who sold it. Useful for the broker
    integrity agent, which keys off agent_id rather than policy_id."""
    agent_id = data.get_agent_id_for_policy(policy_id)
    if agent_id is None:
        return json.dumps({"error": f"No agent found for policy_id={policy_id}"})
    return json.dumps({"policy_id": policy_id, "agent_id": agent_id})
