"""拨付链测试：每条用例对应审计叙述中的一个风险点。

用例编号与审计发现对应：
* 1-3  同一张设备发票跨"扶持资金 / 银行贷款"申请复用 → 指纹提示 + 授权裁决
* 4    已验收但企业配套款未到账 → 拦截；配套款迟到 → 追加事实解锁
* 5    额度被谁占用 → 预留占用台账与可用余额
* 6    审批 / 验收 / 付款三权分立
* 7    部分验收 → 按累计验收比例释放，全部只追加
* 8    企业重组 → 追加预算调整修正余额，原结论不覆盖
* 9    追回 → 追加修正余额，超额追回拒绝
* 10   银行回执重放 → 幂等，不再次拨付；回执号不得跨付款复用
* 11   执行中断 → 从资金动作检查点续作
* 12   批量材料冲突 → 只隔离对应项目/条目
* 13   审计穿透视图
* 14   事件流可整体重放，状态一致
* 15   用途限制
* 16   指纹稳定性与重复提交检测
"""

import unittest
from decimal import Decimal

from src.disbursement_chain import (
    DisbursementChain,
    ChainError,
    evidence_fingerprint,
    EVIDENCE_INVOICE,
    EVIDENCE_CONTRACT,
    EVIDENCE_DELIVERABLE,
    ROLE_APPROVER,
    ROLE_ACCEPTOR,
    ROLE_PAYER,
    ROLE_OVERLAP_OFFICER,
    CONDITION_MATCHING_FUNDS,
    OVERLAP_OPEN,
    OVERLAP_ALLOWED,
    OVERLAP_DENIED,
    PAYMENT_RESERVED,
    PAYMENT_RELEASED,
    PAYMENT_SETTLED,
    PAYMENT_RECOVERED,
)

T = lambda n: f"2026-09-{n:02d}T09:00:00+08:00"

INVOICE_FIELDS = {
    "invoice_code": "044001900111",
    "invoice_no": "00287654",
    "seller": "华东数控设备有限公司",
    "buyer": "demo-011",
    "issued_at": "2026-09-01",
    "amount": "300000.00",
}


def build_chain(with_invoice_shared=True):
    """构造双申请基线：app-grant 扶持资金、app-loan 银行贷款，同一家企业。"""
    c = DisbursementChain()
    for aid, role in [
        ("apv", ROLE_APPROVER),
        ("acc", ROLE_ACCEPTOR),
        ("pay", ROLE_PAYER),
        ("off", ROLE_OVERLAP_OFFICER),
    ]:
        c.register_actor(aid, role)

    c.submit_application(
        "app-grant", "demo-011", "1000000", "GRANT",
        use_restrictions=["EQUIPMENT", "SERVICE"],
        related_parties=["bank-alpha"],
        other_commitments=[
            {"source": "BANK_LOAN", "amount": "500000", "status": "PROMISED"},
        ],
        matching_required="200000",
        occurred_at=T(1),
    )
    c.submit_application(
        "app-loan", "demo-011", "500000", "BANK_LOAN",
        use_restrictions=["EQUIPMENT"],
        occurred_at=T(1),
    )
    if with_invoice_shared:
        share_invoice(c, "app-grant", "inv-shared", T(2))
        share_invoice(c, "app-loan", "inv-shared", T(3))
    return c


def share_invoice(chain, app_id, ev_id, when):
    return chain.register_evidence(app_id, ev_id, EVIDENCE_INVOICE,
                                   dict(INVOICE_FIELDS), occurred_at=when)


def grant_milestone(chain, ratio="0.5", preconditions=None, ev_ids=("inv-shared",),
                    purpose="EQUIPMENT"):
    chain.define_milestone(
        "app-grant", "m1", "proto-2026-A", "acc", ratio, purpose,
        preconditions=[CONDITION_MATCHING_FUNDS] if preconditions is None else preconditions,
        evidence_ids=list(ev_ids), occurred_at=T(4),
    )
    chain.approve_milestone("app-grant", "m1", "apv", occurred_at=T(5))
    chain.reserve_quota("app-grant", "m1", "500000", "pay", "k-m1",
                        occurred_at=T(7))


def fully_accept(chain, when=T(6)):
    chain.accept_milestone("app-grant", "m1", "1", "acc", occurred_at=when)


def single_app_chain():
    """无重叠的单申请链，用于职责分离、部分验收等用例。"""
    c = DisbursementChain()
    for aid, role in [
        ("apv", ROLE_APPROVER),
        ("acc", ROLE_ACCEPTOR),
        ("pay", ROLE_PAYER),
        ("off", ROLE_OVERLAP_OFFICER),
    ]:
        c.register_actor(aid, role)
    c.submit_application("app-x", "ent-x", "800000", "GRANT",
                         use_restrictions=["EQUIPMENT"], matching_required="100000",
                         occurred_at=T(1))
    c.register_evidence("app-x", "inv-x", EVIDENCE_INVOICE,
                        {**INVOICE_FIELDS, "invoice_no": "99999999"}, occurred_at=T(2))
    return c


class FingerprintTest(unittest.TestCase):
    def test_fingerprint_is_stable_and_format_insensitive(self):
        # 空白差异、键序差异不影响指纹
        fp1 = evidence_fingerprint(EVIDENCE_INVOICE, {
            "invoice_no": "123", "seller": " 设备公司\n", "amount": "100.00",
        })
        fp2 = evidence_fingerprint(EVIDENCE_INVOICE, {
            "amount": "100.00", "invoice_no": "123", "seller": "设备公司",
        })
        self.assertEqual(fp1, fp2)
        self.assertTrue(fp1.startswith("sha256:"))
        # 任一业务字段变化都会改变指纹
        fp3 = evidence_fingerprint(EVIDENCE_INVOICE, {
            "amount": "100.01", "invoice_no": "123", "seller": "设备公司",
        })
        self.assertNotEqual(fp1, fp3)
        # 证据种类也是指纹的一部分
        fp4 = evidence_fingerprint(EVIDENCE_CONTRACT, {"x": 1})
        fp5 = evidence_fingerprint(EVIDENCE_DELIVERABLE, {"x": 1})
        self.assertNotEqual(fp4, fp5)

    def test_duplicate_evidence_id_rejected(self):
        c = single_app_chain()
        with self.assertRaises(ChainError) as ctx:
            c.register_evidence("app-x", "inv-x", EVIDENCE_INVOICE,
                                {**INVOICE_FIELDS, "invoice_no": "99999999"})
        self.assertEqual(ctx.exception.code, "evidence-exists")


