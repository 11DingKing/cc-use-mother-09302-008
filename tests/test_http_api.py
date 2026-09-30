"""HTTP API 测试：鉴权、公众脱敏、审计全量视图。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ledger_service.http_api import build_server


class HttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = build_server(":memory:", "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.service.close()
        self.server.server_close()

    def _request(self, method: str, path: str, payload: dict | None = None,
                 token: str | None = None, expect_error: bool = False):
        data = None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if expect_error:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            raise

    def _token(self, role: str, person_id: str, name: str) -> str:
        _, body = self._request("POST", "/auth/token",
                                {"role": role, "person_id": person_id, "name": name})
        return body["data"]["token"]

    def _seed_full_flow(self) -> tuple[str, str, str, str, str]:
        buyer = self._token("学校采购员", "b1", "王采购")
        auditor = self._token("审计人员", "a1", "钱审计")
        _, s1b = self._request("POST", "/suppliers", {"name": "梨园春艺术团"}, buyer)
        _, s2b = self._request("POST", "/suppliers", {"name": "锦韵演出公司"}, buyer)
        s1, s2 = s1b["data"]["supplier_id"], s2b["data"]["supplier_id"]
        _, pb = self._request("POST", "/procurements", {
            "title": "三校联合非遗演出",
            "schools": ["一小", "二小"],
            "budget_yuan": "50000.00",
        }, buyer)
        pid = pb["data"]["procurement_id"]
        self._request("POST", f"/procurements/{pid}/inquiry", token=buyer)
        self._request("POST", f"/procurements/{pid}/reviewers",
                      {"person_id": "r1", "name": "李评委", "school": "一小"}, buyer)
        self._request("POST", f"/procurements/{pid}/quotes",
                      {"supplier_id": s1, "amount_yuan": "48000.00"}, buyer)
        self._request("POST", f"/procurements/{pid}/quotes",
                      {"supplier_id": s2, "amount_yuan": "46000.00"}, buyer)
        self._request("POST", f"/procurements/{pid}/freeze", token=buyer)
        r1 = self._token("评审委员", "r1", "李评委")
        self._request("POST", f"/procurements/{pid}/signoffs",
                      {"decision": "同意"}, r1)
        self._request("POST", f"/procurements/{pid}/contract",
                      {"supplier_id": s2, "amount_yuan": "46000.00",
                       "reason": "评审通过"}, buyer)
        self._request("POST", f"/procurements/{pid}/acceptances",
                      {"amount_yuan": "46000.00", "final": True, "note": "验收合格"}, buyer)
        self._request("POST", f"/procurements/{pid}/payments",
                      {"amount_yuan": "46000.00"}, buyer)
        self._request("POST", f"/procurements/{pid}/settle", token=buyer)
        return pid, s2, buyer, auditor, r1

    def test_public_summary_is_masked(self) -> None:
        pid, s2, *_ = self._seed_full_flow()
        status, body = self._request("GET", f"/public/procurements/{pid}")
        self.assertEqual(status, 200)
        data = body["data"]
        self.assertEqual(data["status"], "结算")
        self.assertEqual(data["awarded_supplier_name"], "锦韵演出公司")
        self.assertEqual(data["contract_yuan"], "46000.00")
        self.assertEqual(data["paid_net_yuan"], "46000.00")
        # 脱敏：不含评委姓名、未中标供应方及其报价、回避细节
        text = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("李评委", text)
        self.assertNotIn("梨园春", text)
        self.assertNotIn("48000.00", text)
        self.assertNotIn("reviewers", text)
        self.assertNotIn("signoffs", text)

    def test_audit_detail_is_complete_and_verified(self) -> None:
        pid, s2, buyer, auditor, _ = self._seed_full_flow()
        status, body = self._request("GET", f"/procurements/{pid}/audit", token=auditor)
        self.assertEqual(status, 200)
        data = body["data"]
        self.assertTrue(data["verification"]["ok"], data["verification"]["problems"])
        self.assertEqual(len(data["reviewers"]), 1)
        self.assertEqual(len(data["signoffs"]), 1)
        # 审计可见全部报价（含未中标方）
        suppliers_seen = {q["supplier_name"] for q in data["quote_versions"]}
        self.assertEqual(suppliers_seen, {"梨园春艺术团", "锦韵演出公司"})
        # 可见责任人
        self.assertEqual(data["payments"][0]["amount_yuan"], "46000.00")
        self.assertTrue(any("王采购" in e["actor"] for e in data["audit_log"]))
        # 采购方不能访问审计端点
        self._request("GET", f"/procurements/{pid}/audit", token=buyer,
                      expect_error=True)

    def test_ledger_endpoint_hash_chain(self) -> None:
        pid, *_ = self._seed_full_flow()
        buyer = self._token("学校采购员", "b1", "王采购")
        _, body = self._request("GET", f"/procurements/{pid}/ledger", token=buyer)
        entries = body["data"]["entries"]
        events = [e["event_type"] for e in entries]
        self.assertEqual(events, ["BUDGET", "COMMIT", "ACCEPT", "PAYMENT"])
        self.assertEqual(entries[0]["prev_hash"], "GENESIS")
        for prev, cur in zip(entries, entries[1:]):
            self.assertEqual(cur["prev_hash"], prev["entry_hash"])
        for e in entries:
            debit = sum(l["debit_cents"] for l in e["lines"])
            credit = sum(l["credit_cents"] for l in e["lines"])
            self.assertEqual(debit, credit)

    def test_auth_required(self) -> None:
        code, body = self._request("GET", "/procurements", expect_error=True)
        self.assertEqual(code, 401)
        code, body = self._request(
            "GET", "/procurements", token="deadbeef", expect_error=True)
        self.assertEqual(code, 401)

    def test_freeze_conflict_returns_409(self) -> None:
        buyer = self._token("学校采购员", "b1", "王采购")
        _, pb = self._request("POST", "/procurements", {
            "title": "t", "schools": ["一小"], "budget_yuan": "100.00"}, buyer)
        pid = pb["data"]["procurement_id"]
        # 询价未开始直接冻结 → 409
        code, body = self._request(
            "POST", f"/procurements/{pid}/freeze", token=buyer, expect_error=True)
        self.assertEqual(code, 409)
        self.assertIn("不能开始评审", body["error"])


if __name__ == "__main__":
    unittest.main()
