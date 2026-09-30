/* Part 2: the Leads page (Yes / No marks, undo, keeping up with colleagues).
   The server filters, sorts and pages the list: the page holds only the rows it shows. */
function viewQuery() {
  const p = new URLSearchParams({ tab: S.leadView || "unchecked", sort: S.sort, dir: S.dir });
  if (S.q) p.set("q", S.q);
  if (S.tier) p.set("tier", S.tier);
  return p;
}
async function loadLeads(quiet) {
  const seq = ++S.seq, wrap = $("leads-wrap"), top = wrap.scrollTop;
  const p = viewQuery();
  p.set("limit", S.limit);
  if (S.pinned.size) p.set("keep", [...S.pinned.keys()].join(","));   // rows just marked stay put
  try {
    const body = await api(`/leads?${p}`);
    if (seq !== S.seq) return;                     // a newer view was asked for meanwhile
    S.leads = body.leads; S.total = body.total; S.counts = body.counts; S.recent = body.recent;
    S.since = body.now; S.loaded = true; S.loadError = ""; S.refreshError = "";
  } catch (err) {
    if (seq !== S.seq || err.status === 401) return;
    if (S.loaded) S.refreshError = quiet ? "Couldn't refresh the list just now; it will try again shortly."
                                         : `Couldn't show this view. ${err.message}`;
    else S.loadError = err.message;
  }
  S.viewLoading = false;
  renderAll();
  wrap.scrollTop = top;
}
// A new tab, filter, tier or sort: the old rows stay (dimmed) until the new ones arrive.
function changeView() {
  S.viewLoading = true;
  renderLeads();
  loadLeads();
}
// Only what changed since the last look (colleagues' marks and calls, a finished search).
async function loadChanges() {
  if (!S.since) return loadLeads(true);
  const seq = S.seq, wrap = $("leads-wrap"), top = wrap.scrollTop;
  const p = viewQuery();
  p.set("since", S.since);
  p.set("limit", S.limit);
  if (S.pinned.size) p.set("keep", [...S.pinned.keys()].join(","));
  try {
    const body = await api(`/leads?${p}`);
    if (seq !== S.seq) return;
    // More changed than this page shows (a search touched them all): fetch the page's rows again.
    if (body.reload) { if (!$("page-calls").hidden) loadCalled(); else S.calledLoaded = false; return loadLeads(true); }
    S.since = body.now; S.refreshError = "";
    if (!body.leads.length && !body.removed.length) { renderLeads(); return; }
    let missing = false;
    for (const l of body.leads) {
      const inView = l.in_view; delete l.in_view;
      const old = leadByKey(l.key);
      if (old && old.saving) continue;               // this page's own click is still being saved
      const shown = S.leads.some((x) => x.key === l.key);
      updateLead(l);
      if (shown && !inView && !pinnedNow(l.key)) S.leads = S.leads.filter((x) => x.key !== l.key);
      if (!shown && inView) missing = true;
    }
    const gone = new Set(body.removed);
    S.leads = S.leads.filter((l) => !gone.has(l.key));
    S.called = S.called.filter((l) => !gone.has(l.key));
    S.total = body.total; S.counts = body.counts; S.recent = body.recent;
    if (!$("page-calls").hidden) loadCalled(); else S.calledLoaded = false;
    // A business that now belongs in this view: fetch the view again (it is one page of rows).
    if (missing) return loadLeads(true);
  } catch (err) {
    if (err.status === 401) return;
    S.refreshError = "Couldn't refresh the list just now; it will try again shortly.";
  }
  renderAll();
  wrap.scrollTop = top;
}
// Every copy of a lead (the Leads rows, the Calls page, Recent changes) gets the new values.
function updateLead(data) {
  for (const list of [S.leads, S.called, S.recent]) for (const l of list) if (l.key === data.key) Object.assign(l, data);
}
function addRecent(lead) {
  S.recent = [lead, ...S.recent.filter((l) => l.key !== lead.key)];
}
// A Yes / No click moves a business between tabs; the counts follow at once. A business
// closed for good is never under Not checked (it has its own tab).
function moveCount(lead, from, to) {
  if (!S.counts) return;
  if (from || !lead.closed) S.counts[from || "unchecked"]--;
  if (to || !lead.closed) S.counts[to || "unchecked"]++;
}
function renderAll() { renderLeads(); renderCalls(); renderRecent(); counts(); }
function counts() {
  // The number, then its word (phones show just the number; the link's title says what it counts).
  const show = (id, n, word) => { $(id).replaceChildren(n.toLocaleString(), el("span", ` ${word}`, "cw")); $(id).hidden = !S.counts; };
  if (!S.counts) return;
  show("n-leads", S.counts.unchecked, "to check");
  show("n-calls", S.counts.called, "called");
}

