"""本地全栈入口。每个请求使用独立连接，业务写入在单一 SQLite 事务内提交。"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from db import connect, open_db, yuan
from evaluate import BUILD, bundled_report, compare, refund_candidate, suite_fingerprint
from nlu import ModelReader, PHONE_RE
from orchestrator import Agent, TS

ROOT = Path(__file__).resolve().parent.parent
API = "/api/v1"
ACCOUNTS = {
    "U1001": ("customer", "customer1001", "小林"),
    "U1002": ("customer", "customer1002", "阿哲"),
    "U1003": ("customer", "customer1003", "Vivian"),
    "leader": ("leader", "leader-demo", "客服主管"),
    "finance": ("finance", "finance-demo", "财务审批"),
    "quality": ("quality", "quality-demo", "质量运营"),
}
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


def now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime(TS)


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = "4000"):
        self.status, self.message, self.code = status, message, code


def field(body: dict, name: str, limit: int = 100) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not 0 < len(value.strip()) <= limit:
        raise ApiError(400, f"请填写有效的 {name}")
    return value.strip()


def owned_session(conn, user_no: str, session_no: str):
    session = conn.execute(
        "SELECT s.* FROM agent_sessions s JOIN users u ON u.id=s.user_id"
        " WHERE s.session_no=? AND u.user_no=?", (session_no, user_no)).fetchone()
    if not session:
        raise ApiError(404, "会话不存在", "3003")
    return session


def session_view(conn, session) -> dict:
    messages = conn.execute(
        "SELECT seq,role,content,card_payload,created_at FROM agent_messages WHERE session_id=? ORDER BY seq",
        (session["id"],)).fetchall()
    active = None
    if session["state"] == "PROPOSAL_PENDING":
        payload = json.loads(session["state_payload"])
        active = {key: payload.get(key) for key in ("refund_no", "confirm_token")}
    return {
        "session_no": session["session_no"], "state": session["state"], "trace_id": session["trace_id"],
        "active_proposal": active,
        "messages": [{"seq": row["seq"], "role": row["role"], "content": row["content"],
                      "created_at": row["created_at"], "card": json.loads(row["card_payload"] or "null")}
                     for row in messages],
    }


def refunds_for(conn, user_no: str, refund_no: str | None = None) -> list[dict]:
    rows = conn.execute(
        "SELECT r.refund_no,r.status,r.apply_amount,r.approved_amount,r.refund_type,r.confirmed_at,"
        " r.approval_level,r.policy_version,o.order_no FROM refund_requests r JOIN users u ON u.id=r.user_id"
        " JOIN orders o ON o.id=r.order_id WHERE u.user_no=? AND r.confirmed_at IS NOT NULL"
        " AND (? IS NULL OR r.refund_no=?) ORDER BY r.id DESC", (user_no, refund_no, refund_no)).fetchall()
    return [{**dict(row), "amount": yuan(row["apply_amount"])} for row in rows]


def refund_events(conn, refund_no: str) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT status,actor,created_at FROM refund_events WHERE refund_no=? ORDER BY id", (refund_no,))]


class DemoApp:
    def __init__(self, path: Path, clock=now, model: ModelReader | None = None,
                 comparison: dict | None = None):
        self.path, self.clock = path, clock
        self.model = model
        self.version = model.version if model else BUILD
        self.comparison = comparison
        if comparison and (not model or comparison.get("model_version") != self.version
                           or comparison.get("suite_fingerprint") != suite_fingerprint()):
            raise ValueError("评测报告与当前模型、提示词或默认评测集不一致")
        self.model_authorized = bool(comparison and comparison["gate"]["passed"])
        self.tokens = {secrets.token_urlsafe(32): name for name in ACCOUNTS}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() or self.path.stat().st_size == 0:
            with closing(connect(self.path, seed_at=self.clock())):
                pass
        with closing(open_db(self.path)) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS runtime_authority "
                         "(id INTEGER PRIMARY KEY CHECK(id=1),mode TEXT NOT NULL,candidate_version TEXT)")
            if "candidate_version" not in {row["name"] for row in conn.execute("PRAGMA table_info(runtime_authority)")}:
                conn.execute("ALTER TABLE runtime_authority ADD COLUMN candidate_version TEXT")
            conn.execute("UPDATE runtime_authority SET candidate_version=? WHERE candidate_version IS NULL", (BUILD,))
            conn.execute("INSERT OR IGNORE INTO runtime_authority VALUES (1,'SHADOW',?)", (BUILD,))
            conn.execute("CREATE TABLE IF NOT EXISTS authority_events "
                         "(id INTEGER PRIMARY KEY AUTOINCREMENT,mode TEXT,actor TEXT,created_at TEXT)")
            conn.execute("""CREATE TABLE IF NOT EXISTS shadow_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL, order_no TEXT NOT NULL,
                reason_code TEXT NOT NULL, candidate_version TEXT NOT NULL, candidate TEXT NOT NULL,
                order_snapshot TEXT NOT NULL,
                baseline TEXT, outcome TEXT, reviewer TEXT, note TEXT, created_at TEXT NOT NULL, reviewed_at TEXT
            )""")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(shadow_observations)")}
            if "baseline_revision" not in columns:
                conn.execute("ALTER TABLE shadow_observations ADD COLUMN baseline_revision INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE shadow_observations SET baseline_revision=1"
                         " WHERE baseline IS NOT NULL AND baseline_revision=0")
            conn.execute("""CREATE TABLE IF NOT EXISTS shadow_review_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, observation_id INTEGER NOT NULL,
                revision INTEGER NOT NULL, baseline TEXT NOT NULL, outcome TEXT NOT NULL,
                reviewer TEXT, note TEXT, change_reason TEXT, created_at TEXT NOT NULL,
                UNIQUE(observation_id, revision)
            )""")
            conn.execute(
                "INSERT OR IGNORE INTO shadow_review_history"
                " (observation_id,revision,baseline,outcome,reviewer,note,created_at)"
                " SELECT id,baseline_revision,baseline,outcome,reviewer,note,COALESCE(reviewed_at,created_at)"
                " FROM shadow_observations WHERE baseline IS NOT NULL")
            conn.execute("""CREATE TABLE IF NOT EXISTS nlu_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
                candidate_version TEXT NOT NULL, source TEXT NOT NULL, intent TEXT NOT NULL,
                order_no TEXT, reason_code TEXT, latency_ms INTEGER NOT NULL, failure TEXT,
                created_at TEXT NOT NULL
            )""")

    def login(self, body: dict) -> dict:
        account = field(body, "account")
        password = field(body, "password")
        record = ACCOUNTS.get(account)
        if not record or not secrets.compare_digest(record[1].encode(), password.encode()):
            raise ApiError(401, "演示账号或口令不正确", "1001")
        token = next(token for token, name in self.tokens.items() if name == account)
        return {"token": token, "account": account, "role": record[0], "name": record[2]}

    def dispatch(self, method: str, path: str, auth: str, body: dict) -> dict:
        if method == "GET" and path == "/health":
            return {"status": "ok", "settlement": "MOCK"}
        if method == "POST" and path == "/login":
            return self.login(body)
        user_no = self.tokens.get(auth.removeprefix("Bearer ")) if auth.startswith("Bearer ") else None
        if user_no is None:
            raise ApiError(401, "请登录演示账号", "1001")
        role = ACCOUNTS[user_no][0]
        with closing(open_db(self.path)) as conn, conn:
            agent = Agent(conn, f"tr_{uuid4().hex}", self.clock, model=self.model)
            prepared = None
            if self.model and role == "customer" and method == "POST" and path == "/agent/chat":
                # 等待模型期间不占用 SQLite 写锁。编排会检查会话上下文是否变化。
                text = PHONE_RE.sub("[手机号已隐藏]", field(body, "text", 1000))
                session = None
                if "session_no" in body:
                    number = field(body, "session_no", 80)
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", number):
                        raise ApiError(400, "会话编号格式无效")
                    row = conn.execute("SELECT * FROM agent_sessions WHERE session_no=?", (number,)).fetchone()
                    if row:
                        session = dict(owned_session(conn, user_no, number))
                context = Agent.nlu_context(session, text)
                prepared = (context, agent.read_turn(text, context) if context is not None else None)
            conn.execute("BEGIN IMMEDIATE" if method == "POST" else "BEGIN")
            authority = conn.execute("SELECT mode,candidate_version FROM runtime_authority WHERE id=1").fetchone()
            mode = authority["mode"]
            if authority["candidate_version"] != self.version or (self.model and not self.model_authorized):
                mode = "SHADOW"
            agent.shadow = mode == "SHADOW"
            if method == "GET" and path == "/config":
                return {"mode": mode, "settlement": "MOCK", "nlu_version": self.version,
                        "nlu_mode": "MODEL" if self.model else "RULES"}
            if role == "customer":
                return self.customer(conn, agent, method, path, user_no, body, mode, prepared)
            return self.staff(conn, agent, method, path, user_no, role, body, mode)

    def customer(self, conn, agent, method, path, user_no, body, mode, prepared):
        if method == "GET" and path == "/orders":
            rows = conn.execute(
                "SELECT o.order_no,o.status,o.pay_amount,o.after_sale_status,"
                " (SELECT group_concat(sku_name,'、') FROM order_items WHERE order_id=o.id) title"
                " FROM orders o JOIN users u ON u.id=o.user_id WHERE u.user_no=? ORDER BY o.id", (user_no,))
            return {"items": [{**dict(row), "amount": yuan(row["pay_amount"])} for row in rows]}
        if method == "GET" and path == "/refunds":
            return {"items": refunds_for(conn, user_no)}
        if method == "GET" and re.fullmatch(r"/refunds/RF\d+", path):
            refund_no = path.rsplit("/", 1)[1]
            items = refunds_for(conn, user_no, refund_no)
            if not items:
                raise ApiError(404, "退款单不存在", "3002")
            return {**items[0], "events": refund_events(conn, refund_no)}
        if method == "GET" and path == "/agent/sessions":
            rows = conn.execute(
                "SELECT s.session_no,s.state,s.updated_at FROM agent_sessions s JOIN users u ON u.id=s.user_id"
                " WHERE u.user_no=? ORDER BY s.id DESC LIMIT 30", (user_no,))
            return {"items": [dict(row) for row in rows]}
        if method == "GET" and re.fullmatch(r"/agent/sessions/[A-Za-z0-9_-]{1,80}", path):
            return session_view(conn, owned_session(conn, user_no, path.rsplit("/", 1)[1]))
        if method == "POST" and path in {"/agent/chat", "/agent/confirm", "/agent/handoff"}:
            session_no = field(body, "session_no", 80) if "session_no" in body else f"S_{uuid4().hex}"
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", session_no):
                raise ApiError(400, "会话编号格式无效")
            if path == "/agent/chat":
                result = agent.handle(session_no, user_no, field(body, "text", 1000), prepared)
            elif path == "/agent/handoff":
                result = agent.request_handoff(session_no, user_no)
            else:
                owned_session(conn, user_no, session_no)
                action = field(body, "action")
                if action not in {"CONFIRM", "CANCEL"}:
                    raise ApiError(400, "请选择确认或取消")
                if mode != "DELEGATED" and action == "CONFIRM":
                    raise ApiError(409, "系统退款权限已暂停", "4004")
                result = agent.respond_to_proposal(session_no, user_no, field(body, "refund_no"),
                                                   field(body, "confirm_token"), action)
                if result.get("ok") is False:
                    raise ApiError(404 if result["code"] == "3002" else 409, result["message"], result["code"])
            session = owned_session(conn, user_no, session_no)
            reading = result.get("nlu")
            if reading:
                conn.execute(
                    "INSERT INTO nlu_events(session_id,candidate_version,source,intent,order_no,reason_code,"
                    " latency_ms,failure,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (session["id"], self.version, reading["source"], reading["intent"], reading["order_no"],
                     reading["reason_code"], reading["latency_ms"], reading["failure"], self.clock()))
            if mode == "SHADOW" and result["kind"] == "SHADOW_REVIEW":
                decision = refund_candidate(result["decision"])
                decision["nlu_source"] = reading["source"] if reading else None
                payload = json.loads(session["state_payload"])
                facts = dict(conn.execute(
                    "SELECT o.status,o.pay_amount,o.refunded_amount,o.delivered_at,o.shipped_at,o.paid_at,o.after_sale_status,"
                    " u.risk_level,u.refund_rate_30d,"
                    " (SELECT group_concat(category_code) FROM order_items WHERE order_id=o.id) AS categories"
                    " FROM orders o JOIN users u ON u.id=o.user_id WHERE o.order_no=?",
                    (session["order_no_ctx"],)).fetchone())
                conn.execute(
                    "INSERT INTO shadow_observations(trace_id,order_no,reason_code,candidate_version,candidate,"
                    " order_snapshot,created_at) VALUES(?,?,?,?,?,?,?)",
                    (session["trace_id"], session["order_no_ctx"], payload["reason_code"], self.version,
                     json.dumps(decision), json.dumps(facts), agent.clock()))
            return {"result": {key: result[key] for key in ("kind", "state") if key in result},
                    "session": session_view(conn, session)}
        raise ApiError(403, "当前账号没有该操作权限", "1002")

    def staff(self, conn, agent, method, path, user_no, role, body, mode):
        if role == "quality" and path in {"/quality/report", "/quality/authority"}:
            dangerous = conn.execute(
                "SELECT COUNT(*) FROM shadow_observations WHERE outcome='DANGEROUS' AND candidate_version=?",
                (self.version,)).fetchone()[0]
            gate_passed = self.model_authorized if self.model else bundled_report()["gate"]["passed"]
            if method == "POST" and path == "/quality/authority":
                requested = field(body, "mode")
                if requested not in {"DELEGATED", "SHADOW"}:
                    raise ApiError(400, "授权模式无效")
                if requested == "DELEGATED" and (not gate_passed or dangerous):
                    raise ApiError(409, "评测门槛未通过或存在危险影子分歧")
                if requested != mode:
                    conn.execute("UPDATE runtime_authority SET mode=?,candidate_version=? WHERE id=1",
                                 (requested, self.version))
                    conn.execute("INSERT INTO authority_events(mode,actor,created_at) VALUES (?,?,?)",
                                 (requested, user_no, self.clock()))
                    mode = requested
            if method == "GET" and path == "/quality/report":
                if self.model:
                    if self.comparison:
                        report = self.comparison["model"]
                        return {**report, "gate": self.comparison["gate"],
                                "comparison": self.comparison["summary"]}
                    return {**bundled_report(), "gate": {"passed": False},
                            "source": "当前候选暂无评测报告，指标展示规则式基线"}
                return bundled_report()
            blockers = [dict(row) for row in conn.execute(
                "SELECT id,order_no FROM shadow_observations"
                " WHERE outcome='DANGEROUS' AND candidate_version=? ORDER BY id", (self.version,))]
            return {"mode": mode, "live_dangerous": dangerous, "blockers": blockers,
                    "gate_passed": gate_passed, "events": [dict(row) for row in conn.execute(
                "SELECT mode,actor,created_at FROM authority_events ORDER BY id DESC LIMIT 10")]}
        if role == "quality" and method == "GET" and path == "/quality/shadow":
            rows = conn.execute(
                "SELECT * FROM shadow_observations ORDER BY"
                " CASE WHEN outcome='DANGEROUS' AND candidate_version=? THEN 0 ELSE 1 END,id DESC LIMIT 50",
                (self.version,)).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["baseline"] = json.loads(row["baseline"]) if row["baseline"] else None
                item["order_snapshot"] = json.loads(row["order_snapshot"])
                # 先独立记录人工决定，再揭示候选结果。
                item["candidate"] = json.loads(row["candidate"]) if row["baseline"] else None
                item["history"] = [
                    {**dict(review), "baseline": json.loads(review["baseline"])}
                    for review in conn.execute(
                        "SELECT revision,baseline,outcome,reviewer,note,change_reason,created_at"
                        " FROM shadow_review_history WHERE observation_id=? ORDER BY revision", (row["id"],))]
                items.append(item)
            return {"items": items}
        if role == "quality" and method == "POST" and re.fullmatch(r"/quality/shadow/\d+/(baseline|review)", path):
            observation_id = int(path.split("/")[3])
            row = conn.execute("SELECT * FROM shadow_observations WHERE id=?", (observation_id,)).fetchone()
            if not row:
                raise ApiError(404, "影子记录不存在")
            correcting = path.endswith("/review")
            if row["baseline"] and not correcting:
                raise ApiError(409, "该人工基线已经登记")
            if correcting and not row["baseline"]:
                raise ApiError(409, "请先登记独立人工基线")
            change_reason = None
            if correcting:
                if type(body.get("expected_revision")) is not int or body["expected_revision"] != row["baseline_revision"]:
                    raise ApiError(409, "基线已被更正，请刷新后重新复核")
                change_reason = PHONE_RE.sub("[手机号已隐藏]", field(body, "change_reason", 500))
            action = field(body, "action")
            note = PHONE_RE.sub("[手机号已隐藏]", field(body, "note", 500))
            if action not in {"PROPOSAL", "REJECT", "HANDOFF", "NEED_EVIDENCE"}:
                raise ApiError(400, "人工动作无效")
            baseline = {"action": action}
            if action == "PROPOSAL":
                cents, level = body.get("amount_cents"), body.get("approval_level")
                refund_type = field(body, "refund_type")
                if (type(cents) is not int or not 0 < cents <= 100_000_000
                        or type(level) is not int or level not in {0, 1, 2}
                        or refund_type not in {"CANCEL", "RETURN_REFUND", "REFUND_ONLY"}):
                    raise ApiError(400, "请填写合法的金额、审批级别和退款类型")
                baseline.update(amount_cents=cents, approval_level=level, refund_type=refund_type,
                                return_gate=(refund_type == "RETURN_REFUND"))
            outcome, deltas = compare(json.loads(row["candidate"]), baseline)
            revision = row["baseline_revision"] + 1
            reviewed_at = self.clock()
            conn.execute(
                "UPDATE shadow_observations SET baseline=?,outcome=?,reviewer=?,note=?,reviewed_at=?,baseline_revision=?"
                " WHERE id=?", (json.dumps(baseline), outcome, user_no, note, reviewed_at, revision, observation_id))
            conn.execute(
                "INSERT INTO shadow_review_history"
                " (observation_id,revision,baseline,outcome,reviewer,note,change_reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (observation_id, revision, json.dumps(baseline), outcome, user_no, note, change_reason, reviewed_at))
            if outcome == "DANGEROUS" and mode == "DELEGATED" and row["candidate_version"] == self.version:
                conn.execute("UPDATE runtime_authority SET mode='SHADOW' WHERE id=1")
                conn.execute("INSERT INTO authority_events(mode,actor,created_at) VALUES ('SHADOW',?,?)",
                             (f"{user_no}/DANGEROUS", self.clock()))
            return {"outcome": outcome, "deltas": deltas, "baseline_revision": revision}
        if role == "quality" and method == "GET" and re.fullmatch(r"/traces/tr_[a-f0-9]+", path):
            trace = path.rsplit("/", 1)[1]
            session = conn.execute("SELECT * FROM agent_sessions WHERE trace_id=?", (trace,)).fetchone()
            if not session:
                raise ApiError(404, "未找到审计链路")
            view = session_view(conn, session)
            view.pop("active_proposal")
            view["transitions"] = [dict(row) for row in conn.execute(
                "SELECT from_state,to_state,event,operator,created_at FROM agent_state_events"
                " WHERE session_id=? ORDER BY id", (session["id"],))]
            view["tools"] = [dict(row) for row in conn.execute(
                "SELECT tool_name,status,error_code,created_at FROM agent_tool_calls WHERE session_id=? ORDER BY id",
                (session["id"],))]
            view["nlu"] = [dict(row) for row in conn.execute(
                "SELECT candidate_version,source,intent,order_no,reason_code,latency_ms,failure,created_at FROM nlu_events"
                " WHERE session_id=? ORDER BY id", (session["id"],))]
            view["refunds"] = [{"refund_no": row["refund_no"], "events": refund_events(conn, row["refund_no"])}
                               for row in conn.execute("SELECT refund_no FROM refund_requests WHERE session_id=?",
                                                       (session["id"],))]
            return view
        if role in {"leader", "finance"} and method == "GET" and path == "/approvals":
            level = 1 if role == "leader" else 2
            return {"items": [task for task in agent.tools.pending_tasks() if task["level"] == level]}
        if role in {"leader", "finance"} and method == "POST" and re.fullmatch(r"/approvals/AP\d+/decision", path):
            task_no = path.split("/")[2]
            task = conn.execute("SELECT level FROM approval_tasks WHERE task_no=?", (task_no,)).fetchone()
            if not task:
                raise ApiError(404, "审批任务不存在", "3002")
            if task["level"] != (1 if role == "leader" else 2):
                raise ApiError(403, "该任务不属于当前审批角色", "6001")
            self.require_delegated(mode)
            comment = PHONE_RE.sub("[手机号已隐藏]", field(body, "comment", 500)) if body.get("comment") else ""
            result = agent.on_approval(task_no, field(body, "decision"), user_no, comment)
            if not result["ok"]:
                raise ApiError(409, result["error"]["message"], result["error"]["code"])
            return result
        if role == "leader" and method == "GET" and path == "/returns":
            return {"items": [dict(row) for row in conn.execute(
                "SELECT r.refund_no,r.apply_amount,r.refund_type,o.order_no FROM refund_requests r"
                " JOIN orders o ON o.id=r.order_id WHERE r.status='PENDING_RETURN' ORDER BY r.id")]}
        if role == "leader" and method == "POST" and re.fullmatch(r"/returns/RF\d+/verify", path):
            self.require_delegated(mode)
            refund_no = path.split("/")[2]
            if body.get("evidence") not in {"WAREHOUSE_RECEIVED", "CARRIER_INTERCEPTED"}:
                raise ApiError(400, "请登记已核验的回执类型")
            result = agent.on_return_verified(refund_no, f"{user_no}/{body['evidence']}")
            if not result["ok"]:
                raise ApiError(409, result["error"]["message"], result["error"]["code"])
            return result["data"]
        if role == "leader" and method == "GET" and path == "/tickets":
            return {"items": [dict(row) for row in conn.execute(
                "SELECT ticket_no,reason,summary,status,created_at FROM service_tickets ORDER BY id DESC")]}
        if role == "leader" and method == "POST" and re.fullmatch(r"/tickets/TK\d+/reply", path):
            ticket_no = path.split("/")[2]
            ticket = conn.execute("SELECT * FROM service_tickets WHERE ticket_no=?", (ticket_no,)).fetchone()
            if not ticket:
                raise ApiError(404, "工单不存在")
            session = conn.execute("SELECT * FROM agent_sessions WHERE id=?", (ticket["session_id"],)).fetchone()
            reply = PHONE_RE.sub("[手机号已隐藏]", field(body, "text", 1000))
            agent._say(session, "staff", reply)
            conn.execute("UPDATE service_tickets SET status='ASSIGNED' WHERE id=?", (ticket["id"],))
            return {"ticket_no": ticket_no, "status": "ASSIGNED"}
        raise ApiError(403, "当前账号没有该操作权限", "1002")

    @staticmethod
    def require_delegated(mode: str):
        if mode != "DELEGATED":
            raise ApiError(409, "系统退款权限已暂停", "4004")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method: str):
        path = urlsplit(self.path).path
        if method == "GET" and path in ASSETS:
            filename, content_type = ASSETS[path]
            self.send_content(200, (ROOT / "frontend" / filename).read_bytes(), content_type)
            return
        if not path.startswith(API + "/"):
            self.send_json(404, error={"code": "3000", "message": "接口不存在"})
            return
        try:
            body = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 16384 or self.headers.get_content_type() != "application/json":
                    raise ApiError(400, "请提交有效的 JSON 请求")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ApiError(400, "请求体必须是 JSON 对象")
            data = self.server.app.dispatch(method, path[len(API):], self.headers.get("Authorization", ""), body)
            self.send_json(200, data=data)
        except ApiError as exc:
            self.send_json(exc.status, error={"code": exc.code, "message": exc.message})
        except PermissionError:
            self.send_json(404, error={"code": "3003", "message": "会话不存在"})
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, error={"code": "4000", "message": "JSON 格式错误"})
        except ValueError as exc:
            self.log_error("Request rejected: %s", type(exc).__name__)
            self.send_json(409, error={"code": "4004", "message": "当前请求无法处理，请刷新状态后重试"})
        except sqlite3.Error as exc:
            self.log_error("SQLite failure: %s", type(exc).__name__)
            self.send_json(503, error={"code": "5001", "message": "本地数据库暂时无法处理请求"})

    def send_json(self, status, data=None, error=None):
        value = json.dumps({"ok": error is None, "data": data, "error": error}, ensure_ascii=False)
        self.send_content(status, value.encode("utf-8"), "application/json; charset=utf-8")

    def send_content(self, status, content, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "demo.sqlite3")
    parser.add_argument("--nlu", choices=["rules", "model"], default="rules")
    parser.add_argument("--nlu-report", type=Path, help="由 evaluate.py --compare 导出的模型评测报告")
    args = parser.parse_args()
    model = ModelReader.from_env() if args.nlu == "model" else None
    comparison = json.loads(args.nlu_report.read_text(encoding="utf-8")) if args.nlu_report else None
    app = DemoApp(args.db, model=model, comparison=comparison)
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.app = app
        print(f"AI 售后工作台 http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
