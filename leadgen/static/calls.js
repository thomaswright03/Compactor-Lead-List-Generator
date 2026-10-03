/* Part 3: the Calls page, the "Just called" box and call history. */
// The names this part shares with the others (eslint.config.mjs reads this list).
/* exported emptyNote, loadCalled, renderCalls, openCall, openHistory, verifiedBox */
// An empty page that says how it fills up, with a link to where that happens.
/** @param {string} title @param {string} text @param {string} href @param {string} label */
function emptyNote(title, text, href, label) {
  const box = el("div", undefined, "empty");
  box.append(el("strong", title), el("p", text));
  const a = el("a", label, "btn quiet"); a.href = href;
  box.append(a);
  return box;
}
// The called businesses the Calls page shows: the server keeps those the words typed match (in
// the business's details, or in what its calls said, how they went and who made them) and, on an
// outcome's tab, those whose latest call went that way; latest call first, one page at a time
// ("Show more" adds the next), with every tab's count however many businesses were called.
function callQuery() {
  const p = new URLSearchParams({ tab: "called", sort: "called", dir: "desc" });
  if (S.callQ) p.set("q", S.callQ);
  if (S.callView) p.set("outcome", S.callView);
  return p;
}
// quiet: a refresh in the background (colleagues' calls, a call just saved), the rows not dimmed.
/** @param {boolean} [quiet] */
async function loadCalled(quiet) {
  const seq = ++S.callSeq;
  const p = callQuery();
  p.set("limit", String(Math.max(S.callLimit, PAGE)));
  if (!quiet) {
    S.calledLoading = true;
    if (S.calledLoaded) renderCalls();               // the rows shown stay, dimmed, until the new ones arrive
  }
  try {
    const body = /** @type {LeadsPage} */ (await api(`/leads?${p}`));
    if (seq !== S.callSeq) return;                   // a newer tab or filter was asked for meanwhile
    S.called = body.leads; S.calledTotal = body.total; S.callCounts = body.call_counts || null;
    S.counts = body.counts; S.calledLoaded = true; S.calledError = "";
  } catch (e) {
    const err = /** @type {ApiError} */ (e);
    if (seq !== S.callSeq || err.status === 401) return;
    S.calledError = err.message;
  }
  S.calledLoading = false;
  renderCalls(); counts();
}
/** @param {string} view an outcome, or "" for every called business */
function pickCallTab(view) { S.callView = view; S.callLimit = PAGE; writeHash("calls"); loadCalled(); }
function renderCalls() {
  if (S.calledError && !S.calledLoaded) {
    problem($("calls-problem"), "Can't show your calls right now", S.calledError, () => loadCalled());
    $("calls-body").hidden = true; loadingCue("calls-loading", false); return;
  }
  // Loaded before: the rows shown stay, with a note that they couldn't be brought up to date.
  $("calls-problem").replaceChildren(...(S.calledError
    ? [el("div", `Couldn't update the calls just now. ${S.calledError}`, "warn")] : []));
  $("calls-body").hidden = !S.calledLoaded;
  if (!S.calledLoaded) return;
  /** @type {CallCounts} */
  const c = S.callCounts || { all: S.called.length, outcomes: {} };
  const anyCalls = (S.counts ? S.counts.called : c.all) > 0;
  $("call-filter-row").hidden = !anyCalls && !S.callQ;
  if ($("call-filter").value.trim() !== S.callQ) $("call-filter").value = S.callQ;
  // Every called business first (one row each, latest call first), so a call just saved is always in view.
  /** @type {[string, string, number][]} */
  const tabItems = [["", "All called businesses", c.all],
                    ...OUTCOMES.map((/** @type {string} */ o) => /** @type {[string, string, number]} */ (
                      [o, o, c.outcomes[o] || 0]))];
  tabs($("call-tabs"), tabItems, S.callView, pickCallTab);
  // Phones: the same choice as one list, with the counts.
  $("call-pick").replaceChildren(...tabItems.map(([v, t, n]) => {
    const o = el("option", `${t}: ${n.toLocaleString()}`); o.value = v; return o;
  }));
  $("call-pick").value = S.callView;
  // A call just saved may have moved a business to another outcome's tab.
  const rows = S.callView ? S.called.filter((l) => l.call_outcome === S.callView) : [...S.called];
  const wrap = $("calls-wrap"), more = $("calls-more");
  wrap.classList.toggle("stale", S.calledLoading);
  wrap.setAttribute("aria-busy", S.calledLoading);
  loadingCue("calls-loading", S.calledLoading);
  const left = S.calledTotal - S.called.length;
  more.hidden = left <= 0 || !rows.length;
  more.disabled = S.calledLoading;
  more.textContent = `Show more (${left.toLocaleString()} left)`;
  if (!anyCalls) {
    wrap.replaceChildren(emptyNote("No calls logged yet.",
      "Log a call with Just called on any business on the Leads page. It doesn't change the business's " +
      "Yes / No answer.",
      "#leads", "Go to Leads"));
    return;
  }
  if (!rows.length && S.callQ) {
    const box = el("div", undefined, "empty");
    box.append(el("strong", `No calls match “${S.callQ}”${S.callView ? ` under ${S.callView}` : ""}.`),
               el("p", "The filter looks in each business's name, town and category, in what was said on its " +
                       "calls, how they went and who made them. Check the spelling, pick another tab, or clear " +
                       "the filter to see every called business."),
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
    phone.append(l.phone ? phoneLink(l.phone) : el("span", "No phone listed", "sub"), verifiedBox(l));
    tr.append(phone);
    const when = el("td", undefined, "c-when");
    when.append(el("span", l.call_outcome, "badge"), el("div", l.last_call, "when-at"));
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
// The next page of called businesses, added under the ones shown.
async function showMoreCalls() {
  const more = $("calls-more"), seq = S.callSeq;
  const p = callQuery();
  p.set("offset", String(S.called.length));
  p.set("limit", String(PAGE));
  const hadFocus = document.activeElement === more;
  more.disabled = true; more.textContent = "Loading more...";
  try {
    const body = /** @type {LeadsPage} */ (await api(`/leads?${p}`));
    if (seq !== S.callSeq) return;                   // the tab or filter changed meanwhile
    const have = new Set(S.called.map((l) => l.key));
    S.called = S.called.concat(body.leads.filter((l) => !have.has(l.key)));
    S.callLimit = Math.max(S.callLimit, S.called.length);
    S.calledTotal = body.total; S.callCounts = body.call_counts || null; S.counts = body.counts;
    S.calledError = "";
  } catch (e) {
    const err = /** @type {ApiError} */ (e);
    if (err.status === 401) return;
    S.calledError = err.message;
  }
  renderCalls();
  if (hadFocus && !more.hidden) more.focus();
}
$("calls-more").addEventListener("click", showMoreCalls);
function clearCallFilter() {
  S.callQ = ""; $("call-filter").value = ""; S.callLimit = PAGE; writeHash("calls"); loadCalled();
  $("call-filter").focus();
}
// Asked once typing pauses.
let callFilterTimer = 0;
$("call-filter").addEventListener("input", () => {
  S.callQ = $("call-filter").value.trim(); S.callLimit = PAGE; writeHash("calls");
  clearTimeout(callFilterTimer);
  callFilterTimer = setTimeout(loadCalled, 250);
});
$("call-pick").addEventListener("change", () => pickCallTab($("call-pick").value));

// The latest call's notes; when it had none, the latest notes from an earlier call, with their date.
/** @param {Lead} l */
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
/** @type {Map<string, CallDraft>} */
const draftMemory = new Map();
/** @type {(key: string) => string} */
const draftKey = (key) => `call-draft:${key}`;
/** @param {string} key @returns {CallDraft | null} */
function readDraft(key) {
  try {
    const d = JSON.parse(localStorage.getItem(draftKey(key)) || "null");
    if (d) return d;
  } catch (e) { /* not kept */ }
  return draftMemory.get(key) || null;
}
/** @param {string} key @param {CallDraft} draft */
function writeDraft(key, draft) {
  const empty = !draft.notes.trim() && !draft.outcome;
  if (empty) draftMemory.delete(key); else draftMemory.set(key, draft);
  try {
    if (empty) localStorage.removeItem(draftKey(key)); else localStorage.setItem(draftKey(key), JSON.stringify(draft));
  } catch (e) { /* kept for this visit only */ }
}
// Each call gets its own id when the box opens (kept with the draft), so sending it twice
// (a retry after a lost answer) records it once.
/** @type {Lead | null} */
let callLead = null;
let callOutcome = "", callId = "";
function newId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}
/** @param {string} o an outcome, or "" for none picked */
function pickOutcome(o) {
  callOutcome = o;
  $("outcomes").querySelectorAll("button").forEach((/** @type {HTMLButtonElement} */ x) => {
    const on = x.textContent === o; x.classList.toggle("on", on); x.setAttribute("aria-pressed", String(on));
  });
  $("outcomes").classList.remove("need");
  $("call-need").hidden = Boolean(o);
  if (o && $("call-error").dataset.need) { $("call-error").textContent = ""; delete $("call-error").dataset.need; }
}
function saveDraft() {
  if (callLead) writeDraft(callLead.key, { notes: $("call-notes").value, outcome: callOutcome, id: callId });
}
/** @param {Lead} lead */
function openCall(lead) { withName(() => openCallBox(lead)); }
/** @param {Lead} lead */
function openCallBox(lead) {
  callLead = lead;
  $("call-title").textContent = `Just called: ${lead.name}`;
  $("call-error").textContent = ""; delete $("call-error").dataset.need; $("call-slow").textContent = "";
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
$("call-notes").addEventListener("paste", (/** @type {ClipboardEvent} */ e) => {
  const box = $("call-notes"), text = (e.clipboardData && e.clipboardData.getData("text")) || "";
  const kept = box.value.length - (box.selectionEnd - box.selectionStart);
  pastedOver = Math.max(0, kept + text.length - NOTES_MAX);
  setTimeout(() => { if (pastedOver) notesCount(); }, 0);   // a paste that changed nothing (already full)
});
$("call-notes").addEventListener("input", () => { saveDraft(); notesCount(); });
$("call-cancel").addEventListener("click", () => { saveDraft(); $("call-dlg").close(); });
$("call-dlg").addEventListener("close", saveDraft);
$("call-form").addEventListener("submit", async (/** @type {SubmitEvent} */ e) => {
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
  $("call-error").textContent = ""; $("call-slow").textContent = "";
  // Slow (core.js save): said in the box, or, once it was closed, at the bottom of the screen.
  const slow = () => {
    if ($("call-dlg").open && callLead && callLead.key === key) $("call-slow").textContent = `Saving… ${SLOW_SAVE}`;
    else toast(`Saving the call to ${target.name}… ${SLOW_SAVE}`);
  };
  try {
    const { call } = /** @type {{call: SavedCall}} */ (await save("/calls", {
      key, outcome: callOutcome, notes: $("call-notes").value, id: callId, by: myName() }, slow));
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
    if (!$("page-calls").hidden) loadCalled(true); else S.calledLoaded = false;
    toast(`${lead.name}: saved as ${call.outcome}.`, () => undoCall(key));
  } catch (err) {
    // The notes stay (in the box, and as the business's draft), so Save tries again with them;
    // the call keeps its id, so it is recorded once even if the first try reached the server.
    const { message } = /** @type {Error} */ (err);
    if ($("call-dlg").open && callLead && callLead.key === key) {
      $("call-error").textContent = `Not saved. ${message} Your notes are kept.`;
      $("call-save").disabled = false;
    } else {
      toast(`The call to ${target.name} was not saved. ${message} Your notes are kept: open Just called ` +
            "on it to save them.", null, true);
    }
  } finally { S.busy--; $("call-slow").textContent = ""; }
});

/* A business's verified phone and who to ask for (contacts.py): found while checking it and
   saved for the whole team, beside the listing's own phone (which stays as it was). Searches
   never change them. */
/** @param {Lead} l @returns {HTMLElement} */
function verifiedBox(l) {
  const box = el("div", undefined, "verified");
  if (l.verified_phone || l.contact_name) {
    const line = el("div", undefined, "v-line");
    line.append(el("span", "Verified", "badge v-tag"), " ");
    if (l.verified_phone) line.append(phoneLink(l.verified_phone));
    if (l.verified_phone && l.contact_name) line.append(" · ");
    if (l.contact_name) line.append(el("span", `Ask for ${l.contact_name}`, "v-name"));
    box.append(line);
    if (l.contact_by) box.append(el("div", `Saved by ${l.contact_by}, ${l.contact_when}`, "sub"));
  }
  if (l.key && l.contact_saves > 1) {
    const earlier = button("Earlier versions", "link v-earlier", () => openHistory(l));
    earlier.setAttribute("aria-label", `Earlier verified contacts: ${l.name}`);
    box.append(earlier, " ");
  }
  if (l.key) {
    const label = l.verified_phone || l.contact_name ? "Edit verified contact" : "Add verified phone or contact";
    const edit = button(label, "link v-edit", () => openContact(l));
    edit.setAttribute("aria-label", `${label}: ${l.name}`);
    box.append(edit);
  }
  return box;
}
/** @type {Lead | null} */
let contactLead = null;
/** @param {Lead} lead */
function openContact(lead) { withName(() => openContactBox(lead)); }
/** @param {Lead} lead */
function openContactBox(lead) {
  contactLead = lead;
  $("contact-title").textContent = `Verified contact: ${lead.name}`;
  $("contact-listed").textContent = lead.phone
    ? `The listing's phone, ${lead.phone}, stays as it is; this is saved beside it.`
    : "The listing has no phone number. What you save here is shown beside it.";
  /** @type {HTMLInputElement} */ ($("contact-phone")).value = lead.verified_phone || "";
  /** @type {HTMLInputElement} */ ($("contact-name")).value = lead.contact_name || "";
  $("contact-error").textContent = ""; $("contact-slow").textContent = "";
  $("contact-save").disabled = false;
  $("contact-dlg").showModal();
  $("contact-phone").focus();
}
$("contact-cancel").addEventListener("click", () => $("contact-dlg").close());
$("contact-form").addEventListener("submit", async (/** @type {SubmitEvent} */ e) => {
  e.preventDefault();
  if (!contactLead) return;
  const target = contactLead, key = contactLead.key;
  $("contact-save").disabled = true; S.busy++;
  $("contact-error").textContent = ""; $("contact-slow").textContent = "";
  const slow = () => {
    if ($("contact-dlg").open && contactLead && contactLead.key === key) {
      $("contact-slow").textContent = `Saving… ${SLOW_SAVE}`;
    } else toast(`Saving the verified contact of ${target.name}… ${SLOW_SAVE}`);
  };
  try {
    const { contact } = /** @type {{contact: VerifiedContact}} */ (await save("/contact", {
      key, phone: /** @type {HTMLInputElement} */ ($("contact-phone")).value,
      contact: /** @type {HTMLInputElement} */ ($("contact-name")).value, by: myName() }, slow));
    updateLead({ key, ...contact });
    if (contactLead && contactLead.key === key) { contactLead = null; $("contact-dlg").close(); }
    renderAll();
    if (!$("page-calls").hidden) loadCalled(true); else S.calledLoaded = false;
    const lead = leadByKey(key) || target;
    toast(contact.verified_phone || contact.contact_name ? `${lead.name}: verified contact saved.`
      : `${lead.name}: verified contact taken off.`);
  } catch (err) {
    const { message } = /** @type {Error} */ (err);
    if ($("contact-dlg").open && contactLead && contactLead.key === key) {
      $("contact-error").textContent = `Not saved. ${message}`;
      $("contact-save").disabled = false;
    } else {
      toast(`The verified contact of ${target.name} was not saved. ${message}`, null, true);
    }
  } finally { S.busy--; $("contact-slow").textContent = ""; }
});

/** @param {Lead} lead */
async function openHistory(lead) {
  $("hist-title").textContent = `History of ${lead.name}`;
  const list = $("hist-list");
  list.replaceChildren(el("div", "Loading...", "muted"));
  $("hist-dlg").showModal();
  try {
    const { calls, marks, contacts } = /** @type {CallHistory} */ (await api(`/calls/${encodeURIComponent(lead.key)}`));
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
    // Every verified phone and contact name saved on it (newest first), with who saved them.
    if ((contacts || []).length) {
      list.append(el("h3", "Verified phone and contact", "hist-head"));
      for (const c of contacts) {
        const item = el("div", undefined, "item");
        const what = [c.phone, c.contact && `ask for ${c.contact}`].filter(Boolean).join(" · ") || "Taken off";
        item.append(el("span", what), el("span", `  ${c.when}${byWho(c.by)}`, "sub"));
        list.append(item);
      }
    }
  } catch (err) { list.replaceChildren(el("div", /** @type {Error} */ (err).message, "error")); }
}
$("hist-close").addEventListener("click", () => $("hist-dlg").close());
