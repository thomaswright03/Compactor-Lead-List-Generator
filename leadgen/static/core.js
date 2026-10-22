/* The Lead Finder page, part 1 of 7: shared helpers, the colour theme and page switching.
   One HTML page (templates/index.html) holds Find leads, Leads, Calls, Map, Stats and Customers,
   switched by the #address; these scripts load in order and share their top-level names. */
// The names this part shares with the others (eslint.config.mjs reads this list).
/* exported $, $all, eventEl, CONFIG, OUTCOMES, PAUSED, PIN_MS, PAGE, SHIFT_GUARD_MS, TIMING, S, el,
   emptyBox, link, phoneLink, button, DOT, api, noAnswer, post, save, SLOW_SAVE, fmtTime, serverNow,
   leftOf, problem, loadingCue, themeChoice, applyTheme, myName, withName, byWho, parseHash,
   readLeadView, route, writeHash, loggedOut, OWN, enterRows, countUp */
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
const SITE = CONFIG.site;
// The company's short name, where the page says whose shop or area ("Summit's shop").
const OWN = CONFIG.short;
/** @type {Record<string, string>} */
const TITLES = { find: "Find leads", leads: "Leads", calls: "Calls", map: "Map", stats: "Stats",
                 customers: "Current customers", settings: "Settings",
                 admin: "Site administrator" };
// Sign-up mode adds the company's Settings (a page of the server's own forms).
// The administrator's page (/admin) has no link: whoever looks after the site opens it.
const PAGES = ["find", "leads", "calls", "map", "stats", "customers",
               ...(CONFIG.signup ? ["settings"] : []), "admin"];
// A row just marked Yes or No stays where it is (showing its answer and Undo) for this long
// after the pointer leaves the table, so a double-click can never land on the next business.
const PIN_MS = 5000;
// Rows the server sends at a time (web/leads.py PAGE_SIZE); "Show more" asks for the next ones.
const PAGE = 100;
// Clicks this soon after rows moved up are ignored (they were aimed at the row that left).
const SHIFT_GUARD_MS = 700;
// How long the page waits, in milliseconds, before it says something is slow: the first answer
// after the site woke up (start.js: `waking`, then `stillWaking`), and a save (core.js save():
// "taking longer than usual" after `saveSlow`, given up as not saved after `saveLimit`); and
// how often a page that couldn't load tries again by itself (`retry`, problem()). The
// browser tests shorten them (window.leadgenTiming, set before the page's scripts run).
/** @type {Timing} */
const TIMING = { waking: 4000, stillWaking: 45000, saveSlow: 10000, saveLimit: 30000, retry: 20000,
                 ...(window.leadgenTiming || {}) };
// The Leads page holds only the rows it shows (one tab, filtered and sorted by the server,
// the first `limit` of them), every tab's count, and the leads that can still be undone.
// `failed`: the Yes / No clicks that didn't reach the server, by lead, until retried or dismissed.
// The Calls page likewise holds the first `callLimit` called businesses of its tab and filter
// (`called`, of `calledTotal`), and its tabs' counts (`callCounts`).
/** @type {PageState} */
const S = { leads: [], total: 0, counts: null, recent: [], called: [], calledLoaded: false,
            calledError: "", calledTotal: 0, callCounts: null, callLimit: PAGE, callSeq: 0,
            calledLoading: false, sending: new Map(), failed: new Map(), loaded: false,
            loadError: "", refreshError: "", viewLoading: false, seq: 0,
            leadView: "", callView: "", callQ: "", q: "", lead: "", tier: "", phone: false,
            sort: "score", dir: "desc", limit: PAGE, job: null, error: "", skew: 0, busy: 0,
            since: 0, pinned: new Map(), shiftedAt: 0, cutOffTimer: 0 };

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
// The small pictures above an empty page's words, drawn in the text's colour (so they suit
// both themes) and never moving, so an empty page can't be taken for one still loading: the
// page's own picture from the sidebar (a phone on Calls, a chart on Stats, a list on Leads, a
// pin on the Map), a funnel when filters hide everything, a tick when every business is checked.
const EMPTY_ICONS = {
  phone: '<path d="M5 3h4l2 5-2.5 1.5a11 11 0 0 0 6 6L16 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 ' +
         '3 5a2 2 0 0 1 2-2z"/>',
  chart: '<path d="M4 20h16M7 16v-5M12 16V6M17 16v-8"/>',
  list: '<path d="M9 6h11M9 12h11M9 18h11"/>' +
        '<path d="M4.5 6h.01M4.5 12h.01M4.5 18h.01" stroke-width="2.6"/>',
  pin: '<path d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21z"/>' +
       '<circle cx="12" cy="9.5" r="2.5"/>',
  filter: '<path d="M4 5h16l-6.2 7.6v5.6l-3.6 1.8v-7.4z"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
};
/** An empty page's box (class "empty"), its picture already in it; the caller adds the words.
    @param {keyof typeof EMPTY_ICONS} icon @param {...(Node | string)} words
    @returns {HTMLDivElement} */
