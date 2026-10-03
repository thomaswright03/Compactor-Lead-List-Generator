/* The Lead Finder page, the tutorial: a step-by-step tour of every page, opened with the
   Tutorial button in the sidebar (in Menu on phones), and offered once on a browser's first
   visit. Each step switches to its page, highlights one part of it and explains it. The tour
   only looks: it never clicks, marks, calls or searches. Loaded after the seven parts in
   core.js to start.js. */
/** @type {TourStep[]} */
const TOUR = [
  { page: "find", title: "Welcome to the Lead Finder",
    text: "This site finds businesses around Salt Lake City that are likely to run a baler or compactor. " +
          "You check each one, call them, and keep track of how it went. This tour shows every page. " +
          "It only looks around: nothing you see here is changed." },
  { page: "find", target: "nav", title: "The five pages",
    text: "Find leads runs a search. Leads is the list of businesses to check. Calls keeps every call. " +
          "Map shows them all around AARCO. Stats sums up what you found. The numbers next to Leads and Calls " +
          "show how many are waiting." },
  { page: "find", target: "#form .form-grid", title: "Where to search",
    text: "Type a ZIP code, city or address and how many miles around it to look. AARCO's address is filled in. " +
          "Extra search words are optional; the standard words are always searched." },
  { page: "find", target: ".go-row", title: "Find leads, once a day",
    text: "Press Find leads to start. You get one search per day (Utah time), so the site asks before it starts. " +
          "A progress bar shows each step, and you can use other pages while it runs. " +
          "New businesses are added to your saved leads; ones you already have are updated, never duplicated." },
  { page: "find", target: "#history", title: "Search history",
    text: "Every search is listed here: when it ran, where it looked, and how many businesses it found and added." },
  { page: "find", target: "#admin-card", title: "For the site administrator",
    text: "Kept closed for the sales team and locked with its own administrator password. It lists recent problems, " +
          "has switches to pause searching right away, and a scoring check file to send in once a month." },
  { page: "leads", target: "#lead-tabs, #tab-pick", title: "Leads: one tab per answer",
    text: "Not checked holds the businesses nobody has answered yet. Once marked, a business moves to " +
          "Has baler or compactor or No baler or compactor. Competitors (like Pro Baler and Action Compaction) " +
          "are flagged in their own tab and never asked." },
  { page: "leads", target: "#page-leads .toolbar", title: "Find the right business",
    text: "Filter by name, city or category, pick a tier, show only businesses with a phone number, " +
          "and download the whole list as Excel or CSV." },
  { page: "leads", target: "#leads-wrap", title: "Each business",
    text: "Every business has a score from 0 to 100 and a tier: A strong, B likely, C possible, D weak. " +
          "Why this score lists the reasons. The phone, address and map link are there to call or look it up. " +
          "Found a better number or who to ask for? Save it with Add verified phone or contact." },
  { page: "leads", target: "#leads-wrap .c-mark .mark, #leads-wrap", title: "Has baler or compactor? Yes or No",
    text: "Once you know, press Yes or No. The mark is kept for good and shows who made it. " +
          "Pressed the wrong one? Undo is offered for 5 minutes. The first time, the site asks your name." },
  { page: "leads", target: "#leads-wrap .call-cell, #leads-wrap", title: "Just called",
    text: "After a call, press Just called. Write a short Conversation Summary and pick how it went: " +
          "Interested, Follow Up, Not Interested, Not Qualified, No Contact or Bad Lead. " +
          "History shows every earlier call. A call never changes the Yes / No answer." },
  { page: "calls", target: "#call-tabs, #call-pick", title: "Calls",
    text: "Every business that has been called, latest call first. Each outcome has its own tab, " +
          "so Follow Up shows exactly who to call back. The filter finds a business by its name, town, what " +
          "was said on its calls or who called." },
  { page: "map", target: "#map-box", title: "Map",
    text: `AARCO's pin, the ${CONFIG.area_miles}-mile area searches are meant for, and the parts a search looks ` +
          "through one at a time, shaded by how the latest search went. Every saved business is a dot coloured by " +
          "its answer: Yes, No or not checked yet; competitors are hollow squares. Tap one for its phone, its " +
          "latest call and a link to it on the Leads page." },
  { page: "map", target: "#map-groups", title: "Show or hide businesses",
    text: "Untick a group to hide it from the map, tick it to show it again. The key explains every symbol, and " +
          "the list below the map says how each search area stands, in words." },
  { page: "stats", target: "#stats-body", title: "Stats",
    text: "How many businesses have a baler or compactor, their average score, and how often each tier " +
          "turned out right. It fills in as the team marks businesses Yes or No." },
  { page: "stats", target: "#me", title: "Your name",
    text: "Marks and calls are saved with the name set in this browser. Change it here (under Menu on a " +
          "phone or tablet) if someone else uses this computer." },
  { page: "stats", target: "#yelp-quota", needs: "#yelp-quota", title: "Yelp lookups",
    text: "Yelp adds phone numbers. The site uses at most 50 Yelp lookups in any 24 hours and shows when they reset." },
  { page: "stats", target: ".theme", title: "Light or dark",
    text: "Pick a light or dark look, or follow your computer's setting." },
  { page: "find", target: "#tour-btn", title: "That's it",
    text: "Open this tour again anytime with the Tutorial button (under Menu on a phone or tablet). Start on " +
          "Find leads, then work through Leads." },
];

