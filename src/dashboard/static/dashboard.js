/* ─── Polymarket Bot Dashboard ─ Interactive Frontend ────────── */
"use strict";

// ─── State ──────────────────────────────────────────────────────
let _configData = {};          // current config from API
let _configDirty = {};         // sections with unsaved edits
let _activeConfigTab = null;   // currently visible config section
let _modalConfirmCb = null;    // callback for confirm modal
let _charts = {};              // Chart.js instances

const $  = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// ─── Tab Navigation ─────────────────────────────────────────────
let _activeTab = localStorage.getItem('dashboardTab') || 'overview';

function switchTab(tabName) {
    _activeTab = tabName;
    localStorage.setItem('dashboardTab', tabName);

    // Update button states
    $$('.tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tabName);
    });

    // Update content visibility
    $$('.tab-content').forEach(content => {
        content.classList.toggle('active', content.dataset.tab === tabName);
    });

    // Scroll to top of content
    window.scrollTo({ top: 0, behavior: 'smooth' });

    // Charts need a resize when their container becomes visible
    requestAnimationFrame(() => {
        Object.values(_charts).forEach(chart => {
            if (chart && chart.canvas && chart.canvas.offsetParent !== null) {
                chart.resize();
            }
        });
    });
}

// Mapping: which update functions belong to which tab
const TAB_UPDATERS = {
    overview: ['updatePortfolio', 'updateCharts', 'updateEquityCurve', 'updateEngineStatus', 'updateDrawdown', 'updateRegime'],
    trading:  ['updatePositions', 'updateCandidates', 'updateForecasts', 'updateTrades'],
    analytics: ['updateAnalytics'],
    whales:   ['updateWhaleTracker'],
    decisions: ['updateDecisionLog'],
    strategies: ['updateStrategiesTab'],
    journal:  ['updateVaR', 'updateWatchlist', 'updateJournal', 'updateEquitySnapshots'],
    admin:    ['updateAdminPanel', 'updateRisk', 'updateAlerts', 'updateAudit', 'updateConfig', 'updateSettingsTab'],
    docs:     [],
};

// ─── Smart DOM Update (skip if unchanged) ───────────────────────
function safeHTML(el, html) {
    if (!el) return;
    if (el.innerHTML === html) return;      // no-op if identical
    el.innerHTML = html;
}

function safeText(el, text) {
    if (!el) return;
    if (el.textContent === text) return;
    el.textContent = text;
}

// ─── Expanded Decision Cards State ─────────────────────────────
const _expandedDecisionKeys = new Set();   // "cycleId-marketId" keys

// ─── Helpers ────────────────────────────────────────────────────
const fmt  = (v, d=2) => Number(v||0).toFixed(d);
const fmtD = (v) => `$${fmt(v)}`;
const fmtP = (v) => `${fmt(v)}%`;
const pnlClass = (v) => v > 0.001 ? 'pnl-positive' : v < -0.001 ? 'pnl-negative' : 'pnl-zero';
const pillClass = (d) => {
    const dl = (d||'').toLowerCase();
    if (dl === 'trade' || dl === 'buy') return 'pill-trade';
    if (dl === 'no trade' || dl === 'sell') return 'pill-no-trade';
    if (dl === 'filled') return 'pill-filled';
    return 'pill-dry';
};
const shortDate = (iso) => {
    if (!iso) return '—';
    const d = new Date(iso);
    return d.toLocaleString('en-US', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
};

// Animated number counter for card values
function animateValue(el, end, prefix='', suffix='', decimals=2) {
    if (!el) return;
    const start = parseFloat(el.dataset.currentValue || '0');
    const target = parseFloat(end) || 0;
    if (Math.abs(start - target) < 0.001) return;
    el.dataset.currentValue = target;
    const duration = 400;
    const startTime = performance.now();
    function tick(now) {
        const progress = Math.min((now - startTime) / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3); // easeOutCubic
        const current = start + (target - start) * eased;
        el.textContent = `${prefix}${current.toFixed(decimals)}${suffix}`;
        if (progress < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
}

/* Extract API key from page URL so all fetches are authenticated */
const _API_KEY = new URLSearchParams(window.location.search).get('api_key') || '';

/** Wrap native fetch to always include api_key */
function authFetch(url, opts) {
    const sep = url.includes('?') ? '&' : '?';
    const authUrl = _API_KEY ? `${url}${sep}api_key=${encodeURIComponent(_API_KEY)}` : url;
    return fetch(authUrl, opts);
}

async function apiFetch(url, opts = {}) {
    try {
        const res = await authFetch(url, opts);
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        return await res.json();
    } catch (e) {
        console.error(`API ${url}:`, e);
        return null;
    }
}

// ─── Toast Notifications ────────────────────────────────────────
function showToast(message, type='info') {
    const container = $('#toast-container');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    const icons = {success:'✅',error:'❌',info:'ℹ️',warning:'⚠️'};
    toast.innerHTML = `<span class="toast-icon">${icons[type]||'ℹ️'}</span><span class="toast-msg">${message}</span>`;
    container.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add('toast-show'));
    setTimeout(() => {
        toast.classList.remove('toast-show');
        toast.classList.add('toast-hide');
        setTimeout(() => toast.remove(), 400);
    }, 3500);
}

// ─── Confirm Modal ──────────────────────────────────────────────
function showConfirmModal(title, message, onConfirm) {
    $('#modal-title').textContent = title;
    $('#modal-message').textContent = message;
    _modalConfirmCb = onConfirm;
    $('#modal-overlay').style.display = 'flex';
    const confirmBtn = $('#modal-confirm');
    // clone to remove old listeners
    const newBtn = confirmBtn.cloneNode(true);
    confirmBtn.parentNode.replaceChild(newBtn, confirmBtn);
    newBtn.id = 'modal-confirm';
    newBtn.addEventListener('click', () => {
        closeModal();
        if (_modalConfirmCb) _modalConfirmCb();
    });
}
function closeModal() {
    $('#modal-overlay').style.display = 'none';
    _modalConfirmCb = null;
}

// ─── Position Detail Modal ──────────────────────────────────────
async function openPositionDetail(marketId) {
    const overlay = $('#position-detail-overlay');
    const body = $('#pos-detail-body');
    const badges = $('#pos-detail-badges');
    const footer = $('#pos-detail-footer');
    const titleEl = $('#pos-detail-question');

    titleEl.textContent = 'Loading…';
    safeHTML(badges, '');
    safeHTML(body, '<div class="pos-detail-loading">Loading position data…</div>');
    safeHTML(footer, '');
    overlay.style.display = 'flex';

    const d = await apiFetch(`/api/positions/${encodeURIComponent(marketId)}`);
    if (!d || d.error) {
        safeHTML(body, `<div class="pos-detail-loading">❌ ${d?.error || 'Failed to load position data'}</div>`);
        titleEl.textContent = 'Error';
        return;
    }

    // Header
    titleEl.textContent = d.question || d.market_id;
    const dirBuy = d.direction === 'BUY_YES' || d.direction === 'BUY';
    safeHTML(badges, `
        <span class="pos-detail-badge ${dirBuy ? 'badge-direction-buy' : 'badge-direction-sell'}">${d.direction || '—'}</span>
        <span class="pos-detail-badge badge-type">${d.market_type || '—'}</span>
        ${d.category && d.category !== '—' ? `<span class="pos-detail-badge badge-cat">${d.category}</span>` : ''}
    `);

    // Build body sections
    const pnlCls = d.pnl > 0.001 ? 'pnl-positive' : d.pnl < -0.001 ? 'pnl-negative' : 'pnl-zero';
    const priceCls = d.price_change > 0 ? 'pnl-positive' : d.price_change < 0 ? 'pnl-negative' : 'pnl-zero';

    // TP/SL bar colors & widths
    const slPct = Math.min((d.sl_proximity || 0) * 100, 100);
    const tpPct = Math.min((d.tp_proximity || 0) * 100, 100);
    const holdPct = Math.min(d.holding_pct || 0, 100);
    const holdBarColor = holdPct > 80 ? 'bar-red' : holdPct > 50 ? 'bar-orange' : 'bar-blue';

    // Forecast section
    const fc = d.forecast;
    let forecastHTML = '<div class="pd-section"><div class="pd-section-title">🔮 Latest Forecast</div><div style="color:var(--text-muted);font-size:0.82rem;">No forecast data available</div></div>';
    if (fc) {
        const edgeCls = (fc.edge||0) > 0 ? 'pnl-positive' : 'pnl-negative';
        forecastHTML = `
        <div class="pd-section">
            <div class="pd-section-title">🔮 Latest Forecast</div>
            <div class="pd-grid">
                <div class="pd-field"><span class="pd-label">Model Prob</span><span class="pd-value">${fmtP((fc.model_probability||0)*100)}</span></div>
                <div class="pd-field"><span class="pd-label">Implied Prob</span><span class="pd-value">${fmtP((fc.implied_probability||0)*100)}</span></div>
                <div class="pd-field"><span class="pd-label">Edge</span><span class="pd-value ${edgeCls}">${fmtP((fc.edge||0)*100)}</span></div>
                <div class="pd-field"><span class="pd-label">Confidence</span><span class="pd-value">${fc.confidence_level||'—'}</span></div>
                <div class="pd-field"><span class="pd-label">Evidence Quality</span><span class="pd-value">${fmt(fc.evidence_quality||0, 3)}</span></div>
                <div class="pd-field"><span class="pd-label">Sources</span><span class="pd-value">${fc.num_sources||0}</span></div>
            </div>
            ${fc.reasoning ? `<div class="pd-reasoning">${escapeHTML(fc.reasoning)}</div>` : ''}
        </div>`;
    }

    // Entry trade section
    let entryTradeHTML = '';
    if (d.entry_trade) {
        const et = d.entry_trade;
        entryTradeHTML = `
        <div class="pd-section">
            <div class="pd-section-title">📝 Entry Trade</div>
            <div class="pd-grid">
                <div class="pd-field"><span class="pd-label">Side</span><span class="pd-value">${et.side||'—'}</span></div>
                <div class="pd-field"><span class="pd-label">Price</span><span class="pd-value">${fmt(et.price||0, 4)}</span></div>
                <div class="pd-field"><span class="pd-label">Size</span><span class="pd-value">${fmt(et.size||0, 1)}</span></div>
                <div class="pd-field"><span class="pd-label">Stake</span><span class="pd-value">${fmtD(et.stake_usd||0)}</span></div>
                <div class="pd-field"><span class="pd-label">Status</span><span class="pd-value">${et.status||'—'}</span></div>
                <div class="pd-field"><span class="pd-label">Mode</span><span class="pd-value">${et.dry_run ? 'Paper' : 'Live'}</span></div>
            </div>
        </div>`;
    }

    const timeRem = d.time_remaining_hours != null
        ? (d.time_remaining_hours >= 24 ? `${(d.time_remaining_hours/24).toFixed(1)}d` : `${d.time_remaining_hours.toFixed(1)}h`)
        : '—';

    safeHTML(body, `
        <!-- Price & P&L -->
        <div class="pd-section">
            <div class="pd-section-title">💰 Price & P&L</div>
            <div class="pd-grid">
                <div class="pd-field"><span class="pd-label">Entry Price</span><span class="pd-value big">${fmt(d.entry_price, 4)}</span></div>
                <div class="pd-field"><span class="pd-label">Current Price</span><span class="pd-value big ${priceCls}">${fmt(d.current_price, 4)}</span></div>
                <div class="pd-field"><span class="pd-label">Price Change</span><span class="pd-value ${priceCls}">${d.price_change >= 0 ? '+' : ''}${fmt(d.price_change, 4)} (${fmtP(d.price_change_pct)})</span></div>
                <div class="pd-field"><span class="pd-label">P&L</span><span class="pd-value big ${pnlCls}">${d.pnl >= 0 ? '+' : ''}${fmtD(d.pnl)}</span></div>
                <div class="pd-field"><span class="pd-label">P&L %</span><span class="pd-value ${pnlCls}">${d.pnl_pct >= 0 ? '+' : ''}${fmtP(d.pnl_pct)}</span></div>
            </div>
            <div class="pd-grid" style="margin-top:10px;">
                <div class="pd-field"><span class="pd-label">Size</span><span class="pd-value">${fmt(d.size, 2)}</span></div>
                <div class="pd-field"><span class="pd-label">Stake</span><span class="pd-value">${fmtD(d.stake_usd)}</span></div>
            </div>
        </div>

        <!-- TP/SL Proximity -->
        <div class="pd-section">
            <div class="pd-section-title">🎯 Risk Levels & Proximity</div>

            <div class="pd-bar-group">
                <div class="pd-bar-header">
                    <span class="pd-bar-label">🟢 Take Profit (${fmtP(d.take_profit_pct * 100)})</span>
                    <span class="pd-bar-value pnl-positive">${fmtP(tpPct)} reached</span>
                </div>
                <div class="pd-bar-track"><div class="pd-bar-fill bar-green" style="width:${tpPct}%"></div></div>
                <div class="pd-bar-sub">TP triggers at ${fmtP(d.tp_trigger_pnl_pct)} P&L${d.tp_price != null ? ` · TP price: ${fmt(d.tp_price, 4)}` : ''} · Current P&L: ${fmtP(d.pnl_pct)}</div>
            </div>

            <div class="pd-bar-group">
                <div class="pd-bar-header">
                    <span class="pd-bar-label">🔴 Stop Loss (${fmtP(d.stop_loss_pct * 100)})</span>
                    <span class="pd-bar-value pnl-negative">${fmtP(slPct)} reached</span>
                </div>
                <div class="pd-bar-track"><div class="pd-bar-fill bar-red" style="width:${slPct}%"></div></div>
                <div class="pd-bar-sub">SL triggers at ${fmtP(d.sl_trigger_pnl_pct)} P&L${d.sl_price != null ? ` · SL price: ${fmt(d.sl_price, 4)}` : ''} · Current P&L: ${fmtP(d.pnl_pct)}</div>
            </div>

            <div class="pd-bar-group">
                <div class="pd-bar-header">
                    <span class="pd-bar-label">⏱️ Holding Period (${d.max_holding_hours}h max)</span>
                    <span class="pd-bar-value" style="color:${holdPct > 80 ? 'var(--accent-red)' : holdPct > 50 ? 'var(--accent-orange)' : 'var(--accent-blue)'}">${fmtP(holdPct)}</span>
                </div>
                <div class="pd-bar-track"><div class="pd-bar-fill ${holdBarColor}" style="width:${holdPct}%"></div></div>
                <div class="pd-bar-sub">Held: ${d.holding_label} · Remaining: ${timeRem}</div>
            </div>
        </div>

        <!-- Market Info -->
        <div class="pd-section">
            <div class="pd-section-title">📊 Market Info</div>
            <div class="pd-grid">
                <div class="pd-field"><span class="pd-label">Volume</span><span class="pd-value">${fmtD(d.volume)}</span></div>
                <div class="pd-field"><span class="pd-label">Liquidity</span><span class="pd-value">${fmtD(d.liquidity)}</span></div>
                <div class="pd-field"><span class="pd-label">End Date</span><span class="pd-value" style="font-size:0.8rem;">${d.end_date || '—'}</span></div>
                <div class="pd-field"><span class="pd-label">Resolution</span><span class="pd-value" style="font-size:0.8rem;">${d.resolution_source || '—'}</span></div>
            </div>
        </div>

        ${forecastHTML}
        ${entryTradeHTML}
    `);

    // Footer
    safeHTML(footer, `
        <a href="${d.polymarket_url}" target="_blank" rel="noopener">🔗 View on Polymarket ↗</a>
        <span class="pos-detail-id" title="${d.market_id}">${d.market_id}</span>
    `);
}

function escapeHTML(str) {
    const div = document.createElement('div');
    div.textContent = str || '';
    return div.innerHTML;
}

function closePositionDetail(event) {
    // If called from overlay click, only close if clicking the overlay itself
    if (event && event.target !== event.currentTarget) return;
    $('#position-detail-overlay').style.display = 'none';
}

// Close position detail on Escape key
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        const overlay = $('#position-detail-overlay');
        if (overlay && overlay.style.display === 'flex') {
            overlay.style.display = 'none';
        }
    }
});

// ─── Table Search / Filter ──────────────────────────────────────
function filterTable(tbodyId, query) {
    const tbody = document.getElementById(tbodyId);
    if (!tbody) return;
    const q = query.toLowerCase().trim();
    const rows = tbody.querySelectorAll('tr');
    rows.forEach(row => {
        if (row.querySelector('.empty-state')) { row.style.display = q ? 'none' : ''; return; }
        const text = row.textContent.toLowerCase();
        row.style.display = text.includes(q) ? '' : 'none';
    });
}

// ─── Data Export ────────────────────────────────────────────────
async function exportData(table) {
    showToast(`Exporting ${table}…`, 'info');
    const data = await apiFetch(`/api/export/${table}`);
    if (!data || !data.rows) { showToast('Export failed', 'error'); return; }
    if (data.rows.length === 0) { showToast('No data to export', 'warning'); return; }

    // Convert to CSV
    const keys = Object.keys(data.rows[0]);
    const csvRows = [keys.join(',')];
    for (const row of data.rows) {
        csvRows.push(keys.map(k => {
            let v = row[k] ?? '';
            if (typeof v === 'string' && (v.includes(',') || v.includes('"') || v.includes('\n'))) {
                v = `"${v.replace(/"/g, '""')}"`;
            }
            return v;
        }).join(','));
    }
    const blob = new Blob([csvRows.join('\n')], {type:'text/csv'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `${table}_export.csv`; a.click();
    URL.revokeObjectURL(url);
    showToast(`Exported ${data.rows.length} rows`, 'success');
}

// ─── Kill Switch ────────────────────────────────────────────────
function confirmKillSwitch(enable) {
    if (enable) {
        showConfirmModal('Activate Kill Switch',
            'This will immediately halt all trading. Are you sure?',
            toggleKillSwitch);
    } else {
        showConfirmModal('Deactivate Kill Switch',
            'Trading will resume on next cycle. Are you sure?',
            toggleKillSwitch);
    }
}

async function toggleKillSwitch() {
    const data = await apiFetch('/api/kill-switch', {method:'POST'});
    if (data) {
        showToast(data.message, data.kill_switch ? 'warning' : 'success');
        updateRisk();
        updateDrawdown();
    }
}

// ═══════════════════════════════════════════════════════════════
//  DATA REFRESH FUNCTIONS
// ═══════════════════════════════════════════════════════════════

// ─── Portfolio Cards ────────────────────────────────────────────
async function updatePortfolio() {
    const d = await apiFetch('/api/portfolio');
    if (!d) return;
    $('#bankroll').textContent   = fmtD(d.bankroll);
    $('#available-capital').textContent = `Available: ${fmtD(d.available_capital)}`;

    const pnlEl = $('#total-pnl');
    pnlEl.textContent = fmtD(d.total_pnl);
    pnlEl.className   = `card-value ${pnlClass(d.total_pnl)}`;
    const pnlSub = `Realized: ${fmtD(d.realized_pnl || 0)} | Unrealized: ${fmtD(d.unrealized_pnl)}`;
    const pnlBest = d.best_pnl ? ` | Best: ${fmtD(d.best_pnl)}` : '';
    const pnlWorst = d.worst_pnl ? ` | Worst: ${fmtD(d.worst_pnl)}` : '';
    $('#unrealized-pnl').textContent = d.open_positions > 0
        ? pnlSub + pnlBest + pnlWorst
        : pnlSub;

    $('#open-positions').textContent = d.open_positions;
    $('#total-invested').textContent = `Invested: ${fmtD(d.total_invested)}`;

    $('#total-trades').textContent   = d.total_trades;
    $('#trade-breakdown').textContent = `Live: ${d.live_trades} | Paper: ${d.paper_trades}`;

    $('#avg-edge').textContent = fmtP(d.avg_edge * 100);
    $('#avg-evidence-quality').textContent = `Avg EQ: ${fmt(d.avg_evidence_quality, 3)}`;

    $('#today-trades').textContent = `${d.today_trades} trades`;
    $('#daily-volume').textContent = `Volume: ${fmtD(d.daily_volume)}`;

    // Mode badge
    const modeBadge = $('#mode-badge');
    if (d.live_trading_enabled && !d.dry_run) {
        modeBadge.textContent = 'LIVE'; modeBadge.className = 'badge badge-live';
    } else {
        modeBadge.textContent = 'PAPER MODE'; modeBadge.className = 'badge badge-paper';
    }
}

// ─── Risk Monitor ───────────────────────────────────────────────
async function updateRisk() {
    const d = await apiFetch('/api/risk');
    if (!d) return;

    // Daily exposure bar
    const dailyPct = d.current.daily_loss_pct;
    const dailyBar = $('#risk-bar-daily');
    dailyBar.style.width = `${Math.min(dailyPct,100)}%`;
    dailyBar.className = `risk-bar ${dailyPct > 80 ? 'danger' : dailyPct > 50 ? 'warning' : ''}`;
    $('#risk-daily-current').textContent = fmtD(d.current.daily_exposure);
    $('#risk-daily-limit').textContent   = fmtD(d.limits.max_daily_loss);

    // Positions bar
    const posPct = d.current.positions_pct;
    const posBar = $('#risk-bar-positions');
    posBar.style.width = `${Math.min(posPct,100)}%`;
    posBar.className = `risk-bar ${posPct > 80 ? 'danger' : posPct > 60 ? 'warning' : ''}`;
    $('#risk-pos-current').textContent = d.current.open_positions;
    $('#risk-pos-limit').textContent   = d.limits.max_open_positions;

    // Evidence quality bar (inverted — higher is better)
    const eqMin = d.forecasting.min_evidence_quality;
    const eqCur = d.current.avg_evidence_quality;
    const eqPct = eqMin > 0 ? Math.min((eqCur / eqMin) * 100, 100) : 0;
    const eqBar = $('#risk-bar-eq');
    eqBar.style.width = `${eqPct}%`;
    $('#risk-eq-current').textContent = fmt(eqCur, 3);
    $('#risk-eq-limit').textContent   = fmt(eqMin, 3);

    // Kill switch
    const ksBadge = $('#kill-switch-badge');
    const btnOn   = $('#btn-kill-on');
    const btnOff  = $('#btn-kill-off');
    if (d.kill_switch) {
        ksBadge.textContent = '🛑 KILL SWITCH ON'; ksBadge.className = 'badge badge-danger'; ksBadge.style.display = '';
        btnOn.style.display = 'none'; btnOff.style.display = '';
    } else {
        ksBadge.style.display = 'none';
        btnOn.style.display = ''; btnOff.style.display = 'none';
    }

    // Risk params pills
    const params = $('#risk-params');
    safeHTML(params, [
        ['Max Stake', fmtD(d.limits.max_stake_per_market)],
        ['Bankroll Frac', fmtP(d.limits.max_bankroll_fraction * 100)],
        ['Min Edge', fmtP(d.limits.min_edge * 100)],
        ['Kelly Frac', fmt(d.limits.kelly_fraction, 3)],
        ['Min Liquidity', fmtD(d.limits.min_liquidity)],
        ['Max Spread', fmtP(d.limits.max_spread * 100)],
        ['Slippage Tol', fmtP(d.execution.slippage_tolerance * 100)],
        ['LLM Model', d.forecasting.llm_model || '—'],
    ].map(([l,v]) => `<div class="risk-param"><div class="rp-label">${l}</div><div class="rp-value">${v}</div></div>`).join(''));
}

// ─── Positions Table ────────────────────────────────────────────
async function updatePositions() {
    const d = await apiFetch('/api/positions');
    if (!d) return;
    const tbody = $('#positions-body');
    if (!d.positions || d.positions.length === 0) {
        safeHTML(tbody, '<tr><td colspan="11" class="empty-state">No active positions</td></tr>');
        // Update summary strip
        const strip = $('#positions-summary');
        if (strip) safeHTML(strip, '');
        return;
    }

    // Positions summary strip
    const s = d.summary || {};
    const strip = $('#positions-summary');
    if (strip) {
        safeHTML(strip, `
            <div class="pnl-summary-strip">
                <span class="pnl-stat">📊 <strong>${s.count||0}</strong> positions</span>
                <span class="pnl-stat ${pnlClass(s.total_pnl)}">P&L: <strong>${fmtD(s.total_pnl)}</strong> (${fmtP(s.pnl_pct)})</span>
                <span class="pnl-stat">Invested: <strong>${fmtD(s.total_invested)}</strong></span>
                <span class="pnl-stat pnl-positive">✅ ${s.winners||0}W</span>
                <span class="pnl-stat pnl-negative">❌ ${s.losers||0}L</span>
                <span class="pnl-stat pnl-zero">➖ ${s.flat||0}F</span>
            </div>
        `);
    }

    safeHTML(tbody, d.positions.map(p => {
        const pnl = p.pnl || 0;
        const pnlPct = p.pnl_pct || 0;
        const priceChange = p.price_change || 0;
        const priceChangePct = p.price_change_pct || 0;
        const arrow = priceChange > 0.001 ? '▲' : priceChange < -0.001 ? '▼' : '─';
        const arrowClass = priceChange > 0.001 ? 'price-up' : priceChange < -0.001 ? 'price-down' : 'price-flat';
        const hoursHeld = p.hours_held || 0;
        const timeLabel = hoursHeld >= 24 ? `${(hoursHeld/24).toFixed(1)}d` : `${hoursHeld.toFixed(1)}h`;

        return `<tr class="position-row ${pnlClass(pnl)}" onclick="openPositionDetail('${(p.market_id||'').replace(/'/g, "\\'")}')">
            <td title="${p.market_id}">${(p.question||p.market_id||'').substring(0,50)}</td>
            <td>${p.market_type||'—'}</td>
            <td><span class="pill ${p.direction==='BUY_YES'||p.direction==='BUY'?'pill-buy':'pill-sell'}">${p.direction||'—'}</span></td>
            <td>${fmt(p.entry_price,3)}</td>
            <td>
                <span class="live-price">${fmt(p.current_price,3)}</span>
                <span class="price-arrow ${arrowClass}">${arrow}</span>
            </td>
            <td>${fmt(p.size,1)}</td>
            <td>${fmtD(p.stake_usd)}</td>
            <td class="${pnlClass(pnl)}">
                <span class="pnl-value">${fmtD(pnl)}</span>
            </td>
            <td class="${pnlClass(pnlPct)}">
                <span class="pnl-badge ${pnl > 0 ? 'pnl-badge-pos' : pnl < 0 ? 'pnl-badge-neg' : 'pnl-badge-flat'}">${fmtP(pnlPct)}</span>
            </td>
            <td class="${arrowClass}">
                ${fmtP(priceChangePct)}
            </td>
            <td title="${p.opened_at||''}">${timeLabel}</td>
        </tr>`;
    }).join(''));
}

// ─── Forecasts Table ────────────────────────────────────────────
async function updateForecasts() {
    const d = await apiFetch('/api/forecasts');
    if (!d) return;
    const tbody = $('#forecasts-body');
    if (!d.forecasts || d.forecasts.length === 0) {
        safeHTML(tbody, '<tr><td colspan="10" class="empty-state">No forecasts yet</td></tr>');
        return;
    }
    safeHTML(tbody, d.forecasts.map(f => `<tr>
        <td title="${f.question||''}">${(f.question||f.market_id||'').substring(0,50)}</td>
        <td>${f.market_type||'—'}</td>
        <td>${fmtP((f.implied_probability||0)*100)}</td>
        <td>${fmtP((f.model_probability||0)*100)}</td>
        <td class="${pnlClass(f.edge)}">${fmtP((f.edge||0)*100)}</td>
        <td>${fmt(f.evidence_quality,3)}</td>
        <td>${f.num_sources||f.evidence_count||0}</td>
        <td>${f.confidence_level||'—'}</td>
        <td><span class="pill ${pillClass(f.decision)}">${f.decision||'—'}</span></td>
        <td>${shortDate(f.created_at)}</td>
    </tr>`).join(''));
}

// ─── Trades Table (Enhanced) ────────────────────────────────────
let _allTrades = [];  // Store for client-side filtering

function tradeStatusBadge(status) {
    const map = {
        'ACTIVE':   '<span class="trade-badge trade-active">🟢 Active</span>',
        'TP_HIT':   '<span class="trade-badge trade-tp">🎯 TP Hit</span>',
        'SL_HIT':   '<span class="trade-badge trade-sl">🛑 SL Hit</span>',
        'RESOLVED': '<span class="trade-badge trade-resolved">📋 Resolved</span>',
        'TIME_EXIT':'<span class="trade-badge trade-time">⏰ Time Exit</span>',
        'CLOSED':   '<span class="trade-badge trade-closed">🔒 Closed</span>',
        'ENTRY':    '<span class="trade-badge trade-entry">📝 Entry</span>',
    };
    return map[status] || `<span class="trade-badge">${status||'—'}</span>`;
}

function formatDuration(hours) {
    if (!hours && hours !== 0) return '—';
    if (hours < 1) return `${Math.round(hours*60)}m`;
    if (hours < 24) return `${hours.toFixed(1)}h`;
    const days = Math.floor(hours / 24);
    const rem = hours % 24;
    return `${days}d ${Math.round(rem)}h`;
}

function renderTradeRow(t) {
    const pnl = t.pnl != null ? t.pnl : null;
    const pnlCls = pnl > 0 ? 'pnl-positive' : pnl < 0 ? 'pnl-negative' : 'pnl-zero';
    const pnlStr = pnl != null ? (pnl >= 0 ? '+' : '') + fmtD(pnl) : '—';
    const pnlPctStr = t.pnl_pct != null ? (t.pnl_pct >= 0 ? '+' : '') + t.pnl_pct.toFixed(2) + '%' : '—';
    const entry = t.entry_price != null ? fmt(t.entry_price, 3) : '—';
    const exitVal = t.trade_status === 'ACTIVE'
        ? `<span style="color:var(--accent-blue)">${fmt(t.current_price||0,3)}</span>`
        : (t.exit_price != null ? fmt(t.exit_price, 3) : '—');
    const dirCls = (t.direction||'').toUpperCase() === 'YES' ? 'pill-buy' : 'pill-sell';
    const reasonLabel = t.close_reason_label || (t.trade_status === 'ACTIVE' ? '—' : '—');
    const isActive = t.trade_status === 'ACTIVE';

    // TP/SL proximity bar for active trades
    let proximityHtml = '';
    if (isActive && (t.sl_proximity > 0 || t.tp_proximity > 0)) {
        if (t.sl_proximity > 0) proximityHtml += `<div class="prox-bar prox-sl" style="width:${Math.min(t.sl_proximity*100,100)}%"></div>`;
        if (t.tp_proximity > 0) proximityHtml += `<div class="prox-bar prox-tp" style="width:${Math.min(t.tp_proximity*100,100)}%"></div>`;
        proximityHtml = `<div class="prox-wrap">${proximityHtml}</div>`;
    }

    return `<tr class="trade-row trade-row-clickable ${isActive ? 'trade-row-active' : ''}" data-status="${t.trade_status}" data-market-id="${t.market_id}" onclick="openTradeDetail('${t.market_id}')">
        <td title="${t.question||''}">${(t.question||t.market_id||'').substring(0,55)}${(t.question||'').length>55?'…':''}</td>
        <td><span class="pill ${dirCls}">${t.direction||'—'}</span></td>
        <td style="font-family:var(--font-mono)">${entry}</td>
        <td style="font-family:var(--font-mono)">${exitVal}</td>
        <td style="font-family:var(--font-mono)">${fmtD(t.stake_usd)}</td>
        <td class="${pnlCls}" style="font-weight:700;font-family:var(--font-mono)">${pnlStr}${proximityHtml}</td>
        <td class="${pnlCls}" style="font-family:var(--font-mono)">${pnlPctStr}</td>
        <td>${tradeStatusBadge(t.trade_status)}</td>
        <td>${reasonLabel}</td>
        <td>${formatDuration(t.hours_held)}</td>
        <td>${t.is_paper ? '🧪 Paper' : '💰 Live'}</td>
        <td>${shortDate(t.opened_at)}</td>
    </tr>`;
}

async function updateTrades() {
    const d = await apiFetch('/api/trades');
    if (!d) return;
    const tbody = $('#trades-body');
    const summaryDiv = $('#trades-summary');

    if (!d.trades || d.trades.length === 0) {
        safeHTML(tbody, '<tr><td colspan="12" class="empty-state">No trades yet — start the engine to generate trades</td></tr>');
        if (summaryDiv) summaryDiv.style.display = 'none';
        return;
    }

    _allTrades = d.trades;

    // Render summary strip
    if (d.summary && summaryDiv) {
        const s = d.summary;
        summaryDiv.style.display = 'flex';
        const pnlCls = s.total_pnl >= 0 ? 'pnl-positive' : 'pnl-negative';
        const pnlSign = s.total_pnl >= 0 ? '+' : '';
        safeHTML($('#ts-total'), `📊 Total: <strong>${s.total_trades}</strong>`);
        safeHTML($('#ts-active'), `🟢 Active: <strong>${s.active_count}</strong>`);
        safeHTML($('#ts-closed'), `Closed: <strong>${s.closed_count}</strong>`);
        safeHTML($('#ts-winrate'), `🎯 Win Rate: <strong>${s.win_rate}%</strong> (${s.winners}W / ${s.losers}L)`);
        safeHTML($('#ts-pnl'), `💰 P&L: <strong class="${pnlCls}">${pnlSign}$${Math.abs(s.total_pnl).toFixed(2)}</strong> (${s.pnl_pct>=0?'+':''}${s.pnl_pct}%)`);
        safeHTML($('#ts-tp'), `🟢 TP: <strong>${s.tp_hits}</strong>`);
        safeHTML($('#ts-sl'), `🔴 SL: <strong>${s.sl_hits}</strong>`);
        safeHTML($('#ts-avghold'), `⏱ Avg: <strong>${formatDuration(s.avg_hold_hours)}</strong>`);
        safeHTML($('#ts-best'), `🏆 Best: <strong class="pnl-positive">+$${Math.abs(s.best_trade).toFixed(2)}</strong>`);
        safeHTML($('#ts-worst'), `📉 Worst: <strong class="pnl-negative">-$${Math.abs(s.worst_trade).toFixed(2)}</strong>`);
    }

    // Render rows
    safeHTML(tbody, d.trades.map(renderTradeRow).join(''));
}

function filterTradesByStatus() {
    const sel = document.getElementById('trade-filter-status');
    if (!sel) return;
    const status = sel.value;
    const tbody = $('#trades-body');
    if (!_allTrades.length) return;
    const filtered = status === 'all' ? _allTrades : _allTrades.filter(t => t.trade_status === status);
    if (filtered.length === 0) {
        safeHTML(tbody, `<tr><td colspan="12" class="empty-state">No ${status} trades</td></tr>`);
    } else {
        safeHTML(tbody, filtered.map(renderTradeRow).join(''));
    }
}

// ─── Trade Detail Modal ─────────────────────────────────────────
async function openTradeDetail(marketId) {
    const overlay = $('#trade-detail-overlay');
    const body = $('#td-body');
    const badges = $('#td-badges');
    const footer = $('#td-footer');
    const titleEl = $('#td-question');

    titleEl.textContent = 'Loading…';
    safeHTML(badges, '');
    safeHTML(body, '<div class="pos-detail-loading">Loading trade data…</div>');
    safeHTML(footer, '');
    overlay.style.display = 'flex';

    const d = await apiFetch(`/api/trade-detail/${encodeURIComponent(marketId)}`);
    if (!d || d.error) {
        safeHTML(body, `<div class="pos-detail-loading">❌ ${d?.error || 'Failed to load trade data'}</div>`);
        titleEl.textContent = 'Error';
        return;
    }

    // ── Header ──────────────────────────────────────────────
    titleEl.textContent = d.question || d.market_id;
    const dirBuy = (d.direction||'').includes('BUY') || (d.direction||'').includes('YES');
    safeHTML(badges, `
        ${tradeStatusBadge(d.trade_status)}
        <span class="pos-detail-badge ${dirBuy ? 'badge-direction-buy' : 'badge-direction-sell'}">${d.direction || '—'}</span>
        <span class="pos-detail-badge badge-type">${d.market_type || '—'}</span>
        ${d.category && d.category !== '—' ? `<span class="pos-detail-badge badge-cat">${d.category}</span>` : ''}
        <span class="pos-detail-badge" style="background:rgba(255,255,255,0.06);color:var(--text-muted)">${d.is_paper ? '🧪 Paper' : '💰 Live'}</span>
    `);

    // ── Build body sections ─────────────────────────────────
    const pnlCls = d.pnl > 0.001 ? 'pnl-positive' : d.pnl < -0.001 ? 'pnl-negative' : 'pnl-zero';
    const priceCls = (d.price_change||0) > 0 ? 'pnl-positive' : (d.price_change||0) < 0 ? 'pnl-negative' : 'pnl-zero';
    const isActive = d.trade_status === 'ACTIVE';
    const isClosed = !isActive && d.trade_status !== 'ENTRY';

    // ── Outcome Banner (for closed trades) ──────────────────
    let outcomeBanner = '';
    if (isClosed) {
        const outcomeIcon = d.pnl >= 0 ? '🏆' : '📉';
        const outcomeCls = d.pnl >= 0 ? 'td-outcome-win' : 'td-outcome-loss';
        outcomeBanner = `
        <div class="td-outcome-banner ${outcomeCls}">
            <div class="td-outcome-icon">${outcomeIcon}</div>
            <div class="td-outcome-details">
                <div class="td-outcome-label">${d.close_reason_label || 'Closed'}</div>
                <div class="td-outcome-pnl">${d.pnl >= 0 ? '+' : ''}$${Math.abs(d.pnl).toFixed(4)} (${d.pnl_pct >= 0 ? '+' : ''}${d.pnl_pct.toFixed(2)}%)</div>
            </div>
            <div class="td-outcome-duration">Held for ${formatDuration(d.hours_held)}</div>
        </div>`;
    }

    // ── Price & P&L Section ─────────────────────────────────
    const exitLabel = isActive ? 'Current Price' : 'Exit Price';
    const exitVal = isActive ? d.current_price : d.exit_price;
    const priceHTML = `
    <div class="pd-section">
        <div class="pd-section-title">💰 Price & P&L</div>
        <div class="pd-grid">
            <div class="pd-field"><span class="pd-label">Entry Price</span><span class="pd-value big">${fmt(d.entry_price, 4)}</span></div>
            <div class="pd-field"><span class="pd-label">${exitLabel}</span><span class="pd-value big ${priceCls}">${exitVal != null ? fmt(exitVal, 4) : '—'}</span></div>
            <div class="pd-field"><span class="pd-label">Price Change</span><span class="pd-value ${priceCls}">${(d.price_change||0) >= 0 ? '+' : ''}${fmt(d.price_change||0, 4)} (${(d.price_change_pct||0) >= 0 ? '+' : ''}${fmtP(d.price_change_pct||0)})</span></div>
            <div class="pd-field"><span class="pd-label">P&L</span><span class="pd-value big ${pnlCls}">${d.pnl >= 0 ? '+' : ''}${fmtD(d.pnl)}</span></div>
            <div class="pd-field"><span class="pd-label">P&L %</span><span class="pd-value ${pnlCls}">${d.pnl_pct >= 0 ? '+' : ''}${fmtP(d.pnl_pct)}</span></div>
            <div class="pd-field"><span class="pd-label">Size</span><span class="pd-value">${fmt(d.size, 2)} shares</span></div>
            <div class="pd-field"><span class="pd-label">Stake</span><span class="pd-value">${fmtD(d.stake_usd)}</span></div>
        </div>
    </div>`;

    // ── TP/SL Risk Levels ───────────────────────────────────
    const slPct = Math.min((d.sl_proximity||0)*100, 100);
    const tpPct = Math.min((d.tp_proximity||0)*100, 100);
    const holdPct = Math.min(d.holding_pct||0, 100);
    const holdBarColor = holdPct > 80 ? 'bar-red' : holdPct > 50 ? 'bar-orange' : 'bar-blue';

    let riskHTML = '';
    if (isActive) {
        const timeRem = d.time_remaining_hours != null
            ? (d.time_remaining_hours >= 24 ? `${(d.time_remaining_hours/24).toFixed(1)}d` : `${d.time_remaining_hours.toFixed(1)}h`)
            : '—';
        riskHTML = `
        <div class="pd-section">
            <div class="pd-section-title">🎯 Risk Levels & Proximity</div>
            <div class="pd-bar-group">
                <div class="pd-bar-header">
                    <span class="pd-bar-label">🟢 Take Profit (${fmtP(d.tp_pct * 100)})</span>
                    <span class="pd-bar-value pnl-positive">${fmtP(tpPct)} reached</span>
                </div>
                <div class="pd-bar-track"><div class="pd-bar-fill bar-green" style="width:${tpPct}%"></div></div>
                <div class="pd-bar-sub">TP at ${fmtP(d.tp_trigger_pnl_pct)} P&L${d.tp_price != null ? ` · Price: ${fmt(d.tp_price, 4)}` : ''}</div>
            </div>
            <div class="pd-bar-group">
                <div class="pd-bar-header">
                    <span class="pd-bar-label">🔴 Stop Loss (${fmtP(d.sl_pct * 100)})</span>
                    <span class="pd-bar-value pnl-negative">${fmtP(slPct)} reached</span>
                </div>
                <div class="pd-bar-track"><div class="pd-bar-fill bar-red" style="width:${slPct}%"></div></div>
                <div class="pd-bar-sub">SL at ${fmtP(d.sl_trigger_pnl_pct)} P&L${d.sl_price != null ? ` · Price: ${fmt(d.sl_price, 4)}` : ''}</div>
            </div>
            <div class="pd-bar-group">
                <div class="pd-bar-header">
                    <span class="pd-bar-label">⏱️ Holding Period (${d.max_holding_hours}h max)</span>
                    <span class="pd-bar-value" style="color:${holdPct > 80 ? 'var(--accent-red)' : holdPct > 50 ? 'var(--accent-orange)' : 'var(--accent-blue)'}">${fmtP(holdPct)}</span>
                </div>
                <div class="pd-bar-track"><div class="pd-bar-fill ${holdBarColor}" style="width:${holdPct}%"></div></div>
                <div class="pd-bar-sub">Held: ${formatDuration(d.hours_held)} · Remaining: ${timeRem}</div>
            </div>
        </div>`;
    } else if (isClosed) {
        // Show what the TP/SL levels were for the closed trade
        riskHTML = `
        <div class="pd-section">
            <div class="pd-section-title">🎯 Risk Levels (at close)</div>
            <div class="pd-grid">
                <div class="pd-field"><span class="pd-label">Stop Loss</span><span class="pd-value">${fmtP(d.sl_pct * 100)} (${d.sl_price != null ? fmt(d.sl_price, 4) : '—'})</span></div>
                <div class="pd-field"><span class="pd-label">Take Profit</span><span class="pd-value">${fmtP(d.tp_pct * 100)} (${d.tp_price != null ? fmt(d.tp_price, 4) : '—'})</span></div>
                <div class="pd-field"><span class="pd-label">Max Hold</span><span class="pd-value">${d.max_holding_hours}h</span></div>
                <div class="pd-field"><span class="pd-label">Actual Hold</span><span class="pd-value">${formatDuration(d.hours_held)}</span></div>
            </div>
        </div>`;
    }

    // ── Timing Section ──────────────────────────────────────
    let timingHTML = `
    <div class="pd-section">
        <div class="pd-section-title">⏱ Timing</div>
        <div class="pd-grid">
            <div class="pd-field"><span class="pd-label">Opened</span><span class="pd-value" style="font-size:0.8rem">${d.opened_at ? new Date(d.opened_at).toLocaleString() : '—'}</span></div>
            <div class="pd-field"><span class="pd-label">${isActive ? 'Duration (so far)' : 'Closed'}</span><span class="pd-value" style="font-size:0.8rem">${isActive ? formatDuration(d.hours_held) : (d.closed_at ? new Date(d.closed_at).toLocaleString() : '—')}</span></div>
            ${!isActive && d.hours_held ? `<div class="pd-field"><span class="pd-label">Total Duration</span><span class="pd-value">${formatDuration(d.hours_held)}</span></div>` : ''}
        </div>
    </div>`;

    // ── Market Info Section ──────────────────────────────────
    let marketHTML = `
    <div class="pd-section">
        <div class="pd-section-title">📊 Market Info</div>
        <div class="pd-grid">
            <div class="pd-field"><span class="pd-label">Volume</span><span class="pd-value">${d.volume ? fmtD(d.volume) : '—'}</span></div>
            <div class="pd-field"><span class="pd-label">Liquidity</span><span class="pd-value">${d.liquidity ? fmtD(d.liquidity) : '—'}</span></div>
            <div class="pd-field"><span class="pd-label">End Date</span><span class="pd-value" style="font-size:0.8rem">${d.end_date || '—'}</span></div>
            <div class="pd-field"><span class="pd-label">Resolution</span><span class="pd-value" style="font-size:0.78rem">${d.resolution_source || '—'}</span></div>
        </div>
    </div>`;

    // ── Forecast Section ────────────────────────────────────
    const fc = d.forecast;
    let forecastHTML = '';
    if (fc) {
        const edgeCls = (fc.edge||0) > 0 ? 'pnl-positive' : 'pnl-negative';
        forecastHTML = `
        <div class="pd-section">
            <div class="pd-section-title">🔮 Forecast Analysis</div>
            <div class="pd-grid">
                <div class="pd-field"><span class="pd-label">Model Prob</span><span class="pd-value">${fmtP((fc.model_probability||0)*100)}</span></div>
                <div class="pd-field"><span class="pd-label">Implied Prob</span><span class="pd-value">${fmtP((fc.implied_probability||0)*100)}</span></div>
                <div class="pd-field"><span class="pd-label">Edge</span><span class="pd-value ${edgeCls}">${fmtP((fc.edge||0)*100)}</span></div>
                <div class="pd-field"><span class="pd-label">Confidence</span><span class="pd-value">${fc.confidence_level||'—'}</span></div>
                <div class="pd-field"><span class="pd-label">Evidence Quality</span><span class="pd-value">${fmt(fc.evidence_quality||0, 3)}</span></div>
                <div class="pd-field"><span class="pd-label">Sources</span><span class="pd-value">${fc.num_sources||0}</span></div>
                <div class="pd-field"><span class="pd-label">Decision</span><span class="pd-value"><span class="pill ${pillClass(fc.decision)}">${fc.decision||'—'}</span></span></div>
                <div class="pd-field"><span class="pd-label">Forecast Time</span><span class="pd-value" style="font-size:0.78rem">${fc.created_at ? shortDate(fc.created_at) : '—'}</span></div>
            </div>
            ${fc.reasoning ? `<div class="pd-reasoning">${escapeHTML(fc.reasoning)}</div>` : ''}
        </div>`;
    }

    // ── Trade Timeline ──────────────────────────────────────
    const records = d.trade_records || [];
    let timelineHTML = '';
    if (records.length > 0) {
        const timelineRows = records.map(r => {
            const isBuy = (r.side||'').includes('BUY');
            const sideIcon = isBuy ? '🟢' : '🔴';
            const statusParts = (r.status||'').split('|');
            const mainStatus = statusParts[0] || '—';
            const exitInfo = statusParts.length > 1 ? statusParts[1] : '';
            return `
            <div class="td-timeline-item ${isBuy ? 'td-tl-buy' : 'td-tl-sell'}">
                <div class="td-tl-icon">${sideIcon}</div>
                <div class="td-tl-content">
                    <div class="td-tl-header">
                        <span class="td-tl-side">${r.side||'—'}</span>
                        <span class="td-tl-time">${r.created_at ? shortDate(r.created_at) : '—'}</span>
                    </div>
                    <div class="td-tl-details">
                        Price: ${fmt(r.price||0, 4)} · Size: ${fmt(r.size||0, 1)} · Stake: ${fmtD(r.stake_usd||0)}
                    </div>
                    <div class="td-tl-status">
                        <span class="pill ${pillClass(mainStatus)}" style="font-size:0.65rem">${mainStatus}</span>
                        ${exitInfo ? `<span style="font-size:0.72rem;color:var(--text-muted);margin-left:6px">${exitInfo}</span>` : ''}
                    </div>
                </div>
            </div>`;
        }).join('');
        timelineHTML = `
        <div class="pd-section">
            <div class="pd-section-title">📜 Trade Timeline (${records.length} record${records.length!==1?'s':''})</div>
            <div class="td-timeline">${timelineRows}</div>
        </div>`;
    }

    // ── Decision Trail ──────────────────────────────────────
    const decisions = d.decisions || [];
    let decisionsHTML = '';
    if (decisions.length > 0) {
        const decRows = decisions.map(dec => {
            const stageIcons = {SCAN:'🔍', RESEARCH:'📚', FORECAST:'🔮', EDGE:'📐', RISK:'🛡️', SIZE:'📏', EXECUTE:'⚡', MONITOR:'👁️'};
            const icon = stageIcons[(dec.stage||'').toUpperCase()] || '📋';
            return `
            <div class="td-decision-item">
                <div class="td-dec-icon">${icon}</div>
                <div class="td-dec-content">
                    <div class="td-dec-header">
                        <span class="td-dec-stage">${dec.stage||'—'}</span>
                        <span class="pill ${pillClass(dec.decision)}" style="font-size:0.62rem">${dec.decision||'—'}</span>
                        <span class="td-dec-time">${dec.timestamp ? shortDate(dec.timestamp) : '—'}</span>
                    </div>
                    ${dec.details ? `<div class="td-dec-details">${escapeHTML(dec.details).substring(0,200)}${(dec.details||'').length>200?'…':''}</div>` : ''}
                </div>
                ${dec.integrity_hash ? '<div class="td-dec-hash" title="Integrity verified">✅</div>' : ''}
            </div>`;
        }).join('');
        decisionsHTML = `
        <div class="pd-section">
            <div class="pd-section-title">🧠 Decision Trail (${decisions.length} decision${decisions.length!==1?'s':''})</div>
            <div class="td-decisions-list">${decRows}</div>
        </div>`;
    }

    // ── Performance Records ─────────────────────────────────
    const perfs = d.performance || [];
    let perfHTML = '';
    if (perfs.length > 0) {
        const perfRows = perfs.map(p => {
            const roi = p.pnl && p.stake_usd ? (p.pnl / p.stake_usd) : (p.roi || 0);
            return `
            <div class="pd-grid" style="margin-bottom:8px;">
                <div class="pd-field"><span class="pd-label">Forecast</span><span class="pd-value">${fmtP((p.forecast_prob||0)*100)}</span></div>
                <div class="pd-field"><span class="pd-label">Outcome</span><span class="pd-value">${p.actual_outcome != null ? fmtP(p.actual_outcome*100) : '—'}</span></div>
                <div class="pd-field"><span class="pd-label">Edge</span><span class="pd-value">${fmtP((p.edge_at_entry||0)*100)}</span></div>
                <div class="pd-field"><span class="pd-label">P&L</span><span class="pd-value ${pnlClass(p.pnl||0)}">${fmtD(p.pnl||0)}</span></div>
                <div class="pd-field"><span class="pd-label">Confidence</span><span class="pd-value">${p.confidence||'—'}</span></div>
                <div class="pd-field"><span class="pd-label">Resolved</span><span class="pd-value" style="font-size:0.78rem">${p.resolved_at ? shortDate(p.resolved_at) : '—'}</span></div>
            </div>`;
        }).join('');
        perfHTML = `
        <div class="pd-section">
            <div class="pd-section-title">📈 Performance Record</div>
            ${perfRows}
        </div>`;
    }

    // ── Assemble ────────────────────────────────────────────
    safeHTML(body, `
        ${outcomeBanner}
        ${priceHTML}
        ${riskHTML}
        ${timingHTML}
        ${marketHTML}
        ${forecastHTML}
        ${timelineHTML}
        ${decisionsHTML}
        ${perfHTML}
    `);

    // Footer
    safeHTML(footer, `
        <a href="${d.polymarket_url}" target="_blank" rel="noopener">🔗 View on Polymarket ↗</a>
        <span class="pos-detail-id" title="${d.market_id}">${d.market_id}</span>
    `);
}

function closeTradeDetail(event) {
    if (event && event.target !== event.currentTarget) return;
    $('#trade-detail-overlay').style.display = 'none';
}

// Close trade detail on Escape key
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        const tdOverlay = $('#trade-detail-overlay');
        if (tdOverlay && tdOverlay.style.display === 'flex') {
            tdOverlay.style.display = 'none';
        }
    }
});

