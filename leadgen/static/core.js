/* The Lead Finder page, part 1 of 7: shared helpers, the colour theme and page switching.
   One HTML page (templates/index.html) holds Find leads, Leads, Calls, Map and Stats, switched by
   the #address; these scripts load in order and share their top-level names. */
// The names this part shares with the others (eslint.config.mjs reads this list).
/* exported $, $all, eventEl, CONFIG, OUTCOMES, PAUSED, PIN_MS, PAGE, SHIFT_GUARD_MS, TIMING, S, el, link, phoneLink,
   button, api, post, save, SLOW_SAVE, fmtTime, serverNow, leftOf, problem, loadingCue, themeChoice, applyTheme, myName,
   withName, byWho, parseHash, readLeadView, route, writeHash */
// An element of the page by its id: an input, a dialog, a button... (typed `any`, as the page's
// ids name every kind; the type check still catches a misspelt function or variable).
/** @type {(id: string) => any} */
const $ = (id) => document.getElementById(id);
// The page's elements a CSS selector picks, and the element an event happened on.
/** @type {(selector: string) => NodeListOf<HTMLElement>} */
const $all = (selector) => document.querySelectorAll(selector);
/** @type {(e: Event) => HTMLElement} */
const eventEl = (e) => /** @type {HTMLElement} */ (e.target);
const CONFIG = /** @type {PageConfig} */ (JSON.parse($("page-config").textContent));
const OUTCOMES = CONFIG.outcomes;
const PAUSED = CONFIG.paused;
const SITE = "AARCO Compactor Lead Finder";
/** @type {Record<string, string>} */
const TITLES = { find: "Find leads", leads: "Leads", calls: "Calls", map: "Map", stats: "Stats" };
const PAGES = ["find", "leads", "calls", "map", "stats"];
// A row just marked Yes or No stays where it is (showing its answer and Undo) for this long
// after the pointer leaves the table, so a double-click can never land on the next business.
const PIN_MS = 5000;
// Rows the server sends at a time (web/leads.py PAGE_SIZE); "Show more" asks for the next ones.
const PAGE = 100;
// Clicks this soon after rows moved up are ignored (they were aimed at the row that left).
const SHIFT_GUARD_MS = 700;
// How long the page waits, in milliseconds, before it says something is slow: the first answer
// after the site woke up (start.js: `waking`, then `stillWaking`), and a save (core.js save():
// "taking longer than usual" after `saveSlow`, given up as not saved after `saveLimit`). The
// browser tests shorten them (window.leadgenTiming, set before the page's scripts run).
/** @type {Timing} */
const TIMING = { waking: 4000, stillWaking: 45000, saveSlow: 10000, saveLimit: 30000,
                 ...(window.leadgenTiming || {}) };
// The Leads page holds only the rows it shows (one tab, filtered and sorted by the server,
// the first `limit` of them), every tab's count, and the leads that can still be undone.
// `failed`: the Yes / No clicks that didn't reach the server, by lead, until retried or dismissed.
// The Calls page likewise holds the first `callLimit` called businesses of its tab and filter
// (`called`, of `calledTotal`), and its tabs' counts (`callCounts`).
/** @type {PageState} */
const S = { leads: [], total: 0, counts: null, recent: [], called: [], calledLoaded: false, calledError: "",
            calledTotal: 0, callCounts: null, callLimit: PAGE, callSeq: 0, calledLoading: false,
            sending: new Map(), failed: new Map(), loaded: false, loadError: "", refreshError: "",
            viewLoading: false, seq: 0,
            leadView: "", callView: "", callQ: "", q: "", lead: "", tier: "", phone: false, sort: "score", dir: "desc",
            limit: PAGE, job: null, error: "", skew: 0, busy: 0, since: 0, pinned: new Map(), shiftedAt: 0,
            cutOffTimer: 0 };

/**
 * A new element: <tag>, with its text and class names when given.
 * @template {keyof HTMLElementTagNameMap} K
 * @param {K} tag
 * @param {string | number | null} [text]
 * @param {string} [cls]
 * @returns {HTMLElementTagNameMap[K]}
 */
function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  if (cls) e.className = cls;
  return e;
}
/** A link opening in a new tab (a plain span when href isn't a web address).
    @param {string} href @param {string} [text] @returns {HTMLElement} */