// The tutorial's own parts (made the first time it opens) and where it is.
/** @typedef {{box: HTMLElement, spot: HTMLElement, shade: HTMLElement, keys: (e: KeyboardEvent) => void}} TourParts */
/** @type {{steps: TourStep[], i: number, parts: TourParts | null, last: Element | null, timer: number}} */
const tour = { steps: [], i: -1, parts: null, last: null, timer: 0 };

/** @param {Element | undefined} node */
function tourVisible(node) {
  if (!node) return false;
  const r = node.getBoundingClientRect();
  return r.width > 0 && r.height > 0 && getComputedStyle(node).visibility !== "hidden";
}
/** @param {TourStep} step @returns {Element | null} */
function tourTarget(step) {
  // A step can list several places, best first: the first one on screen is used.
  for (const sel of (step.target || "").split(",").map((s) => s.trim()).filter(Boolean)) {
    const node = [...document.querySelectorAll(sel)].find(tourVisible);
    if (node) return node;
  }
  return null;
}

/** @returns {TourParts} */
function tourParts() {
  if (tour.parts) return tour.parts;
  const spot = el("div", undefined, "tour-spot");
  spot.setAttribute("aria-hidden", "true");
  const box = el("div", undefined, "tour-box");
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-modal", "true");
  box.setAttribute("aria-labelledby", "tour-title");
  box.setAttribute("aria-describedby", "tour-text");
  box.innerHTML = '<div class="tour-count" id="tour-count"></div><h2 id="tour-title"></h2>' +
                  '<p id="tour-text"></p><div class="tour-actions">' +
                  '<button type="button" class="link" id="tour-close">Close tutorial</button>' +
                  '<span class="tour-nav"><button type="button" class="quiet" id="tour-back">Back</button>' +
                  '<button type="button" id="tour-next">Next</button></span></div>';
  const shade = el("div", undefined, "tour-shade");
  shade.addEventListener("click", endTour);
  document.body.append(shade, spot, box);
  $("tour-close").addEventListener("click", endTour);
  $("tour-back").addEventListener("click", () => showStep(tour.i - 1));
  $("tour-next").addEventListener("click", () => showStep(tour.i + 1));
  /** @param {KeyboardEvent} e */
  const keys = (e) => {
    if (e.key === "Escape") { e.preventDefault(); endTour(); }
    else if (e.key === "ArrowRight") showStep(tour.i + 1);
    else if (e.key === "ArrowLeft") showStep(tour.i - 1);
    else if (e.key === "Tab") {                      // keep the keyboard inside the tutorial
      const buttons = /** @type {HTMLButtonElement[]} */ ([...box.querySelectorAll("button:not([disabled])")]);
      const at = buttons.indexOf(/** @type {HTMLButtonElement} */ (document.activeElement));
      if (e.shiftKey && at <= 0) { e.preventDefault(); buttons[buttons.length - 1].focus(); }
      else if (!e.shiftKey && (at === buttons.length - 1 || at < 0)) { e.preventDefault(); buttons[0].focus(); }
    }
  };
  tour.parts = { box, spot, shade, keys };
  return tour.parts;
}

function startTour() {
  if (tour.i >= 0) return;
  tourOffered(true);
  const { box, spot, shade, keys } = tourParts();
  tour.last = document.activeElement;
  // A step about something this site doesn't have (Yelp not set up) is left out.
  tour.steps = TOUR.filter((step) => !step.needs || document.querySelector(step.needs));
  $("side-bottom").closest(".side").classList.remove("open");   // the phone menu closes
  box.hidden = spot.hidden = shade.hidden = false;
  document.body.classList.add("touring");
  window.addEventListener("resize", placeTour);
  window.addEventListener("scroll", placeTour, true);
  document.addEventListener("keydown", keys);        // wherever the focus is while it opens
  showStep(0);
}

function endTour() {
  if (tour.i < 0) return;
  const { box, spot, shade, keys } = tourParts();
  tour.i = -1;
  clearTimeout(tour.timer);
  box.hidden = spot.hidden = shade.hidden = true;
  document.body.classList.remove("touring");
  window.removeEventListener("resize", placeTour);
  window.removeEventListener("scroll", placeTour, true);
  document.removeEventListener("keydown", keys);
  const last = /** @type {HTMLElement | null} */ (tour.last);
  if (last && last.focus) last.focus();
}

