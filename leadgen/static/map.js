/* Part 6: the Map page. AARCO's area (the circle searches are meant for), the areas a search
   asks the free map data in (shaded by how the latest search of the area went), AARCO's own pin,
   and a pin for every saved business, coloured by its Yes / No answer, with toggles per group.
   The map is Leaflet (vendor/leaflet), loaded the first time the page opens; the street map
   comes from OpenStreetMap's tiles, and without them the rest is drawn on a plain background.
   Pins are drawn on one canvas, so a few thousand stay quick. */
// The names this part shares with the others (eslint.config.mjs reads this list).
/* exported loadMap, restyleMap */

// The pin groups (area_map.GROUPS), in drawing order: the biggest underneath, competitors on top.
const PIN_ORDER = ["unchecked", "no", "yes", "competitor"];
// What a pin's answer says in its box.
/** @type {Record<string, string>} */
const ANSWERS = { yes: "Yes", no: "No", unchecked: "Not checked yet" };
// The order of a pin's fields as the server sends them (area_map.FIELDS).
const PIN_FIELDS = "key,lat,lon,group,name,tier,score,phone,verified_phone,address,outcome,called,closed,kind";
const HIDDEN_KEY = "map-hidden";
// A pin's details box: kept clear of the zoom buttons when the map moves to show it.
const POPUP = { maxWidth: 300, autoPanPaddingTopLeft: /** @type {L.Point} */ ([64, 12]) };
const TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const TILE_CREDIT = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

/**
 * The map and what is drawn on it; null until the page first opens.
 * @typedef {{marker: L.Path, group: string}} PinShape
 * @typedef {{shape: L.Path, status: string}} AreaShape
 * @type {{map: L.Map | null, groups: Record<string, L.LayerGroup>, areas: L.LayerGroup | null,
 *         ring: L.Path | null, pins: PinShape[], shapes: AreaShape[], seq: number, loaded: boolean,
 *         tilesOk: number, tilesBad: number}}
 */
const M = { map: null, groups: {}, areas: null, ring: null, pins: [], shapes: [], seq: 0, loaded: false,
            tilesOk: 0, tilesBad: 0 };

/** @type {Promise<void> | null} */
let leafletLoading = null;
// Leaflet's script and styles, added to the page once (the Map page is the only one using them).
// (Not named `leaflet`: Leaflet's script takes that global name for itself.)
function loadLeaflet() {
  if (typeof L !== "undefined") return Promise.resolve();
  if (!leafletLoading) {
    const box = $("map");
    /** @type {(node: HTMLLinkElement | HTMLScriptElement) => Promise<void>} */
    const added = (node) => new Promise((resolve, reject) => {
      node.addEventListener("load", () => resolve());
      node.addEventListener("error", () => reject(new Error("load")));
      document.head.append(node);
    });
    const css = el("link"); css.rel = "stylesheet"; css.href = box.dataset.css;
    const js = el("script"); js.src = box.dataset.js;
    leafletLoading = Promise.all([added(css), added(js)]).then(() => undefined, () => {
      leafletLoading = null; css.remove(); js.remove();
      throw Object.assign(new Error("The map couldn't be loaded. Check your internet connection and try again."),
                          { status: 0 });
    });
  }
  return leafletLoading;
}

// A colour of the theme shown (templates/_theme.html), for what the canvas draws.
/** @type {(name: string) => string} */
const themeColour = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/** @param {string} group @returns {L.PathOptions} */
function pinStyle(group) {
  return { radius: 6, color: themeColour("--pin-ring"), weight: 1.5, opacity: 1,
           fillColor: themeColour(`--pin-${group}`), fillOpacity: 1 };
}
// The search areas: a solid edge when searched, dashed when (partly) missed, dotted when not known.
/** @param {string} status @returns {L.PathOptions} */
function areaStyle(status) {
  const done = themeColour("--area-done"), miss = themeColour("--area-miss");
  if (status === "searched") {
    return { color: done, weight: 1.5, opacity: 0.85, fill: true, fillColor: done, fillOpacity: 0.07, dashArray: null };
  }
  if (status === "partly" || status === "missed" || status === "asking") {
    return { color: miss, weight: 2.5, opacity: 0.95, fill: true, fillColor: miss,
             fillOpacity: status === "partly" ? 0.12 : 0.22, dashArray: "8 6" };
  }
  return { color: themeColour("--area-unknown"), weight: 1.5, opacity: 0.9, fill: false, dashArray: "2 6" };
}
const ringStyle = () => ({ color: themeColour("--ink"), weight: 2.5, opacity: 0.9, fill: false, dashArray: null });

