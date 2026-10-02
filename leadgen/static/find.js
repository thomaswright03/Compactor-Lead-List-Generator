/* Part 4: the Find leads page (today's search, its progress and history). */
// The note beside Find leads says how the day stands (in plain, muted text); a refusal or a
// problem goes in the red line under it (showError) and stays until the form is edited.
function setGo(enabled, note) {
  $("go").disabled = !enabled;
  if (note !== undefined) $("day-note").textContent = note;
}
// Once today's search is used, the form reads as closed: a notice at its top and its fields
// greyed out, so nobody edits settings for a search that can't run until tomorrow.
function lockForm(note) {
  const locked = Boolean(note);
  $("form-closed").hidden = !locked;
  if (locked) $("form-closed-text").textContent = note;
  $("form").classList.toggle("closed", locked);
  $("form").querySelectorAll("input, select").forEach((x) => { x.disabled = locked; });
}
function clearErrors() {
  S.error = "";
  $("go-error").textContent = ""; $("go-error").hidden = true;
  $("form").querySelectorAll(".field-error").forEach((e) => e.remove());
  $("form").querySelectorAll("[aria-invalid]").forEach((e) => { e.removeAttribute("aria-invalid"); e.removeAttribute("aria-describedby"); });
}
// Show why the search can't start: once, beside the field it is about, or (for a problem
// that belongs to no field) in the red line beside the button.
function showError(message, field) {
  clearErrors();
  S.error = message;
  const input = field && $("form").querySelector(`[name="${field}"]`);
  if (input) {
    const adv = input.closest("details"); if (adv) adv.open = true;
    const err = el("span", message, "field-error"); err.id = `err-${field}`;
    input.closest("label").append(err);
    input.setAttribute("aria-invalid", "true"); input.setAttribute("aria-describedby", err.id);
    input.focus();
  }
  if (input) return;
  $("go-error").textContent = message;
  $("go-error").hidden = false;
}
$("form").addEventListener("input", () => { if (S.error) clearErrors(); });
// Lists and the tick box say "change" (a text box's late "change" on leaving it is not an edit).
$("form").addEventListener("change", (e) => { if (S.error && e.target.matches("select, input[type=checkbox]")) clearErrors(); });
async function loadSearches() {
  let body;
  try { body = await api("/searches"); }
  catch (err) {
    if (err.status === 401) return;
    problem($("history"), "Can't load the search history", err.message, () => { $("history").replaceChildren(el("div", "Loading...", "muted")); loadSearches(); });
    if (!$("admin-content").hidden) adminUnread();
    if (!S.job) setGo(false, "Find leads is off until the saved data can be reached. Use Retry below.");
    return;
  }
  if (body.yelp && $("yelp-quota")) {
    $("yelp-quota").textContent = body.yelp.text;
    $("yelp-quota").className = "quota" + (body.yelp.paused ? " paused" : "");
  }
  $("paused-box").hidden = !body.paused;
  const today = body.current;
  if (body.running && !S.job) follow(body.running);
  // A refusal or failure stays in its own red line (showError, the failure box); this only says how the day stands.
  lockForm(!S.job && body.used_today && !body.running && today.leads !== undefined
    ? "The next one can run tomorrow, from midnight Utah time." : "");
  if (S.job) setGo(false, "");
  else if (body.paused) setGo(false, "Searching is paused by the administrator.");
  else if (body.used_today && today.leads === undefined && !body.running) {
    setGo(false, `Today's search was interrupted before it finished. It can be run again after ${today.free_at}.`);
  } else if (body.used_today) {
    setGo(false, `Today's search ran at ${today.when.split(", ").pop()}. The next one can run tomorrow.`);
  } else if (today) {
    setGo(true, "Today's search didn't finish, so it can be run again.");
  } else if (body.reruns_left) {
    // After an incomplete search (a source failed) it may run again, a set number of times.
    const n = body.reruns_left;
    setGo(true, `Today's search was incomplete, so it can be run again: ${n} re-run${n === 1 ? "" : "s"} left today.`);
  } else setGo(true, "One search a day. Today's is available.");
  renderHistory(body.searches);
  showFill(today && today.fill);
  renderProblems(body.problems, body.problems_unread);
  if (body.admin && body.admin !== "open") showAdmin(false);   // locked again elsewhere
  if (body.switches) { S.switches = body.switches; renderSwitches(body.switches); }
}
// The emergency switches (web/finding.py flip_switch): each takes effect on the next request.
const SWITCH_SHOWN = { search_paused: () => true, google_off: () => CONFIG.google_on, yelp_off: () => CONFIG.yelp_on };
// What turning each switch on or off does, for the confirmation: [question, effect, button].
const SWITCH_ASK = {
  search_paused: [["Pause all searching for everyone?", "Find leads will refuse to start, for everyone, until this is turned off. A search running now stops within a few seconds; what it already found is saved. Saved leads, marks and calls keep working.", "Pause searching"],
                  ["Let everyone search again?", "Find leads works again for everyone straight away.", "Turn searching back on"]],
  google_off: [["Stop using Google for everyone?", "Searches will skip Google (no paid Google lookups) until this is turned off.", "Stop using Google"],
               ["Use Google again?", "Searches will use Google (paid lookups) again straight away.", "Use Google again"]],
  yelp_off: [["Stop using Yelp for everyone?", "Searches will skip Yelp until this is turned off.", "Stop using Yelp"],
             ["Use Yelp again?", "Searches will use Yelp again straight away.", "Use Yelp again"]],
};
let switchThen = null;
function askSwitch(key, on, then) {
  const [title, why, label] = SWITCH_ASK[key][on ? 0 : 1];
  $("switch-title").textContent = title; $("switch-why").textContent = why; $("switch-go").textContent = label;
  $("switch-go").className = on ? "danger" : "";
  switchThen = then;
  $("switch-dlg").showModal();
  $("switch-cancel").focus();
}
$("switch-form").addEventListener("submit", (e) => { e.preventDefault(); $("switch-dlg").close(); const then = switchThen; switchThen = null; if (then) then(); });
$("switch-cancel").addEventListener("click", () => { switchThen = null; $("switch-dlg").close(); });
$("switch-dlg").addEventListener("cancel", () => { switchThen = null; });
// Each switch reads as what it controls and how that stands now ("Yelp: In use"), never
// as "Switch Yelp off: Off"; its button says what pressing it does.
// [thing, state when the switch is off, state when on, button to switch on, button to switch off]
const SWITCH_TEXT = {
  search_paused: ["Searching", "Working normally", "Paused", "Pause searching", "Resume searching"],
  google_off: ["Google", "In use", "Stopped", "Stop using Google", "Use Google again"],
  yelp_off: ["Yelp", "In use", "Stopped", "Stop using Yelp", "Use Yelp again"],
};
function renderSwitches(list) {
  const box = $("switch-list");
  // A switch the database didn't answer for can be neither shown nor flipped: say so, never an empty list.
  if (Object.values(list).some((s) => s.unread)) { problem(box, "Couldn't load the site switches", ADMIN_UNREAD, adminRetry); return; }
  box.replaceChildren();
  for (const [key, s] of Object.entries(list)) {
    if (!SWITCH_SHOWN[key]() || !SWITCH_TEXT[key]) continue;
    const [thing, offText, onText, turnOn, turnOff] = SWITCH_TEXT[key];
    const row = el("div", undefined, "switch-row");
    const text = el("div");
    text.append(el("strong", `${thing}: ${s.on ? onText : offText}`));
    if (s.env) text.append(el("span", "Set in the server's settings (change it there).", "sub"));
    else if (s.on && (s.when || s.by)) text.append(el("span", `Since ${s.when || "?"}${s.by ? `, by ${s.by}` : ""}.`, "sub"));
    row.append(text);
    if (!s.env) {
      const b = button(s.on ? turnOff : turnOn, s.on ? "" : "quiet", () => askSwitch(key, !s.on, async () => {
        b.disabled = true;
        try { await post("/switches", { key, on: !s.on, by: myName() }); }
        catch (err) { box.append(el("div", err.message, "error")); }
        loadSearches();
      }));

      row.append(b);
    }
    box.append(row);
  }
}
// When the saved data doesn't answer, the administrator's section says so (with the way to stop
// searches without it), instead of an all-clear or an empty list of switches.
const ADMIN_UNREAD = "The saved data isn't answering. Try again in a minute. If you need to stop searches now, set LEADGEN_SEARCH_PAUSED to 1 in Render → Environment.";
function adminRetry() {
  renderProblems(null);
  $("switch-list").replaceChildren(el("p", "Loading...", "muted"));
  loadSearches();
}
function adminUnread() {
  problem($("problems"), "Couldn't load recent problems", ADMIN_UNREAD, adminRetry);
  problem($("switch-list"), "Couldn't load the site switches", ADMIN_UNREAD, adminRetry);
}
// The site's problems in the last 7 days (failed searches, errors), so they are never only in the
// logs. They sit in the administrator's section (closed by default): a note, not an alarm.
// "Nothing went wrong" only when the problems were read and there were none.
function renderProblems(p, unread) {
  const box = $("problems");
  if (unread) { problem(box, "Couldn't load recent problems", ADMIN_UNREAD, adminRetry); return; }
  if (!p) { box.replaceChildren(el("p", "Loading...", "muted")); return; }
  if (!p.count) { box.replaceChildren(el("p", "Nothing went wrong in the last 7 days.", "muted")); return; }
  const list = el("ul");
  for (const x of p.latest) { const li = el("li"); li.append(el("span", x.when, "sub"), ` ${x.text}`); list.append(li); }
  box.replaceChildren(el("p", `${p.count.toLocaleString()} in the last 7 days (the latest first):`), list,
                      el("p", "If these keep happening, tell whoever looks after the site.", "sub"));
}
// Map areas today's search missed are filled in in the background (fillin.py): how that stands,
// in words, and the page looks again every half minute while it goes on.
const areasText = (n) => (n === 1 ? "1 area" : `${n} areas`);
function fillText(f) {
  const more = f.found ? ` ${num(f.found)} more businesses so far (${num(f.new)} new).` : "";
  if (f.state === "filling") {
    return `Still filling in ${areasText(f.left)} of the free map data that didn't answer, in the background until about ${f.until_text}.${more} They join your saved leads as they arrive.`;
  }
  const added = f.found ? ` ${num(f.found)} more businesses were added (${num(f.new)} new).` : "";
  if (f.state === "complete") return `Complete: the map areas that didn't answer at first were filled in later.${added}`;
  if (f.state === "stopped") return `Filling in the missing map areas stopped because searching was paused${f.left ? `; ${areasText(f.left)} never answered` : ""}.${added}`;
  return `${f.left ? `${areasText(f.left)[0].toUpperCase()}${areasText(f.left).slice(1)} of the free map data never answered today; their businesses are missing until the next search.` : "The missing map areas answered later."}${added}`;
}
function showFill(f) {
  const box = $("fill-note");
  clearTimeout(S.fillTimer);
  box.hidden = !f;
  if (!f) return;
  box.textContent = fillText(f);
  box.classList.toggle("done", f.state === "complete");
  if (S.fillFound !== undefined && f.found !== S.fillFound) loadLeads();   // new businesses arrived
  S.fillFound = f.found;
  if (f.state === "filling") S.fillTimer = setTimeout(loadSearches, 30000);
}
// Every count on the page reads the same way: 1,135.
const num = (v) => typeof v === "number" ? v.toLocaleString() : v ?? "-";
// Each search is its own <tbody> (its row, then its reason or details), so on a phone
// each becomes one stacked card with every field in view (app.css, table.plain.hist).
function renderHistory(searches) {
  const box = $("history");
  if (!searches.length) { box.replaceChildren(el("div", "No searches yet.", "muted")); return; }
  const table = el("table", undefined, "plain hist");
  const head = el("tr");
  for (const t of ["When", "Location", "Radius", "Leads", "New", ""]) head.append(el("th", t));
  const thead = el("thead"); thead.append(head);
  table.append(thead);
  // On a phone each value gets its column's name above it (the table's header is hidden there).
  const cell = (text, cls, label) => {
    const td = el("td", undefined, cls);
    if (label) td.append(el("span", label, "h-label"));
    if (text !== undefined) td.append(text);
    return td;
  };
  for (const s of searches) {
    const tbody = el("tbody", undefined, s.failed ? "failed" : "");
    const tr = el("tr", undefined, s.failed ? "failed" : "");
    const leadsCell = cell(undefined, "h-leads", "Leads");
    // An incomplete search (a source failed) saved what the others found, and gave the day back.
    if (s.stopped) leadsCell.append(`${num(s.leads)} `, el("span", "Stopped", "tag"));
    else if (s.fill && s.fill.state === "filling") leadsCell.append(`${num(s.leads)} `, el("span", "Filling in", "tag info"));
    else if (s.partial) leadsCell.append(`${num(s.leads)} `, el("span", "Incomplete", "tag"));
    else if (s.failed) leadsCell.append(el("span", "Failed", "tag"));
    else leadsCell.append(num(s.leads));
    tr.append(cell(s.when, "nowrap h-when"), whereCell(s),
              cell(s.radius ? `${s.radius} mi` : "", "h-radius", "Radius"),
              leadsCell, cell(s.failed && !s.partial ? "" : num(s.new), "h-new", "New"));
    const td = cell(undefined, "h-act");
    tr.append(td); tbody.append(tr); table.append(tbody);
    if (s.failed) {
      const extra = el("tr", undefined, "failed");
      const why = el("td"); why.colSpan = 6; why.className = "sub h-why";
      why.textContent = `${s.reason || "The search didn't finish."} It didn't use up the day's search.`;
      extra.append(why); tbody.append(extra);
      continue;
    }
    if (s.stopped) {
      const extra = el("tr");
      const why = el("td"); why.colSpan = 6; why.className = "sub h-why";
      why.textContent = `Stopped by the administrator (${s.stopped}); the businesses found before then were saved.`;
      extra.append(why); tbody.append(extra);
    }
    if (s.details || (s.warnings || []).length) {
      const extra = el("tr", undefined, "more-row"); extra.hidden = true;
      const more = el("td"); more.colSpan = 6;
      const dl = el("dl", undefined, "details");
      // [label, value] pairs in funnel order (older answers were an object).
      const pairs = Array.isArray(s.details) ? s.details : Object.entries(s.details || {});
      for (const [k, v] of pairs) {
        const row = el("div"); row.append(el("dt", k), el("dd", num(v))); dl.append(row);
      }
      more.append(dl);
      for (const w of s.warnings || []) more.append(el("div", w, "warn"));
      extra.append(more); tbody.append(extra);
      const toggle = button("Details", "link", () => {
        extra.hidden = !extra.hidden;
        toggle.textContent = extra.hidden ? "Details" : "Hide details";
        toggle.setAttribute("aria-expanded", !extra.hidden);
      });
      toggle.setAttribute("aria-expanded", "false");
      td.append(toggle);
    }
  }
  const wrap = el("div", undefined, "scroll-x"); wrap.append(table);
  box.replaceChildren(wrap);
}

