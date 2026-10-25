// Geonews frontend: feed + interactive Leaflet map + prediction markets.

const TYPE_COLORS = {
    conflict: '#ef4444',
    economic: '#eab308',
    political: '#3b82f6',
    environmental: '#22c55e',
    other: '#a855f7',
};

const TYPE_LABELS = {
    conflict: 'Conflict',
    economic: 'Economic',
    political: 'Political',
    environmental: 'Environmental',
    other: 'Other',
};

let map;
let markerCluster;
let markersById = {};
let currentArticles = [];
let markerArticleIds = [];
let currentDetailArticleId = null;
let feedFocusIndex = -1;
let marketFocusIndex = -1;
let hotspotFocusIndex = -1;
let menuFocusIndex = -1;
let mapNodeIndex = 0;
let currentBbox = null;
let currentTab = 'feed';
let activeTypes = new Set(Object.keys(TYPE_LABELS));
let searchDebounce = null;

function initMap() {
    map = L.map('map', {
        center: [20, 0],
        zoom: 2,
        minZoom: 2,
        maxZoom: 16,
        worldCopyJump: true,
    });

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; OpenStreetMap contributors',
        maxZoom: 19,
    }).addTo(map);

    markerCluster = L.markerClusterGroup({
        maxClusterRadius: 60,
        spiderfyOnMaxZoom: true,
        showCoverageOnHover: false,
    });
    map.addLayer(markerCluster);

    map.on('moveend zoomend', () => {
        currentBbox = map.getBounds().toBBoxString();
        loadMapMarkers();
    });

    map.on('click', handleMapClick);

    currentBbox = map.getBounds().toBBoxString();
}

function colorForEventType(type) {
    return TYPE_COLORS[type] || TYPE_COLORS.other;
}

function formatDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
    });
}

function formatRelativeTime(iso) {
    if (!iso) return 'never';
    const then = new Date(iso).getTime();
    const now = Date.now();
    const seconds = Math.floor((now - then) / 1000);
    if (seconds < 60) return 'just now';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
}

function createPopupContent(props) {
    return `
        <div style="min-width: 180px;">
            <div style="font-size: 13px; font-weight: 600; margin-bottom: 4px;">${escapeHtml(props.title)}</div>
            <div style="font-size: 11px; color: #64748b;">
                ${TYPE_LABELS[props.event_type] || 'Other'} · ${props.location_name || 'Unknown location'}<br>
                ${props.source_name}
            </div>
        </div>
    `;
}

function setFeedStatus(message) {
    document.getElementById('feed-status').textContent = message;
}