function link(href, text) {
  if (!href || !/^https?:\/\//i.test(href)) return el("span", "");
  const a = el("a", text || href.replace(/^https?:\/\/(www\.)?/i, "").replace(/\/$/, ""));
  a.href = href; a.target = "_blank"; a.rel = "noopener";
  return a;
}
/** @param {string} phone @returns {HTMLElement} */
function phoneLink(phone) {
  const digits = (phone || "").replace(/[^\d+]/g, "");
  if (digits.replace(/\D/g, "").length < 7) return el("span", phone || "");
  const a = el("a", phone); a.href = `tel:${digits}`; a.className = "nowrap";
  return a;
}
/** @param {string} text @param {string} [cls] @param {(e: MouseEvent) => unknown} [onClick] */
function button(text, cls, onClick) {
  const b = el("button", text, cls);
  b.type = "button";
  if (onClick) b.addEventListener("click", onClick);
  return b;
}
// The server's JSON answer (typed `any`: each caller names the answer it reads, LeadsPage...);
// an answer that isn't OK throws an ApiError saying why.
/** @param {string} path @param {RequestInit} [opts] @returns {Promise<any>} */
async function api(path, opts) {
  let res;
  try { res = await fetch(path, opts); }
  catch (err) {
    throw Object.assign(new Error("Can't reach the Lead Finder. Check your internet connection and try again."),
                        { status: 0 });
  }
  if (res.status === 401) { location.href = "/login"; throw new Error("Logged out"); }
  const body = await res.json().catch(() => ({}));
  if (typeof body.now === "number") S.skew = body.now - Date.now() / 1000;
  if (!res.ok) {
    throw Object.assign(new Error(body.error || "Something went wrong. Try again in a minute."),
                        { status: res.status, body });
  }
  return body;
}
/** @type {(path: string, data: unknown) => Promise<any>} */
const post = (path, data) => api(path, { method: "POST", headers: { "Content-Type": "application/json" },
                                        body: JSON.stringify(data) });
// A save (a Yes / No, a call, a verified contact, an undo): post() that never leaves the person
// wondering. After TIMING.saveSlow it calls `slow` (the caller says, where the person is looking,
// that it is taking longer than usual); after TIMING.saveLimit it stops waiting and throws an
// ApiError (status 0, timedOut) saying it may not have been saved. The request is cancelled
// then, so a late answer never changes the page; trying again is safe, as each save is
// recorded once (a call carries its own id, and the same Yes / No or contact again changes nothing).
/** @param {string} path @param {unknown} data @param {() => void} [slow] @returns {Promise<any>} */
async function save(path, data, slow) {
  const stop = new AbortController();
  const slowTimer = slow ? setTimeout(slow, TIMING.saveSlow) : 0;
  const limit = setTimeout(() => stop.abort(), TIMING.saveLimit);
  try {
    const body = await api(path, { method: "POST", headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify(data), signal: stop.signal });
    if (stop.signal.aborted) throw new Error("cancelled");         // its answer was cut off
    return body;
  } catch (err) {
    if (!stop.signal.aborted) throw err;
    throw Object.assign(new Error(`The Lead Finder didn't answer within ${Math.round(TIMING.saveLimit / 1000)} ` +
                                  "seconds, so it may not have been saved. Check your connection and try again."),
                        { status: 0, timedOut: true });
  } finally { clearTimeout(slowTimer); clearTimeout(limit); }
}
// What a save says once it has taken TIMING.saveSlow.
const SLOW_SAVE = "This is taking longer than usual. Still trying…";
/** @type {(s: number) => string} */
const fmtTime = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const serverNow = () => Date.now() / 1000 + S.skew;
/** @type {(undo: Undo | null | undefined) => number} */
const leftOf = (undo) => undo ? Math.max(0, Math.floor(undo.until - serverNow())) : 0;
// A list whose next view (a tab, filter or sort) is on its way: its old rows stay, dimmed, and
// after LOADING_MS it says "Loading…" over them (the cue `id`, a role="status" box), after SLOW_MS
// also that it is taking longer than usual. on=false (the view arrived, or failed) clears it.
const LOADING_MS = 300, SLOW_MS = 5000;
/** @type {Map<string, number[]>} */
const cueTimers = new Map();
/** @param {string} id @param {boolean} on */
function loadingCue(id, on) {
  const cue = $(id), timers = cueTimers.get(id);
  if (on) {
    if (timers) return;                                   // already on its way
    cueTimers.set(id, [
      setTimeout(() => cue.replaceChildren(el("span", undefined, "spinner"), el("span", "Loading…")), LOADING_MS),
      setTimeout(() => cue.append(el("span", "This is taking longer than usual. The list appears as soon as it " +
                                            "arrives.", "slow")), SLOW_MS)]);
    return;
  }
  for (const t of timers || []) clearTimeout(t);
  cueTimers.delete(id);
  if (cue.childNodes.length) cue.replaceChildren();
}
/** @param {HTMLElement} box @param {string} title @param {string} message @param {() => unknown} [retry] */
function problem(box, title, message, retry) {
  const p = el("div", undefined, "problem");
  const text = el("div"); text.append(el("strong", title), el("span", message, "muted"));
  p.append(text);
  if (retry) p.append(button("Retry", "quiet", retry));
  box.replaceChildren(p);
}

function themeChoice() { try { return localStorage.getItem("theme") || "system"; } catch (e) { return "system"; } }
/** @param {string} choice "light", "dark" or "system" */
function applyTheme(choice) {
  if (choice === "light" || choice === "dark") document.documentElement.setAttribute("data-theme", choice);
  else document.documentElement.removeAttribute("data-theme");
  try {
    if (choice === "system") localStorage.removeItem("theme"); else localStorage.setItem("theme", choice);
  } catch (e) { /* not kept */ }
  $all("[data-theme-pick]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themePick === choice)));
  window.setThemeColor(choice);              // the browser's bar matches (templates/_theme.html)
  if (chartRows && !$("page-stats").hidden) drawChart(chartRows);
  restyleMap();                               // the map's pins and areas take the theme's colours
}
$all("[data-theme-pick]").forEach((b) => {
  b.addEventListener("click", () => applyTheme(b.dataset.themePick || "system"));
});
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (chartRows) drawChart(chartRows);
  restyleMap();
});

