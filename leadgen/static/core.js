/* The Lead Finder page, part 1 of 6: shared helpers, the colour theme and page switching.
   One HTML page (templates/index.html) holds Find leads, Leads, Calls and Stats, switched by
   the #address; these scripts load in order and share their top-level names. */
const $ = (id) => document.getElementById(id);
const CONFIG = JSON.parse($("page-config").textContent);
const OUTCOMES = CONFIG.outcomes;
const PAUSED = CONFIG.paused;
const SITE = "Arco Compactor Lead Finder";
const TITLES = { find: "Find leads", leads: "Leads", calls: "Calls", stats: "Stats" };
// A row just marked Yes or No stays where it is (showing its answer and Undo) for this long
// after the pointer leaves the table, so a double-click can never land on the next business.
const PIN_MS = 5000;
// Rows the server sends at a time (web/leads.py PAGE_SIZE); "Show more" asks for the next ones.
const PAGE = 100;
// Clicks this soon after rows moved up are ignored (they were aimed at the row that left).
const SHIFT_GUARD_MS = 700;
// The Leads page holds only the rows it shows (one tab, filtered and sorted by the server,
// the first `limit` of them), every tab's count, and the leads that can still be undone.
const S = { leads: [], total: 0, counts: null, recent: [], called: [], calledLoaded: false, calledError: "", sending: new Map(),
            loaded: false, loadError: "", refreshError: "", viewLoading: false, seq: 0,
            leadView: "", callView: "", callQ: "", q: "", tier: "", phone: false, sort: "score", dir: "desc", limit: PAGE,
            job: null, error: "", skew: 0, busy: 0, since: 0, pinned: new Map(), shiftedAt: 0, cutOffTimer: 0 };

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
function link(href, text) {
  if (!href || !/^https?:\/\//i.test(href)) return el("span", "");
  const a = el("a", text || href.replace(/^https?:\/\/(www\.)?/i, "").replace(/\/$/, ""));
  a.href = href; a.target = "_blank"; a.rel = "noopener";
  return a;
}
function phoneLink(phone) {
  const digits = (phone || "").replace(/[^\d+]/g, "");
  if (digits.replace(/\D/g, "").length < 7) return el("span", phone || "");
  const a = el("a", phone); a.href = `tel:${digits}`; a.className = "nowrap";
  return a;
}
function button(text, cls, onClick) {
  const b = el("button", text, cls);
  b.type = "button";
  if (onClick) b.addEventListener("click", onClick);
  return b;
}
async function api(path, opts) {
  let res;
  try { res = await fetch(path, opts); }
  catch (err) { throw Object.assign(new Error("Can't reach the Lead Finder. Check your internet connection and try again."), { status: 0 }); }
  if (res.status === 401) { location.href = "/login"; throw new Error("Logged out"); }
  const body = await res.json().catch(() => ({}));
  if (typeof body.now === "number") S.skew = body.now - Date.now() / 1000;
  if (!res.ok) {
    const e = new Error(body.error || "Something went wrong. Try again in a minute.");
    e.status = res.status; e.body = body; throw e;
  }
  return body;
}
const post = (path, data) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
const fmtTime = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const serverNow = () => Date.now() / 1000 + S.skew;
const leftOf = (undo) => undo ? Math.max(0, Math.floor(undo.until - serverNow())) : 0;
function problem(box, title, message, retry) {
  const p = el("div", undefined, "problem");
  const text = el("div"); text.append(el("strong", title), el("span", message, "muted"));
  p.append(text);
  if (retry) p.append(button("Retry", "quiet", retry));
  box.replaceChildren(p);
}

function themeChoice() { try { return localStorage.getItem("theme") || "system"; } catch (e) { return "system"; } }
function applyTheme(choice) {
  if (choice === "light" || choice === "dark") document.documentElement.setAttribute("data-theme", choice);
  else document.documentElement.removeAttribute("data-theme");
  try { if (choice === "system") localStorage.removeItem("theme"); else localStorage.setItem("theme", choice); } catch (e) { /* not kept */ }
  document.querySelectorAll("[data-theme-pick]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.themePick === choice));
  window.setThemeColor(choice);              // the browser's bar matches (templates/_theme.html)
  if (chartRows && !$("page-stats").hidden) drawChart(chartRows);
}
document.querySelectorAll("[data-theme-pick]").forEach((b) => b.addEventListener("click", () => applyTheme(b.dataset.themePick)));
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (chartRows) drawChart(chartRows); });

