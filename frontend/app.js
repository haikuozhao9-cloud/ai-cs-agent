"use strict";

const workspace = document.querySelector("#workspace");
const dialog = document.querySelector("#dialog");
const toast = document.querySelector("#toast");
const demoAccounts = [
  ["U1001", "customer1001", "小林", "待发货取消 · 退货核验"],
  ["U1002", "customer1002", "阿哲", "主管与财务审批"],
  ["U1003", "customer1003", "Vivian", "生鲜类目拦截"],
  ["leader", "leader-demo", "客服主管", "L1 · 退货核验 · 人工工单"],
  ["finance", "finance-demo", "财务审批", "L2 退款审批"],
  ["quality", "quality-demo", "质量运营", "评测 · 影子对比 · 授权"],
];
const pageNames = {customer: "客户会话", staff: "审批工作台", quality: "质量评测"};
const states = {
  PAID: "待发货", SHIPPED: "运输中", DELIVERED: "已签收", COMPLETED: "已完成",
  PROPOSAL_PENDING: "待确认", PENDING_APPROVAL: "待审批", PENDING_RETURN: "待退货核验",
  REJECTED_BY_HUMAN: "审批驳回", CANCELLED: "已取消", ESCALATED: "已转人工",
  SUCCESS: "模拟退款成功", EXPIRED: "方案失效", DRAFT: "方案草稿",
  USER_CONFIRMED: "用户确认", APPROVAL_PASSED: "审批通过", APPROVED: "可执行",
  RETURN_VERIFIED: "退回核验通过", PROCESSING: "模拟清结算处理中", OPEN: "待接入", ASSIGNED: "已接入",
  INIT: "新会话", COLLECTING_SLOT: "待补充信息",
};
const actions = {
  ORDER: "查订单", LOGISTICS: "查物流", FAQ: "政策咨询", FALLBACK: "澄清或回退", ASK_SLOT: "索要订单号",
  ERROR: "查询失败", HANDOFF: "转人工", NEED_EVIDENCE: "补凭证", PROPOSAL: "生成方案",
  REJECT: "规则拒绝", REASK: "提示操作方案卡", WAIT_APPROVAL: "进入审批", WAIT_RETURN: "等待退回",
  WAITING: "查看审核进度", CANCEL: "取消申请", DUPLICATE: "幂等返回", HUMAN_WAIT: "等待人工",
};
const fieldNames = {
  action: "动作", amount_cents: "金额", approval_level: "审批级别", refund_type: "退款类型", return_gate: "退回核验",
  decision_code: "规则码", intent: "意图", formal: "正式单数", payouts: "执行数",
  order_no: "提取订单号", reason_code: "提取退款原因",
};
let person;
let mode;
let nluVersion;
let page;
let currentSession;
let sessionList = [];
let chatBusy = false;
let readGeneration = 0;
let toastTimer;

function el(tag, classes, ...children) {
  const element = document.createElement(tag);
  if (classes) element.className = classes;
  for (const child of children.flat()) {
    if (child !== null && child !== undefined) element.append(child);
  }
  return element;
}

function button(label, handler, classes = "", disabled = false) {
  const control = el("button", `button ${classes}`, label);
  control.type = "button";
  control.disabled = disabled;
  control.addEventListener("click", async () => {
    control.disabled = true;
    try {
      await handler();
    } catch (error) {
      notice(error.message, true);
    } finally {
      control.disabled = disabled;
    }
  });
  return control;
}

function notice(text, error = false) {
  toast.textContent = text;
  toast.classList.toggle("error", error);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 4500);
}

function badge(text, tone = "") {
  return el("span", `badge ${tone}`, text);
}

function statusName(value) {
  return states[value] || actions[value] || value || "—";
}

function amount(cents) {
  return `¥${(cents / 100).toFixed(2)}`;
}