class OverlapAndDecisionTest(unittest.TestCase):
    def test_same_invoice_flags_cross_application_overlap(self):
        c = build_chain()
        # 同一张发票在扶持资金与银行贷款两个申请中 → 一个 OPEN 提示
        flags = list(c.flags.values())
        self.assertEqual(len(flags), 1)
        flag = flags[0]
        self.assertEqual(flag["first_application_id"], "app-grant")
        self.assertEqual(flag["later_application_id"], "app-loan")
        self.assertEqual(flag["status"], OVERLAP_OPEN)
        # 审计台账：同一指纹被两个申请持有
        board = c.occupation_board()
        self.assertEqual(len(board["applications"]), 2)

    def test_open_overlap_blocks_release_on_both_sides(self):
        c = build_chain()
        grant_milestone(c)
        fully_accept(c)
        blockers = c.release_blockers("pay:app-grant:m1:k-m1")
        self.assertTrue(any("共用同一指纹" in b for b in blockers), blockers)
        # 贷款侧也定义一个里程碑，同样被未裁决重叠拦住
        c.define_milestone("app-loan", "ml", "proto-L", "acc", "0.6", "EQUIPMENT",
                           evidence_ids=["inv-shared"], occurred_at=T(4))
        c.approve_milestone("app-loan", "ml", "apv", occurred_at=T(5))
        c.accept_milestone("app-loan", "ml", "1", "acc", occurred_at=T(6))
        c.reserve_quota("app-loan", "ml", "300000", "pay", "k-ml", occurred_at=T(7))
        blockers_loan = c.release_blockers("pay:app-loan:ml:k-ml")
        self.assertTrue(any("共用同一指纹" in b for b in blockers_loan), blockers_loan)

    def _settled_pair(self, decision=OVERLAP_ALLOWED, rationale=None):
        c = build_chain()
        grant_milestone(c)
        fully_accept(c)
        flag_id = next(iter(c.flags))
        c.decide_overlap(
            flag_id, decision, "off",
            rationale or "经核验贷款与扶持资金为不同资金渠道，同一台设备的发票不构成重复列支",
            occurred_at=T(8),
        )
        c.record_matching_funds("app-grant", "200000", occurred_at=T(9))
        return c, flag_id

    def test_authorized_allowed_decision_unblocks_and_is_immutable(self):
        c, flag_id = self._settled_pair(OVERLAP_ALLOWED)
        self.assertEqual(c.flags[flag_id]["status"], OVERLAP_ALLOWED)
        self.assertEqual(c.release_blockers("pay:app-grant:m1:k-m1"), [])
        # 已裁决的提示不能再改判
        with self.assertRaises(ChainError) as ctx:
            c.decide_overlap(flag_id, OVERLAP_DENIED, "off", "改判")
        self.assertEqual(ctx.exception.code, "overlap-decided")
        # 裁决人角色不对 / 无理由都不允许
        c2 = build_chain()
        grant_milestone(c2)
        fully_accept(c2)
        with self.assertRaises(ChainError) as ctx:
            c2.decide_overlap(next(iter(c2.flags)), OVERLAP_ALLOWED, "apv", "x")
        self.assertEqual(ctx.exception.code, "role-required")
        with self.assertRaises(ChainError) as ctx:
            c2.decide_overlap(next(iter(c2.flags)), OVERLAP_ALLOWED, "off", "   ")
        self.assertEqual(ctx.exception.code, "rationale-required")

    def test_denied_decision_blocks_only_the_reusing_application(self):
        # 裁决：该发票已在扶持资金中列支，贷款侧复用属于重复，拒绝贷款侧
        c = build_chain()
        grant_milestone(c)
        fully_accept(c)
        flag_id = next(iter(c.flags))
        c.decide_overlap(flag_id, OVERLAP_DENIED, "off",
                         "该设备发票已在扶持资金中列支，再申请贷款属于重复融资",
                         occurred_at=T(8))
        c.record_matching_funds("app-grant", "200000", occurred_at=T(9))
        # 原始登记方（grant）不受拒绝连坐，可以释放
        self.assertEqual(c.release_blockers("pay:app-grant:m1:k-m1"), [])
        # 复用方（loan）被拒，不能释放
        c.define_milestone("app-loan", "ml", "proto-L", "acc", "0.6", "EQUIPMENT",
                           evidence_ids=["inv-shared"], occurred_at=T(4))
        c.approve_milestone("app-loan", "ml", "apv", occurred_at=T(5))
        c.accept_milestone("app-loan", "ml", "1", "acc", occurred_at=T(6))
        c.reserve_quota("app-loan", "ml", "300000", "pay", "k-ml", occurred_at=T(7))
        blockers = c.release_blockers("pay:app-loan:ml:k-ml")
        self.assertTrue(any("复用已被裁决拒绝" in b for b in blockers), blockers)
        with self.assertRaises(ChainError) as ctx:
            c.release_tranche("pay:app-loan:ml:k-ml")
        self.assertEqual(ctx.exception.code, "release-blocked")

    def test_decide_unknown_flag(self):
        c = build_chain()
        with self.assertRaises(ChainError) as ctx:
            c.decide_overlap("ovlp:nope", OVERLAP_ALLOWED, "off", "x")
        self.assertEqual(ctx.exception.code, "flag-not-found")


