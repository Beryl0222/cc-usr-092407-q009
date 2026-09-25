"""审计查询：从某笔付款向前穿透整条拨付链。"""

from __future__ import annotations

from .service import DisbursementChain


def trace_payment(chain: DisbursementChain, payment_id: str) -> dict:
    """从一笔付款向前展示：预算来源、证据指纹、重叠判定、历次调整与余额。"""
    state = chain.payment_state(payment_id)
    context = state["context"]
    application_id = context["application_id"]
    dossier = chain.application_dossier(application_id)
    submitted = dossier["submitted"]
    approval = dossier["approval"]
    balances = chain.balance_of(application_id)
    return {
        "payment_id": payment_id,
        "payment": {
            "status": state["status"],
            "steps": state["steps"],
            "amount": context["amount"],
            "milestone_id": context["milestone_id"],
            "approver": context["approver"],
            "payer": context["payer"],
            "released_event": state["released_event"],
            "receipts": state["receipts"],
        },
        "budget_source": {
            "application_id": application_id,
            "approved_amount": approval["approved_amount"] if approval else None,
            "budget": submitted["budget"],
            "usage_restrictions": submitted["usage_restrictions"],
            "related_entities": submitted["related_entities"],
            "funding_commitments": submitted["funding_commitments"],
        },
        "milestone": chain.milestone_of(context["milestone_id"]),
        "evidence_fingerprints": [
            {
                "evidence_id": item["evidence_id"],
                "evidence_kind": item["evidence_kind"],
                "fingerprint": item["fingerprint"],
            }
            for item in chain.evidence_of(application_id)
        ],
        "overlap_adjudications": chain.overlap_of(application_id),
        "adjustments": chain.adjustments_of(application_id),
        "occupancy": [
            {**item, "amount": str(item["amount"])} for item in chain.occupancy_of(application_id)
        ],
        "balances": {name: str(amount) for name, amount in balances.items()},
    }
