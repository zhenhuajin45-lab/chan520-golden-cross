const DATA_URL = "./data/local_sim/latest_account.json";
const AUTO_REFRESH_MS = 15000;

const state = {
  tab: "fills",
  payload: null,
  lastError: null,
  lastRefreshAt: null,
  nextRefreshAt: null,
  refreshing: false,
};

main();

async function main() {
  try {
    state.payload = await fetchJson(`${DATA_URL}?t=${Date.now()}`);
    state.lastRefreshAt = new Date();
    scheduleNextRefresh();
    render();
  } catch (error) {
    renderError(error);
  }
  startAutoRefresh();
  startCountdown();
}

async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  updateRefreshStatus();
  try {
    state.payload = await fetchJson(`${DATA_URL}?t=${Date.now()}`);
    state.lastError = null;
    state.lastRefreshAt = new Date();
    scheduleNextRefresh();
    render();
  } catch (error) {
    state.lastError = error;
    scheduleNextRefresh();
    if (state.payload) {
      render();
    } else {
      renderError(error);
    }
  } finally {
    state.refreshing = false;
    updateRefreshStatus();
  }
}

function startAutoRefresh() {
  window.setInterval(refresh, AUTO_REFRESH_MS);
}

function startCountdown() {
  window.setInterval(updateRefreshStatus, 1000);
}