async function api(path, body, token = person?.token) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body) headers["Content-Type"] = "application/json";
  const response = await fetch(`/api/v1${path}`, {
    method: body ? "POST" : "GET",
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  const value = await response.json();
  if (!value.ok) throw new Error(value.error.message);
  return value.data;
}

function openDialog(title, ...content) {
  document.querySelector("#dialog-title").textContent = title;
  document.querySelector("#dialog-body").replaceChildren(...content);
  if (!dialog.open) dialog.showModal();
}

async function login(account, password) {
  person = await api("/login", {account, password}, null);
  sessionStorage.setItem("demo-account", account);
  page = person.role === "customer" ? "customer" : person.role === "quality" ? "quality" : "staff";
  currentSession = null;
  sessionList = [];
  if (dialog.open) dialog.close();
  await refresh(true);
}

function chooseRole(target = null) {
  const list = demoAccounts.filter(([account]) => !target
    || (target === "customer" && account.startsWith("U"))
    || (target === "quality" && account === "quality")
    || (target === "staff" && ["leader", "finance"].includes(account)));
  openDialog("切换演示身份", el("p", "dialog-text", "选择账号进入对应工作区。"),
    el("div", "roles", list.map(([account, password, name, description]) => {
      const control = button(name, () => login(account, password));
      control.append(el("small", "", description));
      return control;
    })));
}

function syncHeader() {
  document.querySelector("#page-name").textContent = pageNames[page];
  document.querySelector("#identity-name").textContent = person.name;
  document.querySelector(".identity .avatar").textContent =
    {customer: "客", leader: "主", finance: "财", quality: "质"}[person.role];
  const modeBadge = document.querySelector("#mode");
  modeBadge.textContent = mode === "DELEGATED" ? "模拟执行已授权" : "影子观察中";
  modeBadge.className = mode === "DELEGATED" ? "badge light" : "badge warm";
  for (const control of document.querySelectorAll("[data-page]")) {
    if (control.dataset.page === page) control.setAttribute("aria-current", "page");
    else control.removeAttribute("aria-current");
  }
}

function hero(kicker, title, description, ...controls) {
  return el("section", "hero", el("div", "", el("div", "hero-kicker", kicker), el("h1", "", title),
    el("p", "", description)), controls.length ? el("div", "hero-actions", controls) : null);
}

function panel(title, subtitle, body, control) {
  return el("section", "panel", el("header", "panel-head", el("div", "", el("h2", "", title),
    subtitle ? el("small", "", subtitle) : null), control),
  el("div", "panel-body", body));
}

async function refresh(useLast = false) {
  const generation = ++readGeneration;
  const thisPage = page;
  const config = await api("/config");
  if (generation !== readGeneration) return;
  mode = config.mode;
  nluVersion = config.nlu_mode === "MODEL" ? config.nlu_version : null;
  syncHeader();
  if (thisPage === "customer") {
    const [orders, refunds, sessions] = await Promise.all([api("/orders"), api("/refunds"), api("/agent/sessions")]);
    if (generation !== readGeneration) return;
    sessionList = sessions.items;
    const sessionNo = useLast ? sessionList[0]?.session_no : currentSession?.session_no;
    const session = sessionNo ? await api(`/agent/sessions/${sessionNo}`) : null;
    if (generation !== readGeneration) return;
    currentSession = session;
    renderCustomer(orders.items, refunds.items);
  } else if (thisPage === "staff") {
    const lead = person.role === "leader";
    const [tasks, returns, tickets] = await Promise.all([
      api("/approvals"), lead ? api("/returns") : {items: []}, lead ? api("/tickets") : {items: []},
    ]);
    if (generation !== readGeneration) return;
    renderStaff(tasks.items, returns.items, tickets.items, lead);
  } else {
    const [report, authority, observations] = await Promise.all([
      api("/quality/report"), api("/quality/authority"), api("/quality/shadow"),
    ]);
    if (generation !== readGeneration) return;
    renderQuality(report, authority, observations.items);
  }
}

async function sendText(text, path = "/agent/chat") {
  if (chatBusy || (!text && path === "/agent/chat")) return;
  chatBusy = true;
  const owner = person.account;
  const body = currentSession ? {session_no: currentSession.session_no} : {};
  if (text) body.text = text;
  try {
    const result = await api(path, body);
    if (page !== "customer" || person.account !== owner) return;
    currentSession = result.session;
    await refresh();
    document.querySelector("#chat-text")?.focus();
  } finally {
    chatBusy = false;
  }
}

async function actOnProposal(action) {
  const owner = person.account;
  const body = {session_no: currentSession.session_no, ...currentSession.active_proposal, action};
  const result = await api("/agent/confirm", body);
  if (page !== "customer" || person.account !== owner) return;
  currentSession = result.session;
  await refresh();
}

function proposalCard(card) {
  const details = card.amount_detail;
  const active = currentSession?.active_proposal?.refund_no === card.refund_no;
  const label = card.approval_level > 0 ? `L${card.approval_level} 审批` : "免金额审批";
  const fields = el("dl", "",
    el("dt", "muted", "金额明细"),
    el("dd", "", `商品 ¥${details.goods} · 退运费 ¥${details.freight_refund} · 扣回 ¥${details.freight_deduct}`),
    el("dt", "muted", "商品处理"),
    el("dd", "", card.return_method, ...(card.need_user_action?.length ? [el("br"), card.need_user_action.join("、")] : [])),
    el("dt", "muted", "原支付方式"),
    el("dd", "", "演练执行后记录模拟退款流水"),
  );
  return el("div", "proposal",
    el("div", "row-top", el("h3", "", card.refund_type_text), badge(label)),
    el("p", "amount", `¥${details.total}`), fields,
    active ? el("small", "mono", `有效至 ${card.expire_at}`) : el("small", "", "历史方案"),
    active ? el("div", "quick-links",
      button("确认退款", () => actOnProposal("CONFIRM"), "primary", mode !== "DELEGATED"),
      button("取消方案", () => actOnProposal("CANCEL"))) : null,
  );
}

function renderMessage(message) {
  const role = {user: "你", assistant: "售后助手", staff: "人工客服"}[message.role] || message.role;
  const card = message.card;
  return el("div", `message ${message.role}`, el("small", "", role),
    el("div", "bubble", message.content),
    card?.type === "refund_proposal" ? proposalCard(card) : null,
    card?.type === "refund_status" ? button("查看退款进度", () => showRefund(card.refund_no)) : null);
}

function renderCustomer(orders, refunds) {
  const history = el("select", "session-select");
  history.setAttribute("aria-label", "会话历史");
  history.add(new Option("新会话", ""));
  for (const [index, session] of sessionList.entries()) {
    history.add(new Option(`会话 ${sessionList.length - index} · ${statusName(session.state)}`, session.session_no));
  }
  history.value = currentSession?.session_no || "";
  history.addEventListener("change", async () => {
    try {
      currentSession = history.value ? await api(`/agent/sessions/${history.value}`) : null;
      renderCustomer(orders, refunds);
    } catch (error) {
      notice(error.message, true);
    }
  });
  const first = {role: "assistant", content: "你好，我可以查订单、查物流，也可以帮你核对是否符合退款条件。"};
  const messages = currentSession?.messages.length ? currentSession.messages : [first];
  const stream = el("div", "chat-scroll", messages.map(renderMessage));
  stream.setAttribute("aria-label", "会话消息");
  const compose = el("textarea");
  compose.id = "chat-text";
  compose.placeholder = "输入订单号或描述你的售后诉求";
  compose.setAttribute("aria-label", compose.placeholder);
  compose.maxLength = 1000;
  compose.rows = 1;
  const form = el("form", "composer", compose, el("button", "button primary", "发送"));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await sendText(compose.value.trim()); }
    catch (error) { notice(error.message, true); }
  });
  compose.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  const chat = el("section", "panel chat",
    el("header", "panel-head", el("div", "", el("h2", "", "售后会话"),
      el("small", "", currentSession ? statusName(currentSession.state) : "等待你的消息")),
    el("div", "inline", history, button("转人工", () => sendText(null, "/agent/handoff")))),
    stream, form);
  workspace.replaceChildren(
    hero(nluVersion ? `模型识别 · ${nluVersion}` : "客户工作区", "订单售后", mode === "DELEGATED"
      ? "核对方案后由你确认提交，可在右侧查看退款进度。"
      : "你可以查询订单或转人工。当前退款建议仅用于影子观察。",
    button("新会话", () => {
      currentSession = null;
      renderCustomer(orders, refunds);
      document.querySelector("#chat-text").focus();
    }, "accent"),
    button("刷新进度", refresh)),
    el("div", "grid", chat, el("div", "stack",
      panel("我的订单", `${orders.length} 笔演示订单`, orders.map((order) => el("article", "order",
        el("div", "row-top", el("h3", "", order.title), badge(statusName(order.status))),
        el("small", "order-id mono", `${order.order_no} · ¥${order.amount}`),
        el("div", "quick-links",
          button("查订单", () => sendText(`查询订单 ${order.order_no}`)),
          button("查物流", () => sendText(`查询物流 ${order.order_no}`)),
          button("申请退款", () => sendText(`申请退款 ${order.order_no}`)))))),
      panel("退款进度", null, refunds.length ? refunds.map((refund) => el("article", "refund",
        el("div", "row-top", el("strong", "", `¥${refund.amount}`), badge(statusName(refund.status))),
        button(refund.refund_no, () => showRefund(refund.refund_no), "quiet mono")))
        : el("p", "empty", "你确认提交后，退款单会显示在这里。")),
      currentSession ? panel("会话编号", null, el("small", "mono", currentSession.trace_id)) : null)));
  stream.scrollTop = stream.scrollHeight;
}

