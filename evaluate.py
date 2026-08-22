"""
evaluate.py — runs the fused multi-agent system (and, for comparison, the
policy agent alone) against every policy_id in the dataset, scores against
ground-truth fraud_flag, and prints precision/recall/F1/confusion matrix for
both. This is the ablation your thesis results chapter needs.

Usage:
    python evaluate.py                 # full 1000-policy run
    python evaluate.py --limit 100     # quick sample run
"""
import argparse
import asyncio
import json

from sklearn.metrics import classification_report, confusion_matrix

import data
from app import build_chat_client, build_specialist_agents


async def run_single_agent_only(policy_id: str, policy_agent) -> bool:
    """Baseline: policy agent's verdict alone, no fusion."""
    response = await policy_agent.run(f"Investigate policy_id: {policy_id}")
    text = response.text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
        return bool(parsed.get("fraud_flag", False))
    except (json.JSONDecodeError, TypeError, ValueError):
        return False


async def run_fused(policy_id: str, workflow) -> str:
    from app import investigate  # local import to avoid building two workflows
    result = await investigate(policy_id)
    return result["decision"]


async def main(limit: int | None):
    policy_ids = list(data.all_policy_ids())
    if limit:
        policy_ids = policy_ids[:limit]

    chat_client = build_chat_client()
    policy_agent, _, _, _ = build_specialist_agents(chat_client)

    y_true = []
    y_pred_fused = []
    y_pred_single = []

    for i, pid in enumerate(policy_ids, 1):
        truth = data.get_ground_truth_fraud_flag(pid)
        y_true.append(truth)

        from app import investigate
        fused_result = await investigate(pid)
        y_pred_fused.append(fused_result["decision"] in ("REVIEW", "ESCALATE"))

        single_flag = await run_single_agent_only(pid, policy_agent)
        y_pred_single.append(single_flag)

        if i % 25 == 0:
            print(f"  ...evaluated {i}/{len(policy_ids)}")

    print("\n" + "=" * 60)
    print("MULTI-AGENT FUSION (ConcurrentBuilder + fuse_verdicts)")
    print("=" * 60)
    print(classification_report(y_true, y_pred_fused, target_names=["clean", "fraud"]))
    print("Confusion matrix [rows=true, cols=pred] (order: clean, fraud):")
    print(confusion_matrix(y_true, y_pred_fused))

    print("\n" + "=" * 60)
    print("SINGLE-AGENT BASELINE (policy agent only, no fusion)")
    print("=" * 60)
    print(classification_report(y_true, y_pred_single, target_names=["clean", "fraud"]))
    print("Confusion matrix [rows=true, cols=pred] (order: clean, fraud):")
    print(confusion_matrix(y_true, y_pred_single))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Evaluate only the first N policies")
    args = parser.parse_args()
    asyncio.run(main(args.limit))