function emptyBox(icon, ...words) {
  const pic = el("span", undefined, `empty-icon ${icon}`);
  pic.setAttribute("aria-hidden", "true");
  pic.innerHTML = '<svg viewBox="0 0 24 24" width="28" height="28" fill="none" ' +
    'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
    `${EMPTY_ICONS[icon]}</svg>`;
  const box = el("div", undefined, "empty");
  box.append(pic, ...words);
  return box;
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
// The "·" between items on one line, kept on the line of the item before it (a no-break space
// before it, an ordinary one after), so a line that wraps never starts with it.
const DOT = "\u00a0· ";
/** @param {string} text @param {string} [cls] @param {(e: MouseEvent) => unknown} [onClick] */
function button(text, cls, onClick) {
  const b = el("button", text, cls);
  b.type = "button";
  if (onClick) b.addEventListener("click", onClick);
  return b;
}
// True when the browser says this device has no network at all (switched off, out of range).
// It can't say the opposite for sure, so a request that got no answer while it is online is
// put down to the site (stopped, restarting, overloaded), never to the person's internet.
const isOffline = () => window.navigator.onLine === false;
// Why a request got no answer at all, in the words a page shows (with its Try again or Retry).
const noAnswer = () => isOffline() ? "You're offline. Try again when you're back online."
  : "The Lead Finder isn't answering right now. Try again in a minute.";
// The server's JSON answer (typed `any`: each caller names the answer it reads, LeadsPage...);
// an answer that isn't OK throws an ApiError saying why (no answer at all: status 0, and
// `offline` when the browser says the device is offline).
/** @param {string} path @param {RequestInit} [opts] @returns {Promise<any>} */
async function api(path, opts) {
  let res;
  try { res = await fetch(path, opts); }
  catch (err) {
    throw Object.assign(new Error(noAnswer()), { status: 0, offline: isOffline() });
  }
  if (res.status === 401) {
    loggedOut(lostAction(path, opts));
    throw Object.assign(new Error("You were logged out."), { status: 401 });
  }
  const body = await res.json().catch(() => ({}));
  if (typeof body.now === "number") S.skew = body.now - Date.now() / 1000;
  if (!res.ok) {
    throw Object.assign(new Error(body.error || "Something went wrong. Try again in a minute."),
                        { status: res.status, body });
  }
  return body;
}
/* The login ended (the site answered "logged out", 401): the login page says so, and what
   wasn't saved ("Your Yes for Target wasn't saved."), and after logging in comes back to the
   same page and tab (its #address; web/auth.py return_to). The words go to the login page in
   this tab's sessionStorage, never in its address. Notes typed in the call box stay as the
   business's draft (calls.js), so nothing typed is lost. */
const LOGGED_OUT_KEY = "logged-out";
let leaving = false;
/** @param {string} note what wasn't saved, or "" (a list that couldn't load: nothing was lost) */
function loggedOut(note) {
  try {
    // A click that wasn't saved is said over a list that couldn't refresh at the same moment.
    if (note || !leaving) {
      window.sessionStorage.setItem(LOGGED_OUT_KEY, JSON.stringify({ note, at: Date.now() }));
    }
  } catch (e) { /* the login page says it more generally */ }
  if (leaving) return;
  leaving = true;
  location.assign(`/login?ended=1&next=${encodeURIComponent(location.pathname + location.hash)}`);
}
try { window.sessionStorage.removeItem(LOGGED_OUT_KEY); } catch (e) { /* nothing kept */ }
// What a request the login ended on was doing, in the words the login page shows; "" for one
// that only reads (a list, the stats), which loses nothing.
/** @param {string} path @param {RequestInit} [opts] @returns {string} */
function lostAction(path, opts) {
  if (!opts || !opts.method || opts.method.toUpperCase() === "GET") return "";
  /** @type {Record<string, any>} */
  let data = {};
  try { data = typeof opts.body === "string" ? JSON.parse(opts.body) : {}; }
  catch (e) { /* not JSON */ }
  // The business it was about: by its key (a Yes / No, a call, a contact) or its undo's id.
  const lead = [...S.leads, ...S.called, ...S.recent].find((l) => (data.key && l.key === data.key)
    || (data.id && ((l.undo_mark && l.undo_mark.id === data.id)
                    || (l.undo_call && l.undo_call.id === data.id))));
  /** @type {(word: string) => string} */
  const of = (word) => lead ? ` ${word} ${lead.name}` : "";
  const answer = data.value === "yes" ? "Yes" : data.value === "no" ? "No" : "answer";
  /** @type {Record<string, string>} */
  const said = {
    "/mark": `Your ${answer}${of("for")} wasn't saved.`,
    "/mark/undo": `Your Undo${of("for")} wasn't saved.`,
    "/calls": `Your call${of("to")} wasn't saved. Your notes are kept: open Just called on it ` +
              "again to save them.",
    "/calls/undo": `Removing the call${of("to")} wasn't saved.`,
    "/contact": `The verified contact${of("for")} wasn't saved.`,
    "/search": "Your search didn't start, so today's search wasn't used.",
    "/switches": "The switch wasn't changed.",
    "/admin/unlock": "The administrator's section wasn't unlocked.",
  };
  return said[path.split("?")[0]] || "What you did last wasn't saved.";
}
/** @type {(path: string, data: unknown) => Promise<any>} */
const post =(path, data) => api(path, { method: "POST",
                                        headers: { "Content-Type": "application/json" },
                                        body: JSON.stringify(data) });
// A save (a Yes / No, a call, a verified contact, an undo): post() that never leaves the person
// wondering. After TIMING.saveSlow it calls `slow` (the caller says, where the person is looking,
// that it is taking longer than usual); after TIMING.saveLimit it stops waiting and throws an
// ApiError (status 0, timedOut) saying it may not have been saved. The request is cancelled
// then, so a late answer never changes the page; trying again is safe, as each save is
// recorded once (a call carries its own id, and the same Yes / No or contact again changes
// nothing).
/** @param {string} path @param {unknown} data @param {() => void} [slow]
    @returns {Promise<any>} */
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
    throw Object.assign(new Error(`The Lead Finder didn't answer within ` +
                                  `${Math.round(TIMING.saveLimit / 1000)} seconds, so it may not ` +
                                  "have been saved. Check your connection and try again."),
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
// after LOADING_MS it says "Loading…" over them (the cue `id`, a role="status" box), after
// SLOW_MS also that it is taking longer than usual. on=false (the view arrived, or failed)
// clears it.
const LOADING_MS = 300, SLOW_MS = 5000;
/** @type {Map<string, number[]>} */
const cueTimers = new Map();
/** @param {string} id @param {boolean} on */
function loadingCue(id, on) {
  const cue = $(id), timers = cueTimers.get(id);
  if (on) {
    if (timers) return;                                   // already on its way
    cueTimers.set(id, [
      setTimeout(() => cue.replaceChildren(el("span", undefined, "spinner"),
                                           el("span", "Loading…")), LOADING_MS),
      setTimeout(() => cue.append(el("span", "This is taking longer than usual. The list appears " +
                                            "as soon as it arrives.", "slow")), SLOW_MS)]);
    return;
  }
  for (const t of timers || []) clearTimeout(t);
  cueTimers.delete(id);
  if (cue.childNodes.length) cue.replaceChildren();
}
// A box saying what couldn't be shown, with a Retry button. `again` (a page's list, map or stats
// that couldn't load: the site or its database may be back in a moment) is tried quietly every
// TIMING.retry, and at once when the browser tab is shown again, until a load works and the page
// replaces the box. Only while the box's page is open: another page, or leaving the site, stops it.
/** @typedef {{node: HTMLElement, again: () => unknown, timer: number}} Retrying */
/** @type {Map<HTMLElement, Retrying>} */
const retrying = new Map();
/**
 * @param {HTMLElement} box @param {string} title @param {string} message
 * @param {() => unknown} [retry] @param {() => unknown} [again]
 */
function problem(box, title, message, retry, again) {
  const p = el("div", undefined, "problem");
  const text = el("div"); text.append(el("strong", title), el("span", message, "muted"));
  p.append(text);
  if (retry) p.append(button("Retry", "quiet", retry));
  box.replaceChildren(p);
  let r = retrying.get(box);
  if (!again) {
    if (r) clearTimeout(r.timer);
    retrying.delete(box);
    return;
  }
  // Drawn again while it waits (the page was redrawn): the next try keeps its time.
  if (r) { r.node = p; r.again = again; } else retrying.set(box, r = { node: p, again, timer: 0 });
  if (!r.timer) retryBox(box, r, false);
}
// One box's next quiet try: after TIMING.retry (now: straight away), if its page is open in a
// tab in view and the box is still there.
/** @param {HTMLElement} box @param {Retrying} r @param {boolean} now */
function retryBox(box, r, now) {
  clearTimeout(r.timer); r.timer = 0;
  if (!box.contains(r.node)) { retrying.delete(box); return; }          // loaded since, or replaced
  const page = /** @type {HTMLElement | null} */ (box.closest(".page"));
  if (document.visibilityState !== "visible" || (page && page.hidden)) return;
  const go = () => {
    r.timer = 0;
    if (retrying.get(box) !== r) return;
    retrying.delete(box);                     // a failed try draws the box again, and waits again
    if (box.contains(r.node)) r.again();
  };
  if (now) go(); else r.timer = setTimeout(go, TIMING.retry);
}
// Another page was opened, or the tab shown (now) or hidden: each box tries again only on its
// own page.
/** @param {boolean} [now] */
function retryLater(now) {
  for (const [box, r] of [...retrying]) retryBox(box, r, !!now);
}
document.addEventListener("visibilitychange", () => retryLater(true));
// Back online: each box on the page shown tries again straight away.
window.addEventListener("online", () => retryLater(true));

function themeChoice() {
  try { return localStorage.getItem("theme") || "system"; } catch (e) { return "system"; }
}
/** @param {string} choice "light", "dark" or "system" */
function paintTheme(choice) {
  if (choice === "light" || choice === "dark") {
    document.documentElement.setAttribute("data-theme", choice);
  } else document.documentElement.removeAttribute("data-theme");
  if (chartRows && !$("page-stats").hidden) drawChart(chartRows);
  restyleMap();                               // the map's pins and areas take the theme's colours
}
/** @param {string} choice "light", "dark" or "system" @param {{x: number, y: number}} [from] */
function applyTheme(choice, from) {
  // Picked with a click: the new colours spread out in a circle from the button (app.css,
  // Motion). The browser's bar and the buttons' state change at once either way.
  const doc = /** @type {any} */ (document);
  if (from && doc.startViewTransition && !reducedMotion()) {
    const shift = doc.startViewTransition(() => paintTheme(choice));
    const at = `at ${from.x}px ${from.y}px`;
    const r = Math.hypot(Math.max(from.x, window.innerWidth - from.x),
                         Math.max(from.y, window.innerHeight - from.y));
    shift.ready.then(() => {
      document.documentElement.animate(
        { clipPath: [`circle(0px ${at})`, `circle(${r}px ${at})`] },
        { duration: 650, easing: "cubic-bezier(.16,1,.3,1)",
          pseudoElement: "::view-transition-new(root)" });
    }).catch(() => { /* skipped: the colours simply change */ });
  } else paintTheme(choice);
  try {
    if (choice === "system") localStorage.removeItem("theme");
    else localStorage.setItem("theme", choice);
  } catch (e) { /* not kept */ }
  $all("[data-theme-pick]").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.themePick === choice));
  });
  window.setThemeColor(choice);              // the browser's bar matches (templates/_theme.html)
}
$all("[data-theme-pick]").forEach((b) => {
  b.addEventListener("click", (e) => {
    const box = b.getBoundingClientRect();
    // A click's own point; a key press (no point) starts from the button's middle.
    const from = e.clientX || e.clientY ? { x: e.clientX, y: e.clientY }
      : { x: box.left + box.width / 2, y: box.top + box.height / 2 };
    applyTheme(b.dataset.themePick || "system", from);
  });
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
  try {
    if (nameMemory) localStorage.setItem(NAME_KEY, nameMemory);
    else localStorage.removeItem(NAME_KEY);
  } catch (e) { /* kept for this visit only */ }
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
  const side = /** @type {HTMLElement} */ (document.querySelector(".side"));
  const open = side.classList.toggle("open");
  $("menu-btn").setAttribute("aria-expanded", open);
});

