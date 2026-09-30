# Lead Finder: guide for the sales team

What the Lead Finder is for, and how to use each page. For keeping the site
running see the [operator runbook](operator-runbook.md); for the code, the
[developer overview](developer-overview.md).

## A day's work in five steps

1. **Find leads** (once a day, Utah time): press **Find leads**, check the summary,
   press **Start search**. New businesses join the one saved list.
2. **Leads**, tab **Not checked**: work down the list, best score first. Phone
   the business (the number is a tap-to-call link on a phone).
3. After each call, press **Just called** on its row, write what was said and
   pick how it went (Interested, Follow Up, Not Interested, Not Qualified, No
   Contact or Bad Lead). This works whether or not the business is marked yet.
4. When you know, press **Yes** or **No** for "has a baler or compactor". A
   wrong click can be undone for 5 minutes.
5. **Calls** shows every called business by result; **Stats** shows how well the
   scores find businesses with equipment. **Download Excel** gives the whole list.

What the tiers mean: **A** strong (score 60+), **B** likely (40+), **C**
possible (20+), **D** weak. Every score shows its tier's meaning next to it.

## The four pages

The sidebar has four pages, a Light / Dark / System colour switch (System
follows the computer's setting; the choice is remembered in each browser), and
every page carries the Wright AI Solutions copyright. The browser tab names the
page ("Leads · Arco Compactor Lead Finder") and shows the Lead Finder icon.
On phones and tablets every button and link is at least 44 px each way (on a
narrow window the "Only businesses matching the search words" tick box too). On a
phone the navigation is one short bar at the top (the page counts show just the
number; on the narrowest phones the links wrap to a second line, so Stats is
never cut off), with the Yelp count, the colour switch and Log out under **Menu**; the
browser's own bar takes the sidebar's colour, light or dark. Times are written
one way everywhere, in Utah time: "Sep 29, 2026, 4:43 PM", or "4:43 PM".
The first Tab stop on every page is **Skip to main content**, which jumps past
the sidebar to the page's list (Leads, Calls) or heading.

- **Find leads**: the search form (extra settings, each explained in plain
  words, under **More options**), a step-by-step progress bar while it runs,
  and the search history with each search's counts under **Details**, in plain
  words and in funnel order so the numbers add up: businesses found per source,
  listings within the radius, duplicates merged, businesses after merging, then
  what was left out (closed for good, a score below the minimum, e.g. "Left out:
  score below 20", not matching the search words, over a lead limit), leads kept,
  competitors among them, the leads per tier A to D, then the paid lookups and how
  long it took. The form checks itself before anything runs: **Search around** must
  not be empty (it is never quietly replaced by Arco's address), and the search
  words are limited to 20, each up to 60 characters (the hint says so). When
  a search can't start, whether the browser catches it or the server refuses
  (a bad value, today's search already run, searching paused, the database
  down), the reason shows in red under **Find leads** and next to the field it is
  about, and stays until the form is edited. The note beside the button (how the
  day stands, e.g. "Today's search ran at 4:43 PM") is plain grey text.
  When a search finishes, the page gets only the counts it shows ("N new leads,
  M saved leads in all"), not the saved list, so finishing stays quick however
  long the list grows. **Find Leads works once per calendar day** (Utah time), for the whole site:
  after today's search it is off until midnight. Pressing **Find leads** first
  shows a summary (location, miles, search words, sources) and says this uses
  today's only search: **Start search** runs it, **Go back and edit** (or Escape)
  leaves the day unused. A search that fails outright
  (e.g. an unknown location, or the map data service is down) gives the day
  back, says so in one or two plain sentences (with one piece of advice: "You
  can try again now; if it fails again, try later today"), and stays in the history marked
  **Failed** with the reason; so does one that never finished (the server
  restarted), after 30 minutes (the page says when, in Utah time). A search
  where one source failed while the others worked (say Google refused its key,
  or the map data service was down) is **incomplete**: the businesses the other
  sources found are saved, but the day is given back, so the search can be run
  again once that source works. The page says which source is missing and that
  today's search was not used up (for Google or Yelp: "ask whoever looks after
  the site to check the key"), and the history shows the search's lead count
  marked **Incomplete**, with the reason. An incomplete search can be re-run
  **once** that day (`INCOMPLETE_RERUNS` in `leadgen/daily.py`); the note beside
  the button says "1 re-run left today". If the re-run is incomplete too, what
  it found is saved and it uses up the day like a complete search, so a key that
  stays broken can't turn the day into unlimited searches (each spending Yelp
  calls); a third search that day is refused with "the next search can run
  tomorrow, from midnight Utah time". A source switched off by the
  administrator is not a failure (the day is used as usual). Under the history,
  **Problems in the last 7 days** (shown only when there were some) lists failed
  or incomplete searches and server errors, so nobody has to read the logs to
  notice them (see "Problems: the webhook" in the [operator runbook](operator-runbook.md)). The public map servers often
  refuse or time out on one big query, so a wide search asks the free map data
  **in parts**: a grid of areas up to 20 miles wide (`OVERPASS_PART_MILES`; a
  30-mile search is 9 areas), two at a time, each starting at a different map
  server (the progress bar says "area 3 of 9"). An area no server answers is
  asked again as four smaller ones; if one still gets no answer, the search is
  **incomplete** (the businesses from the areas that answered are saved, as
  above). Answers are kept for 7 days, so a re-run only asks for the areas still
  missing. The map data step gives up after four minutes in all
  (`OVERPASS_DEADLINE_SECONDS`), an area after 90 seconds; a map server that
  hasn't answered an area after 25 seconds (`OVERPASS_STAGGER_SECONDS`) is not
  waited out: the next one is asked as well and the first good answer wins. The progress bar says when a step
  is taking longer than usual. Steps that don't apply (Google and Yelp when
  neither is set up) are shown as skipped.
- **Leads**: the saved list, with **Yes** / **No** buttons for "has a baler or
  compactor" and tabs for Not checked, Has baler or compactor, No baler or
  compactor, Competitors, Closed and All. A saved business that a later search
  reports **closed for good** stays in the list with its mark and calls, flagged
  "Closed for good" in red: it leaves Not checked (there is nothing left to
  check), stays under Has / No baler or compactor if it was marked, and every
  closed business is under the **Closed** tab. It stays on the Calls page and in
  the downloads (the flag is in the Flags column), and Stats still counts its
  mark: the mark says what it had while it was open. If a later search finds it
  open again, the flag goes. Competitors and Arco's own listing are
  flagged (orange) and have their own **Competitors** tab: they aren't prospects,
  so they get no Yes / No buttons, aren't in Not checked and aren't counted in
  Stats (they still appear under All and in the downloads). The sidebar badge
  counts the prospects still to check. The server filters, sorts and pages the
  list, so opening the page fetches only the rows shown (300 at a time, **Show
  more** for the next 300) and the tab counts, not the whole saved list. A
  business just marked stays where it is, showing its answer ("Saved. Moves to
  …") and its Undo, until the pointer leaves the list (then a few seconds), or
  you change tab, filter or sort, so a double-click or a quick second click can
  never mark the next business by mistake. A mark
  is kept for good: it can be switched between Yes and No, and a later search
  that finds the business again updates its row without moving it. **Every
  click can be undone for 5 minutes**: from the Undo link on the row (with the
  time left) or from **Recent changes** above the list, even after a reload.
  The server remembers what each click replaced, and refuses an undo once a
  newer change has been made to that business (e.g. by a colleague). Recent
  changes shows the latest two, with **Show all** for the rest, so the list
  stays in view. Click a
  column heading (Score, Business, Contact, Miles) to sort; the tab, filter,
  tier and sort are kept in the address, so a reload or a shared link shows the
  same view. **Download Excel** / **Download CSV** say "Preparing Excel…" while
  the file is built (10,000 leads take about a second) and ignore a second click
  meanwhile. Phone numbers are tap-to-call links; a business without one says
  "No phone listed", and one without a street address says "No street address"
  (next to its map link). Each score shows its tier and what the tier means
  ("55" over "B likely": A strong, B likely, C possible, D weak; hovering says
  it in full). The list refreshes every
  minute and when you come back to the tab, so colleagues' marks and calls show
  up without a reload; each refresh only fetches the businesses that changed
  (`GET /leads?since=…`, which also sends the new tab counts): in full the ones
  in the view shown, just their id for the rest, and when more changed than the
  page shows (a search just touched them all) the page simply asks for its 300
  rows again, so a refresh is never bigger than one page. The server keeps the
  parsed saved list between requests and reads it again only when a search has
  changed it, so opening a tab stays quick however long the list grows.
  **Just called** is on every business (not competitors): staff usually find
  out whether a business has a baler by phoning it, so a call can be logged
  before it is marked, and logging a call never changes its Yes / No answer.
  Below 700 px wide (phones) each lead is a card: the business name with its
  score and tier, right above its **Yes** / **No** buttons and **Just called**, then the address with a map link, the tap-to-call
  phone and the miles, with the reasons for the score behind **Why this score**.
  Nothing scrolls sideways; a **Sort** list replaces the column headings.
- **Calls**: on any business on the Leads page, whatever its Yes / No answer,
  **Just called** opens a
  Conversation Summary box and the result of the call (Interested, Follow Up,
  Not Interested, Not Qualified, No Contact or Bad Lead). Every call is kept
  for good (a saved call can be undone for 5 minutes, like a mark); **History**
  shows them all. Typed notes are never lost: closing the box (Cancel, Escape,
  even a reload) keeps them as a draft for that business in this browser until
  they are saved. A call sent twice (a retry on a flaky connection) is recorded
  once: the page gives each call its own id. The Calls page lists every called
  business, whatever its mark, and opens on **All
  called businesses**: one row per business, latest call first, so a call just
  saved is in view (every call is under **History**); then there is a tab per
  result, where a business sits under its latest call's result (`#calls?tab=Follow Up`
  in the address opens that tab). The Conversation summary column shows the
  latest call's notes; when the latest call had none it says so and shows the
  most recent notes from an earlier call, with that call's date (the downloads'
  Call Notes column does the same). With no calls yet it says how to log one.
  Below 700 px wide (phones) each called business is a card like on the Leads
  page: name and latest result, the tap-to-call phone, the Conversation summary,
  then **Just called**, **History** and Undo, with nothing scrolling sideways. A
  business closed for good says so under its name.
- **Stats**: how many businesses have a baler or compactor (marked Yes), their
  average score, and a chart of the share of checked businesses (marked Yes or
  No) that have one, by tier. Competitors and Arco's own listing are left out of
  every figure (the page says how many), since the numbers measure how well the
  scoring finds prospects. Businesses that have since closed for good still
  count (the page says how many of the checked ones have closed). On a wide
  screen the table sits beside the chart. Tier D is usually empty, and the page says why:
  searches only save businesses scoring at least the minimum score (20; the tier
  boundaries come from the scoring settings). Before anything is marked, the page
  shows only a note saying how to fill it, with a link to the Leads page.

Marks and the latest call (result, time and notes) are also columns in the downloads.

If the database can't be reached, each page says so in one plain message with a
**Retry** button (nothing is lost), and Find leads and the downloads are off
until it is back. If only the Yes / No marks or calls can't be read, the pages
say that too rather than showing every business as unchecked. Unknown
addresses and server errors show a branded page with a link back.

## How leads are scored (0 to 100)

Every lead's **Why This Score** column lists each rule that fired, so a bad
ranking can be traced to a specific rule and fixed in `leadgen/config.py`.

| Signal | Points |
| --- | --- |
| Business type: grocery 35, warehouse club/wholesale 34, warehouse/distribution 34, big-box 32, food & beverage production 32, recycling/waste 30, manufacturing 28, hospital 28, mall/stadium/airport 28, university 20, hotel 18, mid-size retail (electronics, sporting goods, furniture, discount) 15, apartments 15, government/correctional 15, restaurant 8, other retail 8. What the listing says a place is (Google type / Yelp category / map tag) wins over words in its name, and vets, clinics, pharmacies, gas stations, parking and the like never count as prospects | by type |
| Known high-volume brand (Walmart, Costco, Smith's, Harmons, Home Depot, Amazon, Intermountain, ...). Brands that are also surnames or common words (Smith's, Target, UPS) only count when the business type, map brand tag or website backs them up | +20 |
| Busy site: Google reviews ≥100 / ≥500 / ≥2,000, or Yelp reviews ≥40 / ≥150 / ≥400 (the stronger one counts) | +5 / +10 / +15 |
| Big building: approx. footprint ≥15k / ≥40k / ≥100k sq ft (OpenStreetMap outlines) | +5 / +10 / +15 |
| Matches your keywords | +10 each, max +20 |

Tiers: **A** ≥ 60, **B** ≥ 40, **C** ≥ 20, **D** below 20.

Saved leads are always scored with the default keywords (compactor, baler,
waste, recycling), whatever a search typed, so a business's score and tier
depend only on facts about it and the Stats page's tiers stay comparable over
time. `tests/fixtures/scoring_reference.json` lists reference businesses and
whether they run a baler or compactor; a test fails if a scoring change stops
ranking them where they should. Its **confirmed** list holds real businesses
Arco's staff marked Yes / No on the Leads page, with the tier each had:
`python -m leadgen reference` (with `DATABASE_URL` set to the website's
database) refreshes it from the site's marks, copying only the facts the
scoring reads (no phone numbers, addresses or call notes). A test then fails
if a scoring change moves a business marked Yes to a lower tier, or one marked
No to a higher one. Refresh it every month or so, run the tests, and commit it.

**Lead Type** column:
- `Prospect`: a business that likely produces the waste.
- `Waste / recycling facility`: runs balers/compactors itself (buyers or partners).
- `Industry (equipment / hauler)`: name mentions compactors, balers, dumpsters or hauling. Probably a dealer, servicer or hauler.
- `Competitor`: Pro Baler or Action Compaction (orange rows in Excel).
- `Own company`: Arco Compactor.

## What's in the export

Score, tier, lead type, flags, name, category, address, city, state, ZIP,
phone (formatted), website, distance, why-this-score, matched keywords, Google
and Yelp review counts, approx. footprint, source category, which searches found it,
source(s), map link, lat/lon, "Has Baler or Compactor?", the latest call's
result, time and notes, plus empty **My notes: equipment seen (this file only)** (a dropdown: saw a
compactor, saw a baler, saw neither, not sure) and **My notes (this file only)**
columns for private notes on a printed or offline copy:
nothing typed there goes back into the website (record Yes / No and calls on
the Leads page). A second sheet, **Run Info**, records when it was made (Utah
time) and what the file holds: for a search, its settings and counts; for the
saved list (**Download Excel** on the Leads page), how many leads, how many are
marked Yes / No / not checked, competitors and own listing, the count per
tier, how many were called, and the dates of the first and latest search. The
Excel file is written by `leadgen/xlsx.py` in time that grows in step with the
list (10,000 leads in about a second).

---

© Wright AI Solutions. Back to the [README](../README.md).