// Competitors and Arco's own listing have their own tab: they are flagged, never asked Yes / No.
// Businesses a later search found closed for good keep their mark (and stay under Yes or No,
// flagged); the Closed tab lists them all, and they leave Not checked.
const LEAD_TABS = [["", "Not checked"], ["yes", "Has baler or compactor"], ["no", "No baler or compactor"],
                   ["competitors", "Competitors"], ["closed", "Closed"], ["all", "All"]];
// Column sorts (done by the server) and the first direction a click picks.
// What each tier means (the tier list's words).
const TIER_WORDS = { A: "strong", B: "likely", C: "possible", D: "weak" };
const SORTS = { score: { dir: "desc" }, name: { dir: "asc" }, city: { dir: "asc" }, miles: { dir: "asc" } };

function tabs(box, items, current, onPick) {
  box.replaceChildren();
  for (const [value, label, n] of items) {
    const b = button(`${label} (${n.toLocaleString()})`, value === current ? "on" : "", () => onPick(value));
    b.setAttribute("aria-pressed", value === current);
    box.append(b);
  }
}

// "+35 Grocery / supermarket (by Yelp category): Grocery stores bale..." -> parts
function parseReason(r) {
  const m = /^([+-]\d+)\s+(.*)$/.exec(r);
  if (!m) return { pts: null, title: r, note: "" };
  let title = m[2], note = "";
  const by = /^(.*?) \(by ([^)]+)\): (.*)$/.exec(title);
  if (by) { title = by[1]; note = `${by[3]} (matched by ${by[2]}).`; }
  return { pts: parseInt(m[1], 10), title: title.charAt(0).toUpperCase() + title.slice(1), note };
}
function whyCell(reasons) {
  const td = el("td", undefined, "c-why");
  // On a phone the reasons sit behind this toggle (each lead is a card there); wider screens show them.
  const toggle = button("Why this score", "link why-toggle", () => {
    td.classList.toggle("open");
    toggle.setAttribute("aria-expanded", td.classList.contains("open"));
  });
  toggle.setAttribute("aria-expanded", "false");
  td.append(toggle);
  const list = el("div", undefined, "reasons");
  const notes = [];
  for (const r of reasons) {
    const p = parseReason(r);
    if (p.pts === null) { notes.push(p.title.charAt(0).toUpperCase() + p.title.slice(1) + "."); continue; }
    const row = el("div", undefined, "reason");
    row.title = r;
    row.append(el("span", (p.pts > 0 ? "+" : "") + p.pts, "pts" + (p.pts < 0 ? " neg" : p.pts === 0 ? " zero" : "")));
    row.append(el("span", p.title));
    list.append(row);
    if (p.note) notes.unshift(p.note);
  }
  td.append(list);
  if (notes.length) {
    const d = el("details", undefined, "explain");
    d.append(el("summary", "Explain"));
    for (const n of notes) d.append(el("p", n));
    td.append(d);
  }
  return td;
}

/* The toast: a short note after a click (the Undo stays on the row and in Recent changes). */
let toastTimer = null, toastUndo = null;
function toast(message, undo, bad) {
  $("toast-msg").textContent = message;
  toastUndo = undo || null;
  $("toast-undo").hidden = !undo; $("toast-undo").disabled = false;
  $("toast").classList.toggle("bad", !!bad);
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, bad ? 9000 : 7000);
}
function hideToast() { clearTimeout(toastTimer); $("toast").hidden = true; toastUndo = null; }
$("toast-undo").addEventListener("click", () => { if (toastUndo) { const u = toastUndo; hideToast(); u(); } });
const leadByKey = (key) => [...S.leads, ...S.called, ...S.recent].find((l) => l.key === key);

const pinnedNow = (key) => S.pinned.has(key);
function unpinAll() { S.pinned.clear(); }
// Does the row belong in the tab shown (a row just marked may stay for a moment when it doesn't)?
const inTab = (l) => S.leadView === "all" || (S.leadView === "competitors" ? !l.prospect
  : S.leadView === "closed" ? l.closed
  : l.prospect && (l.has_baler || "") === S.leadView && !(l.closed && !S.leadView));