function renderFeed(articles) {
    currentArticles = articles;
    mapNodeIndex = 0;
    feedFocusIndex = -1;
    const feed = document.getElementById('feed');
    feed.innerHTML = '';

    if (articles.length === 0) {
        feed.innerHTML = '<div style="padding: 20px; text-align: center; color: #94a3b8;">No articles found. Try a different search or wait for ingestion.</div>';
        setFeedStatus(`0 articles`);
        return;
    }

    setFeedStatus(`${articles.length} articles shown`);

    articles.forEach(article => {
        const card = document.createElement('div');
        card.className = 'article-card';
        card.dataset.id = article.id;
        const color = colorForEventType(article.event_type);

        card.innerHTML = `
            <div class="article-header">
                <span class="dot" style="background: ${color}"></span>
                <h3 class="article-title">${escapeHtml(article.title)}</h3>
            </div>
            <div class="article-meta">
                <span>${escapeHtml(article.source_name)}</span>
                <span>·</span>
                <span>${TYPE_LABELS[article.event_type] || 'Other'}</span>
                ${article.location_name ? `<span>·</span><span>📍 ${escapeHtml(article.location_name)}</span>` : ''}
                ${article.published_at ? `<span>·</span><span>${formatDate(article.published_at)}</span>` : ''}
            </div>
            ${article.summary ? `<div class="article-summary">${escapeHtml(article.summary)}</div>` : ''}
            <div class="article-actions">
                ${article.url ? `<a href="${escapeHtml(article.url)}" target="_blank" rel="noopener">Open source ↗</a>` : ''}
                ${article.latitude ? `<a href="#" class="show-on-map" data-id="${article.id}">Show on map</a>` : ''}
            </div>
        `;

        card.addEventListener('click', (e) => {
            if (e.target.tagName === 'A') return;
            openArticleDetail(article.id);
        });

        card.querySelector('.show-on-map')?.addEventListener('click', (e) => {
            e.preventDefault();
            showOnMap(article.id);
        });

        feed.appendChild(card);
    });
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function highlightArticle(id) {
    document.querySelectorAll('.article-card').forEach(c => c.classList.remove('active'));
    const card = document.querySelector(`.article-card[data-id="${id}"]`);
    if (card) {
        card.classList.add('active');
        card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
}

function showOnMap(id) {
    const marker = markersById[id];
    if (!marker || !map) return;
    const latLng = marker.getLatLng();
    map.setView(latLng, 10, { animate: true });
    marker.openPopup();
    highlightArticle(id);
}

async function fetchArticleSummary(id) {
    const bodyEl = document.getElementById('detail-body');
    bodyEl.textContent = 'Generating local summary…';
    try {
        const res = await fetch(`/api/summarize/${id}`, { method: 'POST' });
        if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
        const data = await res.json();
        bodyEl.textContent = data.summary || 'No summary generated.';
    } catch (err) {
        console.error('Summary failed:', err);
        const article = currentArticles.find(a => a.id == id);
        bodyEl.textContent = (article && (article.body || article.summary)) || 'Summary unavailable.';
    }
}


function getSearchQuery() {
    return document.getElementById('search-input').value.trim();
}

function getRegionFilter() {
    return document.getElementById('filter-region').value;
}

async function loadFeed() {
    const params = new URLSearchParams({ page_size: '50' });
    const selectedType = getSingleSelectedType();
    const region = getRegionFilter();
    const search = getSearchQuery();

    if (selectedType) params.set('event_type', selectedType);
    if (region) params.set('region', region);
    if (search) params.set('search', search);

    try {
        setFeedStatus('Loading…');
        const res = await fetch(`/api/events?${params}`);
        const data = await res.json();
        renderFeed(data.items || []);
    } catch (err) {
        console.error('Failed to load feed:', err);
        setFeedStatus('Failed to load feed');
    }
}

async function loadMapMarkers() {
    const params = new URLSearchParams();
    const selectedType = getSingleSelectedType();
    const search = getSearchQuery();

    if (currentBbox) params.set('bbox', currentBbox);
    if (selectedType) params.set('event_type', selectedType);
    if (search) params.set('search', search);

    try {
        const res = await fetch(`/api/events/geo?${params}`);
        const data = await res.json();
        updateMarkers(data.features || []);
    } catch (err) {
        console.error('Failed to load map markers:', err);
    }
}

function updateMarkers(features) {
    markerCluster.clearLayers();
    markersById = {};
    markerArticleIds = features.map(f => f.properties.id);

    features.forEach(feature => {
        const props = feature.properties;
        const [lon, lat] = feature.geometry.coordinates;
        const color = colorForEventType(props.event_type);

        const marker = L.circleMarker([lat, lon], {
            radius: 6 + Math.min((props.severity || 5) / 2, 6),
            fillColor: color,
            color: '#fff',
            weight: 1,
            opacity: 1,
            fillOpacity: 0.85,
        });
        marker.feature = feature;

        marker.bindPopup(createPopupContent(props));
        marker.on('click', async () => {
            highlightArticle(props.id);
            await openArticleDetail(props.id);
        });

        markerCluster.addLayer(marker);
        markersById[props.id] = marker;
    });
}

async function loadStats() {
    try {
        const [statsRes, healthRes] = await Promise.all([
            fetch('/api/stats'),
            fetch('/api/source-health'),
        ]);
        const data = await statsRes.json();
        const health = await healthRes.json();

        const typeBreakdown = Object.entries(data.event_types || {})
            .map(([k, v]) => `${TYPE_LABELS[k] || k}: ${v}`)
            .join(' · ');
        document.getElementById('footer-stats').textContent =
            `${data.total_articles} articles · ${data.primary_sources} primary sources · ${typeBreakdown}`;

        const latest = health.sources && health.sources[0] && health.sources[0].last_run;
        document.getElementById('footer-freshness').textContent =
            `Last refresh: ${formatRelativeTime(latest)}`;
    } catch (err) {
        console.error('Failed to load stats:', err);
    }
}

async function triggerIngest() {
    try {
        await fetch('/api/ingest', { method: 'POST' });
        document.getElementById('status-pill').innerHTML =
            '<span class="pulse"></span> Refreshing…';
        setTimeout(() => {
            document.getElementById('status-pill').innerHTML =
                '<span class="pulse"></span> Live';
        }, 3000);
    } catch (err) {
        console.error('Failed to trigger ingestion:', err);
    }
}

async function triggerProcess() {
    try {
        const res = await fetch('/api/process', { method: 'POST' });
        const data = await res.json();
        document.getElementById('status-pill').innerHTML =
            `<span class="pulse"></span> ${data.message}`;
        setTimeout(refreshAll, 1000);
        setTimeout(() => {
            document.getElementById('status-pill').innerHTML =
                '<span class="pulse"></span> Live';
        }, 4000);
    } catch (err) {
        console.error('Failed to trigger processing:', err);
    }
}

function populateRegionFilter() {
    const regions = new Set(currentArticles.map(a => a.source_region).filter(Boolean));
    const select = document.getElementById('filter-region');
    const currentValue = select.value;
    select.innerHTML = '<option value="">All regions</option>';
    Array.from(regions).sort().forEach(region => {
        const option = document.createElement('option');
        option.value = region;
        option.textContent = region;
        select.appendChild(option);
    });
    select.value = currentValue;
}

function renderLayerChips() {
    document.querySelectorAll('.layer-chip').forEach(chip => {
        const type = chip.dataset.type;
        chip.classList.toggle('active', activeTypes.has(type));
    });
}

function getSingleSelectedType() {
    if (activeTypes.size === Object.keys(TYPE_LABELS).length) return '';
    if (activeTypes.size === 1) return Array.from(activeTypes)[0];
    return '';
}

function updateActiveTypeChip() {
    const type = getSingleSelectedType();
    activeTypes.clear();
    if (type) {
        activeTypes.add(type);
    } else {
        Object.keys(TYPE_LABELS).forEach(t => activeTypes.add(t));
    }
    renderLayerChips();
}

function handleMapClick(e) {
    if (!e.originalEvent || e.originalEvent.defaultPrevented) return;
    if (e.originalEvent.target.closest('.leaflet-popup')) return;

    const { lat, lng } = e.latlng;
    const tol = 1.5;
    const nearby = currentArticles.filter(a =>
        a.latitude && a.longitude &&
        Math.abs(a.latitude - lat) < tol &&
        Math.abs(a.longitude - lng) < tol
    );

    if (nearby.length > 0) {
        renderFeed(nearby);
        setFeedStatus(`${nearby.length} articles near clicked location`);
    } else {
        const toast = document.getElementById('map-toast');
        toast.textContent = `No mapped articles near ${lat.toFixed(2)}, ${lng.toFixed(2)}. Pan/zoom to explore.`;
        toast.style.display = 'block';
        setTimeout(() => { toast.style.display = 'none'; }, 3000);
    }
}

async function loadArticleById(id) {
    let article = currentArticles.find(a => a.id == id);
    if (article) return article;
    try {
        const res = await fetch(`/api/events/${id}`);
        if (res.ok) article = await res.json();
    } catch (err) {
        console.error('Failed to fetch article by id:', err);
    }
    return article;
}

async function openArticleDetail(id) {
    const article = await loadArticleById(id);
    if (!article) return;

    currentDetailArticleId = article.id;

    document.getElementById('detail-title').textContent = article.title;
    document.getElementById('detail-meta').innerHTML = `
        <span class="dot" style="display:inline-block;vertical-align:middle;background:${colorForEventType(article.event_type)}"></span>
        ${TYPE_LABELS[article.event_type] || 'Other'}
        ${article.source_name ? ` · ${escapeHtml(article.source_name)}` : ''}
        ${article.location_name ? ` · 📍 ${escapeHtml(article.location_name)}` : ''}
        ${article.published_at ? ` · ${formatDate(article.published_at)}` : ''}
    `;
    document.getElementById('detail-body').textContent = 'Generating local summary…';

    const actions = document.getElementById('detail-actions');
    actions.innerHTML = '';
    if (article.url) {
        const link = document.createElement('a');
        link.href = article.url;
        link.target = '_blank';
        link.rel = 'noopener';
        link.textContent = 'Open primary source ↗';
        actions.appendChild(link);
    }
    if (article.latitude && article.longitude) {
        const btn = document.createElement('button');
        btn.textContent = 'Zoom on map';
        btn.addEventListener('click', () => {
            closeArticleDetail();
            showOnMap(article.id);
        });
        actions.appendChild(btn);
    }

    document.getElementById('detail-modal').classList.add('visible');

    // Center the map while the modal is open and then request a local LLM summary.
    if (article.latitude && article.longitude) {
        showOnMap(article.id);
    }
    fetchArticleSummary(article.id);
}

function closeArticleDetail() {
    document.getElementById('detail-modal').classList.remove('visible');
}

// ─────────────────────────────────────────────────────────────
// KEYBOARD + MAP-NODE NAVIGATION
// ─────────────────────────────────────────────────────────────

function clearFocus() {
    document.querySelectorAll('.article-card.focused, .market-card.focused, [data-hotspot-id].focused, .menu-item.focused').forEach(el => el.classList.remove('focused'));
}

function getFocusList() {
    const menuOpen = document.getElementById('menu-drawer')?.classList.contains('visible');
    if (menuOpen) return Array.from(document.querySelectorAll('#menu-drawer .menu-item'));
    if (currentTab === 'feed') return Array.from(document.querySelectorAll('#feed .article-card'));
    if (currentTab === 'markets') return Array.from(document.querySelectorAll('#markets .market-card'));
    if (currentTab === 'trends') return Array.from(document.querySelectorAll('#trends [data-hotspot-id]'));
    return [];
}

function focusItem(list, index) {
    clearFocus();
    const item = list[index];
    if (!item) return;
    item.classList.add('focused');
    item.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function cycleMapNode(direction) {
    const articles = currentArticles.filter(a => a.latitude != null && a.longitude != null);
    if (!articles.length) return;
    mapNodeIndex = (mapNodeIndex + direction + articles.length) % articles.length;
    openArticleDetail(articles[mapNodeIndex].id);
}

function handleKeydown(e) {
    const active = document.activeElement;
    const tag = active?.tagName;
    const typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || active?.isContentEditable;
    if (typing && e.key !== 'Escape') return;

    const modalOpen = document.getElementById('detail-modal')?.classList.contains('visible');
    if (e.key === 'Escape') {
        e.preventDefault();
        closeMenu();
        closeArticleDetail();
        return;
    }
    if (modalOpen) return;

    const menuOpen = document.getElementById('menu-drawer')?.classList.contains('visible');
    const items = getFocusList();

    if (menuOpen) {
        if (!items.length) return;
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            menuFocusIndex = menuFocusIndex < 0 ? 0 : (menuFocusIndex + 1) % items.length;
            focusItem(items, menuFocusIndex);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            menuFocusIndex = menuFocusIndex < 0 ? items.length - 1 : (menuFocusIndex - 1 + items.length) % items.length;
            focusItem(items, menuFocusIndex);
        } else if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            items[menuFocusIndex]?.click();
        }
        return;
    }

    if (currentTab === 'feed') {
        if (!items.length) return;
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            feedFocusIndex = feedFocusIndex < 0 ? 0 : (feedFocusIndex + 1) % items.length;
            focusItem(items, feedFocusIndex);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            feedFocusIndex = feedFocusIndex < 0 ? items.length - 1 : (feedFocusIndex - 1 + items.length) % items.length;
            focusItem(items, feedFocusIndex);
        } else if (e.key === 'Enter') {
            e.preventDefault();
            const id = items[feedFocusIndex]?.dataset.id;
            if (id) openArticleDetail(id);
        }
    } else if (currentTab === 'markets') {
        if (!items.length) return;
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            marketFocusIndex = marketFocusIndex < 0 ? 0 : (marketFocusIndex + 1) % items.length;
            focusItem(items, marketFocusIndex);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            marketFocusIndex = marketFocusIndex < 0 ? items.length - 1 : (marketFocusIndex - 1 + items.length) % items.length;
            focusItem(items, marketFocusIndex);
        } else if (e.key === 'Enter') {
            e.preventDefault();
            items[marketFocusIndex]?.querySelector('input')?.focus();
        }
    } else if (currentTab === 'trends') {
        if (!items.length) return;
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            hotspotFocusIndex = hotspotFocusIndex < 0 ? 0 : (hotspotFocusIndex + 1) % items.length;
            focusItem(items, hotspotFocusIndex);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            hotspotFocusIndex = hotspotFocusIndex < 0 ? items.length - 1 : (hotspotFocusIndex - 1 + items.length) % items.length;
            focusItem(items, hotspotFocusIndex);
        } else if (e.key === 'Enter') {
            e.preventDefault();
            const id = items[hotspotFocusIndex]?.dataset.hotspotId;
            if (id) {
                showTab('feed');
                openArticleDetail(parseInt(id));
            }
        }
    }
}

