"""采购透明台账领域服务。

金额一律以整数“分”存储与比较，接口层负责“元”字符串转换。
所有写操作在同一把服务锁 + SQLite IMMEDIATE 事务内完成，
保证并发签署、付款、退款不会破坏金额平衡。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .storage import connect, init_db


class DomainError(Exception):
    """业务规则冲突（映射为 HTTP 409）。"""


class AuthError(Exception):
    """身份或权限不足（映射为 HTTP 401/403）。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


def yuan_to_cents(text: str | int | float) -> int:
    """把“元”金额字符串安全转换为整数分。"""
    if isinstance(text, int):
        return text * 100
    try:
        value = Decimal(str(text)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise DomainError(f"非法金额：{text!r}")
    if value <= 0:
        raise DomainError("金额必须为正数")
    return int(value.scaleb(2))


def cents_to_yuan(value: int) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}{value // 100}.{value % 100:02d}"


def _canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# 分录模板：(借方科目, 贷方科目)
_POSTINGS = {
    "BUDGET": ("5101", "5102"),
    "COMMIT": ("5201", "5202"),
    "ACCEPT": ("3101", "2101"),
    "PAYMENT": ("2101", "1101"),
    "REFUND": ("1101", "2101"),
}


def _posting_lines(event_type: str, amount_cents: int) -> list[dict]:
    """生成借贷平衡的分录行；差额为负的合同变更自动反向。"""
    amount_cents = int(amount_cents)
    if event_type == "COMMIT_CHANGE":
        if amount_cents > 0:
            debit, credit = "5201", "5202"
            positive = amount_cents
        else:
            debit, credit = "5202", "5201"
            positive = -amount_cents
    else:
        debit, credit = _POSTINGS[event_type]
        positive = amount_cents
    return [
        {"account": debit, "debit_cents": positive, "credit_cents": 0},
        {"account": credit, "debit_cents": 0, "credit_cents": positive},
    ]


class LedgerService:
    def __init__(self, db_path: str = ":memory:"):
        self.conn: sqlite3.Connection = connect(db_path)
        init_db(self.conn)
        self._lock = threading.RLock()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ 认证

    def issue_token(self, role: str, person_id: str, name: str) -> str:
        token = uuid.uuid4().hex
        with self._lock:
            self.conn.execute(
                "INSERT INTO auth_tokens(token, role, person_id, name, created_at) "
                "VALUES (?,?,?,?,?)",
                (token, role, person_id, name, now_iso()),
            )
        return token

    def authenticate(self, token: str | None) -> dict:
        if not token:
            raise AuthError("缺少访问令牌")
        with self._lock:
            row = self.conn.execute(
                "SELECT role, person_id, name FROM auth_tokens WHERE token=?", (token,)
            ).fetchone()
        if row is None:
            raise AuthError("令牌无效")
        return {"role": row["role"], "person_id": row["person_id"], "name": row["name"]}

    def require_role(self, principal: dict, *roles: str) -> None:
        if principal["role"] not in roles:
            raise AuthError(f"需要角色：{'、'.join(roles)}")

    def _audit(self, conn: sqlite3.Connection, actor: str, action: str,
               procurement_id: str | None, detail: dict | None = None) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor, action, procurement_id, detail_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (actor, action, procurement_id, json.dumps(detail or {}, ensure_ascii=False), now_iso()),
        )

    def _get_proc(self, conn: sqlite3.Connection, pid: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM procurements WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise DomainError("采购需求不存在")
        return row

    def _totals(self, conn: sqlite3.Connection, pid: str) -> dict:
        contract = conn.execute(
            "SELECT amount_cents FROM contracts WHERE procurement_id=?", (pid,)
        ).fetchone()
        committed = contract["amount_cents"] if contract else 0
        committed += conn.execute(
            "SELECT COALESCE(SUM(delta_cents),0) AS v FROM contract_changes WHERE procurement_id=?",
            (pid,),
        ).fetchone()["v"]
        accepted = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS v FROM acceptances WHERE procurement_id=?",
            (pid,),
        ).fetchone()["v"]
        paid = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS v FROM payments WHERE procurement_id=?",
            (pid,),
        ).fetchone()["v"]
        refunded = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS v FROM refunds WHERE procurement_id=?",
            (pid,),
        ).fetchone()["v"]
        return {
            "committed": committed,
            "accepted": accepted,
            "paid": paid,
            "refunded": refunded,
            "net_paid": paid - refunded,
        }

    def _supplier_barred(self, conn: sqlite3.Connection, pid: str, supplier_id: str) -> bool:
        """最新声明中仍存在未回避的关联关系即禁止报价与授标。"""
        row = conn.execute(
            "SELECT recused FROM conflict_declarations "
            "WHERE procurement_id=? AND supplier_id=? "
            "ORDER BY id DESC LIMIT 1",
            (pid, supplier_id),
        ).fetchone()
        return row is not None and row["recused"] == 0

    def _reviewer_conflict(self, conn: sqlite3.Connection, pid: str,
                           person_id: str) -> sqlite3.Row | None:
        """评委本人在全部供应方申报中的最新关联状态。"""
        return conn.execute(
            "SELECT supplier_id, recused, relation FROM conflict_declarations "
            "WHERE procurement_id=? AND person_id=? ORDER BY id DESC LIMIT 1",
            (pid, person_id),
        ).fetchone()

    # ------------------------------------------------------------------ 台账

    def _post(self, conn: sqlite3.Connection, pid: str, event_type: str,
              event_ref: str, amount_cents: int, responsible: str) -> dict:
        """追加一条不可变复式分录（哈希链）。"""
        lines = _posting_lines(event_type, amount_cents)
        debit_sum = sum(line["debit_cents"] for line in lines)
        credit_sum = sum(line["credit_cents"] for line in lines)
        if debit_sum != credit_sum:
            raise DomainError("分录借贷不平衡")
        prev = conn.execute(
            "SELECT entry_hash FROM ledger_entries WHERE procurement_id=? ORDER BY seq DESC LIMIT 1",
            (pid,),
        ).fetchone()
        prev_hash = prev["entry_hash"] if prev else "GENESIS"
        created_at = now_iso()
        body = _canonical({
            "procurement_id": pid,
            "event_type": event_type,
            "event_ref": event_ref,
            "lines": lines,
            "amount_cents": amount_cents,
            "responsible": responsible,
            "created_at": created_at,
            "prev_hash": prev_hash,
        })
        entry_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        cur = conn.execute(
            "INSERT INTO ledger_entries(procurement_id, event_type, event_ref, lines_json, "
            "amount_cents, responsible, created_at, prev_hash, entry_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, event_type, event_ref, json.dumps(lines, ensure_ascii=False),
             amount_cents, responsible, created_at, prev_hash, entry_hash),
        )
        return {"seq": cur.lastrowid, "entry_hash": entry_hash, "lines": lines}

    # ------------------------------------------------------------------ 需求

    def create_procurement(self, principal: dict, title: str, schools: list[str],
                           budget_yuan: str, owner: str | None = None) -> dict:
        self.require_role(principal, "学校采购员")
        if not title or not schools:
            raise DomainError("标题与联合学校不能为空")
        budget = yuan_to_cents(budget_yuan)
        pid = new_id()
        actor = owner or principal["name"]
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO procurements(id, title, schools_json, budget_cents, owner, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (pid, title, json.dumps(schools, ensure_ascii=False), budget, actor, now_iso()),
                )
                entry = self._post(conn, pid, "BUDGET", f"procurement:{pid}", budget, actor)
                self._audit(conn, principal["name"], "创建需求并登记预算", pid,
                            {"budget_cents": budget})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_procurement(principal, pid)

    def start_inquiry(self, principal: dict, pid: str) -> dict:
        self.require_role(principal, "学校采购员")
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                proc = self._get_proc(conn, pid)
                if proc["status"] != "需求":
                    raise DomainError(f"当前状态为{proc['status']}，不能进入询价")
                conn.execute("UPDATE procurements SET status='询价' WHERE id=?", (pid,))
                self._audit(conn, principal["name"], "进入询价", pid)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_procurement(principal, pid)

    def add_reviewer(self, principal: dict, pid: str, person_id: str,
                     name: str, school: str) -> dict:
        self.require_role(principal, "学校采购员")
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                proc = self._get_proc(conn, pid)
                if proc["status"] not in ("需求", "询价"):
                    raise DomainError("评审开始后评委名单冻结，不得新增评委")
                conn.execute(
                    "INSERT INTO reviewers(procurement_id, person_id, name, school, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (pid, person_id, name, school, now_iso()),
                )
                self._audit(conn, principal["name"], "登记评委", pid,
                            {"person_id": person_id})
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                raise DomainError("评委已存在")
            except Exception:
                conn.rollback()
                raise
        return {"procurement_id": pid, "person_id": person_id, "name": name}

    # ------------------------------------------------------------- 供应方/回避

    def create_supplier(self, principal: dict, name: str, contact: str = "") -> dict:
        self.require_role(principal, "学校采购员", "供应方")
        sid = new_id()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO suppliers(id, name, contact, created_at) VALUES (?,?,?,?)",
                (sid, name, contact, now_iso()),
            )
            self._audit(self.conn, principal["name"], "登记供应方", sid, {"name": name})
        return {"supplier_id": sid, "name": name}

    def declare_conflict(self, principal: dict, pid: str, supplier_id: str,
                         person_id: str, person_name: str, relation: str,
                         recused: bool) -> dict:
        self.require_role(principal, "学校采购员", "评审委员", "供应方")
        if not relation:
            raise DomainError("关联关系描述不能为空")
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                proc = self._get_proc(conn, pid)
                if proc["status"] not in ("需求", "询价"):
                    raise DomainError("评审开始后回避声明冻结，不得再补报或变更")
                exists = conn.execute("SELECT 1 FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
                if exists is None:
                    raise DomainError("供应方不存在")
                cur = conn.execute(
                    "INSERT INTO conflict_declarations(procurement_id, supplier_id, person_id, "
                    "person_name, relation, recused, created_at) VALUES (?,?,?,?,?,?,?)",
                    (pid, supplier_id, person_id, person_name, relation,
                     1 if recused else 0, now_iso()),
                )
                self._audit(conn, principal["name"], "申报供应关系/回避声明", pid, {
                    "supplier_id": supplier_id,
                    "person_id": person_id,
                    "recused": recused,
                })
                conn.commit()
                return {"declaration_id": cur.lastrowid, "recused": recused}
            except Exception:
                conn.rollback()
                raise

    # ------------------------------------------------------------------ 报价

    def submit_quote(self, principal: dict, pid: str, supplier_id: str,
                     amount_yuan: str, note: str = "") -> dict:
        self.require_role(principal, "供应方", "学校采购员")
        amount = yuan_to_cents(amount_yuan)
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                proc = self._get_proc(conn, pid)
                if proc["status"] != "询价":
                    raise DomainError("评审开始即冻结报价，不得新增或替换")
                if principal["role"] == "供应方" and principal["person_id"] != supplier_id:
                    raise AuthError("供应方只能提交本主体报价")
                exists = conn.execute("SELECT 1 FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
                if exists is None:
                    raise DomainError("供应方不存在")
                if self._supplier_barred(conn, pid, supplier_id):
                    raise DomainError("存在未回避的关联关系，不得提交报价")
                version = conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 AS v FROM quotes "
                    "WHERE procurement_id=? AND supplier_id=?",
                    (pid, supplier_id),
                ).fetchone()["v"]
                # 旧版本留痕但不再可比
                conn.execute(
                    "UPDATE quotes SET status='已替换' WHERE procurement_id=? AND supplier_id=? "
                    "AND status='有效'",
                    (pid, supplier_id),
                )
                cur = conn.execute(
                    "INSERT INTO quotes(procurement_id, supplier_id, version, amount_cents, "
                    "note, status, created_at) VALUES (?,?,?,?,?,'有效',?)",
                    (pid, supplier_id, version, amount, note, now_iso()),
                )
                self._audit(conn, principal["name"], "提交报价版本", pid, {
                    "supplier_id": supplier_id, "version": version,
                    "amount_cents": amount,
                })
                conn.commit()
                return {"quote_id": cur.lastrowid, "version": version,
                        "amount_cents": amount, "status": "有效"}
            except Exception:
                conn.rollback()
                raise

    def freeze_for_review(self, principal: dict, pid: str) -> dict:
        """评审开始：冻结全部可比报价与评委名单。"""
        self.require_role(principal, "学校采购员")
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                proc = self._get_proc(conn, pid)
                if proc["status"] != "询价":
                    raise DomainError(f"当前状态为{proc['status']}，不能开始评审")
                count = conn.execute(
                    "SELECT COUNT(*) AS c FROM quotes WHERE procurement_id=? AND status='有效'",
                    (pid,),
                ).fetchone()["c"]
                if count == 0:
                    raise DomainError("没有有效报价，不能开始评审")
                reviewers = conn.execute(
                    "SELECT COUNT(*) AS c FROM reviewers WHERE procurement_id=?", (pid,)
                ).fetchone()["c"]
                if reviewers == 0:
                    raise DomainError("未登记评委，不能开始评审")
                frozen_at = now_iso()
                conn.execute(
                    "UPDATE quotes SET status='冻结' WHERE procurement_id=? AND status='有效'",
                    (pid,),
                )
                conn.execute(
                    "UPDATE procurements SET status='评审', frozen_at=? WHERE id=?",
                    (frozen_at, pid),
                )
                self._audit(conn, principal["name"], "开始评审并冻结报价", pid,
                            {"frozen_quotes": count})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_procurement(principal, pid)

    # ------------------------------------------------------------------ 签署

    def sign_review(self, principal: dict, pid: str, decision: str,
                    comment: str = "") -> dict:
        self.require_role(principal, "评审委员")
        if decision not in ("同意", "反对", "弃权"):
            raise DomainError("评审意见必须是 同意/反对/弃权")
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                proc = self._get_proc(conn, pid)
                if proc["status"] != "评审":
                    raise DomainError(f"当前状态为{proc['status']}，不能签署评审")
                reviewer = conn.execute(
                    "SELECT * FROM reviewers WHERE procurement_id=? AND person_id=?",
                    (pid, principal["person_id"]),
                ).fetchone()
                if reviewer is None:
                    raise AuthError("不在冻结后的评委名单内")
                conflict = self._reviewer_conflict(conn, pid, principal["person_id"])
                if conflict is not None and conflict["recused"] == 0:
                    raise DomainError("存在未回避的关联关系，该评委不得参与评审签署")
                if conflict is not None and conflict["recused"] == 1 and decision != "弃权":
                    raise DomainError("已声明回避的评委只能签署“弃权”")
                signed_at = now_iso()
                try:
                    cur = conn.execute(
                        "INSERT INTO review_signoffs(procurement_id, reviewer_id, reviewer_name, "
                        "decision, comment, signed_at) VALUES (?,?,?,?,?,?)",
                        (pid, principal["person_id"], principal["name"],
                         decision, comment, signed_at),
                    )
                except sqlite3.IntegrityError:
                    raise DomainError("不得重复签署")
                self._audit(conn, principal["name"], "评审签署", pid, {"decision": decision})
                conn.commit()
                return {"signoff_id": cur.lastrowid, "reviewer": principal["name"],
                        "decision": decision, "signed_at": signed_at}
            except Exception:
                conn.rollback()
                raise

    def _signoff_quorum(self, conn: sqlite3.Connection, pid: str) -> tuple[int, int, bool]:
        reviewers = conn.execute(
            "SELECT COUNT(*) AS c FROM reviewers WHERE procurement_id=?", (pid,)
        ).fetchone()["c"]
        rows = conn.execute(
            "SELECT decision FROM review_signoffs WHERE procurement_id=?", (pid,)
        ).fetchall()
        signed = len(rows)
        complete = (
            signed == reviewers
            and all(r["decision"] in ("同意", "弃权") for r in rows)
            and any(r["decision"] == "同意" for r in rows)
        )
        return reviewers, signed, complete

    # ------------------------------------------------------------------ 合同

    def award_contract(self, principal: dict, pid: str, supplier_id: str,
                       amount_yuan: str, reason: str) -> dict:
        self.require_role(principal, "学校采购员")
        amount = yuan_to_cents(amount_yuan)
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                proc = self._get_proc(conn, pid)
                if proc["status"] != "评审":
                    raise DomainError(f"当前状态为{proc['status']}，不能授标签约")
                _, _, quorum = self._signoff_quorum(conn, pid)
                if not quorum:
                    raise DomainError("评委未全部完成有效签署，不得授标")
                if self._supplier_barred(conn, pid, supplier_id):
                    raise DomainError("供应方存在未回避关联关系，不得授标")
                quote = conn.execute(
                    "SELECT * FROM quotes WHERE procurement_id=? AND supplier_id=? AND status='冻结'",
                    (pid, supplier_id),
                ).fetchone()
                if quote is None:
                    raise DomainError("被授标供应方必须持有冻结报价")
                if quote["amount_cents"] != amount:
                    raise DomainError(
                        f"合同价 {cents_to_yuan(amount)} 元与冻结报价 "
                        f"{cents_to_yuan(quote['amount_cents'])} 元不一致"
                    )
                if amount > proc["budget_cents"]:
                    raise DomainError("合同承诺超出预算")
                signed_at = now_iso()
                conn.execute(
                    "INSERT INTO contracts(procurement_id, supplier_id, amount_cents, signed_at) "
                    "VALUES (?,?,?,?)",
                    (pid, supplier_id, amount, signed_at),
                )
                conn.execute(
                    "UPDATE procurements SET status='履约', awarded_supplier_id=? WHERE id=?",
                    (supplier_id, pid),
                )
                self._post(conn, pid, "COMMIT", f"contract:{pid}", amount, principal["name"])
                self._audit(conn, principal["name"], "授标签订合同", pid, {
                    "supplier_id": supplier_id, "amount_cents": amount, "reason": reason,
                })
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                raise DomainError("合同已存在")
            except Exception:
                conn.rollback()
                raise
        return self.get_procurement(principal, pid)

    def change_contract(self, principal: dict, pid: str, delta_yuan: str,
                        reason: str) -> dict:
        """合同变更：差额可正可负，受预算与已验收金额双重约束。"""
        self.require_role(principal, "学校采购员")
        text = str(delta_yuan).strip()
        negative = text.startswith("-")
        if negative:
            text = text[1:]
        magnitude = yuan_to_cents(text)
        delta = -magnitude if negative else magnitude
        if not reason:
            raise DomainError("变更原因不能为空")
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                proc = self._get_proc(conn, pid)
                if proc["status"] != "履约":
                    raise DomainError("只有履约中的合同可以变更")
                totals = self._totals(conn, pid)
                new_committed = totals["committed"] + delta
                if new_committed > proc["budget_cents"]:
                    raise DomainError("变更后合同承诺超出预算")
                if new_committed < totals["accepted"]:
                    raise DomainError("变更后合同金额低于已验收金额")
                cur = conn.execute(
                    "INSERT INTO contract_changes(procurement_id, delta_cents, reason, operator, "
                    "created_at) VALUES (?,?,?,?,?)",
                    (pid, delta, reason, principal["name"], now_iso()),
                )
                self._post(conn, pid, "COMMIT_CHANGE",
                           f"contract_change:{cur.lastrowid}", delta, principal["name"])
                self._audit(conn, principal["name"], "合同变更", pid, {
                    "change_id": cur.lastrowid, "delta_cents": delta, "reason": reason,
                })
                conn.commit()
                return {"change_id": cur.lastrowid, "delta_cents": delta,
                        "committed_cents": new_committed}
            except Exception:
                conn.rollback()
                raise

    # ------------------------------------------------------------------ 验收

    def accept_delivery(self, principal: dict, pid: str, amount_yuan: str,
                        final_: bool, note: str = "") -> dict:
        self.require_role(principal, "学校采购员")
        amount = yuan_to_cents(amount_yuan)
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                proc = self._get_proc(conn, pid)
                if proc["status"] != "履约":
                    raise DomainError(f"当前状态为{proc['status']}，不能登记验收")
                totals = self._totals(conn, pid)
                new_accepted = totals["accepted"] + amount
                if new_accepted > totals["committed"]:
                    raise DomainError(
                        f"累计验收 {cents_to_yuan(new_accepted)} 元超出合同承诺 "
                        f"{cents_to_yuan(totals['committed'])} 元"
                    )
                if final_ and new_accepted != totals["committed"]:
                    raise DomainError(
                        "最终验收时累计验收金额必须等于合同金额（含变更），"
                        f"当前 {cents_to_yuan(new_accepted)} / {cents_to_yuan(totals['committed'])} 元"
                    )
                cur = conn.execute(
                    "INSERT INTO acceptances(procurement_id, amount_cents, final, note, accepted_by, "
                    "created_at) VALUES (?,?,?,?,?,?)",
                    (pid, amount, 1 if final_ else 0, note, principal["name"], now_iso()),
                )
                self._post(conn, pid, "ACCEPT", f"acceptance:{cur.lastrowid}",
                           amount, principal["name"])
                self._audit(conn, principal["name"],
                            "最终验收" if final_ else "部分验收", pid,
                            {"acceptance_id": cur.lastrowid, "amount_cents": amount})
                conn.commit()
                return {"acceptance_id": cur.lastrowid, "amount_cents": amount,
                        "final": bool(final_), "accepted_total_cents": new_accepted}
            except Exception:
                conn.rollback()
                raise

    # ------------------------------------------------------------- 付款/退款

    def pay(self, principal: dict, pid: str, amount_yuan: str,
            ref_acceptance_id: int | None = None) -> dict:
        self.require_role(principal, "学校采购员")
        amount = yuan_to_cents(amount_yuan)
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                proc = self._get_proc(conn, pid)
                if proc["status"] != "履约":
                    raise DomainError("签约验收后方可付款")
                totals = self._totals(conn, pid)
                if totals["net_paid"] + amount > totals["accepted"]:
                    raise DomainError(
                        f"付款后净额 {cents_to_yuan(totals['net_paid'] + amount)} 元"
                        f"超过累计验收 {cents_to_yuan(totals['accepted'])} 元，缺少付款依据"
                    )
                cur = conn.execute(
                    "INSERT INTO payments(procurement_id, amount_cents, ref_acceptance_id, created_at) "
                    "VALUES (?,?,?,?)",
                    (pid, amount, ref_acceptance_id, now_iso()),
                )
                self._post(conn, pid, "PAYMENT", f"payment:{cur.lastrowid}",
                           amount, principal["name"])
                self._audit(conn, principal["name"], "付款", pid,
                            {"payment_id": cur.lastrowid, "amount_cents": amount})
                conn.commit()
                return {"payment_id": cur.lastrowid, "amount_cents": amount}
            except Exception:
                conn.rollback()
                raise

    def refund(self, principal: dict, pid: str, payment_id: int,
               amount_yuan: str, reason: str = "") -> dict:
        """退款冲正：只追加反向分录，不改写原付款。"""
        self.require_role(principal, "学校采购员")
        amount = yuan_to_cents(amount_yuan)
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                proc = self._get_proc(conn, pid)
                if proc["status"] != "履约":
                    raise DomainError("当前阶段不能登记退款")
                payment = conn.execute(
                    "SELECT * FROM payments WHERE id=? AND procurement_id=?",
                    (payment_id, pid),
                ).fetchone()
                if payment is None:
                    raise DomainError("被冲正付款不存在")
                already = conn.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) AS v FROM refunds WHERE payment_id=?",
                    (payment_id,),
                ).fetchone()["v"]
                if already + amount > payment["amount_cents"]:
                    raise DomainError(
                        f"该笔付款累计退款将超过原付款 "
                        f"{cents_to_yuan(payment['amount_cents'])} 元"
                    )
                cur = conn.execute(
                    "INSERT INTO refunds(procurement_id, payment_id, amount_cents, reason, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (pid, payment_id, amount, reason, now_iso()),
                )
                self._post(conn, pid, "REFUND", f"refund:{cur.lastrowid}",
                           amount, principal["name"])
                self._audit(conn, principal["name"], "退款冲正", pid, {
                    "refund_id": cur.lastrowid, "payment_id": payment_id,
                    "amount_cents": amount,
                })
                conn.commit()
                return {"refund_id": cur.lastrowid, "payment_id": payment_id,
                        "amount_cents": amount}
            except Exception:
                conn.rollback()
                raise

    def complete_settlement(self, principal: dict, pid: str) -> dict:
        self.require_role(principal, "学校采购员")
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                proc = self._get_proc(conn, pid)
                if proc["status"] != "履约":
                    raise DomainError(f"当前状态为{proc['status']}，不能进入结算")
                totals = self._totals(conn, pid)
                if totals["accepted"] != totals["committed"]:
                    raise DomainError("须完成最终验收（验收额等于合同额）后方可结算")
                if totals["net_paid"] != totals["accepted"]:
                    raise DomainError(
                        f"付款净额 {cents_to_yuan(totals['net_paid'])} 元"
                        f"不等于验收额 {cents_to_yuan(totals['accepted'])} 元，结清后方可结算"
                    )
                conn.execute("UPDATE procurements SET status='结算' WHERE id=?", (pid,))
                self._audit(conn, principal["name"], "进入结算", pid)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_procurement(principal, pid)

    # ------------------------------------------------------------------ 查询

    def _procurement_dict(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        schools = json.loads(row["schools_json"])
        supplier = None
        if row["awarded_supplier_id"]:
            s = conn.execute("SELECT name FROM suppliers WHERE id=?",
                             (row["awarded_supplier_id"],)).fetchone()
            supplier = s["name"] if s else None
        return {
            "procurement_id": row["id"],
            "title": row["title"],
            "schools": schools,
            "budget_yuan": cents_to_yuan(row["budget_cents"]),
            "status": row["status"],
            "owner": row["owner"],
            "frozen_at": row["frozen_at"],
            "awarded_supplier_name": supplier,
            "totals": {k: cents_to_yuan(v) if k != "net_paid" else cents_to_yuan(v)
                       for k, v in self._totals(conn, row["id"]).items()},
        }

    def list_procurements(self, principal: dict) -> list[dict]:
        self.require_role(principal, "学校采购员", "审计人员", "评审委员", "供应方")
        with self._lock:
            rows = self.conn.execute("SELECT * FROM procurements ORDER BY created_at").fetchall()
            return [self._procurement_dict(self.conn, r) for r in rows]

    def get_procurement(self, principal: dict, pid: str) -> dict:
        self.require_role(principal, "学校采购员", "审计人员", "评审委员", "供应方")
        with self._lock:
            row = self._get_proc(self.conn, pid)
            return self._procurement_dict(self.conn, row)

    def quote_versions(self, principal: dict, pid: str) -> list[dict]:
        """审计视角：全部报价版本（含已替换、冻结），可复核比价依据。"""
        self.require_role(principal, "审计人员", "学校采购员")
        with self._lock:
            self._get_proc(self.conn, pid)
            rows = self.conn.execute(
            "SELECT q.id, q.supplier_id, s.name AS supplier_name, q.version, q.amount_cents, "
            "q.status, q.note, q.created_at, "
            "COALESCE((SELECT c.recused FROM conflict_declarations c "
            " WHERE c.procurement_id=q.procurement_id AND c.supplier_id=q.supplier_id "
            " ORDER BY c.id DESC LIMIT 1), 1) = 0 AS barred "
            "FROM quotes q JOIN suppliers s ON s.id=q.supplier_id "
            "WHERE q.procurement_id=? ORDER BY q.version, q.id",
            (pid,),
            ).fetchall()
            return [{
                "quote_id": r["id"],
                "supplier_id": r["supplier_id"],
                "supplier_name": r["supplier_name"],
                "version": r["version"],
                "amount_yuan": cents_to_yuan(r["amount_cents"]),
                "status": r["status"],
                "note": r["note"],
                "created_at": r["created_at"],
                "supplier_barred_by_conflict": bool(r["barred"]),
            } for r in rows]

    def public_summary(self, pid: str) -> dict:
        """公众视角：脱敏摘要，不含任何人员信息与未中标报价。"""
        with self._lock:
            row = self._get_proc(self.conn, pid)
            totals = self._totals(self.conn, pid)
            quote_count = self.conn.execute(
                "SELECT COUNT(DISTINCT supplier_id) AS c FROM quotes WHERE procurement_id=?",
                (pid,),
            ).fetchone()["c"]
            summary = {
                "procurement_id": row["id"],
                "title": row["title"],
                "schools": json.loads(row["schools_json"]),
                "status": row["status"],
                "budget_yuan": cents_to_yuan(row["budget_cents"]),
                "quoted_supplier_count": quote_count,
                "awarded_supplier_name": None,
                "contract_yuan": None,
                "accepted_yuan": cents_to_yuan(totals["accepted"]),
                "paid_net_yuan": cents_to_yuan(totals["net_paid"]),
                "refunded_yuan": cents_to_yuan(totals["refunded"]),
            }
            if row["awarded_supplier_id"]:
                s = self.conn.execute(
                    "SELECT name FROM suppliers WHERE id=?", (row["awarded_supplier_id"],)
                ).fetchone()
                summary["awarded_supplier_name"] = s["name"] if s else None
                summary["contract_yuan"] = cents_to_yuan(totals["committed"])
            return summary

    def audit_detail(self, principal: dict, pid: str) -> dict:
        """审计人员：沿不可变分录复核预算、付款依据与责任人的全量视图。"""
        self.require_role(principal, "审计人员")
        with self._lock:
            proc = self._get_proc(self.conn, pid)
            detail = self._procurement_dict(self.conn, proc)

            def rows(sql: str, params: tuple = ()) -> list[dict]:
                return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

            reviewers = rows(
                "SELECT person_id, name, school, created_at FROM reviewers WHERE procurement_id=?",
                (pid,))
            conflicts = rows(
                "SELECT id, supplier_id, person_id, person_name, relation, recused, created_at "
                "FROM conflict_declarations WHERE procurement_id=?", (pid,))
            signoffs = rows(
                "SELECT reviewer_id, reviewer_name, decision, comment, signed_at "
                "FROM review_signoffs WHERE procurement_id=? ORDER BY id", (pid,))
            changes = rows(
                "SELECT id, delta_cents, reason, operator, created_at "
                "FROM contract_changes WHERE procurement_id=? ORDER BY id", (pid,))
            acceptances = rows(
                "SELECT id, amount_cents, final, note, accepted_by, created_at "
                "FROM acceptances WHERE procurement_id=? ORDER BY id", (pid,))
            payments = rows(
                "SELECT id, amount_cents, ref_acceptance_id, created_at "
                "FROM payments WHERE procurement_id=? ORDER BY id", (pid,))
            refunds = rows(
                "SELECT id, payment_id, amount_cents, reason, created_at "
                "FROM refunds WHERE procurement_id=? ORDER BY id", (pid,))
            audit_log = rows(
                "SELECT id, actor, action, detail_json, created_at FROM audit_log "
                "WHERE procurement_id=? ORDER BY id", (pid,))
            verification = self.verify_chain(pid)
            detail.update({
                "reviewers": reviewers,
                "conflict_declarations": [
                    {**c, "recused": bool(c["recused"])} for c in conflicts],
                "quote_versions": self.quote_versions(principal, pid),
                "signoffs": signoffs,
                "contract_changes": [
                    {**c, "delta_yuan": cents_to_yuan(c.pop("delta_cents"))} for c in changes],
                "acceptances": [
                    {**a, "final": bool(a["final"]),
                     "amount_yuan": cents_to_yuan(a.pop("amount_cents"))} for a in acceptances],
                "payments": [
                    {**p, "amount_yuan": cents_to_yuan(p.pop("amount_cents"))} for p in payments],
                "refunds": [
                    {**r, "amount_yuan": cents_to_yuan(r.pop("amount_cents"))} for r in refunds],
                "audit_log": audit_log,
                "verification": verification,
            })
            return detail

    def ledger_entries(self, principal: dict, pid: str) -> dict:
        self.require_role(principal, "审计人员", "学校采购员")
        with self._lock:
            self._get_proc(self.conn, pid)
            rows = self.conn.execute(
                "SELECT seq, event_type, event_ref, lines_json, amount_cents, responsible, "
                "created_at, prev_hash, entry_hash FROM ledger_entries "
                "WHERE procurement_id=? ORDER BY seq", (pid,),
            ).fetchall()
            return {
                "procurement_id": pid,
                "entries": [{
                    "seq": r["seq"],
                    "event_type": r["event_type"],
                    "event_ref": r["event_ref"],
                    "lines": json.loads(r["lines_json"]),
                    "amount_yuan": cents_to_yuan(r["amount_cents"]),
                    "responsible": r["responsible"],
                    "created_at": r["created_at"],
                    "prev_hash": r["prev_hash"],
                    "entry_hash": r["entry_hash"],
                } for r in rows],
                "verification": self.verify_chain(pid),
            }

    # ------------------------------------------------------------------ 校验

    def verify_chain(self, pid: str) -> dict:
        """重算哈希链并核对金额平衡规则。任何篡改都会暴露。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM ledger_entries WHERE procurement_id=? ORDER BY seq", (pid,)
            ).fetchall()
            problems: list[str] = []
            prev_hash = "GENESIS"
            debit_total = credit_total = 0
            for r in rows:
                body = _canonical({
                    "procurement_id": r["procurement_id"],
                    "event_type": r["event_type"],
                    "event_ref": r["event_ref"],
                    "lines": json.loads(r["lines_json"]),
                    "amount_cents": r["amount_cents"],
                    "responsible": r["responsible"],
                    "created_at": r["created_at"],
                    "prev_hash": r["prev_hash"],
                })
                expected = hashlib.sha256(body.encode("utf-8")).hexdigest()
                if r["prev_hash"] != prev_hash:
                    problems.append(f"分录 {r['seq']} 前序哈希断裂")
                if r["entry_hash"] != expected:
                    problems.append(f"分录 {r['seq']} 内容哈希不匹配（可能被篡改）")
                lines = json.loads(r["lines_json"])
                d = sum(l["debit_cents"] for l in lines)
                c = sum(l["credit_cents"] for l in lines)
                if d != c:
                    problems.append(f"分录 {r['seq']} 借贷不平衡")
                debit_total += d
                credit_total += c
                prev_hash = r["entry_hash"]

            proc = self._get_proc(self.conn, pid)
            t = self._totals(self.conn, pid)
            if t["committed"] > proc["budget_cents"]:
                problems.append("合同承诺超出预算")
            if t["accepted"] > t["committed"]:
                problems.append("累计验收超出合同承诺")
            if t["net_paid"] > t["accepted"]:
                problems.append("付款净额超出累计验收")
            if t["refunded"] > t["paid"]:
                problems.append("累计退款超出累计付款")
            if debit_total != credit_total:
                problems.append("全部分录借贷合计不相等")

            # 分录重放金额应与业务表汇总一致
            replay = {"BUDGET": 0, "COMMIT": 0, "COMMIT_CHANGE": 0,
                      "ACCEPT": 0, "PAYMENT": 0, "REFUND": 0}
            for r in rows:
                replay[r["event_type"]] += r["amount_cents"]
            if replay["BUDGET"] != proc["budget_cents"]:
                problems.append("预算分录与需求金额不一致")
            if replay["COMMIT"] + replay["COMMIT_CHANGE"] != t["committed"]:
                problems.append("承诺分录重放与合同金额不一致")
            if replay["ACCEPT"] != t["accepted"]:
                problems.append("验收分录重放与验收汇总不一致")
            if replay["PAYMENT"] != t["paid"] or replay["REFUND"] != t["refunded"]:
                problems.append("付款/退款分录重放与汇总不一致")

            return {
                "entry_count": len(rows),
                "balanced": debit_total == credit_total,
                "last_hash": prev_hash if rows else None,
                "problems": problems,
                "ok": not problems,
            }