class MatchingFundsTest(unittest.TestCase):
    def test_accepted_but_matching_funds_missing_blocks_release(self):
        """已验收但企业配套款未到账不应放款。"""
        c = build_chain()
        grant_milestone(c)
        fully_accept(c)
        # 重叠先裁决允许，排除干扰，只剩配套款一个阻碍
        c.decide_overlap(next(iter(c.flags)), OVERLAP_ALLOWED, "off",
                         "不同渠道，允许", occurred_at=T(8))
        blockers = c.release_blockers("pay:app-grant:m1:k-m1")
        self.assertEqual(len(blockers), 1)
        self.assertIn("配套款", blockers[0])
        with self.assertRaises(ChainError) as ctx:
            c.release_tranche("pay:app-grant:m1:k-m1")
        self.assertEqual(ctx.exception.code, "release-blocked")
        self.assertIn("配套款", " ".join(ctx.exception.blockers))

    def test_late_matching_funds_is_appended_fact_that_unblocks(self):
        """配套款迟到账只是追加事实，原验收结论不变，随后自动满足条件。"""
        c = build_chain()
        grant_milestone(c)
        fully_accept(c, when=T(6))
        c.decide_overlap(next(iter(c.flags)), OVERLAP_ALLOWED, "off",
                         "不同渠道，允许", occurred_at=T(8))
        # 验收后第 20 天才到账（晚于里程碑定义与验收）
        c.record_matching_funds("app-grant", "200000", occurred_at=T(26),
                                note="企业自筹迟于验收到账")
        blockers = c. release_blockers("pay:app-grant:m1:k-m1")
        self.assertEqual(blockers, [])
        # 原验收事件仍然存在、结论未被覆盖
        kinds = [e["kind"] for e in c.events]
        self.assertIn("MILESTONE_ACCEPTED", kinds)
        accepted = [e for e in c.events if e["kind"] == "MILESTONE_ACCEPTED"]
        self.assertEqual(accepted[0]["payload"]["accepted_cumulative_ratio"], "1")
        # 到账事件晚于验收事件
        receipt = [e for e in c.events if e["kind"] == "MATCHING_FUNDS_RECEIVED"][0]
        self.assertGreater(receipt["seq"], accepted[0]["seq"])
        # 可用余额扣除预留
        self.assertEqual(c.available_balance("app-grant"), Decimal("500000"))

    def test_partial_matching_funds_still_blocks(self):
        c = build_chain()
        grant_milestone(c)
        fully_accept(c)
        c.decide_overlap(next(iter(c.flags)), OVERLAP_ALLOWED, "off", "允许")
        c.record_matching_funds("app-grant", "120000")  # 还差 8 万
        blockers = c.release_blockers("pay:app-grant:m1:k-m1")
        self.assertTrue(any("配套款" in b for b in blockers))


class QuotaOccupationTest(unittest.TestCase):
    def test_reservation_occupies_and_insufficient_quota_rejected(self):
        c = build_chain(with_invoice_shared=False)
        c.define_milestone("app-grant", "m1", "proto", "acc", "0.5", "EQUIPMENT",
                           preconditions=[], evidence_ids=[], occurred_at=T(4))
        c.approve_milestone("app-grant", "m1", "apv", occurred_at=T(5))
        fully_accept(c)
        # 预算 100 万；里程碑容量 = 100万×0.5×1 = 50万
        c.reserve_quota("app-grant", "m1", "500000", "pay", "k1", occurred_at=T(7))
        self.assertEqual(c.active_reservations("app-grant")[0]["payment_id"],
                         "pay:app-grant:m1:k1")
        # 第二个里程碑尝试再占 60 万 → 可用余额只有 50 万，拒绝
        c.define_milestone("app-grant", "m2", "proto", "acc", "0.6", "EQUIPMENT",
                           preconditions=[], occurred_at=T(8))
        c.approve_milestone("app-grant", "m2", "apv", occurred_at=T(9))
        c.accept_milestone("app-grant", "m2", "1", "acc", occurred_at=T(10))
        with self.assertRaises(ChainError) as ctx:
            c.reserve_quota("app-grant", "m2", "600000", "pay", "k2", occurred_at=T(11))
        self.assertEqual(ctx.exception.code, "quota-unavailable")
        # 占用台账能回答"额度被谁占用"
        board = c.occupation_board()
        row = next(r for r in board["applications"] if r["application_id"] == "app-grant")
        self.assertEqual(row["reserved"], "500000.00")
        self.assertEqual(row["available"], "500000.00")
        # 预留幂等：同一幂等键重放不产生第二笔
        ev_count = len(c.events)
        e1 = c.reserve_quota("app-grant", "m1", "500000", "pay", "k1")
        self.assertEqual(len(c.events), ev_count)
        self.assertEqual(e1["kind"], "QUOTA_RESERVED")

    def test_reservation_idempotency_after_replay(self):
        c = build_chain(with_invoice_shared=False)
        c.define_milestone("app-grant", "m1", "proto", "acc", "0.5", "EQUIPMENT",
                           preconditions=[], evidence_ids=[], occurred_at=T(4))
        c.approve_milestone("app-grant", "m1", "apv", occurred_at=T(5))
        fully_accept(c)
        c.reserve_quota("app-grant", "m1", "500000", "pay", "k1", occurred_at=T(7))
        c2 = DisbursementChain.replay(c.events)
        # 角色目录来自外部 IAM，不在事件流内；重放后重新登记再发命令
        for aid, role in [("apv", ROLE_APPROVER), ("acc", ROLE_ACCEPTOR),
                          ("pay", ROLE_PAYER), ("off", ROLE_OVERLAP_OFFICER)]:
            c2.register_actor(aid, role)
        before = len(c2.events)
        c2.reserve_quota("app-grant", "m1", "500000", "pay", "k1")
        self.assertEqual(len(c2.events), before)  # 重放后幂等仍生效

    def test_allowed_overlap_does_not_consume_each_others_quota_across_apps(self):
        """允许的跨渠道重叠不应互相占用同一申请内额度（分属不同预算池）。"""
        c = build_chain()
        grant_milestone(c)
        fully_accept(c)
        c.decide_overlap(next(iter(c.flags)), OVERLAP_ALLOWED, "off", "允许")
        c.record_matching_funds("app-grant", "200000")
        c.release_tranche("pay:app-grant:m1:k-m1")
        # grant 池扣 50 万，loan 池原封不动
        self.assertEqual(c.available_balance("app-grant"), Decimal("500000"))
        self.assertEqual(c.available_balance("app-loan"), Decimal("500000"))

    def test_recovery_frees_quota(self):
        c, _ = self._setup_released()
        self.assertEqual(c.available_balance("app-grant"), Decimal("500000"))
        c.recover("pay:app-grant:m1:k-m1", "100000", "off", "部分退货", occurred_at=T(12))
        self.assertEqual(c.available_balance("app-grant"), Decimal("600000"))
        board = c.occupation_board()
        row = next(r for r in board["applications"] if r["application_id"] == "app-grant")
        self.assertEqual(row["recovered"], "100000.00")

    def _setup_released(self):
        c = build_chain()
        grant_milestone(c)
        fully_accept(c)
        c.decide_overlap(next(iter(c.flags)), OVERLAP_ALLOWED, "off", "允许")
        c.record_matching_funds("app-grant", "200000")
        c.release_tranche("pay:app-grant:m1:k-m1")
        return c, None