// ─────────────────────────────────────────────────────────────
// AGENT CHAT + LIGHTWEIGHT MARKDOWN FORMATTING
// ─────────────────────────────────────────────────────────────

function formatAgentMarkdown(raw) {
    if (!raw) return '';

    const codeBlocks = [];
    const inlineCodes = [];
    let idx = 0;

    // Triple-backtick code blocks (preserve content, don't process inside).
    let text = raw.replace(/```(\w*)\n?([\s\S]*?)\n?```/g, (match, lang, code) => {
        const placeholder = `\x00BLOCK${idx}\x00`;
        codeBlocks.push(`<pre${lang ? ` class="lang-${lang}"` : ''}><code>${escapeHtml(code)}</code></pre>`);
        idx += 1;
        return placeholder;
    });

    // Inline backtick code.
    text = text.replace(/`([^`\n]+)`/g, (match, code) => {
        const placeholder = `\x00INLINE${idx}\x00`;
        inlineCodes.push(`<code>${escapeHtml(code)}</code>`);
        idx += 1;
        return placeholder;
    });

    let html = escapeHtml(text);

    // Headers.
    html = html.replace(/^####\s+(.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^###\s+(.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^##\s+(.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/^#\s+(.+)$/gm, '<h1>$1</h1>');

    // Bold / italic (process bold first so ** wins over *).
    html = html.replace(/(?<!\*)\*\*([^*]+)\*\*(?!\*)/g, '<b>$1</b>');
    html = html.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, '<i>$1</i>');

    // Blockquote: after escaping, ">" becomes "&gt;".
    html = html.replace(/^&gt;\s+(.+)$/gm, '<blockquote>$1</blockquote>');

    // Lists: process line-by-line so consecutive list items group together.
    const lines = html.split('\n');
    const parts = [];
    let inUl = false;
    let inOl = false;

    function closeLists() {
        if (inUl) {
            parts.push('</ul>');
            inUl = false;
        }
        if (inOl) {
            parts.push('</ol>');
            inOl = false;
        }
    }

    for (const line of lines) {
        const ul = line.match(/^[-*]\s+(.+)$/);
        const ol = line.match(/^(\d+)\.\s+(.+)$/);
        if (ul) {
            if (!inUl) {
                closeLists();
                parts.push('<ul>');
                inUl = true;
            }
            parts.push(`<li>${ul[1]}</li>`);
        } else if (ol) {
            if (!inOl) {
                closeLists();
                parts.push('<ol>');
                inOl = true;
            }
            parts.push(`<li>${ol[2]}</li>`);
        } else if (line.trim() === '') {
            closeLists();
            parts.push('<br>');
        } else {
            closeLists();
            parts.push(line);
        }
    }
    closeLists();
    html = parts.join('\n');

    // Restore code placeholders last.
    for (let i = codeBlocks.length - 1; i >= 0; i--) {
        html = html.replace(`\x00BLOCK${i}\x00`, codeBlocks[i]);
    }
    for (let i = inlineCodes.length - 1; i >= 0; i--) {
        html = html.replace(`\x00INLINE${i}\x00`, inlineCodes[i]);
    }

    return html;
}

let agentLastMessage = null;

function appendAgentMessage(role, text) {
    const log = document.getElementById('agent-log');
    const div = document.createElement('div');
    div.className = `agent-message ${role}`;
    div.dataset.rawText = text;
    div.innerHTML = role === 'error' ? escapeHtml(text) : formatAgentMarkdown(text);
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    return div;
}

function appendAgentDelta(chunk) {
    if (!agentLastMessage) {
        agentLastMessage = appendAgentMessage('assistant', '');
    }
    const raw = (agentLastMessage.dataset.rawText || '') + chunk;
    agentLastMessage.dataset.rawText = raw;
    agentLastMessage.innerHTML = formatAgentMarkdown(raw);
    const log = document.getElementById('agent-log');
    log.scrollTop = log.scrollHeight;
}

// ─────────────────────────────────────────────────────────────
// CHAT HISTORY
//
// The transcript is stored server-side, so it survives a reload and can be
// reopened later. Three pieces of state drive it: the id of the chat currently
// open (null = a fresh, not-yet-created one), the list of chats, and the
// "loading" flag that stops a slow fetch from racing a new question.
// ─────────────────────────────────────────────────────────────

const AGENT_HISTORY_KEY = 'geonews.agent.session';
let agentSessionId = null;
let agentChats = [];
let agentHistoryLoading = false;

function agentLog() {
    return document.getElementById('agent-log');
}

function agentIsBusy() {
    const btn = document.getElementById('agent-send');
    return btn && btn.disabled;
}

function clearAgentLog() {
    const log = agentLog();
    log.innerHTML = '';
    agentLastMessage = null;
    agentPendingSources = null;
    clearAgentProgress();
}

// Set once the server has told us which chat a turn landed in. Kept in
// localStorage so a reload reopens the conversation rather than orphaning it.
function rememberSession(id) {
    agentSessionId = id;
    if (id) {
        try { localStorage.setItem(AGENT_HISTORY_KEY, String(id)); }
        catch { /* private mode: the in-memory id still works */ }
    } else {
        try { localStorage.removeItem(AGENT_HISTORY_KEY); } catch {}
    }
}

function restoreSession() {
    try {
        const saved = localStorage.getItem(AGENT_HISTORY_KEY);
        agentSessionId = saved ? parseInt(saved, 10) || null : null;
    } catch {
        agentSessionId = null;
    }
}

function formatChatTime(iso) {
    if (!iso) return '';
    const when = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
    if (Number.isNaN(when.getTime())) return '';
    const diff = Date.now() - when.getTime();
    const mins = Math.round(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.round(hours / 24);
    if (days < 7) return `${days}d ago`;
    return when.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}

// The select doubles as the history list. Titles repeat and can be long, so the
// date goes in the label and the first question in the tooltip.
function renderChatHistory() {
    const select = document.getElementById('agent-history');
    if (!select) return;
    select.innerHTML = '';

    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = agentChats.length ? 'Recent conversations…' : 'No earlier chats yet';
    select.appendChild(placeholder);

    for (const chat of agentChats) {
        const option = document.createElement('option');
        option.value = String(chat.id);
        const when = formatChatTime(chat.updated_at);
        option.textContent = when ? `${chat.title} — ${when}` : chat.title;
        option.title = chat.preview || chat.title;
        select.appendChild(option);
    }

    if (agentSessionId && agentChats.some(c => c.id === agentSessionId)) {
        select.value = String(agentSessionId);
    } else {
        select.value = '';
    }

    // Delete is only meaningful against a real conversation, so it appears once
    // there is one to delete rather than as a permanently dead button.
    const del = document.getElementById('agent-delete-chat');
    if (del) del.hidden = !select.value;
}

async function loadChatList() {
    try {
        const res = await fetch('/api/agent/chats');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        agentChats = (await res.json()).chats || [];
        renderChatHistory();
    } catch (err) {
        console.error('Could not load chat history:', err);
        renderChatHistory();
    }
}

async function openChat(chatId) {
    const log = agentLog();
    if (agentHistoryLoading || agentIsBusy()) return;

    agentHistoryLoading = true;
    try {
        const res = await fetch(`/api/agent/chats/${chatId}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const chat = await res.json();

        clearAgentLog();
        if (!chat.messages || !chat.messages.length) {
            appendGreetingNode('This conversation is empty. Ask something to start it.');
        }
        for (const message of chat.messages) {
            appendAgentMessage(message.role, message.content || '');
            // Re-show provenance so an old answer is still auditable. The metadata
            // hangs off the turn, not the chat.
            showAgentProvenance(message.meta);
        }
        rememberSession(chat.id);
        renderChatHistory();
        log.scrollTop = log.scrollHeight;
    } catch (err) {
        console.error('Could not open chat:', err);
        appendAgentMessage('error', 'Could not open that conversation: ' + err.message);
    } finally {
        agentHistoryLoading = false;
    }
}

