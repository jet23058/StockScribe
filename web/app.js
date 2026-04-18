const form = document.querySelector("#snapshot-form");
const button = document.querySelector("#submit-button");
const statusBox = document.querySelector("#status");
const progressPanel = document.querySelector("#progress-panel");
const progressLabel = document.querySelector("#progress-label");
const progressCount = document.querySelector("#progress-count");
const progressBar = document.querySelector("#progress-bar");
const results = document.querySelector("#results");
const rangeTitle = document.querySelector("#range-title");
const returnChart = document.querySelector("#return-chart");
const accountRankings = document.querySelector("#account-rankings");
const profitGrid = document.querySelector("#profit-grid");
const lossGrid = document.querySelector("#loss-grid");
const noDataBlock = document.querySelector("#no-data-block");
const noDataGrid = document.querySelector("#no-data-grid");
const jsonOutput = document.querySelector("#json-output");
const downloadButton = document.querySelector("#download-button");
const tabButtons = Array.from(document.querySelectorAll(".tab-button"));
const tabPanels = Array.from(document.querySelectorAll(".tab-panel"));

let latestSnapshot = null;
let pollTimer = null;

tabButtons.forEach((tabButton) => {
  tabButton.addEventListener("click", () => activateTab(tabButton.dataset.tab));
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearPollTimer();
  setStatus("建立快照中...");
  setProgress({ stage: "queued", current: 0, total: 1, message: "送出任務" });
  button.disabled = true;
  results.hidden = true;

  const payload = {
    url: document.querySelector("#url").value,
    start: document.querySelector("#start").value,
    end: document.querySelector("#end").value,
    market: document.querySelector("#market").value,
    force_refresh: document.querySelector("#force-refresh").checked,
  };

  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "建立快照失敗");
    }
    pollJob(data.job_id);
  } catch (error) {
    latestSnapshot = null;
    setStatus(error.message, true);
    button.disabled = false;
  } finally {
  }
});

