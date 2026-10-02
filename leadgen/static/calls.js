/* Part 3: the Calls page, the "Just called" box and call history. */
// The names this part shares with the others (eslint.config.mjs reads this list).
/* exported emptyNote, loadCalled, renderCalls, openCall, openHistory */
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
function pickCallTab(view) { S.callView = view; renderCalls(); writeHash("calls"); }
function renderCalls() {
  if (S.calledError && !S.calledLoaded) {
    problem($("calls-problem"), "Can't show your calls right now", S.calledError, loadCalled);
    $("calls-body").hidden = true; return;
  }
  $("calls-problem").replaceChildren(); $("calls-body").hidden = !S.calledLoaded;
  if (!S.calledLoaded) return;
  // The filter box narrows the list (and the tab counts) to businesses whose name, address or city match.
  const matching = S.callQ ? S.called.filter((l) => callMatches(l, S.callQ)) : S.called;
  const called = matching, outcomes = S.callQ ? countOutcomes(matching) : (S.counts && S.counts.outcomes) || {};
  $("call-filter-row").hidden = !S.called.length;
  if ($("call-filter").value !== S.callQ) $("call-filter").value = S.callQ;
  // Every called business first (one row each, latest call first), so a call just saved is always in view.
  const tabItems = [["", "All called businesses", called.length], ...OUTCOMES.map((o) => [o, o, outcomes[o] || 0])];
  tabs($("call-tabs"), tabItems, S.callView, pickCallTab);
  // Phones: the same choice as one list, with the counts.
  $("call-pick").replaceChildren(...tabItems.map(([v, t, n]) => {
    const o = el("option", `${t}: ${n.toLocaleString()}`); o.value = v; return o;
  }));
  $("call-pick").value = S.callView;
  const rows = S.callView ? called.filter((l) => l.call_outcome === S.callView) : [...called];
  const wrap = $("calls-wrap");
  if (!S.called.length) {
    wrap.replaceChildren(emptyNote("No calls logged yet.",
      "Log a call with Just called on any business on the Leads page. It doesn't change the business's " +
      "Yes / No answer.",
      "#leads", "Go to Leads"));
    return;
  }
  if (!rows.length && S.callQ) {
    const box = el("div", undefined, "empty");
    box.append(el("strong", `No calls match “${S.callQ}”${S.callView ? ` under ${S.callView}` : ""}.`),
               el("p", "Check the spelling, pick another tab, or clear the filter to see every called business."),
               button("Clear filter", "quiet", clearCallFilter));
    wrap.replaceChildren(box);
    return;
  }
  if (!rows.length) {
    wrap.replaceChildren(el("div", `No calls with the result “${S.callView}” yet.`, "empty"));
    return;
  }
  const table = el("table", undefined, "plain calls");
  const head = el("tr");
  for (const t of ["Business", "Phone", "Latest call", "Conversation summary", ""]) head.append(el("th", t));
  const thead = el("thead"); thead.append(head);
  const tbody = el("tbody");
  rows.sort((a, b) => (b.last_call_at || 0) - (a.last_call_at || 0));   // latest call first
  for (const l of rows) {
    const tr = el("tr");
    const name = el("td", undefined, "c-name");
    name.append(el("strong", l.name),
                el("div", [l.address || "No street address", l.city || l.near].filter(Boolean).join(" · "), "sub"));
    if (l.closed) name.append(el("div", "Closed for good", "flag"));
    tr.append(name);
    const phone = el("td", undefined, "c-phone");
    phone.append(l.phone ? phoneLink(l.phone) : el("span", "No phone listed", "sub")); tr.append(phone);
    const when = el("td", undefined, "c-when");
    when.append(el("span", l.call_outcome, "badge"), el("div", l.last_call));
    if (l.last_call_by) when.append(el("div", `by ${l.last_call_by}`, "sub"));
    when.append(el("div", `${l.call_count} call${l.call_count > 1 ? "s" : ""}`, "sub"));
    tr.append(when);
    tr.append(notesCell(l));
    const act = el("td", undefined, "c-act"); const box = el("div", undefined, "call-cell");
    box.append(button("Just called", "", () => openCall(l)));
    box.append(button("History", "quiet", () => openHistory(l)));
    if (l.undo_call && leftOf(l.undo_call) > 0) box.append(undoButton(l.undo_call, "Undo call", () => undoCall(l.key)));
    act.append(box); tr.append(act);
    tbody.append(tr);
  }
  table.append(thead, tbody);
  wrap.replaceChildren(table);
}

