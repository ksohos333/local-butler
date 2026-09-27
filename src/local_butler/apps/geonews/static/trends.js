// Geonews — trend analyzer frontend.
// Self-contained SVG charts; no external chart library required.

const TRENDS_DAYS = [7, 14, 30];
let currentTrendDays = 7;
let cachedTrendData = null;

function renderTrendDays(container, onChange) {
    container.innerHTML = '';
    TRENDS_DAYS.forEach(days => {
        const btn = document.createElement('button');
        btn.textContent = `${days}d`;
        btn.className = days === currentTrendDays ? 'active' : '';
        btn.addEventListener('click', () => {
            currentTrendDays = days;
            onChange(days);
        });
        container.appendChild(btn);
    });
}

function svgEl(tag, attrs = {}) {
    const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs)) {
        el.setAttribute(k, v);
    }
    return el;
}

function simpleLineChart(data, width, height) {
    if (!data.length) {
        return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><text x="10" y="20" fill="#94a3b8" font-size="12">No data</text></svg>`;
    }
    const padding = { top: 10, right: 10, bottom: 24, left: 30 };
    const chartW = width - padding.left - padding.right;
    const chartH = height - padding.top - padding.bottom;
    const counts = data.map(d => d.count);
    const maxCount = Math.max(1, ...counts);
    const dates = data.map(d => d.date.slice(5)); // mm-dd

    // Grid lines
    let gridLines = '';
    const steps = 4;
    for (let i = 0; i <= steps; i++) {
        const y = padding.top + chartH - (i / steps) * chartH;
        gridLines += `<line x1="${padding.left}" y1="${y}" x2="${width - padding.right}" y2="${y}" stroke="#334155" stroke-width="1"/>`;
        gridLines += `<text x="${padding.left - 6}" y="${y + 3}" fill="#64748b" font-size="9" text-anchor="end">${Math.round(maxCount * i / steps)}</text>`;
    }

    // Area path
    const n = data.length;
    const dx = n > 1 ? chartW / (n - 1) : 0;
    let pathD = '';
    let areaD = '';
    data.forEach((pt, i) => {
        const x = padding.left + (n === 1 ? chartW / 2 : i * dx);
        const y = padding.top + chartH - (pt.count / maxCount) * chartH;
        if (i === 0) {
            pathD += `M ${x} ${y}`;
            areaD += `M ${x} ${padding.top + chartH} L ${x} ${y}`;
        } else {
            pathD += ` L ${x} ${y}`;
            areaD += ` L ${x} ${y}`;
        }
    });
    if (n) {
        const lastX = padding.left + (n === 1 ? chartW / 2 : (n - 1) * dx);
        areaD += ` L ${lastX} ${padding.top + chartH} Z`;
    }

    let dots = '';
    data.forEach((pt, i) => {
        const x = padding.left + (n === 1 ? chartW / 2 : i * dx);
        const y = padding.top + chartH - (pt.count / maxCount) * chartH;
        dots += `<circle cx="${x}" cy="${y}" r="3" fill="#38bdf8"/>`;
        // show every other label
        if (i % 2 === 0 || n <= 6) {
            dots += `<text x="${x}" y="${height - 6}" fill="#64748b" font-size="9" text-anchor="middle">${dates[i]}</text>`;
        }
    });

    return `
        <svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" style="width:100%;height:100%;">
            ${gridLines}
            <path d="${areaD}" fill="rgba(56,189,248,0.12)" stroke="none"/>
            <path d="${pathD}" fill="none" stroke="#38bdf8" stroke-width="2"/>
            ${dots}
        </svg>
    `;
}

function renderTrendStats(container, data) {
    const delta = data.total - data.previous_total;
    const deltaPct = data.previous_total > 0
        ? Math.round((delta / data.previous_total) * 100)
        : (data.total > 0 ? 100 : 0);
    const cls = delta >= 0 ? 'up' : 'down';
    const sign = delta >= 0 ? '+' : '';
    container.innerHTML = `
        <div class="trend-hero">
            <span class="trend-big">${data.total.toLocaleString()}</span>
            <span class="trend-delta ${cls}">${sign}${delta} articles · ${sign}${deltaPct}% vs previous ${data.days} days</span>
        </div>
    `;
}

function renderTypeDistribution(container, typeData) {
    if (!typeData.length) {
        container.innerHTML = '<div class="trend-subtitle">No event type data</div>';
        return;
    }
    const maxVal = Math.max(1, ...typeData.map(t => t.current));
    const rows = typeData.map(t => {
        const pct = Math.round((t.current / maxVal) * 100);
        const color = colorForEventType(t.event_type);
        const deltaSign = t.delta >= 0 ? '+' : '';
        const deltaCls = t.delta >= 0 ? 'up' : 'down';
        return `
            <div class="trend-row">
                <span class="trend-name"><span class="dot" style="background:${color}"></span>${t.event_type}</span>
                <div class="trend-bar-bg"><div class="trend-bar-fill" style="width:${pct}%;background:${color}"></div></div>
                <strong>${t.current}</strong>
                <span class="trend-delta ${deltaCls}">${deltaSign}${t.delta}</span>
            </div>
        `;
    }).join('');
    container.innerHTML = rows;
}

function renderRegionTrends(container, regionData) {
    if (!regionData.length) {
        container.innerHTML = '<div class="trend-subtitle">No regional data</div>';
        return;
    }
    const maxVal = Math.max(1, ...regionData.map(r => r.current));
    const rows = regionData.slice(0, 12).map(r => {
        const pct = Math.round((r.current / maxVal) * 100);
        const deltaSign = r.delta >= 0 ? '+' : '';
        const deltaCls = r.delta >= 0 ? 'up' : 'down';
        return `
            <div class="trend-row">
                <span class="trend-name">${escapeHtml(r.region || 'Unknown')}</span>
                <div class="trend-bar-bg"><div class="trend-bar-fill" style="width:${pct}%;background:#38bdf8"></div></div>
                <strong>${r.current}</strong>
                <span class="trend-delta ${deltaCls}">${deltaSign}${r.delta}</span>
            </div>
        `;
    }).join('');
    container.innerHTML = rows;
}

function renderHotspots(container, hotspots) {
    if (!hotspots.length) {
        container.innerHTML = '<div class="trend-subtitle">No mapped hotspots</div>';
        return;
    }
    container.innerHTML = hotspots.slice(0, 25).map(h => `
        <div class="trend-row" style="cursor:pointer;" data-hotspot-id="${h.id}">
            <span class="trend-name"><span class="dot" style="background:${colorForEventType(h.event_type)}"></span>${escapeHtml(h.title)}</span>
            <span style="font-size:11px;color:#94a3b8;">${h.severity || '—'} · ${h.location_name || ''}</span>
        </div>
    `).join('');
    container.querySelectorAll('[data-hotspot-id]').forEach(row => {
        row.addEventListener('click', () => {
            const id = parseInt(row.dataset.hotspotId);
            showTab('feed');
            openArticleDetail(id);
        });
    });
}

async function loadTrends(days = currentTrendDays) {
    currentTrendDays = days;
    const container = document.getElementById('trends');
    container.innerHTML = '<div style="padding:20px;color:#94a3b8;text-align:center">Loading trend analysis…</div>';
    try {
        const res = await fetch(`/api/trends?days=${days}`);
        const data = await res.json();
        cachedTrendData = data;
        renderTrendsPanel(container, data);
    } catch (err) {
        console.error('Trends failed:', err);
        container.innerHTML = '<div style="padding:20px;color:#ef4444;text-align:center">Failed to load trends.</div>';
    }
}

function renderTrendsPanel(container, data) {
    container.innerHTML = '';

    const controls = document.createElement('div');
    controls.className = 'trend-days';
    renderTrendDays(controls, days => loadTrends(days));
    container.appendChild(controls);

    const hero = document.createElement('div');
    hero.className = 'trend-section';
    hero.innerHTML = '<h4>Volume trend</h4>';
    const stats = document.createElement('div');
    renderTrendStats(stats, data);
    hero.appendChild(stats);
    const chartWrap = document.createElement('div');
    chartWrap.className = 'trend-chart';
    chartWrap.innerHTML = simpleLineChart(data.time_series, 400, 140);
    hero.appendChild(chartWrap);
    container.appendChild(hero);

    const types = document.createElement('div');
    types.className = 'trend-section';
    types.innerHTML = '<h4>By event type</h4>';
    const typeList = document.createElement('div');
    renderTypeDistribution(typeList, data.type_distribution);
    types.appendChild(typeList);
    container.appendChild(types);

    const regions = document.createElement('div');
    regions.className = 'trend-section';
    regions.innerHTML = '<h4>By region</h4>';
    const regionList = document.createElement('div');
    renderRegionTrends(regionList, data.region_trends);
    regions.appendChild(regionList);
    container.appendChild(regions);

    const hotspotSection = document.createElement('div');
    hotspotSection.className = 'trend-section';
    hotspotSection.innerHTML = '<h4>Recent hotspots</h4><div class="trend-subtitle">Click any item to open it on the map.</div>';
    const hotspotList = document.createElement('div');
    renderHotspots(hotspotList, data.hotspots);
    hotspotSection.appendChild(hotspotList);
    container.appendChild(hotspotSection);
}

// Global alias used by app.js when the Trends tab is selected.
window.loadTrends = loadTrends;