function scheduleNextRefresh() {
  state.nextRefreshAt = new Date(Date.now() + AUTO_REFRESH_MS);
}

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${url} 读取失败：${response.status}`);
  return response.json();
}

function render() {
  const payload = state.payload || {};
  const account = payload.account || {};
  document.querySelector("#app").innerHTML = `
    <section class="shell">
      <header class="topbar">
        <div class="topbar-inner">
          <div>
            <h1>Chan520 本地模拟盘工作台</h1>
            <p class="meta">账户 ${escapeHtml(account.account_id || "local-sim")}｜交易日 ${escapeHtml(payload.trade_date || "未记录")}｜估值 ${escapeHtml(payload.valuation_status || "-")}｜更新 ${escapeHtml(payload.generated_at || "—")}</p>
          </div>
          <div class="actions">
            <button class="icon-button" type="button" data-action="refresh" title="刷新">↻</button>
            <div class="refresh-status" data-refresh-status>
              ${refreshStatusHtml()}
            </div>
          </div>
        </div>
      </header>
      <div class="content">
        ${renderValuationAlert(payload)}
        ${renderReadinessAlert(payload)}
        ${renderCorePlanAlert(payload)}
        ${renderKpis(account)}
        <section class="grid">
          <article class="panel">
            <div class="panel-head">
              <div>
                <h2>持仓</h2>
                <p class="meta">${escapeHtml(payload.valuation_basis || "按本地账本估值")}</p>
              </div>
            </div>
            ${renderPositions(payload.positions || [])}
          </article>
          <article class="panel">
            <div class="panel-head">
              <div>
                <h2>每日汇总</h2>
                <p class="meta">按成交账本聚合</p>
              </div>
            </div>
            ${renderDaily(payload.daily || [])}
          </article>
        </section>
        <article class="panel" style="margin-top:16px">
          <div class="panel-head">
            <div>
              <h2>交易明细</h2>
              <p class="meta">订单、成交和资金流向</p>
            </div>
            <div class="tabs">
              ${tabButton("plans", "计划")}
              ${tabButton("fills", "成交")}
              ${tabButton("orders", "订单")}
            </div>
          </div>
          ${state.tab === "plans" ? renderPlans(payload.planned_orders || []) : state.tab === "fills" ? renderFills(payload.fills || []) : renderOrders(payload.orders || [])}
        </article>
      </div>
    </section>
  `;
  document.querySelector("[data-action='refresh']").addEventListener("click", refresh);
  updateRefreshStatus();
  for (const button of document.querySelectorAll("[data-tab]")) {
    button.addEventListener("click", () => {
      state.tab = button.dataset.tab;
      render();
    });
  }
}

function refreshStatusHtml() {
  const statusClass = state.lastError ? "error" : state.refreshing ? "loading" : "ok";
  return `
    <span class="status-dot ${statusClass}"></span>
    <span data-refresh-label>${escapeHtml(refreshStatusText())}</span>
  `;
}

function updateRefreshStatus() {
  const target = document.querySelector("[data-refresh-status]");
  if (!target) return;
  target.innerHTML = refreshStatusHtml();
}

function refreshStatusText() {
  if (state.refreshing) return "正在自动刷新";
  if (state.lastError) return `刷新失败：${state.lastError.message || state.lastError}`;
  const last = state.lastRefreshAt ? `上次 ${formatTime(state.lastRefreshAt)}` : "尚未刷新";
  const remain = state.nextRefreshAt ? Math.max(0, Math.ceil((state.nextRefreshAt.getTime() - Date.now()) / 1000)) : Math.ceil(AUTO_REFRESH_MS / 1000);
  return `自动刷新 ${Math.ceil(AUTO_REFRESH_MS / 1000)}s｜${last}｜下次 ${remain}s`;
}

function renderKpis(account) {
  return `
    <section class="kpis">
      ${kpi("总资产", money(account.total_equity))}
      ${kpi("账户盈亏", `${signedMoney(account.total_pnl)} / ${signedPct(account.total_pnl_pct)}`, pnlClass(account.total_pnl))}
      ${kpi("可用现金", money(account.cash))}
      ${kpi("持仓市值", money(account.market_value))}
      ${kpi("仓位", pct(account.gross_exposure_pct))}
      ${kpi("持仓数", intText(account.open_position_count))}
      ${kpi("成交数", intText(account.fill_count))}
    </section>
  `;
}

function renderValuationAlert(payload) {
  if (payload.valuation_status === "COMPLETE") return "";
  const kind = payload.valuation_complete === false ? "danger" : "warning";
  return `<div class="valuation-alert ${kind}">估值状态 ${escapeHtml(payload.valuation_status || "UNKNOWN")}：${escapeHtml(payload.valuation_basis || "行情口径未知")}。行情不完整时飞书盘后复盘会停止推送。</div>`;
}

function renderCorePlanAlert(payload) {
  const core = payload.core_plan || {};
  if (!core.status) return "";
  const quality = core.scan_quality || {};
  const coverage = Number(quality.coverage);
  const coverageText = Number.isFinite(coverage) ? `${(coverage * 100).toFixed(2)}%` : "未知";
  const kind = core.status === "PASS" && Number(core.executable_buy_count || 0) > 0 ? "ok" : core.status === "PASS" ? "warning" : "danger";
  const boundary = core.status !== "PASS"
    ? "自动新增买入已关闭，仅保留现有持仓风控。"
    : Number(core.executable_buy_count || 0) > 0
      ? `仅 ${intText(core.executable_buy_count)} 只严格候选可进入盘中二阶段确认。`
      : "没有严格候选，观察池不会自动成交。";
  return `<div class="valuation-alert ${kind}">核心计划 ${escapeHtml(core.status)}｜信号日 ${escapeHtml(core.signal_date || "-")}｜扫描覆盖率 ${escapeHtml(coverageText)}｜几何拦截 ${intText(core.geometry_blocked_count || 0)}｜${escapeHtml(boundary)}</div>`;
}

function renderReadinessAlert(payload) {
  const readiness = payload.readiness || {};
  if (!readiness.status) return "";
  const riskReady = readiness.local_sim_risk_loop_ready === true;
  const buyReady = readiness.local_sim_buy_entry_ready === true;
  const kind = !riskReady ? "danger" : buyReady ? "ok" : "warning";
  const riskText = riskReady ? "风险闭环 READY" : "风险闭环 BLOCKED";
  const buyText = buyReady ? "新增买入 READY" : "新增买入 BLOCKED";
  const blockers = (readiness.buy_entry_blocking_checks || []).join(", ") || "无";
  return `<div class="valuation-alert ${kind}">${escapeHtml(readiness.status)}｜${escapeHtml(riskText)}｜${escapeHtml(buyText)}｜买入阻断 ${escapeHtml(blockers)}</div>`;
}

function kpi(label, value, valueClass = "") {
  return `<article class="kpi"><span>${escapeHtml(label)}</span><strong class="${escapeHtml(valueClass)}">${escapeHtml(value)}</strong></article>`;
}

function tabButton(tab, label) {
  const active = state.tab === tab ? " active" : "";
  return `<button class="tab${active}" type="button" data-tab="${escapeHtml(tab)}">${escapeHtml(label)}</button>`;
}

function renderPositions(rows) {
  if (!rows.length) return empty("暂无持仓");
  return `
    <div class="table-wrap">
      <table>
        <thead><tr><th>股票</th><th>股数</th><th>T+1可卖</th><th>成本</th><th>估值价</th><th>市值</th><th>浮盈亏</th><th>峰值/保护</th><th>行情</th><th>入场理由</th><th>更新</th></tr></thead>
        <tbody>
          ${rows.map((row) => `
            <tr>
              <td class="symbol">${stockCell(row)}</td>
              <td>${intText(row.shares)}</td>
              <td title="${escapeHtml(row.t_plus_one_status || "")}">${intText(row.sellable_shares)}</td>
              <td>${price(row.average_price)}</td>
              <td>${price(row.market_price)}</td>
              <td>${money(row.market_value)}</td>
              <td class="${pnlClass(row.unrealized_pnl)}">${signedMoney(row.unrealized_pnl)} / ${signedPct(row.unrealized_pnl_pct)}</td>
              <td class="${pnlClass(row.peak_unrealized_pnl_pct)}">${signedPct(row.peak_unrealized_pnl_pct)} / ${row.profit_protection_armed ? "已武装" : "未武装"}</td>
              <td>${quoteBadge(row)}</td>
              <td class="reason-cell">${escapeHtml(positionEntryReason(row))}</td>
              <td>${escapeHtml(row.updated_at || "")}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderDaily(rows) {
  if (!rows.length) return empty("暂无每日成交");
  return `
    <div class="daily-list">
      ${rows.slice(0, 12).map((row) => `
        <div class="daily-row">
          <strong>${escapeHtml(row.trade_date)}</strong>
          <span>买入 ${money(row.buy_gross)}</span>
          <span>卖出 ${money(row.sell_gross)}</span>
          <span>费用 ${money(row.fees, 2)}</span>
        </div>
      `).join("")}
    </div>
  `;
}

function renderPlans(rows) {
  if (!rows.length) return empty("暂无计划订单");
  return `
    <div class="table-wrap">
      <table>
        <thead><tr><th>创建</th><th>交易日</th><th>股票</th><th>方向</th><th>股数</th><th>状态</th><th>触发区</th><th>止损/目标</th><th>几何/T+1</th><th>理由</th><th>消息</th></tr></thead>
        <tbody>
          ${rows.map((row) => `
            <tr>
              <td>${escapeHtml(row.created_at || "")}</td>
              <td>${escapeHtml(row.trade_date || "")}</td>
              <td class="symbol">${stockCell(row)}</td>
              <td class="${sideClass(row.side)}">${sideLabel(row.side)}</td>
              <td>${intText(row.volume)}</td>
              <td><em class="badge ${String(row.status || "").toLowerCase()}">${escapeHtml(row.status || "")}</em></td>
              <td>${price(row.lower_price)} - ${price(row.upper_price || row.trigger_price)}</td>
              <td>${price(row.stop_price)} / ${price(row.target_price)}</td>
              <td>${planGeometry(row)} / ${pct((row.payload || {}).t1_loss_buffer_pct)}</td>
              <td class="reason-cell">${escapeHtml(planReason(row))}</td>
              <td class="reason-cell">${escapeHtml(row.last_message || "")}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderFills(rows) {
  if (!rows.length) return empty("暂无成交明细");
  return `
    <div class="table-wrap">
      <table>
        <thead><tr><th>时间</th><th>股票</th><th>方向</th><th>股数</th><th>价格</th><th>成交额</th><th>理由</th><th>佣金</th><th>过户费</th><th>印花税</th><th>成交ID</th></tr></thead>
        <tbody>
          ${rows.map((row) => `
            <tr>
              <td>${escapeHtml(row.created_at || "")}</td>
              <td class="symbol">${stockCell(row)}</td>
              <td class="${sideClass(row.side)}">${sideLabel(row.side)}</td>
              <td>${intText(row.volume)}</td>
              <td>${price(row.price)}</td>
              <td>${money(row.gross)}</td>
              <td class="reason-cell">${escapeHtml(tradeReason(row))}</td>
              <td>${money(row.commission, 2)}</td>
              <td>${money(row.transfer_fee, 2)}</td>
              <td>${money(row.stamp_duty, 2)}</td>
              <td>${escapeHtml(row.fill_id)}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderOrders(rows) {
  if (!rows.length) return empty("暂无订单明细");
  return `
    <div class="table-wrap">
      <table>
        <thead><tr><th>时间</th><th>交易日</th><th>股票</th><th>方向</th><th>股数</th><th>价格</th><th>理由</th><th>状态</th><th>订单ID</th></tr></thead>
        <tbody>
          ${rows.map((row) => `
            <tr>
              <td>${escapeHtml(row.created_at || "")}</td>
              <td>${escapeHtml(row.session_date || "")}</td>
              <td class="symbol">${stockCell(row)}</td>
              <td class="${sideClass(row.side)}">${sideLabel(row.side)}</td>
              <td>${intText(row.volume)}</td>
              <td>${price(row.price)}</td>
              <td class="reason-cell">${escapeHtml(tradeReason(row))}</td>
              <td><em class="badge ${String(row.status || "").toLowerCase()}">${escapeHtml(row.status || "")}</em></td>
              <td>${escapeHtml(row.order_id)}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function empty(text) {
  return `<div class="empty">${escapeHtml(text)}</div>`;
}

function renderError(error) {
  document.querySelector("#app").innerHTML = `
    <section class="error">
      <h1>工作台数据尚未生成</h1>
      <p>${escapeHtml(error.message || String(error))}</p>
      <p class="meta">先运行：python scripts/export_local_sim_dashboard.py</p>
    </section>
  `;
}

function sideLabel(value) {
  const side = String(value || "").toUpperCase();
  return side === "BUY" ? "买入" : side === "SELL" ? "卖出" : escapeHtml(value || "");
}

function sideClass(value) {
  return String(value || "").toUpperCase() === "BUY" ? "buy" : String(value || "").toUpperCase() === "SELL" ? "sell" : "";
}

function positionEntryReason(row) {
  return compactText([row.signal_name, row.entry_reason, row.entry_notes]);
}

function tradeReason(row) {
  const side = String(row.side || "").toUpperCase();
  if (side === "BUY") return compactText([row.signal_name, row.entry_reason, row.notes]);
  if (side === "SELL") return compactText([row.risk_reason_code, row.risk_reason, row.exit_reason, row.notes]);
  return compactText([row.signal_name, row.entry_reason, row.exit_reason, row.risk_reason, row.notes]);
}

function planReason(row) {
  const payload = row.payload || {};
  return compactText([payload.signal_name, row.reason_code, row.reason_text, payload.entry_reason, payload.exit_reason, payload.risk_reason, payload.notes]);
}

function planGeometry(row) {
  const value = (row.payload || {}).geometry_valid;
  return value === true ? "有效" : value === false ? "无效" : "—";
}

function quoteBadge(row) {
  const status = row.quote_status || "COST_FALLBACK";
  const title = [row.quote_time, row.quote_age_minutes != null ? `${Number(row.quote_age_minutes).toFixed(1)}分钟` : ""].filter(Boolean).join("｜");
  return `<span class="quote ${escapeHtml(String(status).toLowerCase())}" title="${escapeHtml(title)}">${escapeHtml(status)}</span>`;
}

function stockCell(row) {
  const symbol = row.symbol || "—";
  const name = row.stock_name || "";
  return `
    <span class="stock-code">${escapeHtml(symbol)}</span>
    ${name ? `<span class="stock-name">${escapeHtml(name)}</span>` : ""}
  `;
}

function compactText(parts) {
  const text = parts.map((item) => String(item || "").trim()).filter(Boolean).join("｜");
  return text || "未记录";
}

function money(value, digits = 0) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "—";
  if (Math.abs(num) >= 100000000) return `${(num / 100000000).toFixed(2)}亿`;
  if (Math.abs(num) >= 10000) return `${(num / 10000).toFixed(1)}万`;
  return num.toFixed(digits);
}

function signedMoney(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "—";
  const sign = num > 0 ? "+" : "";
  return `${sign}${money(num)}`;
}

function price(value) {
  const num = Number(value);
  return Number.isFinite(num) ? num.toFixed(2) : "—";
}

function pct(value) {
  const num = Number(value);
  return Number.isFinite(num) ? `${(num * 100).toFixed(2)}%` : "—";
}

function signedPct(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "—";
  const sign = num > 0 ? "+" : "";
  return `${sign}${pct(num)}`;
}

function intText(value) {
  const num = Number(value);
  return Number.isFinite(num) ? String(Math.trunc(num)) : "—";
}

function pnlClass(value) {
  const num = Number(value);
  if (!Number.isFinite(num) || Math.abs(num) < 0.000001) return "pnl-flat";
  return num > 0 ? "pnl-positive" : "pnl-negative";
}

function formatTime(value) {
  return value.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
