"""可穿透拨付链的行为测试。

场景对应审计抽查发现：同一张设备发票同时出现在扶持资金与银行贷款的
进度材料中；已验收里程碑因企业配套款未到账不应放款。
"""

import unittest
from decimal import Decimal

from src.disbursement import (
    STEP_CONDITIONS_VERIFIED,
    STEP_RELEASED,
    VERDICT_DUPLICATE,
    VERDICT_NOT_DUPLICATE,
    DisbursementChain,
    DomainError,
    DuplicateEvidenceVerdictError,
    PaymentBlockedError,
    QuotaUnavailableError,
    RoleNotAuthorizedError,
    SeparationOfDutiesError,
    fingerprint_evidence,
    trace_payment,
)

DIRECTORY = {
    "wang": {"APPROVER"},      # 审批
    "li": {"ACCEPTOR"},        # 里程碑固定验收人
    "zhao": {"PAYER"},         # 付款
    "chen": {"ADJUDICATOR"},   # 重叠裁定（授权人员）
}

INVOICE = {
    "number": "INV-2026-001",
    "issuer": "甲设备公司",
    "amount": 12000.50,
    "items": [{"name": "摄像机", "qty": 1}],
}


def make_chain() -> DisbursementChain:
    """扶持资金申请 A：预算 100 万，另有银行贷款与企业配套承诺。"""
    chain = DisbursementChain(directory=DIRECTORY)
    chain.submit_application(
        "A",
        budget=[{"category": "设备购置", "amount": "1000000.00", "usage": "仅限设备购置"}],
        usage_restrictions=["仅限设备购置", "不得转借关联主体"],
        related_entities=[{"entity_id": "E-1", "relation": "母公司"}],
        funding_commitments=[
            {"source": "银行贷款", "amount": "500000.00"},
            {"source": "企业配套", "amount": "200000.00"},
        ],
    )
    chain.approve_application("A", approver="wang")
    return chain


def add_accepted_milestone(chain: DisbursementChain, milestone_id="M1", planned="600000", **kwargs) -> None:
    chain.define_milestone(
        milestone_id,
        application_id="A",
        agreement_id="AG-1",
        acceptor_id="li",
        release_ratio="0.6",
        planned_amount=planned,
        **kwargs,
    )
    chain.accept_milestone(milestone_id, acceptor_id="li", accepted_ratio="1.0")


def run_payment(chain: DisbursementChain, payment_id: str, milestone_id="M1") -> None:
    chain.initiate_payment(payment_id, milestone_id=milestone_id, approver="wang", payer="zhao")
    chain.approve_payment(payment_id, approver="wang")
    chain.advance_payment(payment_id)


class ApplicationTest(unittest.TestCase):
    def test_application_records_budget_usage_entities_and_commitments(self):
        chain = make_chain()
        dossier = chain.application_dossier("A")
        submitted = dossier["submitted"]
        self.assertEqual(submitted["budget"][0]["amount"], "1000000.00")
        self.assertIn("仅限设备购置", submitted["usage_restrictions"])
        self.assertEqual(submitted["related_entities"][0]["entity_id"], "E-1")
        self.assertEqual(
            {item["source"] for item in submitted["funding_commitments"]},
            {"银行贷款", "企业配套"},
        )
        self.assertEqual(dossier["approval"]["approved_amount"], "1000000.00")

    def test_approve_requires_approver_role(self):
        chain = DisbursementChain(directory=DIRECTORY)
        chain.submit_application(
            "A", budget=[{"category": "设备", "amount": "100"}],
            usage_restrictions=[], related_entities=[], funding_commitments=[],
        )
        with self.assertRaises(RoleNotAuthorizedError):
            chain.approve_application("A", approver="zhao")