// ─── Audit Trail ────────────────────────────────────────────────
async function updateAudit() {
    const d = await apiFetch('/api/audit');
    if (!d) return;
    const tbody = $('#audit-body');
    if (!d.entries || d.entries.length === 0) {
        safeHTML(tbody, '<tr><td colspan="7" class="empty-state">No audit entries</td></tr>');
        return;
    }
    safeHTML(tbody, d.entries.map(e => `<tr>
        <td style="font-family:var(--font-mono);font-size:0.72rem;">${(e.id||'').substring(0,8)}</td>
        <td>${(e.market_id||'').substring(0,20)}</td>
        <td><span class="pill ${pillClass(e.decision)}">${e.decision||'—'}</span></td>
        <td>${e.stage||'—'}</td>
        <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${e.details||''}">${(e.details||'—').substring(0,60)}</td>
        <td>${e.integrity_hash ? '✅' : '—'}</td>
        <td>${shortDate(e.timestamp)}</td>
    </tr>`).join(''));
}

// ─── Alerts ─────────────────────────────────────────────────────
async function updateAlerts() {
    const d = await apiFetch('/api/alerts');
    if (!d) return;
    const tbody = $('#alerts-body');
    if (!d.alerts || d.alerts.length === 0) {
        safeHTML(tbody, '<tr><td colspan="4" class="empty-state">No alerts</td></tr>');
        return;
    }
    const levelClass = (l) => {
        const ll = (l||'').toLowerCase();
        return ll === 'critical' || ll === 'error' ? 'pnl-negative' :
               ll === 'warning' ? 'accent-orange' : '';
    };
    safeHTML(tbody, d.alerts.map(a => `<tr>
        <td class="${levelClass(a.level)}" style="font-weight:700;text-transform:uppercase">${a.level||'info'}</td>
        <td>${a.channel||'—'}</td>
        <td>${a.message||'—'}</td>
        <td>${shortDate(a.timestamp || a.created_at)}</td>
    </tr>`).join(''));
}

// ─── Pipeline Candidates ────────────────────────────────────────
async function updateCandidates() {
    const d = await apiFetch('/api/candidates');
    if (!d) return;
    const tbody = $('#candidates-body');
    if (!d.candidates || d.candidates.length === 0) {
        safeHTML(tbody, '<tr><td colspan="13" class="empty-state">No candidates processed yet</td></tr>');
        return;
    }
    safeHTML(tbody, d.candidates.map(c => {
        const decClass = (c.decision||'').toUpperCase() === 'TRADE' ? 'pill-trade' :
                         (c.decision||'').toUpperCase() === 'NO TRADE' ? 'pill-no-trade' : 'pill-dry';
        return `<tr>
            <td>${c.cycle_id||'—'}</td>
            <td title="${c.question||''}">${(c.question||c.market_id||'').substring(0,45)}</td>
            <td>${c.market_type||'—'}</td>
            <td>${fmtP((c.implied_prob||0)*100)}</td>
            <td>${fmtP((c.model_prob||0)*100)}</td>
            <td class="${pnlClass(c.edge)}">${fmtP((c.edge||0)*100)}</td>
            <td>${fmt(c.evidence_quality,3)}</td>
            <td>${c.num_sources||0}</td>
            <td>${c.confidence||'—'}</td>
            <td><span class="pill ${decClass}">${c.decision||'—'}</span></td>
            <td>${c.stake_usd ? fmtD(c.stake_usd) : '—'}</td>
            <td title="${c.decision_reasons||''}" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${(c.decision_reasons||'—').substring(0,40)}</td>
            <td>${shortDate(c.created_at)}</td>
        </tr>`;
    }).join(''));
}

// ─── Engine & Drawdown Cards ────────────────────────────────────
async function updateEngineStatus() {
    const d = await apiFetch('/api/engine-status');
    if (!d) return;
    const statusEl = $('#engine-status');
    const isRunning = d.running;
    statusEl.textContent = isRunning ? 'RUNNING' : 'OFF';
    statusEl.className   = `card-value ${isRunning ? 'pnl-positive' : 'pnl-zero'}`;
    $('#engine-cycles').textContent = `Cycles: ${d.cycles || 0}`;

    const engineBadge = $('#engine-badge');
    if (isRunning) {
        engineBadge.textContent = '🟢 ENGINE ON'; engineBadge.className = 'badge badge-ok'; engineBadge.style.display = '';
    } else {
        engineBadge.textContent = '⚫ ENGINE OFF'; engineBadge.className = 'badge badge-paper'; engineBadge.style.display = '';
    }

    // Toggle start/stop buttons
    const btnStart = document.getElementById('btn-engine-start');
    const btnStop  = document.getElementById('btn-engine-stop');
    if (btnStart && btnStop) {
        btnStart.style.display = isRunning ? 'none' : '';
        btnStop.style.display  = isRunning ? '' : 'none';
    }

    // Show last cycle info if available
    if (d.last_cycle) {
        const lc = d.last_cycle;
        let info = `Cycles: ${d.cycles || 0} | Last: ${lc.markets_scanned||0} scanned, ${lc.edges_found||0} edges, ${lc.trades_executed||0} trades (${lc.duration_secs||0}s)`;
        if (d.uptime_secs) {
            const mins = Math.floor(d.uptime_secs / 60);
            info += ` | Up ${mins}m`;
        }
        $('#engine-cycles').textContent = info;
    }

    // Show error if engine crashed
    if (d.engine_error && !isRunning) {
        $('#engine-cycles').textContent = `⚠️ Error: ${d.engine_error.substring(0, 80)}`;
    }
}

async function toggleEngine(start) {
    const url = start ? '/api/engine/start' : '/api/engine/stop';
    try {
        const resp = await authFetch(url, { method: 'POST' });
        const data = await resp.json();
        if (data.message) {
            console.log('Engine:', data.message);
        }
        // Refresh status after a brief delay to let the engine start/stop
        setTimeout(updateEngineStatus, 1000);
    } catch (e) {
        console.error('Engine toggle error:', e);
    }
}

async function updateDrawdown() {
    const d = await apiFetch('/api/drawdown');
    if (!d) return;
    $('#drawdown-pct').textContent = fmtP(d.drawdown_pct * 100);
    $('#drawdown-detail').textContent = `Peak: ${fmtD(d.peak_equity)} | Current: ${fmtD(d.current_equity)}`;

    const heat = d.heat_level || 0;
    const heatEl = $('#heat-level');
    if (heat === 0) { heatEl.textContent = '🟢 Cool'; }
    else if (heat === 1) { heatEl.textContent = '🟡 Warm'; }
    else if (heat === 2) { heatEl.textContent = '🟠 Hot'; }
    else { heatEl.textContent = '🔴 Critical'; }

    $('#kelly-mult').textContent = `Kelly multiplier: ${fmt(d.kelly_multiplier, 2)}x`;

    const ksStatus = $('#kill-switch-status');
    ksStatus.textContent = d.is_killed ? '🛑 ACTIVE' : 'OFF';
    ksStatus.className = `card-value ${d.is_killed ? 'pnl-negative' : 'pnl-zero'}`;
}

// ─── Charts ─────────────────────────────────────────────────────
const chartColors = {
    blue:   'rgba(76,141,255,0.9)',
    green:  'rgba(0,214,143,0.9)',
    orange: 'rgba(255,159,67,0.9)',
    purple: 'rgba(168,85,247,0.9)',
    red:    'rgba(255,77,106,0.9)',
    teal:   'rgba(20,184,166,0.9)',
    pink:   'rgba(244,114,182,0.9)',
    yellow: 'rgba(251,191,36,0.9)',
};
const chartBg = {
    blue:   'rgba(76,141,255,0.12)',
    green:  'rgba(0,214,143,0.12)',
    orange: 'rgba(255,159,67,0.12)',
    purple: 'rgba(168,85,247,0.12)',
    red:    'rgba(255,77,106,0.12)',
};
const chartDefaults = {
    responsive: true,
    maintainAspectRatio: false,
    animation: {
        duration: 600,
        easing: 'easeOutQuart',
    },
    plugins: {
        legend: {
            labels: {
                color: '#8b8fa3',
                font: { size: 11, weight: '600' },
                padding: 16,
                usePointStyle: true,
                pointStyleWidth: 8,
            },
        },
        tooltip: {
            backgroundColor: 'rgba(15,17,23,0.9)',
            titleColor: '#e2e4ed',
            bodyColor: '#b0b4c8',
            borderColor: 'rgba(255,255,255,0.08)',
            borderWidth: 1,
            cornerRadius: 8,
            padding: 12,
            titleFont: { weight: '700', size: 12 },
            bodyFont: { size: 11 },
            displayColors: true,
            boxPadding: 4,
        },
    },
    scales: {
        x: {
            ticks: { color: '#5a5e72', font: { size: 10 } },
            grid: { color: 'rgba(255,255,255,0.03)', drawBorder: false },
        },
        y: {
            ticks: { color: '#5a5e72', font: { size: 10 } },
            grid: { color: 'rgba(255,255,255,0.03)', drawBorder: false },
        },
    },
};

async function updateCharts() {
    const [perf, types] = await Promise.all([
        apiFetch('/api/performance'),
        apiFetch('/api/market-types'),
    ]);

    // ── Daily Activity Chart
    if (perf && perf.daily_forecasts) {
        const days = perf.daily_forecasts.reverse();
        const labels = days.map(d => d.day);
        if (_charts.daily) _charts.daily.destroy();
        _charts.daily = new Chart($('#chart-daily-activity'), {
            type: 'bar',
            data: {
                labels,
                datasets: [
                    {label:'Forecasts', data:days.map(d=>d.forecasts), backgroundColor:chartBg.blue, borderColor:chartColors.blue, borderWidth:1},
                    {label:'Trades',    data:days.map(d=>d.trades),    backgroundColor:chartBg.green, borderColor:chartColors.green, borderWidth:1},
                ],
            },
            options: {...chartDefaults},
        });
    }

    // ── Edge & EQ Chart
    if (perf && perf.daily_forecasts) {
        const days = perf.daily_forecasts;
        const labels = days.map(d => d.day);
        if (_charts.edgeEq) _charts.edgeEq.destroy();
        _charts.edgeEq = new Chart($('#chart-edge-eq'), {
            type: 'line',
            data: {
                labels,
                datasets: [
                    {label:'Avg Edge',data:days.map(d=>(d.avg_edge||0)*100), borderColor:chartColors.green, backgroundColor:chartBg.green, fill:true, tension:0.3},
                    {label:'Avg EQ',  data:days.map(d=>d.avg_eq||0),        borderColor:chartColors.purple,backgroundColor:chartBg.purple,fill:true, tension:0.3, yAxisID:'y1'},
                ],
            },
            options: {
                ...chartDefaults,
                scales: {
                    ...chartDefaults.scales,
                    y1: {position:'right', ticks:{color:'#5a5e72',font:{size:10}}, grid:{display:false}},
                },
            },
        });
    }

    // ── Market Types Doughnut
    if (types && types.market_types && types.market_types.length > 0) {
        const labels = types.market_types.map(t => t.market_type || 'Unknown');
        const data   = types.market_types.map(t => t.count);
        const colors = [chartColors.blue, chartColors.green, chartColors.orange, chartColors.purple, chartColors.teal, chartColors.pink, chartColors.yellow, chartColors.red];
        if (_charts.types) _charts.types.destroy();
        _charts.types = new Chart($('#chart-market-types'), {
            type: 'doughnut',
            data: {labels, datasets:[{data, backgroundColor:colors.slice(0,data.length), borderWidth:0}]},
            options: {responsive:true, maintainAspectRatio:false, plugins:{legend:{position:'bottom',labels:{color:'#8b8fa3',font:{size:10}}}}},
        });
    }
}

// ─── Equity Curve ───────────────────────────────────────────────
async function updateEquityCurve() {
    const resp = await apiFetch('/api/equity-curve');
    if (!resp || !resp.points || resp.points.length === 0) return;

    const labels = resp.points.map(p => shortDate(p.timestamp));
    const data   = resp.points.map(p => p.pnl_cumulative);

    if (_charts.equity) _charts.equity.destroy();
    _charts.equity = new Chart($('#chart-equity-curve'), {
        type: 'line',
        data: {
            labels,
            datasets: [{
                label: 'Cumulative P&L ($)',
                data,
                borderColor: data[data.length - 1] >= 0 ? chartColors.green : chartColors.red,
                backgroundColor: data[data.length - 1] >= 0 ? chartBg.green : chartBg.red,
                fill: true,
                tension: 0.3,
                pointRadius: 2,
            }],
        },
        options: {
            ...chartDefaults,
            plugins: {
                ...chartDefaults.plugins,
                tooltip: {callbacks:{label: ctx => `P&L: ${fmtD(ctx.raw)}`}},
            },
        },
    });
}

// ═══════════════════════════════════════════════════════════════
//  EDITABLE CONFIGURATION
// ═══════════════════════════════════════════════════════════════

const CONFIG_SECTION_LABELS = {
    scanning: '🔍 Scanning',
    research: '📚 Research',
    forecasting: '🎯 Forecasting',
    ensemble: '🧩 Ensemble',
    risk: '⚠️ Risk',
    drawdown: '📉 Drawdown',
    portfolio: '💼 Portfolio',
    timeline: '⏱️ Timeline',
    microstructure: '🔬 Microstructure',
    execution: '⚡ Execution',
    cache: '💾 Cache',
    engine: '🏗️ Engine',
    alerts: '🔔 Alerts',
    observability: '📊 Observability',
};

async function updateConfig() {
    const data = await apiFetch('/api/config');
    if (!data) return;

    // Skip re-render if user has unsaved edits (don't blow away their changes)
    if (Object.keys(_configDirty).length > 0) return;

    // Skip re-render if data hasn't changed
    if (JSON.stringify(data) === JSON.stringify(_configData)) return;

    _configData = data;
    renderConfigTabs();
    renderConfigSection(_activeConfigTab || Object.keys(data)[0]);
}

function renderConfigTabs() {
    const container = $('#config-tabs');
    if (!container) return;
    const sections = Object.keys(_configData);
    container.innerHTML = sections.map(section => {
        const label = CONFIG_SECTION_LABELS[section] || section;
        const active = section === _activeConfigTab ? 'config-tab-active' : '';
        return `<button class="config-tab ${active}" data-section="${section}" onclick="switchConfigTab('${section}')">${label}</button>`;
    }).join('');
}

function switchConfigTab(section) {
    _activeConfigTab = section;
    // Update active tab styling
    $$('.config-tab').forEach(t => t.classList.toggle('config-tab-active', t.dataset.section === section));
    renderConfigSection(section);
}

function renderConfigSection(section) {
    _activeConfigTab = section;
    const container = $('#config-grid');
    if (!container || !_configData[section]) return;

    const fields = _configData[section];
    let html = `<div class="config-editor">`;
    html += `<div class="config-section-header">
        <h3>${CONFIG_SECTION_LABELS[section] || section}</h3>
        <button class="btn btn-save btn-sm" onclick="saveSection('${section}')">💾 Save ${section}</button>
    </div>`;
    html += `<div class="config-fields">`;

    for (const [key, value] of Object.entries(fields)) {
        html += renderConfigField(section, key, value);
    }

    html += `</div></div>`;
    container.innerHTML = html;
}

function renderConfigField(section, key, value) {
    const id = `cfg-${section}-${key}`;
    const label = key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    let input;

    if (typeof value === 'boolean') {
        input = `<label class="toggle-switch">
            <input type="checkbox" id="${id}" ${value ? 'checked' : ''} onchange="markDirty('${section}','${key}',this.checked)">
            <span class="toggle-slider"></span>
            <span class="toggle-label">${value ? 'On' : 'Off'}</span>
        </label>`;
    } else if (typeof value === 'number') {
        const step = Number.isInteger(value) ? '1' : '0.001';
        input = `<input type="number" id="${id}" class="config-input" value="${value}" step="${step}"
                   onchange="markDirty('${section}','${key}',parseFloat(this.value))">`;
    } else if (Array.isArray(value)) {
        input = `<input type="text" id="${id}" class="config-input config-input-wide" value="${value.join(', ')}"
                   placeholder="comma-separated values"
                   onchange="markDirty('${section}','${key}',this.value.split(',').map(s=>s.trim()).filter(Boolean))">`;
    } else if (typeof value === 'object' && value !== null) {
        input = `<textarea id="${id}" class="config-input config-textarea" rows="3"
                   onchange="markDirty('${section}','${key}',JSON.parse(this.value))">${JSON.stringify(value, null, 2)}</textarea>`;
    } else {
        input = `<input type="text" id="${id}" class="config-input config-input-wide" value="${value ?? ''}"
                   onchange="markDirty('${section}','${key}',this.value)">`;
    }

    return `<div class="config-field-row">
        <label class="config-field-label" for="${id}">${label}</label>
        <div class="config-field-input">${input}</div>
    </div>`;
}

function markDirty(section, key, value) {
    if (!_configDirty[section]) _configDirty[section] = {};
    _configDirty[section][key] = value;

    // Update toggle label if boolean
    const el = document.getElementById(`cfg-${section}-${key}`);
    if (el && el.type === 'checkbox') {
        const label = el.parentElement.querySelector('.toggle-label');
        if (label) label.textContent = el.checked ? 'On' : 'Off';
    }

    // Visual indicator that section has unsaved changes
    const tab = document.querySelector(`.config-tab[data-section="${section}"]`);
    if (tab && !tab.classList.contains('config-tab-dirty')) {
        tab.classList.add('config-tab-dirty');
    }
}

async function saveSection(section) {
    const changes = _configDirty[section];
    if (!changes || Object.keys(changes).length === 0) {
        showToast(`No changes in ${section}`, 'info');
        return;
    }
    showToast(`Saving ${section}…`, 'info');
    const result = await apiFetch('/api/config', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({[section]: changes}),
    });
    if (result && result.ok) {
        showToast(`${section} saved successfully!`, 'success');
        delete _configDirty[section];
        const tab = document.querySelector(`.config-tab[data-section="${section}"]`);
        if (tab) tab.classList.remove('config-tab-dirty');
        // Reload full config to reflect changes
        updateConfig();
    } else {
        showToast(`Failed to save: ${result?.error || 'Unknown error'}`, 'error');
    }
}

async function saveAllConfig() {
    const sections = Object.keys(_configDirty);
    if (sections.length === 0) {
        showToast('No unsaved changes', 'info');
        return;
    }
    showToast(`Saving ${sections.length} section(s)…`, 'info');
    const result = await apiFetch('/api/config', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(_configDirty),
    });
    if (result && result.ok) {
        showToast('All changes saved!', 'success');
        _configDirty = {};
        $$('.config-tab-dirty').forEach(t => t.classList.remove('config-tab-dirty'));
        updateConfig();
    } else {
        showToast(`Save failed: ${result?.error || 'Unknown error'}`, 'error');
    }
}

function confirmResetConfig() {
    showConfirmModal(
        'Reset Configuration',
        'This will reset ALL settings to their defaults. Your config.yaml will be deleted. This cannot be undone.',
        resetConfig
    );
}

async function resetConfig() {
    const result = await apiFetch('/api/config/reset', {method: 'POST'});
    if (result && result.ok) {
        showToast('Configuration reset to defaults', 'success');
        _configDirty = {};
        updateConfig();
        // Also refresh settings tab if open
        updateSettingsTab();
        // Reload risk/drawdown since they depend on config
        updateRisk();
        updateDrawdown();
        updateEngineStatus();
    } else {
        showToast(`Reset failed: ${result?.error || 'Unknown error'}`, 'error');
    }
}

// ═══════════════════════════════════════════════════════════════
//  SETTINGS TAB — ENV VARS, FLAGS, FULL CONFIG EDITOR
// ═══════════════════════════════════════════════════════════════

let _settingsEnvData = null;
let _settingsConfigData = null;
let _settingsConfigDirty = {};
let _activeSettingsSection = null;

const FLAG_LABELS = {
    ensemble_enabled: { icon: '🤖', label: 'Ensemble Forecasting', desc: 'Combine multiple LLM models for consensus forecasts' },
    drawdown_enabled: { icon: '📉', label: 'Drawdown Guard', desc: 'Automatically halt trading during drawdowns' },
    wallet_scanner_enabled: { icon: '🐋', label: 'Whale Scanner', desc: 'Monitor large wallet movements' },
    alerts_enabled: { icon: '🔔', label: 'Alerts', desc: 'Send notifications on important events' },
    cache_enabled: { icon: '💾', label: 'Cache', desc: 'Cache API responses and market data' },
    twap_enabled: { icon: '📊', label: 'TWAP Execution', desc: 'Use time-weighted average price for large orders' },
    adaptive_pricing: { icon: '🎯', label: 'Adaptive Pricing', desc: 'Dynamically adjust order prices based on microstructure' },
    dry_run: { icon: '🧪', label: 'Dry Run', desc: 'Simulate order execution without placing real trades' },
    kill_switch: { icon: '🛑', label: 'Kill Switch', desc: 'Emergency stop for all trading activity' },
    paper_mode: { icon: '📝', label: 'Paper Mode', desc: 'Record trades in DB only — no real execution' },
    auto_start: { icon: '▶️', label: 'Auto Start Engine', desc: 'Start engine automatically when dashboard launches' },
    daily_summary: { icon: '📧', label: 'Daily Summary', desc: 'Send daily performance summary via alerts' },
    metrics_enabled: { icon: '📈', label: 'Metrics Collection', desc: 'Collect internal performance metrics' },
    fetch_full_content: { icon: '📄', label: 'Full Content Fetch', desc: 'Fetch full article text in research phase' },
    track_leaderboard: { icon: '🏆', label: 'Track Leaderboard', desc: 'Monitor top trader leaderboard positions' },
};

async function updateSettingsTab() {
    // Load env vars
    const envResp = await apiFetch('/api/env');
    if (envResp && envResp.items) {
        _settingsEnvData = envResp.items;
        renderSettingsEnvVars();
    }

    // Load flags from admin endpoint (reuses existing data)
    const adminData = await apiFetch('/api/admin');
    if (adminData && adminData.feature_flags) {
        renderSettingsFlags(adminData.feature_flags);
    }

    // Load config for the editor
    const cfgData = await apiFetch('/api/config');
    if (cfgData) {
        _settingsConfigData = cfgData;
        renderSettingsConfigTabs();
        if (!_activeSettingsSection) {
            _activeSettingsSection = Object.keys(cfgData)[0];
        }
        renderSettingsConfigSection(_activeSettingsSection);
    }
}

function renderSettingsEnvVars() {
    const grid = $('#settings-env-grid');
    if (!grid || !_settingsEnvData) return;

    // Don't re-render if user is editing
    if (grid.querySelector(':focus')) return;

    const groups = {
        'LLM & AI': ['OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GOOGLE_API_KEY'],
        'Polymarket': ['POLYMARKET_API_KEY', 'POLYMARKET_API_SECRET', 'POLYMARKET_API_PASSPHRASE', 'POLYMARKET_PRIVATE_KEY', 'POLYMARKET_CHAIN_ID', 'CLOB_API_KEY', 'PRIVATE_KEY'],
        'Search & Research': ['SERPAPI_KEY', 'TAVILY_API_KEY', 'SERPER_API_KEY'],
        'Notifications': ['DISCORD_WEBHOOK_URL', 'SLACK_WEBHOOK_URL', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'],
        'Email (SMTP)': ['SMTP_HOST', 'SMTP_PORT', 'SMTP_USER', 'SMTP_PASS', 'ALERT_EMAIL_FROM', 'ALERT_EMAIL_TO'],
        'System': ['SENTRY_DSN', 'DASHBOARD_API_KEY', 'ENABLE_LIVE_TRADING'],
    };

    let html = '';
    for (const [groupName, groupKeys] of Object.entries(groups)) {
        const items = _settingsEnvData.filter(i => groupKeys.includes(i.key));
        if (items.length === 0) continue;

        html += `<div class="env-group">
            <div class="env-group-title">${groupName}</div>
            <div class="env-group-items">`;

        for (const item of items) {
            const inputType = item.is_secret ? 'password' : 'text';
            const statusDot = item.is_set
                ? '<span class="env-dot env-dot-ok" title="Configured">●</span>'
                : '<span class="env-dot env-dot-missing" title="Not set">●</span>';
            const placeholder = item.is_set
                ? `••• set (${item.masked_value || 'hidden'}) — leave blank to keep`
                : 'Enter value…';

            html += `<div class="env-key-row">
                <div class="env-key-header">
                    ${statusDot}
                    <label class="env-key-label" for="senv-${item.key}">${item.key}</label>
                </div>
                <div class="env-key-input-wrap">
                    <input type="${inputType}" class="env-key-input" id="senv-${item.key}"
                           placeholder="${placeholder}" data-env-key="${item.key}" autocomplete="off">
                    ${item.is_secret ? `<button class="env-reveal-btn" onclick="toggleEnvReveal(this)" title="Show/hide value">👁️</button>` : ''}
                </div>
            </div>`;
        }

        html += `</div></div>`;
    }

    safeHTML(grid, html);
}

function toggleEnvReveal(btn) {
    const input = btn.previousElementSibling;
    if (!input) return;
    if (input.type === 'password') {
        input.type = 'text';
        btn.textContent = '🔒';
    } else {
        input.type = 'password';
        btn.textContent = '👁️';
    }
}

async function saveAllEnvVars() {
    // Collect from settings tab
    const inputs = document.querySelectorAll('#settings-env-grid .env-key-input');
    const vars = {};
    inputs.forEach(input => {
        const key = input.dataset.envKey;
        const val = input.value.trim();
        if (key && val) {
            vars[key] = val;
        }
    });

    // Also collect from admin panel inputs
    const adminInputs = document.querySelectorAll('#admin-keys-grid .admin-key-input');
    adminInputs.forEach(input => {
        const key = input.dataset.envKey;
        const val = input.value.trim();
        if (key && val) {
            vars[key] = val;
        }
    });

    if (Object.keys(vars).length === 0) {
        showToast('No changes to save — enter values in the input fields first', 'info');
        return;
    }

    showToast(`Saving ${Object.keys(vars).length} key(s)…`, 'info');
    const result = await apiFetch('/api/env', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ vars }),
    });

    if (result && result.ok) {
        showToast(`✅ ${result.message}`, 'success');
        // Clear inputs
        inputs.forEach(i => i.value = '');
        adminInputs.forEach(i => i.value = '');
        // Refresh to show updated status
        updateSettingsTab();
        updateAdminPanel();
    } else {
        showToast(`❌ Failed: ${result?.error || 'Unknown error'}`, 'error');
    }
}