// "Skip to main content" (the first Tab stop): past the sidebar, to the page's list or heading.
$("skip").addEventListener("click", () => {
  const page = document.querySelector(".page:not([hidden])");
  if (!page) return;
  const target = { "page-leads": $("leads-wrap"), "page-calls": $("calls-wrap") }[page.id]
    || page.querySelector("h1");
  target.setAttribute("tabindex", "-1");
  target.focus();
});

/* ---------- motion (app.css, Motion): the sidebar's pill, ripples, rows coming in ---------- */
const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
// A new list's first rows slide in one after another; `rows` are its <tr>s.
/** @param {Iterable<Element>} rows */
function enterRows(rows) {
  if (reducedMotion()) return;
  let i = 0;
  for (const tr of rows) {
    if (i >= 14) break;
    /** @type {HTMLElement} */ (tr).style.setProperty("--i", String(i++));
    tr.classList.add("enter");
  }
}
// A big number counts up to its value (small ones are simply shown).
/** @param {HTMLElement} box @param {number} n */
function countUp(box, n) {
  const done = n.toLocaleString();
  if (n < 10 || reducedMotion()) { box.textContent = done; return; }
  const start = window.performance.now(), ms = 900;
  box.setAttribute("aria-label", done);
  /** @param {number} now */
  const step = (now) => {
    const t = Math.min(1, (now - start) / ms), eased = 1 - Math.pow(1 - t, 4);
    box.textContent = t < 1 ? Math.round(n * eased).toLocaleString() : done;
    if (t < 1) window.requestAnimationFrame(step); else box.removeAttribute("aria-label");
  };
  window.requestAnimationFrame(step);
}
// The sidebar's pill glides to the page opened (the links' "on" class, set by route()).
(() => {
  const nav = document.querySelector(".side nav");
  if (!nav) return;
  const glide = el("span", undefined, "nav-glide");
  glide.setAttribute("aria-hidden", "true");
  nav.append(glide); nav.classList.add("glides");
  const place = () => {
    const on = /** @type {HTMLElement | null} */ (nav.querySelector("a.on"));
    glide.hidden = !on || !on.offsetWidth;
    if (!on || glide.hidden) return;
    const box = nav.getBoundingClientRect(), at = on.getBoundingClientRect();
    glide.style.width = `${at.width}px`; glide.style.height = `${at.height}px`;
    glide.style.transform = `translate(${at.left - box.left}px, ${at.top - box.top}px)`;
    // It moves only once it has found its first place (no slide in from the corner on load).
    if (!placed) {
      placed = true;
      const raf = window.requestAnimationFrame;
      raf(() => raf(() => glide.classList.add("moves")));
    }
  };
  let placed = false;
  new window.MutationObserver(place).observe(nav, {
    subtree: true, attributes: true, attributeFilter: ["class"], childList: true,
    characterData: true });
  new window.ResizeObserver(place).observe(nav);
  place();
})();
// A press ripples out from under the pointer.
/** @type {WeakMap<HTMLElement, number>} */
const rippleTimers = new WeakMap();
document.addEventListener("pointerdown", (e) => {
  const b = /** @type {HTMLElement | null} */ (eventEl(e).closest?.("button, .btn"));
  if (!b || b.matches(":disabled, .link, .undo") || reducedMotion()) return;
  const box = b.getBoundingClientRect();
  b.style.setProperty("--rx", `${e.clientX - box.left}px`);
  b.style.setProperty("--ry", `${e.clientY - box.top}px`);
  b.style.setProperty("--rd", `${Math.hypot(box.width, box.height) * 2}px`);
  b.classList.remove("rippling");
  void b.offsetWidth;                         // start again on a quick second press
  b.classList.add("rippling");
  clearTimeout(rippleTimers.get(b));
  rippleTimers.set(b, setTimeout(() => b.classList.remove("rippling"), 650));
}, true);

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
  S.q = params.get("q") || ""; S.tier = params.get("tier") || "";
  S.phone = params.get("phone") === "1";
  S.lead = (params.get("lead") || "").slice(0, 64);       // one business (the Map page's link)
  const sort = params.get("sort") || "";
  S.sort = SORTS[sort] ? sort : "score";
  S.dir = params.get("dir") === "asc" ? "asc" : params.get("dir") === "desc" ? "desc"
    : SORTS[S.sort].dir;
  $("filter").value = S.q; $("tier").value = S.tier; $("has-phone").checked = S.phone;
  return before !== [S.leadView, S.q, S.lead, S.tier, S.phone, S.sort, S.dir].join("|");
}
// The page on screen; a new page starts at the top. Without this, the browser
// jumps to the element whose id matches the address, such as the map at #map.
let shownPage = "";
// Chrome keeps the address's element in view while the page lays out, so ask again
// on the next two frames as well.
function toTop() {
  const top = () => window.scrollTo(0, 0);
  top();
  window.requestAnimationFrame(() => { top(); window.requestAnimationFrame(top); });
}
function route() {
  const { page: asked, params } = parseHash();
  const page = PAGES.includes(asked) ? asked
    : (S.counts && S.counts.all ? "leads" : "find");
  // A note about one page never covers the next (leads.js may not be loaded yet).
  if (typeof hideToast === "function") hideToast();
  if (page === "leads" && params.toString() && readLeadView(params)) {
    S.pinned.clear(); S.limit = PAGE; changeView();
  }
  if (page === "calls") {
    const tab = params.get("tab");
    S.callView = tab && OUTCOMES.includes(tab) ? tab : "";
    S.callQ = params.get("q") || "";
    S.callLimit = PAGE;
    loadCalled();
  }
  for (const p of PAGES) $(`page-${p}`).hidden = p !== page;
  if (page !== shownPage) { shownPage = page; toTop(); }
  document.title = `${TITLES[page]} · ${SITE}`;
  for (const side of ["customers", "settings"]) {
    const link = $(`${side}-link`);
    if (!link) continue;
    link.classList.toggle("on", page === side);
    if (page === side) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  $all("nav a").forEach((a) => {
    a.classList.toggle("on", a.dataset.page === page);
    if (a.dataset.page === page) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  if (page === "leads" || page === "calls") writeHash(page);
  retryLater();                               // only the page shown tries again by itself
  if (page === "stats") loadStats();
  if (page === "map") loadMap();
  if (page === "customers") loadCustomers();
  if (page === "find" || page === "admin") loadSearches();
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
    if (S.sort !== "score" || S.dir !== "desc") {
      params.set("sort", S.sort); params.set("dir", S.dir);
    }
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
  if (e.detail > 1 && (Date.now() - dialogClickAt < 800 || eventEl(e).closest("dialog button"))) {
    e.preventDefault();
  }
}, true);
document.addEventListener("dblclick", () => {
  if (Date.now() - dialogClickAt < 800) window.getSelection()?.removeAllRanges();
}, true);