// What was typed, and under it the place the search actually ran around (and how far that
// is from Arco's shop) when it says more than the typed words.
function whereCell(s) {
  const td = el("td", undefined, "h-where");
  td.append(el("span", s.location || ""));
  const place = s.place || s.found_near;
  if (place && place.toLowerCase() !== (s.location || "").toLowerCase()) {
    const miles = typeof s.miles === "number" ? ` · ${milesText(s.miles)} from Arco` : "";
    td.append(el("span", `Searched around ${place}${miles}`, `sub${s.miles > CONFIG.area_miles ? " far" : ""}`));
  } else if (typeof s.miles === "number" && s.miles > CONFIG.area_miles) {
    td.append(el("span", `${milesText(s.miles)} from Arco`, "sub far"));
  }
  return td;
}
const milesText = (m) => `${m < 10 ? m.toFixed(1) : Math.round(m).toLocaleString()} miles`;

function showProgress(job) {
  $("progress-card").hidden = false;
  const steps = $("steps"); steps.replaceChildren();
  const skipped = job.skipped || [];
  job.steps.forEach((name, i) => {
    const skip = skipped.includes(i);
    const s = el("div", undefined, "step" + (skip ? " skip" : i < job.step ? " done" : i === job.step ? " now" : ""));
    const label = el("span", name);
    if (skip) label.append(el("span", i === 1 ? "Skipped: not set up" : "Skipped", "why"));
    s.append(el("span", skip ? "–" : i < job.step ? "✓" : "", "dot"), label);
    steps.append(s);
  });
  $("bar-fill").style.width = `${job.pct}%`;
  $("bar").setAttribute("aria-valuenow", Math.round(job.pct));
  $("pct").textContent = `${Math.round(job.pct)}%`;
  $("elapsed").textContent = fmtTime(job.elapsed || 0);
  $("progress-msg").textContent = job.message;
  $("progress-card").classList.toggle("stopping", Boolean(job.stopping));
  $("slow-note").hidden = !job.slow;
}
// One short note (an incomplete search's summary, or the first warning); the details behind "More".
function showNotes(note, warnings) {
  const box = $("done-warnings"); box.replaceChildren();
  const rest = note ? warnings : warnings.slice(1), main = note || warnings[0];
  if (!main) return;
  const div = el("div", "", "warn"); div.append(el("span", main));
  if (rest.length) {
    const more = el("details", "", "more-notes"); more.append(el("summary", "More"));
    for (const w of rest) more.append(el("p", w));
    div.append(more);
  }
  box.append(div);
}
async function follow(jobId, misses = 0) {
  S.job = jobId;
  setGo(false, ""); $("done-card").hidden = true; $("search-failed").hidden = true;
  let job;
  try { job = await api(`/status/${jobId}`); }
  catch (err) {
    if (err.status !== 404 && misses < 5) { setTimeout(() => follow(jobId, misses + 1), 3000); return; }
    S.job = null; $("progress-card").hidden = true;
    setGo(false, "Lost track of the search (the server may have restarted). Reload the page."); return;
  }
  showProgress(job);
  if (job.state === "running") { setTimeout(() => follow(jobId), 1500); return; }
  S.job = null;
  $("progress-card").hidden = true;
  if (job.state === "error") {
    $("search-failed-msg").textContent = job.message;
    $("search-failed").hidden = false;
    loadSearches();
    return;
  }
  $("done-card").hidden = false;
  $("done-card").classList.toggle("stopped", Boolean(job.stopped));
  $("done-big").textContent = (job.stopped ? "Stopped by the administrator: " : "")
    + (job.saved ? `${job.new_leads.toLocaleString()} new leads` : `${job.found.toLocaleString()} leads found`);
  $("done-sub").textContent = !job.saved ? `near ${job.location} (not saved)`
    : job.saved_count == null ? `near ${job.location}` : `${job.saved_count.toLocaleString()} saved leads in all, near ${job.location}`;
  showNotes(job.note, job.warnings);
  if (job.yelp && $("yelp-quota")) $("yelp-quota").textContent = job.yelp.text;
  await loadLeads();
  loadSearches();
}
// Today's only search: show what it will do and ask first. "Everywhere available" is
// spelled out as the sources that will actually be asked.
function autoSources() {
  const off = (key) => Boolean(S.switches && S.switches[key] && S.switches[key].on);
  const google = CONFIG.google_on && !off("google_off"), yelp = CONFIG.yelp_on && !off("yelp_off");
  if (!google && !yelp) {
    return `Free map data only (${CONFIG.google_on || CONFIG.yelp_on ? "Google and Yelp are switched off" : "Google and Yelp aren't set up"})`;
  }
  const names = [google && "Google (paid)", yelp && "Yelp", "free map data"].filter(Boolean);
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}
const SOURCE_TEXT = { auto: "Everywhere available", osm: "Free map data only", yelp: "Yelp only",
                      google: "Google only (paid)", both: "Google and free map data (paid)" };