function appendGreetingNode(text) {
    const div = document.createElement('div');
    div.className = 'agent-message assistant';
    div.textContent = text;
    agentLog().appendChild(div);
    return div;
}

// Rebuild the "Consulted: …" line from a stored turn so a reopened answer still
// shows what it was based on.
function showAgentProvenance(meta) {
    if (!meta) return;
    if (meta.deep_research) {
        showAgentSources(
            `Deep research: ${meta.angles} angles, ${meta.articles_read} articles read`,
            true
        );
    } else if (Array.isArray(meta.tools) && meta.tools.length) {
        showAgentSources(`Consulted: ${[...new Set(meta.tools)].join(', ')}`, true);
    }
}

async function startNewChat() {
    if (agentIsBusy() || agentHistoryLoading) return;

    const res = await fetch('/api/agent/chats', { method: 'POST' });
    if (!res.ok) {
        appendAgentMessage('error', 'Could not start a new chat.');
        return;
    }
    const chat = (await res.json()).chat;

    clearAgentLog();
    appendGreetingNode('New conversation. What would you like to look into?');
    rememberSession(chat.id);
    await loadChatList();
    document.getElementById('agent-input')?.focus();
}

async function deleteChat(chatId) {
    const res = await fetch(`/api/agent/chats/${chatId}`, { method: 'DELETE' });
    if (!res.ok) return false;
    if (agentSessionId === chatId) {
        clearAgentLog();
        appendGreetingNode('Conversation deleted.');
        rememberSession(null);
    }
    await loadChatList();
    return true;
}

