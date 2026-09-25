"""可穿透拨付链的领域服务。

领域规则：
1. 项目申请保存预算、用途限制、关联主体与其他资金承诺；
2. 合同/发票/交付物按内容生成稳定指纹，跨申请复用只触发重叠提示，
   是否重复由具备 ADJUDICATOR 角色的授权人员裁定并留下依据；
3. 里程碑固定适用协议、验收人与释放比例；只有条件满足且额度未被
   占用才可进入付款；审批、验收、付款必须由不同角色承担；
4. 部分验收、企业重组、配套款迟到、追回都以追加事实修正余额，
   不覆盖原结论；
5. 银行回执按回执号幂等，重放不会再次拨付；批量材料中的冲突只
   隔离对应项目；
6. 付款（资金动作）逐步落检查点，执行中断后从最近检查点继续。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .events import EventStore
from .fingerprints import fingerprint_evidence

CENT = Decimal("0.01")

# 角色：审批、验收、付款三者必须分离；重叠裁定需授权人员。
ROLE_APPROVER = "APPROVER"
ROLE_ACCEPTOR = "ACCEPTOR"
ROLE_PAYER = "PAYER"
ROLE_ADJUDICATOR = "ADJUDICATOR"

# 资金动作检查点
STEP_INITIATED = "INITIATED"
STEP_APPROVED = "APPROVED"
STEP_CONDITIONS_VERIFIED = "CONDITIONS_VERIFIED"
STEP_RELEASED = "RELEASED"
STEP_RECEIPT_CONFIRMED = "RECEIPT_CONFIRMED"
STEP_CANCELLED = "CANCELLED"

# 重叠裁定结论
VERDICT_DUPLICATE = "DUPLICATE"
VERDICT_NOT_DUPLICATE = "NOT_DUPLICATE"

STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_COMPLETED = "COMPLETED"
STATUS_CANCELLED = "CANCELLED"

# 以追加事实修正余额的事件种类
_ADJUSTMENT_KINDS = {
    "MILESTONE_ACCEPTED",
    "MATCHING_FUNDS_RECORDED",
    "RESTRUCTURING_RECORDED",
    "RECOVERY_RECONCILED",
}


class DomainError(ValueError):
    """领域规则被拒绝。"""


class UnknownApplicationError(DomainError):
    """申请不存在。"""


class UnknownMilestoneError(DomainError):
    """里程碑不存在。"""


class UnknownPaymentError(DomainError):
    """付款不存在。"""


class UnknownOverlapError(DomainError):
    """重叠提示不存在。"""


class RoleNotAuthorizedError(PermissionError):
    """操作者不具备所需角色。"""


class SeparationOfDutiesError(PermissionError):
    """审批、验收、付款未由不同角色承担。"""


class QuotaUnavailableError(DomainError):
    """可用额度不足：额度被其他付款占用或已拨付。"""


class DuplicateEvidenceVerdictError(DomainError):
    """证据已被授权人员裁定为重复，不得进入付款。"""


class PaymentBlockedError(DomainError):
    """条件暂不满足；补齐追加事实后可从检查点继续。"""

    def __init__(self, payment_id: str, step: str, reason: str) -> None:
        super().__init__(f"付款 {payment_id} 在 {step} 中断：{reason}")
        self.payment_id = payment_id
        self.step = step
        self.reason = reason


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _ratio(value) -> Decimal:
    result = Decimal(str(value))
    if result <= 0 or result > 1:
        raise ValueError(f"比例必须在 (0, 1] 区间：{value}")
    return result


class DisbursementChain:
    """拨付链服务：写侧追加事实，读侧投影余额与占用。"""

    def __init__(self, store: EventStore | None = None, directory: dict | None = None) -> None:
        self.store = store or EventStore()
        # directory: 操作者 -> 其拥有的角色集合
        self.directory = {actor: set(roles) for actor, roles in (directory or {}).items()}

    # ------------------------------------------------------------------
    # 申请：预算、用途限制、关联主体、其他资金承诺
    # ------------------------------------------------------------------
    def submit_application(
        self,
        application_id: str,
        *,
        budget: list[dict],
        usage_restrictions: list[str],
        related_entities: list,
        funding_commitments: list[dict],
        occurred_at: str | None = None,
    ) -> dict:
        if not budget:
            raise ValueError("预算不能为空")
        lines = []
        for line in budget:
            amount = money(line["amount"])
            if amount <= 0:
                raise ValueError("预算金额必须为正")
            lines.append({"category": line["category"], "amount": str(amount), "usage": line.get("usage", "")})
        commitments = [
            {"source": item["source"], "amount": str(money(item["amount"])), "note": item.get("note", "")}
            for item in funding_commitments
        ]
        return self.store.append(
            "APPLICATION_SUBMITTED",
            application_id,
            {
                "application_id": application_id,
                "budget": lines,
                "usage_restrictions": list(usage_restrictions),
                "related_entities": list(related_entities),
                "funding_commitments": commitments,
            },
            event_id=f"app:{application_id}:submitted",
            occurred_at=occurred_at,
        )

    def approve_application(
        self,
        application_id: str,
        *,
        approver: str,
        approved_amount=None,
        occurred_at: str | None = None,
    ) -> dict:
        self._require_role(approver, ROLE_APPROVER)
        application = self._application(application_id)
        amount = money(approved_amount) if approved_amount is not None else self._budget_total(application)
        return self.store.append(
            "APPLICATION_APPROVED",
            application_id,
            {"application_id": application_id, "approver": approver, "approved_amount": str(amount)},
            event_id=f"app:{application_id}:approved",
            occurred_at=occurred_at,
        )

    # ------------------------------------------------------------------
    # 证据指纹与重叠裁定
    # ------------------------------------------------------------------
    def register_evidence(
        self,
        application_id: str,
        evidence_id: str,
        evidence_kind: str,
        content: dict,
        *,
        occurred_at: str | None = None,
    ) -> dict:
        """登记合同/发票/交付物，生成稳定指纹并提示跨申请重叠。"""
        self._application(application_id)
        fingerprint = fingerprint_evidence(evidence_kind, content)
        record, created = self.store.try_append(
            "EVIDENCE_REGISTERED",
            application_id,
            {
                "application_id": application_id,
                "evidence_id": evidence_id,
                "evidence_kind": evidence_kind,
                "fingerprint": fingerprint,
                "content": content,
            },
            event_id=f"evi:{application_id}:{evidence_id}",
            occurred_at=occurred_at,
        )
        if created:
            self._maybe_flag_overlap(fingerprint, occurred_at=occurred_at)
        return record

    def adjudicate_overlap(
        self,
        fingerprint: str,
        *,
        adjudicator: str,
        verdict: str,
        rationale: str,
        occurred_at: str | None = None,
    ) -> dict:
        """授权人员裁定是否重复，必须留下依据；裁定只追加，不覆盖。"""
        self._require_role(adjudicator, ROLE_ADJUDICATOR)
        if verdict not in (VERDICT_DUPLICATE, VERDICT_NOT_DUPLICATE):
            raise ValueError(f"未知裁定结论: {verdict}")
        if not rationale or not rationale.strip():
            raise ValueError("裁定必须留下依据")
        if not any(e["payload"]["fingerprint"] == fingerprint for e in self.store.find("OVERLAP_FLAGGED")):
            raise UnknownOverlapError(f"指纹 {fingerprint} 没有待裁定的重叠提示")
        seq = sum(1 for e in self.store.find("OVERLAP_ADJUDICATED", fingerprint))
        return self.store.append(
            "OVERLAP_ADJUDICATED",
            fingerprint,
            {
                "fingerprint": fingerprint,
                "adjudicator": adjudicator,
                "verdict": verdict,
                "rationale": rationale,
            },
            event_id=f"adj:{fingerprint}:{seq + 1}",
            occurred_at=occurred_at,
        )

    # ------------------------------------------------------------------
    # 里程碑：固定适用协议、验收人、释放比例
    # ------------------------------------------------------------------
    def define_milestone(
        self,
        milestone_id: str,
        *,
        application_id: str,
        agreement_id: str,
        acceptor_id: str,
        release_ratio,
        planned_amount,
        requires_matching_funds: bool = False,
        required_matching_amount="0",
        occurred_at: str | None = None,
    ) -> dict:
        self._application(application_id)
        return self.store.append(
            "MILESTONE_DEFINED",
            application_id,
            {
                "milestone_id": milestone_id,
                "application_id": application_id,
                "agreement_id": agreement_id,
                "acceptor_id": acceptor_id,
                "release_ratio": str(_ratio(release_ratio)),
                "planned_amount": str(money(planned_amount)),
                "requires_matching_funds": bool(requires_matching_funds),
                "required_matching_amount": str(money(required_matching_amount)),
            },
            event_id=f"mst:{milestone_id}:defined",
            occurred_at=occurred_at,
        )

    def accept_milestone(
        self,
        milestone_id: str,
        *,
        acceptor_id: str,
        accepted_ratio,
        note: str = "",
        occurred_at: str | None = None,
    ) -> dict:
        """验收（支持部分验收）；只能由里程碑固定的验收人执行。"""
        definition = self._milestone(milestone_id)
        if acceptor_id != definition["acceptor_id"]:
            raise RoleNotAuthorizedError(
                f"验收人必须是里程碑固定适用协议中登记的 {definition['acceptor_id']}"
            )
        accepted = _ratio(accepted_ratio)
        if self._accepted_total(milestone_id) + accepted > 1:
            raise ValueError("累计验收比例超过 100%")
        seq = sum(1 for e in self.store.find("MILESTONE_ACCEPTED") if e["payload"]["milestone_id"] == milestone_id)
        return self.store.append(
            "MILESTONE_ACCEPTED",
            definition["application_id"],
            {
                "milestone_id": milestone_id,
                "application_id": definition["application_id"],
                "acceptor_id": acceptor_id,
                "accepted_ratio": str(accepted),
                "note": note,
            },
            event_id=f"mst:{milestone_id}:accepted:{seq + 1}",
            occurred_at=occurred_at,
        )

    # ------------------------------------------------------------------
    # 追加事实：配套款到账（可迟到）、企业重组、追回
    # ------------------------------------------------------------------
    def record_matching_funds(
        self,
        application_id: str,
        *,
        amount,
        source: str,
        received_at: str,
        occurred_at: str | None = None,
    ) -> dict:
        self._application(application_id)
        seq = len(self.store.find("MATCHING_FUNDS_RECORDED", application_id))
        return self.store.append(
            "MATCHING_FUNDS_RECORDED",
            application_id,
            {
                "application_id": application_id,
                "amount": str(money(amount)),
                "source": source,
                "received_at": received_at,
            },
            event_id=f"app:{application_id}:matching:{seq + 1}",
            occurred_at=occurred_at,
        )

    def record_restructuring(
        self,
        application_id: str,
        *,
        successor_entities: list,
        note: str,
        occurred_at: str | None = None,
    ) -> dict:
        self._application(application_id)
        seq = len(self.store.find("RESTRUCTURING_RECORDED", application_id))
        return self.store.append(
            "RESTRUCTURING_RECORDED",
            application_id,
            {
                "application_id": application_id,
                "successor_entities": list(successor_entities),
                "note": note,
            },
            event_id=f"app:{application_id}:restructuring:{seq + 1}",
            occurred_at=occurred_at,
        )

    def reconcile_recovery(
        self,
        application_id: str,
        *,
        amount,
        reason: str,
        payment_id: str | None = None,
        occurred_at: str | None = None,
    ) -> dict:
        """追回：追加事实修正余额，原拨付记录保持不动。"""
        self._application(application_id)
        seq = len(self.store.find("RECOVERY_RECONCILED", application_id))
        return self.store.append(
            "RECOVERY_RECONCILED",
            application_id,
            {
                "application_id": application_id,
                "amount": str(money(amount)),
                "reason": reason,
                "payment_id": payment_id,
            },
            event_id=f"app:{application_id}:recovery:{seq + 1}",
            occurred_at=occurred_at,
        )

    # ------------------------------------------------------------------
    # 付款（资金动作）：逐步落检查点，中断后从检查点继续
    # ------------------------------------------------------------------
    def initiate_payment(
        self,
        payment_id: str,
        *,
        milestone_id: str,
        approver: str,
        payer: str,
        occurred_at: str | None = None,
    ) -> dict:
        """发起付款：校验职责分离与可用额度，占用额度并落 INITIATED 检查点。"""
        definition = self._milestone(milestone_id)
        application_id = definition["application_id"]
        self._check_separation(approver=approver, acceptor=definition["acceptor_id"], payer=payer)
        self._require_role(approver, ROLE_APPROVER)
        self._require_role(payer, ROLE_PAYER)
        due = self._milestone_due(milestone_id)
        if due <= 0:
            raise DomainError(f"里程碑 {milestone_id} 无可付额度（未验收或已付清）")
        usable = self.balance_of(application_id)["usable"]
        if due > usable:
            raise QuotaUnavailableError(
                f"里程碑 {milestone_id} 可付 {due}，但申请 {application_id} 仍可使用余额仅 {usable}"
            )
        self.store.append(
            "PAYMENT_STEP",
            payment_id,
            {
                "payment_id": payment_id,
                "step": STEP_INITIATED,
                "milestone_id": milestone_id,
                "application_id": application_id,
                "amount": str(due),
                "approver": approver,
                "payer": payer,
            },
            event_id=f"pay:{payment_id}:{STEP_INITIATED}",
            occurred_at=occurred_at,
        )
        return self.payment_state(payment_id)

    def approve_payment(self, payment_id: str, *, approver: str) -> dict:
        """审批检查点：必须由发起时指定的审批人执行。"""
        state = self.payment_state(payment_id)
        context = state["context"]
        if approver != context["approver"]:
            raise RoleNotAuthorizedError("审批人必须是发起付款时指定的审批人")
        self._require_role(approver, ROLE_APPROVER)
        acceptor = self._milestone(context["milestone_id"])["acceptor_id"]
        self._check_separation(approver=approver, acceptor=acceptor, payer=context["payer"])
        self._checkpoint(payment_id, STEP_APPROVED, actor=approver)
        return self.payment_state(payment_id)

    def advance_payment(self, payment_id: str) -> dict:
        """从最近检查点继续执行，直到等待回执或再次中断。"""
        state = self.payment_state(payment_id)
        if state["status"] == STATUS_CANCELLED:
            raise DomainError(f"付款 {payment_id} 已取消")
        context = state["context"]
        done = set(state["steps"])
        if STEP_APPROVED not in done:
            raise PaymentBlockedError(payment_id, STEP_APPROVED, "等待审批，请先调用 approve_payment")
        if STEP_CONDITIONS_VERIFIED not in done:
            self._verify_conditions(payment_id, context)
        if STEP_RELEASED not in done:
            self._release(payment_id, context)
        return self.payment_state(payment_id)

    def record_bank_receipt(self, receipt_ref: str, *, payment_id: str, amount) -> tuple[dict, bool]:
        """登记银行回执；同一回执号重放返回原记录，不会再次拨付。"""
        state = self.payment_state(payment_id)
        if STEP_RELEASED not in state["steps"]:
            raise DomainError(f"付款 {payment_id} 尚未拨付，不能登记回执")
        if money(amount) != Decimal(state["context"]["amount"]):
            raise DomainError("回执金额与拨付金额不一致")
        record, created = self.store.try_append(
            "BANK_RECEIPT_RECORDED",
            payment_id,
            {"receipt_ref": receipt_ref, "payment_id": payment_id, "amount": str(money(amount))},
            event_id=f"rcpt:{receipt_ref}",
        )
        if not created and record["payload"]["payment_id"] != payment_id:
            raise DomainError(f"回执 {receipt_ref} 已属于付款 {record['payload']['payment_id']}")
        self._checkpoint(payment_id, STEP_RECEIPT_CONFIRMED, actor="bank", detail={"receipt_ref": receipt_ref})
        return record, created

    def cancel_payment(self, payment_id: str, *, actor: str, reason: str) -> dict:
        """取消未拨付的付款，释放占用的额度；已拨付的只能追回。"""
        state = self.payment_state(payment_id)
        if STEP_RELEASED in state["steps"]:
            raise DomainError("已拨付的付款不能取消，应通过追回修正")
        self._checkpoint(payment_id, STEP_CANCELLED, actor=actor, detail={"reason": reason})
        return self.payment_state(payment_id)

    # ------------------------------------------------------------------
    # 批量材料：冲突只隔离对应项目
    # ------------------------------------------------------------------
    def import_batch(self, batch_id: str, items: list[dict]) -> dict:
        """逐项登记批量材料；单项冲突只隔离该项，其余继续。"""
        registered, quarantined = [], []
        for item in items:
            try:
                if self.store.get(f"evi:{item['application_id']}:{item['evidence_id']}") is not None:
                    raise DomainError(f"证据已登记: {item['evidence_id']}")
                record = self.register_evidence(
                    item["application_id"],
                    item["evidence_id"],
                    item["evidence_kind"],
                    item["content"],
                )
                registered.append(
                    {
                        "item_id": item.get("item_id"),
                        "evidence_id": item["evidence_id"],
                        "fingerprint": record["payload"]["fingerprint"],
                    }
                )
            except (DomainError, KeyError, ValueError) as exc:
                seq = len(self.store.find("BATCH_ITEM_QUARANTINED", batch_id))
                self.store.append(
                    "BATCH_ITEM_QUARANTINED",
                    batch_id,
                    {
                        "batch_id": batch_id,
                        "item_id": item.get("item_id"),
                        "application_id": item.get("application_id"),
                        "reason": str(exc),
                    },
                    event_id=f"batch:{batch_id}:quarantine:{seq + 1}",
                )
                quarantined.append({"item_id": item.get("item_id"), "reason": str(exc)})
        return {"batch_id": batch_id, "registered": registered, "quarantined": quarantined}

    # ------------------------------------------------------------------
    # 读侧投影
    # ------------------------------------------------------------------
    def payment_state(self, payment_id: str) -> dict:
        steps = self.store.find("PAYMENT_STEP", payment_id)
        if not steps:
            raise UnknownPaymentError(f"付款不存在: {payment_id}")
        done = [event["payload"]["step"] for event in steps]
        initiated = next(event for event in steps if event["payload"]["step"] == STEP_INITIATED)
        if STEP_CANCELLED in done:
            status = STATUS_CANCELLED
        elif STEP_RECEIPT_CONFIRMED in done:
            status = STATUS_COMPLETED
        else:
            status = STATUS_IN_PROGRESS
        return {
            "payment_id": payment_id,
            "status": status,
            "steps": done,
            "context": initiated["payload"],
            "released_event": self.store.get(f"pay:{payment_id}:released"),
            "receipts": self.store.find("BANK_RECEIPT_RECORDED", payment_id),
        }

    def application_dossier(self, application_id: str) -> dict:
        approvals = self.store.find("APPLICATION_APPROVED", application_id)
        return {
            "application_id": application_id,
            "submitted": self._application(application_id),
            "approval": approvals[-1]["payload"] if approvals else None,
        }

    def evidence_of(self, application_id: str) -> list[dict]:
        return [event["payload"] for event in self.store.find("EVIDENCE_REGISTERED", application_id)]

    def overlap_of(self, application_id: str) -> list[dict]:
        fingerprints = {item["fingerprint"] for item in self.evidence_of(application_id)}
        overlaps = []
        for flag in self.store.find("OVERLAP_FLAGGED"):
            fingerprint = flag["payload"]["fingerprint"]
            if fingerprint not in fingerprints:
                continue
            adjudications = [
                event["payload"] for event in self.store.find("OVERLAP_ADJUDICATED", fingerprint)
            ]
            overlaps.append(
                {
                    "fingerprint": fingerprint,
                    "flag": flag["payload"],
                    "adjudications": adjudications,
                    "current_verdict": adjudications[-1]["verdict"] if adjudications else None,
                }
            )
        return overlaps

    def milestone_of(self, milestone_id: str) -> dict:
        return {
            "definition": self._milestone(milestone_id),
            "accepted_ratio": self._accepted_total(milestone_id),
            "released_amount": self._milestone_released(milestone_id),
            "due_amount": self._milestone_due(milestone_id),
        }

    def adjustments_of(self, application_id: str) -> list[dict]:
        """历次调整：部分验收、配套款、重组、追回，按发生顺序。"""
        return [
            {"kind": event["kind"], "occurred_at": event["occurred_at"], "payload": event["payload"]}
            for event in self.store.find(subject_id=application_id)
            if event["kind"] in _ADJUSTMENT_KINDS
        ]

    def occupancy_of(self, application_id: str) -> list[dict]:
        """额度被谁占用：已发起但未拨付、未取消的付款。"""
        occupied = []
        for event in self.store.find("PAYMENT_STEP"):
            payload = event["payload"]
            if payload.get("step") != STEP_INITIATED or payload.get("application_id") != application_id:
                continue
            state = self.payment_state(payload["payment_id"])
            done = set(state["steps"])
            if STEP_RELEASED in done or STEP_CANCELLED in done:
                continue
            occupied.append(
                {
                    "payment_id": payload["payment_id"],
                    "milestone_id": payload["milestone_id"],
                    "amount": Decimal(payload["amount"]),
                    "last_step": state["steps"][-1],
                }
            )
        return occupied

    def balance_of(self, application_id: str) -> dict[str, Decimal]:
        approvals = self.store.find("APPLICATION_APPROVED", application_id)
        approved = Decimal(approvals[-1]["payload"]["approved_amount"]) if approvals else Decimal("0")
        released = sum(
            (Decimal(event["payload"]["amount"]) for event in self.store.find("TRANCHE_RELEASED", application_id)),
            Decimal("0"),
        )
        recovered = sum(
            (Decimal(event["payload"]["amount"]) for event in self.store.find("RECOVERY_RECONCILED", application_id)),
            Decimal("0"),
        )
        occupied = sum((item["amount"] for item in self.occupancy_of(application_id)), Decimal("0"))
        return {
            "approved": approved,
            "released": released,
            "recovered": recovered,
            "occupied": occupied,
            "usable": approved - released + recovered - occupied,
        }

    # ------------------------------------------------------------------
    # 内部：检查点步骤与投影辅助
    # ------------------------------------------------------------------
    def _verify_conditions(self, payment_id: str, context: dict) -> None:
        definition = self._milestone(context["milestone_id"])
        application_id = context["application_id"]
        if definition["requires_matching_funds"]:
            arrived = self._matching_total(application_id)
            required = Decimal(definition["required_matching_amount"])
            if arrived < required:
                raise PaymentBlockedError(
                    payment_id,
                    STEP_CONDITIONS_VERIFIED,
                    f"配套款未到账：已到账 {arrived}，要求 {required}",
                )
        flagged = {item["fingerprint"]: item for item in self.overlap_of(application_id)}
        for fingerprint, item in flagged.items():
            if item["current_verdict"] is None:
                raise PaymentBlockedError(
                    payment_id,
                    STEP_CONDITIONS_VERIFIED,
                    f"证据指纹 {fingerprint} 存在待裁定的重叠提示",
                )
            if item["current_verdict"] == VERDICT_DUPLICATE:
                raise DuplicateEvidenceVerdictError(
                    f"证据指纹 {fingerprint} 已被授权人员裁定为重复"
                )
        self._checkpoint(payment_id, STEP_CONDITIONS_VERIFIED, actor="system")

    def _release(self, payment_id: str, context: dict) -> None:
        payer = context["payer"]
        self._require_role(payer, ROLE_PAYER)
        acceptor = self._milestone(context["milestone_id"])["acceptor_id"]
        self._check_separation(approver=context["approver"], acceptor=acceptor, payer=payer)
        record, _ = self.store.try_append(
            "TRANCHE_RELEASED",
            context["application_id"],
            {
                "payment_id": payment_id,
                "milestone_id": context["milestone_id"],
                "application_id": context["application_id"],
                "amount": context["amount"],
                "payer": payer,
            },
            event_id=f"pay:{payment_id}:released",
        )
        self._checkpoint(payment_id, STEP_RELEASED, actor=payer, detail={"tranche_event_id": record["event_id"]})

    def _checkpoint(self, payment_id: str, step: str, *, actor: str, detail: dict | None = None) -> dict:
        record, _ = self.store.try_append(
            "PAYMENT_STEP",
            payment_id,
            {"payment_id": payment_id, "step": step, "actor": actor, "detail": detail or {}},
            event_id=f"pay:{payment_id}:{step}",
        )
        return record

    def _maybe_flag_overlap(self, fingerprint: str, *, occurred_at: str | None = None) -> None:
        applications = sorted(
            {
                event["payload"]["application_id"]
                for event in self.store.find("EVIDENCE_REGISTERED")
                if event["payload"]["fingerprint"] == fingerprint
            }
        )
        if len(applications) > 1:
            self.store.try_append(
                "OVERLAP_FLAGGED",
                fingerprint,
                {"fingerprint": fingerprint, "application_ids": applications},
                event_id=f"ovl:{fingerprint}",
                occurred_at=occurred_at,
            )

    def _application(self, application_id: str) -> dict:
        record = self.store.get(f"app:{application_id}:submitted")
        if record is None:
            raise UnknownApplicationError(f"申请不存在: {application_id}")
        return record["payload"]

    def _milestone(self, milestone_id: str) -> dict:
        record = self.store.get(f"mst:{milestone_id}:defined")
        if record is None:
            raise UnknownMilestoneError(f"里程碑不存在: {milestone_id}")
        return record["payload"]

    def _require_role(self, actor: str, role: str) -> None:
        if role not in self.directory.get(actor, set()):
            raise RoleNotAuthorizedError(f"{actor} 不具备 {role} 角色")

    @staticmethod
    def _check_separation(*, approver: str, acceptor: str, payer: str) -> None:
        if len({approver, acceptor, payer}) != 3:
            raise SeparationOfDutiesError("审批、验收和付款必须由不同角色承担")

    @staticmethod
    def _budget_total(application: dict) -> Decimal:
        return sum((Decimal(line["amount"]) for line in application["budget"]), Decimal("0"))

    def _accepted_total(self, milestone_id: str) -> Decimal:
        return sum(
            (
                Decimal(event["payload"]["accepted_ratio"])
                for event in self.store.find("MILESTONE_ACCEPTED")
                if event["payload"]["milestone_id"] == milestone_id
            ),
            Decimal("0"),
        )

    def _matching_total(self, application_id: str) -> Decimal:
        return sum(
            (
                Decimal(event["payload"]["amount"])
                for event in self.store.find("MATCHING_FUNDS_RECORDED", application_id)
            ),
            Decimal("0"),
        )

    def _milestone_released(self, milestone_id: str) -> Decimal:
        return sum(
            (
                Decimal(event["payload"]["amount"])
                for event in self.store.find("TRANCHE_RELEASED")
                if event["payload"]["milestone_id"] == milestone_id
            ),
            Decimal("0"),
        )

    def _milestone_due(self, milestone_id: str) -> Decimal:
        definition = self._milestone(milestone_id)
        entitled = money(Decimal(definition["planned_amount"]) * self._accepted_total(milestone_id))
        in_flight = sum(
            (
                item["amount"]
                for item in self.occupancy_of(definition["application_id"])
                if item["milestone_id"] == milestone_id
            ),
            Decimal("0"),
        )
        return entitled - self._milestone_released(milestone_id) - in_flight
