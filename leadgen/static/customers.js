/* The Lead Finder page, the Customers page: the company's current customers, kept off the lead
   lists. The customer file (CSV or Excel) is read here, in the browser, and never sent: each
   customer's phone, street address and name become match keys, and only a one-way fingerprint of
   each key (SHA-256 of the site's salt and the key) goes to the server (leadgen/customers.py, whose
   rules these must match: tests/test_customers.py checks both). Loaded after map.js. */
// The names this part shares with the others (eslint.config.mjs reads this list).
/* exported loadCustomers, customerKeys, readCsv */

/* ---------- match keys (the same rules as leadgen/customers.py) ---------- */
const NAME_DROP = new Set(["the", "inc", "incorporated", "llc", "co", "corp", "corporation",
                           "company", "ltd", "limited"]);
/** @type {Record<string, string>} */
const STREET_WORDS = {
  street: "st", road: "rd", avenue: "ave", av: "ave", drive: "dr", boulevard: "blvd", lane: "ln",
  parkway: "pkwy", circle: "cir", court: "ct", place: "pl", highway: "hwy", terrace: "ter",
  north: "n", south: "s", east: "e", west: "w", northeast: "ne", northwest: "nw", southeast: "se",
  southwest: "sw",
};
const UNIT_WORDS = new Set(["suite", "ste", "unit", "apt", "apartment", "bldg", "building", "floor",
                            "fl", "room", "rm"]);

/** @param {unknown} text @returns {string[]} */
function custWords(text) {
  const t = String(text ?? "").toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "")
    .replace(/&/g, " and ").replace(/['’]/g, "");
  return t.replace(/[^a-z0-9]+/g, " ").split(" ").filter(Boolean);
}
/** @param {unknown} phone */
function phoneKey(phone) {
  let digits = String(phone ?? "").replace(/\D/g, "");
  if (digits.length === 11 && digits.startsWith("1")) digits = digits.slice(1);
  return digits.length === 10 ? digits : "";
}
/** @param {unknown} address */
function streetKey(address) {
  const out = [];
  for (const word of custWords(String(address ?? "").split(",")[0])) {
    if (UNIT_WORDS.has(word)) break;
    out.push(STREET_WORDS[word] || word);
  }
  return out.length < 2 || !/^\d/.test(out[0]) ? "" : out.join(" ");
}
/** @param {unknown} name */
function nameKey(name) {
  const text = custWords(name).filter((w) => !NAME_DROP.has(w) && !/^\d+$/.test(w)).join(" ");
  return text.length >= 3 ? text : "";
}
/** @param {unknown} zip */
function zipKey(zip) {
  const digits = String(zip ?? "").replace(/\D/g, "");
  return digits.length >= 5 ? digits.slice(0, 5) : "";
}
/**
 * One customer's match keys. A full address in one column ("876 W Fortune Rd, Salt Lake City,
 * UT 84104") gives its city and ZIP when the file has no columns for them.
 * @param {{name?: unknown, phone?: unknown, address?: unknown, city?: unknown, zip?: unknown}} c
 * @returns {string[]}
 */
function customerKeys(c) {
  const parts = String(c.address ?? "").split(",").map((p) => p.trim());
  let city = String(c.city ?? ""), zip = String(c.zip ?? "");
  if (parts.length > 1) {
    const tail = parts.slice(1).join(" ");
    if (!zip) zip = (tail.match(/(\d{5})(?:-\d{4})?\s*$/) || ["", ""])[1];
    if (!city) city = parts[1].replace(/\b[A-Za-z]{2}\s+\d{5}(?:-\d{4})?$/, "").trim();
  }
  /** @type {Set<string>} */
  const keys = new Set();
  const number = phoneKey(c.phone);
  if (number) keys.add(`phone:${number}`);
  const town = custWords(city).join(" "), zip5 = zipKey(zip);
  for (const [kind, value] of [["addr", streetKey(c.address)], ["name", nameKey(c.name)]]) {
    if (!value) continue;
    if (zip5) keys.add(`${kind}:${value}|${zip5}`);
    if (town) keys.add(`${kind}city:${value}|${town}`);
  }
  return [...keys];
}
/** @param {string} salt @param {string} key */
async function fingerprint(salt, key) {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`${salt}\n${key}`));
  return [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/* ---------- reading the file (in this browser only) ---------- */
/**
 * Rows of a CSV (or tab- or semicolon-separated) text: quoted fields may hold commas, quotes ("")
 * and line breaks.
 * @param {string} text
 * @returns {string[][]}
 */
function readCsv(text) {
  text = text.replace(/^﻿/, "");
  const first = text.split(/\r?\n/, 1)[0];
  const sep = [",", "\t", ";"].map((s) => [s, first.split(s).length])
    .sort((a, b) => Number(b[1]) - Number(a[1]))[0][0];
  /** @type {string[][]} */
  const rows = [];
  /** @type {string[]} */
  let row = [];
  let field = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (ch === '"') quoted = false;
      else field += ch;
    } else if (ch === '"' && field === "") quoted = true;
    else if (ch === sep) { row.push(field); field = ""; }
    else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(field); rows.push(row); row = []; field = "";
    } else field += ch;
  }
  if (field || row.length) { row.push(field); rows.push(row); }
  return rows.filter((r) => r.some((f) => f.trim()));
}