async function showRefund(refundNo) {
  const refund = await api(`/refunds/${refundNo}`);
  openDialog("退款进度", el("p", "mono", refundNo),
    el("div", "row-top", el("strong", "", `¥${refund.amount}`), badge(statusName(refund.status))),
    el("ol", "timeline", refund.events.map((event) => el("li", "", statusName(event.status),
      el("small", "mono", `  ${event.created_at}`)))));
}

function renderStaff(tasks, returns, tickets, lead) {
  const paused = mode !== "DELEGATED";
  const taskNodes = tasks.map((task) => {
    const comment = el("textarea");
    comment.rows = 2;
    comment.placeholder = "审批意见，驳回时必填";
    comment.setAttribute("aria-label", comment.placeholder);
    async function decide(decision) {
      if (decision === "REJECT" && !comment.value.trim()) throw new Error("请填写驳回原因");
      await api(`/approvals/${task.task_no}/decision`, {decision, comment: comment.value.trim()});
      notice(decision === "REJECT" ? "已驳回并同步会话" : "审批结果已同步会话");
      await refresh();
    }
    return el("article", "work-item", el("div", "row-top", el("h3", "", `¥${task.amount}`),
      badge(`L${task.level} 待审批`, "warm")), el("small", "mono", `${task.task_no} · ${task.order_no}`),
    el("p", "description", task.reason), el("small", "", `处理时限 ${task.sla_deadline}`), comment,
    el("div", "inline", button("通过审批", () => decide("APPROVE"), "primary", paused),
      button("驳回", () => decide("REJECT"), "danger", paused)));
  });
  const returnNodes = returns.map((refund) => {
    const evidence = el("select");
    evidence.setAttribute("aria-label", "回执类型");
    evidence.add(new Option("选择模拟回执", ""));
    evidence.add(new Option("仓库已验收退货", "WAREHOUSE_RECEIVED"));
    evidence.add(new Option("物流拦截已确认", "CARRIER_INTERCEPTED"));
    return el("article", "work-item", el("div", "row-top", el("h3", "", amount(refund.apply_amount)),
      badge("待核验", "warm")), el("small", "mono", `${refund.refund_no} · ${refund.order_no}`),
    el("div", "inline", evidence, button("登记回执并执行", async () => {
      if (!evidence.value) throw new Error("请选择已核验的回执类型");
      await api(`/returns/${refund.refund_no}/verify`, {evidence: evidence.value});
      notice("退回已核验，模拟退款完成");
      await refresh();
    }, "primary", paused)));
  });
  const ticketNodes = tickets.map((ticket) => {
    const reply = el("textarea");
    reply.rows = 2;
    reply.placeholder = "输入回复，客户可在会话中查看";
    reply.setAttribute("aria-label", reply.placeholder);
    return el("article", "work-item",
      el("div", "row-top", el("h3", "mono", ticket.ticket_no), badge(statusName(ticket.status))),
      el("p", "description", ticket.summary), reply,
      button("回复客户", async () => {
        if (!reply.value.trim()) throw new Error("请填写回复");
        await api(`/tickets/${ticket.ticket_no}/reply`, {text: reply.value.trim()});
        notice("人工回复已发到会话");
        await refresh();
      }));
  });
  workspace.replaceChildren(
    hero(lead ? "客服主管 · L1" : "财务审批 · L2", "审批工作台",
      paused ? "当前执行权限已暂停，仍可查看待办。" : "审核客户已确认的申请，审批结论同步到客户会话。",
      button("刷新待办", refresh, "accent")),
    el("div", "grid", panel("待办审批", `${tasks.length} 条待处理`,
      taskNodes.length ? taskNodes : el("p", "empty", "当前没有待审批的退款。")),
    lead ? el("div", "stack",
      panel("退回核验", "登记仓库验收或物流拦截的模拟回执",
        returnNodes.length ? returnNodes : el("p", "empty", "暂无等待退回核验的退款。")),
      panel("人工接管", null, ticketNodes.length ? ticketNodes : el("p", "empty", "暂无人工工单。"))) : null));
}

