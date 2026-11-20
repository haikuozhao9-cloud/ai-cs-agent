"""
业务规则引擎：15 条规则（BR-01 ~ BR-15）的纯函数实现。

设计原则：
  * 纯函数：不碰数据库、不发网络请求，同输入必同输出 → 可单测、可回放。
  * 短路：命中拒绝类规则立刻返回，不再算金额（不给用户"差一点就能退"的错觉）。
  * 结构化：输出固定 schema，Agent 只负责把结论翻译成人话。
"""
from __future__ import annotations

from datetime import datetime, timedelta

TS = "%Y-%m-%d %H:%M:%S"

QUALITY_REASONS = {"QUALITY", "WRONG_ITEM", "DAMAGED", "NOT_RECEIVED"}
REASON_TEXT = {
    "NO_REASON": "不想要了",
    "QUALITY": "商品质量问题",
    "WRONG_ITEM": "发错货",
    "DAMAGED": "运输破损",
    "NOT_RECEIVED": "未收到货",
}


def _dt(value: str | None) -> datetime | None:
    return datetime.strptime(value, TS) if value else None


def _empty_amount() -> dict:
    return {"goods": 0, "freight_refund": 0, "freight_deduct": 0, "discount_deduct": 0, "total": 0}


def _result(*, eligible: bool, code: str, reasons: list[str], hits: list[str],
            refund_type: str = "RETURN_REFUND", amount: dict | None = None,
            level: int = 0, evidence: bool = False, policy_version: int = 3,
            extra: dict | None = None) -> dict:
    out = {
        "eligible": eligible,
        "decision_code": code,
        "refund_type": refund_type,
        "amount": amount or _empty_amount(),
        "need_manual_approval": level > 0,
        "approval_level": level,
        "requires_evidence": evidence,
        "reasons": reasons,
        "hit_rules": hits,
        "policy_version": policy_version,
    }
    if extra:
        out.update(extra)
    return out


def merge_rule(global_rule: dict, category_rule: dict | None) -> dict:
    """类目级规则覆盖全局规则（对应生产里的 scope_type=CATEGORY + priority）。"""
    if not category_rule:
        return dict(global_rule)
    merged = dict(global_rule)
    merged.update(category_rule)
    return merged


