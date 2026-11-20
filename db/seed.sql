-- =====================================================================
--  演示数据：5 个订单覆盖 5 种判定结果
--    ORD20261101001  已签收3天 / 服饰 / 299元      → OK_AUTO
--    ORD20261101002  已签收12天 / 鞋 / 459元       → REJECT_EXPIRED
--    ORD20261101003  已发货未签收 / 小家电 / 1880  → OK_NEED_APPROVAL_L1
--    ORD20261101004  已签收1天 / 生鲜 / 128元      → REJECT_CATEGORY
--    ORD20261101005  已签收2天 / 手机 / 3200元     → OK_NEED_APPROVAL_L2
-- =====================================================================
SET NAMES utf8mb4;

INSERT INTO categories (code, name, support_no_reason, no_reason_days, return_type_default, need_quality_evidence) VALUES
('APPAREL',  '服饰鞋包', 1, 7,  'RETURN_REFUND', 0),
('DIGITAL',  '数码家电', 1, 7,  'RETURN_REFUND', 0),
('FRESH',    '生鲜食品', 0, 0,  'REFUND_ONLY',   1),
('VIRTUAL',  '虚拟商品', 0, 0,  'REFUND_ONLY',   0),
('CUSTOM',   '定制商品', 0, 0,  'RETURN_REFUND', 1),
('UNDERWEAR','贴身衣物', 0, 0,  'RETURN_REFUND', 1),
('GENERAL',  '通用百货', 1, 7,  'RETURN_REFUND', 0);

INSERT INTO users (user_no, nickname, phone, phone_tail4, member_level, risk_level) VALUES
('U1001', '小林', 0x00, '6601', 3, 0),
('U1002', '阿哲', 0x00, '8820', 2, 0),
('U1003', 'Vivian', 0x00, '3312', 4, 0);

INSERT INTO user_after_sale_stats (user_id, stat_month, order_cnt_30d, refund_cnt_30d, refund_amt_30d, refund_rate_30d) VALUES
(1, '2026-11', 8, 1, 199.00, 0.1250),
(2, '2026-11', 6, 0, 0.00,   0.0000),
(3, '2026-11', 12, 7, 1580.00, 0.5833);

-- 规则：全局版本3（演示"版本化判定"）
INSERT INTO refund_policies (policy_no, name, scope_type, scope_value, rule_json, priority, version, enabled) VALUES
('POL-GLOBAL-01', '全局售后规则', 'GLOBAL', '*', JSON_OBJECT(
  'no_reason_days', 7,
  'max_refund_times', 2,
  'auto_approve_limit', 500,
  'l1_limit', 2000,
  'freight_deduct_on_no_reason', true,
  'refund_rate_threshold', 0.60,
  'excluded_categories', JSON_ARRAY('VIRTUAL','CUSTOM','FRESH','UNDERWEAR'),
  'forbid_status', JSON_ARRAY('CREATED','CANCELLED','CLOSED'),
  'sla_hours', JSON_OBJECT('L1', 4, 'L2', 8)
), 100, 3, 1),
('POL-FRESH-01', '生鲜特殊规则', 'CATEGORY', 'FRESH', JSON_OBJECT(
  'no_reason_days', 0,
  'quality_only', true,
  'auto_approve_limit', 200
), 200, 1, 1);

INSERT INTO orders
(order_no, user_id, status, after_sale_status, channel, goods_amount, freight_amount, discount_amount,
 pay_amount, refunded_amount, is_free_shipping, has_excluded_item, pay_method,
 receiver_name, receiver_phone_tail4, receiver_region, paid_at, shipped_at, delivered_at, finished_at) VALUES
('ORD20261101001', 1, 'DELIVERED', 'NONE', 'APP', 329.00, 0.00, 30.00, 299.00, 0.00, 1, 0, 'WECHAT',
 '林*', '6601', '广东省深圳市南山区', '2026-11-10 10:12:00', '2026-11-11 09:30:00', '2026-11-14 16:20:00', NULL),
('ORD20261101002', 1, 'COMPLETED', 'NONE', 'APP', 499.00, 0.00, 40.00, 459.00, 0.00, 1, 0, 'ALIPAY',
 '林*', '6601', '广东省深圳市南山区', '2026-10-26 20:05:00', '2026-10-27 11:00:00', '2026-11-05 14:10:00', '2026-11-12 00:00:00'),
('ORD20261101003', 2, 'SHIPPED', 'NONE', 'MINI', 1880.00, 0.00, 0.00, 1880.00, 0.00, 1, 0, 'WECHAT',
 '陈*', '8820', '浙江省杭州市余杭区', '2026-11-15 21:40:00', '2026-11-16 10:15:00', NULL, NULL),