function formatDecision(decision) {
  const content = [el("strong", "", statusName(decision.action))];
  if (decision.amount_cents !== null && decision.amount_cents !== undefined) content.push(amount(decision.amount_cents));
  if (decision.approval_level !== null && decision.approval_level !== undefined) {
    content.push(decision.approval_level ? `L${decision.approval_level} 审批` : "免金额审批");
  }
  if (decision.refund_type) content.push({CANCEL: "取消订单", RETURN_REFUND: "退货退款", REFUND_ONLY: "仅退款"}[decision.refund_type]);
  if (decision.return_gate) content.push("需退回核验");
  if (decision.decision_code) content.push(el("span", "mono", decision.decision_code));
  if (decision.nlu_source) content.push({MODEL: "模型识别", RULES: "规则式识别", FALLBACK: "规则式降级"}[decision.nlu_source]);
  return content.map((value) => el("div", "", value));
}

function reviewObservation(entry) {
  const facts = entry.order_snapshot;
  const correcting = Boolean(entry.baseline);
  const action = el("select");
  action.setAttribute("aria-label", "人工动作");
  for (const value of ["PROPOSAL", "REJECT", "HANDOFF", "NEED_EVIDENCE"]) {
    action.add(new Option(statusName(value), value));
  }
  const cents = el("input");
  cents.type = "number";
  cents.min = 0.01;
  cents.max = 1000000;
  cents.step = 0.01;
  cents.placeholder = "退款金额（元）";
  cents.setAttribute("aria-label", cents.placeholder);
  const approval = el("select");
  approval.setAttribute("aria-label", "人工审批级别");
  for (const [level, name] of ["免审批", "L1 主管", "L2 财务"].entries()) approval.add(new Option(name, level));
  const type = el("select");
  type.setAttribute("aria-label", "人工退款类型");
  type.add(new Option("取消订单", "CANCEL"));
  type.add(new Option("退货退款", "RETURN_REFUND"));
  type.add(new Option("仅退款", "REFUND_ONLY"));
  const refundFields = el("div", "baseline-fields", cents, approval, type);
  action.addEventListener("change", () => { refundFields.hidden = action.value !== "PROPOSAL"; });
  const note = el("textarea");
  note.placeholder = "填写判断依据";
  note.setAttribute("aria-label", note.placeholder);
  note.rows = 2;
  const changeReason = el("textarea");
  changeReason.placeholder = "说明更正依据，例如金额单位误填或订单状态看错";
  changeReason.setAttribute("aria-label", "更正原因");
  changeReason.rows = 2;
  if (correcting) {
    action.value = entry.baseline.action;
    cents.value = entry.baseline.amount_cents === undefined ? "" : (entry.baseline.amount_cents / 100).toFixed(2);
    approval.value = entry.baseline.approval_level ?? 0;
    type.value = entry.baseline.refund_type ?? "CANCEL";
    note.value = entry.note;
    refundFields.hidden = action.value !== "PROPOSAL";
  }
  openDialog(correcting ? "复核并更正人工基线" : "登记人工基线",
    el("small", "mono", `${entry.order_no} · ${entry.reason_code}`),
    el("p", "dialog-text", correcting
      ? "请依据采样时的订单快照复核。保存后重新对比，原始判断及更正原因保留在历史记录中。"
      : "请核对同一笔订单和售后原因。保存人工决定后，工作台会显示候选结果。"),
    el("div", "proposal",
      el("strong", "", `采样时${statusName(facts.status)} · 实付 ${amount(facts.pay_amount)}`),
      el("p", "", `已退 ${amount(facts.refunded_amount)} · 类目 ${facts.categories}`),
      el("p", "", `订单节点 ${facts.delivered_at || facts.shipped_at || facts.paid_at}`),
      el("p", "", `风控等级 ${facts.risk_level} · 30 天退款率 ${(facts.refund_rate_30d * 100).toFixed(1)}%`)),
    el("small", "", "人工动作"), action,
    el("small", "", "方案金额填写元，例如 299.00。已签收商品应核对退货退款类型。"),
    refundFields, note, correcting ? changeReason : null,
    button(correcting ? "保存更正并重新对比" : "保存并对比", async () => {
      if (!note.value.trim()) throw new Error("请填写判断依据");
      const body = {action: action.value, note: note.value.trim()};
      if (action.value === "PROPOSAL") {
        if (!/^\d+(\.\d{1,2})?$/.test(cents.value) || !cents.checkValidity()) {
          throw new Error("请输入元金额，最多两位小数，例如 299.00");
        }
        const [whole, fraction = ""] = cents.value.split(".");
        const amountCents = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
        Object.assign(body, {amount_cents: amountCents, approval_level: Number(approval.value), refund_type: type.value});
      }
      if (correcting) {
        if (!changeReason.value.trim()) throw new Error("请填写更正原因");
        Object.assign(body, {expected_revision: entry.baseline_revision, change_reason: changeReason.value.trim()});
      }
      await api(`/quality/shadow/${entry.id}/${correcting ? "review" : "baseline"}`, body);
      dialog.close();
      await refresh();
    }, "primary"));
}

