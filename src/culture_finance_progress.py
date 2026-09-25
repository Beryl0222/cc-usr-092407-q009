"""culture_finance_progress 领域资料的基础结构。

事件是拨付链上只追加（append-only）的事实。任何余额变化都通过追加新事件
重算得到，禁止修改或覆盖既有事件——这是"可穿透"的前提：从任意一笔付款
都能沿事件流回溯到预算来源、证据指纹、重叠裁决与历次调整。
"""

from __future__ import annotations

# 既有进度事件
EVENT_KINDS = [
    # —— 申请与承诺 ——
    'APPLICATION_SUBMITTED',      # 项目申请：预算、用途限制、关联主体、其他资金承诺
    'FUNDING_COMMITMENT_ADDED',   # 追加/澄清一笔其他资金承诺（银行贷款、配套款等）
    # —— 证据与重叠 ——
    'EVIDENCE_REGISTERED',        # 合同/发票/交付物登记，生成稳定指纹
    'OVERLAP_FLAGGED',            # 同一指纹跨申请出现，系统仅提示，不作结论
    'OVERLAP_DECIDED',            # 授权人员对重叠作出裁决（允许/拒绝）并留依据
    # —— 里程碑：固定条款 ——
    'MILESTONE_DEFINED',          # 固定适用协议、验收人、释放比例、放款前置条件
    'MILESTONE_APPROVED',         # 审批人批准里程碑可进入验收（与验收/付款角色不同）
    'MILESTONE_ACCEPTED',         # 验收（可为部分验收，记录验收比例）
    'MATCHING_FUNDS_RECEIVED',    # 企业配套款到账（迟到也只是追加事实）
    'ENTITY_RESTRUCTURED',        # 企业重组等主体变化，作为追加事实
    # —— 资金动作（带检查点） ——
    'QUOTA_RESERVED',             # 付款前先预留额度，形成占用
    'TRANCHE_RELEASED',           # 满足条件且额度未被占用，实际拨付
    'BANK_RECEIPT_RECORDED',      # 银行回执登记；重放不会再次拨付（回执号幂等）
    'RECOVERY_RECONCILED',        # 追回，以追加事实修正余额，不覆盖原拨付结论
]

REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")


def validate_event(record: dict) -> list[str]:
    """检查样例事件是否具备可交换的最小字段。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    if record.get("kind") not in EVENT_KINDS:
        problems.append("kind")
    return problems