// Which tools the agent actually used for the pending answer. Shown above the
// reply so a grounded answer is visibly different from one written from weights.
let agentPendingSources = null;

// Progress line for long runs (deep research). Reused in place so the log does
// not fill with one line per research stage.
let agentProgressNode = null;

function showAgentProgress(text) {
    const log = document.getElementById('agent-log');
    if (!agentProgressNode || !agentProgressNode.isConnected) {
        agentProgressNode = document.createElement('div');
        agentProgressNode.className = 'agent-progress';
        log.appendChild(agentProgressNode);
    }
    agentProgressNode.textContent = text + '…';
    log.scrollTop = log.scrollHeight;
}

function clearAgentProgress() {
    if (agentProgressNode && agentProgressNode.isConnected) agentProgressNode.remove();
    agentProgressNode = null;
}

function showAgentSources(text, fresh = false) {
    const log = document.getElementById('agent-log');
    // Reusing one node keeps the live single-answer case tidy, but a transcript
    // replay has several answers and each needs its own provenance line.
    if (fresh || !agentPendingSources) {
        agentPendingSources = document.createElement('div');
        agentPendingSources.className = 'agent-sources';
        log.appendChild(agentPendingSources);
    }
    agentPendingSources.textContent = text;
    log.scrollTop = log.scrollHeight;
}