function renderSettingsFlags(flags) {
    const grid = $('#settings-flags-grid');
    if (!grid) return;

    let html = '<div class="flags-grid-inner">';
    for (const [key, val] of Object.entries(flags)) {
        const info = FLAG_LABELS[key] || { icon: '⚙️', label: key, desc: '' };
        const isOn = val === true;
        html += `<div class="flag-card ${isOn ? 'flag-card-on' : 'flag-card-off'}">
            <div class="flag-card-left">
                <span class="flag-icon">${info.icon}</span>
                <div class="flag-info">
                    <div class="flag-name">${info.label}</div>
                    <div class="flag-desc">${info.desc}</div>
                </div>
            </div>
            <label class="toggle-switch">
                <input type="checkbox" ${isOn ? 'checked' : ''} onchange="toggleFlag('${key}', this.checked)">
                <span class="toggle-slider"></span>
            </label>
        </div>`;
    }
    html += '</div>';
    safeHTML(grid, html);
}

async function toggleFlag(flagName, value) {
    const result = await apiFetch('/api/flags', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ flag: flagName, value: value }),
    });

    if (result && result.ok) {
        showToast(`${FLAG_LABELS[flagName]?.label || flagName}: ${value ? 'ON' : 'OFF'}`, 'success');
        // Refresh both admin panel flags and settings flags
        updateAdminPanel();
        // Re-render flags in settings tab
        const adminData = await apiFetch('/api/admin');
        if (adminData && adminData.feature_flags) {
            renderSettingsFlags(adminData.feature_flags);
        }
    } else {
        showToast(`Failed to toggle: ${result?.error || 'Unknown error'}`, 'error');
    }
}

// ─── Settings Config Editor (reuses rendering from System tab) ──
function renderSettingsConfigTabs() {
    const container = $('#settings-cfg-tabs');
    if (!container || !_settingsConfigData) return;
    const sections = Object.keys(_settingsConfigData);
    container.innerHTML = sections.map(section => {
        const label = CONFIG_SECTION_LABELS[section] || section;
        const active = section === _activeSettingsSection ? 'config-tab-active' : '';
        const dirty = _settingsConfigDirty[section] ? 'config-tab-dirty' : '';
        return `<button class="config-tab ${active} ${dirty}" data-section="${section}" onclick="switchSettingsConfigTab('${section}')">${label}</button>`;
    }).join('');
}

function switchSettingsConfigTab(section) {
    _activeSettingsSection = section;
    $$('#settings-cfg-tabs .config-tab').forEach(t =>
        t.classList.toggle('config-tab-active', t.dataset.section === section)
    );
    renderSettingsConfigSection(section);
}

function renderSettingsConfigSection(section) {
    _activeSettingsSection = section;
    const container = $('#settings-cfg-grid');
    if (!container || !_settingsConfigData || !_settingsConfigData[section]) return;

    const fields = _settingsConfigData[section];
    let html = `<div class="config-editor">`;
    html += `<div class="config-section-header">
        <h3>${CONFIG_SECTION_LABELS[section] || section}</h3>
        <button class="btn btn-save btn-sm" onclick="saveSettingsSection('${section}')">💾 Save ${section}</button>
    </div>`;
    html += `<div class="config-fields">`;

    for (const [key, value] of Object.entries(fields)) {
        html += renderSettingsConfigField(section, key, value);
    }

    html += `</div></div>`;
    container.innerHTML = html;
}

function renderSettingsConfigField(section, key, value) {
    const id = `scfg-${section}-${key}`;
    const label = key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    let input;

    if (typeof value === 'boolean') {
        input = `<label class="toggle-switch">
            <input type="checkbox" id="${id}" ${value ? 'checked' : ''} onchange="markSettingsDirty('${section}','${key}',this.checked)">
            <span class="toggle-slider"></span>
            <span class="toggle-label">${value ? 'On' : 'Off'}</span>
        </label>`;
    } else if (typeof value === 'number') {
        const step = Number.isInteger(value) ? '1' : '0.001';
        input = `<input type="number" id="${id}" class="config-input" value="${value}" step="${step}"
                   onchange="markSettingsDirty('${section}','${key}',parseFloat(this.value))">`;
    } else if (Array.isArray(value)) {
        input = `<input type="text" id="${id}" class="config-input config-input-wide" value="${value.join(', ')}"
                   placeholder="comma-separated values"
                   onchange="markSettingsDirty('${section}','${key}',this.value.split(',').map(s=>s.trim()).filter(Boolean))">`;
    } else if (typeof value === 'object' && value !== null) {
        input = `<textarea id="${id}" class="config-input config-textarea" rows="3"
                   onchange="markSettingsDirty('${section}','${key}',JSON.parse(this.value))">${JSON.stringify(value, null, 2)}</textarea>`;
    } else {
        input = `<input type="text" id="${id}" class="config-input config-input-wide" value="${value ?? ''}"
                   onchange="markSettingsDirty('${section}','${key}',this.value)">`;
    }

    return `<div class="config-field-row">
        <label class="config-field-label" for="${id}">${label}</label>
        <div class="config-field-input">${input}</div>
    </div>`;
}

function markSettingsDirty(section, key, value) {
    if (!_settingsConfigDirty[section]) _settingsConfigDirty[section] = {};
    _settingsConfigDirty[section][key] = value;

    // Update toggle label if boolean
    const el = document.getElementById(`scfg-${section}-${key}`);
    if (el && el.type === 'checkbox') {
        const label = el.parentElement.querySelector('.toggle-label');
        if (label) label.textContent = el.checked ? 'On' : 'Off';
    }

    // Visual indicator on tab
    const tab = document.querySelector(`#settings-cfg-tabs .config-tab[data-section="${section}"]`);
    if (tab && !tab.classList.contains('config-tab-dirty')) {
        tab.classList.add('config-tab-dirty');
    }
}

async function saveSettingsSection(section) {
    const changes = _settingsConfigDirty[section];
    if (!changes || Object.keys(changes).length === 0) {
        showToast(`No changes in ${section}`, 'info');
        return;
    }
    showToast(`Saving ${section}…`, 'info');
    const result = await apiFetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [section]: changes }),
    });
    if (result && result.ok) {
        showToast(`${section} saved successfully!`, 'success');
        delete _settingsConfigDirty[section];
        const tab = document.querySelector(`#settings-cfg-tabs .config-tab[data-section="${section}"]`);
        if (tab) tab.classList.remove('config-tab-dirty');
        // Reload config
        const cfgData = await apiFetch('/api/config');
        if (cfgData) {
            _settingsConfigData = cfgData;
            renderSettingsConfigSection(section);
        }
        // Also refresh the System tab config
        updateConfig();
    } else {
        showToast(`Failed to save: ${result?.error || 'Unknown error'}`, 'error');
    }
}

async function reloadConfigForSettings() {
    showToast('Reloading configuration…', 'info');
    const result = await apiFetch('/api/config/reload', { method: 'POST' });
    if (result && result.ok) {
        showToast('Configuration reloaded from disk', 'success');
        updateSettingsTab();
        updateConfig();
    } else {
        showToast(`Reload failed: ${result?.error || 'Unknown error'}`, 'error');
    }
}

// ═══════════════════════════════════════════════════════════════
//  DECISION INTELLIGENCE LOG
// ═══════════════════════════════════════════════════════════════

let _decisionExpanded = false;
let _diChartDecisions = null;
let _diChartGrades = null;
let _diChartEdge = null;
let _diChartCategories = null;
let _diLastEntries = [];

function _decisionKey(entry) {
    return `${entry.cycle_id||0}-${entry.market_id||''}`;
}

async function updateDecisionLog() {
    const cycleFilter = $('#decision-cycle-filter');
    const cycleVal = cycleFilter ? cycleFilter.value : '';
    const url = cycleVal ? `/api/decision-log?cycle=${cycleVal}` : '/api/decision-log?limit=50';
    const d = await apiFetch(url);
    if (!d) return;

    // Populate cycle selector (preserve current selection)
    if (d.cycles && d.cycles.length > 0 && cycleFilter) {
        const cur = cycleFilter.value;
        const newOpts = '<option value="">All Cycles</option>' +
            d.cycles.map(c => `<option value="${c}" ${String(c)===cur?'selected':''}>Cycle ${c}</option>`).join('');
        safeHTML(cycleFilter, newOpts);
    }

    // Populate category filter
    const catFilter = $('#di-category-filter');
    if (catFilter && d.stats && d.stats.unique_categories) {
        const curCat = catFilter.value;
        let catOpts = '<option value="">All Categories</option>';
        d.stats.unique_categories.forEach(c => {
            catOpts += `<option value="${c}" ${c===curCat?'selected':''}>${categoryIcon(c)} ${c}</option>`;
        });
        safeHTML(catFilter, catOpts);
    }

    // ── Render KPIs ──
    _renderDIKpis(d.stats);

    // ── Render Charts ──
    if (d.stats) {
        _renderDICharts(d.stats);
        _renderDITimeline(d.stats.decision_timeline || []);
        _renderDIFunnel(d.stats.funnel || []);
        _renderDIInsights(d.stats.insights || []);
        _renderDIScoreTrend(d.stats.cycle_trend || []);
        _renderDIOutcomes(d.stats.outcomes || []);
        _renderDICalibration(d.stats.confidence_calibration || []);
        _renderDICategoryRank(d.stats.category_performance || []);
        _renderDIMissedOpps(d.stats.missed_opportunities || []);
        _renderDIResearchROI(d.stats.research_roi || {});
    }

    // ── Toolbar stats ──
    if (d.stats) {
        const lastCycle = d.cycles && d.cycles.length > 0 ? `Cycle ${d.cycles[0]}` : '—';
        safeText($('#di-last-cycle'), `Last cycle: ${lastCycle}`);
        safeText($('#di-total-count'), `${d.stats.total} decisions`);
    }

    // ── Decision Cards ──
    const container = $('#decision-log-container');
    _diLastEntries = d.entries || [];
    if (!d.entries || d.entries.length === 0) {
        safeHTML(container, '<div class="empty-state" style="padding:40px 0;">No decision data yet — run an engine cycle to see how the bot makes decisions.</div>');
        return;
    }

    const newHTML = d.entries.map((e, idx) => renderDecisionCard(e, idx)).join('');
    const normalize = (h) => h.replace(/style="display:(none|block);?"/g, '').replace(/▶|▼/g, '');
    if (normalize(container.innerHTML) === normalize(newHTML)) {
        return;
    }

    _saveExpandedState();
    container.innerHTML = newHTML;
    _restoreExpandedState(d.entries);
    filterDecisionCards();
}

function _renderDIKpis(stats) {
    if (!stats) return;
    safeText($('#di-kpi-total'), String(stats.total || 0));
    safeText($('#di-kpi-trade-rate'), `${stats.trade_rate || 0}%`);
    safeText($('#di-kpi-avg-score'), String(stats.avg_di_score || '—'));
    const edgeStr = stats.avg_edge != null ? `${stats.avg_edge >= 0 ? '+' : ''}${stats.avg_edge}%` : '—';
    safeText($('#di-kpi-avg-edge'), edgeStr);
    safeText($('#di-kpi-avg-eq'), stats.avg_eq != null ? String(stats.avg_eq) : '—');
    safeText($('#di-kpi-pipeline'), stats.avg_pipeline != null ? `${stats.avg_pipeline}%` : '—');
    safeText($('#di-kpi-top-cat'), stats.top_category || '—');
    safeText($('#di-kpi-avg-grade'), stats.avg_grade || '—');
}

function _renderDICharts(stats) {
    // Decision Distribution (doughnut)
    const dc = document.getElementById('di-chart-decisions');
    if (dc) {
        if (_diChartDecisions) _diChartDecisions.destroy();
        _diChartDecisions = new Chart(dc, {
            type: 'doughnut',
            data: {
                labels: ['✅ Trade', '❌ No Trade', '⏭️ Skip'],
                datasets: [{
                    data: [stats.trade_count || 0, stats.no_trade_count || 0, stats.skip_count || 0],
                    backgroundColor: ['rgba(0,230,138,0.7)', 'rgba(255,77,106,0.7)', 'rgba(148,153,179,0.5)'],
                    borderColor: ['#00e68a', '#ff4d6a', '#9499b3'],
                    borderWidth: 1,
                }],
            },
            options: {
                responsive: true, maintainAspectRatio: false, cutout: '65%',
                plugins: {
                    legend: { position: 'bottom', labels: { color: '#9499b3', padding: 8, font: { size: 11 }, boxWidth: 12 } },
                    tooltip: { callbacks: { label: ctx => `${ctx.label}: ${ctx.raw} (${((ctx.raw/Math.max(stats.total,1))*100).toFixed(0)}%)` } },
                },
            },
        });
    }

    // Grade Distribution (bar)
    const gc = document.getElementById('di-chart-grades');
    if (gc) {
        if (_diChartGrades) _diChartGrades.destroy();
        const gradeOrder = ['A+', 'A', 'B+', 'B', 'C+', 'C', 'D', 'F'];
        const gradeColors = { 'A+': '#00e68a', 'A': '#00d68f', 'B+': '#4c8dff', 'B': '#3b82f6', 'C+': '#f59e0b', 'C': '#eab308', 'D': '#f97316', 'F': '#ef4444' };
        const gd = stats.grade_distribution || {};
        _diChartGrades = new Chart(gc, {
            type: 'bar',
            data: {
                labels: gradeOrder,
                datasets: [{
                    label: 'Count',
                    data: gradeOrder.map(g => gd[g] || 0),
                    backgroundColor: gradeOrder.map(g => (gradeColors[g] || '#9499b3') + '80'),
                    borderColor: gradeOrder.map(g => gradeColors[g] || '#9499b3'),
                    borderWidth: 1, borderRadius: 4, barThickness: 22,
                }],
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    x: { ticks: { color: '#9499b3', font: { size: 11 } }, grid: { display: false } },
                    y: { beginAtZero: true, ticks: { color: '#9499b3', stepSize: 1, font: { size: 10 } }, grid: { color: 'rgba(255,255,255,0.03)' } },
                },
            },
        });
    }

    // Edge Distribution (histogram)
    const ec = document.getElementById('di-chart-edge');
    if (ec) {
        if (_diChartEdge) _diChartEdge.destroy();
        const eb = stats.edge_buckets || {};
        const bucketLabels = Object.keys(eb);
        const bucketVals = Object.values(eb);
        const bucketColors = bucketLabels.map(l => l.includes('-') || l.startsWith('<') ? 'rgba(255,77,106,0.6)' : 'rgba(0,230,138,0.6)');
        _diChartEdge = new Chart(ec, {
            type: 'bar',
            data: {
                labels: bucketLabels,
                datasets: [{
                    label: 'Decisions',
                    data: bucketVals,
                    backgroundColor: bucketColors,
                    borderColor: bucketColors.map(c => c.replace('0.6', '1')),
                    borderWidth: 1, borderRadius: 3,
                }],
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    x: { ticks: { color: '#9499b3', font: { size: 9 }, maxRotation: 45 }, grid: { display: false } },
                    y: { beginAtZero: true, ticks: { color: '#9499b3', stepSize: 1, font: { size: 10 } }, grid: { color: 'rgba(255,255,255,0.03)' } },
                },
            },
        });
    }

    // Category Breakdown (horizontal bar)
    const cc = document.getElementById('di-chart-categories');
    if (cc) {
        if (_diChartCategories) _diChartCategories.destroy();
        const cd = stats.category_distribution || {};
        const catNames = Object.keys(cd).sort((a, b) => cd[b].total - cd[a].total);
        _diChartCategories = new Chart(cc, {
            type: 'bar',
            data: {
                labels: catNames.map(c => `${categoryIcon(c)} ${c}`),
                datasets: [
                    {
                        label: 'Trades',
                        data: catNames.map(c => cd[c].trades || 0),
                        backgroundColor: 'rgba(0,230,138,0.6)',
                        borderColor: '#00e68a',
                        borderWidth: 1, borderRadius: 3,
                    },
                    {
                        label: 'No Trade',
                        data: catNames.map(c => cd[c].no_trades || 0),
                        backgroundColor: 'rgba(255,77,106,0.5)',
                        borderColor: '#ff4d6a',
                        borderWidth: 1, borderRadius: 3,
                    },
                    {
                        label: 'Other',
                        data: catNames.map(c => (cd[c].total - (cd[c].trades||0) - (cd[c].no_trades||0)) || 0),
                        backgroundColor: 'rgba(148,153,179,0.4)',
                        borderColor: '#9499b3',
                        borderWidth: 1, borderRadius: 3,
                    },
                ],
            },
            options: {
                responsive: true, maintainAspectRatio: false, indexAxis: 'y',
                plugins: {
                    legend: { position: 'bottom', labels: { color: '#9499b3', padding: 8, font: { size: 10 }, boxWidth: 10 } },
                },
                scales: {
                    x: { stacked: true, ticks: { color: '#9499b3', font: { size: 10 } }, grid: { color: 'rgba(255,255,255,0.03)' } },
                    y: { stacked: true, ticks: { color: '#9499b3', font: { size: 10 } }, grid: { display: false } },
                },
            },
        });
    }
}

function _renderDITimeline(timeline) {
    const strip = $('#di-timeline-strip');
    if (!strip || !timeline.length) {
        if (strip) safeHTML(strip, '<div class="empty-state" style="padding:16px 0;font-size:0.8rem;">No timeline data</div>');
        return;
    }
    const maxTotal = Math.max(...timeline.map(t => t.total), 1);
    const html = timeline.map(t => {
        let level = 0;
        if (t.total > 0) level = 1;
        if (t.total >= maxTotal * 0.25) level = 2;
        if (t.total >= maxTotal * 0.5) level = 3;
        if (t.total >= maxTotal * 0.75) level = 4;
        const hourLabel = t.hour.length >= 13 ? t.hour.substring(11, 13) + ':00' : t.hour;
        const tooltip = `${hourLabel}: ${t.total} decisions (${t.trades} trades, ${t.no_trades} rejected)`;
        return `<div class="di-tl-cell di-tl-${level}" title="${tooltip}">
            <span class="di-tl-count">${t.total}</span>
            <span class="di-tl-hour">${hourLabel}</span>
        </div>`;
    }).join('');
    safeHTML(strip, html);
}

function _renderDIFunnel(funnel) {
    const el = $('#di-funnel');
    if (!el || !funnel.length) {
        if (el) safeHTML(el, '<div class="empty-state" style="padding:16px 0;font-size:0.8rem;">No pipeline data</div>');
        return;
    }
    const maxTotal = Math.max(...funnel.map(f => f.total), 1);
    const stageIcons = {'Discovery & Filter':'🔍','Classification':'🏷️','Research':'📚','Forecast':'🎯','Risk Check':'⚠️','Execution':'⚡'};
    const html = funnel.map((f, i) => {
        const widthPct = Math.max(20, (f.passed / maxTotal) * 100);
        const passRate = ((f.passed / Math.max(f.total, 1)) * 100).toFixed(0);
        const icon = stageIcons[f.stage] || '📋';
        return `<div class="di-funnel-stage">
            <div class="di-funnel-label">${icon} ${f.stage}</div>
            <div class="di-funnel-bar-wrap">
                <div class="di-funnel-bar di-funnel-passed" style="width:${widthPct}%">
                    <span>${f.passed} passed</span>
                </div>
                ${f.blocked > 0 ? `<div class="di-funnel-bar di-funnel-blocked" style="width:${Math.max(8, (f.blocked/maxTotal)*100)}%"><span>${f.blocked}</span></div>` : ''}
                ${f.skipped > 0 ? `<div class="di-funnel-bar di-funnel-skipped" style="width:${Math.max(8, (f.skipped/maxTotal)*100)}%"><span>${f.skipped}</span></div>` : ''}
            </div>
            <div class="di-funnel-rate">${passRate}%</div>
            ${i < funnel.length - 1 ? '<div class="di-funnel-arrow">▼</div>' : ''}
        </div>`;
    }).join('');
    safeHTML(el, html);
}

// ── DI-Outcomes: Trade Outcome Cards ─────────────────────────
function _renderDIOutcomes(outcomes) {
    const container = $('#di-outcomes-list');
    const countEl = $('#di-outcomes-count');
    if (!container) return;
    if (countEl) countEl.textContent = String((outcomes || []).length);
    if (!outcomes || outcomes.length === 0) {
        safeHTML(container, '<div class="empty-state" style="padding:16px 0;font-size:0.8rem;">No trade outcomes yet — outcomes appear after trades are matched.</div>');
        return;
    }
    const html = outcomes.map(o => {
        const pnl = o.pnl || 0;
        const isWin = pnl > 0;
        const isPending = !o.status || o.status === 'open';
        const statusClass = isPending ? 'pending' : isWin ? 'win' : 'loss';
        const statusTag = isPending ? 'OPEN' : isWin ? 'WIN' : 'LOSS';
        const pnlClass = pnl >= 0 ? 'pos' : 'neg';
        const pnlText = pnl >= 0 ? `+$${pnl.toFixed(2)}` : `-$${Math.abs(pnl).toFixed(2)}`;
        return `<div class="di-outcome-card ${statusClass}">
            <div class="di-outcome-q" title="${escHtml(o.question)}">${escHtml(o.question)}</div>
            <div class="di-outcome-meta">
                <span class="di-outcome-pnl ${pnlClass}">${pnlText}</span>
                <span class="di-outcome-tag ${statusClass}">${statusTag}</span>
                ${o.entry_price ? `<span class="di-outcome-date">Entry: ${(o.entry_price * 100).toFixed(1)}¢</span>` : ''}
                ${o.current_price ? `<span class="di-outcome-date">Now: ${(o.current_price * 100).toFixed(1)}¢</span>` : ''}
            </div>
        </div>`;
    }).join('');
    safeHTML(container, html);
}

// ── DI-A: Cycle Score Trend Chart ────────────────────────────
let _diChartTrend = null;
function _renderDIScoreTrend(cycleTrend) {
    const canvas = document.getElementById('di-score-trend');
    if (!canvas) return;
    if (_diChartTrend) _diChartTrend.destroy();
    if (!cycleTrend || cycleTrend.length === 0) {
        canvas.parentElement.querySelector('.empty-state')?.remove();
        const parent = canvas.parentElement;
        if (!parent.querySelector('.empty-state')) {
            const emp = document.createElement('div');
            emp.className = 'empty-state';
            emp.style.cssText = 'padding:16px 0;font-size:0.8rem;';
            emp.textContent = 'Not enough cycle data for trend';
            parent.appendChild(emp);
        }
        return;
    }
    _diChartTrend = new Chart(canvas, {
        type: 'line',
        data: {
            labels: cycleTrend.map(c => `C${c.cycle_id}`),
            datasets: [
                {
                    label: 'DI Score',
                    data: cycleTrend.map(c => c.avg_score),
                    borderColor: '#4c8dff',
                    backgroundColor: 'rgba(76,141,255,0.1)',
                    fill: true, tension: 0.35, pointRadius: 4, pointHoverRadius: 6,
                    borderWidth: 2, yAxisID: 'y',
                },
                {
                    label: 'Avg Edge %',
                    data: cycleTrend.map(c => c.avg_edge),
                    borderColor: '#00e68a',
                    backgroundColor: 'rgba(0,230,138,0.1)',
                    fill: false, tension: 0.35, pointRadius: 3, pointHoverRadius: 5,
                    borderWidth: 2, borderDash: [4, 2], yAxisID: 'y',
                },
                {
                    label: 'Trade Rate %',
                    data: cycleTrend.map(c => c.trade_rate),
                    borderColor: '#f59e0b',
                    backgroundColor: 'rgba(245,158,11,0.1)',
                    fill: false, tension: 0.35, pointRadius: 3, pointHoverRadius: 5,
                    borderWidth: 2, borderDash: [6, 3], yAxisID: 'y1',
                },
            ],
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { position: 'bottom', labels: { color: '#9499b3', padding: 8, font: { size: 10 }, boxWidth: 10 } },
                tooltip: { backgroundColor: 'rgba(15,17,23,0.95)', borderColor: 'rgba(255,255,255,0.1)', borderWidth: 1 },
            },
            scales: {
                x: { ticks: { color: '#9499b3', font: { size: 10 } }, grid: { display: false } },
                y: { position: 'left', beginAtZero: true, max: 100, ticks: { color: '#9499b3', font: { size: 10 } }, grid: { color: 'rgba(255,255,255,0.03)' }, title: { display: true, text: 'Score / Edge', color: '#9499b3', font: { size: 10 } } },
                y1: { position: 'right', beginAtZero: true, max: 100, ticks: { color: '#f59e0b', font: { size: 10 } }, grid: { display: false }, title: { display: true, text: 'Trade Rate %', color: '#f59e0b', font: { size: 10 } } },
            },
        },
    });
}

// ── DI-B: Confidence Calibration Matrix ──────────────────────
function _renderDICalibration(calibData) {
    const container = $('#di-calibration');
    if (!container) return;
    if (!calibData || calibData.length === 0) {
        safeHTML(container, '<div class="empty-state" style="padding:16px 0;font-size:0.8rem;">No calibration data yet</div>');
        return;
    }
    const levelColors = { HIGH: '#00e68a', MEDIUM: '#f59e0b', LOW: '#ff4d6a', NONE: '#5a5f78' };
    const levelIcons = { HIGH: '🟢', MEDIUM: '🟡', LOW: '🔴', NONE: '⚪' };
    const html = calibData.map(c => {
        const color = levelColors[c.level] || '#5a5f78';
        const icon = levelIcons[c.level] || '⚪';
        const accText = c.accuracy != null ? `${c.accuracy}%` : 'N/A';
        const accClass = c.accuracy != null ? (c.accuracy >= 60 ? 'di-cal-good' : c.accuracy >= 40 ? 'di-cal-mid' : 'di-cal-bad') : 'di-cal-na';
        return `<div class="di-cal-card" style="border-top:3px solid ${color};">
            <div class="di-cal-header">
                <span class="di-cal-icon">${icon}</span>
                <span class="di-cal-level" style="color:${color};">${c.level}</span>
            </div>
            <div class="di-cal-stats">
                <div class="di-cal-stat"><span class="di-cal-stat-val">${c.total}</span><span class="di-cal-stat-lbl">Decisions</span></div>
                <div class="di-cal-stat"><span class="di-cal-stat-val">${c.trades}</span><span class="di-cal-stat-lbl">Trades</span></div>
                <div class="di-cal-stat"><span class="di-cal-stat-val">${c.avg_edge.toFixed(1)}%</span><span class="di-cal-stat-lbl">Avg Edge</span></div>
                <div class="di-cal-stat"><span class="di-cal-stat-val">${c.avg_score}</span><span class="di-cal-stat-lbl">Avg Score</span></div>
            </div>
            <div class="di-cal-accuracy ${accClass}">
                <span class="di-cal-acc-label">Outcome Accuracy</span>
                <span class="di-cal-acc-val">${accText}</span>
            </div>
            <div class="di-cal-outcomes">
                <span class="di-cal-win">✅ ${c.positive_outcomes} W</span>
                <span class="di-cal-loss">❌ ${c.negative_outcomes} L</span>
            </div>
        </div>`;
    }).join('');
    safeHTML(container, html);
}

// ── DI-C: Category Performance Ranking ───────────────────────
function _renderDICategoryRank(catPerf) {
    const container = $('#di-category-rank');
    if (!container) return;
    if (!catPerf || catPerf.length === 0) {
        safeHTML(container, '<div class="empty-state" style="padding:16px 0;font-size:0.8rem;">No category data yet</div>');
        return;
    }
    const html = catPerf.map((c, i) => {
        const rankMedal = i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : `#${i + 1}`;
        const scorePct = Math.min(100, c.avg_score);
        const scoreColor = scorePct >= 70 ? '#00e68a' : scorePct >= 45 ? '#f59e0b' : '#ff4d6a';
        return `<div class="di-catrank-row">
            <div class="di-catrank-rank">${rankMedal}</div>
            <div class="di-catrank-name">${categoryIcon(c.category)} ${c.category}</div>
            <div class="di-catrank-stats">
                <span class="di-catrank-stat" title="Decisions">${c.total} dec</span>
                <span class="di-catrank-stat" title="Trades">${c.trades} trades</span>
                <span class="di-catrank-stat" title="Avg Edge">${c.avg_edge.toFixed(1)}% edge</span>
                <span class="di-catrank-stat" title="Win Rate">${c.wins + c.losses > 0 ? c.win_rate + '%' : '—'} WR</span>
            </div>
            <div class="di-catrank-bar-wrap">
                <div class="di-catrank-bar" style="width:${scorePct}%;background:${scoreColor};"></div>
                <span class="di-catrank-score">${c.avg_score}</span>
            </div>
        </div>`;
    }).join('');
    safeHTML(container, html);
}

// ── DI-D: Missed Opportunities ───────────────────────────────
function _renderDIMissedOpps(missedOpps) {
    const container = $('#di-missed-opps');
    const countEl = $('#di-missed-count');
    if (!container) return;
    if (countEl) countEl.textContent = String((missedOpps || []).length);
    if (!missedOpps || missedOpps.length === 0) {
        safeHTML(container, '<div class="empty-state" style="padding:16px 0;font-size:0.8rem;">No missed opportunities detected — all high-edge markets were traded or correctly rejected</div>');
        return;
    }
    const html = missedOpps.map(m => {
        const gradeColor = m.di_grade.startsWith('A') ? '#00e68a' :
                           m.di_grade.startsWith('B') ? '#4c8dff' :
                           m.di_grade.startsWith('C') ? '#f59e0b' : '#ff4d6a';
        const reasonsList = (m.rejection_reasons || []).map(r => `<span class="di-mo-reason">• ${escHtml(r)}</span>`).join('');
        return `<div class="di-mo-card">
            <div class="di-mo-header">
                <span class="di-mo-edge" title="Edge">📈 ${m.edge.toFixed(1)}%</span>
                <span class="di-mo-grade" style="color:${gradeColor};">${m.di_grade}</span>
                <span class="di-mo-cat">${categoryIcon(m.category)} ${m.category || '—'}</span>
                <span class="di-mo-time">${shortDate(m.created_at)}</span>
            </div>
            <div class="di-mo-question">${escHtml(m.question)}</div>
            <div class="di-mo-prob">Market: ${m.implied_prob}% → Model: ${m.model_prob}%</div>
            <div class="di-mo-reasons">${reasonsList}</div>
        </div>`;
    }).join('');
    safeHTML(container, html);
}

// ── DI-E: Research ROI ───────────────────────────────────────
function _renderDIResearchROI(roi) {
    const container = $('#di-research-roi');
    if (!container) return;
    if (!roi || Object.keys(roi).length === 0) {
        safeHTML(container, '<div class="empty-state" style="padding:16px 0;font-size:0.8rem;">No research ROI data</div>');
        return;
    }
    const tierOrder = ['excellent', 'good', 'fair', 'poor'];
    const tierColors = { excellent: '#00e68a', good: '#4c8dff', fair: '#f59e0b', poor: '#ff4d6a' };
    const tierIcons = { excellent: '🌟', good: '✅', fair: '🟡', poor: '❌' };
    const maxEdge = Math.max(...tierOrder.map(t => (roi[t] || {}).avg_edge || 0), 1);
    const html = tierOrder.map(t => {
        const d = roi[t] || { count: 0, avg_edge: 0, max_edge: 0 };
        const color = tierColors[t];
        const icon = tierIcons[t];
        const barW = Math.max(5, (d.avg_edge / maxEdge) * 100);
        return `<div class="di-roi-row">
            <div class="di-roi-tier">
                <span class="di-roi-icon">${icon}</span>
                <span class="di-roi-label" style="color:${color};">${t.charAt(0).toUpperCase() + t.slice(1)}</span>
                <span class="di-roi-count">${d.count} decisions</span>
            </div>
            <div class="di-roi-bar-wrap">
                <div class="di-roi-bar" style="width:${barW}%;background:${color};"></div>
                <span class="di-roi-val">${d.avg_edge.toFixed(1)}% avg edge</span>
            </div>
            <span class="di-roi-max">max ${d.max_edge.toFixed(1)}%</span>
        </div>`;
    }).join('');
    safeHTML(container, html);
}

// ── DI-F: Auto-Generated Insights ────────────────────────────
function _renderDIInsights(insights) {
    const container = $('#di-insights');
    const countEl = $('#di-insights-count');
    if (!container) return;
    if (countEl) countEl.textContent = String((insights || []).length);
    if (!insights || insights.length === 0) {
        safeHTML(container, '<div class="empty-state" style="padding:16px 0;font-size:0.8rem;">No insights generated — more decision data needed</div>');
        return;
    }
    const typeColors = { WARNING: '#f59e0b', INFO: '#4c8dff', SUCCESS: '#00e68a', OPPORTUNITY: '#a855f7' };
    const typeBg = { WARNING: 'rgba(245,158,11,0.08)', INFO: 'rgba(76,141,255,0.08)', SUCCESS: 'rgba(0,230,138,0.08)', OPPORTUNITY: 'rgba(168,85,247,0.08)' };
    const html = insights.map(ins => {
        const color = typeColors[ins.type] || '#9499b3';
        const bg = typeBg[ins.type] || 'rgba(255,255,255,0.03)';
        return `<div class="di-insight-card" style="border-left:3px solid ${color};background:${bg};">
            <div class="di-insight-header">
                <span class="di-insight-icon">${ins.icon || '💡'}</span>
                <span class="di-insight-title">${escHtml(ins.title)}</span>
                <span class="di-insight-type" style="color:${color};">${ins.type}</span>
            </div>
            <div class="di-insight-message">${escHtml(ins.message)}</div>
        </div>`;
    }).join('');
    safeHTML(container, html);
}

function exportDecisionCSV() {
    if (!_diLastEntries || _diLastEntries.length === 0) {
        showToast('No decision data to export', 'warning');
        return;
    }
    const headers = ['Cycle','Market ID','Question','Category','Decision','DI Grade','DI Score','Edge','Implied Prob','Model Prob','Evidence Quality','Sources','Confidence','Pipeline %','Created'];
    const rows = _diLastEntries.map(e => [
        e.cycle_id || '', e.market_id || '', `"${(e.question||'').replace(/"/g,'""')}"`,
        e.category || '', e.decision || '', e.di_grade || '', e.di_score || '',
        ((e.edge||0)*100).toFixed(2), ((e.implied_prob||0)*100).toFixed(1),
        ((e.model_prob||0)*100).toFixed(1), (e.evidence_quality||0).toFixed(3),
        e.num_sources || 0, e.confidence || '', e.pipeline_completeness || '',
        e.created_at || '',
    ]);
    const csv = [headers.join(','), ...rows.map(r => r.join(','))].join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `decision_log_${new Date().toISOString().slice(0,10)}.csv`;
    a.click(); URL.revokeObjectURL(url);
    showToast('Decision log exported', 'success');
}

function _saveExpandedState() {
    // Persist which cards are currently expanded (don't clear the set,
    // user-toggled state carries across refreshes)
    document.querySelectorAll('.decision-card').forEach(card => {
        const key = card.dataset.decisionKey;
        if (!key) return;
        const detail = card.querySelector('.dc-detail');
        if (detail && detail.style.display !== 'none') {
            _expandedDecisionKeys.add(key);
        } else {
            _expandedDecisionKeys.delete(key);
        }
    });
}

function _restoreExpandedState(entries) {
    entries.forEach((entry, idx) => {
        const key = _decisionKey(entry);
        if (_expandedDecisionKeys.has(key) || _decisionExpanded) {
            const detail = document.getElementById(`dc-detail-${idx}`);
            const icon = document.getElementById(`dc-expand-${idx}`);
            if (detail) detail.style.display = 'block';
            if (icon) icon.textContent = '▼';
        }
    });
}

