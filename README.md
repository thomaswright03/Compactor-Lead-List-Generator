# Compactor Lead List Generator

Finds businesses around Salt Lake City (or any ZIP/city) that are likely to run
**large commercial trash compactors or cardboard balers**, scores each one, and
exports a ranked lead list to Excel or CSV for manual vetting. Built for
Arco Compactor.

Competitors (**Pro Baler**, **Action Compaction**) are **flagged, never dropped**,
and Arco Compactor's own listing is flagged too.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Command line: 30 miles around Arco Compactor (876 Fortune Rd, SLC), saved to output/leads.xlsx
python -m leadgen run

# Web page at http://127.0.0.1:5000
python -m leadgen web
```

It works with **no API key** (free OpenStreetMap data). For much better
coverage and phone numbers, add a Google Places key, a Yelp key, or both:

```bash
cp .env.example .env     # then paste your key(s) into .env
```

- **Google Places** (best: phones and websites): Google Cloud Console → create
  a project → enable **Places API (New)** → Credentials → Create API key.
  Set `GOOGLE_PLACES_API_KEY`.
- **Yelp** (phones and review counts for stores, hotels, hospitals; no websites):
  https://www.yelp.com/developers/v3/manage_app → copy the **API Key** (not the
  Client ID). Set `YELP_API_KEY`. See the Yelp notes below before relying on it.

With `--source auto` (the default) every source that has a key is used,
together with OpenStreetMap, and the results are merged.

## Put it online (Render)

The repo includes `render.yaml`, so Render can set everything up:

1. Sign in at https://render.com with GitHub.
2. **New** > **Blueprint**, pick this repo, and click **Apply**.
3. When asked, set **APP_USERNAME** and **APP_PASSWORD** (the login page asks for them; both are case-sensitive) and, optionally, **GOOGLE_PLACES_API_KEY** and/or **YELP_API_KEY**.
4. Open the `onrender.com` link Render shows.

### The database (saved leads, marks, calls, the Yelp count)

Render's disk is wiped on every redeploy, so the site keeps its data in a
Postgres database named by `DATABASE_URL`. A free Neon database works and does
not expire (Render's free Postgres is deleted after 30 days):

1. Sign up at https://neon.tech (GitHub login works) and create a project
   (region US West (Oregon), near Render's servers).
2. Click **Connect** and copy the connection string (`postgresql://...`).
3. In Render, open the service, **Environment**, add `DATABASE_URL` with that
   string, and save (the service restarts).

The tables are created on first use. Without `DATABASE_URL` on Render, searches
still run, but nothing is saved and Yelp is paused (its daily limit could not be
kept). Off Render, a SQLite file in `.cache/` is used instead.

**Saved leads.** Every search merges into one saved list: a business found again
(the same listing, or the duplicate rules below) updates its row instead of
adding one. The page shows the saved list when it opens, and the downloads
contain all of it. Everything is kept, including Yelp's details, although
Yelp's terms allow keeping its data for 24 hours (and Google's for 30 days);
`SAVED_SOURCE_KEEP_SECONDS` in `leadgen/config.py` drops a source's details
after a set time instead, keeping the business's id so its mark comes back.

### The website's pages

The sidebar has four pages, a Light / Dark / System colour switch (System
follows the computer's setting; the choice is remembered in each browser), and
every page carries the Wright AI Solutions copyright. The browser tab names the
page ("Leads · Arco Compactor Lead Finder") and shows the Lead Finder icon.
On phones and tablets every button and link is at least 44 px each way. On a
phone the navigation is one short bar at the top (the page counts show just the
number), with the Yelp count, the colour switch and Log out under **Menu**; the
browser's own bar takes the sidebar's colour, light or dark. Times are written
one way everywhere, in Utah time: "Sep 29, 2026, 4:43 PM", or "4:43 PM".

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
  restarted), after 30 minutes (the page says when, in Utah time). The map data step gives up after two minutes
  in all (`OVERPASS_DEADLINE_SECONDS`); a map server that hasn't answered after
  25 seconds (`OVERPASS_STAGGER_SECONDS`) is not waited out: the next one is asked
  as well and the first good answer wins, so one hanging server can't use up the
  time the others need. The progress bar says when a step
  is taking longer than usual. Steps that don't apply (Google and Yelp when
  neither is set up) are shown as skipped.