function observationRow(entry) {
  const label = {MATCH: "一致", REVIEW: "待复核", DANGEROUS: "危险分歧"}[entry.outcome];
  return el("div", "shadow-block",
    el("div", "row-top", el("h3", "mono", `#${entry.id} · ${entry.order_no}`),
      entry.baseline ? badge(label, entry.outcome === "DANGEROUS" ? "negative" : "") : badge("待登记", "warm")),
    el("small", "mono", `${entry.reason_code} · ${entry.created_at}`),
    entry.baseline ? el("div", "shadow-columns",
      el("div", "", el("small", "", "候选系统"), formatDecision(entry.candidate)),
      el("div", "", el("small", "", "人工基线"), formatDecision(entry.baseline))) :
      el("div", "quick-links", button("登记人工基线", () => reviewObservation(entry))),
    entry.note ? el("small", "", entry.note) : null,
    entry.baseline ? el("div", "quick-links", button("复核并更正", () => reviewObservation(entry))) : null,
    entry.history?.length > 1 ? el("details", "case", el("summary", "", `复核历史 · ${entry.history.length} 次`),
      entry.history.map((review) => el("div", "work-item",
        el("small", "", `第 ${review.revision} 版 · ${review.created_at} · ${review.reviewer}`),
        formatDecision(review.baseline), el("p", "", review.note),
        review.change_reason ? el("small", "", `更正原因　${review.change_reason}`) : null))) : null);
}

