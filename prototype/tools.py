"""
工具层：Agent 可调用的原子业务能力。

每个工具都返回统一信封，且**永不向模型抛异常**：
    {"ok": bool, "data": {...}, "error": {"code","message","retryable"} | None, "meta": {...}}
模型根据 ok 与 error.retryable 自行决定重试、换路或转人工。

关键安全点：
    confirm_refund 不接受"自由调用"，必须携带 create_refund_draft 签发的 confirm_token，
    且 token 只存哈希、一次性消费、10 分钟过期。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta

import rules
from db import NOW, yuan

TS = "%Y-%m-%d %H:%M:%S"
TOKEN_TTL_MIN = 10


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ToolBox:
    def __init__(self, conn: sqlite3.Connection, trace_id: str = "tr_demo_0001",
                 clock: Callable[[], str] = lambda: NOW):
        self.conn = conn
        self.trace_id = trace_id
        self._decisions: dict[str, dict] = {}     # fingerprint -> 判定上下文（生产落 Redis，5min TTL）
        self.clock = clock
        self.user_id = None
        self.call_log: list[tuple] = []           # 供 demo / 测试断言
        self._session_id = 0

    # ------------------------------------------------------------------ 内部
    def _ok(self, data: dict, latency: int = 3) -> dict:
        return {"ok": True, "data": data, "error": None,
                "meta": {"trace_id": self.trace_id, "latency_ms": latency}}

    def _err(self, code: str, message: str, retryable: bool = False) -> dict:
        return {"ok": False, "data": None,
                "error": {"code": code, "message": message, "retryable": retryable},
                "meta": {"trace_id": self.trace_id, "latency_ms": 0}}

    def _log(self, tool: str, args: dict, result: dict, latency: int = 0) -> None:
        result.setdefault("_tool", tool)
        audit = result
        if result["ok"] and "confirm_token" in result["data"]:
            audit = {**result, "data": {key: value for key, value in result["data"].items()
                                        if key != "confirm_token"}}
        self.conn.execute(
            "INSERT INTO agent_tool_calls(session_id,call_id,tool_name,args,result,status,"
            "error_code,latency_ms,trace_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self._session_id, f"call_{len(self.call_log) + 1}", tool,
             json.dumps(args, ensure_ascii=False), json.dumps(audit, ensure_ascii=False),
             "SUCCESS" if result["ok"] else "ERROR",
             (result["error"] or {}).get("code", ""), latency, self.trace_id, self.clock()),
        )
        self.call_log.append((tool, result["ok"], latency))

    def _order(self, order_no: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
        return dict(row) if row and (self.user_id is None or row["user_id"] == self.user_id) else None

    def _items(self, order_id: int) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()
        return [dict(r) for r in rows]

    def _user(self, user_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row)

    def _categories(self) -> dict[str, dict]:
        rows = self.conn.execute("SELECT * FROM categories").fetchall()
        return {r["code"]: dict(r) for r in rows}

    def _policy(self, category_code: str | None = None) -> tuple[dict, int]:
        """类目级覆盖全局（对应生产 scope_type + priority + version）。"""
        global_row = self.conn.execute(
            "SELECT * FROM refund_policies WHERE scope_type='GLOBAL' AND enabled=1"
            " ORDER BY priority DESC, version DESC LIMIT 1").fetchone()
        base = json.loads(global_row["rule_json"])
        version = int(global_row["version"])
        if category_code:
            cat_row = self.conn.execute(
                "SELECT * FROM refund_policies WHERE scope_type='CATEGORY' AND scope_value=? AND enabled=1"
                " ORDER BY priority DESC LIMIT 1", (category_code,)).fetchone()
            if cat_row:
                base = rules.merge_rule(base, json.loads(cat_row["rule_json"]))
        return base, version

    def _history(self, order_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM refund_requests WHERE order_id=? AND confirmed_at IS NOT NULL",
            (order_id,)).fetchone()
        return int(row["c"])

    def _next_refund_no(self) -> str:
        row = self.conn.execute("SELECT COUNT(*) c FROM refund_requests").fetchone()
        return f"RF20261117{int(row['c']) + 1:04d}"

    # ------------------------------------------------------------------ T1
    def get_order(self, order_no: str) -> dict:
        t0 = time.time()
        order = self._order(order_no)
        if not order:
            res = self._err("3001", f"未找到订单 {order_no}")
            self._log("get_order", {"order_no": order_no}, res)
            return res
        items = self._items(order["id"])
        data = {
            "order_no": order["order_no"],
            "status": order["status"],
            "status_text": {"PAID": "已付款待发货", "SHIPPED": "已发货",
                            "DELIVERED": "已签收", "COMPLETED": "已完成"}.get(order["status"], order["status"]),
            "goods_amount": yuan(order["goods_amount"]),
            "freight_amount": yuan(order["freight_amount"]),
            "discount_amount": yuan(order["discount_amount"]),
            "pay_amount": yuan(order["pay_amount"]),
            "refunded_amount": yuan(order["refunded_amount"]),
            "receiver_region": order["receiver_region"],
            "paid_at": order["paid_at"],
            "shipped_at": order["shipped_at"],
            "delivered_at": order["delivered_at"],
            "items": [{"order_item_id": i["id"], "sku_code": i["sku_code"], "sku_name": i["sku_name"],
                       "category": i["category_code"], "qty": i["qty"],
                       "pay_amount": yuan(i["pay_amount"])} for i in items],
        }
        res = self._ok(data, int((time.time() - t0) * 1000) + 2)
        self._log("get_order", {"order_no": order_no}, res, res["meta"]["latency_ms"])
        return res

    # ------------------------------------------------------------------ T2
    def list_user_orders(self, user_no: str, limit: int = 3) -> dict:
        user = self.conn.execute("SELECT * FROM users WHERE user_no=?", (user_no,)).fetchone()
        if not user:
            res = self._err("3001", "用户不存在")
            self._log("list_user_orders", {"user_no": user_no}, res)
            return res
        rows = self.conn.execute(
            "SELECT order_no,status,pay_amount,paid_at FROM orders WHERE user_id=?"
            " ORDER BY paid_at DESC LIMIT ?", (user["id"], limit)).fetchall()
        res = self._ok({"orders": [{"order_no": r["order_no"], "status": r["status"],
                                    "pay_amount": yuan(r["pay_amount"]), "paid_at": r["paid_at"]}
                                   for r in rows]})
        self._log("list_user_orders", {"user_no": user_no, "limit": limit}, res)
        return res

    # ------------------------------------------------------------------ T3
    def get_logistics(self, order_no: str) -> dict:
        order = self._order(order_no)
        if not order:
            res = self._err("3001", f"未找到订单 {order_no}")
            self._log("get_logistics", {"order_no": order_no}, res)
            return res
        ship = self.conn.execute("SELECT * FROM shipments WHERE order_id=?", (order["id"],)).fetchone()
        if not ship:
            res = self._ok({"order_no": order_no, "shipping": False,
                            "message": "该订单尚未发货"})
            self._log("get_logistics", {"order_no": order_no}, res)
            return res
        traces = self.conn.execute(
            "SELECT trace_time,status_code,description,location FROM shipment_traces"
            " WHERE shipment_id=? ORDER BY trace_time DESC LIMIT 3", (ship["id"],)).fetchall()
        data = {
            "order_no": order_no,
            "shipping": True,
            "carrier": ship["carrier_name"],
            "tracking_no": ship["tracking_no"],
            "status": ship["status"],
            "status_text": {"PICKED": "已揽收", "IN_TRANSIT": "运输中",
                            "OUT_FOR_DELIVERY": "派送中", "SIGNED": "已签收",
                            "EXCEPTION": "异常"}.get(ship["status"], ship["status"]),
            "estimated_at": ship["estimated_at"],
            "signed_at": ship["signed_at"],
            "signed_by": ship["signed_by"],
            "traces": [{"time": t["trace_time"], "desc": t["description"],
                        "location": t["location"]} for t in traces],
        }
        res = self._ok(data)
        self._log("get_logistics", {"order_no": order_no}, res)
        return res

    # ------------------------------------------------------------------ T4
    def check_refund_eligibility(self, order_no: str, reason_code: str = "NO_REASON",
                                 session_id: int = 0) -> dict:
        self._session_id = session_id or self._session_id
        order = self._order(order_no)
        if not order:
            res = self._err("3001", f"未找到订单 {order_no}")
            self._log("check_refund_eligibility", {"order_no": order_no}, res)
            return res
        items = self._items(order["id"])
        cats = self._categories()
        rule, version = self._policy(items[0]["category_code"] if items else None)
        user = self._user(order["user_id"])
        decision = rules.evaluate(order=order, items=items, categories=cats, rule=rule,
                                  user=user, reason_code=reason_code, now=self.clock(),
                                  refund_history_count=self._history(order["id"]),
                                  policy_version=version)
        fp_src = json.dumps({"order": order_no, "items": [i["id"] for i in items],
                             "amount": decision["amount"], "v": version,
                             "reason": reason_code}, sort_keys=True, ensure_ascii=False)
        decision["fingerprint"] = "sha256:" + _sha(fp_src)[:32]
        self._decisions[decision["fingerprint"]] = {"order": order, "items": items,
                                                    "decision": decision,
                                                    "reason_code": reason_code,
                                                    "session_id": session_id}
        res = self._ok(decision)
        self._log("check_refund_eligibility",
                  {"order_no": order_no, "reason_code": reason_code}, res)
        return res

    # ------------------------------------------------------------------ T5
    def create_refund_draft(self, fingerprint: str, session_id: int = 0) -> dict:
        ctx = self._decisions.get(fingerprint)
        if not ctx or ctx["session_id"] != session_id or not ctx["decision"]["eligible"]:
            res = self._err("4003", "方案已失效，请重新判定")
            self._log("create_refund_draft", {"fingerprint": fingerprint}, res)
            return res
        order, decision = ctx["order"], ctx["decision"]

        refund_no = self._next_refund_no()
        token = secrets.token_urlsafe(16)
        amount = decision["amount"]
        cur = self.conn.execute(
            "INSERT INTO refund_requests(refund_no,order_id,user_id,session_id,refund_type,reason_code,"
            "goods_amount,freight_refund,freight_deduct,discount_deduct,apply_amount,status,decision_code,"
            "decision_snapshot,policy_version,approval_level,refund_seq,confirm_token_hash,confirm_expire_at,"
            "created_at,updated_at,trace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (refund_no, order["id"], order["user_id"], session_id, decision["refund_type"],
             ctx["reason_code"], amount["goods"], amount["freight_refund"], amount["freight_deduct"],
             amount["discount_deduct"], amount["total"], "DRAFT", decision["decision_code"],
             json.dumps(decision, ensure_ascii=False), decision["policy_version"],
             decision["approval_level"], self._history(order["id"]) + 1,
             _sha(token), self._expire(), self.clock(), self.clock(), self.trace_id))
        refund_id = cur.lastrowid
        for it in ctx["items"]:
            self.conn.execute(
                "INSERT INTO refund_items(refund_id,order_item_id,qty,amount) VALUES (?,?,?,?)",
                (refund_id, it["id"], it["qty"], it["pay_amount"]))
        self.record_refund_event(refund_no, "DRAFT", "AGENT")
        row = dict(self.conn.execute("SELECT * FROM refund_requests WHERE id=?", (refund_id,)).fetchone())
        res = self._ok(self._proposal(row, decision, token))
        self._log("create_refund_draft", {"fingerprint": fingerprint[:20] + "..."}, res)
        return res

    # ------------------------------------------------------------------ T6
    def confirm_refund(self, refund_no: str, confirm_token: str,
                       idempotency_key: str = "") -> dict:
        row = self.conn.execute("SELECT * FROM refund_requests WHERE refund_no=?", (refund_no,)).fetchone()
        if not row or (self.user_id is not None and row["user_id"] != self.user_id):
            return self._err("3002", "退款单不存在")
        row = dict(row)
        if row["status"] not in ("DRAFT", "ELIGIBLE"):
            # 幂等：已确认过，返回当前状态而不是报错
            return self._ok({"refund_no": refund_no, "status": row["status"],
                             "duplicated": True, "next_action": "WAIT_RESULT",
                             "message": "该退款申请已提交，请勿重复操作"})
        if not row["confirm_token_hash"] or not secrets.compare_digest(_sha(confirm_token), row["confirm_token_hash"]):
            res = self._err("4003", "确认凭证无效或已使用")
            self._log("confirm_refund", {"refund_no": refund_no}, res)
            return res
        now = self.clock()
        if now >= row["confirm_expire_at"]:
            self.conn.execute("UPDATE refund_requests SET status='EXPIRED', confirm_token_hash=NULL WHERE id=?",
                              (row["id"],))
            self.record_refund_event(refund_no, "EXPIRED", "SYSTEM")
            return self._err("4003", "确认已超时，请重新生成方案")

        order = self.conn.execute("SELECT * FROM orders WHERE id=?", (row["order_id"],)).fetchone()
        current = self.check_refund_eligibility(order["order_no"], row["reason_code"], row["session_id"])["data"]
        before = json.loads(row["decision_snapshot"])
        fields = ("decision_code", "refund_type", "amount", "approval_level", "policy_version")
        if any(current[key] != before[key] for key in fields):
            self.conn.execute("UPDATE refund_requests SET status='EXPIRED', confirm_token_hash=NULL WHERE id=?",
                              (row["id"],))
            self.record_refund_event(refund_no, "EXPIRED", "SYSTEM")
            return self._err("4003", "订单或规则已变化，请重新获取退款方案")

        need_approval = row["approval_level"] > 0
        new_status = "PENDING_APPROVAL" if need_approval else self.next_execution_state(row)
        self.conn.execute(
            "UPDATE refund_requests SET status=?, confirmed_at=?, confirm_token_hash=NULL,"
             " version=version+1, updated_at=?, refund_seq=? WHERE id=?",
            (new_status, now, now, self._history(row["order_id"]) + 1, row["id"]))
        self.conn.execute("UPDATE orders SET after_sale_status='REFUNDING' WHERE id=?", (row["order_id"],))
        self.record_refund_event(refund_no, "USER_CONFIRMED", "USER")
        self.record_refund_event(refund_no, new_status, "SYSTEM")

        sla = None
        if need_approval:
            sla = self._sla(row["approval_level"])
            self.conn.execute(
                "INSERT INTO approval_tasks(task_no,biz_type,biz_id,level,status,assignee_role,reason,"
                "sla_deadline,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"AP20261117{row['id']:04d}", "REFUND", row["id"], row["approval_level"], "PENDING",
                 "CS_LEADER" if row["approval_level"] == 1 else "FINANCE",
                 "；".join(before["reasons"]), sla, now))

        data = {"refund_no": refund_no, "status": new_status,
                "need_manual_approval": need_approval,
                "approval_level": row["approval_level"],
                "next_action": "WAIT_APPROVAL" if need_approval else "EXECUTING",
                "sla_deadline": sla,
                "amount": yuan(row["apply_amount"])}
        if new_status == "APPROVED":
            self._run_reimbursement(refund_no)
            data["status"] = "SUCCESS"
            data["next_action"] = "NONE"
            data["message"] = "退款已原路退回，预计 1-3 个工作日到账"
        elif need_approval:
            data["message"] = (f"已提交{'客服主管' if row['approval_level'] == 1 else '财务'}"
                               f"审批，预计 {4 if row['approval_level'] == 1 else 8} 小时内完成")
        else:
            data["next_action"] = "WAIT_RETURN"
        res = self._ok(data)
        self._log("confirm_refund", {"refund_no": refund_no}, res)
        return res

    @staticmethod
    def next_execution_state(refund: dict) -> str:
        return "PENDING_RETURN" if refund["refund_type"] == "RETURN_REFUND" else "APPROVED"

    def record_refund_event(self, refund_no: str, status: str, actor: str) -> None:
        self.conn.execute("INSERT INTO refund_events(refund_no,status,actor,created_at) VALUES (?,?,?,?)",
                          (refund_no, status, actor, self.clock()))

    # ------------------------------------------------------------------ T7
    def _run_reimbursement(self, refund_no: str) -> None:
        row = self.conn.execute("SELECT * FROM refund_requests WHERE refund_no=?", (refund_no,)).fetchone()
        order = self.conn.execute("SELECT * FROM orders WHERE id=?", (row["order_id"],)).fetchone()
        amount = row["apply_amount"]
        if (row["status"] != "APPROVED" or not row["confirmed_at"]
                or order["after_sale_status"] != "REFUNDING"
                or (row["refund_type"] == "CANCEL" and order["status"] != "PAID")
                or not 0 < amount <= order["pay_amount"] - order["refunded_amount"]):
            raise ValueError("退款状态或金额已变化，需要人工核查")
        now = self.clock()
        self.conn.execute("INSERT INTO outbox_events VALUES (?,?,'PENDING',?,NULL)", (refund_no, amount, now))
        self.conn.execute("UPDATE refund_requests SET status='PROCESSING', updated_at=? WHERE id=?",
                          (now, row["id"]))
        self.record_refund_event(refund_no, "PROCESSING", "MOCK_SETTLEMENT")
        # 模拟清结算同步返回成功（生产为异步回调 /webhooks/payment-refund）
        self.conn.execute(
            "UPDATE refund_requests SET status='SUCCESS', approved_amount=apply_amount,"
            " finished_at=?, updated_at=? WHERE id=?", (now, now, row["id"]))
        self.conn.execute("UPDATE orders SET after_sale_status='REFUNDED', refunded_amount=refunded_amount+?,"
                          " status=CASE WHEN ?='CANCEL' THEN 'CANCELLED' ELSE status END,"
                          " version=version+1 WHERE id=?", (row["apply_amount"], row["refund_type"], row["order_id"]))
        self.conn.execute("UPDATE order_items SET after_sale_status='REFUNDED' WHERE order_id=?",
                          (row["order_id"],))
        self.conn.execute("UPDATE outbox_events SET status='PROCESSED', processed_at=? WHERE refund_no=?",
                          (now, refund_no))
        self.record_refund_event(refund_no, "SUCCESS", "MOCK_SETTLEMENT")

    def verify_return(self, refund_no: str, actor: str) -> dict:
        row = self.conn.execute("SELECT * FROM refund_requests WHERE refund_no=?", (refund_no,)).fetchone()
        if not row or row["status"] != "PENDING_RETURN":
            return self._err("4004", "当前退款单无法核验退回")
        self.conn.execute("UPDATE refund_requests SET status='APPROVED', updated_at=? WHERE refund_no=?",
                          (self.clock(), refund_no))
        self.record_refund_event(refund_no, "RETURN_VERIFIED", actor)
        self._run_reimbursement(refund_no)
        return self._ok({"refund_no": refund_no, "status": "SUCCESS"})

    def execute_refund(self, refund_no: str) -> dict:
        row = self.conn.execute("SELECT * FROM refund_requests WHERE refund_no=?", (refund_no,)).fetchone()
        if not row:
            return self._err("3002", "退款单不存在")
        if row["status"] != "APPROVED":
            return self._err("4004", f"状态 {row['status']} 不允许执行退款")
        self._run_reimbursement(refund_no)
        res = self._ok({"refund_no": refund_no, "status": "SUCCESS"})
        self._log("execute_refund", {"refund_no": refund_no}, res)
        return res

    # ------------------------------------------------------------------ T8 / T9
    def get_refund_status(self, refund_no: str) -> dict:
        row = self.conn.execute("SELECT * FROM refund_requests WHERE refund_no=?", (refund_no,)).fetchone()
        if not row:
            return self._err("3002", "退款单不存在")
        node = {"DRAFT": "待你确认", "PENDING_APPROVAL": "人工审批中", "APPROVED": "已通过，准备退款",
                "PENDING_RETURN": "等待退货签收或物流拦截核验", "EXPIRED": "方案已失效",
                "CANCELLED": "已取消",
                "PROCESSING": "退款处理中", "SUCCESS": "退款成功",
                "REJECTED_BY_HUMAN": "审批未通过"}.get(row["status"], row["status"])
        res = self._ok({"refund_no": refund_no, "status": row["status"], "current_node": node,
                        "amount": yuan(row["apply_amount"]), "decision_code": row["decision_code"]})
        self._log("get_refund_status", {"refund_no": refund_no}, res)
        return res

    def cancel_refund(self, refund_no: str) -> dict:
        row = self.conn.execute("SELECT * FROM refund_requests WHERE refund_no=?", (refund_no,)).fetchone()
        if not row:
            return self._err("3002", "退款单不存在")
        if row["status"] not in ("DRAFT", "ELIGIBLE"):
            return self._err("4004", f"状态 {row['status']} 不可取消")
        self.conn.execute("UPDATE refund_requests SET status='CANCELLED', updated_at=? WHERE id=?",
                          (self.clock(), row["id"]))
        self.record_refund_event(refund_no, "CANCELLED", "USER")
        res = self._ok({"refund_no": refund_no, "status": "CANCELLED"})
        self._log("cancel_refund", {"refund_no": refund_no}, res)
        return res

    # ------------------------------------------------------------------ 审批
    def approve_task(self, task_no: str, decision: str, approver: str = "leader_01",
                     comment: str = "", approved_amount_cents: int | None = None) -> dict:
        task = self.conn.execute("SELECT * FROM approval_tasks WHERE task_no=?", (task_no,)).fetchone()
        if not task:
            return self._err("3002", "审批任务不存在")
        task = dict(task)
        if task["status"] != "PENDING":
            return self._err("6002", "该任务已被处理，请刷新待办")
        refund = dict(self.conn.execute("SELECT * FROM refund_requests WHERE id=?", (task["biz_id"],)).fetchone())
        if refund["status"] != "PENDING_APPROVAL":
            return self._err("4004", "退款单当前无法审批")
        if decision not in {"APPROVE", "REJECT"} or (decision == "REJECT" and not comment.strip()):
            return self._err("4000", "请选择审批结论，驳回时填写原因")
        if approved_amount_cents is not None and approved_amount_cents != refund["apply_amount"]:
            return self._err("4004", "变更金额需要生成新方案并重新确认")
        now = self.clock()
        if decision == "APPROVE":
            status = self.next_execution_state(refund)
            self.conn.execute("UPDATE approval_tasks SET status='APPROVED', decided_at=?, decided_by=?,"
                              " decision_comment=? WHERE id=?", (now, approver, comment, task["id"]))
            self.conn.execute("UPDATE refund_requests SET status=?, approved_amount=?,"
                              " updated_at=?, version=version+1 WHERE id=?",
                              (status, refund["apply_amount"], now, refund["id"]))
            self.record_refund_event(refund["refund_no"], "APPROVAL_PASSED", approver)
            self.record_refund_event(refund["refund_no"], status, "SYSTEM")
            if status == "APPROVED":
                self._run_reimbursement(refund["refund_no"])
                status = "SUCCESS"
            return self._ok({"task_no": task_no, "status": "APPROVED",
                             "refund_status": status, "refund_no": refund["refund_no"]})
        self.conn.execute("UPDATE approval_tasks SET status='REJECTED', decided_at=?, decided_by=?,"
                          " decision_comment=? WHERE id=?", (now, approver, comment, task["id"]))
        self.conn.execute("UPDATE refund_requests SET status='REJECTED_BY_HUMAN', updated_at=?,"
                          " version=version+1 WHERE id=?", (now, refund["id"]))
        self.conn.execute("UPDATE orders SET after_sale_status='NONE' WHERE id=?", (refund["order_id"],))
        self.record_refund_event(refund["refund_no"], "REJECTED_BY_HUMAN", approver)
        return self._ok({"task_no": task_no, "status": "REJECTED",
                         "refund_status": "REJECTED_BY_HUMAN", "refund_no": refund["refund_no"]})

    def pending_tasks(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT t.*, r.refund_no, r.apply_amount, r.decision_code, o.order_no"
            " FROM approval_tasks t JOIN refund_requests r ON r.id=t.biz_id"
            " JOIN orders o ON o.id=r.order_id WHERE t.status='PENDING' ORDER BY t.sla_deadline").fetchall()
        return [{"task_no": r["task_no"], "refund_no": r["refund_no"], "order_no": r["order_no"],
                 "amount": yuan(r["apply_amount"]), "level": r["level"],
                 "decision_code": r["decision_code"], "reason": r["reason"],
                 "sla_deadline": r["sla_deadline"]} for r in rows]

    # ------------------------------------------------------------------ T10 转人工
    def handoff_to_human(self, session_no: str, reason: str, summary: str) -> dict:
        row = self.conn.execute("SELECT COUNT(*) c FROM service_tickets").fetchone()
        ticket_no = f"TK20261117{int(row['c']) + 1:04d}"
        self.conn.execute(
            "INSERT INTO service_tickets(ticket_no,session_id,reason,summary,priority,status,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (ticket_no, self._session_id, reason, summary, 1, "OPEN", self.clock()))
        res = self._ok({"ticket_no": ticket_no, "queue_position": 2, "summary": summary})
        self._log("handoff_to_human", {"reason": reason}, res)
        return res

    # ------------------------------------------------------------------ 辅助
    def _proposal(self, row: dict, decision: dict, token: str) -> dict:
        rtype = decision["refund_type"]
        label = {"CANCEL": "取消订单（全额退款）", "REFUND_ONLY": "仅退款",
                 "RETURN_REFUND": "退货退款"}[rtype]
        method = {"CANCEL": "无需退回商品", "REFUND_ONLY": "无需退回商品",
                  "RETURN_REFUND": "上门取件（快递员 24 小时内联系）"}[rtype]
        actions = (["保持商品与吊牌完好", "按预约时间交寄"] if rtype == "RETURN_REFUND"
                   else ["无需退回商品"] if rtype == "REFUND_ONLY" else [])
        amount = decision["amount"]
        return {
            "refund_no": row["refund_no"],
            "refund_type": rtype,
            "refund_type_text": label,
            "status": row["status"],
            "amount_detail": {
                "goods": yuan(amount["goods"]),
                "freight_refund": yuan(amount["freight_refund"]),
                "freight_deduct": yuan(amount["freight_deduct"]),
                "total": yuan(amount["total"]),
            },
            "return_method": method,
            "eta": "审核通过后 1-3 个工作日原路退回",
            "need_user_action": actions,
            "reasons": decision["reasons"],
            "need_manual_approval": decision["need_manual_approval"],
            "approval_level": decision["approval_level"],
            "confirm_token": token,
            "expire_at": row["confirm_expire_at"],
        }

    def _expire(self) -> str:
        return (datetime.strptime(self.clock(), TS) + timedelta(minutes=TOKEN_TTL_MIN)).strftime(TS)

    def _sla(self, level: int) -> str:
        hours = 4 if level == 1 else 8
        return (datetime.strptime(self.clock(), TS) + timedelta(hours=hours)).strftime(TS)
