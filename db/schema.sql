-- =====================================================================
--  AI 客服与售后 Agent · 数据库结构（MySQL 8.0 / InnoDB / utf8mb4）
--  设计原则：
--   1) 业务表（订单/物流/退款/审批）与 Agent 表（会话/消息/工具调用）物理同库、逻辑分层，
--      便于事务内完成"判定 + 落单"，避免跨库分布式事务。
--   2) 所有状态字段用 ENUM + 显式状态机约束（应用层校验，DB 兜底）。
--   3) 所有金额 DECIMAL(12,2)，禁止 FLOAT。
--   4) 所有表带 created_at / updated_at；核心表带 version 乐观锁。
--   5) 关键链路口径统一为 trace_id，便于串联日志、消息、工具调用。
-- =====================================================================

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------
-- 1. 用户
-- ---------------------------------------------------------------------
CREATE TABLE users (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_no         VARCHAR(32)  NOT NULL COMMENT '对外用户编号',
  nickname        VARCHAR(64)  NOT NULL DEFAULT '',
  phone           VARBINARY(255) NOT NULL COMMENT 'AES 加密存储',
  phone_tail4     CHAR(4)      NOT NULL DEFAULT '' COMMENT '尾号4位，用于身份校验（不可逆）',
  member_level    TINYINT      NOT NULL DEFAULT 1 COMMENT '1普通 2银 3金 4黑',
  risk_level      TINYINT      NOT NULL DEFAULT 0 COMMENT '0正常 1关注 2高风险(强制人工)',
  status          TINYINT      NOT NULL DEFAULT 1 COMMENT '1正常 0注销',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_user_no (user_no),
  KEY idx_phone_tail4 (phone_tail4)
) ENGINE=InnoDB COMMENT='用户主表';

CREATE TABLE user_after_sale_stats (
  user_id            BIGINT UNSIGNED NOT NULL,
  stat_month         CHAR(7)      NOT NULL COMMENT '统计月份 YYYY-MM',
  order_cnt_30d      INT           NOT NULL DEFAULT 0,
  refund_cnt_30d     INT           NOT NULL DEFAULT 0,
  refund_amt_30d     DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  refund_rate_30d    DECIMAL(5,4)  NOT NULL DEFAULT 0.0000 COMMENT '退款单数/订单数',
  last_refund_at     DATETIME      NULL,
  updated_at         DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id, stat_month)
) ENGINE=InnoDB COMMENT='用户售后画像（供风控规则 BR-11 使用，离线/准实时刷新）';

-- ---------------------------------------------------------------------
-- 2. 商品类目（决定是否支持无理由退货，BR-08）
-- ---------------------------------------------------------------------
CREATE TABLE categories (
  id                    INT UNSIGNED NOT NULL AUTO_INCREMENT,
  code                  VARCHAR(32)  NOT NULL,
  name                  VARCHAR(64)  NOT NULL,
  support_no_reason     TINYINT      NOT NULL DEFAULT 1 COMMENT '1支持7天无理由 0不支持',
  no_reason_days        SMALLINT     NOT NULL DEFAULT 7,
  return_type_default   ENUM('REFUND_ONLY','RETURN_REFUND') NOT NULL DEFAULT 'RETURN_REFUND',
  need_quality_evidence TINYINT      NOT NULL DEFAULT 0 COMMENT '超期后是否必须凭证',
  created_at            DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_code (code)
) ENGINE=InnoDB COMMENT='商品类目售后属性';

