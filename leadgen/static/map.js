/* Part 6: the Map page. AARCO's area (the circle searches are meant for), the areas a search
   asks the free map data in (shaded by how the latest search of the area went), AARCO's own pin,
   and a pin for every saved business, coloured by its Yes / No answer, with toggles per group.
   The map is Leaflet (vendor/leaflet), loaded the first time the page opens; the street map
   comes from OpenStreetMap's tiles, and without them the rest is drawn on a plain background.
   Pins are drawn on one canvas. Zoomed out, pins close together are grouped into one circle with
   their count (placePins: a grid of CLUSTER_CELL px squares per zoom), which splits into single
   pins as the map is zoomed in; only what is in or near the view is drawn, so tens of thousands
   of businesses stay quick. */
// The names this part shares with the others (eslint.config.mjs reads this list).
/* exported loadMap, restyleMap */

// The pin groups (area_map.GROUPS), in drawing order: the biggest underneath, competitors on top.
const PIN_ORDER = ["unchecked", "no", "yes", "competitor"];
// What a pin's answer says in its box.
/** @type {Record<string, string>} */
const ANSWERS = { yes: "Yes", no: "No", unchecked: "Not checked yet" };
// A group's pins in a circle's description ("12 Yes, 3 No, 40 not checked yet").
/** @type {Record<string, string>} */
const MIX_WORDS = { yes: "Yes", no: "No", unchecked: "not checked yet",
                    competitor: "competitors or AARCO" };
// The order of a pin's fields as the server sends them (area_map.FIELDS).
const PIN_FIELDS = "key,lat,lon,group,name,tier,score,phone,verified_phone,address,outcome," +
  "called,closed,kind";
const HIDDEN_KEY = "map-hidden";
// A pin's details box: kept clear of the zoom buttons when the map moves to show it, and never
// wider or taller than the room that leaves (fitPopups: a phone's map is narrower than 300 px).
/** @type {L.Point} */
const PAD_TOP_LEFT = [64, 12];
/** @type {L.Point} */
const PAD_BOTTOM_RIGHT = [8, 8];
const POPUP = { maxWidth: 300, minWidth: 200, autoPanPaddingTopLeft: PAD_TOP_LEFT,
                autoPanPaddingBottomRight: PAD_BOTTOM_RIGHT };
// What a box adds around its text: the text's margins and the box's 1 px edge (app.css), the
// 20 px tip under it (Leaflet's .leaflet-popup margin), and 2 px to spare.
const POPUP_EDGE = { x: 16 + 24 + 2 + 2, y: 12 + 12 + 2 + 20 + 2 };
const TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const TILE_CREDIT = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> ' +
  "contributors";
// Pins are grouped by squares of this many pixels on screen (two pins in one square are one circle
// with their count)...
const CLUSTER_CELL = 56;
// ...below this zoom (a few streets across); from it on, every pin is drawn on its own.
const CLUSTER_UNTIL = 15;
// Pins grouped at one spot (the same address): drawn as one circle at every zoom, opening a list.
const SAME_SPOT_PX = 6;
// Only what is within this share of the map's size around the view is drawn (redrawn as it moves).
const VIEW_PAD = 0.5;
// How far above the pins the area names are drawn (AARCO's pin is above both).
const AREA_NAME_Z = 500;
// Where an area's name may move to when circles cover its own spot: every NAME_STEP px up to
// NAME_REACH px each way, nearest first (placeLabels), keeping NAME_GAP px clear of what it avoids.
const NAME_STEP = 8, NAME_REACH = 160, NAME_GAP = 3;
/** @type {L.Point[]} */
const NAME_SPOTS = [];
for (let dx = -NAME_REACH; dx <= NAME_REACH; dx += NAME_STEP) {
  for (let dy = -NAME_REACH; dy <= NAME_REACH; dy += NAME_STEP) NAME_SPOTS.push([dx, dy]);
}
NAME_SPOTS.sort((a, b) => a[0] ** 2 + a[1] ** 2 - b[0] ** 2 - b[1] ** 2);