// Just-marked rows stay while the mouse is over the list (or focus is in it), and PIN_MS after.
// The hover is read from the page itself, so no pointer event can be missed.
let pointerKind = "mouse";
document.addEventListener("pointermove", (e) => { pointerKind = e.pointerType; }, true);
document.addEventListener("pointerdown", (e) => { pointerKind = e.pointerType; }, true);
function holdingRows() {
  const wrap = $("leads-wrap");
  return (pointerKind === "mouse" && wrap.matches(":hover")) || wrap.contains(document.activeElement);
}
setInterval(() => {
  if (!S.pinned.size) return;
  const now = Date.now();
  if (holdingRows()) { for (const key of S.pinned.keys()) S.pinned.set(key, now + PIN_MS); return; }
  let gone = false;
  for (const [key, until] of S.pinned) {
    if (until > now) continue;
    S.pinned.delete(key); gone = true;
    const lead = S.leads.find((l) => l.key === key);
    if (lead && !inTab(lead)) { S.leads = S.leads.filter((l) => l !== lead); S.total--; }
  }
  if (gone) { S.shiftedAt = now; renderLeads(); }
}, 250);

async function setMark(lead, value) {
  const before = lead.has_baler || "";
  if (value === before || lead.saving || !lead.key || !lead.prospect) return;   // clearing is only via Undo
  if (Date.now() - S.shiftedAt < SHIFT_GUARD_MS) return;     // aimed at a row that just left
  const key = lead.key;
  // Keep the row where it is (with its new answer) instead of letting the next one slide under the pointer.
  if (S.leadView !== "all") S.pinned.set(key, Date.now() + PIN_MS);
  lead.saving = true; S.busy++;
  const beforeBy = lead.marked_by || "";
  updateLead({ key, has_baler: value, marked_by: myName() }); moveCount(lead, before, value);
  renderAll();
  try {
    const { undo } = await post("/mark", { key, value, by: myName() });
    updateLead({ key, undo_mark: undo });
    addRecent(leadByKey(key) || lead);
    renderAll();
    toast(`${lead.name}: marked ${value === "yes" ? "Yes" : "No"}.`, () => undoMark(key));
  } catch (err) {
    updateLead({ key, has_baler: before, marked_by: beforeBy }); moveCount(lead, value, before);
    renderAll();
    toast(`${lead.name}: answer not saved. ${err.message}`, null, true);
  } finally { lead.saving = false; S.busy--; }
}
async function undoMark(key) {
  const lead = leadByKey(key);
  if (!lead || !lead.undo_mark) return;
  try { await post("/mark/undo", { id: lead.undo_mark.id }); toast(`${lead.name}: undone.`); }
  catch (err) { toast(`Couldn't undo ${lead.name}. ${err.message}`, null, true); }
  await loadChanges();
}
async function undoCall(key) {
  const lead = leadByKey(key);
  if (!lead || !lead.undo_call) return;
  try { await post("/calls/undo", { id: lead.undo_call.id }); toast(`${lead.name}: call removed.`); }
  catch (err) { toast(`Couldn't undo the call to ${lead.name}. ${err.message}`, null, true); }
  await loadChanges();
}
function undoButton(undo, label, onClick) {
  const b = button("", "undo", onClick);
  b.dataset.until = undo.until; b.dataset.label = label;
  b.textContent = `${label} (${fmtTime(leftOf(undo))})`;
  return b;
}
// Count the Undo buttons down; when one runs out, it disappears.
setInterval(() => {
  let expired = false;
  document.querySelectorAll("button.undo[data-until]").forEach((b) => {
    const left = Math.floor(Number(b.dataset.until) - serverNow());
    if (left <= 0) { b.remove(); expired = true; } else b.textContent = `${b.dataset.label} (${fmtTime(left)})`;
  });
  if (expired) {
    for (const l of [...S.leads, ...S.called, ...S.recent]) {
      if (l.undo_mark && leftOf(l.undo_mark) <= 0) l.undo_mark = null;
      if (l.undo_call && leftOf(l.undo_call) <= 0) l.undo_call = null;
    }
    S.recent = S.recent.filter((l) => l.undo_mark || l.undo_call);
    renderRecent();
  }
}, 1000);