// A called business matches the Calls filter when every word typed is somewhere in its name,
// town, ZIP, category, address, type or flags, in any order and any case: the Leads filter's
// rule (web/leads.py, matches).
function callMatches(l, q) {
  const hay = [l.name, l.address, l.city, l.near, l.zip, l.category, l.lead_type, ...(l.flags || [])]
    .filter(Boolean).join(" ").toLowerCase();
  return q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => hay.includes(w));
}
function countOutcomes(leads) {
  const n = {};
  for (const l of leads) if (l.call_outcome) n[l.call_outcome] = (n[l.call_outcome] || 0) + 1;
  return n;
}
function clearCallFilter() {
  S.callQ = ""; $("call-filter").value = ""; renderCalls(); writeHash("calls"); $("call-filter").focus();
}
$("call-filter").addEventListener("input", () => {
  S.callQ = $("call-filter").value.trim(); renderCalls(); writeHash("calls");
});
$("call-pick").addEventListener("change", () => pickCallTab($("call-pick").value));

// The latest call's notes; when it had none, the latest notes from an earlier call, with their date.
function notesCell(l) {
  const td = el("td", undefined, "notes");
  if (l.call_notes) { td.textContent = l.call_notes; return td; }
  if (!l.earlier_notes) { td.append(el("span", "(no notes)", "sub")); return td; }
  td.append(el("div", "Latest call: no notes.", "sub"),
            el("div", `From the call on ${l.earlier_notes_when}:`, "sub earlier"), el("div", l.earlier_notes));
  return td;
}

/* Unsent call notes are kept per business (in this browser) until they are saved, so closing
   the box, pressing Escape or reloading never loses them. */
