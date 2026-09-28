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

# Command line: 30 miles around Salt Lake City, saved to output/leads.xlsx
python -m leadgen run

# Web page at http://127.0.0.1:5000
python -m leadgen web
```

It works with **no API key** (free OpenStreetMap data). For much better
coverage, phone numbers and websites, add a Google Places key:

```bash
cp .env.example .env     # then paste your key into .env
```

To get a key: Google Cloud Console → create a project → enable
**Places API (New)** → Credentials → Create API key.

## Command line options

```bash
python -m leadgen run --location 84101 --radius 30 --keywords compactor baler recycling --out leads.xlsx
python -m leadgen run --location "Ogden, UT" --radius 15 --out ogden.csv
python -m leadgen run --source google --grid 7                        # wider Google coverage
python -m leadgen run --grid 7 --max-requests 300                     # wider, but capped spend
python -m leadgen run --min-score 40 --limit 200                      # only strong leads
```

| Option | Default | What it does |
| --- | --- | --- |
| `--location` | Salt Lake City, UT | ZIP, city, address, or `lat,lon` |
| `--radius` | 30 | Miles from the center; results outside are removed |
| `--keywords` | compactor baler waste recycling | Extra search terms; matches add points |
| `--source` | auto | `auto` = Google + OpenStreetMap if a key is set, else OpenStreetMap. Also `google`, `osm`, `both` |
| `--min-score` | 20 | Drop leads below this score (competitors are always kept) |
| `--grid` | 1 | Google only. 1, 7 or 19 search cells. Google caps each search at 60 results, so more cells find more businesses (and cost more) |
| `--max-requests` | auto | Google only. Hard cap on API calls per run. Auto = enough for every search (about 100 at `--grid 1`). Under a cap, every search gets its first page before any gets a second, and your keywords and the competitor names are searched first |
| `--only-keyword-matches` | off | Keep only leads matching a keyword |
| `--limit` | 0 (all) | Keep the top N prospects (competitors are always kept) |
| `--out` | output/leads.xlsx | `.xlsx` or `.csv` |

## How leads are scored (0 to 100)

Every lead's **Why This Score** column lists each rule that fired, so a bad
ranking can be traced to a specific rule and fixed in `leadgen/config.py`.

| Signal | Points |
| --- | --- |
| Business type: grocery 35, warehouse/distribution 34, big-box 32, food & beverage production 32, recycling/waste 30, manufacturing 28, hospital 28, mall/stadium/airport 28, university 20, hotel 18, mid-size retail (electronics, sporting goods, furniture, discount) 15, apartments 15, government/correctional 15, restaurant 8, other retail 8. What the listing says a place is (Google type / map tag) wins over words in its name | by type |
| Known high-volume brand (Walmart, Costco, Smith's, Harmons, Home Depot, Amazon, Intermountain, ...) | +20 |
| Busy site: Google review count ≥100 / ≥500 / ≥2,000 | +5 / +10 / +15 |
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
review count, approx. footprint, source category, which searches found it,
source(s), map link, lat/lon, plus empty **Verified?** (dropdown) and **Notes**
columns for vetting. A second sheet, **Run Info**, records the settings and
counts for each run.

## Tuning after vetting

Everything adjustable is in `leadgen/config.py`:
- `CATEGORIES`: business types, their weights, and the Google types / OSM tags / name words that identify them.
- `HIGH_VOLUME_BRANDS`: chains that almost always have a baler or compactor.
- `GOOGLE_QUERIES`: the search phrases sent to Google.
- `COMPETITORS`: names and website fragments to flag.

## How it works

1. **Geocode** the location (built-in table for SLC-area cities, then ZIP lookup, Google, or OpenStreetMap Nominatim).
2. **Search**: Google Places text search for your keywords, the competitor names, then ~27 business-type phrases, across 1/7/19 grid cells; and/or one OpenStreetMap Overpass query for matching tags and name words (several mirror servers are tried).
3. **Filter** to the exact radius (Haversine distance).
4. **Dedupe**: listings within ~200 m with matching names (or the same phone) are merged, keeping Google's contact details and OpenStreetMap's building size. Different phone numbers or names that only share generic words ("Inn & Suites Airport") are kept apart. Places Google reports permanently closed are then dropped.
5. **Score**, sort by score then distance, and **export**.

API responses are cached in `.cache/` for 7 days, so re-running the same search
is instant and doesn't re-bill Google.

## Cost notes (Google)

With the default `--grid 1`, a run makes at most about 100 requests (about 33
phrases × up to 3 pages). The command prints the maximum before it starts, and
`--max-requests` caps it. Google's pricing for Text Search
with phone/website fields falls under the Enterprise SKU (roughly $35 per 1,000
requests after the monthly free allowance), so a default run costs a few
dollars at most, and cached re-runs are free. `--grid 7` or `--grid 19` multiplies
requests by 7 or 19 (up to about 700 or 1,900), so set `--max-requests` if cost matters.

## Limits and next steps

- OpenStreetMap coverage of businesses is uneven and often lacks phones; Google is recommended for production runs.
- The score estimates likelihood; it cannot see whether a compactor is actually on site. That's what vetting is for.
- Ideas from the spec for later: website text scanning / AI classification, USPS address validation, enrichment (employees, NAICS), scheduled weekly runs with email, CRM export.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```