/**
 * The map and what is drawn on it; null until the page first opens.
 * @typedef {{row: MapPinRow, group: string, x: number, y: number, marker: L.Layer | null}} Pin
 *   x, y: where it is on the whole world's map at zoom 0 (a zoom's pixels are these times 2^zoom)
 * @typedef {{marker: L.Marker, members: Pin[]}} Cluster
 * @typedef {{shape: L.Path, status: string, name: HTMLElement, outline: L.Point[]}} AreaShape
 *   name: the area's name on the map; outline: its edge, as [lat, lon] points
 * @type {{map: L.Map | null, groups: Record<string, L.LayerGroup>, areas: L.LayerGroup | null,
 *         ring: L.Path | null, pins: Pin[], shown: Set<Pin>, clusters: L.LayerGroup | null,
 *         clustered: Map<string, Cluster>, dots: L.Renderer | null,
 *         styles: Record<string, L.PathOptions>, shapes: AreaShape[], seq: number,
 *         loaded: boolean, tilesOk: number, tilesBad: number}}
 */
const M = { map: null, groups: {}, areas: null, ring: null, pins: [], shown: new Set(),
            clusters: null, clustered: new Map(), dots: null, styles: {}, shapes: [], seq: 0,
            loaded: false, tilesOk: 0, tilesBad: 0 };

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
      throw Object.assign(new Error(`The map couldn't be loaded. ${noAnswer()}`),
                          { status: 0 });
    });
  }
  return leafletLoading;
}

// A colour of the theme shown (templates/_theme.html), for what the canvas draws.
/** @type {(name: string) => string} */
const themeColour = (name) => getComputedStyle(document.documentElement).getPropertyValue(name)
  .trim();

/** @param {string} group @returns {L.PathOptions} */
function pinStyle(group) {
  return { radius: 6, color: themeColour("--pin-ring"), weight: 1.5, opacity: 1,
           fillColor: themeColour(`--pin-${group}`), fillOpacity: 1 };
}
// The search areas: a solid edge when searched, dashed when (partly) missed, dotted when not
// searched at all yet ("no search yet") or not known.
/** @param {string} status @returns {L.PathOptions} */
function areaStyle(status) {
  const done = themeColour("--area-done"), miss = themeColour("--area-miss");
  if (status === "searched") {
    return { color: done, weight: 1.5, opacity: 0.85, fill: true, fillColor: done,
             fillOpacity: 0.07, dashArray: null };
  }
  if (status === "partly" || status === "missed" || status === "asking") {
    return { color: miss, weight: 2.5, opacity: 0.95, fill: true, fillColor: miss,
             fillOpacity: status === "partly" ? 0.12 : 0.22, dashArray: "8 6" };
  }
  return { color: themeColour("--area-unknown"), weight: 1.5, opacity: 0.9, fill: false,
           dashArray: "2 6" };
}
const ringStyle = () => ({ color: themeColour("--ink"), weight: 2.5, opacity: 0.9, fill: false,
                           dashArray: null });

// The theme changed (Light / Dark / System): the canvas takes the new colours.
function restyleMap() {
  if (!M.map) return;
  if (M.ring) M.ring.setStyle(ringStyle());
  for (const { shape, status } of M.shapes) shape.setStyle(areaStyle(status));
  // The dots made so far (the others are made in the new colours); the groups' circles take
  // the theme's colours by themselves.
  M.styles = {};
  for (const { marker, group } of M.pins) {
    if (marker && group !== "competitor") /** @type {L.Path} */ (marker).setStyle(dotStyle(group));
  }
}
/** A group's dot style, made once per theme. @param {string} group */
const dotStyle = (group) => M.styles[group] || (M.styles[group] = pinStyle(group));

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
    problem($("map-problem"), "Can't show the map right now", err.message, loadMap, loadMap);
    $("map-body").hidden = !M.loaded;
    return;
  }
  if (seq !== M.seq) return;
  if (data.fields.join(",") !== PIN_FIELDS) {                   // a page older than the server
    mapStatus("");
    problem($("map-problem"), "The site was updated", "Reload the page to see the map.",
            () => location.reload());
    return;
  }
  $("map-problem").replaceChildren();
  $("map-body").hidden = false;
  mapStatus("");
  drawMap(data);
}