function renderDecisionCard(entry, idx) {
    const decision = (entry.decision || 'SKIP').toUpperCase();
    const decClass = decision === 'TRADE' ? 'dc-trade' :
                     decision === 'NO TRADE' ? 'dc-no-trade' : 'dc-skip';
    const decIcon = decision === 'TRADE' ? '✅' :
                    decision === 'NO TRADE' ? '❌' : '⏭️';

    const edgeVal = (entry.edge || 0) * 100;
    const edgeSign = edgeVal >= 0 ? '+' : '';
    const edgeClass = edgeVal > 0 ? 'pnl-positive' : edgeVal < 0 ? 'pnl-negative' : 'pnl-zero';

    // Pipeline stages dots
    const stageDots = (entry.stages || []).map(s => {
        const sc = s.status === 'passed' || s.status === 'executed' ? 'stage-pass' :
                   s.status === 'blocked' ? 'stage-block' : 'stage-skip';
        return `<span class="stage-dot ${sc}" title="${s.icon} ${s.name}: ${s.status}">${s.icon}</span>`;
    }).join('');

    // Grade badge
    const grade = entry.di_grade || '—';
    const gradeColorClass = grade.startsWith('A') ? 'grade-a' :
                            grade.startsWith('B') ? 'grade-b' :
                            grade.startsWith('C') ? 'grade-c' :
                            grade.startsWith('D') ? 'grade-d' : 'grade-f';

    // Category badge
    const catBadge = entry.category
        ? `<span class="dc-category-badge" title="Category: ${entry.category}">${categoryIcon(entry.category)} ${entry.category}</span>`
        : '';

    // Mini probability bar
    const implPct = ((entry.implied_prob || 0) * 100).toFixed(0);
    const modPct = ((entry.model_prob || 0) * 100).toFixed(0);
    const miniProbBar = `<div class="dc-mini-prob" title="Market ${implPct}% → Model ${modPct}%">
        <div class="dc-mini-prob-track">
            <div class="dc-mini-prob-implied" style="width:${implPct}%"></div>
            <div class="dc-mini-prob-model" style="width:${modPct}%"></div>
        </div>
        <span class="dc-mini-prob-label">${implPct}→${modPct}%</span>
    </div>`;

    // Confidence indicator
    const conf = (entry.confidence || '').toLowerCase();
    const confDot = conf === 'high' ? 'conf-high' : conf === 'medium' ? 'conf-med' : conf === 'low' ? 'conf-low' : 'conf-none';
    const confLabel = conf || '—';

    // Collapsed summary — enhanced
    const summary = `
    <div class="dc-header" onclick="toggleDecisionDetail(${idx})">
        <div class="dc-header-left">
            <span class="dc-grade-badge ${gradeColorClass}" title="Decision Intelligence Score: ${entry.di_score || 0}/100">${grade}</span>
            <span class="dc-decision-badge ${decClass}">${decIcon} ${decision}</span>
            <span class="dc-question" title="${entry.question || ''}">${escHtml((entry.question || entry.market_id || '').substring(0, 80))}</span>
        </div>
        <div class="dc-header-right">
            ${catBadge}
            <span class="dc-type-badge">${entry.market_type || '—'}</span>
            <div class="dc-stages-mini">${stageDots}</div>
            ${miniProbBar}
            <span class="dc-metric"><span class="dc-metric-label">Edge</span> <span class="${edgeClass}">${edgeSign}${edgeVal.toFixed(1)}%</span></span>
            <span class="dc-metric"><span class="dc-metric-label">EQ</span> ${fmt(entry.evidence_quality, 2)}</span>
            <div class="dc-conf-indicator ${confDot}" title="Confidence: ${confLabel}"><span class="dc-conf-dot"></span><span class="dc-conf-text">${confLabel}</span></div>
            <span class="dc-time">${shortDate(entry.created_at)}</span>
            <span class="dc-expand-icon" id="dc-expand-${idx}">▶</span>
        </div>
    </div>`;

    // Expanded detail
    const detail = renderDecisionDetail(entry, idx);

    const stableKey = _decisionKey(entry);
    const searchText = `${entry.question||''} ${entry.market_id||''} ${entry.category||''} ${entry.market_type||''} ${decision} ${grade}`.replace(/"/g, '');
    return `<div class="decision-card ${decClass}" data-decision="${decision}" data-idx="${idx}" data-decision-key="${stableKey}" data-grade="${grade}" data-category="${entry.category||''}" data-searchtext="${escHtml(searchText)}">
        ${summary}
        <div class="dc-detail" id="dc-detail-${idx}" style="display:none;">${detail}</div>
    </div>`;
}

function categoryIcon(cat) {
    const icons = {
        MACRO: '📈', ELECTION: '🗳️', CRYPTO: '₿', CORPORATE: '🏢',
        LEGAL: '⚖️', SCIENCE: '🔬', TECH: '💻', SPORTS: '🏆',
        WEATHER: '🌦️', GEOPOLITICS: '🌍', SOCIAL_MEDIA: '📱', UNKNOWN: '❓'
    };
    return icons[(cat || '').toUpperCase()] || '❓';
}

function renderDecisionDetail(entry, idx) {
    const stages = entry.stages || [];
    const decision = (entry.decision || 'SKIP').toUpperCase();
    const decClass = decision === 'TRADE' ? 'dc-trade' :
                     decision === 'NO TRADE' ? 'dc-no-trade' : 'dc-skip';

    // ── Decision Summary Panel ──
    const grade = entry.di_grade || '—';
    const diScore = entry.di_score || 0;
    const gradeColorClass = grade.startsWith('A') ? 'grade-a' :
                            grade.startsWith('B') ? 'grade-b' :
                            grade.startsWith('C') ? 'grade-c' :
                            grade.startsWith('D') ? 'grade-d' : 'grade-f';

    const edgeVal = (entry.edge || 0) * 100;
    const implPct = ((entry.implied_prob || 0) * 100).toFixed(1);
    const modPct = ((entry.model_prob || 0) * 100).toFixed(1);
    const eq = entry.evidence_quality || 0;
    const researchability = entry.researchability || 0;
    const conf = (entry.confidence || '—').toLowerCase();
    const confPercent = conf === 'high' ? 85 : conf === 'medium' ? 55 : conf === 'low' ? 25 : 0;
    const completeness = entry.pipeline_completeness || 0;

    // Verdict text
    const verdictIcon = decision === 'TRADE' ? '🟢' : decision === 'NO TRADE' ? '🔴' : '🟡';
    const verdictText = decision === 'TRADE'
        ? `Trade executed with ${edgeVal >= 0 ? '+' : ''}${edgeVal.toFixed(1)}% edge and ${conf} confidence`
        : decision === 'NO TRADE'
        ? `Trade rejected — insufficient edge or failed risk checks`
        : `Market skipped during pipeline evaluation`;

    // Reasons
    const reasonsList = (entry.decision_reasons_list || []).map(r =>
        `<li>${escHtml(r)}</li>`
    ).join('');

    let html = '';

    // ═══ DECISION SUMMARY PANEL ═══
    html += `<div class="dc-summary-panel ${decClass}">
        <div class="dc-summary-header">
            <div class="dc-summary-verdict">
                <span class="dc-verdict-icon">${verdictIcon}</span>
                <div class="dc-verdict-text-wrap">
                    <div class="dc-verdict-label">DECISION VERDICT</div>
                    <div class="dc-verdict-text">${escHtml(verdictText)}</div>
                </div>
            </div>
            <div class="dc-summary-grade">
                <div class="dc-grade-circle ${gradeColorClass}">
                    <span class="dc-grade-letter">${grade}</span>
                    <span class="dc-grade-score">${diScore}/100</span>
                </div>
            </div>
        </div>

        <div class="dc-summary-question">
            <span class="dc-full-question">${escHtml(entry.question || '')}</span>
        </div>

        <div class="dc-summary-metrics">
            <div class="dc-summary-metric">
                <div class="dc-sm-label">Market Price</div>
                <div class="dc-sm-value">${implPct}%</div>
                <div class="dc-sm-bar"><div class="dc-sm-fill dc-sm-market" style="width:${implPct}%"></div></div>
            </div>
            <div class="dc-summary-metric">
                <div class="dc-sm-label">Model Forecast</div>
                <div class="dc-sm-value">${modPct}%</div>
                <div class="dc-sm-bar"><div class="dc-sm-fill dc-sm-model" style="width:${modPct}%"></div></div>
            </div>
            <div class="dc-summary-metric">
                <div class="dc-sm-label">Edge</div>
                <div class="dc-sm-value ${edgeVal > 0 ? 'pnl-positive' : edgeVal < 0 ? 'pnl-negative' : ''}">${edgeVal >= 0 ? '+' : ''}${edgeVal.toFixed(2)}%</div>
                <div class="dc-sm-bar"><div class="dc-sm-fill dc-sm-edge" style="width:${Math.min(Math.abs(edgeVal) * 5, 100)}%"></div></div>
            </div>
            <div class="dc-summary-metric">
                <div class="dc-sm-label">Evidence Quality</div>
                <div class="dc-sm-value">${(eq).toFixed(2)}</div>
                <div class="dc-sm-bar"><div class="dc-sm-fill dc-sm-eq" style="width:${eq * 100}%"></div></div>
            </div>
            <div class="dc-summary-metric">
                <div class="dc-sm-label">Confidence</div>
                <div class="dc-sm-value">${conf}</div>
                <div class="dc-sm-bar"><div class="dc-sm-fill dc-sm-conf" style="width:${confPercent}%"></div></div>
            </div>
            <div class="dc-summary-metric">
                <div class="dc-sm-label">Researchability</div>
                <div class="dc-sm-value">${researchability}%</div>
                <div class="dc-sm-bar"><div class="dc-sm-fill dc-sm-research" style="width:${researchability}%"></div></div>
            </div>
        </div>

        <div class="dc-summary-bottom">
            <div class="dc-summary-reasons">
                <div class="dc-reasons-title">📋 Decision Breakdown</div>
                <ul class="dc-reasons-list">${reasonsList || '<li>No specific reasons recorded</li>'}</ul>
            </div>
            <div class="dc-summary-completeness">
                <div class="dc-completeness-title">Pipeline Progress</div>
                <div class="dc-completeness-ring">
                    <svg viewBox="0 0 36 36" class="dc-circular-chart">
                        <path class="dc-circle-bg" d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"/>
                        <path class="dc-circle-fill ${gradeColorClass}" stroke-dasharray="${completeness}, 100" d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"/>
                        <text x="18" y="19.5" class="dc-circle-text">${completeness}%</text>
                    </svg>
                </div>
                <div class="dc-completeness-label">${stages.filter(s => s.status === 'passed' || s.status === 'executed').length}/${stages.length} stages</div>
            </div>
        </div>
    </div>`;

    // ═══ PIPELINE SECTION HEADER ═══
    html += '<div class="dc-pipeline-section">';
    html += '<div class="dc-pipeline-title">⚡ Pipeline Stage Breakdown</div>';
    html += '<div class="dc-pipeline">';

    stages.forEach((stage, si) => {
        const sc = stage.status === 'passed' || stage.status === 'executed' ? 'stage-pass' :
                   stage.status === 'blocked' ? 'stage-block' : 'stage-skip';
        const d = stage.details || {};

        html += `<div class="dc-stage ${sc}">`;
        html += `<div class="dc-stage-header">
            <span class="dc-stage-icon">${stage.icon}</span>
            <span class="dc-stage-name">${stage.name}</span>
            <span class="dc-stage-number">${si + 1}/${stages.length}</span>
            <span class="dc-stage-status-pill ${sc}">${stage.status.toUpperCase()}</span>
        </div>`;
        html += '<div class="dc-stage-body">';

        if (stage.name === 'Discovery & Filter') {
            html += '<div class="dc-kv-grid">';
            if (d.market_type) html += kvPill('Type', d.market_type);
            if (d.category) html += kvPill('Category', d.category);
            if (d.resolution_source) html += kvPill('Resolution', d.resolution_source);
            if (d.volume != null) html += kvPill('Volume', fmtD(d.volume));
            if (d.liquidity != null) html += kvPill('Liquidity', fmtD(d.liquidity));
            if (d.end_date) html += kvPill('Expiry', d.end_date.substring(0, 10));
            html += '</div>';
        }

        if (stage.name === 'Classification') {
            html += '<div class="dc-kv-grid">';
            html += kvPill('Category', d.category || 'UNKNOWN');
            html += kvPill('Subcategory', (d.subcategory || 'unknown').replace(/_/g, ' '));
            html += kvPill('Strategy', (d.search_strategy || '—').replace(/_/g, ' '));
            html += kvPill('Query Budget', d.recommended_queries || '—');
            html += kvPillClass('Worth Research', d.worth_researching ? 'YES' : 'NO',
                d.worth_researching ? 'pnl-positive' : 'pnl-negative');
            html += '</div>';

            // Researchability bar
            const rPct = d.researchability || 0;
            const rColor = rPct >= 70 ? '#00d68f' : rPct >= 40 ? '#f59e0b' : '#ef4444';
            html += `<div class="dc-quality-breakdown">
                <div class="dc-evidence-title">🔬 Researchability Score</div>
                <div class="dc-quality-row">
                    <span class="dc-quality-label">Score</span>
                    <div class="dc-quality-track"><div class="dc-quality-fill" style="width:${rPct}%;background:${rColor}"></div></div>
                    <span class="dc-quality-val">${rPct}%</span>
                </div>
            </div>`;

            // Researchability reasons
            if (d.researchability_reasons && d.researchability_reasons.length > 0) {
                html += '<div class="dc-research-summary">';
                html += '<div class="dc-evidence-title">💡 Why this score</div>';
                d.researchability_reasons.forEach(r => {
                    html += `<div class="dc-summary-text" style="margin-bottom:4px;">• ${escHtml(r)}</div>`;
                });
                html += '</div>';
            }

            // Primary sources
            if (d.primary_sources && d.primary_sources.length > 0) {
                html += '<div class="dc-research-summary">';
                html += '<div class="dc-evidence-title">📡 Primary Sources</div>';
                html += '<div class="dc-kv-grid">';
                d.primary_sources.forEach(s => {
                    html += `<div class="dc-kv"><span class="dc-kv-value" style="font-size:12px;">${escHtml(s)}</span></div>`;
                });
                html += '</div></div>';
            }

            // Tags
            if (d.tags && d.tags.length > 0) {
                html += '<div class="dc-research-summary">';
                html += '<div class="dc-evidence-title">🏷️ Tags</div>';
                html += '<div class="dc-kv-grid">';
                d.tags.forEach(t => {
                    const tagColor = t === 'scheduled_event' ? '#3b82f6' :
                                     t === 'high_signal' ? '#00d68f' :
                                     t === 'unpredictable' ? '#ef4444' :
                                     t === 'volatile' ? '#f59e0b' : '#a78bfa';
                    html += `<div class="dc-kv"><span class="dc-kv-value" style="font-size:11px;background:${tagColor}20;color:${tagColor};padding:2px 8px;border-radius:10px;">${escHtml(t.replace(/_/g, ' '))}</span></div>`;
                });
                html += '</div></div>';
            }
        }

        if (stage.name === 'Research') {
            html += '<div class="dc-kv-grid">';
            html += kvPill('Sources', d.num_sources || 0);
            html += kvPill('Quality', fmt(d.evidence_quality, 3));
            if (d.llm_quality_score != null && d.llm_quality_score > 0) {
                html += kvPill('LLM Quality', fmt(d.llm_quality_score, 2));
            }
            html += '</div>';

            // Quality breakdown bars
            if (d.quality_breakdown && Object.keys(d.quality_breakdown).length > 0) {
                const qb = d.quality_breakdown;
                html += '<div class="dc-quality-breakdown">';
                html += '<div class="dc-evidence-title">📊 Quality Breakdown</div>';
                const dims = [
                    {label: 'Recency', key: 'recency', color: '#00d68f'},
                    {label: 'Authority', key: 'authority', color: '#3b82f6'},
                    {label: 'Agreement', key: 'agreement', color: '#a78bfa'},
                    {label: 'Numeric Data', key: 'numeric_density', color: '#f59e0b'},
                    {label: 'Content Depth', key: 'content_depth', color: '#06b6d4'},
                ];
                dims.forEach(dim => {
                    const val = qb[dim.key] || 0;
                    const pct = (val * 100).toFixed(0);
                    html += `<div class="dc-quality-row">
                        <span class="dc-quality-label">${dim.label}</span>
                        <div class="dc-quality-track"><div class="dc-quality-fill" style="width:${pct}%;background:${dim.color}"></div></div>
                        <span class="dc-quality-val">${pct}%</span>
                    </div>`;
                });
                html += '</div>';
            }

            // Research summary
            if (d.summary) {
                html += `<div class="dc-research-summary">
                    <div class="dc-evidence-title">📋 Research Summary</div>
                    <div class="dc-summary-text">${escHtml(d.summary)}</div>
                </div>`;
            }

            // Evidence bullets with clickable source links
            if (d.evidence_bullets && d.evidence_bullets.length > 0) {
                html += '<div class="dc-evidence-list">';
                html += '<div class="dc-evidence-title">� Key Evidence from Sources</div>';
                d.evidence_bullets.forEach(b => {
                    const text = typeof b === 'string' ? b : (b.text || JSON.stringify(b));
                    const citation = (typeof b === 'object' && b.citation) ? b.citation : null;
                    const confidence = (typeof b === 'object' && b.confidence != null) ? b.confidence : null;
                    const isNumeric = (typeof b === 'object' && b.is_numeric) ? true : false;

                    // Impact/confidence badge
                    let impactBadge = '';
                    if (typeof b === 'object' && b.impact) {
                        const impClass = b.impact === 'supports' ? 'dc-impact-supports' :
                                        b.impact === 'opposes' ? 'dc-impact-opposes' : 'dc-impact-neutral';
                        const impIcon = b.impact === 'supports' ? '▲' : b.impact === 'opposes' ? '▼' : '●';
                        impactBadge = `<span class="dc-impact-badge ${impClass}">${impIcon} ${b.impact}</span>`;
                    }

                    // Numeric data badge
                    let numericBadge = '';
                    if (isNumeric && b.metric_name) {
                        const metricVal = b.metric_value || '';
                        const metricUnit = b.metric_unit || '';
                        numericBadge = `<span class="dc-numeric-badge">📊 ${escHtml(b.metric_name)}: ${escHtml(metricVal)}${metricUnit ? ' ' + escHtml(metricUnit) : ''}</span>`;
                    }

                    // Relevance indicator
                    const rel = (typeof b === 'object' && b.relevance != null)
                        ? `<span class="dc-relevance">${(b.relevance * 100).toFixed(0)}% relevant</span>`
                        : '';

                    // Confidence indicator
                    const confBadge = confidence != null
                        ? `<span class="dc-confidence-badge">${(confidence * 100).toFixed(0)}% conf.</span>`
                        : '';

                    // Source link with title
                    let sourceLink = '';
                    if (citation) {
                        const publisher = citation.publisher || '';
                        const title = citation.title || '';
                        const url = citation.url || '';
                        const date = citation.date || '';

                        if (url) {
                            const linkText = title || publisher || url;
                            sourceLink = `<a href="${escHtml(url)}" target="_blank" rel="noopener" class="dc-source-link" title="${escHtml(url)}">🔗 ${escHtml(linkText.substring(0, 80))}</a>`;
                        }
                        if (publisher) {
                            sourceLink += `<span class="dc-publisher">${escHtml(publisher)}</span>`;
                        }
                        if (date) {
                            sourceLink += `<span class="dc-source-date">${escHtml(date)}</span>`;
                        }
                    } else if (typeof b === 'object' && b.source) {
                        // Fallback for LLM evidence format {text, source, url, date, impact}
                        const url = b.url || '';
                        if (url) {
                            sourceLink = `<a href="${escHtml(url)}" target="_blank" rel="noopener" class="dc-source-link">🔗 ${escHtml(b.source || url)}</a>`;
                        } else if (b.source) {
                            sourceLink = `<span class="dc-publisher">${escHtml(b.source)}</span>`;
                        }
                        if (b.date) sourceLink += `<span class="dc-source-date">${escHtml(b.date)}</span>`;
                    }

                    html += `<div class="dc-evidence-item${isNumeric ? ' dc-evidence-numeric' : ''}">
                        <div class="dc-evidence-header">
                            ${impactBadge}${rel}${confBadge}${numericBadge}
                        </div>
                        <div class="dc-evidence-text">${escHtml(text)}</div>
                        <div class="dc-evidence-meta">${sourceLink}</div>
                    </div>`;
                });
                html += '</div>';
            }

            // Contradictions
            if (d.contradictions && d.contradictions.length > 0) {
                html += '<div class="dc-contradictions">';
                html += '<div class="dc-evidence-title">⚖️ Contradictions Found</div>';
                d.contradictions.forEach(c => {
                    html += `<div class="dc-contradiction-item">
                        <div class="dc-contradiction-claim">
                            <span class="dc-contra-label">Claim A:</span> ${escHtml(c.claim_a || '')}
                        </div>
                        <div class="dc-contradiction-claim">
                            <span class="dc-contra-label">Claim B:</span> ${escHtml(c.claim_b || '')}
                        </div>
                        ${c.description ? `<div class="dc-contradiction-desc">${escHtml(c.description)}</div>` : ''}
                    </div>`;
                });
                html += '</div>';
            }
        }

        if (stage.name === 'Forecast') {
            html += '<div class="dc-kv-grid">';
            html += kvPill('Implied P', fmtP((d.implied_prob || 0) * 100));
            html += kvPill('Model P', fmtP((d.model_prob || 0) * 100));
            const e = (d.edge || 0) * 100;
            html += kvPillClass('Edge', (e >= 0 ? '+' : '') + e.toFixed(2) + '%', e > 0 ? 'pnl-positive' : 'pnl-negative');
            html += kvPill('Confidence', d.confidence || '—');
            html += '</div>';

            // Edge visualization bar
            const implPct = ((d.implied_prob || 0) * 100).toFixed(1);
            const modPct = ((d.model_prob || 0) * 100).toFixed(1);
            html += `<div class="dc-prob-compare">
                <div class="dc-prob-bar-wrap">
                    <div class="dc-prob-label">Market ${implPct}%</div>
                    <div class="dc-prob-bar-track">
                        <div class="dc-prob-bar dc-prob-implied" style="width:${implPct}%"></div>
                    </div>
                </div>
                <div class="dc-prob-bar-wrap">
                    <div class="dc-prob-label">Model ${modPct}%</div>
                    <div class="dc-prob-bar-track">
                        <div class="dc-prob-bar dc-prob-model" style="width:${modPct}%"></div>
                    </div>
                </div>
            </div>`;

            if (d.reasoning) {
                html += `<div class="dc-reasoning">
                    <div class="dc-reasoning-title">💭 LLM Reasoning</div>
                    <div class="dc-reasoning-text">${escHtml(d.reasoning)}</div>
                </div>`;
            }
            if (d.invalidation_triggers && d.invalidation_triggers.length > 0) {
                html += `<div class="dc-triggers">
                    <div class="dc-triggers-title">⚠️ Invalidation Triggers</div>
                    <ul class="dc-triggers-list">
                        ${d.invalidation_triggers.map(t => `<li>${escHtml(t)}</li>`).join('')}
                    </ul>
                </div>`;
            }
        }

        if (stage.name === 'Risk Check') {
            html += '<div class="dc-kv-grid">';
            html += kvPillClass('Decision', d.decision || '—',
                (d.decision || '') === 'TRADE' ? 'pnl-positive' : 'pnl-negative');
            html += '</div>';
            if (d.violations && d.violations.length > 0) {
                html += '<div class="dc-violations">';
                html += '<div class="dc-violations-title">🚫 Violations</div>';
                d.violations.forEach(v => {
                    html += `<div class="dc-violation-item">${escHtml(v)}</div>`;
                });
                html += '</div>';
            }
            if (d.reasons && !d.violations?.length) {
                html += `<div class="dc-reason-text">${escHtml(d.reasons)}</div>`;
            }
        }

        if (stage.name === 'Execution') {
            html += '<div class="dc-kv-grid">';
            html += kvPill('Stake', d.stake_usd ? fmtD(d.stake_usd) : '—');
            html += kvPill('Status', d.order_status || '—');
            html += '</div>';
        }

        html += '</div></div>'; // close body, stage

        // Connector arrow between stages
        if (si < stages.length - 1) {
            html += '<div class="dc-stage-connector"><div class="dc-connector-line"></div><div class="dc-connector-arrow">›</div></div>';
        }
    });

    html += '</div>'; // close pipeline
    html += '</div>'; // close pipeline-section
    return html;
}

function kvPill(label, value) {
    return `<div class="dc-kv"><span class="dc-kv-label">${label}</span><span class="dc-kv-value">${value}</span></div>`;
}
function kvPillClass(label, value, cls) {
    return `<div class="dc-kv"><span class="dc-kv-label">${label}</span><span class="dc-kv-value ${cls}">${value}</span></div>`;
}
function escHtml(str) {
    const d = document.createElement('div');
    d.textContent = str || '';
    return d.innerHTML;
}

function toggleDecisionDetail(idx) {
    const detail = document.getElementById(`dc-detail-${idx}`);
    const icon = document.getElementById(`dc-expand-${idx}`);
    if (!detail) return;
    const open = detail.style.display !== 'none';
    detail.style.display = open ? 'none' : 'block';
    if (icon) icon.textContent = open ? '▶' : '▼';

    // Persist expanded state by stable key
    const card = detail.closest('.decision-card');
    if (card && card.dataset.decisionKey) {
        if (open) _expandedDecisionKeys.delete(card.dataset.decisionKey);
        else      _expandedDecisionKeys.add(card.dataset.decisionKey);
    }
}

function toggleDecisionExpand() {
    _decisionExpanded = !_decisionExpanded;
    const btn = $('#btn-expand-all');
    if (btn) btn.textContent = _decisionExpanded ? 'Collapse All' : 'Expand All';
    const details = document.querySelectorAll('.dc-detail');
    const icons = document.querySelectorAll('.dc-expand-icon');
    details.forEach(d => d.style.display = _decisionExpanded ? 'block' : 'none');
    icons.forEach(i => i.textContent = _decisionExpanded ? '▼' : '▶');
}

function filterDecisionCards() {
    const decisionSel = $('#decision-filter');
    const gradeSel = $('#di-grade-filter');
    const catSel = $('#di-category-filter');
    const searchInput = $('#di-search');

    const decFilter = decisionSel ? decisionSel.value.toUpperCase() : '';
    const gradeFilter = gradeSel ? gradeSel.value.toUpperCase() : '';
    const catFilter = catSel ? catSel.value.toUpperCase() : '';
    const searchTerm = searchInput ? searchInput.value.toLowerCase().trim() : '';

    const cards = document.querySelectorAll('.decision-card');
    let shown = 0;
    let total = cards.length;
    cards.forEach(c => {
        let visible = true;

        // Decision filter
        if (decFilter && c.dataset.decision !== decFilter) visible = false;

        // Grade filter
        if (visible && gradeFilter) {
            const cardGrade = (c.dataset.grade || '').toUpperCase();
            if (!cardGrade.startsWith(gradeFilter)) visible = false;
        }

        // Category filter
        if (visible && catFilter) {
            const cardCat = (c.dataset.category || '').toUpperCase();
            if (cardCat !== catFilter) visible = false;
        }

        // Search filter
        if (visible && searchTerm) {
            const cardText = (c.dataset.searchtext || '').toLowerCase();
            if (!cardText.includes(searchTerm)) visible = false;
        }

        c.style.display = visible ? '' : 'none';
        if (visible) shown++;
    });

    // Update filter count
    const countEl = $('#di-filter-count');
    if (countEl) {
        if (decFilter || gradeFilter || catFilter || searchTerm) {
            safeText(countEl, `${shown}/${total} shown`);
        } else {
            safeText(countEl, `${total} decisions`);
        }
    }
}

// ═══════════════════════════════════════════════════════════════
//  STRATEGIES & WALLETS TAB
// ═══════════════════════════════════════════════════════════════

let _swData = null;   // cached overview data
let _swPerfChart = null;

async function updateStrategiesTab() {
    const d = await apiFetch('/api/strategies-overview');
    if (!d) return;
    _swData = d;

    // KPIs
    const s = d.summary || {};
    safeText($('#sw-kpi-balance'), `$${(s.total_balance||0).toLocaleString()}`);
    const pnlVal = s.total_pnl || 0;
    const pnlEl = $('#sw-kpi-pnl');
    if (pnlEl) { pnlEl.textContent = `${pnlVal >= 0 ? '+' : ''}$${pnlVal.toLocaleString()}`; pnlEl.className = `sw-kpi-val ${pnlVal >= 0 ? 'pnl-positive' : 'pnl-negative'}`; }
    safeText($('#sw-kpi-paper'), String(s.paper_wallets || 0));
    safeText($('#sw-kpi-live'), String(s.live_wallets || 0));
    safeText($('#sw-kpi-strategies'), String(s.active_strategies || 0));
    safeText($('#sw-kpi-trades'), String(s.total_trades || 0));

    // Render wallet cards
    renderWalletGrid(d.wallets || []);

    // Render strategy cards
    renderStrategyGrid(d.strategies || []);

    // Populate wallet dropdowns in modals
    populateWalletDropdowns(d.wallets || []);
    populateStrategyDropdowns(d.strategies || []);
}

function renderWalletGrid(wallets) {
    const grid = $('#sw-wallet-grid');
    if (!grid) return;
    if (!wallets.length) {
        safeHTML(grid, '<div class="empty-state" style="padding:30px 0;">No wallets yet — create one to get started.</div>');
        return;
    }
    const html = wallets.map(w => {
        const typeBadge = w.wallet_type === 'paper'
            ? '<span class="sw-badge sw-badge-paper">📄 Paper</span>'
            : '<span class="sw-badge sw-badge-live">🔑 Live</span>';
        const pnl = w.total_pnl || 0;
        const pnlClass = pnl >= 0 ? 'pnl-positive' : 'pnl-negative';
        const pnlStr = `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`;
        const roi = w.initial_balance > 0 ? ((pnl / w.initial_balance) * 100).toFixed(1) : '0.0';
        const roiStr = `${pnl >= 0 ? '+' : ''}${roi}%`;
        const stratBadges = (w.strategies || []).map(s =>
            `<span class="sw-strat-badge" style="border-color:${s.color||'#4c8dff'};">${s.icon||'📋'} ${s.name}</span>`
        ).join('') || '<span class="sw-no-strat">No strategies assigned</span>';
        const isDefault = w.id === 'default-paper';
        return `<div class="sw-wallet-card" data-wallet-type="${w.wallet_type}" data-wallet-id="${w.id}" style="border-top: 3px solid ${w.color || '#4c8dff'}">
            <div class="sw-wc-header">
                <div class="sw-wc-name">${w.icon || '💰'} ${w.name}</div>
                ${typeBadge}
            </div>
            <div class="sw-wc-balance">$${(w.current_balance || 0).toLocaleString(undefined, {minimumFractionDigits: 2})}</div>
            <div class="sw-wc-stats">
                <div class="sw-wc-stat"><span class="sw-wc-stat-label">P&L</span><span class="${pnlClass}">${pnlStr}</span></div>
                <div class="sw-wc-stat"><span class="sw-wc-stat-label">ROI</span><span class="${pnlClass}">${roiStr}</span></div>
                <div class="sw-wc-stat"><span class="sw-wc-stat-label">Trades</span><span>${w.total_trades || 0}</span></div>
                <div class="sw-wc-stat"><span class="sw-wc-stat-label">Win Rate</span><span>${w.win_rate || 0}%</span></div>
                <div class="sw-wc-stat"><span class="sw-wc-stat-label">Open</span><span>${w.open_positions || 0}</span></div>
            </div>
            <div class="sw-wc-strategies">${stratBadges}</div>
            <div class="sw-wc-actions">
                <button class="sw-btn sw-btn-sm" onclick="showWalletPerformance('${w.id}')">📊 Performance</button>
                <button class="sw-btn sw-btn-sm" onclick="showAssignModal('${w.id}','wallet')">🔗 Assign Strategy</button>
                ${!isDefault ? `<button class="sw-btn sw-btn-sm sw-btn-danger" onclick="deleteWallet('${w.id}','${w.name}')">🗑️</button>` : ''}
            </div>
        </div>`;
    }).join('');
    safeHTML(grid, html);
}

function renderStrategyGrid(strategies) {
    const grid = $('#sw-strategy-grid');
    if (!grid) return;
    if (!strategies.length) {
        safeHTML(grid, '<div class="empty-state" style="padding:30px 0;">No strategies yet — create one to get started.</div>');
        return;
    }
    const riskIcons = {conservative: '🛡️', moderate: '⚖️', aggressive: '🔥'};
    const typeLabels = {ai_trading: 'AI Trading', manual: 'Manual', momentum: 'Momentum',
        mean_reversion: 'Mean Reversion', whale_follow: 'Whale Follow',
        arbitrage: 'Arbitrage', custom: 'Custom'};
    const html = strategies.map(s => {
        const walletBadges = (s.wallets || []).map(w =>
            `<span class="sw-wallet-badge" style="border-color:${w.color||'#4c8dff'};">
                ${w.icon||'💰'} ${w.name}
                <span class="sw-badge-alloc">$${(w.allocated_balance||0).toLocaleString()}</span>
                ${w.binding_active ? '' : '<span class="sw-badge-paused">⏸</span>'}
            </span>`
        ).join('') || '<span class="sw-no-strat">No wallets assigned</span>';
        const isDefault = s.id === 'default-ai';
        const statusDot = s.is_active ? '<span class="sw-status-dot sw-dot-active"></span>' : '<span class="sw-status-dot sw-dot-inactive"></span>';
        return `<div class="sw-strategy-card" style="border-top: 3px solid ${s.color || '#00e68a'}">
            <div class="sw-sc-header">
                <div class="sw-sc-name">${s.icon || '📋'} ${s.name} ${statusDot}</div>
                <span class="sw-badge sw-badge-type">${typeLabels[s.strategy_type] || s.strategy_type}</span>
            </div>
            <div class="sw-sc-meta">
                <span>${riskIcons[s.risk_profile] || '⚖️'} ${(s.risk_profile || 'moderate').charAt(0).toUpperCase() + (s.risk_profile || 'moderate').slice(1)}</span>
                ${s.description ? `<span class="sw-sc-desc">${s.description.substring(0, 80)}</span>` : ''}
            </div>
            <div class="sw-sc-wallets">${walletBadges}</div>
            <div class="sw-wc-actions">
                <button class="sw-btn sw-btn-sm" onclick="toggleStrategy('${s.id}', ${s.is_active ? 0 : 1})">${s.is_active ? '⏸ Pause' : '▶ Activate'}</button>
                <button class="sw-btn sw-btn-sm" onclick="showAssignModal('${s.id}','strategy')">🔗 Assign Wallet</button>
                ${!isDefault ? `<button class="sw-btn sw-btn-sm sw-btn-danger" onclick="deleteStrategy('${s.id}','${s.name}')">🗑️</button>` : ''}
            </div>
        </div>`;
    }).join('');
    safeHTML(grid, html);
}

function filterWalletCards() {
    const filter = $('#sw-wallet-filter')?.value || '';
    document.querySelectorAll('.sw-wallet-card').forEach(c => {
        if (!filter || c.dataset.walletType === filter) c.style.display = '';
        else c.style.display = 'none';
    });
}

// ─── Wallet CRUD ────────────────────────────────────────────────
let _swNewWalletType = 'paper';
let _swNewWalletColor = '#4c8dff';

function showCreateWalletModal() {
    _swNewWalletType = 'paper';
    _swNewWalletColor = '#4c8dff';
    const m = $('#modal-create-wallet');
    if (m) m.style.display = 'flex';
    const nameEl = $('#sw-new-wallet-name');
    if (nameEl) nameEl.value = '';
    const addrEl = $('#sw-new-wallet-address');
    if (addrEl) addrEl.value = '';
    const balEl = $('#sw-new-wallet-balance');
    if (balEl) balEl.value = '10000';
    const notesEl = $('#sw-new-wallet-notes');
    if (notesEl) notesEl.value = '';
    // Reset type toggle
    document.querySelectorAll('#modal-create-wallet .sw-type-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.type === 'paper');
    });
    document.querySelectorAll('#modal-create-wallet .sw-color-swatch').forEach(b => {
        b.classList.toggle('active', b.dataset.color === '#4c8dff');
    });
    if ($('#sw-address-group')) $('#sw-address-group').style.display = 'none';
    if ($('#sw-balance-group')) $('#sw-balance-group').style.display = '';
}

function selectWalletType(type) {
    _swNewWalletType = type;
    document.querySelectorAll('#modal-create-wallet .sw-type-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.type === type);
    });
    if ($('#sw-address-group')) $('#sw-address-group').style.display = type === 'live' ? '' : 'none';
    if ($('#sw-balance-group')) $('#sw-balance-group').style.display = type === 'paper' ? '' : 'none';
}

function selectWalletColor(el) {
    _swNewWalletColor = el.dataset.color;
    document.querySelectorAll('#modal-create-wallet .sw-color-swatch').forEach(b => {
        b.classList.toggle('active', b.dataset.color === _swNewWalletColor);
    });
}

async function createWallet() {
    const name = $('#sw-new-wallet-name')?.value?.trim();
    if (!name) { showToast('Wallet name is required', 'warning'); return; }
    const body = {
        name,
        wallet_type: _swNewWalletType,
        address: $('#sw-new-wallet-address')?.value?.trim() || '',
        initial_balance: parseFloat($('#sw-new-wallet-balance')?.value || '10000'),
        color: _swNewWalletColor,
        notes: $('#sw-new-wallet-notes')?.value?.trim() || '',
    };
    if (_swNewWalletType === 'live' && !body.address) {
        showToast('Live wallet requires an address', 'warning'); return;
    }
    const resp = await authFetch('/api/wallets', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const result = await resp.json();
    if (result.ok) {
        showToast(`Wallet "${name}" created`, 'success');
        closeSWModal('modal-create-wallet');
        updateStrategiesTab();
    } else {
        showToast(result.error || 'Failed to create wallet', 'error');
    }
}

async function deleteWallet(id, name) {
    if (!confirm(`Delete wallet "${name}"? This cannot be undone.`)) return;
    const resp = await authFetch(`/api/wallets/${id}`, {method: 'DELETE'});
    const result = await resp.json();
    if (result.ok) {
        showToast(`Wallet "${name}" deleted`, 'success');
        updateStrategiesTab();
    } else {
        showToast(result.error || 'Failed to delete wallet', 'error');
    }
}

// ─── Strategy CRUD ──────────────────────────────────────────────
let _swNewRiskProfile = 'moderate';

function showCreateStrategyModal() {
    _swNewRiskProfile = 'moderate';
    const m = $('#modal-create-strategy');
    if (m) m.style.display = 'flex';
    const nameEl = $('#sw-new-strat-name');
    if (nameEl) nameEl.value = '';
    const descEl = $('#sw-new-strat-desc');
    if (descEl) descEl.value = '';
    const typeEl = $('#sw-new-strat-type');
    if (typeEl) typeEl.value = 'ai_trading';
    const walletEl = $('#sw-new-strat-wallet');
    if (walletEl) walletEl.value = '';
    document.querySelectorAll('#modal-create-strategy .sw-risk-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.risk === 'moderate');
    });
    if ($('#sw-alloc-group')) $('#sw-alloc-group').style.display = 'none';
    // Populate wallet dropdown
    populateWalletDropdowns(_swData?.wallets || []);
}

function selectRiskProfile(risk) {
    _swNewRiskProfile = risk;
    document.querySelectorAll('#modal-create-strategy .sw-risk-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.risk === risk);
    });
}

