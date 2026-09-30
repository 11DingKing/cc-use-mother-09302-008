"""SQLite 持久化层：表结构与连接管理。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS procurements (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    schools_json TEXT NOT NULL,
    budget_cents INTEGER NOT NULL CHECK (budget_cents > 0),
    owner TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '需求',
    created_at TEXT NOT NULL,
    awarded_supplier_id TEXT,
    frozen_at TEXT
);

CREATE TABLE IF NOT EXISTS reviewers (
    procurement_id TEXT NOT NULL REFERENCES procurements(id),
    person_id TEXT NOT NULL,
    name TEXT NOT NULL,
    school TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (procurement_id, person_id)
);

CREATE TABLE IF NOT EXISTS suppliers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    contact TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- 供应关系申报 / 回避声明
CREATE TABLE IF NOT EXISTS conflict_declarations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procurement_id TEXT NOT NULL REFERENCES procurements(id),
    supplier_id TEXT NOT NULL REFERENCES suppliers(id),
    person_id TEXT NOT NULL,
    person_name TEXT NOT NULL,
    relation TEXT NOT NULL,
    recused INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conflict_latest
    ON conflict_declarations(procurement_id, supplier_id, person_id, id);

CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procurement_id TEXT NOT NULL REFERENCES procurements(id),
    supplier_id TEXT NOT NULL REFERENCES suppliers(id),
    version INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    currency TEXT NOT NULL DEFAULT 'CNY',
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '有效',
    created_at TEXT NOT NULL,
    UNIQUE (procurement_id, supplier_id, version)
);

-- 评审签署：同一采购的签署串行化（DB 级唯一锁）
CREATE TABLE IF NOT EXISTS review_signoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procurement_id TEXT NOT NULL REFERENCES procurements(id),
    sign_lock TEXT NOT NULL DEFAULT 'SINGLE',
    reviewer_id TEXT NOT NULL,
    reviewer_name TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('同意','反对','弃权')),
    comment TEXT NOT NULL DEFAULT '',
    signed_at TEXT NOT NULL,
    UNIQUE (procurement_id, sign_lock, reviewer_id)
);

CREATE TABLE IF NOT EXISTS contracts (
    procurement_id TEXT PRIMARY KEY REFERENCES procurements(id),
    supplier_id TEXT NOT NULL REFERENCES suppliers(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    signed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contract_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procurement_id TEXT NOT NULL REFERENCES procurements(id),
    delta_cents INTEGER NOT NULL,
    reason TEXT NOT NULL,
    operator TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS acceptances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procurement_id TEXT NOT NULL REFERENCES procurements(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    final INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    accepted_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procurement_id TEXT NOT NULL REFERENCES procurements(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    ref_acceptance_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refunds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procurement_id TEXT NOT NULL REFERENCES procurements(id),
    payment_id INTEGER NOT NULL REFERENCES payments(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- 不可变复式分录，哈希链
CREATE TABLE IF NOT EXISTS ledger_entries (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    procurement_id TEXT NOT NULL REFERENCES procurements(id),
    event_type TEXT NOT NULL,
    event_ref TEXT NOT NULL,
    lines_json TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    responsible TEXT NOT NULL,
    created_at TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS auth_tokens (
    token TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    person_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    procurement_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