function renderQuality(report, authority, observations) {
  const delegated = authority.mode === "DELEGATED";
  const authorize = button(delegated ? "撤回模拟授权" : "授权模拟退款", () => {
    if (!delegated && (!authority.gate_passed || authority.live_dangerous > 0)) {
      openDialog("暂时无法授权模拟退款",
        !authority.gate_passed ? el("p", "dialog-text", "当前候选尚未通过评测门槛，请查看规则与模型对比中的未通过原因。") : null,
        authority.live_dangerous > 0 ? el("p", "dialog-text",
          `当前有 ${authority.live_dangerous} 条实时影子记录存在危险分歧。请逐条核对人工基线，填写更正依据后重新对比。`) : null,
        ...(authority.blockers || []).map((blocker) => {
          const entry = observations.find((item) => item.id === blocker.id);
          return entry ? button(`复核样本 #${blocker.id} · ${blocker.order_no}`, () => reviewObservation(entry))
            : el("p", "mono", `待复核样本 #${blocker.id} · ${blocker.order_no}`);
        }));
      return;
    }
    openDialog(delegated ? "撤回模拟授权" : "授权模拟退款",
      el("p", "dialog-text", delegated
        ? "系统将停止确认和执行退款。可继续查询订单、创建人工工单和查看退款进度。"
        : "评测门槛已通过。用户确认后，系统按照金额、审批与退货回执执行模拟退款。"),
      button(delegated ? "确认撤回" : "确认授权", async () => {
        await api("/quality/authority", {mode: delegated ? "SHADOW" : "DELEGATED"});
        dialog.close();
        notice(delegated ? "已恢复影子观察" : "已开启模拟执行权限");
        await refresh();
      }, "primary"));
  }, "accent");
  const metrics = report.metrics;
  const shadow = report.shadow;
  const percentage = (ratio) => `${(ratio * 100).toFixed(1)}%`;
  const metric = (label, value, detail) => el("div", "metric", el("small", "", label), el("strong", "", String(value)),
    el("small", "", detail));
  const comparison = report.comparison;
  const slices = comparison ? ["original", "challenge"].map((key) => {
    const slice = comparison.suites[key];
    const row = (label, summary) => {
      const score = summary.metrics;
      return el("div", "", el("p", "", `${label}　意图 ${score.intent_hits}/${score.intents} · 动作 ${score.action_hits}/${score.actions}
 · 危险分歧 ${summary.shadow.dangerous}/${summary.shadow.paired}`),
      score.orders ? el("small", "muted", `订单号 ${score.order_hits}/${score.orders} · 原因 ${score.reason_hits}/${score.reasons}`) : null);
    };
    return el("div", "work-item",
      el("h3", "", key === "original" ? "原回归集" : "语义挑战集"),
      row("规则式", slice.rules), row("大模型", slice.model));
  }) : [];
  const filter = el("select", "filters");
  filter.setAttribute("aria-label", "筛选评测用例");
  for (const [label, value] of [["全部用例", "all"], ["失败", "fail"], ["影子分歧", "diff"], ["危险分歧", "danger"]]) {
    filter.add(new Option(label, value));
  }
  const list = el("div", "case-scroll");
  function fillCases() {
    const cases = report.cases.filter((entry) => filter.value === "all"
      || (filter.value === "fail" && !entry.passed)
      || (filter.value === "diff" && entry.shadow && entry.shadow.result !== "MATCH")
      || (filter.value === "danger" && entry.shadow?.result === "DANGEROUS"));
    list.replaceChildren(...(cases.length ? cases.map((entry) => {
      return el("details", "case", el("summary", "", `${entry.id} · ${entry.title}  `,
        badge(entry.passed ? "通过" : "失败", entry.passed ? "" : "negative")),
      el("small", "", entry.tags.join(" / ")),
      el("ol", "turns", entry.turns.map((turn) => el("li", "",
        el("div", "", turn.input),
        el("small", "", `预期 ${statusName(turn.expected.action)} · 实际 ${statusName(turn.observed.action)}`),
        el("small", "mono", `正式单 ${turn.observed.formal} · 执行 ${turn.observed.payouts}`),
        turn.reply ? el("small", "", turn.reply) : null,
        turn.mismatches.length ? badge(`字段差异 ${turn.mismatches.map((key) => fieldNames[key]).join("、")}`, "negative") : null,
        turn.violations.length ? badge(turn.violations.join("、"), "negative") : null))));
    }) : [el("p", "empty", "没有符合筛选条件的用例。")]));
  }
  filter.addEventListener("change", fillCases);
  fillCases();
  const pairs = report.cases.filter((entry) => entry.shadow).sort((a, b) =>
    {const priority = {DANGEROUS: 0, REVIEW: 1, MATCH: 2}; return priority[a.shadow.result] - priority[b.shadow.result];});
  const pairNodes = pairs.map((entry) => el("details", "shadow-block",
    el("summary", "", `${entry.id} · ${entry.title}  `, badge(
      {MATCH: "一致", REVIEW: "待复核", DANGEROUS: "危险分歧"}[entry.shadow.result],
      {MATCH: "", REVIEW: "warm", DANGEROUS: "negative"}[entry.shadow.result])),
    el("div", "shadow-columns",
      el("div", "", el("small", "", "候选系统"), formatDecision(entry.shadow.candidate)),
      el("div", "", el("small", "", "模拟坐席基线"), formatDecision(entry.shadow.baseline))),
    el("small", "", entry.shadow.note)));
  const trace = el("input");
  trace.placeholder = "粘贴客户会话的 tr_ 编号";
  trace.setAttribute("aria-label", trace.placeholder);
  workspace.replaceChildren(
    hero("质量运营工作区", "对话评测与授权",
      `${report.source} · ${report.version} · ${authority.live_dangerous > 0 ? "实时样本存在危险分歧"
        : report.gate.passed ? "已通过模拟授权门槛" : "授权门槛未通过"}`, authorize),
    el("div", "metrics",
      metric("评测用例", metrics.scenarios, `${metrics.failed} 个用例失败`),
      metric("意图准确率", percentage(metrics.intent_accuracy), `${metrics.intent_hits}/${metrics.intents} · 门槛 92%`),
      metric("动作准确率", percentage(metrics.action_accuracy), `${metrics.action_hits}/${metrics.actions} · 门槛 90%`),
      metric("危险分歧", shadow.dangerous, `${metrics.critical} 个关键轮次未达标`)),
    el("div", "grid quality-grid",
      panel("用例回放", report.candidate_version, list, filter),
      el("div", "stack",
        panel("影子对比", `离线样本 · ${shadow.paired} 个配对 · ${shadow.matched} 个一致 · ${shadow.review} 个待复核`,
          el("div", "case-scroll", pairNodes)),
        panel("实时影子样本", "影子模式下记录退款建议",
          observations.length ? el("div", "case-scroll", observations.map(observationRow))
            : el("p", "empty", "影子模式下在客户会话发起退款咨询后，样本会出现在这里。")),
        panel("执行授权与审计", delegated ? "当前授权模拟执行" : "当前为影子模式",
          [authority.events.length ? el("ol", "timeline", authority.events.map((entry) => el("li", "", 
            entry.mode === "DELEGATED" ? "授权" : "撤回", el("small", "", ` · ${entry.actor} · ${entry.created_at}`))))
            : el("p", "empty", "还没有授权记录。"),
          el("div", "inline trace", trace, button("查看链路", () => showTrace(trace.value.trim())))])
      )),
    comparison ? el("div", "grid", panel("规则与模型对比", report.candidate_version,
      [...slices, el("p", "muted", `模型有效响应 ${metrics.nlu.model_valid}/${metrics.nlu.model_calls} · 降级 ${metrics.nlu.fallback} 次`),
       ...(report.gate.reasons || []).map((reason) => el("p", "muted", reason))])) : null);
}