- **Leads**: the saved list, with **Yes** / **No** buttons for "has a baler or
  compactor" and tabs for Not checked, Has baler or compactor, No baler or
  compactor, Competitors, and All. Competitors and Arco's own listing are
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
  newer change has been made to that business (e.g. by a colleague). Click a
  column heading (Score, Business, Contact, Miles) to sort; the tab, filter,
  tier and sort are kept in the address, so a reload or a shared link shows the
  same view. **Download Excel** / **Download CSV** say "Preparing Excel…" while
  the file is built (10,000 leads take about a second) and ignore a second click
  meanwhile. Phone numbers are tap-to-call links. The list refreshes every
  minute and when you come back to the tab, so colleagues' marks and calls show
  up without a reload; each refresh only fetches the businesses that changed
  (`GET /leads?since=…`, which also sends the new tab counts), so it stays small
  however long the list grows.
  Tabs other than Has baler or compactor say where calls are logged.
  Below 700 px wide (phones) each lead is a card: the business name with its
  score, right above its **Yes** / **No** buttons (and **Just called** on the Has
  baler or compactor tab), then the address with a map link, the tap-to-call
  phone and the miles, with the reasons for the score behind **Why this score**.
  Nothing scrolls sideways; a **Sort** list replaces the column headings.
- **Calls**: in the Has baler or compactor tab, **Just called** opens a
  Conversation Summary box and the result of the call (Interested, Follow Up,
  Not Interested, Not Qualified, No Contact or Bad Lead). Every call is kept
  for good (a saved call can be undone for 5 minutes, like a mark); **History**
  shows them all. Typed notes are never lost: closing the box (Cancel, Escape,
  even a reload) keeps them as a draft for that business in this browser until
  they are saved. A call sent twice (a retry on a flaky connection) is recorded
  once: the page gives each call its own id. The Calls page opens on **All
  called businesses**: one row per business, latest call first, so a call just
  saved is in view (every call is under **History**); then there is a tab per
  result, where a business sits under its latest call's result (`#calls?tab=Follow Up`
  in the address opens that tab). The Conversation summary column shows the
  latest call's notes; when the latest call had none it says so and shows the
  most recent notes from an earlier call, with that call's date (the downloads'
  Call Notes column does the same). With no calls yet it says how to log one.
- **Stats**: how many businesses have a baler or compactor (marked Yes), their
  average score, and a chart of the share of checked businesses (marked Yes or
  No) that have one, by tier. Competitors and Arco's own listing are left out of
  every figure (the page says how many), since the numbers measure how well the
  scoring finds prospects. Tier D is usually empty, and the page says why:
  searches only save businesses scoring at least the minimum score (20; the tier
  boundaries come from the scoring settings). Before anything is marked, the page
  shows only a note saying how to fill it, with a link to the Leads page.

Marks and the latest call (result, time and notes) are also columns in the downloads.

If the database can't be reached, each page says so in one plain message with a
**Retry** button (nothing is lost), and Find leads and the downloads are off
until it is back. If only the Yes / No marks or calls can't be read, the pages
say that too rather than showing every business as unchecked. Unknown
addresses and server errors show a branded page with a link back.

Always set `APP_PASSWORD` on a public site: every search can spend your API keys.
A login lasts 30 days on a device; changing the username or password logs everyone
out. After 10 wrong passwords from one address, logins from it pause for 15 minutes.
Without a password the page only answers on `localhost` or an IP address; to use
another hostname, list it in `LEADGEN_ALLOWED_HOSTS`.
The free plan sleeps after 15 idle minutes, so the first visit takes about a minute
to wake up, and its disk is wiped on each restart (which is why data lives in the database).
To run the production server yourself: `gunicorn wsgi:app --workers 1 --threads 8 --timeout 0`.

### Settings (environment variables)

In Render: open the service, **Environment**, add or edit the variable, then
**Save Changes** (the service restarts with it in about a minute). Locally, put
them in `.env` (see `.env.example`) or the shell.

