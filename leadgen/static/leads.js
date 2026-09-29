/* Part 2: the Leads page (Yes / No marks, undo, keeping up with colleagues). */
async function loadLeads(quiet) {
  const wrap = $("leads-wrap"), top = wrap.scrollTop;
  try {
    const body = await api("/leads");
    S.leads = body.leads; S.since = body.now;
    S.loaded = true; S.loadError = ""; S.refreshError = "";
  } catch (err) {
    if (err.status === 401) return;
    if (quiet && S.loaded) S.refreshError = "Couldn't refresh the list just now; it will try again shortly.";
    else S.loadError = err.message;
  }
  renderAll();
  wrap.scrollTop = top;
}
// Only what changed since the last look (colleagues' marks and calls, a finished search).
async function loadChanges() {
  if (!S.since) return loadLeads(true);
  const wrap = $("leads-wrap"), top = wrap.scrollTop;
  try {
    const body = await api(`/leads?since=${encodeURIComponent(S.since)}`);
    S.since = body.now; S.refreshError = "";
    if (!body.leads.length && !body.removed.length) { renderLeads(); return; }
    const byKey = new Map(S.leads.map((l) => [l.key, l]));
    for (const l of body.leads) {
      const old = byKey.get(l.key);
      if (old && old.saving) continue;               // this page's own click is still being saved
      byKey.set(l.key, old ? Object.assign(old, l) : l);
    }
    for (const key of body.removed) byKey.delete(key);
    S.leads = [...byKey.values()];
  } catch (err) {
    if (err.status === 401) return;
    S.refreshError = "Couldn't refresh the list just now; it will try again shortly.";
  }
  renderAll();
  wrap.scrollTop = top;
}
function renderAll() { renderLeads(); renderCalls(); renderRecent(); counts(); }
function counts() {
  const show = (id, text) => { $(id).textContent = text; $(id).hidden = !S.loaded; };
  const toCheck = S.leads.filter((l) => !l.has_baler).length;
  show("n-leads", `${toCheck.toLocaleString()} to check`);
  show("n-calls", `${S.leads.filter((l) => l.call_count).length.toLocaleString()} called`);
}

const LEAD_TABS = [["", "Not checked"], ["yes", "Has baler or compactor"], ["no", "No baler or compactor"], ["all", "All"]];
// Column sorts: the value to sort by, and the first direction a click picks.
const SORTS = {
  score: { key: (l) => l.score, dir: "desc" },
  name: { key: (l) => l.name.toLowerCase(), dir: "asc" },
  city: { key: (l) => (l.city || "~").toLowerCase(), dir: "asc" },
  miles: { key: (l) => l.distance ?? 1e9, dir: "asc" },
};

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
  const td = el("td");
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
  toastTimer = setTimeout(() => { $("toast").hidden = true; toastUndo = null; }, bad ? 9000 : 7000);
}
$("toast-undo").addEventListener("click", () => { if (toastUndo) { const u = toastUndo; toastUndo = null; $("toast").hidden = true; u(); } });
const leadByKey = (key) => S.leads.find((l) => l.key === key);

const pinnedNow = (key) => S.pinned.has(key);
function unpinAll() { S.pinned.clear(); }
// Let just-marked rows go once the pointer and focus have left the table for PIN_MS.
setInterval(() => {
  if (!S.pinned.size || S.inTable || $("leads-wrap").contains(document.activeElement)) return;
  const now = Date.now();
  let gone = false;
  for (const [key, until] of S.pinned) if (until <= now) { S.pinned.delete(key); gone = true; }
  if (gone) { S.shiftedAt = now; renderLeads(); }
}, 500);
$("leads-wrap").addEventListener("pointerenter", (e) => { if (e.pointerType === "mouse") S.inTable = true; });
$("leads-wrap").addEventListener("pointerleave", () => {
  S.inTable = false;
  const until = Date.now() + PIN_MS;
  for (const key of S.pinned.keys()) S.pinned.set(key, until);
});

