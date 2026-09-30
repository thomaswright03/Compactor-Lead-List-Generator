# Compactor Lead List Generator

Finds businesses around Salt Lake City (or any ZIP/city) that are likely to run
**large commercial trash compactors or cardboard balers**, scores each one, and
exports a ranked lead list to Excel or CSV for manual vetting. Built for
Arco Compactor.

Competitors (**Pro Baler**, **Action Compaction**) are **flagged, never dropped**,
and Arco Compactor's own listing is flagged too.

## Where to look

| You are | Read |
| --- | --- |
| On the sales team | [Sales guide](docs/sales-guide.md): short how-tos for the day's search, marking, logging calls, Stats, downloads and what the scores mean |
| Looking after the live site | [Operator runbook](docs/operator-runbook.md): **emergency switches** (pause searching), deploying, rollback, alerts, settings, the database |
| Changing the code | [Developer overview](docs/developer-overview.md): a diagram of search → sources → merge/score → saved list → pages, tests and checks, the command line, and every page's behaviour in detail |

**Emergency stop:** on the site, **Find leads** → **Site switches** → **Pause
searching** → **Turn on** (or switch just Google or Yelp off). It works on the next
request, with no restart. The backup, if the site itself won't load: in Render, the
service → **Environment**, add `LEADGEN_SEARCH_PAUSED` = `1` and **Save Changes**
(`LEADGEN_GOOGLE_OFF` / `LEADGEN_YELP_OFF` stop just one paid source). See the
[runbook](docs/operator-runbook.md#emergency-switches-stop-searches-or-paid-calls).

## What it does

- **Find leads** once a day (Utah calendar day) around Arco's shop or any ZIP or
  city, from Google Places, Yelp (at most 50 calls in any 24 hours, with the reset
  time shown, "today at ..." or "tomorrow at ...") and the free OpenStreetMap data
  (asked in parts, so busy public servers still answer). Extra search words add
  businesses to look for; scores always use the standard words, so the minimum
  score, the list and Stats all use the same number.
- One **saved list**, one row per business, kept for good with its source details.
- **Yes / No** "has a baler or compactor" marks (permanent; a click can be undone
  for 5 minutes), **Just called** notes with six results and a Calls tab for each,
  a **Stats** page, and Excel / CSV downloads (in plain words; columns empty for
  every lead in the file are left out and named on the Run Info sheet). Each mark and call records who made
  it (the name set under **Your name** in that browser).
- Works on phones, tablets and laptops: below 1,100 px wide each lead is a card,
  so the reasons for its score are always in view without scrolling sideways.
- A login from `APP_USERNAME` / `APP_PASSWORD` in the environment.

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
  Client ID). Set `YELP_API_KEY`. See the Yelp notes in the [operator runbook](docs/operator-runbook.md#yelp-notes) before relying on it.

With `--source auto` (the default) every source that has a key is used,
together with OpenStreetMap, and the results are merged.

To put it online, follow "Put it online (Render)" in the
[operator runbook](docs/operator-runbook.md#put-it-online-render).

---

© Wright AI Solutions.