| Variable | Needed? | Default | What it does |
| --- | --- | --- | --- |
| `APP_USERNAME` | On a public site | none (any name works) | The name to log in with (case-sensitive) |
| `APP_PASSWORD` | On a public site | none (no login; only `localhost`/IP access) | The password for the login page |
| `DATABASE_URL` | On Render | SQLite file in the cache folder | The permanent Postgres database (saved leads, marks, calls, searches, the Yelp count) |
| `GOOGLE_PLACES_API_KEY` | No | none | Google Places key (paid; best phones and websites) |
| `YELP_API_KEY` | No | none | Yelp key (at most 50 calls in any 24 hours) |
| `SECRET_KEY` | No | derived from the login | Signs the login cookie. Changing it (or the username or password) logs everyone out |
| `LEADGEN_SEARCH_PAUSED` | No | off | Emergency stop: `1` makes Find leads refuse to start (see below) |
| `LEADGEN_GOOGLE_OFF` | No | off | `1` stops every Google call (no Google charges); searches use the other sources |
| `LEADGEN_YELP_OFF` | No | off | `1` stops every Yelp call; searches use the other sources |
| `LEADGEN_SUPPORT_CONTACT` | No | "the person who manages the Lead Finder" | Who the login page tells people to ask for access, e.g. `Matt at (801) 555-0100` |
| `LEADGEN_ALLOWED_HOSTS` | No | `localhost` | Without a password, extra host names the page answers on (comma separated) |
| `LEADGEN_CACHE_DIR` | No | `.cache` | Folder for the API response cache and the local SQLite database |
| `RENDER`, `RENDER_GIT_COMMIT`, `PORT` | Set by Render | | Render's own; `/healthz` shows the deployed commit |
| `PYTHON_VERSION` | Render | 3.11.9 (`render.yaml`) | Python version Render builds with |
| `LEADGEN_TEST_DATABASE_URL` | Tests only | none | Runs the tests on a throwaway Postgres instead of SQLite (its tables are emptied) |
| `LEADGEN_CHROMIUM` | Tests only | Playwright's Chromium | A Chromium binary for the browser tests |

`1`, `true`, `yes` and `on` all switch a flag on; removing the variable (or
setting it to anything else) switches it off.

### Emergency switches: stop searches or paid calls

If searches misbehave (unexpected Google charges, bad data going into the saved
list), whoever looks after the site can stop them without a code change:

1. In Render, open the **compactor-lead-finder** service and click **Environment**.
2. Add `LEADGEN_SEARCH_PAUSED` with the value `1` to stop all searching, or
   `LEADGEN_GOOGLE_OFF` = `1` / `LEADGEN_YELP_OFF` = `1` to stop just that
   paid source (the free map data and everything else keep working).
3. Click **Save Changes**. The service restarts in about a minute; from then
   on Find leads says "Searching is paused by the administrator" and refuses
   to start. Leads, Calls, Stats and the downloads keep working. A search that
   is already running checks the switches between sources and before every
   paid Google or Yelp call: it stops there, keeps (and saves) what it found,
   and says it was stopped by the administrator.
4. To switch it back on, delete the variable (or set it to `0`) and save.

### Deploying

A push to `main` deploys automatically, but only once GitHub's CI checks have
passed on it: `render.yaml` sets `autoDeployTrigger: checksPass` (in Render:
**Settings** > **Build & Deploy** > **Auto-Deploy** = **After CI Checks Pass**).
A commit whose lint or tests fail is never deployed. A deploy takes about 5
minutes. To see which version is live, open
`https://compactor-lead-finder.onrender.com/healthz`: `version` is the first
seven characters of the deployed commit (`git log -1 --format=%h`). Every hour
GitHub Actions' **Live site** workflow (`.github/workflows/live.yml`, also
runnable by hand from the Actions tab) checks that the latest commit on `main`
that passed CI is live, and fails, which emails the owner, if it still isn't
40 minutes after the push (set a repository variable `LIVE_URL` if the address
changes).

Protect `main` so that nothing reaches it without green checks (a one-time
setting only the repository owner can make): GitHub → **Settings** →
**Branches** → **Add branch protection rule** for `main` → **Require status
checks to pass before merging**, and pick `lint`, `test (sqlite)` and
`test (postgres)`.

If it didn't deploy (the Live site workflow failed, or `/healthz` shows an old version):

1. In Render, open the **compactor-lead-finder** service. **Events** shows
   whether the last deploy failed (its log says why) or never started.
2. Check the commit's checks on GitHub are green (a red one is not deployed:
   fix it and push), that **Settings** > **Auto-Deploy** is **After CI Checks
   Pass** and that the branch is `main`.
3. Click **Manual Deploy** > **Deploy latest commit**, wait for "Live", and
   check `/healthz` again.

The Python packages are pinned to exact versions in `requirements.txt`, so a
deploy never picks up a new Flask or psycopg by surprise (the Excel files are
written by the site itself, `leadgen/xlsx.py`; openpyxl is only used by the tests). To upgrade
one, change its version there, run the tests, and push.

### Logs, and rolling back a bad deploy

The site logs to Render's **Logs** tab: every failed search, database error and
unexpected error, with the technical detail the pages leave out.

To go back to the previous version:

1. In Render, open the service and click **Events** (or **Deploys**).
2. Find the last deploy that worked, open its menu and choose **Rollback**
   (or **Redeploy** on older dashboards). Render builds and starts that commit.
