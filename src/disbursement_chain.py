"""可穿透的拨付链（append-only 领域服务）。

设计原则（对应审计要求）：

* 事件只追加：部分验收、企业重组、配套款迟到、追回都是"追加事实"，余额由事件
  流重算得到，任何原结论都不会被覆盖或删除。
* 证据指纹：合同/发票/交付物按规范字段生成稳定指纹；同一指纹跨申请出现只产生
  提示（OVERLAP_FLAGGED），是否重复由授权人员裁决（OVERLAP_DECIDED）并留依据。
* 固定条款：里程碑在定义时固定适用协议、验收人和释放比例；审批、验收、付款必须
  由不同角色承担。
* 先占用后拨付：付款先预留额度（QUOTA_RESERVED），条件满足且额度未被他处占用才
  释放（TRANCHE_RELEASED），银行回执号幂等，重放不会再次拨付。
* 检查点续作：付款在 RESERVED → RELEASED → SETTLED 之间推进，任何一步中断后都
  从同一检查点幂等继续（例如配套款迟到账后继续释放）。
* 故障隔离：批量材料按项目/条目隔离，单条冲突不影响其他项目。
* 可穿透：从任意一笔付款可回溯预算来源、证据指纹、重叠裁决、历次调整与可用余额。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from .culture_finance_progress import validate_event

# —— 角色：审批、验收、付款、重叠裁决必须分离 ——
ROLE_APPROVER = "APPROVER"            # 审批人
ROLE_ACCEPTOR = "ACCEPTOR"            # 验收人
ROLE_PAYER = "PAYER"                  # 付款人
ROLE_OVERLAP_OFFICER = "OVERLAP_OFFICER"  # 跨申请重叠的授权裁决人

# —— 证据种类 ——
EVIDENCE_CONTRACT = "CONTRACT"
EVIDENCE_INVOICE = "INVOICE"
EVIDENCE_DELIVERABLE = "DELIVERABLE"
EVIDENCE_KINDS = (EVIDENCE_CONTRACT, EVIDENCE_INVOICE, EVIDENCE_DELIVERABLE)

# —— 里程碑放款前置条件 ——
CONDITION_MATCHING_FUNDS = "MATCHING_FUNDS"  # 企业配套款足额到账

# —— 重叠裁决 ——
OVERLAP_OPEN = "OPEN"
OVERLAP_ALLOWED = "ALLOWED"
OVERLAP_DENIED = "DENIED"

# —— 付款检查点 ——
PAYMENT_RESERVED = "RESERVED"    # 额度已预留，尚未释放
PAYMENT_RELEASED = "RELEASED"    # 已拨付，等待银行回执
PAYMENT_SETTLED = "SETTLED"      # 回执登记完成
PAYMENT_RECOVERED = "RECOVERED"  # 已（可能部分）追回

_CENT = Decimal("0.01")


def money(value) -> Decimal:
    """把输入规整为两位小数的金额。"""
    return Decimal(str(value)).quantize(_CENT)


def ratio(value) -> Decimal:
    return Decimal(str(value))


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _normalize(value):
    """递归规整指纹输入：字符串去首尾空白，键排序，保证指纹稳定。"""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        return {k: _normalize(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


def evidence_fingerprint(kind: str, canonical_fields: dict) -> str:
    """由证据的规范业务字段计算稳定指纹。

    指纹只依赖业务字段（发票代码/号码、购销双方、开票日期、金额等），与文件
    版式无关；同一证据重复提交指纹不变，跨申请复用即可被识别。
    """
    if kind not in EVIDENCE_KINDS:
        raise ChainError("unknown-evidence-kind", f"未知证据种类: {kind}")
    canonical = json.dumps(
        _normalize({"kind": kind, "fields": canonical_fields}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class ChainError(Exception):
    """领域规则冲突；在批量处理中仅隔离当前条目。"""

    def __init__(self, code: str, message: str, blockers: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.blockers = blockers or []


@dataclass
class BatchItem:
    ok: bool
    application_id: str | None
    result: dict | None = None
    error: str | None = None
    error_code: str | None = None


@dataclass
class _Application:
    application_id: str
    applicant_id: str
    funding_source: str
    budget: Decimal
    use_restrictions: frozenset[str]
    related_parties: tuple
    commitments: list = field(default_factory=list)
    adjustments: list = field(default_factory=list)   # 企业重组等追加调整
    matching_received: Decimal = Decimal("0")


@dataclass
class _Milestone:
    application_id: str
    milestone_id: str
    protocol_id: str
    acceptor_id: str
    release_ratio: Decimal
    purpose: str
    preconditions: frozenset[str]
    evidence_ids: list
    approved_by: str | None = None
    approved_at: str | None = None
    acceptances: list = field(default_factory=list)   # [{delta,cumulative,by,at,event_id}]


@dataclass
class _Payment:
    payment_id: str
    application_id: str
    milestone_id: str
    payer_id: str
    amount: Decimal
    state: str
    created_at: str
    receipt_no: str | None = None
    released_event_id: str | None = None
    receipt_event_id: str | None = None
    recovered: Decimal = Decimal("0")


class DisbursementChain:
    """基于只追加事件流的拨付链。状态由事件折叠得出，可整体重放。"""

    def __init__(self):
        self.events: list[dict] = []
        self._seq = 0
        self.actors: dict[str, str] = {}
        self.applications: dict[str, _Application] = {}
        self.evidence: dict[tuple[str, str], dict] = {}
        self.fingerprints: dict[str, list[tuple[str, str]]] = {}
        self.flags: dict[str, dict] = {}
        self.decisions: list[dict] = []
        self.milestones: dict[tuple[str, str], _Milestone] = {}
        self.payments: dict[str, _Payment] = {}
        self._idempotency: dict[tuple, str] = {}
        self._receipts: dict[str, str] = {}  # 回执号 -> 付款（全局唯一）

    # —— 重放 ——
    @classmethod
    def replay(cls, events: list[dict]) -> "DisbursementChain":
        chain = cls()
        for event in events:
            chain._ingest(event)
        return chain

    def _ingest(self, event: dict) -> None:
        problems = validate_event(event)
        if problems:
            raise ChainError("bad-event", f"事件缺少字段或种类未知: {problems}")
        self.events.append(event)
        self._fold(event)

    def _emit(self, kind: str, application_id: str, payload: dict,
              occurred_at: str | None = None, event_id: str | None = None) -> dict:
        event = {
            "event_id": event_id or uuid.uuid4().hex,
            "kind": kind,
            "occurred_at": occurred_at or now_iso(),
            "subject_id": application_id,
            "payload": payload,
        }
        # 先校验再折叠；payload 中的金额统一为字符串以便交换
        validate_event(event)
        self.events.append(event)
        self._fold(event)
        return event

    # —— 角色 ——
    def register_actor(self, actor_id: str, role: str) -> None:
        if role not in (ROLE_APPROVER, ROLE_ACCEPTOR, ROLE_PAYER, ROLE_OVERLAP_OFFICER):
            raise ChainError("unknown-role", f"未知角色: {role}")
        self.actors[actor_id] = role

    def _require_role(self, actor_id: str, role: str) -> None:
        if self.actors.get(actor_id) != role:
            raise ChainError(
                "role-required", f"{actor_id} 不具备角色 {role}（实际: {self.actors.get(actor_id)}）"
            )

    # —— 1. 项目申请：预算、用途限制、关联主体、其他资金承诺 ——
    def submit_application(self, application_id: str, applicant_id: str, budget,
                           funding_source: str, use_restrictions: list[str] | None = None,
                           related_parties: list[str] | None = None,
                           other_commitments: list[dict] | None = None,
                           matching_required=None, occurred_at: str | None = None) -> dict:
        if application_id in self.applications:
            raise ChainError("application-exists", f"申请已存在: {application_id}")
        commitments = list(other_commitments or [])
        if matching_required is not None and money(matching_required) > 0:
            commitments.append({"source": "ENTERPRISE_MATCHING",
                                "amount": str(money(matching_required)), "status": "PROMISED"})
        payload = {
            "applicant_id": applicant_id,
            "funding_source": funding_source,
            "budget": str(money(budget)),
            "use_restrictions": sorted(set(use_restrictions or [])),
            "related_parties": sorted(set(related_parties or [])),
            "other_commitments": commitments,
        }
        return self._emit("APPLICATION_SUBMITTED", application_id, payload, occurred_at)

    # —— 2. 证据登记：稳定指纹 + 跨申请重叠提示 ——
    def register_evidence(self, application_id: str, evidence_id: str, kind: str,
                          canonical_fields: dict, occurred_at: str | None = None) -> dict:
        app = self._app(application_id)
        key = (application_id, evidence_id)
        if key in self.evidence:
            raise ChainError("evidence-exists", f"证据已登记: {evidence_id}")
        fingerprint = evidence_fingerprint(kind, canonical_fields)
        holders = self.fingerprints.setdefault(fingerprint, [])
        cross = [(a, e) for (a, e) in holders if a != application_id]
        payload = {
            "evidence_id": evidence_id,
            "evidence_kind": kind,
            "fingerprint": fingerprint,
            "canonical_fields": _normalize(canonical_fields),
            "applicant_id": app.applicant_id,
        }
        event = self._emit("EVIDENCE_REGISTERED", application_id, payload, occurred_at)
        # 同一指纹出现在另一申请 → 仅提示，结论留待授权人员裁决
        # （指纹持有人台账由 _fold 统一登记，此处不重复写入）
        for other_app, other_ev in cross:
            flag_id = f"ovlp:{fingerprint[:19]}:{other_app}->{application_id}"
            flag = {
                "flag_id": flag_id,
                "fingerprint": fingerprint,
                "first_application_id": other_app,
                "first_evidence_id": other_ev,
                "later_application_id": application_id,
                "later_evidence_id": evidence_id,
                "status": OVERLAP_OPEN,
            }
            self.flags[flag_id] = flag
            self._emit("OVERLAP_FLAGGED", application_id,
                       {**flag, "note": "系统仅提示跨申请证据重叠，是否重复待授权裁决"},
                       occurred_at)
        return event

    def decide_overlap(self, flag_id: str, decision: str, officer_id: str,
                       rationale: str, occurred_at: str | None = None) -> dict:
        """授权人员对重叠提示作出裁决；裁决只追加，不可更改。"""
        self._require_role(officer_id, ROLE_OVERLAP_OFFICER)
        flag = self.flags.get(flag_id)
        if flag is None:
            raise ChainError("flag-not-found", f"重叠提示不存在: {flag_id}")
        if flag["status"] != OVERLAP_OPEN:
            raise ChainError("overlap-decided",
                             f"重叠已裁决为 {flag['status']}，裁决不可覆盖")
        if decision not in (OVERLAP_ALLOWED, OVERLAP_DENIED):
            raise ChainError("bad-decision", f"非法裁决: {decision}")
        if not rationale or not rationale.strip():
            raise ChainError("rationale-required", "裁决必须留下书面依据")
        record = {
            "flag_id": flag_id,
            "fingerprint": flag["fingerprint"],
            "decision": decision,
            "decided_by": officer_id,
            "rationale": rationale,
            "application_id": flag["later_application_id"],
        }
        return self._emit("OVERLAP_DECIDED", flag["later_application_id"],
                          record, occurred_at)

    # —— 3. 里程碑：固定协议、验收人、释放比例、前置条件 ——
    def define_milestone(self, application_id: str, milestone_id: str, protocol_id: str,
                         acceptor_id: str, release_ratio_, purpose: str,
                         preconditions: list[str] | None = None,
                         evidence_ids: list[str] | None = None,
                         occurred_at: str | None = None) -> dict:
        self._app(application_id)
        self._require_role(acceptor_id, ROLE_ACCEPTOR)
        if (application_id, milestone_id) in self.milestones:
            raise ChainError("milestone-exists", f"里程碑已存在: {milestone_id}")
        rr = ratio(release_ratio_)
        if not (Decimal("0") < rr <= Decimal("1")):
            raise ChainError("bad-ratio", "释放比例必须在 (0, 1] 区间")
        for ev_id in evidence_ids or []:
            if (application_id, ev_id) not in self.evidence:
                raise ChainError("evidence-not-found", f"证据未登记: {ev_id}")
        pre = frozenset(preconditions or ())
        payload = {
            "milestone_id": milestone_id,
            "protocol_id": protocol_id,
            "acceptor_id": acceptor_id,
            "release_ratio": str(rr),
            "purpose": purpose,
            "preconditions": sorted(pre),
            "evidence_ids": list(evidence_ids or []),
            "fixed_terms_note": "协议/验收人/释放比例定义后固定，不得就地修改",
        }
        return self._emit("MILESTONE_DEFINED", application_id, payload, occurred_at)

    def approve_milestone(self, application_id: str, milestone_id: str, approver_id: str,
                          occurred_at: str | None = None) -> dict:
        self._require_role(approver_id, ROLE_APPROVER)
        ms = self._ms(application_id, milestone_id)
        if ms.approved_by is not None:
            raise ChainError("already-approved", "里程碑已审批，结论不可覆盖")
        if approver_id == ms.acceptor_id:
            raise ChainError(
                "separation-of-duties",
                f"审批人 {approver_id} 与固定验收人相同，审批与验收必须分离",
            )
        return self._emit("MILESTONE_APPROVED", application_id, {
            "milestone_id": milestone_id, "approved_by": approver_id,
        }, occurred_at)

    def accept_milestone(self, application_id: str, milestone_id: str, delta_ratio_,
                         acceptor_id: str, occurred_at: str | None = None,
                         note: str = "") -> dict:
        """验收（可为部分验收）。每次验收都是追加事实，不覆盖此前验收结论。"""
        self._require_role(acceptor_id, ROLE_ACCEPTOR)
        ms = self._ms(application_id, milestone_id)
        if acceptor_id != ms.acceptor_id:
            raise ChainError(
                "acceptor-mismatch",
                f"验收人 {acceptor_id} 与里程碑固定验收人 {ms.acceptor_id} 不符",
            )
        delta = ratio(delta_ratio_)
        if not (Decimal("0") < delta <= Decimal("1")):
            raise ChainError("bad-ratio", "单次验收比例必须在 (0, 1] 区间")
        cumulative = self.accepted_ratio(application_id, milestone_id) + delta
        if cumulative > Decimal("1") + Decimal("0.000001"):
            raise ChainError(
                "acceptance-exceeded",
                f"累计验收比例 {cumulative} 超过 1；部分验收只能追加不能覆盖",
            )
        cumulative = min(cumulative, Decimal("1")).normalize()
        event = self._emit("MILESTONE_ACCEPTED", application_id, {
            "milestone_id": milestone_id,
            "accepted_delta_ratio": str(delta),
            "accepted_cumulative_ratio": str(cumulative),
            "accepted_by": acceptor_id,
            "partial": cumulative < Decimal("1"),
            "note": note or ("部分验收" if cumulative < Decimal("1") else "全部验收"),
        }, occurred_at)
        return event

    def accepted_ratio(self, application_id: str, milestone_id: str) -> Decimal:
        ms = self._ms(application_id, milestone_id)
        return min(Decimal("1"), sum((a["delta"] for a in ms.acceptances),
                                     Decimal("0"))).normalize()

    # —— 4. 追加事实：配套款到账、企业重组 ——
    def record_matching_funds(self, application_id: str, amount, occurred_at: str | None = None,
                              note: str = "") -> dict:
        """企业配套款到账。迟到账也只是追加事实，随后自动满足放款条件。"""
        app = self._app(application_id)
        return self._emit("MATCHING_FUNDS_RECEIVED", application_id, {
            "amount": str(money(amount)),
            "matching_received_total": str(app.matching_received),
            "note": note or "配套款到账（追加事实）",
        }, occurred_at)

    def record_restructuring(self, application_id: str, budget_adjustment, actor_id: str,
                             note: str, occurred_at: str | None = None) -> dict:
        """企业重组：以追加的预算调整修正余额，原拨付结论保持不变。"""
        app = self._app(application_id)
        adj = money(budget_adjustment)
        if self.effective_budget(application_id) + adj < 0:
            raise ChainError("adjustment-below-zero", "重组调整后预算不能为负")
        event = self._emit("ENTITY_RESTRUCTURED", application_id, {
            "budget_adjustment": str(adj),
            "recorded_by": actor_id,
            "note": note,
        }, occurred_at)
        return event

    # —— 5. 资金动作：预留 → 释放 → 回执（检查点） ——
    def reserve_quota(self, application_id: str, milestone_id: str, amount, payer_id: str,
                      idempotency_key: str, purpose: str | None = None,
                      occurred_at: str | None = None) -> dict:
        """检查点 1：为付款预留额度，形成可查询的占用。"""
        self._require_role(payer_id, ROLE_PAYER)
        ms = self._ms(application_id, milestone_id)
        dedup_key = ("reserve", application_id, milestone_id, idempotency_key)
        if dedup_key in self._idempotency:
            return self._event_by_id[self._idempotency[dedup_key]]
        amt = money(amount)
        if amt <= 0:
            raise ChainError("bad-amount", "预留金额必须为正")
        if purpose is not None and purpose != ms.purpose:
            raise ChainError("purpose-mismatch",
                             f"付款用途 {purpose} 与里程碑固定用途 {ms.purpose} 不符")
        available = self.available_balance(application_id)
        if available < amt:
            raise ChainError(
                "quota-unavailable",
                f"可用额度不足：需要 {amt}，仅剩 {available}（额度已被他处预留或拨付占用）",
            )
        payment_id = f"pay:{application_id}:{milestone_id}:{idempotency_key}"
        if payment_id in self.payments:
            raise ChainError("payment-exists", f"付款已存在: {payment_id}")
        event = self._emit("QUOTA_RESERVED", application_id, {
            "payment_id": payment_id,
            "milestone_id": milestone_id,
            "amount": str(amt),
            "reserved_by": payer_id,
            "purpose": ms.purpose,
            "checkpoint": PAYMENT_RESERVED,
        }, occurred_at)
        self._idempotency[dedup_key] = event["event_id"]
        return event

    def release_blockers(self, payment_id: str) -> list[str]:
        """返回当前阻止释放的条件清单；为空即可释放。"""
        p = self._pay(payment_id)
        app = self._app(p.application_id)
        ms = self._ms(p.application_id, p.milestone_id)
        blockers: list[str] = []

        if ms.approved_by is None:
            blockers.append("里程碑尚未经审批人批准")
        else:
            judges = {ms.approved_by, ms.acceptor_id, p.payer_id}
            if len(judges) < 3:
                blockers.append("审批人、验收人、付款人必须为三个不同角色（职责分离）")

        accepted = self.accepted_ratio(p.application_id, p.milestone_id)
        if accepted == 0:
            blockers.append("里程碑尚未验收，不能释放")

        if CONDITION_MATCHING_FUNDS in ms.preconditions:
            required = self._matching_required(p.application_id)
            if app.matching_received + Decimal("0.000001") < required:
                blockers.append(
                    f"企业配套款未足额到账：已到 {app.matching_received} / 应到 {required}"
                )

        if app.use_restrictions and ms.purpose not in app.use_restrictions:
            blockers.append(f"用途 {ms.purpose} 不在申请的用途限制 {sorted(app.use_restrictions)} 内")

        # 证据重叠：未裁决或被裁决拒绝，都不能放行
        for ev_id in ms.evidence_ids:
            rec = self.evidence[(p.application_id, ev_id)]
            for flag in self._flags_for(p.application_id, rec["fingerprint"]):
                other_app, other_ev = self._counterparty(flag, p.application_id)
                if flag["status"] == OVERLAP_OPEN:
                    blockers.append(
                        f"证据 {ev_id} 与申请 {other_app}/{other_ev} 共用同一指纹，"
                        "尚待授权裁决"
                    )
                elif flag["status"] == OVERLAP_DENIED:
                    # 裁决拒绝针对复用方（后登记的申请）；原始登记方不受此裁决连坐
                    if flag["later_application_id"] == p.application_id:
                        blockers.append(
                            f"证据 {ev_id} 对申请 {other_app}/{other_ev} 的复用已被裁决拒绝"
                            f"（{flag['flag_id']}）"
                        )

        outstanding = self._releasable_capacity(ms)
        if p.amount > outstanding + Decimal("0.000001"):
            blockers.append(
                f"本次 {p.amount} 超过里程碑当前可释放额度 {outstanding}（验收比例 "
                f"{accepted}，部分验收须追加）"
            )
        if self.available_balance(p.application_id) + self._reservation_charge(p) < p.amount - Decimal("0.000001"):
            blockers.append("额度已被其他付款占用，可用余额不足")
        return blockers

    def release_tranche(self, payment_id: str, occurred_at: str | None = None) -> dict:
        """检查点 2：条件全部满足且额度未被占用，才实际拨付。"""
        p = self._pay(payment_id)
        if p.state != PAYMENT_RESERVED:
            raise ChainError("bad-checkpoint",
                             f"付款处于 {p.state}，只有 RESERVED 可释放（检查点不可跳跃）")
        blockers = self.release_blockers(payment_id)
        if blockers:
            raise ChainError("release-blocked",
                             f"付款 {payment_id} 暂不能释放：{'; '.join(blockers)}",
                             blockers=blockers)
        event = self._emit("TRANCHE_RELEASED", p.application_id, {
            "payment_id": payment_id,
            "milestone_id": p.milestone_id,
            "amount": str(p.amount),
            "released_by": p.payer_id,
            "checkpoint": PAYMENT_RELEASED,
        }, occurred_at)
        return event

    def record_bank_receipt(self, payment_id: str, receipt_no: str,
                            occurred_at: str | None = None) -> dict:
        """检查点 3：登记银行回执。回执号全局唯一，重放不会再次拨付。"""
        p = self._pay(payment_id)
        if p.state == PAYMENT_SETTLED and p.receipt_no == receipt_no:
            # 同一回执重放：幂等返回，不产生新事件、不再次拨付
            return self._event_by_id[p.receipt_event_id]
        if receipt_no in self._receipts:
            other = self._receipts[receipt_no]
            raise ChainError(
                "receipt-reused",
                f"回执号 {receipt_no} 已属于付款 {other}，不能用于 {payment_id}",
            )
        if p.state != PAYMENT_RELEASED:
            raise ChainError("bad-checkpoint",
                             f"付款处于 {p.state}，须先释放才能登记回执")
        return self._emit("BANK_RECEIPT_RECORDED", p.application_id, {
            "payment_id": payment_id,
            "receipt_no": receipt_no,
            "amount": str(p.amount),
            "checkpoint": PAYMENT_SETTLED,
        }, occurred_at)

    def resume_payment(self, payment_id: str, receipt_no: str | None = None) -> dict:
        """从中断处继续：自动推进到下一个可执行检查点，每步幂等。

        典型场景：释放时配套款尚未到账而中断，配套款追加到账后调用本方法即从
        RESERVED 检查点继续释放，再凭回执号完成结算。
        """
        p = self._pay(payment_id)
        progressed: list[str] = []
        blockers: list[str] = []
        if p.state == PAYMENT_RESERVED:
            blockers = self.release_blockers(payment_id)
            if not blockers:
                self.release_tranche(payment_id)
                progressed.append(PAYMENT_RELEASED)
        if p.state == PAYMENT_RELEASED and receipt_no is not None:
            self.record_bank_receipt(payment_id, receipt_no)
            progressed.append(PAYMENT_SETTLED)
        return {
            "payment_id": payment_id,
            "checkpoint": p.state,
            "progressed": progressed,
            "next": None if p.state == PAYMENT_SETTLED
                    else ("登记银行回执" if p.state == PAYMENT_RELEASED else "释放拨付"),
            "blockers": blockers if p.state == PAYMENT_RESERVED else [],
        }

    def recover(self, payment_id: str, amount, actor_id: str, reason: str,
                occurred_at: str | None = None) -> dict:
        """追回：追加事实修正余额，原 TRANCHE_RELEASED 结论保留。"""
        p = self._pay(payment_id)
        if p.state not in (PAYMENT_RELEASED, PAYMENT_SETTLED, PAYMENT_RECOVERED):
            raise ChainError("not-released", "资金尚未释放，无从追回")
        amt = money(amount)
        if p.recovered + amt > p.amount + Decimal("0.000001"):
            raise ChainError(
                "recovery-exceeded",
                f"累计追回 {p.recovered + amt} 超过已付 {p.amount}，超额追回不允许",
            )
        return self._emit("RECOVERY_RECONCILED", p.application_id, {
            "payment_id": payment_id,
            "recovered_amount": str(amt),
            "recovered_total": str(p.recovered + amt),
            "released_total": str(p.amount),
            "available_balance_after": str(
                self.available_balance(p.application_id) + amt),
            "recovered_by": actor_id,
            "reason": reason,
            "note": "追回为追加事实，原拨付事件保留，余额据全部事件重算",
        }, occurred_at)

    # —— 6. 批量：按条目隔离冲突 ——
    _BATCH_OPS = frozenset({
        "register_evidence", "define_milestone", "approve_milestone",
        "accept_milestone", "reserve_quota", "record_matching_funds",
    })

    def run_batch(self, items: list[dict]) -> list[BatchItem]:
        """逐条执行批量材料；任意一条失败只隔离该条（及对应项目），不连坐。"""
        report: list[BatchItem] = []
        for item in items:
            app_id = item.get("application_id")
            op = item.get("op")
            try:
                if op not in self._BATCH_OPS:
                    raise ChainError("unknown-op", f"批量操作不支持: {op}")
                result = getattr(self, op)(**{k: v for k, v in item.items()
                                              if k not in ("op",)})
                report.append(BatchItem(True, app_id, result=result))
            except ChainError as exc:
                report.append(BatchItem(False, app_id, error=str(exc),
                                        error_code=exc.code))
        return report

    # —— 余额与占用 ——
    def effective_budget(self, application_id: str) -> Decimal:
        app = self._app(application_id)
        return app.budget + sum((a["amount"] for a in app.adjustments), Decimal("0"))

    def _reservation_charge(self, p: _Payment) -> Decimal:
        """该付款自身预留对可用余额的占用（计算时避免把自己重复计入）。"""
        return p.amount if p.state == PAYMENT_RESERVED else Decimal("0")

    def active_reservations(self, application_id: str | None = None) -> list[dict]:
        rows = []
        for p in self.payments.values():
            if p.state != PAYMENT_RESERVED:
                continue
            if application_id is not None and p.application_id != application_id:
                continue
            rows.append({
                "payment_id": p.payment_id,
                "application_id": p.application_id,
                "milestone_id": p.milestone_id,
                "reserved_by": p.payer_id,
                "amount": str(p.amount),
            })
        return rows

    def released_total(self, application_id: str) -> Decimal:
        return sum(
            (p.amount for p in self.payments.values()
             if p.application_id == application_id
             and p.state in (PAYMENT_RELEASED, PAYMENT_SETTLED, PAYMENT_RECOVERED)),
            Decimal("0"),
        )

    def recovered_total(self, application_id: str) -> Decimal:
        return sum(
            (p.recovered for p in self.payments.values()
             if p.application_id == application_id),
            Decimal("0"),
        )

    def available_balance(self, application_id: str) -> Decimal:
        """有效预算 - 活跃预留 -（已拨付 - 已追回）。全部由追加事实重算。"""
        reserved = sum((money(r["amount"]) for r in self.active_reservations(application_id)),
                       Decimal("0"))
        return (self.effective_budget(application_id) - reserved
                - self.released_total(application_id) + self.recovered_total(application_id))

    def occupation_board(self) -> dict:
        """回答"额度被谁占用"：逐申请列出预算、预留、拨付、追回与可用余额。"""
        rows = []
        for app_id, app in self.applications.items():
            rows.append({
                "application_id": app_id,
                "applicant_id": app.applicant_id,
                "funding_source": app.funding_source,
                "budget": str(app.budget),
                "effective_budget": str(self.effective_budget(app_id)),
                "reserved": str(sum(
                    (money(r["amount"]) for r in self.active_reservations(app_id)),
                    Decimal("0"))),
                "released": str(self.released_total(app_id)),
                "recovered": str(self.recovered_total(app_id)),
                "available": str(self.available_balance(app_id)),
            })
        return {"applications": rows, "active_reservations": self.active_reservations()}

    # —— 7. 审计穿透：从一笔付款向前展示完整链路 ——
    def trace_payment(self, payment_id: str) -> dict:
        p = self._pay(payment_id)
        app = self._app(p.application_id)
        ms = self._ms(p.application_id, p.milestone_id)

        evidence_rows = []
        overlap_rows = []
        for ev_id in ms.evidence_ids:
            rec = self.evidence[(p.application_id, ev_id)]
            evidence_rows.append({
                "evidence_id": ev_id,
                "kind": rec["kind"],
                "fingerprint": rec["fingerprint"],
                "shared_with": [
                    {"application_id": a, "evidence_id": e}
                    for a, e in self.fingerprints[rec["fingerprint"]]
                    if (a, e) != (p.application_id, ev_id)
                ],
            })
            for flag in self._flags_for(p.application_id, rec["fingerprint"]):
                other_app, other_ev = self._counterparty(flag, p.application_id)
                decision = next((d for d in self.decisions
                                 if d["flag_id"] == flag["flag_id"]), None)
                overlap_rows.append({
                    "flag_id": flag["flag_id"],
                    "fingerprint": flag["fingerprint"],
                    "other_application_id": other_app,
                    "other_evidence_id": other_ev,
                    "system_flag": "OVERLAP_FLAGGED",
                    "status": flag["status"],
                    "decision": None if decision is None else {
                        "decision": decision["decision"],
                        "decided_by": decision["decided_by"],
                        "rationale": decision["rationale"],
                    },
                })

        adjustments = self._adjustment_timeline(p.application_id)
        return {
            "payment": {
                "payment_id": payment_id,
                "amount": str(p.amount),
                "state": p.state,
                "checkpoints": [
                    {"checkpoint": PAYMENT_RESERVED, "at": p.created_at, "by": p.payer_id},
                    {"checkpoint": PAYMENT_RELEASED,
                     "event_id": p.released_event_id,
                     "at": (self._event_by_id.get(p.released_event_id, {}) or {}).get("occurred_at")},
                    {"checkpoint": PAYMENT_SETTLED, "receipt_no": p.receipt_no,
                     "event_id": p.receipt_event_id},
                ],
                "recovered_total": str(p.recovered),
            },
            "budget_source": {
                "application_id": p.application_id,
                "applicant_id": app.applicant_id,
                "funding_source": app.funding_source,
                "budget": str(app.budget),
                "effective_budget": str(self.effective_budget(p.application_id)),
                "use_restrictions": sorted(app.use_restrictions),
                "related_parties": sorted(app.related_parties),
                "other_commitments": app.commitments,
                "matching_required": str(self._matching_required(p.application_id)),
                "matching_received": str(app.matching_received),
            },
            "evidence": evidence_rows,
            "overlap_findings": overlap_rows,
            "milestone": {
                "milestone_id": p.milestone_id,
                "protocol_id": ms.protocol_id,
                "acceptor_id": ms.acceptor_id,
                "release_ratio": str(ms.release_ratio),
                "purpose": ms.purpose,
                "preconditions": sorted(ms.preconditions),
                "approved_by": ms.approved_by,
                "accepted_cumulative_ratio": str(
                    self.accepted_ratio(p.application_id, p.milestone_id)),
                "acceptances": [
                    {"delta": str(a["delta"]), "cumulative": str(a["cumulative"]),
                     "by": a["by"], "at": a["at"], "event_id": a["event_id"]}
                    for a in ms.acceptances
                ],
                "duty_chain": {
                    "approver": ms.approved_by,
                    "acceptor": ms.acceptor_id,
                    "payer": p.payer_id,
                    "three_distinct_roles": len(
                        {ms.approved_by, ms.acceptor_id, p.payer_id}) == 3,
                },
            },
            "adjustments": adjustments,
            "balance_now": {
                "reserved_by_others": str(
                    sum((money(r["amount"]) for r in self.active_reservations(p.application_id)),
                        Decimal("0")) - (p.amount if p.state == PAYMENT_RESERVED else Decimal("0"))),
                "released_total": str(self.released_total(p.application_id)),
                "recovered_total": str(self.recovered_total(p.application_id)),
                "available": str(self.available_balance(p.application_id)),
            },
        }

    # —— 内部 ——
    def _app(self, application_id: str) -> _Application:
        app = self.applications.get(application_id)
        if app is None:
            raise ChainError("application-not-found", f"申请不存在: {application_id}")
        return app

    def _ms(self, application_id: str, milestone_id: str) -> _Milestone:
        ms = self.milestones.get((application_id, milestone_id))
        if ms is None:
            raise ChainError("milestone-not-found",
                             f"里程碑不存在: {application_id}/{milestone_id}")
        return ms

    def _pay(self, payment_id: str) -> _Payment:
        p = self.payments.get(payment_id)
        if p is None:
            raise ChainError("payment-not-found", f"付款不存在: {payment_id}")
        return p

    def _matching_required(self, application_id: str) -> Decimal:
        app = self._app(application_id)
        return sum((money(c.get("amount", 0)) for c in app.commitments
                    if c.get("source") == "ENTERPRISE_MATCHING"), Decimal("0"))

    def _flags_for(self, application_id: str, fingerprint: str) -> list[dict]:
        return [f for f in self.flags.values()
                if f["fingerprint"] == fingerprint
                and (f["later_application_id"] == application_id
                     or f["first_application_id"] == application_id)]

    def _counterparty(self, flag: dict, application_id: str) -> tuple[str, str]:
        """返回重叠提示中相对于指定申请的对端（申请、证据）。"""
        if flag["first_application_id"] == application_id:
            return flag["later_application_id"], flag["later_evidence_id"]
        return flag["first_application_id"], flag["first_evidence_id"]

    def _releasable_capacity(self, ms: _Milestone) -> Decimal:
        app = self._app(ms.application_id)
        cap = app.budget + sum((a["amount"] for a in app.adjustments), Decimal("0"))
        cap *= ms.release_ratio * self.accepted_ratio(ms.application_id, ms.milestone_id)
        used = sum(
            (p.amount for p in self.payments.values()
             if p.application_id == ms.application_id
             and p.milestone_id == ms.milestone_id
             and p.state in (PAYMENT_RELEASED, PAYMENT_SETTLED, PAYMENT_RECOVERED)),
            Decimal("0"),
        )
        return (cap - used).quantize(_CENT)

    def _adjustment_timeline(self, application_id: str) -> list[dict]:
        """按事件顺序列出修正余额的追加事实，并给出每步后的可用余额。"""
        watch = {
            "MATCHING_FUNDS_RECEIVED", "MILESTONE_ACCEPTED", "ENTITY_RESTRUCTURED",
            "TRANCHE_RELEASED", "QUOTA_RESERVED", "RECOVERY_RECONCILED",
        }
        lines = []
        for event in self.events:
            if event["subject_id"] != application_id or event["kind"] not in watch:
                continue
            lines.append({
                "seq": event["seq"],
                "event_id": event["event_id"],
                "kind": event["kind"],
                "at": event["occurred_at"],
                "payload": event["payload"],
            })
        return lines

    # —— 折叠：从事件重建全部状态 ——
    def _fold(self, event: dict) -> None:
        self._seq += 1
        event["seq"] = self._seq
        kind = event["kind"]
        app_id = event["subject_id"]
        p = event["payload"]

        if kind == "APPLICATION_SUBMITTED":
            self.applications[app_id] = _Application(
                application_id=app_id,
                applicant_id=p["applicant_id"],
                funding_source=p["funding_source"],
                budget=money(p["budget"]),
                use_restrictions=frozenset(p.get("use_restrictions", [])),
                related_parties=tuple(p.get("related_parties", [])),
                commitments=list(p.get("other_commitments", [])),
            )
        elif kind == "EVIDENCE_REGISTERED":
            self.evidence[(app_id, p["evidence_id"])] = {
                "kind": p["evidence_kind"],
                "fingerprint": p["fingerprint"],
                "canonical_fields": p["canonical_fields"],
            }
            self.fingerprints.setdefault(p["fingerprint"], []).append(
                (app_id, p["evidence_id"]))
        elif kind == "OVERLAP_FLAGGED":
            self.flags.setdefault(p["flag_id"], dict(p))
        elif kind == "OVERLAP_DECIDED":
            flag = self.flags.get(p["flag_id"])
            if flag is not None:
                flag["status"] = p["decision"]
            if not any(d["flag_id"] == p["flag_id"] for d in self.decisions):
                self.decisions.append(dict(p))
        elif kind == "MILESTONE_DEFINED":
            self.milestones[(app_id, p["milestone_id"])] = _Milestone(
                application_id=app_id,
                milestone_id=p["milestone_id"],
                protocol_id=p["protocol_id"],
                acceptor_id=p["acceptor_id"],
                release_ratio=ratio(p["release_ratio"]),
                purpose=p["purpose"],
                preconditions=frozenset(p.get("preconditions", [])),
                evidence_ids=list(p.get("evidence_ids", [])),
            )
        elif kind == "MILESTONE_APPROVED":
            self.milestones[(app_id, p["milestone_id"])].approved_by = p["approved_by"]
            self.milestones[(app_id, p["milestone_id"])].approved_at = event["occurred_at"]
        elif kind == "MILESTONE_ACCEPTED":
            ms = self.milestones[(app_id, p["milestone_id"])]
            ms.acceptances.append({
                "delta": ratio(p["accepted_delta_ratio"]),
                "cumulative": ratio(p["accepted_cumulative_ratio"]),
                "by": p["accepted_by"],
                "at": event["occurred_at"],
                "event_id": event["event_id"],
            })
        elif kind == "MATCHING_FUNDS_RECEIVED":
            self.applications[app_id].matching_received += money(p["amount"])
        elif kind == "ENTITY_RESTRUCTURED":
            # 调整金额需由前后余额反推，保证只追加重放结果一致
            app = self.applications[app_id]
            app.adjustments.append({
                "amount": money(p["budget_adjustment"]),
                "by": p.get("recorded_by"),
                "note": p.get("note", ""),
                "at": event["occurred_at"],
                "event_id": event["event_id"],
            })
        elif kind == "QUOTA_RESERVED":
            pid = p["payment_id"]
            if pid not in self.payments:
                self.payments[pid] = _Payment(
                    payment_id=pid, application_id=app_id,
                    milestone_id=p["milestone_id"],
                    payer_id=p["reserved_by"], amount=money(p["amount"]),
                    state=PAYMENT_RESERVED, created_at=event["occurred_at"],
                )
            # 预留命令按 (申请,里程碑,幂等键) 去重，重放后仍可识别同一请求
            idem_key = pid.rsplit(":", 1)[-1]
            self._idempotency.setdefault(
                ("reserve", app_id, p["milestone_id"], idem_key), event["event_id"])
        elif kind == "TRANCHE_RELEASED":
            pay = self.payments[p["payment_id"]]
            pay.state = PAYMENT_RELEASED
            pay.released_event_id = event["event_id"]
        elif kind == "BANK_RECEIPT_RECORDED":
            pay = self.payments[p["payment_id"]]
            pay.state = PAYMENT_SETTLED
            pay.receipt_no = p["receipt_no"]
            pay.receipt_event_id = event["event_id"]
            self._receipts.setdefault(p["receipt_no"], pay.payment_id)
        elif kind == "RECOVERY_RECONCILED":
            pay = self.payments[p["payment_id"]]
            pay.recovered += money(p["recovered_amount"])
            if pay.recovered >= pay.amount - Decimal("0.000001"):
                pay.state = PAYMENT_RECOVERED

    @property
    def _event_by_id(self) -> dict[str, dict]:
        return {e["event_id"]: e for e in self.events}