/* Who is using this browser: the name saved with each Yes / No and call made here, so the
   team can see who to ask. Kept in this browser; asked once before the first mark or call. */
const NAME_KEY = "my-name";
let nameMemory = "";
/** @type {(() => void) | null} */
let nameThen = null;
function myName() {
  try { return localStorage.getItem(NAME_KEY) || nameMemory; } catch (e) { return nameMemory; }
}
/** @param {string} name */
function setMyName(name) {
  nameMemory = name.trim().replace(/\s+/g, " ").slice(0, 60);
  try { if (nameMemory) localStorage.setItem(NAME_KEY, nameMemory); else localStorage.removeItem(NAME_KEY); }
  catch (e) { /* kept for this visit only */ }
  showMyName();
}
function showMyName() {
  const name = myName();
  $("me-name").textContent = name || "not set";
  $("me-change").textContent = name ? "Change" : "Set";
}
/** @param {() => void} [then] */
function askName(then) {
  nameThen = then || null;
  $("name-input").value = myName();
  $("name-error").hidden = true;
  $("name-dlg").showModal();
  $("name-input").focus();
}
// Runs fn once the name is known: every mark and call says who made it, so there is no skipping.
/** @param {() => void} fn */
function withName(fn) { if (myName()) fn(); else askName(fn); }
$("name-form").addEventListener("submit", (/** @type {SubmitEvent} */ e) => {
  e.preventDefault();
  if (!$("name-input").value.trim()) {
    $("name-error").hidden = false;
    $("name-input").setAttribute("aria-invalid", "true");
    $("name-input").focus();
    return;
  }
  $("name-input").removeAttribute("aria-invalid");
  setMyName($("name-input").value);
  const then = nameThen; nameThen = null;
  $("name-dlg").close();
  if (then) then();
});
// Cancel (or Escape): nothing is saved, and the click that asked is dropped.
$("name-cancel").addEventListener("click", () => { nameThen = null; $("name-dlg").close(); });
$("name-dlg").addEventListener("cancel", () => { nameThen = null; });
$("me-change").addEventListener("click", () => askName());
showMyName();
/** @type {(name: string) => string} */
const byWho = (name) => name ? ` by ${name}` : "";

// Phones fold the Yelp count, the colour theme and Log out into a Menu button, so the
// navigation stays one short bar at the top.
$("menu-btn").addEventListener("click", () => {
  const open = /** @type {HTMLElement} */ (document.querySelector(".side")).classList.toggle("open");
  $("menu-btn").setAttribute("aria-expanded", open);
});