function markCell(lead, withCall) {
  const td = el("td", undefined, "c-mark");
  if (!lead.prospect) {
    td.append(el("div", lead.lead_type === "Competitor" ? "Competitor: not a prospect" : "Arco's own listing", "sub not-asked"));
    return td;
  }
  const box = el("div", undefined, "mark");
  for (const [value, label] of [["yes", "Yes"], ["no", "No"]]) {
    const b = button(label, value + (lead.has_baler === value ? " on" : ""), () => withName(() => setMark(lead, value)));
    b.disabled = !lead.key;
    b.setAttribute("aria-pressed", lead.has_baler === value);
    b.setAttribute("aria-label", `${lead.name}: ${value === "yes" ? "has" : "doesn't have"} a baler or compactor`);
    box.append(b);
  }
  td.append(box);
  if (lead.has_baler && lead.marked_by) td.append(el("div", `Marked by ${lead.marked_by}`, "sub by"));
  if (pinnedNow(lead.key) && !inTab(lead)) {
    const tab = LEAD_TABS.find(([v]) => v === (lead.has_baler || ""));
    td.append(el("div", `Saved. Moves to “${tab ? tab[1] : "All"}”.`, "sub moved"));
  }
  if (lead.undo_mark && leftOf(lead.undo_mark) > 0) td.append(undoButton(lead.undo_mark, "Undo", () => undoMark(lead.key)));
  if (withCall) td.append(callBox(lead));
  return td;
}
function callBox(lead) {
  const box = el("div", undefined, "call-cell");
  if (lead.call_outcome) {
    // The latest call on one line (outcome, date, who), the buttons side by side below it.
    const last = el("div", undefined, "call-last");
    last.append(el("span", lead.call_outcome, "badge"), " ", el("span", lead.last_call + byWho(lead.last_call_by), "sub"));
    box.append(last);
  }
  box.append(button("Just called", "", () => openCall(lead)));
  if (lead.call_count) box.append(button(`History (${lead.call_count})`, "quiet", () => openHistory(lead)));
  if (lead.undo_call && leftOf(lead.undo_call) > 0) box.append(undoButton(lead.undo_call, "Undo call", () => undoCall(lead.key)));
  return box;
}

function leadRow(l, withCall) {
  const tr = el("tr");
  tr.className = l.tier + (l.lead_type === "Competitor" ? " competitor" : "")
    + (pinnedNow(l.key) && !inTab(l) ? " just-marked" : "");
  tr.append(markCell(l, withCall));
  // The tier's meaning is written next to the score (A strong, B likely, C possible, D weak).
  const word = TIER_WORDS[l.tier] || "";
  const score = el("td", String(l.score), "score nowrap");
  score.append(el("span", ` ${l.tier} ${word}`.trimEnd(), "tier-word"));
  score.title = `Score ${l.score} of 100, tier ${l.tier}${word ? `: ${word} to have a baler or compactor` : ""}`;
  tr.append(score);
  const name = el("td", undefined, "c-name");
  name.append(el("strong", l.name));
  if (l.category) name.append(el("div", l.category, "sub"));
  if (l.lead_type !== "Prospect") name.append(el("div", l.lead_type, "sub"));
  for (const f of l.flags) name.append(el("div", f, "flag"));
  tr.append(name);
  const contact = el("td", undefined, "contact");
  // A lead without a street address says so (like "No phone listed"), next to its map link.
  const addr = el("div", [l.address, l.city, l.zip].filter(Boolean).join(", "));
  if (!l.address) addr.prepend(el("span", "No street address", "sub"), l.city || l.zip ? " · " : "");
  if (l.map_url) {
    const map = link(l.map_url, "map"); map.classList.add("map"); map.setAttribute("aria-label", `${l.name} on a map`);
    addr.append(" ", map);
  }
  contact.append(addr);
  const p = el("div"); p.append(l.phone ? phoneLink(l.phone) : el("span", "No phone listed", "sub")); contact.append(p);
  if (l.website) {
    const a = link(l.website, l.website.replace(/^https?:\/\/(www\.)?/i, "").split("/")[0]);
    a.classList.add("site"); a.title = l.website; contact.append(a);
  }
  tr.append(contact);
  const miles = el("td", undefined, "c-miles nowrap");
  if (l.distance != null) { miles.append(String(l.distance)); miles.append(el("span", " mi", "unit")); }
  tr.append(miles);
  tr.append(whyCell(l.reasons));
  return tr;
}

