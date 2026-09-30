/* Part 4: the Find leads page (today's search, its progress and history). */
// The note beside Find leads says how the day stands (in plain, muted text); a refusal or a
// problem goes in the red line under it (showError) and stays until the form is edited.
function setGo(enabled, note) {
  $("go").disabled = !enabled;
  if (note !== undefined) $("day-note").textContent = note;
}
function clearErrors() {
  S.error = "";
  $("go-error").textContent = ""; $("go-error").hidden = true;
  $("form").querySelectorAll(".field-error").forEach((e) => e.remove());
  $("form").querySelectorAll("[aria-invalid]").forEach((e) => { e.removeAttribute("aria-invalid"); e.removeAttribute("aria-describedby"); });
}
// Show why the search can't start: beside the field it is about (when there is one) and beside the button.
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
  $("go-error").textContent = input ? `Can't start the search: ${message}` : message;
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
  if (S.job) setGo(false, "");
  else if (body.paused) setGo(false, "Searching is paused by the administrator.");
  else if (body.used_today && today.leads === undefined && !body.running) {
    setGo(false, `Today's search was interrupted before it finished. It can be run again after ${today.free_at}.`);
  } else if (body.used_today) {
    setGo(false, `Today's search ran at ${today.when.split(", ").pop()}. The next one can run tomorrow.`);
  } else if (today) {
    setGo(true, "Today's search didn't finish, so it can be run again.");
  } else setGo(true, "One search a day. Today's is available.");
  renderHistory(body.searches);
  renderProblems(body.problems);
}
// The site's problems in the last 7 days (failed searches, errors), so they are never only in the logs.
function renderProblems(p) {
  const box = $("problems");
  box.hidden = !p || !p.count;
  if (box.hidden) return;
  const d = el("details");
  d.append(el("summary", `Problems in the last 7 days: ${p.count.toLocaleString()}`));
  const list = el("ul");
  for (const x of p.latest) { const li = el("li"); li.append(el("span", x.when, "sub"), ` ${x.text}`); list.append(li); }
  d.append(list, el("p", "If these keep happening, tell whoever looks after the site.", "sub"));
  box.replaceChildren(d);
}
function renderHistory(searches) {
  const box = $("history");
  if (!searches.length) { box.replaceChildren(el("div", "No searches yet.", "muted")); return; }
  const table = el("table", undefined, "plain");
  const head = el("tr");
  for (const t of ["When", "Location", "Radius", "Leads", "New", ""]) head.append(el("th", t));
  const thead = el("thead"); thead.append(head);
  const tbody = el("tbody");
  for (const s of searches) {
    const tr = el("tr", undefined, s.failed ? "failed" : "");
    const leadsCell = el("td");
    // An incomplete search (a source failed) saved what the others found, and gave the day back.
    if (s.partial) leadsCell.append(`${s.leads ?? "-"} `, el("span", "Incomplete", "tag"));
    else if (s.failed) leadsCell.append(el("span", "Failed", "tag"));
    else leadsCell.textContent = s.leads ?? "-";
    tr.append(el("td", s.when, "nowrap"), el("td", s.location), el("td", s.radius ? `${s.radius} mi` : ""),
              leadsCell, el("td", s.failed && !s.partial ? "" : s.new ?? "-"));
    const td = el("td");
    tr.append(td); tbody.append(tr);
    if (s.failed) {
      const extra = el("tr", undefined, "failed");
      const cell = el("td"); cell.colSpan = 6; cell.className = "sub";
      cell.textContent = `${s.reason || "The search didn't finish."} It didn't use up the day's search.`;
      extra.append(cell); tbody.append(extra);
      continue;
    }
    if (s.details || (s.warnings || []).length) {
      const extra = el("tr", undefined, "more-row"); extra.hidden = true;
      const cell = el("td"); cell.colSpan = 6;
      const dl = el("dl", undefined, "details");
      // [label, value] pairs in funnel order (older answers were an object).
      const pairs = Array.isArray(s.details) ? s.details : Object.entries(s.details || {});
      for (const [k, v] of pairs) {
        const row = el("div"); row.append(el("dt", k), el("dd", v)); dl.append(row);
      }
      cell.append(dl);
      for (const w of s.warnings || []) cell.append(el("div", w, "warn"));
      extra.append(cell); tbody.append(extra);
      const toggle = button("Details", "link", () => {
        extra.hidden = !extra.hidden;
        toggle.textContent = extra.hidden ? "Details" : "Hide details";
        toggle.setAttribute("aria-expanded", !extra.hidden);
      });
      toggle.setAttribute("aria-expanded", "false");
      td.append(toggle);
    }
  }
  table.append(thead, tbody);
  const wrap = el("div", undefined, "scroll-x"); wrap.append(table);
  box.replaceChildren(wrap);
}

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
  $("slow-note").hidden = !job.slow;
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
  $("done-big").textContent = job.saved ? `${job.new_leads.toLocaleString()} new leads`
                                        : `${job.found.toLocaleString()} leads found`;
  $("done-sub").textContent = !job.saved ? `near ${job.location} (not saved)`
    : job.saved_count == null ? `near ${job.location}` : `${job.saved_count.toLocaleString()} saved leads in all, near ${job.location}`;
  const warn = $("done-warnings"); warn.replaceChildren();
  for (const w of job.warnings) warn.append(el("div", w, "warn"));
  if (job.yelp && $("yelp-quota")) $("yelp-quota").textContent = job.yelp.text;
  await loadLeads();
  loadSearches();
}
// Today's only search: show what it will do and ask first.
const SOURCE_TEXT = { auto: "Everywhere available", osm: "Free map data only", yelp: "Yelp only",
                      google: "Google only (paid)", both: "Google and free map data (paid)" };
