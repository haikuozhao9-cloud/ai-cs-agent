"""
会话编排：状态机 + 工具调用序列 + 确认闸门 + 审批回调。

这是整个系统的"大脑"，但它刻意做得很"笨"：
    * 不做自由发挥的判断 —— 可退与否只信规则引擎；
    * 不直接调用 confirm_refund —— 必须由用户的明确确认触发；
    * 每一次跃迁都校验表、落事件，非法跃迁直接拒绝。

真实系统里，intent/slot 抽取由 LLM 完成（Function Calling）；
原型用确定性关键词匹配代替，保证 demo 可复现、可断言。
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, replace

from db import NOW
from nlu import Context, ModelReader, ModelUnavailable, ORDER_RE, PHONE_RE, Reading
from tools import ToolBox

TS = "%Y-%m-%d %H:%M:%S"

# (当前态, 事件) -> 下一态。表里没有的组合就是非法跃迁。
TRANSITIONS: dict[tuple[str, str], str] = {
    ("INIT", "session_created"): "AUTH_VERIFY",
    ("AUTH_VERIFY", "auth_ok"): "INTENT_RESOLVED",
    ("AUTH_VERIFY", "auth_fail"): "ESCALATED",

    ("INTENT_RESOLVED", "need_slot"): "COLLECTING_SLOT",
    ("INTENT_RESOLVED", "to_tool"): "TOOL_RUNNING",
    ("INTENT_RESOLVED", "chat"): "COMPLETED",

    ("COLLECTING_SLOT", "slot_filled"): "TOOL_RUNNING",
    ("COLLECTING_SLOT", "give_up"): "ESCALATED",

    ("TOOL_RUNNING", "query_done"): "COMPLETED",
    ("TOOL_RUNNING", "eligible"): "PROPOSAL_PENDING",
    ("TOOL_RUNNING", "rejected"): "COMPLETED",
    ("TOOL_RUNNING", "need_evidence"): "COLLECTING_SLOT",
    ("TOOL_RUNNING", "tool_error"): "ESCALATED",

    ("PROPOSAL_PENDING", "user_confirm"): "EXECUTING",
    ("PROPOSAL_PENDING", "user_cancel"): "ABORTED",
    ("PROPOSAL_PENDING", "user_modify"): "TOOL_RUNNING",
    ("PROPOSAL_PENDING", "token_expired"): "EXPIRED",

    ("EXECUTING", "auto_approved"): "COMPLETED",
    ("EXECUTING", "wait_return"): "COMPLETED",
    ("EXECUTING", "need_approval"): "PENDING_APPROVAL",

    ("PENDING_APPROVAL", "approved"): "EXECUTING",
    ("PENDING_APPROVAL", "rejected"): "COMPLETED",
    ("PENDING_APPROVAL", "sla_timeout"): "PENDING_APPROVAL",

    ("COMPLETED", "new_turn"): "INTENT_RESOLVED",
    ("ABORTED", "new_turn"): "INTENT_RESOLVED",
    ("EXPIRED", "new_turn"): "INTENT_RESOLVED",

    # 任意活跃态都可被人工接管
    ("AUTH_VERIFY", "escalate"): "ESCALATED",
    ("INTENT_RESOLVED", "escalate"): "ESCALATED",
    ("COLLECTING_SLOT", "escalate"): "ESCALATED",
    ("TOOL_RUNNING", "escalate"): "ESCALATED",
    ("PROPOSAL_PENDING", "escalate"): "ESCALATED",
    ("PENDING_APPROVAL", "escalate"): "ESCALATED",
    ("COMPLETED", "escalate"): "ESCALATED",
    ("ABORTED", "escalate"): "ESCALATED",
    ("EXPIRED", "escalate"): "ESCALATED",
}

REFUND_WORDS = ("退款", "退钱", "退货", "退掉", "不想要", "不要了", "申请退", "退一下", "想退",
                "能退", "办退", "退回给我", "取消订单", "取消这单")
LOGISTICS_WORDS = ("物流", "快递", "到哪", "到哪了", "送到", "发货", "派送", "签收", "运费险")
ORDER_WORDS = ("订单", "买了", "下单", "查一下单", "我的货")
HUMAN_WORDS = ("转人工", "人工客服", "找人工", "找客服", "联系人工", "投诉", "消协", "真人")


class IllegalTransition(Exception):
    pass


class Agent:
    def __init__(self, conn: sqlite3.Connection, trace_id: str = "tr_demo_0001",
                 clock: Callable[[], str] = lambda: NOW, shadow: bool = False,
                 model: ModelReader | None = None):
        self.conn = conn
        self.tools = ToolBox(conn, trace_id, clock)
        self.trace_id = trace_id
        self.clock = clock
        self.shadow = shadow
        self.model = model
        self._reading: Reading | None = None
        self._events: list[dict] = []

    # ---------------------------------------------------------------- 会话
    def _ensure_session(self, session_no: str, user_no: str) -> dict:
        user = self.conn.execute("SELECT id FROM users WHERE user_no=?", (user_no,)).fetchone()
        if not user:
            raise PermissionError("找不到会话用户")
        self.tools.user_id = user["id"]
        row = self.conn.execute("SELECT * FROM agent_sessions WHERE session_no=?",
                                (session_no,)).fetchone()
        if row:
            if row["user_id"] != user["id"]:
                raise PermissionError("找不到会话")
            self.trace_id = row["trace_id"]
            self.tools.trace_id = self.trace_id
            return dict(row)
        self.conn.execute(
            "INSERT INTO agent_sessions(session_no,user_id,state,state_payload,created_at,updated_at,trace_id)"
            " VALUES (?,?,?,?,?,?,?)",
            (session_no, user["id"], "INIT", "{}", self.clock(), self.clock(), self.trace_id))
        s = dict(self.conn.execute("SELECT * FROM agent_sessions WHERE session_no=?",
                                   (session_no,)).fetchone())
        self.tools._session_id = s["id"]
        self._transit(s, "session_created")
        self._transit(s, "auth_ok")
        return s

    def _payload(self, s: dict) -> dict:
        return json.loads(s.get("state_payload") or "{}")

    def _save_payload(self, s: dict, payload: dict) -> None:
        s["state_payload"] = json.dumps(payload, ensure_ascii=False)
        self.conn.execute("UPDATE agent_sessions SET state_payload=? WHERE id=?",
                          (s["state_payload"], s["id"]))

    def _transit(self, s: dict, event: str, payload: dict | None = None,
                 operator: str = "AGENT") -> str:
        key = (s["state"], event)
        if key not in TRANSITIONS:
            raise IllegalTransition(f"非法跃迁: {s['state']} --{event}--> ?")
        nxt = TRANSITIONS[key]
        prev = s["state"]
        s["state"] = nxt
        self.conn.execute(
            "UPDATE agent_sessions SET state=?, updated_at=?, version=version+1"
            " WHERE id=?", (nxt, self.clock(), s["id"]))
        self.conn.execute(
            "INSERT INTO agent_state_events(session_id,from_state,to_state,event,payload,operator,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (s["id"], prev, nxt, event, json.dumps(payload or {}, ensure_ascii=False), operator, self.clock()))
        ev = {"from": prev, "to": nxt, "event": event}
        self._events.append(ev)
        return nxt

    def _say(self, s: dict, role: str, content: str, card: dict | None = None) -> None:
        row = self.conn.execute("SELECT COALESCE(MAX(seq),0) m FROM agent_messages WHERE session_id=?",
                                (s["id"],)).fetchone()
        self.conn.execute(
            "INSERT INTO agent_messages(session_id,seq,role,content,card_payload,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (s["id"], int(row["m"]) + 1, role, content,
             json.dumps(card, ensure_ascii=False) if card else None, self.clock()))

    def _set_ctx(self, s: dict, order_no: str) -> None:
        s["order_no_ctx"] = order_no
        self.conn.execute("UPDATE agent_sessions SET order_no_ctx=? WHERE id=?", (order_no, s["id"]))

    # ---------------------------------------------------------------- 主入口
    @staticmethod
    def nlu_context(session: dict | None, text: str) -> Context | None:
        if any(word in text for word in HUMAN_WORDS):
            return None
        if session:
            if session["human_takeover"] or session["state"] in {"ESCALATED", "PENDING_APPROVAL", "PROPOSAL_PENDING"}:
                return None
            if session["state"] == "COLLECTING_SLOT":
                payload = json.loads(session["state_payload"])
                if payload.get("decision", {}).get("requires_evidence"):
                    return None
                return Context(session["intent"], payload.get("reason_code"))
        return Context()

    def read_turn(self, text: str, context: Context) -> Reading:
        if self.model:
            try:
                return self.model.read(text, context)
            except ModelUnavailable as error:
                return replace(self.read_rules(text, context), source="FALLBACK",
                               latency_ms=error.latency_ms, failure=error.kind)
        return self.read_rules(text, context)

    def handle(self, session_no: str, user_no: str, text: str,
               prepared: tuple[Context | None, Reading | None] | None = None) -> dict:
        self._events = []
        self._reading = None
        s = self._ensure_session(session_no, user_no)
        self.tools._session_id = s["id"]
        text = PHONE_RE.sub("[手机号已隐藏]", text)
        self._say(s, "user", text)
        self.conn.execute("UPDATE agent_sessions SET turn_count=turn_count+1 WHERE id=?", (s["id"],))

        if s["human_takeover"] or s["state"] == "ESCALATED":
            return self._out(s, "已经为你转接人工客服了，正在接入，请稍等一下。", kind="HUMAN_WAIT")

        if any(word in text for word in HUMAN_WORDS):
            return self._request_handoff(s)

        if s["state"] == "PROPOSAL_PENDING":
            return self._out(s, "请使用方案卡上的「确认退款」或「取消方案」。需要修改时，可取消当前方案后重新申请。",
                             kind="REASK")

        if s["state"] == "PENDING_APPROVAL":
            return self._handle_waiting_turn(s, text)

        if s["state"] in ("COMPLETED", "ABORTED", "EXPIRED"):
            self._transit(s, "new_turn")

        context = self.nlu_context(s, text)
        if context is None:
            return self._escalate(s, "EVIDENCE", "质量问题超期，需由人工收集和核验证据")
        if prepared is not None:
            if prepared[0] != context or prepared[1] is None:
                raise ValueError("会话状态已变化，请重试当前消息")
            self._reading = prepared[1]
        else:
            self._reading = self.read_turn(text, context)
        reading = self._reading
        if reading.intent == "HANDOFF":
            return self._request_handoff(s)

        if s["state"] == "COLLECTING_SLOT":
            return self._handle_slot_turn(s, reading)

        intent = reading.intent
        s["intent"] = intent
        self.conn.execute("UPDATE agent_sessions SET intent=? WHERE id=?", (intent, s["id"]))
        if intent != "OTHER":
            self.conn.execute("UPDATE agent_sessions SET fallback_count=0 WHERE id=?", (s["id"],))

        order_no = reading.order_no if reading.order_count else s["order_no_ctx"]
        if reading.order_count > 1:
            self._set_ctx(s, "")
            self._save_payload(s, {"reason_code": reading.reason_code or "NO_REASON"})
            self._transit(s, "need_slot", {"missing": "single_order_no"})
            return self._out(s, "请先发一笔需要处理的订单号。", kind="ASK_SLOT")

        if intent == "REFUND":
            if any(phrase in text for phrase in ("部分退款", "部分退货", "只退一件", "只退其中", "只退运费", "退一半")):
                return self._escalate(s, "PARTIAL_REFUND", "需确认退款明细与数量")
            return self._flow_refund(s, order_no, reading.reason_code or "NO_REASON")
        if intent == "LOGISTICS":
            if not order_no:
                self._transit(s, "need_slot", {"missing": "order_no"})
                return self._out(s, "麻烦提供一下订单号（在「我的订单」里可以复制），我帮你查物流。",
                                 kind="ASK_SLOT")
            self._set_ctx(s, order_no)
            self._transit(s, "to_tool")
            return self._flow_logistics(s, order_no)
        if intent == "ORDER_QUERY":
            if not order_no:
                self._transit(s, "need_slot", {"missing": "order_no"})
                return self._out(s, "请从「我的订单」复制订单号发给我。",
                                 kind="ASK_SLOT")
            self._set_ctx(s, order_no)
            self._transit(s, "to_tool")
            return self._flow_order(s, order_no)

        if intent == "FAQ":
            self._transit(s, "chat")
            return self._out(s, "退款由系统按订单状态、商品类目和售后时效判定。到账时间因原支付方式而异，可在退款进度中查询。",
                             kind="FAQ")

        # 兜底
        s["fallback_count"] = int(s["fallback_count"]) + 1
        self.conn.execute("UPDATE agent_sessions SET fallback_count=? WHERE id=?",
                          (s["fallback_count"], s["id"]))
        if s["fallback_count"] >= 3:
            return self._escalate(s, "AGENT_FALLBACK", "连续 3 轮未识别用户诉求")
        self._transit(s, "chat")
        return self._out(s, "我可以帮你查订单、查物流，或者办理退款。你想问哪一单？告诉我订单号就行。",
                         kind="FALLBACK")

    # ---------------------------------------------------------------- 各流程
    def _flow_refund(self, s: dict, order_no: str | None, reason: str) -> dict:
        if not order_no:
            self._save_payload(s, {"reason_code": reason})
            self._transit(s, "need_slot", {"missing": "order_no"})
            return self._out(s, "请把订单号发我（形如 ORD20261101001），我帮你核对退款条件。",
                             kind="ASK_SLOT")

        self._set_ctx(s, order_no)
        if s["state"] != "TOOL_RUNNING":
            self._transit(s, "to_tool", {"order_no": order_no})

        r = self.tools.get_order(order_no)
        if not r["ok"]:
            self._transit(s, "query_done")
            return self._out(s, f"没查到这个订单，麻烦核对一下单号。{r['error']['message']}",
                             kind="ERROR", tools=r)

        d = self.tools.check_refund_eligibility(order_no, reason, s["id"])
        decision = d["data"]
        code = decision["decision_code"]
        payload = self._payload(s)
        payload["decision"] = decision
        payload["reason_code"] = reason
        self._save_payload(s, payload)

        if self.shadow:
            self._transit(s, "query_done", {"decision_code": code})
            return self._out(s, "当前由人工处理退款。请联系人工客服核对并提交申请。",
                             kind="SHADOW_REVIEW", decision=decision, tools=[r, d])

        if code == "NEED_EVIDENCE":
            self._transit(s, "need_evidence", {"decision_code": code})
            return self._out(
                s,
                "这笔订单已经超过 7 天无理由退货期了，如果商品有质量问题，"
                "请准备能看清问题的照片，并联系人工客服核实。",
                kind="NEED_EVIDENCE", decision=decision, tools=d)

        if code == "NEED_HUMAN_RISK":
            return self._escalate(s, "RISK", "风控命中：账户售后行为异常", decision=decision)

        if not decision["eligible"]:
            self._transit(s, "rejected", {"decision_code": code})
            suggestion = decision.get("suggestion") or "可以帮你转接人工进一步协商"
            text_out = (f"这单暂时不符合退款条件：{decision['reasons'][0]}。\n"
                        f"{suggestion}。需要我帮你转人工吗？")
            existing = None
            if code == "REJECT_DUPLICATE":
                existing = self.conn.execute(
                    "SELECT refund_no FROM refund_requests WHERE order_id=(SELECT id FROM orders WHERE order_no=?)"
                    " AND status IN ('PENDING_APPROVAL','PENDING_RETURN','SUCCESS') ORDER BY id DESC LIMIT 1",
                    (order_no,)).fetchone()
            card = {"type": "refund_status", "refund_no": existing["refund_no"]} if existing else None
            return self._out(s, text_out, kind="REJECT", decision=decision, tools=[r, d], card=card)

        # 生成方案草稿（不落正式单）
        dr = self.tools.create_refund_draft(decision["fingerprint"], s["id"])
        if not dr["ok"]:
            return self._escalate(s, "TOOL_FAIL", f"建方案失败：{dr['error']['message']}")

        proposal = dr["data"]
        payload["refund_no"] = proposal["refund_no"]
        payload["confirm_token"] = proposal["confirm_token"]
        self._save_payload(s, payload)

        # 严格校验：草稿必须存在且为 DRAFT
        row = self.conn.execute("SELECT status FROM refund_requests WHERE refund_no=?",
                                (proposal["refund_no"],)).fetchone()
        assert row and row["status"] == "DRAFT", "方案必须是草稿态"

        self._transit(s, "eligible", {"refund_no": proposal["refund_no"]})
        amt = proposal["amount_detail"]
        text_out = (
            f"核对完成，这单可以退。方案如下：\n"
            f"· 退款方式：{proposal['refund_type_text']}\n"
            f"· 退款金额：¥{amt['total']}（商品 ¥{amt['goods']}"
            + (f"，退运费 ¥{amt['freight_refund']}" if amt["freight_refund"] != "0.00" else "")
            + (f"，扣回运费 ¥{amt['freight_deduct']}" if amt["freight_deduct"] != "0.00" else "")
            + f"）\n"
            f"· 退回方式：{proposal['return_method']}\n"
            f"· 到账时效：{proposal['eta']}\n"
            + (f"· 该笔需{'客服主管' if proposal['approval_level'] == 1 else '财务'}审批，提交后会告诉你预计时间\n"
               if proposal["need_manual_approval"] else "")
            + "\n请核对金额与退回方式，方案有效期为 10 分钟。"
        )
        card = {"type": "refund_proposal", **{k: v for k, v in proposal.items()
                                             if k != "confirm_token"}}
        return self._out(s, text_out, kind="PROPOSAL", card=card, decision=decision,
                         tools=[r, d, dr])

    def _flow_logistics(self, s: dict, order_no: str) -> dict:
        r = self.tools.get_logistics(order_no)
        self._transit(s, "query_done")
        if not r["ok"]:
            return self._out(s, f"物流没查到：{r['error']['message']}", kind="ERROR", tools=r)
        d = r["data"]
        if not d["shipping"]:
            return self._out(s, f"订单 {order_no} 还没发货，付款后一般 48 小时内发出。", kind="LOGISTICS", tools=r)
        lines = [f"{d['carrier']} {d['tracking_no']}，当前状态：{d['status_text']}"]
        for t in d["traces"]:
            lines.append(f"· {t['time'][5:16]} {t['desc']}")
        if d["signed_at"]:
            lines.append(f"已于 {d['signed_at'][5:16]} 签收（{d['signed_by']}）。")
        else:
            lines.append(f"预计 {d['estimated_at']} 前送达。")
        return self._out(s, "\n".join(lines), kind="LOGISTICS", tools=r)

    def _flow_order(self, s: dict, order_no: str) -> dict:
        r = self.tools.get_order(order_no)
        self._transit(s, "query_done")
        if not r["ok"]:
            return self._out(s, f"没查到订单：{r['error']['message']}", kind="ERROR", tools=r)
        d = r["data"]
        items = "、".join(f"{i['sku_name']}×{i['qty']}" for i in d["items"])
        text = (f"订单 {d['order_no']}：{d['status_text']}\n"
                f"商品：{items}\n实付 ¥{d['pay_amount']}，"
                f"付款 {(d['paid_at'] or '未付款')[:16]}"
                + (f"，签收 {d['delivered_at'][:16]}" if d["delivered_at"] else "") + "。")
        return self._out(s, text, kind="ORDER", tools=r)

    def _handle_slot_turn(self, s: dict, reading: Reading) -> dict:
        order_no = reading.order_no
        if not order_no or reading.order_count > 1:
            return self._escalate(s, "AGENT_FALLBACK", "用户两次未提供有效订单号")
        self._transit(s, "slot_filled", {"order_no": order_no})
        self._set_ctx(s, order_no)
        intent = s.get("intent") or "REFUND"
        if intent == "LOGISTICS":
            return self._flow_logistics(s, order_no)
        if intent == "ORDER_QUERY":
            return self._flow_order(s, order_no)
        reason = reading.reason_code
        if not reason or reason == "NO_REASON":
            reason = self._payload(s).get("reason_code") or "NO_REASON"
        return self._flow_refund(s, order_no, reason)

    def respond_to_proposal(self, session_no: str, user_no: str, refund_no: str,
                            token: str, action: str) -> dict:
        self._events = []
        self._reading = None
        s = self._ensure_session(session_no, user_no)
        self.tools._session_id = s["id"]
        row = self.conn.execute("SELECT * FROM refund_requests WHERE refund_no=? AND session_id=?",
                                (refund_no, s["id"])).fetchone()
        if not row:
            return {"ok": False, "code": "3002", "message": "找不到当前会话的退款方案"}
        if row["confirmed_at"] and action == "CONFIRM":
            return {"kind": "DUPLICATE", "state": s["state"], "refund_no": refund_no, "status": row["status"]}
        if row["status"] == "EXPIRED" and action == "CONFIRM":
            return {"kind": "EXPIRED", "state": s["state"], "refund_no": refund_no, "status": row["status"]}
        payload = self._payload(s)
        if (s["state"] != "PROPOSAL_PENDING" or payload.get("refund_no") != refund_no
                or not secrets.compare_digest(token.encode(), payload.get("confirm_token", "").encode())):
            return {"ok": False, "code": "4003", "message": "当前方案或确认凭证无效"}
        if action == "CANCEL":
            r = self.tools.cancel_refund(refund_no)
            self._forget_proposal(s)
            self._transit(s, "user_cancel", {"refund_no": refund_no}, operator="USER")
            return self._out(s, "好的，这次先不退了。售后期内随时可以再找我。", kind="CANCEL", tools=r)

        if action != "CONFIRM" or self.shadow:
            return {"ok": False, "code": "4004", "message": "当前关闭系统退款执行权限"}
        r = self.tools.confirm_refund(refund_no, token, idempotency_key=f"confirm:{refund_no}")
        self._forget_proposal(s)
        if not r["ok"]:
            if r["error"]["code"] == "4003":
                self._transit(s, "token_expired", {"refund_no": refund_no}, operator="USER")
                return self._out(s, "方案已超过 10 分钟有效期，我重新给你生成一份，回复订单号即可。",
                                 kind="EXPIRED")
            raise ValueError(r["error"]["message"])

        d = r["data"]
        self._transit(s, "user_confirm", {"refund_no": refund_no}, operator="USER")
        status_card = {"type": "refund_status", "refund_no": refund_no, "status": d["status"]}
        if d.get("need_manual_approval"):
            self._transit(s, "need_approval", {"refund_no": refund_no, "level": d["approval_level"]})
            lvl = "客服主管" if d["approval_level"] == 1 else "财务"
            return self._out(
                s,
                f"已提交（单号 {refund_no}），这笔 ¥{d['amount']} 需要{lvl}审批，"
                f"处理时限为 {d['sla_deadline']}。可在退款进度查看结果。",
                kind="WAIT_APPROVAL", tools=r, card=status_card)
        if d["status"] == "PENDING_RETURN":
            self._transit(s, "wait_return", {"refund_no": refund_no})
            return self._out(s, "申请已提交。退货签收或物流拦截核验通过后，将按确认金额发起退款。",
                             kind="WAIT_RETURN", tools=r, card=status_card)
        self._transit(s, "auto_approved", {"refund_no": refund_no})
        return self._out(
            s,
            f"模拟退款处理成功，¥{d['amount']}。"
            f"退款单号 {refund_no}，可在「退款进度」查看。",
            kind="SUCCESS", tools=r, card=status_card)

    def _forget_proposal(self, s: dict) -> None:
        payload = self._payload(s)
        payload.pop("confirm_token", None)
        self._save_payload(s, payload)

    def request_handoff(self, session_no: str, user_no: str) -> dict:
        self._events = []
        self._reading = None
        s = self._ensure_session(session_no, user_no)
        self.tools._session_id = s["id"]
        return self._request_handoff(s)

    def _request_handoff(self, s: dict) -> dict:
        if s["state"] == "ESCALATED":
            return self._out(s, "人工客服工单已建立，请留意后续进度。", kind="HUMAN_WAIT")
        if s["state"] == "PROPOSAL_PENDING":
            self.tools.cancel_refund(self._payload(s)["refund_no"])
            self._forget_proposal(s)
        return self._escalate(s, "USER_REQUEST", "用户主动要求人工")

    def _handle_waiting_turn(self, s: dict, text: str) -> dict:
        payload = self._payload(s)
        r = self.tools.get_refund_status(payload.get("refund_no", ""))
        node = r["data"]["current_node"] if r["ok"] else "处理中"
        return self._out(s, f"你的退款单还在{node}，有结果我会马上通知你，不用重复提交。",
                         kind="WAITING", tools=r)

    # ---------------------------------------------------------------- 审批回调
    def on_approval(self, task_no: str, decision: str, approver: str = "leader_01",
                    comment: str = "") -> dict:
        """审批人处理后，唤醒会话并把结论翻译成用户话术。"""
        self._events = []
        res = self.tools.approve_task(task_no, decision, approver, comment)
        if not res["ok"]:
            return {"ok": False, "error": res["error"]}
        refund_no = res["data"]["refund_no"]
        refund = dict(self.conn.execute("SELECT * FROM refund_requests WHERE refund_no=?",
                                       (refund_no,)).fetchone())
        s = dict(self.conn.execute("SELECT * FROM agent_sessions WHERE id=?",
                                   (refund["session_id"],)).fetchone())
        self.tools._session_id = s["id"]
        status = res["data"]["refund_status"]
        if s["state"] == "PENDING_APPROVAL":
            self._transit(s, "approved" if decision == "APPROVE" else "rejected",
                          {"task_no": task_no, "by": approver}, operator="HUMAN")
            if decision == "APPROVE":
                event = "wait_return" if status == "PENDING_RETURN" else "auto_approved"
                self._transit(s, event, {"refund_no": refund_no}, operator="HUMAN")
        if decision == "APPROVE":
            text = ("退款申请已审批通过，等待退货签收或物流拦截核验。" if status == "PENDING_RETURN"
                    else f"退款已审批通过，模拟退款 ¥{refund['apply_amount'] / 100:.2f} 处理成功。")
        else:
            text = (f"很抱歉，你的退款申请未通过审批：{comment or '不符合售后规则'}。"
                    f"如有疑问可以转人工进一步沟通。")
        self._say(s, "assistant", text, {"type": "refund_status", "refund_no": refund_no, "status": status})
        return {"ok": True, **res["data"], "text": text, "session_no": s["session_no"], "events": self._events}

    def on_return_verified(self, refund_no: str, actor: str) -> dict:
        res = self.tools.verify_return(refund_no, actor)
        if res["ok"]:
            s = self.conn.execute(
                "SELECT s.* FROM agent_sessions s JOIN refund_requests r ON r.session_id=s.id WHERE r.refund_no=?",
                (refund_no,)).fetchone()
            self._say(s, "assistant", "退货签收或物流拦截已核验，模拟退款处理成功。",
                      {"type": "refund_status", **res["data"]})
        return res

    # ---------------------------------------------------------------- 辅助
    def _out(self, s: dict, text: str, *, kind: str = "TEXT", card: dict | None = None,
             decision: dict | None = None, tools=None, handled_by: str = "AGENT") -> dict:
        if text:
            self._say(s, "assistant", text, card)
        out = {"kind": kind, "text": text, "card": card, "state": s["state"], "intent": s.get("intent"),
               "events": self._events, "decision": decision, "handled_by": handled_by}
        if self._reading is not None:
            out["nlu"] = asdict(self._reading)
        if tools is not None:
            items = tools if isinstance(tools, list) else [tools]
            out["tools"] = [{"name": self._last_tool_name(t), "ok": t["ok"]} for t in items]
        return out

    @staticmethod
    def _last_tool_name(res: dict) -> str:
        return res.get("_tool") or "tool"

    def _escalate(self, s: dict, reason: str, detail: str, decision: dict | None = None) -> dict:
        payload = self._payload(s)
        summary = (
            f"用户诉求：{s.get('intent') or '未识别'}\n"
            f"订单：{s.get('order_no_ctx') or '未提供'}\n"
            f"已判：{payload.get('decision', {}).get('decision_code', '未判定')}\n"
            f"触发原因：{detail}\n"
            f"建议：核对订单与物流后人工处理"
        )
        if s["state"] != "ESCALATED":
            self._transit(s, "escalate", {"reason": reason})
        r = self.tools.handoff_to_human(s["session_no"], reason, summary)
        self.conn.execute("UPDATE agent_sessions SET human_takeover=1 WHERE id=?", (s["id"],))
        text = ("已经帮你转接人工客服，工单号 "
                f"{r['data']['ticket_no']}，前面还有 {r['data']['queue_position']} 位。"
                "我已经把情况同步给客服，不用重复描述。")
        return self._out(s, text, kind="HANDOFF", decision=decision, tools=r)

    @staticmethod
    def _detect_intent(text: str) -> str:
        if any(word in text for word in ("先别退", "暂不退款", "不要退款", "不想退款", "无需退款")):
            if any(word in text for word in LOGISTICS_WORDS):
                return "LOGISTICS"
            return "OTHER"
        if any(word in text for word in ("多久到账", "怎么退款", "怎么退", "退款规则", "退款政策", "无理由政策")):
            return "FAQ"
        if any(w in text for w in REFUND_WORDS):
            return "REFUND"
        if any(w in text for w in LOGISTICS_WORDS):
            return "LOGISTICS"
        if any(w in text for w in ORDER_WORDS):
            return "ORDER_QUERY"
        return "OTHER"

    @staticmethod
    def _detect_reason(text: str) -> str:
        if any(w in text for w in ("质量", "坏了", "破损", "瑕疵", "线头", "漏", "裂")):
            return "QUALITY"
        if any(w in text for w in ("发错", "错货", "不是这个")):
            return "WRONG_ITEM"
        if any(w in text for w in ("没收到", "未收到")):
            return "NOT_RECEIVED"
        return "NO_REASON"

    @classmethod
    def read_rules(cls, text: str, context: Context) -> Reading:
        intent = context.pending_intent or cls._detect_intent(text)
        orders = set(ORDER_RE.findall(text.upper()))
        reason = cls._detect_reason(text) if intent == "REFUND" else None
        if reason == "NO_REASON" and context.pending_reason:
            reason = context.pending_reason
        return Reading(intent, next(iter(orders)) if len(orders) == 1 else None, reason, len(orders))