class SeparationOfDutiesTest(unittest.TestCase):
    """一人一角色，审批/验收/付款账号必须两两不同。"""

    def test_approve_requires_approver_role(self):
        c = single_app_chain()
        c.define_milestone("app-x", "m", "proto", "acc", "0.5", "EQUIPMENT",
                           preconditions=[], evidence_ids=["inv-x"], occurred_at=T(3))
        # acc 是验收角色，不能执行审批
        with self.assertRaises(ChainError) as ctx:
            c.approve_milestone("app-x", "m", "acc")
        self.assertEqual(ctx.exception.code, "role-required")
        # pay 付款角色也不能审批
        with self.assertRaises(ChainError) as ctx:
            c.approve_milestone("app-x", "m", "pay")
        self.assertEqual(ctx.exception.code, "role-required")

    def test_acceptor_must_be_acceptor_role(self):
        c = single_app_chain()
        with self.assertRaises(ChainError) as ctx:
            c.define_milestone("app-x", "m", "proto", "apv", "0.5", "EQUIPMENT",
                               preconditions=[])
        self.assertEqual(ctx.exception.code, "role-required")

    def test_payer_must_be_payer_role(self):
        c = single_app_chain()
        c.define_milestone("app-x", "m", "proto", "acc", "0.5", "EQUIPMENT",
                           preconditions=[], evidence_ids=["inv-x"], occurred_at=T(3))
        c.approve_milestone("app-x", "m", "apv", occurred_at=T(4))
        c.accept_milestone("app-x", "m", "1", "acc", occurred_at=T(5))
        with self.assertRaises(ChainError) as ctx:
            c.reserve_quota("app-x", "m", "100000", "apv", "k")
        self.assertEqual(ctx.exception.code, "role-required")

    def test_overlap_decision_requires_dedicated_officer(self):
        c = build_chain()
        # 审批人无权对跨申请重叠作裁决
        with self.assertRaises(ChainError) as ctx:
            c.decide_overlap(next(iter(c.flags)), OVERLAP_ALLOWED, "apv", "理由")
        self.assertEqual(ctx.exception.code, "role-required")

    def test_three_distinct_actors_duty_chain_in_trace(self):
        c = single_app_chain()
        c.define_milestone("app-x", "m", "proto", "acc", "0.5", "EQUIPMENT",
                           preconditions=[], evidence_ids=["inv-x"], occurred_at=T(3))
        c.approve_milestone("app-x", "m", "apv", occurred_at=T(4))
        c.accept_milestone("app-x", "m", "1", "acc", occurred_at=T(5))
        c.reserve_quota("app-x", "m", "400000", "pay", "k", occurred_at=T(6))
        c.release_tranche("pay:app-x:m:k")
        chain = c.trace_payment("pay:app-x:m:k")["milestone"]["duty_chain"]
        self.assertTrue(chain["three_distinct_roles"])
        self.assertEqual((chain["approver"], chain["acceptor"], chain["payer"]),
                         ("apv", "acc", "pay"))

    def test_release_requires_prior_approval(self):
        c = single_app_chain()
        c.define_milestone("app-x", "m", "proto", "acc", "0.5", "EQUIPMENT",
                           preconditions=[], evidence_ids=["inv-x"], occurred_at=T(3))
        c.accept_milestone("app-x", "m", "1", "acc", occurred_at=T(5))
        c.reserve_quota("app-x", "m", "100000", "pay", "k")
        blockers = c.release_blockers("pay:app-x:m:k")
        self.assertTrue(any("尚未经审批人批准" in b for b in blockers))


class PartialAcceptanceTest(unittest.TestCase):
    def test_partial_acceptance_capacity_and_appends(self):
        c = single_app_chain()
        # 预算 80 万、释放比例 0.5 → 全验收容量 40 万；先验收 0.5 → 可释放 20 万
        c.define_milestone("app-x", "m", "proto", "acc", "0.5", "EQUIPMENT",
                           preconditions=[], evidence_ids=["inv-x"], occurred_at=T(3))
        c.approve_milestone("app-x", "m", "apv", occurred_at=T(4))
        c.accept_milestone("app-x", "m", "0.5", "acc", occurred_at=T(5),
                           note="首批设备到位")
        c.reserve_quota("app-x", "m", "200000", "pay", "k1", occurred_at=T(6))
        self.assertEqual(c.release_blockers("pay:app-x:m:k1"), [])
        c.release_tranche("pay:app-x:m:k1")
        # 试图把同一里程碑再付 25 万（剩余容量只有 20 万）→ 容量不足拦截
        c.reserve_quota("app-x", "m", "250000", "pay", "k2", occurred_at=T(7))
        blockers = c.release_blockers("pay:app-x:m:k2")
        self.assertTrue(any("可释放额度" in b for b in blockers), blockers)
        # 追加验收剩余 0.5（不覆盖首次结论）→ 容量补齐至 40 万
        c.accept_milestone("app-x", "m", "0.5", "acc", occurred_at=T(8),
                           note="第二批到位，追加验收")
        self.assertEqual(c.accepted_ratio("app-x", "m"), Decimal("1"))
        blockers = c.release_blockers("pay:app-x:m:k2")
        # 25 万仍超过剩余容量 20 万（40-20）
        self.assertTrue(any("可释放额度" in b for b in blockers), blockers)
        # 另起一笔正好用满剩余容量 20 万
        c.reserve_quota("app-x", "m", "200000", "pay", "k3", occurred_at=T(9))
        self.assertEqual(c.release_blockers("pay:app-x:m:k3"), [])
        # 两次验收记录都在，cumulative 0.5 → 1，首次结论未被覆盖
        accepts = [e for e in c.events if e["kind"] == "MILESTONE_ACCEPTED"]
        self.assertEqual([a["payload"]["accepted_cumulative_ratio"] for a in accepts],
                         ["0.5", "1"])
        self.assertEqual([a["payload"]["accepted_delta_ratio"] for a in accepts],
                         ["0.5", "0.5"])
        self.assertEqual(accepts[0]["payload"]["partial"], True)

    def test_over_acceptance_rejected(self):
        c = single_app_chain()
        c.define_milestone("app-x", "m", "proto", "acc", "0.5", "EQUIPMENT",
                           preconditions=[], occurred_at=T(3))
        c.approve_milestone("app-x", "m", "apv", occurred_at=T(4))
        c.accept_milestone("app-x", "m", "0.8", "acc", occurred_at=T(5))
        with self.assertRaises(ChainError) as ctx:
            c.accept_milestone("app-x", "m", "0.5", "acc", occurred_at=T(6))
        self.assertEqual(ctx.exception.code, "acceptance-exceeded")

    def test_acceptance_by_wrong_actor_rejected(self):
        c = single_app_chain()
        c.define_milestone("app-x", "m", "proto", "acc", "0.5", "EQUIPMENT",
                           preconditions=[], occurred_at=T(3))
        c.register_actor("other-acc", ROLE_ACCEPTOR)
        with self.assertRaises(ChainError) as ctx:
            c.accept_milestone("app-x", "m", "0.5", "other-acc")
        self.assertEqual(ctx.exception.code, "acceptor-mismatch")