async function sendAgentMessage() {
    const input = document.getElementById('agent-input');
    const btn = document.getElementById('agent-send');
    const text = input.value.trim();
    if (!text) return;

    input.value = '';
    appendAgentMessage('user', text);
    agentLastMessage = null;
    agentPendingSources = null;
    clearAgentProgress();
    btn.disabled = true;
    input.disabled = true;

    // Deep research is a separate endpoint because it takes a minute or more and
    // streams stage progress; the chat path must stay fast.
    const deep = document.getElementById('agent-deep')?.checked;
    const endpoint = deep ? '/api/agent/research' : '/api/agent/chat';

    try {
        const res = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            // currentDetailArticleId is set by the detail panel, so the chat is grounded in
// whatever article the user is actually reading.
body: JSON.stringify(deep
                ? { question: text, session_id: agentSessionId }
                : { message: text, article_id: currentDetailArticleId, session_id: agentSessionId }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();
            for (const line of lines) {
                const trimmed = line.trim();
                if (!trimmed.startsWith('data:')) continue;
                const data = trimmed.slice(5).trim();
                if (!data || data === '[DONE]') continue;
                try {
                    const ev = JSON.parse(data);
                    if (ev.type === 'delta') appendAgentDelta(ev.content || '');
                    else if (ev.type === 'sources') showAgentSources(ev.content || '');
                    else if (ev.type === 'progress') showAgentProgress(ev.content || '');
                    else if (ev.type === 'chat') {
                        // The server decides which chat this is: it may have started a
                        // new one because we had none, or ours may have been deleted.
                        rememberSession(ev.session_id);
                    }
                    else if (ev.type === 'thinking') {
                        // Create the bubble now so the wait is visible; the
                        // first delta then appends into this same element.
                        if (!agentLastMessage) agentLastMessage = appendAgentMessage('assistant', '');
                    }
                    else if (ev.type === 'error') appendAgentMessage('error', ev.content || 'Agent error');
                    else if (ev.type === 'done') clearAgentProgress();
                } catch {}
            }
        }
    } catch (err) {
        clearAgentProgress();
        console.error('Agent chat failed:', err);
        appendAgentMessage('error', 'Agent unavailable: ' + err.message);
    } finally {
        clearAgentProgress();
        btn.disabled = false;
        input.disabled = false;
        input.focus();
        // Refresh in the background: the title only exists once the first question
        // has been stored, so waiting for this would leave the list stale.
        loadChatList();
    }
}

