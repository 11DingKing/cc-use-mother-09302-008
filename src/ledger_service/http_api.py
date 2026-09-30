"""基于标准库 http.server 的 JSON API。

公开端点无需令牌（公众脱敏摘要）；其余端点凭
Authorization: Bearer <token> 鉴权。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .service import AuthError, DomainError, LedgerService


class ApiHandler(BaseHTTPRequestHandler):
    @property
    def service(self) -> "LedgerService":
        return self.server.service  # type: ignore[attr-defined]

    # 静默常规访问日志，测试输出保持干净
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------- 工具方法

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise DomainError("请求体必须是 UTF-8 JSON")
        if not isinstance(data, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return data

    def _principal(self) -> dict:
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else None
        return self.service.authenticate(token)

    def _call(self, fn, *args, **kwargs):
        """统一执行服务方法并映射异常到 HTTP 状态码。"""
        try:
            result = fn(*args, **kwargs)
            return self._send(200, {"ok": True, "data": result})
        except AuthError as exc:
            code = 401 if "令牌" in str(exc) else 403
            return self._send(code, {"ok": False, "error": str(exc)})
        except DomainError as exc:
            return self._send(409, {"ok": False, "error": str(exc)})

    # ------------------------------------------------------------------ 路由

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.strip("/")
        parts = path.split("/") if path else []

        # 公众脱敏摘要：GET /public/procurements/{id}
        if len(parts) == 3 and parts[0] == "public" and parts[1] == "procurements":
            try:
                return self._send(200, {"ok": True, "data": self.service.public_summary(parts[2])})
            except DomainError as exc:
                return self._send(404, {"ok": False, "error": str(exc)})

        try:
            principal = self._principal()
        except AuthError as exc:
            return self._send(401, {"ok": False, "error": str(exc)})

        if path == "procurements":
            return self._call(self.service.list_procurements, principal)
        if len(parts) == 2 and parts[0] == "procurements":
            try:
                return self._send(200, {"ok": True,
                                        "data": self.service.get_procurement(principal, parts[1])})
            except DomainError as exc:
                return self._send(404, {"ok": False, "error": str(exc)})
        if len(parts) == 3 and parts[0] == "procurements" and parts[2] == "quotes":
            return self._call(self.service.quote_versions, principal, parts[1])
        if len(parts) == 3 and parts[0] == "procurements" and parts[2] == "ledger":
            return self._call(self.service.ledger_entries, principal, parts[1])
        if len(parts) == 3 and parts[0] == "procurements" and parts[2] == "audit":
            return self._call(self.service.audit_detail, principal, parts[1])

        return self._send(404, {"ok": False, "error": "未知端点"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.strip("/")
        parts = path.split("/") if path else []

        # 令牌签发（演示环境：按角色换取令牌）
        if path == "auth/token":
            data = self._read_json()
            role = data.get("role")
            person_id = data.get("person_id")
            name = data.get("name")
            if not all([role, person_id, name]):
                return self._send(409, {"ok": False,
                                        "error": "role/person_id/name 均为必填"})
            if role not in ("学校采购员", "评审委员", "供应方", "审计人员"):
                return self._send(409, {"ok": False, "error": "未知角色"})
            token = self.service.issue_token(role, person_id, name)
            return self._send(200, {"ok": True, "data": {"token": token, "role": role}})

        try:
            data = self._read_json()
            principal = self._principal()
        except AuthError as exc:
            return self._send(401, {"ok": False, "error": str(exc)})
        except DomainError as exc:
            return self._send(409, {"ok": False, "error": str(exc)})

        s = self.service

        if path == "suppliers":
            return self._call(s.create_supplier, principal,
                              data.get("name", ""), data.get("contact", ""))

        if path == "procurements":
            return self._call(s.create_procurement, principal,
                              data.get("title", ""), data.get("schools", []),
                              data.get("budget_yuan", ""), data.get("owner"))

        if len(parts) == 3 and parts[0] == "procurements" and parts[2] == "inquiry":
            return self._call(s.start_inquiry, principal, parts[1])

        if len(parts) == 3 and parts[0] == "procurements" and parts[2] == "reviewers":
            return self._call(s.add_reviewer, principal, parts[1],
                              data.get("person_id", ""), data.get("name", ""),
                              data.get("school", ""))

        if len(parts) == 3 and parts[0] == "procurements" and parts[2] == "conflicts":
            return self._call(s.declare_conflict, principal, parts[1],
                              data.get("supplier_id", ""), data.get("person_id", ""),
                              data.get("person_name", ""), data.get("relation", ""),
                              bool(data.get("recused", False)))

        if len(parts) == 3 and parts[0] == "procurements" and parts[2] == "quotes":
            return self._call(s.submit_quote, principal, parts[1],
                              data.get("supplier_id", ""), data.get("amount_yuan", ""),
                              data.get("note", ""))

        if len(parts) == 3 and parts[0] == "procurements" and parts[2] == "freeze":
            return self._call(s.freeze_for_review, principal, parts[1])

        if len(parts) == 3 and parts[0] == "procurements" and parts[2] == "signoffs":
            return self._call(s.sign_review, principal, parts[1],
                              data.get("decision", ""), data.get("comment", ""))

        if len(parts) == 3 and parts[0] == "procurements" and parts[2] == "contract":
            return self._call(s.award_contract, principal, parts[1],
                              data.get("supplier_id", ""), data.get("amount_yuan", ""),
                              data.get("reason", ""))

        if len(parts) == 3 and parts[0] == "procurements" and parts[2] == "contract-changes":
            return self._call(s.change_contract, principal, parts[1],
                              data.get("delta_yuan", ""), data.get("reason", ""))

        if len(parts) == 3 and parts[0] == "procurements" and parts[2] == "acceptances":
            return self._call(s.accept_delivery, principal, parts[1],
                              data.get("amount_yuan", ""),
                              bool(data.get("final", False)), data.get("note", ""))

        if len(parts) == 3 and parts[0] == "procurements" and parts[2] == "payments":
            ref = data.get("ref_acceptance_id")
            return self._call(s.pay, principal, parts[1],
                              data.get("amount_yuan", ""), ref)

        if len(parts) == 3 and parts[0] == "procurements" and parts[2] == "refunds":
            payment_id = data.get("payment_id")
            if payment_id is None:
                return self._send(409, {"ok": False, "error": "payment_id 为必填"})
            return self._call(s.refund, principal, parts[1],
                              int(payment_id), data.get("amount_yuan", ""),
                              data.get("reason", ""))

        if len(parts) == 3 and parts[0] == "procurements" and parts[2] == "settle":
            return self._call(s.complete_settlement, principal, parts[1])

        return self._send(404, {"ok": False, "error": "未知端点"})


def build_server(db_path: str, host: str = "127.0.0.1", port: int = 8080,
                 verbose: bool = False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), ApiHandler)
    server.service = LedgerService(db_path)  # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    return server