class RestructuringTest(unittest.TestCase):
    def test_restructuring_adjusts_budget_by_appending(self):
        c = single_app_chain()
        # 初始有效预算 80 万
        self.assertEqual(c.effective_budget("app-x"), Decimal("800000"))
        # 企业合并，专项资金池核减 30 万
        c.record_restructuring("app-x", "-300000", "apv",
                               "吸收合并后核减对应专项预算", occurred_at=T(9))
        self.assertEqual(c.effective_budget("app-x"), Decimal("500000"))
        # 再追加核增（新一轮配套承诺落实）
        c.record_restructuring("app-x", "100000", "apv",
                               "重组完成后预算恢复一部分", occurred_at=T(10))
        self.assertEqual(c.effective_budget("app-x"), Decimal("600000"))
        # 原申请事件原样保留
        sub = [e for e in c.events if e["kind"] == "APPLICATION_SUBMITTED"][0]
        self.assertEqual(sub["payload"]["budget"], "800000.00")
        # 两次调整均作为独立事件存在
        adjs = [e for e in c.events if e["kind"] == "ENTITY_RESTRUCTURED"]
        self.assertEqual(len(adjs), 2)

    def test_restructuring_cannot_push_budget_below_zero(self):
        c = single_app_chain()
        with self.assertRaises(ChainError) as ctx:
            c.record_restructuring("app-x", "-900000", "apv", "x")
        self.assertEqual(ctx.exception.code, "adjustment-below-zero")


class RecoveryTest(unittest.TestCase):
    def _ready(self):
        c = build_chain()
        grant_milestone(c)
        fully_accept(c)
        c.decide_overlap(next(iter(c.flags)), OVERLAP_ALLOWED, "off", "允许")
        c.record_matching_funds("app-grant", "200000")
        c.release_tranche("pay:app-grant:m1:k-m1")
        return c

    def test_recovery_is_appended_and_original_conclusion_kept(self):
        c = self._ready()
        released = [e for e in c.events if e["kind"] == "TRANCHE_RELEASED"]
        self.assertEqual(len(released), 1)
        c.recover("pay:app-grant:m1:k-m1", "120000", "off", "验收复核发现设备清单与发票不符",
                  occurred_at=T(15))
        # 原拨付事件仍在且只有一笔；追回独立成事件
        self.assertEqual(len([e for e in c.events if e["kind"] == "TRANCHE_RELEASED"]), 1)
        recs = [e for e in c.events if e["kind"] == "RECOVERY_RECONCILED"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["payload"]["recovered_total"], "120000.00")
        # 部分追回不改 SETTLED 状态名以外的原结论；付款仍记录原拨额
        p = c.payments["pay:app-grant:m1:k-m1"]
        self.assertEqual(p.amount, Decimal("500000"))
        self.assertEqual(p.recovered, Decimal("120000"))
        # 部分追回后状态仍可继续追回至全额
        c.recover("pay:app-grant:m1:k-m1", "380000", "off", "剩余部分继续追回",
                  occurred_at=T(16))
        self.assertEqual(p.state, PAYMENT_RECOVERED)
        self.assertEqual(c.available_balance("app-grant"), Decimal("1000000"))

    def test_recovery_exceeding_release_rejected(self):
        c = self._ready()
        with self.assertRaises(ChainError) as ctx:
            c.recover("pay:app-grant:m1:k-m1", "500001", "off", "x")
        self.assertEqual(ctx.exception.code, "recovery-exceeded")
        # 事件流中不应出现追回事件
        self.assertFalse(any(e["kind"] == "RECOVERY_RECONCILED" for e in c.events))

    def test_recovery_requires_release(self):
        c = build_chain()
        grant_milestone(c)
        fully_accept(c)
        with self.assertRaises(ChainError) as ctx:
            c.recover("pay:app-grant:m1:k-m1", "1", "off", "x")
        self.assertEqual(ctx.exception.code, "not-released")


