// Client Finder — interface logic. Plain ES module, no dependencies.
// Talks to the local API described in docs/API.md.
//
// Safety rule used throughout: text from the API only ever reaches the page
// as text nodes (never as HTML), and links only get an href after passing
// safeUrl / telHref / mailHref.

const SVG_NS = 'http://www.w3.org/2000/svg';
const $ = (id) => document.getElementById(id);

const VIEWS = ['find', 'leads', 'settings'];
const VIEW_TITLES = { find: 'Find', leads: 'Leads', settings: 'Settings' };
const STATUSES = ['new', 'shortlisted', 'contacted', 'replied', 'won', 'rejected'];
const STATUS_LABELS = {
  new: 'New', shortlisted: 'Shortlisted', contacted: 'Contacted',
  replied: 'Replied', won: 'Won', rejected: 'Rejected',
};
const SERVICE_LABELS = { website: 'Website', app: 'App', ai: 'AI', data: 'Data' };
const SEVERITIES = [
  ['high', 'High priority'],
  ['medium', 'Medium priority'],
  ['low', 'Low priority'],
];
const SOCIAL_LABELS = {
  facebook: 'Facebook', instagram: 'Instagram', linkedin: 'LinkedIn', twitter: 'Twitter',
  x: 'X', youtube: 'YouTube', tiktok: 'TikTok', whatsapp: 'WhatsApp', pinterest: 'Pinterest',
  yelp: 'Yelp', threads: 'Threads',
};
const SORTS = ['score', 'newest', 'name', 'rating'];
const FILTER_KEYS = ['q', 'status', 'min_score', 'has_email', 'has_website', 'sort', 'search_id'];
const DEFAULT_FILTERS = Object.freeze({
  q: '', status: '', min_score: '', has_email: '', has_website: '', sort: 'score', search_id: '',
});
const OFFLINE_MESSAGE = 'Client Finder is not answering. Check that the app is still running on this computer.';
const POLL_MS = 2000;
const POLL_OFFLINE_MS = 5000;

const state = {
  settings: null,
  searches: [], searchesLoaded: false, searchesFailed: false,
  stats: null,
  leads: [], leadsLoaded: false, leadsFailed: false,
  detailId: null, detail: null, detailFailed: false,
  seedLead: null,
  offline: false,
};
let route = { view: null, filters: { ...DEFAULT_FILTERS }, lead: null };
const els = {};

/* ------------------------------------------------------------------ */
/* DOM helpers                                                         */
/* ------------------------------------------------------------------ */

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  let value;
  if (attrs) {
    for (const [key, val] of Object.entries(attrs)) {
      if (val === null || val === undefined || val === false) continue;
      if (key === 'class') el.className = val;
      else if (key === 'dataset') {
        for (const [dk, dv] of Object.entries(val)) {
          if (dv !== null && dv !== undefined) el.dataset[dk] = String(dv);
        }
      }
      else if (key === 'value') value = val;
      else if (val === true) el.setAttribute(key, '');
      else el.setAttribute(key, String(val));
    }
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  if (value !== undefined) el.value = value;
  return el;
}

function icon(name) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

function fill(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
}

function srOnly(text) {
  return h('span', { class: 'sr-only' }, text);
}

function announce(text) {
  els.live.textContent = '';
  // A fresh text node on the next frame makes screen readers read repeats too.
  requestAnimationFrame(() => { els.live.textContent = text; });
}

/**
 * Keyed list update. Rows whose data did not change keep their DOM node, so
 * polling never steals focus, closes an open dropdown or resets scroll.
 */
function syncList(parent, items, keyOf, render) {
  const existing = new Map();
  for (const child of Array.from(parent.children)) {
    if (child.dataset.key !== undefined) existing.set(child.dataset.key, child);
  }
  let prev = null;
  for (const item of items) {
    const key = String(keyOf(item));
    const sig = JSON.stringify(item);
    let el = existing.get(key);
    const active = document.activeElement;
    const busySelect = el && active && active.tagName === 'SELECT' && el.contains(active);
    if (!el || (el._sig !== sig && !busySelect)) {
      const fresh = render(item);
      fresh.dataset.key = key;
      fresh._sig = sig;
      if (el) {
        const focusKey = el.contains(active) ? active.dataset.fk : null;
        el.replaceWith(fresh);
        if (focusKey) {
          const again = fresh.querySelector(`[data-fk="${CSS.escape(focusKey)}"]`);
          if (again) again.focus({ preventScroll: true });
        }
      }
      el = fresh;
    }
    existing.delete(key);
    const wanted = prev ? prev.nextElementSibling : parent.firstElementChild;
    if (wanted !== el) parent.insertBefore(el, wanted);
    prev = el;
  }
  for (const stale of existing.values()) stale.remove();
}

/* ------------------------------------------------------------------ */
/* Formatting and link safety                                          */
/* ------------------------------------------------------------------ */