// The theme changed (Light / Dark / System): the canvas takes the new colours.
function restyleMap() {
  if (!M.map) return;
  if (M.ring) M.ring.setStyle(ringStyle());
  for (const { shape, status } of M.shapes) shape.setStyle(areaStyle(status));
  /** @type {Record<string, L.PathOptions>} */
  const styles = {};
  for (const { marker, group } of M.pins) marker.setStyle(styles[group] || (styles[group] = pinStyle(group)));
}

/** @param {string} text */
function mapStatus(text) { $("map-loading").textContent = text; $("map-loading").hidden = !text; }

async function loadMap() {
  const seq = ++M.seq;
  if (!M.loaded) mapStatus("Loading the map…");
  /** @type {MapData} */
  let data;
  try {
    [, data] = await Promise.all([loadLeaflet(), api("/map-data")]);
  } catch (e) {
    const err = /** @type {ApiError} */ (e);
    if (seq !== M.seq || err.status === 401) return;
    mapStatus("");
    problem($("map-problem"), "Can't show the map right now", err.message, loadMap);
    $("map-body").hidden = !M.loaded;
    return;
  }
  if (seq !== M.seq) return;
  if (data.fields.join(",") !== PIN_FIELDS) {                   // a page older than the server
    mapStatus("");
    problem($("map-problem"), "The site was updated", "Reload the page to see the map.", () => location.reload());
    return;
  }
  $("map-problem").replaceChildren();
  $("map-body").hidden = false;
  mapStatus("");
  drawMap(data);
}

/** @param {MapData} data */
function drawMap(data) {
  const first = !M.map;
  const map = M.map || makeMap();
  map.invalidateSize();                     // the page was hidden when the map was made
  map.closePopup();
  if (M.areas) M.areas.remove();
  if (M.ring) M.ring.remove();
  for (const group of Object.values(M.groups)) group.remove();
  M.pins = []; M.shapes = []; M.groups = {};

  // The search areas under everything else, then the circle around AARCO.
  const shapes = L.svg({ padding: 0.5 });
  M.areas = L.layerGroup();
  for (const area of data.areas) {
    const shape = L.polygon(area.outline, { renderer: shapes, interactive: false, ...areaStyle(area.status) });
    M.shapes.push({ shape, status: area.status });
    M.areas.addLayer(shape);
    const label = el("div", undefined, `area-name ${area.status}`);
    label.append(el("span", area.name));
    if (area.status !== "searched") label.append(el("small", area.status === "unknown" ? "not known" : area.status ===
      "partly" ? "partly searched" : area.status === "asking" ? "being asked again" : "not searched"));
    M.areas.addLayer(L.marker(area.label, { icon: L.divIcon({ className: "area-label", html: label, iconSize: null }),
                                             interactive: false, keyboard: false }));
  }
  M.ring = L.polygon(data.circle, { renderer: shapes, interactive: false, ...ringStyle() }).addTo(map);
  if ($("map-show-areas").checked) M.areas.addTo(map);

  // The businesses: Yes / No / Not checked as dots on one canvas (quick for thousands), competitors
  // as hollow squares, each opening its details when tapped.
  const coarse = window.matchMedia("(pointer: coarse)").matches;
  const dots = L.canvas({ padding: 0.5, tolerance: coarse ? 10 : 4 });
  for (const group of PIN_ORDER) M.groups[group] = L.layerGroup();
  /** @type {Record<string, L.PathOptions>} */
  const styles = {};
  for (const row of data.pins) {
    const [, lat, lon, group, name] = row;
    const layer = M.groups[group];
    if (!layer) continue;
    if (group === "competitor") {
      const square = L.marker([lat, lon], { icon: L.divIcon({ className: "pin-comp", iconSize: [14, 14] }),
                                            title: name, keyboard: true, riseOnHover: true });
      square.bindPopup(() => pinBox(row), POPUP);
      layer.addLayer(square);
      continue;
    }
    const dot = L.circleMarker([lat, lon], { renderer: dots, bubblingMouseEvents: false,
                                             ...(styles[group] || (styles[group] = pinStyle(group))) });
    dot.bindPopup(() => pinBox(row), POPUP);
    M.pins.push({ marker: dot, group });
    layer.addLayer(dot);
  }
  const hidden = hiddenGroups();
  for (const group of PIN_ORDER) {
    $(`map-n-${group}`).textContent = `(${(data.counts[group] || 0).toLocaleString()})`;
    const box = /** @type {HTMLInputElement} */ (document.querySelector(`#map-groups input[data-group="${group}"]`));
    box.checked = !hidden.includes(group);
    if (box.checked) M.groups[group].addTo(map);
  }
  aarcoPin(map, data);

  $("map-coverage").textContent = data.coverage;
  $("map-no-position").hidden = !data.no_position;
  $("map-no-position").textContent = data.no_position === 1
    ? "1 saved business has no map position, so it isn't on the map (it is on the Leads page)."
    : `${data.no_position.toLocaleString()} saved businesses have no map position, so they aren't on the map ` +
      "(they are on the Leads page).";
  $("map-aarco-address").textContent = `, ${data.aarco.address}`;
  areaList(data.areas);
  if (first) map.fitBounds(L.latLngBounds(data.circle), { padding: [8, 8] });
  placeLabels();
  M.loaded = true;
}