/** @param {number} i */
function showStep(i) {
  if (i < 0 || i >= tour.steps.length) { if (i >= tour.steps.length) endTour(); return; }
  const { box } = tourParts();
  tour.i = i;
  const step = tour.steps[i];
  if (parseHash().page !== step.page) location.hash = step.page;
  $("tour-count").textContent = `Step ${i + 1} of ${tour.steps.length}`;
  $("tour-title").textContent = step.title;
  $("tour-text").textContent = step.text;
  $("tour-back").disabled = i === 0;
  $("tour-next").textContent = i === tour.steps.length - 1 ? "Finish" : "Next";
  box.classList.add("moving");
  clearTimeout(tour.timer);
  // Give the page a moment to switch and draw before finding the part to highlight.
  tour.timer = setTimeout(() => {
    const node = tourTarget(step);
    if (node) node.scrollIntoView({ block: "center", behavior: "instant" });
    placeTour();
    box.classList.remove("moving");
    $("tour-next").focus();
  }, 150);
}

function placeTour() {
  if (tour.i < 0) return;
  const node = tourTarget(tour.steps[tour.i]);
  const { box, spot, shade } = tourParts(), pad = 6, gap = 12, edge = 12;
  const vw = document.documentElement.clientWidth, vh = window.innerHeight;
  if (!node) {                                       // nothing to point at: the box sits in the middle
    spot.hidden = true;
    shade.classList.add("dim");
    box.style.left = `${Math.max(edge, (vw - box.offsetWidth) / 2)}px`;
    box.style.top = `${Math.max(edge, (vh - box.offsetHeight) / 2)}px`;
    return;
  }
  shade.classList.remove("dim");                     // the spot's own shadow dims the rest
  const r = node.getBoundingClientRect();
  const top = Math.max(0, r.top - pad), left = Math.max(0, r.left - pad);
  const bottom = Math.min(vh, r.bottom + pad), right = Math.min(vw, r.right + pad);
  Object.assign(spot.style, { top: `${top}px`, left: `${left}px`,
                              width: `${Math.max(0, right - left)}px`, height: `${Math.max(0, bottom - top)}px` });
  spot.hidden = false;
  const w = box.offsetWidth, h = box.offsetHeight;
  let x, y;
  if (vh - bottom >= h + gap) { y = bottom + gap; x = left; }          // below
  else if (top >= h + gap) { y = top - gap - h; x = left; }             // above
  else if (vw - right >= w + gap) { x = right + gap; y = top; }         // to the right
  else if (left >= w + gap) { x = left - gap - w; y = top; }            // to the left
  else { x = (vw - w) / 2; y = vh - h - edge; }                         // over it, at the bottom
  box.style.left = `${Math.min(Math.max(edge, x), vw - w - edge)}px`;
  box.style.top = `${Math.min(Math.max(edge, y), vh - h - edge)}px`;
}

/* The first visit: a small note in a corner offers the tour. Taken or turned down, it isn't
   offered again in this browser; the Tutorial button still opens the tour at any time. */
const TOUR_OFFERED = "tour-offered";
/** Whether this browser was offered the tour; with now = true, remember that it was.
    @param {boolean} [now] @returns {boolean} */
function tourOffered(now) {
  const offer = document.querySelector(".tour-offer");
  try {
    if (now) { offer?.remove(); localStorage.setItem(TOUR_OFFERED, "1"); }
    return !!localStorage.getItem(TOUR_OFFERED);
  } catch (e) { return true; }      // a browser that keeps nothing would be asked on every visit
}

function offerTour() {
  if (tourOffered()) return;
  const offer = el("aside", undefined, "tour-offer");
  offer.setAttribute("aria-labelledby", "tour-offer-title");
  offer.innerHTML = '<p><strong id="tour-offer-title">New here?</strong> A short tour shows each page: ' +
                    "finding leads, marking Yes or No, and logging calls. It only looks; nothing is changed. " +
                    "You can also open it later with the Tutorial button.</p>" +
                    '<div class="tour-offer-actions"><button type="button" class="quiet" id="tour-offer-no">' +
                    'No thanks</button><button type="button" id="tour-offer-yes">Take the tour</button></div>';
  document.body.append(offer);
  $("tour-offer-yes").addEventListener("click", startTour);
  $("tour-offer-no").addEventListener("click", () => {
    tourOffered(true);
    if (tourVisible($("tour-btn"))) $("tour-btn").focus();     // where the tour can be found later
  });
}

$("tour-btn").addEventListener("click", startTour);
offerTour();