-- ---------------------------------------------------------------------
-- 3. 订单
-- ---------------------------------------------------------------------
CREATE TABLE orders (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_no          VARCHAR(32)   NOT NULL,
  user_id           BIGINT UNSIGNED NOT NULL,
  status            ENUM('CREATED','PAID','SHIPPED','DELIVERED','COMPLETED','CANCELLED','CLOSED')
                    NOT NULL DEFAULT 'CREATED' COMMENT '主状态',
  after_sale_status ENUM('NONE','APPLYING','REFUNDING','RETURNING','REFUNDED','PARTIAL_REFUNDED')
                    NOT NULL DEFAULT 'NONE' COMMENT '售后汇总状态（冗余，便于快查）',
  channel           VARCHAR(16)   NOT NULL DEFAULT 'APP' COMMENT 'APP/MINI/WEB/CS',
  goods_amount      DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  freight_amount    DECIMAL(12,2) NOT NULL DEFAULT 0.00 COMMENT '实付运费',
  discount_amount   DECIMAL(12,2) NOT NULL DEFAULT 0.00 COMMENT '优惠总额（含券/满减/积分）',
  pay_amount        DECIMAL(12,2) NOT NULL DEFAULT 0.00 COMMENT '实付 = 商品 + 运费 - 优惠',
  refunded_amount   DECIMAL(12,2) NOT NULL DEFAULT 0.00 COMMENT '已退金额累计',
  currency          CHAR(3)       NOT NULL DEFAULT 'CNY',
  is_free_shipping  TINYINT       NOT NULL DEFAULT 0,
  has_excluded_item TINYINT       NOT NULL DEFAULT 0 COMMENT '是否含禁退类目（冗余，规则前置）',
  pay_method        VARCHAR(24)   NOT NULL DEFAULT '',
  receiver_name     VARCHAR(32)   NOT NULL DEFAULT '',
  receiver_phone_tail4 CHAR(4)    NOT NULL DEFAULT '' COMMENT '收货人手机尾号，二次校验用',
  receiver_region   VARCHAR(64)   NOT NULL DEFAULT '' COMMENT '省市区，不含详址',
  paid_at           DATETIME      NULL,
  shipped_at        DATETIME      NULL,
  delivered_at      DATETIME      NULL COMMENT '签收时间，BR-05/06 的基准',
  finished_at       DATETIME      NULL,
  created_at        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  version           INT           NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uk_order_no (order_no),
  KEY idx_user_created (user_id, created_at DESC),
  KEY idx_status (status),
  KEY idx_after_sale (after_sale_status)
) ENGINE=InnoDB COMMENT='订单主表';

CREATE TABLE order_items (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id         BIGINT UNSIGNED NOT NULL,
  sku_code         VARCHAR(48)   NOT NULL,
  sku_name         VARCHAR(128)  NOT NULL,
  category_code    VARCHAR(32)   NOT NULL,
  qty              INT           NOT NULL DEFAULT 1,
  unit_price       DECIMAL(12,2) NOT NULL DEFAULT 0.00 COMMENT '下单时单价快照',
  pay_amount       DECIMAL(12,2) NOT NULL DEFAULT 0.00 COMMENT '该行实付（已分摊优惠）',
  refunded_qty     INT           NOT NULL DEFAULT 0,
  refunded_amount  DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  after_sale_status ENUM('NONE','APPLYING','REFUNDING','RETURNING','REFUNDED')
                    NOT NULL DEFAULT 'NONE',
  is_virtual       TINYINT       NOT NULL DEFAULT 0,
  is_customized    TINYINT       NOT NULL DEFAULT 0 COMMENT '定制类，禁退',
  is_fresh         TINYINT       NOT NULL DEFAULT 0 COMMENT '生鲜，禁无理由',
  is_opened        TINYINT       NOT NULL DEFAULT 0 COMMENT '是否已拆封',
  created_at       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_order_sku (order_id, sku_code),
  KEY idx_category (category_code)
) ENGINE=InnoDB COMMENT='订单明细（退款按行计算 BR-14）';

-- ---------------------------------------------------------------------
-- 4. 物流
-- ---------------------------------------------------------------------
CREATE TABLE shipments (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id      BIGINT UNSIGNED NOT NULL,
  shipment_no   VARCHAR(32)  NOT NULL,
  carrier_code  VARCHAR(16)  NOT NULL,
  carrier_name  VARCHAR(32)  NOT NULL,
  tracking_no   VARCHAR(48)  NOT NULL,
  status        ENUM('PENDING','PICKED','IN_TRANSIT','OUT_FOR_DELIVERY','SIGNED',
                     'EXCEPTION','RETURNING','RETURNED','CANCELLED')
                NOT NULL DEFAULT 'PENDING',
  shipped_at    DATETIME     NULL,
  estimated_at  DATETIME     NULL COMMENT '预计送达',
  signed_at     DATETIME     NULL,
  signed_by     VARCHAR(32)  NOT NULL DEFAULT '' COMMENT '本人/代收/快递柜',
  last_sync_at  DATETIME     NULL COMMENT '最近一次与物流商同步时间',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_shipment_no (shipment_no),
  KEY idx_order (order_id),
  KEY idx_tracking (tracking_no),
  KEY idx_status (status)
) ENGINE=InnoDB COMMENT='物流单';