// An area's status under its name, in words ("not searched"); nothing when searched in full.
/** @param {string} status */
function areaStatusWord(status) {
  return status === "never" ? "no search yet" : status === "unknown" ? "not known"
    : status === "partly" ? "partly searched" : status === "asking" ? "filling in"
      : "not searched";
}
// The search areas under everything else, then the circle around AARCO.
/** @param {L.Map} map @param {MapData} data */
function drawAreas(map, data) {
  const shapes = L.svg({ padding: 0.5 });
  M.areas = L.layerGroup();
  for (const area of data.areas) {
    const shape = L.polygon(area.outline, { renderer: shapes, interactive: false,
                                            ...areaStyle(area.status) });
    M.areas.addLayer(shape);
    const label = el("div", undefined, `area-name ${area.status}`);
    label.append(el("span", area.name));
    if (area.status !== "searched") label.append(el("small", areaStatusWord(area.status)));
    M.shapes.push({ shape, status: area.status, name: label, outline: area.outline });
    // Above the groups' circles (a tap goes through the name to them), under AARCO's pin.
    M.areas.addLayer(L.marker(area.label, {
      icon: L.divIcon({ className: "area-label", html: label, iconSize: null }),
      interactive: false, keyboard: false, zIndexOffset: AREA_NAME_Z }));
  }
  M.ring = L.polygon(data.circle, { renderer: shapes, interactive: false, ...ringStyle() })
    .addTo(map);
  if ($("map-show-areas").checked) M.areas.addTo(map);
}
// The businesses: Yes / No / Not checked as dots on one canvas, competitors as hollow squares,
// each opening its details when tapped; close together, grouped (placePins). Each is made the
// first time it is drawn on its own.
/** @param {L.Map} map @param {MapData} data */
function drawPins(map, data) {
  const coarse = window.matchMedia("(pointer: coarse)").matches;
  M.dots = L.canvas({ padding: 0.5, tolerance: coarse ? 10 : 4 });
  M.styles = {};
  for (const group of PIN_ORDER) M.groups[group] = L.layerGroup();
  for (const row of data.pins) {
    const [, lat, lon, group] = row;
    if (!M.groups[group]) continue;
    const { x, y } = map.project([lat, lon], 0);
    M.pins.push({ row, group, x, y, marker: null });
  }
  M.clusters = L.layerGroup().addTo(map);
  const hidden = hiddenGroups();
  for (const group of PIN_ORDER) {
    $(`map-n-${group}`).textContent = `(${(data.counts[group] || 0).toLocaleString()})`;
    groupBox(group).checked = !hidden.includes(group);
    if (groupBox(group).checked) M.groups[group].addTo(map);
  }
  clusterNote();
}
/** @param {MapData} data */
function drawMap(data) {
  const first = !M.map;
  const map = M.map || makeMap();
  map.invalidateSize();                     // the page was hidden when the map was made
  fitPopups();
  map.closePopup();
  if (M.areas) M.areas.remove();
  if (M.ring) M.ring.remove();
  if (M.clusters) M.clusters.remove();
  for (const group of Object.values(M.groups)) group.remove();
  M.pins = []; M.shapes = []; M.groups = {}; M.shown = new Set(); M.clustered = new Map();
  drawAreas(map, data);
  drawPins(map, data);
  aarcoPin(map, data);

  $("map-coverage").textContent = data.coverage;
  // No saved business at all: say how the map fills up (AARCO and its areas are still drawn).
  const none = !data.pins.length && !data.no_position;
  $("map-empty").hidden = !none;
  $("map-empty").replaceChildren(...(none ? [emptyNote("No saved businesses yet.",
    "Run today's search on the Find leads page: each business it finds gets a dot on this map.",
    "#find", "Go to Find leads", "pin")] : []));
  $("map-no-position").hidden = !data.no_position;
  $("map-no-position").textContent = data.no_position === 1
    ? "1 saved business has no map position, so it isn't on the map (it is on the Leads page)."
    : `${data.no_position.toLocaleString()} saved businesses have no map position, so they ` +
      "aren't on the map (they are on the Leads page).";
  $("map-aarco-address").textContent = `, ${data.aarco.address}`;
  areaList(data.areas);
  if (first) map.fitBounds(L.latLngBounds(data.circle), { padding: [8, 8] });
  placePins();
  placeLabels();
  M.loaded = true;
}