/* Who is using this browser: the name saved with each Yes / No and call made here, so the
   team can see who to ask. Kept in this browser; asked once before the first mark or call. */
const NAME_KEY = "my-name";
let nameMemory = "", nameThen = null;
function myName() {
  try { return localStorage.getItem(NAME_KEY) || nameMemory; } catch (e) { return nameMemory; }
}
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
function askName(then) {
  nameThen = then || null;
  $("name-input").value = myName();
  $("name-error").hidden = true;
  $("name-dlg").showModal();
  $("name-input").focus();
}
// Runs fn once the name is known: every mark and call says who made it, so there is no skipping.
function withName(fn) { if (myName()) fn(); else askName(fn); }
$("name-form").addEventListener("submit", (e) => {
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
const byWho = (name) => name ? ` by ${name}` : "";

// Phones fold the Yelp count, the colour theme and Log out into a Menu button, so the
// navigation stays one short bar at the top.
$("menu-btn").addEventListener("click", () => {
  const open = document.querySelector(".side").classList.toggle("open");
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
function readLeadView(params) {
  const before = [S.leadView, S.q, S.tier, S.phone, S.sort, S.dir].join("|");
  S.leadView = params.get("tab") || "";
  if (!LEAD_TABS.some(([v]) => v === S.leadView)) S.leadView = "";
  S.q = params.get("q") || ""; S.tier = params.get("tier") || ""; S.phone = params.get("phone") === "1";
  S.sort = SORTS[params.get("sort")] ? params.get("sort") : "score";
  S.dir = params.get("dir") === "asc" ? "asc" : params.get("dir") === "desc" ? "desc" : SORTS[S.sort].dir;
  $("filter").value = S.q; $("tier").value = S.tier; $("has-phone").checked = S.phone;
  return before !== [S.leadView, S.q, S.tier, S.phone, S.sort, S.dir].join("|");
}
function route() {
  const { page: asked, params } = parseHash();
  const page = ["find", "leads", "calls", "stats"].includes(asked) ? asked : (S.counts && S.counts.all ? "leads" : "find");
  if (typeof hideToast === "function") hideToast();   // a note about one page never covers the next (leads.js may not be loaded yet)
  if (page === "leads" && params.toString() && readLeadView(params)) { S.pinned.clear(); S.limit = PAGE; changeView(); }
  if (page === "calls") {
    const tab = params.get("tab");
    S.callView = tab && OUTCOMES.includes(tab) ? tab : "";
    S.callQ = params.get("q") || "";
    loadCalled();
  }
  for (const p of ["find", "leads", "calls", "stats"]) $(`page-${p}`).hidden = p !== page;
  document.title = `${TITLES[page]} · ${SITE}`;
  document.querySelectorAll("nav a").forEach((a) => {
    a.classList.toggle("on", a.dataset.page === page);
    if (a.dataset.page === page) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  if (page === "leads" || page === "calls") writeHash(page);
  if (page === "stats") loadStats();
  if (page === "find") loadSearches();
}
function writeHash(page) {
  const params = new URLSearchParams();
  if (page === "leads") {
    if (S.leadView) params.set("tab", S.leadView);
    if (S.q) params.set("q", S.q);
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
document.addEventListener("click", (e) => { if (e.target.closest("dialog button")) dialogClickAt = Date.now(); }, true);
document.addEventListener("mousedown", (e) => {
  if (e.detail > 1 && (Date.now() - dialogClickAt < 800 || e.target.closest("dialog button"))) e.preventDefault();
}, true);
document.addEventListener("dblclick", () => {
  if (Date.now() - dialogClickAt < 800) window.getSelection().removeAllRanges();
}, true);