function sortHeader(label, sort) {
  const th = el("th");
  if (!sort) { th.textContent = label; return th; }
  const on = S.sort === sort;
  const b = button("", "sort", () => {
    if (S.sort === sort) S.dir = S.dir === "asc" ? "desc" : "asc";
    else { S.sort = sort; S.dir = SORTS[sort].dir; }
    S.limit = 300; unpinAll(); changeView();
  });
  b.append(el("span", label), el("span", on ? (S.dir === "asc" ? "▲" : "▼") : "↕", "arrow"));
  th.setAttribute("aria-sort", on ? (S.dir === "asc" ? "ascending" : "descending") : "none");
  b.title = `Sort by ${label.toLowerCase()}`;
  th.append(b);
  return th;
}
function leadTable(rows, withCall) {
  const table = el("table", undefined, "leads");
  // Shown from 1100px wide (narrower windows show cards); the Why column takes the rest.
  const cols = [[withCall ? "Baler? / Call" : "Baler or compactor?", withCall ? "250px" : "118px"],
                ["Score", "74px", "score"], ["Business", "20%", "name"], ["Contact", "20%", "city"],
                ["Miles", "72px", "miles"], ["Why this score", null]];
  const cg = el("colgroup");
  for (const [, w] of cols) { const c = el("col"); if (w) c.style.width = w; cg.append(c); }
  const head = el("tr");
  for (const [t, , sort] of cols) head.append(sortHeader(t, sort));
  const thead = el("thead"); thead.append(head);
  const tbody = el("tbody");
  for (const l of rows) tbody.append(leadRow(l, withCall));
  table.append(cg, thead, tbody);
  return table;
}

function clearFilters() {
  S.q = ""; S.tier = ""; $("filter").value = ""; $("tier").value = "";
  S.limit = 300; unpinAll(); changeView();
  $("filter").focus();
}
function pickTab(v) { S.leadView = v; S.limit = 300; unpinAll(); changeView(); }
function renderLeads() {
  const problemBox = $("leads-problem");
  if (S.loadError && !S.loaded) {
    problem(problemBox, "Can't show your leads right now", S.loadError, () => { problemBox.replaceChildren(el("div", "Loading...", "muted")); loadLeads(); });
    $("leads-body").hidden = true;
    return;
  }
  problemBox.replaceChildren(); $("leads-body").hidden = !S.loaded;
  $("leads-note").replaceChildren(...(S.refreshError ? [el("div", S.refreshError, "warn")] : []));
  const c = S.counts || {};
  const tabItems = LEAD_TABS.map(([v, t]) => [v, t, c[v || "unchecked"] || 0]);
  tabs($("lead-tabs"), tabItems, S.leadView, pickTab);
  $("tab-pick").replaceChildren(...tabItems.map(([v, t, n]) => { const o = el("option", `${t}: ${n.toLocaleString()}`); o.value = v; return o; }));
  $("tab-pick").value = S.leadView;
  $("downloads").hidden = !c.all;                  // nothing to download yet
  $("call-hint").hidden = ["competitors", "closed"].includes(S.leadView) || !c.all;
  $("competitor-hint").hidden = S.leadView !== "competitors";
  $("closed-hint").hidden = S.leadView !== "closed";
  const wrap = $("leads-wrap");
  wrap.classList.toggle("stale", S.viewLoading);
  wrap.setAttribute("aria-busy", S.viewLoading);
  if (!S.leads.length) {
    if (!c.all) {
      wrap.replaceChildren(emptyNote("No saved leads yet.",
        "Run today's search on the Find leads page: the businesses it finds are saved here.", "#find", "Go to Find leads"));
    } else if (S.q || S.tier) {
      // Say what was filtered, and offer the way back to the whole list.
      const tab = (LEAD_TABS.find(([v]) => v === S.leadView) || ["", "All"])[1];
      const what = [S.q && `“${S.q}”`, S.tier && `tier ${S.tier} (${TIER_WORDS[S.tier]})`].filter(Boolean).join(" and ");
      const box = el("div", undefined, "empty");
      box.append(el("strong", `No leads match ${what} in ${tab}.`),
                 el("p", "Check the spelling, pick another tab, or clear the filters to see the whole list."),
                 button("Clear filters", "quiet", clearFilters));
      wrap.replaceChildren(box);
    } else {
      wrap.replaceChildren(el("div", S.leadView === "" ? "Every business has been checked."
        : S.leadView === "closed" ? "No saved business has been reported closed for good." : "Nothing here yet.", "empty"));
    }
  } else {
    // Every prospect can be called (a call is how staff find out); competitors have no buttons.
    wrap.replaceChildren(leadTable(S.leads, true));
  }
  $("sort-pick").value = `${S.sort}:${S.dir}`;
  if (!$("sort-pick").value) $("sort-pick").value = "score:desc";
  const more = $("leads-more"), left = S.total - S.leads.length;
  more.hidden = left <= 0;
  more.disabled = S.viewLoading;
  more.textContent = `Show more (${left.toLocaleString()} left)`;
  if (!$("page-leads").hidden) writeHash("leads");
}
function renderRecent() {
  const items = [];
  for (const l of S.recent) {
    if (l.undo_mark && leftOf(l.undo_mark) > 0)
      items.push([l.undo_mark.until, `${l.name}: marked ${l.has_baler === "yes" ? "Yes" : l.has_baler === "no" ? "No" : "Not checked"}${l.has_baler ? byWho(l.marked_by) : ""}`,
                  undoButton(l.undo_mark, "Undo", () => undoMark(l.key))]);
    if (l.undo_call && leftOf(l.undo_call) > 0)
      items.push([l.undo_call.until, `${l.name}: call saved as ${l.call_outcome}${byWho(l.last_call_by)}`, undoButton(l.undo_call, "Undo", () => undoCall(l.key))]);
  }
  items.sort((a, b) => b[0] - a[0]);
  $("recent").hidden = !items.length;
  // The latest two (one on a phone), so the list stays in view; the rest behind "Show all".
  const few = recentShown();
  const shown = S.recentOpen ? items : items.slice(0, few);
  $("recent-list").replaceChildren(...shown.map(([, text, b]) => { const li = el("li"); li.append(el("span", text), b); return li; }));
  const more = $("recent-more");
  more.hidden = items.length <= few;
  more.textContent = S.recentOpen ? "Show fewer" : `Show all ${items.length}`;
  more.setAttribute("aria-expanded", !!S.recentOpen);
}
const recentShown = () => (window.matchMedia("(max-width: 700px)").matches ? 1 : 2);
$("recent-more").addEventListener("click", () => { S.recentOpen = !S.recentOpen; renderRecent(); });
$("leads-more").addEventListener("click", () => { S.limit += 300; S.viewLoading = true; renderLeads(); loadLeads(); });
let filterTimer = null;
$("filter").addEventListener("input", () => {
  S.q = $("filter").value; S.limit = 300; unpinAll();
  clearTimeout(filterTimer);
  filterTimer = setTimeout(changeView, 250);       // ask once typing pauses
});
// Cards (below 1100px wide) sort with this list: the table's column headers aren't shown there.
$("sort-pick").addEventListener("change", () => {
  [S.sort, S.dir] = $("sort-pick").value.split(":");
  S.limit = 300; unpinAll(); changeView();
});
$("tab-pick").addEventListener("change", () => pickTab($("tab-pick").value));
$("tier").addEventListener("change", () => { S.tier = $("tier").value; S.limit = 300; unpinAll(); changeView(); });

