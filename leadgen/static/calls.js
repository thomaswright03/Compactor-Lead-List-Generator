/* Part 3: the Calls page, the "Just called" box and call history. */
// An empty page that says how it fills up, with a link to where that happens.
function emptyNote(title, text, href, label) {
  const box = el("div", undefined, "empty");
  box.append(el("strong", title), el("p", text));
  const a = el("a", label, "btn quiet"); a.href = href;
  box.append(a);
  return box;
}
// The businesses with a call logged (all of them: calls are made by hand, so they stay few).
async function loadCalled() {
  try {
    const body = await api("/leads?tab=called&sort=called&dir=desc&limit=5000");
    S.called = body.leads; S.counts = body.counts; S.calledLoaded = true; S.calledError = "";
  } catch (err) {
    if (err.status === 401) return;
    S.calledError = err.message;
  }
  renderCalls(); counts();
}
function renderCalls() {
  if (S.calledError && !S.calledLoaded) {
    problem($("calls-problem"), "Can't show your calls right now", S.calledError, loadCalled);
    $("calls-body").hidden = true; return;
  }
  $("calls-problem").replaceChildren(); $("calls-body").hidden = !S.calledLoaded;
  if (!S.calledLoaded) return;
  const called = S.called, outcomes = (S.counts && S.counts.outcomes) || {};
  // "All calls" first, newest first, so a call just saved is always in view.
  tabs($("call-tabs"), [["", "All calls", called.length], ...OUTCOMES.map((o) => [o, o, outcomes[o] || 0])], S.callView,
       (v) => { S.callView = v; renderCalls(); writeHash("calls"); });
  const rows = S.callView ? called.filter((l) => l.call_outcome === S.callView) : [...called];
  const wrap = $("calls-wrap");
  if (!called.length) {
    wrap.replaceChildren(emptyNote("No calls logged yet.",
      "Log a call with Just called on a business marked Yes (the Leads page's Has baler or compactor tab).",
      "#leads?tab=yes", "Go to Has baler or compactor"));
    return;
  }
  if (!rows.length) { wrap.replaceChildren(el("div", `No calls with the result “${S.callView}” yet.`, "empty")); return; }
  const table = el("table", undefined, "plain calls");
  const head = el("tr");
  for (const t of ["Business", "Phone", "Latest call", "Conversation summary", ""]) head.append(el("th", t));
  const thead = el("thead"); thead.append(head);
  const tbody = el("tbody");
  rows.sort((a, b) => (b.last_call_at || 0) - (a.last_call_at || 0));   // latest call first
  for (const l of rows) {
    const tr = el("tr");
    const name = el("td"); name.append(el("strong", l.name)); name.append(el("div", [l.address, l.city].filter(Boolean).join(", "), "sub"));
    tr.append(name);
    const phone = el("td"); phone.append(phoneLink(l.phone)); tr.append(phone);
    const when = el("td");
    when.append(el("span", l.call_outcome, "badge"), el("div", l.last_call));
    when.append(el("div", `${l.call_count} call${l.call_count > 1 ? "s" : ""}`, "sub"));
    tr.append(when);
    tr.append(el("td", l.call_notes || "(no notes)", "notes"));
    const act = el("td"); const box = el("div", undefined, "call-cell");
    box.append(button("Just called", "", () => openCall(l)));
    box.append(button("History", "quiet", () => openHistory(l)));
    if (l.undo_call && leftOf(l.undo_call) > 0) box.append(undoButton(l.undo_call, "Undo call", () => undoCall(l.key)));
    act.append(box); tr.append(act);
    tbody.append(tr);
  }
  table.append(thead, tbody);
  wrap.replaceChildren(table);
}

/* Unsent call notes are kept per business (in this browser) until they are saved, so closing
   the box, pressing Escape or reloading never loses them. */