/** Returns a normalised http(s) URL, or null for anything else. */
function safeUrl(raw) {
  if (typeof raw !== 'string') return null;
  const text = raw.trim();
  if (!text) return null;
  let url;
  try {
    url = new URL(text);
  } catch {
    // A bare domain such as "example.com/contact" — treat it as https.
    if (!/^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(:\d+)?([/?#].*)?$/i.test(text)) return null;
    try { url = new URL(`https://${text}`); } catch { return null; }
  }
  return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
}

function prettyUrl(raw) {
  return String(raw || '').trim().replace(/^https?:\/\//i, '').replace(/^www\./i, '').replace(/\/$/, '');
}

function telHref(phone) {
  const digits = String(phone || '').replace(/[^\d+]/g, '');
  return /\d{3,}/.test(digits) ? `tel:${digits}` : null;
}

function mailHref(email) {
  const text = String(email || '').trim();
  if (!/^[^\s@<>"'`\\]+@[^\s@<>"'`\\]+\.[^\s@<>"'`\\]+$/.test(text)) return null;
  return `mailto:${encodeURIComponent(text).replace(/%40/g, '@')}`;
}

function plural(n, one, many) {
  return `${Number(n || 0).toLocaleString()} ${n === 1 ? one : many}`;
}

function parseDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
const dayFmt = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', year: 'numeric' });

function fmtWhen(iso) {
  const d = parseDate(iso);
  if (!d) return '';
  const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOf(new Date()) - startOf(d)) / 86400000);
  const time = timeFmt.format(d);
  if (days === 0) return `Today, ${time}`;
  if (days === 1) return `Yesterday, ${time}`;
  return `${dayFmt.format(d)}, ${time}`;
}

function fmtSeconds(ms) {
  const s = ms / 1000;
  return `${s < 10 ? s.toFixed(1) : Math.round(s)} seconds`;
}

function fmtSize(kb) {
  if (kb >= 1024) return `${(kb / 1024).toFixed(1)} MB`;
  return `${Math.round(kb).toLocaleString()} KB`;
}

function searchTitle(s) {
  return s.location ? `${s.query} in ${s.location}` : String(s.query || '');
}

function leadName(lead) {
  return (lead.name && String(lead.name).trim()) || prettyUrl(lead.website) || 'Unnamed business';
}

const isAuditing = (lead) => !!lead && (lead.audit_status === 'pending' || lead.audit_status === 'running');
const isSearchActive = (s) => s.status !== 'done' && s.status !== 'error';

/* ------------------------------------------------------------------ */
/* API                                                                 */
/* ------------------------------------------------------------------ */

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function errorMessage(res) {
  let detail;
  try {
    const data = await res.json();
    detail = data && data.detail;
  } catch { /* not JSON */ }
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => d && (d.msg || d.message)).filter(Boolean);
    if (parts.length) return parts.join('. ');
  }
  return `Something went wrong (error ${res.status}). Try again.`;
}

async function request(path, { method = 'GET', body } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method,
      cache: 'no-store',
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    setOffline(true);
    throw new ApiError(OFFLINE_MESSAGE, 0);
  }
  setOffline(false);
  if (!res.ok) throw new ApiError(await errorMessage(res), res.status);
  return res;
}

async function api(path, options) {
  const res = await request(path, options);
  if (res.status === 204) return null;
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    throw new ApiError('The app sent back something unreadable. Try again.', res.status);
  }
}

function setOffline(flag) {
  if (state.offline === flag) return;
  state.offline = flag;
  els.offlineBanner.hidden = !flag;
  if (!flag) refreshCurrentView();
}

/** Every failed request ends up here, so nothing fails silently. */
function reportError(err, { quiet = false } = {}) {
  const message = (err && err.message) || 'Something went wrong. Try again.';
  // When the app is unreachable the banner across the top already says so.
  // (With the drawer open the banner sits behind it, so a toast is shown too.)
  if (err && err.status === 0 && (quiet || state.detailId === null)) return;
  toast(message, 'error', { quiet });
}

/* ------------------------------------------------------------------ */
/* Toasts                                                              */
/* ------------------------------------------------------------------ */

const quietToastTimes = new Map();

function toast(message, kind = 'info', { quiet = false } = {}) {
  const same = Array.from(els.toasts.children).find((t) => t.dataset.msg === message);
  if (same) {
    same._restart();
    return;
  }
  if (quiet) {
    // A repeating background error is shown once, not every two seconds.
    const last = quietToastTimes.get(message);
    if (last && Date.now() - last < 30000) return;
    quietToastTimes.set(message, Date.now());
  }
  const isError = kind === 'error';
  const close = h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Dismiss message' }, icon('close'));
  const el = h('div', {
    class: `toast${isError ? ' toast--error' : ''}`,
    role: isError ? 'alert' : 'status',
    dataset: { msg: message },
  }, icon(isError ? 'alert' : 'check'), h('p', null, message), close);

  let timer = null;
  const dismiss = () => {
    clearTimeout(timer);
    const hadFocus = el.contains(document.activeElement);
    el.remove();
    if (hadFocus) (state.detailId !== null ? els.drawerPanel : els.main).focus({ preventScroll: true });
  };
  const start = () => {
    clearTimeout(timer);
    timer = setTimeout(dismiss, isError ? 10000 : 5000);
  };
  el._restart = start;
  el.addEventListener('mouseenter', () => clearTimeout(timer));
  el.addEventListener('mouseleave', start);
  el.addEventListener('focusin', () => clearTimeout(timer));
  el.addEventListener('focusout', start);
  close.addEventListener('click', dismiss);
  els.toasts.append(el);
  start();
}

/* ------------------------------------------------------------------ */
/* Small UI utilities                                                  */
/* ------------------------------------------------------------------ */

/** Runs fn while the button is marked busy. Keeps focus on the button. */
async function withBusy(btn, busyText, fn) {
  if (btn.getAttribute('aria-disabled') === 'true') return undefined;
  const label = btn.querySelector('span:not(.sr-only)') || btn;
  const before = label.textContent;
  btn.setAttribute('aria-disabled', 'true');
  if (busyText) label.textContent = busyText;
  try {
    return await fn();
  } finally {
    btn.removeAttribute('aria-disabled');
    if (busyText) label.textContent = before;
  }
}

function showFormError(el, message) {
  if (!message) {
    el.hidden = true;
    el.textContent = '';
    return;
  }
  fill(el, icon('alert'), h('span', null, message));
  el.hidden = false;
}

function setResult(el, kind, message) {
  el.className = el.className.replace(/\s*is-(ok|bad|busy|note)/g, '');
  if (!message) {
    el.textContent = '';
    return;
  }
  el.classList.add(`is-${kind}`);
  const mark = kind === 'ok' ? icon('check') : kind === 'bad' ? icon('alert') : null;
  fill(el, mark, h('span', null, message));
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Older or locked-down browsers: fall back to a hidden text box.
    const box = h('textarea', { 'aria-hidden': 'true', tabindex: '-1', class: 'sr-only' });
    box.value = text;
    document.body.append(box);
    const prev = document.activeElement;
    box.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    box.remove();
    if (prev && prev.focus) prev.focus({ preventScroll: true });
    return ok;
  }
}

function copyButton(value, what, focusKey) {
  return h('button', {
    type: 'button', class: 'icon-btn', title: `Copy ${what}`,
    'aria-label': `Copy ${what} ${value}`,
    dataset: { copy: value, fk: focusKey },
  }, icon('copy'));
}

async function onCopyClick(btn) {
  const ok = await copyText(btn.dataset.copy);
  if (!ok) {
    toast('Could not copy. Select the text and copy it yourself.', 'error');
    return;
  }
  const use = btn.querySelector('use');
  if (use) use.setAttribute('href', '#i-check');
  btn.classList.add('is-done');
  announce('Copied');
  setTimeout(() => {
    if (use) use.setAttribute('href', '#i-copy');
    btn.classList.remove('is-done');
  }, 1600);
}

function emptyState(el, { title, text, actions = [], boxed = false, level = 'h2' }) {
  // Unchanged message: leave it alone so a focused button inside keeps focus.
  const sig = JSON.stringify([title, text, boxed, level, actions.map((a) => a.textContent)]);
  if (!el.hidden && el._sig === sig) return;
  el._sig = sig;
  el.className = `empty${boxed ? ' empty--boxed' : ''}`;
  fill(el,
    h(level, null, title),
    text ? h('p', null, text) : null,
    actions.length ? h('div', { class: 'empty-actions' }, actions) : null);
  el.hidden = false;
}

function loadingState(el, text) {
  el._sig = null;
  el.className = 'empty is-loading';
  fill(el, h('span', { class: 'spinner', 'aria-hidden': 'true' }), h('p', null, text));
  el.hidden = false;
}

function actionButton(label, onClick, extraClass = '') {
  const btn = h('button', { type: 'button', class: `btn ${extraClass}`.trim() }, label);
  btn.addEventListener('click', onClick);
  return btn;
}

function actionLink(label, href, extraClass = '') {
  return h('a', { class: `btn ${extraClass}`.trim(), href }, label);
}

/* ------------------------------------------------------------------ */
/* Routing (the view and its filters live in the URL hash)             */
/* ------------------------------------------------------------------ */

function parseHash(hash) {
  const raw = String(hash || '').replace(/^#\/?/, '');
  const cut = raw.indexOf('?');
  const path = cut === -1 ? raw : raw.slice(0, cut);
  const params = new URLSearchParams(cut === -1 ? '' : raw.slice(cut + 1));
  const view = VIEWS.includes(path) ? path : 'find';
  const filters = { ...DEFAULT_FILTERS };
  let lead = null;
  if (view === 'leads') {
    filters.q = (params.get('q') || '').trim();
    if (STATUSES.includes(params.get('status'))) filters.status = params.get('status');
    if (['40', '60', '80'].includes(params.get('min_score'))) filters.min_score = params.get('min_score');
    if (params.get('has_email') === 'true') filters.has_email = 'true';
    if (['true', 'false'].includes(params.get('has_website'))) filters.has_website = params.get('has_website');
    if (SORTS.includes(params.get('sort'))) filters.sort = params.get('sort');
    if (/^\d+$/.test(params.get('search_id') || '')) filters.search_id = params.get('search_id');
    if (/^\d+$/.test(params.get('lead') || '')) lead = Number(params.get('lead'));
  }
  return { view, filters, lead };
}

function filterParams(filters) {
  const params = new URLSearchParams();
  for (const key of FILTER_KEYS) {
    const value = filters[key];
    if (value && !(key === 'sort' && value === 'score')) params.set(key, value);
  }
  return params;
}

function buildHash(r) {
  if (r.view !== 'leads') return `#/${r.view}`;
  const params = filterParams(r.filters);
  if (r.lead !== null && r.lead !== undefined) params.set('lead', String(r.lead));
  const qs = params.toString();
  return `#/leads${qs ? `?${qs}` : ''}`;
}

function go(next, { replace = false } = {}) {
  const hash = buildHash(next);
  if (hash === location.hash) {
    applyRoute();
  } else if (replace) {
    history.replaceState(null, '', hash);
    applyRoute();
  } else {
    location.hash = hash; // fires hashchange → applyRoute
  }
}

function sameFilters(a, b) {
  return FILTER_KEYS.every((k) => a[k] === b[k]);
}

function applyRoute() {
  const prev = route;
  route = parseHash(location.hash);

  if (prev.view !== route.view) {
    for (const view of VIEWS) $(`view-${view}`).hidden = view !== route.view;
    for (const link of els.navLinks) {
      if (link.dataset.nav === route.view) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    document.title = `${VIEW_TITLES[route.view]} – Client Finder`;
    document.body.dataset.view = route.view;
    if (prev.view !== null) {
      window.scrollTo(0, 0);
      $(`${route.view}-title`).focus({ preventScroll: true });
    }
  }

  if (route.view === 'leads') {
    syncFilterControls();
    renderSearchChip();
    els.exportLink.href = `/api/leads/export.csv${leadQuery(route.filters)}`;
    // The Leads tab remembers the filters you last used.
    els.navLeads.setAttribute('href', buildHash({ ...route, lead: null }));
  }

  if (prev.view !== route.view) enterView(route.view);
  else if (route.view === 'leads' && !sameFilters(prev.filters, route.filters)) loadLeads();

  syncDrawer();
  schedulePoll();
}

function enterView(view) {
  if (view === 'find') {
    if (!state.settings) loadSettings({ quiet: true });
    else renderKeyNotice();
    renderSearches();
    loadSearches();
  } else if (view === 'leads') {
    renderLeads();
    loadStats();
    if (!state.searchesLoaded) loadSearches({ quiet: true });
    loadLeads();
  } else if (view === 'settings') {
    openSettings();
  }
}

function refreshCurrentView() {
  if (route.view) enterView(route.view);
  if (state.detailId !== null) loadDetail(state.detailId);
}

/* ------------------------------------------------------------------ */
/* Polling: only while something is in progress (see docs/API.md)      */
/* ------------------------------------------------------------------ */

let pollTimer = null;
let pollBusy = false;

function needsPoll() {
  if (state.searches.some(isSearchActive)) return true;
  if (route.view === 'leads' && state.leads.some(isAuditing)) return true;
  if (state.detailId !== null && isAuditing(state.detail)) return true;
  return false;
}

function schedulePoll() {
  if (pollTimer !== null || pollBusy) return;
  if (!needsPoll()) return;
  pollTimer = setTimeout(pollTick, state.offline ? POLL_OFFLINE_MS : POLL_MS);
}

async function pollTick() {
  pollTimer = null;
  pollBusy = true;
  try {
    const hadActiveSearch = state.searches.some(isSearchActive);
    const onLeads = route.view === 'leads';
    const jobs = [];
    if (hadActiveSearch) jobs.push(loadSearches({ quiet: true }));
    if (onLeads && (hadActiveSearch || state.leads.some(isAuditing))) {
      jobs.push(loadLeads({ quiet: true }), loadStats({ quiet: true }));
    }
    if (state.detailId !== null && isAuditing(state.detail)) {
      jobs.push(loadDetail(state.detailId, { quiet: true }));
    }
    await Promise.all(jobs);
    // A search just finished: pick up its last results once more.
    if (hadActiveSearch && !state.searches.some(isSearchActive) && route.view === 'leads') {
      await Promise.all([loadLeads({ quiet: true }), loadStats({ quiet: true })]);
    }
  } finally {
    pollBusy = false;
    schedulePoll();
  }
}

/* ------------------------------------------------------------------ */
/* Loaders (never throw; they report their own errors)                 */
/* ------------------------------------------------------------------ */

async function loadSettings({ quiet = false } = {}) {
  try {
    state.settings = await api('/api/settings');
    renderKeyNotice();
    return true;
  } catch (err) {
    reportError(err, { quiet });
    return false;
  }
}

// Each list has a ticket counter: an answer is only used if no newer request
// (or local change) has happened since it was asked for.
let searchesRequest = 0;

async function loadSearches({ quiet = false } = {}) {
  const ticket = ++searchesRequest;
  try {
    const data = await api('/api/searches');
    if (ticket !== searchesRequest) return;
    state.searches = Array.isArray(data) ? data : [];
    state.searchesLoaded = true;
    state.searchesFailed = false;
  } catch (err) {
    if (ticket !== searchesRequest) return;
    state.searchesFailed = true;
    reportError(err, { quiet });
  }
  renderSearches();
  if (route.view === 'leads') {
    renderSearchChip();
    if (state.leadsLoaded && state.leads.length === 0) renderLeads();
  }
  schedulePoll();
}

async function loadStats({ quiet = false } = {}) {
  try {
    state.stats = await api('/api/stats');
    renderStats();
  } catch (err) {
    reportError(err, { quiet });
  }
}

function leadQuery(filters) {
  const qs = filterParams({ ...filters, sort: '' }).toString();
  const sort = `sort=${encodeURIComponent(filters.sort || 'score')}`;
  return `?${qs ? `${qs}&` : ''}${sort}`;
}

let leadsRequest = 0;
let leadsInFlight = 0;

async function loadLeads({ quiet = false } = {}) {
  const ticket = ++leadsRequest;
  leadsInFlight += 1;
  try {
    const data = await api(`/api/leads${leadQuery(route.filters)}`);
    if (ticket !== leadsRequest) return; // a newer request replaced this one
    state.leads = Array.isArray(data) ? data : [];
    state.leadsLoaded = true;
    state.leadsFailed = false;
  } catch (err) {
    if (ticket !== leadsRequest) return;
    state.leadsFailed = true;
    reportError(err, { quiet });
  } finally {
    leadsInFlight -= 1;
  }
  renderLeads();
  schedulePoll();
}

let detailRequest = 0;
let detailInFlight = 0;

async function loadDetail(id, { quiet = false } = {}) {
  const ticket = ++detailRequest;
  detailInFlight += 1;
  try {
    let lead;
    try {
      lead = await api(`/api/leads/${id}`);
    } finally {
      detailInFlight -= 1;
    }
    if (state.detailId !== id || ticket !== detailRequest) return;
    state.detail = lead;
    state.detailFailed = false;
    renderDrawer();
  } catch (err) {
    if (state.detailId !== id || ticket !== detailRequest) return;
    reportError(err, { quiet });
    if (err.status === 404 || !state.detail) {
      closeDrawer();
      return;
    }
    state.detailFailed = true;
    renderDrawer();
  }
  schedulePoll();
}

/* ------------------------------------------------------------------ */
/* Find                                                                */
/* ------------------------------------------------------------------ */

function renderKeyNotice() {
  els.keyNotice.hidden = !(state.settings && state.settings.has_google_key === false);
}

function searchItem(s) {
  const title = searchTitle(s);
  const found = Number(s.found) || 0;
  const audited = Number(s.audited) || 0;
  const info = h('div', { class: 'search-info' });

  info.append(h('p', { class: 'search-title' },
    h('strong', null, s.query), s.location ? ` in ${s.location}` : null));

  let word = String(s.status || '');
  let extra = '';
  let cls = '';
  let busy = false;
  if (s.status === 'queued') { word = 'Queued'; extra = 'Waiting to start'; busy = true; }
  else if (s.status === 'finding') { word = 'Finding businesses'; extra = found ? `${found.toLocaleString()} so far` : ''; busy = true; }
  else if (s.status === 'auditing') { word = `Auditing websites ${audited.toLocaleString()} of ${found.toLocaleString()}`; busy = true; }
  else if (s.status === 'done') { word = 'Done'; extra = `${plural(found, 'business', 'businesses')} found`; cls = ' is-done'; }
  else if (s.status === 'error') { word = 'Failed'; extra = found ? `${plural(found, 'business', 'businesses')} found before it stopped` : ''; cls = ' is-error'; }

  info.append(h('p', { class: `search-state${cls}` },
    busy ? h('span', { class: 'spinner', 'aria-hidden': 'true' }) : null,
    h('span', { class: 'state-word' }, word),
    extra ? h('span', { class: 'state-extra' }, extra) : null,
    h('span', { class: 'search-when' }, fmtWhen(s.created_at))));

  if (s.status === 'auditing') {
    const bar = h('progress', { class: 'bar', max: Math.max(found, 1), 'aria-label': `Websites audited for ${title}` });
    bar.value = Math.min(audited, Math.max(found, 1));
    info.append(bar);
  }
  if (s.status === 'error') {
    info.append(h('p', { class: 'search-error' }, s.error || 'This search stopped before it finished.'));
  }

  const showLink = found > 0 || isSearchActive(s);
  return h('li', null, info, showLink
    ? h('a', {
      class: 'btn btn--small search-action', dataset: { fk: 'view' },
      href: buildHash({ view: 'leads', filters: { ...DEFAULT_FILTERS, search_id: String(s.id) }, lead: null }),
      'aria-label': `View leads for ${title}`,
    }, 'View leads')
    : null);
}

function renderSearches() {
  const list = state.searches;
  syncList(els.searchList, list, (s) => s.id, searchItem);
  els.searchList.hidden = list.length === 0;
  if (list.length > 0) {
    els.searchEmpty.hidden = true;
  } else if (state.searchesFailed) {
    emptyState(els.searchEmpty, {
      title: 'Recent searches did not load', level: 'h3',
      actions: [actionButton('Try again', () => loadSearches())],
    });
  } else if (!state.searchesLoaded) {
    loadingState(els.searchEmpty, 'Loading your searches…');
  } else {
    emptyState(els.searchEmpty, {
      title: 'No searches yet', level: 'h3',
      text: 'Start one above. Each search is kept here so you can come back to its leads.',
    });
  }
}

async function submitSearch(event) {
  event.preventDefault();
  const query = els.sQuery.value.trim();
  els.sQuery.closest('.sb-field').classList.toggle('is-invalid', !query);
  if (!query) {
    showFormError(els.searchError, 'Type a kind of business first, for example “dentists”.');
    els.sQuery.focus();
    return;
  }
  const body = { query, max_results: Number(els.sMax.value) || 20 };
  const location = els.sLocation.value.trim();
  if (location) body.location = location;

  await withBusy(els.sSubmit, 'Starting…', async () => {
    try {
      const search = await api('/api/searches', { method: 'POST', body });
      searchesRequest += 1; // ignore any list answer asked for before this search existed
      showFormError(els.searchError, '');
      state.searches = [search, ...state.searches.filter((s) => s.id !== search.id)];
      state.searchesLoaded = true;
      state.searchesFailed = false;
      renderSearches();
      els.sQuery.value = '';
      announce(`Search started for ${searchTitle(search)}. Progress is shown under Recent searches.`);
      schedulePoll();
    } catch (err) {
      showFormError(els.searchError, err.message);
    }
  });
}

async function submitManual(event) {
  event.preventDefault();
  const website = els.mWebsite.value.trim();
  if (!website) {
    showFormError(els.manualError, 'Paste a website address first, for example “example.com”.');
    els.mWebsite.focus();
    return;
  }
  const body = { website };
  const name = els.mName.value.trim();
  if (name) body.name = name;

  await withBusy(els.mSubmit, 'Adding…', async () => {
    try {
      const lead = await api('/api/leads', { method: 'POST', body });
      showFormError(els.manualError, '');
      els.mWebsite.value = '';
      els.mName.value = '';
      state.seedLead = lead;
      drawerOpener = null;
      go({ view: 'leads', filters: { ...DEFAULT_FILTERS }, lead: lead.id });
    } catch (err) {
      showFormError(els.manualError, err.message);
    }
  });
}

/* ------------------------------------------------------------------ */
/* Leads                                                               */
/* ------------------------------------------------------------------ */

function renderStats() {
  const s = state.stats;
  if (!s) return;
  const num = (v) => (typeof v === 'number' ? v.toLocaleString() : '–');
  els.statTotal.textContent = num(s.total);
  els.statEmail.textContent = num(s.with_email);
  els.statPhone.textContent = num(s.with_phone);
  els.statNosite.textContent = num(s.no_website);
  els.statHigh.textContent = num(s.high_opportunity);

  const by = s.by_status || {};
  for (const option of els.fStatus.options) {
    if (!option.value) continue;
    const count = by[option.value];
    option.textContent = typeof count === 'number'
      ? `${STATUS_LABELS[option.value]} (${count.toLocaleString()})`
      : STATUS_LABELS[option.value];
  }
}

function scoreBand(score) {
  if (typeof score !== 'number') return 'none';
  if (score >= 80) return 'top';
  if (score >= 60) return 'high';
  if (score >= 40) return 'mid';
  return 'low';
}

function scoreEl(lead, big = false) {
  const score = lead.opportunity_score;
  const has = typeof score === 'number';
  const meter = h('span', { class: 'meter', 'aria-hidden': 'true' });
  meter.style.setProperty('--v', has ? String(Math.max(0, Math.min(100, score))) : '0');
  return h('div', { class: `score${big ? ' score--big' : ''}`, dataset: { band: scoreBand(score) } },
    has
      ? [h('span', { class: 'score-num' }, String(Math.round(score))), srOnly(' out of 100')]
      : [h('span', { class: 'score-num', 'aria-hidden': 'true' }, '–'), srOnly(isAuditing(lead) ? 'Not scored yet, still auditing' : 'Not scored')],
    meter);
}

function auditBadge(lead) {
  switch (lead.audit_status) {
    case 'pending':
    case 'running':
      return h('span', { class: 'state state--busy' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Auditing…');
    case 'no_website':
      return h('span', { class: 'state state--nosite' }, 'No website');
    case 'failed':
      return h('span', { class: 'state state--failed' }, 'Could not read site');
    case 'blocked':
      return h('span', { class: 'state state--blocked' }, 'Site blocks bots');
    default:
      return null;
  }
}

function statusSelect(lead, id) {
  const current = STATUSES.includes(lead.status) ? lead.status : 'new';
  return h('select', {
    class: 'status-select', id,
    'aria-label': `Status for ${leadName(lead)}`,
    dataset: { status: current, rowStatus: String(lead.id), fk: 'status' },
    value: current,
  }, STATUSES.map((s) => h('option', { value: s }, STATUS_LABELS[s])));
}

function leadRow(lead) {
  const name = leadName(lead);
  const issues = Array.isArray(lead.top_issues) ? lead.top_issues.filter(Boolean) : [];
  const emails = Array.isArray(lead.emails) ? lead.emails.filter(Boolean) : [];

  const found = h('td', null, auditBadge(lead));
  if (issues.length) {
    found.append(h('ul', { class: 'issue-list' }, issues.slice(0, 3).map((t) => h('li', null, t))));
    if (issues.length > 3) found.append(h('p', { class: 'contact-more' }, `and ${issues.length - 3} more`));
  } else if (lead.audit_status === 'done') {
    found.append(h('span', { class: 'none' }, 'No problems found'));
  }

  const contact = h('td', null);
  if (lead.phone) {
    const href = telHref(lead.phone);
    contact.append(h('div', { class: 'contact-line' },
      href ? h('a', { href }, lead.phone) : h('span', null, lead.phone),
      copyButton(lead.phone, 'phone number', 'copy-phone')));
  }
  if (emails.length) {
    const href = mailHref(emails[0]);
    contact.append(h('div', { class: 'contact-line' },
      href ? h('a', { href, title: emails[0] }, emails[0]) : h('span', null, emails[0]),
      copyButton(emails[0], 'email address', 'copy-email')));
    if (emails.length > 1) {
      contact.append(h('p', { class: 'contact-more' }, `and ${plural(emails.length - 1, 'more email', 'more emails')}`));
    }
  }
  if (!lead.phone && !emails.length) {
    contact.append(h('span', { class: 'none' }, isAuditing(lead) ? 'Looking…' : 'None found'));
  }

  const rating = h('td', null);
  if (typeof lead.rating === 'number') {
    rating.append(h('span', { class: 'rating' }, icon('star'), lead.rating.toFixed(1), srOnly(' out of 5')));
    if (typeof lead.reviews === 'number') {
      rating.append(h('span', { class: 'rating-count' }, plural(lead.reviews, 'review', 'reviews')));
    }
  } else {
    rating.append(h('span', { class: 'none', 'aria-hidden': 'true' }, '–'), srOnly('No rating'));
  }

  return h('tr', { dataset: { id: String(lead.id) } },
    h('td', null, scoreEl(lead)),
    h('td', null,
      h('button', { type: 'button', class: 'biz-name', title: name, dataset: { open: String(lead.id), fk: 'open' } }, name),
      lead.category ? h('p', { class: 'biz-cat' }, lead.category) : null),
    found,
    contact,
    rating,
    h('td', null, statusSelect(lead)));
}

function hasNarrowingFilters(f) {
  return !!(f.q || f.status || f.min_score || f.has_email || f.has_website);
}

function currentSearch() {
  const id = route.filters.search_id;
  return id ? state.searches.find((s) => String(s.id) === id) || null : null;
}

function markOpenRow() {
  for (const tr of els.leadRows.children) {
    tr.classList.toggle('is-open', state.detailId !== null && tr.dataset.id === String(state.detailId));
  }
}

function renderLeads() {
  const leads = state.leads;
  const f = route.filters;
  const hasRows = state.leadsLoaded && leads.length > 0;

  if (hasRows) syncList(els.leadRows, leads, (l) => l.id, leadRow);
  else els.leadRows.replaceChildren();
  els.leadTableWrap.hidden = !hasRows;
  els.fClear.hidden = !hasNarrowingFilters(f);
  markOpenRow();

  if (hasRows) {
    els.leadsEmpty.hidden = true;
    const scoped = hasNarrowingFilters(f) || f.search_id;
    els.leadCount.textContent = `${plural(leads.length, 'lead', 'leads')}${scoped ? ' shown' : ''}`;
    return;
  }

  els.leadCount.textContent = '';
  if (!state.leadsLoaded) {
    if (state.leadsFailed) {
      emptyState(els.leadsEmpty, {
        title: 'Leads did not load', boxed: true,
        actions: [actionButton('Try again', () => loadLeads(), 'btn--primary')],
      });
    } else {
      loadingState(els.leadsEmpty, 'Loading leads…');
    }
    return;
  }

  const search = currentSearch();
  if (hasNarrowingFilters(f)) {
    emptyState(els.leadsEmpty, {
      title: 'No leads match these filters', boxed: true,
      text: 'Loosen a filter, or clear them to see everything again.',
      actions: [actionButton('Clear filters', clearFilters, 'btn--primary')],
    });
  } else if (f.search_id && search && isSearchActive(search)) {
    emptyState(els.leadsEmpty, {
      title: 'Still looking', boxed: true,
      text: 'Businesses from this search show up here as soon as they are found. No need to refresh.',
    });
  } else if (f.search_id && search && search.status === 'error') {
    emptyState(els.leadsEmpty, {
      title: 'This search failed before it found anyone', boxed: true,
      text: search.error || '',
      actions: [actionLink('Go to Find', '#/find', 'btn--primary'), actionButton('Show all leads', clearSearchScope)],
    });
  } else if (f.search_id) {
    emptyState(els.leadsEmpty, {
      title: 'This search found no businesses', boxed: true,
      text: 'Try a broader kind of business, or a bigger place.',
      actions: [actionLink('Start a new search', '#/find', 'btn--primary'), actionButton('Show all leads', clearSearchScope)],
    });
  } else {
    emptyState(els.leadsEmpty, {
      title: 'No leads yet', boxed: true,
      text: 'Search for a kind of business in a place, and every business Client Finder finds is listed here with a score.',
      actions: [actionLink('Find leads', '#/find', 'btn--primary')],
    });
  }
}

function renderSearchChip() {
  const id = route.filters.search_id;
  els.searchChip.hidden = !id;
  if (!id) return;
  const search = currentSearch();
  els.searchChipText.textContent = search ? `From the search: ${searchTitle(search)}` : `From search number ${id}`;
  els.searchChipText.title = els.searchChipText.textContent;
}

function syncFilterControls() {
  const f = route.filters;
  if (els.fQ.value.trim() !== f.q) els.fQ.value = f.q;
  els.fStatus.value = f.status;
  els.fScore.value = f.min_score;
  els.fWebsite.value = f.has_website;
  els.fSort.value = f.sort;
  els.fEmail.checked = f.has_email === 'true';
}

function readFilterControls() {
  return {
    q: els.fQ.value.trim(),
    status: els.fStatus.value,
    min_score: els.fScore.value,
    has_email: els.fEmail.checked ? 'true' : '',
    has_website: els.fWebsite.value,
    sort: els.fSort.value || 'score',
    search_id: route.filters.search_id,
  };
}

let filterTimer = null;

function applyFilterControls() {
  clearTimeout(filterTimer);
  if (route.view !== 'leads') return;
  go({ view: 'leads', filters: readFilterControls(), lead: route.lead }, { replace: true });
}

function clearFilters() {
  go({
    view: 'leads', lead: null,
    filters: { ...DEFAULT_FILTERS, sort: route.filters.sort, search_id: route.filters.search_id },
  }, { replace: true });
  els.fQ.focus();
}

function clearSearchScope() {
  go({ view: 'leads', filters: { ...route.filters, search_id: '' }, lead: null }, { replace: true });
  els.fQ.focus();
}

async function exportCsv(event) {
  event.preventDefault();
  await withBusy(els.exportLink, 'Exporting…', async () => {
    try {
      const res = await request(`/api/leads/export.csv${leadQuery(route.filters)}`);
      const blob = await res.blob();
      const header = res.headers.get('Content-Disposition') || '';
      const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(header);
      let filename = 'client-finder-leads.csv';
      if (match) {
        try { filename = decodeURIComponent(match[1]); } catch { filename = match[1]; }
      }
      const url = URL.createObjectURL(blob);
      const link = h('a', { href: url, download: filename, hidden: true });
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      toast(`Exported ${filename}. Look in your Downloads folder.`);
    } catch (err) {
      reportError(err);
    }
  });
}

/** Applies a lead the server just sent back to both the table and the drawer. */
function mergeLead(updated) {
  if (!updated || updated.id === undefined) return;
  const { audit, ...listFields } = updated;
  const index = state.leads.findIndex((l) => l.id === updated.id);
  if (index !== -1) state.leads[index] = { ...state.leads[index], ...listFields };
  if (state.detailId === updated.id) {
    state.detail = { ...(state.detail || {}), ...listFields };
    if (audit !== undefined) state.detail.audit = audit;
    renderDrawer();
    // A refresh that was already on its way would bring back the old values.
    if (detailInFlight > 0) loadDetail(updated.id, { quiet: true });
  }
  if (route.view === 'leads') {
    renderLeads();
    if (leadsInFlight > 0) loadLeads({ quiet: true });
  }
}

async function changeStatus(id, status, select) {
  const before = (state.leads.find((l) => l.id === id) || state.detail || {}).status;
  select.dataset.status = status;
  try {
    const updated = await api(`/api/leads/${id}`, { method: 'PATCH', body: { status } });
    mergeLead(updated);
    select.classList.add('is-saved');
    setTimeout(() => select.classList.remove('is-saved'), 1400);
    announce(`Status saved: ${STATUS_LABELS[status] || status}`);
    loadStats({ quiet: true });
    // With a status filter on, the lead may no longer belong in this list.
    if (route.view === 'leads' && route.filters.status && route.filters.status !== status) {
      const name = leadName(updated);
      await loadLeads({ quiet: true });
      toast(`${name} moved to ${STATUS_LABELS[status] || status}. It no longer matches the status filter.`);
    }
  } catch (err) {
    if (before && select.isConnected) {
      select.value = before;
      select.dataset.status = before;
    }
    reportError(err);
  }
}

/* ------------------------------------------------------------------ */
/* Lead detail drawer                                                  */
/* ------------------------------------------------------------------ */

let drawerOpener = null;
let drawerSig = '';
const notes = { saved: null, saving: false, again: false };

function openLead(id, opener) {
  drawerOpener = opener || document.activeElement;
  go({ view: 'leads', filters: route.view === 'leads' ? route.filters : { ...DEFAULT_FILTERS }, lead: id },
    { replace: route.view === 'leads' });
}

function closeDrawer() {
  if (route.view === 'leads' && route.lead !== null) go({ ...route, lead: null }, { replace: true });
  else hideDrawer();
}

function syncDrawer() {
  const id = route.view === 'leads' ? route.lead : null;
  if (id === state.detailId) return;
  if (id === null) hideDrawer();
  else showDrawer(id);
}

function setBackgroundInert(flag) {
  for (const el of [els.topbar, els.main, els.offlineBanner]) el.inert = flag;
}

function showDrawer(id) {
  const wasOpen = state.detailId !== null;
  if (wasOpen) saveNotes(false);
  else if (!drawerOpener) drawerOpener = document.activeElement;

  state.detailId = id;
  state.detailFailed = false;
  const known = state.leads.find((l) => l.id === id) || (state.seedLead && state.seedLead.id === id ? state.seedLead : null);
  state.seedLead = null;
  state.detail = known ? { ...known } : null; // `audit` stays undefined until the full lead arrives
  drawerSig = '';
  notes.saved = null;
  setResult(els.dNotesMsg, '', '');
  showDeleteConfirm(false);

  els.drawer.hidden = false;
  document.body.classList.add('drawer-open');
  setBackgroundInert(true);
  renderDrawer();
  els.drawerScroll.scrollTop = 0;
  els.drawerPanel.focus({ preventScroll: true });
  markOpenRow();
  loadDetail(id);
}

function hideDrawer() {
  if (state.detailId === null) return;
  saveNotes(false); // do not lose notes typed just before closing
  const closedId = state.detailId;
  state.detailId = null;
  state.detail = null;
  els.drawer.hidden = true;
  document.body.classList.remove('drawer-open');
  setBackgroundInert(false);
  markOpenRow();

  const opener = drawerOpener;
  drawerOpener = null;
  const rowButton = els.leadRows.querySelector(`[data-open="${closedId}"]`);
  const target = (opener && opener.isConnected && opener.offsetParent !== null && opener !== document.body && opener)
    || rowButton || $(`${route.view}-title`);
  if (target) target.focus({ preventScroll: true });
}

/** Runs fn (which rebuilds parts of the drawer) without dropping keyboard focus. */
function keepDrawerFocus(fn) {
  const active = document.activeElement;
  const inside = els.drawerPanel.contains(active);
  const key = inside ? active.dataset.fk : null;
  fn();
  if (inside && !els.drawerPanel.contains(document.activeElement)) {
    const again = key ? els.drawerPanel.querySelector(`[data-fk="${CSS.escape(key)}"]`) : null;
    (again || els.drawerPanel).focus({ preventScroll: true });
  }
}

function scoreWords(lead) {
  const band = scoreBand(lead.opportunity_score);
  if (band === 'none') return isAuditing(lead) ? 'Working out the score…' : 'Not scored';
  return {
    top: 'Very strong opportunity', high: 'Strong opportunity',
    mid: 'Fair opportunity', low: 'Low opportunity',
  }[band];
}

function serviceTag(service) {
  const key = String(service || '').toLowerCase();
  return h('span', { class: 'tag' }, SERVICE_LABELS[key] || String(service || ''));
}

function externalLink(url, label, focusKey) {
  return h('a', { href: url, target: '_blank', rel: 'noopener noreferrer', dataset: { fk: focusKey } },
    label, icon('external'), srOnly(' (opens in a new tab)'));
}

function drawerFacts(lead) {
  const rows = [];
  if (lead.address) rows.push(h('p', { class: 'fact' }, icon('pin'), h('span', null, lead.address)));

  const site = safeUrl(lead.website);
  if (site) rows.push(h('p', { class: 'fact' }, icon('globe'), externalLink(site, prettyUrl(lead.website), 'website')));
  else if (lead.website) rows.push(h('p', { class: 'fact' }, icon('globe'), h('span', null, lead.website)));
  else rows.push(h('p', { class: 'fact' }, icon('globe'), h('span', { class: 'none' }, 'No website')));

  const maps = safeUrl(lead.maps_url);
  if (maps) rows.push(h('p', { class: 'fact' }, icon('map'), externalLink(maps, 'Open in Google Maps', 'maps')));

  if (typeof lead.rating === 'number') {
    const reviews = typeof lead.reviews === 'number' ? ` from ${plural(lead.reviews, 'review', 'reviews')}` : '';
    rows.push(h('p', { class: 'fact' }, icon('star'), h('span', null, `${lead.rating.toFixed(1)} out of 5${reviews}`)));
  }
  return rows;
}

function drawerContact(lead) {
  const audit = lead.audit || null;
  const seen = new Set();
  const phones = [lead.phone, ...((audit && audit.phones_found) || [])].filter((p) => {
    if (!p) return false;
    const key = String(p).replace(/\D/g, '') || String(p);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const emails = Array.from(new Set((Array.isArray(lead.emails) ? lead.emails : []).filter(Boolean)));
  const social = Object.entries((audit && audit.social) || {})
    .map(([name, url]) => [name, safeUrl(url)])
    .filter(([, url]) => url);

  const groups = [];
  if (phones.length) {
    groups.push(h('div', { class: 'contact-group' }, h('h4', null, phones.length > 1 ? 'Phone numbers' : 'Phone'),
      phones.map((p, i) => {
        const href = telHref(p);
        return h('div', { class: 'contact-line' }, icon('phone'),
          href ? h('a', { href, dataset: { fk: `phone-${i}` } }, p) : h('span', null, p),
          copyButton(p, 'phone number', `copy-phone-${i}`));
      })));
  }
  if (emails.length) {
    groups.push(h('div', { class: 'contact-group' }, h('h4', null, emails.length > 1 ? 'Email addresses' : 'Email'),
      emails.map((e, i) => {
        const href = mailHref(e);
        return h('div', { class: 'contact-line' }, icon('mail'),
          href ? h('a', { href, dataset: { fk: `email-${i}` } }, e) : h('span', null, e),
          copyButton(e, 'email address', `copy-email-${i}`));
      })));
  }
  if (social.length) {
    groups.push(h('div', { class: 'contact-group' }, h('h4', null, 'Social'),
      h('div', { class: 'social-list' }, social.map(([name, url]) => {
        const label = SOCIAL_LABELS[name.toLowerCase()] || name.charAt(0).toUpperCase() + name.slice(1);
        return externalLink(url, label, `social-${name}`);
      }))));
  }
  if (!groups.length) {
    groups.push(h('p', { class: 'none' }, isAuditing(lead)
      ? 'Nothing yet. The website is still being checked.'
      : 'No public phone number or email was found.'));
  }
  return groups;
}

function drawerAudit(lead) {
  const out = [];
  const audit = lead.audit;
  const badge = auditBadge(lead);
  const notesByState = {
    pending: 'Client Finder is checking this website now. Results appear here on their own.',
    running: 'Client Finder is checking this website now. Results appear here on their own.',
    no_website: 'No website is listed for this business, so there was nothing to check.',
    failed: 'The website did not load, so it could not be checked.',
    blocked: 'This website asks automated visitors to stay out, so Client Finder did not read it.',
  };
  if (badge) out.push(h('div', null, badge, h('p', { class: 'audit-note' }, notesByState[lead.audit_status])));

  if (state.detailFailed && audit === undefined) {
    out.push(h('p', { class: 'audit-error' }, 'The full details did not load.'),
      actionButton('Try again', () => loadDetail(state.detailId), 'btn--small'));
    return out;
  }
  if (audit === undefined) {
    out.push(h('p', { class: 'none' }, 'Loading the website check…'));
    return out;
  }
  if (!audit) {
    if (!badge) out.push(h('p', { class: 'none' }, 'This website has not been checked yet.'));
    return out;
  }

  if (audit.error) out.push(h('p', { class: 'audit-error' }, audit.error));

  const rows = [];
  const add = (label, value, bad = false) => rows.push(h('dt', null, label), h('dd', { class: bad ? 'is-bad' : null }, value));
  const siteWasRead = lead.audit_status !== 'no_website' && lead.audit_status !== 'blocked'
    && !(audit.error && typeof audit.load_ms !== 'number');

  if (typeof audit.load_ms === 'number') add('Server response time', fmtSeconds(audit.load_ms));
  if (typeof audit.page_kb === 'number') add('Page code size', fmtSize(audit.page_kb));
  if (siteWasRead && typeof audit.https === 'boolean') add('Secure connection (HTTPS)', audit.https ? 'Yes' : 'No', !audit.https);
  if (audit.platform) add('Built with', audit.platform);
  if (typeof audit.pagespeed_mobile === 'number') add('Google mobile speed score', `${Math.round(audit.pagespeed_mobile)} out of 100`);

  const pages = Array.isArray(audit.pages_checked) ? audit.pages_checked.filter(Boolean) : [];
  if (pages.length) {
    add('Pages checked', h('details', { class: 'pages' },
      h('summary', { dataset: { fk: 'pages' } }, plural(pages.length, 'page', 'pages')),
      h('ul', null, pages.map((p, i) => {
        const url = safeUrl(p);
        return h('li', null, url ? externalLink(url, prettyUrl(p), `page-${i}`) : p);
      }))));
  }
  const checked = fmtWhen(audit.checked_at);
  if (checked) add('Checked', checked);
  if (rows.length) out.push(h('dl', { class: 'audit-grid' }, rows));
  return out;
}

function drawerIssues(lead) {
  const issues = (lead.audit && Array.isArray(lead.audit.issues)) ? lead.audit.issues.filter(Boolean) : [];
  const known = new Set(SEVERITIES.map(([key]) => key));
  return SEVERITIES.map(([key, label]) => {
    const group = issues.filter((issue) => (known.has(issue.severity) ? issue.severity : 'low') === key);
    if (!group.length) return null;
    return h('div', { class: 'sev-group', dataset: { sev: key } },
      h('h4', null, label, h('span', { class: 'sev-count' }, String(group.length))),
      h('ul', null, group.map((issue) => h('li', { class: 'issue' },
        h('div', { class: 'item-head' },
          h('span', { class: 'item-title' }, issue.title || issue.code || 'Problem'),
          issue.service ? serviceTag(issue.service) : null),
        issue.detail ? h('p', { class: 'item-detail' }, issue.detail) : null))));
  }).filter(Boolean);
}

function drawerIdeas(lead) {
  const ideas = (lead.audit && Array.isArray(lead.audit.ideas)) ? lead.audit.ideas.filter(Boolean) : [];
  return ideas.map((idea) => h('li', { class: 'idea' },
    h('div', { class: 'item-head' },
      h('span', { class: 'item-title' }, idea.title || 'Idea'),
      idea.service ? serviceTag(idea.service) : null),
    idea.detail ? h('p', { class: 'item-detail' }, idea.detail) : null));
}

function renderDrawer() {
  const lead = state.detail;
  if (state.detailId === null) return;
  const sig = JSON.stringify([lead, state.detailFailed]);
  if (sig === drawerSig) return;
  drawerSig = sig;

  keepDrawerFocus(() => {
    if (!lead) {
      els.dName.textContent = 'Loading…';
      els.dCategory.textContent = '';
      fill(els.dScore);
      fill(els.dFacts);
      fill(els.dContact);
      fill(els.dAudit, h('p', { class: 'none' }, 'Loading this lead…'));
      els.dIssuesSection.hidden = true;
      els.dIdeasSection.hidden = true;
      els.dStatus.disabled = true;
      els.dNotes.disabled = true;
      els.dNotes.value = '';
      renderDrawerActions();
      return;
    }

    els.dName.textContent = leadName(lead);
    els.dCategory.textContent = lead.category || '';

    const services = Array.isArray(lead.services) ? lead.services.filter(Boolean) : [];
    fill(els.dScore, (() => {
      const block = scoreEl(lead, true);
      block.append(h('div', { class: 'score-side' },
        h('p', { class: 'score-words' }, scoreWords(lead),
          h('small', null, 'Opportunity score out of 100. Higher means more to sell.')),
        services.length ? h('p', { class: 'tag-row' }, 'Fits your services:', services.map(serviceTag)) : null));
      return block;
    })());
    fill(els.dFacts, drawerFacts(lead));
    fill(els.dContact, drawerContact(lead));

    els.dStatus.disabled = false;
    els.dNotes.disabled = false;
    const status = STATUSES.includes(lead.status) ? lead.status : 'new';
    if (document.activeElement !== els.dStatus) {
      els.dStatus.value = status;
      els.dStatus.dataset.status = status;
    }
    const serverNotes = typeof lead.notes === 'string' ? lead.notes : '';
    const dirty = notes.saved !== null && els.dNotes.value !== notes.saved;
    if (notes.saved === null || (!dirty && document.activeElement !== els.dNotes)) {
      els.dNotes.value = serverNotes;
      notes.saved = serverNotes;
    }

    fill(els.dAudit, drawerAudit(lead));

    const issueGroups = drawerIssues(lead);
    const auditDone = !!lead.audit && lead.audit_status === 'done';
    if (issueGroups.length) fill(els.dIssues, issueGroups);
    else if (auditDone) fill(els.dIssues, h('p', { class: 'none' }, 'No problems were found on the pages checked.'));
    els.dIssuesSection.hidden = !(issueGroups.length || auditDone);

    const ideas = drawerIdeas(lead);
    fill(els.dIdeas, ideas);
    els.dIdeasSection.hidden = ideas.length === 0;

    renderDrawerActions();
  });
}

function renderDrawerActions() {
  const lead = state.detail;
  const auditing = isAuditing(lead);
  els.dReauditLabel.textContent = auditing ? 'Auditing…' : 'Re-audit website';
  if (!lead || auditing) els.dReaudit.setAttribute('aria-disabled', 'true');
  else els.dReaudit.removeAttribute('aria-disabled');
  els.dDelete.disabled = !lead;
}

function showDeleteConfirm(flag) {
  els.dConfirm.hidden = !flag;
  els.dActions.hidden = flag;
}

async function saveNotes(explicit) {
  const id = state.detailId;
  if (id === null || notes.saved === null || !state.detail) return;
  const value = els.dNotes.value;
  if (value === notes.saved) {
    if (explicit) setResult(els.dNotesMsg, 'ok', 'Notes saved');
    return;
  }
  if (notes.saving) {
    notes.again = true;
    return;
  }
  notes.saving = true;
  setResult(els.dNotesMsg, 'busy', 'Saving…');
  try {
    const updated = await api(`/api/leads/${id}`, { method: 'PATCH', body: { notes: value } });
    if (state.detailId === id) {
      notes.saved = value;
      setResult(els.dNotesMsg, 'ok', 'Notes saved');
    }
    mergeLead(updated);
  } catch (err) {
    if (state.detailId === id) setResult(els.dNotesMsg, 'bad', `Notes not saved. ${err.message}`);
    else reportError(new ApiError(`Notes not saved. ${err.message}`, err.status));
  } finally {
    notes.saving = false;
    if (notes.again) {
      notes.again = false;
      if (state.detailId === id) saveNotes(false);
    }
  }
}

async function reauditLead() {
  const id = state.detailId;
  if (id === null || els.dReaudit.getAttribute('aria-disabled') === 'true') return;
  els.dReaudit.setAttribute('aria-disabled', 'true');
  try {
    const updated = await api(`/api/leads/${id}/reaudit`, { method: 'POST' });
    mergeLead(updated);
    announce('Audit started again. Results appear here on their own.');
  } catch (err) {
    reportError(err);
  } finally {
    if (state.detailId === id) renderDrawerActions();
    schedulePoll();
  }
}

async function deleteLead() {
  const id = state.detailId;
  const lead = state.detail;
  if (id === null) return;
  await withBusy(els.dConfirmYes, 'Deleting…', async () => {
    try {
      await api(`/api/leads/${id}`, { method: 'DELETE' });
      const name = lead ? leadName(lead) : 'Lead';
      notes.saved = null; // nothing left to save
      state.leads = state.leads.filter((l) => l.id !== id);
      drawerOpener = els.fQ;
      closeDrawer();
      renderLeads();
      loadStats({ quiet: true });
      loadLeads({ quiet: true });
      toast(`${name} deleted.`);
    } catch (err) {
      reportError(err);
    }
  });
}

function drawerFocusables() {
  const selector = 'a[href], button:not([disabled]), select:not([disabled]), textarea:not([disabled]), input:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';
  return Array.from(els.drawerPanel.querySelectorAll(selector))
    .filter((el) => el.offsetParent !== null || el.getClientRects().length > 0);
}

function onDrawerKeydown(event) {
  if (state.detailId === null) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    if (!els.dConfirm.hidden) {
      showDeleteConfirm(false);
      els.dDelete.focus();
    } else {
      closeDrawer();
    }
    return;
  }
  if (event.key !== 'Tab') return;
  if (els.toasts.contains(document.activeElement)) return; // let toasts be reached and left normally
  const items = drawerFocusables();
  if (!items.length) {
    event.preventDefault();
    els.drawerPanel.focus();
    return;
  }
  const first = items[0];
  const last = items[items.length - 1];
  const active = document.activeElement;
  if (!els.drawerPanel.contains(active) || active === els.drawerPanel) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
  } else if (event.shiftKey && active === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

/* ------------------------------------------------------------------ */
/* Settings                                                            */
/* ------------------------------------------------------------------ */

const settingsUi = { populated: false, dirty: false, snapshot: '' };

/** Compares the form with what was last loaded or saved. */
function markSettingsDirty() {
  if (!settingsUi.populated) return;
  const dirty = JSON.stringify(collectSettings()) !== settingsUi.snapshot;
  if (dirty) setResult(els.stSaveMsg, 'note', 'You have changes that are not saved yet.');
  else if (settingsUi.dirty) setResult(els.stSaveMsg, '', '');
  settingsUi.dirty = dirty;
  updateTestLabel();
}

function updateTestLabel() {
  els.stTestGoogle.textContent = els.stGoogleKey.value.trim() ? 'Save and test Google key' : 'Test Google key';
}

function serviceRow(value = '') {
  const input = h('input', { type: 'text', autocomplete: 'off', 'aria-label': 'Service', value });
  const remove = h('button', { type: 'button', class: 'btn btn--small', dataset: { remove: 'service' } }, 'Remove');
  const sync = () => remove.setAttribute('aria-label', `Remove service${input.value.trim() ? `: ${input.value.trim()}` : ''}`);
  input.addEventListener('input', sync);
  sync();
  return h('li', null, input, remove);
}

let portfolioSeq = 0;

function portfolioRow(item = {}) {
  const n = ++portfolioSeq;
  const field = (key, label, value, wide, extra = {}) => h('div', { class: `field${wide ? ' field--wide' : ''}` },
    h('label', { for: `pf-${key}-${n}` }, label),
    h('input', { type: 'text', id: `pf-${key}-${n}`, autocomplete: 'off', dataset: { pf: key }, value: value || '', ...extra }));
  return h('li', null,
    h('fieldset', { class: 'pf-fields' },
      h('legend', { class: 'sr-only' }, 'Project'),
      field('name', 'Project name', item.name, false),
      field('url', 'Link', item.url, false, { inputmode: 'url', spellcheck: 'false', placeholder: 'https://' }),
      field('description', 'What it is', item.description, true)),
    h('button', { type: 'button', class: 'btn btn--small pf-remove', dataset: { remove: 'portfolio' } }, 'Remove project'));
}

function keyState(el, helpEl, saved, purpose) {
  el.className = `keystate ${saved ? 'is-set' : 'is-unset'}`;
  fill(el, saved ? icon('check') : null, saved ? 'Saved' : 'Not set');
  helpEl.textContent = `${purpose}${saved
    ? 'Leave this empty to keep the saved key, or paste a new one to replace it.'
    : 'No key is saved yet. Paste yours here, then save.'}`;
}

function populateSettings(s) {
  els.stYourName.value = s.your_name || '';
  els.stBusinessName.value = s.business_name || '';
  els.stEmail.value = s.email || '';
  els.stWhatsapp.value = s.whatsapp || '';
  els.stPortfolioUrl.value = s.portfolio_url || '';
  fill(els.stServices, (Array.isArray(s.services) ? s.services : []).map((v) => serviceRow(String(v || ''))));
  fill(els.stPortfolio, (Array.isArray(s.portfolio) ? s.portfolio : []).map((p) => portfolioRow(p || {})));
  els.stGoogleKey.value = '';
  els.stClaudeKey.value = '';
  keyState(els.stGoogleState, $('st-google-help'), !!s.has_google_key, 'Needed to find businesses. ');
  keyState(els.stClaudeState, $('st-claude-help'), !!s.has_claude_key, '');
  els.stPagespeed.checked = !!s.use_pagespeed;
  settingsUi.populated = true;
  settingsUi.dirty = false;
  settingsUi.snapshot = JSON.stringify(collectSettings());
  updateTestLabel();
  els.settingsLoading.hidden = true;
  els.settingsForm.hidden = false;
}

async function openSettings() {
  if (settingsUi.populated && settingsUi.dirty) return; // keep edits that are not saved yet
  if (!settingsUi.populated) {
    els.settingsForm.hidden = true;
    loadingState(els.settingsLoading, 'Loading your settings…');
  }
  const ok = await loadSettings({ quiet: settingsUi.populated });
  if (ok && state.settings) {
    if (!settingsUi.dirty) {
      populateSettings(state.settings);
      setResult(els.stSaveMsg, '', '');
    }
  } else if (!settingsUi.populated) {
    emptyState(els.settingsLoading, {
      title: 'Settings did not load', boxed: true,
      text: 'Nothing has been changed. Check that the app is running, then try again.',
      actions: [actionButton('Try again', openSettings, 'btn--primary')],
    });
  }
}

function collectSettings() {
  const body = {
    your_name: els.stYourName.value.trim(),
    business_name: els.stBusinessName.value.trim(),
    email: els.stEmail.value.trim(),
    whatsapp: els.stWhatsapp.value.trim(),
    portfolio_url: els.stPortfolioUrl.value.trim(),
    services: Array.from(els.stServices.querySelectorAll('input')).map((i) => i.value.trim()).filter(Boolean),
    portfolio: Array.from(els.stPortfolio.children).map((li) => {
      const get = (key) => li.querySelector(`[data-pf="${key}"]`).value.trim();
      return { name: get('name'), description: get('description'), url: get('url') };
    }).filter((p) => p.name || p.description || p.url),
    use_pagespeed: els.stPagespeed.checked,
  };
  const google = els.stGoogleKey.value.trim();
  const claude = els.stClaudeKey.value.trim();
  if (google) body.google_api_key = google;
  if (claude) body.claude_api_key = claude;
  return body;
}

async function saveSettings() {
  if (!settingsUi.populated) return false;
  let saved = false;
  await withBusy(els.stSave, 'Saving…', async () => {
    setResult(els.stSaveMsg, 'busy', '');
    try {
      const updated = await api('/api/settings', { method: 'PUT', body: collectSettings() });
      state.settings = updated;
      const keepFocus = document.activeElement;
      populateSettings(updated);
      if (keepFocus && keepFocus.isConnected) keepFocus.focus({ preventScroll: true });
      else els.stSave.focus({ preventScroll: true }); // the edited row was rebuilt
      renderKeyNotice();
      setResult(els.stSaveMsg, 'ok', 'Settings saved');
      saved = true;
    } catch (err) {
      setResult(els.stSaveMsg, 'bad', `Not saved. ${err.message}`);
    }
  });
  return saved;
}

async function testGoogleKey() {
  await withBusy(els.stTestGoogle, null, async () => {
    setResult(els.stTestResult, 'busy', 'Testing…');
    if (els.stGoogleKey.value.trim()) {
      // A newly typed key has to be saved before the app can test it.
      const saved = await saveSettings();
      if (!saved) {
        setResult(els.stTestResult, 'bad', 'The key could not be saved, so it was not tested.');
        return;
      }
    }
    try {
      const result = await api('/api/settings/test-google', { method: 'POST' });
      const ok = !!(result && result.ok);
      setResult(els.stTestResult, ok ? 'ok' : 'bad',
        (result && result.message) || (ok ? 'The Google key works.' : 'The Google key did not work.'));
    } catch (err) {
      setResult(els.stTestResult, 'bad', err.message);
    }
  });
}

function removeListRow(button) {
  const li = button.closest('li');
  const list = li.parentElement;
  const next = li.nextElementSibling || li.previousElementSibling;
  li.remove();
  const target = next ? next.querySelector('input') : (list === els.stServices ? els.stAddService : els.stAddPortfolio);
  if (target) target.focus();
  markSettingsDirty();
}

/* ------------------------------------------------------------------ */
/* Wiring                                                              */
/* ------------------------------------------------------------------ */

function cacheElements() {
  Object.assign(els, {
    topbar: $('topbar'), main: $('main'), live: $('live'), toasts: $('toasts'),
    offlineBanner: $('offline-banner'), offlineRetry: $('offline-retry'),
    navLinks: Array.from(document.querySelectorAll('[data-nav]')),
    navLeads: document.querySelector('[data-nav="leads"]'),
    // Find
    keyNotice: $('key-notice'), searchForm: $('search-form'), sQuery: $('s-query'), sLocation: $('s-location'),
    sMax: $('s-max'), sSubmit: $('s-submit'), searchError: $('search-error'),
    searchList: $('search-list'), searchEmpty: $('search-empty'),
    manualForm: $('manual-form'), mWebsite: $('m-website'), mName: $('m-name'), mSubmit: $('m-submit'), manualError: $('manual-error'),
    // Leads
    statTotal: $('stat-total'), statEmail: $('stat-email'), statPhone: $('stat-phone'), statNosite: $('stat-nosite'), statHigh: $('stat-high'),
    filters: $('filters'), fQ: $('f-q'), fStatus: $('f-status'), fScore: $('f-score'), fWebsite: $('f-website'),
    fSort: $('f-sort'), fEmail: $('f-email'), fClear: $('f-clear'), exportLink: $('export-link'),
    leadCount: $('lead-count'), searchChip: $('search-chip'), searchChipText: $('search-chip-text'), searchChipClear: $('search-chip-clear'),
    leadTableWrap: $('lead-table-wrap'), leadRows: $('lead-rows'), leadsEmpty: $('leads-empty'),
    // Drawer
    drawer: $('drawer'), drawerBackdrop: $('drawer-backdrop'), drawerPanel: $('drawer-panel'), drawerScroll: $('drawer-scroll'),
    dName: $('d-name'), dCategory: $('d-category'), dClose: $('d-close'), dScore: $('d-score'), dFacts: $('d-facts'),
    dContact: $('d-contact'), dStatus: $('d-status'), dNotes: $('d-notes'), dNotesSave: $('d-notes-save'), dNotesMsg: $('d-notes-msg'),
    dAudit: $('d-audit'), dIssuesSection: $('d-issues-section'), dIssues: $('d-issues'),
    dIdeasSection: $('d-ideas-section'), dIdeas: $('d-ideas'),
    dActions: $('d-actions'), dReaudit: $('d-reaudit'), dReauditLabel: $('d-reaudit-label'), dDelete: $('d-delete'),
    dConfirm: $('d-confirm'), dConfirmYes: $('d-confirm-yes'), dConfirmNo: $('d-confirm-no'),
    // Settings
    settingsLoading: $('settings-loading'), settingsForm: $('settings-form'),
    stYourName: $('st-your-name'), stBusinessName: $('st-business-name'), stEmail: $('st-email'), stWhatsapp: $('st-whatsapp'),
    stPortfolioUrl: $('st-portfolio-url'), stServices: $('st-services'), stAddService: $('st-add-service'),
    stPortfolio: $('st-portfolio'), stAddPortfolio: $('st-add-portfolio'),
    stGoogleKey: $('st-google-key'), stGoogleState: $('st-google-state'), stTestGoogle: $('st-test-google'), stTestResult: $('st-test-result'),
    stClaudeKey: $('st-claude-key'), stClaudeState: $('st-claude-state'), stPagespeed: $('st-pagespeed'),
    stSave: $('st-save'), stSaveMsg: $('st-save-msg'),
  });
}

function wireEvents() {
  window.addEventListener('hashchange', applyRoute);
  document.addEventListener('keydown', onDrawerKeydown);

  document.querySelector('.skip-link').addEventListener('click', (event) => {
    event.preventDefault(); // a plain "#main" would be mistaken for a screen name
    $(`${route.view}-title`).focus();
  });
  els.offlineRetry.addEventListener('click', refreshCurrentView);

  // Copy buttons anywhere on the page.
  document.addEventListener('click', (event) => {
    const btn = event.target.closest('[data-copy]');
    if (btn) onCopyClick(btn);
  });

  // Find
  els.searchForm.addEventListener('submit', submitSearch);
  els.sQuery.addEventListener('input', () => {
    if (els.sQuery.value.trim()) els.sQuery.closest('.sb-field').classList.remove('is-invalid');
  });
  els.manualForm.addEventListener('submit', submitManual);

  // Leads: filters
  els.filters.addEventListener('submit', (event) => {
    event.preventDefault();
    applyFilterControls();
  });
  els.fQ.addEventListener('input', () => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(applyFilterControls, 300);
  });
  for (const control of [els.fStatus, els.fScore, els.fWebsite, els.fSort, els.fEmail]) {
    control.addEventListener('change', applyFilterControls);
  }
  els.fClear.addEventListener('click', clearFilters);
  els.searchChipClear.addEventListener('click', clearSearchScope);
  els.exportLink.addEventListener('click', exportCsv);

  // Leads: table
  els.leadRows.addEventListener('click', (event) => {
    const opener = event.target.closest('[data-open]');
    if (opener) {
      openLead(Number(opener.dataset.open), opener);
      return;
    }
    if (event.target.closest('a, button, select, input, label')) return;
    if (String(window.getSelection && window.getSelection()).trim()) return; // the user is selecting text
    const row = event.target.closest('tr[data-id]');
    if (row) openLead(Number(row.dataset.id), row.querySelector('[data-open]'));
  });
  els.leadRows.addEventListener('change', (event) => {
    const select = event.target.closest('select[data-row-status]');
    if (select) changeStatus(Number(select.dataset.rowStatus), select.value, select);
  });

  // Drawer
  els.dClose.addEventListener('click', closeDrawer);
  els.drawerBackdrop.addEventListener('click', closeDrawer);
  els.dStatus.addEventListener('change', () => {
    if (state.detailId !== null) changeStatus(state.detailId, els.dStatus.value, els.dStatus);
  });
  els.dNotes.addEventListener('input', () => setResult(els.dNotesMsg, '', ''));
  els.dNotes.addEventListener('blur', () => saveNotes(false));
  els.dNotesSave.addEventListener('click', () => saveNotes(true));
  els.dReaudit.addEventListener('click', reauditLead);
  els.dDelete.addEventListener('click', () => {
    showDeleteConfirm(true);
    els.dConfirmNo.focus();
  });
  els.dConfirmNo.addEventListener('click', () => {
    showDeleteConfirm(false);
    els.dDelete.focus();
  });
  els.dConfirmYes.addEventListener('click', deleteLead);

  // Settings
  els.settingsForm.addEventListener('submit', (event) => {
    event.preventDefault();
    saveSettings();
  });
  els.settingsForm.addEventListener('input', markSettingsDirty);
  els.settingsForm.addEventListener('change', markSettingsDirty);
  els.settingsForm.addEventListener('click', (event) => {
    const remove = event.target.closest('[data-remove]');
    if (remove) removeListRow(remove);
  });
  els.stAddService.addEventListener('click', () => {
    const row = serviceRow('');
    els.stServices.append(row);
    row.querySelector('input').focus();
    markSettingsDirty();
  });
  els.stAddPortfolio.addEventListener('click', () => {
    const row = portfolioRow({});
    els.stPortfolio.append(row);
    row.querySelector('input').focus();
    markSettingsDirty();
  });
  els.stTestGoogle.addEventListener('click', testGoogleKey);
}

function init() {
  cacheElements();
  wireEvents();
  applyRoute();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