class FingerprintTest(unittest.TestCase):
    def test_fingerprint_stable_across_formatting(self):
        reformatted = {
            "items": [{"qty": 1.0, "name": "  摄像机  "}],
            "amount": 12000.5,
            "issuer": "甲设备公司",
            "number": "INV-2026-001",
        }
        self.assertEqual(
            fingerprint_evidence("INVOICE", INVOICE),
            fingerprint_evidence("INVOICE", reformatted),
        )

    def test_fingerprint_changes_with_content(self):
        altered = {**INVOICE, "amount": 12000.51}
        self.assertNotEqual(
            fingerprint_evidence("INVOICE", INVOICE),
            fingerprint_evidence("INVOICE", altered),
        )

    def test_shared_invoice_flags_cross_application_overlap(self):
        chain = make_chain()
        # 银行贷款进度材料中的同一项目，作为另一条申请登记
        chain.submit_application(
            "B", budget=[{"category": "设备", "amount": "500000.00"}],
            usage_restrictions=[], related_entities=[],
            funding_commitments=[{"source": "银行贷款", "amount": "500000.00"}],
        )
        chain.register_evidence("A", "EV-A-1", "INVOICE", INVOICE)
        self.assertEqual(chain.store.find("OVERLAP_FLAGGED"), [])
        chain.register_evidence("B", "EV-B-1", "INVOICE", INVOICE)
        flags = chain.store.find("OVERLAP_FLAGGED")
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["payload"]["application_ids"], ["A", "B"])


class OverlapAdjudicationTest(unittest.TestCase):
    def setUp(self):
        self.chain = make_chain()
        self.chain.submit_application(
            "B", budget=[{"category": "设备", "amount": "500000.00"}],
            usage_restrictions=[], related_entities=[], funding_commitments=[],
        )
        self.chain.register_evidence("A", "EV-A-1", "INVOICE", INVOICE)
        self.chain.register_evidence("B", "EV-B-1", "INVOICE", INVOICE)
        self.fingerprint = fingerprint_evidence("INVOICE", INVOICE)
        add_accepted_milestone(self.chain)

    def test_adjudication_requires_authorized_role_and_rationale(self):
        with self.assertRaises(RoleNotAuthorizedError):
            self.chain.adjudicate_overlap(
                self.fingerprint, adjudicator="wang",
                verdict=VERDICT_NOT_DUPLICATE, rationale="无权裁定",
            )
        with self.assertRaises(ValueError):
            self.chain.adjudicate_overlap(
                self.fingerprint, adjudicator="chen",
                verdict=VERDICT_NOT_DUPLICATE, rationale="  ",
            )
        record = self.chain.adjudicate_overlap(
            self.fingerprint, adjudicator="chen",
            verdict=VERDICT_NOT_DUPLICATE,
            rationale="银行贷款材料仅为进度报备，未重复融资",
        )
        self.assertEqual(record["payload"]["verdict"], VERDICT_NOT_DUPLICATE)

    def test_pending_flag_blocks_payment_until_adjudicated(self):
        self.chain.initiate_payment("P1", milestone_id="M1", approver="wang", payer="zhao")
        self.chain.approve_payment("P1", approver="wang")
        with self.assertRaises(PaymentBlockedError):
            self.chain.advance_payment("P1")
        self.chain.adjudicate_overlap(
            self.fingerprint, adjudicator="chen",
            verdict=VERDICT_NOT_DUPLICATE, rationale="排除重复融资嫌疑",
        )
        self.chain.advance_payment("P1")
        self.assertIn(STEP_RELEASED, self.chain.payment_state("P1")["steps"])

    def test_duplicate_verdict_blocks_payment(self):
        self.chain.adjudicate_overlap(
            self.fingerprint, adjudicator="chen",
            verdict=VERDICT_DUPLICATE, rationale="同一发票重复申报扶持资金与贷款",
        )
        self.chain.initiate_payment("P1", milestone_id="M1", approver="wang", payer="zhao")
        self.chain.approve_payment("P1", approver="wang")
        with self.assertRaises(DuplicateEvidenceVerdictError):
            self.chain.advance_payment("P1")