async function showTrace(id) {
  if (!id.startsWith("tr_")) throw new Error("请填写会话链路编号");
  const record = await api(`/traces/${encodeURIComponent(id)}`);
  openDialog("会话审计", el("small", "mono", id),
    el("div", "trace-audit",
      el("h3", "", "状态迁移"), el("ol", "timeline", record.transitions.map((event) =>
        el("li", "mono", `${event.from_state} → ${event.to_state} · ${event.event}`))),
      el("h3", "", "工具调用"), el("ol", "timeline", record.tools.map((tool) =>
        el("li", "mono", `${tool.tool_name} · ${tool.status}`))),
      record.nlu?.length ? el("h3", "", "意图与信息提取") : null,
      record.nlu?.length ? el("ol", "timeline", record.nlu.map((reading) =>
        el("li", "mono", `${reading.source} · ${reading.intent} · ${reading.order_no || "—"} · ${reading.reason_code || "—"} · ${reading.latency_ms}ms`))) : null,
      el("h3", "", "退款事件"), record.refunds.map((refund) => el("div", "", el("small", "mono", refund.refund_no),
        el("ol", "timeline", refund.events.map((event) => el("li", "", statusName(event.status),
          el("small", "", ` · ${event.actor}`))))))));
}

document.querySelector("#close-dialog").addEventListener("click", () => dialog.close());
document.querySelector("#identity").addEventListener("click", () => chooseRole());
for (const control of document.querySelectorAll("[data-page]")) {
  control.addEventListener("click", () => {
    if (control.dataset.page === page) return;
    chooseRole(control.dataset.page);
  });
}

const start = demoAccounts.find(([account]) => account === sessionStorage.getItem("demo-account")) || demoAccounts[0];
login(start[0], start[1]).catch((error) => {
  workspace.replaceChildren(el("p", "empty", error.message), button("重新连接", () => login(start[0], start[1])));
});
