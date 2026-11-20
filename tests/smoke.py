"""通过实际 HTTP 验证本地全栈退款闭环和执行边界。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "prototype"))

from db import NOW, open_db
from nlu import ModelReader
from server import ACCOUNTS, DemoApp, Handler, ThreadingHTTPServer
from orchestrator import TS


class QuietHandler(Handler):
    def log_message(self, format, *args):
        pass


class ClassifierFixture(QuietHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        value = self.server.replies.pop(0)
        response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}
        self.send_content(200, json.dumps(response).encode(), "application/json")


class RefundSmoke(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "test.sqlite3"
        self.clock = datetime.strptime(NOW, TS)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.server.app = DemoApp(self.db, lambda: self.clock.strftime(TS))
        worker = Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(worker.join, 5)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.tokens = {}
        for account, (_, password, _) in ACCOUNTS.items():
            self.tokens[account] = self.call("/login", body={"account": account, "password": password})["token"]

    def request(self, path, account=None, body=None):
        headers = {}
        if account:
            headers["Authorization"] = f"Bearer {self.tokens[account]}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.base + path, json.dumps(body).encode() if body is not None else None, headers)
        try:
            return urlopen(request, timeout=20)
        except HTTPError as error:
            return error

    def call(self, path, account=None, body=None, expect=200):
        with self.request("/api/v1" + path, account, body) as response:
            content = json.load(response)
            self.assertEqual(response.status, expect, (path, content))
            self.assertEqual(content["ok"], expect < 400, content)
            return content["data"] if expect < 400 else content["error"]

    def authorize(self, mode="DELEGATED", expect=200):
        return self.call("/quality/authority", "quality", {"mode": mode}, expect)

    def chat(self, text, account="U1001", session=None):
        body = {"text": text}
        if session is not None:
            body["session_no"] = session
        return self.call("/agent/chat", account, body)

    def proposal(self, order, account="U1001"):
        answer = self.chat(f"我要对订单 {order} 申请退款", account)
        self.assertEqual(answer["result"]["kind"], "PROPOSAL")
        session = answer["session"]
        return {"session_no": session["session_no"], **session["active_proposal"], "action": "CONFIRM"}

    def test_static_shell_and_shadow_gate(self):
        for filename, token in [("/", "售后协作台"), ("/app.js", "renderQuality"), ("/styles.css", ".sidebar")]:
            with self.request(filename) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(token, response.read().decode())
        self.call("/orders", expect=401)
        self.call("/quality/report", "U1001", expect=403)
        report = self.call("/quality/report", "quality")
        self.assertTrue(report["gate"]["passed"])
        self.assertEqual(report["metrics"]["failed"], 0)
        shadow = self.chat("请取消订单 ORD20261101006")
        self.assertEqual(shadow["result"]["kind"], "SHADOW_REVIEW")
        self.assertIsNone(shadow["session"]["active_proposal"])
        self.assertEqual(self.call("/refunds", "U1001")["items"], [])
        items = self.call("/quality/shadow", "quality")["items"]
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]["candidate"])
        self.assertEqual(items[0]["order_snapshot"]["pay_amount"], 16900)
        self.authorize()
        baseline = self.call(f"/quality/shadow/{items[0]['id']}/baseline", "quality",
                             {"action": "HANDOFF", "note": "复核时发现订单需人工"})
        self.assertEqual(baseline["outcome"], "DANGEROUS")
        self.assertEqual(self.call("/config", "U1001")["mode"], "SHADOW")
        self.authorize(expect=409)
        with closing(open_db(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM refund_requests").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0], 0)

    def test_customer_scope_expiry_and_pause(self):
        self.authorize()
        self.assertEqual(self.chat("查物流 ORD20261101005")["result"]["kind"], "ERROR")
        proposal = self.proposal("ORD20261101001")
        session = proposal["session_no"]
        self.call(f"/agent/sessions/{session}", "U1002", expect=404)
        self.call("/agent/confirm", "U1002", proposal, expect=404)
        self.call("/agent/confirm", "U1001", {**proposal, "confirm_token": "fake"}, expect=409)
        result = self.chat("确认但先别退款，手机号 138 0013 8000", session=session)
        self.assertEqual(result["result"]["kind"], "REASK")
        self.assertIn("[手机号已隐藏]", result["session"]["messages"][-2]["content"])
        self.authorize("SHADOW")
        self.call("/agent/confirm", "U1001", proposal, expect=409)
        self.authorize()
        self.clock += timedelta(minutes=10)
        stale = self.call("/agent/confirm", "U1001", proposal)
        self.assertEqual(stale["result"]["kind"], "EXPIRED")
        self.assertEqual(self.call("/refunds", "U1001")["items"], [])
        trace = self.call(f"/traces/{result['session']['trace_id']}", "quality")
        self.assertNotIn(proposal["confirm_token"], json.dumps(trace))
        with closing(open_db(self.db)) as conn:
            calls = conn.execute("SELECT group_concat(result) FROM agent_tool_calls").fetchone()[0]
            self.assertNotIn(proposal["confirm_token"], calls)

    def test_shadow_baseline_correction_preserves_history_and_gate(self):
        self.chat("申请退款 ORD20261101001")
        item = self.call("/quality/shadow", "quality")["items"][0]
        path = f"/quality/shadow/{item['id']}"
        wrong = {"action": "PROPOSAL", "amount_cents": 299, "approval_level": 0,
                 "refund_type": "CANCEL", "note": "演示误填"}
        self.assertEqual(self.call(path + "/baseline", "quality", wrong)["outcome"], "DANGEROUS")
        authority = self.call("/quality/authority", "quality")
        self.assertEqual(authority["blockers"], [{"id": item["id"], "order_no": item["order_no"]}])
        self.authorize(expect=409)
        self.call(path + "/baseline", "quality", wrong, expect=409)
        corrected = {**wrong, "amount_cents": 29900, "refund_type": "RETURN_REFUND",
                     "note": "采样时已签收，实付299元，应退货核验",
                     "expected_revision": 1}
        self.call(path + "/review", "quality", corrected, expect=400)
        corrected["change_reason"] = "将299分更正为299元，按已签收快照更正退款类型"
        self.call(path + "/review", "leader", corrected, expect=403)
        self.assertEqual(self.call(path + "/review", "quality", corrected)["outcome"], "MATCH")
        self.call(path + "/review", "quality", corrected, expect=409)
        item = self.call("/quality/shadow", "quality")["items"][0]
        self.assertEqual(item["baseline_revision"], 2)
        self.assertEqual([row["outcome"] for row in item["history"]], ["DANGEROUS", "MATCH"])
        self.assertEqual(item["history"][0]["baseline"]["amount_cents"], 299)
        self.assertEqual(item["history"][1]["change_reason"], corrected["change_reason"])
        self.assertIsNone(item["history"][0]["change_reason"])
        self.authorize()
        still_wrong = {**corrected, "expected_revision": 2, "action": "REJECT"}
        self.assertEqual(self.call(path + "/review", "quality", still_wrong)["outcome"], "DANGEROUS")
        self.assertEqual(self.call("/config", "U1001")["mode"], "SHADOW")
        self.authorize(expect=409)
        # 已有数据升级时保留首次人工判断，重复启动不重复插入历史。
        with closing(open_db(self.db)) as conn, conn:
            conn.execute("DELETE FROM shadow_review_history WHERE observation_id=? AND revision=3", (item["id"],))
        DemoApp(self.db)
        DemoApp(self.db)
        item = self.call("/quality/shadow", "quality")["items"][0]
        self.assertEqual([row["revision"] for row in item["history"]], [1, 2, 3])

    def test_concurrent_confirm_pays_once_across_drafts(self):
        self.authorize()
        first = self.proposal("ORD20261101006")
        second = self.proposal("ORD20261101006")
        with ThreadPoolExecutor(max_workers=6) as workers:
            results = list(workers.map(
                lambda number: self.call("/agent/confirm", "U1001", first if number % 2 else second), range(100)))
        self.assertEqual(sum(result["result"]["kind"] == "SUCCESS" for result in results), 1)
        self.assertIn("EXPIRED", {result["result"]["kind"] for result in results})
        refunds = self.call("/refunds", "U1001")["items"]
        self.assertEqual(len(refunds), 1)
        self.assertEqual(refunds[0]["status"], "SUCCESS")
        self.assertEqual(refunds[0]["apply_amount"], 16900)
        self.call(f"/refunds/{refunds[0]['refund_no']}", "U1002", expect=404)
        with closing(open_db(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT amount_cents,status FROM outbox_events").fetchall()[0][:],
                             (16900, "PROCESSED"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT refunded_amount,status FROM orders WHERE id=6").fetchone()[:],
                             (16900, "CANCELLED"))

    def test_approval_return_verification_and_staff_handoff(self):
        self.authorize()
        proposal = self.proposal("ORD20261101003", "U1002")
        verify_path = f"/returns/{proposal['refund_no']}/verify"
        evidence = {"evidence": "CARRIER_INTERCEPTED"}
        self.call(verify_path, "leader", evidence, expect=409)
        result = self.call("/agent/confirm", "U1002", proposal)
        self.assertEqual(result["result"]["kind"], "WAIT_APPROVAL")
        task = self.call("/approvals", "leader")["items"][0]
        self.assertEqual(self.call("/approvals", "finance")["items"], [])
        decision_path = f"/approvals/{task['task_no']}/decision"
        self.call(decision_path, "finance", {"decision": "APPROVE"}, expect=403)
        self.call(decision_path, "leader", {"decision": "REJECT"}, expect=409)
        approval = self.call(decision_path, "leader", {"decision": "APPROVE"})
        self.assertEqual(approval["refund_status"], "PENDING_RETURN")
        with closing(open_db(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0], 0)
        self.authorize("SHADOW")
        self.call(verify_path, "leader", evidence, expect=409)
        self.authorize()
        self.call(verify_path, "leader", evidence)
        self.call(verify_path, "leader", evidence, expect=409)
        refund = self.call(f"/refunds/{proposal['refund_no']}", "U1002")
        self.assertEqual(refund["status"], "SUCCESS")
        self.assertIn("RETURN_VERIFIED", [event["status"] for event in refund["events"]])
        handoff = self.call("/agent/handoff", "U1002", {"session_no": proposal["session_no"]})
        self.assertEqual(handoff["result"]["kind"], "HANDOFF")
        ticket = self.call("/tickets", "leader")["items"][0]
        self.call(f"/tickets/{ticket['ticket_no']}/reply", "leader", {"text": "你好，我是人工客服。"})
        view = self.call(f"/agent/sessions/{proposal['session_no']}", "U1002")
        self.assertEqual(view["messages"][-1]["role"], "staff")
        high = self.proposal("ORD20261101005", "U1002")
        self.call("/agent/confirm", "U1002", high)
        self.assertEqual(self.call("/approvals", "leader")["items"], [])
        finance_task = self.call("/approvals", "finance")["items"][0]
        self.assertEqual(finance_task["level"], 2)
        approved = self.call(f"/approvals/{finance_task['task_no']}/decision", "finance", {"decision": "APPROVE"})
        self.assertEqual(approved["refund_status"], "PENDING_RETURN")

    def test_failed_execution_rolls_back_approval(self):
        self.authorize()
        with closing(open_db(self.db)) as conn, conn:
            conn.execute("UPDATE orders SET pay_amount=65000,goods_amount=64000 WHERE id=6")
            conn.execute("UPDATE order_items SET pay_amount=64000 WHERE order_id=6")
        proposal = self.proposal("ORD20261101006")
        self.call("/agent/confirm", "U1001", proposal)
        task = self.call("/approvals", "leader")["items"][0]
        with closing(open_db(self.db)) as conn, conn:
            conn.execute("UPDATE orders SET status='SHIPPED' WHERE id=6")
        path = f"/approvals/{task['task_no']}/decision"
        self.call(path, "leader", {"decision": "APPROVE"}, expect=409)
        self.assertEqual(self.call("/approvals", "leader")["items"][0]["task_no"], task["task_no"])
        with closing(open_db(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0], 0)
            conn.execute("UPDATE orders SET status='PAID' WHERE id=6")
        self.assertEqual(self.call(path, "leader", {"decision": "APPROVE"})["refund_status"], "SUCCESS")

    def test_model_slots_bound_to_input_and_refund_authority(self):
        self.authorize()
        provider = ThreadingHTTPServer(("127.0.0.1", 0), ClassifierFixture)
        provider.requests = []
        provider.replies = [
            {"intent": "REFUND", "order_no": "ORD90000000001", "reason_code": "NO_REASON"},
            {"intent": "REFUND", "order_no": "ORD90000000002", "reason_code": "NO_REASON"},
            {"intent": "REFUND", "order_no": "ORD90000000001", "reason_code": "NO_REASON", "amount": 999999},
        ]
        worker = Thread(target=provider.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(provider.server_close)
        self.addCleanup(worker.join, 5)
        self.addCleanup(provider.shutdown)
        model = ModelReader(f"http://127.0.0.1:{provider.server_port}/v1", "fixture-model")
        self.server.app = DemoApp(self.db, lambda: self.clock.strftime(TS), model=model)
        for account in ("U1001", "quality"):
            self.tokens[account] = self.call("/login", body={"account": account, "password": ACCOUNTS[account][1]})["token"]
        self.assertEqual(self.call("/config", "U1001")["mode"], "SHADOW")
        self.authorize(expect=409)
        shadow = self.chat("把这笔钱返给我，订单号 20261101006，手机号 13800138000")
        self.assertEqual(shadow["result"]["kind"], "SHADOW_REVIEW")
        self.assertIsNone(shadow["session"]["active_proposal"])
        trace_id = shadow["session"]["trace_id"]
        audit = self.call(f"/traces/{trace_id}", "quality")["nlu"]
        self.assertEqual((audit[0]["source"], audit[0]["order_no"]), ("MODEL", "ORD20261101006"))
        sent = provider.requests[0]["messages"][-1]["content"]
        self.assertIn("ORD90000000001", sent)
        self.assertNotIn("20261101006", sent)
        self.assertNotIn("13800138000", sent)
        for failure in ("UNGROUNDED_ORDER", "INVALID_OUTPUT"):
            result = self.chat("查订单 ORD20261101006")
            self.assertEqual(result["result"]["kind"], "ORDER")
            audit = self.call(f"/traces/{result['session']['trace_id']}", "quality")["nlu"][0]
            self.assertEqual((audit["source"], audit["failure"]), ("FALLBACK", failure))
        self.assertEqual(len(provider.requests), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