class MilestoneAndPaymentTest(unittest.TestCase):
    def test_acceptance_only_by_designated_acceptor(self):
        chain = make_chain()
        chain.define_milestone(
            "M1", application_id="A", agreement_id="AG-1",
            acceptor_id="li", release_ratio="0.6", planned_amount="600000",
        )
        with self.assertRaises(RoleNotAuthorizedError):
            chain.accept_milestone("M1", acceptor_id="wang", accepted_ratio="1.0")

    def test_separation_of_duties(self):
        chain = make_chain()
        add_accepted_milestone(chain)
        # 审批人与付款人是同一人
        with self.assertRaises(SeparationOfDutiesError):
            chain.initiate_payment("P1", milestone_id="M1", approver="wang", payer="wang")
        # 审批人与验收人是同一人（li 不是审批角色，先补角色再验证分离）
        chain.directory["li"].add("APPROVER")
        with self.assertRaises(SeparationOfDutiesError):
            chain.initiate_payment("P2", milestone_id="M1", approver="li", payer="zhao")

    def test_payment_blocked_until_matching_funds_arrive_then_resumes(self):
        chain = make_chain()
        add_accepted_milestone(
            chain, requires_matching_funds=True, required_matching_amount="200000",
        )
        chain.initiate_payment("P1", milestone_id="M1", approver="wang", payer="zhao")
        chain.approve_payment("P1", approver="wang")
        with self.assertRaises(PaymentBlockedError) as caught:
            chain.advance_payment("P1")
        self.assertEqual(caught.exception.step, STEP_CONDITIONS_VERIFIED)
        # 配套款迟到，作为追加事实到账后从检查点继续
        chain.record_matching_funds("A", amount="200000", source="企业配套", received_at="2026-09-24")
        chain.advance_payment("P1")
        state = chain.payment_state("P1")
        self.assertIn(STEP_RELEASED, state["steps"])
        self.assertEqual(len(chain.store.find("TRANCHE_RELEASED")), 1)

    def test_quota_occupancy_limits_concurrent_payments(self):
        chain = make_chain()
        add_accepted_milestone(chain, "M1", planned="600000")
        add_accepted_milestone(chain, "M2", planned="600000")
        chain.initiate_payment("P1", milestone_id="M1", approver="wang", payer="zhao")
        # 60 万被 P1 占用，可用只剩 40 万
        occupancy = chain.occupancy_of("A")
        self.assertEqual([(item["payment_id"], item["amount"]) for item in occupancy],
                         [("P1", Decimal("600000.00"))])
        with self.assertRaises(QuotaUnavailableError):
            chain.initiate_payment("P2", milestone_id="M2", approver="wang", payer="zhao")
        # P1 完成后额度转为已拨付，可用仍为 40 万
        chain.approve_payment("P1", approver="wang")
        chain.advance_payment("P1")
        with self.assertRaises(QuotaUnavailableError):
            chain.initiate_payment("P2", milestone_id="M2", approver="wang", payer="zhao")
        # 追回 20 万作为追加事实修正余额后，P2 可以进入付款
        chain.reconcile_recovery("A", amount="200000", reason="部分设备退回", payment_id="P1")
        chain.initiate_payment("P2", milestone_id="M2", approver="wang", payer="zhao")
        self.assertEqual(chain.balance_of("A")["usable"], Decimal("0.00"))

    def test_partial_acceptance_scales_payment(self):
        chain = make_chain()
        chain.define_milestone(
            "M1", application_id="A", agreement_id="AG-1",
            acceptor_id="li", release_ratio="0.6", planned_amount="600000",
        )
        chain.accept_milestone("M1", acceptor_id="li", accepted_ratio="0.5")
        run_payment(chain, "P1")
        self.assertEqual(chain.payment_state("P1")["context"]["amount"], "300000.00")
        chain.accept_milestone("M1", acceptor_id="li", accepted_ratio="0.5")
        run_payment(chain, "P2")
        self.assertEqual(chain.payment_state("P2")["context"]["amount"], "300000.00")
        with self.assertRaises(DomainError):
            chain.initiate_payment("P3", milestone_id="M1", approver="wang", payer="zhao")