/**
 * The files inside an .xlsx (a zip), read with the browser's own unzip.
 * @param {ArrayBuffer} buffer
 * @param {(name: string) => boolean} wanted
 * @returns {Promise<Map<string, string>>}
 */
async function unzip(buffer, wanted) {
  const view = new DataView(buffer);
  let end = buffer.byteLength - 22;
  while (end >= 0 && view.getUint32(end, true) !== 0x06054b50) end--;
  if (end < 0) throw new Error("not a zip");
  const count = view.getUint16(end + 10, true);
  let at = view.getUint32(end + 16, true);
  const decoder = new TextDecoder();
  /** @type {Map<string, string>} */
  const files = new Map();
  for (let n = 0; n < count; n++) {
    if (view.getUint32(at, true) !== 0x02014b50) throw new Error("bad zip");
    const method = view.getUint16(at + 10, true), size = view.getUint32(at + 20, true);
    const nameLen = view.getUint16(at + 28, true), extraLen = view.getUint16(at + 30, true);
    const commentLen = view.getUint16(at + 32, true), local = view.getUint32(at + 42, true);
    const name = decoder.decode(new Uint8Array(buffer, at + 46, nameLen));
    at += 46 + nameLen + extraLen + commentLen;
    if (!wanted(name)) continue;
    const start = local + 30 + view.getUint16(local + 26, true) + view.getUint16(local + 28, true);
    const data = new Uint8Array(buffer, start, size);
    if (method === 0) { files.set(name, decoder.decode(data)); continue; }
    if (method !== 8) throw new Error("unsupported zip");
    const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
    files.set(name, await new Response(stream).text());
  }
  return files;
}
/** @param {string} ref a cell reference such as "C12" @returns {number} its column, from 0 */
function columnOf(ref) {
  let n = 0;
  for (const ch of ref.replace(/\d+$/, "")) n = n * 26 + ch.charCodeAt(0) - 64;
  return n - 1;
}
/**
 * The first sheet of an Excel workbook (.xlsx) as rows of text.
 * @param {ArrayBuffer} buffer
 * @returns {Promise<string[][]>}
 */
async function readXlsx(buffer) {
  const parts = new RegExp("^xl/(workbook\\.xml|_rels/workbook\\.xml\\.rels|" +
                           "sharedStrings\\.xml|worksheets/[^/]+\\.xml)$");
  const files = await unzip(buffer, (n) => parts.test(n));
  /** @type {(name: string) => Document | null} */
  const xml = (name) => {
    const text = files.get(name);
    return text ? new DOMParser().parseFromString(text, "application/xml") : null;
  };
  const book = xml("xl/workbook.xml"), rels = xml("xl/_rels/workbook.xml.rels");
  const sheet = book && book.getElementsByTagName("sheet")[0];
  const ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
  const relId = sheet && (sheet.getAttribute("r:id") || sheet.getAttributeNS(ns, "id"));
  let path = "xl/worksheets/sheet1.xml";
  for (const rel of rels ? [...rels.getElementsByTagName("Relationship")] : []) {
    if (rel.getAttribute("Id") === relId) {
      const target = rel.getAttribute("Target") || "";
      path = target.startsWith("/") ? target.slice(1) : `xl/${target}`;
    }
  }
  const strings = [...(xml("xl/sharedStrings.xml")?.getElementsByTagName("si") || [])]
    .map((si) => [...si.getElementsByTagName("t")].map((t) => t.textContent || "").join(""));
  const doc = xml(path);
  if (!doc) throw new Error("no sheet");
  /** @type {string[][]} */
  const rows = [];
  for (const r of doc.getElementsByTagName("row")) {
    /** @type {string[]} */
    const row = [];
    let next = 0;
    for (const c of r.getElementsByTagName("c")) {
      const ref = c.getAttribute("r");
      const col = ref ? columnOf(ref) : next;
      next = col + 1;
      const type = c.getAttribute("t");
      const v = c.getElementsByTagName("v")[0]?.textContent || "";
      let text = v;
      if (type === "s") text = strings[Number(v)] || "";
      else if (type === "inlineStr") {
        text = [...c.getElementsByTagName("t")].map((t) => t.textContent || "").join("");
      }
      while (row.length < col) row.push("");
      row[col] = text;
    }
    if (row.some((f) => f.trim())) rows.push(row);
  }
  return rows;
}

