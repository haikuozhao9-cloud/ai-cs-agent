"""
SQLite 版数据层：字段语义与 db/schema.sql 一致（MySQL 版为生产脚本）。

约定：
  1) 金额统一以「分」存整数，避免浮点误差；对外展示时再转元。
  2) 只建原型需要的表，字段名与生产表保持一致，便于对照。
  3) 所有时间用文本 ISO 格式 'YYYY-MM-DD HH:MM:SS'，便于人读与比较。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

NOW = "2026-11-17 18:30:00"          # 演示基准时间，保证结果可复现
SCHEMA_PATH_HINT = "db/schema.sql"   # 生产 DDL 位置（仅注释引用）


SCHEMA = """
CREATE TABLE users (
  id           INTEGER PRIMARY KEY,
  user_no      TEXT UNIQUE NOT NULL,
  nickname     TEXT NOT NULL,
  phone_tail4  TEXT NOT NULL,
  member_level INTEGER NOT NULL DEFAULT 1,
  risk_level   INTEGER NOT NULL DEFAULT 0,
  refund_cnt_30d  INTEGER NOT NULL DEFAULT 0,
  refund_rate_30d REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE categories (
  code               TEXT PRIMARY KEY,
  name               TEXT NOT NULL,
  support_no_reason  INTEGER NOT NULL DEFAULT 1,
  no_reason_days     INTEGER NOT NULL DEFAULT 7,
  return_type_default TEXT NOT NULL DEFAULT 'RETURN_REFUND',
  need_quality_evidence INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE orders (
  id                INTEGER PRIMARY KEY,
  order_no          TEXT UNIQUE NOT NULL,
  user_id           INTEGER NOT NULL,
  status            TEXT NOT NULL,
  after_sale_status TEXT NOT NULL DEFAULT 'NONE',
  channel           TEXT NOT NULL DEFAULT 'APP',
  goods_amount      INTEGER NOT NULL DEFAULT 0,
  freight_amount    INTEGER NOT NULL DEFAULT 0,
  discount_amount   INTEGER NOT NULL DEFAULT 0,
  pay_amount        INTEGER NOT NULL DEFAULT 0,
  refunded_amount   INTEGER NOT NULL DEFAULT 0,
  is_free_shipping  INTEGER NOT NULL DEFAULT 0,
  has_excluded_item INTEGER NOT NULL DEFAULT 0,
  receiver_region   TEXT NOT NULL DEFAULT '',
  paid_at           TEXT,
  shipped_at        TEXT,
  delivered_at      TEXT,
  version           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE order_items (
  id            INTEGER PRIMARY KEY,
  order_id      INTEGER NOT NULL,
  sku_code      TEXT NOT NULL,
  sku_name      TEXT NOT NULL,
  category_code TEXT NOT NULL,
  qty           INTEGER NOT NULL DEFAULT 1,
  unit_price    INTEGER NOT NULL DEFAULT 0,
  pay_amount    INTEGER NOT NULL DEFAULT 0,
  refunded_qty  INTEGER NOT NULL DEFAULT 0,
  is_virtual    INTEGER NOT NULL DEFAULT 0,
  is_customized INTEGER NOT NULL DEFAULT 0,
  is_fresh      INTEGER NOT NULL DEFAULT 0,
  is_opened     INTEGER NOT NULL DEFAULT 0,
  after_sale_status TEXT NOT NULL DEFAULT 'NONE'
);

CREATE TABLE shipments (
  id           INTEGER PRIMARY KEY,
  order_id     INTEGER NOT NULL,
  shipment_no  TEXT UNIQUE NOT NULL,
  carrier_name TEXT NOT NULL,
  tracking_no  TEXT NOT NULL,
  status       TEXT NOT NULL,
  shipped_at   TEXT,
  estimated_at TEXT,
  signed_at    TEXT,
  signed_by    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE shipment_traces (
  id          INTEGER PRIMARY KEY,
  shipment_id INTEGER NOT NULL,
  trace_time  TEXT NOT NULL,
  status_code TEXT NOT NULL,
  description TEXT NOT NULL,
  location    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE refund_policies (
  id            INTEGER PRIMARY KEY,
  policy_no     TEXT NOT NULL,
  name          TEXT NOT NULL,
  scope_type    TEXT NOT NULL DEFAULT 'GLOBAL',
  scope_value   TEXT NOT NULL DEFAULT '*',
  rule_json     TEXT NOT NULL,
  priority      INTEGER NOT NULL DEFAULT 0,
  version       INTEGER NOT NULL DEFAULT 1,
  enabled       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE refund_requests (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  refund_no         TEXT UNIQUE NOT NULL,
  order_id          INTEGER NOT NULL,
  user_id           INTEGER NOT NULL,
  session_id        INTEGER,
  refund_type       TEXT NOT NULL,
  reason_code       TEXT NOT NULL,
  reason_desc       TEXT NOT NULL DEFAULT '',
  goods_amount      INTEGER NOT NULL DEFAULT 0,
  freight_refund    INTEGER NOT NULL DEFAULT 0,
  freight_deduct    INTEGER NOT NULL DEFAULT 0,
  discount_deduct   INTEGER NOT NULL DEFAULT 0,
  apply_amount      INTEGER NOT NULL DEFAULT 0,
  approved_amount   INTEGER NOT NULL DEFAULT 0,
  status            TEXT NOT NULL DEFAULT 'DRAFT',
  decision_code     TEXT NOT NULL DEFAULT '',
  decision_snapshot TEXT,
  policy_version    INTEGER NOT NULL DEFAULT 0,
  approval_level    INTEGER NOT NULL DEFAULT 0,
  refund_seq        INTEGER NOT NULL DEFAULT 1,
  confirm_token_hash TEXT,
  confirm_expire_at TEXT,
  confirmed_at      TEXT,
  finished_at       TEXT,
  operator_type     TEXT NOT NULL DEFAULT 'AGENT',
  trace_id          TEXT NOT NULL DEFAULT '',
  created_at        TEXT NOT NULL DEFAULT '',
  updated_at        TEXT NOT NULL DEFAULT '',
  version           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE refund_items (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  refund_id     INTEGER NOT NULL,
  order_item_id INTEGER NOT NULL,
  qty           INTEGER NOT NULL DEFAULT 1,
  amount        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE refund_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  refund_no TEXT NOT NULL,
  status TEXT NOT NULL,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE outbox_events (
  refund_no TEXT PRIMARY KEY,
  amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  processed_at TEXT
);

CREATE TABLE approval_tasks (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  task_no        TEXT UNIQUE NOT NULL,
  biz_type       TEXT NOT NULL DEFAULT 'REFUND',
  biz_id         INTEGER NOT NULL,
  level          INTEGER NOT NULL,
  status         TEXT NOT NULL DEFAULT 'PENDING',
  assignee_role  TEXT NOT NULL DEFAULT 'CS_LEADER',
  reason         TEXT NOT NULL DEFAULT '',
  decision_comment TEXT NOT NULL DEFAULT '',
  sla_deadline   TEXT NOT NULL,
  decided_at     TEXT,
  decided_by     TEXT,
  created_at     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE agent_sessions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  session_no     TEXT UNIQUE NOT NULL,
  user_id        INTEGER,
  state          TEXT NOT NULL DEFAULT 'INIT',
  state_payload  TEXT NOT NULL DEFAULT '{}',
  order_no_ctx   TEXT NOT NULL DEFAULT '',
  refund_id      INTEGER,
  intent         TEXT NOT NULL DEFAULT '',
  turn_count     INTEGER NOT NULL DEFAULT 0,
  fallback_count INTEGER NOT NULL DEFAULT 0,
  human_takeover INTEGER NOT NULL DEFAULT 0,
  trace_id       TEXT NOT NULL DEFAULT '',
  created_at     TEXT NOT NULL DEFAULT '',
  updated_at     TEXT NOT NULL DEFAULT '',
  version        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE agent_messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  seq        INTEGER NOT NULL,
  role       TEXT NOT NULL,
  content    TEXT NOT NULL,
  card_payload TEXT,
  tool_name  TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE agent_tool_calls (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  call_id    TEXT NOT NULL,
  tool_name  TEXT NOT NULL,
  args       TEXT NOT NULL,
  result     TEXT,
  status     TEXT NOT NULL DEFAULT 'SUCCESS',
  error_code TEXT NOT NULL DEFAULT '',
  latency_ms INTEGER NOT NULL DEFAULT 0,
  trace_id   TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE agent_state_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  from_state TEXT NOT NULL,
  to_state   TEXT NOT NULL,
  event      TEXT NOT NULL,
  payload    TEXT,
  operator   TEXT NOT NULL DEFAULT 'AGENT',
  created_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE service_tickets (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no  TEXT UNIQUE NOT NULL,
  session_id INTEGER,
  reason     TEXT NOT NULL,
  summary    TEXT,
  priority   INTEGER NOT NULL DEFAULT 2,
  status     TEXT NOT NULL DEFAULT 'OPEN',
  created_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE knowledge_articles (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  title    TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT '',
  content  TEXT NOT NULL,
  tags     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_orders_user ON orders(user_id);
CREATE INDEX idx_refund_order ON refund_requests(order_id);
CREATE UNIQUE INDEX idx_confirmed_refund_seq ON refund_requests(order_id, refund_seq)
  WHERE confirmed_at IS NOT NULL;
CREATE INDEX idx_events_session ON agent_state_events(session_id);
"""


ORDERS = [
    # order_no, user_id, status, goods, freight, discount, pay, free_ship, excluded, paid, shipped, delivered
    ("ORD20261101001", 1, "DELIVERED", 32900, 0, 3000, 29900, 1, 0,
     "2026-11-10 10:12:00", "2026-11-11 09:30:00", "2026-11-14 16:20:00"),
    ("ORD20261101002", 1, "COMPLETED", 49900, 0, 4000, 45900, 1, 0,
     "2026-10-26 20:05:00", "2026-10-27 11:00:00", "2026-11-05 14:10:00"),
    ("ORD20261101003", 2, "SHIPPED", 188000, 0, 0, 188000, 1, 0,
     "2026-11-15 21:40:00", "2026-11-16 10:15:00", None),
    ("ORD20261101004", 3, "DELIVERED", 12800, 1200, 0, 14000, 0, 1,
     "2026-11-15 08:00:00", "2026-11-15 15:00:00", "2026-11-16 09:30:00"),
    ("ORD20261101005", 2, "DELIVERED", 320000, 0, 0, 320000, 1, 0,
     "2026-11-14 12:00:00", "2026-11-14 18:00:00", "2026-11-15 11:00:00"),
    ("ORD20261101006", 1, "PAID", 15900, 1000, 0, 16900, 0, 0,
     "2026-11-17 14:00:00", None, None),
]

ITEMS = [
    # order_id, sku, name, category, qty, unit, pay, virtual, custom, fresh, opened
    (1, "SKU-A1001", "纯棉针织开衫 米白 M", "APPAREL", 1, 32900, 29900, 0, 0, 0, 0),
    (2, "SKU-B2002", "轻量跑鞋 42 灰", "APPAREL", 1, 49900, 45900, 0, 0, 0, 1),
    (3, "SKU-C3003", "无线吸尘器 V8", "DIGITAL", 1, 188000, 188000, 0, 0, 0, 0),
    (4, "SKU-D4004", "智利车厘子 2斤装", "FRESH", 1, 12800, 12800, 0, 0, 1, 0),
    (5, "SKU-E5005", "智能手机 Pro 256G", "DIGITAL", 1, 320000, 320000, 0, 0, 0, 1),
    (6, "SKU-F6006", "桌面收纳盒 雾蓝", "GENERAL", 1, 15900, 15900, 0, 0, 0, 0),
]

SHIPMENTS = [
    (1, "SHP0001", "顺丰速运", "SF1388200111", "SIGNED",
     "2026-11-11 09:30:00", "2026-11-14 18:00:00", "2026-11-14 16:20:00", "本人"),
    (2, "SHP0002", "中通快递", "ZT7733200222", "SIGNED",
     "2026-10-27 11:00:00", "2026-11-05 18:00:00", "2026-11-05 14:10:00", "快递柜"),
    (3, "SHP0003", "京东物流", "JD9900300333", "IN_TRANSIT",
     "2026-11-16 10:15:00", "2026-11-18 20:00:00", None, ""),
    (4, "SHP0004", "顺丰速运", "SF1388200444", "SIGNED",
     "2026-11-15 15:00:00", "2026-11-16 12:00:00", "2026-11-16 09:30:00", "本人"),
    (5, "SHP0005", "顺丰速运", "SF1388200555", "SIGNED",
     "2026-11-14 18:00:00", "2026-11-15 12:00:00", "2026-11-15 11:00:00", "本人"),
]

TRACES = [
    (1, "2026-11-11 09:30:00", "PICKED", "顺丰速运已收取快件", "深圳市"),
    (1, "2026-11-12 22:10:00", "IN_TRANSIT", "快件已到达深圳南山集散点", "深圳市"),
    (1, "2026-11-14 08:40:00", "OUT_FOR_DELIVERY", "快件正在派送途中", "深圳市"),
    (1, "2026-11-14 16:20:00", "SIGNED", "快件已签收，签收人：本人", "深圳市"),
    (2, "2026-11-05 14:10:00", "SIGNED", "快件已存入丰巢快递柜", "深圳市"),
    (3, "2026-11-16 10:15:00", "PICKED", "京东物流已揽收", "杭州市"),
    (3, "2026-11-17 07:20:00", "IN_TRANSIT", "快件已到达杭州转运中心", "杭州市"),
    (3, "2026-11-17 15:50:00", "IN_TRANSIT", "快件已发往上海分拨中心", "杭州市"),
    (4, "2026-11-16 09:30:00", "SIGNED", "快件已签收，签收人：本人", "上海市"),
    (5, "2026-11-15 11:00:00", "SIGNED", "快件已签收，签收人：本人", "杭州市"),
]

GLOBAL_RULE = {
    "no_reason_days": 7,
    "max_refund_times": 2,
    "auto_approve_limit": 50000,       # 500 元 = 50000 分
    "l1_limit": 200000,                # 2000 元
    "freight_deduct_on_no_reason": True,
    "refund_rate_threshold": 0.60,
    "excluded_categories": ["VIRTUAL", "CUSTOM", "FRESH", "UNDERWEAR"],
    "forbid_status": ["CREATED", "CANCELLED", "CLOSED"],
    "sla_hours": {"L1": 4, "L2": 8},
}


def open_db(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def connect(path: str | Path = ":memory:", seed_at: str = NOW) -> sqlite3.Connection:
    conn = open_db(path)
    conn.executescript(SCHEMA)
    _seed(conn)
    if seed_at != NOW:
        # 将演示订单相对于演示基准的时间差保留在新数据库中。
        for table, columns in {
            "orders": ("paid_at", "shipped_at", "delivered_at"),
            "shipments": ("shipped_at", "estimated_at", "signed_at"),
            "shipment_traces": ("trace_time",),
        }.items():
            for column in columns:
                conn.execute(
                    f"UPDATE {table} SET {column}=datetime(?, (unixepoch({column}) - unixepoch(?)) || ' seconds')",
                    (seed_at, NOW))
        conn.commit()
    return conn


def _seed(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO users(id,user_no,nickname,phone_tail4,member_level,risk_level,refund_cnt_30d,refund_rate_30d)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [
            (1, "U1001", "小林", "6601", 3, 0, 1, 0.125),
            (2, "U1002", "阿哲", "8820", 2, 0, 0, 0.0),
            (3, "U1003", "Vivian", "3312", 4, 0, 7, 0.5833),
        ],
    )
    conn.executemany(
        "INSERT INTO categories(code,name,support_no_reason,no_reason_days,return_type_default,need_quality_evidence)"
        " VALUES (?,?,?,?,?,?)",
        [
            ("APPAREL", "服饰鞋包", 1, 7, "RETURN_REFUND", 0),
            ("DIGITAL", "数码家电", 1, 7, "RETURN_REFUND", 0),
            ("FRESH", "生鲜食品", 0, 0, "REFUND_ONLY", 1),
            ("VIRTUAL", "虚拟商品", 0, 0, "REFUND_ONLY", 0),
            ("CUSTOM", "定制商品", 0, 0, "RETURN_REFUND", 1),
            ("UNDERWEAR", "贴身衣物", 0, 0, "RETURN_REFUND", 1),
            ("GENERAL", "通用百货", 1, 7, "RETURN_REFUND", 0),
        ],
    )
    for row in ORDERS:
        conn.execute(
            "INSERT INTO orders(order_no,user_id,status,goods_amount,freight_amount,discount_amount,"
            "pay_amount,is_free_shipping,has_excluded_item,paid_at,shipped_at,delivered_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )
    conn.executemany(
        "INSERT INTO order_items(order_id,sku_code,sku_name,category_code,qty,unit_price,pay_amount,"
        "is_virtual,is_customized,is_fresh,is_opened) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ITEMS,
    )
    conn.executemany(
        "INSERT INTO shipments(order_id,shipment_no,carrier_name,tracking_no,status,"
        "shipped_at,estimated_at,signed_at,signed_by) VALUES (?,?,?,?,?,?,?,?,?)",
        SHIPMENTS,
    )
    conn.executemany(
        "INSERT INTO shipment_traces(shipment_id,trace_time,status_code,description,location)"
        " VALUES (?,?,?,?,?)",
        TRACES,
    )
    import json
    conn.execute(
        "INSERT INTO refund_policies(policy_no,name,scope_type,scope_value,rule_json,priority,version,enabled)"
        " VALUES (?,?,?,?,?,?,?,1)",
        ("POL-GLOBAL-01", "全局售后规则", "GLOBAL", "*",
         json.dumps(GLOBAL_RULE, ensure_ascii=False), 100, 3),
    )
    conn.execute(
        "INSERT INTO refund_policies(policy_no,name,scope_type,scope_value,rule_json,priority,version,enabled)"
        " VALUES (?,?,?,?,?,?,?,1)",
        ("POL-FRESH-01", "生鲜特殊规则", "CATEGORY", "FRESH",
         json.dumps({"no_reason_days": 0, "quality_only": True, "auto_approve_limit": 20000},
                    ensure_ascii=False), 200, 1),
    )
    conn.executemany(
        "INSERT INTO knowledge_articles(title,category,content,tags) VALUES (?,?,?,?)",
        [
            ("七天无理由退货规则", "POLICY",
             "签收次日起 7 天内商品完好可申请无理由退货；生鲜、虚拟、定制、贴身衣物不支持。", "退货,7天"),
            ("退款到账时效", "POLICY", "审核通过后原路退回：微信/支付宝 1-3 个工作日，银行卡 3-7 个工作日。", "到账"),
            ("运费由谁承担", "POLICY", "质量问题平台承担运费；无理由退货且原单包邮的，扣回原运费。", "运费"),
        ],
    )
    conn.commit()


def yuan(cents: int) -> str:
    """分 → 元，带两位小数。"""
    return f"{cents / 100:.2f}"
