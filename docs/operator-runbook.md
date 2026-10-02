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
- [Owner actions still open](#owner-actions-still-open)

## Owner actions still open

Three settings only the owner (Thomas) can make, on GitHub and on Render; the code
can't make them, and nothing else is waiting on them. Each takes a few minutes. When
one is done, change its **Open** to **Done (the date)** here and in the section it
links to.

1. **Protect `main`** (GitHub; **Open**, checked 2026-10-02). So a commit with a
   failing check can't land on `main`. Steps: [Deploying → Protect `main`](#deploying)
   (a ruleset that requires the checks `lint`, `test (sqlite)` and `test (postgres)`).
   *Check:* `gh api repos/thomaswright03/Compactor-Lead-List-Generator/branches/main --jq .protected`
   prints `true` (or the repository's **Branches** page shows `main` as protected).
2. **Name a real contact on the login page** (Render; **Open**, checked 2026-10-02).
   So a salesperson who forgot the password, or is locked out, knows whom to call. In
   Render open the **compactor-lead-finder** service → **Environment** → **Add
   Environment Variable**: key `LEADGEN_SUPPORT_CONTACT`, value a name with a phone
   number or email, such as `Jane Doe at (801) 555-0100 or jane@example.com` → **Save
   Changes** (the site restarts in about a minute). Set it in Render only, never in
   this repository. Until it is set, every start of the site logs "LEADGEN_SUPPORT_CONTACT
   is not set" (Render → **Logs**). *Check:*
   `curl -s https://compactor-lead-finder.onrender.com/login | grep -oE 'href="(tel|mailto):[^"]+"'`
   prints the contact's `tel:` or `mailto:` link (nothing printed: not set yet). More
   in [Login and the free plan](#login-and-the-free-plan).
3. **Delete the old branch `wip-yelp-cap-and-baler-marks`** (GitHub; **Open**, checked
   2026-10-02). Everything on it reached `main` long ago; it only confuses whoever reads
   the repository next. Steps, with an optional archive tag that keeps its commit:
   [Old branches](#the-size-of-a-clone). *Check:* `git ls-remote --heads origin` lists
   only `refs/heads/main`.

## Emergency switches: stop searches or paid calls

If searches misbehave (unexpected Google charges, bad data going into the saved
list), whoever looks after the site can stop them without a code change.

**On the site (first choice, works at once):** open **Find leads**, then **For the
site administrator** at the bottom (closed until opened, and locked: enter the
administrator password, `ADMIN_PASSWORD`, and press **Unlock**; it stays unlocked on
that device until you log out or press **Lock this section**; it also lists the problems
of the last 7 days). If the password isn't at hand, the `LEADGEN_SEARCH_PAUSED`
setting below does the same. Each row says how things stand ("Searching: Working
normally", "Yelp: In use"). **Pause searching**, then confirm with **Pause
searching** in the box that asks first ("Pause all searching for everyone?"), stops all searching;
**Stop using Google** / **Stop using Yelp** (shown when that source is set up) stop
just that paid source. It takes effect on the next request, with no restart: Find
leads says "Searching is paused by the administrator" and refuses to start, and a
search already running stops within a few seconds, wherever it is (between sources,
before every paid Google or Yelp call, and every 2 seconds during the free map data
step: no more map requests are sent). It keeps and saves the businesses it had already
found; its progress card says "Stopping: the administrator paused searching", its
result and the search history say "Stopped by the administrator", and today's search
stays used (one a day). The background filling in of map areas a search missed stops
within a few seconds as well (no more map requests), and Find leads then says "Filling in
the missing map areas stopped because searching was paused". A search stopped before it found anything gives the day back
and is listed as "Stopped by the administrator", not as failed, and not under Recent
problems or on the webhook (a deliberate stop is not a problem). If the switches can't
be read while a search runs (the database stopped answering), a switch last seen on
still counts as on, and no further paid Google or Yelp call is made (the free map data
carries on); the search's notes say the switches couldn't be checked. Each switch shows who
turned it on (the name under **Your name**) and when. **Resume searching** / **Use
Google again** / **Use Yelp again** undoes it. The
switches are kept in the database (the additive `switches` table).

**In Render (the backup, if the site itself won't load):**

1. In Render, open the **compactor-lead-finder** service and click **Environment**.
2. Add `LEADGEN_SEARCH_PAUSED` with the value `1` to stop all searching, or
   `LEADGEN_GOOGLE_OFF` = `1` / `LEADGEN_YELP_OFF` = `1` to stop just that
   paid source (the free map data and everything else keep working).
3. Click **Save Changes**. The service restarts in about a minute; from then
   on Find leads says "Searching is paused by the administrator" and refuses
   to start. Leads, Calls, Stats and the downloads keep working. A search that
   is already running checks the switches between sources, before every
   paid Google or Yelp call and every 2 seconds during the free map data: it
   stops there, keeps (and saves) what it found, and says it was stopped by the
   administrator.
4. To switch it back on, delete the variable (or set it to `0`) and save. While
   the variable is set, the site's switch shows "On, set in the server's
   settings" and can't be turned off there.

## Deploying

**Automatic, and only for green commits (since 2026-09-30).** Render's GitHub app
has access to this repository, so Render hears about every push to `main`. Its
Auto-Deploy is set to **After CI Checks Pass** (Render **Settings** > **Deploy** >
**Auto-Deploy**, and `autoDeployTrigger: checksPass` in `render.yaml`): a push goes
live only once the three GitHub checks `lint`, `test (sqlite)` and
`test (postgres)` are green. A commit whose checks fail is never deployed; the
site keeps running the last green commit until a fix is pushed. (Before
2026-09-30 Render could only clone the public repository when someone pressed
Manual Deploy, and its deploy log said "It looks like we don't have access to your
repo"; if that line comes back, give the Render app access again on GitHub:
Settings > Applications > Render > Configure > Repository access.)

If the dashboard ever shows **On Commit** instead, switch it back to **After CI
Checks Pass**: On Commit deploys every push straight away, red or not.

After a push:

1. Wait for the CI checks on the commit (GitHub **Actions** tab, about 5 minutes),
   then for Render's deploy (**Events**, about 5 minutes more). If a check is red,
   nothing is deployed: fix it and push again.
2. Check the version: open
   `https://compactor-lead-finder.onrender.com/healthz`; `version` must be the
   first seven characters of the commit (`git log -1 --format=%h`). If it isn't,
   **Events** in Render shows whether the deploy failed (its log says why), is
   waiting for the checks, or never started; **Manual Deploy** > **Deploy latest
   commit** still works as a fallback (it skips the check wait, so use it only for
   a green commit).

**Protect `main`** so that a red commit can't even land on it. This is a one-time
setting on GitHub that only someone with admin access to the repository (the owner,
Thomas) can make; the code can't set it.

1. Sign in to GitHub as the owner and open
   `https://github.com/thomaswright03/Compactor-Lead-List-Generator/settings/rules`
   (the repository → **Settings** → **Rules** → **Rulesets** in the left column).
2. Click **New ruleset** → **New branch ruleset**.
3. **Ruleset name**: `Protect main`. **Enforcement status**: **Active**. Leave the
   **Bypass list** empty, so nobody skips the checks.
4. Under **Target branches**, click **Add target** → **Include default branch**
   (that is `main`).
5. Under **Branch rules**, keep **Restrict deletions** and **Block force pushes**
   ticked, and tick **Require status checks to pass**. Click **Add checks** and add,
   one at a time, `lint`, `test (sqlite)` and `test (postgres)` (type each name and
   pick it from the list; GitHub offers a check once it has run on a commit, which
   all three have). Leave **Require branches to be up to date before merging** off.
6. Click **Create** at the bottom.

Older GitHub pages offer the classic rule instead: **Settings** → **Branches** →
**Add classic branch protection rule**, **Branch name pattern** `main`, tick
**Require status checks to pass before merging** and add the same three checks, tick
**Do not allow bypassing the above settings**, leave **Allow force pushes** and
**Allow deletions** off, and click **Create**. Either one does the job; set only one.

**Afterwards**, a commit reaches `main` only once its three checks have passed on
another branch: push the work to a branch (`git push origin HEAD:fix-something`),
wait for the checks on the **Actions** tab, then open a pull request and click
**Merge**, or move `main` up to that commit with `git push origin fix-something:main`.
A plain `git push origin main` of a commit that hasn't been checked is refused
("Required status checks ... are expected"); a pull request whose checks fail shows
the **Merge** button greyed out. Render's deploy rule (above) stays as it is.

**To check it is on**: the repository's **Settings** → **Rules** → **Rulesets** lists
`Protect main` as Active (or **Settings** → **Branches** lists the classic rule for
`main`), and `https://github.com/thomaswright03/Compactor-Lead-List-Generator/branches`
shows `main` with a shield or "protected" mark.

**Status (2026-10-02): not set yet** (GitHub still reports `main` as not
protected). Whoever sets it should change this line to say so, with the date.
Until then a red commit can land on `main`, but Render won't deploy it.

The Python packages are pinned to exact versions in `requirements.txt`, so a
deploy never picks up a new Flask or psycopg by surprise (the Excel files are
written by the site itself, `leadgen/xlsx.py`; openpyxl is only used by the tests). To upgrade
one, change its version there, run the tests, and push.

## Monthly: the scoring check

Once a month the owner (Thomas) opens Find leads > **For the site administrator** >
**Scoring check**, downloads the scoring check file and sends it to whoever looks
after the code. It holds the businesses marked Yes or No (names and categories only,
no phone numbers, addresses or notes). They replace
`tests/fixtures/scoring_reference.json` with it, run the tests and commit it: from
then on a scoring change that pushes a business marked Yes to a lower tier (or one
marked No to a higher tier) fails the tests. The command
`python -m leadgen reference` (with `DATABASE_URL` set) does the same from a
computer. If the Stats page shows tier A's Yes share not above tier C's, ask for the
weights to be looked at.

Write each run down here (the Stats page's tier table gives the shares), so it is
clear whether the ranking works for Arco's market. The first run needs about 50
businesses marked Yes or No on the live site.

| Date | Marked Yes / No | Tier A Yes share | Tier C Yes share | Weights changed? |
|---|---|---|---|---|
| 2026-09-30 | Not run: the developer has no access to the live site's marks; the owner's first download is still needed | - | - | No |

## The one-search-a-day rule

Find leads runs once per Utah calendar day, as the owner asked. A search that
came back incomplete (a source failed, or map areas never answered even after the
automatic retries) saves what it found, says which source was missing, and uses up
the day like a complete one. Map areas the free map servers missed are then asked
again in the background for up to an hour, within that same search (not a second
one): Find leads shows "Still filling in N areas" with the towns they hold
("around Kaysville and Centerville"), then "Complete" or "N areas never answered" (that last one is also reported under Recent problems and to the
webhook). Pausing searching stops the filling in too, within a few seconds, and so does the next day's search
starting (the history then says "stopped because the next day's search started"): a
filling in that runs past midnight stays on Find leads until it ends. A restart or deploy during
it cuts it short (the history then says so). Only a search that failed outright (an unknown place,
nothing found, the leads couldn't be saved) or was cut off by a restart gives the day back.

**A deploy or restart during a search.** Deploys restart the service, and a search
running then stops with it. What it had found up to then (written to the database
every 10 seconds while it runs: the `search_runs` and `search_found` tables, removed
again when a search ends) is saved as soon as someone opens Find leads or starts a
search: at once when the old server shut down normally, or 45 seconds after it was
killed. The day's search is given back straight away, and the history row (marked
**Interrupted**) says how many businesses were saved. Nothing is searched again by
itself; the person can run the day's search again. To avoid it altogether, push
changes outside working hours or when nobody is searching.

Decision record (2026-09-30): the same-day re-run after an incomplete search, which
an earlier version allowed once, was never approved by the owner, so it was removed
(`INCOMPLETE_RERUNS = 0` in `leadgen/daily.py`). If the owner ever wants it back,
set it to 1 (the code and tests for it remain) and record the owner's decision
here with the date.

## Logs, and rolling back a bad deploy

The site logs to Render's **Logs** tab: every failed search, database error and
unexpected error, with the technical detail the pages leave out. A request to
Google, Yelp or a map server that fails is one line naming the server, the status,
how long it took and a short reason (an error page's title, not its HTML), e.g.
`OpenStreetMap server failed: overpass-api.de/api/interpreter returned HTTP 504
after 30.2s: 504 Gateway Time-out`; search the Logs tab for `returned HTTP` or the
server's name. A request that is tried again logs `...; trying again in 2 s`.

To go back to the previous version:

1. In Render, open the service and click **Events** (or **Deploys**).
2. Find the last deploy that worked, open its menu and choose **Rollback**
   (or **Redeploy** on older dashboards). Render builds and starts that commit.
3. If Auto-Deploy is on (service **Settings**), turn it off until the fix is on
   `main`, or the next push deploys again; then fix forward with a new commit
   (`git revert <bad commit>` and push), deploy it (see Deploying) and turn
   Auto-Deploy back on.

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
| `ADMIN_PASSWORD` | On a public site | none (the administrator's section stays locked; open on a local copy with no login) | Unlocks **For the site administrator** on Find leads (problems, site switches, scoring check). Changing it locks the section again for everyone |
| `DATABASE_URL` | On Render | SQLite file in the cache folder | The permanent Postgres database (saved leads, marks, calls, searches, the Yelp count) |
| `GOOGLE_PLACES_API_KEY` | No | none | Google Places key (paid; best phones and websites) |
| `YELP_API_KEY` | No | none | Yelp key (at most 50 calls in any 24 hours) |
| `SECRET_KEY` | No | derived from the login | Signs the login cookie. Changing it (or the username or password) logs everyone out |
| `LEADGEN_SEARCH_PAUSED` | No | off | Emergency stop: `1` makes Find leads refuse to start (see below) |
| `LEADGEN_GOOGLE_OFF` | No | off | `1` stops every Google call (no Google charges); searches use the other sources |
| `LEADGEN_YELP_OFF` | No | off | `1` stops every Yelp call; searches use the other sources |
| `LEADGEN_ALERT_WEBHOOK` | No (recommended on the live site) | none | An https incoming-webhook address (Slack, Microsoft Teams, Google Chat or Discord) that gets a message for every failed or incomplete search and server error; see "Problems: the webhook" above |
| `LEADGEN_SUPPORT_CONTACT` | Yes on the live site (see "Login and the free plan") | "Ask the person who gave you your login, or Wright AI Solutions." | Whom the login page names for access or a forgotten password: a name plus a phone number or email, e.g. `Jane Doe at (801) 555-0100 or jane@example.com` (the number and email become tap-to-call and email links) |
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
3. When asked, set **APP_USERNAME** and **APP_PASSWORD** (the login page asks for them; both are case-sensitive), **LEADGEN_SUPPORT_CONTACT** (whom the login page names for login help) and, optionally, **GOOGLE_PLACES_API_KEY** and/or **YELP_API_KEY**.
4. Open the `onrender.com` link Render shows.

## The size of a clone

A clone of the repository is about 6 MB bigger than the code: an unrelated
Python package file (a `pglast` wheel) was committed by mistake early on and
removed in commit 77589dd, but it stays in the git history. Removing it from the
history would rewrite every commit and need a force-push, which would break
everyone's clones and the deploy link, so it is left there; it has no effect on
the running site. `*.whl` files are now ignored so it can't happen again. Purge
it only if the owner agrees to rewrite the history.

**Old branches.** The remote branch `wip-yelp-cap-and-baler-marks` (last commit
20eb261, 2026-09-29) is the first draft of the 50-a-day Yelp cap, the Yes / No marks
and the Postgres database. That work reached `main` as commit 54de1e5 ("Save leads
and baler marks permanently; cap the website at 50 Yelp calls a day") and has been
built on ever since, so nothing on the branch is missing from `main` and nothing
deploys from it. Checked 2026-10-02; it is safe to delete. To delete it (the owner,
once): GitHub → the repository → **Branches** → the bin icon next to it, or

```bash
git push origin 20eb26110c8537af04bd3e68420251e18a50696d:refs/tags/archive/wip-yelp-cap-and-baler-marks  # optional: keep a tag
git push origin --delete wip-yelp-cap-and-baler-marks
git ls-remote --heads origin        # check: lists only refs/heads/main
```

Then mark it **Done** under [Owner actions still open](#owner-actions-still-open)
and remove this paragraph. `main` is the only branch the site needs.

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
and call, `switches`, which holds the emergency switches flipped on the site, and
`merged_leads`, which records each saved lead merged into another of the same site
(its id, the lead it joined, when, and the row as it was, with its mark). Without `DATABASE_URL` on Render, searches
still run, but nothing is saved and Yelp is paused (its daily limit could not be
kept). Off Render, a SQLite file in `.cache/` is used instead.

The site keeps up to 8 database connections open between page loads (one per web
server thread, `POOL_SIZE` in `leadgen/store.py`, matching `--threads 8` in
`render.yaml`), so a click doesn't wait for a new connection to Neon. Each one is
checked with a quick `SELECT 1` before it is used, and one idle for 4 minutes is
closed (Neon drops idle connections itself). When a check fails, the database has
dropped its connections (a restart or network reset), so all the kept ones are
closed at once: a blip costs one reconnect, not a run of "Can't reach your saved
leads" errors. Change the pool size and thread count together
if the thread count ever changes. The tables are checked and created once each time
the site starts.

**Saved leads.** Every search merges into one saved list: a business found again
(the same listing, or the duplicate rules in the [developer overview](developer-overview.md)) updates its row instead of
adding one. The buildings of one site (an apartment complex's numbered buildings,
a campus's parts, one name spread over up to half a mile, a listing inside the map
outline of a same-named one, an air base's or airport's parts up to 1.5 miles apart,
a name that only adds its town) are one lead, and a later search's part joins the
saved site. Rows saved as separate leads before these rules are left alone by
searches, because joining them moves calls and Yes / No clicks; a search that finds
listings of two such rows updates the one saved first. To join them, run
`python -m leadgen merge-sites` (with `DATABASE_URL` set): it lists each business
saved as more than one row (the row each group would become first, with its town,
the day it was saved, its mark and its calls) and changes nothing. Once the owner
agrees with the list, run `python -m leadgen merge-sites --apply`: each group becomes the row saved first,
keeping every source listing, moving the calls and Yes / No clicks to it and keeping
the latest mark (the earlier ones stay in its history), except that when the group's
marks disagree (some Yes, some No) it keeps Yes and the lead says the marks
disagreed until a salesperson presses Yes or No on it again. Nothing is deleted: the
merged rows stay in the table, hidden, and are recorded in `merged_leads`. The page shows the saved list when it opens, and the downloads
contain all of it. Everything is kept, including Yelp's details, although
Yelp's terms allow keeping its data for 24 hours (and Google's for 30 days);
`SAVED_SOURCE_KEEP_SECONDS` in `leadgen/config.py` drops a source's details
after a set time instead, keeping the business's id so its mark comes back.

**Leads from a search around the wrong place.** Since 2026-10-02 a search only
starts around a place more than 30 miles from Arco's shop (`SERVICE_AREA_MILES` in
`leadgen/config.py`) after a second, explicit confirmation that names the place and
its distance, and a bare town name ("Murray") means the Utah one. Leads saved by an
earlier mistaken search (e.g. a search for "x" that ran around San Francisco) stay
in the list until the owner decides; searches and the site never remove leads. With
`DATABASE_URL` set (Render > the service > **Shell**):

```bash
python -m leadgen out-of-area                 # list leads more than 60 miles from Arco (changes nothing)
python -m leadgen out-of-area --miles 100     # another distance
python -m leadgen out-of-area --remove        # take them out of the saved list
python -m leadgen out-of-area --restore       # put every removed lead back
```

`--remove` never touches a lead someone marked Yes / No or logged a call for (it
is listed as kept). Each removed row is kept, as it was, in the additive
`removed_leads` table, so nothing is lost and `--restore` brings it back.

## Login and the free plan

Always set `APP_PASSWORD` on a public site: every search can spend your API keys.
Also set `LEADGEN_SUPPORT_CONTACT` to the person who hands out the login, with a
phone number or email (Render → the service → **Environment** → **Add Environment
Variable**, then **Save Changes**): the login page's "Need access or forgot the
password?" line names them, and a salesperson locked out first thing in the morning
can call or email straight from it. Without it the line says "Ask the person who
gave you your login, or Wright AI Solutions." The contact details belong in Render
only, never in this repository. **Status (2026-10-02): not set on the live site**
(the owner, Thomas, chooses the contact and sets it); change this line when it is.
Until then every start of the site logs "LEADGEN_SUPPORT_CONTACT is not set". To check
it from anywhere: `curl -s https://compactor-lead-finder.onrender.com/login | grep -oE
'href="(tel|mailto):[^"]+"'` prints the contact's link once it is set.
A login lasts 30 days on a device; changing the username or password logs everyone
out. After 10 wrong passwords from one address, logins from it pause for 15 minutes.
Without a password the page only answers on `localhost` or an IP address; to use
another hostname, list it in `LEADGEN_ALLOWED_HOSTS`.
The free plan sleeps after 15 idle minutes, so the first visit takes 20 to 60 seconds
to wake up, and its disk is wiped on each restart (which is why data lives in the database).
The GitHub Actions job `.github/workflows/keep-awake.yml` opens `/healthz` every 10
minutes from 6 AM to 9 PM Utah time so the site stays awake while the team works
(GitHub may run it a few minutes late, and turns scheduled jobs off after 60 days
with no commits: re-enable it under Actions if so). Render's paid Starter plan never
sleeps and doesn't need it.
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