class AppendOnlyTest(unittest.TestCase):
    def test_corrections_append_without_overwriting(self):
        chain = make_chain()
        add_accepted_milestone(chain)
        run_payment(chain, "P1")
        released_before = chain.store.get("pay:P1:released")
        size_before = len(chain.store.all())
        chain.reconcile_recovery("A", amount="50000", reason="审计追回", payment_id="P1")
        chain.record_restructuring("A", successor_entities=[{"entity_id": "E-2"}], note="企业重组")
        self.assertGreater(len(chain.store.all()), size_before)
        # 原拨付结论保持不动
        self.assertEqual(chain.store.get("pay:P1:released"), released_before)
        balances = chain.balance_of("A")
        self.assertEqual(balances["released"], Decimal("600000.00"))
        self.assertEqual(balances["recovered"], Decimal("50000.00"))
        self.assertEqual(balances["usable"], Decimal("450000.00"))

    def test_bank_receipt_replay_does_not_disburse_twice(self):
        chain = make_chain()
        add_accepted_milestone(chain)
        run_payment(chain, "P1")
        _, created = chain.record_bank_receipt("RCPT-1", payment_id="P1", amount="600000")
        self.assertTrue(created)
        record, created = chain.record_bank_receipt("RCPT-1", payment_id="P1", amount="600000")
        self.assertFalse(created)
        self.assertEqual(record["payload"]["receipt_ref"], "RCPT-1")
        self.assertEqual(len(chain.store.find("TRANCHE_RELEASED")), 1)
        self.assertEqual(chain.balance_of("A")["released"], Decimal("600000.00"))

    def test_receipt_cannot_attach_to_other_payment(self):
        chain = make_chain()
        add_accepted_milestone(chain, "M1")
        add_accepted_milestone(chain, "M2", planned="300000")
        run_payment(chain, "P1")
        run_payment(chain, "P2", milestone_id="M2")
        chain.record_bank_receipt("RCPT-1", payment_id="P1", amount="600000")
        with self.assertRaises(DomainError):
            chain.record_bank_receipt("RCPT-1", payment_id="P2", amount="300000")


class BatchImportTest(unittest.TestCase):
    def test_conflict_quarantines_only_the_item(self):
        chain = make_chain()
        result = chain.import_batch("BATCH-1", [
            {"item_id": "1", "application_id": "A", "evidence_id": "EV-1",
             "evidence_kind": "INVOICE", "content": INVOICE},
            {"item_id": "2", "application_id": "UNKNOWN", "evidence_id": "EV-2",
             "evidence_kind": "INVOICE", "content": INVOICE},
            {"item_id": "3", "application_id": "A", "evidence_id": "EV-1",
             "evidence_kind": "INVOICE", "content": INVOICE},
            {"item_id": "4", "application_id": "A", "evidence_id": "EV-3",
             "evidence_kind": "CONTRACT", "content": {"number": "HT-1", "amount": 8000}},
        ])
        self.assertEqual([item["item_id"] for item in result["registered"]], ["1", "4"])
        self.assertEqual([item["item_id"] for item in result["quarantined"]], ["2", "3"])
        self.assertEqual(len(chain.store.find("BATCH_ITEM_QUARANTINED", "BATCH-1")), 2)
        # 冲突项之后的材料仍正常登记
        self.assertEqual({item["evidence_id"] for item in chain.evidence_of("A")}, {"EV-1", "EV-3"})


class CheckpointRecoveryTest(unittest.TestCase):
    def test_execution_resumes_from_checkpoint_after_restart(self):
        chain = make_chain()
        add_accepted_milestone(
            chain, requires_matching_funds=True, required_matching_amount="200000",
        )
        chain.initiate_payment("P1", milestone_id="M1", approver="wang", payer="zhao")
        chain.approve_payment("P1", approver="wang")
        with self.assertRaises(PaymentBlockedError):
            chain.advance_payment("P1")
        # 模拟执行中断：换一个服务实例，只共享同一份事件日志
        resumed = DisbursementChain(store=chain.store, directory=DIRECTORY)
        resumed.record_matching_funds("A", amount="200000", source="企业配套", received_at="2026-09-24")
        resumed.advance_payment("P1")
        resumed.record_bank_receipt("RCPT-1", payment_id="P1", amount="600000")
        state = resumed.payment_state("P1")
        self.assertEqual(state["status"], "COMPLETED")
        self.assertEqual(
            state["steps"],
            ["INITIATED", "APPROVED", "CONDITIONS_VERIFIED", "RELEASED", "RECEIPT_CONFIRMED"],
        )
        self.assertEqual(len(resumed.store.find("TRANCHE_RELEASED")), 1)