/* ---------- the page ---------- */
const FIELDS = /** @type {const} */ ([
  ["name", "Business name", /(business|company|customer|account|client|^name$|name)/],
  ["phone", "Phone", /(phone|tel|mobile|cell)/],
  ["address", "Street address", /(street|address|addr)/],
  ["city", "City", /(city|town)/],
  ["zip", "ZIP code", /(zip|postal)/],
]);
// The file's rows while this page shows them (never stored, never sent).
/** @type {{rows: string[][], header: string[], salt: string, busy: boolean}} */
const CUST = { rows: [], header: [], salt: "", busy: false };

/** @param {CustomerState} s */
function showCustomers(s) {
  CUST.salt = s.salt;
  $("cust-problem").replaceChildren();
  const on = s.keys > 0;
  const many = (/** @type {number} */ n, /** @type {string} */ one) =>
    `${n.toLocaleString()} ${one}${n === 1 ? "" : "s"}`;
  $("cust-summary").textContent = on
    ? `${many(s.customers, "customer")} on file, from ${s.when}${s.by ? ` by ${s.by}` : ""}.`
    : "No customer list on file. Every lead shows.";
  $("cust-hidden").hidden = !on;
  const one = s.hidden === 1;
  $("cust-hidden").textContent = `${many(s.hidden, "saved lead")} ${one ? "matches" : "match"} ` +
    `a customer and ${one ? "is" : "are"} hidden. New searches are checked too.`;
  $("cust-clear").hidden = !on;
  $("cust-upload-title").textContent = on ? "Upload a new customer list"
    : "Upload your customer list";
}
async function loadCustomers() {
  try { showCustomers(await api("/customers")); }
  catch (e) {
    const err = /** @type {ApiError} */ (e);
    if (err.status === 401) return;
    problem($("cust-problem"), "Can't show the customer list right now", err.message,
            loadCustomers);
  }
}
/** @param {string} text */
function custError(text) {
  $("cust-error").textContent = text;
  $("cust-error").hidden = !text;
}
function forgetFile() {
  CUST.rows = []; CUST.header = [];
  $("cust-file").value = "";
  $("cust-columns").hidden = true;
  $("cust-picks").replaceChildren();
  $("cust-sending").textContent = "";
}
/** The column picked for each detail (-1: none). */
function picked() {
  /** @type {Record<string, number>} */
  const out = {};
  for (const [key] of FIELDS) out[key] = Number($(`cust-col-${key}`).value);
  return out;
}
/** Each row's details, by the columns picked. */
function customerRows() {
  const cols = picked();
  return CUST.rows.map((row) => Object.fromEntries(FIELDS.map(([key]) =>
    [key, cols[key] >= 0 ? row[cols[key]] || "" : ""])));
}
function countUsable() {
  const rows = customerRows();
  const usable = rows.filter((r) => customerKeys(r).length).length;
  $("cust-usable").textContent = usable === rows.length
    ? `Every customer can be matched.`
    : `${(rows.length - usable).toLocaleString()} of ${rows.length.toLocaleString()} rows ` +
      "have no phone, street address, or name with a city or ZIP, so they can't be matched " +
      "and are left out.";
  $("cust-send").disabled = !usable;
}
/** @param {string[][]} rows */
function showColumns(rows) {
  // The header is the first row with at least two filled cells.
  const head = rows.findIndex((r) => r.filter((f) => f.trim()).length >= 2);
  CUST.header = (rows[head] || []).map((h) => h.trim());
  CUST.rows = rows.slice(head + 1);
  if (!CUST.rows.length) {
    custError("That file has no customer rows under its header row.");
    return;
  }
  const picks = $("cust-picks");
  picks.replaceChildren();
  /** @type {Set<number>} */
  const used = new Set();
  for (const [key, label, guess] of FIELDS) {
    const lab = el("label", label);
    const sel = el("select");
    sel.id = `cust-col-${key}`;
    /** @type {(text: string, value: string) => HTMLOptionElement} */
    const option = (text, value) => { const o = el("option", text); o.value = value; return o; };
    sel.append(option("(not in this file)", "-1"));
    CUST.header.forEach((h, i) => sel.append(option(h || `Column ${i + 1}`, String(i))));
    /** @type {(h: string) => boolean} */
    const fits = (h) => guess.test(h) &&
      !(key === "address" && /(city|state|zip|e-?mail)/.test(h)) &&
      !(key === "name" && /(contact|first|last|user)/.test(h));
    const found = CUST.header.findIndex((h, i) => !used.has(i) && fits(h.toLowerCase()));
    if (found >= 0) { sel.value = String(found); used.add(found); }
    sel.addEventListener("change", countUsable);
    lab.append(sel);
    picks.append(lab);
  }
  $("cust-read").textContent = `Read ${CUST.rows.length.toLocaleString()} ` +
    `${CUST.rows.length === 1 ? "row" : "rows"} on this computer. Nothing has been sent.`;
  $("cust-columns").hidden = false;
  countUsable();
}
$("cust-file").addEventListener("change", async () => {
  custError("");
  $("cust-columns").hidden = true;
  const file = /** @type {File | undefined} */ ($("cust-file").files[0]);
  if (!file) return;
  const name = file.name.toLowerCase();
  try {
    if (name.endsWith(".xls")) {
      custError("That is an old Excel file (.xls). Open it in Excel and save it as .xlsx or " +
                "CSV, then pick that file.");
      return;
    }
    const rows = name.endsWith(".xlsx") ? await readXlsx(await file.arrayBuffer())
      : readCsv(await file.text());
    showColumns(rows);
  } catch (e) {
    custError("That file couldn't be read. Save it from Excel as .xlsx or CSV and try again.");
  }
});
$("cust-cancel").addEventListener("click", () => { forgetFile(); custError(""); });
$("cust-send").addEventListener("click", async () => {
  if (CUST.busy) return;
  CUST.busy = true;
  $("cust-send").disabled = true;
  $("cust-sending").textContent = "Making the fingerprints on this computer…";
  try {
    if (!CUST.salt) showCustomers(await api("/customers"));
    const rows = customerRows().map(customerKeys).filter((k) => k.length);
    /** @type {Set<string>} */
    const hashes = new Set();
    for (const keys of rows) for (const k of keys) hashes.add(await fingerprint(CUST.salt, k));
    $("cust-sending").textContent = "Saving the fingerprints…";
    const s = await post("/customers",
                         { hashes: [...hashes], customers: rows.length, by: myName() });
    forgetFile();
    showCustomers(s);
    loadLeads();
  } catch (e) {
    const err = /** @type {ApiError} */ (e);
    $("cust-sending").textContent = "";
    if (err.status !== 401) custError(err.message);
    $("cust-send").disabled = false;
  } finally {
    CUST.busy = false;
  }
});
$("cust-clear").addEventListener("click", () => $("cust-clear-dlg").showModal());
$("cust-clear-cancel").addEventListener("click", () => $("cust-clear-dlg").close());
$("cust-clear-form").addEventListener("submit", async (/** @type {SubmitEvent} */ e) => {
  e.preventDefault();
  $("cust-clear-dlg").close();
  try {
    showCustomers(await post("/customers/clear", { by: myName() }));
    loadLeads();
  } catch (err) {
    const error = /** @type {ApiError} */ (err);
    if (error.status !== 401) {
      problem($("cust-problem"), "The list wasn't cleared", error.message);
    }
  }
});