// "Skip to main content" (the first Tab stop): past the sidebar, to the page's list or heading.
$("skip").addEventListener("click", () => {
  const page = document.querySelector(".page:not([hidden])");
  if (!page) return;
  const target = { "page-leads": $("leads-wrap"), "page-calls": $("calls-wrap") }[page.id] || page.querySelector("h1");
  target.setAttribute("tabindex", "-1");
  target.focus();
});

/* ---------- pages and the address bar ---------- */
function parseHash() {
  const [page, query] = (location.hash || "").slice(1).split("?");
  return { page, params: new URLSearchParams(query || "") };
}
// Take the Leads view (tab, filter, tier, sort) from the address; true when it changed.
/** @param {URLSearchParams} params */
function readLeadView(params) {
  const before = [S.leadView, S.q, S.lead, S.tier, S.phone, S.sort, S.dir].join("|");
  S.leadView = params.get("tab") || "";
  if (!LEAD_TABS.some(([v]) => v === S.leadView)) S.leadView = "";
  S.q = params.get("q") || ""; S.tier = params.get("tier") || ""; S.phone = params.get("phone") === "1";
  S.lead = (params.get("lead") || "").slice(0, 64);       // one business (the Map page's link)
  const sort = params.get("sort") || "";
  S.sort = SORTS[sort] ? sort : "score";
  S.dir = params.get("dir") === "asc" ? "asc" : params.get("dir") === "desc" ? "desc" : SORTS[S.sort].dir;
  $("filter").value = S.q; $("tier").value = S.tier; $("has-phone").checked = S.phone;
  return before !== [S.leadView, S.q, S.lead, S.tier, S.phone, S.sort, S.dir].join("|");
}
function route() {
  const { page: asked, params } = parseHash();
  const page = PAGES.includes(asked) ? asked
    : (S.counts && S.counts.all ? "leads" : "find");
  // A note about one page never covers the next (leads.js may not be loaded yet).
  if (typeof hideToast === "function") hideToast();
  if (page === "leads" && params.toString() && readLeadView(params)) { S.pinned.clear(); S.limit = PAGE; changeView(); }
  if (page === "calls") {
    const tab = params.get("tab");
    S.callView = tab && OUTCOMES.includes(tab) ? tab : "";
    S.callQ = params.get("q") || "";
    S.callLimit = PAGE;
    loadCalled();
  }
  for (const p of PAGES) $(`page-${p}`).hidden = p !== page;
  document.title = `${TITLES[page]} · ${SITE}`;
  $all("nav a").forEach((a) => {
    a.classList.toggle("on", a.dataset.page === page);
    if (a.dataset.page === page) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  if (page === "leads" || page === "calls") writeHash(page);
  if (page === "stats") loadStats();
  if (page === "map") loadMap();
  if (page === "find") loadSearches();
}
/** @param {string} page */
function writeHash(page) {
  const params = new URLSearchParams();
  if (page === "leads") {
    if (S.leadView) params.set("tab", S.leadView);
    if (S.q) params.set("q", S.q);
    if (S.lead) params.set("lead", S.lead);
    if (S.tier) params.set("tier", S.tier);
    if (S.phone) params.set("phone", "1");
    if (S.sort !== "score" || S.dir !== "desc") { params.set("sort", S.sort); params.set("dir", S.dir); }
  } else if (page === "calls") {
    if (S.callView) params.set("tab", S.callView);
    if (S.callQ) params.set("q", S.callQ);
  }
  const hash = `#${page}${params.toString() ? "?" + params : ""}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
}
window.addEventListener("hashchange", route);

// A double-click on a dialog's button (Start search, Save, Close) closes the dialog with the
// first click; the second must not land on the page underneath and select its text.
let dialogClickAt = 0;
document.addEventListener("click", (e) => {
  if (eventEl(e).closest("dialog button")) dialogClickAt = Date.now();
}, true);
document.addEventListener("mousedown", (e) => {
  if (e.detail > 1 && (Date.now() - dialogClickAt < 800 || eventEl(e).closest("dialog button"))) e.preventDefault();
}, true);
document.addEventListener("dblclick", () => {
  if (Date.now() - dialogClickAt < 800) window.getSelection()?.removeAllRanges();
}, true);