/** A pin group's tick box in the key. @param {string} group @returns {HTMLInputElement} */
const groupBox = (group) => /** @type {HTMLInputElement} */ (
  document.querySelector(`#map-groups input[data-group="${group}"]`));

// What a circle with a number means, said once under the key.
function clusterNote() {
  if ($("map-cluster-note")) return;
  const note = el("p", "Businesses close together show as one circle with their number. " +
                       "Tap it, or zoom in, to see each one.",
                "sub");
  note.id = "map-cluster-note";
  $("map-groups").append(note);
}

// The pins of the groups switched on that are in or near the view, by the square of the grid
// (CLUSTER_CELL px, fixed to the world so it doesn't shift as the map moves) each is in.
/** @param {L.Map} map @param {number} scale the zoom's pixels per zoom-0 pixel
    @returns {Map<string, Pin[]>} */
function pinCells(map, scale) {
  const view = map.getPixelBounds(), size = map.getSize();
  const lo = [view.min.x - size.x * VIEW_PAD, view.min.y - size.y * VIEW_PAD];
  const hi = [view.max.x + size.x * VIEW_PAD, view.max.y + size.y * VIEW_PAD];
  /** @type {Record<string, boolean>} */
  const on = {};
  for (const group of PIN_ORDER) on[group] = groupBox(group).checked;
  /** @type {Map<string, Pin[]>} */
  const cells = new Map();
  for (const pin of M.pins) {
    if (!on[pin.group]) continue;
    const x = pin.x * scale, y = pin.y * scale;
    // A whole square is in or out, so a circle's count never depends on where the view's edge is.
    const cx = Math.floor(x / CLUSTER_CELL), cy = Math.floor(y / CLUSTER_CELL);
    if ((cx + 1) * CLUSTER_CELL < lo[0] || cx * CLUSTER_CELL > hi[0] ||
        (cy + 1) * CLUSTER_CELL < lo[1] || cy * CLUSTER_CELL > hi[1]) continue;
    const key = `${cx},${cy}`;
    const cell = cells.get(key);
    if (cell) cell.push(pin); else cells.set(key, [pin]);
  }
  return cells;
}
// Draws the pins in and near the view at this zoom: those close together as one circle with their
// count (the pins of one square of the grid, pinCells), the others on their own. Only what changed
// is redrawn, so a pin whose details are open stays open while the map moves to show them.
function placePins() {
  const map = M.map;
  if (!map || !M.clusters || !map.getSize().x) return;   // the page is hidden: placed when shown
  const zoom = map.getZoom(), grouping = zoom < CLUSTER_UNTIL;
  const cells = pinCells(map, 2 ** zoom);
  /** @type {Set<Pin>} */
  const single = new Set();
  /** @type {Map<string, Pin[]>} */
  const grouped = new Map();
  for (const [key, members] of cells) {
    if (members.length > 1 && (grouping || sameSpot(members))) {
      grouped.set(`${zoom}|${key}|${members.length}`, members);
    } else for (const pin of members) single.add(pin);
  }
  for (const pin of M.shown) {
    if (!single.has(pin) && pin.marker) M.groups[pin.group].removeLayer(pin.marker);
  }
  for (const pin of single) if (!M.shown.has(pin)) M.groups[pin.group].addLayer(pinMarker(pin));
  M.shown = single;
  for (const [key, cluster] of M.clustered) {
    if (!grouped.has(key)) { M.clusters.removeLayer(cluster.marker); M.clustered.delete(key); }
  }
  for (const [key, members] of grouped) {
    if (M.clustered.has(key)) continue;
    const marker = clusterMarker(members);
    M.clusters.addLayer(marker);
    M.clustered.set(key, { marker, members });
  }
}

// A business's own pin: a dot, or a competitor's square.
/** @param {Pin} pin @returns {L.Layer} */
function pinMarker(pin) {
  if (pin.marker) return pin.marker;
  const row = pin.row, [, lat, lon, group, name] = row;
  const marker = group === "competitor"
    ? L.marker([lat, lon], { icon: L.divIcon({ className: "pin-comp", iconSize: [14, 14] }),
                             title: name, keyboard: true, riseOnHover: true })
    : L.circleMarker([lat, lon], { renderer: M.dots || undefined, bubblingMouseEvents: false,
                                   ...dotStyle(group) });
  marker.bindPopup(() => pinBox(row), POPUP);
  pin.marker = marker;
  return marker;
}