3. Turn off **Auto-Deploy** (service **Settings**) until the fix is on `main`,
   or the next push deploys again; then fix forward with a new commit
   (`git revert <bad commit>` and push) and turn Auto-Deploy back on.

Rolling back is safe for the data: database changes are only ever additive
(new tables or columns, created on first use), so an older version keeps
working with the newer database, and nothing is deleted.

## Command line options

```bash
python -m leadgen run --location 84101 --radius 30 --keywords compactor baler recycling --out leads.xlsx
python -m leadgen run --location "Ogden, UT" --radius 15 --out ogden.csv
python -m leadgen run --source google --grid 7                        # wider Google coverage
python -m leadgen run --source yelp --max-requests 20                 # Yelp only, 20 calls
python -m leadgen run --grid 7 --max-requests 300                     # wider, but capped spend
python -m leadgen run --min-score 40 --limit 200                      # only strong leads
python -m leadgen reference       # copy the site's Yes / No marks into the scoring tests
```

Yelp is only used on the command line when `DATABASE_URL` points at the
website's database, so its calls count against the site's one limit of 50 a
day (see the Yelp notes). Without it, `--source yelp` stops with an
explanation, and `--source auto` leaves Yelp out and says so.

| Option | Default | What it does |
| --- | --- | --- |
| `--location` | Arco Compactor (876 Fortune Rd, Salt Lake City) | ZIP, city, address, or `lat,lon`. The radius and the Miles column are measured from here |
| `--radius` | 30 | Miles from the center; results outside are removed |
| `--keywords` | compactor baler waste recycling | Extra search terms; matches add points |
| `--source` | auto | `auto` = OpenStreetMap plus Google and/or Yelp when their key is set. Also `google`, `yelp`, `osm`, and `both` (Google + OpenStreetMap) |
| `--min-score` | 20 | Drop leads below this score (competitors are always kept) |
| `--grid` | 1 | Google and Yelp. 1, 7 or 19 search cells. Google caps each search at 60 results and Yelp at 240, so more cells find more businesses (and cost more). Yelp searches at most 25 miles around a point, so past 25 miles it needs 7 cells; at the default 1 it searches the 25 miles around the center instead when 7 would take more calls than are left |
| `--max-requests` | auto | Hard cap on API calls per run, for Google and for Yelp separately. Auto = Google: enough for every search (about 100 at `--grid 1`); Yelp: 50. Yelp never goes past its daily limit of 50 calls, whatever the cap. Under a cap, every search gets its first page before any gets a second; Google searches your keywords and the competitor names first, Yelp its category searches |
| `--only-keyword-matches` | off | Keep only leads matching a keyword |
| `--limit` | 0 (all) | Keep the top N prospects (competitors are always kept) |
| `--out` | output/leads.xlsx | `.xlsx` or `.csv` |
| `--api-key`, `--yelp-api-key` | from `.env` | Instead of `GOOGLE_PLACES_API_KEY` / `YELP_API_KEY` |

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

## Tuning after vetting

Everything adjustable is in `leadgen/config.py`:
- `CATEGORIES`: business types, their weights, and the Google types / Yelp categories / OSM tags / name words that identify them.
- `HIGH_VOLUME_BRANDS`: chains that almost always have a baler or compactor.
- `GOOGLE_QUERIES`: the search phrases sent to Google.
- `YELP_SEARCHES`: the Yelp category searches, `YELP_DAILY_LIMIT` (50) and `YELP_DEFAULT_MAX_REQUESTS`.
- `REVIEW_BONUS`: review-count thresholds for Google and Yelp.
- `COMPETITORS`: names and website fragments to flag.

## How it works

1. **Geocode** the location (built-in table for SLC-area cities, then ZIP lookup, Google, or OpenStreetMap Nominatim).
2. **Search**: Google Places text search for your keywords, the competitor names, then ~27 business-type phrases, across 1/7/19 grid cells; Yelp's 13 category searches (most-reviewed first), then word searches for your keywords and the competitor names; and one OpenStreetMap Overpass query for matching tags and name words (several mirror servers are tried, within two minutes in all).
3. **Filter** to the exact radius (Haversine distance).
4. **Dedupe**: listings within ~200 m with matching names (or the same phone) are merged, keeping Google's (then Yelp's) contact details and OpenStreetMap's building size. Different phone numbers or names that only share generic words ("Inn & Suites Airport") are kept apart. Places Google or Yelp report permanently closed are then dropped.
5. **Score**, sort by score then distance, and **export**.

API responses are cached for 7 days (Yelp searches in the database), so
re-running the same search is instant and doesn't re-bill.

## Cost notes (Google)

