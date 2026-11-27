let currentTab = 'today';
let companyDrill = false;      // true while viewing a single company's contacts
let _companies = [], _prospects = [], _campaigns = [];
let _signals = null;           // last /api/signals payload, for the cohort builder
let _desk = { items: [], i: 0 };

// ── Appearance ──
//
// Three states, not two: "auto" follows the OS and is the default, so the
// dashboard matches the rest of your machine until you deliberately override
// it. Stored per-browser; nothing is sent anywhere.

const THEMES = ['auto', 'light', 'dark'];

function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === 'auto') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', mode);
  document.querySelectorAll('[data-theme-opt]').forEach((b) =>
    b.setAttribute('aria-pressed', String(b.dataset.themeOpt === mode)));
}

function setTheme(mode) {
  try { localStorage.setItem('sellbird-theme', mode); } catch { /* private mode */ }
  applyTheme(mode);
}

function toggleOledTheme() {
  const attr = document.documentElement.getAttribute('data-theme');
  const isDark = attr === 'dark' || (!attr && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  setTheme(isDark ? 'light' : 'dark');
}

(function initTheme() {
  let saved = 'auto';
  try {
    // Carry the choice over from Harvey, VeloReach's old name.
    const legacy = localStorage.getItem('harvey-theme');
    if (legacy && !localStorage.getItem('sellbird-theme')) localStorage.setItem('sellbird-theme', legacy);
    if (legacy) localStorage.removeItem('harvey-theme');
    saved = localStorage.getItem('sellbird-theme') || 'auto';
  } catch { /* ignore */ }
  applyTheme(THEMES.includes(saved) ? saved : 'auto');
})();

// ── Utilities ──

function escHtml(s) {
  if (s === null || s === undefined || s === '') return '';
  return String(s).replace(/[&<>"']/g, ch => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]
  ));
}

// A value placed inside an onclick handler as a JS string: JSON-quote it,
// then escape for the attribute (escHtml alone is undone before the JS runs).
function jsq(v) {
  return escHtml(JSON.stringify(String(v ?? '')));
}

// The reason a failed action gave, or a fallback when it gave none.
function failMsg(data, fallback) {
  return (data && (data.message || data.error || data.detail)) || fallback;
}

// Lookup tables keyed by stored strings: "constructor" must not hit Object's.
function own(table, key) {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

// Links built from collected data (websites, profiles) open only as http(s):
// a javascript: URL from a scraped page must never run in the dashboard.
function isHttpUrl(u) {
  return /^https?:\/\//i.test(String(u || '').trim());
}
function safeHref(u) {
  const s = String(u || '').trim();
  return isHttpUrl(s) ? escHtml(s) : '';
}

// ── One status vocabulary ──
//
// VeloReach's tables each carry their own words for state: a prospect is `new`,
// a campaign is `draft`, an email is `pending_review`, a signal is `proposed`.
// Five of those mean "waiting on you" and the old dashboard styled every one
// differently. Everything now resolves through this map, so a status reads the
// same way no matter which table it came out of.
//
// tone: waiting (needs a human) | active (in flight) | good | bad | idle
const STATUS = {
  // prospects
  new:            ['New', 'active'],
  contacted:      ['Contacted', 'active'],
  replied:        ['Replied', 'good'],
  accepted_offer: ['Said yes to your offer', 'good'],
  interested:     ['Interested', 'good'],
  meeting:        ['Meeting booked', 'note'],
  meeting_booked: ['Meeting booked', 'good'],
  not_interested: ['Not interested', 'bad'],
  bounced:        ['Bounced', 'bad'],
  write_failed:   ["Couldn't write", 'bad'],
  no_tip:         ['Nothing to write about yet', 'idle'],
  queued:         ['Queued', 'active'],
  opted_out:      ['Opted out', 'bad'],
  // campaigns
  draft:          ['Draft', 'waiting'],
  active:         ['Active', 'good'],
  completed:      ['Completed', 'idle'],
  paused:         ['Paused', 'bad'],
  // conversations
  open:           ['Open', 'active'],
  closed:         ['Closed', 'idle'],
  closed_won:     ['Won', 'good'],
  closed_lost:    ['Lost', 'bad'],
  objection:      ['Objection', 'waiting'],
  question:       ['Question', 'active'],
  ooo:            ['Out of office', 'idle'],
  wrong_person:   ['Wrong person', 'waiting'],
  unsubscribe:    ['Unsubscribed', 'bad'],
  escalate:       ['Needs a human', 'bad'],
  needs_human:    ['Needs you', 'waiting'],
  // outbox
  pending_review: ['Waiting on you', 'waiting'],
  approved:       ['Approved', 'good'],
  sending:        ['Sending now', 'active'],
  scheduled:      ['Scheduled', 'active'],
  sent:           ['Sent', 'good'],
  awaiting_reply: ['Awaiting reply', 'active'],
  failed:         ['Failed', 'bad'],
  rejected:       ['Rejected', 'idle'],
  cancelled:      ['Cancelled', 'idle'],
  // signals
  proposed:       ['Waiting on you', 'waiting'],
  confirmed:      ['Confirmed', 'good'],
  // email deliverability
  verified:       ['Verified', 'good'],
  risky:          ['Catch-all', 'waiting'],
  guess:          ['Unverified', 'idle'],
  invalid:        ['Invalid', 'bad'],
  // runs
  running:        ['Running', 'active'],
  stale:          ['Stale', 'bad'],
  stalled:        ['Stopped', 'bad'],
};

function statusMeta(status) {
  const key = String(status || '').toLowerCase().replace(/[^a-z0-9]+/g, '_');
  const hit = own(STATUS, key);
  if (hit) return { label: hit[0], tone: hit[1] };
  const label = String(status || 'unknown').replace(/_/g, ' ');
  return { label: label.charAt(0).toUpperCase() + label.slice(1), tone: 'idle' };
}

function badge(status) {
  const m = statusMeta(status);
  return '<span class="badge t-' + m.tone + '">' + escHtml(m.label) + '</span>';
}

function formatDate(d) {
  if (!d) return '';
  try {
    let t = String(d).trim().replace(' ', 'T');
    if (/^\d{4}-\d\d-\d\dT/.test(t) && !/[zZ]$|[+-]\d\d:?\d\d$/.test(t)) t += 'Z';  // stored UTC
    const dt = new Date(t);
    if (isNaN(dt)) return escHtml(d);
    return dt.toLocaleString('en-US', {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  } catch { return escHtml(d); }
}

// ── Small render helpers ──

// The sprite in index.html holds every icon; this just references one.
function icon(name, cls) {
  return '<svg class="icon' + (cls ? ' ' + cls : '') + '" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
}

function initials(first, last) {
  const a = String(first || '').trim(), b = String(last || '').trim();
  const ch = x => Array.from(x)[0] || '';  // whole characters, not UTF-16 halves
  const s = (ch(a) + ch(b)) || '?';
  return escHtml(s.toUpperCase());
}

// Stored times are UTC, often without a zone ("2026-10-21 03:07:57").
function parseTs(d) {
  let t = String(d || '').trim().replace(' ', 'T');
  if (t && !/[zZ]$|[+-]\d\d:?\d\d$/.test(t)) t += 'Z';
  return new Date(t);
}

// "3m ago", "5h ago", "Yesterday", then a date: recent things read faster.
function relTime(d) {
  if (!d) return '';
  const dt = parseTs(d);
  if (isNaN(dt)) return escHtml(d);
  const mins = Math.round((Date.now() - dt) / 60000);
  if (mins < -1 && mins > -60) return 'in ' + -mins + 'm';
  if (mins < -1 && mins > -60 * 24) return 'in ' + Math.round(-mins / 60) + 'h';
  if (mins >= -1 && mins < 1) return 'just now';
  if (mins >= 1 && mins < 60) return mins + 'm ago';
  if (mins >= 60 && mins < 60 * 24) return Math.round(mins / 60) + 'h ago';
  if (mins >= 60 * 24 && mins < 60 * 48) return 'Yesterday';
  const opts = {month: 'short', day: 'numeric'};
  if (dt.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  return dt.toLocaleDateString('en-US', opts);
}

function dayLabel(d) {
  const dt = parseTs(d);
  if (isNaN(dt)) return '';
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const that = new Date(dt); that.setHours(0, 0, 0, 0);
  const diff = Math.round((today - that) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  return dt.toLocaleDateString('en-US', {weekday: 'long', month: 'short', day: 'numeric'});
}

function timeOfDay(d) {
  const dt = parseTs(d);
  return isNaN(dt) ? '' : dt.toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit'});
}

// Columns of a tiny bar chart; the last day is "today".
function sparkBars(values, title) {
  const vals = (values || []).map(v => Number(v) || 0);
  if (!vals.length) return '';
  const max = Math.max(...vals, 1);
  return '<span class="spark" role="img" aria-label="' + escHtml(title || 'trend') + '">' +
    vals.map((v, i) => '<i class="' + (v ? 'on' : '') + '" style="height:' +
      Math.max(2, Math.round(v / max * 28)) + 'px" title="' + v + '"></i>').join('') + '</span>';
}

// A segmented meter, 0-100.
function meterHtml(pct, segments) {
  const n = segments || 10;
  const v = Math.max(0, Math.min(100, Number(pct) || 0));
  const on = Math.round(v / 100 * n);
  return '<span class="meter-row"><span class="meter" role="meter" aria-valuemin="0" ' +
    'aria-valuemax="100" aria-valuenow="' + v + '">' +
    Array.from({length: n}, (_, i) => '<i class="' + (i < on ? 'on' : '') + '"></i>').join('') +
    '</span><span class="v">' + v + '</span></span>';
}

const SOURCE_LABELS = {
  company_website: 'Team page', known_company_backfill: 'Team page',
  scraped_mailto: 'Published address', hunter_domain_search: 'Hunter', linkedin_search: 'LinkedIn',
  job_board: 'Job board', web_search: 'Web search', company_inbox: 'Company inbox',
  osm: 'OpenStreetMap', dataforseo_listings: 'DataForSEO', dataforseo_serp: 'DataForSEO SERP', dataforseo_maps: 'DataForSEO Maps',
  serper: 'Serper', tinyfish: 'TinyFish', scavio_maps: 'Scavio Maps', scavio_search: 'Scavio Search',
};
function sourceLabel(s) {
  if (!s) return '';
  return escHtml(Object.prototype.hasOwnProperty.call(SOURCE_LABELS, s) ? SOURCE_LABELS[s]
    : String(s).replace(/_/g, ' ').replace(/^./, c => c.toUpperCase()));
}

const AGENT_ICONS = {scout: 'discover', writer: 'pen', sender: 'campaigns', handler: 'message',
                     analyst: 'usage', user: 'contacts', main: 'refresh'};
// One plain sentence per logged action, from the details each agent records.
function actionText(a) {
  const d = a.details || {};
  const who = escHtml(d.prospect_email || d.prospect || d.to || '');
  const camp = escHtml(d.campaign_name || d.campaign || '');
  const intent = d.intent ? ' · ' + escHtml(String(d.intent).replace(/_/g, ' ')) : '';
  const n = (v, one, many) => (Number(v) || 0) + ' ' + ((Number(v) || 0) === 1 ? one : many);
  switch (a.action_type) {
    case 'prospect': return 'Found ' + n(d.prospects_found, 'new contact', 'new contacts');
    case 'write_campaign': return 'Wrote “' + camp + '” for ' + n(d.prospect_count, 'contact', 'contacts');
    case 'write_failed': return 'Couldn’t write for some contacts' + (camp ? ' in “' + camp + '”' : '');
    case 'stage_campaign': return 'Queued ' + n(d.staged, 'email', 'emails') + ' from “' + camp + '”' +
      (d.mode ? ' · ' + escHtml(d.mode) : '');
    case 'send_campaign': return 'Sent “' + camp + '” to Instantly · ' + n(d.leads_added, 'lead', 'leads');
    case 'email_sent': return d.kind === 'reply' ? 'Sent a reply to ' + who
      : 'Sent email ' + escHtml(String(d.step || 1)) + ' to ' + who;
    case 'reply': return 'Answered ' + who + intent;
    case 'reply_queued': return 'Drafted a reply to ' + who + intent;
    case 'opt_out': return who + ' opted out' + (d.why ? ' · ' + escHtml(d.why) : '');
    case 'opt_out_undone': return 'You undid ' + (who || 'a contact') + '’s opt-out';
    case 'reply_after_opt_out': return who + ' wrote again after opting out · no reply sent';
    case 'escalation': return 'Flagged ' + who + ' for you' + (d.reason ? ' · ' + escHtml(d.reason) : '');
    case 'bounce': return 'Email to ' + who + ' bounced';
    case 'manual_send': return 'You marked a contact as emailed by hand';
    case 'manual_send_undone': return 'You undid a hand-sent mark';
    case 'outbox_edited': return 'You edited a draft';
    case 'conversation_status':
      return d.to === 'closed' ? 'You closed a conversation'
        : d.from === 'needs_human' ? 'You handed a conversation back to VeloReach' : 'You reopened a conversation';
  }
  const text = escHtml(String(a.action_type || '').replace(/_/g, ' '));
  return a.agent === 'main' ? 'Heartbeat: ' + text : text;
}

function emptyState(glyph, title, copy) {
  return '<div class="empty"><div class="glyph">' + glyph + '</div>' +
    '<div class="title">' + title + '</div>' +
    '<div class="copy">' + copy + '</div></div>';
}

function offlineState() {
  return emptyState('&#9888;', 'Dashboard can\'t reach the server',
    'The dashboard process may have stopped. Restart it with <b>sellbird dashboard</b> and refresh this page.');
}

// A tab's content while its fetch is in flight. Replaced by whatever renders
// next — data, an empty state, or offlineState() — so every loader must write
// its container before returning.
function loadingState() {
  return '<div class="loading" role="status">Loading…</div>';
}

async function api(path, opts) {
  try {
    const r = await authFetch(path, opts);
    if (await lockIfRefused(r)) return null;
    if (!r.ok) {
      // An action the server refused says why ("a run is already going");
      // pass that body on so the toast can show it. Reads stay null.
      if (!opts || !opts.method || opts.method === 'GET') return null;
      const body = await r.json().catch(() => null);
      return body && typeof body === 'object' ? {success: false, ...body} : null;
    }
    return await r.json();
  } catch {
    return null;
  }
}

// ── Locked: no token, a wrong one, or the server can't load its own ──

let _locked = false;

async function lockIfRefused(r) {
  if (r.status !== 401 && r.status !== 503) {
    if (r.ok && _locked) hideLock();
    return false;
  }
  let why = '';
  if (r.status === 503) {
    try { why = ((await r.json()) || {}).message || ''; } catch { /* ignore */ }
  }
  showLock(r.status === 503
    ? 'The dashboard server can\'t use its token file, so it is refusing every request. ' + why
    : authToken()
      ? 'The saved token was not accepted (the dashboard may have a new one).'
      : 'This tab doesn\'t have the dashboard token yet.');
  return true;
}

function showLock(reason) {
  _locked = true;
  const el = document.getElementById('lock-screen');
  if (!el) return;
  document.getElementById('lock-reason').textContent = reason;
  el.style.display = 'flex';
  const dot = document.getElementById('header-dot');
  const text = document.getElementById('header-status-text');
  if (dot) dot.className = 'status-dot offline';
  if (text) text.textContent = 'Locked';
}

function hideLock() {
  _locked = false;
  const el = document.getElementById('lock-screen');
  if (el) el.style.display = 'none';
}

function unlockWithToken(event) {
  event.preventDefault();
  const input = document.getElementById('lock-token');
  const value = (input.value || '').trim();
  input.value = '';
  if (!setAuthToken(value)) {
    document.getElementById('lock-reason').textContent =
      'That doesn\'t look like a dashboard token. Copy it again from the link or data/dashboard-token.';
    return;
  }
  hideLock();
  loadCurrentTab();
  loadSellbirdStatus();
}

// Downloads go through the same authorised fetch and are handed to the
// browser as a local blob, so no download URL ever carries the token.
async function downloadCsv(path, filename, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await authFetch(path);
    if (await lockIfRefused(r)) return;
    if (!r.ok) { showToast('Export failed.', 'error'); return; }
    const href = URL.createObjectURL(await r.blob());
    const a = document.createElement('a');
    a.href = href;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 10000);
  } catch {
    showToast('Export failed — is the dashboard still running?', 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function showToast(msg, type = 'info') {
  const t = document.createElement('div');
  t.className = 'toast ' + (type || 'info');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2600);
}

function toggleVisibility(inputId) {
  const el = document.getElementById(inputId);
  el.type = el.type === 'password' ? 'text' : 'password';
}

// ── Tabs ──

function showTab(id, btn, skipLoad) {
  const apply = () => {
    currentTab = id;
    if (id === 'companies') companyDrill = false;
    document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
    document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
    const sec = document.getElementById(id);
    if (sec) sec.classList.add('active');
    const activeBtn = btn || Array.from(document.querySelectorAll('nav button'))
      .find(b => (b.getAttribute('onclick') || '').includes("'" + id + "'"));
    if (activeBtn) activeBtn.classList.add('active');
    if (!skipLoad && typeof window.scrollTo === 'function') window.scrollTo(0, 0);
  };
  const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!skipLoad && typeof document.startViewTransition === 'function' && !reduced) {
    try { document.startViewTransition(apply); } catch { apply(); }
  } else {
    apply();
  }
  if (!skipLoad) loadCurrentTab();
}

function goToTab(id) { showTab(id, null); }

let _asciiTimer = null;
let _asciiIdx = 0;
const _ASCII_FRAMES = ['|', '/', '-', '\\'];

function updateAsciiTelemetry(runningNow) {
  const stateEl = document.getElementById('ascii-state');
  const barsEl = document.getElementById('ascii-bars');
  const spinEl = document.getElementById('ascii-spinner');
  if (!stateEl || !barsEl) return;
  const stats = typeof _todayStats === 'object' && _todayStats ? _todayStats : {};
  const busy = !!(runningNow || _todayLoopRunning);
  stateEl.textContent = busy ? '[PIPE :: ACTIVE]' : '[PIPE :: READY]';
  const co = Number(stats.companies_total ?? stats.companies ?? 0) || 0;
  const pr = Number(stats.prospects_total ?? stats.prospects ?? 0) || 0;
  const ob = Number(stats.outbox_pending ?? 0) || 0;
  const lit = busy ? 5 : Math.min(5, Math.max(1, (co ? 1 : 0) + (pr ? 2 : 0) + (ob ? 2 : 0)));
  barsEl.textContent = '▓'.repeat(lit) + '░'.repeat(5 - lit) + ' ' + co + 'CO · ' + pr + 'P · ' + ob + 'Q';
  if (spinEl) {
    if (busy && !_asciiTimer) {
      _asciiTimer = setInterval(() => {
        _asciiIdx = (_asciiIdx + 1) % _ASCII_FRAMES.length;
        spinEl.textContent = _ASCII_FRAMES[_asciiIdx];
      }, 180);
    } else if (!busy) {
      if (_asciiTimer) { clearInterval(_asciiTimer); _asciiTimer = null; }
      spinEl.textContent = '::';
    }
  }
}

(function wireSpecularDepth() {
  if (typeof document.addEventListener !== 'function') return;
  document.addEventListener('pointermove', (e) => {
    const t = e.target && typeof e.target.closest === 'function'
      ? e.target.closest('.kpi, .stat-card, .prov-card, .preset-card') : null;
    if (!t || typeof t.getBoundingClientRect !== 'function') return;
    const r = t.getBoundingClientRect();
    if (!r.width || !r.height) return;
    t.style.setProperty('--mx', Math.round(((e.clientX - r.left) / r.width) * 100) + '%');
    t.style.setProperty('--my', Math.round(((e.clientY - r.top) / r.height) * 100) + '%');
  }, { passive: true });
})();

// The container each tab refills, painted with loadingState() while its
// fetch is in flight. Controls and Help are static; Today's smaller cards
// keep their last numbers on a failed read (the queue says why).
const TAB_LOADING = {
  today: ['today-queue', 'live-status'],
  signals: ['signals-groups'],
  discover: ['discover-providers'],
  companies: ['companies-list'],
  prospects: ['prospects-table'],
  campaigns: ['campaigns-list'],
  outbox: ['outbox-desk', 'outbox-list'],
  conversations: ['conversations-list'],
  activity: ['activity-list'],
  brain: ['brain-chain-container', 'brain-roles-grid'],
  enrichment: ['clay-column-list'],
  inboxes: ['inboxes-list'],
  usage: ['usage-stats'],
  settings: ['sending-mode-card', 'cw-watches'],
  company: ['company-body'],
};

function showTabLoading(id) {
  // A drilled-in company view isn't reloaded on tab entry; don't wipe it.
  if (id === 'companies' && companyDrill) return;
  for (const cid of own(TAB_LOADING, id) || []) {
    const el = document.getElementById(cid);
    if (el) el.innerHTML = loadingState();
  }
}

function loadCurrentTab() {
  showTabLoading(currentTab);
  switch (currentTab) {
    case 'today': loadLiveStatus(); loadRenameNotice(); loadToday(); loadTrend(); loadFunnel(); loadSetupStatus(); loadTodayResendCard(); loadRuns(); loadTodayActivity(); break;
    case 'signals': loadSignals(); break;
    case 'discover': loadDiscoverProviders(); break;
    case 'companies': if (!companyDrill) loadCompanies(); break;
    case 'prospects': loadProspects(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'outbox': loadOutbox(); break;
    case 'conversations': loadConversations(); break;
    case 'activity': loadActivity(); break;
    case 'brain': loadBrainStudio(); break;
    case 'enrichment': loadEnrichment(); break;
    case 'inboxes': loadInboxes(); break;
    case 'usage': loadUsage(); break;
    case 'settings': loadSettings(); loadSendingModeCard(); loadCompetitorWatch(); break;
    case 'company': loadCompany(); break;
    case 'controls': loadSellbirdStatus(); loadLogs(); break;
  }
}

// ── Today: what needs a human ──

async function loadToday() {
  const data = await api('/api/today');
  const el = document.getElementById('today-queue');
  if (!data) { el.innerHTML = offlineState(); return; }
  if (data.error) {
    // A failed check must never read as "Nothing needs you".
    el.innerHTML = emptyState(icon('alert'), 'Couldn\'t check what needs you',
      'The dashboard hit an error reading VeloReach\'s data: ' + escHtml(data.error));
    return;
  }

  navCount('nav-today', (data.items || []).filter(i => i.tone !== 'good').length);
  navCount('nav-outbox', (data.stats || {}).outbox_pending || 0);
  navCount('nav-signals', (data.stats || {}).signals_proposed || 0);
  navCount('nav-conversations', (data.stats || {}).needs_human || 0);
  _todayStats = data.stats || {};
  renderFigures(_todayStats);
  renderTodayAgentic(data.agentic || null);
  renderTodayWorkflowGuide(_todayStats, data.items || []);
  updateAsciiTelemetry(!!(data.agentic && data.agentic.running));

  const items = data.items || [];
  if (!items.length) {
    el.innerHTML = '<div class="queue-clear">' +
      '<div class="queue-icon">' + icon('check') + '</div><div>' +
      '<div class="qtitle">Nothing needs you</div>' +
      '<div class="qdetail">VeloReach has everything it needs. Anything that requires a ' +
      'decision — an email to approve, a signal to confirm — shows up here.</div>' +
      '</div></div>';
    return;
  }

  const glyph = {bad: 'alert', warn: 'flag', good: 'check'};
  el.innerHTML = '<div class="queue">' + items.map(it => {
    const tone = escHtml(it.tone || 'warn');
    const href = safeHref(it.url);
    const linkBtn = href
      ? '<a class="btn btn-primary btn-sm" href="' + href + '" target="_blank" rel="noopener noreferrer">' +
          escHtml(it.url_label || 'Open') + '</a>'
      : '';
    return '<div class="queue-item ' + tone + '">' +
      '<div class="queue-icon">' + icon(glyph[it.tone] || 'flag') + '</div>' +
      '<div class="qtext">' +
        '<div class="qtitle">' + escHtml(it.title) + '</div>' +
        '<div class="qdetail">' + escHtml(it.detail) + '</div>' +
      '</div>' +
      linkBtn +
      '<button class="btn btn-secondary btn-sm" onclick="goTab(' + jsq(it.tab) + ')">' +
        escHtml(it.action) + icon('arrow') + '</button>' +
    '</div>';
  }).join('') + '</div>';
}

// ── Live status: the "what's happening?" answer, kept on screen ──

async function loadLiveStatus() {
  const el = document.getElementById('live-status');
  if (!el) return;
  const d = await api('/api/live-status');
  if (!d || !Array.isArray(d.steps)) {
    // Keep the last good numbers on screen, but never let them pass for live.
    const flag = el.querySelector('.live-flag');
    if (flag) flag.textContent = "Can't reach VeloReach right now — numbers below may be old.";
    else el.innerHTML = '<p class="muted live-flag">Can\'t reach VeloReach right now. ' +
      'Is the dashboard window still open?</p>';
    return;
  }
  const glyph = {done: icon('check'), running: '<span class="pulse"></span>', partial: '', waiting: ''};
  const fill = {done: 'var(--good)', running: 'var(--accent)', partial: 'var(--accent)', waiting: 'var(--line-strong)'};
  const rows = d.steps.map(s => {
    const st = Object.prototype.hasOwnProperty.call(glyph, s.state) ? s.state : 'waiting';
    const pct = s.total ? Math.min(100, Math.round(100 * s.done / s.total)) : 0;
    const bar = s.total
      ? '<div class="bar"><span style="width:' + pct + '%;background:' + fill[st] + '"></span></div>'
      : '';
    return '<div class="step">' +
      '<span class="step-icon ' + st + '">' + glyph[st] + '</span>' +
      '<span class="step-label">' + escHtml(s.label) + '</span>' +
      '<span class="step-detail">' + escHtml(s.detail) + '</span>' + bar +
    '</div>';
  }).join('');
  const jobName = r => r.stage === 'profile' ? 'Reading websites'
    : r.stage === 'discover' ? 'Searching for businesses (' + (r.provider || '') + ')'
    : String(r.stage || 'Job');
  const running = d.running || [];
  const runningHtml = running.length
    ? running.map(r => '<span class="live-now"><span class="pulse"></span>' + escHtml(jobName(r)) +
        ' · ' + r.elapsed_min + ' min' + (r.eta_text ? ' · ' + escHtml(r.eta_text) : '') + '</span>').join('')
    : '<span class="live-now">Nothing running right now</span>';
  const stoppedHtml = (d.stopped || []).length
    ? '<div class="banner bad" style="margin:8px 0 0">' + icon('alert') + '<p style="color:inherit">' +
      (d.stopped || []).map(r => escHtml(jobName(r))).join(', ') + ' stopped before finishing.</p></div>'
    : '';
  el.innerHTML =
    '<div class="live-head">' + runningHtml +
      '<span class="setup-pct">Cost so far $' + Number(d.cost_usd || 0).toFixed(2) + '</span></div>' +
    stoppedHtml +
    '<div class="steps">' + rows + '</div>' +
    (d.next ? '<div class="live-next">' + icon('arrow') + '<span><b>Next</b> · ' + escHtml(d.next) + '</span>' +
      (d.next_tab ? '<button class="btn btn-secondary btn-sm" onclick="goTab(' + jsq(d.next_tab) + ')">Go</button>' : '') +
      '</div>' : '') +
    '<div class="live-flag">Updated ' + new Date().toLocaleTimeString() + ' · refreshes every 10 seconds</div>';
}

// KPI cards. Totals come from /api/today; the 14-day bars from /api/trend.
function renderFigures(stats) {
  const el = document.getElementById('today-kpis');
  if (!el) return;
  const t = _trend || {};
  const sum = a => (a || []).reduce((x, y) => x + (Number(y) || 0), 0);
  const last7 = a => sum((a || []).slice(-7));
  const card = (label, value, spark, note) =>
    '<div class="kpi"><div class="kpi-label">' + label + '</div>' +
    '<div class="kpi-main"><span class="kpi-value' + (value ? '' : ' zero') + '">' + value + '</span>' + spark + '</div>' +
    '<span class="kpi-note">' + note + '</span></div>';

  // Instantly sends from its own servers, so the Outbox never sees them.
  const sent = stats.email_provider === 'instantly'
    ? card('Emails sent', '&mdash;', '', 'Instantly tracks sends')
    : card('Emails sent', sum(t.sent), sparkBars(t.sent, 'Emails sent per day, last 14 days'),
           'last 14 days · <b>' + (stats.outbox_approved || 0) + '</b> scheduled');
  const waiting = (stats.outbox_pending || 0) + (stats.needs_human || 0);
  const waitNote = [
    stats.outbox_pending ? stats.outbox_pending + ' to approve' : '',
    stats.needs_human ? stats.needs_human + ' handed to you' : '',
  ].filter(Boolean).join(' · ') || 'nothing to approve';
  const facts = document.getElementById('today-facts');
  if (facts) facts.innerHTML =
    '<span><b>' + (stats.companies || 0) + '</b> companies</span>' +
    '<span><b>' + (stats.unprofiled || 0) + '</b> sites not read yet</span>' +
    '<span><b>' + (stats.signals_confirmed || 0) + '</b> signals on</span>';

  el.innerHTML = '<div class="kpis">' +
    card('Contacts found', stats.prospects || 0, sparkBars(t.found, 'Contacts found per day, last 14 days'),
         '<b>+' + last7(t.found) + '</b> this week') +
    sent +
    card('Conversations', stats.open_conversations || 0, sparkBars(t.replies, 'New replies per day, last 14 days'),
         '<b>' + sum(t.replies) + '</b> new in 14 days') +
    card('Waiting on you', waiting, '', waitNote) +
  '</div>';
}

let _todayStats = {};
let _trend = null;

async function loadTrend() {
  const t = await api('/api/trend?days=14&tz=' + -new Date().getTimezoneOffset());
  if (t && Array.isArray(t.days)) { _trend = t; renderFigures(_todayStats); }
}

// Contacts by stage: one bar, one legend, from /api/stats.
async function loadFunnel() {
  const el = document.getElementById('today-figures');
  if (!el) return;
  const data = await api('/api/stats');
  if (!data) return;  // offline: keep the last numbers; Today's queue says so
  if (data.error) { el.innerHTML = '<p class="muted" style="font-size:13px">Couldn\'t read the pipeline.</p>'; return; }
  const total = (data.prospects || {}).total || 0;
  const pipe = data.pipeline || {};
  const stages = [
    ['To write', pipe.to_write || 0, 'c-accent-soft'],
    ['Contacted', pipe.contacted || 0, 'c-accent'],
    ['Replied', pipe.replied || 0, 'c-active'],
    ['Interested', pipe.interested || 0, 'c-good'],
    ['Ready to meet', pipe.ready_to_meet || 0, 'c-note'],
    ['Closed out', pipe.closed_out || 0, 'c-idle'],
  ];
  if (!total) {
    el.innerHTML = '<p class="muted" style="font-size:13px">No contacts yet. Scout adds them as it finds people.</p>';
    return;
  }
  el.innerHTML =
    '<div class="funnel-bar" role="img" aria-label="Contacts by stage">' +
      stages.filter(s => s[1] > 0).map(s =>
        '<span class="seg ' + s[2] + '" style="flex:' + s[1] + '" title="' + s[0] + ': ' + s[1] + '"></span>').join('') +
    '</div>' +
    '<div class="legend-rows">' + stages.map(s =>
      '<div class="legend-row"><span class="legend-key"><i class="' + s[2] + '"></i>' + s[0] + '</span>' +
      '<span class="legend-val">' + s[1] + '</span>' +
      '<span class="legend-pct">' + Math.round(100 * s[1] / total) + '%</span></div>'
    ).join('') + '</div>';
}

async function loadTodayActivity() {
  const el = document.getElementById('today-activity');
  if (!el) return;
  const data = await api('/api/activity');
  if (!Array.isArray(data)) return;  // offline: Today's queue says so
  // Heartbeats are noise here; the Activity tab keeps them.
  const shown = data.filter(a => a.agent !== 'main').slice(0, 8);
  if (!shown.length) {
    el.innerHTML = '<p class="muted" style="font-size:13px">' +
      'Nothing yet. Every action VeloReach takes shows up here.</p>';
    return;
  }
  el.innerHTML = '<div class="timeline">' + shown.map(a =>
    '<div class="tl-item">' +
      '<span class="tl-time">' + relTime(a.created_at) + '</span>' +
      '<span class="tl-icon">' + icon(own(AGENT_ICONS, a.agent) || 'activity') + '</span>' +
      '<span class="tl-text">' + actionText(a) + '</span>' +
      '<span class="tl-agent">' + escHtml(a.agent) + '</span>' +
    '</div>').join('') + '</div>';
}

function renderTodayWorkflowGuide(stats, items) {
  const guide = document.getElementById('today-workflow-guide');
  if (!guide) return;
  const st = stats || {};
  const needsSetup = (items || []).some(it => String(it.title || '').toLowerCase().includes('placeholder') || String(it.title || '').toLowerCase().includes('company'));
  const cos = Number(st.companies || 0);
  const pros = Number(st.prospects || 0);
  const camps = Number(st.campaigns || 0);
  const pending = Number(st.outbox_pending || 0);
  const pill = (n, tab, title, meta, done) =>
    '<button type="button" class="fg-pill' + (done ? ' done' : '') + '" onclick="goTab(' + jsq(tab) + ')">' +
      '<span class="step-n">' + (done ? '&#10003;' : n) + '</span>' +
      '<span>' + title + '</span>' +
      (meta ? '<span class="fg-meta">' + escHtml(meta) + '</span>' : '') +
    '</button>';
  guide.innerHTML =
    '<span><b>Outreach Workflow:</b></span>' +
    pill(1, 'company', 'Offer &amp; ICP', needsSetup ? 'Setup' : 'Ready', !needsSetup) +
    pill(2, 'discover', 'Discover Leads', cos + ' co', cos > 0) +
    pill(3, 'prospects', 'Tag &amp; Edit Contacts', pros + ' contacts', pros > 0) +
    pill(4, 'campaigns', 'A/B Campaigns', camps + ' seq', camps > 0) +
    pill(5, 'outbox', 'Preview &amp; Approve', pending + ' waiting', pending > 0);
}

function navCount(id, n) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = n > 0 ? n : '';
  el.className = 'nav-count' + (n > 0 ? ' on' : '');
}

// Jump to a tab from a link that isn't itself a nav button.
function goTab(id, skipLoad) {
  const btns = [...document.querySelectorAll('nav button')];
  const btn = btns.find(b => (b.getAttribute('onclick') || '').includes("'" + id + "'"));
  showTab(id, btn, skipLoad);
}

async function loadRuns() {
  const el = document.getElementById('today-runs');
  const block = document.getElementById('today-runs-block');
  if (!el) return;
  const runs = await api('/api/runs');
  if (!Array.isArray(runs) || !runs.length) {
    if (block) block.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  if (block) block.style.display = '';
  el.innerHTML = '<div class="runs">' + runs.slice(0, 5).map(r =>
    '<div class="run">' +
      '<span class="name">' + escHtml(r.stage) + '</span>' + badge(r.status) +
      '<span class="when">' + relTime(r.started_at) + '</span>' +
      '<span class="stats">' + (r.records || 0) + ' records · ' +
        (r.cost_usd ? '$' + Number(r.cost_usd).toFixed(4) : 'free') + '</span>' +
    '</div>').join('') + '</div>';
}

// ── Setup checklist (lives on Today, and disappears once it's done) ──

// Shown once after an upgrade from Harvey moved this install's files.
async function loadRenameNotice() {
  const el = document.getElementById('today-renamed');
  if (!el) return;
  const data = await api('/api/rename-notice');
  if (!data || !data.show) { el.innerHTML = ''; return; }
  el.innerHTML =
    '<div class="card rename-card">' +
      '<div><h3>Harvey is now VeloReach</h3>' +
        '<p class="muted">Same project, new name. Your settings, data and sign-in came with you. ' +
        'The <code>harvey</code> command still works for now; use <code>sellbird</code> from here on.</p></div>' +
      '<div class="rename-actions">' +
        '<a class="btn btn-ghost" href="https://github.com/ethanplusai/sellbird#upgrading-from-harvey" target="_blank" rel="noopener">What changed</a>' +
        '<button class="btn btn-secondary" onclick="dismissRenameNotice()">Got it</button>' +
      '</div></div>';
}

async function dismissRenameNotice() {
  const el = document.getElementById('today-renamed');
  const prev = el ? el.innerHTML : '';
  if (el) el.innerHTML = '';
  const data = await api('/api/rename-notice/dismiss', {method: 'POST'});
  if (!data || !data.success) {
    if (el) el.innerHTML = prev;
    showToast(failMsg(data, 'Could not dismiss notice.'), 'error');
  }
}

async function loadTodayResendCard() {
  const badgeEl = document.getElementById('dash-resend-badge');
  const keyEl = document.getElementById('dash-resend-key');
  const fromEl = document.getElementById('dash-resend-from');
  if (!badgeEl) return;
  try {
    const data = await api('/api/settings');
    if (!data) return;
    const isSmtp = data.provider === 'smtp';
    const hasResend = isSmtp && (data.smtp_host || '').toLowerCase().includes('resend.com') && data.smtp_password_set;
    if (hasResend) {
      badgeEl.className = 'badge t-good';
      badgeEl.textContent = 'Resend Ready (' + (data.smtp_from || 'verified domain') + ')';
    } else if (isSmtp && data.smtp_password_set) {
      badgeEl.className = 'badge t-good';
      badgeEl.textContent = 'SMTP Active (' + (data.smtp_host || 'configured') + ')';
    } else {
      badgeEl.className = 'badge t-waiting';
      badgeEl.textContent = 'Active: ' + (data.provider || 'gmail') + ' · Needs Setup';
    }
    if (fromEl && !fromEl.value) {
      fromEl.value = data.smtp_from || 'onboarding@resend.dev';
    }
    if (keyEl) {
      keyEl.placeholder = data.smtp_password_set ? 'Saved in .env — enter new key to replace' : 're_... (paste your Resend API key)';
    }
  } catch (_) {}
}

function fillDashboardResendSandbox() {
  const fromEl = document.getElementById('dash-resend-from');
  const keyEl = document.getElementById('dash-resend-key');
  if (fromEl) fromEl.value = 'onboarding@resend.dev';
  if (keyEl && !keyEl.value) keyEl.focus();
  showToast('Sandbox sender set to onboarding@resend.dev.', 'info');
}

async function connectResendFromDashboard(btn) {
  const keyEl = document.getElementById('dash-resend-key');
  const fromEl = document.getElementById('dash-resend-from');
  const resEl = document.getElementById('dash-resend-result');
  const apiKey = (keyEl?.value || '').trim();
  const fromAddr = (fromEl?.value || '').trim() || 'onboarding@resend.dev';

  if (btn) btn.disabled = true;
  if (resEl) resEl.innerHTML = '<span class="muted">Saving Resend SMTP, activating provider &amp; testing connection…</span>';
  try {
    const envPayload = {
      SMTP_HOST: 'smtp.resend.com',
      SMTP_PORT: '587',
      SMTP_USERNAME: 'resend',
      SMTP_FROM: fromAddr,
    };
    if (apiKey) envPayload.SMTP_PASSWORD = apiKey;

    const saved = await api('/api/settings/env', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(envPayload),
    });
    if (!saved || !saved.success) {
      const err = failMsg(saved, 'Could not save Resend settings.');
      if (resEl) resEl.innerHTML = '<span class="email-tag invalid">' + escHtml(err) + '</span>';
      showToast(err, 'error');
      return;
    }

    await api('/api/settings/email-provider', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: 'smtp' }),
    });

    const testRes = await api('/api/settings/test-smtp', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        smtp_host: 'smtp.resend.com',
        smtp_port: '587',
        smtp_username: 'resend',
        smtp_password: apiKey,
      }),
    });
    const ok = !!(testRes && testRes.success);
    const msg = (testRes && testRes.message) || (ok ? 'Resend SMTP connected!' : 'Enter a valid Resend API key first.');
    if (resEl) {
      resEl.innerHTML = '<span class="email-tag ' + (ok ? 'verified' : 'invalid') + '">' +
        (ok ? 'Connected' : 'Check Key') + '</span> ' + escHtml(msg);
    }
    showToast(msg, ok ? 'success' : 'error');
    if (ok && keyEl) keyEl.value = '';
    loadTodayResendCard();
    loadSetupStatus();
    if (typeof loadSettings === 'function') loadSettings();
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function loadSetupStatus() {
  const el = document.getElementById('today-setup');
  if (!el) return;
  const data = await api('/api/setup-status');
  if (!data || !data.checks) { el.innerHTML = ''; return; }

  const pct = data.percent || 0;
  if (pct === 100) {
    el.innerHTML =
      '<div class="card setup-card" style="margin-bottom:0;height:100%;display:flex;flex-direction:column;justify-content:center;">' +
        '<div class="setup-head"><div><h3>System Ready</h3>' +
        '<p class="muted">All required pipeline &amp; email checks passed.</p></div>' +
        '<span class="badge t-good">100% Ready</span></div>' +
      '</div>';
    return;
  }

  const next = data.checks.find(c => c.required && !c.done);
  const renderCheck = (c, optional) =>
    '<div class="check-item' + (c === next ? ' current' : '') + '">' +
      (c.done ? '<span class="check-icon done">' + icon('check') + '</span>'
              : '<span class="check-icon pending"></span>') +
      '<div class="check-info">' +
        '<div class="check-label ' + (c.done ? 'done' : '') + '">' + escHtml(c.label) +
          (optional ? '<span class="optional-tag">optional</span>' : '') + '</div>' +
        (!c.done ? '<div class="check-help">' + escHtml(c.help) + '</div>' : '') +
        (!c.done && c.id === 'warmup_service' ? warmupServiceActions(c) : '') +
      '</div></div>';

  const required = data.checks.filter(c => c.required);
  const done = required.filter(c => c.done).length;
  const remaining = data.checks.filter(c => c !== next);

  el.innerHTML =
    '<div class="card setup-card" style="margin-bottom:0;height:100%">' +
      '<div class="setup-head"><div><h3>Setup Checklist</h3>' +
        '<p class="muted">Next recommended action highlighted below.</p></div>' +
        '<span class="setup-pct">' + done + ' / ' + required.length + ' ready</span></div>' +
      '<div class="bar thick" style="margin-bottom:12px"><span style="width:' + pct + '%;background:var(--accent)"></span></div>' +
      (next ? renderCheck(next, false) : '') +
      (remaining.length
        ? '<details style="margin-top:10px"><summary class="muted" style="font-size:12px">View all ' + data.checks.length + ' checks (' + done + ' complete)</summary>' +
          '<div style="margin-top:6px">' + remaining.map(c => renderCheck(c, !c.required)).join('') + '</div></details>'
        : '') +
    '</div>';
}

// The warmup step: get TrulyInbox, paste its key, or say you use another service.
function warmupServiceActions(c) {
  return '<div class="btn-group warmup-actions">' +
      '<a class="btn btn-primary btn-sm" href="' + safeHref(c.link) + '" target="_blank" ' +
        'rel="noopener sponsored">Get TrulyInbox</a>' +
      '<input class="form-input" id="trulyinbox-key" type="password" autocomplete="off" ' +
        'placeholder="Paste your TrulyInbox API key" aria-label="TrulyInbox API key" ' +
        'onkeydown="if (event.key === \'Enter\') connectTrulyInbox()">' +
      '<button class="btn btn-secondary btn-sm" id="trulyinbox-go" onclick="connectTrulyInbox()">Connect</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="useOtherWarmup()">I use another service</button>' +
    '</div>';
}

async function connectTrulyInbox() {
  const input = document.getElementById('trulyinbox-key');
  const go = document.getElementById('trulyinbox-go');
  if (!input || !input.value.trim()) return;
  if (go) go.disabled = true;
  const data = await api('/api/trulyinbox/connect', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key: input.value.trim()}),
  });
  if (go) go.disabled = false;
  if (data && data.success) {
    input.value = '';
    showToast(data.message || 'TrulyInbox connected', 'success');
    loadSetupStatus();
  } else {
    showToast((data && data.message) || 'Could not connect TrulyInbox', 'error');
  }
}

async function useOtherWarmup() {
  const data = await api('/api/trulyinbox/other', {method: 'POST'});
  if (data && data.success) loadSetupStatus();
  else showToast(failMsg(data, 'Could not save warmup preference.'), 'error');
}

// ── Signals: VeloReach proposes, you confirm ──

async function loadSignals() {
  const data = await api('/api/signals');
  const sumEl = document.getElementById('signals-summary');
  const grpEl = document.getElementById('signals-groups');
  if (!data || data.error) { grpEl.innerHTML = offlineState(); sumEl.innerHTML = ''; return; }
  _signals = data;

  const s = data.summary || {};
  navCount('nav-signals', s.proposed || 0);
  const parts = [['Confirmed', s.confirmed || 0, 'c-good'], ['Waiting on you', s.proposed || 0, 'c-wait'],
                 ['Turned off', s.rejected || 0, 'c-idle']];
  const all = parts.reduce((x, p) => x + p[1], 0) || 1;
  sumEl.innerHTML = '<div class="card sig-summary"><div class="funnel-bar">' +
    parts.filter(p => p[1]).map(p => '<span class="seg ' + p[2] + '" style="flex:' + p[1] + '"></span>').join('') +
    '</div><div class="funnel-legend">' + parts.map(p =>
      '<div class="legend-item"><span class="legend-key"><i class="' + p[2] + '"></i>' + p[0] + '</span>' +
      '<span class="legend-val">' + p[1] + '<small>' + Math.round(100 * p[1] / all) + '%</small></span></div>'
    ).join('') + '</div></div>';

  grpEl.innerHTML = (data.groups || []).map(g => {
    // Only the undecided ones: "Confirm all" must never re-enable a signal
    // you turned off (or "Skip all" turn off one you confirmed).
    const pending = escHtml(JSON.stringify(g.signals.filter(x => x.status === 'proposed').map(x => x.code)));
    const undecided = g.signals.filter(x => x.status === 'proposed').length;
    return '<div class="sig-group">' +
      '<div class="sig-group-head"><div>' +
        '<h3>' + escHtml(g.label) + '</h3><p>' + escHtml(g.blurb) + '</p>' +
      '</div>' +
      (undecided
        ? '<div style="display:flex;gap:6px;flex-shrink:0">' +
            '<button class="btn btn-primary btn-sm" onclick="setSignals(' + pending +
              ', \'confirmed\')">Confirm all ' + undecided + '</button>' +
            '<button class="btn btn-secondary btn-sm" onclick="setSignals(' + pending +
              ', \'rejected\')">Skip all ' + undecided + '</button>' +
          '</div>'
        : '') +
      '</div>' +
      '<div class="card flush">' + g.signals.map(sigRow).join('') + '</div>' +
    '</div>';
  }).join('');

  renderCohortBuilder();
}

function sigRow(sig) {
  const free = /free|included/i.test(sig.cost_note || '');
  const costCls = free ? 'free' : (/\$/.test(sig.cost_note || '') ? 'paid' : '');
  const seen = sig.companies
    ? '<span class="cost-chip">seen on ' + sig.companies +
      (sig.companies === 1 ? ' company' : ' companies') + ' so far</span>' : '';
  const floor = sig.confidence_floor > 0
    ? '<span class="cost-chip">only recorded above ' +
      Math.round(sig.confidence_floor * 100) + '% confidence</span>' : '';

  const decide = sig.status === 'confirmed'
    ? '<button class="btn btn-secondary btn-sm" onclick="setSignals([' + jsq(sig.code) +
        '],\'rejected\')">Turn off</button>'
    : sig.status === 'rejected'
      ? '<button class="btn btn-secondary btn-sm" onclick="setSignals([' + jsq(sig.code) +
          '],\'confirmed\')">Turn on</button>'
      : '<button class="btn btn-primary btn-sm" onclick="setSignals([' + jsq(sig.code) +
          '],\'confirmed\')">Confirm</button>' +
        '<button class="btn btn-secondary btn-sm" onclick="setSignals([' + jsq(sig.code) +
          '],\'rejected\')">Skip</button>';

  return '<div class="sig-row ' + (sig.status === 'rejected' ? 'rejected' : '') + '">' +
    '<div class="sig-main">' +
      '<div class="sig-label">' + escHtml(sig.label || sig.code) +
        '<span class="sig-code">' + escHtml(sig.code) + '</span></div>' +
      '<div class="sig-desc">' + escHtml(sig.description) + '</div>' +
      '<div class="sig-meta">' +
        '<span class="cost-chip ' + costCls + '">' + escHtml(sig.cost_note || 'cost unknown') + '</span>' +
        floor + seen + badge(sig.status) +
      '</div>' +
    '</div>' +
    '<div class="sig-decide">' + decide + '</div>' +
  '</div>';
}

async function setSignals(codes, status) {
  const data = await api('/api/signals/status', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({codes: codes, status: status}),
  });
  if (data && data.success) {
    showToast(
      status === 'confirmed'
        ? 'Confirmed ' + data.changed + ' signal' + (data.changed === 1 ? '' : 's') +
          ' — VeloReach will collect ' + (data.changed === 1 ? 'it' : 'them') + ' from now on.'
        : data.changed + ' signal' + (data.changed === 1 ? '' : 's') + ' turned off.',
      'success');
  } else {
    showToast(failMsg(data, 'Could not update that signal.'), 'error');
  }
  loadSignals();
}

// ── Discover: pick a source, price it, then run it ──

let _provider = null;
let _discoverPoll = null;
let _discoverFilter = 'all';

function setDiscoverFilter(mode) {
  _discoverFilter = mode === 'free' ? 'free' : 'all';
  const a = document.getElementById('disc-filter-all');
  const f = document.getElementById('disc-filter-free');
  if (a) a.classList.toggle('on', _discoverFilter === 'all');
  if (f) f.classList.toggle('on', _discoverFilter === 'free');
  loadDiscoverProviders();
}

async function loadDiscoverProviders() {
  const el = document.getElementById('discover-providers');
  const data = await api('/api/discover/providers');
  if (!data || !data.providers) { el.innerHTML = offlineState(); return; }

  const list = _discoverFilter === 'free'
    ? data.providers.filter(p => !p.needs_key)
    : data.providers;
  if (!_provider || (_discoverFilter === 'free' && !list.some(p => p.key === _provider))) {
    _provider = list.some(p => p.key === (data.selected || data.default))
      ? (data.selected || data.default)
      : (list[0]?.key || data.selected || data.default);
  }
  el.innerHTML = '<div class="prov-grid">' + list.map(p => {
    const ready = !p.needs_key
      ? '<span class="badge t-good">$0 Free Keyless</span>'
      : (p.configured
        ? '<span class="badge t-good">Ready</span>'
        : '<span class="badge t-waiting">Needs API key</span>');
    return '<div class="prov-card ' + (p.key === _provider ? 'selected' : '') +
      '" onclick="pickProvider(' + jsq(p.key) + ')">' +
      '<div class="prov-head"><h3>' + escHtml(p.label) + '</h3>' + ready + '</div>' +
      '<div class="blurb">' + escHtml(p.blurb) + '</div>' +
      '<div class="row"><span class="k">Cost</span><span class="v">' +
        escHtml(p.cost_note) + '</span></div>' +
      '<div class="row"><span class="k">Free</span><span class="v">' +
        escHtml(p.free_tier) + '</span></div>' +
      (p.needs_key
        ? '<div class="row"><span class="k">Setup</span><span class="v">' +
          escHtml(p.env_keys.join(', ')) + ' in .env &middot; ' +
          '<a href="' + safeHref(p.signup_url) + '" target="_blank" rel="noopener noreferrer">get a key</a>' +
          '</span></div>'
        : '') +
      (p.caveat ? '<div class="caveat">' + escHtml(p.caveat) + '</div>' : '') +
    '</div>';
  }).join('') + '</div>';

  const running = data.running;
  // Reading sites can't be stopped part-way; only a discovery run can.
  document.getElementById('disc-stop').style.display = running && data.running_kind !== 'profile' ? '' : 'none';
  renderProfileCard(data.unprofiled || 0, running);
  if (running && !_discoverPoll) {
    _discoverPoll = setInterval(loadDiscoverProviders, 4000);
  } else if (!running && _discoverPoll) {
    clearInterval(_discoverPoll);
    _discoverPoll = null;
    // A finished run leaves the button stuck on "Running…" otherwise.
    const btn = document.getElementById('disc-run');
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Find New Leads';
    }
    showToast(data.running_kind === 'profile' ? 'Finished reading websites.' : 'Discovery finished.', 'success');
  }
  renderDiscoverResult(data.last_report, running);
}

function pickProvider(key) {
  _provider = key;
  const btn = document.getElementById('disc-run');
  if (btn) {
    btn.disabled = false;
    btn.textContent = 'Find New Leads';
  }
  document.getElementById('discover-estimate').innerHTML = '';
  loadDiscoverProviders();
}

function pickDiscoverChip(term) {
  const el = document.getElementById('disc-query');
  if (!el) return;
  const cur = el.value.split(/[,;\n]/).map(s => s.trim()).filter(Boolean);
  const low = term.trim().toLowerCase();
  const idx = cur.findIndex(x => x.toLowerCase() === low);
  if (idx >= 0) cur.splice(idx, 1);
  else cur.push(term.trim());
  el.value = cur.join(', ');
  el.focus();
}

function discoverBody() {
  const rawCities = (document.getElementById('disc-cities')?.value || '').trim();
  const rawQuery = (document.getElementById('disc-query')?.value || '').trim();
  const terms = rawQuery ? rawQuery.split(/[,;\n]/).map(t => t.trim()).filter(Boolean) : [];
  return {
    provider: _provider,
    query: rawQuery,
    terms,
    // Semicolons or newlines, never commas: "Denver, CO" is one city.
    cities: rawCities ? rawCities.split(/[;\n]/).map(c => c.trim()).filter(Boolean) : [],
    depth: Number(document.getElementById('disc-depth')?.value) || 30,
    limit: Number(document.getElementById('disc-limit')?.value) || 50,
    max_spend: Number(document.getElementById('disc-cap')?.value) || 1,
    preset: (document.getElementById('disc-preset') || {}).value || '',
  };
}

async function estimateDiscovery() {
  const el = document.getElementById('discover-estimate');
  el.innerHTML = '<p class="muted" style="font-size:13px">Pricing it…</p>';
  const data = await api('/api/discover/estimate', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(discoverBody()),
  });
  if (!data || data.error) {
    el.innerHTML = '<div class="test-result error">' +
      escHtml((data && data.error) || 'Could not estimate.') + '</div>';
    return;
  }

  const cap = discoverBody().max_spend;
  const over = data.estimated_cost > cap;
  el.innerHTML = '<div class="estimate-box">' +
    '<div class="amount ' + (data.free ? 'free' : 'paid') + '">' +
      (data.free ? 'Free' : '$' + data.estimated_cost.toFixed(4)) + '</div>' +
    '<div class="muted" style="font-size:13px;margin-top:2px">' +
      data.query_count + ' quer' + (data.query_count === 1 ? 'y' : 'ies') +
      (data.free ? '' : ' &middot; cap is $' + cap.toFixed(2)) + '</div>' +
    (over ? '<div class="test-result error" style="margin-top:12px">' +
      'Over your cap. Raise the cap or narrow the search.</div>' : '') +
    '<div class="query-list">' +
      data.queries.slice(0, 40).map(escHtml).join('<br>') +
      (data.queries.length > 40 ? '<br>… and ' + (data.queries.length - 40) + ' more' : '') +
    '</div></div>';

  const btn = document.getElementById('disc-run');
  btn.disabled = over;
  btn.textContent = over ? 'Over cap'
    : (data.free ? 'Run — free' : 'Run — spend up to $' + data.estimated_cost.toFixed(2));
}

async function runDiscovery() {
  const btn = document.getElementById('disc-run');
  const body = discoverBody();
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Checking cost…';
  }
  const est = await api('/api/discover/estimate', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  if (!est || est.error) {
    showToast((est && est.error) || 'Enter a valid business query or city first.', 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Find New Leads'; }
    return;
  }
  if (est.estimated_cost > body.max_spend) {
    await estimateDiscovery();
    showToast('Estimated cost exceeds spend cap ($' + body.max_spend.toFixed(2) + ').', 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Find New Leads'; }
    return;
  }
  if (btn) btn.textContent = 'Running…';
  const data = await api('/api/discover/run', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  if (data && data.success) {
    showToast('Discovery started — ' + data.queries + ' queries.', 'success');
  } else {
    showToast((data && data.message) || 'Could not start.', 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Find New Leads'; }
  }
  loadDiscoverProviders();
}

async function stopDiscovery() {
  const data = await api('/api/discover/stop', {method: 'POST'});
  showToast(data && data.success ? 'Stopping after the current query.'
                                 : failMsg(data, 'Could not stop.'), data && data.success ? 'success' : 'error');
  loadDiscoverProviders();
}

// Companies found but not looked at: reading their sites is free and
// model-free, and it is what makes an email specific.
function renderProfileCard(pending, running) {
  const el = document.getElementById('discover-profile');
  if (!el) return;
  if (!pending) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="subhead">Read websites</div><div class="card"><div class="queue-item good" style="padding:0;border:0;box-shadow:none">' +
    '<div class="qtext"><div class="qtitle">' + pending + ' compan' + (pending === 1 ? 'y' : 'ies') +
    ' not looked at yet</div><div class="qdetail">Free, no account needed. VeloReach reads each site for ' +
    'who their agency is, what they are missing, and whether they run ads. Up to 200 per run.</div></div>' +
    '<button class="btn btn-primary btn-sm" onclick="runProfile(this)"' + (running ? ' disabled' : '') + '>' +
    (running ? 'Busy…' : 'Read their sites') + '</button></div></div>';
}

async function runProfile(btn) {
  btn.disabled = true;
  btn.textContent = 'Starting…';
  const data = await api('/api/profile/run', {method: 'POST'});
  if (data && data.success) {
    showToast(data.pending ? 'Reading ' + Math.min(data.pending, 200) + ' websites.' : 'Every site is already read.', 'success');
    if (data.pending && !_discoverPoll) _discoverPoll = setInterval(loadDiscoverProviders, 4000);
  } else {
    showToast(failMsg(data, 'Could not start.'), 'error');
  }
  loadDiscoverProviders();
}

function renderDiscoverResult(report, running) {
  const el = document.getElementById('discover-result');
  if (running) {
    el.innerHTML = '<div class="card"><h2>Running…</h2>' +
      '<p class="muted" style="font-size:13px">VeloReach is working through the ' +
      'queries. Results land in Companies, and the run log is on Today.</p></div>';
    return;
  }
  if (!report) { el.innerHTML = ''; return; }

  const stat = (label, value, tone) =>
    '<div class="stat-card"><div class="label">' + label + '</div>' +
    '<div class="value"' + (tone ? ' style="color:var(--' + tone + ')"' : '') + '>' +
    value + '</div></div>';

  if (report.profiled_companies !== undefined && report.new_companies === undefined) {
    el.innerHTML = '<div class="subhead">Last run</div><div class="stats-grid">' +
      stat('Websites read', report.profiled_companies || 0, 'accent') +
      stat('Facts recorded', report.profile_observations || 0) +
      stat('Cost', 'free') + '</div>' +
      (report.stopped ? '<div class="test-result error" style="margin-top:14px">Stopped early: ' + escHtml(report.stopped) + '</div>' : '');
    return;
  }
  el.innerHTML = '<div class="subhead">Last run</div>' +
    '<div class="stats-grid">' +
      stat('New companies', report.new_companies || 0, 'accent') +
      stat('Already known', report.known_companies || 0) +
      stat('Observations', report.observations || 0) +
      stat('Filtered as junk', report.junk || 0) +
      (report.profiled_companies ? stat('Websites read', report.profiled_companies) : '') +
      stat('Actual cost', report.actual_cost ? '$' + report.actual_cost.toFixed(4) : 'free') +
    '</div>' +
    (report.stopped ? '<div class="test-result error" style="margin-top:14px">' +
      'Stopped early: ' + escHtml(report.stopped) + '</div>' : '') +
    ((report.errors || []).length
      ? '<div class="card" style="margin-top:14px"><h2>Problems</h2>' +
        (report.errors || []).slice(0, 8).map(e =>
          '<div class="check-help">' + escHtml(e) + '</div>').join('') + '</div>'
      : '');
}

// ── Cohort builder: a prospect list is a query ──

let _cohort = { require: new Set(), exclude: new Set(), ranges: {} };
let _cohortOffset = 0;
let _cohortGeneration = 0;
const COHORT_PAGE = 50;

function renderCohortBuilder() {
  const el = document.getElementById('cohort-builder');
  if (!el || !_signals) return;

  const confirmed = (_signals.groups || [])
    .flatMap(g => g.signals)
    .filter(s => s.status === 'confirmed');

  if (!confirmed.length) {
    el.innerHTML = '<p class="muted" style="font-size:13px">' +
      'Confirm some signals above and they become the building blocks here.</p>';
    document.getElementById('cohort-result').innerHTML = '';
    return;
  }

  const col = (title, key, hint) =>
    '<div><div class="subhead" style="margin-top:0">' + title +
      ' <span class="muted" style="font-weight:400;text-transform:none;letter-spacing:0">' +
      hint + '</span></div><div class="cohort-grid">' +
    confirmed.map(s =>
      '<label class="cohort-pick"><input type="checkbox" ' +
        (_cohort[key].has(s.code) ? 'checked ' : '') +
        'onchange="toggleCohort(\'' + key + '\',' + jsq(s.code) + ',this.checked)">' +
        '<span>' + escHtml(s.label || s.code) + '</span>' +
        '<span class="n">' + (s.companies || 0) + '</span></label>'
    ).join('') + '</div></div>';

  // Numeric signals (ranks, ratings) take a range: "organic rank 11-30".
  const numeric = confirmed.filter(s => s.value_type === 'num');
  const bound = (code, side) => {
    const v = (_cohort.ranges[code] || {})[side];
    return '<input type="number" class="cohort-bound" placeholder="' + side + '" ' +
      'value="' + (v === undefined || v === null ? '' : escHtml(String(v))) + '" ' +
      'aria-label="' + escHtml(code) + ' ' + side + '" ' +
      'onchange="setCohortRange(' + jsq(code) + ',' + jsq(side) + ',this.value)">';
  };
  const rangeCol = numeric.length ?
    '<div style="height:18px"></div><div><div class="subhead" style="margin-top:0">In range ' +
      '<span class="muted" style="font-weight:400;text-transform:none;letter-spacing:0">' +
      '&mdash; e.g. organic rank 11 to 30; leave a side blank for no limit</span></div>' +
      '<div class="cohort-grid">' +
      numeric.map(s =>
        '<div class="cohort-pick cohort-range"><span>' + escHtml(s.label || s.code) + '</span>' +
          bound(s.code, 'min') + '<span class="muted">to</span>' + bound(s.code, 'max') + '</div>'
      ).join('') + '</div></div>' : '';

  el.innerHTML = col('Must have', 'require', '&mdash; every company in the cohort carries all of these') +
    '<div style="height:18px"></div>' +
    col('Must not have', 'exclude', '&mdash; disqualifiers') + rangeCol;
}

function setCohortRange(code, side, value) {
  const r = _cohort.ranges[code] || {min: null, max: null};
  r[side] = value === '' || isNaN(Number(value)) ? null : Number(value);
  if (r.min === null && r.max === null) delete _cohort.ranges[code];
  else _cohort.ranges[code] = r;
  _cohortOffset = 0;
  runCohort();
}

function toggleCohort(key, code, on) {
  if (on) _cohort[key].add(code); else _cohort[key].delete(code);
  // A company can't be both required and excluded on the same signal.
  const other = key === 'require' ? 'exclude' : 'require';
  if (on) _cohort[other].delete(code);
  _cohortOffset = 0;  // a new cohort starts on its first page
  runCohort();
  renderCohortBuilder();
}

async function runCohort() {
  const generation = ++_cohortGeneration;
  const offset = _cohortOffset;
  const el = document.getElementById('cohort-result');
  if (!el) return;
  const require = [..._cohort.require], exclude = [..._cohort.exclude];
  const ranges = _cohortRanges();
  if (!require.length && !ranges.length) { el.innerHTML = ''; return; }
  // Remove the prior export button as soon as its criteria become stale.
  el.innerHTML = '<p class="muted">Loading cohort…</p>';

  const data = await api('/api/cohort', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({require: require, exclude: exclude, ranges: ranges,
                          offset: offset, limit: COHORT_PAGE}),
  });
  if (generation !== _cohortGeneration) return;
  if (!data || data.error) {
    el.innerHTML = data ? '<div class="test-result error">' + escHtml(data.error) + '</div>' : '';
    return;
  }

  let html = '<div style="border-top:1px solid var(--line);margin-top:20px;padding-top:20px">' +
    '<div class="cohort-size">' + data.size + '</div>' +
    '<div class="muted" style="font-size:13px;margin-top:2px">compan' +
      (data.size === 1 ? 'y matches' : 'ies match') + ' this cohort right now</div>';

  if (data.companies && data.companies.length) {
    html += '<div class="table-card" style="margin-top:16px"><table><thead><tr>' +
      '<th>Company</th><th>Domain</th><th>Industry</th><th>Location</th>' +
      '</tr></thead><tbody>' +
      data.companies.map(c =>
        '<tr><td>' + escHtml(c.name) + '</td><td class="muted">' + escHtml(c.domain) + '</td>' +
        '<td class="muted">' + escHtml(c.industry) + '</td>' +
        '<td class="muted">' + escHtml(c.location) + '</td></tr>'
      ).join('') + '</tbody></table></div>';

    const first = data.offset + 1, last = data.offset + data.companies.length;
    const hasPrev = data.offset > 0, hasNext = last < data.size;
    html += '<div style="display:flex;align-items:center;gap:8px;margin-top:10px">' +
      '<span class="muted" style="font-size:12px">Showing ' + first + '&ndash;' + last +
        ' of ' + data.size + '</span>' +
      '<span style="flex:1"></span>' +
      '<button class="btn btn-secondary btn-sm" onclick="pageCohort(-1)"' +
        (hasPrev ? '' : ' disabled') + '>Prev</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="pageCohort(1)"' +
        (hasNext ? '' : ' disabled') + '>Next</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="exportCohort(this)">Export CSV</button>' +
      '</div>';
  } else if (data.size === 0) {
    html += '<p class="muted" style="font-size:13px;margin-top:12px">' +
      'No company carries all of those yet. Either loosen the cohort, or run ' +
      'prospecting to collect more.</p>';
  } else if (_cohortOffset > 0) {
    // The cohort shrank under us (a re-profile, a delete): back to page one.
    _cohortOffset = 0;
    return runCohort();
  }
  el.innerHTML = html + '</div>';
}

function _cohortRanges() {
  return Object.entries(_cohort.ranges || {})
    .filter(([, r]) => r.min !== null || r.max !== null)
    .map(([code, r]) => ({code: code, min: r.min, max: r.max}));
}

async function exportCohort(button) {
  const qs = new URLSearchParams({require: [..._cohort.require].join(','),
                                  exclude: [..._cohort.exclude].join(',')});
  // Ranges travel as "CODE:min:max", a blank side meaning no limit.
  const ranges = Object.entries(_cohort.ranges || {})
    .map(([code, r]) => code + ':' + (r.min ?? '') + ':' + (r.max ?? ''));
  if (ranges.length) qs.set('ranges', ranges.join(','));
  button.disabled = true;
  button.textContent = 'Exporting…';
  let url;
  let link;
  try {
    const response = await authFetch('/api/cohort.csv?' + qs);
    if (await lockIfRefused(response)) throw new Error('Dashboard authorization required');
    if (!response.ok) throw new Error('Export failed (HTTP ' + response.status + ')');
    url = URL.createObjectURL(await response.blob());
    link = document.createElement('a');
    link.href = url;
    link.download = 'cohort.csv';
    document.body.appendChild(link);
    link.click();
    button.textContent = 'Export CSV';
  } catch (error) {
    button.textContent = 'Export failed — retry';
  } finally {
    if (link) link.remove();
    // Give the browser time to start consuming the download before revoking.
    if (url) setTimeout(() => URL.revokeObjectURL(url), 1000);
    button.disabled = false;
  }
}

function pageCohort(dir) {
  _cohortOffset = Math.max(0, _cohortOffset + dir * COHORT_PAGE);
  runCohort();
}

// ── Settings ──

async function loadSettings() {
  const data = await api('/api/settings');
  if (!data) return;

  // Active provider indicator
  const prov = data.provider || 'instantly';
  const provEl = document.getElementById('active-provider');
  if (provEl) provEl.textContent = prov;
  ['gmail', 'smtp', 'microsoft', 'instantly'].forEach(p => {
    const c = document.getElementById('ep-' + p);
    if (c) c.classList.toggle('on', p === prov);
  });

  // Secret fields never echo a value — show a "saved" placeholder instead.
  const savedPh = (id, isSet, base) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = '';
    el.placeholder = isSet ? 'Saved — enter new value to change' : base;
  };
  const tag = (id, ok, okLabel) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = 'email-tag ' + (ok ? 'verified' : 'guess');
    el.textContent = ok ? okLabel : 'not set';
  };

  // Non-secret values repopulate
  document.getElementById('gmail-id').value = data.gmail_client_id || '';
  document.getElementById('smtp-host').value = data.smtp_host || '';
  document.getElementById('smtp-port').value = data.smtp_port || '';
  document.getElementById('smtp-user').value = data.smtp_username || '';
  const smtpFromEl = document.getElementById('smtp-from');
  if (smtpFromEl) smtpFromEl.value = data.smtp_from || '';
  document.getElementById('imap-host').value = data.imap_host || '';
  document.getElementById('imap-port').value = data.imap_port || '';
  document.getElementById('imap-user').value = data.imap_username || '';
  document.getElementById('m365-client').value = data.m365_client_id || '';
  document.getElementById('m365-tenant').value = data.m365_tenant_id || '';
  document.getElementById('linkedin-email').value = data.linkedin_email || '';
  document.getElementById('cf-account-id').value = data.cloudflare_account_id || '';

  savedPh('gmail-secret', data.gmail_client_secret_set, 'Enter client secret');
  savedPh('smtp-pass', data.smtp_password_set, 'Enter password / app password');
  savedPh('imap-pass', data.imap_password_set, 'Same as SMTP password');
  document.getElementById('imap-same-pass').checked = !data.imap_password_set;
  savedPh('instantly-key', data.instantly_api_key_set, 'Enter your Instantly API key');
  savedPh('reoon-key', data.reoon_api_key_set, '600 free/mo — reoon.com/email-verifier');
  savedPh('zerobounce-key', data.zerobounce_api_key_set, '100 free/mo — best for M365/Workspace catch-alls');
  savedPh('hunter-key', data.hunter_api_key_set, '50 free/mo + email-pattern lookup');
  savedPh('linkedin-password', data.linkedin_password_set, 'Enter password');
  savedPh('cf-api-token', data.cloudflare_api_token_set, 'Your Cloudflare API Token');

  // Gmail auth status chip
  const g = document.getElementById('gmail-status');
  if (g) {
    const realId = /^\d+-[0-9a-z]+\.apps\.googleusercontent\.com$/.test((data.gmail_client_id || '').trim());
    if (data.gmail_client_id && !realId) { g.className = 'email-tag invalid'; g.textContent = 'client ID isn\'t real'; }
    else if (data.gmail_authorized) { g.className = 'email-tag verified'; g.textContent = 'authorized'; }
    else if (data.gmail_client_secret_set) { g.className = 'email-tag risky'; g.textContent = 'sign in on Inboxes'; }
    else { g.className = 'email-tag guess'; g.textContent = 'not set up'; }
  }
  const m = document.getElementById('m365-status');
  if (m) {
    const realM = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test((data.m365_client_id || '').trim());
    if (data.m365_client_id && !realM) { m.className = 'email-tag invalid'; m.textContent = 'client ID isn\'t real'; }
    else if (data.m365_authorized) { m.className = 'email-tag verified'; m.textContent = 'authorized'; }
    else if (data.m365_client_id) { m.className = 'email-tag risky'; m.textContent = 'sign in on Inboxes'; }
    else { m.className = 'email-tag guess'; m.textContent = 'not set up'; }
  }
  tag('reoon-status', data.reoon_api_key_set, 'set');
  tag('zerobounce-status', data.zerobounce_api_key_set, 'set');
  tag('hunter-status', data.hunter_api_key_set, 'set');

  // AI & Universal endpoints
  const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.value = val || ''; };
  setVal('openai-base-url', data.openai_base_url);
  setVal('openai-model', data.openai_model);
  setVal('searxng-url', data.searxng_url);
  setVal('twenty-url', data.twenty_base_url);
  setVal('telegram-chat', data.telegram_chat_id);
  setVal('calcom-event-type-id', data.calcom_event_type_id);

  savedPh('openai-key', data.openai_api_key_set, 'API key (or sk-none)');
  savedPh('openrouter-key', data.openrouter_api_key_set, 'sk-or-v1-...');
  savedPh('deepseek-key', data.deepseek_api_key_set, 'sk-...');
  savedPh('gemini-key', data.gemini_api_key_set, 'AIzaSy...');
  savedPh('xai-key', data.xai_api_key_set, 'xai-...');
  savedPh('groq-key', data.groq_api_key_set, 'gsk_...');

  savedPh('tavily-key', data.tavily_api_key_set, 'tvly-...');
  savedPh('firecrawl-key', data.firecrawl_api_key_set, 'fc-...');
  savedPh('serper-key', data.serper_api_key_set, 'Enter key');
  savedPh('calcom-key', data.calcom_api_key_set, 'cal_live_...');
  savedPh('calcom-webhook-secret', data.calcom_webhook_secret_set, 'Enter webhook signing secret');
  savedPh('twenty-key', data.twenty_api_key_set, 'Enter API key');
  savedPh('telegram-token', data.telegram_bot_token_set, 'bot123456:ABC-DEF...');
  savedPh('slack-webhook', data.slack_webhook_url_set, 'https://hooks.slack.com/services/...');

  tag('openrouter-status', data.openrouter_api_key_set, 'set');
  tag('deepseek-status', data.deepseek_api_key_set, 'set');
  tag('gemini-status', data.gemini_api_key_set, 'set');
  tag('xai-status', data.xai_api_key_set, 'set');
  tag('groq-status', data.groq_api_key_set, 'set');
  await loadComplianceSettings();
}

async function loadComplianceSettings() {
  if (!document.getElementById('compliance-card')) return;
  const data = await api('/api/settings/compliance');
  if (!data || data.error) return;
  const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.value = val == null ? '' : String(val); };
  const setChk = (id, val) => { const el = document.getElementById(id); if (el) el.checked = !!val; };
  setVal('comp-postal', data.postal_address || '');
  setVal('comp-optout', data.opt_out_line || '');
  setVal('comp-from-name', data.from_name || '');
  setVal('comp-daily-limit', data.daily_limit ?? 50);
  setVal('comp-inbox-cap', data.per_inbox_daily_cap ?? 30);
  setChk('comp-require-approval', data.require_approval !== false);
  setChk('comp-send-risky', !!data.send_to_risky);

  const wu = data.warmup || {};
  setVal('comp-wu-start', wu.start_date || '');
  setVal('comp-wu-min', wu.min_days ?? 14);
  setVal('comp-wu-initial', wu.start_sends ?? 5);
  setVal('comp-wu-inc', wu.daily_increase ?? 3);

  const sw = data.send_window || {};
  setChk('comp-sw-enabled', !!sw.enabled);
  setVal('comp-sw-start', sw.start || '08:00');
  setVal('comp-sw-end', sw.end || '17:00');
  setVal('comp-sw-days', sw.days || 'mon-fri');
  setVal('comp-sw-tz', sw.timezone || 'America/New_York');

  const qh = data.quiet_hours || {};
  setChk('comp-qh-enabled', qh.enabled !== false);
  setVal('comp-qh-start', qh.start || '22:00');
  setVal('comp-qh-end', qh.end || '07:00');
  setVal('comp-qh-tz', qh.timezone || 'America/New_York');
}

async function saveComplianceSettings() {
  const val = id => String((document.getElementById(id) || {}).value ?? '').trim();
  const chk = id => !!(document.getElementById(id) || {}).checked;
  const btn = document.getElementById('comp-save-btn');
  const limitRaw = val('comp-daily-limit');
  const dailyLimit = limitRaw === '' ? 50 : Number(limitRaw);
  const inboxCapRaw = val('comp-inbox-cap');
  const inboxCap = inboxCapRaw === '' ? 30 : Number(inboxCapRaw);
  const payload = {
    postal_address: val('comp-postal'),
    opt_out_line: val('comp-optout'),
    from_name: val('comp-from-name'),
    daily_limit: Number.isFinite(dailyLimit) ? Math.trunc(dailyLimit) : -1,
    per_inbox_daily_cap: Number.isFinite(inboxCap) ? Math.trunc(inboxCap) : 30,
    require_approval: chk('comp-require-approval'),
    send_to_risky: chk('comp-send-risky'),
    warmup: {
      start_date: val('comp-wu-start'),
      min_days: Math.max(0, Number(val('comp-wu-min') || 14)),
      start_sends: Math.max(1, Number(val('comp-wu-initial') || 5)),
      daily_increase: Math.max(1, Number(val('comp-wu-inc') || 3)),
    },
    send_window: {
      enabled: chk('comp-sw-enabled'),
      start: val('comp-sw-start') || '08:00',
      end: val('comp-sw-end') || '17:00',
      days: val('comp-sw-days') || 'mon-fri',
      timezone: val('comp-sw-tz') || 'America/New_York',
    },
    quiet_hours: {
      enabled: chk('comp-qh-enabled'),
      start: val('comp-qh-start') || '22:00',
      end: val('comp-qh-end') || '07:00',
      timezone: val('comp-qh-tz') || 'America/New_York',
    },
  };
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/settings/compliance', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if (res && res.success) {
      showToast('Compliance & schedule saved.', 'success');
      await loadComplianceSettings();
      loadSendingModeCard();
    } else {
      showToast(failMsg(res, 'Could not save compliance settings.'), 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function saveUniversalBrain() {
  const payload = {
    OPENAI_BASE_URL: (document.getElementById('openai-base-url') || {}).value.trim(),
    OPENAI_MODEL: (document.getElementById('openai-model') || {}).value.trim(),
  };
  const key = (document.getElementById('openai-key') || {}).value;
  if (key) payload.OPENAI_API_KEY = key;
  saveEnv(payload, 'Universal OpenAI-compatible brain saved.').then(loadSettings);
}

function saveCloudModelKeys() {
  const payload = {};
  const or = (document.getElementById('openrouter-key') || {}).value;
  const ds = (document.getElementById('deepseek-key') || {}).value;
  const gm = (document.getElementById('gemini-key') || {}).value;
  const xai = (document.getElementById('xai-key') || {}).value;
  const gr = (document.getElementById('groq-key') || {}).value;
  if (or) payload.OPENROUTER_API_KEY = or;
  if (ds) payload.DEEPSEEK_API_KEY = ds;
  if (gm) payload.GEMINI_API_KEY = gm;
  if (xai) payload.XAI_API_KEY = xai;
  if (gr) payload.GROQ_API_KEY = gr;
  if (!Object.keys(payload).length) { showToast('No keys entered. Existing keys preserved.', 'info'); return; }
  saveEnv(payload, 'Cloud model keys updated.').then(loadSettings);
}

function saveSearchTools() {
  const payload = {
    SEARXNG_URL: (document.getElementById('searxng-url') || {}).value.trim(),
  };
  const tv = (document.getElementById('tavily-key') || {}).value;
  const fc = (document.getElementById('firecrawl-key') || {}).value;
  const sp = (document.getElementById('serper-key') || {}).value;
  if (tv) payload.TAVILY_API_KEY = tv;
  if (fc) payload.FIRECRAWL_API_KEY = fc;
  if (sp) payload.SERPER_API_KEY = sp;
  saveEnv(payload, 'Search & discovery tools updated.').then(loadSettings);
}

function saveIntegrations() {
  const payload = {
    TWENTY_BASE_URL: ((document.getElementById('twenty-url') || {}).value || '').trim(),
    TELEGRAM_CHAT_ID: ((document.getElementById('telegram-chat') || {}).value || '').trim(),
    CALCOM_EVENT_TYPE_ID: ((document.getElementById('calcom-event-type-id') || {}).value || '').trim(),
  };
  const cc = (document.getElementById('calcom-key') || {}).value;
  const cws = (document.getElementById('calcom-webhook-secret') || {}).value;
  const tw = (document.getElementById('twenty-key') || {}).value;
  const tg = (document.getElementById('telegram-token') || {}).value;
  const sl = (document.getElementById('slack-webhook') || {}).value;
  if (cc) payload.CALCOM_API_KEY = cc;
  if (cws) payload.CALCOM_WEBHOOK_SECRET = cws;
  if (tw) payload.TWENTY_API_KEY = tw;
  if (tg) payload.TELEGRAM_BOT_TOKEN = tg;
  if (sl) payload.SLACK_WEBHOOK_URL = sl;
  saveEnv(payload, 'Integrations & alerts updated.').then(loadSettings);
}

async function testIntegrationConnection(btn) {
  const service = ((document.getElementById('integration-test-service') || {}).value || 'telegram').trim();
  const resultEl = document.getElementById('integration-test-result');
  const sendPing = !!(document.getElementById('integration-test-ping') || {}).checked;
  const payload = { service };
  const val = id => ((document.getElementById(id) || {}).value || '').trim();
  if (service === 'telegram') {
    const tok = val('telegram-token');
    const cid = val('telegram-chat');
    if (tok) payload.telegram_bot_token = tok;
    if (cid) payload.telegram_chat_id = cid;
  } else if (service === 'slack') {
    const hook = val('slack-webhook');
    if (hook) payload.slack_webhook_url = hook;
    payload.send_test = sendPing;
  } else if (service === 'calcom') {
    const key = val('calcom-key');
    if (key) payload.calcom_api_key = key;
  } else if (service === 'twenty') {
    const url = val('twenty-url');
    const key = val('twenty-key');
    if (url) payload.twenty_base_url = url;
    if (key) payload.twenty_api_key = key;
  }
  if (btn) btn.disabled = true;
  if (resultEl) resultEl.textContent = 'Testing connection…';
  try {
    const res = await api('/api/integrations/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const ok = !!(res && res.success);
    const msg = (res && res.message) || 'Connection test failed.';
    if (resultEl) {
      resultEl.innerHTML = '<span class="email-tag ' + (ok ? 'verified' : 'invalid') + '">' +
        (ok ? 'Connected' : 'Failed') + '</span> ' + escHtml(msg);
    }
    showToast(msg, ok ? 'success' : 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function saveGmail() {
  const payload = {GMAIL_CLIENT_ID: document.getElementById('gmail-id').value.trim()};
  const sec = document.getElementById('gmail-secret').value;
  if (sec) payload.GMAIL_CLIENT_SECRET = sec;
  saveEnv(payload, 'Saved. Now sign in on the Inboxes tab.').then(loadSettings);
}

function jumpSettings(targetId) {
  const el = document.getElementById(targetId);
  if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function switchEmailProvider(provider) {
  try {
    const res = await api('/api/settings/email-provider', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider }),
    });
    if (res && res.success) {
      showToast('Active email provider switched to ' + res.provider + '.', 'success');
      loadSettings();
    } else {
      showToast(failMsg(res, 'Could not switch email provider.'), 'error');
    }
  } catch (e) {
    showToast('Could not switch email provider.', 'error');
  }
}

function fillResendPreset() {
  const hostEl = document.getElementById('smtp-host');
  const portEl = document.getElementById('smtp-port');
  const userEl = document.getElementById('smtp-user');
  const fromEl = document.getElementById('smtp-from');
  const passEl = document.getElementById('smtp-pass');
  const imapHostEl = document.getElementById('imap-host');
  if (hostEl) hostEl.value = 'smtp.resend.com';
  if (portEl) portEl.value = '587';
  if (userEl) userEl.value = 'resend';
  if (fromEl && !fromEl.value) fromEl.value = 'onboarding@resend.dev';
  if (imapHostEl) imapHostEl.value = '';
  if (passEl) {
    passEl.placeholder = 'Paste Resend API key (re_...)';
    passEl.focus();
  }
  showToast('Resend Sandbox SMTP preset loaded (From: onboarding@resend.dev). Paste your re_... API key, then click Save & Activate SMTP.', 'info');
}

async function saveSmtp(activateProvider) {
  const payload = {
    SMTP_HOST: document.getElementById('smtp-host').value.trim(),
    SMTP_PORT: document.getElementById('smtp-port').value.trim(),
    SMTP_USERNAME: document.getElementById('smtp-user').value.trim(),
    SMTP_FROM: (document.getElementById('smtp-from')?.value || '').trim(),
  };
  const pass = document.getElementById('smtp-pass').value;
  if (pass) payload.SMTP_PASSWORD = pass;
  // Blank IMAP fields fall back to the SMTP ones. A typed IMAP password wins;
  // otherwise the checkbox clears it back to the SMTP one, and an unticked
  // empty box keeps whatever IMAP password is saved.
  payload.IMAP_HOST = document.getElementById('imap-host').value.trim();
  payload.IMAP_PORT = document.getElementById('imap-port').value.trim();
  payload.IMAP_USERNAME = document.getElementById('imap-user').value.trim();
  const imapPass = document.getElementById('imap-pass').value;
  if (imapPass) payload.IMAP_PASSWORD = imapPass;
  else if (document.getElementById('imap-same-pass').checked) payload.IMAP_PASSWORD = '';
  await saveEnv(payload, activateProvider ? 'SMTP settings saved.' : 'SMTP credentials saved.');
  if (activateProvider) {
    try {
      await api('/api/settings/email-provider', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({provider: 'smtp'}),
      });
      showToast('Active email provider switched to SMTP.', 'success');
    } catch (_) {}
  }
  loadSettings();
}

async function testSmtpConnection(btn) {
  const statusEl = document.getElementById('smtp-test-result');
  const payload = {
    smtp_host: (document.getElementById('smtp-host')?.value || '').trim(),
    smtp_port: (document.getElementById('smtp-port')?.value || '').trim(),
    smtp_username: (document.getElementById('smtp-user')?.value || '').trim(),
    smtp_password: document.getElementById('smtp-pass')?.value || '',
    imap_host: (document.getElementById('imap-host')?.value || '').trim(),
    imap_port: (document.getElementById('imap-port')?.value || '').trim(),
    imap_username: (document.getElementById('imap-user')?.value || '').trim(),
    imap_password: document.getElementById('imap-pass')?.value || '',
    imap_same_pass: !!document.getElementById('imap-same-pass')?.checked,
  };
  if (btn) btn.disabled = true;
  if (statusEl) {
    statusEl.className = 'muted';
    statusEl.textContent = 'Testing SMTP + IMAP...';
  }
  try {
    const res = await api('/api/settings/test-smtp', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const ok = !!(res && res.success);
    const msg = (res && res.message) || 'Could not reach SMTP test endpoint.';
    if (statusEl) {
      statusEl.className = ok ? 'email-tag verified' : 'email-tag invalid';
      statusEl.textContent = msg;
    }
    showToast(msg, ok ? 'success' : 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function saveMicrosoft() {
  const client = document.getElementById('m365-client').value.trim();
  if (!client) { showToast('Paste the Application (client) ID first.', 'error'); return; }
  saveEnv({M365_CLIENT_ID: client, M365_TENANT_ID: document.getElementById('m365-tenant').value.trim()},
          'Saved. Now sign in on the Inboxes tab.').then(loadSettings);
}

function saveVerifiers() {
  const payload = {};
  const r = document.getElementById('reoon-key').value;
  const z = document.getElementById('zerobounce-key').value;
  const h = document.getElementById('hunter-key').value;
  if (r) payload.REOON_API_KEY = r;
  if (z) payload.ZEROBOUNCE_API_KEY = z;
  if (h) payload.HUNTER_API_KEY = h;
  if (!Object.keys(payload).length) { showToast('Enter at least one key first.', 'error'); return; }
  saveEnv(payload, 'Verification keys saved.').then(loadSettings);
}

async function saveEnv(payload, okMsg) {
  const data = await api('/api/settings/env', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  if (data && data.success) showToast(okMsg, 'success');
  else showToast((data && data.message) || 'Save failed — is the dashboard still running?', 'error');
}

// Secrets are only sent when typed: an empty box means "keep the saved one".
function saveInstantly() {
  const key = document.getElementById('instantly-key').value.trim();
  if (!key) { showToast('Enter a new key first. The saved one is kept.', 'error'); return; }
  saveEnv({INSTANTLY_API_KEY: key}, 'Instantly API key saved.').then(loadSettings);
}

function saveLinkedIn() {
  const payload = {LINKEDIN_EMAIL: document.getElementById('linkedin-email').value.trim()};
  const pass = document.getElementById('linkedin-password').value;
  if (pass) payload.LINKEDIN_PASSWORD = pass;
  saveEnv(payload, 'LinkedIn credentials saved.').then(loadSettings);
}

function saveCloudflare() {
  const payload = {CLOUDFLARE_ACCOUNT_ID: document.getElementById('cf-account-id').value.trim()};
  const token = document.getElementById('cf-api-token').value.trim();
  if (token) payload.CLOUDFLARE_API_TOKEN = token;
  saveEnv(payload, 'Cloudflare credentials saved.').then(loadSettings);
}

async function testInstantly() {
  const key = document.getElementById('instantly-key').value.trim();
  const el = document.getElementById('instantly-test-result');
  el.innerHTML = '<div class="test-result pending">Testing&hellip;</div>';
  const data = await api('/api/settings/test-instantly', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({api_key: key})
  });
  if (!data) {
    el.innerHTML = '<div class="test-result error">Could not reach the dashboard server.</div>';
    return;
  }
  el.innerHTML = '<div class="test-result ' + (data.success ? 'success' : 'error') + '">' + escHtml(failMsg(data, '')) + '</div>';
}

// ── Controls ──

async function loadSellbirdStatus() {
  const data = await api('/api/sellbird/status');
  const headerDot = document.getElementById('header-dot');
  const headerText = document.getElementById('header-status-text');

  if (_locked) return;  // the header already says "Locked"
  if (!data) {
    headerDot.className = 'status-dot offline';
    headerText.textContent = 'Offline';
    // The Controls tab must not read a dead server as "Stopped".
    document.getElementById('control-dot').className = 'dot offline';
    const label = document.getElementById('control-label');
    label.className = 'label stopped';
    label.textContent = 'Offline';
    document.getElementById('control-meta').textContent = "Can't reach VeloReach right now.";
    return;
  }
  const running = !!data.running;

  headerDot.className = 'status-dot ' + (running ? 'running' : 'stopped');
  headerText.textContent = running ? 'VeloReach is running' : 'VeloReach is stopped';
  document.getElementById('control-dot').className = 'dot ' + (running ? 'running' : 'stopped');
  const label = document.getElementById('control-label');
  label.className = 'label ' + (running ? 'running' : 'stopped');
  label.textContent = running ? 'Running' : 'Stopped';

  const meta = document.getElementById('control-meta');
  if (running && data.pid) {
    let info = 'PID ' + escHtml(String(data.pid));
    if (data.started_at) info += ' &middot; started ' + formatDate(data.started_at);
    meta.innerHTML = info;
  } else {
    meta.innerHTML = 'VeloReach wakes every few minutes, does what needs doing, and sleeps.';
  }

  document.getElementById('btn-start').style.display = running ? 'none' : '';
  document.getElementById('btn-stop').style.display = running ? '' : 'none';
  syncTodayLoopBtn(running);
}

let _todayLoopRunning = false;
let _todayCycleBusy = false;
let _todayAgenticStatusHtml = '';

function syncTodayLoopBtn(running) {
  _todayLoopRunning = !!running;
  updateAsciiTelemetry(_todayLoopRunning);
  const btn = document.getElementById('today-loop-btn');
  if (!btn) return;
  btn.textContent = _todayLoopRunning ? 'Stop Loop' : 'Start Loop';
  btn.className = 'btn btn-sm ' + (_todayLoopRunning ? 'btn-danger' : 'btn-secondary');
}

function setTodayAgenticResult(html) {
  _todayAgenticStatusHtml = html || '';
  const resEl = document.getElementById('today-agentic-result');
  if (!resEl) return;
  if (_todayAgenticStatusHtml) {
    resEl.style.display = 'block';
    resEl.innerHTML = _todayAgenticStatusHtml;
  } else {
    resEl.style.display = 'none';
    resEl.innerHTML = '';
  }
}

function renderTodayAgentic(ag) {
  const el = document.getElementById('today-agentic');
  if (!el) return;
  if (!ag) { el.innerHTML = ''; return; }
  syncTodayLoopBtn(ag.running);

  const brainTxt = ag.brain_ready
    ? 'AI Brain: ' + escHtml(ag.brain_provider || 'ready') + (ag.brain_model ? ' (' + escHtml(ag.brain_model) + ')' : '')
    : (ag.brain_paused ? 'AI Brain paused' : 'AI Brain not ready');
  const brainTag = '<button type="button" class="agentic-chip ' + (ag.brain_ready ? 'good' : 'bad') +
    '" onclick="goTab(\'brain\')"><span class="dot"></span>' + brainTxt + '</button>';

  const spend = Number(ag.spend_today_usd || 0).toFixed(4);
  const maxUsd = Number(ag.max_daily_usd || 5).toFixed(2);
  const spendTag = '<button type="button" class="agentic-chip" onclick="goTab(\'usage\')">Budget: $' +
    spend + ' / $' + maxUsd + '</button>';

  const mailTxt = ag.mailbox_ready
    ? 'Mailbox connected'
    : 'Mailbox deferred (drafts queue safely in Outbox)';
  const mailTag = '<button type="button" class="agentic-chip ' + (ag.mailbox_ready ? 'good' : 'warn') +
    '" onclick="goTab(' + jsq(ag.mailbox_ready ? 'inboxes' : 'settings') + ')">' + escHtml(mailTxt) + '</button>';

  const qhNote = ag.quiet_hours_active
    ? '<div class="agentic-note">Quiet hours active (' + escHtml(ag.quiet_hours_label || 'configured') +
      ') — background loop sleeps until the window opens. Quick actions below run immediately, or adjust in ' +
      '<a href="#" onclick="goTab(\'settings\');return false">Settings</a>.</div>'
    : '';

  el.innerHTML = '<div class="card agentic-card" aria-label="Agentic Workbench">' +
    '<div class="agentic-head">' +
      '<div><h3>Agentic Workbench</h3><p class="desc" style="margin:2px 0 0">Run prospecting, OSINT research, Clay AI, and draft writing anytime — even before connecting a mailbox.</p></div>' +
      '<div class="agentic-chips">' + brainTag + spendTag + mailTag + '</div>' +
    '</div>' +
    qhNote +
    '<div class="agentic-actions">' +
      '<button class="btn btn-secondary btn-sm" id="today-scout-btn" onclick="runTodayStage(\'/api/scout/run\', \'Scout Contacts\', this)">Scout Contacts</button>' +
      '<button class="btn btn-secondary btn-sm" id="today-research-btn" onclick="runTodayStage(\'/api/research/run\', \'Deep Research\', this, {limit: 5})">Deep Research (5)</button>' +
      '<button class="btn btn-secondary btn-sm" id="today-enrich-btn" onclick="runTodayStage(\'/api/enrichment/run\', \'Clay AI\', this, {limit: 10})">Enrich AI Columns</button>' +
      '<button class="btn btn-secondary btn-sm" id="today-writer-btn" onclick="runTodayStage(\'/api/writer/run\', \'Writer + Critic\', this)">Write Drafts</button>' +
    '</div>' +
    '<div id="today-agentic-result" class="agentic-result" style="display:' +
      (_todayAgenticStatusHtml ? 'block' : 'none') + '">' + _todayAgenticStatusHtml + '</div>' +
  '</div>';
}

async function toggleAgentLoopFromToday(btn) {
  const button = btn || document.getElementById('today-loop-btn');
  if (button) button.disabled = true;
  try {
    if (_todayLoopRunning) {
      await stopSellbird();
    } else {
      await startSellbird();
    }
    await loadToday();
  } finally {
    if (button) button.disabled = false;
  }
}

async function runAgenticCycleToday(btn) {
  if (_todayCycleBusy) return;
  _todayCycleBusy = true;
  const button = btn || document.getElementById('today-cycle-btn');
  const orig = 'Run Agentic Cycle';
  if (button) { button.disabled = true; button.textContent = 'Running cycle…'; }
  setTodayAgenticResult('Starting Scout &rarr; Deep Research &rarr; Clay Enrich &rarr; Writer + Critic&hellip;');
  let keepPolling = false;
  try {
    const res = await api('/api/agent/cycle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ background: true, research_limit: 3, enrich_limit: 5, include_writer: true }),
    });
    if (res && res.started) {
      keepPolling = true;
      await _pollAgenticCycle(button, orig);
      return;
    }
    if (res && res.success) {
      _showCycleComplete(res);
      await Promise.all([loadToday(), loadLiveStatus(), loadFunnel(), loadTodayActivity()]);
    } else {
      const err = (res && res.message) || 'Agentic cycle failed.';
      showToast(err, 'error');
      setTodayAgenticResult('Error: ' + escHtml(err));
    }
  } finally {
    if (!keepPolling) {
      _todayCycleBusy = false;
      if (button) { button.disabled = false; button.textContent = orig; }
    }
  }
}

function _showCycleComplete(res) {
  const steps = (res.steps_run || []).join(', ') || 'none';
  const summary = 'Cycle complete (' + steps + ') · +' + (res.prospects_added || 0) +
    ' new contact(s) · ' + (res.outbox_pending || 0) + ' draft(s) waiting in Outbox. ' +
    (res.note || '');
  showToast('Agentic cycle finished.', 'success');
  setTodayAgenticResult('<span>' + escHtml(summary) + '</span> ' +
    '<button class="btn btn-ghost btn-sm" onclick="goTab(\'outbox\')">Open Outbox &rarr;</button>');
}

async function _pollAgenticCycle(button, origLabel) {
  const poll = async () => {
    try {
      const st = await api('/api/agent/cycle/status');
      if (!st) {
        _todayCycleBusy = false;
        if (button) { button.disabled = false; button.textContent = origLabel; }
        return;
      }
      if (st.running) {
        const done = (st.steps_done || []).join(', ');
        const cur = st.current_step ? 'Active: ' + escHtml(st.current_step) : 'Running…';
        setTodayAgenticResult(cur + (done ? ' · Done: ' + escHtml(done) : ''));
        setTimeout(poll, 2000);
        return;
      }
      _todayCycleBusy = false;
      if (button) { button.disabled = false; button.textContent = origLabel; }
      if (st.result && st.result.success) {
        _showCycleComplete(st.result);
        await Promise.all([loadToday(), loadLiveStatus(), loadFunnel(), loadTodayActivity()]);
      } else if (st.error) {
        showToast(st.error, 'error');
        setTodayAgenticResult('Error: ' + escHtml(st.error));
      }
    } catch (e) {
      _todayCycleBusy = false;
      if (button) { button.disabled = false; button.textContent = origLabel; }
    }
  };
  setTimeout(poll, 1000);
}

async function runTodayStage(endpoint, label, btn, payload) {
  const orig = btn ? btn.textContent : label;
  if (btn) { btn.disabled = true; btn.textContent = label + '…'; }
  setTodayAgenticResult('Running ' + escHtml(label) + '&hellip;');
  try {
    const res = await api(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {}),
    });
    if (res && res.success) {
      let detail = label + ' finished.';
      if (endpoint === '/api/scout/run') {
        detail = 'Scout finished · +' + (res.added || 0) + ' contact(s) (' + (res.prospects_after || 0) + ' total).';
      } else if (endpoint === '/api/writer/run') {
        detail = 'Writer finished · ' + (res.pending_outbox || 0) + ' draft(s) waiting in Outbox.';
      } else if (endpoint === '/api/enrichment/run') {
        detail = 'Clay AI finished · ' + (res.enriched_cells ?? res.result ?? 0) + ' cell(s) processed.';
      } else if (endpoint === '/api/research/run') {
        const count = Array.isArray(res.result) ? res.result.length : 1;
        detail = 'Deep Research finished · ' + count + ' company dossier(s) updated.';
      }
      showToast(label + ' complete.', 'success');
      setTodayAgenticResult(escHtml(detail));
      await Promise.all([loadToday(), loadLiveStatus(), loadFunnel(), loadTodayActivity()]);
    } else {
      const err = (res && res.message) || (label + ' failed.');
      showToast(err, 'error');
      setTodayAgenticResult('Error: ' + escHtml(err));
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = orig; }
  }
}

async function startSellbird() {
  const btn = document.getElementById('btn-start');
  btn.disabled = true;
  try {
    const data = await api('/api/sellbird/start', {method: 'POST'});
    if (data && data.success) showToast('VeloReach started.', 'success');
    else showToast((data && data.message) || 'Failed to start.', 'error');
  } finally {
    btn.disabled = false;
  }
  loadSellbirdStatus();
  loadLogs();
}

async function stopSellbird() {
  const btn = document.getElementById('btn-stop');
  const origText = btn.textContent || 'Stop VeloReach';
  btn.textContent = 'Stopping…';
  btn.disabled = true;
  try {
    const data = await api('/api/sellbird/stop', {method: 'POST'});
    if (data && data.success) showToast('VeloReach stopped.', 'success');
    else showToast((data && data.message) || 'Failed to stop.', 'error');
  } finally {
    btn.textContent = origText;
    btn.disabled = false;
  }
  loadSellbirdStatus();
  loadLogs();
}

async function loadLogs() {
  const data = await api('/api/sellbird/logs');
  const el = document.getElementById('log-viewer');
  if (!el) return;
  if (!data) {
    if (!el.dataset.hasLogs) {
      el.textContent = "Can't reach VeloReach right now — logs unavailable.";
    }
    return;
  }
  if (data.lines && data.lines.length) {
    const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.textContent = data.lines.join('\n');
    el.dataset.hasLogs = '1';
    if (stick) el.scrollTop = el.scrollHeight;
  } else {
    delete el.dataset.hasLogs;
    el.textContent = 'No logs yet. Start VeloReach to see activity.';
  }
}

// ── Pipeline data ──

// Calls today per subscription — each labelled with the provider it ran on.
function brainCallChips(data) {
  const chip = (label, n) => '<span class="chip">' + label + ' calls today <b>' + escHtml(String(n || 0)) + '</b></span>';
  const codex = data.codex_calls_today || 0;
  const st = data.brain_status || {};
  // A paused brain must not look healthy: say so, and why.
  const paused = st.status === 'paused'
    ? '<span class="chip" title="' + escHtml(st.reason || '') + '">' +
      escHtml(st.provider === 'codex' ? 'Codex' : 'Brain') + ' <b>paused</b>: ' +
      escHtml((st.reason || '').slice(0, 120)) + '</span>'
    : '';
  if (data.brain_provider === 'codex') {
    return paused + chip('Codex', codex) + (data.claude_calls_today ? chip('Claude', data.claude_calls_today) : '');
  }
  return paused + chip('Claude', data.claude_calls_today) + (codex ? chip('Codex', codex) : '');
}

function fmtTokens(n) {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}


function emailTag(p) {
  if (!p.email) return '';
  // Fall back to the legacy boolean for rows predating email_status.
  const status = p.email_status || (p.email_verified ? 'verified' : 'guess');
  if (!own(STATUS, status)) return '';
  const m = statusMeta(status);
  return ' <span class="mini t-' + m.tone + '" title="Email ' + escHtml(m.label.toLowerCase()) + '">' +
    escHtml(m.label) + '</span>';
}

// Quota gauges for the ACTIVE provider only: Claude's are the numbers
// `/usage` shows in Claude Code; Codex's are the ChatGPT plan's limits.
// When they can't be read, say so rather than hiding the card.
function quotaCardHtml(data) {
  const provider = data && data.quota_provider;
  if (!provider) return '';
  const title = provider === 'codex' ? 'Codex (ChatGPT plan) Quota' : 'Claude Subscription Quota';
  let html = '<h2>' + escHtml(title) + '</h2>';
  if (!data.quota || !Object.keys(data.quota).length) {
    const why = provider === 'codex'
      ? "Quota unavailable: VeloReach couldn't read your ChatGPT plan's limits (check codex login, the Codex CLI version, and network)."
      : "Quota unavailable: VeloReach couldn't read your Claude subscription's limits (check claude login and network).";
    return html + '<p class="muted">' + escHtml(why) + ' Until it can, budgets count VeloReach\'s own calls.</p>';
  }
  const labels = {five_hour: '5-hour window', seven_day: 'Weekly', primary: 'Primary window',
                  secondary: 'Secondary window', limit_reached: 'Usage limit reached'};
  for (const [key, w] of Object.entries(data.quota)) {
    const pct = Math.min(100, Math.max(0, w.utilization || 0));
    const color = pct >= 80 ? 'yellow' : 'green';
    const resets = w.resets_at ? 'resets ' + formatDate(w.resets_at) : '';
    html += '<div class="progress-wrap">' +
      '<div class="progress-label">' +
        '<span class="text">' + escHtml(labels[key] || key) + (resets ? ' &middot; ' + escHtml(resets) : '') + '</span>' +
        '<span class="pct">' + pct.toFixed(0) + '%</span>' +
      '</div>' +
      '<div class="progress-bar"><div class="progress-fill ' + color + '" style="width:' + pct + '%"></div></div>' +
    '</div>';
  }
  return html;
}

async function loadUsage() {
  const data = await api('/api/usage');
  const statsEl = document.getElementById('usage-stats');
  if (!data) {
    statsEl.innerHTML = offlineState();
    document.getElementById('usage-quota').style.display = 'none';
    document.getElementById('usage-daily').style.display = 'none';
    document.getElementById('usage-tables').innerHTML = '';
    return;
  }

  const quotaEl = document.getElementById('usage-quota');
  const qHtml = quotaCardHtml(data);
  quotaEl.innerHTML = qHtml;
  quotaEl.style.display = qHtml ? 'block' : 'none';

  // Totals cards — calls as headline; tokens + USD cost (when > 0) as chips.
  const fmtUsd = v => {
    const n = Number(v || 0);
    return n >= 0.1 ? '$' + n.toFixed(2) : '$' + n.toFixed(4);
  };
  const t = data.totals || {};
  const card = (label, p) => {
    p = p || {};
    const costVal = Number(p.cost_usd || 0);
    const costChip = costVal > 0 ? '<span class="chip">cost <b>' + fmtUsd(costVal) + '</b></span>' : '';
    return '<div class="stat-card"><div class="label">' + label + '</div>' +
      '<div class="value">' + (p.calls || 0) + '</div>' +
      '<div class="breakdown">' +
        '<span class="chip">calls</span>' +
        '<span class="chip">out <b>' + fmtTokens(p.output_tokens) + '</b></span>' +
        '<span class="chip">in <b>' + fmtTokens(p.input_tokens) + '</b></span>' +
        '<span class="chip">cached <b>' + fmtTokens(p.cache_read_tokens) + '</b></span>' +
        costChip +
      '</div></div>';
  };
  statsEl.innerHTML = card('Today', t.today) + card('Last 7 Days', t.week) + card('Last 30 Days', t.month);

  // Daily columns — output tokens per day (the work done)
  const dailyEl = document.getElementById('usage-daily');
  const days = (data.by_day || []).slice(-30);
  if (days.length) {
    const maxOut = Math.max(...days.map(d => d.output_tokens || 0), 1);
    dailyEl.innerHTML = '<h2>Output tokens per day <span class="hint muted" style="font-weight:400">last ' +
      days.length + ' days · peak ' + fmtTokens(maxOut) + '</span></h2>' +
      '<div class="chart-cols" role="img" aria-label="Output tokens per day">' + days.map(d =>
        '<span class="col" title="' + escHtml(d.day || '') + ': ' + fmtTokens(d.output_tokens) + ' out, ' +
          (d.calls || 0) + ' calls' + (Number(d.cost_usd || 0) > 0 ? ', ' + fmtUsd(d.cost_usd) : '') +
          '"><i style="height:' + Math.max(2, (d.output_tokens || 0) / maxOut * 100) + '%"></i></span>'
      ).join('') + '</div>' +
      '<div class="chart-axis"><span>' + escHtml(days[0].day || '') + '</span><span>' +
        escHtml(days[days.length - 1].day || '') + '</span></div>';
    dailyEl.style.display = 'block';
  } else {
    dailyEl.style.display = 'none';
  }

  // Breakdown tables — calls + tokens, plus Cost (USD) when cost_usd > 0
  const tablesEl = document.getElementById('usage-tables');
  const table = (title, rows, keyName) => {
    if (!rows || !rows.length) return '';
    const max = Math.max(...rows.map(r => r.calls || 0), 1);
    const hasCost = rows.some(r => Number(r.cost_usd || 0) > 0);
    let h = '<div class="card flush"><div class="card-head"><h3>' + title + '</h3></div><table><thead><tr>' +
      '<th>' + keyName + '</th><th>Calls</th><th class="num">Input</th><th class="num">Output</th><th class="num">Cache read</th>' +
      (hasCost ? '<th class="num">Cost (USD)</th>' : '') +
      '</tr></thead><tbody>';
    for (const r of rows) {
      const rCost = Number(r.cost_usd || 0);
      h += '<tr><td class="cell-main">' + escHtml(String(r[keyName.toLowerCase()] || '')) + '</td>' +
        '<td><span class="meter-row"><span class="bar" style="width:120px"><span style="width:' +
          (100 * (r.calls || 0) / max) + '%;background:var(--accent)"></span></span><span class="v">' + (r.calls || 0) + '</span></span></td>' +
        '<td class="num muted">' + fmtTokens(r.input_tokens) + '</td>' +
        '<td class="num">' + fmtTokens(r.output_tokens) + '</td>' +
        '<td class="num muted">' + fmtTokens(r.cache_read_tokens) + '</td>' +
        (hasCost ? '<td class="num">' + (rCost > 0 ? fmtUsd(rCost) : '—') + '</td>' : '') +
        '</tr>';
    }
    return h + '</tbody></table></div>';
  };

  const anyRows = (data.by_agent || []).length || (data.by_task || []).length;
  if (!anyRows) {
    tablesEl.innerHTML = emptyState(icon('usage'), 'No usage recorded yet',
      'Once VeloReach starts making model calls (Claude or Codex), every one is logged here with exact calls and tokens by provider, agent and task.');
  } else {
    const providers = data.by_provider || [];
    const showProviders = providers.length > 1 || (providers.length === 1 && providers[0].provider !== 'claude');
    tablesEl.innerHTML =
      (showProviders ? table('By Provider (30 days)', providers, 'Provider') : '') +
      table('By Agent (30 days)', data.by_agent, 'Agent') +
      table('By Task (30 days)', data.by_task, 'Task') +
      table('By Model (30 days)', data.by_model, 'Model');
  }
}

// ── Outbox: a decisions desk, not a wall of drafts ──
//
// Approving mail is a queue of one-at-a-time judgements. Showing all of them
// stacked invites a single "approve all" reflex, which is exactly the review
// the approval ladder exists to prevent. One email fills the pane; the rest
// wait in the rail.

let _outboxSelected = new Set();
let _outboxSenderName = '';

async function loadOutbox() {
  const [data, mode] = await Promise.all([api('/api/outbox'), api('/api/sending-mode')]);
  _sendingMode = mode && !mode.error ? mode : null;
  const banner = document.getElementById('outbox-banner');
  const desk = document.getElementById('outbox-desk');
  const list = document.getElementById('outbox-list');
  const actions = document.getElementById('outbox-actions');
  if (!data) { desk.innerHTML = offlineState(); list.innerHTML = ''; return; }
  if (data.error) {
    // Never let a failed read pass for "Nothing to review".
    desk.innerHTML = emptyState(icon('alert'), 'Couldn\'t read the Outbox', escHtml(data.error));
    list.innerHTML = '';
    return;
  }

  const pending = data.pending || [];
  _desk.items = pending;
  _desk.footer = data.footer || '';
  _outboxSenderName = data.sender_name || '';
  const pendingIds = new Set(pending.map(p => p.id));
  _outboxSelected = new Set([..._outboxSelected].filter(id => pendingIds.has(id)));
  // Keep the desk on the draft being edited even if the list shifted.
  const editingAt = _desk.editing ? pending.findIndex(p => p.id === _desk.editing) : -1;
  if (editingAt >= 0) _desk.i = editingAt;
  else if (_desk.editing) {
    _desk.editing = null;
    _desk.unsaved = null;
  }
  if (_desk.i >= pending.length) _desk.i = Math.max(0, pending.length - 1);
  navCount('nav-outbox', pending.length);

  renderOutboxActions(actions, pending, data.paused);

  // The kill switch gets a banner only when it's actually on — a permanent
  // bar for a thing that isn't happening is just noise.
  const mailboxNote = (!data.mailbox_ready && !data.paused)
    ? '<div class="banner">' + icon('mail') + '<div><b>Mailbox not connected yet</b>' +
        '<p>You can draft, rewrite, and approve emails now — approved drafts stay safely queued' +
        ((data.approved || []).length ? ' (' + (data.approved || []).length + ' approved waiting)' : '') +
        ' until you connect Gmail, Microsoft 365, or SMTP in <a href="#settings" onclick="goToTab(\'settings\');return false">Settings</a>.</p></div></div>'
    : '';
  banner.innerHTML = sendingModeBanner(_sendingMode) + mailboxNote + (data.paused
    ? '<div class="banner bad">' + icon('alert') + '<div><b>Sending is paused</b>' +
        '<p>' + escHtml(data.paused) + '. Approved mail stays queued until you resume.</p></div></div>'
    : '');
  // Only once there is mail to warn about: a fresh install sends nothing.
  const anyMail = pending.length || (data.approved || []).length || (data.sent || []).length;
  if (data.compliance_notice && anyMail) {
    banner.innerHTML +=
      '<div class="banner wait">' + icon('alert') + '<div><b>Add your postal address</b>' +
        '<p>' + escHtml(data.compliance_notice) + '</p></div></div>';
  }

  desk.innerHTML = pending.length ? renderDesk(pending, _desk.i) :
    '<div class="card">' + emptyState(icon('check'), 'Nothing to review',
      _sendingMode && _sendingMode.mode === 'autopilot'
        ? 'Autopilot is on, so VeloReach sends on its own. Anything it holds for you shows up here.'
        : 'Every draft VeloReach writes lands here first. Approve one and it sends on schedule.') +
    '</div>';

  const table = (title, rows, cols) => {
    if (!rows || !rows.length) return '';
    let h = '<div class="subhead">' + title + ' <span class="hint">' + rows.length + '</span></div>' +
      '<div class="table-card"><table class="nowrap clip"><thead><tr>' +
      cols.map(c => '<th>' + c[0] + '</th>').join('') + '</tr></thead><tbody>';
    for (const r of rows) {
      h += '<tr>' + cols.map(c => '<td' + (c[2] ? ' class="muted"' : '') + '>' +
        (c[3] ? c[1](r) : escHtml(String(c[1](r) ?? ''))) + '</td>').join('') + '</tr>';
    }
    return h + '</tbody></table></div>';
  };

  list.innerHTML =
    table('Approved &amp; scheduled', data.approved, [
      ['To', r => r.to_email], ['Step', r => r.step], ['Subject', r => r.subject],
      ['Sends', r => r.status === 'sending' ? badge(r.status) : escHtml(formatDate(r.send_at)), true, true],
    ]) +
    table('Recently sent', data.sent, [
      ['To', r => r.to_email], ['Step', r => r.step], ['Subject', r => r.subject],
      ['Sent', r => formatDate(r.sent_at), true],
    ]) +
    table('Didn\'t send', data.failed, [
      ['To', r => r.to_email], ['Status', r => badge(r.status), false, true],
      ['Reason', r => r.error, true], ['Updated', r => formatDate(r.updated_at), true],
    ]);
}

const CONTACT_SOURCES = {
  known_company_backfill: 'team page on their site',
  company_website: 'team page on their site',
  scraped_mailto: 'address published on their site',
  linkedin_search: 'LinkedIn search',
  hunter_domain_search: 'Hunter domain search',
  job_board: 'job board',
  web_search: 'web search',
};

// What the draft is built on, so every claim can be checked against it.
function renderDeskContext(ctx) {
  if (!ctx) return '';
  const row = (k, v) => v ? '<div class="k">' + k + '</div><div class="v">' + v + '</div>' : '';
  const href = safeHref(ctx.website);
  const site = href
    ? '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' +
        escHtml(ctx.website.replace(/^https?:\/\//i, '')) + ' &#8599;</a>'
    : '';
  const who = [ctx.contact, ctx.contact_title].filter(Boolean).map(escHtml).join(' &middot; ');
  const email = [ctx.email_status, ctx.score ? 'score ' + ctx.score : '']
    .filter(Boolean).map(escHtml).join(' &middot; ');
  const signals = Object.entries(ctx.signals || {})
    .map(([k, v]) => escHtml(k) + ': <b>' + escHtml(v) + '</b>').join('<br>');
  return '<div class="desk-context">' +
    row('Company', escHtml(ctx.company)) +
    row('Website', site) +
    row('About', escHtml(ctx.description)) +
    row('Contact', who) +
    row('Found via', escHtml(Object.prototype.hasOwnProperty.call(CONTACT_SOURCES, ctx.contact_source)
      ? CONTACT_SOURCES[ctx.contact_source] : ctx.contact_source)) +
    row('Email', email) +
    row('Signals', signals) +
    row('Campaign', ctx.campaign ? escHtml(ctx.campaign) +
      ' <span class="muted">(how they were found, not a fact about them)</span>' : '') +
    row('Writer notes', escHtml(ctx.notes)) +
  '</div>';
}

// ── Approval or autopilot ──
// Approval (the default): every email waits in the Outbox for you.
// Autopilot (channels.email.require_approval: false): VeloReach sends on its
// own, and still holds what gate.autopilot_review flags. Turning autopilot
// on takes typing the word; turning it off is one click and works at once.
let _sendingMode = null, _autopilotArmed = false, _modeBusy = false;

function sendingModeBanner(m) {
  if (!m) return '';
  if (m.mode === 'autopilot') {
    return '<div class="banner wait mode-banner">' + icon('alert') + '<div><b>Autopilot is on</b>' +
      '<p>VeloReach sends email on its own. It holds anything that links to a site that isn’t yours or ' +
      'quotes its own instructions; those wait here' + (m.held ? ' (' + m.held + ' now)' : '') + '.</p></div>' +
      '<button class="btn btn-secondary btn-sm" onclick="setSendingMode(\'approval\')"' +
        (_modeBusy ? ' disabled' : '') + '>Turn off autopilot</button></div>';
  }
  const instantly = m.provider === 'instantly';
  return '<div class="banner mode-banner">' + icon('check') + '<div><b>Approval mode</b>' +
    '<p>' + (instantly
      ? 'Instantly can’t hold single emails, so campaigns aren’t sent to Instantly until you turn on autopilot.'
      : 'Every email VeloReach writes waits here until you approve it.') +
    ' <a href="#" onclick="goTab(\'settings\');return false">About autopilot</a></p></div></div>';
}

function sendingModeCard(m) {
  if (!m) return '<div class="card">' + offlineState() + '</div>';
  const auto = m.mode === 'autopilot';
  const instantly = m.provider === 'instantly';
  const domains = (m.your_domains || []).map(escHtml).join(', ') || 'none yet';
  let html = '<div class="card mode-card"><h3>Sending mode: ' + (auto ? 'Autopilot' : 'Approval') + '</h3>' +
    '<p><b>Approval</b> (the default): every email VeloReach writes, first emails, follow-ups and replies, ' +
      'waits in the Outbox until you approve it.' +
      (instantly ? ' On Instantly, which can’t hold single emails, that means campaigns aren’t deployed.' : '') + '</p>' +
    '<p><b>Autopilot</b>: VeloReach sends on its own, on schedule, within your daily limit and quiet hours. ' +
      'Opt-outs, bounces and Pause all sending work the same. It still holds an email for you when it links ' +
      'to, or gives an address at, a site that isn’t yours, or quotes VeloReach’s own instructions: text from ' +
      'websites and replies can try to steer what it writes. The first reply after you undo an opt-out waits too.</p>' +
    '<p class="muted">Counted as yours: ' + domains + (m.booking_page ? '; your booking page ' + escHtml(m.booking_page) : '') +
      '. Add others under <code>channels.email.link_domains</code> in <code>' + escHtml(m.file) + '</code>.</p>';
  if (auto) {
    html += '<div class="convo-actions"><span class="muted">Turning it off works at once: emails autopilot ' +
      'approved that haven’t gone out come back to the Outbox.' +
      (instantly ? ' Campaigns already on Instantly keep running until you pause them there.' : '') + '</span>' +
      '<button class="btn btn-primary btn-sm" onclick="setSendingMode(\'approval\')"' +
        (_modeBusy ? ' disabled' : '') + '>Turn off autopilot</button></div>';
  } else if (_autopilotArmed) {
    html += '<div class="convo-actions mode-confirm"><span class="muted">Approve a batch or two by hand first ' +
      'and read what VeloReach writes. Type <b>autopilot</b> to turn it on. It starts from VeloReach’s next cycle.</span>' +
      '<input class="form-input" id="autopilot-confirm" autocomplete="off" aria-label="Type autopilot to confirm" ' +
        'oninput="document.getElementById(\'autopilot-go\').disabled = this.value.trim().toLowerCase() !== \'autopilot\'" ' +
        'onkeydown="if (event.key === \'Enter\' && this.value.trim().toLowerCase() === \'autopilot\') setSendingMode(\'autopilot\')">' +
      '<button class="btn btn-primary btn-sm" id="autopilot-go" disabled onclick="setSendingMode(\'autopilot\')">Turn on autopilot</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="_autopilotArmed=false;renderSendingModeCard()">Cancel</button></div>';
  } else {
    html += '<div class="convo-actions"><button class="btn btn-secondary btn-sm" ' +
      'onclick="_autopilotArmed=true;renderSendingModeCard()">Turn on autopilot…</button></div>';
  }
  return html + '</div>';
}

function renderSendingModeCard() {
  const el = document.getElementById('sending-mode-card');
  if (el) el.innerHTML = sendingModeCard(_sendingMode);
  const input = document.getElementById('autopilot-confirm');
  if (input) input.focus();
}

async function loadSendingModeCard() {
  const m = await api('/api/sending-mode');
  _sendingMode = m && !m.error ? m : null;
  renderSendingModeCard();
}

async function setSendingMode(mode) {
  if (_modeBusy) return;
  const body = {mode};
  if (mode === 'autopilot') {
    const input = document.getElementById('autopilot-confirm');
    body.confirm = input ? input.value.trim().toLowerCase() : '';
  }
  _modeBusy = true;
  document.querySelectorAll('.mode-banner button, .mode-card button').forEach(b => { b.disabled = true; });
  try {
    const res = await api('/api/sending-mode', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    if (res && res.success) {
      _autopilotArmed = false;
      showToast(mode === 'autopilot'
        ? 'Autopilot is on from VeloReach’s next cycle. Held emails still wait for you.'
        : 'Autopilot is off. Every email waits for you' +
          (res.requeued ? '; ' + res.requeued + ' queued by autopilot came back to the Outbox.' : '.') +
          (_sendingMode && _sendingMode.provider === 'instantly'
            ? ' Campaigns already on Instantly keep running: pause them in Instantly.' : ''), 'success');
    } else {
      showToast(failMsg(res, 'Couldn’t change the sending mode.'), 'error');
    }
  } finally {
    _modeBusy = false;
  }
  loadSendingModeCard();
  loadOutbox();
}

// Why autopilot sent this back for a human (sender: gate.autopilot_review).
const HELD_PREFIX = 'held for your review: ';   // state.HELD_FOR_REVIEW
function heldReason(item) {
  const e = String((item && item.error) || '');
  const why = e.startsWith(HELD_PREFIX) ? e.slice(HELD_PREFIX.length) : '';
  return why.charAt(0).toUpperCase() + why.slice(1);
}

function renderOutboxActions(actionsEl, pending, paused) {
  if (!actionsEl) return;
  const selCount = _outboxSelected.size;
  const unheld = pending.filter(p => !heldReason(p)).length;
  actionsEl.innerHTML =
    (pending.length
      ? '<button class="btn btn-ghost btn-sm" id="outbox-select-all-btn" onclick="outboxToggleSelectAll()">' +
          (selCount === pending.length && pending.length ? 'Clear selected (' + selCount + ')' : 'Select all (' + pending.length + ')') +
        '</button>'
      : '') +
    (selCount && !paused
      ? '<button class="btn btn-primary btn-sm" id="outbox-bulk-approve-btn" onclick="outboxBulk(\'approve\', this)">Approve selected ' + selCount + '</button>'
      : '') +
    (selCount
      ? '<button class="btn btn-secondary btn-sm" id="outbox-bulk-reject-btn" onclick="outboxBulk(\'reject\', this)">Reject selected ' + selCount + '</button>'
      : '') +
    (!selCount && pending.length && !paused && unheld
      ? '<button class="btn btn-primary btn-sm" onclick="outboxApproveAll()" ' +
        'title="Emails VeloReach held are left for you to approve one by one">Approve all ' + unheld + '</button>'
      : '') +
    (paused
      ? '<button class="btn btn-secondary btn-sm" onclick="sendingToggle(\'resume\')">Resume sending</button>'
      : '<button class="btn btn-secondary btn-sm" onclick="sendingToggle(\'pause\')">Pause all sending</button>');
  actionsEl.dataset.paused = paused ? '1' : '';
}

function renderDesk(items, i) {
  const cur = items[i];
  const rail = items.map((it, n) => {
    const who = it.context && it.context.contact ? it.context.contact : it.to_email;
    const parts = String(who || '').split(/[\s@.]+/);
    const checked = _outboxSelected.has(it.id) ? ' checked' : '';
    const hasTags = Array.isArray(it.unrendered_tags) && it.unrendered_tags.length > 0;
    return '<div class="desk-item ' + (n === i ? 'active' : '') + '" onclick="deskGo(' + n + ')">' +
      '<input type="checkbox" class="desk-check" aria-label="Select email to ' + escHtml(who) + '"' + checked +
        ' onclick="event.stopPropagation();toggleOutboxSelect(' + jsq(it.id) + ',this.checked)">' +
      '<span class="avatar">' + initials(parts[0], parts[1]) + '</span>' +
      '<div class="di-text"><div class="to">' + escHtml(who) +
        (heldReason(it) ? ' <span class="tag t-bad" title="VeloReach held this instead of sending it">Held</span>' : '') +
        (hasTags ? ' <span class="tag t-warn" title="Unrendered merge tag found">Tag!</span>' : '') +
        '</div>' +
      '<div class="sub">' + escHtml(it.subject || '(no subject)') + '</div></div>' +
    '</div>';
  }).join('');

  const editing = _desk.editing === cur.id;
  const unsaved = (editing && _desk.unsaved) || {subject: cur.subject || '', body: cur.body || ''};
  // A reply keeps its subject: Gmail threads by it.
  const fixedSubject = cur.kind === 'reply'
    ? ' readonly title="A reply keeps its subject so it stays in the thread"' : '';
  const draft = editing
    ? '<input id="desk-subject" class="desk-edit-subject" value="' + escHtml(unsaved.subject) + '"' +
        fixedSubject + ' oninput="deskKeep()">' +
      '<textarea id="desk-body" class="desk-edit-body" rows="14" oninput="deskKeep()">' +
        escHtml(unsaved.body) + '</textarea>'
    : '<div class="subject">' + escHtml(cur.subject || '(no subject)') + '</div>' +
      '<div class="body">' + escHtml(cur.body) + '</div>';
  const unrendered = Array.isArray(cur.unrendered_tags) ? cur.unrendered_tags : [];
  const tagWarn = unrendered.length
    ? '<div class="banner bad desk-held" id="desk-tag-warning">' + icon('alert') +
        '<div><b>Unrendered merge tag' + (unrendered.length > 1 ? 's' : '') + ' detected</b>' +
        '<p>Replace ' + unrendered.map(t => '<code>' + escHtml(String(t).startsWith('{{') ? t : '{{' + t + '}}') + '</code>').join(', ') +
        ' with real details via Edit or Rewrite with AI before approving.</p></div></div>'
    : '';
  const hasSignoff = _outboxSenderName && (cur.body || '').toLowerCase().includes(_outboxSenderName.toLowerCase());
  const signoffPreview = !editing && cur.kind === 'sequence' && _outboxSenderName && !hasSignoff
    ? '<div class="desk-signoff" title="Added when sent (sender.from_name in sellbird.yaml)">Best regards,<br>' +
        escHtml(_outboxSenderName) + '</div>'
    : '';
  // Cold email leaves with the footer underneath; show it as it will go out.
  // Not added again when the body already ends with it, as in the Sender.
  const added = cur.kind === 'sequence' && !(cur.body || '').trimEnd().endsWith(_desk.footer)
    ? _desk.footer : '';
  const footer = added
    ? '<div class="desk-footer" title="Added when it sends (compliance in sellbird.yaml)">' +
        escHtml(added) + '</div>' : '';
  const actions = editing
    ? '<button class="btn btn-primary" onclick="deskSave(' + jsq(cur.id) + ')">Save</button>' +
      '<button class="btn btn-secondary" onclick="deskEdit(null)">Cancel</button>'
    : '<button class="btn btn-primary" onclick="outboxAct(' + jsq(cur.id) + ',\'approve\')">' +
        'Approve <kbd>A</kbd></button>' +
      '<button class="btn btn-secondary" onclick="deskEdit(' + jsq(cur.id) + ')">' +
        'Edit <kbd>E</kbd></button>' +
      '<button class="btn btn-secondary" id="desk-rewrite-btn" onclick="deskRegenerate(' + jsq(cur.id) + ', this)">' +
        'Rewrite with AI</button>' +
      '<button class="btn btn-secondary" id="desk-test-btn" onclick="openTestSendModal(' + jsq(cur.id) + ')">' +
        'Test Preview</button>' +
      '<button class="btn btn-secondary" onclick="outboxAct(' + jsq(cur.id) + ',\'reject\')">' +
        'Reject <kbd>R</kbd></button>';

  const wordCount = String(cur.body || '').trim().split(/\s+/).filter(Boolean).length;
  const tagSafety = unrendered.length
    ? '<span class="mini bad" title="Resolve merge tags before sending">Tag leak</span>'
    : '<span class="mini good" title="All merge tags resolved">Tags clean</span>';
  return '<div class="desk">' +
    '<div class="desk-list">' + rail + '</div>' +
    '<div class="desk-pane">' +
      '<div class="to-line">To <b>' + escHtml(cur.to_email) + '</b>' +
        '<span class="tag">' + (cur.kind === 'reply' ? 'Reply' : 'Email ' + escHtml(String(cur.step))) + '</span>' +
        (cur.variant === 'B' ? '<span class="mini accent">Variant B</span>' : '') +
        tagSafety +
        '<span class="muted">' + wordCount + ' words &middot; sends ' + formatDate(cur.send_at) + '</span></div>' +
      renderDeskContext(cur.context) +
      tagWarn +
      (heldReason(cur)
        ? '<div class="banner wait desk-held">' + icon('alert') + '<div><b>VeloReach held this instead of sending it</b>' +
          '<div>' + escHtml(heldReason(cur)) + '. Read it before approving: text from a website or an ' +
          'email can try to steer what VeloReach writes.</div></div></div>'
        : '') +
      draft + signoffPreview + footer +
      '<div class="desk-actions">' + actions +
        '<div class="desk-pager" style="margin-left:auto;display:flex;align-items:center;gap:6px">' +
          '<button type="button" class="btn btn-ghost btn-sm" ' + (i <= 0 ? 'disabled ' : '') + 'onclick="deskGo(' + (i - 1) + ')" title="Previous (K)">&larr; Prev</button>' +
          '<span class="muted" style="font-size:12px">' + (i + 1) + ' of ' + items.length + '</span>' +
          '<button type="button" class="btn btn-ghost btn-sm" ' + (i >= items.length - 1 ? 'disabled ' : '') + 'onclick="deskGo(' + (i + 1) + ')" title="Next (J)">Next &rarr;</button>' +
        '</div>' +
      '</div>' +
    '</div></div>';
}

function deskEdit(id) {
  _desk.editing = id;
  _desk.unsaved = null;
  document.getElementById('outbox-desk').innerHTML = renderDesk(_desk.items, _desk.i);
  if (id) document.getElementById('desk-body').focus();
}

// What's typed survives a re-render (switching tabs and back reloads the desk).
function deskKeep() {
  _desk.unsaved = {
    subject: document.getElementById('desk-subject').value,
    body: document.getElementById('desk-body').value,
  };
}

// While a draft is being edited, nothing else on the desk may act on the
// stored version of it: Save or Cancel first.
function deskBusy() {
  if (!_desk.editing) return false;
  showToast('Save or cancel your edit first.', 'error');
  return true;
}

let _outboxBusy = false;

async function deskSave(id) {
  if (_outboxBusy) return;
  const subject = document.getElementById('desk-subject').value.trim();
  const body = document.getElementById('desk-body').value.trim();
  if (!subject || !body) { showToast('Subject and body are both required.', 'error'); return; }
  _outboxBusy = true;
  try {
    let data = null;
    try {
      const r = await authFetch('/api/outbox/' + encodeURIComponent(id) + '/edit', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({subject: subject, body: body}),
      });
      if (await lockIfRefused(r)) return;
      data = await r.json();
    } catch {}
    if (data && data.success) {
      _desk.editing = null;
      _desk.unsaved = null;
      showToast('Saved. Still waiting for your approval.', 'success');
      await loadOutbox();
    } else {
      showToast((data && data.message) || 'Save failed.', 'error');
    }
  } finally {
    _outboxBusy = false;
  }
}

function deskGo(n) {
  if (n < 0 || n >= _desk.items.length || deskBusy()) return;
  _desk.i = n;
  document.getElementById('outbox-desk').innerHTML = renderDesk(_desk.items, n);
}

// Keyboard review. Ignored while typing into a field, so Settings still works.
document.addEventListener('keydown', e => {
  if (currentTab !== 'outbox' || e.metaKey || e.ctrlKey || e.altKey) return;
  // Mid-edit, A would approve the stored draft, not what's typed. Only
  // Escape (cancel) works until the edit is saved or cancelled.
  if (_desk.editing) {
    const typing = /^(input|textarea)$/i.test(e.target.tagName || '');
    if (e.key === 'Escape') { e.preventDefault(); deskEdit(null); }
    else if (!typing && 'jkare'.includes(e.key.toLowerCase())) { e.preventDefault(); deskBusy(); }
    return;
  }
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || e.target.isContentEditable) return;
  const cur = _desk.items[_desk.i];
  const k = e.key.toLowerCase();
  if (k === 'j') { e.preventDefault(); deskGo(_desk.i + 1); }
  else if (k === 'k') { e.preventDefault(); deskGo(_desk.i - 1); }
  else if (k === 'a' && cur) { e.preventDefault(); outboxAct(cur.id, 'approve'); }
  else if (k === 'r' && cur) { e.preventDefault(); outboxAct(cur.id, 'reject'); }
  else if (k === 'e' && cur) { e.preventDefault(); deskEdit(cur.id); }
});

async function deskRegenerate(id, btn) {
  if (deskBusy() || _outboxBusy) return;
  _outboxBusy = true;
  const orig = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Rewriting...'; }
  try {
    const data = await api('/api/outbox/' + encodeURIComponent(id) + '/regenerate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({}),
    });
    if (data && data.success) {
      showToast('Draft rewritten with AI.', 'success');
      await loadOutbox();
    } else {
      showToast(failMsg(data, 'Could not rewrite draft.'), 'error');
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = orig || 'Rewrite with AI'; }
    _outboxBusy = false;
  }
}

async function outboxAct(id, action) {
  if (deskBusy() || _outboxBusy) return;
  _outboxBusy = true;
  try {
    const data = await api('/api/outbox/' + encodeURIComponent(id) + '/' + action, {method: 'POST'});
    if (data && data.success) showToast(action === 'approve' ? 'Approved — will send on schedule.' : 'Rejected.', 'success');
    else showToast(failMsg(data, 'Action failed.'), 'error');
    await loadOutbox();
  } finally {
    _outboxBusy = false;
  }
}

async function outboxApproveAll() {
  if (deskBusy() || _outboxBusy) return;
  _outboxBusy = true;
  try {
    const data = await api('/api/outbox/approve-all', {method: 'POST'});
    if (data && data.success) showToast('Approved ' + data.approved + ' email(s).' + (data.held
      ? ' ' + data.held + ' VeloReach held ' + (data.held === 1 ? 'is' : 'are') + ' left for you to read and approve one by one.'
      : ''), 'success');
    else showToast(failMsg(data, 'Approve-all failed.'), 'error');
    await loadOutbox();
  } finally {
    _outboxBusy = false;
  }
}

function toggleOutboxSelect(id, checked) {
  if (checked) _outboxSelected.add(id);
  else _outboxSelected.delete(id);
  const actions = document.getElementById('outbox-actions');
  const paused = !!(actions && actions.dataset.paused);
  renderOutboxActions(actions, _desk.items, paused);
}

function outboxToggleSelectAll() {
  if (_outboxSelected.size === _desk.items.length && _desk.items.length) {
    _outboxSelected.clear();
  } else {
    _outboxSelected = new Set(_desk.items.map(it => it.id));
  }
  const actions = document.getElementById('outbox-actions');
  const paused = !!(actions && actions.dataset.paused);
  renderOutboxActions(actions, _desk.items, paused);
  const desk = document.getElementById('outbox-desk');
  if (desk && _desk.items.length) desk.innerHTML = renderDesk(_desk.items, _desk.i);
}

async function outboxBulk(action, btn) {
  if (deskBusy() || _outboxBusy) return;
  const ids = [..._outboxSelected];
  if (!ids.length) return;
  _outboxBusy = true;
  if (btn) btn.disabled = true;
  try {
    const data = await api('/api/outbox/bulk', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids, action}),
    });
    if (data && data.success) {
      _outboxSelected.clear();
      showToast((action === 'approve' ? 'Approved ' : 'Rejected ') + (data.count || 0) + ' email(s).', 'success');
      await loadOutbox();
    } else {
      showToast(failMsg(data, 'Bulk outbox action failed.'), 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
    _outboxBusy = false;
  }
}

let _testSendItemId = null;
let _testSendCampaignId = null;

function openTestSendModal(itemId) {
  _testSendItemId = itemId || null;
  _testSendCampaignId = null;
  const p = document.getElementById('ts-preview');
  if (p) p.innerHTML = '<div class="hint muted">Rendering preview…</div>';
  const d = document.getElementById('test-send-dialog');
  if (d && d.showModal) d.showModal();
  return runTestSendPreview();
}
function openCampaignTestSend(campaignId) {
  _testSendItemId = null;
  _testSendCampaignId = campaignId || null;
  const p = document.getElementById('ts-preview');
  if (p) p.innerHTML = '<div class="hint muted">Rendering campaign preview…</div>';
  const d = document.getElementById('test-send-dialog');
  if (d && d.showModal) d.showModal();
  return runTestSendPreview();
}
function pickTestEmail(addr) {
  const el = document.getElementById('ts-email');
  if (el) {
    el.value = addr;
    el.focus();
  }
}
function closeTestSendModal() {
  const d = document.getElementById('test-send-dialog');
  if (d && d.close) d.close();
}
(function wireTestSendDialog() {
  const d = document.getElementById('test-send-dialog');
  if (!d) return;
  d.addEventListener('click', e => { if (e.target === d) closeTestSendModal(); });
})();
async function runTestSendPreview(btn) {
  const toEl = document.getElementById('ts-email');
  const prevEl = document.getElementById('ts-preview');
  const to_email = (toEl?.value || '').trim();
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/outbox/test-send', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        item_id: _testSendItemId,
        campaign_id: _testSendCampaignId,
        to_email,
      }),
    });
    if (res && res.success && prevEl) {
      const pr = res.preview || {};
      const fromAddr = pr.from || res.from_email || '';
      const provName = res.provider_name || '';
      prevEl.innerHTML =
        '<div class="banner ' + (res.sent ? 'good' : '') + '" style="margin:8px 0">' +
          icon(res.sent ? 'check' : 'mail') +
          '<div><b>' + (res.sent ? 'Sent test email' : 'Rendered inbox preview') + '</b>' +
          '<p>' + escHtml(res.message || '') + '</p></div></div>' +
        '<div class="ts-frame">' +
          '<div class="ts-frame-bar">' +
            (fromAddr ? '<b>From:</b> ' + escHtml(fromAddr) + ' &middot; ' : '') +
            '<b>To:</b> ' + escHtml(pr.to || res.to_email || '') +
            (provName ? ' &middot; <b>Via:</b> ' + escHtml(provName) : '') +
            '<br><b>Subject:</b> ' + escHtml(pr.subject || res.subject || '') +
          '</div>' +
          '<div class="ts-frame-body">' + (pr.html || '<pre>' + escHtml(pr.text || '') + '</pre>') + '</div>' +
        '</div>';
    } else if (prevEl) {
      prevEl.innerHTML = '<div class="banner bad">' + escHtml(failMsg(res, 'Could not render preview.')) + '</div>';
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function sendingToggle(action) {
  if (_outboxBusy) return;
  _outboxBusy = true;
  try {
    const data = await api('/api/sending/' + action, {method: 'POST'});
    if (data && data.success) showToast(action === 'pause' ? 'Sending paused.' : 'Sending resumed.', 'success');
    else showToast(failMsg(data, action === 'pause' ? 'Could not pause sending.' : 'Could not resume sending.'), 'error');
    await loadOutbox();
  } finally {
    _outboxBusy = false;
  }
}

const COMPANIES_PER_PAGE = 50;
let _companiesPage = 0;

async function loadCompanies() {
  companyDrill = false;
  const el = document.getElementById('companies-list');
  const data = await api('/api/companies');
  if (!data) { el.innerHTML = offlineState(); return; }
  _companies = data;
  if (!data.length) {
    el.innerHTML = emptyState(icon('companies'), 'No companies yet',
      'Confirm your <b>Signals</b>, then find businesses on the <b>Discover</b> tab. ' +
      'OpenStreetMap is free and needs no account.');
    return;
  }
  renderCompaniesPage(_companiesPage);
}

function renderCompaniesPage(page) {
  const el = document.getElementById('companies-list');
  const data = _companies;
  const pages = Math.max(1, Math.ceil(data.length / COMPANIES_PER_PAGE));
  _companiesPage = Math.min(Math.max(0, page), pages - 1);
  const start = _companiesPage * COMPANIES_PER_PAGE;
  let html = '<div class="table-card"><table class="nowrap"><thead><tr><th>Company</th>' +
    '<th title="Company fit, 0-100: sector, decision maker, reachability, buying signal. Hover a score for why.">Fit</th>' +
    '<th title="Hover a status for why the company was or wasn\'t contacted">Status</th>' +
    '<th>Industry</th><th>Location</th><th class="num">Contacts</th><th>Added</th></tr></thead><tbody>';
  data.slice(start, start + COMPANIES_PER_PAGE).forEach((c, j) => {
    const i = start + j;
    const website = c.website || (c.domain ? 'https://' + c.domain : '');
    const domain = safeHref(website)
      ? '<a href="' + safeHref(website) + '" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">' +
        escHtml(c.domain || website.replace(/^https?:\/\//i, '')) + '</a>'
      : escHtml(c.domain);
    html += '<tr class="clickable" onclick="showCompanyContacts(' + i + ')">' +
      '<td><div class="cell-stack"><span class="cell-main">' + escHtml(c.name) + '</span>' +
        '<span class="cell-sub">' + domain + '</span></div></td>' +
      '<td title="' + escHtml((c.fit_reasons || []).join(' · ')) + '">' +
        (typeof c.fit === 'number' ? meterHtml(c.fit) : '') + '</td>' +
      '<td title="' + escHtml(c.register_why || '') + '">' + escHtml(c.register_status || '') + '</td>' +
      '<td>' + (c.industry ? '<span class="tag">' + escHtml(c.industry) + '</span>' : '') + '</td>' +
      '<td>' + escHtml(c.location) + '</td>' +
      '<td class="num">' + (c.contact_count || 0) + '</td>' +
      '<td class="muted">' + relTime(c.created_at) + '</td></tr>';
  });
  const pager = pages > 1
    ? ' &nbsp; <button class="btn btn-ghost btn-sm" ' + (_companiesPage === 0 ? 'disabled ' : '') +
        'onclick="renderCompaniesPage(' + (_companiesPage - 1) + ')">&larr; Prev</button> ' +
      'Page ' + (_companiesPage + 1) + ' of ' + pages +
      ' <button class="btn btn-ghost btn-sm" ' + (_companiesPage >= pages - 1 ? 'disabled ' : '') +
        'onclick="renderCompaniesPage(' + (_companiesPage + 1) + ')">Next &rarr;</button>'
    : '';
  el.innerHTML = html + '</tbody></table></div>' +
    '<div class="table-foot">' + (start + 1) + '–' + Math.min(start + COMPANIES_PER_PAGE, data.length) +
    ' of ' + data.length + ' compan' + (data.length === 1 ? 'y' : 'ies') + pager + '</div>';
}

let _currentCompanyIndex = -1;

// Basic facts about one company, above its contacts: where it came from,
// why it fits, what's been done, and what VeloReach saw on its own site.
function companyInfoCard(company, profile) {
  const c = (profile && profile.company) || company;
  const reg = (profile && profile.register) || {};
  const obs = (profile && profile.observations) || [];
  const link = (url, text) => safeHref(url)
    ? '<a href="' + safeHref(url) + '" target="_blank" rel="noopener noreferrer">' + escHtml(text || url) + '</a>'
    : escHtml(text || url || '');
  const website = c.website || (c.domain ? 'https://' + c.domain : '');
  const row = (k, v) => v ? '<tr><td class="muted" style="vertical-align:top">' + k +
    '</td><td style="white-space:normal">' + v + '</td></tr>' : '';
  const latest = {};
  for (const o of obs) {
    const key = o.signal_code + '|' + (o.value_text || o.value_num);
    if (!latest[key] && (o.value_text || o.value_num !== null)) latest[key] = o;
  }
  const by = code => Object.values(latest).filter(o => o.signal_code === code);
  const list = (items, max) => items.slice(0, max).map(o =>
    (o.evidence_url ? link(o.evidence_url, o.value_text || String(o.value_num)) : escHtml(o.value_text || String(o.value_num)))
  ).join('<br>');
  const people = by('DECISION_MAKER_TITLE').length ? by('DECISION_MAKER_TITLE') : by('CONTACT_FOUND');
  const form = by('CONTACT_FORM_URL')[0];
  const dossier = by('REGISTRY_VERIFIED');
  const dossierHtml = dossier.slice(0, 12).map(o => {
    const val = escHtml(o.value_text || String(o.value_num || ''));
    const src = (o.evidence_url && safeHref(o.evidence_url))
      ? ' <span class="muted">(' + link(o.evidence_url, 'source') + ')</span>'
      : '';
    return val + src;
  }).join('<br>');
  const shown = new Set(['DECISION_MAKER_TITLE', 'CONTACT_FOUND', 'CONTACT_FORM_URL', 'HIRING_ROLE', 'FOUND_IN_SERP', 'TECH_STACK', 'REGISTRY_VERIFIED']);
  const other = Object.values(latest).filter(o => !shown.has(o.signal_code));
  const companyId = c.id || company.id || '';
  const companyDomain = c.domain || company.domain || '';
  let html = '<div class="card" style="margin-bottom:16px"><div class="card-head" style="flex-wrap:wrap;gap:10px">' +
    '<div><h3>' + escHtml(c.name || c.domain) + '</h3>' +
    (typeof company.fit === 'number' ? '<span class="meta">Fit ' + company.fit + ' / 100</span>' : '') + '</div>' +
    '<div class="page-actions">' +
      '<button class="btn btn-primary btn-sm" onclick="runCompanyResearch(' + jsq(companyDomain) + ', ' + jsq(companyId) + ', this)">Deep Research</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="runCompanyEnrichment(' + jsq(companyId) + ', this)">Run AI Columns</button>' +
    '</div></div>' +
    '<table><tbody>' +
    row('Website', link(website, c.domain || website)) +
    row('Sector', escHtml(c.industry)) +
    row('Area', escHtml(c.location)) +
    row('Why selected', escHtml(reg.why_selected || (company.fit_reasons || []).join(' · '))) +
    row('Status', '<b>' + escHtml(reg.status || company.register_status || '') + '</b>' +
      (reg.why_not_contacted || company.register_why ? ' — ' + escHtml(reg.why_not_contacted || company.register_why) : '')) +
    row('Deep Research Dossier', dossierHtml) +
    row('People named on site', list(people, 8)) +
    row('Contact form', form ? link(form.value_text, form.value_text) : '') +
    row('Hiring', list(by('HIRING_ROLE'), 6)) +
    row('Tech on website', escHtml(by('TECH_STACK').map(o => o.value_text).slice(0, 1).join(''))) +
    row('Other signals', other.slice(0, 8).map(o => escHtml(o.label) + ': ' +
      escHtml(o.value_text || String(o.value_num))).join('<br>')) +
    row('Found via', c.source_url ? link(c.source_url, 'search result') + ' · ' + escHtml(c.source || '') : escHtml(c.source || '')) +
    row('First found', escHtml((c.created_at || '').slice(0, 10))) +
    '</tbody></table></div>';
  return html;
}

async function runBatchResearch(btn) {
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/research/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ limit: 5 }),
    });
    if (res && res.success) {
      showToast('Deep research completed for top companies.', 'success');
      loadCompanies();
    } else {
      showToast((res && res.message) || 'Deep research failed.', 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function runCompanyResearch(domain, companyId, btn) {
  if (btn) btn.disabled = true;
  try {
    const payload = {};
    if (domain) payload.domain = domain;
    if (companyId) payload.company_id = companyId;
    const res = await api('/api/research/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (res && res.success) {
      showToast('Deep research completed.', 'success');
      if (companyDrill && _currentCompanyIndex >= 0) {
        showCompanyContacts(_currentCompanyIndex);
      } else {
        loadCompanies();
      }
    } else {
      showToast((res && res.message) || 'Deep research failed.', 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function runCompanyEnrichment(companyId, btn) {
  if (!companyId) return;
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/enrichment/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ company_ids: [companyId], limit: 1 }),
    });
    if (res && res.success) {
      showToast('AI column enrichment completed.', 'success');
      const enrEl = document.getElementById('enrichment');
      if (enrEl && enrEl.classList.contains('active')) loadEnrichment();
    } else {
      showToast((res && res.message) || 'AI column enrichment failed.', 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function showCompanyContacts(index) {
  const company = _companies[index];
  if (!company) return;
  _currentCompanyIndex = index;
  companyDrill = true;
  const el = document.getElementById('companies-list');
  el.innerHTML = loadingState();
  const [data, profile] = await Promise.all([
    api('/api/companies/' + encodeURIComponent(company.id) + '/contacts'),
    api('/api/companies/' + encodeURIComponent(company.id) + '/profile'),
  ]);
  if (!data) {
    el.innerHTML = '<button class="btn btn-ghost btn-sm" onclick="loadCompanies()" style="margin-bottom:12px">' +
      '&larr; All companies</button>' + offlineState();
    return;
  }
  let html = '<button class="btn btn-ghost btn-sm" onclick="loadCompanies()" style="margin-bottom:12px">' +
    '&larr; All companies</button>' + companyInfoCard(company, profile) +
    '<div class="card flush"><div class="card-head"><h3>Contacts</h3>' +
    '<span class="meta">' + escHtml([company.industry, company.location].filter(Boolean).join(' · ')) + '</span></div>';
  if (!data || !data.length) {
    html += emptyState(icon('contacts'), 'No contacts here yet',
      'VeloReach hasn\'t found a person at this company. It reads the team and about ' +
      'pages of each site it profiles — try <b>Read websites</b> on the Discover tab.') + '</div>';
  } else {
    html += '<table><thead><tr><th>Name</th><th>Email</th><th>Phone</th><th>LinkedIn</th><th>Status</th><th>Source</th></tr></thead><tbody>';
    for (const p of data) {
      const phoneIcon = p.phone_verified ? ' <span class="muted">&#10003;</span>' : '';
      html += '<tr><td><div class="cell-row"><span class="avatar">' + initials(p.first_name, p.last_name) + '</span>' +
          '<div class="cell-stack"><span class="cell-main">' + escHtml(p.first_name) + ' ' + escHtml(p.last_name) + '</span>' +
          '<span class="cell-sub">' + escHtml(p.title) + '</span></div></div></td>' +
        '<td>' + escHtml(p.email) + emailTag(p) + '</td>' +
        '<td>' + escHtml(p.phone) + phoneIcon + '</td>' +
        '<td>' + (safeHref(p.linkedin_url) ? '<a href="' + safeHref(p.linkedin_url) + '" target="_blank" rel="noopener noreferrer">Profile</a>' : '') + '</td>' +
        '<td>' + badge(p.status) + '</td><td class="muted">' + sourceLabel(p.source) + '</td></tr>';
    }
    html += '</tbody></table></div>';
  }
  el.innerHTML = html;
}

async function submitFeedback(entityType, entityId, promptText) {
  const comment = prompt(promptText || 'Add your feedback:');
  if (!comment) return;
  const data = await api('/api/feedback', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({entity_type: entityType, entity_id: entityId, comment: comment})
  });
  if (data && data.success) showToast(entityType === 'campaign'
    ? 'Saved. VeloReach follows it in the emails it writes from now on.'
    : 'Saved. VeloReach uses it if it writes this person\'s first email (Gmail, SMTP or Microsoft 365).', 'success');
  else showToast((data && data.message) || 'Could not save feedback.', 'error');
}

function fbProspect(i) {
  const p = _prospects[i];
  if (p) submitFeedback('contact', p.id, 'Anything VeloReach should know before writing to this person?');
}

function fbCampaign(i) {
  const c = _campaigns[i];
  if (c) submitFeedback('campaign', c.id, 'What should VeloReach do differently in the emails it writes next? (e.g. "shorter; don\'t mention reviews")');
}

// Search and status filter live outside the table, so the 15-second refresh
// re-renders rows without losing what you typed.
let _contactQuery = '', _contactFilter = 'all', _contactTag = '';
let _contactSelected = new Set();
let _armedBulkDelete = false;
const CONTACT_FILTERS = [
  ['all', 'All', null],
  ['todo', 'To write', ['new', 'queued']],
  ['contacted', 'Contacted', ['contacted']],
  ['replied', 'Replied', ['replied', 'interested', 'meeting']],
  ['closed', 'Closed out', null],
];

let _suppressions = [];
let _suppressionsOpen = false;
let _armedDeleteContactId = null;

function parseTags(str) {
  return String(str || '').split(',').map(t => t.trim().toLowerCase()).filter(Boolean);
}

async function loadProspects(ensureId) {
  _armedDeleteContactId = null;
  _armedBulkDelete = false;
  const el = document.getElementById('prospects-table');
  const qs = ensureId ? '?prospect_id=' + encodeURIComponent(ensureId) : '';
  const [data] = await Promise.all([
    api('/api/prospects' + qs),
    loadSuppressions(_suppressionsOpen),
  ]);
  if (!data) { el.innerHTML = offlineState(); return; }
  _prospects = data;
  const validIds = new Set(data.map(p => p.id));
  _contactSelected = new Set([..._contactSelected].filter(id => validIds.has(id)));
  if (!data.length) {
    el.innerHTML = emptyState(icon('contacts'), 'No contacts yet',
      'Add a contact manually with <b>+ Add Contact</b>, upload a list with <b>+ Import CSV</b>, or run <b>Scout</b> from Today.');
    return;
  }
  if (!document.getElementById('contact-rows')) {
    el.innerHTML = '<div class="toolbar">' +
      '<label class="search">' + icon('discover') +
        '<input class="form-input" id="contact-search" type="search" placeholder="Search name, company, email, tags" ' +
        'aria-label="Search contacts" oninput="_contactQuery=this.value;renderContacts()"></label>' +
      '<select class="form-input toolbar-select" id="contact-tag-filter" aria-label="Filter by tag" ' +
        'onchange="_contactTag=this.value;renderContacts()"><option value="">All tags</option></select>' +
      '<div class="segmented" id="contact-filters" role="group" aria-label="Filter by status"></div>' +
      '</div><div id="contacts-bulk-bar"></div><div id="contact-rows"></div>';
    // A rebuild after an offline blip must show the search still applied.
    document.getElementById('contact-search').value = _contactQuery;
  }
  renderContacts();
}

function openAddContactModal() {
  ['ac-first', 'ac-last', 'ac-email', 'ac-title', 'ac-company', 'ac-domain', 'ac-phone', 'ac-tags'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  const st = document.getElementById('ac-status');
  if (st) st.value = 'verified';
  const d = document.getElementById('add-contact-dialog');
  if (d && d.showModal) d.showModal();
}

function closeAddContactModal() {
  const d = document.getElementById('add-contact-dialog');
  if (d && d.close) d.close();
}

(function wireAddContactDialog() {
  const d = document.getElementById('add-contact-dialog');
  if (!d) return;
  d.addEventListener('click', e => { if (e.target === d) closeAddContactModal(); });
})();

const FREEMAIL_DOMAINS = new Set(['gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com', 'icloud.com', 'aol.com', 'proton.me', 'protonmail.com']);

function loadSampleImportCsv() {
  const el = document.getElementById('import-csv-text');
  if (!el) return;
  el.value = 'first_name,last_name,email,title,company,domain\nJane,Doe,jane@acmesaas.io,VP Sales,Acme SaaS,acmesaas.io\nAli,Khan,ali@brightdental.com,Practice Owner,Bright Dental,brightdental.com';
  el.focus();
}

async function submitAddContact(btn) {
  const email = (document.getElementById('ac-email')?.value || '').trim();
  if (!email) { showToast('Work email is required.', 'error'); return; }
  let domain = (document.getElementById('ac-domain')?.value || '').trim();
  if (!domain && email.includes('@')) {
    const dom = email.split('@').pop().toLowerCase();
    if (dom && !FREEMAIL_DOMAINS.has(dom)) domain = dom;
  }
  const payload = {
    first_name: (document.getElementById('ac-first')?.value || '').trim(),
    last_name: (document.getElementById('ac-last')?.value || '').trim(),
    email,
    title: (document.getElementById('ac-title')?.value || '').trim(),
    company: (document.getElementById('ac-company')?.value || '').trim(),
    domain,
    email_status: (document.getElementById('ac-status')?.value || 'verified').trim(),
    phone: (document.getElementById('ac-phone')?.value || '').trim(),
    tags: (document.getElementById('ac-tags')?.value || '').trim(),
  };
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/prospects', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if (res && res.success) {
      showToast('Contact saved.', 'success');
      closeAddContactModal();
      await loadProspects(res.id);
    } else {
      showToast(failMsg(res, 'Could not save contact.'), 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function toggleSuppressionsPanel() {
  _suppressionsOpen = !_suppressionsOpen;
  const panel = document.getElementById('suppressions-panel');
  if (!panel) return;
  panel.style.display = _suppressionsOpen ? 'block' : 'none';
  if (_suppressionsOpen) loadSuppressions(true);
}

async function loadSuppressions(renderPanel) {
  const data = await api('/api/suppressions');
  if (!data || !Array.isArray(data.items)) return;
  _suppressions = data.items;
  const btn = document.getElementById('suppressions-toggle-btn');
  if (btn) btn.textContent = 'Suppressions (' + _suppressions.length + ')';
  if (renderPanel) renderSuppressionsPanel();
}

function renderSuppressionsPanel() {
  const panel = document.getElementById('suppressions-panel');
  if (!panel) return;
  const rowsHtml = _suppressions.length
    ? '<div class="table-card" style="margin-top:10px;"><table class="nowrap"><thead><tr>' +
      '<th>Type</th><th>Address or Domain</th><th>Reason</th><th>Source</th><th>Added</th><th></th>' +
      '</tr></thead><tbody>' +
      _suppressions.map(s =>
        '<tr><td><span class="tag">' + escHtml(s.kind) + '</span></td>' +
        '<td><code>' + escHtml(s.value) + '</code></td>' +
        '<td>' + escHtml(s.reason || 'manual') + '</td>' +
        '<td class="muted">' + escHtml(s.source || '—') + '</td>' +
        '<td class="muted">' + relTime(s.created_at) + '</td>' +
        '<td><button class="btn btn-ghost btn-sm" onclick="removeSuppressionEntry(' + jsq(s.id) + ', this)">Remove</button></td></tr>'
      ).join('') + '</tbody></table></div>'
    : '<p class="muted" style="margin-top:8px;">No suppressed addresses or domains yet.</p>';

  panel.innerHTML = '<div class="card">' +
    '<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">' +
      '<div><h3 style="margin:0;">Do-Not-Contact &amp; Suppression List</h3>' +
      '<p class="muted" style="margin:4px 0 0;font-size:12px;">Emails and domains here are skipped by Scout and blocked at the send gate.</p></div>' +
      '<button class="btn btn-ghost btn-sm" onclick="toggleSuppressionsPanel()">Hide</button>' +
    '</div>' +
    '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;align-items:center;">' +
      '<input type="text" class="form-input" id="sup-value" placeholder="user@company.com or competitor.com" style="flex:1;min-width:220px;">' +
      '<select class="form-input" id="sup-reason" style="width:auto;">' +
        '<option value="manual">Manual DNC</option>' +
        '<option value="opted_out">Opted out</option>' +
        '<option value="bounced">Bounced</option>' +
        '<option value="complaint">Complaint</option>' +
      '</select>' +
      '<button class="btn btn-primary btn-sm" onclick="addSuppressionEntry(this)">Block</button>' +
    '</div>' +
    rowsHtml + '</div>';
}

async function addSuppressionEntry(btn) {
  const valEl = document.getElementById('sup-value');
  const reasonEl = document.getElementById('sup-reason');
  const value = (valEl?.value || '').trim();
  if (!value) { showToast('Enter an email address or domain first.', 'error'); return; }
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/suppressions', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({value, reason: reasonEl?.value || 'manual'}),
    });
    if (res && res.success) {
      if (valEl) valEl.value = '';
      showToast('Blocked ' + res.value + (res.matched_prospects ? ' (' + res.matched_prospects + ' contact updated)' : '') + '.', 'success');
      await Promise.all([loadSuppressions(true), loadProspects()]);
    } else {
      showToast(failMsg(res, 'Could not add suppression.'), 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function removeSuppressionEntry(id, btn) {
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/suppressions/' + encodeURIComponent(id), {method: 'DELETE'});
    if (res && res.success) {
      showToast('Removed from suppression list.', 'success');
      await loadSuppressions(true);
    } else {
      showToast(failMsg(res, 'Could not remove suppression.'), 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function suppressContact(i) {
  const p = _prospects[i];
  if (!p || !p.email) { showToast('Contact has no email to suppress.', 'error'); return; }
  const res = await api('/api/suppressions', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({kind: 'address', value: p.email, reason: 'manual'}),
  });
  if (res && res.success) {
    showToast('Added ' + p.email + ' to suppression list.', 'success');
    await Promise.all([loadSuppressions(_suppressionsOpen), loadProspects()]);
  } else {
    showToast(failMsg(res, 'Could not suppress contact.'), 'error');
  }
}

async function deleteContact(i) {
  const p = _prospects[i];
  if (!p) return;
  if (_armedDeleteContactId !== p.id) {
    _armedDeleteContactId = p.id;
    renderContacts();
    return;
  }
  _armedDeleteContactId = null;
  const res = await api('/api/prospects/' + encodeURIComponent(p.id), {method: 'DELETE'});
  if (res && res.success) {
    showToast('Deleted contact.', 'success');
    await loadProspects();
  } else {
    showToast(failMsg(res, 'Could not delete contact.'), 'error');
  }
}

function openImportCsvModal() {
  const fileEl = document.getElementById('import-csv-file');
  const textEl = document.getElementById('import-csv-text');
  if (fileEl) fileEl.value = '';
  if (textEl) textEl.value = '';
  const d = document.getElementById('import-csv-dialog');
  if (d && d.showModal) d.showModal();
}

function closeImportCsvModal() {
  const d = document.getElementById('import-csv-dialog');
  if (d && d.close) d.close();
}

(function wireImportCsvDialog() {
  const d = document.getElementById('import-csv-dialog');
  if (!d) return;
  d.addEventListener('click', e => { if (e.target === d) closeImportCsvModal(); });
})();

function handleImportCsvFile(input) {
  const file = input && input.files && input.files[0];
  if (!file || typeof FileReader === 'undefined') return;
  const reader = new FileReader();
  reader.onload = () => {
    const textEl = document.getElementById('import-csv-text');
    if (textEl) textEl.value = String(reader.result || '');
  };
  reader.readAsText(file);
}

async function submitImportCsv(btn) {
  const textEl = document.getElementById('import-csv-text');
  const csvText = ((textEl && textEl.value) || '').trim();
  if (!csvText) {
    showToast('Select a CSV file or paste CSV rows first.', 'error');
    return;
  }
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/prospects/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ csv_text: csvText }),
    });
    if (res && res.success) {
      showToast(
        'Imported ' + (res.imported || 0) + ' contacts (' +
        (res.skipped_duplicate || 0) + ' duplicates, ' +
        (res.skipped_suppressed || 0) + ' suppressed, ' +
        (res.skipped_invalid || 0) + ' invalid skipped).',
        'success'
      );
      closeImportCsvModal();
      loadProspects();
    } else {
      showToast((res && res.message) || 'CSV import failed.', 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function contactMatches(p, f) {
  const known = ['new', 'queued', 'contacted', 'replied', 'interested', 'meeting'];
  if (f === 'all') return true;
  if (f === 'closed') return !known.includes(p.status);
  const hit = CONTACT_FILTERS.find(x => x[0] === f);
  return !!(hit && hit[2] && hit[2].includes(p.status));
}

function setContactFilter(f) { _contactFilter = f; renderContacts(); }

function renderContactsBulkBar() {
  const bar = document.getElementById('contacts-bulk-bar');
  if (!bar) return;
  if (!_contactSelected.size) { bar.innerHTML = ''; return; }
  const count = _contactSelected.size;
  bar.innerHTML = '<div class="contacts-bulk-bar">' +
    '<span class="bulk-count">' + count + ' selected</span>' +
    '<input type="text" class="form-input form-input-sm" id="bulk-tag-input" placeholder="tag (e.g. saas, vip)" autocomplete="off">' +
    '<button class="btn btn-secondary btn-sm" onclick="bulkContactAction(\'tag\', this)">Add Tag</button>' +
    '<button class="btn btn-ghost btn-sm" onclick="bulkContactAction(\'untag\', this)">Remove Tag</button>' +
    '<button class="btn btn-secondary btn-sm" onclick="bulkContactAction(\'suppress\', this)">Suppress</button>' +
    '<button class="btn btn-secondary btn-sm" onclick="bulkContactAction(\'delete\', this)">' +
      (_armedBulkDelete ? 'Confirm Delete ' + count + '?' : 'Delete') + '</button>' +
    '<button class="btn btn-ghost btn-sm" onclick="_contactSelected.clear();_armedBulkDelete=false;renderContacts()">Clear</button>' +
  '</div>';
}

function toggleContactSelect(id, checked) {
  _armedBulkDelete = false;
  if (checked) _contactSelected.add(id);
  else _contactSelected.delete(id);
  renderContacts();
}

function toggleSelectAllVisibleContacts(checked) {
  _armedBulkDelete = false;
  const q = _contactQuery.trim().toLowerCase();
  const tagOk = p => !_contactTag || parseTags(p.tags).includes(_contactTag);
  const found = p => !q || [p.first_name, p.last_name, p.company, p.email, p.title, p.tags].join(' ').toLowerCase().includes(q);
  const visible = _prospects.filter(p => contactMatches(p, _contactFilter) && tagOk(p) && found(p));
  for (const p of visible) {
    if (checked) _contactSelected.add(p.id);
    else _contactSelected.delete(p.id);
  }
  renderContacts();
}

async function bulkContactAction(action, btn) {
  const ids = [..._contactSelected];
  if (!ids.length) return;
  if (action === 'delete' && !_armedBulkDelete) {
    _armedBulkDelete = true;
    renderContactsBulkBar();
    return;
  }
  const tagVal = (document.getElementById('bulk-tag-input')?.value || '').trim();
  if ((action === 'tag' || action === 'untag') && !tagVal) {
    showToast('Enter a tag name first.', 'error');
    return;
  }
  _armedBulkDelete = false;
  if (btn) btn.disabled = true;
  try {
    const payload = {ids, action};
    if (action === 'tag' || action === 'untag') payload.tags = tagVal;
    const res = await api('/api/prospects/bulk', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if (res && res.success) {
      if (action === 'delete') _contactSelected.clear();
      showToast('Updated ' + (res.count || 0) + ' contact(s) (' + action + ').', 'success');
      await loadProspects();
    } else {
      showToast(failMsg(res, 'Bulk contact action failed.'), 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderContacts() {
  const rowsEl = document.getElementById('contact-rows');
  const filtersEl = document.getElementById('contact-filters');
  const tagSel = document.getElementById('contact-tag-filter');
  if (!rowsEl) return;
  if (tagSel) {
    const allTags = [...new Set(_prospects.flatMap(p => parseTags(p.tags)))].sort();
    if (_contactTag && !allTags.includes(_contactTag)) _contactTag = '';
    tagSel.innerHTML = '<option value="">All tags</option>' +
      allTags.map(t => '<option value="' + escHtml(t) + '"' + (t === _contactTag ? ' selected' : '') + '>' + escHtml(t) + '</option>').join('');
  }
  const q = _contactQuery.trim().toLowerCase();
  const tagOk = p => !_contactTag || parseTags(p.tags).includes(_contactTag);
  const found = p => !q || [p.first_name, p.last_name, p.company, p.email, p.title, p.tags].join(' ').toLowerCase().includes(q);
  filtersEl.innerHTML = CONTACT_FILTERS.map(f =>
    '<button class="' + (f[0] === _contactFilter ? 'on' : '') + '" aria-pressed="' + (f[0] === _contactFilter) + '" onclick="setContactFilter(\'' + f[0] + '\')">' +
      f[1] + ' <span class="n">' + _prospects.filter(p => contactMatches(p, f[0]) && tagOk(p) && found(p)).length + '</span></button>'
  ).join('');

  renderContactsBulkBar();

  const rows = _prospects.map((p, i) => [p, i]).filter(([p]) => contactMatches(p, _contactFilter) && tagOk(p) && found(p));
  if (!rows.length) {
    rowsEl.innerHTML = '<div class="table-card">' + emptyState(icon('discover'), 'No matches',
      'Nothing matches that search and filter.') + '</div>';
    return;
  }
  const allVisibleSelected = rows.length > 0 && rows.every(([p]) => _contactSelected.has(p.id));
  let html = '<div class="table-card"><table class="contacts-table-fit"><thead><tr>' +
    '<th class="col-check"><input type="checkbox" aria-label="Select all visible contacts"' +
      (allVisibleSelected ? ' checked' : '') + ' onchange="toggleSelectAllVisibleContacts(this.checked)"></th>' +
    '<th class="col-name">Name</th><th class="col-company">Company &amp; Tags</th><th class="col-email">Email</th>' +
    '<th>Score</th><th>Status</th><th>Source</th><th></th></tr></thead><tbody>';
function filterByContactTag(tag) {
  _contactTag = (_contactTag === tag ? '' : tag);
  renderContacts();
}

  for (const [p, i] of rows) {
    const tags = parseTags(p.tags);
    const tagPills = tags.length
      ? '<div class="tag-pills">' + tags.map(t =>
          '<button type="button" class="tag-pill' + (t === _contactTag ? ' active' : '') +
          '" onclick="filterByContactTag(' + jsq(t) + ')" title="Filter by #' + escHtml(t) + '">' + escHtml(t) + '</button>'
        ).join('') + '</div>'
      : '';
    html += '<tr>' +
      '<td class="col-check"><input type="checkbox" aria-label="Select ' + escHtml(p.email || 'contact') + '"' +
        (_contactSelected.has(p.id) ? ' checked' : '') + ' onchange="toggleContactSelect(' + jsq(p.id) + ',this.checked)"></td>' +
      '<td class="col-name"><div class="cell-row"><span class="avatar">' + initials(p.first_name, p.last_name) + '</span>' +
        '<div class="cell-stack"><span class="cell-main"><a href="#" onclick="openContactDetailModal(' + jsq(p.id) + ');return false">' +
          escHtml([p.first_name, p.last_name].filter(Boolean).join(' ') || p.email || 'Unnamed') + '</a></span>' +
        '<span class="cell-sub">' + escHtml(p.title) + '</span></div></div></td>' +
      '<td class="col-company"><span>' + escHtml(p.company) + '</span>' + tagPills + '</td>' +
      '<td class="col-email"><div class="cell-stack"><span>' + escHtml(p.email) + '</span>' +
        '<span class="cell-sub">' + [emailTag(p).trim(), p.phone ? escHtml(p.phone) +
          (p.phone_verified ? ' <span class="verified" title="Phone verified">&#10003;</span>' : '') : '']
          .filter(Boolean).join(' · ') + '</span>' +
        '</div></td>' +
      '<td>' + (p.score || p.score === 0 ? meterHtml(p.score) : '') + '</td>' +
      '<td>' + badge(p.status) + '</td>' +
      '<td class="muted"><div class="cell-stack"><span>' + sourceLabel(p.source) + '</span>' +
        '<span class="cell-sub">' + relTime(p.created_at) + '</span></div></td>' +
      '<td class="actions-td"><div class="cell-row">' + (p.status === 'opted_out' || p.opt_out_undone || _optOut.id === p.id
        ? '<button class="btn btn-ghost btn-sm" onclick="toggleOptOut(' + i + ')" ' +
          'aria-expanded="' + (_optOut.id === p.id) + '" title="What made VeloReach stop emailing them">' +
          (p.status === 'opted_out' ? 'Why?' : 'Opt-out history') + '</button>' : '') +
        '<button class="btn btn-ghost btn-sm row-action" onclick="openContactDetailModal(' + jsq(p.id) + ')" title="Edit contact & view email timeline">Edit</button>' +
        (p.email && p.status !== 'opted_out'
          ? '<button class="btn btn-ghost btn-sm row-action" onclick="suppressContact(' + i + ')" title="Add to Do-Not-Contact suppression list">Suppress</button>' : '') +
        '<button class="btn btn-ghost btn-sm row-action" onclick="deleteContact(' + i + ')" title="Remove contact and cancel queued drafts">' +
          (_armedDeleteContactId === p.id ? 'Confirm delete?' : 'Delete') + '</button>' +
        '<button class="btn btn-ghost btn-sm row-action" onclick="fbProspect(' + i + ')" ' +
        'title="Leave feedback on this contact">Feedback</button></div></td></tr>';
    if (_optOut.id === p.id) html += '<tr class="detail-row"><td colspan="8">' + optOutPanel(p) + '</td></tr>';
  }
  rowsEl.innerHTML = html + '</tbody></table></div>' +
    '<div class="table-foot">' + rows.length + ' of ' + _prospects.length + ' contacts</div>';
}

let _editingContactId = null;
async function openContactDetailModal(prospectId) {
  _editingContactId = prospectId;
  const histEl = document.getElementById('cd-history');
  if (histEl) histEl.innerHTML = '<div class="hint muted">Loading profile &amp; activity…</div>';
  const d = document.getElementById('contact-detail-dialog');
  if (d && d.showModal) d.showModal();
  const data = await api('/api/prospects/' + encodeURIComponent(prospectId));
  if (!data || !data.prospect) {
    if (histEl) histEl.innerHTML = '<div class="hint muted">Could not load contact details.</div>';
    return;
  }
  const p = data.prospect;
  const setV = (id, v) => { const el = document.getElementById(id); if (el) el.value = v == null ? '' : String(v); };
  setV('cd-first', p.first_name || '');
  setV('cd-last', p.last_name || '');
  setV('cd-email', p.email || '');
  setV('cd-phone', p.phone || '');
  setV('cd-title', p.title || '');
  setV('cd-company', p.company || '');
  setV('cd-email-status', p.email_status || 'verified');
  setV('cd-status', p.status || 'new');
  setV('cd-tags', p.tags || '');
  if (histEl) {
    const out = (data.outbox || []).map(o =>
      '<div class="cd-hist-item"><div class="cd-hist-head"><b>' + escHtml(o.subject || '(no subject)') + '</b>' +
        '<span>' + badge(o.status) + (o.variant === 'B' ? ' <span class="mini">Var B</span>' : '') + '</span></div>' +
        '<div class="muted">' + escHtml(String(o.body || '').slice(0, 140)) + '</div></div>'
    );
    const convs = (data.conversations || []).map(c =>
      '<div class="cd-hist-item"><div class="cd-hist-head"><b>Reply thread (' + escHtml(c.stage || c.status) + ')</b>' +
        badge(c.status) + '</div><div class="muted">' + (c.messages || []).length + ' message(s)</div></div>'
    );
    histEl.innerHTML = (out.length || convs.length)
      ? out.concat(convs).join('')
      : '<div class="hint muted">No emails or replies recorded for this contact yet.</div>';
  }
}
function closeContactDetailModal() {
  _editingContactId = null;
  const d = document.getElementById('contact-detail-dialog');
  if (d && d.close) d.close();
}
(function wireContactDetailDialog() {
  const d = document.getElementById('contact-detail-dialog');
  if (!d) return;
  d.addEventListener('click', e => { if (e.target === d) closeContactDetailModal(); });
})();
async function saveContactDetail(btn) {
  if (!_editingContactId) return;
  const val = id => (document.getElementById(id)?.value || '').trim();
  if (!val('cd-email')) { showToast('Work email is required.', 'error'); return; }
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/prospects/' + encodeURIComponent(_editingContactId), {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        first_name: val('cd-first'),
        last_name: val('cd-last'),
        email: val('cd-email'),
        phone: val('cd-phone'),
        title: val('cd-title'),
        company: val('cd-company'),
        email_status: val('cd-email-status') || 'verified',
        status: val('cd-status') || 'new',
        tags: val('cd-tags'),
      }),
    });
    if (res && res.success) {
      showToast('Contact updated.', 'success');
      closeContactDetailModal();
      await loadProspects();
    } else {
      showToast(failMsg(res, 'Could not update contact.'), 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Why someone is opted out, and undoing it. VeloReach never moves anyone off
// "opted out" on its own, so a mistaken one (a bare "stop" that meant
// something else) needs you: see what triggered it, then undo it here.
let _optOut = {id: null, data: null, armed: false, busy: false};
const OPT_OUT_RULES = {
  phrase: 'Matched an opt-out phrase',
  no_thanks: 'Matched “no thanks”',
  only_words: 'Their reply was only an opt-out word',
  model: 'The model’s judgement (no phrase matched)',
};

async function toggleOptOut(i) {
  const p = _prospects[i];
  if (!p) return;
  if (_optOut.id === p.id) { _optOut = {id: null, data: null, armed: false, busy: false}; renderContacts(); return; }
  _optOut = {id: p.id, data: null, armed: false, busy: false};
  renderContacts();
  const data = await api('/api/prospects/' + encodeURIComponent(p.id) + '/opt-out');
  if (_optOut.id !== p.id) return;   // closed or switched meanwhile
  _optOut.data = data || {error: true};
  renderContacts();
}

function optOutPanel(p) {
  const d = _optOut.data;
  const name = escHtml([p.first_name, p.last_name].filter(Boolean).join(' ') || p.email);
  if (!d) return '<div class="optout-panel muted">Loading…</div>';
  if (d.error) return '<div class="optout-panel muted">Couldn’t load the opt-out record. Try again.</div>';
  let html = '<div class="optout-panel"><h4>' + (d.opted_out ? 'Why VeloReach stopped emailing ' : 'Opt-out history for ') +
    name + '</h4>';
  if (d.recorded) {
    html += '<p>' + escHtml(d.why) + '</p><div class="optout-meta">' +
      escHtml(own(OPT_OUT_RULES, d.rule) || d.rule || '') + (d.at ? ' · ' + relTime(d.at) : '') + '</div>';
  } else if (d.opted_out || (d.history || []).length) {
    html += '<p>They opted out before VeloReach kept a record of why.' +
      (d.message ? ' The last thing they wrote:' : ' There’s no reply from them on file.') + '</p>';
  }
  if (d.message) {
    html += '<div class="thread-msg received"><div class="sender">' + escHtml(p.first_name || 'They') +
      (d.at ? ' &middot; ' + relTime(d.at) : '') + '</div>' + escHtml(d.message) + '</div>';
  }
  const undone = (d.history || []).filter(h => h.action === 'opt_out_undone');
  if (undone.length) {
    html += '<div class="optout-meta">' + undone.map(h => 'You undid an opt-out ' + relTime(h.at)).join(' · ') + '</div>';
  }
  if (!d.opted_out) {
    html += '<div class="convo-actions"><span class="muted">Not opted out now (' +
      escHtml(statusMeta(d.status).label) + ').</span></div>';
  } else if (_optOut.armed) {
    html += '<div class="convo-actions"><span class="muted">VeloReach will treat them as having replied. Nothing is ' +
      'sent automatically, emails the opt-out cancelled stay cancelled, and VeloReach’s answer to their next ' +
      'reply waits for your approval, even on autopilot. Only undo it if they didn’t mean to opt out.</span>' +
      '<button class="btn btn-primary btn-sm" onclick="undoOptOut()"' + (_optOut.busy ? ' disabled' : '') +
        '>Confirm undo</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="_optOut.armed=false;renderContacts()">Cancel</button></div>';
  } else {
    html += '<div class="convo-actions"><button class="btn btn-secondary btn-sm" ' +
      'onclick="_optOut.armed=true;renderContacts()">Undo opt-out</button></div>';
  }
  return html + '</div>';
}

async function undoOptOut() {
  const id = _optOut.id;
  if (!id || _optOut.busy) return;
  _optOut.busy = true;
  renderContacts();
  const res = await api('/api/prospects/' + encodeURIComponent(id) + '/opt-out/undo', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({confirm: true}),
  });
  if (res && res.success) showToast('Opt-out undone. They’re marked as replied; VeloReach sends nothing on its own.', 'success');
  else showToast(failMsg(res, 'Could not undo the opt-out. Try again.'), 'error');
  if (_optOut.id !== id) return;
  _optOut.armed = false;
  _optOut.busy = false;
  _optOut.data = await api('/api/prospects/' + encodeURIComponent(id) + '/opt-out') || {error: true};
  loadProspects();
}

// From a conversation: the contact, with the opt-out record open.
async function showOptOut(prospectId, email) {
  _contactFilter = 'all';
  _contactQuery = email || '';
  goTab('prospects', true);
  showTabLoading('prospects');
  await loadProspects(prospectId);
  const search = document.getElementById('contact-search');
  if (search) search.value = _contactQuery;
  const i = _prospects.findIndex(p => p.id === prospectId);
  if (i >= 0 && _optOut.id !== prospectId) toggleOptOut(i);
}

// Opens a filled-in compose window in whatever Gmail account the browser is
// logged into. Nothing is sent from here — the user reads it and presses Send.
// Same values as the Sender's _render, so a hand-sent email reads the same.
function fillMergeVars(text, r) {
  const inbox = r.source === 'company_inbox';
  const vals = {
    first_name: r.first_name || (inbox ? 'there' : ''), last_name: r.last_name || '',
    company: r.company || '', title: r.title || '',
    personalization: r.personalization_notes || '',
  };
  return String(text || '').replace(/\{\{\s*(\w+)\s*\}\}/g, (m, k) => {
    k = k.toLowerCase();
    return Object.prototype.hasOwnProperty.call(vals, k) ? vals[k] : m;
  });
}

let _gmailBusy = false;  // one compose window per click, never two

async function openInGmail(ci, si, ri) {
  if (_gmailBusy) return;
  const c = _campaigns[ci];
  const step = c && (c.sequence || [])[si];
  const r = c && (c.recipients || [])[ri];
  if (!step || !r) return;
  // Cold email carries the postal address and opt-out line, sent by hand too.
  const filled = fillMergeVars(step.body, r).trimEnd();
  const body = filled + (c.footer && !filled.endsWith(c.footer) ? '\n\n' + c.footer : '');
  // authuser picks the Google account VeloReach sends as, not just whichever
  // one the browser happens to be signed into.
  const url = 'https://mail.google.com/mail/?view=cm&fs=1' +
    (c.send_as ? '&authuser=' + encodeURIComponent(c.send_as) : '') +
    '&to=' + encodeURIComponent(r.email) +
    '&su=' + encodeURIComponent(fillMergeVars(step.subject, r)) +
    '&body=' + encodeURIComponent(body);
  const left = /\{\{\s*\w+\s*\}\}/.test(fillMergeVars(step.subject, r) + fillMergeVars(step.body, r));
  // No 'noopener' feature string: with it window.open always returns null,
  // so a blocked pop-up could not be told apart from a successful one. The
  // tab opens blank right away (inside the click, so it isn't blocked) and
  // only gets the draft once the send is recorded.
  const w = window.open('', '_blank');
  if (!w) {
    showToast('Your browser blocked the Gmail tab. Allow pop-ups for this page and click again.', 'error');
    return;
  }
  w.opener = null;
  // Record the send BEFORE the user can press Send in Gmail, so VeloReach can
  // never email them as well — not even if they forget to tick the box.
  _gmailBusy = true;
  let claimed = r.sent_by_hand;
  try { if (!claimed) claimed = await markSent(ci, ri, true); } finally { _gmailBusy = false; }
  if (!claimed) { w.close(); return; }
  w.location.href = url;
  if (left) showToast('This draft still has a {{placeholder}} in it. Fix it in Gmail before sending.', 'error');
}

async function markSent(ci, ri, sent) {
  const r = (_campaigns[ci] && _campaigns[ci].recipients || [])[ri];
  if (!r) return;
  let res = null;
  try {
    const resp = await authFetch('/api/prospects/' + encodeURIComponent(r.id) + '/sent', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sent}),
    });
    if (await lockIfRefused(resp)) return;
    res = await resp.json();
  } catch {}
  const ok = !!(res && res.success);
  if (!ok) showToast((res && res.message) || 'Could not save that. Try again.', 'error');
  else if (!sent && res.sent) showToast('They already replied, so this stays marked as sent.');
  else showToast(sent ? 'Marked as sent by you.' : 'Unmarked.', 'success');
  loadCampaigns();
  // True only when this call changed it: a status means they replied or
  // opted out, 'already' means another click or tab got there first.
  return ok && sent === !!res.sent && !res.status && !res.already;
}

// Where a campaign's people stand, as one stacked bar.
function campaignProgress(c) {
  const rs = c.recipients || [];
  if (!rs.length) return '';
  const count = st => rs.filter(r => st.includes(r.status)).length;
  const parts = [
    ['Contacted', count(['contacted']), 'c-accent'],
    ['Replied', count(['replied']), 'c-active'],
    ['Interested', count(['interested', 'meeting']), 'c-good'],
    ['Waiting', count(['new', 'queued']), 'c-accent-soft'],
  ];
  const closed = rs.length - parts.reduce((x, p) => x + p[1], 0);
  if (closed > 0) parts.push(['Closed out', closed, 'c-idle']);
  return '<div class="campaign-progress"><div class="bar" role="img" aria-label="Where this campaign\'s people stand">' +
    parts.filter(p => p[1]).map(p => '<span class="' + p[2] + '" style="width:' + (100 * p[1] / rs.length) +
      '%" title="' + p[0] + ': ' + p[1] + '"></span>').join('') + '</div>' +
    '<div class="campaign-legend">' + parts.filter(p => p[1]).map(p =>
      '<span><i class="' + p[2] + '"></i>' + p[0] + ' <b>' + p[1] + '</b></span>').join('') + '</div></div>';
}

// ── Framework experiment: which email shape earns replies ──

const FRAMEWORK_LABELS = {
  pas: 'PAS — problem, agitate, solve',
  bab: 'BAB — before, after, bridge',
  direct_offer: 'Direct offer',
  question_lead: 'Question lead',
};

function renderFrameworks(data) {
  const el = document.getElementById('frameworks-card');
  if (!el) return;
  const configured = (data && data.configured) || [];
  const rows = (data && data.rows) || [];
  if (!configured.length && !rows.length) { el.innerHTML = ''; return; }
  const stats = {};
  rows.forEach(r => { stats[r.framework] = r; });
  const arms = [...new Set([...configured, ...rows.map(r => r.framework)])];
  const body = arms.map(fw => {
    const s = own(stats, fw) || {replies: 0, finished: 0};
    const rate = s.finished ? (100 * s.replies / s.finished).toFixed(1) + '%' : '—';
    return '<tr><td>' + escHtml(own(FRAMEWORK_LABELS, fw) || fw) + '</td>' +
      '<td class="num">' + s.finished + '</td><td class="num">' + s.replies + '</td>' +
      '<td class="num">' + rate + '</td></tr>';
  }).join('');
  el.innerHTML = '<section class="card frameworks-card" aria-labelledby="frameworks-h">' +
    '<h3 id="frameworks-h">Framework experiment</h3>' +
    '<p class="lede">Each campaign tests one email shape. VeloReach gives the next campaign to a shape by Thompson sampling over these real replies, so winners gradually get more traffic. “Finished” means a person replied or their sequence ran its course.</p>' +
    '<div class="table-card"><table><thead><tr><th>Framework</th><th class="num">Finished</th>' +
    '<th class="num">Replies</th><th class="num">Reply rate</th></tr></thead><tbody>' + body +
    '</tbody></table></div></section>';
}

// ── Settings: DNS and competitor watch ──

async function runDnsCheck() {
  const btn = document.getElementById('dns-run');
  const out = document.getElementById('dns-results');
  const domain = document.getElementById('dns-domain').value.trim();
  btn.disabled = true;
  out.innerHTML = '<p class="hint">Checking DNS…</p>';
  const data = await api('/api/dns-check', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({domain}),
  });
  btn.disabled = false;
  if (!data || data.success === false) {
    out.innerHTML = '<p class="hint">' + escHtml(failMsg(data, 'The DNS check could not run.')) + '</p>';
    return;
  }
  const statusClasses = {ok: 't-good', warn: 't-waiting', fail: 't-bad', info: 't-idle'};
  const checks = (data.checks || []).map(c => {
    const status = String(c.status || 'info').toLowerCase();
    return '<div class="dns-check"><span class="mini ' + (own(statusClasses, status) || 't-idle') + '">' +
      escHtml(status.toUpperCase()) + '</span><div><b>' + escHtml(c.name) + '</b>' +
      '<div class="muted">' + escHtml(c.detail) + '</div></div></div>';
  }).join('');
  out.innerHTML = '<div class="dns-summary"><b>' + escHtml(data.domain) + '</b> — ' +
    (data.failed ? data.failed + ' FAIL item(s): fix these before cold email.' : 'No FAIL items.') +
    '</div><div class="dns-checks">' + checks + '</div>';
}

let _cwWatches = [];
let _cwLoaded = false;
let _cwEnabled = false;

async function loadCompetitorWatch() {
  const data = await api('/api/competitor-watch');
  if (!data) { document.getElementById('cw-watches').innerHTML = offlineState(); return; }
  _cwWatches = (data.watches || []).map(w => ({name: w.name, url: w.url, kind: w.kind}));
  _cwLoaded = true;
  _cwEnabled = !!data.enabled;
  document.getElementById('cw-enabled').checked = _cwEnabled;
  renderCompetitorWatches();
  const changes = data.changes || [];
  document.getElementById('cw-changes').innerHTML = changes.length
    ? '<h4 class="cw-changes-title">Recent changes</h4>' + changes.map(c => {
        const href = safeHref(c.url);
        const link = href
          ? ' <a href="' + href + '" target="_blank" rel="noopener noreferrer">' + escHtml(c.url) + '</a>'
          : ' ' + escHtml(c.url);
        return '<div class="cw-change"><div><b>' + escHtml(c.watch) + '</b>' +
          ' <span class="mini t-note">' + escHtml(c.kind || 'page') + '</span></div>' +
          '<div class="muted">' + link + '</div><div class="muted">' + relTime(c.at) + '</div></div>';
      }).join('')
    : '';
}

function renderCompetitorWatches() {
  document.getElementById('cw-watches').innerHTML = _cwWatches.length
    ? _cwWatches.map((w, i) =>
        '<div class="cw-watch"><div><b>' + escHtml(w.name) + '</b> <span class="mini t-idle">' +
        escHtml(w.kind) + '</span><div class="muted">' + escHtml(w.url) + '</div></div>' +
        '<button class="btn btn-ghost btn-sm" onclick="removeCompetitorWatch(' + i + ')">Remove</button></div>'
      ).join('')
    : '<p class="hint">No watches yet — add a page below.</p>';
}

function addCompetitorWatch() {
  if (!_cwLoaded) return;
  const name = document.getElementById('cw-name').value.trim();
  const url = document.getElementById('cw-url').value.trim();
  const kind = document.getElementById('cw-kind').value;
  const status = document.getElementById('cw-status');
  if (!name || !url) { status.textContent = 'Enter both a name and a URL.'; return; }
  if (!isHttpUrl(url)) { status.textContent = 'The URL must start with https:// or http://.'; return; }
  if (_cwWatches.some(w => w.name.toLowerCase() === name.toLowerCase())) {
    status.textContent = 'Watch names must be unique.'; return;
  }
  _cwWatches.push({name, url, kind});
  document.getElementById('cw-name').value = '';
  document.getElementById('cw-url').value = '';
  status.textContent = 'Unsaved — click Save watches.';
  renderCompetitorWatches();
}

function removeCompetitorWatch(i) {
  _cwWatches.splice(i, 1);
  document.getElementById('cw-status').textContent = 'Unsaved — click Save watches.';
  renderCompetitorWatches();
}

async function saveCompetitorWatch() {
  const status = document.getElementById('cw-status');
  if (!_cwLoaded) { status.textContent = 'Still loading — try again in a moment.'; return; }
  const btn = document.getElementById('cw-save');
  btn.disabled = true;
  const data = await api('/api/competitor-watch', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({enabled: document.getElementById('cw-enabled').checked, watches: _cwWatches}),
  });
  btn.disabled = false;
  if (!data || data.success === false) {
    const chk = document.getElementById('cw-enabled');
    if (chk) chk.checked = _cwEnabled;
    status.textContent = failMsg(data, 'Could not save the watches.');
    return;
  }
  status.textContent = data.message || 'Saved.';
  await loadCompetitorWatch();
}

function renderCampaignsFunnel(stats) {
  const el = document.getElementById('campaigns-funnel');
  if (!el) return;
  const f = (stats && stats.funnel) || null;
  if (!f) { el.innerHTML = ''; return; }
  const steps = [
    ['Companies', f.companies || 0],
    ['Contacts', f.contacts || 0],
    ['Researched', f.researched || 0],
    ['Drafted', f.drafted || 0],
    ['Sent', f.sent || 0],
    ['Replied', f.replied || 0],
    ['Meetings', f.booked || 0],
  ];
  el.innerHTML = '<div class="card funnel-card" aria-label="Outreach conversion pipeline"><div class="pipeline-funnel-bar">' +
    steps.map((s, idx) =>
      (idx > 0 ? '<span class="funnel-arrow" aria-hidden="true">&rarr;</span>' : '') +
      '<div class="funnel-step"><div class="funnel-val">' + escHtml(String(s[1])) + '</div>' +
      '<div class="funnel-lbl">' + escHtml(s[0]) + '</div></div>'
    ).join('') + '</div></div>';
}

async function triggerWriterRun(btn) {
  const button = btn || document.getElementById('writer-run-btn');
  const orig = button ? button.textContent : 'Write Emails Now';
  if (button) { button.disabled = true; button.textContent = 'Writing…'; }
  try {
    const res = await api('/api/writer/run', { method: 'POST' });
    if (res && res.success) {
      showToast('Writer finished — ' + (res.pending_outbox ?? 0) + ' draft(s) in Outbox.', 'success');
      await loadCampaigns();
      loadToday();
      if (typeof loadOutbox === 'function') loadOutbox();
    } else {
      showToast(failMsg(res, 'Writer run failed.'), 'error');
    }
  } finally {
    if (button) { button.disabled = false; button.textContent = orig; }
  }
}

let _armedDeleteCampaignId = null;
let _campaignBuilder = { id: null, steps: [] };
let _templates = [];
let _editingTemplateId = null;
let _armedDeleteTemplateId = null;
let _openCampaignAnalytics = new Set();

async function ensureTemplatesLoaded(force) {
  if (_templates.length && !force) return _templates;
  const data = await api('/api/templates');
  if (data && Array.isArray(data.items)) _templates = data.items;
  return _templates;
}

function templatePickerOptions(placeholder) {
  return '<option value="">' + escHtml(placeholder || 'Insert template…') + '</option>' +
    _templates.map(t => '<option value="' + escHtml(t.id) + '">' + escHtml(t.name) +
      (t.category ? ' (' + escHtml(t.category) + ')' : '') + '</option>').join('');
}

async function openTemplatesModal() {
  resetTemplateForm();
  const d = document.getElementById('templates-dialog');
  if (d && d.showModal) d.showModal();
  await ensureTemplatesLoaded(true);
  renderTemplatesList();
}
function closeTemplatesModal() {
  const d = document.getElementById('templates-dialog');
  if (d && d.close) d.close();
}
(function wireTemplatesDialog() {
  const d = document.getElementById('templates-dialog');
  if (!d) return;
  d.addEventListener('click', e => { if (e.target === d) closeTemplatesModal(); });
})();

function renderTemplatesList() {
  const el = document.getElementById('templates-list');
  if (!el) return;
  if (!_templates.length) {
    el.innerHTML = emptyState(icon('campaigns'), 'No saved templates yet',
      'Click <b>Load Starter Pack</b> above for ready-made SaaS &amp; local templates, or create your own.');
    return;
  }
  el.innerHTML = _templates.map(t => {
    const delTxt = _armedDeleteTemplateId === t.id ? 'Confirm delete?' : 'Delete';
    return '<div class="tpl-card">' +
      '<div class="tpl-card-head">' +
        '<div><b>' + escHtml(t.name) + '</b> <span class="tag">' + escHtml(t.category || 'general') + '</span>' +
          (t.tags ? ' <span class="muted" style="font-size:12px">#' + escHtml(t.tags) + '</span>' : '') + '</div>' +
        '<div class="inline-action-row">' +
          '<button type="button" class="btn btn-primary btn-sm" onclick="useTemplateInNewCampaign(' + jsq(t.id) + ')">Use in Campaign</button>' +
          '<button type="button" class="btn btn-ghost btn-sm" onclick="editTemplate(' + jsq(t.id) + ')">Edit</button>' +
          '<button type="button" class="btn btn-ghost btn-sm" onclick="deleteTemplate(' + jsq(t.id) + ', this)">' + delTxt + '</button>' +
        '</div>' +
      '</div>' +
      '<div class="subject" style="margin-top:6px;font-weight:600">' + escHtml(t.subject) + '</div>' +
      '<div class="muted" style="font-size:12px;margin-top:4px;white-space:pre-wrap">' + escHtml(t.body) + '</div>' +
    '</div>';
  }).join('');
}

function resetTemplateForm() {
  _editingTemplateId = null;
  ['tpl-name', 'tpl-subject', 'tpl-tags', 'tpl-body'].forEach(id => {
    const el = document.getElementById(id); if (el) el.value = '';
  });
  const cat = document.getElementById('tpl-category'); if (cat) cat.value = 'cold';
  const t = document.getElementById('tpl-form-title'); if (t) t.textContent = 'Save New Template';
  const b = document.getElementById('tpl-save-btn'); if (b) b.textContent = 'Save Template';
  const c = document.getElementById('tpl-cancel-edit-btn'); if (c) c.style.display = 'none';
}

function editTemplate(id) {
  const tpl = _templates.find(x => x.id === id);
  if (!tpl) return;
  _editingTemplateId = id;
  const setV = (k, v) => { const el = document.getElementById(k); if (el) el.value = v || ''; };
  setV('tpl-name', tpl.name);
  setV('tpl-category', tpl.category || 'cold');
  setV('tpl-subject', tpl.subject);
  setV('tpl-tags', tpl.tags || '');
  setV('tpl-body', tpl.body);
  const t = document.getElementById('tpl-form-title'); if (t) t.textContent = 'Edit Template';
  const b = document.getElementById('tpl-save-btn'); if (b) b.textContent = 'Update Template';
  const c = document.getElementById('tpl-cancel-edit-btn'); if (c) c.style.display = 'inline-flex';
}

async function saveTemplateForm(btn) {
  const val = id => (document.getElementById(id)?.value || '').trim();
  const payload = {
    name: val('tpl-name'),
    category: val('tpl-category') || 'cold',
    subject: val('tpl-subject'),
    tags: val('tpl-tags'),
    body: val('tpl-body'),
  };
  if (!payload.name || !payload.subject || !payload.body) {
    showToast('Template name, subject, and body are required.', 'error');
    return;
  }
  if (btn) btn.disabled = true;
  try {
    const url = _editingTemplateId ? '/api/templates/' + encodeURIComponent(_editingTemplateId) : '/api/templates';
    const res = await api(url, {
      method: _editingTemplateId ? 'PATCH' : 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if (res && res.success) {
      showToast(_editingTemplateId ? 'Template updated.' : 'Template saved.', 'success');
      resetTemplateForm();
      await ensureTemplatesLoaded(true);
      renderTemplatesList();
    } else {
      showToast(failMsg(res, 'Could not save template.'), 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deleteTemplate(id, btn) {
  if (_armedDeleteTemplateId !== id) {
    _armedDeleteTemplateId = id;
    renderTemplatesList();
    return;
  }
  _armedDeleteTemplateId = null;
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/templates/' + encodeURIComponent(id), {method: 'DELETE'});
    if (res && res.success) {
      showToast('Template deleted.', 'success');
      await ensureTemplatesLoaded(true);
      renderTemplatesList();
    } else {
      showToast(failMsg(res, 'Could not delete template.'), 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function seedStarterTemplates(btn) {
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/templates/seed', {method: 'POST'});
    if (res && res.success) {
      _templates = res.items || [];
      renderTemplatesList();
      showToast('Loaded ' + (res.added || 0) + ' starter template(s).', 'success');
    } else {
      showToast(failMsg(res, 'Could not load starter templates.'), 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function useTemplateInNewCampaign(id) {
  const tpl = _templates.find(x => x.id === id);
  if (!tpl) return;
  closeTemplatesModal();
  openCampaignBuilderModal();
  const nameEl = document.getElementById('cb-name');
  const tagsEl = document.getElementById('cb-tags');
  if (nameEl && !nameEl.value) nameEl.value = tpl.name || '';
  if (tagsEl && !tagsEl.value && tpl.tags) tagsEl.value = tpl.tags;
  if (_campaignBuilder.steps[0]) {
    _campaignBuilder.steps[0].subject = tpl.subject || '';
    _campaignBuilder.steps[0].body = tpl.body || '';
    renderBuilderSteps();
  }
}

function insertTemplateIntoStep(idx, selectEl) {
  const id = selectEl?.value;
  if (!id) return;
  const tpl = _templates.find(x => x.id === id);
  if (tpl && _campaignBuilder.steps[idx]) {
    _campaignBuilder.steps[idx].subject = tpl.subject || '';
    _campaignBuilder.steps[idx].body = tpl.body || '';
    renderBuilderSteps();
  }
  if (selectEl) selectEl.value = '';
}

function insertTemplateIntoConvo(convoId, selectEl) {
  const id = selectEl?.value;
  if (!id) return;
  const tpl = _templates.find(x => x.id === id);
  const convo = (_convos || []).find(x => x.id === convoId) || {};
  const firstName = (convo.first_name || '').trim() || 'there';
  const company = (convo.company || '').trim() || 'your team';
  const ta = document.getElementById('convo-reply-text-' + convoId);
  if (tpl && ta) {
    ta.value = String(tpl.body || '')
      .replace(/\{\{\s*first_name\s*\}\}/gi, firstName)
      .replace(/\{\{\s*company\s*\}\}/gi, company);
    ta.focus();
  }
  if (selectEl) selectEl.value = '';
}

function copyVariantAToB(idx) {
  const st = _campaignBuilder.steps[idx];
  if (!st) return;
  st.subject_b = st.subject || '';
  st.body_b = st.body || '';
  st._showB = true;
  renderBuilderSteps();
}

function toggleStepAbFields(idx) {
  if (!_campaignBuilder.steps[idx]) return;
  _campaignBuilder.steps[idx]._showB = !_campaignBuilder.steps[idx]._showB;
  renderBuilderSteps();
}

function openCampaignBuilderModal(campaignId) {
  const existing = campaignId ? (_campaigns || []).find(c => c.id === campaignId) : null;
  _campaignBuilder = {
    id: existing ? existing.id : null,
    steps: existing && Array.isArray(existing.sequence) && existing.sequence.length
      ? existing.sequence.map((s, idx) => ({
          step: s.step || idx + 1,
          delay_days: Number(s.delay_days ?? (idx === 0 ? 0 : 3)),
          subject: s.subject || '',
          body: s.body || '',
          subject_b: s.subject_b || '',
          body_b: s.body_b || '',
          _showB: !!(s.subject_b || s.body_b),
        }))
      : [
          { step: 1, delay_days: 0, subject: 'Quick question for {{company}}', body: 'Hi {{first_name}},\n\nNoticed {{company}}...\n', subject_b: '', body_b: '', _showB: false },
          { step: 2, delay_days: 3, subject: 'Re: Quick question for {{company}}', body: 'Hi {{first_name}},\n\nWanted to bump this in case it got buried.\n', subject_b: '', body_b: '', _showB: false },
        ],
  };
  const title = document.getElementById('campaign-builder-title');
  if (title) title.textContent = existing ? 'Edit Campaign Sequence' : 'New Campaign Sequence';
  const nameEl = document.getElementById('cb-name');
  const tagsEl = document.getElementById('cb-tags');
  const fwEl = document.getElementById('cb-framework');
  const goalEl = document.getElementById('cb-goal');
  if (nameEl) nameEl.value = existing ? (existing.name || '') : '';
  if (tagsEl) tagsEl.value = existing ? (existing.target_tags || '') : '';
  if (fwEl) fwEl.value = existing ? (existing.framework || '') : '';
  if (goalEl) goalEl.value = '';
  renderBuilderSteps();
  const d = document.getElementById('campaign-builder-dialog');
  if (d && d.showModal) d.showModal();
  if (!_templates.length) {
    ensureTemplatesLoaded(false).then(items => { if (items && items.length) renderBuilderSteps(); });
  }
}

function closeCampaignBuilderModal() {
  const d = document.getElementById('campaign-builder-dialog');
  if (d && d.close) d.close();
}

(function wireCampaignBuilderDialog() {
  const d = document.getElementById('campaign-builder-dialog');
  if (!d) return;
  d.addEventListener('click', e => { if (e.target === d) closeCampaignBuilderModal(); });
})();

function renderBuilderSteps() {
  const wrap = document.getElementById('cb-steps');
  if (!wrap) return;
  wrap.innerHTML = _campaignBuilder.steps.map((s, idx) =>
    '<div class="cb-step-card">' +
      '<div class="cb-step-bar">' +
        '<span class="cb-step-label">Step ' + (idx + 1) + (idx === 0 ? ' (Initial)' : ' (Follow-up)') + '</span>' +
        '<div class="inline-action-row">' +
          (_templates.length
            ? '<select class="form-input tpl-picker" aria-label="Insert template" onchange="insertTemplateIntoStep(' + idx + ',this)">' +
                templatePickerOptions('Insert template…') + '</select>'
            : '') +
          '<button type="button" class="btn btn-ghost btn-sm" onclick="toggleStepAbFields(' + idx + ')">' +
            (s._showB ? 'Hide A/B' : '+ A/B Test') + '</button>' +
          '<label class="hint muted" style="font-size:12px">Delay (days): ' +
            '<input type="number" min="0" class="form-input cb-delay-input" value="' + escHtml(String(s.delay_days ?? 0)) + '" ' +
              'onchange="updateBuilderStepField(' + idx + ',\'delay_days\',Number(this.value||0))"></label>' +
          (_campaignBuilder.steps.length > 1
            ? '<button type="button" class="btn btn-ghost btn-sm" onclick="removeBuilderStep(' + idx + ')">Remove</button>'
            : '') +
        '</div>' +
      '</div>' +
      '<div class="form-group" style="margin-bottom:8px">' +
        '<input type="text" class="form-input" placeholder="Subject line Variant A (supports {{company}}, {{first_name}})" ' +
          'value="' + escHtml(s.subject || '') + '" oninput="updateBuilderStepField(' + idx + ',\'subject\',this.value)">' +
      '</div>' +
      '<div class="form-group" style="margin-bottom:0">' +
        '<textarea class="form-input" rows="4" placeholder="Email body Variant A..." ' +
          'oninput="updateBuilderStepField(' + idx + ',\'body\',this.value)">' + escHtml(s.body || '') + '</textarea>' +
      '</div>' +
      (s._showB
        ? '<div class="cb-ab-box">' +
            '<div class="cb-step-bar" style="margin-bottom:6px">' +
              '<span class="form-label" style="margin:0">Variant B (50/50 split on launch)</span>' +
              '<button type="button" class="btn btn-ghost btn-sm" onclick="copyVariantAToB(' + idx + ')">Copy A &rarr; B</button>' +
            '</div>' +
            '<div class="form-group" style="margin-bottom:8px">' +
              '<input type="text" class="form-input" placeholder="Subject line Variant B (optional)" ' +
                'value="' + escHtml(s.subject_b || '') + '" oninput="updateBuilderStepField(' + idx + ',\'subject_b\',this.value)">' +
            '</div>' +
            '<div class="form-group" style="margin-bottom:0">' +
              '<textarea class="form-input" rows="3" placeholder="Body Variant B (blank reuses Variant A body)" ' +
                'oninput="updateBuilderStepField(' + idx + ',\'body_b\',this.value)">' + escHtml(s.body_b || '') + '</textarea>' +
            '</div>' +
          '</div>'
        : '') +
    '</div>'
  ).join('');
}

function updateBuilderStepField(idx, field, val) {
  if (!_campaignBuilder.steps[idx]) return;
  _campaignBuilder.steps[idx][field] = val;
}

function addBuilderStep() {
  const n = _campaignBuilder.steps.length + 1;
  const lastSubj = (_campaignBuilder.steps[0] && _campaignBuilder.steps[0].subject) || '';
  _campaignBuilder.steps.push({
    step: n,
    delay_days: 3,
    subject: lastSubj ? 'Re: ' + lastSubj.replace(/^Re:\s*/i, '') : '',
    body: '',
  });
  renderBuilderSteps();
}

function removeBuilderStep(idx) {
  if (_campaignBuilder.steps.length <= 1) return;
  _campaignBuilder.steps.splice(idx, 1);
  _campaignBuilder.steps.forEach((s, i) => { s.step = i + 1; });
  renderBuilderSteps();
}

async function aiDraftCampaignSteps(btn) {
  const name = (document.getElementById('cb-name')?.value || '').trim();
  const target_tags = (document.getElementById('cb-tags')?.value || '').trim();
  const goal = (document.getElementById('cb-goal')?.value || '').trim();
  const orig = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Drafting…'; }
  try {
    const res = await api('/api/campaigns/draft-steps', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, target_tags, goal}),
    });
    if (res && res.success && Array.isArray(res.sequence) && res.sequence.length) {
      _campaignBuilder.steps = res.sequence.map((s, idx) => ({
        step: idx + 1,
        delay_days: Number(s.delay_days ?? (idx === 0 ? 0 : 3)),
        subject: s.subject || '',
        body: s.body || '',
      }));
      renderBuilderSteps();
      showToast('AI drafted 3 sequence steps.', 'success');
    } else {
      showToast(failMsg(res, 'Could not draft campaign steps.'), 'error');
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = orig || 'AI Draft 3 Steps'; }
  }
}

async function submitCampaignBuilder(alsoLaunch, btn) {
  const name = (document.getElementById('cb-name')?.value || '').trim();
  if (!name) { showToast('Campaign name is required.', 'error'); return; }
  const target_tags = (document.getElementById('cb-tags')?.value || '').trim();
  const framework = (document.getElementById('cb-framework')?.value || '').trim();
  const cleanSteps = _campaignBuilder.steps
    .map((s, idx) => ({
      step: idx + 1,
      delay_days: Math.max(0, Number(s.delay_days || 0)),
      subject: (s.subject || '').trim(),
      body: (s.body || '').trim(),
      subject_b: (s.subject_b || '').trim(),
      body_b: (s.body_b || '').trim(),
    }))
    .filter(s => s.subject && s.body);
  if (!cleanSteps.length) {
    showToast('Add at least one step with both subject and body.', 'error');
    return;
  }
  if (btn) btn.disabled = true;
  try {
    const isEdit = !!_campaignBuilder.id;
    const url = isEdit ? '/api/campaigns/' + encodeURIComponent(_campaignBuilder.id) : '/api/campaigns';
    const res = await api(url, {
      method: isEdit ? 'PATCH' : 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, target_tags, framework, sequence: cleanSteps}),
    });
    if (!res || !res.success) {
      showToast(failMsg(res, 'Could not save campaign.'), 'error');
      return;
    }
    const cid = res.id || _campaignBuilder.id;
    if (alsoLaunch && cid) {
      const lres = await api('/api/campaigns/' + encodeURIComponent(cid) + '/launch', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({target_tags}),
      });
      if (lres && lres.success) {
        showToast('Saved and staged ' + (lres.queued_emails || 0) + ' draft(s) for ' +
          (lres.queued_prospects || 0) + ' contact(s) in Outbox.', 'success');
      } else {
        showToast(failMsg(lres, 'Saved, but launch did not queue drafts.'), 'error');
      }
    } else {
      showToast(isEdit ? 'Campaign updated.' : 'Campaign saved.', 'success');
    }
    closeCampaignBuilderModal();
    await Promise.all([loadCampaigns(), loadOutbox()]);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function launchCampaignById(id, btn) {
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/campaigns/' + encodeURIComponent(id) + '/launch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({}),
    });
    if (res && res.success) {
      showToast('Launched to Outbox: ' + (res.queued_emails || 0) + ' draft(s) staged for ' +
        (res.queued_prospects || 0) + ' contact(s)' +
        (res.skipped ? ' (' + res.skipped + ' skipped).' : '.'), 'success');
      await Promise.all([loadCampaigns(), loadOutbox()]);
    } else {
      showToast(failMsg(res, 'Could not launch campaign.'), 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function toggleCampaignStatus(id, nextStatus, btn) {
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/campaigns/' + encodeURIComponent(id) + '/status', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status: nextStatus}),
    });
    if (res && res.success) {
      showToast('Campaign marked ' + nextStatus + '.', 'success');
      await loadCampaigns();
    } else {
      showToast(failMsg(res, 'Could not update campaign status.'), 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function cloneCampaignById(id, btn) {
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/campaigns/' + encodeURIComponent(id) + '/clone', {method: 'POST'});
    if (res && res.success) {
      showToast('Campaign cloned.', 'success');
      await loadCampaigns();
    } else {
      showToast(failMsg(res, 'Could not clone campaign.'), 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deleteCampaignById(id, btn) {
  if (_armedDeleteCampaignId !== id) {
    _armedDeleteCampaignId = id;
    await loadCampaigns();
    return;
  }
  _armedDeleteCampaignId = null;
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/campaigns/' + encodeURIComponent(id), {method: 'DELETE'});
    if (res && res.success) {
      showToast('Campaign deleted.', 'success');
      await loadCampaigns();
    } else {
      showToast(failMsg(res, 'Could not delete campaign.'), 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function toggleCampaignAnalytics(id) {
  if (_openCampaignAnalytics.has(id)) _openCampaignAnalytics.delete(id);
  else _openCampaignAnalytics.add(id);
  loadCampaigns();
}

function renderCampaignAnalyticsPanel(c) {
  if (!_openCampaignAnalytics.has(c.id)) return '';
  const an = c.analytics || {};
  const steps = Array.isArray(an.steps) ? an.steps : [];
  const va = (an.variants && an.variants.A) || {sent: 0, pending: 0, replies: 0, reply_rate_pct: 0};
  const vb = (an.variants && an.variants.B) || {sent: 0, pending: 0, replies: 0, reply_rate_pct: 0};
  const win = an.winning_variant ? '<span class="mini good">Winner: Variant ' + escHtml(an.winning_variant) + '</span>' : '';
  const hint = !an.sent
    ? '<div class="muted" style="margin-top:8px">No emails sent yet — stage drafts with <b>Launch to Outbox</b> and approve in <b>Outbox</b> to collect live reply &amp; A/B winner stats.</div>'
    : '';
  return '<div class="ca-panel" id="ca-panel-' + escHtml(c.id) + '">' +
    '<div class="campaign-analytics-grid">' +
      '<div class="ca-stat"><div class="v">' + (an.sent || 0) + '</div><div class="k">Emails sent</div></div>' +
      '<div class="ca-stat"><div class="v">' + (an.replied || 0) + ' (' + (an.reply_rate_pct || 0) + '%)</div><div class="k">Replied</div></div>' +
      '<div class="ca-stat"><div class="v">' + (an.interested || 0) + '</div><div class="k">Interested</div></div>' +
      '<div class="ca-stat"><div class="v">' + (an.meetings || 0) + '</div><div class="k">Meetings</div></div>' +
    '</div>' +
    '<div style="margin-top:10px;display:flex;gap:12px;flex-wrap:wrap;align-items:center">' +
      '<span><b>A/B Split:</b> Variant A (' + va.sent + ' sent, ' + va.pending + ' pending, ' + va.replies + ' replies / ' + va.reply_rate_pct + '%)</span>' +
      '<span>Variant B (' + vb.sent + ' sent, ' + vb.pending + ' pending, ' + vb.replies + ' replies / ' + vb.reply_rate_pct + '%)</span>' +
      win +
    '</div>' +
    (steps.length
      ? '<div style="margin-top:8px"><b>Per-step funnel:</b> ' +
          steps.map(s => 'Step ' + s.step + ': ' + s.sent + ' sent, ' + s.pending + ' pending, ' + s.scheduled + ' scheduled').join(' &middot; ') +
        '</div>'
      : '') +
    hint +
  '</div>';
}

let _campaignFilter = 'all';
function setCampaignFilter(f) {
  _campaignFilter = f || 'all';
  renderCampaignsList();
}

async function loadCampaigns() {
  const el = document.getElementById('campaigns-list');
  const [fwData, statsData] = await Promise.all([
    api('/api/frameworks'),
    api('/api/stats'),
    ensureTemplatesLoaded(false),
  ]);
  renderCampaignsFunnel(statsData);
  renderFrameworks(fwData);
  const data = await api('/api/campaigns');
  if (!data) { if (el) el.innerHTML = offlineState(); return; }
  _campaigns = data;
  renderCampaignsList();
}

function renderCampaignsList() {
  const el = document.getElementById('campaigns-list');
  if (!el) return;
  const data = _campaigns || [];
  if (!data.length) {
    el.innerHTML = emptyState(icon('campaigns'), 'No campaigns yet',
      'Create a sequence with <b>+ New Campaign</b> or click <b>Write Emails Now</b> to let VeloReach draft from your scored prospects.');
    return;
  }
  const filters = ['all', 'active', 'paused', 'draft'];
  const shown = _campaignFilter === 'all' ? data : data.filter(c => (c.status || 'draft') === _campaignFilter);
  let html = '<div class="toolbar"><div class="segmented" role="group" aria-label="Filter campaigns by status">' +
    filters.map(f => {
      const n = f === 'all' ? data.length : data.filter(c => (c.status || 'draft') === f).length;
      const lbl = f.charAt(0).toUpperCase() + f.slice(1);
      return '<button class="' + (f === _campaignFilter ? 'on' : '') + '" aria-pressed="' + (f === _campaignFilter) +
        '" onclick="setCampaignFilter(' + jsq(f) + ')">' + lbl + ' <span class="n">' + n + '</span></button>';
    }).join('') +
    '</div></div>';
  if (!shown.length) {
    el.innerHTML = html + emptyState(icon('campaigns'), 'No ' + escHtml(_campaignFilter) + ' campaigns', 'Switch filter or create a new campaign above.');
    return;
  }
  shown.forEach((c) => {
    const i = data.indexOf(c);
    let stepsHtml = '';
    (c.sequence || []).forEach((step, s) => {
      const sendBtns = (c.recipients || []).map((r, ri) => {
        // Only offered when VeloReach can't send itself and this person can
        // still be emailed (the server decides: can_send_by_hand).
        if (r.sent_by_hand) {
          const tick = '<label style="display:inline-flex;align-items:center;gap:4px;font-size:13px;cursor:pointer">' +
            '<input type="checkbox" checked onchange="markSent(' + i + ',' + ri + ',this.checked)"> ' +
            '&#10003; Sent by you</label>';
          if (s === 0) return '<span class="rcpt-who">' + escHtml(r.email) + '</span>' + tick;
          // Follow-ups are the user's too: VeloReach never emails someone they emailed.
          return '<span class="rcpt-who">' + escHtml(r.email) + '</span>' +
            '<button class="btn btn-secondary btn-sm" onclick="openInGmail(' + i + ',' + s + ',' + ri + ')">' +
            'Open in Gmail</button>' +
            '<span class="muted" style="font-size:12px">VeloReach won\'t send follow-ups to people you emailed yourself.</span>';
        }
        if (s !== 0) return '';
        if (!r.can_send_by_hand) {
          // VeloReach sends here; this only records an email the user already sent.
          return r.can_mark_sent
            ? '<label class="muted" style="display:inline-flex;align-items:center;gap:4px;font-size:12px;cursor:pointer">' +
              '<input type="checkbox" onchange="markSent(' + i + ',' + ri + ',this.checked)"> ' +
              'I already emailed ' + escHtml(r.email) + ' myself</label>'
            : '';
        }
        const risky = r.email_status === 'risky'
          ? ' <span class="mini t-waiting">Catch-all, may bounce</span>' : '';
        return '<span class="rcpt-who">' + escHtml(r.email) + risky + '</span>' +
          '<button class="btn btn-secondary btn-sm" onclick="openInGmail(' + i + ',' + s + ',' + ri + ')">' +
          'Open in Gmail</button>' +
          '<label><input type="checkbox" onchange="markSent(' + i + ',' + ri + ',this.checked)"> Mark as sent</label>'
      }).filter(Boolean).map(x => '<div class="rcpt">' + x + '</div>').join('');
      stepsHtml += '<div class="email-step"><span class="step-dot">' + escHtml(String(step.step || '?')) + '</span>' +
        '<div><div class="step-num">' + (s === 0 ? 'First email' : 'Follow-up') +
        (step.delay_days ? ' &middot; ' + escHtml(String(step.delay_days)) + ' days after the last' : '') + '</div>' +
        '<div class="subject">' + escHtml(step.subject) + '</div>' +
        '<div class="body">' + escHtml(step.body) + '</div>' +
        (sendBtns ? '<div class="step-actions">' + sendBtns + '</div>' : '') +
        '</div></div>';
    });
    const pc = (c.prospect_ids || []).length;
    const oc = c.outbox_counts || {};
    const an = c.analytics || {};
    const tagPills = parseTags(c.target_tags).map(t => '<span class="tag-pill">' + escHtml(t) + '</span>').join('');
    const nextStatus = c.status === 'active' ? 'paused' : 'active';
    const statusLabel = c.status === 'active' ? 'Pause' : 'Activate';
    const delLabel = _armedDeleteCampaignId === c.id ? 'Confirm delete?' : 'Delete';
    html += '<div class="campaign-card"><div class="campaign-top">' +
      '<div class="campaign-title"><h3>' + escHtml(c.name || 'Untitled Campaign') + '</h3>' +
        (c.framework ? '<span class="mini" title="Copywriting framework this campaign tests">' + escHtml(c.framework) + '</span>' : '') +
        '<div class="campaign-actions">' +
          '<button class="btn btn-primary btn-sm" onclick="launchCampaignById(' + jsq(c.id) + ', this)">Launch to Outbox</button>' +
          '<button class="btn btn-secondary btn-sm" onclick="openCampaignTestSend(' + jsq(c.id) + ')">Test Email</button>' +
          '<button class="btn btn-secondary btn-sm" onclick="toggleCampaignStatus(' + jsq(c.id) + ',' + jsq(nextStatus) + ', this)">' + statusLabel + '</button>' +
          '<button class="btn btn-ghost btn-sm" onclick="toggleCampaignAnalytics(' + jsq(c.id) + ')">Analytics</button>' +
          '<button class="btn btn-ghost btn-sm" onclick="openCampaignBuilderModal(' + jsq(c.id) + ')">Edit</button>' +
          '<button class="btn btn-ghost btn-sm" onclick="cloneCampaignById(' + jsq(c.id) + ', this)">Clone</button>' +
          '<button class="btn btn-ghost btn-sm" onclick="fbCampaign(' + i + ')">Feedback</button>' +
          '<button class="btn btn-ghost btn-sm" onclick="deleteCampaignById(' + jsq(c.id) + ', this)">' + delLabel + '</button>' +
        '</div></div>' +
      '<div class="meta">' + badge(c.status) + '<span>' + pc + ' prospect' + (pc !== 1 ? 's' : '') + '</span>' +
        '<span>' + (c.sequence || []).length + ' emails</span>' +
        '<span>Outbox: ' + (oc.pending_review || 0) + ' pending · ' + ((oc.approved || 0) + (oc.sending || 0)) + ' scheduled · ' + (oc.sent || 0) + ' sent</span>' +
        (an.replied ? '<span>Reply rate: ' + (an.reply_rate_pct || 0) + '%</span>' : '') +
        '<span>Created ' + relTime(c.created_at) + '</span>' +
        (tagPills ? '<span class="tag-pills">Targets: ' + tagPills + '</span>' : '') + '</div>' +
      renderCampaignAnalyticsPanel(c) +
      campaignProgress(c) + '</div>' +
      '<div class="sequence">' +
      (stepsHtml || '<p class="muted" style="font-size:13px;padding-top:16px">No email steps in this campaign.</p>') + '</div></div>';
  });
  el.innerHTML = html;
}

// The whole exchange in order: VeloReach's cold emails (from the Outbox), the
// replies, VeloReach's answers, and any answer still waiting for approval.
let _convos = [], _convoFilter = 'needs';
const STAGE_LABELS = {
  initial_outreach: 'Outreach', engaged: 'Engaged', qualifying: 'Qualifying', presenting: 'Presenting',
  negotiating: 'Negotiating', closing: 'Closing', closed_won: 'Won', closed_lost: 'Lost',
};
const CONVO_FILTERS = [
  ['needs', 'Needs you', c => !!c.draft || c.status === 'needs_human'],
  ['open', 'Open', c => c.status !== 'closed' && c.status !== 'awaiting_reply'],
  ['awaiting', 'Awaiting reply', c => c.status === 'awaiting_reply'],
  ['closed', 'Closed', c => c.status === 'closed'],
  ['all', 'All', () => true],
];

async function loadConversations() {
  const el = document.getElementById('conversations-list');
  const [data] = await Promise.all([
    api('/api/conversations?awaiting=1'),
    ensureTemplatesLoaded(false),
  ]);
  if (!data) { el.innerHTML = offlineState(); return; }
  _convos = data;
  if (!data.length) {
    el.innerHTML = emptyState(icon('conversations'), 'No conversations yet',
      'No prospects have replied so far. When they do, the Handler agent classifies each reply and responds — every thread shows up here.');
    return;
  }
  // Open on "Needs you" only when something needs you.
  if (_convoFilter === 'needs' && !data.some(CONVO_FILTERS[0][2]))
    _convoFilter = data.some(CONVO_FILTERS[1][2]) ? 'open' : 'all';
  renderConversations();
}

function setConvoFilter(f) { _convoFilter = f; renderConversations(); }

function renderConversations() {
  const el = document.getElementById('conversations-list');
  const filter = CONVO_FILTERS.find(f => f[0] === _convoFilter) || CONVO_FILTERS[CONVO_FILTERS.length - 1];
  const bar = '<div class="toolbar"><div class="segmented" role="group" aria-label="Filter conversations">' +
    CONVO_FILTERS.map(f => '<button class="' + (f[0] === filter[0] ? 'on' : '') + '" aria-pressed="' + (f[0] === filter[0]) +
      '" onclick="setConvoFilter(\'' + f[0] + '\')">' + f[1] + ' <span class="n">' +
      _convos.filter(f[2]).length + '</span></button>').join('') + '</div></div>';
  // Handed-to-you first, then drafts waiting; otherwise newest first (as served).
  const rank = c => c.status === 'needs_human' ? 0 : c.draft ? 1 : 2;
  const shown = _convos.filter(filter[2]).map((c, i) => [c, i])
    .sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map(x => x[0]);
  if (!shown.length) {
    el.innerHTML = bar + '<div class="card">' + emptyState(icon('check'), 'Nothing here',
      'No conversation matches this filter.') + '</div>';
    return;
  }
  el.innerHTML = bar + shown.map(convoCard).join('');
}

function convoCard(c) {
  const items = (c.emails || []).map(e => ({ts: e.sent_at, email: e}))
    .concat((c.thread || []).map(m => ({ts: m.timestamp, msg: m})))
    .map(it => ({...it, key: +parseTs(it.ts)}))
    // An item with no time goes last instead of scrambling the order.
    .sort((a, b) => (isNaN(a.key) ? Infinity : a.key) - (isNaN(b.key) ? Infinity : b.key) || 0);
  const first = c.first_name || 'Them';
  const thread = items.map(it => {
    if (it.email) {
      return '<div class="thread-email"><div class="te-head">' + icon('campaigns') +
        '<b>Email ' + escHtml(String(it.email.step)) + '</b><span>' + escHtml(it.email.subject) + '</span>' +
        '<span class="te-when">' + relTime(it.email.sent_at) + '</span></div>' +
        '<div class="te-body">' + escHtml(it.email.body) + '</div></div>';
    }
    const m = it.msg, sent = m.sender !== 'prospect';  // 'harvey' (the old name) is ours too
    return '<div class="thread-msg ' + (sent ? 'sent' : 'received') + '"><div class="sender">' +
      (sent ? 'VeloReach' : escHtml(first)) + ' &middot; ' + relTime(m.timestamp) + '</div>' +
      escHtml(m.content) + '</div>';
  }).join('');
  const draft = c.draft
    ? '<div class="thread-draft"><div class="sender">' + icon('pen') + 'VeloReach drafted a reply · waiting for you</div>' +
      '<div class="td-body">' + escHtml(c.draft.body) + '</div>' +
      '<button class="btn btn-primary btn-sm" onclick="goTab(\'outbox\')">Review in Outbox' + icon('arrow') + '</button></div>'
    : '';
  const name = [c.first_name, c.last_name].filter(Boolean).join(' ') || c.company || 'Unknown';
  const stage = own(STAGE_LABELS, c.stage) ? '<span class="tag">' + STAGE_LABELS[c.stage] + '</span>' : '';
  return '<div class="convo-card"><div class="convo-head">' +
    '<span class="avatar">' + initials(c.first_name, c.last_name) + '</span>' +
    '<div><h3>' + escHtml(name) + '</h3><div class="sub">' +
      escHtml([c.company, c.prospect_email].filter(Boolean).join(' · ')) + ' · ' + relTime(c.updated_at) + '</div></div>' +
    // An escalation's intent badge says the same as its status: show one.
    '<div class="tags">' + (c.intent && !(c.status === 'needs_human' && c.intent === 'escalate') ? badge(c.intent) : '') +
      stage + badge(c.status) + '</div></div>' +
    '<div class="thread">' + (thread || '<p class="muted" style="font-size:13px">No messages in this thread yet.</p>') +
    draft + '</div>' + convoActions(c) + '</div>';
}

let _convoSlots = {};

// VeloReach answers new replies only on open conversations: an escalated one
// waits for you to reply yourself, then close it or hand it back.
function convoActions(c) {
  const canReply = c.prospect_status !== 'opted_out';
  const canBook = (c.status === 'open' || c.status === 'awaiting_reply' || c.status === 'needs_human') && canReply;
  const replyBtn = canReply
    ? '<button class="btn btn-primary btn-sm" onclick="toggleConvoReply(' + jsq(c.id) + ')">Reply</button>'
    : '';
  const bookBtn = canBook
    ? '<button class="btn btn-secondary btn-sm" onclick="toggleConvoBooking(' + jsq(c.id) + ')">Book Meeting</button>'
    : '';
  const replyPanel = canReply
    ? '<div id="convo-reply-' + escHtml(c.id) + '" class="convo-reply-panel" style="display:none">' +
        '<textarea id="convo-reply-text-' + escHtml(c.id) + '" class="form-input" rows="3" ' +
          'placeholder="Write your reply to stage in Outbox, or type guidance and click AI Draft Reply..."></textarea>' +
        '<div class="convo-reply-actions">' +
          '<button class="btn btn-primary btn-sm" onclick="submitConvoReply(' + jsq(c.id) + ',\'queue\',this)">Stage Reply to Outbox</button>' +
          '<button class="btn btn-secondary btn-sm" onclick="submitConvoReply(' + jsq(c.id) + ',\'ai_draft\',this)">AI Draft Reply</button>' +
          (_templates.length
            ? '<select class="form-input tpl-picker" aria-label="Insert reply template" onchange="insertTemplateIntoConvo(' + jsq(c.id) + ',this)">' +
                templatePickerOptions('Insert template…') + '</select>'
            : '') +
          '<button class="btn btn-ghost btn-sm" onclick="toggleConvoReply(' + jsq(c.id) + ')">Cancel</button>' +
        '</div>' +
      '</div>'
    : '';
  const bookPanel = canBook
    ? '<div id="convo-book-' + escHtml(c.id) + '" class="convo-book-panel" style="display:none"></div>'
    : '';
  if (c.status === 'needs_human' && c.prospect_status === 'opted_out') {
    // A complaint or legal threat from someone who opted out: handing it
    // back wouldn't help, VeloReach never answers them.
    return '<div class="convo-actions"><span class="muted">' + icon('alert') +
      'Opted out, and VeloReach handed this to you. Reply from your inbox if you need to, then:</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="setConvoStatus(' + jsq(c.id) + ',\'closed\')">Close</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="showOptOut(' + jsq(c.prospect_id) + ',' + jsq(c.prospect_email) +
      ')">Why opted out?</button></div>';
  }
  if (c.status === 'needs_human') {
    return '<div class="convo-actions"><span class="muted">' + icon('alert') +
      'VeloReach handed this to you. Stage a reply below or handle directly:</span>' +
      replyBtn + bookBtn +
      '<button class="btn btn-secondary btn-sm" onclick="setConvoStatus(' + jsq(c.id) + ',\'open\')">Hand back to VeloReach</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="setConvoStatus(' + jsq(c.id) + ',\'closed\')">Close</button></div>' +
      replyPanel + bookPanel;
  }
  if (c.status === 'open') {
    return '<div class="convo-actions">' + replyBtn + bookBtn + '<button class="btn btn-ghost btn-sm" onclick="setConvoStatus(' +
      jsq(c.id) + ',\'closed\')">Close conversation</button></div>' + replyPanel + bookPanel;
  }
  if (c.status === 'awaiting_reply' && canBook) {
    return '<div class="convo-actions">' + replyBtn + bookBtn + '</div>' + replyPanel + bookPanel;
  }
  if (c.status === 'closed' && c.prospect_status === 'opted_out') {
    // Reopening wouldn't help: VeloReach never answers someone who opted out.
    return '<div class="convo-actions"><span class="muted">' + icon('alert') + 'Opted out. VeloReach won’t write to them.</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="showOptOut(' + jsq(c.prospect_id) + ',' + jsq(c.prospect_email) +
      ')">Why opted out?</button></div>';
  }
  if (c.status === 'closed') {
    return '<div class="convo-actions">' + replyBtn + '<button class="btn btn-ghost btn-sm" onclick="setConvoStatus(' +
      jsq(c.id) + ',\'open\')">Reopen</button></div>' + replyPanel;
  }
  return replyBtn ? '<div class="convo-actions">' + replyBtn + '</div>' + replyPanel : '';
}

function toggleConvoReply(convoId) {
  const panel = document.getElementById('convo-reply-' + convoId);
  if (!panel) return;
  const open = panel.style.display !== 'none';
  panel.style.display = open ? 'none' : 'block';
  if (!open) {
    const ta = document.getElementById('convo-reply-text-' + convoId);
    if (ta) ta.focus();
  }
}

async function submitConvoReply(convoId, mode, btn) {
  const ta = document.getElementById('convo-reply-text-' + convoId);
  const text = (ta?.value || '').trim();
  if (mode === 'queue' && !text) {
    showToast('Write a reply message first (or click AI Draft Reply).', 'error');
    return;
  }
  if (btn) btn.disabled = true;
  try {
    const payload = mode === 'ai_draft'
      ? {mode: 'ai_draft', instruction: text}
      : {mode: 'queue', body: text};
    const res = await api('/api/conversations/' + encodeURIComponent(convoId) + '/reply', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if (res && res.success) {
      if (ta) ta.value = '';
      showToast(mode === 'ai_draft'
        ? 'AI reply drafted and staged in Outbox for review.'
        : 'Reply staged in Outbox for review.', 'success');
      await Promise.all([loadConversations(), loadOutbox()]);
    } else {
      showToast(failMsg(res, 'Could not stage reply.'), 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function toggleConvoBooking(convoId) {
  const panel = document.getElementById('convo-book-' + convoId);
  if (!panel) return;
  if (panel.dataset.open === '1') {
    panel.style.display = 'none';
    panel.dataset.open = '0';
    return;
  }
  panel.style.display = 'block';
  panel.dataset.open = '1';
  panel.innerHTML = '<div class="hint">Loading available calendar slots…</div>';
  const data = await api('/api/calendar/slots?days=5');
  if (!data) {
    panel.innerHTML = '<div class="hint">Could not load calendar slots.</div>';
    return;
  }
  if (!data.configured) {
    panel.innerHTML = '<div class="hint">Calendar not configured. <a href="#" onclick="goTab(\'settings\');return false">Configure Cal.com or Google Calendar in Settings</a></div>';
    return;
  }
  const slots = data.slots || [];
  _convoSlots[convoId] = slots;
  if (!slots.length) {
    panel.innerHTML = '<div class="hint">No open slots found in the next 5 days.</div>';
    return;
  }
  panel.innerHTML = '<div class="convo-book-head"><span>Select a slot (' + escHtml(data.provider || 'calendar') + '):</span></div>' +
    '<div class="convo-slot-grid">' +
    slots.slice(0, 12).map((s, idx) =>
      '<button class="btn btn-secondary btn-sm convo-slot-btn" onclick="confirmBookSlot(' + jsq(convoId) + ',' + idx + ',this)">' +
      '<span>' + formatDate(s.start) + '</span> <span class="mini t-good">Confirm Book</span></button>'
    ).join('') + '</div>';
}

async function confirmBookSlot(convoId, idx, btn) {
  const convo = _convos.find(x => x.id === convoId);
  const slot = (_convoSlots[convoId] || [])[idx];
  if (!convo || !slot) return;
  if (btn) { btn.disabled = true; btn.textContent = 'Booking…'; }
  try {
    const res = await api('/api/calendar/book', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        conversation_id: convo.id,
        prospect_id: convo.prospect_id || '',
        prospect_email: convo.prospect_email || '',
        prospect_name: [convo.first_name, convo.last_name].filter(Boolean).join(' '),
        slot: slot,
        notes: 'Booked from VeloReach dashboard',
      }),
    });
    if (res && res.success) {
      showToast('Meeting booked!', 'success');
      loadConversations();
      loadToday();
    } else {
      showToast(failMsg(res, 'Could not book meeting.'), 'error');
      if (btn) { btn.disabled = false; btn.textContent = 'Confirm Book'; }
    }
  } catch (err) {
    showToast('Could not book meeting.', 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Confirm Book'; }
  }
}

async function setConvoStatus(id, status) {
  const data = await api('/api/conversations/' + encodeURIComponent(id) + '/status', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({status: status}),
  });
  if (data && data.success) showToast(status === 'open' ? 'Open again. VeloReach will answer the next reply.' : 'Closed. A new reply starts a new conversation.', 'success');
  else showToast(failMsg(data, 'Could not update that conversation.'), 'error');
  loadConversations();
  loadToday();
}

// ── Inboxes: the main mailbox and the extra ones you connected ──

const INBOX_STATE = {
  sending: ['Sending', 't-good'],
  paused: ['Paused', 't-waiting'],
  not_connected: ['Not connected', 't-bad'],
};
const INBOX_KINDS = {
  gmail: ['Gmail', 'Google Workspace or Gmail. Sign in with Google in your browser.'],
  microsoft: ['Microsoft 365', 'Outlook or Microsoft 365. Sign in with Microsoft in your browser.'],
  smtp: ['Other (SMTP)', 'Any other email host, with its SMTP server and password.'],
};
let _inboxes = null;
let _inboxPoll = null;

function inboxBadge(ib) {
  const [label, tone] = INBOX_STATE[ib.state] || [ib.state, 't-idle'];
  return '<span class="badge ' + tone + '">' + escHtml(label) + '</span>';
}

function inboxMeter(ib) {
  const cap = Math.max(1, Number(ib.cold_cap) || 0);
  const sent = Number(ib.cold_sent_today) || 0;
  const pct = Math.min(100, Math.round(sent / cap * 100));
  return '<div class="inbox-meter"><div class="line"><span>Cold emails today</span><span>' +
    sent + ' / ' + Number(ib.cold_cap) + '</span></div>' +
    '<div class="progress-bar"><div class="progress-fill ' + (pct >= 100 ? 'yellow' : 'green') +
    '" style="width:' + pct + '%"></div></div></div>';
}

function inboxDate(iso) {
  if (!iso) return '';
  const d = new Date(iso.length === 10 ? iso + 'T00:00:00' : iso);
  return isNaN(d) ? iso : d.toLocaleDateString(undefined, {month: 'short', day: 'numeric', year: 'numeric'});
}

async function loadInboxes() {
  const el = document.getElementById('inboxes-list');
  const actions = document.getElementById('inboxes-actions');
  const data = await api('/api/inboxes');
  if (!data) { el.innerHTML = offlineState(); return; }
  _inboxes = data;
  if (!data.native) {
    actions.innerHTML = '';
    el.innerHTML = emptyState(icon('inboxes'), 'Instantly sends for you',
      'Sending through several of your own inboxes works with Gmail, Microsoft 365 or SMTP. ' +
      'Set channels.email.provider in sellbird.yaml to one of those to use it.');
    return;
  }
  actions.innerHTML = '<button class="btn btn-primary" onclick="openAddInbox()">' + icon('plus') +
    'Add inbox</button>';

  let html = '';
  if (data.provider !== 'smtp') {
    html += '<div class="card" style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;border-color:var(--accent-line);">' +
      '<div><b>Quick Demo / Test Mode: Resend SMTP</b>' +
      '<div class="muted" style="font-size:12.5px;margin-top:2px;">Want to test email delivery right now without Google/M365 OAuth? Configure Resend in 1 click on Today or Settings.</div></div>' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap;">' +
        '<button type="button" class="btn btn-primary btn-sm" onclick="goTab(\'today\')">Configure Resend on Today</button>' +
        '<button type="button" class="btn btn-secondary btn-sm" onclick="goTab(\'settings\');setTimeout(() => jumpSettings(\'settings-email-providers\'), 120)">Open Settings</button>' +
      '</div></div>';
  }
  if (data.sending_paused) {
    html += '<div class="notice wait"><b>All sending is paused:</b> ' + escHtml(data.sending_paused) +
      '. Resume it on the Outbox tab.</div>';
  }
  const list = data.inboxes || [];
  const sending = list.filter(i => i.state === 'sending');
  const capToday = sending.reduce((n, i) => n + (Number(i.cold_cap) || 0), 0);
  const sentToday = list.reduce((n, i) => n + (Number(i.cold_sent_today) || 0), 0);
  const waiting = list.reduce((n, i) => n + (Number(i.first_emails_waiting) || 0), 0);
  html += '<div class="inbox-summary">' +
    '<span><b>' + list.length + '</b> ' + (list.length === 1 ? 'inbox' : 'inboxes') + ', <b>' + sending.length + '</b> sending</span>' +
    '<span><b>' + sentToday + '</b>' + (capToday ? ' of <b>' + Math.min(capToday, Number(data.max_daily_sends)) + '</b>' : '') + ' cold emails sent today</span>' +
    '<span><b>' + waiting + '</b> first emails waiting to go</span></div>';

  html += '<div class="inbox-list">';
  list.forEach((ib, i) => {
    const letter = (ib.address || '?').charAt(0).toUpperCase();
    const sub = [ib.name, ib.main ? 'Main mailbox' : (INBOX_KINDS[ib.provider === 'google' ? 'gmail' : ib.provider] || [ib.provider])[0]]
      .filter(Boolean).join(' · ');
    html += '<button class="inbox-row" onclick="openInbox(' + i + ')" aria-label="Open ' + escHtml(ib.address || 'main mailbox') + '">' +
      '<span class="inbox-avatar" aria-hidden="true">' + escHtml(letter) + '</span>' +
      '<span class="inbox-who"><div class="addr">' + escHtml(ib.address || '(main mailbox)') + '</div>' +
      '<div class="sub">' + escHtml(sub) + '</div></span>' +
      inboxBadge(ib) + inboxMeter(ib) +
      '<span class="inbox-waiting"><b>' + Number(ib.first_emails_waiting) + '</b>waiting</span>' +
      icon('chevron', 'chev') + '</button>';
  });
  html += '</div>';

  if (!data.several) {
    html += '<div class="card"><div class="card-title">Why add another inbox?</div><div class="inbox-help">' +
      '<div class="inset"><h4>Send more each day</h4><p>Each inbox sends up to ' + Number(data.per_inbox_daily_cap) +
      ' cold emails a day. Two inboxes, twice as many, all under your daily total of ' + Number(data.max_daily_sends) + '.</p></div>' +
      '<div class="inset"><h4>Protect your main address</h4><p>Send cold email from a second domain, so a bad week never touches your everyday mail.</p></div>' +
      '<div class="inset"><h4>Pick per campaign</h4><p>Choose which inboxes each campaign sends from. Replies always come back to the inbox that started the thread.</p></div>' +
      '</div><div class="btn-group"><button class="btn btn-secondary" onclick="openAddInbox()">' + icon('plus') + 'Add inbox</button></div></div>';
  }
  el.innerHTML = html;
}

// The dialog: one inbox's detail, or adding one.

function inboxDialog(html) {
  const d = document.getElementById('inbox-dialog');
  d.innerHTML = html;
  if (!d.open) d.showModal();
  return d;
}

function closeInboxDialog() {
  if (_inboxPoll) { clearInterval(_inboxPoll); _inboxPoll = null; }
  const d = document.getElementById('inbox-dialog');
  if (d.open) d.close();
}

function inboxDialogHead(title, sub, extra) {
  return '<div class="modal-head"><div><h3 id="inbox-dialog-title">' + title + '</h3>' +
    (sub ? '<div class="sub">' + sub + '</div>' : '') + '</div>' + (extra || '') +
    '<button class="btn btn-ghost btn-sm btn-icon" onclick="closeInboxDialog()" aria-label="Close">' + icon('close') + '</button></div>';
}

function openInbox(i) {
  const ib = (_inboxes && _inboxes.inboxes || [])[i];
  if (!ib) return;
  const campaigns = (_inboxes.campaigns || []);
  const pools = _inboxes.pools || {};
  const using = campaigns.filter(c => (pools[c.id] || []).includes(ib.id));
  const everyInbox = campaigns.filter(c => !pools[c.id]);
  let body = '';
  if (ib.state === 'not_connected') {
    body += '<div class="notice bad"><b>Not connected.</b> ' +
      escHtml(ib.connect_hint || 'VeloReach can\'t sign in to it from this computer. Check the connection to see why, or remove it and add it again.') + '</div>';
  }
  if (ib.paused_reason) {
    body += '<div class="notice wait"><b>Paused:</b> ' + escHtml(ib.paused_reason) + '</div>';
  }
  body += '<div class="modal-stats">' +
    '<div class="inset"><div class="k">Cold today</div><div class="v">' + Number(ib.cold_sent_today) + ' / ' + Number(ib.cold_cap) + '</div></div>' +
    '<div class="inset"><div class="k">First emails waiting</div><div class="v">' + Number(ib.first_emails_waiting) + '</div></div>' +
    '<div class="inset"><div class="k">Warming up since</div><div class="v">' + (ib.warmup_started_at ? escHtml(inboxDate(ib.warmup_started_at)) : '<span class="muted">Established</span>') + '</div></div>' +
    '</div>';
  body += '<div class="modal-section"><h4>Campaigns sending from it</h4>';
  if (!campaigns.length) body += '<p>No campaigns yet.</p>';
  else {
    const names = using.concat(everyInbox).map(c => escHtml(c.name || 'Untitled'));
    const shown = names.slice(0, 6).join(', ') + (names.length > 6 ? ', and ' + (names.length - 6) + ' more' : '');
    body += names.length ? '<p>' + shown + '</p>' : '<p>None. Every campaign has chosen other inboxes.</p>';
    if (everyInbox.length) body += '<p class="form-hint">Campaigns that haven\'t chosen inboxes send from all of them.</p>';
  }
  body += '</div>';

  if (ib.main) {
    const kind = ib.provider === 'gmail' || ib.provider === 'microsoft' ? ib.provider : '';
    if (ib.state === 'not_connected' && kind) {
      const can = (_inboxes.can_connect || {})[kind];
      body = '<div id="main-status"></div>' + (can
        ? '<div class="notice bad"><b>Not connected.</b> Sign in to the mailbox VeloReach sends from.</div>' +
          '<button class="btn btn-primary" id="main-go" onclick="connectMainMailbox(\'' + kind + '\')">Sign in with ' +
          (kind === 'gmail' ? 'Google' : 'Microsoft') + '</button><div style="height:16px"></div>'
        : connectSetupNotice(kind)) + body.replace(/<div class="notice bad"><b>Not connected.<\/b>[\s\S]*?<\/div>/, '');
    }
    body += '<div class="modal-section"><h4>Settings</h4><p>Your main mailbox comes from sellbird.yaml: ' +
      'its address is persona.email, its daily cap is channels.email.per_inbox_daily_cap, and pausing it pauses ' +
      'all sending, on the Outbox tab.</p></div>';
    inboxDialog(inboxDialogHead(escHtml(ib.address || 'Main mailbox'), inboxBadge(ib) + ' Main mailbox') +
      '<div class="modal-body">' + body + '</div>');
    return;
  }
  const id = jsq(ib.id);
  body += '<div class="modal-section"><h4>Daily cold-email cap</h4>' +
    '<div class="inline-edit"><input class="form-input" id="ib-cap" type="number" min="1" max="500" inputmode="numeric" placeholder="Default (' +
    Number(_inboxes.per_inbox_daily_cap) + ')" value="' + (ib.cap_is_yours ? Number(ib.cold_cap) : '') + '">' +
    '<button class="btn btn-secondary" onclick="saveInboxCap(' + id + ')">Save</button></div>' +
    '<div class="form-hint">Leave empty for the default. Warming inboxes start lower on their own.</div></div>';
  body += '<div class="modal-section"><h4>Sender name</h4>' +
    '<div class="inline-edit"><input class="form-input" id="ib-name" maxlength="100" placeholder="Your persona name" value="' + escHtml(ib.name || '') + '">' +
    '<button class="btn btn-secondary" onclick="saveInboxName(' + id + ')">Save</button></div>' +
    '<div class="form-hint">The name people see in From.</div></div>';
  body += '<div class="modal-section"><h4>Remove this inbox</h4><p>VeloReach stops sending from it, and threads it started end. Its sign-in stays in your keychain.</p>' +
    '<div class="inline-edit" style="margin-top:8px"><input class="form-input" id="ib-confirm" placeholder="Type ' + escHtml(ib.address) + ' to confirm">' +
    '<button class="btn btn-danger" onclick="removeInbox(' + id + ')">Remove</button></div></div>';

  const pauseBtn = ib.paused_reason
    ? '<button class="btn btn-primary" onclick="inboxAction(' + id + ',\'resume\')">Resume sending</button>'
    : '<button class="btn btn-secondary" onclick="inboxAction(' + id + ',\'pause\')"' + (ib.state === 'not_connected' ? ' disabled' : '') + '>Pause</button>';
  inboxDialog(inboxDialogHead(escHtml(ib.address), inboxBadge(ib) + ' ' + escHtml(ib.name || '')) +
    '<div class="modal-body">' + body + '</div>' +
    '<div class="modal-foot"><button class="btn btn-ghost" onclick="testInbox(' + id + ')">Check connection</button>' + pauseBtn + '</div>');
}

async function inboxPost(path, body, done, keepOpen) {
  const r = await api(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
                             body: JSON.stringify(body || {})});
  if (r && r.success) {
    if (done) showToast(done, 'success');
    if (!keepOpen) closeInboxDialog();
    loadInboxes();
  } else showToast((r && r.message) || 'That didn\'t work.', 'error');
  return r;
}

function inboxAction(id, action) {
  inboxPost('/api/inboxes/' + encodeURIComponent(id) + '/' + action, {},
            action === 'pause' ? 'Inbox paused' : 'Inbox sends again');
}

function saveInboxCap(id) {
  const v = document.getElementById('ib-cap').value.trim();
  inboxPost('/api/inboxes/' + encodeURIComponent(id) + '/cap', {cap: v === '' ? null : Number(v)}, 'Cap saved');
}

function saveInboxName(id) {
  inboxPost('/api/inboxes/' + encodeURIComponent(id) + '/name',
            {name: document.getElementById('ib-name').value.trim()}, 'Name saved');
}

function removeInbox(id) {
  inboxPost('/api/inboxes/' + encodeURIComponent(id) + '/remove',
            {confirm: document.getElementById('ib-confirm').value.trim().toLowerCase()}, 'Inbox removed');
}

async function testInbox(id) {
  showToast('Checking the connection...', 'info');
  const r = await api('/api/inboxes/' + encodeURIComponent(id) + '/test', {method: 'POST'});
  if (r && r.success) showToast(r.detail || (r.ok ? 'Connected' : 'Not connected'), r.ok ? 'success' : 'error');
  else showToast((r && r.message) || 'Couldn\'t check it.', 'error');
}

// Adding an inbox

let _addKind = 'gmail';

function openAddInbox(kind) {
  if (kind) _addKind = kind;
  const can = (_inboxes && _inboxes.can_connect) || {};
  const picks = Object.keys(INBOX_KINDS).map(k =>
    '<button class="provider-pick' + (k === _addKind ? ' on' : '') + '" onclick="openAddInbox(\'' + k + '\')" aria-pressed="' + (k === _addKind) + '">' +
    '<b>' + INBOX_KINDS[k][0] + '</b><span>' + INBOX_KINDS[k][1] + '</span></button>').join('');
  let form = '<div class="form-group"><label class="form-label" for="add-address">Email address</label>' +
    '<input class="form-input" id="add-address" type="email" autocomplete="off" placeholder="you@yourdomain.com" oninput="updateAddCommand()"></div>' +
    '<div class="form-row"><div class="form-group"><label class="form-label" for="add-name">Sender name <span class="optional-tag">optional</span></label>' +
    '<input class="form-input" id="add-name" maxlength="100" placeholder="Your persona name"></div>' +
    '<div class="form-group"><label class="form-label" for="add-warmup">Warming up since <span class="optional-tag">optional</span></label>' +
    '<input class="form-input" id="add-warmup" type="date"></div></div>' +
    '<div class="form-hint" style="margin:-8px 0 16px">Leave the date empty if this inbox has been sending normally for a while.</div>';
  let note = '';
  if (_addKind === 'smtp') {
    form += '<div class="form-row"><div class="form-group"><label class="form-label" for="add-host">SMTP server</label>' +
      '<input class="form-input" id="add-host" placeholder="smtp.yourhost.com"></div>' +
      '<div class="form-group" style="max-width:110px"><label class="form-label" for="add-port">Port</label>' +
      '<input class="form-input" id="add-port" type="number" value="587"></div></div>' +
      '<div class="form-group"><label class="form-label" for="add-password">Password or app password</label>' +
      '<input class="form-input" id="add-password" type="password" autocomplete="new-password"></div>' +
      '<details class="more-fields"><summary>Username and IMAP (for reading replies)</summary>' +
      '<div class="form-group"><label class="form-label" for="add-user">Username</label><input class="form-input" id="add-user" placeholder="Same as the address"></div>' +
      '<div class="form-row"><div class="form-group"><label class="form-label" for="add-imap">IMAP server</label><input class="form-input" id="add-imap" placeholder="Same as the SMTP server"></div>' +
      '<div class="form-group" style="max-width:110px"><label class="form-label" for="add-imap-port">Port</label><input class="form-input" id="add-imap-port" type="number" value="993"></div></div></details>';
    note = '<p class="form-hint">The password goes straight to your system keychain. VeloReach never shows it again.</p>';
  } else if (!can[_addKind]) {
    note = connectSetupNotice(_addKind);
  } else {
    note = '<p class="form-hint">A ' + INBOX_KINDS[_addKind][0] + ' sign-in opens in the browser on the computer running VeloReach. Sign in as the address above.</p>';
  }
  note += '<div class="modal-section"><h4>Or from your terminal</h4><div class="cmd-copy"><code id="add-cmd"></code>' +
    '<button class="btn btn-secondary btn-sm" onclick="copyAddCommand()">Copy</button></div></div>';
  const disabled = _addKind !== 'smtp' && !can[_addKind];
  inboxDialog(inboxDialogHead('Add an inbox', 'Connect an inbox you already own. VeloReach sends from it and reads its replies.') +
    '<div class="modal-body"><div class="provider-picks">' + picks + '</div><div style="height:16px"></div>' +
    '<div id="add-status"></div>' + form + note + '</div>' +
    '<div class="modal-foot"><button class="btn btn-ghost" onclick="closeInboxDialog()">Cancel</button>' +
    '<button class="btn btn-primary" id="add-go" onclick="submitAddInbox()"' + (disabled ? ' disabled' : '') + '>' +
    (_addKind === 'smtp' ? 'Connect' : 'Sign in with ' + (_addKind === 'gmail' ? 'Google' : 'Microsoft')) + '</button></div>');
  updateAddCommand();
}

function updateAddCommand() {
  const el = document.getElementById('add-cmd');
  if (!el) return;
  const addr = (document.getElementById('add-address').value || '').trim() || 'you@yourdomain.com';
  el.textContent = 'sellbird inboxes connect ' + _addKind + ' --address ' + addr +
    (_addKind === 'smtp' ? ' --smtp-host smtp.yourhost.com' : '');
}

function copyAddCommand() {
  const text = document.getElementById('add-cmd').textContent;
  navigator.clipboard.writeText(text).then(() => showToast('Copied', 'success'),
    () => showToast('Couldn\'t copy. Select the command instead.', 'error'));
}

function addStatus(cls, html) {
  const el = document.getElementById('add-status');
  if (el) el.innerHTML = html ? '<div class="notice ' + cls + '">' + html + '</div>' : '';
}

// Where the browser sign-in's own app (Google OAuth client / Microsoft app
// registration) is set up, when it isn't yet or doesn't look real.
const CONNECT_SETUP = {
  gmail: {
    field: 'gmail-id',
    steps: [
      'In <a href="https://console.cloud.google.com/apis/library/gmail.googleapis.com" target="_blank" rel="noopener noreferrer">Google Cloud Console</a>, pick or create a project and enable the Gmail API.',
      'Under <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener noreferrer">Credentials</a>, create an OAuth client ID of type <b>Desktop app</b>.',
      'Paste its Client ID and Client secret in Settings, under Gmail, and save.',
      'Come back here and sign in.',
    ],
  },
  microsoft: {
    field: 'm365-client',
    steps: [
      'In <a href="https://entra.microsoft.com" target="_blank" rel="noopener noreferrer">Entra</a>, go to App registrations and register an app for your organization only.',
      'Add the redirect URI <code>http://localhost:8766</code> as "Public client/native".',
      'Paste the Application (client) ID and tenant ID in Settings, under Microsoft 365, and save.',
      'Come back here and sign in.',
    ],
  },
};

function connectSetupNotice(kind) {
  const setup = CONNECT_SETUP[kind];
  const problem = ((_inboxes && _inboxes.connect_problems) || {})[kind] || '';
  return '<div class="notice info"><b>' + escHtml(problem) + '</b>' +
    '<ol class="setup-steps">' + setup.steps.map(t => '<li>' + t + '</li>').join('') + '</ol>' +
    '<button class="btn btn-secondary btn-sm" onclick="openSettingsAt(\'' + setup.field + '\')">Open Settings</button></div>';
}

function openSettingsAt(fieldId) {
  closeInboxDialog();
  const btn = Array.from(document.querySelectorAll('nav button'))
    .find(b => (b.getAttribute('onclick') || '').includes("'settings'"));
  showTab('settings', btn);
  setTimeout(() => {
    const el = document.getElementById(fieldId);
    if (!el) return;
    el.scrollIntoView({behavior: 'smooth', block: 'center'});
    el.focus({preventScroll: true});
  }, 300);
}

async function connectMainMailbox(kind) {
  const go = document.getElementById('main-go');
  if (go) go.disabled = true;
  const status = document.getElementById('main-status');
  const say = (cls, html) => { if (status) status.innerHTML = '<div class="notice ' + cls + '">' + html + '</div>'; };
  say('info', 'Opening the sign-in...');
  const r = await api('/api/inboxes/main/connect', {method: 'POST'});
  if (!r || !r.success) {
    if (go) go.disabled = false;
    say('bad', escHtml((r && r.message) || 'That didn\'t work.'));
    return;
  }
  say('wait', 'A ' + (kind === 'gmail' ? 'Google' : 'Microsoft') + ' sign-in opened in your browser. ' +
    'Sign in as your sending address and allow access. This page updates when you\'re done.');
  if (_inboxPoll) clearInterval(_inboxPoll);
  _inboxPoll = setInterval(async () => {
    const s = await api('/api/inboxes/connect/status');
    if (!s || s.state === 'waiting') return;
    clearInterval(_inboxPoll); _inboxPoll = null;
    if (s.state === 'connected') {
      showToast('Main mailbox connected', 'success');
      closeInboxDialog(); loadInboxes();
    } else {
      if (go) go.disabled = false;
      say('bad', escHtml(s.message || 'The sign-in didn\'t finish.'));
    }
  }, 1500);
}

async function submitAddInbox() {
  const v = id => { const el = document.getElementById(id); return el ? el.value.trim() : ''; };
  const body = {address: v('add-address'), name: v('add-name'), warmup_started: v('add-warmup')};
  const go = document.getElementById('add-go');
  if (_addKind === 'smtp') {
    Object.assign(body, {smtp_host: v('add-host'), smtp_port: Number(v('add-port') || 587),
      password: document.getElementById('add-password').value, username: v('add-user'),
      imap_host: v('add-imap'), imap_port: Number(v('add-imap-port') || 993)});
  }
  go.disabled = true;
  addStatus('info', _addKind === 'smtp' ? 'Connecting and checking the sign-in...' : 'Opening the sign-in...');
  const r = await api('/api/inboxes/connect/' + _addKind, {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  if (!r || !r.success) {
    go.disabled = false;
    addStatus('bad', escHtml((r && r.message) || 'That didn\'t work.'));
    return;
  }
  if (_addKind === 'smtp') {
    showToast('Connected ' + r.address, 'success');
    if (!r.test_ok) showToast('It\'s connected, but the check failed: ' + (r.test_detail || ''), 'error');
    closeInboxDialog(); loadInboxes();
    return;
  }
  addStatus('wait', 'A ' + (_addKind === 'gmail' ? 'Google' : 'Microsoft') + ' sign-in opened in your browser. ' +
    'Sign in as <b>' + escHtml(body.address.toLowerCase()) + '</b> and allow access. This page updates when you\'re done.');
  _inboxPoll = setInterval(async () => {
    const s = await api('/api/inboxes/connect/status');
    if (!s || s.state === 'waiting') return;
    clearInterval(_inboxPoll); _inboxPoll = null;
    if (s.state === 'connected') {
      showToast('Connected ' + s.address, 'success');
      if (s.message) showToast('It\'s connected, but the check failed: ' + s.message, 'error');
      closeInboxDialog(); loadInboxes();
    } else {
      go.disabled = false;
      addStatus('bad', escHtml(s.message || 'The sign-in didn\'t finish.'));
    }
  }, 1500);
}

async function loadActivity() {
  const el = document.getElementById('activity-list');
  const data = await api('/api/activity');
  if (!data) { el.innerHTML = offlineState(); return; }
  if (!data.length) {
    el.innerHTML = emptyState(icon('activity'), 'No activity yet',
      'VeloReach hasn\'t taken any actions. Every prospect found, email written, and reply handled will appear here the moment it happens.');
    return;
  }
  let html = '<div class="card"><div class="timeline">';
  let day = '';
  for (const a of data) {
    const label = dayLabel(a.created_at);
    if (label !== day) { day = label; html += '<div class="tl-day">' + escHtml(label) + '</div>'; }
    html += '<div class="tl-item' + (a.agent === 'main' ? ' quiet' : '') + '"><span class="tl-time">' + timeOfDay(a.created_at) + '</span>' +
      '<span class="tl-icon">' + icon(own(AGENT_ICONS, a.agent) || 'activity') + '</span>' +
      '<span class="tl-text">' + actionText(a) + '</span>' +
      '<span class="tl-agent">' + escHtml(a.agent) + '</span></div>';
  }
  el.innerHTML = html + '</div></div>';
}

// ── Init & live refresh ──

showTabLoading('today');
loadRenameNotice();
loadToday();
loadTrend();
loadFunnel();
loadSetupStatus();
loadRuns();
loadTodayActivity();
loadSellbirdStatus();

loadLiveStatus();

// Agent status: quick poll (not while locked: it would only be refused)
setInterval(() => { if (!_locked) loadSellbirdStatus(); }, 8000);

// Live status panel: refreshed while the Today tab is open and visible.
setInterval(() => {
  if (!document.hidden && !_locked && currentTab === 'today') loadLiveStatus();
}, 10000);

// Nav counts stay live wherever you are, so "something needs me" is visible
// from any tab without polling that tab's contents.
setInterval(async () => {
  if (document.hidden || _locked || currentTab === 'today') return;
  const data = await api('/api/today');
  if (!data) return;
  navCount('nav-today', (data.items || []).filter(i => i.tone !== 'good').length);
  navCount('nav-outbox', (data.stats || {}).outbox_pending || 0);
}, 20000);

// Data tabs: auto-refresh live views without clobbering anything in progress.
// Outbox, settings and help are deliberately excluded — re-rendering the desk
// under someone mid-decision loses their place, and settings may be mid-edit.
setInterval(() => {
  if (document.hidden || _locked) return;
  switch (currentTab) {
    case 'today': loadToday(); loadTrend(); loadFunnel(); loadRuns(); loadTodayActivity(); break;
    case 'companies': if (!companyDrill) loadCompanies(); break;
    case 'prospects': loadProspects(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'conversations': loadConversations(); break;
    case 'activity': loadActivity(); break;
    case 'enrichment': loadEnrichment(); break;
    case 'inboxes': loadInboxes(); break;
    case 'usage': loadUsage(); break;
    case 'controls': loadLogs(); break;
  }
}, 15000);


// ── Your company (sellbird.offer): what VeloReach writes from, and the offer ──

let _company = null;
let _offerIdeas = [];

function ce(t) {
  const d = document.createElement('div');
  d.textContent = t == null ? '' : String(t);
  return d.innerHTML;
}

function lines(list) { return (list || []).join('\n'); }
function splitLines(text) { return String(text || '').split('\n').map(x => x.trim()).filter(Boolean); }

const COMPANY_PRESETS = {
  saas_b2b: {
    label: 'Custom B2B SaaS',
    desc: 'Promote your own SaaS product to startups, agencies, and software teams via personalized research & sequences.',
    product: {
      name: 'Your SaaS Name',
      pricing: 'Starting at $99/mo (14-day free trial)',
      description: 'B2B SaaS platform that eliminates manual workflow bottlenecks and saves teams 10+ hours a week.',
      key_benefits: [
        'Deploys in under 15 minutes with zero engineering lift',
        'Cuts manual ops work and speeds up revenue response time',
        'Predictable monthly pricing with clear ROI inside 30 days',
      ],
    },
    offer: 'a 2-minute custom teardown video showing where your current workflow leaks leads',
    icp: {
      industries: ['SaaS', 'Software', 'Digital Agency', 'E-commerce'],
      titles: ['Founder', 'CEO', 'Head of Growth', 'VP Operations'],
      company_size: '10-200 employees',
      geography: ['Austin, TX', 'San Francisco, CA', 'New York, NY'],
    },
  },
  saas_local: {
    label: 'SaaS -> Local Businesses',
    desc: 'Sell SaaS (booking, AI receptionist, SEO, CRM, billing, reviews) to dentists, HVAC, clinics, plumbers, and shops.',
    product: {
      name: 'LocalFlow SaaS',
      pricing: 'Starting at $149/mo per location',
      description: 'All-in-one automation & booking SaaS built specifically for local service businesses to capture missed calls and fill their calendar.',
      key_benefits: [
        'Replies to missed calls and web leads within 60 seconds',
        'Turns Google Maps traffic into confirmed appointments automatically',
        'Zero complex setup — works alongside existing phone & booking tools',
      ],
    },
    offer: 'a 1-page breakdown of local leads slipping past your website & Google listing this week',
    icp: {
      industries: ['dentist', 'hvac', 'plumber', 'roofer', 'clinic', 'auto repair'],
      titles: ['Owner', 'Practice Owner', 'Managing Partner', 'Office Manager'],
      company_size: '1-50 employees',
      geography: ['Denver, CO', 'Austin, TX'],
    },
  },
  agency: {
    label: 'B2B Service / Agency',
    desc: 'Outreach for dev shops, marketing agencies, or consultancies targeting local SMBs or growing SaaS companies.',
    product: {
      name: 'Growth Partner Studio',
      pricing: 'Retainer or performance-based',
      description: 'Done-for-you web conversion, paid acquisition, and CRM automation partner for service businesses.',
      key_benefits: [
        'Rebuilds slow landing pages into high-converting booking funnels',
        'Transparent weekly pipeline reporting tied directly to booked calls',
        'Launch first campaign inside 7 days',
      ],
    },
    offer: 'a custom 3-point conversion mockup for your homepage before any commitment',
    icp: {
      industries: ['dentist', 'real estate', 'lawyer', 'salon', 'gym'],
      titles: ['Owner', 'Founder', 'Managing Director'],
      company_size: '2-50 employees',
      geography: ['Chicago, IL', 'Miami, FL'],
    },
  },
};

const LOCAL_TRADE_CHIPS = [
  'dentist', 'hvac', 'plumber', 'roofer', 'lawyer', 'contractor',
  'clinic', 'chiro', 'vet', 'auto repair', 'salon', 'gym',
  'real estate', 'accountant', 'restaurant', 'SaaS',
];

function applyCompanyPreset(key) {
  const preset = COMPANY_PRESETS[key];
  if (!preset) return;
  const setV = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
  setV('co-p-name', preset.product.name);
  setV('co-p-pricing', preset.product.pricing);
  setV('co-p-desc', preset.product.description);
  setV('co-p-benefits', lines(preset.product.key_benefits));
  setV('offer-entry', preset.offer);
  setV('co-i-industries', lines(preset.icp.industries));
  setV('co-i-titles', lines(preset.icp.titles));
  setV('co-i-size', preset.icp.company_size);
  setV('co-i-geo', lines(preset.icp.geography));
  renderIndustryChipStates();
  showToast('Applied "' + preset.label + '" template — customize fields below and click Save on each card (or Save All Blueprint).', 'info');
}

async function saveCompanyBlueprintAll(btn) {
  const v = id => (document.getElementById(id)?.value || '');
  const orig = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }
  try {
    const calls = [
      api('/api/company', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          section: 'product',
          fields: {
            name: v('co-p-name'), pricing: v('co-p-pricing'),
            description: v('co-p-desc'), key_benefits: splitLines(v('co-p-benefits')),
          },
        }),
      }),
      api('/api/company', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          section: 'icp',
          fields: {
            industries: splitLines(v('co-i-industries')), titles: splitLines(v('co-i-titles')),
            company_size: v('co-i-size'), geography: splitLines(v('co-i-geo')),
          },
        }),
      }),
      api('/api/company', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          section: 'persona',
          fields: {
            name: v('co-s-name'), role: v('co-s-role'), company: v('co-s-company'),
            email: v('co-s-email'), linkedin: v('co-s-linkedin'), tone: v('co-s-tone'),
          },
        }),
      }),
      api('/api/offer', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          entry: v('offer-entry').trim(),
          booking_url: v('offer-booking-url').trim(),
        }),
      }),
    ];
    const results = await Promise.all(calls);
    const failed = results.find(r => !r || !r.success);
    if (failed) {
      showToast(failMsg(failed, 'Some sections could not be saved — check product name/description.'), 'error');
    } else {
      showToast('All SaaS, Offer, ICP, and Sender settings saved!', 'success');
      await loadCompany();
      loadToday();
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = orig || 'Save All Sections'; }
  }
}

function toggleIndustryChip(term) {
  const el = document.getElementById('co-i-industries');
  if (!el) return;
  const current = splitLines(el.value);
  const idx = current.findIndex(x => x.toLowerCase() === term.toLowerCase());
  if (idx >= 0) current.splice(idx, 1);
  else current.push(term);
  el.value = lines(current);
  renderIndustryChipStates();
}

function renderIndustryChipStates() {
  const el = document.getElementById('co-i-industries');
  const box = document.getElementById('co-industry-chips');
  if (!el || !box) return;
  const active = new Set(splitLines(el.value).map(x => x.toLowerCase()));
  box.querySelectorAll('[data-trade]').forEach(btn => {
    const on = active.has(String(btn.dataset.trade || '').toLowerCase());
    btn.classList.toggle('on', on);
    btn.setAttribute('aria-pressed', String(on));
  });
}

async function loadCompany() {
  const body = document.getElementById('company-body');
  const data = await api('/api/company');
  if (!data) { body.innerHTML = offlineState(); return; }
  if (data.error) { body.innerHTML = emptyState(icon('alert'), 'Couldn\'t read your company config',
    escHtml(data.error) + ' Fix it and reopen this tab, or retrain with <b>sellbird train &lt;url&gt;</b>.'); return; }
  _company = data;
  const o = data.offer || {}, p = data.product || {}, icp = data.icp || {}, s = data.persona || {};
  const objections = Object.entries(p.objection_responses || {});
  const isPlaceholder = /smoke test|placeholder|yourcompany/i.test((p.name || '') + ' ' + (s.company || ''));
  body.innerHTML = `
  <section class="card company-card playbook-card" aria-labelledby="co-playbook-h">
    <div class="company-card-head">
      <h3 id="co-playbook-h">Launch Your SaaS or Local Outreach</h3>
      <button class="btn btn-primary btn-sm" onclick="saveCompanyBlueprintAll(this)">Save All Sections</button>
    </div>
    ${isPlaceholder ? '<div class="banner" style="margin-bottom:4px">' + icon('alert') + '<div><b>Currently using Smoke Test placeholder details</b><p>Pick a starter blueprint below or enter your real SaaS website under <b>Train from Website</b> before sending real campaigns.</p></div></div>' : ''}
    <div class="playbook-steps">
      <div class="pb-step"><span class="pb-num">1</span><div><b>Define Offer &amp; SaaS</b><div class="hint">Train from URL or fill product, pricing &amp; CTA below</div></div></div>
      <div class="pb-step"><span class="pb-num">2</span><div><b>Set Target &amp; Cities</b><div class="hint">Pick local niches (dentist, hvac) or SaaS + cities</div></div></div>
      <div class="pb-step"><span class="pb-num">3</span><div><b><a href="#discover" onclick="goToTab('discover');return false">Discover</a> &amp; <a href="#today" onclick="goToTab('today');return false">Agentic Cycle</a></b><div class="hint">Find businesses, scrape websites &amp; verify decision-makers</div></div></div>
      <div class="pb-step"><span class="pb-num">4</span><div><b><a href="#outbox" onclick="goToTab('outbox');return false">Outbox</a> &amp; <a href="#settings" onclick="goToTab('settings');return false">Mailbox</a></b><div class="hint">Review/Rewrite AI drafts, then connect Gmail/SMTP</div></div></div>
    </div>
    <div>
      <div class="form-label" style="margin-bottom:6px">1-Click Starter Blueprints <span class="hint">(fills the form below without saving until you click Save)</span></div>
      <div class="preset-grid">
        ${Object.entries(COMPANY_PRESETS).map(([k, pr]) => `
          <button type="button" class="preset-card" onclick="applyCompanyPreset('${k}')">
            <span class="preset-title">${ce(pr.label)}</span>
            <span class="preset-desc">${ce(pr.desc)}</span>
          </button>`).join('')}
      </div>
    </div>
  </section>

  <section class="card company-card" aria-labelledby="co-brief-h">
    <div class="company-card-head">
      <h3 id="co-brief-h">Quick Setup: Describe What You Want &amp; Who You Need</h3>
      <span class="hint">Type in plain English or Roman Urdu — VeloReach fills your Product, Offer, Target Audience &amp; Cities</span>
    </div>
    <div class="form-group" style="margin-bottom:8px">
      <textarea class="form-input" id="co-brief-input" rows="2" placeholder="Example: Main apni AI appointment SaaS ($99/mo) dentists aur salons ko Austin, TX aur Denver, CO mein pitch karna chahta hoon. Free 15-min demo offer hai."></textarea>
    </div>
    <div class="offer-actions">
      <button class="btn btn-primary btn-sm" id="co-brief-btn" onclick="setupCompanyFromBrief(this)">Auto-Fill &amp; Save Setup</button>
      <span class="hint">Feeds directly into <b>Discover</b>, <b>Campaigns</b> &amp; <b>Outbox</b></span>
    </div>
  </section>

  <section class="card company-card" aria-labelledby="co-train-h">
    <div class="company-card-head">
      <h3 id="co-train-h">Train from Website</h3>
      <span class="hint">Crawl your SaaS or agency URL and auto-populate product, ICP, and objections</span>
    </div>
    <div class="company-train-row">
      <input type="url" class="form-input" id="co-train-url" placeholder="https://your-saas.com" autocomplete="off">
      <button class="btn btn-primary btn-sm" id="co-train-btn" onclick="trainCompanyFromUrl()">Train VeloReach</button>
    </div>
    <div id="co-train-status" class="hint" aria-live="polite" style="margin-top:8px"></div>
  </section>

  <section class="card company-card" aria-labelledby="co-offer-h">
    <div class="company-card-head">
      <h3 id="co-offer-h">Your offer</h3>
      <span class="hint">What every first email ends by offering</span>
    </div>
    ${o.proposed ? `<div class="offer-proposed">
      <div><span class="tag accent">Your agent proposed</span></div>
      <p class="offer-proposed-text">${ce(o.proposed.entry)}</p>
      ${o.proposed.why ? `<p class="desc">${ce(o.proposed.why)}</p>` : ''}
      <div class="offer-actions">
        <button class="btn btn-primary btn-sm" onclick="useProposedOffer()">Use this offer</button>
        <button class="btn btn-secondary btn-sm" onclick="dismissProposedOffer()">Dismiss</button>
      </div>
    </div>` : ''}
    <p class="desc">Each first email names something VeloReach found about the prospect, says what it costs them, and offers this as the fix. Make it free, specific, and deliverable this week. ${o.entry ? '' : '<b>No offer yet</b>, so first emails end with a question instead.'}</p>
    <div class="form-group"><label class="form-label" for="offer-entry">Offer CTA</label>
      <textarea class="form-input" id="offer-entry" rows="2" maxlength="300" placeholder="e.g. a 2-minute breakdown of leads slipping past your homepage &amp; Google listing">${ce(o.entry || '')}</textarea></div>
    <div class="form-group"><label class="form-label" for="offer-booking-url">Calendar / Booking Link <span class="hint">(optional Cal.com or Calendly URL shared when prospects ask to book)</span></label>
      <input type="url" class="form-input" id="offer-booking-url" placeholder="https://cal.com/you/15min" value="${ce(o.booking_url || '')}" autocomplete="off"></div>
    <div class="offer-actions">
      <button class="btn btn-primary btn-sm" onclick="saveOffer()">Save offer</button>
      <button class="btn btn-secondary btn-sm" id="offer-preview-btn" onclick="previewOffer()">Preview an email</button>
      <button class="btn btn-secondary btn-sm" id="offer-suggest-btn" onclick="suggestOffers()">Suggest ideas</button>
    </div>
    <div id="offer-preview" class="offer-preview" hidden></div>
    <div id="offer-ideas" class="offer-ideas"></div>
  </section>

  <section class="card company-card" aria-labelledby="co-product-h">
    <div class="company-card-head"><h3 id="co-product-h">What you sell</h3></div>
    <div class="company-grid">
      <div class="form-group"><label class="form-label" for="co-p-name">Product or SaaS name</label>
        <input class="form-input" id="co-p-name" value="${ce(p.name)}"></div>
      <div class="form-group"><label class="form-label" for="co-p-pricing">Pricing</label>
        <input class="form-input" id="co-p-pricing" value="${ce(p.pricing)}"></div>
    </div>
    <div class="form-group"><label class="form-label" for="co-p-desc">What it is and who it's for</label>
      <textarea class="form-input" id="co-p-desc" rows="3">${ce(p.description)}</textarea></div>
    <div class="form-group"><label class="form-label" for="co-p-benefits">Key benefits <span class="hint">one per line</span></label>
      <textarea class="form-input" id="co-p-benefits" rows="4">${ce(lines(p.key_benefits))}</textarea></div>
    <div class="offer-actions"><button class="btn btn-primary btn-sm" onclick="saveCompanySection('product')">Save</button></div>
  </section>

  <section class="card company-card" aria-labelledby="co-icp-h">
    <div class="company-card-head"><h3 id="co-icp-h">Who you sell to (ICP &amp; Local Markets)</h3></div>
    <div>
      <div class="form-label" style="margin-bottom:6px">Quick-toggle OpenStreetMap Local Trades &amp; B2B Niches</div>
      <div class="chip-row" id="co-industry-chips">
        ${LOCAL_TRADE_CHIPS.map(t => `<button type="button" class="chip" data-trade="${ce(t)}" aria-pressed="false" onclick="toggleIndustryChip(${jsq(t)})">${ce(t)}</button>`).join('')}
      </div>
    </div>
    <div class="company-grid">
      <div class="form-group"><label class="form-label" for="co-i-industries">Industries <span class="hint">one per line</span></label>
        <textarea class="form-input" id="co-i-industries" rows="4" oninput="renderIndustryChipStates()">${ce(lines(icp.industries))}</textarea></div>
      <div class="form-group"><label class="form-label" for="co-i-titles">Job titles <span class="hint">one per line</span></label>
        <textarea class="form-input" id="co-i-titles" rows="4">${ce(lines(icp.titles))}</textarea></div>
      <div class="form-group"><label class="form-label" for="co-i-size">Company size</label>
        <input class="form-input" id="co-i-size" value="${ce(icp.company_size)}"></div>
      <div class="form-group"><label class="form-label" for="co-i-geo">Where <span class="hint">one city per line (auto-geocoded on Discover)</span></label>
        <textarea class="form-input" id="co-i-geo" rows="2" placeholder="Denver, CO&#10;Austin, TX">${ce(lines(icp.geography))}</textarea></div>
    </div>
    <div class="offer-actions"><button class="btn btn-primary btn-sm" onclick="saveCompanySection('icp')">Save</button></div>
  </section>

  <section class="card company-card" aria-labelledby="co-sender-h">
    <div class="company-card-head"><h3 id="co-sender-h">Who it's from</h3>
      <span class="hint">Appears in email headers &amp; sign-offs</span></div>
    <div class="company-grid">
      <div class="form-group"><label class="form-label" for="co-s-name">Sender name <span class="hint">signed under every email</span></label>
        <input class="form-input" id="co-s-name" value="${ce(s.name)}"></div>
      <div class="form-group"><label class="form-label" for="co-s-role">Role / Title</label>
        <input class="form-input" id="co-s-role" value="${ce(s.role)}"></div>
      <div class="form-group"><label class="form-label" for="co-s-company">Company name</label>
        <input class="form-input" id="co-s-company" value="${ce(s.company)}"></div>
      <div class="form-group"><label class="form-label" for="co-s-email">Sender email</label>
        <input type="email" class="form-input" id="co-s-email" placeholder="founder@your-saas.com" value="${ce(s.email || '')}"></div>
    </div>
    <div class="company-grid">
      <div class="form-group"><label class="form-label" for="co-s-tone">Tone</label>
        <input class="form-input" id="co-s-tone" value="${ce(s.tone)}"></div>
      <div class="form-group"><label class="form-label" for="co-s-linkedin">LinkedIn URL <span class="hint">(optional)</span></label>
        <input class="form-input" id="co-s-linkedin" placeholder="linkedin.com/in/your-name" value="${ce(s.linkedin || '')}"></div>
    </div>
    <div class="offer-actions"><button class="btn btn-primary btn-sm" onclick="saveCompanySection('persona')">Save</button></div>
  </section>

  <section class="card company-card" aria-labelledby="co-obj-h">
    <div class="company-card-head"><h3 id="co-obj-h">Objections and answers</h3>
      <span class="hint">How replies handle pushback</span></div>
    <div id="co-objections" class="company-objections">
      ${(objections.length ? objections : [['', '']]).map(([k, v], i) => objectionRow(k, v, i)).join('')}
    </div>
    <div class="offer-actions">
      <button class="btn btn-secondary btn-sm" onclick="addObjection()">Add an objection</button>
      <button class="btn btn-primary btn-sm" onclick="saveCompanySection('product', true)">Save</button>
    </div>
  </section>

  ${data.knowledge || data.competitors ? `<section class="card company-card" aria-labelledby="co-learned-h">
    <div class="company-card-head"><h3 id="co-learned-h">What VeloReach learned from your site</h3>
      <span class="hint">From training. Retrain with <b>Train from Website</b> above or <code>sellbird train &lt;url&gt;</code></span></div>
    ${data.knowledge ? `<details class="company-learned"><summary>Product knowledge</summary><pre>${ce(data.knowledge)}</pre></details>` : ''}
    ${data.competitors ? `<details class="company-learned"><summary>Competitors</summary><pre>${ce(data.competitors)}</pre></details>` : ''}
  </section>` : ''}`;
  renderIndustryChipStates();
  _offerIdeas = (o.entry_ideas || []).map(x => ({offer: x}));
  renderOfferIdeas();
  navCount('nav-company', o.proposed ? 1 : 0);
}

async function setupCompanyFromBrief(btn) {
  const input = document.getElementById('co-brief-input');
  const brief = ((input && input.value) || '').trim();
  if (brief.length < 8) {
    showToast('Write a short sentence describing what you sell and who you want to target.', 'error');
    if (input && input.focus) input.focus();
    return;
  }
  const orig = btn ? btn.textContent : 'Auto-Fill & Save Setup';
  if (btn) { btn.disabled = true; btn.textContent = 'Building setup…'; }
  try {
    const res = await api('/api/company/setup-from-brief', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({brief}),
    });
    if (res && res.success) {
      showToast('Setup updated from your description' + (res.used_ai ? ' (AI).' : '.'), 'success');
      await loadCompany();
      loadToday();
    } else {
      showToast(failMsg(res, 'Could not build setup from description.'), 'error');
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = orig; }
  }
}

async function trainCompanyFromUrl() {
  const input = document.getElementById('co-train-url');
  const btn = document.getElementById('co-train-btn');
  const status = document.getElementById('co-train-status');
  const url = ((input && input.value) || '').trim();
  if (!url) {
    showToast('Enter your website URL first.', 'error');
    if (input && input.focus) input.focus();
    return;
  }
  const origText = btn ? btn.textContent : 'Train VeloReach';
  if (btn) { btn.disabled = true; btn.textContent = 'Training…'; }
  if (status) status.textContent = 'Crawling website and learning about your product…';
  try {
    const res = await api('/api/company/train', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url}),
    });
    if (res && res.success) {
      const msg = 'Trained from ' + (res.pages_crawled || 0) + ' page(s)' + (res.product ? ' — ' + res.product : '') + '.';
      if (status) status.textContent = msg;
      showToast(msg, 'success');
      await loadCompany();
      loadToday();
    } else {
      const err = failMsg(res, 'Could not train from that website.');
      if (status) status.textContent = err;
      showToast(err, 'error');
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

function objectionRow(k, v, i) {
  return `<div class="company-objection">
    <div class="form-group"><label class="form-label" for="co-ok-${i}">When they say</label>
      <input class="form-input co-ok" id="co-ok-${i}" value="${ce(k)}"></div>
    <div class="form-group"><label class="form-label" for="co-ov-${i}">Answer with</label>
      <textarea class="form-input co-ov" id="co-ov-${i}" rows="2">${ce(v)}</textarea></div>
  </div>`;
}

function addObjection() {
  const box = document.getElementById('co-objections');
  box.insertAdjacentHTML('beforeend', objectionRow('', '', box.children.length));
}

async function saveCompanySection(section, objectionsOnly) {
  const v = id => document.getElementById(id).value;
  let fields;
  if (section === 'product' && objectionsOnly) {
    const out = {};
    document.querySelectorAll('.company-objection').forEach(row => {
      const k = row.querySelector('.co-ok').value.trim(), a = row.querySelector('.co-ov').value.trim();
      if (k && a) out[k] = a;
    });
    fields = {objection_responses: out};
  } else if (section === 'product') {
    fields = {name: v('co-p-name'), pricing: v('co-p-pricing'), description: v('co-p-desc'),
              key_benefits: splitLines(v('co-p-benefits'))};
  } else if (section === 'icp') {
    fields = {industries: splitLines(v('co-i-industries')), titles: splitLines(v('co-i-titles')),
              company_size: v('co-i-size'), geography: splitLines(v('co-i-geo'))};
  } else if (section === 'persona') {
    fields = {
      name: v('co-s-name'), role: v('co-s-role'),
      company: v('co-s-company'), email: v('co-s-email'),
      linkedin: v('co-s-linkedin'), tone: v('co-s-tone'),
    };
  }
  const r = await api('/api/company', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                       body: JSON.stringify({section, fields})});
  if (r && r.success) showToast(r.message, 'success');
  else showToast((r && r.message) || 'Couldn\'t save.', 'error');
}

function renderOfferIdeas() {
  const box = document.getElementById('offer-ideas');
  if (!box) return;
  if (!_offerIdeas.length) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="form-label">Ideas to start from (click one to put it in the box, then make it yours)</div>' +
    _offerIdeas.map((idea, i) => `<button type="button" class="offer-idea" onclick="useOfferIdea(${i})">
      <span class="offer-idea-text">${ce(idea.offer)}</span>
      ${idea.finding ? `<span class="offer-idea-meta">Pairs with: ${ce(idea.finding)}</span>` : ''}
      ${idea.why ? `<span class="offer-idea-meta">${ce(idea.why)}</span>` : ''}
    </button>`).join('');
}

function useOfferIdea(i) {
  const el = document.getElementById('offer-entry');
  el.value = _offerIdeas[i].offer;
  el.focus();
}

async function suggestOffers() {
  const btn = document.getElementById('offer-suggest-btn');
  btn.disabled = true; btn.textContent = 'Thinking...';
  document.getElementById('offer-ideas').innerHTML =
    '<p class="desc">VeloReach is reading your product and coming up with offers that fit it...</p>';
  const r = await api('/api/offer/suggest', {method: 'POST'});
  btn.disabled = false; btn.textContent = 'Suggest ideas';
  if (!r || r.error || !r.ideas) { renderOfferIdeas(); showToast((r && r.error) || 'VeloReach couldn\'t come up with offers just now. Try again.', 'error'); return; }
  _offerIdeas = r.ideas;
  renderOfferIdeas();
}

async function previewOffer() {
  const entry = document.getElementById('offer-entry').value.trim();
  if (!entry) { showToast('Write an offer first.', 'error'); return; }
  const btn = document.getElementById('offer-preview-btn');
  const box = document.getElementById('offer-preview');
  btn.disabled = true; btn.textContent = 'Writing...';
  box.hidden = false;
  box.innerHTML = '<p class="desc">Writing a sample first email for one of your prospects. Nothing is saved or sent.</p>';
  box.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  const r = await api('/api/offer/preview', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                            body: JSON.stringify({entry})});
  btn.disabled = false; btn.textContent = 'Preview an email';
  if (!r || !r.ok) { box.innerHTML = `<p class="desc">${ce((r && r.message) || 'The preview failed. Try again.')}</p>`; return; }
  box.innerHTML = `<div class="form-label">Sample for ${ce(r.to)} (not saved, not sent)</div>
    <div class="offer-mail"><div class="offer-mail-subject">${ce(r.subject)}</div><pre>${ce(r.body)}</pre></div>`;
  box.scrollIntoView({block: 'nearest', behavior: 'smooth'});
}

async function saveOffer(entryOverride) {
  const entry = entryOverride != null ? entryOverride : document.getElementById('offer-entry').value.trim();
  const bookingEl = document.getElementById('offer-booking-url');
  const payload = {entry};
  if (bookingEl && entryOverride == null) payload.booking_url = bookingEl.value.trim();
  const r = await api('/api/offer', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                     body: JSON.stringify(payload)});
  if (r && r.success) { showToast(r.message, 'success'); loadCompany(); }
  else showToast((r && r.message) || 'Couldn\'t save the offer.', 'error');
}

async function useProposedOffer() {
  if (_company && _company.offer && _company.offer.proposed) await saveOffer(_company.offer.proposed.entry);
}

async function dismissProposedOffer() {
  await api('/api/offer/propose', {method: 'DELETE'});
  loadCompany();
}

(function wireInboxDialog() {
  const d = document.getElementById('inbox-dialog');
  if (!d) return;
  d.addEventListener('close', () => { if (_inboxPoll) { clearInterval(_inboxPoll); _inboxPoll = null; } });
  d.addEventListener('click', e => { if (e.target === d) closeInboxDialog(); });
})();


// ═══════════════════════════════════════════════════════════════════════════
// Brain Studio (Universal Model Router, Chain & Role Management)
// ═══════════════════════════════════════════════════════════════════════════

let _brainConfig = null;

const ALL_ROLES = [
  { id: 'scout', name: 'Scout', desc: 'Decision Maker & ICP scoring' },
  { id: 'writer', name: 'Writer', desc: 'Personalized cold copy & sequence generation' },
  { id: 'researcher', name: 'Deep Researcher', desc: 'OSINT web crawler & signal extractor' },
  { id: 'enricher', name: 'Clay Enricher', desc: 'Custom AI columns & structured attributes' },
  { id: 'critic', name: 'Copy Critic', desc: 'Hallucination, spam & deliverability review' },
  { id: 'handler', name: 'Handler', desc: 'Reply categorization & auto-booking' },
  { id: 'analyst', name: 'Analyst', desc: 'Campaign performance & funnel diagnostics' },
  { id: 'trainer', name: 'Trainer', desc: 'Product knowledge & objection playbook' }
];

const KNOWN_PROVIDERS = [
  'openai_compat', 'openrouter', 'deepseek', 'xai', 'gemini', 'groq',
  'together', 'mistral', 'fireworks', 'cerebras', 'ollama', 'lmstudio', 'vllm', 'custom'
];

const PROVIDER_DEFAULTS = {
  deepseek:      { model: 'deepseek-chat',                                   url: 'https://api.deepseek.com/v1',                           env: 'DEEPSEEK_API_KEY' },
  openrouter:    { model: 'openai/gpt-4o-mini',                              url: 'https://openrouter.ai/api/v1',                          env: 'OPENROUTER_API_KEY' },
  openai:        { model: 'gpt-4o-mini',                                     url: 'https://api.openai.com/v1',                             env: 'OPENAI_API_KEY' },
  openai_compat: { model: 'gpt-4o-mini',                                     url: '',                                                      env: 'OPENAI_API_KEY' },
  gemini:        { model: 'gemini-2.5-flash',                                url: 'https://generativelanguage.googleapis.com/v1beta/openai', env: 'GEMINI_API_KEY' },
  groq:          { model: 'llama-3.3-70b-versatile',                         url: 'https://api.groq.com/openai/v1',                        env: 'GROQ_API_KEY' },
  xai:           { model: 'grok-3-mini',                                     url: 'https://api.x.ai/v1',                                   env: 'XAI_API_KEY' },
  mistral:       { model: 'mistral-small-latest',                            url: 'https://api.mistral.ai/v1',                             env: 'MISTRAL_API_KEY' },
  together:      { model: 'meta-llama/Llama-3.3-70B-Instruct-Turbo',         url: 'https://api.together.xyz/v1',                           env: 'TOGETHER_API_KEY' },
  fireworks:     { model: 'accounts/fireworks/models/llama-v3p3-70b-instruct', url: 'https://api.fireworks.ai/inference/v1',               env: 'FIREWORKS_API_KEY' },
  cerebras:      { model: 'llama-3.3-70b',                                   url: 'https://api.cerebras.ai/v1',                            env: 'CEREBRAS_API_KEY' },
  ollama:        { model: 'llama3.1',                                        url: 'http://localhost:11434/v1',                             env: '' },
  lmstudio:      { model: 'local-model',                                     url: 'http://localhost:1234/v1',                              env: '' },
  vllm:          { model: 'local-model',                                     url: 'http://localhost:8000/v1',                              env: '' },
  custom:        { model: '',                                                url: '',                                                      env: '' },
};

async function loadBrainStudio() {
  const data = await api('/api/brain/config');
  if (!data) {
    // Never leave the studio stuck on "Loading…" when the read fails.
    const chainEl = document.getElementById('brain-chain-container');
    const rolesEl = document.getElementById('brain-roles-grid');
    if (chainEl) chainEl.innerHTML = offlineState();
    if (rolesEl) rolesEl.innerHTML = '';
    return;
  }
  _brainConfig = data;

  const routerToggle = document.getElementById('brain-activate-router');
  if (routerToggle) routerToggle.checked = !!data.transport_active;

  // Render budget & spend
  const maxUsd = (data.budget && data.budget.max_usd_per_day != null) ? Number(data.budget.max_usd_per_day) : 5.0;
  const spentUsd = (data.spend && data.spend.spend_today_usd != null) ? Number(data.spend.spend_today_usd) : 0.0;

  const spendEl = document.getElementById('brain-spend-usd');
  if (spendEl) spendEl.textContent = '$' + spentUsd.toFixed(4);
  const capEl = document.getElementById('brain-spend-cap');
  if (capEl) capEl.textContent = `of $${maxUsd.toFixed(2)}/day limit`;
  const inpEl = document.getElementById('brain-max-usd');
  if (inpEl) inpEl.value = maxUsd;

  const pct = maxUsd > 0 ? Math.min(100, Math.round((spentUsd / maxUsd) * 100)) : 0;
  const barEl = document.getElementById('brain-budget-bar');
  if (barEl) {
    barEl.style.width = pct + '%';
    barEl.className = 'progress-fill ' + (pct >= 90 ? 'red' : pct >= 70 ? 'yellow' : 'emerald');
  }
  const statusEl = document.getElementById('brain-budget-status');
  if (statusEl) {
    if (maxUsd > 0 && spentUsd >= maxUsd) {
      statusEl.textContent = 'Budget Exhausted';
      statusEl.className = 'badge t-bad';
    } else if (maxUsd > 0 && pct >= 80) {
      statusEl.textContent = 'Near Limit';
      statusEl.className = 'badge t-waiting';
    } else {
      statusEl.textContent = 'Active';
      statusEl.className = 'tag accent';
    }
  }

  // Render Chain
  renderBrainChain(data.chain || []);

  // Render Roles
  renderBrainRoles(data.profiles || {});
}

function onChainProviderChange(selectEl) {
  const row = selectEl && selectEl.closest('.chain-row');
  if (!row) return;
  const prov = (selectEl.value || '').toLowerCase();
  const def = own(PROVIDER_DEFAULTS, prov);
  if (!def) return;
  const modelEl = row.querySelector('.chain-model');
  const urlEl = row.querySelector('.chain-url');
  const envEl = row.querySelector('.chain-env');
  const knownModels = new Set(Object.values(PROVIDER_DEFAULTS).map(d => d.model).filter(Boolean));
  const knownUrls = new Set(Object.values(PROVIDER_DEFAULTS).map(d => d.url).filter(Boolean));
  const knownEnvs = new Set(Object.values(PROVIDER_DEFAULTS).map(d => d.env).filter(Boolean));
  if (modelEl && (!modelEl.value.trim() || knownModels.has(modelEl.value.trim()))) {
    modelEl.value = def.model;
  }
  if (urlEl && (!urlEl.value.trim() || knownUrls.has(urlEl.value.trim()))) {
    urlEl.value = def.url;
  }
  if (envEl && (!envEl.value.trim() || knownEnvs.has(envEl.value.trim()))) {
    envEl.value = def.env;
  }
}

function renderBrainChain(chain) {
  const container = document.getElementById('brain-chain-container');
  if (!container) return;
  if (!chain || !chain.length) {
    container.innerHTML = '<div class="inset" style="text-align:center;color:var(--text-2);padding:14px;">No fallback chain configured. Requests will use default openai_compat.</div>';
    return;
  }
  container.innerHTML = chain.map((item, idx) => {
    const rawProv = (item.provider || '').trim();
    const provLower = rawProv.toLowerCase();
    const opts = (!rawProv || KNOWN_PROVIDERS.includes(provLower))
      ? KNOWN_PROVIDERS
      : [rawProv, ...KNOWN_PROVIDERS];
    return `
      <div class="chain-row" data-idx="${idx}">
        <span class="chain-idx">${idx + 1}</span>
        <select class="form-input chain-prov" onchange="onChainProviderChange(this)">
          ${opts.map(p => `<option value="${escHtml(p)}" ${p.toLowerCase() === provLower ? 'selected' : ''}>${escHtml(p)}</option>`).join('')}
        </select>
        <input type="text" class="form-input chain-model" placeholder="Model ID (e.g. deepseek-chat)" value="${escHtml(item.model || '')}">
        <input type="text" class="form-input chain-url" placeholder="Base URL (optional)" value="${escHtml(item.base_url || '')}">
        <input type="text" class="form-input chain-env" placeholder="API Key Env (e.g. DEEPSEEK_API_KEY)" value="${escHtml(item.api_key_env || '')}">
        <button class="btn btn-ghost" title="Remove" onclick="removeChainRow(this)"><svg class="icon"><use href="#i-close"/></svg></button>
      </div>
    `;
  }).join('');
}

function addChainRow() {
  const container = document.getElementById('brain-chain-container');
  if (!container) return;
  const idx = container.querySelectorAll('.chain-row').length;
  const defProv = 'deepseek';
  const def = PROVIDER_DEFAULTS[defProv];
  const div = document.createElement('div');
  div.className = 'chain-row';
  div.dataset.idx = idx;
  div.innerHTML = `
    <span class="chain-idx">${idx + 1}</span>
    <select class="form-input chain-prov" onchange="onChainProviderChange(this)">
      ${KNOWN_PROVIDERS.map(p => `<option value="${escHtml(p)}" ${p === defProv ? 'selected' : ''}>${escHtml(p)}</option>`).join('')}
    </select>
    <input type="text" class="form-input chain-model" placeholder="Model ID" value="${escHtml(def.model)}">
    <input type="text" class="form-input chain-url" placeholder="Base URL (optional)" value="${escHtml(def.url)}">
    <input type="text" class="form-input chain-env" placeholder="API Key Env (e.g. DEEPSEEK_API_KEY)" value="${escHtml(def.env)}">
    <button class="btn btn-ghost" title="Remove" onclick="removeChainRow(this)"><svg class="icon"><use href="#i-close"/></svg></button>
  `;
  container.appendChild(div);
}

function removeChainRow(btn) {
  const row = btn.closest('.chain-row');
  if (row) row.remove();
  const rows = document.querySelectorAll('#brain-chain-container .chain-row');
  rows.forEach((r, i) => {
    const idxEl = r.querySelector('.chain-idx');
    if (idxEl) idxEl.textContent = i + 1;
  });
}

async function toggleActivateRouter(enabled) {
  const toggle = document.getElementById('brain-activate-router');
  const payload = {
    activate_router: !!enabled,
    chain: (_brainConfig && Array.isArray(_brainConfig.chain) && _brainConfig.chain.length)
      ? _brainConfig.chain : undefined,
    profiles: (_brainConfig && _brainConfig.profiles) ? _brainConfig.profiles : undefined,
  };
  const res = await api('/api/brain/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (res && res.success) {
    showToast(enabled ? 'AI Router activated.' : 'AI Router deactivated.', 'success');
    loadBrainStudio();
  } else {
    if (toggle) toggle.checked = !enabled;
    showToast((res && res.message) || 'Failed to update AI Router state.', 'error');
  }
}

async function saveBrainBudget() {
  const val = parseFloat((document.getElementById('brain-max-usd') || {}).value || '5');
  const payload = {
    budget: { max_usd_per_day: val },
    max_usd_per_day: val,
    profiles: _brainConfig ? (_brainConfig.profiles || {}) : {}
  };
  if (_brainConfig && Array.isArray(_brainConfig.chain) && _brainConfig.chain.length) {
    payload.chain = _brainConfig.chain;
  }
  const res = await api('/api/brain/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  if (res && res.success) { showToast('Daily budget updated.', 'success'); loadBrainStudio(); }
  else showToast((res && res.message) || 'Failed to update budget.', 'error');
}

async function saveBrainChain() {
  const rows = document.querySelectorAll('#brain-chain-container .chain-row');
  const chain = [];
  rows.forEach(r => {
    const prov = (r.querySelector('.chain-prov') || {}).value || 'openai_compat';
    const model = ((r.querySelector('.chain-model') || {}).value || '').trim();
    const url = ((r.querySelector('.chain-url') || {}).value || '').trim();
    const env = ((r.querySelector('.chain-env') || {}).value || '').trim();
    chain.push({
      provider: prov,
      model: model,
      base_url: url,
      api_key_env: env
    });
  });
  if (!chain.length) { showToast('Add at least one provider in the chain.', 'error'); return; }

  const payload = {
    budget: { max_usd_per_day: parseFloat((document.getElementById('brain-max-usd') || {}).value || '5') },
    chain: chain,
    profiles: _brainConfig ? _brainConfig.profiles : {}
  };
  const res = await api('/api/brain/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  if (res && res.success) { showToast('Fallback chain saved.', 'success'); loadBrainStudio(); }
  else showToast((res && res.message) || 'Failed to save chain.', 'error');
}

function renderBrainRoles(profiles) {
  const grid = document.getElementById('brain-roles-grid');
  if (!grid) return;
  grid.innerHTML = ALL_ROLES.map(role => {
    const p = profiles[role.id] || {};
    return `
      <div class="role-box" data-role="${role.id}">
        <div class="role-box-head">
          <b>${role.name}</b>
          <span>${role.desc}</span>
        </div>
        <div class="role-box-inputs">
          <input type="text" class="form-input role-model" placeholder="Model (optional)" value="${escHtml(p.model || '')}">
          <input type="text" class="form-input role-prov" placeholder="Provider override" value="${escHtml(p.provider || '')}">
        </div>
      </div>
    `;
  }).join('');
}

async function saveBrainRoles() {
  const boxes = document.querySelectorAll('#brain-roles-grid .role-box');
  const profiles = {};
  boxes.forEach(b => {
    const roleId = b.dataset.role;
    const model = ((b.querySelector('.role-model') || {}).value || '').trim();
    const prov = ((b.querySelector('.role-prov') || {}).value || '').trim();
    profiles[roleId] = {
      model: model,
      provider: prov
    };
  });
  const chain = (_brainConfig && _brainConfig.chain) ? _brainConfig.chain : [];
  const payload = {
    budget: { max_usd_per_day: parseFloat((document.getElementById('brain-max-usd') || {}).value || '5') },
    chain: chain,
    profiles: profiles
  };
  const res = await api('/api/brain/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  if (res && res.success) { showToast('Role routing saved.', 'success'); loadBrainStudio(); }
  else showToast((res && res.message) || 'Failed to save roles.', 'error');
}

async function runBrainDoctor() {
  const btn = document.getElementById('brain-doctor-btn');
  const statusEl = document.getElementById('brain-doctor-status');
  const panel = document.getElementById('brain-doctor-results');
  if (btn) btn.disabled = true;
  if (statusEl) statusEl.textContent = 'Probing endpoints...';
  if (panel) panel.style.display = 'none';

  try {
    const data = await api('/api/brain/doctor?network=1');
    if (!data) {
      if (statusEl) statusEl.textContent = 'Doctor check failed.';
      return;
    }
    const providers = data.providers || data.checks || [];
    const readyCount = providers.filter(c => c && c.ok).length;
    if (statusEl) {
      statusEl.textContent = data.summary || `Completed — ${readyCount}/${providers.length} provider(s) ready.`;
    }
    if (panel) {
      panel.innerHTML = providers.map(c => {
        const sub = [c.model, c.base_url].filter(Boolean).map(escHtml).join(' &middot; ');
        const statusText = c.ok ? 'Ready' : escHtml(c.error || c.detail || 'Failed');
        const latNum = Number(c.latency_ms);
        const latHtml = (c.latency_ms != null && c.latency_ms !== '' && Number.isFinite(latNum))
          ? `<span class="meta">${Math.round(latNum * 10) / 10}ms</span>` : '';
        return `
          <div class="doctor-row">
            <div>
              <b>${escHtml(c.name || c.provider || '')}</b>
              ${sub ? `<div class="meta" style="font-size:11px;color:var(--text-2)">${sub}</div>` : ''}
            </div>
            <div style="display:flex;align-items:center;gap:8px;">
              <span class="email-tag ${c.ok ? 'verified' : 'invalid'}" title="${escHtml(c.detail || '')}">${statusText}</span>
              ${latHtml}
            </div>
          </div>
        `;
      }).join('') || '<div class="meta">No provider checks returned.</div>';
      panel.style.display = 'block';
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function runBrainProbe() {
  const role = (document.getElementById('probe-role') || {}).value || 'scout';
  const domain = ((document.getElementById('probe-domain') || {}).value || '').trim();
  const promptEl = document.getElementById('probe-prompt');
  const customPrompt = promptEl ? (promptEl.value || '').trim() : '';
  const prompt = customPrompt || (domain ? `Verify connectivity for ${domain}. Reply with OK.` : 'Reply with the single word OK.');
  const btn = document.getElementById('probe-btn');
  const out = document.getElementById('probe-output');
  if (btn) btn.disabled = true;
  if (out) { out.style.display = 'block'; out.textContent = 'Sending probe via ' + role + '...'; }

  try {
    const payload = { live: true, role, prompt };
    if (domain) payload.domain = domain;
    const data = await api('/api/brain/probe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (out) {
      out.textContent = data
        ? JSON.stringify(data, null, 2)
        : 'Error: Probe request failed or dashboard server unreachable.';
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Clay AI Custom Columns
// ═══════════════════════════════════════════════════════════════════════════

async function loadEnrichment() {
  const list = document.getElementById('clay-column-list');
  const matrixEl = document.getElementById('clay-matrix-table');
  if (!list) return;
  const data = await api('/api/enrichment/columns');
  if (!data) {
    list.innerHTML = offlineState();
    if (matrixEl) matrixEl.innerHTML = offlineState();
    return;
  }
  const cols = data.columns || [];
  const vals = data.values || {};
  const companiesMap = data.companies || {};
  if (!cols.length) {
    list.innerHTML = emptyState(icon('enrichment'), 'No AI columns yet',
      'Define a prompt once and VeloReach runs it against every company it has read. ' +
      'Click <b>+ Add AI Column</b> to create the first one.');
  } else {
    list.innerHTML = cols.map(col => {
      const colId = col.id || col.key || '';
      const colKey = col.key || col.id || '';
      const colType = col.output_type || col.output_format || col.value_type || 'text';
      return `
        <div class="clay-col-item">
          <div>
            <h4>${escHtml(col.name)} <span class="tag" style="text-transform:none;font-weight:normal">${escHtml(colKey)}</span> <span class="tag accent">${escHtml(colType)}</span>${col.use_web_search ? ' <span class="tag">Web Search</span>' : ''}</h4>
            <p>${escHtml(col.prompt || 'No instruction prompt.')}</p>
          </div>
          <div style="display:flex;align-items:center;gap:6px;flex-shrink:0">
            <button class="btn btn-secondary btn-sm" onclick="runEnrichmentColumn(${jsq(colId)}, this)">Run Column</button>
            <button class="btn btn-ghost" title="Delete Column" onclick="deleteEnrichmentCol(${jsq(colId)})">
              <svg class="icon"><use href="#i-close"/></svg>
            </button>
          </div>
        </div>
      `;
    }).join('');
  }

  if (matrixEl) {
    const companyIds = Object.keys(companiesMap);
    for (const cid of Object.keys(vals)) {
      if (!Object.prototype.hasOwnProperty.call(companiesMap, cid)) companyIds.push(cid);
    }
    if (!companyIds.length || !cols.length) {
      matrixEl.innerHTML = emptyState(icon('enrichment'), 'No matrix rows yet',
        'Add at least one AI column and discover companies to view and export enriched matrix data.');
      return;
    }
    let mHtml = '<div class="table-card" style="overflow-x:auto"><table class="nowrap"><thead><tr>' +
      '<th>Company</th><th>Domain</th>';
    for (const col of cols) {
      mHtml += '<th>' + escHtml(col.name || col.key || col.id) + '</th>';
    }
    mHtml += '<th>Actions</th></tr></thead><tbody>';
    for (const cid of companyIds) {
      const comp = companiesMap[cid] || { id: cid, name: cid, domain: '' };
      const compVals = vals[cid] || {};
      const domainUrl = comp.domain ? (isHttpUrl(comp.domain) ? comp.domain : 'https://' + comp.domain) : '';
      const domainCell = safeHref(domainUrl)
        ? '<a href="' + safeHref(domainUrl) + '" target="_blank" rel="noopener noreferrer">' + escHtml(comp.domain) + '</a>'
        : escHtml(comp.domain || '');
      mHtml += '<tr><td class="cell-main">' + escHtml(comp.name || cid) + '</td><td>' + domainCell + '</td>';
      for (const col of cols) {
        const cell = compVals[col.id] || (col.key ? compVals[col.key] : null);
        const valText = cell && cell.value ? escHtml(cell.value) : '<span class="muted">—</span>';
        const srcLink = (cell && cell.source_url && safeHref(cell.source_url))
          ? ' <a href="' + safeHref(cell.source_url) + '" target="_blank" rel="noopener noreferrer" class="hint">[source]</a>'
          : '';
        mHtml += '<td class="clay-cell" title="' + escHtml((cell && cell.value) || '') + '">' + valText + srcLink + '</td>';
      }
      mHtml += '<td><button class="btn btn-secondary btn-sm" onclick="runCompanyEnrichment(' + jsq(cid) + ', this)">Run</button></td></tr>';
    }
    matrixEl.innerHTML = mHtml + '</tbody></table></div>';
  }
}

async function runEnrichmentColumn(colId, btn) {
  if (!colId) return;
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/enrichment/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ column_id: colId }),
    });
    if (res && res.success) {
      showToast('Column enrichment completed.', 'success');
      loadEnrichment();
    } else {
      showToast((res && res.message) || 'Column enrichment failed.', 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function openAddColumnModal() {
  const nameEl = document.getElementById('col-name');
  const keyEl = document.getElementById('col-key');
  const promptEl = document.getElementById('col-prompt');
  const typeEl = document.getElementById('col-type');
  const webEl = document.getElementById('col-web-search');
  if (nameEl) nameEl.value = '';
  if (keyEl) keyEl.value = '';
  if (promptEl) promptEl.value = '';
  if (typeEl) typeEl.value = 'text';
  if (webEl) webEl.checked = false;
  const d = document.getElementById('enrichment-dialog');
  if (d && d.showModal) d.showModal();
}

function closeAddColumnModal() {
  const d = document.getElementById('enrichment-dialog');
  if (d && d.close) d.close();
}

(function wireEnrichmentDialog() {
  const d = document.getElementById('enrichment-dialog');
  if (!d) return;
  d.addEventListener('click', e => { if (e.target === d) closeAddColumnModal(); });
})();

function autoSlugColKey(val) {
  const keyEl = document.getElementById('col-key');
  if (keyEl) keyEl.value = String(val || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
}

async function submitAddColumn() {
  const name = ((document.getElementById('col-name') || {}).value || '').trim();
  const key = ((document.getElementById('col-key') || {}).value || '').trim();
  const prompt = ((document.getElementById('col-prompt') || {}).value || '').trim();
  const type = ((document.getElementById('col-type') || {}).value || 'text').trim();
  const useWebSearch = !!(document.getElementById('col-web-search') || {}).checked;

  if (!name || !key || !prompt) { showToast('Please enter Name, Key, and AI Prompt.', 'error'); return; }

  const res = await api('/api/enrichment/columns', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name: name,
      key: key,
      prompt: prompt,
      output_type: type,
      output_format: type,
      value_type: type,
      use_web_search: useWebSearch
    })
  });

  if (res && res.success) {
    showToast('Custom column added.', 'success');
    closeAddColumnModal();
    loadEnrichment();
  } else {
    showToast((res && res.message) || 'Failed to add column.', 'error');
  }
}

async function deleteEnrichmentCol(colId) {
  if (!confirm('Delete this AI column?')) return;
  const res = await api('/api/enrichment/columns/' + encodeURIComponent(colId), { method: 'DELETE' });
  if (res && res.success) { showToast('Column deleted.', 'success'); loadEnrichment(); }
  else showToast((res && res.message) || 'Failed to delete column.', 'error');
}

async function triggerEnrichmentRun() {
  const btn = document.getElementById('enrichment-run-btn');
  const statusEl = document.getElementById('enrichment-run-status');
  if (btn) btn.disabled = true;
  if (statusEl) statusEl.textContent = 'Enrichment started in background...';
  try {
    const res = await api('/api/enrichment/run', { method: 'POST' });
    if (res && res.success) {
      const count = res.enriched_cells ?? res.result ?? res.enriched_count ?? 0;
      showToast('Batch enrichment completed.', 'success');
      if (statusEl) statusEl.textContent = 'Enriched: ' + count + ' items processed.';
      loadEnrichment();
    } else {
      showToast((res && res.message) || 'Enrichment trigger failed.', 'error');
      if (statusEl) statusEl.textContent = 'Ready';
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}