async function createStrategy() {
    const name = $('#sw-new-strat-name')?.value?.trim();
    if (!name) { showToast('Strategy name is required', 'warning'); return; }
    const body = {
        name,
        strategy_type: $('#sw-new-strat-type')?.value || 'ai_trading',
        risk_profile: _swNewRiskProfile,
        description: $('#sw-new-strat-desc')?.value?.trim() || '',
    };
    const resp = await authFetch('/api/strategies', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const result = await resp.json();
    if (result.ok) {
        // Optionally assign to wallet
        const walletId = $('#sw-new-strat-wallet')?.value;
        if (walletId) {
            const alloc = parseFloat($('#sw-new-strat-alloc')?.value || '5000');
            await authFetch('/api/strategy-wallets', {method: 'POST', headers: {'Content-Type':'application/json'},
                body: JSON.stringify({action: 'bind', strategy_id: result.strategy_id, wallet_id: walletId, allocated_balance: alloc})});
        }
        showToast(`Strategy "${name}" created`, 'success');
        closeSWModal('modal-create-strategy');
        updateStrategiesTab();
    } else {
        showToast(result.error || 'Failed to create strategy', 'error');
    }
}

async function deleteStrategy(id, name) {
    if (!confirm(`Delete strategy "${name}"? This cannot be undone.`)) return;
    const resp = await authFetch(`/api/strategies/${id}`, {method: 'DELETE'});
    const result = await resp.json();
    if (result.ok) {
        showToast(`Strategy "${name}" deleted`, 'success');
        updateStrategiesTab();
    } else {
        showToast(result.error || 'Failed to delete strategy', 'error');
    }
}

async function toggleStrategy(id, newState) {
    await authFetch(`/api/strategies/${id}`, {method: 'PUT', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({is_active: newState})});
    updateStrategiesTab();
}

// ─── Assignment ─────────────────────────────────────────────────
function showAssignModal(id, type) {
    const m = $('#modal-assign-strategy');
    if (m) m.style.display = 'flex';
    populateStrategyDropdowns(_swData?.strategies || []);
    populateWalletDropdowns(_swData?.wallets || []);
    if (type === 'wallet') {
        const wSel = $('#sw-assign-wallet');
        if (wSel) wSel.value = id;
    } else {
        const sSel = $('#sw-assign-strategy');
        if (sSel) sSel.value = id;
    }
}

async function assignStrategyToWallet() {
    const stratId = $('#sw-assign-strategy')?.value;
    const walletId = $('#sw-assign-wallet')?.value;
    const alloc = parseFloat($('#sw-assign-alloc')?.value || '5000');
    if (!stratId || !walletId) { showToast('Select both strategy and wallet', 'warning'); return; }
    const resp = await authFetch('/api/strategy-wallets', {method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({action: 'bind', strategy_id: stratId, wallet_id: walletId, allocated_balance: alloc})});
    const result = await resp.json();
    if (result.ok) {
        showToast('Strategy assigned to wallet', 'success');
        closeSWModal('modal-assign-strategy');
        updateStrategiesTab();
    } else {
        showToast(result.error || 'Failed', 'error');
    }
}

function populateWalletDropdowns(wallets) {
    const selectors = ['#sw-new-strat-wallet', '#sw-assign-wallet'];
    selectors.forEach(sel => {
        const el = $(sel);
        if (!el) return;
        const cur = el.value;
        let opts = sel === '#sw-new-strat-wallet'
            ? '<option value="">No wallet (assign later)</option>'
            : '<option value="">Select wallet…</option>';
        wallets.forEach(w => {
            opts += `<option value="${w.id}" ${w.id === cur ? 'selected' : ''}>${w.icon||'💰'} ${w.name} (${w.wallet_type})</option>`;
        });
        safeHTML(el, opts);
    });
    // Show/hide alloc group
    const stratWalletSel = $('#sw-new-strat-wallet');
    if (stratWalletSel) {
        stratWalletSel.onchange = () => {
            const allocGrp = $('#sw-alloc-group');
            if (allocGrp) allocGrp.style.display = stratWalletSel.value ? '' : 'none';
        };
    }
}

function populateStrategyDropdowns(strategies) {
    const el = $('#sw-assign-strategy');
    if (!el) return;
    const cur = el.value;
    let opts = '<option value="">Select strategy…</option>';
    strategies.forEach(s => {
        opts += `<option value="${s.id}" ${s.id === cur ? 'selected' : ''}>${s.icon||'📋'} ${s.name}</option>`;
    });
    safeHTML(el, opts);
}

// ─── Per-Wallet Performance Panel ───────────────────────────────
async function showWalletPerformance(walletId) {
    const section = $('#sw-perf-section');
    if (!section) return;
    section.style.display = '';
    section.scrollIntoView({behavior: 'smooth', block: 'nearest'});

    const d = await apiFetch(`/api/wallets/${walletId}/performance`);
    if (!d) return;

    // Title
    safeText($('#sw-perf-title'), `📊 ${d.wallet?.name || 'Wallet'} Performance`);

    // KPI row
    const s = d.stats || {};
    const kpiHTML = `
        <div class="sw-perf-kpi"><span class="sw-pk-val ${s.total_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}">${s.total_pnl >= 0 ? '+' : ''}$${(s.total_pnl||0).toFixed(2)}</span><span class="sw-pk-label">Total P&L</span></div>
        <div class="sw-perf-kpi"><span class="sw-pk-val ${s.roi >= 0 ? 'pnl-positive' : 'pnl-negative'}">${s.roi >= 0 ? '+' : ''}${(s.roi||0).toFixed(1)}%</span><span class="sw-pk-label">ROI</span></div>
        <div class="sw-perf-kpi"><span class="sw-pk-val">${s.total_trades || 0}</span><span class="sw-pk-label">Trades</span></div>
        <div class="sw-perf-kpi"><span class="sw-pk-val">${s.win_rate || 0}%</span><span class="sw-pk-label">Win Rate</span></div>
        <div class="sw-perf-kpi"><span class="sw-pk-val pnl-positive">${s.avg_win >= 0 ? '+' : ''}$${(s.avg_win||0).toFixed(2)}</span><span class="sw-pk-label">Avg Win</span></div>
        <div class="sw-perf-kpi"><span class="sw-pk-val pnl-negative">$${(s.avg_loss||0).toFixed(2)}</span><span class="sw-pk-label">Avg Loss</span></div>
        <div class="sw-perf-kpi"><span class="sw-pk-val pnl-positive">$${(s.best_trade||0).toFixed(2)}</span><span class="sw-pk-label">Best Trade</span></div>
        <div class="sw-perf-kpi"><span class="sw-pk-val pnl-negative">$${(s.worst_trade||0).toFixed(2)}</span><span class="sw-pk-label">Worst Trade</span></div>
    `;
    safeHTML($('#sw-perf-kpis'), kpiHTML);

    // Equity curve chart
    const ec = d.equity_curve || [];
    const canvas = document.getElementById('sw-perf-chart');
    if (canvas && ec.length > 0) {
        if (_swPerfChart) _swPerfChart.destroy();
        _swPerfChart = new Chart(canvas, {
            type: 'line',
            data: {
                labels: ec.map(p => p.timestamp?.substring(5, 16) || ''),
                datasets: [{
                    label: 'Equity',
                    data: ec.map(p => p.equity || 0),
                    borderColor: d.wallet?.color || '#4c8dff',
                    backgroundColor: (d.wallet?.color || '#4c8dff') + '20',
                    fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2,
                }],
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    x: { ticks: { color: '#9499b3', maxTicksLimit: 8, font: {size: 10} }, grid: { display: false } },
                    y: { ticks: { color: '#9499b3', font: {size: 10}, callback: v => '$' + v.toLocaleString() }, grid: { color: 'rgba(255,255,255,0.03)' } },
                },
            },
        });
    } else if (canvas) {
        if (_swPerfChart) { _swPerfChart.destroy(); _swPerfChart = null; }
        canvas.parentElement.innerHTML = '<div class="empty-state" style="padding:20px;">No equity data yet — trades will populate this chart.</div>';
    }

    // Recent trades
    const trades = d.trades || [];
    if (trades.length === 0) {
        safeHTML($('#sw-perf-trades'), '<div class="empty-state" style="padding:16px;">No trades yet for this wallet.</div>');
    } else {
        let tHTML = '<table class="sw-trades-table"><thead><tr><th>Market</th><th>Side</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Status</th></tr></thead><tbody>';
        trades.slice(0, 20).forEach(t => {
            const pnlClass = (t.pnl || 0) >= 0 ? 'pnl-positive' : 'pnl-negative';
            tHTML += `<tr>
                <td title="${t.question || ''}">${(t.question || t.market_id || '').substring(0, 40)}…</td>
                <td>${t.side || '—'}</td>
                <td>$${(t.entry_price || 0).toFixed(3)}</td>
                <td>${t.exit_price ? '$' + t.exit_price.toFixed(3) : '—'}</td>
                <td class="${pnlClass}">${(t.pnl||0) >= 0 ? '+' : ''}$${(t.pnl||0).toFixed(2)}</td>
                <td><span class="sw-trade-status sw-ts-${t.status || 'open'}">${t.status || 'open'}</span></td>
            </tr>`;
        });
        tHTML += '</tbody></table>';
        safeHTML($('#sw-perf-trades'), tHTML);
    }
}

function closePerfPanel() {
    const section = $('#sw-perf-section');
    if (section) section.style.display = 'none';
}

// ─── Modal helpers ──────────────────────────────────────────────
function closeSWModal(id) {
    const m = document.getElementById(id);
    if (m) m.style.display = 'none';
}

// ═══════════════════════════════════════════════════════════════
//  PERFORMANCE ANALYTICS
// ═══════════════════════════════════════════════════════════════

let chartCategoryPnl = null;
let chartCalibration = null;
let chartModelAccuracy = null;

async function updateAnalytics() {
    try {
        const [analytics, calibration, modelAcc] = await Promise.all([
            authFetch('/api/analytics').then(r => r.json()).catch(() => null),
            authFetch('/api/calibration-curve').then(r => r.json()).catch(() => null),
            authFetch('/api/model-accuracy').then(r => r.json()).catch(() => null),
        ]);

        if (analytics) {
            // KPI Cards
            const wr = analytics.win_rate || 0;
            $('#ana-win-rate').textContent = `${(wr * 100).toFixed(1)}%`;
            $('#ana-win-rate-sub').textContent = `7d: ${(analytics.win_rate_7d * 100 || 0).toFixed(1)}% | 30d: ${(analytics.win_rate_30d * 100 || 0).toFixed(1)}%`;

            const sharpe = analytics.sharpe_ratio || 0;
            $('#ana-sharpe').textContent = sharpe.toFixed(2);
            $('#ana-sortino-sub').textContent = `Sortino: ${(analytics.sortino_ratio || 0).toFixed(2)}`;

            const pf = analytics.profit_factor || 0;
            $('#ana-profit-factor').textContent = pf === Infinity ? '∞' : pf.toFixed(2);
            const avgWin = (analytics.avg_win || 0).toFixed(2);
            const avgLoss = Math.abs(analytics.avg_loss || 0).toFixed(2);
            $('#ana-pf-sub').textContent = `Avg W: $${avgWin} / L: $${avgLoss}`;

            const maxDD = (analytics.max_drawdown_pct || 0) * 100;
            $('#ana-max-dd').textContent = `${maxDD.toFixed(2)}%`;
            $('#ana-calmar-sub').textContent = `Calmar: ${(analytics.calmar_ratio || 0).toFixed(2)}`;

            const roi = analytics.roi_pct || 0;
            $('#ana-roi').textContent = `${roi >= 0 ? '+' : ''}${roi.toFixed(2)}%`;
            $('#ana-roi').style.color = roi >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
            $('#ana-roi-sub').textContent = `Staked: $${(analytics.total_staked || 0).toFixed(2)}`;

            const streak = analytics.current_streak || 0;
            const streakEmoji = streak > 0 ? '🔥' : streak < 0 ? '❄️' : '—';
            $('#ana-streak').textContent = `${streakEmoji} ${Math.abs(streak)}`;
            $('#ana-streak-sub').textContent = `Best: ${analytics.best_streak || 0} | Worst: ${analytics.worst_streak || 0}`;

            // Leaderboard
            const lb = analytics.leaderboard || [];
            const lbBody = $('#leaderboard-body');
            if (lb.length > 0) {
                lbBody.innerHTML = lb.map(entry => {
                    const rankClass = entry.rank <= 3 ? `rank-${entry.rank}` : 'rank-other';
                    const roiColor = entry.roi_pct >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
                    return `<tr>
                        <td><span class="leaderboard-rank ${rankClass}">${entry.rank}</span></td>
                        <td>${entry.category}</td>
                        <td style="color:${roiColor};font-weight:700;">${entry.roi_pct >= 0 ? '+' : ''}${entry.roi_pct.toFixed(1)}%</td>
                        <td>${(entry.win_rate * 100).toFixed(1)}%</td>
                        <td style="color:${entry.total_pnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)'}">$${entry.total_pnl.toFixed(2)}</td>
                        <td>${entry.trades}</td>
                        <td>${(entry.avg_edge * 100).toFixed(2)}%</td>
                        <td>${entry.score.toFixed(1)}</td>
                    </tr>`;
                }).join('');
            }

            // Category PnL Chart
            const cats = analytics.category_stats || [];
            if (cats.length > 0) {
                const labels = cats.map(c => c.category);
                const pnlData = cats.map(c => c.total_pnl);
                const bgColors = pnlData.map(v => v >= 0
                    ? 'rgba(0, 214, 143, 0.7)'
                    : 'rgba(255, 77, 106, 0.7)');

                const ctx = document.getElementById('chart-category-pnl');
                if (chartCategoryPnl) chartCategoryPnl.destroy();
                chartCategoryPnl = new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels,
                        datasets: [{
                            label: 'P&L ($)',
                            data: pnlData,
                            backgroundColor: bgColors,
                            borderRadius: 4,
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: true,
                        plugins: { legend: { display: false } },
                        scales: {
                            x: { ticks: { color: '#8b8fa3', maxRotation: 45 }, grid: { display: false } },
                            y: { ticks: { color: '#8b8fa3' }, grid: { color: 'rgba(42,45,58,0.5)' } },
                        },
                    },
                });
            }
        }

        // Calibration Chart
        if (calibration && calibration.bins && calibration.bins.length > 0) {
            const bins = calibration.bins;
            const labels = bins.map(b => b.range);
            const forecasted = bins.map(b => b.avg_forecast);
            const actual = bins.map(b => b.avg_outcome);

            const ctx = document.getElementById('chart-calibration');
            if (chartCalibration) chartCalibration.destroy();
            chartCalibration = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels,
                    datasets: [
                        {
                            label: 'Forecast Avg',
                            data: forecasted,
                            backgroundColor: 'rgba(76, 141, 255, 0.6)',
                            borderRadius: 3,
                        },
                        {
                            label: 'Actual Outcome',
                            data: actual,
                            backgroundColor: 'rgba(0, 214, 143, 0.6)',
                            borderRadius: 3,
                        },
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: true,
                    plugins: {
                        legend: { labels: { color: '#8b8fa3' } },
                    },
                    scales: {
                        x: { ticks: { color: '#8b8fa3' }, grid: { display: false } },
                        y: { ticks: { color: '#8b8fa3' }, grid: { color: 'rgba(42,45,58,0.5)' }, min: 0, max: 1 },
                    },
                },
            });
        }

        // Model Accuracy Chart
        if (modelAcc && modelAcc.models && modelAcc.models.length > 0) {
            const models = modelAcc.models;
            const labels = models.map(m => m.model_name.split('/').pop().split('-').slice(0, 2).join('-'));
            const brierData = models.map(m => m.brier_score);

            const ctx = document.getElementById('chart-model-accuracy');
            if (chartModelAccuracy) chartModelAccuracy.destroy();
            chartModelAccuracy = new Chart(ctx, {
                type: 'doughnut',
                data: {
                    labels,
                    datasets: [{
                        data: models.map(m => m.total_forecasts),
                        backgroundColor: [
                            'rgba(76, 141, 255, 0.7)',
                            'rgba(168, 85, 247, 0.7)',
                            'rgba(255, 159, 67, 0.7)',
                            'rgba(0, 214, 143, 0.7)',
                            'rgba(244, 114, 182, 0.7)',
                        ],
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: true,
                    plugins: {
                        legend: { labels: { color: '#8b8fa3', font: { size: 11 } } },
                        tooltip: {
                            callbacks: {
                                afterLabel: (ctx) => {
                                    const m = models[ctx.dataIndex];
                                    return `Brier: ${m.brier_score.toFixed(4)} | Err: ${m.avg_error.toFixed(4)}`;
                                }
                            }
                        }
                    },
                },
            });
        }
    } catch (e) {
        console.warn('Analytics update error:', e);
    }
}

// ═══════════════════════════════════════════════════════════════
//  MARKET REGIME
// ═══════════════════════════════════════════════════════════════

async function updateRegime() {
    try {
        const data = await authFetch('/api/regime').then(r => r.json()).catch(() => null);
        if (!data || !data.current) return;

        const c = data.current;
        const regimeEl = $('#regime-current');
        regimeEl.textContent = c.regime || 'NORMAL';
        regimeEl.className = 'card-value';

        const conf = c.confidence || 0;
        $('#regime-confidence').textContent = `Confidence: ${(conf * 100).toFixed(1)}%`;

        const kelly = c.kelly_multiplier || 1;
        $('#regime-kelly').textContent = `${kelly.toFixed(2)}x`;
        $('#regime-kelly').style.color = kelly < 0.8 ? 'var(--accent-red)' : kelly > 1.1 ? 'var(--accent-green)' : 'var(--text)';

        const sizeMult = c.size_multiplier || 1;
        $('#regime-size-mult').textContent = `Size: ${sizeMult.toFixed(2)}x`;

        $('#regime-explanation').textContent = c.explanation || '—';
        $('#regime-detected-at').textContent = c.detected_at ? `Detected: ${new Date(c.detected_at).toLocaleString()}` : '—';
    } catch (e) {
        console.warn('Regime update error:', e);
    }
}

// ═══════════════════════════════════════════════════════════════
//  WHALE / SMART MONEY TERMINAL (Pro)
// ═══════════════════════════════════════════════════════════════

let _whaleDirectionChart = null;
let _whaleMarketsChart = null;
let _whaleCategoryChart = null;
let _whaleData = null;
let _whaleRefreshInterval = null;
let _whaleRefreshCountdown = 30;

// ── Category config ──────────────────────────────────────────
const CATEGORY_META = {
    'NBA':      { icon: '🏀', color: '#ff6b35' },
    'NFL':      { icon: '🏈', color: '#4c8dff' },
    'Soccer':   { icon: '⚽', color: '#00e68a' },
    'Politics': { icon: '🏛️', color: '#a855f7' },
    'Olympics': { icon: '🏅', color: '#ffd700' },
    'MLB':      { icon: '⚾', color: '#e74c3c' },
    'Golf':     { icon: '⛳', color: '#2ecc71' },
    'Crypto':   { icon: '₿', color: '#f7931a' },
    'Other':    { icon: '📦', color: '#9499b3' },
};

async function updateWhaleTracker() {
    const data = await apiFetch('/api/whale-activity');
    if (!data) return;
    _whaleData = data;

    const s = data.summary || {};

    // ── Pulse indicator ──────────────────────────────────────
    const pulse = $('#whale-pulse');
    if (pulse) {
        const lastScan = s.last_scan;
        if (lastScan) {
            const age = (Date.now() - new Date(lastScan).getTime()) / 60000;
            if (age < 5)       { pulse.textContent = '● LIVE'; pulse.className = 'whale-pulse whale-pulse-live'; }
            else if (age < 30) { pulse.textContent = '● ACTIVE'; pulse.className = 'whale-pulse whale-pulse-active'; }
            else               { pulse.textContent = '● IDLE'; pulse.className = 'whale-pulse whale-pulse-idle'; }
        }
    }

    // ── KPI Cards ────────────────────────────────────────────
    safeText($('#whale-total-wallets'), String(s.total_wallets || 0));
    safeText($('#whale-total-positions'), `${s.total_positions || 0} positions`);
    safeText($('#whale-total-signals'), String(s.total_signals || 0));
    safeText($('#whale-signal-breakdown'), `Strong: ${s.strong_signals || 0} · Mod: ${s.moderate_signals || 0}`);

    const smi = s.smart_money_index || 50;
    safeText($('#whale-smi'), String(Math.round(smi)));
    const smiLabel = smi >= 70 ? 'Very Bullish' : smi >= 60 ? 'Bullish' : smi >= 40 ? 'Neutral' : smi >= 30 ? 'Bearish' : 'Very Bearish';
    safeText($('#whale-smi-label'), smiLabel);

    const netFlow = s.net_flow || 0;
    const flowEl = $('#whale-net-flow');
    if (flowEl) {
        flowEl.textContent = `${netFlow >= 0 ? '+' : ''}$${Math.abs(netFlow).toLocaleString(undefined, {maximumFractionDigits:0})}`;
        flowEl.className = 'whale-kpi-value ' + (netFlow >= 0 ? 'whale-positive' : 'whale-negative');
    }
    safeText($('#whale-flow-detail'), `In: $${(s.flow_in || 0).toLocaleString(undefined, {maximumFractionDigits:0})} · Out: $${(s.flow_out || 0).toLocaleString(undefined, {maximumFractionDigits:0})}`);

    safeText($('#whale-avg-winrate'), `${((s.avg_win_rate || 0) * 100).toFixed(1)}%`);
    safeText($('#whale-total-pnl'), `Total P&L: $${((s.total_whale_pnl || 0) / 1000000).toFixed(1)}M`);

    safeText($('#whale-top-conv-score'), String(s.top_conviction_score || 0));
    safeText($('#whale-top-conv-market'), s.top_conviction_market || '—');

    safeText($('#whale-last-scan'), s.last_scan ? `Scan: ${shortDate(s.last_scan)}` : 'Last scan: —');
    safeText($('#whale-scan-count'), `${s.total_wallets || 0} wallets`);

    // ── Smart Money Index Gauge ──────────────────────────────
    const smiPct = Math.max(0, Math.min(100, smi));
    const smiFill = $('#whale-smi-fill');
    const smiMarker = $('#whale-smi-marker');
    if (smiFill) smiFill.style.width = `${smiPct}%`;
    if (smiMarker) smiMarker.style.left = `${smiPct}%`;
    if (smiFill) {
        if (smiPct >= 60) smiFill.style.background = 'linear-gradient(90deg, #3b82f6, #00e68a)';
        else if (smiPct <= 40) smiFill.style.background = 'linear-gradient(90deg, #ff4d6a, #ff9f43)';
        else smiFill.style.background = 'linear-gradient(90deg, #ff9f43, #3b82f6)';
    }
    const descEl = $('#whale-smi-desc');
    if (descEl) {
        const bd = s.direction_distribution || {};
        if (smi >= 65) descEl.textContent = `Whales are strongly bullish — ${bd.bullish || 0} bullish vs ${bd.bearish || 0} bearish signals. Smart money is positioning for upside.`;
        else if (smi <= 35) descEl.textContent = `Whales are bearish — ${bd.bearish || 0} bearish vs ${bd.bullish || 0} bullish signals. Smart money is hedging or shorting.`;
        else descEl.textContent = `Whale sentiment is mixed — ${bd.bullish || 0} bullish vs ${bd.bearish || 0} bearish signals. No clear directional bias.`;
    }

    // ── Flow Breakdown Bars ──────────────────────────────────
    const ab = s.action_breakdown || {};
    const maxAction = Math.max(ab.new_entries || 0, ab.size_increases || 0, ab.size_decreases || 0, ab.exits || 0, 1);
    _setFlowBar('whale-flow-entries', 'whale-flow-entries-n', ab.new_entries || 0, maxAction);
    _setFlowBar('whale-flow-incr', 'whale-flow-incr-n', ab.size_increases || 0, maxAction);
    _setFlowBar('whale-flow-decr', 'whale-flow-decr-n', ab.size_decreases || 0, maxAction);
    _setFlowBar('whale-flow-exits', 'whale-flow-exits-n', ab.exits || 0, maxAction);

    // ── Charts (fixed height containers) ─────────────────────
    _renderWhaleDirectionChart(s.direction_distribution || {});
    _renderWhaleMarketsChart(data.top_markets || []);
    _renderWhaleCategoryChart(data.categories || []);

    // ── Risk Alert Ticker ────────────────────────────────────
    _renderWhaleAlertTicker(data.risk_alerts || []);

    // ── Momentum & Velocity ──────────────────────────────────
    _renderWhaleMomentum(data.momentum || []);
    _renderWhaleVelocity(data.velocity || {});
    _renderHerdIndex(data.herd_index || 0);

    // ── Activity Timeline ────────────────────────────────────
    _renderActivityTimeline(data.activity_timeline || []);

    // ── Accumulation / Distribution ──────────────────────────
    _renderAccumDist(data.accumulation_distribution || []);

    // ── Whale Overlap Matrix ─────────────────────────────────
    _renderWhaleOverlap(data.whale_overlap || []);

    // ── Whale Tier Summary ───────────────────────────────────
    _renderTierSummary(data.tier_summary || {});

    // ── High Consensus Alerts ────────────────────────────────
    _renderHighConsensus(data.high_consensus_signals || []);

    // ── Conviction Signals Table ─────────────────────────────
    _renderConvictionTable(data.conviction_signals || []);

    // ── Live Activity Feed ───────────────────────────────────
    _renderActivityFeed(data.recent_deltas || []);

    // ── Whale Leaderboard ────────────────────────────────────
    _renderWhaleLeaderboard(data.tracked_wallets || []);

    // ── Alert History ────────────────────────────────────────
    _renderAlertHistory(data.alert_history || []);

    // ── Liquid Market Scanner ────────────────────────────────
    loadScannerStatus();

    // ── Start refresh countdown ──────────────────────────────
    _startWhaleRefreshTimer();
}

function _setFlowBar(barId, countId, value, max) {
    const bar = document.getElementById(barId);
    const cnt = document.getElementById(countId);
    if (bar) bar.style.width = `${(value / max) * 100}%`;
    if (cnt) cnt.textContent = String(value);
}

function _startWhaleRefreshTimer() {
    if (_whaleRefreshInterval) clearInterval(_whaleRefreshInterval);
    _whaleRefreshCountdown = 30;
    const el = $('#whale-refresh-timer');
    _whaleRefreshInterval = setInterval(() => {
        _whaleRefreshCountdown--;
        if (el) el.textContent = `⟳ ${_whaleRefreshCountdown}s`;
        if (_whaleRefreshCountdown <= 0) {
            clearInterval(_whaleRefreshInterval);
            _whaleRefreshInterval = null;
        }
    }, 1000);
}

// ── Direction Doughnut Chart ─────────────────────────────────
function _renderWhaleDirectionChart(dist) {
    const canvas = document.getElementById('whale-direction-chart');
    if (!canvas) return;
    const bull = dist.bullish || 0;
    const bear = dist.bearish || 0;
    if (_whaleDirectionChart) _whaleDirectionChart.destroy();
    _whaleDirectionChart = new Chart(canvas, {
        type: 'doughnut',
        data: {
            labels: ['Bullish', 'Bearish'],
            datasets: [{
                data: [bull, bear],
                backgroundColor: ['#00e68a', '#ff4d6a'],
                borderColor: ['rgba(0,230,138,0.3)', 'rgba(255,77,106,0.3)'],
                borderWidth: 2,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            cutout: '65%',
            plugins: {
                legend: { position: 'bottom', labels: { color: '#9499b3', padding: 8, font: { size: 11 } } },
                tooltip: {
                    callbacks: {
                        label: ctx => {
                            const total = bull + bear;
                            const pct = total > 0 ? ((ctx.raw / total) * 100).toFixed(1) : 0;
                            return `${ctx.label}: ${ctx.raw} (${pct}%)`;
                        }
                    }
                },
            },
        },
    });
}

// ── Top Markets Bar Chart (FIXED — no expanding) ─────────────
let _whaleMarketsData = []; // Store for click handler
function _renderWhaleMarketsChart(topMarkets) {
    const canvas = document.getElementById('whale-markets-chart');
    if (!canvas) return;
    if (_whaleMarketsChart) _whaleMarketsChart.destroy();
    const top5 = topMarkets.slice(0, 5);
    _whaleMarketsData = top5; // Store for click lookup
    const labels = top5.map(m => (m.title || '').substring(0, 25) || 'Unknown');
    const values = top5.map(m => m.total_usd || 0);
    const colors = top5.map(m => m.direction === 'BULLISH' ? '#00e68a' : '#ff4d6a');
    _whaleMarketsChart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels,
            datasets: [{
                label: 'Whale $',
                data: values,
                backgroundColor: colors.map(c => c + '80'),
                borderColor: colors,
                borderWidth: 1,
                borderRadius: 3,
                barThickness: 18,
                maxBarThickness: 22,
            }],
        },
        options: {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: false,
            onClick: (_evt, elements) => {
                if (elements.length > 0) {
                    const idx = elements[0].index;
                    const mkt = _whaleMarketsData[idx];
                    if (mkt && mkt.market_slug) openMarketDetail(mkt.market_slug);
                }
            },
            plugins: {
                legend: { display: false },
                tooltip: { callbacks: { label: ctx => `$${ctx.raw.toLocaleString()}` } },
            },
            scales: {
                x: {
                    ticks: { color: '#9499b3', font: { size: 10 }, callback: v => `$${(v/1000).toFixed(0)}k` },
                    grid: { color: 'rgba(255,255,255,0.03)' },
                },
                y: {
                    ticks: { color: '#9499b3', font: { size: 10 }, cursor: 'pointer' },
                    grid: { display: false },
                },
            },
            onHover: (event, elements) => {
                event.native.target.style.cursor = elements.length > 0 ? 'pointer' : 'default';
            },
        },
    });
}

