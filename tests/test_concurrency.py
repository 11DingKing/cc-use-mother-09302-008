"""并发安全测试：同时签署、并发付款/退款、并发报价版本号不冲突。"""
from __future__ import annotations

import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ledger_service.service import DomainError, LedgerService


def principal(role: str, person_id: str, name: str) -> dict:
    return {"role": role, "person_id": person_id, "name": name}


BUYER = principal("学校采购员", "buyer-1", "王采购")
AUDITOR = principal("审计人员", "aud-1", "钱审计")


class ConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = LedgerService(":memory:")
        self.suppliers = [
            self.svc.create_supplier(BUYER, f"供应方{i}")["supplier_id"]
            for i in range(6)
        ]

    def tearDown(self) -> None:
        self.svc.close()

    def _contract_ready(self, n_reviewers: int = 5) -> str:
        proc = self.svc.create_procurement(
            BUYER, "联合采购", ["一小", "二小"], "200000.00")
        pid = proc["procurement_id"]
        self.svc.start_inquiry(BUYER, pid)
        for i in range(n_reviewers):
            self.svc.add_reviewer(BUYER, pid, f"r{i}", f"评委{i}", "一小")
        for i, sid in enumerate(self.suppliers[:3]):
            self.svc.submit_quote(BUYER, pid, sid, f"{100000 + 1000 * i}.00")
        self.svc.freeze_for_review(BUYER, pid)
        return pid

    def test_concurrent_signoffs_each_reviewer_once(self) -> None:
        pid = self._contract_ready(n_reviewers=5)
        winner = self.suppliers[0]

        def sign(i: int):
            p = principal("评审委员", f"r{i}", f"评委{i}")
            try:
                return self.svc.sign_review(p, pid, "同意")
            except DomainError:
                return None

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(sign, range(5)))
        self.assertEqual(5, sum(r is not None for r in results))

        # 同一评委并发重复签署：恰好一次成功
        def duplicate_sign(_):
            try:
                return self.svc.sign_review(
                    principal("评审委员", "r0", "评委0"), pid, "同意")
            except DomainError:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            dups = list(pool.map(duplicate_sign, range(8)))
        successes = sum(1 for r in dups if r is not None)
        self.assertEqual(successes, 0)  # r0 已签署，全部应失败

        self.svc.award_contract(BUYER, pid, winner, "100000.00", "中标")
        self.assertTrue(self.svc.verify_chain(pid)["ok"])

    def test_concurrent_payments_never_exceed_accepted(self) -> None:
        pid = self._contract_ready(n_reviewers=1)
        self.svc.sign_review(principal("评审委员", "r0", "评委0"), pid, "同意")
        self.svc.award_contract(BUYER, pid, self.suppliers[0], "100000.00", "中标")
        self.svc.accept_delivery(BUYER, pid, "100000.00", True, "终验")

        # 20 笔 10000 元付款并发，累计应为 200000，但验收只有 100000
        def pay(_):
            try:
                return self.svc.pay(BUYER, pid, "10000.00")
            except DomainError:
                return None

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(pay, range(20)))
        succeeded = [r for r in results if r is not None]
        self.assertEqual(len(succeeded), 10)
        totals = self.svc.get_procurement(BUYER, pid)["totals"]
        self.assertEqual(totals["paid"], "100000.00")
        self.assertEqual(totals["net_paid"], "100000.00")

    def test_concurrent_payments_and_refunds_keep_balance(self) -> None:
        pid = self._contract_ready(n_reviewers=1)
        self.svc.sign_review(principal("评审委员", "r0", "评委0"), pid, "同意")
        self.svc.award_contract(BUYER, pid, self.suppliers[0], "100000.00", "中标")
        self.svc.accept_delivery(BUYER, pid, "100000.00", True, "终验")
        payment = self.svc.pay(BUYER, pid, "100000.00")
        pay_id = payment["payment_id"]

        def refund(_):
            try:
                return self.svc.refund(BUYER, pid, pay_id, "30000.00", "冲正")
            except DomainError:
                return None

        with ThreadPoolExecutor(max_workers=16) as pool:
            refunds = list(pool.map(refund, range(10)))
        # 原付款 100000，最多成功 3 笔 30000（90000），第 4 笔必须失败
        succeeded = [r for r in refunds if r is not None]
        self.assertEqual(len(succeeded), 3)
        totals = self.svc.get_procurement(BUYER, pid)["totals"]
        self.assertEqual(totals["refunded"], "90000.00")
        self.assertEqual(totals["net_paid"], "10000.00")
        self.assertTrue(self.svc.verify_chain(pid)["ok"])

    def test_concurrent_quote_submissions_distinct_versions(self) -> None:
        proc = self.svc.create_procurement(BUYER, "t", ["一小"], "100000.00")
        pid = proc["procurement_id"]
        self.svc.start_inquiry(BUYER, pid)
        sid = self.suppliers[0]

        def submit(i: int):
            return self.svc.submit_quote(BUYER, pid, sid, f"{10000 + i}.00")

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(submit, range(12)))
        versions = sorted(r["version"] for r in results)
        self.assertEqual(versions, list(range(1, 13)))
        quotes = self.svc.quote_versions(AUDITOR, pid)
        active = [q for q in quotes if q["status"] != "已替换"]
        self.assertEqual(len(active), 1)
        # 最后插入的报价拿到最大版本号并成为唯一有效版本
        self.assertEqual(active[0]["version"], 12)
        amounts = {q["amount_yuan"] for q in quotes}
        self.assertEqual(amounts, {f"{10000 + i}.00" for i in range(12)})
        self.assertTrue(self.svc.verify_chain(pid)["ok"])

    def test_freeze_and_quotes_are_serialized(self) -> None:
        """报价提交与冻结并发：冻结后无一报价落入冻结窗口。"""
        proc = self.svc.create_procurement(BUYER, "t", ["一小"], "100000.00")
        pid = proc["procurement_id"]
        self.svc.start_inquiry(BUYER, pid)
        self.svc.add_reviewer(BUYER, pid, "r0", "评委0", "一小")
        for sid in self.suppliers[:3]:
            self.svc.submit_quote(BUYER, pid, sid, "90000.00")

        barrier_errors: list[Exception] = []

        def late_quote():
            try:
                self.svc.submit_quote(BUYER, pid, self.suppliers[3], "1.00")
            except DomainError as exc:
                barrier_errors.append(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(late_quote)
            f2 = pool.submit(self.svc.freeze_for_review, BUYER, pid)
            f1.result()
            f2.result()

        proc_row = self.svc.get_procurement(BUYER, pid)
        self.assertEqual(proc_row["status"], "评审")
        # 再报价必然被冻结拦截
        with self.assertRaises(DomainError):
            self.svc.submit_quote(BUYER, pid, self.suppliers[4], "2.00")
        self.assertTrue(self.svc.verify_chain(pid)["ok"])


if __name__ == "__main__":
    unittest.main()