const draftMemory = new Map();
const draftKey = (key) => `call-draft:${key}`;
function readDraft(key) {
  try {
    const d = JSON.parse(localStorage.getItem(draftKey(key)) || "null");
    if (d) return d;
  } catch (e) { /* not kept */ }
  return draftMemory.get(key) || null;
}
function writeDraft(key, draft) {
  const empty = !draft.notes.trim() && !draft.outcome;
  if (empty) draftMemory.delete(key); else draftMemory.set(key, draft);
  try {
    if (empty) localStorage.removeItem(draftKey(key)); else localStorage.setItem(draftKey(key), JSON.stringify(draft));
  } catch (e) { /* kept for this visit only */ }
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
  $("outcomes").querySelectorAll("button").forEach((x) => {
    const on = x.textContent === o; x.classList.toggle("on", on); x.setAttribute("aria-pressed", String(on));
  });
  $("outcomes").classList.remove("need");
  $("call-need").hidden = Boolean(o);
  if (o && $("call-error").dataset.need) { $("call-error").textContent = ""; delete $("call-error").dataset.need; }
}
function saveDraft() {
  if (callLead) writeDraft(callLead.key, { notes: $("call-notes").value, outcome: callOutcome, id: callId });
}
function openCall(lead) { withName(() => openCallBox(lead)); }
function openCallBox(lead) {
  callLead = lead;
  $("call-title").textContent = `Just called: ${lead.name}`;
  $("call-error").textContent = ""; delete $("call-error").dataset.need;
  $("call-save").disabled = false;
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
  pastedOver = 0; notesCount();
  $("call-draft-note").textContent = draft ? "Your unsaved notes from before are back. They are kept until you save."
    : "Notes are kept as a draft until you save.";
  $("call-dlg").showModal();
  $("call-notes").focus();
}
// A summary holds up to 5,000 characters (the server's calls.MAX_NOTES). Near the limit the box
// says how many are left; at the limit, or when a paste didn't fit, it says so plainly.
const NOTES_MAX = Number($("call-notes").maxLength) > 0 ? Number($("call-notes").maxLength) : 5000;
let pastedOver = 0;
function notesCount() {
  const box = $("call-notes-count"), left = NOTES_MAX - $("call-notes").value.length;
  const over = pastedOver; pastedOver = 0;
  box.hidden = left > 500 && !over;
  box.classList.toggle("limit", left <= 0 || over > 0);
  box.textContent = over > 0
    ? `Limit reached: a summary can be up to ${NOTES_MAX.toLocaleString()} characters, so the last ` +
      `${over.toLocaleString()} characters you pasted were left out. Shorten it, or keep the rest elsewhere.`
    : left <= 0
      ? `Limit reached: a summary can be up to ${NOTES_MAX.toLocaleString()} characters. Anything more isn't kept.`
    : `${left.toLocaleString()} characters left (${NOTES_MAX.toLocaleString()} at most).`;
}
$("call-notes").addEventListener("paste", (e) => {
  const box = $("call-notes"), text = (e.clipboardData && e.clipboardData.getData("text")) || "";
  const kept = box.value.length - (box.selectionEnd - box.selectionStart);
  pastedOver = Math.max(0, kept + text.length - NOTES_MAX);
  setTimeout(() => { if (pastedOver) notesCount(); }, 0);   // a paste that changed nothing (already full)
});
$("call-notes").addEventListener("input", () => { saveDraft(); notesCount(); });
$("call-cancel").addEventListener("click", () => { saveDraft(); $("call-dlg").close(); });
$("call-dlg").addEventListener("close", saveDraft);
$("call-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!callLead) return;
  if (!callOutcome) {
    // Save needs one of the six results: say so, and point at them.
    $("call-error").textContent = "Pick how the call went (one of the buttons under “How did it go?”), then Save.";
    $("call-error").dataset.need = "1";
    $("outcomes").classList.add("need");
    $("outcomes").querySelector("button").focus();
    return;
  }
  const target = callLead, key = callLead.key;
  $("call-save").disabled = true; S.busy++;
  try {
    const { call } = await post("/calls", { key, outcome: callOutcome, notes: $("call-notes").value, id: callId,
                                            by: myName() });
    writeDraft(key, { notes: "", outcome: "" });
    // Update the lead as it is now (the list may have been reloaded while saving).
    const lead = leadByKey(key) || target;
    const first = !lead.call_count;
    const earlier = call.notes ? { earlier_notes: "", earlier_notes_when: "" }
      : lead.call_notes ? { earlier_notes: lead.call_notes, earlier_notes_when: lead.last_call } : {};
    updateLead({ key, ...earlier, call_outcome: call.outcome, call_notes: call.notes, last_call: call.when,
                 last_call_at: call.at, last_call_by: call.by, call_count: (lead.call_count || 0) + 1,
                 undo_call: call.undo });
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
  $("hist-title").textContent = `History of ${lead.name}`;
  const list = $("hist-list");
  list.replaceChildren(el("div", "Loading...", "muted"));
  $("hist-dlg").showModal();
  try {
    const { calls, marks } = await api(`/calls/${encodeURIComponent(lead.key)}`);
    list.replaceChildren(el("h3", "Calls", "hist-head"));
    for (const c of calls) {
      const item = el("div", undefined, "item");
      const top = el("div");
      top.append(el("span", c.outcome, "badge"), el("span", `  ${c.when}${byWho(c.by)}`, "sub"));
      item.append(top, el("div", c.notes || "(no notes)", "notes"));
      list.append(item);
    }
    if (!calls.length) list.append(el("div", "No calls yet.", "muted"));
    // Every Yes / No it was given (newest first): a business merged from several listings
    // keeps the marks each of them had.
    if ((marks || []).length) {
      list.append(el("h3", "Yes / No marks", "hist-head"));
      for (const m of marks) {
        const item = el("div", undefined, "item");
        item.append(el("span", m.value === "yes" ? "Yes" : "No", "badge"),
                    el("span", `  ${m.when}${byWho(m.by)}`, "sub"));
        list.append(item);
      }
    }
  } catch (err) { list.replaceChildren(el("div", err.message, "error")); }
}
$("hist-close").addEventListener("click", () => $("hist-dlg").close());