class AuditTraceTest(unittest.TestCase):
    def test_trace_reconstructs_full_chain_from_payment(self):
        chain = make_chain()
        # 同一发票同时出现在银行贷款进度材料中
        chain.submit_application(
            "B", budget=[{"category": "设备", "amount": "500000.00"}],
            usage_restrictions=[], related_entities=[],
            funding_commitments=[{"source": "银行贷款", "amount": "500000.00"}],
        )
        chain.register_evidence("A", "EV-A-1", "INVOICE", INVOICE)
        chain.register_evidence("B", "EV-B-1", "INVOICE", INVOICE)
        fingerprint = fingerprint_evidence("INVOICE", INVOICE)
        chain.adjudicate_overlap(
            fingerprint, adjudicator="chen",
            verdict=VERDICT_NOT_DUPLICATE, rationale="贷款材料仅报备，未重复占用",
        )
        add_accepted_milestone(
            chain, requires_matching_funds=True, required_matching_amount="200000",
        )
        chain.initiate_payment("P1", milestone_id="M1", approver="wang", payer="zhao")
        chain.approve_payment("P1", approver="wang")
        with self.assertRaises(PaymentBlockedError):
            chain.advance_payment("P1")
        chain.record_matching_funds("A", amount="200000", source="企业配套", received_at="2026-09-24")
        chain.advance_payment("P1")
        chain.record_bank_receipt("RCPT-1", payment_id="P1", amount="600000")
        chain.reconcile_recovery("A", amount="50000", reason="审计追回", payment_id="P1")
        chain.record_restructuring("A", successor_entities=[{"entity_id": "E-2"}], note="企业重组")

        trace = trace_payment(chain, "P1")

        # 预算来源
        source = trace["budget_source"]
        self.assertEqual(source["application_id"], "A")
        self.assertEqual(source["approved_amount"], "1000000.00")
        self.assertEqual(source["usage_restrictions"], ["仅限设备购置", "不得转借关联主体"])
        self.assertEqual(
            {item["source"] for item in source["funding_commitments"]},
            {"银行贷款", "企业配套"},
        )
        # 证据指纹
        self.assertEqual(trace["evidence_fingerprints"][0]["fingerprint"], fingerprint)
        # 重叠判定与依据
        overlap = trace["overlap_adjudications"][0]
        self.assertEqual(overlap["flag"]["application_ids"], ["A", "B"])
        self.assertEqual(overlap["current_verdict"], VERDICT_NOT_DUPLICATE)
        self.assertEqual(overlap["adjudications"][0]["rationale"], "贷款材料仅报备，未重复占用")
        # 历次调整：验收、配套款迟到、追回、重组
        kinds = [item["kind"] for item in trace["adjustments"]]
        self.assertEqual(
            kinds,
            ["MILESTONE_ACCEPTED", "MATCHING_FUNDS_RECORDED", "RECOVERY_RECONCILED", "RESTRUCTURING_RECORDED"],
        )
        # 额度占用与仍可使用的余额
        self.assertEqual(trace["occupancy"], [])
        self.assertEqual(
            trace["balances"],
            {
                "approved": "1000000.00",
                "released": "600000.00",
                "recovered": "50000.00",
                "occupied": "0",
                "usable": "450000.00",
            },
        )
        self.assertEqual(trace["payment"]["released_event"]["payload"]["amount"], "600000.00")
        self.assertEqual(trace["payment"]["receipts"][0]["payload"]["receipt_ref"], "RCPT-1")


if __name__ == "__main__":
    unittest.main()