CREATE TABLE shipment_traces (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  shipment_id  BIGINT UNSIGNED NOT NULL,
  trace_time   DATETIME      NOT NULL,
  status_code  VARCHAR(24)   NOT NULL,
  description  VARCHAR(255)  NOT NULL,
  location     VARCHAR(64)   NOT NULL DEFAULT '',
  source       VARCHAR(16)   NOT NULL DEFAULT 'CARRIER' COMMENT 'CARRIER/CACHE/MANUAL',
  created_at   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_shipment_time (shipment_id, trace_time DESC)
) ENGINE=InnoDB COMMENT='物流轨迹';

-- ---------------------------------------------------------------------
-- 5. 退款规则（可配置，带版本与灰度）
-- ---------------------------------------------------------------------
CREATE TABLE refund_policies (
  id             INT UNSIGNED NOT NULL AUTO_INCREMENT,
  policy_no      VARCHAR(32)  NOT NULL,
  name           VARCHAR(64)  NOT NULL,
  scope_type     ENUM('GLOBAL','CATEGORY','MEMBER_LEVEL','CHANNEL') NOT NULL DEFAULT 'GLOBAL',
  scope_value    VARCHAR(64)  NOT NULL DEFAULT '*' COMMENT '如 category_code 或会员等级',
  rule_json      JSON         NOT NULL COMMENT '见 02-数据库设计 附录A 结构',
  priority       INT          NOT NULL DEFAULT 0 COMMENT '数值大者优先',
  version        INT          NOT NULL DEFAULT 1,
  enabled        TINYINT      NOT NULL DEFAULT 1,
  effective_from DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  effective_to   DATETIME     NULL,
  created_by     VARCHAR(32)  NOT NULL DEFAULT 'system',
  created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_policy_version (policy_no, version),
  KEY idx_scope (scope_type, scope_value, priority DESC)
) ENGINE=InnoDB COMMENT='退款规则配置';