function searchSummary(form) {
  const f = new FormData(form), rows = [];
  rows.push(["Search around", f.get("location").trim()]);
  rows.push(["How far", `${f.get("radius") || "30"} miles`]);
  rows.push(["Search words", (f.get("keywords") || "").split(",").map((k) => k.trim()).filter(Boolean).join(", ") || "None"]);
  rows.push(["Where to look", SOURCE_TEXT[f.get("source")] || "Everywhere available"]);
  rows.push(["Leave out scores below", f.get("min_score")]);
  if (f.get("grid") && f.get("grid") !== "1") rows.push(["Coverage for Google and Yelp", `${f.get("grid")} searches`]);
  if (f.get("max_requests")) rows.push(["Most paid lookups", f.get("max_requests")]);
  if (f.get("only_keyword_matches")) rows.push(["Only", "businesses matching the search words"]);
  return rows;
}
// The server's limits, checked here first so a mistake is caught before the confirmation.
const MAX_KEYWORDS = 20, MAX_KEYWORD_LEN = 60;
function number(f, name, label, lo, hi, whole) {
  const raw = (f.get(name) || "").trim();
  if (!raw) return null;
  const v = Number(raw);
  if (!Number.isFinite(v)) return `${label} must be a number`;
  if (whole && !Number.isInteger(v)) return `${label} must be a whole number`;
  return v < lo || v > hi ? `${label} must be between ${lo} and ${hi}` : null;
}
function checkForm(form) {
  const f = new FormData(form);
  if (!(f.get("location") || "").trim()) return ["location", "Enter where to search around: a ZIP code, city or street address."];
  const words = (f.get("keywords") || "").split(",").map((k) => k.trim()).filter(Boolean);
  if (words.length > MAX_KEYWORDS) return ["keywords", `Use at most ${MAX_KEYWORDS} search words (you have ${words.length}).`];
  const long = words.find((k) => k.length > MAX_KEYWORD_LEN);
  if (long) return ["keywords", `Each search word can be up to ${MAX_KEYWORD_LEN} characters; “${long.slice(0, 20)}…” has ${long.length}. Separate words with commas.`];
  for (const [name, label, lo, hi, whole] of [["radius", "Radius", 1, 100, false], ["min_score", "Minimum score", 0, 100, true],
                                              ["max_requests", "The limit on paid lookups", 1, 5000, true]]) {
    const bad = number(f, name, label, lo, hi, whole);
    if (bad) return [name, bad];
  }
  return null;
}
$("form").addEventListener("submit", (e) => {
  e.preventDefault();
  if ($("go").disabled) return;
  const bad = checkForm($("form"));
  if (bad) { showError(bad[1], bad[0]); return; }
  const dl = $("confirm-list"); dl.replaceChildren();
  for (const [k, v] of searchSummary($("form"))) { const row = el("div"); row.append(el("dt", k), el("dd", v)); dl.append(row); }
  $("confirm-dlg").showModal();
  $("confirm-go").focus();
});
$("confirm-back").addEventListener("click", () => { $("confirm-dlg").close(); $("form").querySelector("input[name=location]").focus(); });
$("confirm-form").addEventListener("submit", (e) => { e.preventDefault(); $("confirm-dlg").close(); startSearch(); });
async function startSearch() {
  setGo(false, "Starting...");
  clearErrors(); $("search-failed").hidden = true;
  try {
    const body = await api("/search", { method: "POST", body: new FormData($("form")) });
    follow(body.job_id);
  } catch (err) {
    if (err.status === 429 && err.body && err.body.job_id) return follow(err.body.job_id);
    showError(err.message, err.status === 400 && err.body ? err.body.field : "");
    setGo(false, "");
    loadSearches();
  }
}