class BankReceiptTest(unittest.TestCase):
    def test_receipt_replay_is_idempotent(self):
        c = build_chain()
        grant_milestone(c)
        fully_accept(c)
        c.decide_overlap(next(iter(c.flags)), OVERLAP_ALLOWED, "off", "允许")
        c.record_matching_funds("app-grant", "200000")
        c.release_tranche("pay:app-grant:m1:k-m1")
        before = len(c.events)
        e1 = c.record_bank_receipt("pay:app-grant:m1:k-m1", "RCPT-2026-0001")
        self.assertEqual(len(c.events), before + 1)
        # 银行回执重放：不新增事件、不改变状态
        e2 = c.record_bank_receipt("pay:app-grant:m1:k-m1", "RCPT-2026-0001")
        self.assertEqual(len(c.events), before + 1)
        self.assertEqual(e2["event_id"], e1["event_id"])
        self.assertEqual(c.payments["pay:app-grant:m1:k-m1"].state, PAYMENT_SETTLED)

    def test_same_receipt_no_cannot_settle_two_payments(self):
        c = build_chain(with_invoice_shared=False)
        c.define_milestone("app-grant", "m1", "proto", "acc", "0.4", "EQUIPMENT",
                           preconditions=[], occurred_at=T(4))
        c.approve_milestone("app-grant", "m1", "apv", occurred_at=T(5))
        c.accept_milestone("app-grant", "m1", "1", "acc", occurred_at=T(6))
        c.reserve_quota("app-grant", "m1", "400000", "pay", "k1", occurred_at=T(7))
        c.release_tranche("pay:app-grant:m1:k1")
        c.record_bank_receipt("pay:app-grant:m1:k1", "RCPT-DUP")
        # 第二笔付款试图共用同一回执号
        c.define_milestone("app-grant", "m2", "proto", "acc", "0.2", "EQUIPMENT",
                           preconditions=[], occurred_at=T(8))
        c.approve_milestone("app-grant", "m2", "apv", occurred_at=T(9))
        c.accept_milestone("app-grant", "m2", "1", "acc", occurred_at=T(10))
        c.reserve_quota("app-grant", "m2", "200000", "pay", "k2", occurred_at=T(11))
        c.release_tranche("pay:app-grant:m2:k2")
        with self.assertRaises(ChainError) as ctx:
            c.record_bank_receipt("pay:app-grant:m2:k2", "RCPT-DUP")
        self.assertEqual(ctx.exception.code, "receipt-reused")

    def test_receipt_before_release_rejected(self):
        c = build_chain(with_invoice_shared=False)
        c.define_milestone("app-grant", "m1", "proto", "acc", "0.4", "EQUIPMENT",
                           preconditions=[], occurred_at=T(4))
        c.approve_milestone("app-grant", "m1", "apv", occurred_at=T(5))
        c.accept_milestone("app-grant", "m1", "1", "acc", occurred_at=T(6))
        c.reserve_quota("app-grant", "m1", "400000", "pay", "k1", occurred_at=T(7))
        with self.assertRaises(ChainError) as ctx:
            c.record_bank_receipt("pay:app-grant:m1:k1", "RCPT-X")
        self.assertEqual(ctx.exception.code, "bad-checkpoint")


class CheckpointResumeTest(unittest.TestCase):
    def test_interrupted_at_reserved_resumes_after_conditions_met(self):
        """释放时条件不满足而中断；配套款追加到账后从 RESERVED 检查点继续。"""
        c = build_chain()
        grant_milestone(c)
        fully_accept(c)
        c.decide_overlap(next(iter(c.flags)), OVERLAP_ALLOWED, "off", "允许")
        # 尚无配套款 → 尝试续作：停留 RESERVED，报告阻碍
        status = c.resume_payment("pay:app-grant:m1:k-m1")
        self.assertEqual(status["checkpoint"], PAYMENT_RESERVED)
        self.assertTrue(status["blockers"])
        self.assertEqual(status["progressed"], [])
        # 配套款迟到账
        c.record_matching_funds("app-grant", "200000", occurred_at=T(20))
        # 再从检查点续作，带银行回执一路到 SETTLED
        status = c.resume_payment("pay:app-grant:m1:k-m1", receipt_no="RCPT-9")
        self.assertEqual(status["checkpoint"], PAYMENT_SETTLED)
        self.assertEqual(status["progressed"], [PAYMENT_RELEASED, PAYMENT_SETTLED])
        # 已结清再续作：不产生任何变化
        again = c.resume_payment("pay:app-grant:m1:k-m1", receipt_no="RCPT-9")
        self.assertEqual(again["checkpoint"], PAYMENT_SETTLED)
        self.assertEqual(again["progressed"], [])

    def test_interrupted_at_released_completes_with_receipt(self):
        """拨付成功后在回执登记前中断（如银行通道抖动），只需补回执。"""
        c = build_chain()
        grant_milestone(c)
        fully_accept(c)
        c.decide_overlap(next(iter(c.flags)), OVERLAP_ALLOWED, "off", "允许")
        c.record_matching_funds("app-grant", "200000")
        c.release_tranche("pay:app-grant:m1:k-m1")
        status = c.resume_payment("pay:app-grant:m1:k-m1", receipt_no="RCPT-42")
        self.assertEqual(status["checkpoint"], PAYMENT_SETTLED)
        self.assertEqual(status["progressed"], ["SETTLED"])

class FixedMilestoneTermsTest(unittest.TestCase):
    def test_milestone_terms_cannot_be_redefined(self):
        c = single_app_chain()
        c.define_milestone("app-x", "m", "proto-v1", "acc", "0.5", "EQUIPMENT",
                           preconditions=[], evidence_ids=["inv-x"], occurred_at=T(3))
        with self.assertRaises(ChainError) as ctx:
            c.define_milestone("app-x", "m", "proto-v2", "acc", "0.9", "EQUIPMENT",
                               preconditions=[], evidence_ids=["inv-x"], occurred_at=T(4))
        self.assertEqual(ctx.exception.code, "milestone-exists")
        # 事件流中只保留首次定义，协议与比例固定
        defined = [e for e in c.events if e["kind"] == "MILESTONE_DEFINED"]
        self.assertEqual(len(defined), 1)
        self.assertEqual(defined[0]["payload"]["protocol_id"], "proto-v1")
        self.assertEqual(defined[0]["payload"]["release_ratio"], "0.5")

    def test_bad_release_ratio_rejected(self):
        c = single_app_chain()
        with self.assertRaises(ChainError) as ctx:
            c.define_milestone("app-x", "m", "proto", "acc", "0", "EQUIPMENT",
                               preconditions=[])
        self.assertEqual(ctx.exception.code, "bad-ratio")
        with self.assertRaises(ChainError) as ctx:
            c.define_milestone("app-x", "m2", "proto", "acc", "1.01", "EQUIPMENT",
                               preconditions=[])
        self.assertEqual(ctx.exception.code, "bad-ratio")

    def test_milestone_requires_registered_evidence(self):
        c = single_app_chain()
        with self.assertRaises(ChainError) as ctx:
            c.define_milestone("app-x", "m", "proto", "acc", "0.5", "EQUIPMENT",
                               evidence_ids=["missing-ev"])
        self.assertEqual(ctx.exception.code, "evidence-not-found")