-- ---------------------------------------------------------------------
-- 6. 退款单（核心业务实体）
-- ---------------------------------------------------------------------
CREATE TABLE refund_requests (
  id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  refund_no          VARCHAR(32)   NOT NULL,
  order_id           BIGINT UNSIGNED NOT NULL,
  user_id            BIGINT UNSIGNED NOT NULL,
  session_id         BIGINT UNSIGNED NULL COMMENT '来源会话，便于回溯对话',
  refund_type        ENUM('CANCEL','REFUND_ONLY','RETURN_REFUND') NOT NULL,
  reason_code        VARCHAR(32)   NOT NULL COMMENT 'NO_REASON/QUALITY/WRONG_ITEM/DAMAGED/NOT_RECEIVED',
  reason_desc        VARCHAR(255)  NOT NULL DEFAULT '',
  goods_amount       DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  freight_refund     DECIMAL(12,2) NOT NULL DEFAULT 0.00 COMMENT '退还运费',
  freight_deduct     DECIMAL(12,2) NOT NULL DEFAULT 0.00 COMMENT '扣回运费',
  discount_deduct    DECIMAL(12,2) NOT NULL DEFAULT 0.00 COMMENT '优惠分摊扣回',
  apply_amount       DECIMAL(12,2) NOT NULL DEFAULT 0.00 COMMENT '申请金额',
  approved_amount    DECIMAL(12,2) NOT NULL DEFAULT 0.00 COMMENT '审批后金额',
  currency           CHAR(3)       NOT NULL DEFAULT 'CNY',
  status             ENUM('DRAFT','ELIGIBLE','USER_CONFIRMED','PENDING_APPROVAL','APPROVED',
                          'PROCESSING','SUCCESS','FAILED','REJECTED_BY_RULE',
                          'REJECTED_BY_HUMAN','CANCELLED','EXPIRED')
                     NOT NULL DEFAULT 'DRAFT',
  decision_code      VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '规则引擎决策码',
  decision_snapshot  JSON          NULL COMMENT '判定时完整输入输出快照（可解释性）',
  policy_version     INT           NOT NULL DEFAULT 0 COMMENT '判定所用规则版本',
  approval_level     TINYINT       NOT NULL DEFAULT 0 COMMENT '0无需 1主管 2财务',
  refund_seq         INT           NOT NULL DEFAULT 1 COMMENT '同订单第几次退款，幂等键组成',
  evidence_urls      JSON          NULL COMMENT '凭证图片/视频',
  confirm_token_hash CHAR(64)      NULL COMMENT '确认令牌哈希，一次性',
  confirm_expire_at  DATETIME      NULL,
  confirmed_at       DATETIME      NULL,
  submitted_at       DATETIME      NULL,
  finished_at        DATETIME      NULL,
  operator_type      ENUM('AGENT','HUMAN','SYSTEM') NOT NULL DEFAULT 'AGENT',
  fail_reason        VARCHAR(255)  NOT NULL DEFAULT '',
  idempotency_key    VARCHAR(64)   NOT NULL COMMENT '客户端/编排层生成',
  trace_id           VARCHAR(48)   NOT NULL DEFAULT '',
  created_at         DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at         DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  version            INT           NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uk_refund_no (refund_no),
  UNIQUE KEY uk_idem (idempotency_key),
  UNIQUE KEY uk_order_seq (order_id, refund_seq),
  KEY idx_user_status (user_id, status),
  KEY idx_status_created (status, created_at DESC),
  KEY idx_session (session_id),
  KEY idx_trace (trace_id)
) ENGINE=InnoDB COMMENT='退款申请单';

CREATE TABLE refund_items (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  refund_id     BIGINT UNSIGNED NOT NULL,
  order_item_id BIGINT UNSIGNED NOT NULL,
  qty           INT           NOT NULL DEFAULT 1,
  amount        DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  created_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_refund_item (refund_id, order_item_id)
) ENGINE=InnoDB COMMENT='退款明细（部分退）';

-- ---------------------------------------------------------------------
-- 7. 人工审批
-- ---------------------------------------------------------------------
CREATE TABLE approval_tasks (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  task_no        VARCHAR(32)   NOT NULL,
  biz_type       ENUM('REFUND','OVERRIDE') NOT NULL DEFAULT 'REFUND',
  biz_id         BIGINT UNSIGNED NOT NULL COMMENT 'refund_requests.id',
  level          TINYINT       NOT NULL COMMENT '1主管 2财务',
  status         ENUM('PENDING','APPROVED','REJECTED','TRANSFERRED','EXPIRED','CANCELLED')
                 NOT NULL DEFAULT 'PENDING',
  assignee_role  VARCHAR(32)   NOT NULL DEFAULT 'CS_LEADER',
  assignee_id    BIGINT UNSIGNED NULL COMMENT 'NULL 表示池化抢单',
  reason         VARCHAR(255)  NOT NULL DEFAULT '' COMMENT '为什么需要审批',
  decision_comment VARCHAR(255) NOT NULL DEFAULT '',
  decision_payload JSON        NULL,
  sla_deadline   DATETIME      NOT NULL,
  decided_at     DATETIME      NULL,
  decided_by     BIGINT UNSIGNED NULL,
  created_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  version        INT           NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uk_task_no (task_no),
  KEY idx_biz (biz_type, biz_id),
  KEY idx_pending (status, level, sla_deadline)
) ENGINE=InnoDB COMMENT='人工审批任务（L1/L2 分档）';