// ─────────────────────────────────────────────────────────────
// MARKETS UI
// ─────────────────────────────────────────────────────────────

function showTab(tab) {
    currentTab = tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelector(`.tab[data-tab="${tab}"]`)?.classList.add('active');

    document.getElementById('feed').classList.add('hidden');
    document.getElementById('markets').classList.add('hidden');
    document.getElementById('trends').classList.add('hidden');
    document.getElementById('agent').classList.add('hidden');
    document.getElementById('controls').classList.add('hidden');
    document.getElementById('feed-status').classList.add('hidden');
    document.querySelectorAll('.menu-item').forEach(item => item.classList.toggle('active', item.dataset.tab === tab));

    if (tab === 'feed') {
        document.getElementById('feed').classList.remove('hidden');
        document.getElementById('controls').classList.remove('hidden');
        document.getElementById('feed-status').classList.remove('hidden');
        refreshAll();
    } else if (tab === 'markets') {
        document.getElementById('markets').classList.remove('hidden');
        loadMarkets();
    } else if (tab === 'trends') {
        document.getElementById('trends').classList.remove('hidden');
        loadTrends();
    } else if (tab === 'agent') {
        document.getElementById('agent').classList.remove('hidden');
    }
}

function openMenu() {
    document.getElementById('menu-overlay').classList.add('visible');
    document.getElementById('menu-drawer').classList.add('visible');
    menuFocusIndex = -1;
    clearFocus();
}

function closeMenu() {
    document.getElementById('menu-overlay').classList.remove('visible');
    document.getElementById('menu-drawer').classList.remove('visible');
}

let currentPortfolio = { balance: 0, positions: [] };

async function loadPortfolio() {
    try {
        const res = await fetch('/api/markets/portfolio/me');
        currentPortfolio = await res.json();
        renderPortfolio();
    } catch (err) {
        console.error('Failed to load portfolio:', err);
    }
}

function renderPortfolio() {
    const existing = document.querySelector('.portfolio-bar');
    if (existing) existing.remove();

    const bar = document.createElement('div');
    bar.className = 'portfolio-bar';
    bar.innerHTML = `
        <span><strong>💰 Wallet:</strong> ${currentPortfolio.balance.toLocaleString()} credits</span>
        <span><strong>Positions:</strong> ${currentPortfolio.positions.length}</span>
    `;
    const pane = document.getElementById('left-pane');
    pane.insertBefore(bar, pane.children[1]);
}

async function loadMarkets() {
    const container = document.getElementById('markets');
    container.innerHTML = '<div style="padding: 20px; text-align: center; color: #94a3b8;">Loading markets…</div>';

    try {
        const [marketsRes, portfolioRes] = await Promise.all([
            fetch('/api/markets?status=open'),
            fetch('/api/markets/portfolio/me'),
        ]);
        const markets = await marketsRes.json();
        currentPortfolio = await portfolioRes.json();
        renderPortfolio();
        renderMarkets(markets);
    } catch (err) {
        console.error('Failed to load markets:', err);
        container.innerHTML = '<div style="padding: 20px; text-align: center; color: #ef4444;">Failed to load markets.</div>';
    }
}

function renderMarkets(markets) {
    const container = document.getElementById('markets');
    container.innerHTML = '';

    if (markets.length === 0) {
        container.innerHTML = '<div style="padding: 20px; text-align: center; color: #94a3b8;">No open markets. Create one via the API at /api/markets.</div>';
        return;
    }

    markets.forEach(market => {
        const card = document.createElement('div');
        card.className = 'market-card';
        card.innerHTML = `
            <div class="market-title">${escapeHtml(market.title)}</div>
            <div class="market-prices">
                <span class="market-price" style="color:#22c55e">YES ${formatPrice(market.yes_price)}</span>
                <span class="market-price" style="color:#ef4444">NO ${formatPrice(market.no_price)}</span>
                <span class="market-price">💧 ${market.liquidity}</span>
            </div>
            ${market.description ? `<div class="article-summary">${escapeHtml(market.description)}</div>` : ''}
            <div class="bet-form">
                <input type="number" min="1" value="10" id="shares-${market.id}">
                <button class="btn-yes" data-id="${market.id}" data-side="yes">BET YES</button>
                <button class="btn-no" data-id="${market.id}" data-side="no">BET NO</button>
            </div>
            <div class="bet-status" id="status-${market.id}" style="font-size: 11px; margin-top: 6px; min-height: 16px;"></div>
        `;

        card.querySelectorAll('.btn-yes, .btn-no').forEach(btn => {
            btn.addEventListener('click', async () => {
                const marketId = btn.dataset.id;
                const side = btn.dataset.side;
                const shares = parseFloat(document.getElementById(`shares-${marketId}`).value);
                if (!shares || shares <= 0) return;
                await placeBet(marketId, side, shares);
            });
        });

        container.appendChild(card);
    });
}