class UseRestrictionTest(unittest.TestCase):
    def test_purpose_outside_restrictions_blocks_release(self):
        c = single_app_chain()  # 用途仅允许 EQUIPMENT
        c.define_milestone("app-x", "m", "proto", "acc", "0.5", "REAL_ESTATE",
                           preconditions=[], evidence_ids=["inv-x"], occurred_at=T(3))
        c.approve_milestone("app-x", "m", "apv", occurred_at=T(4))
        c.accept_milestone("app-x", "m", "1", "acc", occurred_at=T(5))
        c.reserve_quota("app-x", "m", "100000", "pay", "k", occurred_at=T(6))
        blockers = c.release_blockers("pay:app-x:m:k")
        self.assertTrue(any("用途" in b and "限制" in b for b in blockers), blockers)

    def test_reserve_purpose_mismatch_rejected_directly(self):
        c = single_app_chain()
        c.define_milestone("app-x", "m", "proto", "acc", "0.5", "EQUIPMENT",
                           preconditions=[], evidence_ids=["inv-x"], occurred_at=T(3))
        c.approve_milestone("app-x", "m", "apv", occurred_at=T(4))
        c.accept_milestone("app-x", "m", "1", "acc", occurred_at=T(5))
        with self.assertRaises(ChainError) as ctx:
            c.reserve_quota("app-x", "m", "100000", "pay", "k", purpose="SERVICE")
        self.assertEqual(ctx.exception.code, "purpose-mismatch")


class BatchIsolationTest(unittest.TestCase):
    def test_conflicting_item_isolates_only_its_project(self):
        c = build_chain(with_invoice_shared=False)
        # 三条批量：app-a 正常；app-b 不存在（冲突）；app-grant 正常
        c.submit_application("app-a", "ent-a", "300000", "GRANT",
                             use_restrictions=["EQUIPMENT"], occurred_at=T(1))
        c.register_evidence("app-a", "inv-a", EVIDENCE_INVOICE,
                            {**INVOICE_FIELDS, "invoice_no": "11111111"}, occurred_at=T(2))
        c.register_evidence("app-grant", "inv-g", EVIDENCE_INVOICE,
                            {**INVOICE_FIELDS, "invoice_no": "22222222"}, occurred_at=T(2))
        report = c.run_batch([
            {"op": "register_evidence", "application_id": "app-a",
             "evidence_id": "inv-a2", "kind": EVIDENCE_INVOICE,
             "canonical_fields": {**INVOICE_FIELDS, "invoice_no": "33333333"}},
            {"op": "register_evidence", "application_id": "app-ghost",
             "evidence_id": "inv-x", "kind": EVIDENCE_INVOICE,
             "canonical_fields": INVOICE_FIELDS},
            {"op": "register_evidence", "application_id": "app-grant",
             "evidence_id": "inv-g2", "kind": EVIDENCE_INVOICE,
             "canonical_fields": {**INVOICE_FIELDS, "invoice_no": "44444444"}},
            {"op": "reserve_quota", "application_id": "app-grant",
             "milestone_id": "m-not-defined", "amount": "10", "payer_id": "pay",
             "idempotency_key": "kx"},
            {"op": "frobnicate", "application_id": "app-a"},
        ])
        outcomes = [(r.application_id, r.ok, r.error_code) for r in report]
        self.assertEqual(outcomes, [
            ("app-a", True, None),
            ("app-ghost", False, "application-not-found"),
            ("app-grant", True, None),
            ("app-grant", False, "milestone-not-found"),
            ("app-a", False, "unknown-op"),
        ])
        # 正常条目的证据确实落账
        self.assertIn(("app-a", "inv-a2"), c.evidence)
        self.assertIn(("app-grant", "inv-g2"), c.evidence)
        # 冲突没有污染台账：不存在的申请未被创建
        self.assertNotIn("app-ghost", c.applications)
        # 后续正常操作不受失败条目影响
        c.define_milestone("app-a", "m", "proto", "acc", "0.5", "EQUIPMENT",
                           preconditions=[], evidence_ids=["inv-a2"], occurred_at=T(3))
        c.approve_milestone("app-a", "m", "apv", occurred_at=T(4))
        c.accept_milestone("app-a", "m", "1", "acc", occurred_at=T(5))
        c.reserve_quota("app-a", "m", "150000", "pay", "k", occurred_at=T(6))
        self.assertEqual(c.release_blockers("pay:app-a:m:k"), [])


