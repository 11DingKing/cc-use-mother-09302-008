"""领域契约校验工具。"""
from __future__ import annotations

import json
from pathlib import Path


REQUIRED = {"schema_version", "product", "source_context", "actors", "states",
            "invariants", "sample_cases", "tags"}

REQUIRED_INVARIANTS = {"关系回避", "报价冻结", "金额分录", "分级公开"}
LEDGER_SECTIONS = ("ledger_accounts", "events", "balance_rules",
                   "freeze_rules", "visibility")


def load_contract(path: str | Path) -> dict:
    """读取并校验领域契约。"""
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = REQUIRED - value.keys()
    if missing:
        raise ValueError("领域契约缺少字段：" + "、".join(sorted(missing)))
    if value["schema_version"] != 1:
        raise ValueError("不支持的契约版本")
    for key in ("actors", "states", "invariants", "sample_cases", "tags"):
        if not isinstance(value[key], list) or not value[key]:
            raise ValueError(f"{key} 必须是非空列表")
    case_ids = [item.get("case_id") for item in value["sample_cases"]]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("样例编号不能重复")

    absent = REQUIRED_INVARIANTS - set(value["invariants"])
    if absent:
        raise ValueError("关键不变量缺失：" + "、".join(sorted(absent)))

    # 台账服务章节（schema v1 扩展）
    for key in LEDGER_SECTIONS:
        if key not in value:
            raise ValueError(f"台账章节缺失：{key}")
        if not isinstance(value[key], list) or not value[key]:
            raise ValueError(f"{key} 必须是非空列表")

    codes = [a["code"] for a in value["ledger_accounts"]]
    if len(codes) != len(set(codes)):
        raise ValueError("科目编码不能重复")
    for account in value["ledger_accounts"]:
        if account.get("direction") not in ("debit", "credit"):
            raise ValueError(f"科目 {account.get('code')} 方向非法")
    for event in value["events"]:
        if not event.get("event") or not event.get("posting"):
            raise ValueError("事件必须声明 event 与 posting")
    viewers = {v["viewer"] for v in value["visibility"]}
    if not {"公众", "审计人员"} <= viewers:
        raise ValueError("分级公开必须覆盖公众与审计人员")
    return value


def summarize(value: dict) -> dict:
    """生成稳定的契约摘要。"""
    return {
        "product": value["product"],
        "actor_count": len(value["actors"]),
        "state_count": len(value["states"]),
        "invariant_count": len(value["invariants"]),
        "case_count": len(value["sample_cases"]),
        "account_count": len(value["ledger_accounts"]),
        "event_count": len(value["events"]),
    }
