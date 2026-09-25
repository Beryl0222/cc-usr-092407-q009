"""culture_finance_progress 领域资料的基础结构。"""

from __future__ import annotations

EVENT_KINDS = [
    # 基线事件
    'APPLICATION_SUBMITTED',
    'OVERLAP_FLAGGED',
    'MILESTONE_ACCEPTED',
    'TRANCHE_RELEASED',
    'RECOVERY_RECONCILED',
    # 申请与审批
    'APPLICATION_APPROVED',
    # 证据指纹与重叠判定
    'EVIDENCE_REGISTERED',
    'OVERLAP_ADJUDICATED',
    # 里程碑
    'MILESTONE_DEFINED',
    # 追加事实（修正余额，不覆盖原结论）
    'MATCHING_FUNDS_RECORDED',
    'RESTRUCTURING_RECORDED',
    # 资金动作检查点
    'PAYMENT_STEP',
    # 银行回执
    'BANK_RECEIPT_RECORDED',
    # 批量材料隔离
    'BATCH_ITEM_QUARANTINED',
]
REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")

def validate_event(record: dict) -> list[str]:
    """检查样例事件是否具备可交换的最小字段。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    if record.get("kind") not in EVENT_KINDS:
        problems.append("kind")
    return problems
