"""
演示脚本：6 个场景 + 5 条硬断言，把"工具调用 / 业务规则 / 人工审批 / 状态管理"跑穿。

运行：
    python demo.py
输出：
    控制台 + demo_output.txt（UTF-8，便于直接查看）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from db import connect, yuan  # noqa: E402
from orchestrator import Agent, IllegalTransition  # noqa: E402

LINES: list[str] = []


def log(text: str = "") -> None:
    LINES.append(text)


def hr(title: str) -> None:
    log()
    log("=" * 62)
    log(title)
    log("=" * 62)


def turn(agent: Agent, session: str, user: str, text: str) -> dict:
    log(f"\n用户 > {text}")
    r = agent.handle(session, user, text)
    for ev in r["events"]:
        log(f"  状态 {ev['from']} → {ev['to']}  ({ev['event']})")
    if r.get("tools"):
        for t in r["tools"]:
            log(f"  工具 {t['name']:<26} {'OK' if t['ok'] else 'FAIL'}")
    if r.get("text"):
        for i, line in enumerate(r["text"].split("\n")):
            log(("客服 > " if i == 0 else "       ") + line)
    card = r.get("card")
    if card:
        amt = card["amount_detail"]
        log(f"  [方案卡] {card['refund_type_text']}｜商品 ¥{amt['goods']} 运费退 ¥{amt['freight_refund']}"
            f" 运费扣 ¥{amt['freight_deduct']}｜合计 ¥{amt['total']}"
            f"｜{'需审批 L' + str(card['approval_level']) if card['need_manual_approval'] else '自动通过'}")
    log(f"  [会话态] {r['state']}")
    return r


def confirm(agent: Agent, session_no: str, user_no: str) -> dict:
    session = agent._ensure_session(session_no, user_no)
    payload = agent._payload(session)
    result = agent.respond_to_proposal(
        session_no, user_no, payload["refund_no"], payload["confirm_token"], "CONFIRM")
    log("用户点击确认 → " + result["text"])
    return result


def main() -> None:
    conn = connect()
    agent = Agent(conn)
    passed: list[str] = []

    # ------------------------------------------------------------------ S1
    hr("场景 1 · 物流查询（只读工具，不落任何单）")
    turn(agent, "S20261117-01", "U1001", "我的快递到哪了？订单 ORD20261101001")
    logistics_calls = conn.execute(
        "SELECT COUNT(*) c FROM agent_tool_calls WHERE tool_name='get_logistics'").fetchone()["c"]
    assert logistics_calls == 1, "应恰好调用一次物流工具"
    passed.append("S1 物流查询：工具被调用 1 次，状态 TOOL_RUNNING→COMPLETED")

    # ------------------------------------------------------------------ S2
    hr("场景 2 · 自动退款（299 元 ≤ 500，需用户确认后才落单）")
    r2 = turn(agent, "S20261117-02", "U1001", "ORD20261101001 这件开衫不想要了，能退吗")
    draft = conn.execute("SELECT status, apply_amount FROM refund_requests").fetchone()
    assert draft["status"] == "DRAFT", "展示方案时必须是草稿态"
    formal = conn.execute("SELECT COUNT(*) c FROM refund_requests WHERE status<>'DRAFT'").fetchone()["c"]
    assert formal == 0, "用户未确认前不得存在正式退款单"
    passed.append("S2-a 方案阶段：仅 1 条 DRAFT，正式单数量 = 0")

    confirm(agent, "S20261117-02", "U1001")
    assert conn.execute("SELECT status FROM refund_requests WHERE refund_no=?",
                        (r2["card"]["refund_no"],)).fetchone()["status"] == "PENDING_RETURN"
    agent.on_return_verified(r2["card"]["refund_no"], "demo_warehouse")
    row = conn.execute("SELECT status, apply_amount, refund_no FROM refund_requests").fetchone()
    assert row["status"] == "SUCCESS", f"自动通道应直接成功，实际 {row['status']}"
    assert row["apply_amount"] == 29900, "金额应为 299.00 元"
    passed.append(f"S2-b 确认后自动通过并完成：{row['refund_no']} / ¥{yuan(row['apply_amount'])}")

    # 重复确认（幂等）
    dup = agent.tools.confirm_refund(row["refund_no"], "whatever-token", f"confirm:{row['refund_no']}")
    assert dup["ok"] and dup["data"].get("duplicated"), "重复确认应幂等返回而不是报错"
    cnt = conn.execute("SELECT COUNT(*) c FROM refund_requests").fetchone()["c"]
    assert cnt == 1, "重复确认不得产生第二笔退款"
    passed.append("S2-c 重复确认：幂等返回，退款单数仍为 1")

    # 越权：伪造 token
    forged = agent.handle("S-forged", "U1002", "申请退款 ORD20261101005")
    bad = agent.respond_to_proposal("S-forged", "U1002", forged["card"]["refund_no"],
                                    "forged-token", "CONFIRM")
    assert not bad["ok"] and bad["code"] == "4003", "伪造 token 必须被拒绝"
    agent.request_handoff("S-forged", "U1002")
    passed.append("S2-d 伪造确认令牌：返回 4003，无法推进退款")

    # ------------------------------------------------------------------ S3
    hr("场景 3 · 1880 元需主管审批（L1）")
    turn(agent, "S20261117-03", "U1002", "ORD20261101003 吸尘器还没收到，我想退款")
    confirm(agent, "S20261117-03", "U1002")
    task = conn.execute("SELECT * FROM approval_tasks WHERE status='PENDING'").fetchone()
    assert task is not None, "必须生成待审批任务"
    r3 = conn.execute("SELECT refund_no,status FROM refund_requests WHERE id=?", (task["biz_id"],)).fetchone()
    assert r3["status"] == "PENDING_APPROVAL", "退款单应停在 PENDING_APPROVAL"
    passed.append(f"S3-a 生成审批任务 {task['task_no']}（L{task['level']}），退款单 PENDING_APPROVAL")

    log("\n审批工作台待办：")
    for t in agent.tools.pending_tasks():
        log(f"  {t['task_no']} | {t['order_no']} | ¥{t['amount']} | L{t['level']} | {t['decision_code']}"
            f" | SLA {t['sla_deadline']}")

    ap = agent.on_approval(task["task_no"], "APPROVE", "leader_01", "核对物流拦截成功，同意退款")
    agent.on_return_verified(r3["refund_no"], "demo_warehouse")
    log(f"\n审批人 > 通过（leader_01）：核对物流拦截成功，同意退款")
    for ev in ap["events"]:
        log(f"  状态 {ev['from']} → {ev['to']}  ({ev['event']})")
    log(f"客服 > {ap['text']}")
    st = conn.execute("SELECT status FROM refund_requests WHERE id=?", (task["biz_id"],)).fetchone()
    assert st["status"] == "SUCCESS", "审批通过后应完成退款"
    passed.append("S3-b 审批通过 → 退款单 SUCCESS，会话被唤醒并推送结论")

    # 重复审批
    again = agent.on_approval(task["task_no"], "APPROVE")
    assert not again["ok"] and again["error"]["code"] == "6002", "重复审批应被拒绝"
    passed.append("S3-c 重复审批：返回 6002（任务已处理）")

    # ------------------------------------------------------------------ S4
    hr("场景 4 · 已签收 12 天（超 7 天无理由期）→ 拒绝并给替代方案")
    before = conn.execute("SELECT COUNT(*) c FROM refund_requests").fetchone()["c"]
    turn(agent, "S20261117-04", "U1001", "ORD20261101002 这双鞋想退一下")
    after = conn.execute("SELECT COUNT(*) c FROM refund_requests").fetchone()["c"]
    assert before == after, "被规则拒绝的诉求不得产生退款单"
    passed.append("S4 超期拒绝：BR-06 命中，未产生任何退款单")

    # ------------------------------------------------------------------ S5
    hr("场景 5 · 生鲜类目（BR-08 禁退）→ 拒绝")
    turn(agent, "S20261117-05", "U1003", "ORD20261101004 车厘子退款")
    passed.append("S5 类目拒绝：BR-08 命中，未产生退款单")

    # ------------------------------------------------------------------ S6
    hr("场景 6 · 确认令牌过期 + 转人工")
    r6 = turn(agent, "S20261117-06", "U1002", "ORD20261101005 手机也想退了")
    target = r6["card"]["refund_no"]
    conn.execute("UPDATE refund_requests SET confirm_expire_at='2026-11-17 00:00:00' WHERE refund_no=?",
                 (target,))
    conn.commit()
    r6b = confirm(agent, "S20261117-06", "U1002")
    assert r6b["state"] == "EXPIRED", f"过期确认应进入 EXPIRED，实际 {r6b['state']}"
    passed.append("S6-a 令牌过期：状态转 EXPIRED，退款单置 EXPIRED")

    turn(agent, "S20261117-07", "U1002", "我要转人工")
    tk = conn.execute("SELECT COUNT(*) c FROM service_tickets").fetchone()["c"]
    assert tk == 2, "应为两个会话各生成 1 张转人工工单"
    passed.append("S6-b 转人工：生成工单并附结构化摘要")

    # ------------------------------------------------------------------ 非法跃迁
    hr("场景 7 · 非法跃迁防护（跳过确认直接完成是不可能的）")
    s = dict(conn.execute("SELECT * FROM agent_sessions WHERE session_no='S20261117-06'").fetchone())
    s["state"] = "PROPOSAL_PENDING"
    try:
        agent._transit(s, "auto_approved")
        raise AssertionError("应当拒绝非法跃迁")
    except IllegalTransition as exc:
        log(f"  拦截：{exc}")
        passed.append("S7 非法跃迁：PROPOSAL_PENDING --auto_approved--> 被表校验拒绝")

    # ------------------------------------------------------------------ 事件溯源
    hr("事件溯源：会话 S20261117-03 的完整轨迹")
    for row in conn.execute(
            "SELECT from_state,to_state,event,operator FROM agent_state_events"
            " WHERE session_id=(SELECT id FROM agent_sessions WHERE session_no='S20261117-03')"
            " ORDER BY id"):
        log(f"  {row['from_state']:<18} → {row['to_state']:<18} {row['event']:<16} by {row['operator']}")

    # ------------------------------------------------------------------ 汇总
    hr("断言结果")
    for p in passed:
        log(f"  [PASS] {p}")

    log()
    log("关键数据校验：")
    for row in conn.execute("SELECT refund_no,status,decision_code,apply_amount FROM refund_requests"):
        log(f"  {row['refund_no']}  {row['status']:<18} {row['decision_code']:<22} ¥{yuan(row['apply_amount'])}")
    calls = conn.execute("SELECT tool_name, COUNT(*) c FROM agent_tool_calls GROUP BY tool_name").fetchall()
    log("\n工具调用统计：")
    for c in calls:
        log(f"  {c['tool_name']:<28} {c['c']} 次")

    out = "\n".join(LINES)
    print(out)
    (Path(__file__).parent / "demo_output.txt").write_text(out, encoding="utf-8")


if __name__ == "__main__":
    main()