const draftMemory = new Map();
const draftKey = (key) => `call-draft:${key}`;
function readDraft(key) {
  try { const d = JSON.parse(localStorage.getItem(draftKey(key)) || "null"); if (d) return d; } catch (e) { /* not kept */ }
  return draftMemory.get(key) || null;
}
function writeDraft(key, draft) {
  const empty = !draft.notes.trim() && !draft.outcome;
  if (empty) draftMemory.delete(key); else draftMemory.set(key, draft);
  try { if (empty) localStorage.removeItem(draftKey(key)); else localStorage.setItem(draftKey(key), JSON.stringify(draft)); }
  catch (e) { /* kept for this visit only */ }
}
// Each call gets its own id when the box opens (kept with the draft), so sending it twice
// (a retry after a lost answer) records it once.
let callLead = null, callOutcome = "", callId = "";
function newId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}
function pickOutcome(o) {
  callOutcome = o;
  $("outcomes").querySelectorAll("button").forEach((x) => { const on = x.textContent === o; x.classList.toggle("on", on); x.setAttribute("aria-pressed", on); });
  $("call-save").disabled = !o;
}
function saveDraft() { if (callLead) writeDraft(callLead.key, { notes: $("call-notes").value, outcome: callOutcome, id: callId }); }
function openCall(lead) {
  callLead = lead;
  $("call-title").textContent = `Just called: ${lead.name}`;
  $("call-error").textContent = "";
  const box = $("outcomes"); box.replaceChildren();
  for (const o of OUTCOMES) {
    const b = button(o, "", () => { pickOutcome(o); saveDraft(); });
    b.setAttribute("aria-pressed", "false");
    box.append(b);
  }
  const draft = readDraft(lead.key);
  callId = (draft && draft.id) || newId();
  $("call-notes").value = draft ? draft.notes : "";
  pickOutcome(draft && OUTCOMES.includes(draft.outcome) ? draft.outcome : "");
  $("call-draft-note").textContent = draft ? "Your unsaved notes from before are back. They are kept until you save." : "Notes are kept as a draft until you save.";
  $("call-dlg").showModal();
  $("call-notes").focus();
}
$("call-notes").addEventListener("input", saveDraft);
$("call-cancel").addEventListener("click", () => { saveDraft(); $("call-dlg").close(); });
$("call-dlg").addEventListener("close", saveDraft);
$("call-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!callLead || !callOutcome) return;
  const target = callLead, key = callLead.key;
  $("call-save").disabled = true; S.busy++;
  try {
    const { call } = await post("/calls", { key, outcome: callOutcome, notes: $("call-notes").value, id: callId });
    writeDraft(key, { notes: "", outcome: "" });
    // Update the lead as it is now (the list may have been reloaded while saving).
    const lead = leadByKey(key) || target;
    const first = !lead.call_count;
    updateLead({ key, call_outcome: call.outcome, call_notes: call.notes, last_call: call.when,
                 last_call_at: call.at, call_count: (lead.call_count || 0) + 1, undo_call: call.undo });
    addRecent(leadByKey(key) || lead);
    if (S.counts && first) S.counts.called++;
    if (callLead && callLead.key === key) { callLead = null; $("call-dlg").close(); }
    renderAll();
    if (!$("page-calls").hidden) loadCalled(); else S.calledLoaded = false;
    toast(`${lead.name}: saved as ${call.outcome}.`, () => undoCall(key));
  } catch (err) {
    if (callLead && callLead.key === key) {
      $("call-error").textContent = `Not saved. ${err.message}`;
      $("call-save").disabled = false;
    }
  } finally { S.busy--; }
});

async function openHistory(lead) {
  $("hist-title").textContent = `Calls to ${lead.name}`;
  const list = $("hist-list");
  list.replaceChildren(el("div", "Loading...", "muted"));
  $("hist-dlg").showModal();
  try {
    const { calls } = await api(`/calls/${encodeURIComponent(lead.key)}`);
    list.replaceChildren();
    for (const c of calls) {
      const item = el("div", undefined, "item");
      const top = el("div"); top.append(el("span", c.outcome, "badge")); top.append(el("span", `  ${c.when}`, "sub"));
      item.append(top, el("div", c.notes || "(no notes)", "notes"));
      list.append(item);
    }
    if (!calls.length) list.append(el("div", "No calls yet.", "muted"));
  } catch (err) { list.replaceChildren(el("div", err.message, "error")); }
}
$("hist-close").addEventListener("click", () => $("hist-dlg").close());