// The area names that fit at this zoom: nearest the centre first, each one shown only where it
// covers no name already shown and not AARCO's pin, and stays inside the map; the others wait
// until the map is zoomed in (the list under the map names every area).
function placeLabels() {
  const edge = $("map").getBoundingClientRect();
  if (!M.map || !edge.width) return;                  // the page is hidden: placed when shown
  const taken = [...document.querySelectorAll("#map .aarco-mark svg, #map .aarco-name")]
    .map((node) => node.getBoundingClientRect());
  for (const name of document.querySelectorAll("#map .area-name")) {
    name.classList.remove("crowded");
    const box = name.getBoundingClientRect();
    const inside = box.left >= edge.left && box.right <= edge.right && box.top >= edge.top && box.bottom <= edge.bottom;
    const clear = taken.every((t) => box.right <= t.left || box.left >= t.right || box.bottom <= t.top ||
                                     box.top >= t.bottom);
    if (inside && clear) taken.push(box);
    else name.classList.add("crowded");
  }
}

function makeMap() {
  const map = L.map($("map"), { preferCanvas: true, zoomSnap: 0.25, zoomDelta: 0.5, minZoom: 5, maxZoom: 18 });
  map.on("zoomend resize", placeLabels);
  map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a>');
  const tiles = L.tileLayer(TILES, { attribution: TILE_CREDIT, maxZoom: 19 });
  // Without the street map (offline, or the tile servers blocked) the rest still shows, on a plain
  // background, and the page says why the streets are missing.
  tiles.on("tileload", () => { M.tilesOk++; $("map-tiles-note").hidden = true; });
  tiles.on("tileerror", () => { M.tilesBad++; $("map-tiles-note").hidden = M.tilesOk > 0 || M.tilesBad < 2; });
  tiles.addTo(map);
  M.map = map;
  return map;
}

// AARCO's own pin: a larger marker of its own shape, labelled, always on top.
/** @type {L.Marker | null} */
let aarcoMarker = null;
/** @param {L.Map} map @param {MapData} data */
function aarcoPin(map, data) {
  if (aarcoMarker) aarcoMarker.remove();
  const html = el("div", undefined, "aarco-mark");
  html.innerHTML = '<svg viewBox="0 0 32 42" width="32" height="42" aria-hidden="true" focusable="false">' +
    '<path class="aarco-body" d="M16 1.5C7.9 1.5 1.5 7.8 1.5 15.8 1.5 26.6 16 40.5 16 40.5S30.5 26.6 30.5 15.8' +
    'C30.5 7.8 24.1 1.5 16 1.5z"/>' +
    '<path class="aarco-star" d="M16 7.6l2.4 5 5.5.7-4 3.8 1 5.4L16 19.9l-4.9 2.6 1-5.4-4-3.8 5.5-.7z"/></svg>';
  html.append(el("span", data.aarco.name, "aarco-name"));
  aarcoMarker = L.marker([data.aarco.lat, data.aarco.lon], {
    icon: L.divIcon({ className: "aarco-pin", html, iconSize: [32, 42], iconAnchor: [16, 41], popupAnchor: [0, -38] }),
    title: `${data.aarco.name}: ${data.aarco.address}`, keyboard: true, zIndexOffset: 1000 });
  aarcoMarker.bindPopup(() => {
    const box = el("div", undefined, "pin-box");
    box.append(el("strong", data.aarco.company), el("div", data.aarco.address),
               el("div", `The ${data.miles}-mile circle is AARCO's area: searches are meant for it, and Miles on ` +
                         "the Leads page are measured from here.", "sub"));
    return box;
  }, { ...POPUP, maxWidth: 280 });
  aarcoMarker.addTo(map);
}

