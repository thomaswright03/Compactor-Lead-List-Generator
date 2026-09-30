# Lead Finder: operator runbook

For whoever looks after the live site (Render + a Postgres database). Sales
staff want the [sales guide](sales-guide.md); developers the
[developer overview](developer-overview.md).

**In an emergency** (unexpected charges, bad data): set `LEADGEN_SEARCH_PAUSED=1`
in Render → the service → **Environment** → **Save Changes**. Details just below.

- [Emergency switches](#emergency-switches-stop-searches-or-paid-calls)
- [Deploying](#deploying) · [Logs and rollback](#logs-and-rolling-back-a-bad-deploy)
- [Alerts: the webhook](#problems-the-webhook)
- [Settings](#settings-environment-variables) · [Put it online and the database](#put-it-online-render)
- [Login](#login-and-the-free-plan) · [Yelp limits](#yelp-notes) · [Google costs](#cost-notes-google)

## Emergency switches: stop searches or paid calls

If searches misbehave (unexpected Google charges, bad data going into the saved
list), whoever looks after the site can stop them without a code change.

**On the site (first choice, works at once):** open **Find leads**, then **Site
switches** at the bottom. **Pause searching** → **Turn on** stops all searching;
**Switch Google off** / **Switch Yelp off** (shown when that source is set up) stop
just that paid source. It takes effect on the next request, with no restart: Find
leads says "Searching is paused by the administrator" and refuses to start, and a
search already running stops at its next check (below). Each switch shows who
turned it on (the name under **Your name**) and when. **Turn off** undoes it. The
switches are kept in the database (the additive `switches` table).

**In Render (the backup, if the site itself won't load):**

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
4. To switch it back on, delete the variable (or set it to `0`) and save. While
   the variable is set, the site's switch shows "On, set in the server's
   settings" and can't be turned off there.

## Deploying

A push to `main` deploys automatically, but only once GitHub's CI checks have
passed on it: `render.yaml` sets `autoDeployTrigger: checksPass` (in Render:
**Settings** > **Build & Deploy** > **Auto-Deploy** = **After CI Checks Pass**).
A commit whose lint or tests fail is never deployed. A deploy takes about 5
minutes. To see which version is live, open
`https://compactor-lead-finder.onrender.com/healthz`: `version` is the first
seven characters of the deployed commit (`git log -1 --format=%h`). Every 15
minutes GitHub Actions' **Live site** workflow (`.github/workflows/live.yml`, also
runnable by hand from the Actions tab) checks that the latest commit on `main`
that passed CI is live, and fails, which emails the owner, if it still isn't
20 minutes after its checks passed (a deploy takes about 5; set a repository
variable `LIVE_URL` if the address changes).

Protect `main` so that nothing reaches it without green checks (a one-time
setting only the repository owner can make): GitHub → **Settings** →
**Branches** → **Add branch protection rule** for `main` → **Require status
checks to pass before merging**, and pick `lint`, `test (sqlite)` and
`test (postgres)`. **Status (2026-09-30): not confirmed as set.** Whoever
sets it should change this line to say so, with the date. Until then Render still
deploys only green commits, but a red commit can land on `main`.

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

## Logs, and rolling back a bad deploy

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

## Problems: the webhook

So that nobody has to watch the Logs tab, every failed or incomplete search and
every error the site logs is also recorded in the database (the `problems`
table; the Find leads page lists the last 7 days under the search history) and,
when `LEADGEN_ALERT_WEBHOOK` is set, sent as a one-line message such as
"Arco Compactor Lead Finder: Today's search failed: Couldn't reach the map data
service (OpenStreetMap), so no leads were found. (Sep 29, 2026, 5:48 PM, Utah time)".
To set it up:

1. Create an incoming webhook where the person who looks after the site will
   see it: in Slack, **Apps → Incoming Webhooks**; in Microsoft Teams, a
   channel's **Workflows → Post to a channel when a webhook request is
   received**; in Google Chat, a space's **Apps & integrations → Webhooks**; in
   Discord, **Channel settings → Integrations → Webhooks**. Copy its https address.
2. In Render, **Environment**, add `LEADGEN_ALERT_WEBHOOK` with that address and
   **Save Changes**.

The same problem is sent at most once every 15 minutes; messages hold plain
words and error class names, never request data or settings. Reporting runs in
the background and never slows or breaks a page; if the webhook can't be
reached, that is only logged.

## Settings (environment variables)

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
| `LEADGEN_ALERT_WEBHOOK` | No (recommended on the live site) | none | An https incoming-webhook address (Slack, Microsoft Teams, Google Chat or Discord) that gets a message for every failed or incomplete search and server error; see "Problems: the webhook" above |
| `LEADGEN_SUPPORT_CONTACT` | No | "the person who manages the Lead Finder" | Who the login page tells people to ask for access, e.g. `Matt at (801) 555-0100` |
| `LEADGEN_ALLOWED_HOSTS` | No | `localhost` | Without a password, extra host names the page answers on (comma separated) |
| `LEADGEN_CACHE_DIR` | No | `.cache` | Folder for the API response cache and the local SQLite database |
| `RENDER`, `RENDER_GIT_COMMIT`, `PORT` | Set by Render | | Render's own; `/healthz` shows the deployed commit |
| `PYTHON_VERSION` | Render | 3.11.9 (`render.yaml`) | Python version Render builds with |
| `LEADGEN_TEST_DATABASE_URL` | Tests only | none | Runs the tests on a throwaway Postgres instead of SQLite (its tables are emptied) |
| `LEADGEN_CHROMIUM` | Tests only | Playwright's Chromium | A Chromium binary for the browser tests |

`1`, `true`, `yes` and `on` all switch a flag on; removing the variable (or
setting it to anything else) switches it off.

## Put it online (Render)

The repo includes `render.yaml`, so Render can set everything up:

1. Sign in at https://render.com with GitHub.
2. **New** > **Blueprint**, pick this repo, and click **Apply**.
3. When asked, set **APP_USERNAME** and **APP_PASSWORD** (the login page asks for them; both are case-sensitive) and, optionally, **GOOGLE_PLACES_API_KEY** and/or **YELP_API_KEY**.
4. Open the `onrender.com` link Render shows.

## The size of a clone

A clone of the repository is about 6 MB bigger than the code: an unrelated
Python package file (a `pglast` wheel) was committed by mistake early on and
removed in commit 77589dd, but it stays in the git history. Removing it from the
history would rewrite every commit and need a force-push, which would break
everyone's clones and the deploy link, so it is left there; it has no effect on
the running site. `*.whl` files are now ignored so it can't happen again. Purge
it only if the owner agrees to rewrite the history.

## The database (saved leads, marks, calls, the Yelp count)

Render's disk is wiped on every redeploy, so the site keeps its data in a
Postgres database named by `DATABASE_URL`. A free Neon database works and does
not expire (Render's free Postgres is deleted after 30 days):

1. Sign up at https://neon.tech (GitHub login works) and create a project
   (region US West (Oregon), near Render's servers).
2. Click **Connect** and copy the connection string (`postgresql://...`).
3. In Render, open the service, **Environment**, add `DATABASE_URL` with that
   string, and save (the service restarts).

The tables are created on first use, and new ones are added the same way on
the next start (never by changing or dropping an existing table): for example
`made_by`, which holds the name set under "Your name" for each Yes / No click
and call, and `switches`, which holds the emergency switches flipped on the site. Without `DATABASE_URL` on Render, searches
still run, but nothing is saved and Yelp is paused (its daily limit could not be
kept). Off Render, a SQLite file in `.cache/` is used instead.

**Saved leads.** Every search merges into one saved list: a business found again
(the same listing, or the duplicate rules in the [developer overview](developer-overview.md)) updates its row instead of
adding one. The page shows the saved list when it opens, and the downloads
contain all of it. Everything is kept, including Yelp's details, although
Yelp's terms allow keeping its data for 24 hours (and Google's for 30 days);
`SAVED_SOURCE_KEEP_SECONDS` in `leadgen/config.py` drops a source's details
after a set time instead, keeping the business's id so its mark comes back.

## Login and the free plan

Always set `APP_PASSWORD` on a public site: every search can spend your API keys.
A login lasts 30 days on a device; changing the username or password logs everyone
out. After 10 wrong passwords from one address, logins from it pause for 15 minutes.
Without a password the page only answers on `localhost` or an IP address; to use
another hostname, list it in `LEADGEN_ALLOWED_HOSTS`.
The free plan sleeps after 15 idle minutes, so the first visit takes about a minute
to wake up, and its disk is wiped on each restart (which is why data lives in the database).
To run the production server yourself: `gunicorn wsgi:app --workers 1 --threads 8 --timeout 0`.

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

## Cost notes (Google)

With the default `--grid 1`, a run makes at most about 100 requests (about 33
phrases × up to 3 pages). The command prints the maximum before it starts, and
`--max-requests` caps it. Google's pricing for Text Search
with phone/website fields falls under the Enterprise SKU (roughly $35 per 1,000
requests after the monthly free allowance), so a default run costs a few
dollars at most, and cached re-runs are free. `--grid 7` or `--grid 19` multiplies
requests by 7 or 19 (up to about 700 or 1,900), so set `--max-requests` if cost matters.

---

© Wright AI Solutions. Back to the [README](../README.md).