('ORD20261101004', 3, 'DELIVERED', 'NONE', 'APP', 128.00, 12.00, 0.00, 140.00, 0.00, 0, 1, 'WECHAT',
 '王*', '3312', '上海市浦东新区', '2026-11-15 08:00:00', '2026-11-15 15:00:00', '2026-11-16 09:30:00', NULL),
('ORD20261101005', 2, 'DELIVERED', 'NONE', 'APP', 3200.00, 0.00, 0.00, 3200.00, 0.00, 1, 0, 'ALIPAY',
 '陈*', '8820', '浙江省杭州市余杭区', '2026-11-14 12:00:00', '2026-11-14 18:00:00', '2026-11-15 11:00:00', NULL);

INSERT INTO order_items (order_id, sku_code, sku_name, category_code, qty, unit_price, pay_amount, is_virtual, is_customized, is_fresh, is_opened) VALUES
(1, 'SKU-A1001', '纯棉针织开衫 米白 M', 'APPAREL', 1, 329.00, 299.00, 0, 0, 0, 0),
(2, 'SKU-B2002', '轻量跑鞋 42 灰',     'APPAREL', 1, 499.00, 459.00, 0, 0, 0, 1),
(3, 'SKU-C3003', '无线吸尘器 V8',      'DIGITAL', 1, 1880.00, 1880.00, 0, 0, 0, 0),
(4, 'SKU-D4004', '智利车厘子 2斤装',   'FRESH',   1, 128.00, 128.00, 0, 0, 1, 0),
(5, 'SKU-E5005', '智能手机 Pro 256G',  'DIGITAL', 1, 3200.00, 3200.00, 0, 0, 0, 1);

INSERT INTO shipments (order_id, shipment_no, carrier_code, carrier_name, tracking_no, status, shipped_at, estimated_at, signed_at, signed_by, last_sync_at) VALUES
(1, 'SHP0001', 'SF', '顺丰速运', 'SF1388200111', 'SIGNED', '2026-11-11 09:30:00', '2026-11-14 18:00:00', '2026-11-14 16:20:00', '本人', '2026-11-17 10:00:00'),
(2, 'SHP0002', 'ZTO', '中通快递', 'ZT7733200222', 'SIGNED', '2026-10-27 11:00:00', '2026-11-05 18:00:00', '2026-11-05 14:10:00', '快递柜', '2026-11-17 10:00:00'),
(3, 'SHP0003', 'JD', '京东物流', 'JD9900300333', 'IN_TRANSIT', '2026-11-16 10:15:00', '2026-11-18 20:00:00', NULL, '', '2026-11-17 16:00:00'),
(4, 'SHP0004', 'SF', '顺丰速运', 'SF1388200444', 'SIGNED', '2026-11-15 15:00:00', '2026-11-16 12:00:00', '2026-11-16 09:30:00', '本人', '2026-11-17 10:00:00'),
(5, 'SHP0005', 'SF', '顺丰速运', 'SF1388200555', 'SIGNED', '2026-11-14 18:00:00', '2026-11-15 12:00:00', '2026-11-15 11:00:00', '本人', '2026-11-17 10:00:00');

INSERT INTO shipment_traces (shipment_id, trace_time, status_code, description, location) VALUES
(1, '2026-11-11 09:30:00', 'PICKED', '顺丰速运已收取快件', '深圳市'),
(1, '2026-11-12 22:10:00', 'IN_TRANSIT', '快件已到达深圳南山集散点', '深圳市'),
(1, '2026-11-14 08:40:00', 'OUT_FOR_DELIVERY', '快件正在派送途中', '深圳市'),
(1, '2026-11-14 16:20:00', 'SIGNED', '快件已签收，签收人：本人', '深圳市'),
(3, '2026-11-16 10:15:00', 'PICKED', '京东物流已揽收', '杭州市'),
(3, '2026-11-17 07:20:00', 'IN_TRANSIT', '快件已到达杭州转运中心', '杭州市'),
(3, '2026-11-17 15:50:00', 'IN_TRANSIT', '快件已发往上海分拨中心', '杭州市');

INSERT INTO knowledge_articles (title, category, content, tags) VALUES
('七天无理由退货规则', 'POLICY', '签收次日起 7 天内，商品完好可申请无理由退货；生鲜、虚拟、定制、贴身衣物及已拆封个护不支持。', '退货,7天,无理由'),
('退款到账时效', 'POLICY', '审核通过后原路退回：微信/支付宝 1-3 个工作日，银行卡 3-7 个工作日。', '到账,时效'),
('运费由谁承担', 'POLICY', '质量问题由平台承担往返运费；无理由退货且原单包邮的，退款时扣回原运费。', '运费,包邮');