// ── Category Pie Chart ───────────────────────────────────────
function _renderWhaleCategoryChart(categories) {
    const canvas = document.getElementById('whale-category-chart');
    if (!canvas) return;
    if (_whaleCategoryChart) _whaleCategoryChart.destroy();
    if (!categories.length) return;
    const labels = categories.map(c => `${(CATEGORY_META[c.category] || {}).icon || '📦'} ${c.category}`);
    const values = categories.map(c => c.total_usd || 0);
    const colors = categories.map(c => (CATEGORY_META[c.category] || {}).color || '#9499b3');
    _whaleCategoryChart = new Chart(canvas, {
        type: 'doughnut',
        data: {
            labels,
            datasets: [{
                data: values,
                backgroundColor: colors.map(c => c + 'cc'),
                borderColor: colors.map(c => c + '40'),
                borderWidth: 1,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            cutout: '60%',
            plugins: {
                legend: { position: 'bottom', labels: { color: '#9499b3', padding: 6, font: { size: 10 }, boxWidth: 12 } },
                tooltip: { callbacks: { label: ctx => `$${ctx.raw.toLocaleString(undefined, {maximumFractionDigits:0})} (${categories[ctx.dataIndex]?.count || 0} signals)` } },
            },
        },
    });
}

// ── Activity Timeline Heatmap ────────────────────────────────
function _renderActivityTimeline(timeline) {
    const grid = $('#whale-timeline-grid');
    if (!grid || !timeline.length) return;
    const maxCount = Math.max(...timeline.map(t => t.count), 1);
    const html = timeline.map((t, i) => {
        let level = 0;
        if (t.count > 0) level = 1;
        if (t.count >= maxCount * 0.25) level = 2;
        if (t.count >= maxCount * 0.5) level = 3;
        if (t.count >= maxCount * 0.75) level = 4;
        const hourStr = i === 0 ? 'Now' : `${i}h`;
        const tooltip = `${t.count} actions, $${(t.usd || 0).toLocaleString(undefined, {maximumFractionDigits:0})}`;
        return `<div class="whale-tl-cell whale-tl-${level}" title="${hourStr} ago: ${tooltip}">
            <span class="whale-tl-hour">${hourStr}</span>
        </div>`;
    }).join('');
    safeHTML(grid, html);
}

function _renderHighConsensus(signals) {
    const grid = $('#whale-consensus-grid');
    const badge = $('#whale-consensus-count');
    if (!grid) return;
    if (badge) badge.textContent = String(signals.length);
    if (signals.length === 0) {
        safeHTML(grid, '<div class="empty-state" style="grid-column:1/-1;">No high consensus signals yet</div>');
        return;
    }
    const html = signals.map(s => {
        const dirCls = s.direction === 'BULLISH' ? 'whale-consensus-bull' : 'whale-consensus-bear';
        const dirIcon = s.direction === 'BULLISH' ? '🟢' : '🔴';
        const names = (s.whale_names || []).slice(0, 4).join(', ');
        return `<div class="whale-consensus-card ${dirCls}">
            <div class="whale-consensus-top">
                <span class="whale-consensus-dir">${dirIcon} ${s.direction}</span>
                <span class="whale-consensus-conv">${s.conviction}/100</span>
            </div>
            <div class="whale-consensus-title">${(s.title || '—').substring(0, 60)}</div>
            <div class="whale-consensus-meta">
                <span>🐋 ${s.whale_count} whales</span>
                <span>💰 $${(s.total_usd || 0).toLocaleString(undefined, {maximumFractionDigits:0})}</span>
            </div>
            <div class="whale-consensus-names">${names}</div>
        </div>`;
    }).join('');
    safeHTML(grid, html);
}

// ── Conviction Signals Table (Pro — with categories, edge, freshness, trend) ──
function _renderConvictionTable(signals) {
    const tbody = $('#conviction-body');
    if (!tbody) return;
    if (signals.length === 0) {
        safeHTML(tbody, '<tr><td colspan="14" class="empty-state">No conviction signals yet — run a wallet scan to populate</td></tr>');
        return;
    }
    const rows = signals.map(sig => {
        const strengthClass = sig.signal_strength === 'STRONG' ? 'whale-str-strong'
            : sig.signal_strength === 'MODERATE' ? 'whale-str-moderate' : 'whale-str-weak';
        const dirIcon = sig.direction === 'BULLISH' ? '🟢' : '🔴';
        const convPct = Math.min(100, sig.conviction_score || 0);
        const convColor = convPct >= 70 ? '#00e68a' : convPct >= 45 ? '#ff9f43' : '#5a5f78';

        // Category pill
        const cat = sig.category || 'Other';
        const catMeta = CATEGORY_META[cat] || { icon: '📦', color: '#9499b3' };
        const catHtml = `<span class="whale-cat-pill" style="border-color:${catMeta.color}40;color:${catMeta.color}">${catMeta.icon} ${cat}</span>`;

        // Edge
        const edge = sig.price_edge_pct;
        let edgeHtml = '<span class="whale-edge-na">—</span>';
        if (edge !== null && edge !== undefined) {
            const edgeCls = edge > 5 ? 'whale-edge-pos' : edge < -5 ? 'whale-edge-neg' : 'whale-edge-flat';
            edgeHtml = `<span class="${edgeCls}">${edge > 0 ? '+' : ''}${edge.toFixed(1)}%</span>`;
        }

        // Freshness
        const fresh = sig.freshness || 'UNKNOWN';
        const freshCls = { LIVE: 'whale-fresh-live', FRESH: 'whale-fresh-fresh', RECENT: 'whale-fresh-recent', AGING: 'whale-fresh-aging', STALE: 'whale-fresh-stale' }[fresh] || 'whale-fresh-stale';
        const freshHtml = `<span class="whale-fresh-pill ${freshCls}">${fresh}</span>`;

        // Trend indicator (NEW)
        const trend = sig.trend || 'UNKNOWN';
        const trendIconMap = { RISING: '📈', FALLING: '📉', STABLE: '➡️', NEW: '🆕' };
        const trendIcon = trendIconMap[trend] || '❓';
        const trendCls = { RISING: 'whale-trend-rising', FALLING: 'whale-trend-falling', STABLE: 'whale-trend-stable', NEW: 'whale-trend-new' }[trend] || 'whale-trend-unknown';
        let trendTooltip = trend;
        if (sig.conviction_delta != null) {
            trendTooltip += ` (${sig.conviction_delta > 0 ? '+' : ''}${sig.conviction_delta.toFixed(1)})`;
        }
        if (sig.whale_count_delta != null && sig.whale_count_delta !== 0) {
            trendTooltip += ` | Whales: ${sig.whale_count_delta > 0 ? '+' : ''}${sig.whale_count_delta}`;
        }
        const trendHtml = `<span class="whale-trend-pill ${trendCls}" title="${trendTooltip}">${trendIcon} ${trend}</span>`;

        // Signal age (NEW)
        const ageH = sig.signal_age_hours;
        let ageHtml = '<span class="whale-age-na">—</span>';
        if (ageH != null) {
            if (ageH < 1) ageHtml = `<span class="whale-age-live">${Math.round(ageH * 60)}m</span>`;
            else if (ageH < 24) ageHtml = `<span class="whale-age-recent">${ageH.toFixed(1)}h</span>`;
            else ageHtml = `<span class="whale-age-old">${(ageH / 24).toFixed(1)}d</span>`;
        }

        return `<tr data-strength="${sig.signal_strength || ''}" data-direction="${sig.direction || ''}" data-category="${cat}" data-market="${(sig.title || '').toLowerCase()}" data-trend="${trend}">
            <td title="${sig.market_slug || ''}"><span class="wm-star-inline ${sig.is_starred ? 'wm-starred' : ''}" onclick="event.stopPropagation();toggleStarInline(this,'market','${(sig.market_slug||'').replace(/'/g,"\\'")}','${(sig.title||'').substring(0,40).replace(/'/g,"\\'")}')" title="Star">★</span> <a class="wm-clickable-market" href="#" onclick="event.preventDefault();openMarketDetail('${(sig.market_slug || '').replace(/'/g, "\\'")}')">${(sig.title || sig.market_slug || '—').substring(0, 45)}</a></td>
            <td>${catHtml}</td>
            <td>${sig.outcome || '—'}</td>
            <td><strong>${sig.whale_count || 0}</strong></td>
            <td class="whale-mono">$${(sig.total_whale_usd || 0).toLocaleString(undefined, {maximumFractionDigits:0})}</td>
            <td class="whale-mono">${fmt(sig.avg_whale_price, 3)}</td>
            <td class="whale-mono">${fmt(sig.current_price, 3)}</td>
            <td class="whale-mono">${edgeHtml}</td>
            <td>
                <div class="whale-conv-bar-wrap">
                    <div class="whale-conv-bar" style="width:${convPct}%;background:${convColor};"></div>
                    <span class="whale-conv-label">${Math.round(convPct)}</span>
                </div>
            </td>
            <td>${trendHtml}</td>
            <td><span class="whale-strength-pill ${strengthClass}">${sig.signal_strength || '—'}</span></td>
            <td>${freshHtml}</td>
            <td>${ageHtml}</td>
            <td>${dirIcon} ${sig.direction || '—'}</td>
        </tr>`;
    }).join('');
    safeHTML(tbody, rows);
}

function filterWhaleSignals() {
    const filter = $('#whale-signal-filter')?.value || 'all';
    const catFilter = $('#whale-category-filter')?.value || 'all';
    const search = ($('#whale-signal-search')?.value || '').toLowerCase();
    const rows = document.querySelectorAll('#conviction-body tr[data-strength]');
    rows.forEach(row => {
        const strength = row.dataset.strength;
        const direction = row.dataset.direction;
        const category = row.dataset.category || '';
        const market = row.dataset.market || '';
        let show = true;
        if (filter === 'STRONG' && strength !== 'STRONG') show = false;
        if (filter === 'MODERATE' && strength === 'WEAK') show = false;
        if (filter === 'BULLISH' && direction !== 'BULLISH') show = false;
        if (filter === 'BEARISH' && direction !== 'BEARISH') show = false;
        if (catFilter !== 'all' && category !== catFilter) show = false;
        if (search && !market.includes(search)) show = false;
        row.style.display = show ? '' : 'none';
    });
}

function _renderActivityFeed(deltas) {
    const feed = $('#whale-activity-feed');
    if (!feed) return;
    if (deltas.length === 0) {
        safeHTML(feed, '<div class="empty-state">No whale activity detected yet</div>');
        return;
    }
    const actionMeta = {
        'NEW_ENTRY':      { icon: '🟢', label: 'entered', cls: 'whale-feed-entry' },
        'EXIT':           { icon: '🔴', label: 'exited', cls: 'whale-feed-exit' },
        'SIZE_INCREASE':  { icon: '📈', label: 'increased position in', cls: 'whale-feed-increase' },
        'SIZE_DECREASE':  { icon: '📉', label: 'decreased position in', cls: 'whale-feed-decrease' },
    };
    const html = deltas.slice(0, 50).map(d => {
        const meta = actionMeta[d.action] || { icon: '⚪', label: d.action, cls: '' };
        const valStr = Math.abs(d.value_change_usd || 0) >= 1
            ? `$${Math.abs(d.value_change_usd).toLocaleString(undefined, {maximumFractionDigits:0})}` : '';
        const sizeStr = Math.abs(d.size_change || 0) >= 1
            ? `${d.size_change > 0 ? '+' : ''}${d.size_change.toLocaleString(undefined, {maximumFractionDigits:0})} shares` : '';
        return `<div class="whale-feed-item ${meta.cls}" data-action="${d.action}" onclick="openActivityDetail(${JSON.stringify(d).replace(/"/g,'&quot;')})">
            <div class="whale-feed-icon">${meta.icon}</div>
            <div class="whale-feed-body">
                <a class="wm-clickable-name" href="#" onclick="event.preventDefault();event.stopPropagation();openWhaleProfile('${d.wallet_address || ''}')">${d.wallet_name || '—'}</a>
                ${meta.label}
                <a class="wm-clickable-market" href="#" onclick="event.preventDefault();event.stopPropagation();openMarketDetail('${(d.market_slug || '').replace(/'/g, "\\'")}')">${(d.title || d.market_slug || '—').substring(0, 50)}</a>
                <span class="whale-feed-outcome">(${d.outcome || '—'})</span>
                ${valStr ? `<span class="whale-feed-value"> — ${valStr}</span>` : ''}
                ${sizeStr ? `<span class="whale-feed-shares"> · ${sizeStr}</span>` : ''}
            </div>
            <div class="whale-feed-time">${shortDate(d.detected_at)}</div>
        </div>`;
    }).join('');
    safeHTML(feed, html);
}

function filterWhaleActivity() {
    const filter = $('#whale-activity-filter')?.value || 'all';
    const items = document.querySelectorAll('.whale-feed-item');
    items.forEach(item => {
        if (filter === 'all') { item.style.display = ''; return; }
        item.style.display = item.dataset.action === filter ? '' : 'none';
    });
}

function _renderWhaleLeaderboard(wallets) {
    const container = $('#whale-leaderboard');
    if (!container) return;
    if (wallets.length === 0) {
        safeHTML(container, '<div class="empty-state">No wallets tracked yet</div>');
        return;
    }
    const tierColors = { LEGENDARY: '#ffd700', ELITE: '#a855f7', PRO: '#4c8dff', RISING: '#9499b3' };
    const tierIcons = { LEGENDARY: '👑', ELITE: '💎', PRO: '⭐', RISING: '🌱' };
    const followColors = { STRONG_FOLLOW: '#00e68a', FOLLOW: '#4c8dff', CAUTION: '#ff9f43', AVOID: '#ff4d6a', NO_DATA: '#5a5f78' };
    const followIcons = { STRONG_FOLLOW: '🚀', FOLLOW: '✅', CAUTION: '⚠️', AVOID: '🚫', NO_DATA: '—' };
    const holdIcons = { SCALPER: '⚡', SWING: '🔄', POSITION: '📊', HODLER: '🏔️', UNKNOWN: '❓' };
    const html = wallets.map((w, i) => {
        const tier = w.tier || 'RISING';
        const tierColor = tierColors[tier] || '#9499b3';
        const tierIcon = tierIcons[tier] || '🌱';
        const pnlStr = (w.total_pnl || 0) >= 1_000_000
            ? `$${((w.total_pnl) / 1_000_000).toFixed(2)}M`
            : `$${((w.total_pnl || 0) / 1_000).toFixed(0)}K`;
        const shortAddr = w.address ? `${w.address.substring(0, 6)}…${w.address.slice(-4)}` : '—';
        const wr = ((w.win_rate || 0) * 100).toFixed(1);
        const scorePct = Math.min(100, w.score || 0);
        const volStr = (w.total_volume || 0) >= 1_000_000
            ? `$${((w.total_volume) / 1_000_000).toFixed(1)}M`
            : (w.total_volume || 0) >= 1_000
            ? `$${((w.total_volume) / 1_000).toFixed(0)}K`
            : `$${(w.total_volume || 0).toFixed(0)}`;

        // Copy-trade follow signal (NEW)
        const followSig = w.follow_signal || 'NO_DATA';
        const followPnl = w.follow_pnl || 0;
        const followIcon = followIcons[followSig] || '—';
        const followColor = followColors[followSig] || '#5a5f78';
        const followPnlStr = followPnl !== 0 ? `${followPnl >= 0 ? '+' : ''}${followPnl.toFixed(1)}%` : '—';
        const followWr = w.follow_trades > 0 ? `${(w.follow_win_rate || 0).toFixed(0)}%` : '';

        // Hold duration (NEW)
        const holdStyle = w.hold_style || 'UNKNOWN';
        const holdIcon = holdIcons[holdStyle] || '❓';
        let holdStr = '—';
        if (w.avg_hold_hours != null) {
            if (w.avg_hold_hours < 1) holdStr = `${Math.round(w.avg_hold_hours * 60)}m`;
            else if (w.avg_hold_hours < 24) holdStr = `${w.avg_hold_hours.toFixed(1)}h`;
            else holdStr = `${(w.avg_hold_hours / 24).toFixed(1)}d`;
        }

        return `<div class="whale-lb-card" data-score="${w.score||0}" data-pnl="${w.total_pnl||0}" data-winrate="${w.win_rate||0}" data-activity="${w.recent_activity||0}" data-positions="${w.active_positions||0}" data-follow="${w.follow_pnl||0}">
            <div class="whale-lb-rank" style="color:${tierColor};">#${i + 1}</div>
            <div class="whale-lb-info">
                <div class="whale-lb-name">
                    <span class="wm-star-inline ${w.is_starred ? 'wm-starred' : ''}" onclick="event.stopPropagation();toggleStarInline(this,'whale','${w.address}','${(w.name||'').replace(/'/g,"\\'")}')" title="Star">★</span>
                    ${tierIcon} <a class="wm-clickable-name" href="#" onclick="event.preventDefault();openWhaleProfile('${w.address}')">${w.name || '—'}</a>
                    <span class="whale-lb-tier" style="border-color:${tierColor};color:${tierColor};">${tier}</span>
                </div>
                <div class="whale-lb-addr"><code>${shortAddr}</code></div>
            </div>
            <div class="whale-lb-stats">
                <div class="whale-lb-stat">
                    <span class="whale-lb-stat-label">P&L</span>
                    <span class="whale-lb-stat-value ${w.total_pnl >= 0 ? 'whale-positive' : 'whale-negative'}">${pnlStr}</span>
                </div>
                <div class="whale-lb-stat">
                    <span class="whale-lb-stat-label">Win Rate</span>
                    <span class="whale-lb-stat-value">${wr}%</span>
                </div>
                <div class="whale-lb-stat">
                    <span class="whale-lb-stat-label">Volume</span>
                    <span class="whale-lb-stat-value">${volStr}</span>
                </div>
                <div class="whale-lb-stat">
                    <span class="whale-lb-stat-label">Positions</span>
                    <span class="whale-lb-stat-value">${w.active_positions || 0}</span>
                </div>
                <div class="whale-lb-stat">
                    <span class="whale-lb-stat-label">Activity</span>
                    <span class="whale-lb-stat-value">${w.recent_activity || 0}</span>
                </div>
                <div class="whale-lb-stat">
                    <span class="whale-lb-stat-label">Follow P&L</span>
                    <span class="whale-lb-stat-value" style="color:${followColor};">${followIcon} ${followPnlStr}</span>
                </div>
                <div class="whale-lb-stat">
                    <span class="whale-lb-stat-label">Follow WR</span>
                    <span class="whale-lb-stat-value" style="color:${followColor};">${followWr || '—'}</span>
                </div>
                <div class="whale-lb-stat">
                    <span class="whale-lb-stat-label">Hold Style</span>
                    <span class="whale-lb-stat-value"><span class="whale-hold-badge whale-hold-${holdStyle.toLowerCase()}">${holdIcon} ${holdStyle}</span></span>
                </div>
            </div>
            <div class="whale-lb-bottom-row">
                <div class="whale-lb-score-wrap">
                    <div class="whale-lb-score-bar"><div class="whale-lb-score-fill" style="width:${scorePct}%;background:${tierColor};"></div></div>
                    <span class="whale-lb-score-val">${Math.round(scorePct)}</span>
                </div>
                <span class="whale-lb-follow-pill whale-follow-${followSig.toLowerCase().replace('_','-')}" title="Copy-Trade Signal: ${followSig}">${followIcon} ${followSig.replace('_',' ')}</span>
            </div>
        </div>`;
    }).join('');
    safeHTML(container, html);
}

function sortWhaleLeaderboard() {
    const sortBy = $('#whale-lb-sort')?.value || 'score';
    const container = $('#whale-leaderboard');
    if (!container) return;
    const cards = Array.from(container.querySelectorAll('.whale-lb-card'));
    cards.sort((a, b) => {
        const av = parseFloat(a.dataset[sortBy] || '0');
        const bv = parseFloat(b.dataset[sortBy] || '0');
        return bv - av;
    });
    cards.forEach((card, i) => {
        const rank = card.querySelector('.whale-lb-rank');
        if (rank) rank.textContent = `#${i + 1}`;
        container.appendChild(card);
    });
}

function exportWhaleCSV() {
    if (!_whaleData) return;
    const signals = _whaleData.conviction_signals || [];
    if (signals.length === 0) { alert('No data to export'); return; }
    const headers = ['Market', 'Category', 'Outcome', 'Direction', 'Whales', 'Capital_USD', 'Avg_Entry', 'Current_Price', 'Edge_Pct', 'Conviction', 'Trend', 'Conviction_Delta', 'Strength', 'Freshness', 'Age_Hours', 'Detected'];
    const rows = signals.map(s => [
        `"${(s.title || '').replace(/"/g, '""')}"`,
        s.category || 'Other',
        s.outcome || '',
        s.direction || '',
        s.whale_count || 0,
        (s.total_whale_usd || 0).toFixed(2),
        (s.avg_whale_price || 0).toFixed(4),
        (s.current_price || 0).toFixed(4),
        s.price_edge_pct !== null ? s.price_edge_pct : '',
        (s.conviction_score || 0).toFixed(1),
        s.trend || '',
        s.conviction_delta != null ? s.conviction_delta : '',
        s.signal_strength || '',
        s.freshness || '',
        s.signal_age_hours != null ? s.signal_age_hours : '',
        s.detected_at || '',
    ]);
    const csv = [headers.join(','), ...rows.map(r => r.join(','))].join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `whale_signals_${new Date().toISOString().slice(0,10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
}

// ── Risk Alert Ticker ────────────────────────────────────────
function _renderWhaleAlertTicker(alerts) {
    const ticker = $('#whale-alert-ticker');
    const scroll = $('#whale-alert-ticker-scroll');
    const badge = $('#whale-alert-ticker-count');
    if (!ticker || !scroll) return;
    if (!alerts.length) {
        ticker.style.display = 'none';
        return;
    }
    ticker.style.display = 'flex';
    if (badge) badge.textContent = String(alerts.length);
    // Set alert level class
    const hasHigh = alerts.some(a => a.level === 'HIGH');
    const hasMed = alerts.some(a => a.level === 'MEDIUM');
    ticker.className = 'whale-alert-ticker' + (hasHigh ? ' whale-alert-high' : hasMed ? ' whale-alert-medium' : ' whale-alert-low');
    const html = alerts.map(a => {
        const lvlCls = a.level === 'HIGH' ? 'whale-alert-item-high' : a.level === 'MEDIUM' ? 'whale-alert-item-medium' : 'whale-alert-item-low';
        return `<span class="whale-alert-item ${lvlCls}">${a.message}</span>`;
    }).join('<span class="whale-alert-sep">•</span>');
    safeHTML(scroll, html);
}

// ── Momentum Bars ────────────────────────────────────────────
function _renderWhaleMomentum(momentum) {
    const container = $('#whale-momentum-bars');
    if (!container || !momentum.length) return;
    const maxAbs = Math.max(...momentum.map(m => Math.abs(m.net_flow)), 1);
    const html = momentum.map(m => {
        const pct = Math.abs(m.net_flow) / maxAbs * 100;
        const isPos = m.net_flow >= 0;
        const color = isPos ? 'var(--accent-green)' : 'var(--accent-red)';
        const dirLabel = m.direction === 'ACCUMULATING' ? '📈 Accumulating' : m.direction === 'DISTRIBUTING' ? '📉 Distributing' : '⚖️ Neutral';
        const flowStr = `${isPos ? '+' : '-'}$${Math.abs(m.net_flow).toLocaleString(undefined, {maximumFractionDigits:0})}`;
        return `<div class="whale-mom-item">
            <div class="whale-mom-label">${m.window}</div>
            <div class="whale-mom-bar-wrap">
                <div class="whale-mom-bar-bg">
                    <div class="whale-mom-bar-center"></div>
                    <div class="whale-mom-bar-fill ${isPos ? 'whale-mom-pos' : 'whale-mom-neg'}" style="width:${pct/2}%;${isPos ? 'left:50%;' : 'right:50%;'}"></div>
                </div>
            </div>
            <div class="whale-mom-meta">
                <span class="whale-mom-flow" style="color:${color};">${flowStr}</span>
                <span class="whale-mom-dir">${dirLabel}</span>
                <span class="whale-mom-counts">${m.count_in}↑ ${m.count_out}↓</span>
            </div>
        </div>`;
    }).join('');
    safeHTML(container, html);
}

// ── Velocity Display ─────────────────────────────────────────
function _renderWhaleVelocity(velocity) {
    const map = { '1h': 'whale-vel-1h', '6h': 'whale-vel-6h', '24h': 'whale-vel-24h' };
    for (const [k, id] of Object.entries(map)) {
        const el = document.getElementById(id);
        if (el) {
            const v = velocity[k] || 0;
            el.textContent = v.toFixed(1);
            el.className = 'whale-velocity-value' + (v >= 5 ? ' whale-vel-spike' : v >= 2 ? ' whale-vel-active' : '');
        }
    }
}

// ── Herd Behavior Index ──────────────────────────────────────
function _renderHerdIndex(herdIndex) {
    const bar = $('#whale-herd-bar');
    const val = $('#whale-herd-value');
    if (bar) {
        bar.style.width = `${Math.min(100, herdIndex)}%`;
        bar.style.background = herdIndex > 70 ? 'var(--accent-red)' : herdIndex > 40 ? 'var(--accent-orange)' : 'var(--accent-green)';
    }
    if (val) {
        val.textContent = `${Math.round(herdIndex)}%`;
        val.style.color = herdIndex > 70 ? 'var(--accent-red)' : herdIndex > 40 ? 'var(--accent-orange)' : 'var(--accent-green)';
    }
}

// ── Accumulation / Distribution ──────────────────────────────
function _renderAccumDist(data) {
    const container = $('#whale-accum-list');
    if (!container) return;
    if (!data.length) {
        safeHTML(container, '<div class="empty-state">No accumulation data yet</div>');
        return;
    }
    const html = data.slice(0, 10).map(ad => {
        const total = ad.buys_usd + ad.sells_usd;
        const buyPct = total > 0 ? (ad.buys_usd / total) * 100 : 50;
        const sigCls = ad.signal === 'STRONG_ACCUM' ? 'whale-ad-strong-buy' :
                       ad.signal === 'ACCUM' ? 'whale-ad-buy' :
                       ad.signal === 'STRONG_DISTRIB' ? 'whale-ad-strong-sell' :
                       ad.signal === 'DISTRIB' ? 'whale-ad-sell' : 'whale-ad-neutral';
        const sigLabel = ad.signal.replace('_', ' ');
        return `<div class="whale-ad-item">
            <div class="whale-ad-title">${(ad.title || '—').substring(0, 50)}</div>
            <div class="whale-ad-bar-wrap">
                <div class="whale-ad-bar-buy" style="width:${buyPct}%;">
                    <span class="whale-ad-bar-label">$${(ad.buys_usd/1000).toFixed(0)}k</span>
                </div>
                <div class="whale-ad-bar-sell" style="width:${100 - buyPct}%;">
                    <span class="whale-ad-bar-label">$${(ad.sells_usd/1000).toFixed(0)}k</span>
                </div>
            </div>
            <div class="whale-ad-meta">
                <span class="whale-ad-signal ${sigCls}">${sigLabel}</span>
                <span class="whale-ad-ratio whale-mono">${ad.ratio.toFixed(1)}x</span>
                <span class="whale-ad-net whale-mono ${ad.net_usd >= 0 ? 'whale-positive' : 'whale-negative'}">Net: ${ad.net_usd >= 0 ? '+' : ''}$${Math.abs(ad.net_usd).toLocaleString(undefined, {maximumFractionDigits:0})}</span>
            </div>
        </div>`;
    }).join('');
    safeHTML(container, html);
}

// ── Whale Overlap Matrix ─────────────────────────────────────
function _renderWhaleOverlap(overlaps) {
    const container = $('#whale-overlap-grid');
    if (!container) return;
    if (!overlaps.length) {
        safeHTML(container, '<div class="empty-state">No whale overlap data yet</div>');
        return;
    }
    const html = overlaps.slice(0, 12).map(o => {
        const intensity = Math.min(4, o.shared_markets);
        const marketsList = (o.market_names || []).slice(0, 3).join(', ');
        const corrPct = o.correlation_pct || 0;
        const corrLabel = o.correlation_label || 'LOW';
        const corrCls = { HIGH: 'whale-corr-high', MODERATE: 'whale-corr-mod', LOW: 'whale-corr-low' }[corrLabel] || 'whale-corr-low';
        const agreeRate = o.agreement_rate || 0;
        const agreeCount = o.agree_count || 0;
        const disagreeCount = o.disagree_count || 0;
        return `<div class="whale-overlap-card whale-overlap-${intensity}">
            <div class="whale-overlap-pair">
                <span class="whale-overlap-name">${o.whale_a}</span>
                <span class="whale-overlap-connector">⟷</span>
                <span class="whale-overlap-name">${o.whale_b}</span>
            </div>
            <div class="whale-overlap-count">${o.shared_markets} shared market${o.shared_markets > 1 ? 's' : ''}</div>
            <div class="whale-overlap-corr">
                <span class="whale-corr-badge ${corrCls}" title="Jaccard similarity: ${corrPct}%">🔗 ${corrPct}%</span>
                <span class="whale-agree-badge" title="Direction agreement: ${agreeCount} agree, ${disagreeCount} disagree">
                    ${agreeRate >= 80 ? '🤝' : agreeRate >= 50 ? '🔀' : '⚔️'} ${agreeRate.toFixed(0)}% agree
                </span>
            </div>
            <div class="whale-overlap-markets" title="${marketsList}">${marketsList || '—'}</div>
        </div>`;
    }).join('');
    safeHTML(container, html);
}

// ── Tier Summary Cards ───────────────────────────────────────
function _renderTierSummary(tierData) {
    const container = $('#whale-tier-summary');
    if (!container) return;
    const tierOrder = ['LEGENDARY', 'ELITE', 'PRO', 'RISING'];
    const tierColors = { LEGENDARY: '#ffd700', ELITE: '#a855f7', PRO: '#4c8dff', RISING: '#9499b3' };
    const tierIcons = { LEGENDARY: '👑', ELITE: '💎', PRO: '⭐', RISING: '🌱' };
    const tiers = tierOrder.filter(t => tierData[t]);
    if (!tiers.length) {
        container.innerHTML = '';
        return;
    }
    const html = tiers.map(t => {
        const d = tierData[t];
        const color = tierColors[t];
        const pnlStr = d.total_pnl >= 1_000_000 ? `$${(d.total_pnl/1_000_000).toFixed(1)}M` : `$${(d.total_pnl/1_000).toFixed(0)}K`;
        const wrStr = ((d.avg_winrate || 0) * 100).toFixed(1);
        return `<div class="whale-tier-card" style="border-top:3px solid ${color};">
            <div class="whale-tier-icon">${tierIcons[t]}</div>
            <div class="whale-tier-name" style="color:${color};">${t}</div>
            <div class="whale-tier-count">${d.count} whale${d.count > 1 ? 's' : ''}</div>
            <div class="whale-tier-stats">
                <span>P&L: <strong class="${d.total_pnl >= 0 ? 'whale-positive' : 'whale-negative'}">${pnlStr}</strong></span>
                <span>WR: <strong>${wrStr}%</strong></span>
            </div>
        </div>`;
    }).join('');
    safeHTML(container, html);
}

// ── Alert History Panel ──────────────────────────────────────
function _renderAlertHistory(alerts) {
    const container = $('#whale-alert-history-list');
    const countEl = $('#whale-alert-history-count');
    if (!container) return;
    if (countEl) countEl.textContent = String(alerts.length);
    if (!alerts.length) {
        safeHTML(container, '<div class="empty-state">No alerts recorded yet — alerts will appear as risk conditions are detected</div>');
        return;
    }
    const levelIcons = { HIGH: '🔴', MEDIUM: '🟡', LOW: 'ℹ️' };
    const levelCls = { HIGH: 'whale-alert-high', MEDIUM: 'whale-alert-medium', LOW: 'whale-alert-low' };
    const html = alerts.slice(0, 30).map(a => {
        const icon = levelIcons[a.level] || 'ℹ️';
        const cls = levelCls[a.level] || 'whale-alert-low';
        const timeStr = a.created_at ? shortDate(a.created_at) : '—';
        const typeLabel = (a.alert_type || '').replace(/_/g, ' ');
        return `<div class="whale-ah-item ${cls}">
            <div class="whale-ah-icon">${icon}</div>
            <div class="whale-ah-body">
                <div class="whale-ah-message">${a.message || '—'}</div>
                <div class="whale-ah-meta">
                    <span class="whale-ah-type">${typeLabel}</span>
                    <span class="whale-ah-level-pill whale-ah-${(a.level||'low').toLowerCase()}">${a.level || 'LOW'}</span>
                    <span class="whale-ah-time">${timeStr}</span>
                </div>
            </div>
        </div>`;
    }).join('');
    safeHTML(container, html);
}

// ═══════════════════════════════════════════════════════════════
//  WHALE MODALS & INTERACTIVE FEATURES
// ═══════════════════════════════════════════════════════════════

let _currentWhaleAddress = '';
let _currentMarketSlug = '';
let _mentorLoading = false;

// ── Modal Open/Close ─────────────────────────────────────────
function closeWhaleModal(e) {
    if (e && e.target && e.target.id !== 'wm-overlay') return;
    const overlay = $('#wm-overlay');
    if (overlay) {
        overlay.classList.remove('wm-visible');
        // Hide all modals
        document.querySelectorAll('.wm-modal').forEach(m => m.classList.remove('wm-active'));
    }
}

function _showModal(modalId) {
    const overlay = $('#wm-overlay');
    if (overlay) overlay.classList.add('wm-visible');
    document.querySelectorAll('.wm-modal').forEach(m => m.classList.remove('wm-active'));
    const modal = document.getElementById(modalId);
    if (modal) modal.classList.add('wm-active');
}

// ── Open Whale Profile ───────────────────────────────────────
async function openWhaleProfile(address) {
    if (!address) return;
    _currentWhaleAddress = address;
    _showModal('wm-whale-profile');
    // Show loading state
    const kpis = $('#wm-whale-kpis');
    if (kpis) safeHTML(kpis, '<div class="wm-loading">Loading whale profile…</div>');

    const data = await apiFetch(`/api/whale-profile/${encodeURIComponent(address)}`);
    if (!data || data.error) {
        if (kpis) safeHTML(kpis, `<div class="wm-error">Failed to load profile: ${data?.error || 'Unknown error'}</div>`);
        return;
    }

    const w = data.wallet || {};
    const isScanner = data.is_scanner_profile || false;
    const liveStats = data.live_stats || {};

    // Header
    const nameLabel = w.name || 'Unknown Whale';
    const sourceTag = isScanner ? (w.source === 'live_lookup' ? ' 🔍' : ' 📡') : '';
    safeText($('#wm-whale-name'), `${nameLabel}${sourceTag}`);
    const tierBadge = $('#wm-whale-tier');
    if (tierBadge) {
        const tier = w.tier || 'RISING';
        const tierColors = { LEGENDARY: '#ffd700', ELITE: '#a855f7', PRO: '#4c8dff', RISING: '#9499b3' };
        tierBadge.textContent = tier;
        tierBadge.style.color = tierColors[tier] || '#9499b3';
        tierBadge.style.borderColor = tierColors[tier] || '#9499b3';
    }

    // Star state
    const starBtn = $('#wm-whale-star');
    if (starBtn) {
        starBtn.textContent = data.is_starred ? '★' : '☆';
        starBtn.classList.toggle('wm-starred', data.is_starred);
    }

    // KPIs — enhanced for scanner profiles
    const pnlVal = w.total_pnl || 0;
    const pnlStr = Math.abs(pnlVal) >= 1e6 ? `$${(pnlVal/1e6).toFixed(2)}M` : Math.abs(pnlVal) >= 1e3 ? `$${(pnlVal/1e3).toFixed(1)}K` : `$${pnlVal.toFixed(0)}`;
    const wrStr = ((w.win_rate || 0) * 100).toFixed(1);
    const volVal = w.total_volume || 0;
    const volStr = volVal >= 1e6 ? `$${(volVal/1e6).toFixed(1)}M` : `$${(volVal/1e3).toFixed(0)}K`;

    let kpiHtml = `
        <div class="wm-kpi"><div class="wm-kpi-val ${pnlVal>=0?'whale-positive':'whale-negative'}">${pnlStr}</div><div class="wm-kpi-label">Total P&L</div></div>
        <div class="wm-kpi"><div class="wm-kpi-val">${wrStr}%</div><div class="wm-kpi-label">Win Rate</div></div>
        <div class="wm-kpi"><div class="wm-kpi-val">${volStr}</div><div class="wm-kpi-label">Volume</div></div>
        <div class="wm-kpi"><div class="wm-kpi-val">${w.active_positions || 0}</div><div class="wm-kpi-label">Positions</div></div>
    `;

    if (isScanner) {
        // Extra KPIs for scanner profiles
        const avgHold = liveStats.avg_holding_days || 0;
        const holdStr = avgHold > 0 ? `${avgHold.toFixed(0)}d` : '—';
        const avgSize = liveStats.avg_position_size || 0;
        const avgSizeStr = avgSize >= 1e3 ? `$${(avgSize/1e3).toFixed(1)}K` : `$${avgSize.toFixed(0)}`;
        kpiHtml += `
            <div class="wm-kpi"><div class="wm-kpi-val">${holdStr}</div><div class="wm-kpi-label">Avg Hold</div></div>
            <div class="wm-kpi"><div class="wm-kpi-val">${avgSizeStr}</div><div class="wm-kpi-label">Avg Size</div></div>
        `;
    } else {
        kpiHtml += `
            <div class="wm-kpi"><div class="wm-kpi-val">${data.total_signals || 0}</div><div class="wm-kpi-label">Signals</div></div>
            <div class="wm-kpi"><div class="wm-kpi-val">${w.score || 0}</div><div class="wm-kpi-label">Score</div></div>
        `;
    }
    safeHTML(kpis, kpiHtml);

    // ── Scanner-only: Live Stats Detail Card ──
    const liveStatsEl = $('#wm-whale-live-stats');
    if (liveStatsEl) {
        if (isScanner && liveStats.total_positions > 0) {
            const rpnl = liveStats.realized_pnl || 0;
            const upnl = liveStats.unrealized_pnl || 0;
            const bestT = liveStats.best_trade_pnl || 0;
            const worstT = liveStats.worst_trade_pnl || 0;
            const span = liveStats.trading_span_days || 0;
            liveStatsEl.style.display = 'block';
            safeHTML(liveStatsEl, `
                <h3>📊 Live Trading Analytics</h3>
                <div class="wm-live-stats-grid">
                    <div class="wm-ls-item"><span class="wm-ls-label">Win / Loss</span><span class="wm-ls-val"><span class="whale-positive">${liveStats.winners || 0}W</span> / <span class="whale-negative">${liveStats.losers || 0}L</span></span></div>
                    <div class="wm-ls-item"><span class="wm-ls-label">Realized PnL</span><span class="wm-ls-val ${rpnl>=0?'whale-positive':'whale-negative'}">${rpnl>=0?'+':''}$${Math.abs(rpnl).toLocaleString(undefined,{maximumFractionDigits:0})}</span></div>
                    <div class="wm-ls-item"><span class="wm-ls-label">Unrealized PnL</span><span class="wm-ls-val ${upnl>=0?'whale-positive':'whale-negative'}">${upnl>=0?'+':''}$${Math.abs(upnl).toLocaleString(undefined,{maximumFractionDigits:0})}</span></div>
                    <div class="wm-ls-item"><span class="wm-ls-label">Portfolio Value</span><span class="wm-ls-val">$${(liveStats.total_current_value||0).toLocaleString(undefined,{maximumFractionDigits:0})}</span></div>
                    <div class="wm-ls-item"><span class="wm-ls-label">Best Trade</span><span class="wm-ls-val whale-positive">+$${Math.abs(bestT).toLocaleString(undefined,{maximumFractionDigits:0})}</span></div>
                    <div class="wm-ls-item"><span class="wm-ls-label">Worst Trade</span><span class="wm-ls-val whale-negative">-$${Math.abs(worstT).toLocaleString(undefined,{maximumFractionDigits:0})}</span></div>
                    <div class="wm-ls-item"><span class="wm-ls-label">Avg Hold Time</span><span class="wm-ls-val">${(liveStats.avg_holding_days||0).toFixed(0)} days</span></div>
                    <div class="wm-ls-item"><span class="wm-ls-label">Trading Span</span><span class="wm-ls-val">${span} days</span></div>
                    <div class="wm-ls-item"><span class="wm-ls-label">Biggest Position</span><span class="wm-ls-val">$${(liveStats.biggest_position||0).toLocaleString(undefined,{maximumFractionDigits:0})}</span></div>
                    <div class="wm-ls-item"><span class="wm-ls-label">Yes / No Split</span><span class="wm-ls-val">${liveStats.yes_positions||0}Y / ${liveStats.no_positions||0}N</span></div>
                    <div class="wm-ls-item"><span class="wm-ls-label">Realized / Open</span><span class="wm-ls-val">${liveStats.realized_count||0} / ${liveStats.unrealized_count||0}</span></div>
                    <div class="wm-ls-item"><span class="wm-ls-label">Avg Position</span><span class="wm-ls-val">$${(liveStats.avg_position_size||0).toLocaleString(undefined,{maximumFractionDigits:0})}</span></div>
                </div>
            `);
        } else {
            liveStatsEl.style.display = 'none';
        }
    }

    // ── Address display ──
    const addrEl = $('#wm-whale-address');
    if (addrEl) {
        safeHTML(addrEl, `<span class="wm-addr-text" title="${address}">${address}</span>`);
    }

    // Direction split
    const dirBar = $('#wm-whale-direction');
    if (dirBar) {
        const bull = data.bullish_signals || 0;
        const bear = data.bearish_signals || 0;
        const total = bull + bear || 1;
        const bullPct = (bull / total) * 100;
        safeHTML(dirBar, `
            <div class="wm-dir-bar-wrap">
                <div class="wm-dir-bull" style="width:${bullPct}%"><span>🟢 ${bull}</span></div>
                <div class="wm-dir-bear" style="width:${100-bullPct}%"><span>🔴 ${bear}</span></div>
            </div>
        `);
    }

    // Categories
    const catContainer = $('#wm-whale-categories');
    if (catContainer) {
        const cats = data.category_distribution || {};
        const catHtml = Object.entries(cats).sort((a,b) => b[1]-a[1]).map(([cat, count]) => {
            const meta = CATEGORY_META[cat] || { icon: '📦', color: '#9499b3' };
            return `<div class="wm-cat-item"><span style="color:${meta.color}">${meta.icon} ${cat}</span><strong>${count}</strong></div>`;
        }).join('');
        safeHTML(catContainer, catHtml || '<div class="empty-state">No data</div>');
    }

    // Top Markets
    const mktsContainer = $('#wm-whale-top-markets');
    if (mktsContainer) {
        const mkts = data.top_markets || [];
        const maxUsd = Math.max(...mkts.map(m => m.total_usd || m.current_value || 0), 1);
        const mktsHtml = mkts.map(m => {
            const val = m.total_usd || m.current_value || 0;
            const pct = (val / maxUsd) * 100;
            const dirIcon = m.direction === 'BULLISH' ? '🟢' : '🔴';
            const pnlVal = m.cash_pnl || 0;
            const pnlStr = pnlVal !== 0 ? ` · PnL ${pnlVal>=0?'+':''}$${Math.abs(pnlVal).toLocaleString(undefined,{maximumFractionDigits:0})}` : '';
            return `<div class="wm-mkt-item">
                <div class="wm-mkt-title"><a class="wm-clickable-market" href="#" onclick="event.preventDefault();openMarketDetail('${(m.market_slug||m.title||'').replace(/'/g,"\\'")}')">${(m.title||'—').substring(0,50)}</a></div>
                <div class="wm-mkt-bar-wrap"><div class="wm-mkt-bar" style="width:${pct}%;background:${m.direction==='BULLISH'?'var(--accent-green)':'var(--accent-red)'}"></div></div>
                <div class="wm-mkt-meta">${dirIcon} $${val.toLocaleString(undefined,{maximumFractionDigits:0})}${pnlStr} · Conv ${Math.round(m.conviction||0)}</div>
            </div>`;
        }).join('');
        safeHTML(mktsContainer, mktsHtml || '<div class="empty-state">No market positions</div>');
    }

    // Positions tab — use live positions for scanner profiles, signals for tracked
    const sigBody = $('#wm-whale-signals-body');
    if (sigBody) {
        if (isScanner && data.live_positions && data.live_positions.length) {
            const posHtml = data.live_positions.map(p => {
                const pnlClass = p.cash_pnl >= 0 ? 'whale-positive' : 'whale-negative';
                const pnlStr = `${p.cash_pnl>=0?'+':''}$${Math.abs(p.cash_pnl).toLocaleString(undefined,{maximumFractionDigits:0})}`;
                const retPct = p.initial_value > 0 ? ((p.current_value - p.initial_value) / p.initial_value * 100).toFixed(1) : '0.0';
                return `<tr>
                    <td><a class="wm-clickable-market" href="#" onclick="event.preventDefault();openMarketDetail('${(p.market_slug||'').replace(/'/g,"\\'")}')">${(p.title||p.market_slug||'—').substring(0,40)}</a></td>
                    <td>${p.outcome||'—'}</td>
                    <td>${p.outcome?.toLowerCase()==='yes'?'🟢':'🔴'} ${p.outcome?.toLowerCase()==='yes'?'BULL':'BEAR'}</td>
                    <td class="whale-mono">$${(p.current_value||0).toLocaleString(undefined,{maximumFractionDigits:0})}</td>
                    <td class="whale-mono ${pnlClass}">${pnlStr} (${retPct}%)</td>
                    <td class="whale-mono">${(p.cur_price||0).toFixed(3)}</td>
                </tr>`;
            }).join('');
            safeHTML(sigBody, posHtml);
        } else {
            const sigs = data.signals || [];
            const sigHtml = sigs.map(s => `<tr>
                <td><a class="wm-clickable-market" href="#" onclick="event.preventDefault();openMarketDetail('${(s.market_slug||'').replace(/'/g,"\\'")}')">${(s.title||s.market_slug||'—').substring(0,45)}</a></td>
                <td>${s.outcome||'—'}</td>
                <td>${s.direction==='BULLISH'?'🟢':'🔴'} ${s.direction||'—'}</td>
                <td class="whale-mono">$${(s.total_whale_usd||0).toLocaleString(undefined,{maximumFractionDigits:0})}</td>
                <td>${Math.round(s.conviction_score||0)}</td>
                <td class="whale-mono">${(s.current_price||0).toFixed(3)}</td>
            </tr>`).join('');
            safeHTML(sigBody, sigHtml || '<tr><td colspan="6" class="empty-state">No positions</td></tr>');
        }
    }

    // Activity tab — use live_activity for scanner profiles
    const actFeed = $('#wm-whale-activity-feed');
    if (actFeed) {
        if (isScanner && data.live_activity && data.live_activity.length) {
            const actHtml = data.live_activity.slice(0, 80).map(a => {
                const actionIcons = {Buy:'🟢',Sell:'🔴',Redeem:'💰',Mint:'🟢'};
                const icon = actionIcons[a.action] || '⚪';
                const val = Math.abs(a.value_usd || 0);
                const ts = a.timestamp ? new Date(a.timestamp * 1000 || a.timestamp).toLocaleDateString() : '—';
                return `<div class="wm-feed-item">
                    <span class="wm-feed-icon">${icon}</span>
                    <span class="wm-feed-text"><strong>${a.action}</strong> — <a class="wm-clickable-market" href="#" onclick="event.preventDefault();openMarketDetail('${(a.market_slug||'').replace(/'/g,"\\'")}')">${(a.title||a.market_slug||'—').substring(0,40)}</a> (${a.outcome||'—'})${val>=1?` — $${val.toLocaleString(undefined,{maximumFractionDigits:0})}`:''}</span>
                    <span class="wm-feed-time">${ts}</span>
                </div>`;
            }).join('');
            safeHTML(actFeed, actHtml);
        } else {
            const deltas = data.deltas || [];
            const actHtml = deltas.slice(0, 50).map(d => {
                const icons = {NEW_ENTRY:'🟢',EXIT:'🔴',SIZE_INCREASE:'📈',SIZE_DECREASE:'📉'};
                const icon = icons[d.action] || '⚪';
                const val = Math.abs(d.value_change_usd || 0);
                return `<div class="wm-feed-item">
                    <span class="wm-feed-icon">${icon}</span>
                    <span class="wm-feed-text">${d.action} — <a class="wm-clickable-market" href="#" onclick="event.preventDefault();openMarketDetail('${(d.market_slug||'').replace(/'/g,"\\'")}')">${(d.title||d.market_slug||'—').substring(0,40)}</a> (${d.outcome||'—'})${val>=1?` — $${val.toLocaleString(undefined,{maximumFractionDigits:0})}`:''}</span>
                    <span class="wm-feed-time">${shortDate(d.detected_at)}</span>
                </div>`;
            }).join('');
            safeHTML(actFeed, actHtml || '<div class="empty-state">No activity</div>');
        }
    }

    // Back-Analysis tab
    const btHeader = $('#wm-backtest-header');
    const btChart = $('#wm-backtest-chart');
    const btTable = $('#wm-backtest-table');
    if (btHeader) {
        const months = data.months_available || 0;
        safeHTML(btHeader, `
            <div class="wm-bt-info">
                <span class="wm-bt-months">${months} month${months!==1?'s':''}</span> of trading data available for back-analysis
            </div>
            <div class="wm-bt-kpis">
                <div class="wm-bt-kpi"><strong class="${pnlVal>=0?'whale-positive':'whale-negative'}">${pnlStr}</strong><span>Cumulative P&L</span></div>
                <div class="wm-bt-kpi"><strong>${wrStr}%</strong><span>Win Rate</span></div>
                <div class="wm-bt-kpi"><strong>${data.total_signals || w.active_positions || 0}</strong><span>Total Positions</span></div>
            </div>
        `);
    }
    if (btChart) {
        const timeline = data.monthly_timeline || [];
        if (timeline.length > 0) {
            let cumFlow = 0;
            const chartHtml = timeline.map(m => {
                cumFlow += m.net_flow;
                const barH = Math.min(100, Math.abs(m.net_flow) / Math.max(...timeline.map(t=>Math.abs(t.net_flow)),1) * 80);
                const color = m.net_flow >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
                return `<div class="wm-bt-bar-col">
                    <div class="wm-bt-bar" style="height:${barH}%;background:${color};" title="Net: $${m.net_flow.toLocaleString(undefined,{maximumFractionDigits:0})}"></div>
                    <span class="wm-bt-bar-label">${m.month.substring(5)}</span>
                </div>`;
            }).join('');
            safeHTML(btChart, `<div class="wm-bt-chart-bars">${chartHtml}</div>`);
        } else {
            safeHTML(btChart, '<div class="empty-state">No monthly data available</div>');
        }
    }
    if (btTable) {
        const timeline = data.monthly_timeline || [];
        const tableHtml = timeline.length ? `<table class="wm-table"><thead><tr><th>Month</th><th>Entries</th><th>Exits</th><th>Volume</th><th>Net Flow</th></tr></thead><tbody>` +
            timeline.map(m => `<tr>
                <td>${m.month}</td>
                <td>${m.entries}</td>
                <td>${m.exits}</td>
                <td class="whale-mono">$${m.volume.toLocaleString(undefined,{maximumFractionDigits:0})}</td>
                <td class="whale-mono ${m.net_flow>=0?'whale-positive':'whale-negative'}">${m.net_flow>=0?'+':''}$${Math.abs(m.net_flow).toLocaleString(undefined,{maximumFractionDigits:0})}</td>
            </tr>`).join('') + '</tbody></table>' : '<div class="empty-state">No data</div>';
        safeHTML(btTable, tableHtml);
    }

    // Reset to overview tab
    switchWhaleProfileTab('overview');

    // Load mentor history
    _loadMentorHistory(address);
}

function switchWhaleProfileTab(tabName) {
    document.querySelectorAll('#wm-whale-profile .wm-tab').forEach((btn, i) => {
        const tabs = ['overview','positions','activity','backtest','mentor'];
        btn.classList.toggle('active', tabs[i] === tabName);
    });
    document.querySelectorAll('#wm-whale-profile .wm-tab-content').forEach(tc => {
        tc.classList.toggle('active', tc.id === `wm-wp-${tabName}`);
    });
}

// ── Open Market Detail ───────────────────────────────────────
async function openMarketDetail(slug) {
    if (!slug) return;
    _currentMarketSlug = slug;
    _showModal('wm-market-detail');
    const kpis = $('#wm-market-kpis');
    if (kpis) safeHTML(kpis, '<div class="wm-loading">Loading market…</div>');

    const data = await apiFetch(`/api/market-detail/${encodeURIComponent(slug)}`);
    if (!data || data.error) {
        if (kpis) safeHTML(kpis, '<div class="wm-error">Market not found</div>');
        return;
    }

    safeText($('#wm-market-title'), (data.title || slug).substring(0, 60));

    // Star state
    const starBtn = $('#wm-market-star');
    if (starBtn) {
        starBtn.textContent = data.is_starred ? '★' : '☆';
        starBtn.classList.toggle('wm-starred', data.is_starred);
    }

    // Links
    const links = $('#wm-market-links');
    if (links) {
        safeHTML(links, `
            <a href="${data.polymarket_url || '#'}" target="_blank" rel="noopener" class="wm-ext-link">🔗 View on Polymarket</a>
            ${data.condition_id ? `<span class="wm-condition-id" title="${data.condition_id}">ID: ${data.condition_id.substring(0,12)}…</span>` : ''}
        `);
    }

    // KPIs
    safeHTML(kpis, `
        <div class="wm-kpi"><div class="wm-kpi-val">${data.whale_count || 0}</div><div class="wm-kpi-label">Whales</div></div>
        <div class="wm-kpi"><div class="wm-kpi-val">$${(data.total_whale_usd||0).toLocaleString(undefined,{maximumFractionDigits:0})}</div><div class="wm-kpi-label">Whale Capital</div></div>
        <div class="wm-kpi"><div class="wm-kpi-val">${Math.round(data.avg_conviction||0)}</div><div class="wm-kpi-label">Avg Conviction</div></div>
        <div class="wm-kpi"><div class="wm-kpi-val whale-positive">${data.entries||0}</div><div class="wm-kpi-label">Entries</div></div>
        <div class="wm-kpi"><div class="wm-kpi-val whale-negative">${data.exits||0}</div><div class="wm-kpi-label">Exits</div></div>
    `);

    // Signals table
    const sigBody = $('#wm-market-signals-body');
    if (sigBody) {
        const sigs = data.signals || [];
        const html = sigs.map(s => {
            const names = (s.whale_names||[]).slice(0,4).join(', ');
            return `<tr>
                <td>${s.outcome||'—'}</td>
                <td title="${names}">${s.whale_count||0} 🐋</td>
                <td class="whale-mono">$${(s.total_whale_usd||0).toLocaleString(undefined,{maximumFractionDigits:0})}</td>
                <td>${Math.round(s.conviction_score||0)}</td>
                <td>${s.direction==='BULLISH'?'🟢':'🔴'} ${s.direction||'—'}</td>
                <td class="whale-mono">${(s.current_price||0).toFixed(3)}</td>
            </tr>`;
        }).join('');
        safeHTML(sigBody, html || '<tr><td colspan="6" class="empty-state">No signals</td></tr>');
    }

    // Activity feed
    const actFeed = $('#wm-market-activity-feed');
    if (actFeed) {
        const deltas = data.deltas || [];
        const html = deltas.slice(0, 30).map(d => {
            const icons = {NEW_ENTRY:'🟢',EXIT:'🔴',SIZE_INCREASE:'📈',SIZE_DECREASE:'📉'};
            const val = Math.abs(d.value_change_usd || 0);
            return `<div class="wm-feed-item">
                <span class="wm-feed-icon">${icons[d.action]||'⚪'}</span>
                <span class="wm-feed-text"><a class="wm-clickable-name" href="#" onclick="event.preventDefault();openWhaleProfile('${d.wallet_address||''}')">${d.wallet_name||'—'}</a> — ${d.action} (${d.outcome||'—'})${val>=1?` $${val.toLocaleString(undefined,{maximumFractionDigits:0})}`:''}</span>
                <span class="wm-feed-time">${shortDate(d.detected_at)}</span>
            </div>`;
        }).join('');
        safeHTML(actFeed, html || '<div class="empty-state">No activity</div>');
    }
}

// ── Open Activity Detail ─────────────────────────────────────
function openActivityDetail(delta) {
    _showModal('wm-activity-detail');
    const content = $('#wm-activity-content');
    if (!content) return;
    const icons = {NEW_ENTRY:'🟢 New Entry',EXIT:'🔴 Exit',SIZE_INCREASE:'📈 Size Increase',SIZE_DECREASE:'📉 Size Decrease'};
    const val = Math.abs(delta.value_change_usd || 0);
    const size = Math.abs(delta.size_change || 0);
    safeHTML(content, `
        <div class="wm-act-detail">
            <div class="wm-act-action">${icons[delta.action] || delta.action}</div>
            <div class="wm-act-grid">
                <div class="wm-act-row"><span class="wm-act-label">Whale</span><a class="wm-clickable-name" href="#" onclick="event.preventDefault();openWhaleProfile('${delta.wallet_address||''}')">${delta.wallet_name || '—'}</a></div>
                <div class="wm-act-row"><span class="wm-act-label">Market</span><a class="wm-clickable-market" href="#" onclick="event.preventDefault();openMarketDetail('${(delta.market_slug||'').replace(/'/g,"\\'")}')">${(delta.title || delta.market_slug || '—').substring(0,60)}</a></div>
                <div class="wm-act-row"><span class="wm-act-label">Outcome</span><span>${delta.outcome || '—'}</span></div>
                <div class="wm-act-row"><span class="wm-act-label">Value</span><span class="whale-mono">${val >= 1 ? '$'+val.toLocaleString(undefined,{maximumFractionDigits:0}) : '—'}</span></div>
                <div class="wm-act-row"><span class="wm-act-label">Size Change</span><span class="whale-mono">${size >= 1 ? (delta.size_change>0?'+':'-')+size.toLocaleString(undefined,{maximumFractionDigits:0})+' shares' : '—'}</span></div>
                <div class="wm-act-row"><span class="wm-act-label">Price</span><span class="whale-mono">${(delta.current_price||0).toFixed(4)}</span></div>
                <div class="wm-act-row"><span class="wm-act-label">Detected</span><span>${delta.detected_at || '—'}</span></div>
                <div class="wm-act-row"><span class="wm-act-label">Address</span><span class="whale-mono" style="font-size:0.7rem;">${delta.wallet_address || '—'}</span></div>
            </div>
        </div>
    `);
}

// ── Star/Watchlist Toggling ──────────────────────────────────
async function toggleStarInline(el, type, identifier, label) {
    const res = await apiFetch('/api/whale-stars', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({star_type: type, identifier, label}),
    });
    if (res) {
        el.classList.toggle('wm-starred', res.action === 'starred');
    }
}

async function toggleWhaleStar() {
    const btn = $('#wm-whale-star');
    const name = $('#wm-whale-name')?.textContent || '';
    const res = await apiFetch('/api/whale-stars', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({star_type: 'whale', identifier: _currentWhaleAddress, label: name}),
    });
    if (res && btn) {
        const starred = res.action === 'starred';
        btn.textContent = starred ? '★' : '☆';
        btn.classList.toggle('wm-starred', starred);
    }
}

async function toggleMarketStar() {
    const btn = $('#wm-market-star');
    const title = $('#wm-market-title')?.textContent || '';
    const res = await apiFetch('/api/whale-stars', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({star_type: 'market', identifier: _currentMarketSlug, label: title}),
    });
    if (res && btn) {
        const starred = res.action === 'starred';
        btn.textContent = starred ? '★' : '☆';
        btn.classList.toggle('wm-starred', starred);
    }
}

// ── Strategy Mentor Chat ─────────────────────────────────────
async function _loadMentorHistory(address) {
    const messages = $('#wm-mentor-messages');
    if (!messages) return;
    const data = await apiFetch(`/api/whale-mentor/history?whale_address=${encodeURIComponent(address||'')}`);
    if (!data || !data.history || data.history.length === 0) {
        // Show welcome screen — keep existing HTML
        return;
    }
    // Render history
    let html = '';
    for (const msg of data.history) {
        if (msg.role === 'user') {
            html += `<div class="wm-msg wm-msg-user"><div class="wm-msg-bubble wm-msg-user-bubble">${escapeHTML(msg.content)}</div></div>`;
        } else {
            html += `<div class="wm-msg wm-msg-assistant"><div class="wm-msg-avatar">🤖</div><div class="wm-msg-bubble wm-msg-asst-bubble">${_renderMarkdown(msg.content)}</div></div>`;
        }
    }
    safeHTML(messages, html);
    messages.scrollTop = messages.scrollHeight;
}

function sendMentorChip(btn) {
    const input = $('#wm-mentor-input');
    if (input) {
        input.value = btn.textContent;
        sendMentorMessage();
    }
}

async function sendMentorMessage() {
    const input = $('#wm-mentor-input');
    const messages = $('#wm-mentor-messages');
    if (!input || !messages || _mentorLoading) return;
    const text = input.value.trim();
    if (!text) return;
    input.value = '';

    // Remove welcome screen if present
    const welcome = messages.querySelector('.wm-mentor-welcome');
    if (welcome) welcome.remove();

    // Add user message
    const userDiv = document.createElement('div');
    userDiv.className = 'wm-msg wm-msg-user';
    userDiv.innerHTML = `<div class="wm-msg-bubble wm-msg-user-bubble">${escapeHTML(text)}</div>`;
    messages.appendChild(userDiv);

    // Add loading indicator
    const loadDiv = document.createElement('div');
    loadDiv.className = 'wm-msg wm-msg-assistant wm-msg-loading';
    loadDiv.innerHTML = '<div class="wm-msg-avatar">🤖</div><div class="wm-msg-bubble wm-msg-asst-bubble"><span class="wm-typing">Analyzing<span>.</span><span>.</span><span>.</span></span></div>';
    messages.appendChild(loadDiv);
    messages.scrollTop = messages.scrollHeight;

    _mentorLoading = true;
    try {
        const res = await apiFetch('/api/whale-mentor', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({message: text, whale_address: _currentWhaleAddress}),
        });
        loadDiv.remove();
        const reply = res?.reply || 'Failed to get response.';
        const asstDiv = document.createElement('div');
        asstDiv.className = 'wm-msg wm-msg-assistant';
        asstDiv.innerHTML = `<div class="wm-msg-avatar">🤖</div><div class="wm-msg-bubble wm-msg-asst-bubble">${_renderMarkdown(reply)}</div>`;
        messages.appendChild(asstDiv);
        messages.scrollTop = messages.scrollHeight;
    } catch (e) {
        loadDiv.remove();
        const errDiv = document.createElement('div');
        errDiv.className = 'wm-msg wm-msg-assistant';
        errDiv.innerHTML = '<div class="wm-msg-avatar">🤖</div><div class="wm-msg-bubble wm-msg-asst-bubble wm-msg-error">⚠️ Failed to reach mentor. Check your API key.</div>';
        messages.appendChild(errDiv);
    }
    _mentorLoading = false;
}

async function clearMentorChat() {
    await apiFetch('/api/whale-mentor/clear', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({whale_address: _currentWhaleAddress}),
    });
    const messages = $('#wm-mentor-messages');
    if (messages) {
        safeHTML(messages, `
            <div class="wm-mentor-welcome">
                <div class="wm-mentor-avatar">🤖</div>
                <p>I'm your <strong>Strategy Mentor</strong>. I can analyze this whale's trading patterns, identify strategies, and provide actionable insights.</p>
                <p class="wm-mentor-suggestions">Try asking:</p>
                <div class="wm-mentor-chips">
                    <button class="wm-chip" onclick="sendMentorChip(this)">What's their trading strategy?</button>
                    <button class="wm-chip" onclick="sendMentorChip(this)">Are they accumulating or distributing?</button>
                    <button class="wm-chip" onclick="sendMentorChip(this)">What are their best and worst positions?</button>
                    <button class="wm-chip" onclick="sendMentorChip(this)">Should I follow their trades?</button>
                </div>
            </div>
        `);
    }
}

function _renderMarkdown(text) {
    // Simple markdown rendering for LLM responses
    return (text || '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\*(.+?)\*/g, '<em>$1</em>')
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/^### (.+)$/gm, '<h4>$1</h4>')
        .replace(/^## (.+)$/gm, '<h3>$1</h3>')
        .replace(/^# (.+)$/gm, '<h2>$1</h2>')
        .replace(/^- (.+)$/gm, '<li>$1</li>')
        .replace(/(<li>.*<\/li>)/gs, '<ul>$1</ul>')
        .replace(/<\/ul>\s*<ul>/g, '')
        .replace(/\n{2,}/g, '<br><br>')
        .replace(/\n/g, '<br>');
}

// Close modal on Escape key
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeWhaleModal(); });


// ═══════════════════════════════════════════════════════════════
//  LIQUID MARKET WHALE SCANNER
// ═══════════════════════════════════════════════════════════════
let _scannerPolling = null;
let _scannerRunning = false;

async function loadScannerStatus() {
    const d = await apiFetch('/api/whales/liquid-scan/status');
    if (!d) return;

    // Update status badge
    const badge = $('#scanner-status-badge');
    const btn = $('#scanner-toggle-btn');
    const ss = d.scan_status || {};
    const cfg = d.config || {};
    const stats = d.stats || {};
    _scannerRunning = ss.is_running || cfg.enabled;

    if (badge) {
        if (_scannerRunning) {
            const statusText = ss.continuous_status_text || 'scanning';
            const iter = ss.continuous_iteration || 0;
            badge.textContent = `● ${statusText.toUpperCase()} (iter ${iter})`;
            badge.className = 'scanner-status-badge scanner-status-active';
        } else if (ss.last_scan_status === 'error') {
            badge.textContent = '● ERROR';
            badge.className = 'scanner-status-badge scanner-status-error';
        } else if (ss.last_scan_status === 'complete' || ss.last_scan_status === 'scanning') {
            badge.textContent = '● READY';
            badge.className = 'scanner-status-badge scanner-status-ready';
        } else {
            badge.textContent = '● IDLE';
            badge.className = 'scanner-status-badge scanner-status-idle';
        }
    }
    if (btn) {
        btn.innerHTML = _scannerRunning ? '⏹ Stop Scanner' : '▶ Start Scanner';
        btn.className = _scannerRunning ? 'scanner-btn scanner-btn-danger' : 'scanner-btn scanner-btn-primary';
    }

    // Update config inputs
    if (cfg.min_volume) _setVal('#scanner-cfg-volume', cfg.min_volume);
    if (cfg.min_liquidity) _setVal('#scanner-cfg-liquidity', cfg.min_liquidity);
    if (cfg.min_win_rate) _setVal('#scanner-cfg-winrate', cfg.min_win_rate);
    if (cfg.min_pnl) _setVal('#scanner-cfg-pnl', cfg.min_pnl);
    if (cfg.min_positions) _setVal('#scanner-cfg-positions', cfg.min_positions);
    if (cfg.interval_minutes) _setVal('#scanner-cfg-interval', cfg.interval_minutes);

    // Stats row — use continuous accumulators when running, otherwise last scan data
    const totalTrades = ss.continuous_total_trades || ss.last_scan_trades_analyzed || 0;
    const totalAddrs = ss.continuous_unique_addresses || ss.last_scan_addresses_discovered || 0;

    _setText('#scanner-stat-markets', ss.last_scan_markets || 0);
    _setText('#scanner-stat-cycle-markets', (ss.scanned_markets || []).length || 0);
    _setText('#scanner-stat-trades', _fmtNum(totalTrades));
    _setText('#scanner-stat-addresses', _fmtNum(totalAddrs));
    _setText('#scanner-stat-wallets', ss.last_scan_wallets || 0);
    _setText('#scanner-stat-candidates', stats.total_candidates || 0);
    _setText('#scanner-stat-promoted', stats.total_promoted || 0);
    _setText('#scanner-stat-duration', ss.last_scan_duration_s ? ss.last_scan_duration_s.toFixed(1) + 's' : '—');

    // Continuous mode live status text
    const liveStatus = $('#scanner-live-status');
    if (liveStatus) {
        if (_scannerRunning) {
            const iter = ss.continuous_iteration || 0;
            const phase = ss.continuous_status_text || 'scanning';
            const lbSeeded = ss.leaderboard_wallets_seeded || 0;
            const mktTrades = ss.market_trades_scanned || 0;
            const dedupSize = ss.dedup_cache_size || 0;
            let liveExtra = `${_fmtNum(totalTrades)} trades, ${_fmtNum(totalAddrs)} addresses`;
            if (lbSeeded > 0) liveExtra += ` · 🏆 ${lbSeeded} leaderboard`;
            if (mktTrades > 0) liveExtra += ` · 📊 ${_fmtNum(mktTrades)} market trades`;
            liveStatus.innerHTML = `<span class="scanner-live-dot"></span> Iteration #${iter} — ${phase} — ${liveExtra}`;
            liveStatus.style.display = 'flex';
        } else {
            liveStatus.style.display = 'none';
        }
    }

    // Update discovery source stats
    _setText('#scanner-stat-leaderboard', ss.leaderboard_wallets_seeded || 0);
    _setText('#scanner-stat-mkt-trades', _fmtNum(ss.market_trades_scanned || 0));

    // API Pool panel
    _renderApiPoolPanel(ss.api_pool);

    // Pipeline phases (pull from last scan result if available)
    _renderScannerPipeline(ss);

    // Scanned markets grid
    _renderScannerMarketsGrid(ss.scanned_markets || []);

    // Grade distribution bar
    _renderScannerGradeBar(d.stats?.grade_distribution || {});

    // Candidates table
    _renderScannerCandidates(d.candidates || []);
    _setText('#scanner-candidate-count', `${(d.candidates || []).length} candidates`);

    // Promoted list
    const promoted = d.promoted || [];
    const promWrap = $('#scanner-promoted-wrap');
    if (promWrap) {
        promWrap.style.display = promoted.length ? 'block' : 'none';
    }
    _renderScannerPromoted(promoted);
}