/** True when the pins are all at one spot, still together at the closest zoom.
    @param {Pin[]} pins */
function sameSpot(pins) {
  const xs = pins.map((p) => p.x), ys = pins.map((p) => p.y), most = 2 ** CLUSTER_UNTIL;
  return (Math.max(...xs) - Math.min(...xs)) * most < SAME_SPOT_PX &&
    (Math.max(...ys) - Math.min(...ys)) * most < SAME_SPOT_PX;
}

// A circle's picture: its count, with a ring in its groups' colours (in proportion).
/** @param {Record<string, number>} counts pins per group @param {number} n pins in all
    @param {number} d the circle's size (px) */
function clusterIcon(counts, n, d) {
  let from = 0;
  const ring = PIN_ORDER.filter((g) => counts[g]).map((g) => {
    const to = from + counts[g] / n * 100;
    const part = `var(--pin-${g === "competitor" ? "comp" : g}) ${from}% ${to}%`;
    from = to;
    return part;
  });
  const html = el("div");
  Object.assign(html.style, { width: "100%", height: "100%", borderRadius: "50%",
                              display: "grid", placeItems: "center",
                              background: `conic-gradient(${ring.join(", ")})`,
                              boxShadow: "0 0 0 1.5px var(--pin-ring), " +
                                         "0 1px 3px rgba(0, 0, 0, .35)" });
  const count = el("span", n < 1000 ? n.toLocaleString()
                         : `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k`);
  Object.assign(count.style, { width: `${d - 10}px`, height: `${d - 10}px`, borderRadius: "50%",
                               display: "grid", placeItems: "center", background: "var(--card)",
                               color: "var(--ink)", fontWeight: "800", fontSize: "var(--fs-xs)",
                               lineHeight: "1", fontVariantNumeric: "tabular-nums" });
  html.append(count);
  return html;
}
// A group of pins: a circle with their count, its ring in their groups' colours (in proportion),
// at their middle. Tapping it zooms in to them; for businesses at one spot it lists them instead.
/** @param {Pin[]} members @returns {L.Marker} */
function clusterMarker(members) {
  /** @type {Record<string, number>} */
  const counts = {};
  let lat = 0, lon = 0;
  for (const pin of members) {
    counts[pin.group] = (counts[pin.group] || 0) + 1;
    lat += pin.row[1]; lon += pin.row[2];
  }
  const n = members.length, d = n < 10 ? 30 : n < 100 ? 36 : n < 1000 ? 42 : 48;
  const html = clusterIcon(counts, n, d);
  const mix = PIN_ORDER.filter((g) => counts[g])
    .map((g) => `${counts[g].toLocaleString()} ${MIX_WORDS[g]}`);
  const together = sameSpot(members);
  const marker = L.marker([lat / n, lon / n], {
    icon: L.divIcon({ className: "pin-cluster", html, iconSize: [d, d] }), keyboard: true,
    riseOnHover: true,
    title: `${n.toLocaleString()} businesses ${together ? "at this spot" : "close together"} ` +
           `(${mix.join(", ")}). ` + (together ? "Open the list." : "Zoom in to see each one.") });
  if (together) {
    const box = el("div");
    marker.bindPopup(box, POPUP);
    spotList(members, box, () => {
      const popup = marker.getPopup(); if (popup) popup.setContent(box);
    });
  } else marker.on("click", () => zoomTo(members));
  return marker;
}

// Zoom in to a group's pins, so they split into smaller groups or single pins.
/** @param {Pin[]} members */
function zoomTo(members) {
  if (!M.map) return;
  const bounds = L.latLngBounds(members.map((p) => /** @type {L.Point} */ ([p.row[1], p.row[2]])));
  M.map.fitBounds(bounds, { padding: [48, 48], maxZoom: CLUSTER_UNTIL });
}