-- ---------------------------------------------------------------------
-- 8. Agent 会话与消息
-- ---------------------------------------------------------------------
CREATE TABLE agent_sessions (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  session_no     VARCHAR(40)   NOT NULL,
  user_id        BIGINT UNSIGNED NULL,
  channel        VARCHAR(16)   NOT NULL DEFAULT 'APP',
  state          VARCHAR(32)   NOT NULL DEFAULT 'INIT' COMMENT '会话状态机当前态',
  state_payload  JSON          NULL COMMENT '槽位、待确认退款单、上下文快照',
  order_no_ctx   VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '当前锁定订单',
  refund_id      BIGINT UNSIGNED NULL COMMENT '当前进行中的退款单',
  intent         VARCHAR(24)   NOT NULL DEFAULT '',
  turn_count     INT           NOT NULL DEFAULT 0,
  fallback_count INT           NOT NULL DEFAULT 0 COMMENT '连续兜底次数，>=3 转人工',
  human_takeover TINYINT       NOT NULL DEFAULT 0,
  assignee_id    BIGINT UNSIGNED NULL,
  last_msg_at    DATETIME      NULL,
  expires_at     DATETIME      NOT NULL COMMENT '30 分钟无活动过期',
  trace_id       VARCHAR(48)   NOT NULL DEFAULT '',
  created_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  version        INT           NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uk_session_no (session_no),
  KEY idx_user (user_id, created_at DESC),
  KEY idx_state (state),
  KEY idx_active (human_takeover, last_msg_at)
) ENGINE=InnoDB COMMENT='Agent 会话（状态机持久化，支持跨实例续跑）';

CREATE TABLE agent_messages (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  session_id  BIGINT UNSIGNED NOT NULL,
  seq         INT           NOT NULL COMMENT '会话内自增序号',
  role        ENUM('user','assistant','tool','system') NOT NULL,
  content     TEXT          NOT NULL,
  content_type ENUM('text','card','image','file') NOT NULL DEFAULT 'text',
  card_payload JSON         NULL COMMENT '方案卡结构化数据，前端直接渲染',
  tool_name   VARCHAR(48)   NOT NULL DEFAULT '',
  token_in    INT           NOT NULL DEFAULT 0,
  token_out   INT           NOT NULL DEFAULT 0,
  latency_ms  INT           NOT NULL DEFAULT 0,
  trace_id    VARCHAR(48)   NOT NULL DEFAULT '',
  created_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_session_seq (session_id, seq),
  KEY idx_created (created_at)
) ENGINE=InnoDB COMMENT='会话消息流水';

CREATE TABLE agent_tool_calls (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  session_id     BIGINT UNSIGNED NOT NULL,
  message_id     BIGINT UNSIGNED NULL,
  call_id        VARCHAR(48)   NOT NULL COMMENT 'LLM 返回的 tool_call id，用于配对结果',
  tool_name      VARCHAR(48)   NOT NULL,
  args           JSON          NOT NULL,
  result         JSON          NULL,
  status         ENUM('SUCCESS','ERROR','TIMEOUT','DENIED') NOT NULL DEFAULT 'SUCCESS',
  error_code     VARCHAR(48)   NOT NULL DEFAULT '',
  latency_ms     INT           NOT NULL DEFAULT 0,
  retry_count    INT           NOT NULL DEFAULT 0,
  trace_id       VARCHAR(48)   NOT NULL DEFAULT '',
  created_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_session (session_id),
  KEY idx_tool_status (tool_name, status),
  KEY idx_trace (trace_id)
) ENGINE=InnoDB COMMENT='工具调用审计（每次 Function Calling 一行）';

CREATE TABLE agent_state_events (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  session_id  BIGINT UNSIGNED NOT NULL,
  from_state  VARCHAR(32)   NOT NULL,
  to_state    VARCHAR(32)   NOT NULL,
  event       VARCHAR(48)   NOT NULL COMMENT '如 INTENT_OK / ELIGIBLE / USER_CONFIRMED / TOOL_TIMEOUT',
  payload     JSON          NULL,
  operator    ENUM('AGENT','USER','HUMAN','SYSTEM') NOT NULL DEFAULT 'AGENT',
  trace_id    VARCHAR(48)   NOT NULL DEFAULT '',
  created_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_session (session_id, id),
  KEY idx_state (to_state)
) ENGINE=InnoDB COMMENT='会话状态迁移事件（事件溯源，可回放）';