def evaluate(*, order: dict, items: list[dict], categories: dict[str, dict],
             rule: dict, user: dict, reason_code: str = "NO_REASON",
             refund_type: str = "AUTO", now: str = "2026-11-17 18:30:00",
             refund_history_count: int = 0, policy_version: int = 3) -> dict:
    """返回结构化决策。order/items/user 均为 dict，金额单位为分。"""
    now_dt = _dt(now)
    reasons: list[str] = []
    hits: list[str] = []

    no_reason_days = int(rule.get("no_reason_days", 7))
    excluded = set(rule.get("excluded_categories", []))
    forbid_status = set(rule.get("forbid_status", []))

    # ---------- BR-01 / BR-02 订单状态 ----------
    if order["status"] in forbid_status or order["status"] not in {"PAID", "SHIPPED", "DELIVERED", "COMPLETED"}:
        hits.append("BR-01/02")
        return _result(eligible=False, code="REJECT_STATUS", hits=hits,
                       reasons=[f"订单状态为 {order['status']}，不支持退款"],
                       policy_version=policy_version)

    # ---------- BR-10 重复申请（由调用方传入已有退款状态，这里兜底判断） ----------
    if order.get("after_sale_status") in ("REFUNDING", "RETURNING", "REFUNDED"):
        hits.append("BR-10")
        return _result(eligible=False, code="REJECT_DUPLICATE", hits=hits,
                       reasons=["该订单已有进行中或已完成的退款，请在“退款进度”中查看"],
                       policy_version=policy_version)

    # ---------- BR-08 禁退类目 ----------
    bad = []
    for it in items:
        cat = categories.get(it["category_code"], {})
        if (it["category_code"] in excluded
                or it["is_virtual"] or it["is_customized"] or it["is_fresh"]):
            bad.append(f"{it['sku_name']}（{cat.get('name', it['category_code'])}）")
    if bad:
        hits.append("BR-08")
        return _result(eligible=False, code="REJECT_CATEGORY", hits=hits,
                       reasons=[f"{'、'.join(bad)} 属于不支持无理由退货的类目"],
                       policy_version=policy_version)

    # ---------- 时效基准 ----------
    is_quality = reason_code in QUALITY_REASONS
    if order["status"] == "PAID":
        base_time, phase = _dt(order["paid_at"]), "未发货"
    elif order["status"] == "SHIPPED":
        base_time, phase = _dt(order["shipped_at"]), "已发货未签收"
    else:
        base_time, phase = _dt(order["delivered_at"]), "已签收"

    if base_time is None:
        hits.append("BR-01")
        return _result(eligible=False, code="REJECT_STATUS", hits=hits,
                       reasons=["订单缺少关键时间节点，无法判定售后期，建议转人工核实"],
                       policy_version=policy_version)

    elapsed_days = max((now_dt - base_time).days, 0)
    deadline = base_time + timedelta(days=no_reason_days)

    # ---------- BR-05 / BR-06 / BR-07 时效 ----------
    if phase == "已签收":
        hits.append("BR-05")
        if not is_quality and now_dt > deadline:
            hits.append("BR-06")
            return _result(eligible=False, code="REJECT_EXPIRED", hits=hits,
                           reasons=[f"已签收 {elapsed_days} 天，超过 {no_reason_days} 天无理由退货期"],
                           policy_version=policy_version,
                           extra={"elapsed_days": elapsed_days,
                                  "deadline": deadline.strftime(TS),
                                  "suggestion": "若为质量问题可提供照片，由人工核实后处理"})
        if is_quality and now_dt > deadline:
            hits.append("BR-07")
            return _result(eligible=False, code="NEED_EVIDENCE", hits=hits, evidence=True,
                           reasons=[f"已签收 {elapsed_days} 天，超出无理由期，需提供质量问题凭证"],
                           policy_version=policy_version,
                           extra={"elapsed_days": elapsed_days, "deadline": deadline.strftime(TS)})

    # ---------- 退款类型 ----------
    final_type = refund_type if refund_type != "AUTO" else "RETURN_REFUND"
    if refund_type == "AUTO":
        if phase == "未发货":
            final_type = "CANCEL"
        else:
            final_type = "RETURN_REFUND"

    # ---------- BR-14 金额：按明细行实付累计 ----------
    goods = sum(it["pay_amount"] - it.get("refunded_amount", 0) for it in items)
    hits.append("BR-14")

    # ---------- BR-13 运费 ----------
    freight_refund = 0
    freight_deduct = 0
    if final_type == "CANCEL" or is_quality:
        freight_refund = order["freight_amount"]          # 质量问题平台承担
        if is_quality and final_type != "CANCEL":
            reasons.append("质量问题，运费由平台承担")
    total = goods + freight_refund - freight_deduct

    # ---------- BR-15 上限 ----------
    remaining = order["pay_amount"] - order["refunded_amount"]
    if total > remaining:
        total = remaining
        reasons.append("退款金额已按订单剩余可退金额截断")
        hits.append("BR-15")

    amount = {
        "goods": goods,
        "freight_refund": freight_refund,
        "freight_deduct": freight_deduct,
        "discount_deduct": 0,
        "total": max(total, 0),
    }
    if amount["total"] == 0:
        return _result(eligible=False, code="REJECT_DUPLICATE", hits=hits + ["BR-15"],
                       reasons=["订单没有剩余可退金额"], policy_version=policy_version)

    # ---------- BR-09 / BR-11 / BR-12 审批分档 ----------
    auto_limit = int(rule.get("auto_approve_limit", 50000))
    l1_limit = int(rule.get("l1_limit", 200000))
    max_times = int(rule.get("max_refund_times", 2))
    level = 0
    if amount["total"] > l1_limit:
        level = 2
        hits.append("BR-12")
        reasons.append(f"退款金额 {amount['total'] / 100:.2f} 元超过 2000 元，需财务审批")
    elif amount["total"] > auto_limit:
        level = 1
        hits.append("BR-12")
        reasons.append(f"退款金额 {amount['total'] / 100:.2f} 元超过 500 元，需客服主管审批")

    if refund_history_count + 1 >= max_times:
        level = max(level, 1)
        hits.append("BR-09")
        reasons.append(f"该订单已退款 {refund_history_count} 次，需人工复核")

    if (user.get("risk_level", 0) >= 2
            or user.get("refund_rate_30d", 0) > float(rule.get("refund_rate_threshold", 0.6))
            or (reason_code == "NOT_RECEIVED" and phase == "已签收")):
        hits.append("BR-11")
        return _result(eligible=True, code="NEED_HUMAN_RISK", hits=hits, amount=amount,
                       level=2, refund_type=final_type, policy_version=policy_version,
                       reasons=reasons + ["账户售后行为触发风控，需人工核实"])

    if level == 2:
        code = "OK_NEED_APPROVAL_L2"
    elif level == 1:
        code = "OK_NEED_APPROVAL_L1"
    else:
        code = "OK_AUTO"

    if phase == "未发货":
        reasons.insert(0, "订单未发货，可直接取消并全额退款")
    elif phase == "已发货未签收":
        reasons.insert(0, "订单已发货未签收，需物流拦截或拒收后退款")
    else:
        reasons.insert(0, f"已签收 {elapsed_days} 天，在 {no_reason_days} 天售后期内")

    return _result(eligible=True, code=code, hits=sorted(set(hits)), amount=amount,
                   level=level, refund_type=final_type, policy_version=policy_version,
                   reasons=reasons,
                   extra={"elapsed_days": elapsed_days if phase == "已签收" else 0,
                          "deadline": deadline.strftime(TS) if phase == "已签收" else "",
                          "phase": phase})
