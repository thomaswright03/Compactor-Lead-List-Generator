/* Part 5: the Stats page. */
async function loadStats() {
  let s;
  try { s = await api("/stats"); }
  catch (err) {
    if (err.status === 401) return;
    problem($("stats-problem"), "Can't show the stats right now", err.message, loadStats);
    $("stats-body").hidden = true; return;
  }
  $("stats-problem").replaceChildren(); $("stats-body").hidden = false;
  const empty = $("stats-empty");
  // Until something is marked, only the note that says how to get numbers shows.
  empty.hidden = s.checked > 0; $("stats-data").hidden = !s.checked;
  $("stats-left-out").hidden = !s.left_out;
  $("stats-left-out").textContent = `${s.left_out.toLocaleString()} competitor and own-company listing${s.left_out === 1 ? " is" : "s are"} ` +
    "left out of these numbers: they are flagged in the list, but they aren't prospects.";
  if (!s.checked) {
    empty.replaceChildren(s.saved
      ? emptyNote("No businesses checked yet.", "Mark businesses Yes or No on the Leads page to see these numbers.", "#leads", "Go to Leads")
      : emptyNote("No saved leads yet.", "Run a search on the Find leads page, then mark businesses Yes or No on the Leads page.", "#find", "Go to Find leads"));
    return;
  }
  $("s-total").textContent = s.with_equipment.toLocaleString();
  const avg = $("s-avg"); avg.replaceChildren();
  if (s.average_score === null) avg.textContent = "-";
  else { avg.append(String(s.average_score)); avg.append(el("small", ` / 100 · tier ${s.average_tier}`)); }
  drawChart(s.by_tier);
  const d = s.by_tier.find((t) => t.tier === "D");
  $("tier-d-note").hidden = !(d && !d.checked);
  $("tier-d-note").textContent = `Tier D (scores below ${s.tier_floors.C}) is usually empty: searches only save ` +
    `businesses scoring ${s.min_score} or more.`;
  const tbody = $("s-table").querySelector("tbody"); tbody.replaceChildren();
  for (const t of s.by_tier) {
    const tr = el("tr");
    tr.append(el("td", t.tier), el("td", t.checked), el("td", t.yes), el("td", t.pct === null ? "-" : `${t.pct}%`));
    tbody.append(tr);
  }
}

// Columns: one series (share with equipment), so one hue, no legend; the title names it.
let chartRows = null;
function drawChart(rows) {
  chartRows = rows;
  const css = getComputedStyle(document.documentElement), c = (n) => css.getPropertyValue(n).trim();
  const box = $("chart"); box.replaceChildren();
  const W = Math.max(300, Math.round(box.clientWidth || 560)), H = 260, L = 44, R = 12, T = 22, B = 46, NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Share of checked businesses with a baler or compactor, by tier: " +
    rows.map((r) => `${r.tier} ${r.pct === null ? "no data" : r.pct + "%"}`).join(", "));
  const node = (tag, attrs, text) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text !== undefined) n.textContent = text;
    svg.append(n); return n;
  };
  const y = (p) => T + (H - T - B) * (1 - p / 100);
  for (const p of [0, 25, 50, 75, 100]) {
    node("line", { x1: L, x2: W - R, y1: y(p), y2: y(p), stroke: p === 0 ? c("--chart-base") : c("--chart-grid"), "stroke-width": 1 });
    node("text", { x: L - 8, y: y(p) + 4, "text-anchor": "end", "font-size": 11, fill: c("--chart-text") }, `${p}%`);
  }
  const band = (W - L - R) / rows.length, bw = 24;
  const tip = el("div", undefined, "tip"); tip.hidden = true; box.append(tip);
  rows.forEach((r, i) => {
    const cx = L + band * i + band / 2;
    node("text", { x: cx, y: H - B + 18, "text-anchor": "middle", "font-size": 13, "font-weight": 700, fill: c("--ink") }, `Tier ${r.tier}`);
    node("text", { x: cx, y: H - B + 34, "text-anchor": "middle", "font-size": 11, fill: c("--chart-text") },
         r.checked ? `${r.yes} of ${r.checked}` : band < 90 ? "none" : "none checked");
    if (r.pct === null) return;
    const top = y(r.pct), h = Math.max(y(0) - top, r.pct > 0 ? 2 : 0), rad = Math.min(4, h / 2);
    // 4px rounded data-end, square at the baseline.
    const x0 = cx - bw / 2, x1 = cx + bw / 2, yb = y(0);
    const path = node("path", { d: `M${x0},${yb} V${top + rad} Q${x0},${top} ${x0 + rad},${top} H${x1 - rad} Q${x1},${top} ${x1},${top + rad} V${yb} Z`,
                                fill: c("--chart-bar") });
    node("text", { x: cx, y: top - 7, "text-anchor": "middle", "font-size": 12, "font-weight": 700, fill: c("--ink") }, `${r.pct}%`);
    // The hit target is the whole column band, bigger than the mark.
    const hit = node("rect", { x: cx - band / 2 + 4, y: T, width: band - 8, height: H - T - B, fill: "transparent", class: "col", tabindex: 0 });
    const show = () => {
      tip.replaceChildren(el("strong", `${r.pct}%`), document.createTextNode(` · Tier ${r.tier}: ${r.yes} of ${r.checked} have one`));
      tip.hidden = false;
      const rect = box.getBoundingClientRect(), scale = rect.width / W;
      tip.style.left = `${cx * scale}px`; tip.style.top = `${(top - 20) * scale}px`;
      path.setAttribute("opacity", ".8");
    };
    const hide = () => { tip.hidden = true; path.setAttribute("opacity", "1"); };
    hit.addEventListener("pointerenter", show); hit.addEventListener("focus", show);
    hit.addEventListener("pointerleave", hide); hit.addEventListener("blur", hide);
  });
  box.prepend(svg);
}

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (chartRows && !$("page-stats").hidden) drawChart(chartRows); }, 150);
});