class AuditTraceTest(unittest.TestCase):
    def test_trace_walks_forward_from_payment(self):
        """审计查询：从一笔付款向前展示预算来源、证据指纹、重叠判定、历次调整与余额。"""
        c = build_chain()
        # 固定条款里程碑（协议/验收人/释放比例），要求配套款到账，关联共享发票
        c.define_milestone("app-grant", "m1", "proto-2026-A", "acc", "0.5",
                           "EQUIPMENT", preconditions=[CONDITION_MATCHING_FUNDS],
                           evidence_ids=["inv-shared"], occurred_at=T(4))
        c.approve_milestone("app-grant", "m1", "apv", occurred_at=T(5))
        # 部分验收：首批设备到位 0.5（追加事实，后续可继续验收）
        c.accept_milestone("app-grant", "m1", "0.5", "acc", occurred_at=T(6),
                           note="首批设备")
        flag_id = next(iter(c.flags))
        c.decide_overlap(flag_id, OVERLAP_ALLOWED, "off",
                         "贷款与补贴分属不同资金渠道，已比对设备清单与合同，不构成重复",
                         occurred_at=T(8))
        c.record_matching_funds("app-grant", "200000", occurred_at=T(9))
        # 预算100万 × 释放比例0.5 × 验收0.5 = 容量25万，预留并拨付
        pid = "pay:app-grant:m1:k-part"
        c.reserve_quota("app-grant", "m1", "250000", "pay", "k-part", occurred_at=T(10))
        c.release_tranche(pid, occurred_at=T(11))
        c.record_bank_receipt(pid, "RCPT-AUDIT-1", occurred_at=T(12))
        c.recover(pid, "50000", "off", "复核核减", occurred_at=T(13))

        trace = c.trace_payment(pid)

        # 预算来源与申请承诺
        bs = trace["budget_source"]
        self.assertEqual(bs["funding_source"], "GRANT")
        self.assertEqual(bs["budget"], "1000000.00")
        self.assertEqual(bs["use_restrictions"], ["EQUIPMENT", "SERVICE"])
        self.assertIn("bank-alpha", bs["related_parties"])
        self.assertEqual(bs["other_commitments"][0]["source"], "BANK_LOAN")
        self.assertEqual(bs["matching_required"], "200000.00")
        self.assertEqual(bs["matching_received"], "200000.00")

        # 证据指纹与跨申请共享方
        ev = trace["evidence"][0]
        self.assertTrue(ev["fingerprint"].startswith("sha256:"))
        self.assertEqual(ev["shared_with"], [
            {"application_id": "app-loan", "evidence_id": "inv-shared"}])

        # 重叠判定：系统提示 + 授权结论 + 书面依据
        ov = trace["overlap_findings"][0]
        self.assertEqual(ov["system_flag"], "OVERLAP_FLAGGED")
        self.assertEqual(ov["other_application_id"], "app-loan")
        self.assertEqual(ov["status"], OVERLAP_ALLOWED)
        self.assertEqual(ov["decision"]["decided_by"], "off")
        self.assertIn("不构成重复", ov["decision"]["rationale"])

        # 里程碑固定条款与三权链路
        ms = trace["milestone"]
        self.assertEqual(ms["protocol_id"], "proto-2026-A")
        self.assertEqual(ms["acceptor_id"], "acc")
        self.assertEqual(ms["release_ratio"], "0.5")
        self.assertEqual(ms["accepted_cumulative_ratio"], "0.5")
        self.assertEqual(ms["duty_chain"]["three_distinct_roles"], True)

        # 历次调整：只追加，按事件追加顺序（seq）排列，原结论不被覆盖
        kinds = [a["kind"] for a in trace["adjustments"]]
        self.assertEqual(kinds, [
            "MILESTONE_ACCEPTED",
            "MATCHING_FUNDS_RECEIVED",
            "QUOTA_RESERVED",
            "TRANCHE_RELEASED",
            "RECOVERY_RECONCILED",
        ])
        seqs = [a["seq"] for a in trace["adjustments"]]
        self.assertEqual(seqs, sorted(seqs))

        # 检查点与回执
        self.assertEqual(trace["payment"]["state"], PAYMENT_SETTLED)
        checkpoints = {cp["checkpoint"] for cp in trace["payment"]["checkpoints"]}
        self.assertEqual(checkpoints, {PAYMENT_RESERVED, PAYMENT_RELEASED, PAYMENT_SETTLED})
        self.assertEqual(trace["payment"]["recovered_total"], "50000.00")

        # 仍可使用的余额：100万 - 25万 + 追回5万 = 80万
        self.assertEqual(trace["balance_now"]["available"], "800000.00")

    def test_trace_unknown_payment(self):
        c = build_chain()
        with self.assertRaises(ChainError) as ctx:
            c.trace_payment("pay:nope")
        self.assertEqual(ctx.exception.code, "payment-not-found")


class ReplayConsistencyTest(unittest.TestCase):
    def test_replaying_event_log_rebuilds_identical_state(self):
        c = build_chain()
        grant_milestone(c)
        fully_accept(c)
        c.decide_overlap(next(iter(c.flags)), OVERLAP_ALLOWED, "off", "允许")
        c.record_matching_funds("app-grant", "200000")
        c.resume_payment("pay:app-grant:m1:k-m1", receipt_no="RCPT-R")
        # 拨付完成后企业重组核减预算（追加事实），再追回一部分
        c.record_restructuring("app-grant", "-50000", "apv", "重组核减")
        c.recover("pay:app-grant:m1:k-m1", "80000", "off", "追回")

        clone = DisbursementChain.replay(c.events)

        # 余额、占用、裁决、指纹台账全部一致
        for app_id in c.applications:
            self.assertEqual(clone.available_balance(app_id),
                             c.available_balance(app_id))
            self.assertEqual(clone.effective_budget(app_id),
                             c.effective_budget(app_id))
        self.assertEqual(
            {k: v["status"] for k, v in clone.flags.items()},
            {k: v["status"] for k, v in c.flags.items()},
        )
        self.assertEqual(
            [(d["flag_id"], d["decision"], d["rationale"]) for d in clone.decisions],
            [(d["flag_id"], d["decision"], d["rationale"]) for d in c.decisions],
        )
        self.assertEqual(set(clone.fingerprints), set(c.fingerprints))
        pid = "pay:app-grant:m1:k-m1"
        self.assertEqual(clone.payments[pid].state, c.payments[pid].state)
        self.assertEqual(clone.payments[pid].recovered, c.payments[pid].recovered)
        # 审计视图的关键数字一致
        self.assertEqual(clone.trace_payment(pid)["balance_now"],
                         c.trace_payment(pid)["balance_now"])
        # 事件本身不被折叠过程修改（seq 重新编号但内容稳定）
        self.assertEqual(len(clone.events), len(c.events))

    def test_event_log_is_append_only(self):
        c = build_chain()
        grant_milestone(c)
        fully_accept(c)
        original_accept = [e for e in c.events if e["kind"] == "MILESTONE_ACCEPTED"][0]
        snapshot = {k: v for k, v in original_accept["payload"].items()}
        # 后续所有追加事实都不能改动原验收事件
        c.decide_overlap(next(iter(c.flags)), OVERLAP_ALLOWED, "off", "允许")
        c.record_matching_funds("app-grant", "200000")
        c.release_tranche("pay:app-grant:m1:k-m1")
        c.recover("pay:app-grant:m1:k-m1", "1000", "off", "x")
        self.assertEqual(original_accept["payload"], snapshot)


if __name__ == "__main__":
    unittest.main()