CREATE TABLE service_tickets (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  ticket_no    VARCHAR(32)   NOT NULL,
  session_id   BIGINT UNSIGNED NULL,
  user_id      BIGINT UNSIGNED NULL,
  refund_id    BIGINT UNSIGNED NULL,
  reason       VARCHAR(64)   NOT NULL COMMENT 'USER_REQUEST/RISK/TOOL_FAIL/COMPLAINT/OVER_LIMIT',
  summary      TEXT          NULL COMMENT 'Agent 生成的结构化交接摘要',
  priority     TINYINT       NOT NULL DEFAULT 2 COMMENT '1紧急 2普通 3低',
  status       ENUM('OPEN','ASSIGNED','PROCESSING','RESOLVED','CLOSED') NOT NULL DEFAULT 'OPEN',
  assignee_id  BIGINT UNSIGNED NULL,
  closed_at    DATETIME      NULL,
  created_at   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ticket_no (ticket_no),
  KEY idx_status_priority (status, priority)
) ENGINE=InnoDB COMMENT='转人工工单';

-- ---------------------------------------------------------------------
-- 9. 知识库（FAQ / 政策解释）
-- ---------------------------------------------------------------------
CREATE TABLE knowledge_articles (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  title      VARCHAR(128)  NOT NULL,
  category   VARCHAR(32)   NOT NULL DEFAULT '',
  content    MEDIUMTEXT    NOT NULL,
  tags       VARCHAR(255)  NOT NULL DEFAULT '',
  version    INT           NOT NULL DEFAULT 1,
  enabled    TINYINT       NOT NULL DEFAULT 1,
  updated_at DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_category (category, enabled),
  FULLTEXT KEY ft_content (title, content) WITH PARSER ngram
) ENGINE=InnoDB COMMENT='FAQ 与政策知识库';

-- ---------------------------------------------------------------------
-- 10. 基础设施表
-- ---------------------------------------------------------------------
CREATE TABLE idempotency_keys (
  idem_key      VARCHAR(64)  NOT NULL,
  scope         VARCHAR(48)  NOT NULL COMMENT '如 refund.confirm',
  request_hash  CHAR(64)     NOT NULL,
  response_body JSON         NULL,
  status        ENUM('PROCESSING','DONE','FAILED') NOT NULL DEFAULT 'PROCESSING',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at    DATETIME     NOT NULL,
  PRIMARY KEY (idem_key)
) ENGINE=InnoDB COMMENT='接口幂等表';

CREATE TABLE outbox_events (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  aggregate_type VARCHAR(32)   NOT NULL,
  aggregate_id   BIGINT UNSIGNED NOT NULL,
  event_type     VARCHAR(48)   NOT NULL COMMENT 'REFUND_APPROVED/REFUND_SUCCEEDED/...',
  payload        JSON          NOT NULL,
  status         ENUM('NEW','SENT','FAILED') NOT NULL DEFAULT 'NEW',
  retry_count    INT           NOT NULL DEFAULT 0,
  next_retry_at  DATETIME      NULL,
  created_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_dispatch (status, next_retry_at)
) ENGINE=InnoDB COMMENT='事务性发件箱（最终一致性）';

CREATE TABLE audit_logs (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  trace_id    VARCHAR(48)   NOT NULL DEFAULT '',
  actor_type  ENUM('USER','AGENT','HUMAN','SYSTEM') NOT NULL,
  actor_id    VARCHAR(48)   NOT NULL DEFAULT '',
  action      VARCHAR(64)   NOT NULL COMMENT 'REFUND_CONFIRM/APPROVAL_APPROVE/RULE_UPDATE',
  biz_type    VARCHAR(32)   NOT NULL DEFAULT '',
  biz_id      VARCHAR(48)   NOT NULL DEFAULT '',
  before_data JSON          NULL,
  after_data  JSON          NULL,
  ip          VARCHAR(45)   NOT NULL DEFAULT '',
  user_agent  VARCHAR(255)  NOT NULL DEFAULT '',
  created_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_biz (biz_type, biz_id),
  KEY idx_trace (trace_id),
  KEY idx_created (created_at)
) ENGINE=InnoDB COMMENT='操作审计（退款留痕 ≥ 3 年）';