With the default `--grid 1`, a run makes at most about 100 requests (about 33
phrases × up to 3 pages). The command prints the maximum before it starts, and
`--max-requests` caps it. Google's pricing for Text Search
with phone/website fields falls under the Enterprise SKU (roughly $35 per 1,000
requests after the monthly free allowance), so a default run costs a few
dollars at most, and cached re-runs are free. `--grid 7` or `--grid 19` multiplies
requests by 7 or 19 (up to about 700 or 1,900), so set `--max-requests` if cost matters.

## Yelp notes

- The same Yelp key is used elsewhere, so this tool makes at most 50 Yelp calls
  a day in total, across all searches (`YELP_DAILY_LIMIT`), from the website and
  the command line: both count in the website's database (the command line only
  uses Yelp when `DATABASE_URL` points at it). Every call counts,
  retries included; nothing in the form or on the command line raises it. Each
  call counts for 24 hours, so the calls come back 24 hours after the last
  search that used them; the page shows how many are left in the last 24
  hours and when they are all back ("Yelp: 20 of 50 calls left in the last 24
  hours; all back by 3:12 PM"; any 24 hours also covers Yelp's own day, which
  starts at midnight UTC).
- The count lives in the database, so restarts and redeploys don't reset it.
  If the database can't be reached, Yelp is paused rather than risk going over.
- With 50 calls, a 30-mile search uses one Yelp search area (25 miles around
  the center, Yelp's reach) unless you pick a wider coverage; OpenStreetMap
  still covers the full radius. A run also stops early, keeping what it found,
  when Yelp says 5 or fewer calls are left on the key today.
- Re-running within 7 days reuses what was fetched and spends calls only on
  continuing searches deeper where the last run stopped.
- Yelp returns no business websites and only lists places with at least one
  review, so warehouses and plants are thin; OpenStreetMap fills those in.
- Yelp's trial is for evaluation, and its terms restrict commercial use and
  analysis of its data. Check them (or use Google) before relying on Yelp for
  production lead lists.

## Limits and next steps

- OpenStreetMap coverage of businesses is uneven and often lacks phones; Google is recommended for production runs, and Yelp helps for stores, hotels and hospitals.
- The score estimates likelihood; it cannot see whether a compactor is actually on site. That's what vetting is for.
- Ideas from the spec for later: website text scanning / AI classification, USPS address validation, enrichment (employees, NAICS), scheduled weekly runs with email, CRM export.

## Tests and checks

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium          # for the browser tests
python -m pytest                               # SQLite
LEADGEN_TEST_DATABASE_URL=postgresql://... python -m pytest   # a throwaway Postgres
python -m ruff check .                         # lint (settings in pyproject.toml)
python -m vulture                              # dead code (settings in pyproject.toml)
python -m mypy                                 # strict type check (settings in pyproject.toml)
```

The code: `leadgen/pipeline.py` runs a search (sources in `leadgen/sources/`);
`leadgen/saved.py`, `marks.py`, `calls.py` and `daily.py` keep the saved data
(`store.py` is the database). The website is `leadgen/web/`: `auth.py` (login),
`finding.py` (Find leads), `leads.py` (the saved list, marks, calls, stats,
downloads) and `common.py`. `export.py` lays out the CSV and Excel files and
`xlsx.py` writes the Excel format; `reference.py` copies the site's marks into
the scoring tests. The page's markup is `leadgen/templates/index.html`;
its script and styles are in `leadgen/static/` (`core.js` first, then one file
per page, then `start.js`). Spacing, radii and text sizes come from one scale of
CSS variables in `templates/_theme.html` (`--sp-1` … `--sp-6`, `--fs-sm` …
`--fs-xl`); use those rather than raw pixel values.

The tests never call Google, Yelp or OpenStreetMap (they are mocked).
`tests/test_browser.py` drives the real pages in Chromium (mark Yes, a
double-click marks one business only, Undo, Just called and its kept draft, the
Calls page opening on the latest call and keeping earlier notes in view,
competitors not asked Yes / No, the search confirmation, a refused search saying
why (in the browser and from the server), the phone cards and the short phone
header, Stats, downloads and their busy state, reload keeps the
view, the colour switch fitting from 320 px up, 44 px touch targets on a
phone); it is skipped when Playwright's Chromium is missing.
`tests/test_cli.py` runs the command line with the sources mocked.

GitHub Actions (`.github/workflows/ci.yml`) runs the lint, the dead-code and
strict type checks and every test, on SQLite and on Postgres, for each push and
pull request; Render deploys only commits that pass, and branch protection on
`main` should require the three jobs (see "Deploying"). Every public function in
`leadgen/` has parameter and return annotations (`strict = true` for mypy).
