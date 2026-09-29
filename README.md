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

The sidebar has four pages:

- **Find leads**: the search form (extra settings under **More options**), a
  step-by-step progress bar while it runs, and the search history with each
  search's counts under **Details**. **Find Leads works once per calendar day**
  (Utah time), for the whole site: after today's search it is off until
  midnight. A search that fails outright (e.g. an unknown location) gives the
  day back, and so does one that never finished (the server restarted) after
  30 minutes.
- **Leads**: the saved list, with **Yes** / **No** buttons for "has a baler or
  compactor" and tabs for Not checked, Has baler or compactor, No baler or
  compactor, and All. A mark is kept for good: it can be switched between Yes
  and No but never goes back to Not checked, and a later search that finds the
  business again updates its row without moving it.
- **Calls**: in the Has baler or compactor tab, **Just called** opens a
  Conversation Summary box and the result of the call (Interested, Follow Up,
  Not Interested, Not Qualified, No Contact or Bad Lead). Every call is kept
  for good; **History** shows them all. The Calls page has a tab per result,
  and a business sits under its latest call's result.
- **Stats**: how many businesses have a baler or compactor (marked Yes), their
  average score, and a chart of the share of checked businesses (marked Yes or
  No) that have one, by tier.

Marks and the latest call (result, time and notes) are also columns in the downloads.

Always set `APP_PASSWORD` on a public site: every search can spend your API keys.
A login lasts 30 days on a device; changing the username or password logs everyone
out. After 10 wrong passwords from one address, logins from it pause for 15 minutes.
Without a password the page only answers on `localhost` or an IP address; to use
another hostname, list it in `LEADGEN_ALLOWED_HOSTS`.
The free plan sleeps after 15 idle minutes, so the first visit takes about a minute
to wake up, and its disk is wiped on each restart (which is why data lives in the database).
To run the production server yourself: `gunicorn wsgi:app --workers 1 --threads 8 --timeout 0`.

## Command line options

```bash
python -m leadgen run --location 84101 --radius 30 --keywords compactor baler recycling --out leads.xlsx
python -m leadgen run --location "Ogden, UT" --radius 15 --out ogden.csv
python -m leadgen run --source google --grid 7                        # wider Google coverage
python -m leadgen run --source yelp --max-requests 20                 # Yelp only, 20 calls
python -m leadgen run --grid 7 --max-requests 300                     # wider, but capped spend
python -m leadgen run --min-score 40 --limit 200                      # only strong leads
```

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
result, time and notes, plus empty **Verified?** (dropdown) and **Notes**
columns for vetting. A second sheet, **Run Info**, records the settings and
counts for each run.

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
2. **Search**: Google Places text search for your keywords, the competitor names, then ~27 business-type phrases, across 1/7/19 grid cells; Yelp's 13 category searches (most-reviewed first), then word searches for your keywords and the competitor names; and one OpenStreetMap Overpass query for matching tags and name words (several mirror servers are tried).
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
  a day in total, across all searches (`YELP_DAILY_LIMIT`). Every call counts,
  retries included; nothing in the form or on the command line raises it. Each
  call counts for 24 hours, so the calls come back 24 hours after the last
  search that used them; the page shows how many are left and when they reset
  (any 24 hours also covers Yelp's own day, which starts at midnight UTC).
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

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```