function _fmtNum(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return String(n);
}

function _setVal(sel, val) {
    const el = $(sel);
    if (el) el.value = val;
}
function _setText(sel, val) {
    const el = $(sel);
    if (el) el.textContent = val;
}

function _renderApiPoolPanel(pool) {
    const panel = $('#scanner-pool-panel');
    if (!panel) return;
    if (!pool) {
        panel.style.display = 'none';
        return;
    }
    panel.style.display = 'block';

    // Summary line
    const summary = $('#scanner-pool-summary');
    if (summary) {
        const strat = pool.strategy || 'least-loaded';
        const errRate = pool.error_rate || 0;
        const errClass = errRate > 10 ? 'pool-err-high' : errRate > 3 ? 'pool-err-med' : 'pool-err-low';
        summary.innerHTML = `
            <span class="pool-badge pool-strat">${strat}</span>
            <span class="pool-badge pool-rpm">⚡ ${pool.effective_rpm} RPM</span>
            <span class="pool-badge pool-healthy">${pool.healthy_count}/${pool.endpoint_count} healthy</span>
            <span class="pool-badge pool-reqs">📊 ${_fmtNum(pool.total_requests)} reqs</span>
            <span class="pool-badge ${errClass}">${errRate}% err</span>
        `;
    }

    // Per-endpoint cards
    const wrap = $('#scanner-pool-endpoints');
    if (wrap && pool.endpoints) {
        let html = '';
        pool.endpoints.forEach(ep => {
            const healthDot = ep.healthy ? '🟢' : '🔴';
            const lim = ep.limiter || {};
            const usedPct = lim.rpm > 0 ? Math.round((1 - (lim.available / lim.rpm)) * 100) : 0;
            const barColor = usedPct > 80 ? '#ef5350' : usedPct > 50 ? '#ffa726' : '#00e676';
            html += `
                <div class="pool-ep-card ${ep.healthy ? '' : 'pool-ep-down'}">
                    <div class="pool-ep-header">
                        <span class="pool-ep-health">${healthDot}</span>
                        <span class="pool-ep-name">${ep.name}</span>
                        <span class="pool-ep-rpm">${lim.rpm} RPM</span>
                    </div>
                    <div class="pool-ep-bar-bg">
                        <div class="pool-ep-bar-fill" style="width:${Math.min(usedPct,100)}%;background:${barColor};"></div>
                    </div>
                    <div class="pool-ep-stats">
                        <span>✅ ${_fmtNum(ep.total_successes)}</span>
                        <span>❌ ${ep.total_failures}</span>
                        <span>🎯 ${lim.available}/${lim.rpm}</span>
                        ${ep.consecutive_failures > 0 ? `<span class="pool-ep-fails">🔥 ${ep.consecutive_failures} fails</span>` : ''}
                    </div>
                    ${ep.last_error ? `<div class="pool-ep-error" title="${ep.last_error}">⚠️ ${ep.last_error.slice(0,50)}</div>` : ''}
                </div>
            `;
        });
        safeHTML(wrap, html);
    }
}

function _renderScannerPipeline(ss) {
    // Update pipeline phase values based on scan status data
    const lbSeeded = ss.leaderboard_wallets_seeded || 0;
    const markets = ss.last_scan_markets || 0;
    const trades = ss.continuous_total_trades || ss.last_scan_trades_analyzed || 0;
    const mktTrades = ss.market_trades_scanned || 0;
    const addrs = ss.continuous_unique_addresses || ss.last_scan_addresses_discovered || 0;
    const wallets = ss.last_scan_wallets || 0;
    const candidates = ss.last_scan_candidates || 0;

    _setText('#scanner-phase-0-val', lbSeeded ? `${lbSeeded} seeded` : '—');
    _setText('#scanner-phase-1-val', markets ? `${markets} mkts` : '—');
    _setText('#scanner-phase-2-val', trades ? `${_fmtNum(trades)} trades` : '—');
    _setText('#scanner-phase-2b-val', mktTrades ? `${_fmtNum(mktTrades)} trades` : '—');
    _setText('#scanner-phase-3-val', addrs ? `${_fmtNum(addrs)} addrs` : '—');
    _setText('#scanner-phase-4-val', wallets ? `${wallets} wallets` : '—');
    _setText('#scanner-phase-5-val', candidates ? `${candidates} found` : '—');

    // Highlight completed phases
    const phaseMap = {
        '0': lbSeeded > 0,
        '1': markets > 0,
        '2': trades > 0,
        '2b': mktTrades > 0,
        '3': addrs > 0,
        '4': wallets > 0,
        '5': candidates >= 0 && (ss.last_scan_status === 'complete' || ss.last_scan_status === 'scanning'),
    };
    for (const [key, done] of Object.entries(phaseMap)) {
        const el = $(`#scanner-phase-${key}`);
        if (el) {
            el.classList.toggle('scanner-phase-done', !!done);
        }
    }
}

function _renderScannerMarketsGrid(markets) {
    const wrap = $('#scanner-markets-wrap');
    const grid = $('#scanner-markets-grid');
    if (!wrap || !grid) return;
    if (!markets || !markets.length) {
        wrap.style.display = 'none';
        return;
    }
    wrap.style.display = 'block';
    let html = '';
    markets.slice(0, 20).forEach(m => {
        const volStr = m.volume >= 1000000 ? `$${(m.volume/1000000).toFixed(1)}M` :
                       m.volume >= 1000 ? `$${(m.volume/1000).toFixed(0)}K` : `$${m.volume}`;
        html += `<div class="scanner-mkt-card" title="${m.question || m.slug}">
            <div class="scanner-mkt-q">${(m.question || m.slug || '').slice(0, 50)}</div>
            <div class="scanner-mkt-vol">${volStr}</div>
        </div>`;
    });
    safeHTML(grid, html);
}

function _renderScannerGradeBar(gradeDist) {
    const bar = $('#scanner-grade-bar');
    const legend = $('#scanner-grade-legend');
    if (!bar) return;

    const gradeColors = {
        'S': '#ffd700', 'A': '#00e676', 'B': '#4fc3f7',
        'C': '#ffa726', 'D': '#ef5350', 'F': '#616161'
    };
    const gradeOrder = ['S', 'A', 'B', 'C', 'D', 'F'];
    const total = Object.values(gradeDist).reduce((s, v) => s + v, 0);
    if (total === 0) {
        safeHTML(bar, '<div class="scanner-grade-empty">No candidates yet</div>');
        if (legend) safeHTML(legend, '');
        return;
    }

    let barHTML = '';
    let legendHTML = '';
    for (const g of gradeOrder) {
        const count = gradeDist[g] || 0;
        if (count === 0) continue;
        const pct = ((count / total) * 100).toFixed(1);
        barHTML += `<div class="scanner-grade-segment" style="width:${pct}%;background:${gradeColors[g]};" title="${g}: ${count} (${pct}%)">${g}</div>`;
        legendHTML += `<span class="scanner-grade-legend-item"><span class="scanner-grade-dot" style="background:${gradeColors[g]};"></span>${g}: ${count}</span>`;
    }
    safeHTML(bar, barHTML);
    if (legend) safeHTML(legend, legendHTML);
}

function _renderScannerCandidates(candidates) {
    const container = $('#scanner-candidates-table');
    if (!container) return;
    if (!candidates.length) {
        safeHTML(container, '<div class="empty-state">Run a scan to discover whale candidates</div>');
        return;
    }

    let html = `
        <table class="scanner-table">
            <thead>
                <tr>
                    <th>Rank</th>
                    <th>Address</th>
                    <th>Grade</th>
                    <th>Score</th>
                    <th>PnL</th>
                    <th>Win Rate</th>
                    <th>Positions</th>
                    <th>Trade Vol</th>
                    <th>Mkts</th>
                    <th>Source</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
    `;
    candidates.forEach((c, i) => {
        const gradeClass = {S:'grade-s',A:'grade-a',B:'grade-b',C:'grade-c',D:'grade-d',F:'grade-f'}[c.grade] || 'grade-f';
        const pnlClass = c.total_pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
        const pnlStr = c.total_pnl >= 1000000 ? `$${(c.total_pnl/1000000).toFixed(1)}M` :
                        c.total_pnl >= 1000 ? `$${(c.total_pnl/1000).toFixed(1)}K` : `$${c.total_pnl.toFixed(0)}`;
        const volStr = c.total_volume >= 1000000 ? `$${(c.total_volume/1000000).toFixed(1)}M` :
                       c.total_volume >= 1000 ? `$${(c.total_volume/1000).toFixed(1)}K` : `$${c.total_volume.toFixed(0)}`;
        const wrPct = (c.win_rate * 100).toFixed(1);
        const sd = c.scan_data || {};
        const tradeVol = sd.trade_volume || 0;
        const tradeVolStr = tradeVol >= 1000000 ? `$${(tradeVol/1000000).toFixed(1)}M` :
                            tradeVol >= 1000 ? `$${(tradeVol/1000).toFixed(1)}K` : tradeVol > 0 ? `$${tradeVol.toFixed(0)}` : '—';
        const mktsCount = (sd.markets_seen || []).length || c.liquid_market_count || 0;
        const srcTag = c.source === 'trade_discovery' ? '🔍 CLOB' :
                       c.source === 'known_wallet' ? '📋 Known' :
                       c.source === 'leaderboard_profit' ? '🏆 Profit' :
                       c.source === 'leaderboard_volume' ? '📈 Volume' :
                       c.source === 'leaderboard_both' ? '🏆📈 Top' :
                       c.source === 'market_scan' ? '📊 Market' :
                       c.source || '—';

        html += `
            <tr class="scanner-candidate-row" data-address="${c.address}">
                <td class="scanner-rank">#${i + 1}</td>
                <td class="scanner-name">
                    <span class="scanner-name-text" title="${c.address}">${c.name || c.address.slice(0,10)}</span>
                </td>
                <td><span class="scanner-grade ${gradeClass}">${c.grade}</span></td>
                <td class="scanner-score">${c.score.toFixed(1)}</td>
                <td class="${pnlClass}">${pnlStr}</td>
                <td>${wrPct}%</td>
                <td>${c.active_positions}</td>
                <td>${tradeVolStr}</td>
                <td>${mktsCount}</td>
                <td><span class="scanner-source-tag">${srcTag}</span></td>
                <td class="scanner-actions">
                    <button class="scanner-action-btn scanner-promote-btn" onclick="promoteCandidate('${c.address}')" title="Promote to tracked">⬆️ Track</button>
                    <button class="scanner-action-btn scanner-view-btn" onclick="openWhaleProfile('${c.address}')" title="View profile">👁️</button>
                    <button class="scanner-action-btn scanner-dismiss-btn" onclick="dismissCandidate('${c.address}')" title="Dismiss">✕</button>
                </td>
            </tr>
        `;

        // Expandable top markets row
        if (c.top_markets && c.top_markets.length) {
            html += `
                <tr class="scanner-markets-detail" style="display:none;" id="scanner-detail-${c.address.slice(0,8)}">
                    <td colspan="11">
                        <div class="scanner-top-markets">
                            ${c.top_markets.map(m => `
                                <span class="scanner-mkt-chip" title="${m.slug}">
                                    ${m.title} <span class="${m.pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}">${m.pnl >= 0 ? '+' : ''}$${m.pnl.toFixed(0)}</span>
                                </span>
                            `).join('')}
                        </div>
                    </td>
                </tr>
            `;
        }
    });
    html += '</tbody></table>';
    safeHTML(container, html);

    // Click to expand market details
    container.querySelectorAll('.scanner-candidate-row').forEach(row => {
        row.addEventListener('click', (e) => {
            if (e.target.closest('.scanner-actions')) return;
            const addr = row.dataset.address;
            const detail = document.getElementById(`scanner-detail-${addr.slice(0,8)}`);
            if (detail) {
                detail.style.display = detail.style.display === 'none' ? 'table-row' : 'none';
            }
        });
    });
}

function _renderScannerPromoted(promoted) {
    const list = $('#scanner-promoted-list');
    if (!list) return;
    if (!promoted.length) { safeHTML(list, ''); return; }
    let html = '';
    promoted.forEach(p => {
        const pnlStr = p.total_pnl >= 1000 ? `$${(p.total_pnl/1000).toFixed(1)}K` : `$${p.total_pnl.toFixed(0)}`;
        html += `
            <div class="scanner-promoted-card">
                <span class="scanner-grade ${({S:'grade-s',A:'grade-a',B:'grade-b',C:'grade-c'})[p.grade] || 'grade-c'}">${p.grade}</span>
                <span class="scanner-promoted-name">${p.name || p.address.slice(0,10)}</span>
                <span class="pnl-pos">${pnlStr}</span>
                <span class="scanner-promoted-wr">${(p.win_rate * 100).toFixed(1)}% WR</span>
                <span class="scanner-promoted-date">${p.promoted_at ? new Date(p.promoted_at).toLocaleDateString() : '—'}</span>
            </div>
        `;
    });
    safeHTML(list, html);
}

async function toggleWhaleScanner() {
    if (_scannerRunning) {
        await apiFetch('/api/whales/liquid-scan/stop', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
        _scannerRunning = false;
        _stopScannerPolling();
    } else {
        // Gather config from inputs
        const cfg = {
            min_volume: parseFloat($('#scanner-cfg-volume')?.value || 50000),
            min_liquidity: parseFloat($('#scanner-cfg-liquidity')?.value || 10000),
            min_win_rate: parseFloat($('#scanner-cfg-winrate')?.value || 0.45),
            min_pnl: parseFloat($('#scanner-cfg-pnl')?.value || 5000),
            min_positions: parseInt($('#scanner-cfg-positions')?.value || 5),
            interval_minutes: parseInt($('#scanner-cfg-interval')?.value || 15),
        };
        await apiFetch('/api/whales/liquid-scan/start', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(cfg),
        });
        _scannerRunning = true;
        _startScannerPolling();
    }
    // Immediate refresh
    await loadScannerStatus();
}

async function runSingleScan() {
    const btn = document.querySelector('.scanner-btn-secondary');
    if (btn) { btn.disabled = true; btn.innerHTML = '⏳ Scanning...'; }
    const badge = $('#scanner-status-badge');
    if (badge) { badge.textContent = '● SCANNING'; badge.className = 'scanner-status-badge scanner-status-active'; }

    try {
        const res = await apiFetch('/api/whales/liquid-scan/run', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: '{}',
        });
        if (res && res.status === 'complete') {
            const trades = res.trades_analyzed || 0;
            const addrs = res.unique_addresses || 0;
            showToast?.(`🔍 Scan complete: ${res.candidates_found} candidates from ${trades} trades, ${addrs} addresses in ${res.duration_s}s`, 'success');
        } else if (res && res.errors?.length) {
            showToast?.(`⚠️ Scan had errors: ${res.errors[0]}`, 'warning');
        }
    } catch (e) {
        showToast?.('❌ Scan failed: ' + e.message, 'error');
    }

    if (btn) { btn.disabled = false; btn.innerHTML = '⚡ Quick Scan'; }
    await loadScannerStatus();
}

async function promoteCandidate(address) {
    const res = await apiFetch('/api/whales/liquid-scan/promote', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({address}),
    });
    if (res && res.promoted) {
        showToast?.(res.message || '🐋 Whale promoted!', 'success');
        await loadScannerStatus();
    }
}

async function dismissCandidate(address) {
    await apiFetch('/api/whales/liquid-scan/dismiss', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({address}),
    });
    await loadScannerStatus();
}

function toggleScannerConfig() {
    const panel = $('#scanner-config-panel');
    if (panel) panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}

async function saveScannerConfig() {
    const cfg = {
        min_volume: parseFloat($('#scanner-cfg-volume')?.value || 50000),
        min_liquidity: parseFloat($('#scanner-cfg-liquidity')?.value || 10000),
        min_win_rate: parseFloat($('#scanner-cfg-winrate')?.value || 0.45),
        min_pnl: parseFloat($('#scanner-cfg-pnl')?.value || 5000),
        min_positions: parseInt($('#scanner-cfg-positions')?.value || 5),
        interval_minutes: parseInt($('#scanner-cfg-interval')?.value || 15),
    };
    await apiFetch('/api/whales/liquid-scan/config', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(cfg),
    });
    showToast?.('✅ Scanner config saved', 'success');
    toggleScannerConfig();
}

function _startScannerPolling() {
    _stopScannerPolling();
    _scannerPolling = setInterval(loadScannerStatus, 5000);  // 5s when running continuously
}
function _stopScannerPolling() {
    if (_scannerPolling) { clearInterval(_scannerPolling); _scannerPolling = null; }
}

// ═══════════════════════════════════════════════════════════════
//  ADMIN PANEL
// ═══════════════════════════════════════════════════════════════

// Store alerts for client-side filtering
let _adminAlerts = [];

