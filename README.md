# Compactor Lead List Generator

The Lead Finder: AARCO Waste & Disposal's website for finding businesses around its
Salt Lake City shop (or any ZIP or city) that are likely to run **large commercial
trash compactors or cardboard balers**. Once a Utah day it searches Yelp and the free
OpenStreetMap data (Google Places is supported, but the owner chose not to use it on
2026-10-08), scores each business and adds it to one saved list,
where the sales team marks Yes / No, logs calls, sees every business on a map and
downloads the list to Excel or CSV. Competitors and AARCO's own listings are flagged,
never dropped, and never offered as prospects.

## Emergency stop

On the site: **Find leads** → **For the site administrator** (at the bottom, closed
until opened, then unlocked with the administrator password, `ADMIN_PASSWORD`) →
**Pause searching** → confirm **Pause searching** (or **Stop using Google** / **Stop
using Yelp**). It works on the next request, with no restart, and a search already
running stops within a few seconds (also during the free map data step), keeping and
saving what it had found. If the site itself won't load: in Render, the service →
**Environment**, add `LEADGEN_SEARCH_PAUSED` = `1` and **Save Changes**
(`LEADGEN_GOOGLE_OFF` / `LEADGEN_YELP_OFF` stop just one paid source). See the
[runbook](docs/operator-runbook.md#emergency-switches-stop-searches-or-paid-calls).

## Quick start

Needs **Python 3.11 or newer** (`python3 --version`; the code uses `datetime.UTC`,
which older versions lack).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m leadgen web     # the website, at http://127.0.0.1:5000
python -m leadgen run     # command line: 30 miles around AARCO's shop, saved to output/leads.xlsx
```

It works with **no API key** (free OpenStreetMap data). For phone numbers and much
better coverage, `cp .env.example .env` and add a Google Places key, a Yelp key, or
both ([how to get them](docs/developer-overview.md#api-keys-google-and-yelp)). The
other commands (merging duplicates, leads outside the area, backup and restore) and
every option are in the [developer overview](docs/developer-overview.md#run-it-on-your-computer).

## Where to look

| You are | Read |
| --- | --- |
| On the sales team | [Sales guide](docs/sales-guide.md): short how-tos for the day's search, marking, logging calls, the Map, Stats, downloads and what the scores mean |
| Looking after the live site | [Operator runbook](docs/operator-runbook.md): **emergency switches** (pause searching), deploying, rollback, alerts, settings, the database, and the [owner actions still open](docs/operator-runbook.md#owner-actions-still-open) |
| Changing the code | [Developer overview](docs/developer-overview.md): what each feature does, a diagram of search → sources → merge/score → saved list → pages, tests and checks, the command line, and every page's behaviour in detail |

| To | Do this | Details |
| --- | --- | --- |
| Run the day's search | **Find leads** → check the place and miles → **Find leads** → **Start search** | [Sales guide](docs/sales-guide.md#run-the-days-search) |
| Mark a business, log a call | **Leads** → **Yes** / **No**; **Just called** → summary and result → **Save** | [Sales guide](docs/sales-guide.md#mark-yes-or-no) |
| Save a verified phone or contact | **Leads** → **Add verified phone or contact** under the business's phone | [Sales guide](docs/sales-guide.md#save-a-verified-phone-or-contact) |
| See the area and every business on a map | **Map** → tap a pin → **Open on the Leads page** | [Sales guide](docs/sales-guide.md#see-the-businesses-on-the-map) |
| Download the list | **Leads** → **Download Excel** / **Download CSV** | [Sales guide](docs/sales-guide.md#download-the-list) |
| Undo a bad deploy | Render → the service → **Events** → last good deploy → **Rollback** | [Runbook](docs/operator-runbook.md#logs-and-rolling-back-a-bad-deploy) |
| Back up or restore the data | `python -m leadgen backup`; `python -m leadgen restore <copy>` lists, `--apply` adds (never overwrites) | [Runbook](docs/operator-runbook.md#backups-and-restoring) |
| Check that Yelp, Google, the map data and alerts work | `python -m leadgen check-sources` (`--recorded` needs no keys); `python -m leadgen test-alert`; or the admin section's **Check sources** / **Send a test alert**; `/healthz` shows each source's last success | [Runbook](docs/operator-runbook.md#checking-the-sources-and-the-alerts) |

## Deploying

The site runs on Render's Starter plan, which never sleeps; to put it online, follow
[Put it online (Render)](docs/operator-runbook.md#put-it-online-render). Every push to
`main` deploys on its own, but only after the GitHub checks `lint`, `test (sqlite)`
and `test (postgres)` pass, so a commit with a failing test never reaches the live site
(`autoDeployTrigger: checksPass` in `render.yaml`). **`main` is protected (since
2026-10-03).** The ruleset `Protect main` requires the same three checks and blocks
deleting `main` and force pushes, so work is pushed to a branch first and reaches
`main` only once its checks are green. The runbook's
[Deploying](docs/operator-runbook.md#deploying) has the steps, how to check it, and how
to roll back.

---

© Wright AI Solutions.