async function setMark(lead, value) {
  const before = lead.has_baler || "";
  if (value === before || lead.saving || !lead.key) return;   // clearing is only via Undo
  if (Date.now() - S.shiftedAt < SHIFT_GUARD_MS) return;     // aimed at a row that just left
  const key = lead.key;
  // Keep the row where it is (with its new answer) instead of letting the next one slide under the pointer.
  if (S.leadView !== "all") S.pinned.set(key, Date.now() + PIN_MS);
  lead.saving = true; lead.has_baler = value; S.busy++;
  renderAll();
  try {
    const { undo } = await post("/mark", { key, value });
    const now = leadByKey(key) || lead;
    now.undo_mark = undo;
    renderAll();
    toast(`${lead.name}: marked ${value === "yes" ? "Yes" : "No"}.`, () => undoMark(key));
  } catch (err) {
    const now = leadByKey(key) || lead;
    now.has_baler = before; renderAll();
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
    for (const l of S.leads) {
      if (l.undo_mark && leftOf(l.undo_mark) <= 0) l.undo_mark = null;
      if (l.undo_call && leftOf(l.undo_call) <= 0) l.undo_call = null;
    }
    renderRecent();
  }
}, 1000);

function markCell(lead, withCall) {
  const td = el("td");
  const box = el("div", undefined, "mark");
  for (const [value, label] of [["yes", "Yes"], ["no", "No"]]) {
    const b = button(label, value + (lead.has_baler === value ? " on" : ""), () => setMark(lead, value));
    b.disabled = !lead.key;
    b.setAttribute("aria-pressed", lead.has_baler === value);
    b.setAttribute("aria-label", `${lead.name}: ${value === "yes" ? "has" : "doesn't have"} a baler or compactor`);
    box.append(b);
  }
  td.append(box);
  if (pinnedNow(lead.key) && S.leadView !== "all" && (lead.has_baler || "") !== S.leadView) {
    const tab = LEAD_TABS.find(([v]) => v === (lead.has_baler || ""));
    td.append(el("div", `Saved. Moves to “${tab ? tab[1] : "All"}”.`, "sub moved"));
  }
  if (lead.undo_mark && leftOf(lead.undo_mark) > 0) td.append(undoButton(lead.undo_mark, "Undo", () => undoMark(lead.key)));
  if (withCall) td.append(callBox(lead));
  return td;
}
function callBox(lead) {
  const box = el("div", undefined, "call-cell");
  box.style.marginTop = "8px";
  if (lead.call_outcome) {
    box.append(el("span", lead.call_outcome, "badge"));
    box.append(el("span", lead.last_call, "sub"));
  }
  box.append(button("Just called", "", () => openCall(lead)));
  if (lead.call_count) box.append(button(`History (${lead.call_count})`, "quiet", () => openHistory(lead)));
  if (lead.undo_call && leftOf(lead.undo_call) > 0) box.append(undoButton(lead.undo_call, "Undo call", () => undoCall(lead.key)));
  return box;
}