async function updateAdminPanel() {
    const d = await apiFetch('/api/admin');
    if (!d) return;

    // ── Health Score Gauge ────────────────────────────────────
    const health = d.health || {};
    const score = health.score || 0;
    const grade = health.grade || '—';
    safeHTML($('#health-gauge-score'), String(score));
    safeHTML($('#health-gauge-grade'), `Grade ${grade}`);

    // Animate the SVG ring
    const ring = $('#health-ring-fill');
    if (ring) {
        const circumference = 326.7; // 2 * π * 52
        const offset = circumference - (score / 100) * circumference;
        ring.style.strokeDashoffset = offset;
        // Color based on score
        if (score >= 90) ring.style.stroke = 'var(--accent-green)';
        else if (score >= 75) ring.style.stroke = 'var(--accent-blue)';
        else if (score >= 60) ring.style.stroke = 'var(--accent-orange)';
        else ring.style.stroke = 'var(--accent-red)';
    }

    // Health Issues
    const issues = health.issues || [];
    const issuesSection = $('#admin-health-issues');
    const issuesList = $('#admin-issues-list');
    if (issues.length > 0) {
        issuesSection.style.display = 'block';
        const icons = { critical: '🔴', error: '🟠', warning: '🟡', info: 'ℹ️' };
        const issuesHTML = issues.map(i => `
            <div class="admin-issue-item admin-issue-${i.severity}">
                <span class="admin-issue-icon">${icons[i.severity] || 'ℹ️'}</span>
                <span class="admin-issue-text">${escapeHTML(i.message)}</span>
            </div>
        `).join('');
        safeHTML(issuesList, issuesHTML);
    } else {
        issuesSection.style.display = 'none';
    }

    // ── Overview Cards ───────────────────────────────────────
    const sys = d.system_info || {};
    safeHTML($('#admin-uptime'), sys.process_uptime_human || '—');
    safeHTML($('#admin-uptime-sub'), `Since: ${sys.process_uptime_secs > 0 ? new Date(Date.now() - sys.process_uptime_secs * 1000).toLocaleString() : '—'}`);

    const eh = d.engine_health || {};
    safeHTML($('#admin-cycles'), String(eh.cycle_count || 0));
    safeHTML($('#admin-cycles-sub'), `Interval: ${eh.cycle_interval_secs || 0}s`);

    const db = d.db_stats || {};
    safeHTML($('#admin-db-size'), `${db.size_mb || 0} MB`);
    safeHTML($('#admin-db-rows'), `Rows: ${(db.total_rows || 0).toLocaleString()}`);

    const cost = d.cost_tracking || {};
    safeHTML($('#admin-api-cost'), `$${fmt(cost.estimated_total_cost_usd, 4)}`);
    safeHTML($('#admin-api-cost-sub'), `LLM: $${fmt(cost.estimated_llm_cost_usd, 4)} | Search: $${fmt(cost.estimated_search_cost_usd, 4)}`);

    safeHTML($('#admin-keys-count'), `${d.keys_configured || 0} / ${d.keys_total || 0}`);
    safeHTML($('#admin-keys-sub'), 'Configured');

    const logInfo = d.log_info || {};
    safeHTML($('#admin-log-size'), `${logInfo.size_mb || 0} MB`);
    safeHTML($('#admin-log-lines'), `Lines: ${(logInfo.lines || 0).toLocaleString()}`);

    // Memory & threads card
    safeHTML($('#admin-memory'), `${sys.memory_rss_mb || 0} MB`);
    safeHTML($('#admin-memory-sub'), `Threads: ${sys.thread_count || 0} | FDs: ${sys.open_fds || 0}`);

    // Win/loss card
    const ts = d.trading_summary || {};
    safeHTML($('#admin-win-loss'), `${ts.win_count || 0} / ${ts.loss_count || 0}`);
    safeHTML($('#admin-win-loss-sub'), `Neutral: ${ts.neutral_count || 0}`);

    // ── Cycle Performance Chart ──────────────────────────────
    const cycleHistory = d.cycle_history || [];
    updateAdminCycleChart(cycleHistory);
    if (cycleHistory.length > 0) {
        const avgDur = cycleHistory.reduce((s, c) => s + c.duration_secs, 0) / cycleHistory.length;
        const maxDur = Math.max(...cycleHistory.map(c => c.duration_secs));
        safeHTML($('#admin-cycle-stats'), `Avg: ${avgDur.toFixed(1)}s | Max: ${maxDur.toFixed(1)}s | Cycles: ${cycleHistory.length}`);
    } else {
        safeHTML($('#admin-cycle-stats'), 'No cycle data yet');
    }

    // ── Engine Health ────────────────────────────────────────
    const engBadge = $('#admin-engine-badge');
    if (eh.running) {
        engBadge.textContent = '● RUNNING';
        engBadge.className = 'badge badge-ok';
    } else {
        engBadge.textContent = '● STOPPED';
        engBadge.className = 'badge badge-danger';
    }
    safeHTML($('#admin-eh-status'), eh.running ? '✅ Running' : '❌ Stopped');
    safeHTML($('#admin-eh-thread'), eh.thread_alive ? '✅ Alive' : '❌ Dead');
    const modeStr = eh.live_trading ? '🔴 LIVE' : (eh.paper_mode ? '📝 Paper' : '⚠️ Dry Run');
    safeHTML($('#admin-eh-mode'), modeStr);
    safeHTML($('#admin-eh-cycles'), String(eh.cycle_count || 0));
    safeHTML($('#admin-eh-interval'), `${eh.cycle_interval_secs || 0}s`);
    safeHTML($('#admin-eh-error'), eh.error || 'None');
    if (eh.error) {
        $('#admin-eh-error').className = 'admin-health-value admin-error-text';
    } else {
        $('#admin-eh-error').className = 'admin-health-value';
        $('#admin-eh-error').style.color = 'var(--accent-green)';
    }

    // ── API Keys (editable inputs with save) ─────────────────
    const keys = d.api_keys || {};
    const keysGrid = $('#admin-keys-grid');
    if (keysGrid) {
        // Only re-render if user isn't actively editing
        if (!keysGrid.querySelector(':focus')) {
            const keysHTML = Object.entries(keys).map(([name, val]) => {
                const isSet = val === true;
                const isBoolVal = typeof val === 'string' && (val.toLowerCase() === 'true' || val.toLowerCase() === 'false');
                let statusIcon = isSet ? '✅' : (isBoolVal ? '⚙️' : '❌');
                let statusText = isSet ? 'SET' : (isBoolVal ? val : 'MISSING');
                let statusClass = isSet ? 'admin-key-ok' : (isBoolVal ? 'admin-key-value' : 'admin-key-missing');
                return `<div class="admin-key-item admin-key-editable">
                    <div class="admin-key-top">
                        <span class="admin-key-name">${escHtml(String(name))}</span>
                        <span class="admin-key-status ${statusClass}">${statusIcon} ${statusText}</span>
                    </div>
                    <input type="${name.includes('KEY') || name.includes('SECRET') || name.includes('PRIVATE') || name.includes('DSN') || name.includes('PASSPHRASE') ? 'password' : 'text'}"
                           class="admin-key-input" id="admin-env-${name}"
                           placeholder="${isSet ? '••• (already set — leave blank to keep)' : 'Enter value…'}"
                           data-env-key="${name}">
                </div>`;
            }).join('');
            safeHTML(keysGrid, keysHTML);
        }
    }
    safeHTML($('#admin-keys-badge'), `${d.keys_configured || 0} / ${d.keys_total || 0} configured`);

    // ── Feature Flags (interactive toggles) ──────────────────
    const flags = d.feature_flags || {};
    const flagsGrid = $('#admin-flags-grid');
    const flagNames = {
        ensemble_enabled: '🤖 Ensemble',
        drawdown_enabled: '📉 Drawdown Guard',
        wallet_scanner_enabled: '🐋 Whale Scanner',
        alerts_enabled: '🔔 Alerts',
        cache_enabled: '💾 Cache',
        twap_enabled: '📊 TWAP',
        adaptive_pricing: '🎯 Adaptive Pricing',
        dry_run: '🧪 Dry Run',
        kill_switch: '🛑 Kill Switch',
        paper_mode: '📝 Paper Mode',
        live_trading: '🔴 Live Trading',
        daily_summary: '📧 Daily Summary',
        metrics_enabled: '📈 Metrics',
    };
    if (flagsGrid) {
        const flagsHTML = Object.entries(flags).map(([key, val]) => {
            const label = flagNames[key] || key;
            const isOn = val === true;
            return `<div class="admin-flag-item">
                <span class="admin-flag-name">${label}</span>
                <label class="toggle-switch">
                    <input type="checkbox" ${isOn ? 'checked' : ''} onchange="toggleFlag('${key}', this.checked)">
                    <span class="toggle-slider"></span>
                </label>
            </div>`;
        }).join('');
        safeHTML(flagsGrid, flagsHTML);
    }

    // ── Cost Tracker Cards ───────────────────────────────────
    safeHTML($('#admin-llm-calls'), String(cost.llm_calls || 0));
    safeHTML($('#admin-llm-model'), `Model: ${cost.primary_model || '—'}`);
    safeHTML($('#admin-total-tokens'), (cost.llm_total_tokens || 0).toLocaleString());
    safeHTML($('#admin-tokens-detail'), `In: ${(cost.llm_input_tokens || 0).toLocaleString()} | Out: ${(cost.llm_output_tokens || 0).toLocaleString()}`);
    safeHTML($('#admin-search-calls'), String(cost.search_calls || 0));
    safeHTML($('#admin-search-cost'), `Cost: $${fmt(cost.estimated_search_cost_usd, 4)}`);
    safeHTML($('#admin-api-errors'), String(cost.api_errors || 0));

    // Per-API cost breakdown from CostTracker
    const costBreakdownDiv = $('#admin-cost-breakdown');
    const ctCalls = cost.cost_tracker_calls || {};
    const ctEntries = Object.entries(ctCalls);
    if (ctEntries.length > 0) {
        const itemsHTML = ctEntries.sort((a, b) => b[1] - a[1]).map(([api, count]) =>
            `<div class="admin-cost-item">
                <span class="admin-cost-api-name">${api}</span>
                <span class="admin-cost-api-count">${count}×</span>
            </div>`
        ).join('');
        safeHTML(costBreakdownDiv, `
            <div class="admin-cost-title">📊 Per-API Call Breakdown</div>
            <div class="admin-cost-items">${itemsHTML}</div>
        `);
    } else {
        safeHTML(costBreakdownDiv, '');
    }

    // Ensemble models
    const ensembleDiv = $('#admin-ensemble-info');
    if (cost.ensemble_enabled && cost.ensemble_models && cost.ensemble_models.length > 1) {
        const modelsHTML = cost.ensemble_models.map(m => {
            const isPrimary = m === cost.primary_model;
            return `<span class="admin-model-chip ${isPrimary ? 'primary' : ''}">${isPrimary ? '⭐ ' : ''}${m}</span>`;
        }).join('');
        safeHTML(ensembleDiv, `
            <div class="admin-ensemble-title">🤖 Ensemble Models</div>
            <div class="admin-ensemble-models">${modelsHTML}</div>
        `);
    } else {
        safeHTML(ensembleDiv, `
            <div class="admin-ensemble-title">🤖 Model</div>
            <div class="admin-ensemble-models">
                <span class="admin-model-chip primary">⭐ ${cost.primary_model || '—'}</span>
                <span style="font-size:0.75rem;color:var(--text-muted);align-self:center;">Ensemble: disabled</span>
            </div>
        `);
    }

    // ── Rate Limiter ─────────────────────────────────────────
    const rlStats = d.rate_limiter || {};
    const rlEntries = Object.entries(rlStats);
    const rlBody = $('#admin-rate-body');
    if (rlEntries.length === 0) {
        safeHTML(rlBody, '<tr><td colspan="4" class="empty-state">No rate limiter data</td></tr>');
    } else {
        const rlHTML = rlEntries.map(([endpoint, stats]) => {
            const total = stats.total_requests || 0;
            const waits = stats.total_waits || 0;
            const ratio = total > 0 ? (waits / total * 100).toFixed(1) : '0.0';
            const ratioClass = ratio > 50 ? 'pnl-negative' : ratio > 20 ? 'pnl-zero' : 'pnl-positive';
            return `<tr>
                <td><code>${endpoint}</code></td>
                <td>${total.toLocaleString()}</td>
                <td>${waits.toLocaleString()}</td>
                <td class="${ratioClass}">${ratio}%</td>
            </tr>`;
        }).join('');
        safeHTML(rlBody, rlHTML);
    }

    // ── Trading Summary ──────────────────────────────────────
    safeHTML($('#admin-ts-forecasts'), String(ts.total_forecasts || 0));
    safeHTML($('#admin-ts-today-forecasts'), String(ts.today_forecasts || 0));
    safeHTML($('#admin-ts-trades'), String(ts.total_trades || 0));
    safeHTML($('#admin-ts-today-trades'), String(ts.today_trades || 0));
    safeHTML($('#admin-ts-avg-cand'), String(ts.avg_candidates_per_cycle || 0));

    // Hourly activity chart
    updateAdminHourlyChart(ts.hourly_activity || []);

    // Decision breakdown chips
    const breakdown = ts.decision_breakdown || {};
    const breakdownDiv = $('#admin-decision-breakdown');
    const bdHTML = Object.entries(breakdown).map(([decision, count]) => {
        const cls = decision === 'TRADE' ? 'dc-trade' : decision === 'NO TRADE' ? 'dc-no-trade' : 'dc-skip';
        return `<div class="admin-decision-chip ${cls}">
            <span class="admin-decision-label">${decision}</span>
            <span class="admin-decision-count">${count}</span>
        </div>`;
    }).join('');
    safeHTML(breakdownDiv, bdHTML || '<span style="color:var(--text-muted);font-size:0.78rem;">No decisions recorded yet</span>');

    // ── Database Tables ──────────────────────────────────────
    const tableStats = db.table_stats || {};
    const maxRows = Math.max(1, ...Object.values(tableStats).filter(v => v >= 0));
    const exportable = new Set(['forecasts', 'trades', 'positions', 'markets', 'candidates', 'alerts_log', 'audit_trail', 'tracked_wallets', 'wallet_signals']);
    const dbBody = $('#admin-db-body');
    const dbHTML = Object.entries(tableStats).map(([table, count]) => {
        let statusBadge;
        if (count === -1) {
            statusBadge = '<span class="admin-key-status admin-key-missing">NOT CREATED</span>';
        } else if (count === 0) {
            statusBadge = '<span class="admin-key-status admin-key-value">EMPTY</span>';
        } else {
            statusBadge = '<span class="admin-key-status admin-key-ok">ACTIVE</span>';
        }
        const barPct = count > 0 ? Math.max(2, (count / maxRows) * 100) : 0;
        const exportBtn = exportable.has(table) && count > 0
            ? `<button class="btn-export-sm" onclick="adminExportTable('${table}')">⬇ CSV</button>`
            : '<span style="color:var(--text-muted);font-size:0.65rem;">—</span>';
        return `<tr>
            <td><code>${table}</code></td>
            <td>${count >= 0 ? count.toLocaleString() : '—'}</td>
            <td><div class="admin-row-bar"><div class="admin-row-bar-fill" style="width:${barPct}%"></div></div></td>
            <td>${statusBadge}</td>
            <td>${exportBtn}</td>
        </tr>`;
    }).join('');
    safeHTML(dbBody, dbHTML || '<tr><td colspan="5" class="empty-state">No tables found</td></tr>');

    // DB Pragma
    const pragma = db.pragma || {};
    const pragmaDiv = $('#admin-db-pragma');
    if (Object.keys(pragma).length > 0) {
        const pragmaHTML = Object.entries(pragma).map(([k, v]) =>
            `<div class="admin-pragma-chip">
                <span class="admin-pragma-key">${k}:</span>
                <span class="admin-pragma-val">${v}</span>
            </div>`
        ).join('');
        safeHTML(pragmaDiv, pragmaHTML);
    }

    // ── Storage Breakdown ────────────────────────────────────
    const dirBreak = d.dir_breakdown || {};
    const storageGrid = $('#admin-storage-grid');
    const storageIcons = { data: '🗄️', logs: '📜', reports: '📊' };
    const storageHTML = Object.entries(dirBreak).map(([dir, info]) => `
        <div class="admin-storage-card">
            <div class="admin-storage-icon">${storageIcons[dir] || '📁'}</div>
            <div class="admin-storage-name">${dir}/</div>
            <div class="admin-storage-size">${info.size_mb} MB</div>
            <div class="admin-storage-files">${info.file_count} files</div>
        </div>
    `).join('');
    safeHTML(storageGrid, storageHTML);

    // ── Internal Metrics ─────────────────────────────────────
    const counters = d.counters || {};
    const gauges = d.gauges || {};
    const histograms = d.histograms || {};
    const countersGrid = $('#admin-counters-grid');
    const gaugesGrid = $('#admin-gauges-grid');
    const histogramsGrid = $('#admin-histograms-grid');

    const counterEntries = Object.entries(counters);
    if (counterEntries.length === 0) {
        safeHTML(countersGrid, '<div style="color:var(--text-muted);font-size:0.78rem;padding:8px;">No counters recorded yet</div>');
    } else {
        const cHTML = counterEntries.sort((a, b) => b[1] - a[1]).map(([name, val]) =>
            `<div class="admin-metric-item">
                <span class="admin-metric-name">${name}</span>
                <span class="admin-metric-value">${typeof val === 'number' ? val.toLocaleString() : val}</span>
            </div>`
        ).join('');
        safeHTML(countersGrid, cHTML);
    }

    const gaugeEntries = Object.entries(gauges);
    if (gaugeEntries.length === 0) {
        safeHTML(gaugesGrid, '<div style="color:var(--text-muted);font-size:0.78rem;padding:8px;">No gauges recorded yet</div>');
    } else {
        const gHTML = gaugeEntries.sort((a, b) => b[1] - a[1]).map(([name, val]) =>
            `<div class="admin-metric-item">
                <span class="admin-metric-name">${name}</span>
                <span class="admin-metric-value">${typeof val === 'number' ? Number(val).toFixed(4) : val}</span>
            </div>`
        ).join('');
        safeHTML(gaugesGrid, gHTML);
    }

    // Histograms
    const histEntries = Object.entries(histograms);
    if (histEntries.length === 0) {
        safeHTML(histogramsGrid, '<div style="color:var(--text-muted);font-size:0.78rem;padding:8px;">No histograms recorded yet</div>');
    } else {
        const hHTML = histEntries.sort((a, b) => (b[1].count || 0) - (a[1].count || 0)).map(([name, val]) =>
            `<div class="admin-metric-item">
                <span class="admin-metric-name">${name}</span>
                <span class="admin-metric-value" title="count=${val.count} min=${Number(val.min).toFixed(2)} max=${Number(val.max).toFixed(2)} avg=${Number(val.avg).toFixed(2)}">
                    n=${val.count} avg=${Number(val.avg).toFixed(2)}
                </span>
            </div>`
        ).join('');
        safeHTML(histogramsGrid, hHTML);
    }

    // ── Recent Alerts Feed ───────────────────────────────────
    _adminAlerts = d.recent_alerts || [];
    renderAdminAlerts(_adminAlerts);

    // ── System Info ──────────────────────────────────────────
    safeHTML($('#admin-sys-hostname'), sys.hostname || '—');
    safeHTML($('#admin-sys-platform'), sys.platform || '—');
    safeHTML($('#admin-sys-python'), sys.python_version || '—');
    safeHTML($('#admin-sys-arch'), sys.architecture || '—');
    safeHTML($('#admin-sys-pid'), String(sys.pid || '—'));
    safeHTML($('#admin-sys-cpu'), String(sys.cpu_count || '—'));
    safeHTML($('#admin-sys-fds'), String(sys.open_fds || '—'));
    safeHTML($('#admin-sys-dbpath'), db.path || '—');

    const cfgInfo = d.config_info || {};
    safeHTML($('#admin-sys-configpath'), cfgInfo.path || '—');
    safeHTML($('#admin-sys-config-mod'), cfgInfo.last_modified ? shortDate(cfgInfo.last_modified) : '—');

    // ── Backup Files ─────────────────────────────────────────
    const backups = d.backup_files || [];
    const backupBody = $('#admin-backup-body');
    if (backups.length === 0) {
        safeHTML(backupBody, '<tr><td colspan="3" class="empty-state">No backup files found</td></tr>');
    } else {
        const bHTML = backups.map(b =>
            `<tr>
                <td><code>${b.name}</code></td>
                <td>${b.size_mb} MB</td>
                <td>${shortDate(b.modified)}</td>
            </tr>`
        ).join('');
        safeHTML(backupBody, bHTML);
    }

    // ── Log Stats Bar ────────────────────────────────────────
    const logStatsDiv = $('#admin-log-stats');
    safeHTML(logStatsDiv, `
        <div class="admin-log-stat">
            <span class="admin-log-stat-label">Size:</span>
            <span class="admin-log-stat-value">${logInfo.size_mb || 0} MB</span>
        </div>
        <div class="admin-log-stat">
            <span class="admin-log-stat-label">Lines:</span>
            <span class="admin-log-stat-value">${(logInfo.lines || 0).toLocaleString()}</span>
        </div>
        <div class="admin-log-stat admin-log-stat-errors">
            <span class="admin-log-stat-label">Errors:</span>
            <span class="admin-log-stat-value">${logInfo.error_count || 0}</span>
        </div>
        <div class="admin-log-stat admin-log-stat-warns">
            <span class="admin-log-stat-label">Warnings:</span>
            <span class="admin-log-stat-value">${logInfo.warn_count || 0}</span>
        </div>
    `);

    // ── Log Tail (auto-load on first visit) ──────────────────
    if ($('#admin-log-content').textContent === 'Loading log…') {
        refreshLogTail();
    }
}

// ── Admin Charts ─────────────────────────────────────────────

function updateAdminCycleChart(cycleHistory) {
    const ctx = $('#admin-cycle-chart');
    if (!ctx) return;

    const labels = cycleHistory.map(c => `#${c.cycle_id}`);
    const durations = cycleHistory.map(c => c.duration_secs);
    const scanned = cycleHistory.map(c => c.markets_scanned);
    const errors = cycleHistory.map(c => c.errors || 0);

    if (_charts.adminCycle) {
        _charts.adminCycle.data.labels = labels;
        _charts.adminCycle.data.datasets[0].data = durations;
        _charts.adminCycle.data.datasets[1].data = scanned;
        _charts.adminCycle.update('none');
        return;
    }

    _charts.adminCycle = new Chart(ctx, {
        type: 'bar',
        data: {
            labels,
            datasets: [
                {
                    label: 'Duration (s)',
                    data: durations,
                    backgroundColor: 'rgba(76,141,255,0.6)',
                    borderColor: 'rgba(76,141,255,1)',
                    borderWidth: 1,
                    borderRadius: 3,
                    yAxisID: 'y',
                },
                {
                    label: 'Markets Scanned',
                    data: scanned,
                    type: 'line',
                    borderColor: 'rgba(0,214,143,0.8)',
                    backgroundColor: 'rgba(0,214,143,0.1)',
                    borderWidth: 2,
                    pointRadius: 2,
                    fill: true,
                    yAxisID: 'y1',
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { intersect: false, mode: 'index' },
            plugins: {
                legend: { display: true, labels: { color: '#8b8fa3', font: { size: 10 } } },
            },
            scales: {
                x: { ticks: { color: '#8b8fa3', font: { size: 9 } }, grid: { color: 'rgba(255,255,255,0.04)' } },
                y: {
                    position: 'left',
                    title: { display: true, text: 'Duration (s)', color: '#8b8fa3', font: { size: 10 } },
                    ticks: { color: '#8b8fa3', font: { size: 9 } },
                    grid: { color: 'rgba(255,255,255,0.04)' },
                },
                y1: {
                    position: 'right',
                    title: { display: true, text: 'Markets', color: '#8b8fa3', font: { size: 10 } },
                    ticks: { color: '#8b8fa3', font: { size: 9 } },
                    grid: { drawOnChartArea: false },
                },
            },
        },
    });
}

function updateAdminHourlyChart(hourlyData) {
    const ctx = $('#admin-hourly-chart');
    if (!ctx) return;

    // Fill all 24 hours
    const hourMap = {};
    hourlyData.forEach(h => { hourMap[h.hour] = h.count; });
    const labels = [];
    const counts = [];
    for (let i = 0; i < 24; i++) {
        const hh = String(i).padStart(2, '0');
        labels.push(hh + ':00');
        counts.push(hourMap[hh] || 0);
    }

    if (_charts.adminHourly) {
        _charts.adminHourly.data.labels = labels;
        _charts.adminHourly.data.datasets[0].data = counts;
        _charts.adminHourly.update('none');
        return;
    }

    _charts.adminHourly = new Chart(ctx, {
        type: 'bar',
        data: {
            labels,
            datasets: [{
                label: 'Forecasts (24h)',
                data: counts,
                backgroundColor: 'rgba(160,120,255,0.5)',
                borderColor: 'rgba(160,120,255,0.8)',
                borderWidth: 1,
                borderRadius: 2,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                x: { ticks: { color: '#8b8fa3', font: { size: 8 }, maxRotation: 0 }, grid: { display: false } },
                y: { ticks: { color: '#8b8fa3', font: { size: 9 }, stepSize: 1 }, grid: { color: 'rgba(255,255,255,0.04)' }, beginAtZero: true },
            },
        },
    });
}

// ── Admin Alerts ─────────────────────────────────────────────

function renderAdminAlerts(alerts) {
    const feed = $('#admin-alerts-feed');
    if (alerts.length === 0) {
        safeHTML(feed, '<div class="empty-state">No alerts recorded</div>');
        return;
    }
    const html = alerts.map(a => {
        const levelCls = `admin-alert-level-${a.level || 'info'}`;
        return `<div class="admin-alert-item" data-level="${a.level || 'info'}">
            <span class="admin-alert-level ${levelCls}">${a.level || 'info'}</span>
            <span class="admin-alert-msg">${escapeHTML(a.message || '')}</span>
            <span class="admin-alert-time">${a.time ? shortDate(a.time) : '—'}</span>
        </div>`;
    }).join('');
    safeHTML(feed, html);
}

function filterAdminAlerts() {
    const level = $('#admin-alert-filter').value;
    if (level === 'all') {
        renderAdminAlerts(_adminAlerts);
    } else {
        renderAdminAlerts(_adminAlerts.filter(a => a.level === level));
    }
}

// ── Admin Log Tail ───────────────────────────────────────────

async function refreshLogTail() {
    const linesSel = $('#admin-log-lines-select');
    const n = linesSel ? linesSel.value : 100;
    const data = await apiFetch(`/api/admin/log-tail?lines=${n}`);
    const pre = $('#admin-log-content');
    if (!data || !data.lines || data.lines.length === 0) {
        pre.textContent = 'No log output available.';
        return;
    }
    // Colorize log lines
    const html = data.lines.map(line => {
        let cls = '';
        if (/error|exception|traceback/i.test(line)) cls = 'log-error';
        else if (/warn/i.test(line)) cls = 'log-warning';
        else if (/info/i.test(line)) cls = 'log-info';
        return cls ? `<span class="${cls}">${escapeHTML(line)}</span>` : escapeHTML(line);
    }).join('\n');
    pre.innerHTML = html;
    // Scroll to bottom
    const viewer = $('#admin-log-viewer');
    if (viewer) viewer.scrollTop = viewer.scrollHeight;
}

// ── Admin Actions ────────────────────────────────────────────

function refreshAdminPanel() {
    updateAdminPanel();
    showToast('Admin panel refreshed', 'success');
}

async function adminVacuumDB() {
    showConfirmModal('Vacuum Database', 'This will compact the database file and reclaim unused space. Continue?', async () => {
        const res = await apiFetch('/api/admin/db-vacuum', { method: 'POST' });
        if (res && res.ok) {
            showToast(res.message || 'Database vacuumed successfully', 'success');
            updateAdminPanel();
        } else {
            showToast(res?.error || 'Vacuum failed', 'error');
        }
    });
}

async function adminClearCache() {
    showConfirmModal('Clear Cache', 'This will clear all cached data in memory. Continue?', async () => {
        const res = await apiFetch('/api/admin/clear-cache', { method: 'POST' });
        if (res && res.ok) {
            showToast(res.message || 'Cache cleared successfully', 'success');
        } else {
            showToast(res?.error || 'Cache clear failed', 'error');
        }
    });
}

async function adminBackupDB() {
    showConfirmModal('Backup Database', 'Create a backup copy of the database now?', async () => {
        const res = await apiFetch('/api/admin/backup-db', { method: 'POST' });
        if (res && res.ok) {
            showToast(res.message || 'Backup created', 'success');
            updateAdminPanel();
        } else {
            showToast(res?.error || 'Backup failed', 'error');
        }
    });
}

async function adminRotateLogs() {
    showConfirmModal('Rotate Logs', 'This will archive the current log file and start fresh. Continue?', async () => {
        const res = await apiFetch('/api/admin/rotate-logs', { method: 'POST' });
        if (res && res.ok) {
            showToast(res.message || 'Logs rotated', 'success');
            updateAdminPanel();
        } else {
            showToast(res?.error || 'Log rotation failed', 'error');
        }
    });
}

async function adminResetMetrics() {
    showConfirmModal('Reset Metrics', 'This will reset all in-memory counters, gauges, and histograms. Continue?', async () => {
        const res = await apiFetch('/api/admin/reset-metrics', { method: 'POST' });
        if (res && res.ok) {
            showToast(res.message || 'Metrics reset', 'success');
            updateAdminPanel();
        } else {
            showToast(res?.error || 'Metrics reset failed', 'error');
        }
    });
}

async function adminTestAlert() {
    showConfirmModal('Test Alert', 'Send a test alert through all configured channels (Telegram, Discord, Slack)?', async () => {
        const res = await apiFetch('/api/admin/test-alert', { method: 'POST' });
        if (res && res.ok) {
            showToast(res.message || 'Test alert sent', 'success');
        } else {
            showToast(res?.error || 'Test alert failed', 'error');
        }
    });
}

async function adminPurgeOld() {
    showConfirmModal('Purge Old Data', 'This will permanently delete candidates, alerts, and model logs older than 30 days. This cannot be undone!', async () => {
        const res = await apiFetch('/api/admin/purge-old', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ days: 30 }),
        });
        if (res && res.ok) {
            showToast(res.message || `Purged ${res.total_deleted || 0} records`, 'success');
            updateAdminPanel();
        } else {
            showToast(res?.error || 'Purge failed', 'error');
        }
    });
}

async function adminExportTable(tableName) {
    try {
        const data = await apiFetch(`/api/admin/export/${tableName}`);
        if (!data || !data.rows) {
            showToast('No data to export', 'error');
            return;
        }
        // Convert to CSV
        const rows = data.rows;
        if (rows.length === 0) {
            showToast('Table is empty', 'error');
            return;
        }
        const headers = Object.keys(rows[0]);
        const csvLines = [headers.join(',')];
        rows.forEach(row => {
            csvLines.push(headers.map(h => {
                let v = row[h];
                if (v === null || v === undefined) v = '';
                v = String(v).replace(/"/g, '""');
                return `"${v}"`;
            }).join(','));
        });
        const csv = csvLines.join('\n');
        // Download
        const blob = new Blob([csv], { type: 'text/csv' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${tableName}_export_${new Date().toISOString().slice(0,10)}.csv`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast(`Exported ${rows.length} rows from ${tableName}`, 'success');
    } catch (e) {
        showToast('Export failed: ' + e.message, 'error');
    }
}


// ═══════════════════════════════════════════════════════════════
//  THEME TOGGLE
// ═══════════════════════════════════════════════════════════════

function toggleTheme() {
    const html = document.documentElement;
    const current = html.getAttribute('data-theme') || 'dark';
    const next = current === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
}

// Restore theme on load
(function() {
    const saved = localStorage.getItem('theme') || 'dark';
    document.documentElement.setAttribute('data-theme', saved);
})();


// ═══════════════════════════════════════════════════════════════
//  VaR / WATCHLIST / JOURNAL / EQUITY SNAPSHOTS
// ═══════════════════════════════════════════════════════════════

let _equitySnapshotChart = null;

async function updateVaR() {
    try {
        const res = await authFetch('/api/var');
        const data = await res.json();
        if (data.current) {
            const c = data.current;
            safeText($('#var-95'), `$${(c.daily_var_95 || 0).toFixed(2)}`);
            safeText($('#var-99'), `$${(c.daily_var_99 || 0).toFixed(2)}`);
            safeText($('#var-expected-loss'), `$${(c.mean_expected_loss || 0).toFixed(2)}`);
            safeText($('#var-positions'), String(c.num_positions || 0));
        }
    } catch (e) {
        console.warn('VaR update failed:', e);
    }
}

async function updateWatchlist() {
    try {
        const res = await authFetch('/api/watchlist');
        const data = await res.json();
        const body = $('#watchlist-body');
        if (!body) return;

        if (!data.items || data.items.length === 0) {
            safeHTML(body, '<tr><td colspan="5" style="text-align:center;color:var(--text-muted)">No watchlist items</td></tr>');
            return;
        }

        let html = '';
        for (const item of data.items) {
            html += `<tr>
                <td title="${item.market_id}">${(item.question || item.market_id || '').substring(0, 60)}</td>
                <td>${item.category || '—'}</td>
                <td>${item.added_at ? new Date(item.added_at).toLocaleDateString() : '—'}</td>
                <td>${item.notes || ''}</td>
                <td><button class="btn-remove" onclick="removeFromWatchlist('${item.market_id}')">✕</button></td>
            </tr>`;
        }
        safeHTML(body, html);
    } catch (e) {
        console.warn('Watchlist update failed:', e);
    }
}

async function addToWatchlist() {
    const marketId = $('#watchlist-market-id')?.value?.trim();
    const question = $('#watchlist-question')?.value?.trim();
    if (!marketId) {
        showToast('Please enter a Market ID', 'error');
        return;
    }
    try {
        const res = await authFetch('/api/watchlist', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ market_id: marketId, question: question || marketId }),
        });
        if (res.ok) {
            showToast('Added to watchlist', 'success');
            $('#watchlist-market-id').value = '';
            $('#watchlist-question').value = '';
            updateWatchlist();
        }
    } catch (e) {
        showToast('Failed to add: ' + e.message, 'error');
    }
}

async function removeFromWatchlist(marketId) {
    try {
        await authFetch('/api/watchlist/' + encodeURIComponent(marketId), { method: 'DELETE' });
        showToast('Removed from watchlist', 'success');
        updateWatchlist();
    } catch (e) {
        showToast('Failed to remove: ' + e.message, 'error');
    }
}

async function updateJournal() {
    try {
        const res = await authFetch('/api/journal?limit=50');
        const data = await res.json();
        const container = $('#journal-entries');
        if (!container) return;

        if (!data.entries || data.entries.length === 0) {
            safeHTML(container, '<p style="text-align:center;color:var(--text-muted);padding:24px 0;">No journal entries yet. Entries are auto-created when trades close.</p>');
            return;
        }

        let html = '';
        for (const entry of data.entries) {
            const pnl = parseFloat(entry.pnl || 0);
            const pnlClass = pnl >= 0 ? 'journal-pnl-positive' : 'journal-pnl-negative';
            const pnlStr = pnl >= 0 ? `+$${pnl.toFixed(2)}` : `-$${Math.abs(pnl).toFixed(2)}`;

            html += `<div class="journal-entry">
                <div class="journal-entry-header">
                    <strong style="font-size:0.88rem;">${(entry.question || entry.market_id || '—').substring(0, 80)}</strong>
                    <span class="${pnlClass}">${pnlStr}</span>
                </div>
                <div class="journal-meta">
                    <span>📌 ${entry.direction || '—'}</span>
                    <span>Entry: ${(entry.entry_price || 0).toFixed(3)}</span>
                    <span>Exit: ${(entry.exit_price || 0).toFixed(3)}</span>
                    <span>Stake: $${(entry.stake_usd || 0).toFixed(2)}</span>
                    <span>📅 ${entry.created_at ? new Date(entry.created_at).toLocaleDateString() : '—'}</span>
                </div>
                ${entry.reasoning ? `<div class="journal-annotation">🤖 <strong>AI Reasoning:</strong> ${entry.reasoning}</div>` : ''}
                ${entry.annotation ? `<div class="journal-annotation" style="border-left-color:var(--accent-teal);">📝 ${entry.annotation}</div>` : ''}
                ${entry.lessons_learned ? `<div class="journal-annotation" style="border-left-color:var(--accent-yellow);">💡 <strong>Lesson:</strong> ${entry.lessons_learned}</div>` : ''}
            </div>`;
        }
        safeHTML(container, html);
    } catch (e) {
        console.warn('Journal update failed:', e);
    }
}

async function updateEquitySnapshots() {
    try {
        const res = await authFetch('/api/equity-snapshots?limit=500');
        const data = await res.json();
        if (!data.snapshots || data.snapshots.length === 0) return;

        const canvas = document.getElementById('equity-snapshots-chart');
        if (!canvas) return;

        const labels = data.snapshots.map(s => {
            const d = new Date(s.timestamp);
            return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        });
        const equityData = data.snapshots.map(s => s.equity);
        const drawdownData = data.snapshots.map(s => (s.drawdown_pct || 0) * -100);

        if (_equitySnapshotChart) _equitySnapshotChart.destroy();

        _equitySnapshotChart = new Chart(canvas, {
            type: 'line',
            data: {
                labels,
                datasets: [
                    {
                        label: 'Equity ($)',
                        data: equityData,
                        borderColor: '#4c8dff',
                        backgroundColor: 'rgba(76,141,255,0.1)',
                        borderWidth: 2,
                        fill: true,
                        tension: 0.3,
                        yAxisID: 'y',
                    },
                    {
                        label: 'Drawdown (%)',
                        data: drawdownData,
                        borderColor: '#ff4d6a',
                        backgroundColor: 'rgba(255,77,106,0.05)',
                        borderWidth: 1.5,
                        fill: true,
                        tension: 0.3,
                        yAxisID: 'y1',
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { intersect: false, mode: 'index' },
                plugins: {
                    legend: { labels: { color: '#9499b3', font: { size: 11 } } },
                },
                scales: {
                    x: { ticks: { color: '#5a5f78', maxTicksLimit: 12 }, grid: { color: 'rgba(255,255,255,0.04)' } },
                    y: { position: 'left', ticks: { color: '#4c8dff' }, grid: { color: 'rgba(255,255,255,0.04)' } },
                    y1: { position: 'right', ticks: { color: '#ff4d6a', callback: v => v + '%' }, grid: { display: false } },
                },
            },
        });
    } catch (e) {
        console.warn('Equity snapshots update failed:', e);
    }
}


// ═══════════════════════════════════════════════════════════════
//  REFRESH ORCHESTRATION
// ═══════════════════════════════════════════════════════════════

// Map of function names to actual functions
const _updaterFns = {
    updatePortfolio, updateRisk, updatePositions, updateCandidates,
    updateDecisionLog, updateForecasts, updateTrades, updateCharts,
    updateEquityCurve, updateEngineStatus, updateDrawdown, updateAudit,
    updateAlerts, updateConfig, updateAnalytics, updateRegime, updateWhaleTracker,
    updateAdminPanel, updateVaR, updateWatchlist, updateJournal, updateEquitySnapshots,
    updateSettingsTab,
};

async function refreshAll() {
    const scrollY = window.scrollY;

    // Always update portfolio (header badges) and engine status
    const alwaysUpdate = ['updatePortfolio', 'updateEngineStatus'];

    // Get updaters for the active tab
    const tabUpdaters = TAB_UPDATERS[_activeTab] || [];

    // Merge, deduplicate
    const toRun = [...new Set([...alwaysUpdate, ...tabUpdaters])];

    await Promise.all(toRun.map(name => {
        const fn = _updaterFns[name];
        return fn ? fn() : Promise.resolve();
    }));

    $('#last-updated-time').textContent = new Date().toLocaleTimeString();
    requestAnimationFrame(() => window.scrollTo(0, scrollY));
}

// Full refresh - runs ALL updaters (used on first load)
async function refreshFull() {
    const scrollY = window.scrollY;

    await Promise.all(Object.values(_updaterFns).map(fn => fn()));

    $('#last-updated-time').textContent = new Date().toLocaleTimeString();
    requestAnimationFrame(() => window.scrollTo(0, scrollY));
}

// ─── Init ───────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    // Restore saved tab
    switchTab(_activeTab);

    // Add loading state to cards
    $$('.card-value').forEach(el => {
        if (!el.textContent.trim() || el.textContent.trim() === '—') {
            el.style.opacity = '0.5';
        }
    });

    // Full refresh on first load (populate all tabs)
    refreshFull().then(() => {
        // Remove loading state
        $$('.card-value').forEach(el => {
            el.style.opacity = '';
            el.style.transition = 'opacity 0.3s ease';
        });
    });

    // Smart refresh every 15s (only active tab)
    setInterval(refreshAll, 15000);

    // Add hover glow effect to cards
    $$('.card').forEach(card => {
        card.addEventListener('mousemove', (e) => {
            const rect = card.getBoundingClientRect();
            const x = ((e.clientX - rect.left) / rect.width) * 100;
            const y = ((e.clientY - rect.top) / rect.height) * 100;
            card.style.background = `radial-gradient(circle at ${x}% ${y}%, rgba(255,255,255,0.03), transparent 60%), rgba(26,29,39,0.6)`;
        });
        card.addEventListener('mouseleave', () => {
            card.style.background = '';
        });
    });
});

// ─── Documentation Tab Helpers ──────────────────────────────────
function scrollToDoc(event, id) {
    event.preventDefault();
    const el = document.getElementById(id);
    if (!el) return;
    const content = document.getElementById('docs-content');
    if (content) {
        content.scrollTo({ top: el.offsetTop - content.offsetTop - 24, behavior: 'smooth' });
    }
    // Update active nav link
    $$('.docs-nav-link').forEach(l => l.classList.remove('active'));
    event.currentTarget.classList.add('active');
}

function filterDocs(query) {
    const q = query.toLowerCase().trim();
    $$('.docs-section').forEach(sec => {
        if (!q) { sec.style.display = ''; return; }
        const text = sec.textContent.toLowerCase();
        sec.style.display = text.includes(q) ? '' : 'none';
    });
    // Also update nav links
    $$('.docs-nav-link').forEach(link => {
        const targetId = link.getAttribute('href').replace('#', '');
        const sec = document.getElementById(targetId);
        if (!sec) return;
        link.style.display = (!q || sec.style.display !== 'none') ? '' : 'none';
    });
}

// Highlight active docs section on scroll
(function initDocsScrollSpy() {
    const content = document.getElementById('docs-content');
    if (!content) return;
    content.addEventListener('scroll', function() {
        const sections = content.querySelectorAll('.docs-section');
        let current = '';
        sections.forEach(sec => {
            const top = sec.offsetTop - content.offsetTop - 80;
            if (content.scrollTop >= top) current = sec.id;
        });
        if (current) {
            $$('.docs-nav-link').forEach(l => {
                l.classList.toggle('active', l.getAttribute('href') === '#' + current);
            });
        }
    });
})();
