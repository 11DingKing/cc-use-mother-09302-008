"""端到端领域流程测试：需求→询价→报价冻结→签署→合同→验收付款退款→结算。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ledger_service.service import AuthError, DomainError, LedgerService


def principal(role: str, person_id: str, name: str) -> dict:
    return {"role": role, "person_id": person_id, "name": name}


BUYER = principal("学校采购员", "buyer-1", "王采购")
REVIEWER_A = principal("评审委员", "rev-a", "李评委")
REVIEWER_B = principal("评审委员", "rev-b", "赵评委")
REVIEWER_C = principal("评审委员", "rev-c", "孙评委")
AUDITOR = principal("审计人员", "aud-1", "钱审计")
PUBLIC = principal("公众", "anon", "公众")


class FlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = LedgerService(":memory:")
        self.suppliers = []
        for name in ("梨园春艺术团", "锦韵演出公司", "新秀传媒"):
            self.suppliers.append(
                self.svc.create_supplier(BUYER, name)["supplier_id"]
            )
        self.s1, self.s2, self.s3 = self.suppliers
        proc = self.svc.create_procurement(
            BUYER, "三校联合非遗戏曲进校园",
            ["第一小学", "第二小学", "实验中学"], "100000.00")
        self.pid = proc["procurement_id"]

    def tearDown(self) -> None:
        self.svc.close()

    def _prepare_frozen(self) -> None:
        self.svc.start_inquiry(BUYER, self.pid)
        self.svc.add_reviewer(BUYER, self.pid, "rev-a", "李评委", "第一小学")
        self.svc.add_reviewer(BUYER, self.pid, "rev-b", "赵评委", "第二小学")
        self.svc.add_reviewer(BUYER, self.pid, "rev-c", "孙评委", "实验中学")
        self.svc.submit_quote(BUYER, self.pid, self.s1, "95000.00", note="首版")
        self.svc.submit_quote(BUYER, self.pid, self.s1, "92000.00", note="修订价")
        self.svc.submit_quote(BUYER, self.pid, self.s2, "98000.00")
        self.svc.submit_quote(BUYER, self.pid, self.s3, "91000.00")
        self.svc.freeze_for_review(BUYER, self.pid)

    def test_full_happy_path_with_partial_acceptance_refund_settlement(self) -> None:
        self._prepare_frozen()
        self.svc.sign_review(REVIEWER_A, self.pid, "同意")
        self.svc.sign_review(REVIEWER_B, self.pid, "同意")
        self.svc.sign_review(REVIEWER_C, self.pid, "弃权")

        # 合同价必须等于冻结报价
        with self.assertRaises(DomainError):
            self.svc.award_contract(BUYER, self.pid, self.s1, "90000.00", "压价签约")
        self.svc.award_contract(BUYER, self.pid, self.s1, "92000.00", "最低价且资质合规")

        proc = self.svc.get_procurement(BUYER, self.pid)
        self.assertEqual(proc["status"], "履约")
        self.assertEqual(proc["awarded_supplier_name"], "梨园春艺术团")

        # 部分验收两次
        self.svc.accept_delivery(BUYER, self.pid, "50000.00", False, "首场演出")
        self.svc.accept_delivery(BUYER, self.pid, "30000.00", False, "第二场演出")

        # 付款不得超过累计验收
        with self.assertRaises(DomainError):
            self.svc.pay(BUYER, self.pid, "85000.00")
        pay1 = self.svc.pay(BUYER, self.pid, "50000.00")
        pay2 = self.svc.pay(BUYER, self.pid, "30000.00")

        # 第二场有瑕疵，退款冲正 5000 元（原付款不改写）
        refund = self.svc.refund(BUYER, self.pid, pay2["payment_id"], "5000.00", "演出缩短")
        self.assertEqual(refund["amount_cents"], 500000)
        with self.assertRaises(DomainError):
            self.svc.refund(BUYER, self.pid, pay2["payment_id"], "30000.00", "超额退款")

        # 合同变更：追加 4000 元加场（仍在预算内）
        self.svc.change_contract(BUYER, self.pid, "4000.00", "增加一场非遗讲座")
        # 负向变更不得低于已验收额
        with self.assertRaises(DomainError):
            self.svc.change_contract(BUYER, self.pid, "-20000.00", "缩减场次")

        # 完成剩余验收 92000+4000-80000 = 16000
        self.svc.accept_delivery(BUYER, self.pid, "16000.00", True, "终验")

        # 结算前净额须等于验收额：已付 80000 - 退款 5000 = 75000，还需付 21000
        with self.assertRaises(DomainError):
            self.svc.complete_settlement(BUYER, self.pid)
        self.svc.pay(BUYER, self.pid, "21000.00")
        self.svc.complete_settlement(BUYER, self.pid)

        totals = self.svc.get_procurement(BUYER, self.pid)["totals"]
        self.assertEqual(totals["committed"], "96000.00")
        self.assertEqual(totals["accepted"], "96000.00")
        self.assertEqual(totals["paid"], "101000.00")
        self.assertEqual(totals["refunded"], "5000.00")
        self.assertEqual(totals["net_paid"], "96000.00")

        verification = self.svc.verify_chain(self.pid)
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertTrue(verification["balanced"])

    def test_quote_versions_retained_but_only_latest_compared(self) -> None:
        self._prepare_frozen()
        quotes = self.svc.quote_versions(AUDITOR, self.pid)
        s1_versions = [q for q in quotes if q["supplier_id"] == self.s1]
        self.assertEqual([q["version"] for q in s1_versions], [1, 2])
        self.assertEqual({q["status"] for q in s1_versions}, {"已替换", "冻结"})

    def test_freeze_blocks_quote_changes_and_reviewer_changes(self) -> None:
        self._prepare_frozen()
        with self.assertRaises(DomainError):
            self.svc.submit_quote(BUYER, self.pid, self.s2, "80000.00")
        with self.assertRaises(DomainError):
            self.svc.add_reviewer(BUYER, self.pid, "rev-x", "临时评委", "外校")
        with self.assertRaises(DomainError):
            self.svc.declare_conflict(
                BUYER, self.pid, self.s2, "rev-a", "李评委", "配偶持股", False)

    def test_conflict_blocks_quote_and_award(self) -> None:
        self.svc.start_inquiry(BUYER, self.pid)
        self.svc.add_reviewer(BUYER, self.pid, "rev-a", "李评委", "第一小学")
        # 李评委配偶持有 s1 股份且未回避 → s1 被拦截
        self.svc.declare_conflict(
            BUYER, self.pid, self.s1, "rev-a", "李评委", "配偶持股30%", False)
        with self.assertRaises(DomainError):
            self.svc.submit_quote(BUYER, self.pid, self.s1, "90000.00")
        # 补报回避声明后可以报价（声明留痕，最新状态有效）
        self.svc.declare_conflict(
            BUYER, self.pid, self.s1, "rev-a", "李评委", "配偶持股30%，本人回避", True)
        self.svc.submit_quote(BUYER, self.pid, self.s1, "90000.00")
        self.svc.submit_quote(BUYER, self.pid, self.s2, "93000.00")
        self.svc.freeze_for_review(BUYER, self.pid)
        # 回避评委可以签弃权，不能签同意
        with self.assertRaises(DomainError):
            self.svc.sign_review(REVIEWER_A, self.pid, "同意")
        self.svc.sign_review(REVIEWER_A, self.pid, "弃权")

    def test_uncused_reviewer_cannot_sign(self) -> None:
        self.svc.start_inquiry(BUYER, self.pid)
        self.svc.add_reviewer(BUYER, self.pid, "rev-a", "李评委", "第一小学")
        self.svc.add_reviewer(BUYER, self.pid, "rev-b", "赵评委", "第二小学")
        self.svc.declare_conflict(
            BUYER, self.pid, self.s1, "rev-a", "李评委", "亲兄弟为法人", False)
        self.svc.submit_quote(BUYER, self.pid, self.s2, "90000.00")
        self.svc.freeze_for_review(BUYER, self.pid)
        with self.assertRaises(DomainError):
            self.svc.sign_review(REVIEWER_A, self.pid, "同意")

    def test_non_reviewer_cannot_sign_and_quorum_required(self) -> None:
        self._prepare_frozen()
        with self.assertRaises(AuthError):
            self.svc.sign_review(principal("评审委员", "rev-x", "外人"), self.pid, "同意")
        self.svc.sign_review(REVIEWER_A, self.pid, "同意")
        # 未全员签署不能授标
        with self.assertRaises(DomainError):
            self.svc.award_contract(BUYER, self.pid, self.s1, "92000.00", "抢跑")
        self.svc.sign_review(REVIEWER_B, self.pid, "反对")
        self.svc.sign_review(REVIEWER_C, self.pid, "弃权")
        # 有反对票，不能授标
        with self.assertRaises(DomainError):
            self.svc.award_contract(BUYER, self.pid, self.s1, "92000.00", "强行授标")

    def test_budget_ceiling_on_contract_and_change(self) -> None:
        self.svc.start_inquiry(BUYER, self.pid)
        self.svc.add_reviewer(BUYER, self.pid, "rev-a", "李评委", "第一小学")
        self.svc.submit_quote(BUYER, self.pid, self.s2, "100000.00")
        self.svc.freeze_for_review(BUYER, self.pid)
        self.svc.sign_review(REVIEWER_A, self.pid, "同意")
        self.svc.award_contract(BUYER, self.pid, self.s2, "100000.00", "足额")
        with self.assertRaises(DomainError):
            self.svc.change_contract(BUYER, self.pid, "0.01", "超预算")

    def test_final_acceptance_must_equal_committed(self) -> None:
        self.svc.start_inquiry(BUYER, self.pid)
        self.svc.add_reviewer(BUYER, self.pid, "rev-a", "李评委", "第一小学")
        self.svc.submit_quote(BUYER, self.pid, self.s2, "50000.00")
        self.svc.freeze_for_review(BUYER, self.pid)
        self.svc.sign_review(REVIEWER_A, self.pid, "同意")
        self.svc.award_contract(BUYER, self.pid, self.s2, "50000.00", "签约")
        with self.assertRaises(DomainError):
            self.svc.accept_delivery(BUYER, self.pid, "40000.00", True, "提前终验")
        self.svc.accept_delivery(BUYER, self.pid, "40000.00", False, "部分")
        with self.assertRaises(DomainError):
            self.svc.accept_delivery(BUYER, self.pid, "15000.00", True, "超额终验")
        self.svc.accept_delivery(BUYER, self.pid, "10000.00", True, "终验")

    def test_role_boundaries(self) -> None:
        with self.assertRaises(AuthError):
            self.svc.create_procurement(REVIEWER_A, "x", ["校"], "100.00")
        with self.assertRaises(AuthError):
            self.svc.audit_detail(principal("学校采购员", "b", "王采购"), self.pid)
        with self.assertRaises(AuthError):
            self.svc.ledger_entries(REVIEWER_A, self.pid)

    def test_tamper_is_detected(self) -> None:
        self._prepare_frozen()
        self.svc.sign_review(REVIEWER_A, self.pid, "同意")
        self.svc.sign_review(REVIEWER_B, self.pid, "同意")
        self.svc.sign_review(REVIEWER_C, self.pid, "同意")
        self.svc.award_contract(BUYER, self.pid, self.s1, "92000.00", "授标")
        good = self.svc.verify_chain(self.pid)
        self.assertTrue(good["ok"])
        # 绕过服务直接篡改历史分录
        with self.svc._lock:
            self.svc.conn.execute(
                "UPDATE ledger_entries SET amount_cents=1 WHERE seq=1")
        bad = self.svc.verify_chain(self.pid)
        self.assertFalse(bad["ok"])
        self.assertTrue(any("哈希" in p for p in bad["problems"]))


if __name__ == "__main__":
    unittest.main()
