/* Part 2: the Leads page (Yes / No marks, undo, keeping up with colleagues).
   The server filters, sorts and pages the list: the page holds only the rows it shows. */
// The names this part shares with the others (eslint.config.mjs reads this list).
/* exported loadLeads, changeView, updateLead, addRecent, renderAll, counts, LEAD_TABS, SORTS, tabs, toast,
   hideToast, leadByKey, undoCall, undoButton */
function viewQuery() {
  const p = new URLSearchParams({ tab: S.leadView || "unchecked", sort: S.sort, dir: S.dir });
  if (S.q) p.set("q", S.q);
  if (S.tier) p.set("tier", S.tier);
  if (S.phone) p.set("phone", "1");
  return p;
}
async function loadLeads(quiet) {
  const seq = ++S.seq, wrap = $("leads-wrap"), top = wrap.scrollTop;
  const p = viewQuery();
  p.set("limit", String(S.limit));
  if (S.pinned.size) p.set("keep", [...S.pinned.keys()].join(","));   // rows just marked stay put
  try {
    const body = await api(`/leads?${p}`);
    if (seq !== S.seq) return;                     // a newer view was asked for meanwhile
    S.leads = body.leads; S.total = body.total; S.counts = body.counts; S.recent = body.recent;
    S.since = body.now; S.loaded = true; S.loadError = ""; S.refreshError = "";
    for (const [key, value] of S.sending) updateLead({ key, saving: value });   // still being saved
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
  p.set("since", String(S.since));
  p.set("limit", String(S.limit));
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
  for (const list of [S.leads, S.called, S.recent]) {
    for (const l of list) if (l.key === data.key) Object.assign(l, data);
  }
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
  const show = (id, n, word) => {
    $(id).replaceChildren(n.toLocaleString(), el("span", ` ${word}`, "cw")); $(id).hidden = !S.counts;
  };
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
  const toggle = ctl(button("Why this score", "link why-toggle", () => {
    td.classList.toggle("open");
    toggle.setAttribute("aria-expanded", td.classList.contains("open"));
  }), "why");
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
// Just-marked rows stay while the mouse is over the list (or keyboard focus is in it), and PIN_MS after.
// The hover is read from the page itself, so no pointer event can be missed.
let pointerKind = "mouse";
document.addEventListener("pointermove", (e) => { pointerKind = e.pointerType; }, true);
document.addEventListener("pointerdown", (e) => { pointerKind = e.pointerType; }, true);
// A press in progress (button down, not yet released) also holds them: rows never move between
// the press and the release of one click.
let pointerDown = false;
document.addEventListener("pointerdown", () => { pointerDown = true; }, true);
for (const type of ["pointerup", "pointercancel"]) {
  document.addEventListener(type, () => { pointerDown = false; }, true);
}
// Keyboard focus in the list holds them too (a button a mouse click left focused does not).
function holdingRows() {
  const wrap = $("leads-wrap"), active = document.activeElement;
  return pointerDown || (pointerKind === "mouse" && wrap.matches(":hover"))
    || (wrap.contains(active) && (active === wrap || active.matches(":focus-visible")));
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
  // Clearing is only via Undo; clicking the current answer only confirms a lead whose buildings disagreed.
  if ((value === before && !lead.marks_disagreed) || lead.saving || !lead.key || !lead.prospect) return;
  if (Date.now() - S.shiftedAt < SHIFT_GUARD_MS) {
    // The rows had just moved up: the click may have been aimed at the business that left. It is
    // not saved, and the page says so (never a click that silently does nothing).
    toast(`Nothing was saved for ${lead.name}: the list moved just as you clicked. Check the row, then click ` +
          `${value === "yes" ? "Yes" : "No"} again.`, null, true);
    return;
  }
  const key = lead.key;
  // Keep the row where it is instead of letting the next one slide under the pointer.
  if (S.leadView !== "all") S.pinned.set(key, Date.now() + PIN_MS);
  // Until the server has saved it the row says "Saving…" (Yes / No disabled); the answer, "Marked
  // by" and "Saved. Moves to …" appear only once it confirms. Every copy of the lead shows it.
  S.busy++; S.sending.set(key, value);
  updateLead({ key, saving: value });
  renderAll();
  try {
    const { undo } = await post("/mark", { key, value, by: myName() });
    if (S.leadView !== "all") S.pinned.set(key, Date.now() + PIN_MS);   // "Saved. Moves to …" gets its moment
    updateLead({ key, saving: "", has_baler: value, marked_by: myName(), marks_disagreed: false, undo_mark: undo });
    moveCount(lead, before, value);
    addRecent(leadByKey(key) || lead);
    renderAll();
    toast(`${lead.name}: marked ${value === "yes" ? "Yes" : "No"}.`, () => undoMark(key));
  } catch (err) {
    updateLead({ key, saving: "" });
    renderAll();
    toast(`${lead.name}: answer not saved. ${err.message}`, null, true);
  } finally { S.sending.delete(key); if (lead.saving) lead.saving = ""; S.busy--; }
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
  $all("button.undo[data-until]").forEach((b) => {
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
    td.append(el("div", lead.lead_type === "Competitor" ? "Competitor: not a prospect" : "Arco's own listing",
                 "sub not-asked"));
    return td;
  }
  const box = el("div", undefined, "mark");
  for (const [value, label] of [["yes", "Yes"], ["no", "No"]]) {
    const b = button(label, value + (lead.has_baler === value ? " on" : "") + (lead.saving === value ? " sending" : ""),
                     () => withName(() => setMark(lead, value)));
    // While saving the buttons only look disabled: a disabled button would lose the keyboard focus.
    b.disabled = !lead.key;
    if (lead.saving) b.setAttribute("aria-disabled", "true");
    b.dataset.ctl = value;
    b.setAttribute("aria-pressed", lead.has_baler === value);
    b.setAttribute("aria-label", `${lead.name}: ${value === "yes" ? "has" : "doesn't have"} a baler or compactor`);
    box.append(b);
  }
  td.append(box);
  if (lead.saving) {
    const note = el("div", `Saving ${lead.saving === "yes" ? "Yes" : "No"}…`, "sub saving");
    note.setAttribute("role", "status");
    td.append(note);
  }
  if (lead.has_baler && lead.marked_by && !lead.saving) td.append(el("div", `Marked by ${lead.marked_by}`, "sub by"));
  // Joined from buildings marked Yes and No (python -m leadgen merge-sites keeps Yes).
  if (lead.marks_disagreed) {
    td.append(el("div", "Its buildings were marked differently (Yes and No), so it was kept as Yes. " +
                        "Check with the business, then press Yes or No to confirm.", "flag by"));
  }
  // Marked more than once (changed, or merged from listings marked apart): the earlier marks.
  if (lead.has_baler && lead.mark_clicks > 1) {
    const more = el("div", undefined, "sub by");
    const earlier = button("Earlier marks", "link", () => openHistory(lead)); earlier.dataset.ctl = "earlier";
    more.append(earlier);
    td.append(more);
  }
  if (pinnedNow(lead.key) && !inTab(lead) && !lead.saving) {
    const tab = LEAD_TABS.find(([v]) => v === (lead.has_baler || ""));
    td.append(el("div", `Saved. Moves to “${tab ? tab[1] : "All"}”.`, "sub moved"));
  }
  if (lead.undo_mark && leftOf(lead.undo_mark) > 0) {
    td.append(ctl(undoButton(lead.undo_mark, "Undo", () => undoMark(lead.key)), "undo"));
  }
  if (withCall) td.append(callBox(lead));
  return td;
}
// Names a row's control, so keyboard focus can come back to it when the list is drawn again.
function ctl(node, name) { node.dataset.ctl = name; return node; }
function callBox(lead) {
  const box = el("div", undefined, "call-cell");
  if (lead.call_outcome) {
    // The latest call on one line (outcome, date, who), the buttons side by side below it.
    const last = el("div", undefined, "call-last");
    last.append(el("span", lead.call_outcome, "badge"), " ",
                el("span", lead.last_call + byWho(lead.last_call_by), "sub"));
    box.append(last);
  }
  box.append(ctl(button("Just called", "", () => openCall(lead)), "call"));
  if (lead.call_count) {
    box.append(ctl(button(`History (${lead.call_count})`, "quiet", () => openHistory(lead)), "history"));
  }
  if (lead.undo_call && leftOf(lead.undo_call) > 0) {
    box.append(ctl(undoButton(lead.undo_call, "Undo call", () => undoCall(lead.key)), "undo-call"));
  }
  return box;
}

function leadRow(l, withCall) {
  const tr = el("tr");
  tr.dataset.key = l.key;
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
  if (l.lead_type !== "Prospect" && !saysTheSame(l.lead_type, l.category)) name.append(el("div", l.lead_type, "sub"));
  for (const f of l.flags) name.append(el("div", f, "flag"));
  tr.append(name);
  const contact = el("td", undefined, "contact");
  // A lead without a street address says so (like "No phone listed"), next to its map link.
  // One without a city shows the town its map position is near ("near West Jordan, UT 84088",
  // worked out by the server), so same-named stores can be told apart.
  const addr = el("div");
  const add = (node) => addr.append(addr.childNodes.length ? " · " : "", node);
  if (!l.address) add(el("span", "No street address", "sub"));
  const place = [l.address, l.city, l.zip].filter(Boolean).join(", ");
  if (place) add(place);
  if (l.near) {
    const near = el("span", l.near, "near");
    near.title = "Worked out from the map position (the listing gives no town): the nearest town and ZIP code area.";
    add(near);
  }
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
  // One decimal for every distance ("4.6", "2.2"), so the column reads evenly.
  if (l.distance != null) { miles.append(Number(l.distance).toFixed(1)); miles.append(el("span", " mi", "unit")); }
  tr.append(miles);
  tr.append(whyCell(l.reasons));
  return tr;
}

// True when a second label only repeats the first in other words or order
// ("Waste / recycling facility" under "Recycling / waste facility").
function saysTheSame(extra, shown) {
  const words = (t) => new Set((t || "").toLowerCase().match(/[a-z]+/g) || []);
  const have = words(shown);
  return [...words(extra)].every((w) => have.has(w));
}

function sortHeader(label, sort) {
  const th = el("th");
  if (!sort) { th.textContent = label; return th; }
  const on = S.sort === sort;
  const b = button("", "sort", () => {
    if (S.sort === sort) S.dir = S.dir === "asc" ? "desc" : "asc";
    else { S.sort = sort; S.dir = SORTS[sort].dir; }
    S.limit = PAGE; unpinAll(); changeView();
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
  const cols = [[withCall ? "Baler? / Call" : "Baler or compactor?", withCall ? "176px" : "118px"],
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

// "Pro Baler, Action Compaction or Arco Compactor".
function orList(names) {
  return names.length < 2 ? names.join("") : `${names.slice(0, -1).join(", ")} or ${names[names.length - 1]}`;
}
function clearFilters() {
  S.q = ""; S.tier = ""; S.phone = false; $("filter").value = ""; $("tier").value = ""; $("has-phone").checked = false;
  S.limit = PAGE; unpinAll(); changeView();
  $("filter").focus();
}
function pickTab(v) { S.leadView = v; S.limit = PAGE; unpinAll(); changeView(); }
function renderLeads() {
  const problemBox = $("leads-problem");
  if (S.loadError && !S.loaded) {
    problem(problemBox, "Can't show your leads right now", S.loadError, () => {
      problemBox.replaceChildren(el("div", "Loading...", "muted")); loadLeads();
    });
    $("leads-body").hidden = true;
    return;
  }
  problemBox.replaceChildren(); $("leads-body").hidden = !S.loaded;
  $("leads-note").replaceChildren(...(S.refreshError ? [el("div", S.refreshError, "warn")] : []));
  const c = S.counts || {};
  const tabItems = LEAD_TABS.map(([v, t]) => [v, t, c[v || "unchecked"] || 0]);
  tabs($("lead-tabs"), tabItems, S.leadView, pickTab);
  $("tab-pick").replaceChildren(...tabItems.map(([v, t, n]) => {
    const o = el("option", `${t}: ${n.toLocaleString()}`); o.value = v; return o;
  }));
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
        "Run today's search on the Find leads page: the businesses it finds are saved here.", "#find",
        "Go to Find leads"));
    } else if (S.q || S.tier || S.phone) {
      // Say what was filtered, and offer the way back to the whole list.
      const tab = (LEAD_TABS.find(([v]) => v === S.leadView) || ["", "All"])[1];
      const what = [S.q && `“${S.q}”`, S.tier && `tier ${S.tier} (${TIER_WORDS[S.tier]})`,
                    S.phone && "a phone number"].filter(Boolean).join(" and ");
      const box = el("div", undefined, "empty");
      box.append(el("strong", `No leads match ${what} in ${tab}.`),
                 el("p", "Check the spelling, pick another tab, or clear the filters to see the whole list."),
                 button("Clear filters", "quiet", clearFilters));
      wrap.replaceChildren(box);
    } else {
      wrap.replaceChildren(el("div", S.leadView === "" ? "Every business has been checked."
        : S.leadView === "closed" ? "No saved business has been reported closed for good."
        : S.leadView === "competitors"
          ? `No ${orList(CONFIG.flagged || [])} listings found in the saved leads yet. When a search finds one, ` +
            "it is listed here, flagged, and never asked Yes / No."
        : S.leadView === "yes" ? "No business has been marked Yes yet. Mark leads under Not checked."
        : S.leadView === "no" ? "No business has been marked No yet. Mark leads under Not checked."
        : "Nothing here yet.", "empty"));
    }
  } else {
    // Every prospect can be called (a call is how staff find out); competitors have no buttons.
    const spot = focusSpot();
    wrap.replaceChildren(leadTable(S.leads, true));
    restoreFocus(spot);
  }
  $("sort-pick").value = `${S.sort}:${S.dir}`;
  if (!$("sort-pick").value) $("sort-pick").value = "score:desc";
  const more = $("leads-more"), left = S.total - S.leads.length;
  more.hidden = left <= 0;
  more.disabled = S.viewLoading;
  more.textContent = `Show more (${left.toLocaleString()} left)`;
  if (!$("page-leads").hidden) writeHash("leads");
}
/* Keyboard focus survives the list being drawn again (after a mark, an undo, a call saved, a
   refresh): it goes back to the same business's same control, or, when that business left the
   view, to the next business's Yes. A focused element that was removed (an Undo that ran out)
   leaves the focus on the page itself; the spot it was in is remembered for that case. */
let lastSpot = null;
document.addEventListener("focusin", (e) => {
  const at = eventEl(e), tr = at.closest && /** @type {HTMLElement} */ (at.closest("#leads-wrap tr[data-key]"));
  lastSpot = tr ? { key: tr.dataset.key, ctl: at.dataset.ctl || "" } : null;
});
function focusSpot() {
  const wrap = $("leads-wrap"), active = /** @type {HTMLElement} */ (document.activeElement);
  let spot = null;
  if (active && active !== wrap && wrap.contains(active)) {
    const tr = /** @type {HTMLElement} */ (active.closest("tr[data-key]"));
    if (tr) spot = { key: tr.dataset.key, ctl: active.dataset.ctl || "" };
  } else if (!active || active === document.body) spot = lastSpot;
  if (!spot) return null;
  // The businesses after it, in case it leaves the view.
  const keys = [...wrap.querySelectorAll("tr[data-key]")].map((r) => r.dataset.key);
  const at = keys.indexOf(spot.key);
  return { ...spot, next: at < 0 ? [] : [...keys.slice(at + 1), ...keys.slice(0, at).reverse()] };
}
function rowControl(key, name) {
  const row = [...$("leads-wrap").querySelectorAll("tr[data-key]")].find((r) => r.dataset.key === key);
  if (!row) return null;
  return (name && row.querySelector(`[data-ctl="${name}"]`)) || row.querySelector('[data-ctl="yes"]')
    || row.querySelector('[data-ctl="call"]') || row.querySelector("button, a[href]");
}
function restoreFocus(spot) {
  if (!spot) return;
  let target = rowControl(spot.key, spot.ctl);
  for (const key of spot.next) { if (target) break; target = rowControl(key, "yes"); }
  if (target) target.focus({ preventScroll: true });
}

function renderRecent() {
  const items = [];
  for (const l of S.recent) {
    if (l.undo_mark && leftOf(l.undo_mark) > 0)
      items.push([l.undo_mark.until,
                  `${l.name}: marked ${l.has_baler === "yes" ? "Yes" : l.has_baler === "no" ? "No" : "Not checked"}` +
                  (l.has_baler ? byWho(l.marked_by) : ""),
                  undoButton(l.undo_mark, "Undo", () => undoMark(l.key))]);
    if (l.undo_call && leftOf(l.undo_call) > 0)
      items.push([l.undo_call.until, `${l.name}: call saved as ${l.call_outcome}${byWho(l.last_call_by)}`,
                  undoButton(l.undo_call, "Undo", () => undoCall(l.key))]);
  }
  items.sort((a, b) => b[0] - a[0]);
  $("recent").hidden = !items.length;
  // The latest two (one on a phone), so the list stays in view; the rest behind "Show all".
  const few = recentShown();
  const shown = S.recentOpen ? items : items.slice(0, few);
  $("recent-list").replaceChildren(...shown.map(([, text, b]) => {
    const li = el("li"); li.append(el("span", text), b); return li;
  }));
  const more = $("recent-more");
  more.hidden = items.length <= few;
  more.textContent = S.recentOpen ? "Show fewer" : `Show all ${items.length}`;
  more.setAttribute("aria-expanded", !!S.recentOpen);
}
const recentShown = () => (window.matchMedia("(max-width: 700px)").matches ? 1 : 2);
$("recent-more").addEventListener("click", () => { S.recentOpen = !S.recentOpen; renderRecent(); });
// The next page of the view, added under the rows already shown (only those rows are sent).
async function showMore() {
  const more = $("leads-more"), seq = S.seq;
  const p = viewQuery();
  // Rows just marked that stay put for a moment aren't part of the view's order.
  p.set("offset", String(S.leads.filter((l) => !(pinnedNow(l.key) && !inTab(l))).length));
  p.set("limit", String(PAGE));
  const hadFocus = document.activeElement === more;
  more.disabled = true; more.textContent = "Loading more...";
  let added = [];
  try {
    const body = await api(`/leads?${p}`);
    if (seq !== S.seq) return;                     // the view changed meanwhile
    const have = new Set(S.leads.map((l) => l.key));
    added = body.leads.filter((l) => !have.has(l.key));
    S.leads = S.leads.concat(added);
    S.limit = Math.max(S.limit, S.leads.length);
    S.total = body.total; S.counts = body.counts; S.recent = body.recent;
    S.refreshError = "";
  } catch (err) {
    if (err.status === 401) return;
    S.refreshError = `Couldn't show more leads. ${err.message}`;
  }
  renderAll();
  // The button keeps the focus (it was disabled while loading); once nothing is left to show it
  // goes, and the focus moves to the first business just added.
  if (hadFocus) {
    const first = added.length && rowControl(added[0].key, "yes");
    if (!more.hidden) more.focus(); else if (first) first.focus();
  }
}
$("leads-more").addEventListener("click", showMore);
let filterTimer = null;
$("filter").addEventListener("input", () => {
  S.q = $("filter").value; S.limit = PAGE; unpinAll();
  clearTimeout(filterTimer);
  filterTimer = setTimeout(changeView, 250);       // ask once typing pauses
});
// Cards (below 1100px wide) sort with this list: the table's column headers aren't shown there.
$("sort-pick").addEventListener("change", () => {
  [S.sort, S.dir] = $("sort-pick").value.split(":");
  S.limit = PAGE; unpinAll(); changeView();
});
$("tab-pick").addEventListener("change", () => pickTab($("tab-pick").value));
$("tier").addEventListener("change", () => { S.tier = $("tier").value; S.limit = PAGE; unpinAll(); changeView(); });
$("has-phone").addEventListener("change", () => {
  S.phone = $("has-phone").checked; S.limit = PAGE; unpinAll(); changeView();
});

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
    // The server names the file with the Utah date ("compactor-leads-2026-09-30.xlsx").
    const named = /filename="?([^";]+)"?/.exec(res.headers.get("Content-Disposition") || "");
    const save = el("a"); save.href = url; save.download = named ? named[1] : a.dataset.file;
    document.body.append(save); save.click(); save.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (err) {
    toast(`The download didn't work. ${err.message}`, null, true);
  } finally {
    delete a.dataset.busy; a.removeAttribute("aria-disabled"); a.classList.remove("busy");
    a.textContent = label;
  }
}
$all("a[data-download]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); download(a); }));

// Keep up with colleagues' marks and calls: every minute while the page is open, and on return
// to it. Only what changed comes back, so this stays small however long the list grows.
function refreshSoon() {
  if (document.visibilityState !== "visible" || S.busy || !S.loaded) return;
  if ($("call-dlg").open || $("hist-dlg").open) return;
  loadChanges();
}
setInterval(refreshSoon, 60000);
document.addEventListener("visibilitychange", refreshSoon);