function leadRow(l, withCall) {
  const tr = el("tr");
  tr.className = l.tier + (l.lead_type === "Competitor" ? " competitor" : "")
    + (pinnedNow(l.key) && S.leadView !== "all" && (l.has_baler || "") !== S.leadView ? " just-marked" : "");
  tr.append(markCell(l, withCall));
  tr.append(el("td", `${l.score} (${l.tier})`, "score nowrap"));
  const name = el("td");
  name.append(el("strong", l.name));
  if (l.category) name.append(el("div", l.category, "sub"));
  if (l.lead_type !== "Prospect") name.append(el("div", l.lead_type, "sub"));
  for (const f of l.flags) name.append(el("div", f, "flag"));
  tr.append(name);
  const contact = el("td", undefined, "contact");
  const addr = el("div", [l.address, l.city, l.zip].filter(Boolean).join(", "));
  if (l.map_url) { addr.append(" "); addr.append(link(l.map_url, "map")); }
  contact.append(addr);
  if (l.phone) { const p = el("div"); p.append(phoneLink(l.phone)); contact.append(p); }
  if (l.website) {
    const a = link(l.website, l.website.replace(/^https?:\/\/(www\.)?/i, "").split("/")[0]);
    a.classList.add("site"); a.title = l.website; contact.append(a);
  }
  tr.append(contact);
  tr.append(el("td", l.distance ?? "", "nowrap"));
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
    S.limit = 300; unpinAll(); renderLeads();
  });
  b.append(el("span", label), el("span", on ? (S.dir === "asc" ? "▲" : "▼") : "↕", "arrow"));
  th.setAttribute("aria-sort", on ? (S.dir === "asc" ? "ascending" : "descending") : "none");
  b.title = `Sort by ${label.toLowerCase()}`;
  th.append(b);
  return th;
}
function leadTable(rows, withCall) {
  const table = el("table", undefined, "leads");
  // Fits a 1280px-wide window without scrolling sideways; the Why column takes the rest.
  const cols = [[withCall ? "Baler? / Call" : "Baler or compactor?", withCall ? "138px" : "118px"],
                ["Score", "74px", "score"], ["Business", "22%", "name"], ["Contact", "22%", "city"],
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

function renderLeads() {
  const problemBox = $("leads-problem");
  if (S.loadError && !S.loaded) {
    problem(problemBox, "Can't show your leads right now", S.loadError, () => { problemBox.replaceChildren(el("div", "Loading...", "muted")); loadLeads(); });
    $("leads-body").hidden = true;
    return;
  }
  problemBox.replaceChildren(); $("leads-body").hidden = !S.loaded;
  $("leads-note").replaceChildren(...(S.refreshError ? [el("div", S.refreshError, "warn")] : []));
  const count = (v) => v === "all" ? S.leads.length : S.leads.filter((l) => (l.has_baler || "") === v).length;
  tabs($("lead-tabs"), LEAD_TABS.map(([v, t]) => [v, t, count(v)]), S.leadView,
       (v) => { S.leadView = v; S.limit = 300; unpinAll(); renderLeads(); });
  $("call-hint").hidden = S.leadView === "yes" || !S.leads.length;
  const q = S.q.toLowerCase(), tier = S.tier;
  const rows = S.leads.filter((l) => {
    if (S.leadView !== "all" && (l.has_baler || "") !== S.leadView && !pinnedNow(l.key)) return false;
    if (tier && l.tier !== tier) return false;
    if (!q) return true;
    return [l.name, l.city, l.category, l.address, l.lead_type, l.flags.join(" ")].join(" ").toLowerCase().includes(q);
  });
  const { key } = SORTS[S.sort], sign = S.dir === "asc" ? 1 : -1;
  rows.sort((a, b) => { const x = key(a), y = key(b); return x < y ? -sign : x > y ? sign : 0; });
  const wrap = $("leads-wrap");
  if (!rows.length) {
    wrap.replaceChildren(el("div", S.leads.length ? "Nothing here with these filters." : "No saved leads yet. Run a search on the Find leads page.", "empty"));
  } else {
    wrap.replaceChildren(leadTable(rows.slice(0, S.limit), S.leadView === "yes"));
  }
  const more = $("leads-more");
  more.hidden = rows.length <= S.limit;
  more.textContent = `Show more (${(rows.length - S.limit).toLocaleString()} left)`;
  if (!$("page-leads").hidden) writeHash("leads");
}
function renderRecent() {
  const items = [];
  for (const l of S.leads) {
    if (l.undo_mark && leftOf(l.undo_mark) > 0)
      items.push([l.undo_mark.until, `${l.name}: marked ${l.has_baler === "yes" ? "Yes" : l.has_baler === "no" ? "No" : "Not checked"}`,
                  undoButton(l.undo_mark, "Undo", () => undoMark(l.key))]);
    if (l.undo_call && leftOf(l.undo_call) > 0)
      items.push([l.undo_call.until, `${l.name}: call saved as ${l.call_outcome}`, undoButton(l.undo_call, "Undo", () => undoCall(l.key))]);
  }
  items.sort((a, b) => b[0] - a[0]);
  $("recent").hidden = !items.length;
  $("recent-list").replaceChildren(...items.slice(0, 8).map(([, text, b]) => { const li = el("li"); li.append(el("span", text), b); return li; }));
}
$("leads-more").addEventListener("click", () => { S.limit += 300; renderLeads(); });
$("filter").addEventListener("input", () => { S.q = $("filter").value; S.limit = 300; unpinAll(); renderLeads(); });
$("tier").addEventListener("change", () => { S.tier = $("tier").value; S.limit = 300; unpinAll(); renderLeads(); });
$("call-hint-go").addEventListener("click", () => { S.leadView = "yes"; S.limit = 300; unpinAll(); renderLeads(); });

// Keep up with colleagues' marks and calls: every minute while the page is open, and on return
// to it. Only what changed comes back, so this stays small however long the list grows.
function refreshSoon() {
  if (document.visibilityState !== "visible" || S.busy || !S.loaded) return;
  if ($("call-dlg").open || $("hist-dlg").open) return;
  loadChanges();
}
setInterval(refreshSoon, 60000);
document.addEventListener("visibilitychange", refreshSoon);
