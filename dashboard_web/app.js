
/* ===== ZORA Dashboard — App Logic ===== */

(function () {
  'use strict';

  const D = window.ZORA_DATA;

  // ===== Navigation =====
  const navItems = document.querySelectorAll('.nav-item');
  const pages = document.querySelectorAll('.page');
  const pageTitle = document.getElementById('page-title');
  const titles = {
    overview: 'Overview',
    positions: 'Open Positions',
    trades: 'Trade History',
    brain: 'Brain Weights',
    ai: 'AI Panel',
    risk: 'Risk Settings',
  };

  navItems.forEach(item => {
    item.addEventListener('click', (e) => {
      e.preventDefault();
      const page = item.dataset.page;
      navItems.forEach(n => n.classList.remove('active'));
      item.classList.add('active');
      pages.forEach(p => p.classList.remove('active'));
      document.getElementById(page).classList.add('active');
      pageTitle.textContent = titles[page] || page;
    });
  });

  // ===== Update time =====
  function updateTime() {
    const now = new Date();
    const timeStr = now.toLocaleTimeString('en-US', { hour12: false });
    document.getElementById('last-update-time').textContent = timeStr;
  }
  updateTime();
  setInterval(updateTime, 1000);

  // ===== ETH price ticker (simulated) =====
  let ethPrice = 2418.50;
  let ethChange = 2.34;
  function updateEthPrice() {
    ethPrice += (Math.random() - 0.5) * 3;
    ethChange += (Math.random() - 0.5) * 0.1;
    document.getElementById('eth-price').textContent = '$' + ethPrice.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const changeEl = document.getElementById('eth-change');
    changeEl.textContent = (ethChange >= 0 ? '+' : '') + ethChange.toFixed(2) + '%';
    changeEl.style.color = ethChange >= 0 ? 'var(--green)' : 'var(--red)';
  }
  setInterval(updateEthPrice, 3000);

  // ===== Equity Chart =====
  const equityCtx = document.getElementById('equity-chart').getContext('2d');

  // Create gradient
  const gradient = equityCtx.createLinearGradient(0, 0, 0, 320);
  gradient.addColorStop(0, 'rgba(0, 212, 255, 0.25)');
  gradient.addColorStop(1, 'rgba(0, 212, 255, 0.0)');

  const equityChart = new Chart(equityCtx, {
    type: 'line',
    data: {
      labels: D.EQUITY_DATA.map(d => d.date),
      datasets: [{
        label: 'Equity ($)',
        data: D.EQUITY_DATA.map(d => d.equity),
        borderColor: '#00d4ff',
        backgroundColor: gradient,
        borderWidth: 2,
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        pointHoverRadius: 5,
        pointHoverBackgroundColor: '#00d4ff',
        pointHoverBorderColor: '#fff',
        pointHoverBorderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#11131a',
          borderColor: '#232634',
          borderWidth: 1,
          titleColor: '#e8eaf0',
          bodyColor: '#8b8e9b',
          bodyFont: { family: 'JetBrains Mono', size: 12 },
          titleFont: { family: 'Inter', size: 12, weight: '600' },
          padding: 12,
          cornerRadius: 8,
          callbacks: {
            label: (ctx) => '$' + ctx.parsed.y.toFixed(2),
          },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: {
            color: '#565867',
            font: { family: 'Inter', size: 10 },
            maxTicksLimit: 10,
          },
        },
        y: {
          grid: { color: 'rgba(35, 38, 52, 0.6)', drawBorder: false },
          ticks: {
            color: '#565867',
            font: { family: 'JetBrains Mono', size: 10 },
            callback: (val) => '$' + val.toFixed(0),
          },
        },
      },
    },
  });

  // Chart range toggles
  document.querySelectorAll('.chart-toggles .toggle-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.chart-toggles .toggle-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const range = btn.dataset.range;
      let data = D.EQUITY_DATA;
      if (range === '30d') data = D.EQUITY_DATA.slice(-30);
      else if (range === '7d') data = D.EQUITY_DATA.slice(-7);
      equityChart.data.labels = data.map(d => d.date);
      equityChart.data.datasets[0].data = data.map(d => d.equity);
      equityChart.update('smooth');
    });
  });

  // ===== Overview: Positions =====
  const overviewPositions = document.getElementById('overview-positions');
  overviewPositions.innerHTML = D.OPEN_POSITIONS.map(p => `
    <div class="position-item">
      <div>
        <span class="pos-symbol">${p.symbol}</span>
        <span class="pos-side ${p.side.toLowerCase()}">${p.side}</span>
      </div>
      <span class="pos-pnl ${p.unrealized_pnl >= 0 ? 'positive' : 'negative'}">
        ${p.unrealized_pnl >= 0 ? '+' : ''}$${p.unrealized_pnl.toFixed(2)} (${p.pnl_pct.toFixed(2)}%)
      </span>
    </div>
  `).join('');

  // ===== Overview: Signals =====
  const overviewSignals = document.getElementById('overview-signals');
  overviewSignals.innerHTML = D.RECENT_SIGNALS.slice(0, 6).map(s => `
    <div class="signal-item ${s.action.toLowerCase()}">
      <span class="signal-action ${s.action.toLowerCase()}">${s.action}</span>
      <span class="signal-reason">${s.reason}</span>
      <span class="signal-time">${s.time}</span>
    </div>
  `).join('');

  // ===== Positions Table =====
  const positionsTable = document.getElementById('positions-table');
  positionsTable.innerHTML = D.OPEN_POSITIONS.map(p => `
    <tr>
      <td><strong>${p.symbol}</strong></td>
      <td><span class="pos-side ${p.side.toLowerCase()}">${p.side}</span></td>
      <td class="mono">$${p.entry_price.toFixed(2)}</td>
      <td class="mono">$${p.current_price.toFixed(2)}</td>
      <td class="mono">${p.size.toFixed(4)}</td>
      <td class="mono">$${p.notional.toFixed(2)}</td>
      <td class="mono" style="color: var(--red)">$${p.stop_loss.toFixed(2)}</td>
      <td class="mono" style="color: var(--green)">$${p.take_profit.toFixed(2)}</td>
      <td class="mono pnl-${p.unrealized_pnl >= 0 ? 'positive' : 'negative'}">
        ${p.unrealized_pnl >= 0 ? '+' : ''}$${p.unrealized_pnl.toFixed(2)} (${p.pnl_pct.toFixed(2)}%)
      </td>
      <td class="mono" style="color: var(--text-faint); font-size: 0.75rem">${p.opened}</td>
    </tr>
  `).join('');

  // ===== Trades Table =====
  const tradesTable = document.getElementById('trades-table');
  function renderTrades(filter = 'all') {
    const filtered = filter === 'all' ? D.CLOSED_TRADES : D.CLOSED_TRADES.filter(t => t.symbol === filter);
    tradesTable.innerHTML = filtered.map(t => `
      <tr>
        <td class="mono" style="color: var(--text-faint); font-size: 0.75rem">${t.date}</td>
        <td><strong>${t.symbol}</strong></td>
        <td><span class="pos-side ${t.side.toLowerCase()}">${t.side}</span></td>
        <td class="mono">$${t.entry.toFixed(2)}</td>
        <td class="mono">$${t.exit.toFixed(2)}</td>
        <td class="mono">${t.size.toFixed(4)}</td>
        <td class="mono pnl-${t.pnl >= 0 ? 'positive' : 'negative'}">
          ${t.pnl >= 0 ? '+' : ''}$${t.pnl.toFixed(2)}
        </td>
        <td class="mono" style="color: ${t.r_multiple >= 0 ? 'var(--green)' : 'var(--red)'}">
          ${t.r_multiple >= 0 ? '+' : ''}${t.r_multiple.toFixed(2)}R
        </td>
        <td class="mono" style="color: var(--text-faint); font-size: 0.75rem">${t.hold_time}</td>
        <td style="font-size: 0.75rem; color: var(--text-muted)">${t.exit_reason}</td>
      </tr>
    `).join('');
  }
  renderTrades();

  // Trade filter
  document.getElementById('trade-filter').addEventListener('change', (e) => {
    renderTrades(e.target.value);
  });

  // CSV export
  document.getElementById('export-csv').addEventListener('click', () => {
    const headers = ['Date', 'Symbol', 'Side', 'Entry', 'Exit', 'Size', 'PnL', 'R-Multiple', 'Hold Time', 'Exit Reason'];
    const rows = D.CLOSED_TRADES.map(t => [t.date, t.symbol, t.side, t.entry, t.exit, t.size, t.pnl, t.r_multiple, t.hold_time, t.exit_reason]);
    const csv = [headers, ...rows].map(r => r.join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'zora_trades_export.csv';
    a.click();
    URL.revokeObjectURL(url);
  });

  // ===== Brain Charts =====
  const expertCtx = document.getElementById('expert-chart').getContext('2d');
  new Chart(expertCtx, {
    type: 'bar',
    data: {
      labels: Object.keys(D.EXPERT_WEIGHTS),
      datasets: [{
        label: 'Weight',
        data: Object.values(D.EXPERT_WEIGHTS),
        backgroundColor: ['#00d4ff', '#0099bb', '#00b35a', '#a855f7', '#ffa502', '#ff4757'],
        borderRadius: 6,
        borderSkipped: false,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#11131a',
          borderColor: '#232634',
          borderWidth: 1,
          titleColor: '#e8eaf0',
          bodyColor: '#8b8e9b',
          padding: 10,
          cornerRadius: 8,
          callbacks: {
            label: (ctx) => 'Weight: ' + (ctx.parsed.y * 100).toFixed(0) + '%',
          },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { color: '#8b8e9b', font: { family: 'Inter', size: 11 } },
        },
        y: {
          grid: { color: 'rgba(35, 38, 52, 0.6)' },
          ticks: {
            color: '#565867',
            font: { family: 'JetBrains Mono', size: 10 },
            callback: (val) => (val * 100).toFixed(0) + '%',
          },
          max: 0.4,
        },
      },
    },
  });

  const factorCtx = document.getElementById('factor-chart').getContext('2d');
  new Chart(factorCtx, {
    type: 'bar',
    data: {
      labels: Object.keys(D.FACTOR_WEIGHTS),
      datasets: [{
        label: 'Weight',
        data: Object.values(D.FACTOR_WEIGHTS),
        backgroundColor: ['#00d4ff', '#a855f7', '#ffa502', '#ff4757', '#00e87a', '#a855f7'],
        borderRadius: 6,
        borderSkipped: false,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      indexAxis: 'y',
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#11131a',
          borderColor: '#232634',
          borderWidth: 1,
          titleColor: '#e8eaf0',
          bodyColor: '#8b8e9b',
          padding: 10,
          cornerRadius: 8,
          callbacks: {
            label: (ctx) => 'Weight: ' + ctx.parsed.x,
          },
        },
      },
      scales: {
        x: {
          grid: { color: 'rgba(35, 38, 52, 0.6)' },
          ticks: { color: '#565867', font: { family: 'JetBrains Mono', size: 10 } },
        },
        y: {
          grid: { display: false },
          ticks: { color: '#8b8e9b', font: { family: 'Inter', size: 11 } },
        },
      },
    },
  });

  // ===== Brain State JSON =====
  function syntaxHighlight(json) {
    let str = JSON.stringify(json, null, 2);
    str = str.replace(/&/g, '&').replace(/</g, '<').replace(/>/g, '>');
    return str.replace(
      /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+\.?\d*([eE][+-]?\d+)?)/g,
      (match) => {
        let cls = 'json-number';
        if (/^"/.test(match)) cls = /:$/.test(match) ? 'json-key' : 'json-string';
        return '<span class="' + cls + '">' + match + '</span>';
      }
    );
  }
  document.getElementById('brain-state-json').innerHTML = syntaxHighlight(D.BRAIN_STATE);

  // ===== AI Panel =====
  const aiGrid = document.getElementById('ai-providers');
  aiGrid.innerHTML = D.AI_PROVIDERS.map(p => `
    <div class="ai-card">
      <div class="ai-card-header">
        <span class="ai-provider-name">${p.name}</span>
        <span class="ai-status ${p.status}">${p.status}</span>
      </div>
      <div class="ai-verdict">
        <span class="ai-lean ${p.lean.toLowerCase()}">${p.lean}</span>
        <span class="ai-confidence">${p.confidence > 0 ? p.confidence + '% confidence' : ''}</span>
      </div>
      <div class="ai-note">${p.note}</div>
      <div class="ai-latency">
        <span style="color: var(--text-faint)">Model:</span> ${p.model} · 
        <span style="color: var(--text-faint)">Latency:</span> ${p.latency}
      </div>
    </div>
  `).join('');

  // Judge info
  const judgeEl = document.getElementById('judge-info');
  judgeEl.innerHTML = `
    <div style="margin-bottom: 10px;">
      <span style="color: var(--text-muted);">Judge Provider:</span>
      <span class="judge-provider">${D.JUDGE_INFO.provider}</span>
    </div>
    <div style="margin-bottom: 10px;">
      <span style="color: var(--text-muted);">Synthesis:</span>
      <span class="ai-lean buy" style="font-size: 0.75rem;">${D.JUDGE_INFO.synthesis}</span>
    </div>
    <p style="color: var(--text-muted); font-size: 0.8rem; line-height: 1.6; margin-bottom: 10px;">${D.JUDGE_INFO.method}</p>
    <p style="color: var(--text-faint); font-size: 0.75rem;">Fallback: ${D.JUDGE_INFO.fallback}</p>
  `;

  // ===== AI Verdicts Table =====
  const aiVerdictsTable = document.getElementById('ai-verdicts-table');
  aiVerdictsTable.innerHTML = D.AI_VERDICTS.map(v => `
    <tr>
      <td class="mono" style="color: var(--text-faint); font-size: 0.75rem">${v.time}</td>
      <td><strong>${v.symbol}</strong></td>
      <td style="color: var(--text-muted)">${v.provider}</td>
      <td><span class="ai-lean ${v.lean.toLowerCase()}" style="font-size: 0.7rem;">${v.lean}</span></td>
      <td class="mono">${v.confidence > 0 ? v.confidence + '%' : '—'}</td>
      <td style="font-size: 0.75rem; color: var(--text-muted)">${v.note}</td>
      <td class="mono" style="color: var(--text-faint)">${v.latency}</td>
    </tr>
  `).join('');

  // ===== Live equity chart update (simulated) =====
  let chartTick = 0;
  setInterval(() => {
    chartTick++;
    if (chartTick % 5 === 0) {
      // Add a new data point every 15 seconds (simulated live)
      const lastEquity = D.EQUITY_DATA[D.EQUITY_DATA.length - 1].equity;
      const newEquity = lastEquity + (Math.random() - 0.45) * 2;
      const now = new Date();
      equityChart.data.labels.push(now.toISOString().slice(0, 16));
      equityChart.data.datasets[0].data.push(parseFloat(newEquity.toFixed(2)));
      if (equityChart.data.labels.length > 100) {
        equityChart.data.labels.shift();
        equityChart.data.datasets[0].data.shift();
      }
      equityChart.update('none');
    }
  }, 3000);

  console.log('%c⚡ ZORA Dashboard initialized', 'color: #00d4ff; font-weight: bold; font-size: 14px');
  console.log('%cPaper Trading Mode · v0.8.0', 'color: #8b8e9b; font-size: 12px');
})();