// Businesses at one spot: a list of their names in `box`, each opening its details there, with a
// way back to the list; `changed` fits the details box to what it now shows.
/** @param {Pin[]} members @param {HTMLElement} box @param {() => void} changed */
function spotList(members, box, changed) {
  const list = el("div", undefined, "pin-box"), items = el("ul");
  list.append(el("strong", `${members.length.toLocaleString()} businesses at this spot`), items);
  for (const pin of members) {
    const li = el("li");
    li.append(button(pin.row[4], "link", () => {
      const details = pinBox(pin.row);
      details.append(button("Back to the list", "link", () => {
        box.replaceChildren(list); changed();
      }));
      box.replaceChildren(details);
      changed();
    }));
    items.append(li);
  }
  box.replaceChildren(list);
}

// The area names, placed again whenever the map moves or its circles change: nearest the centre
// first, each in free space inside its own area, so a circle with a count never covers a name and
// its status. A name stays where it is drawn when that spot is clear, and otherwise moves to the
// nearest clear spot (NAME_SPOTS) whose middle is still inside its area. Clear means whole inside
// the map and covering no circle, competitor's square, AARCO's pin, map button or name already
// placed. A name with no clear spot waits until the map is zoomed in (the list under the map
// names every area with its status).
function placeLabels() {
  const map = M.map, edge = $("map").getBoundingClientRect();
  // Hidden: placed when shown.
  if (!map || !M.areas || !edge.width || !map.hasLayer(M.areas)) return;
  const zoom = map.getZoom(), origin = map.getPixelBounds().min;
  /** @type {{left: number, right: number, top: number, bottom: number}[]} */
  const taken = [...document.querySelectorAll("#map .pin-cluster, #map .pin-comp, " +
                                              "#map .aarco-mark svg, #map .aarco-name, " +
                                              "#map .leaflet-control")]
    .map((node) => node.getBoundingClientRect());
  for (const { name, outline } of M.shapes) {
    name.classList.remove("crowded");
    name.style.left = name.style.top = "";
    const box = name.getBoundingClientRect();
    /** @type {L.Point[]} */
    const ring = outline.map((at) => {
      const p = map.project(at, zoom);
      return [p.x - origin.x + edge.left, p.y - origin.y + edge.top];
    });
    const spot = NAME_SPOTS.find(([dx, dy]) => {
      const left = box.left + dx, right = box.right + dx, top = box.top + dy;
      const bottom = box.bottom + dy;
      return left >= edge.left && right <= edge.right && top >= edge.top && bottom <= edge.bottom &&
        taken.every((t) => right + NAME_GAP <= t.left || left - NAME_GAP >= t.right ||
                           bottom + NAME_GAP <= t.top || top - NAME_GAP >= t.bottom) &&
        within([(left + right) / 2, (top + bottom) / 2], ring);
    });
    if (!spot) { name.classList.add("crowded"); continue; }
    name.style.left = `${spot[0]}px`;
    name.style.top = `${spot[1]}px`;
    taken.push({ left: box.left + spot[0], right: box.right + spot[0], top: box.top + spot[1],
                 bottom: box.bottom + spot[1] });
  }
  $("map-names-note").hidden = !M.shapes.some(({ name }) => name.classList.contains("crowded"));
}

/** True when the point is inside the polygon (in the same pixels).
    @param {L.Point} p @param {L.Point[]} ring */
function within([x, y], ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [ax, ay] = ring[i], [bx, by] = ring[j];
    if ((ay > y) !== (by > y) && x < (bx - ax) * (y - ay) / (by - ay) + ax) inside = !inside;
  }
  return inside;
}

// The room a pin's details box has inside the map, beside the zoom buttons (POPUP's padding):
// app.css keeps the box's text within it, so on a phone the box fits whole, its close button
// in view, and a long name wraps instead of running off the screen; anything taller scrolls.
function fitPopups() {
  const size = M.map ? M.map.getSize() : null;
  if (!size || !size.x) return;                      // the page is hidden: fitted when shown
  const width = size.x - PAD_TOP_LEFT[0] - PAD_BOTTOM_RIGHT[0] - POPUP_EDGE.x;
  const height = size.y - PAD_TOP_LEFT[1] - PAD_BOTTOM_RIGHT[1] - POPUP_EDGE.y;
  $("map").style.setProperty("--popup-w", `${Math.max(120, width)}px`);
  $("map").style.setProperty("--popup-h", `${Math.max(120, height)}px`);
}