function searchSummary(form) {
  const f = new FormData(form), rows = [];
  rows.push(["Search around", f.get("location").trim()]);
  rows.push(["How far", `${f.get("radius")} miles`]);
  rows.push(["Also look for", (f.get("keywords") || "").split(",").map((k) => k.trim()).filter(Boolean).join(", ") || "Nothing extra"]);
  const source = f.get("source") || "auto";
  rows.push(["Where to look", source === "auto" ? autoSources() : SOURCE_TEXT[source] || autoSources()]);
  rows.push(["Leave out scores below", f.get("min_score")]);
  if (f.get("grid") && f.get("grid") !== "1") rows.push(["Coverage for Google and Yelp", `${f.get("grid")} searches`]);
  if (f.get("max_requests")) rows.push(["Most paid lookups", f.get("max_requests")]);
  if (f.get("only_keyword_matches")) rows.push(["Only", "businesses matching the search words"]);
  return rows;
}
// The server's limits, checked here first so a mistake is caught before the confirmation.
const MAX_KEYWORDS = 20, MAX_KEYWORD_LEN = 60;
function number(f, name, label, lo, hi, whole, unit, required) {
  const raw = (f.get(name) || "").trim();
  // A blank How far or minimum score is pointed out, never quietly replaced by the default.
  if (!raw) return required ? `${label} must be between ${lo} and ${hi}${unit || ""}` : null;
  const v = Number(raw);
  if (!Number.isFinite(v)) return `${label} must be a number`;
  if (whole && !Number.isInteger(v)) return `${label} must be a whole number`;
  return v < lo || v > hi ? `${label} must be between ${lo} and ${hi}${unit || ""}` : null;
}
function checkForm(form) {
  const f = new FormData(form);
  if (!(f.get("location") || "").trim()) return ["location", "Enter where to search around: a ZIP code, city or street address."];
  const words = (f.get("keywords") || "").split(",").map((k) => k.trim()).filter(Boolean);
  if (words.length > MAX_KEYWORDS) return ["keywords", `Use at most ${MAX_KEYWORDS} search words (you have ${words.length}).`];
  const long = words.find((k) => k.length > MAX_KEYWORD_LEN);
  if (long) return ["keywords", `Each search word can be up to ${MAX_KEYWORD_LEN} characters; “${long.slice(0, 20)}…” has ${long.length}. Separate words with commas.`];
  // The same words as the server's (web/finding.py parse_form): the field's own label.
  for (const [name, label, lo, hi, whole, unit, required] of [["radius", "How far", 1, 100, false, " miles", true],
                                                              ["min_score", "The score to leave out weak leads below", 0, 100, true, "", true],
                                                              ["max_requests", "Most paid lookups for this search", 1, 5000, true]]) {
    const bad = number(f, name, label, lo, hi, whole, unit, required);
    if (bad) return [name, bad];
  }
  return null;
}
// Before anything is spent the server finds the place typed (web/finding.py place): the
// confirmation names it and how far it is from Arco's shop. A place outside Arco's area needs a
// second, explicit yes (farDialog) that names it again.
const farText = (p) => `“${p.label}” is ${milesText(p.miles)} from Arco's shop, outside Arco's area (about ${p.area_miles} miles around it).`;
$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if ($("go").disabled || S.locating) return;
  const bad = checkForm($("form"));
  if (bad) { showError(bad[1], bad[0]); return; }
  const note = $("day-note").textContent;
  S.locating = true; $("go").disabled = true; $("day-note").textContent = "Finding the place...";
  let place;
  try { place = await api(`/place?${new URLSearchParams({ location: $("form").location.value.trim() })}`); }
  catch (err) { showError(err.message, err.body && err.body.field); return; }
  finally { S.locating = false; setGo(!S.job, note); }
  S.place = place;
  const dl = $("confirm-list"); dl.replaceChildren();
  const rows = searchSummary($("form"));
  rows.splice(1, 0, ["Searches around", place.label], ["From Arco's shop", milesText(place.miles)]);
  for (const [k, v] of rows) { const row = el("div"); row.append(el("dt", k), el("dd", v)); dl.append(row); }
  $("confirm-far").hidden = !place.outside;
  if (place.outside) $("confirm-far").textContent = `${farText(place)} You'll be asked once more before it starts.`;
  $("confirm-go").textContent = place.outside ? "Next" : "Start search";
  $("confirm-dlg").showModal();
  $("confirm-go").focus();
});
const backToPlace = () => $("form").querySelector("input[name=location]").focus();
$("confirm-back").addEventListener("click", () => { $("confirm-dlg").close(); backToPlace(); });
$("confirm-form").addEventListener("submit", (e) => {
  e.preventDefault(); $("confirm-dlg").close();
  if (S.place && S.place.outside) farDialog(S.place); else startSearch();
});
function farDialog(place) {
  S.place = place;
  $("far-why").textContent = farText(place);
  $("far-dlg").showModal();
  $("far-back").focus();                 // the safe choice is the one already in focus
}
$("far-back").addEventListener("click", () => { $("far-dlg").close(); backToPlace(); });
$("far-form").addEventListener("submit", (e) => { e.preventDefault(); $("far-dlg").close(); startSearch(S.place.confirm); });
async function startSearch(confirmPlace) {
  setGo(false, "Starting...");
  clearErrors(); $("search-failed").hidden = true;
  const form = new FormData($("form"));
  if (confirmPlace) form.set("confirm_place", confirmPlace);
  try {
    const body = await api("/search", { method: "POST", body: form });
    follow(body.job_id);
  } catch (err) {
    if (err.status === 429 && err.body && err.body.job_id) return follow(err.body.job_id);
    setGo(false, "");
    loadSearches();
    // The place turned out to be outside Arco's area: ask, naming it (nothing was spent).
    if (err.body && err.body.confirm_far) { farDialog(err.body.place); return; }
    showError(err.message, err.status === 400 && err.body ? err.body.field : "");
  }
}

// The administrator's section is locked with its own password (web/auth.py admin_unlock).
// Unlocking lasts as long as the login; the problems and switches then load.
function showAdmin(open) {
  $("admin-lock").hidden = open;
  $("admin-content").hidden = !open;
}
$("admin-lock").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("admin-error"), go = $("admin-unlock");
  err.hidden = true;
  if (!$("admin-pass").value) { err.textContent = "Enter the administrator password."; err.hidden = false; return; }
  go.disabled = true;
  try {
    await post("/admin/unlock", { password: $("admin-pass").value });
    $("admin-pass").value = "";
    showAdmin(true);
    loadSearches();
  } catch (x) { err.textContent = x.message; err.hidden = false; $("admin-pass").select(); }
  finally { go.disabled = false; }
});
$("admin-relock").addEventListener("click", async () => {
  try { await post("/admin/lock", {}); } catch (x) { /* the page locks either way */ }
  showAdmin(false);
  renderProblems(null);
  $("admin-pass").focus();
});