/* The downloads: building a big Excel file takes a moment, so the button says so and a second
   click doesn't start another. The server's answer is fetched and saved as a file. */
async function download(a) {
  if (a.dataset.busy) return;
  const label = a.textContent;
  a.dataset.busy = "1"; a.setAttribute("aria-disabled", "true"); a.classList.add("busy");
  a.textContent = a.dataset.busyLabel;
  try {
    const res = await fetch(a.href);
    if (res.status === 401) { location.href = "/login"; return; }
    if (!res.ok) {                                  // the server's error page says why, in plain words
      const page = new DOMParser().parseFromString(await res.text().catch(() => ""), "text/html");
      throw new Error((page.querySelector(".box p") || {}).textContent || "Try again in a minute.");
    }
    const url = URL.createObjectURL(await res.blob());
    const save = el("a"); save.href = url; save.download = a.dataset.file;
    document.body.append(save); save.click(); save.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (err) {
    toast(`The download didn't work. ${err.message}`, null, true);
  } finally {
    delete a.dataset.busy; a.removeAttribute("aria-disabled"); a.classList.remove("busy");
    a.textContent = label;
  }
}
document.querySelectorAll("a[data-download]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); download(a); }));

// Keep up with colleagues' marks and calls: every minute while the page is open, and on return
// to it. Only what changed comes back, so this stays small however long the list grows.
function refreshSoon() {
  if (document.visibilityState !== "visible" || S.busy || !S.loaded) return;
  if ($("call-dlg").open || $("hist-dlg").open) return;
  loadChanges();
}
setInterval(refreshSoon, 60000);
document.addEventListener("visibilitychange", refreshSoon);