function makeMap() {
  const map = L.map($("map"), { preferCanvas: true, zoomSnap: 0.25, zoomDelta: 0.5, minZoom: 5,
                                maxZoom: 18 });
  map.on("resize", fitPopups);
  // The circles first, then the area names among them.
  map.on("moveend resize", () => { placePins(); placeLabels(); });
  map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a>');
  const tiles = L.tileLayer(TILES, { attribution: TILE_CREDIT, maxZoom: 19 });
  // Without the street map (offline, or the tile servers blocked) the rest still shows, on a plain
  // background, and the page says why the streets are missing.
  tiles.on("tileload", () => { M.tilesOk++; $("map-tiles-note").hidden = true; });
  tiles.on("tileerror", () => {
    M.tilesBad++; $("map-tiles-note").hidden = M.tilesOk > 0 || M.tilesBad < 2;
  });
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
  html.innerHTML = '<svg viewBox="0 0 32 42" width="32" height="42" aria-hidden="true" ' +
    'focusable="false">' +
    '<path class="aarco-body" d="M16 1.5C7.9 1.5 1.5 7.8 1.5 15.8 1.5 26.6 16 40.5 16 40.5' +
    'S30.5 26.6 30.5 15.8C30.5 7.8 24.1 1.5 16 1.5z"/>' +
    '<path class="aarco-star" d="M16 7.6l2.4 5 5.5.7-4 3.8 1 5.4L16 19.9l-4.9 2.6 1-5.4-4-3.8 ' +
    '5.5-.7z"/></svg>';
  html.append(el("span", data.aarco.name, "aarco-name"));
  aarcoMarker = L.marker([data.aarco.lat, data.aarco.lon], {
    icon: L.divIcon({ className: "aarco-pin", html, iconSize: [32, 42], iconAnchor: [16, 41],
                      popupAnchor: [0, -38] }),
    title: `${data.aarco.name}: ${data.aarco.address}`, keyboard: true, zIndexOffset: 1000 });
  aarcoMarker.bindPopup(() => {
    const box = el("div", undefined, "pin-box");
    box.append(el("strong", data.aarco.company), el("div", data.aarco.address),
               el("div", `The ${data.miles}-mile circle is AARCO's area: searches are meant for ` +
                         "it, and Miles on the Leads page are measured from here.", "sub"));
    return box;
  }, { ...POPUP, maxWidth: 280 });
  aarcoMarker.addTo(map);
}

// A business's details, opened from its pin.
/** @param {MapPinRow} row */
function pinBox(row) {
  const [key, , , group, name, tier, score, phone, verified, address, outcome, called, closed,
         kind] = row;
  const box = el("div", undefined, "pin-box");
  box.append(el("strong", name));
  if (group === "competitor") {
    box.append(el("div", kind === "Competitor" ? "Competitor: flagged, not a prospect"
      : kind === "Parent company" ? "AARCO's parent company" : "AARCO's own listing", "sub"));
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
  box.append(el("div", outcome ? `Latest call: ${outcome}, ${called}` : "No calls logged yet",
                outcome ? "" : "sub"));
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
    const partly = area.status === "asking" || area.status === "missed";
    const sym = partly ? "partly" : area.status === "never" ? "unknown" : area.status;
    li.append(el("span", undefined, `sym area ${sym}`));
    const text = el("div");
    text.append(el("strong", `${area.n}. ${area.name}`), DOT, el("span", area.status_text));
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
  placePins();
  placeLabels();
  const hidden = hiddenGroups().filter((g) => g !== group).concat(input.checked ? [] : [group]);
  try { localStorage.setItem(HIDDEN_KEY, JSON.stringify(hidden)); }
  catch (e) { /* kept for this visit only */ }
}));
$("map-show-areas").addEventListener("change", () => {
  if (!M.map || !M.areas) return;
  if ($("map-show-areas").checked) { M.map.addLayer(M.areas); placeLabels(); } else {
    M.map.removeLayer(M.areas);
    $("map-names-note").hidden = true;
  }
});