function formatPrice(n) {
    if (n === null || n === undefined) return '—';
    return `$${n.toFixed(2)}`;
}

async function placeBet(marketId, side, shares) {
    const statusEl = document.getElementById(`status-${marketId}`);
    statusEl.textContent = 'Placing bet…';
    statusEl.style.color = '#94a3b8';

    try {
        const res = await fetch(`/api/markets/${marketId}/bets`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ side, shares }),
        });
        const data = await res.json();
        if (!res.ok) {
            statusEl.textContent = data.detail || 'Bet failed';
            statusEl.style.color = '#ef4444';
            return;
        }
        statusEl.textContent = `Bet placed: ${side.toUpperCase()} ${shares} shares @ $${data.avg_price.toFixed(3)}`;
        statusEl.style.color = '#22c55e';
        await loadPortfolio();
    } catch (err) {
        statusEl.textContent = 'Network error';
        statusEl.style.color = '#ef4444';
    }
}

async function refreshAll() {
    if (currentTab === 'feed') {
        await loadFeed();
        populateRegionFilter();
        await loadMapMarkers();
        await loadStats();
    } else if (currentTab === 'markets') {
        await loadMarkets();
    } else if (currentTab === 'trends') {
        await loadTrends();
    }
}

function init() {
    initMap();

    document.getElementById('filter-region')?.addEventListener('change', refreshAll);
    document.getElementById('btn-ingest')?.addEventListener('click', triggerIngest);
    document.getElementById('btn-process')?.addEventListener('click', triggerProcess);

    document.getElementById('search-input')?.addEventListener('input', () => {
        clearTimeout(searchDebounce);
        searchDebounce = setTimeout(refreshAll, 300);
    });

    document.querySelectorAll('.layer-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            const type = chip.dataset.type;
            if (activeTypes.size === Object.keys(TYPE_LABELS).length) {
                activeTypes.clear();
                activeTypes.add(type);
            } else if (activeTypes.has(type) && activeTypes.size === 1) {
                Object.keys(TYPE_LABELS).forEach(t => activeTypes.add(t));
            } else {
                if (activeTypes.has(type)) activeTypes.delete(type);
                else activeTypes.add(type);
            }
            renderLayerChips();
            refreshAll();
        });
    });

    document.querySelectorAll('.tab').forEach(tab => {
        tab.addEventListener('click', () => showTab(tab.dataset.tab));
    });

    document.getElementById('detail-close')?.addEventListener('click', closeArticleDetail);
    document.getElementById('detail-modal')?.addEventListener('click', (e) => {
        if (e.target.id === 'detail-modal') closeArticleDetail();
    });
    document.getElementById('menu-btn')?.addEventListener('click', openMenu);
    document.getElementById('menu-overlay')?.addEventListener('click', closeMenu);
    document.getElementById('menu-close')?.addEventListener('click', closeMenu);
    document.querySelectorAll('.menu-item').forEach(item => {
        item.addEventListener('click', () => {
            closeMenu();
            showTab(item.dataset.tab);
        });
    });

    document.getElementById('map-prev')?.addEventListener('click', () => cycleMapNode(-1));
    document.getElementById('map-next')?.addEventListener('click', () => cycleMapNode(1));

    document.addEventListener('keydown', handleKeydown);

    document.getElementById('agent-send')?.addEventListener('click', sendAgentMessage);
    document.getElementById('agent-input')?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendAgentMessage();
        }
    });
    document.getElementById('agent-new-chat')?.addEventListener('click', startNewChat);
    document.getElementById('agent-history')?.addEventListener('change', (e) => {
        const id = parseInt(e.target.value, 10);
        if (id) openChat(id);
        else renderChatHistory();
    });
    document.getElementById('agent-delete-chat')?.addEventListener('click', async () => {
        if (!agentSessionId || !confirm('Delete this conversation for good?')) return;
        await deleteChat(agentSessionId);
    });

    // Reopen whatever was in progress, then backfill the dropdown. Restoring the
    // transcript has to happen after the list loads so the select can select.
    restoreSession();
    loadChatList().then(() => {
        if (agentSessionId) {
            openChat(agentSessionId).catch((err) => {
                console.error('Could not restore previous chat:', err);
            });
        }
    });

    showTab('feed');
    refreshAll();
    setInterval(() => { if (currentTab === 'feed') refreshAll(); else if (currentTab === 'markets') loadMarkets(); }, 30000);
}

document.addEventListener('DOMContentLoaded', init);