downloadButton.addEventListener("click", () => {
  if (!latestSnapshot) return;
  const blob = new Blob([JSON.stringify(latestSnapshot, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `stockscribe-${latestSnapshot.date_range.start}-${latestSnapshot.date_range.end}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
});

function setStatus(message, isError = false) {
  statusBox.hidden = false;
  statusBox.textContent = message;
  statusBox.classList.toggle("error", isError);
}

async function pollJob(jobId) {
  try {
    const response = await fetch(`/api/jobs/${jobId}`);
    const job = await response.json();
    if (!response.ok) {
      throw new Error(job.error || "讀取進度失敗");
    }
    setProgress(job.progress);
    if (job.state === "done") {
      latestSnapshot = job.result;
      renderSnapshot(job.result);
      setStatus(`完成：找到 ${job.result.stocks.length} 檔股票。`);
      button.disabled = false;
      clearPollTimer();
      return;
    }
    if (job.state === "error") {
      throw new Error(job.error || "建立快照失敗");
    }
    pollTimer = setTimeout(() => pollJob(jobId), 500);
  } catch (error) {
    latestSnapshot = null;
    setStatus(error.message, true);
    button.disabled = false;
    clearPollTimer();
  }
}

function clearPollTimer() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function activateTab(panelId) {
  tabButtons.forEach((tabButton) => {
    const isActive = tabButton.dataset.tab === panelId;
    tabButton.classList.toggle("active", isActive);
    tabButton.setAttribute("aria-selected", String(isActive));
  });
  tabPanels.forEach((panel) => {
    const isActive = panel.id === panelId;
    panel.classList.toggle("active", isActive);
    panel.hidden = !isActive;
  });
}

function setProgress(progress) {
  const current = Number(progress?.current ?? 0);
  const total = Math.max(Number(progress?.total ?? 1), 1);
  const percent = progress?.stage === "done" ? 100 : Math.min(Math.round((current / total) * 100), 99);
  progressPanel.hidden = false;
  progressBar.value = percent;
  progressLabel.textContent = progress?.message || "處理中";
  progressCount.textContent = total > 1 ? `${current} / ${total}` : `${percent}%`;
}

function renderSnapshot(snapshot) {
  const range = snapshot.date_range;
  rangeTitle.textContent = `${range.start} 到 ${range.end}`;
  const okSummaries = snapshot.summaries.filter((summary) => summary.status === "ok");
  const sortedSummaries = okSummaries
    .slice()
    .sort((a, b) => Number(b.pct_change) - Number(a.pct_change));
  const profitSummaries = sortedSummaries.filter((summary) => summary.change > 0);
  const lossSummaries = sortedSummaries.filter((summary) => summary.change < 0);
  const flatSummaries = okSummaries.filter((summary) => summary.change === 0);
  const noDataSummaries = snapshot.summaries.filter((summary) => summary.status !== "ok").concat(flatSummaries);
  renderReturnChart(sortedSummaries);
  renderAccountRankings(okSummaries);
  profitGrid.replaceChildren(...cardsOrEmpty(profitSummaries, "這段期間沒有賺錢的股票。"));
  lossGrid.replaceChildren(...cardsOrEmpty(lossSummaries, "這段期間沒有賠錢的股票。"));
  noDataGrid.replaceChildren(...noDataSummaries.map(renderCard));
  noDataBlock.hidden = noDataSummaries.length === 0;
  jsonOutput.textContent = JSON.stringify(snapshot, null, 2);
  results.hidden = false;
}

function renderReturnChart(summaries) {
  const chartRows = summaries.filter((summary) => Number.isFinite(Number(summary.pct_change)));
  if (chartRows.length === 0) {
    returnChart.replaceChildren(emptyChart("沒有可繪製的報酬率資料。"));
    return;
  }

  const gainRows = chartRows.filter((summary) => Number(summary.pct_change) > 0);
  const lossRows = chartRows
    .filter((summary) => Number(summary.pct_change) < 0)
    .slice()
    .sort((a, b) => Number(a.pct_change) - Number(b.pct_change));
  const maxAbs = Math.max(...gainRows.concat(lossRows).map((summary) => Math.abs(Number(summary.pct_change))), 1);
  const board = document.createElement("div");
  board.className = "movers-board";
  board.append(
    renderMoverSection(`全部賺的股票（${gainRows.length}）`, gainRows, maxAbs, "gain"),
    renderMoverSection(`全部虧的股票（${lossRows.length}）`, lossRows, maxAbs, "loss"),
  );
  returnChart.replaceChildren(board);
}

function renderAccountRankings(summaries) {
  const accountRows = buildAccountRows(summaries);
  if (accountRows.length === 0) {
    accountRankings.replaceChildren(emptyChart("沒有可對應帳號的推薦資料。"));
    return;
  }

  const bestRows = accountRows
    .slice()
    .sort((a, b) => b.averagePct - a.averagePct || b.winRate - a.winRate || b.count - a.count)
    .slice(0, 10);
  const worstRows = accountRows
    .slice()
    .sort((a, b) => a.averagePct - b.averagePct || a.winRate - b.winRate || b.count - a.count)
    .slice(0, 10);
  const maxAbs = Math.max(
    ...bestRows.concat(worstRows).map((row) => Math.abs(row.averagePct)),
    1,
  );
  const board = document.createElement("div");
  board.className = "movers-board";
  board.append(
    renderAccountSection("平均報酬最高", bestRows, maxAbs, "gain"),
    renderAccountSection("平均報酬最低", worstRows, maxAbs, "loss"),
  );
  accountRankings.replaceChildren(board);
}

function buildAccountRows(summaries) {
  const byAccount = new Map();
  for (const summary of summaries) {
    for (const account of summary.mentioned_by || []) {
      if (!byAccount.has(account)) {
        byAccount.set(account, []);
      }
      byAccount.get(account).push(summary);
    }
  }

  return Array.from(byAccount.entries()).map(([account, accountSummaries]) => {
    const sorted = accountSummaries
      .slice()
      .sort((a, b) => Number(b.pct_change) - Number(a.pct_change));
    const pctValues = accountSummaries.map((summary) => Number(summary.pct_change));
    const wins = pctValues.filter((pct) => pct > 0).length;
    const losses = pctValues.filter((pct) => pct < 0).length;
    const averagePct = pctValues.reduce((total, pct) => total + pct, 0) / pctValues.length;
    return {
      account,
      count: accountSummaries.length,
      averagePct,
      winRate: wins / accountSummaries.length,
      wins,
      losses,
      best: sorted[0],
      worst: sorted[sorted.length - 1],
    };
  });
}

function renderAccountSection(title, rows, maxAbs, tone) {
  const section = document.createElement("section");
  section.className = `mover-section ${tone}-section`;
  const heading = document.createElement("h4");
  heading.textContent = title;
  const list = document.createElement("div");
  list.className = "mover-list";
  if (rows.length > 0) {
    list.replaceChildren(...rows.map((row) => renderAccountRow(row, maxAbs, tone)));
  } else {
    list.replaceChildren(...cardsOrEmpty(rows, `這段期間沒有${tone === "gain" ? "賺錢" : "賠錢"}推薦。`));
  }
  section.append(heading, list);
  return section;
}

function renderAccountRow(row, maxAbs, tone) {
  const item = document.createElement("article");
  item.className = `mover-row account-rank-row ${tone}-row`;
  const pct = row.averagePct;
  const width = Math.max(Math.abs(pct) / maxAbs * 100, 2);
  const bestName = row.best.name || row.best.raw || row.best.symbol;
  const worstName = row.worst.name || row.worst.raw || row.worst.symbol;
  const stockLines = row.count === 1
    ? `<span>推薦 ${escapeHtml(bestName)} ${formatNumber(row.best.pct_change)}%</span>`
    : `
      <span>最佳 ${escapeHtml(bestName)} ${formatNumber(row.best.pct_change)}%</span>
      <span>最差 ${escapeHtml(worstName)} ${formatNumber(row.worst.pct_change)}%</span>
    `;
  const accountUrl = pttWebAccountMessagesUrl(row.account);
  item.innerHTML = `
    <div class="mover-name">
      <a class="account-rank-link" href="${accountUrl}" target="_blank" rel="noreferrer noopener">${escapeHtml(row.account)}</a>
      <span>${row.count} 檔，勝率 ${formatNumber(row.winRate * 100)}%</span>
    </div>
    <div class="mover-bar-track">
      <div class="mover-bar" style="width: ${width}%"></div>
    </div>
    <div class="mover-values">
      <strong>平均 ${formatNumber(pct)}%</strong>
      <span>賺 ${row.wins} / 虧 ${row.losses}</span>
      ${stockLines}
    </div>
  `;
  return item;
}

function renderMoverSection(title, summaries, maxAbs, tone) {
  const section = document.createElement("section");
  section.className = `mover-section ${tone}-section`;
  const heading = document.createElement("h4");
  heading.textContent = title;
  const rows = document.createElement("div");
  rows.className = "mover-list";
  if (summaries.length > 0) {
    rows.replaceChildren(...summaries.map((summary) => renderMoverRow(summary, maxAbs, tone)));
  } else {
    rows.replaceChildren(...cardsOrEmpty(summaries, `這段期間沒有${tone === "gain" ? "賺錢" : "賠錢"}的股票。`));
  }
  section.append(heading, rows);
  return section;
}

function renderMoverRow(summary, maxAbs, tone) {
  const row = document.createElement("article");
  row.className = `mover-row ${tone}-row`;
  const pct = Number(summary.pct_change);
  const width = Math.max(Math.abs(pct) / maxAbs * 100, 2);
  const name = summary.name || summary.raw || summary.symbol;
  row.innerHTML = `
    <div class="mover-name">
      <strong>${escapeHtml(name)}</strong>
      <span>${escapeHtml(summary.symbol)}</span>
    </div>
    <div class="mover-bar-track">
      <div class="mover-bar" style="width: ${width}%"></div>
    </div>
    <div class="mover-values">
      <strong>${formatNumber(pct)}%</strong>
      <span>${formatNumber(summary.start_close)} → ${formatNumber(summary.end_close)}</span>
    </div>
  `;
  return row;
}

function emptyChart(message) {
  const empty = document.createElement("p");
  empty.className = "empty-state";
  empty.textContent = message;
  return empty;
}

function cardsOrEmpty(summaries, emptyText) {
  if (summaries.length > 0) {
    return summaries.map(renderCard);
  }
  const empty = document.createElement("p");
  empty.className = "empty-state";
  empty.textContent = emptyText;
  return [empty];
}

function renderCard(summary) {
  const card = document.createElement("article");
  card.className = "stock-card";
  const title = `${summary.name || summary.raw || summary.symbol} ${summary.symbol}`;

  if (summary.status !== "ok") {
    card.innerHTML = `
      <h4>${escapeHtml(title)}</h4>
      <p>這段期間沒有查到 Yahoo 歷史資料。</p>
      <dl class="metric-list">
        ${metric("提及帳號", formatMentionAccounts(summary.mentioned_by))}
      </dl>
    `;
    return card;
  }

  const directionClass = summary.change >= 0 ? "positive" : "negative";
  card.innerHTML = `
    <h4>${escapeHtml(title)}</h4>
    <dl class="metric-list">
      ${metric("交易日", summary.trading_days)}
      ${metric("起始收盤", formatNumber(summary.start_close))}
      ${metric("結束收盤", formatNumber(summary.end_close))}
      ${metric("漲跌", `<strong class="${directionClass}">${formatNumber(summary.change)} (${formatNumber(summary.pct_change)}%)</strong>`)}
      ${metric("最高收盤", formatNumber(summary.highest_close))}
      ${metric("最低收盤", formatNumber(summary.lowest_close))}
      ${metric("平均收盤", formatNumber(summary.average_close))}
      ${metric("總成交量", formatNumber(summary.total_volume))}
      ${metric("提及帳號", formatMentionAccounts(summary.mentioned_by))}
    </dl>
  `;
  return card;
}

function metric(label, value) {
  return `
    <div class="metric-row">
      <span>${label}</span>
      <span>${value}</span>
    </div>
  `;
}

function formatNumber(value) {
  if (value === null || value === undefined) return "-";
  return Number(value).toLocaleString("zh-TW", { maximumFractionDigits: 4 });
}

function formatMentionAccounts(accounts) {
  if (!accounts || accounts.length === 0) {
    return '<span class="muted-value">未對應帳號</span>';
  }
  const visible = accounts.slice(0, 5).map((account) => {
    const url = pttWebAccountMessagesUrl(account);
    return `<a class="account-pill" href="${url}" target="_blank" rel="noreferrer noopener">${escapeHtml(account)}</a>`;
  });
  if (accounts.length > 5) {
    visible.push(`<span class="account-pill">+${accounts.length - 5}</span>`);
  }
  return `<span class="account-list">${visible.join("")}</span>`;
}

function pttWebAccountMessagesUrl(account) {
  return `https://www.pttweb.cc/user/${encodeURIComponent(account)}/stock?t=message`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