// A business's details, opened from its pin.
/** @param {MapPinRow} row */
function pinBox(row) {
  const [key, , , group, name, tier, score, phone, verified, address, outcome, called, closed, kind] = row;
  const box = el("div", undefined, "pin-box");
  box.append(el("strong", name));
  if (group === "competitor") {
    box.append(el("div", kind === "Competitor" ? "Competitor: flagged, not a prospect" : "AARCO's own listing", "sub"));
  } else {
    const word = TIER_WORDS[tier] || "";
    box.append(el("div", `Tier ${tier}${word ? ` (${word})` : ""} · score ${score}`, "sub"));
    const answer = el("div", "Has a baler or compactor: ");
    answer.append(el("strong", ANSWERS[group] || ANSWERS.unchecked));
    box.append(answer);
  }
  if (closed) box.append(el("div", "Closed for good", "flag"));
  const tel = el("div");
  tel.append(phone ? phoneLink(phone) : el("span", "No phone listed", "sub"));
  box.append(tel);
  if (verified) {
    const v = el("div", "Verified: ");
    v.append(phoneLink(verified));
    box.append(v);
  }
  if (address) box.append(el("div", address));
  box.append(el("div", outcome ? `Latest call: ${outcome}, ${called}` : "No calls logged yet", outcome ? "" : "sub"));
  const open = el("a", "Open on the Leads page");
  open.href = `#leads?tab=all&lead=${encodeURIComponent(key)}`;
  open.className = "open-lead";
  box.append(open);
  return box;
}

// The search areas in words (the same as on the map, for reading and for screen readers).
/** @param {MapArea[]} areas */
function areaList(areas) {
  $("map-areas").replaceChildren(...areas.map((area) => {
    const li = el("li");
    li.append(el("span", undefined, `sym area ${area.status === "asking" || area.status === "missed" ? "partly"
                                                 : area.status}`));
    const text = el("div");
    text.append(el("strong", `${area.n}. ${area.name}`), " · ", el("span", area.status_text));
    if (area.towns.length) text.append(el("div", `Also ${area.towns.join(", ")}`, "sub"));
    li.append(text);
    return li;
  }));
}

// The groups switched off in this browser (remembered, like the colour theme).
/** @returns {string[]} */
function hiddenGroups() {
  try {
    const hidden = JSON.parse(localStorage.getItem(HIDDEN_KEY) || "[]");
    return Array.isArray(hidden) ? hidden.filter((g) => typeof g === "string") : [];
  } catch (e) { return []; }
}
$all("#map-groups input[data-group]").forEach((box) => box.addEventListener("change", () => {
  const input = /** @type {HTMLInputElement} */ (box), group = input.dataset.group || "";
  const layer = M.groups[group];
  if (M.map && layer) { if (input.checked) M.map.addLayer(layer); else M.map.removeLayer(layer); }
  const hidden = hiddenGroups().filter((g) => g !== group).concat(input.checked ? [] : [group]);
  try { localStorage.setItem(HIDDEN_KEY, JSON.stringify(hidden)); } catch (e) { /* kept for this visit only */ }
}));
$("map-show-areas").addEventListener("change", () => {
  if (!M.map || !M.areas) return;
  if ($("map-show-areas").checked) { M.map.addLayer(M.areas); placeLabels(); } else M.map.removeLayer(M.areas);
});
