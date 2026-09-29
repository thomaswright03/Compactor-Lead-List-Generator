/* Part 4: the Find leads page (today's search, its progress and history). */
function setGo(enabled, note, used) {
  $("go").disabled = !enabled;
  if (note !== undefined) { $("day-note").textContent = note; $("day-note").className = used ? "used" : ""; }
}
async function loadSearches() {
  let body;
  try { body = await api("/searches"); }
  catch (err) {
    if (err.status === 401) return;
    problem($("history"), "Can't load the search history", err.message, () => { $("history").replaceChildren(el("div", "Loading...", "muted")); loadSearches(); });
    if (!S.job) setGo(false, "Find leads is off until the saved data can be reached. Use Retry below.", true);
    return;
  }
  if (body.yelp && $("yelp-quota")) {
    $("yelp-quota").textContent = body.yelp.text;
    $("yelp-quota").className = "quota" + (body.yelp.paused ? " paused" : "");
  }
  $("paused-box").hidden = !body.paused;
  const today = body.current;
  if (body.running && !S.job) follow(body.running);
  if (S.job) setGo(false, "");
  else if (body.paused) setGo(false, "Searching is paused by the administrator.", true);
  else if (S.error) setGo(!body.used_today, body.used_today ? S.error : "You can try again.", body.used_today);
  else if (body.used_today && today.leads === undefined && !body.running) {
    setGo(false, `Today's search was interrupted before it finished. It can be run again after ${today.free_at}.`, true);
  } else if (body.used_today) {
    setGo(false, `Today's search ran ${today.when.split(", ").pop()}. The next one can run tomorrow.`, true);
  } else if (today) {
    setGo(true, "Today's search didn't finish, so it can be run again.");
  } else setGo(true, "One search a day. Today's is available.");
  renderHistory(body.searches);
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
    if (s.failed) leadsCell.append(el("span", "Failed", "tag")); else leadsCell.textContent = s.leads ?? "-";
    tr.append(el("td", s.when, "nowrap"), el("td", s.location), el("td", s.radius ? `${s.radius} mi` : ""),
              leadsCell, el("td", s.failed ? "" : s.new ?? "-"));
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
      for (const [k, v] of Object.entries(s.details || {})) {
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
    setGo(false, "Lost track of the search (the server may have restarted). Reload the page.", true); return;
  }
  showProgress(job);
  if (job.state === "running") { setTimeout(() => follow(jobId), 1500); return; }
  S.job = null;
  $("progress-card").hidden = true;
  if (job.state === "error") {
    S.error = job.message;
    $("search-failed-msg").textContent = job.message;
    $("search-failed").hidden = false;
    loadSearches();
    return;
  }
  $("done-card").hidden = false;
  $("done-big").textContent = job.saved ? `${job.new_leads.toLocaleString()} new leads`
                                        : `${job.leads.length.toLocaleString()} leads found`;
  $("done-sub").textContent = job.saved ? `${job.saved_count.toLocaleString()} saved leads in all, near ${job.location}`
                                        : `near ${job.location} (not saved)`;
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
  rows.push(["Search around", f.get("location") || "Arco Compactor"]);
  rows.push(["How far", `${f.get("radius") || "30"} miles`]);
  rows.push(["Search words", (f.get("keywords") || "").split(",").map((k) => k.trim()).filter(Boolean).join(", ") || "None"]);
  rows.push(["Where to look", SOURCE_TEXT[f.get("source")] || "Everywhere available"]);
  rows.push(["Leave out scores below", f.get("min_score")]);
  if (f.get("grid") && f.get("grid") !== "1") rows.push(["Coverage for Google and Yelp", `${f.get("grid")} searches`]);
  if (f.get("max_requests")) rows.push(["Most paid lookups", f.get("max_requests")]);
  if (f.get("only_keyword_matches")) rows.push(["Only", "businesses matching the search words"]);
  return rows;
}
$("form").addEventListener("submit", (e) => {
  e.preventDefault();
  if ($("go").disabled) return;
  const dl = $("confirm-list"); dl.replaceChildren();
  for (const [k, v] of searchSummary($("form"))) { const row = el("div"); row.append(el("dt", k), el("dd", v)); dl.append(row); }
  $("confirm-dlg").showModal();
  $("confirm-go").focus();
});
$("confirm-back").addEventListener("click", () => { $("confirm-dlg").close(); $("form").querySelector("input[name=location]").focus(); });
$("confirm-form").addEventListener("submit", (e) => { e.preventDefault(); $("confirm-dlg").close(); startSearch(); });
async function startSearch() {
  setGo(false, "Starting...");
  S.error = ""; $("search-failed").hidden = true;
  try {
    const body = await api("/search", { method: "POST", body: new FormData($("form")) });
    follow(body.job_id);
  } catch (err) {
    if (err.status === 429 && err.body && err.body.job_id) return follow(err.body.job_id);
    S.error = err.message;
    setGo(false, err.message, true);
    loadSearches();
  }
}
