# Lead Finder: operator runbook

For whoever looks after the live site (Render + a Postgres database). Sales
staff want the [sales guide](sales-guide.md); developers the
[developer overview](developer-overview.md).

## Common actions

| To | Do this | Details |
| --- | --- | --- |
| **Stop all searching right now** | Site: **Find leads** → **For the site administrator** (bottom) → **Unlock** → **Pause searching** → confirm. Site won't load: Render → the service → **Environment** → `LEADGEN_SEARCH_PAUSED` = `1` → **Save Changes** | [Emergency switches](#emergency-switches-stop-searches-or-paid-calls) |
| Stop Google or Yelp charges only | Same place: **Stop using Google** / **Stop using Yelp** (or `LEADGEN_GOOGLE_OFF` / `LEADGEN_YELP_OFF` = `1` in Render) | [Emergency switches](#emergency-switches-stop-searches-or-paid-calls) |
| Undo a bad deploy | Render → the service → **Events** → the last good deploy → **Rollback** | [Logs and rollback](#logs-and-rolling-back-a-bad-deploy) |
| See what went wrong | **For the site administrator** lists the last 7 days' problems; Render → **Logs** has the detail | [Logs](#logs-and-rolling-back-a-bad-deploy) · [Alerts](#problems-the-webhook) |
| Make a backup now | `python -m leadgen backup` with `DATABASE_URL` set, or GitHub → **Actions** → **Nightly backup** → **Run workflow** | [Backups](#backups-and-restoring) |
| Get lost data back | Neon → **Branches** → **New branch** from past data (last 6 hours), or `python -m leadgen restore <copy>` (lists what it would add), then the same with `--apply` (adds only, never overwrites) | [Restoring](#restoring) |
| Change the login password | Render → **Environment** → `APP_PASSWORD` → **Save Changes** (everyone logs in again) | [Login](#login-and-the-free-plan) |
| Keep the site awake while the team works | cron-job.org (or UptimeRobot) opens `https://compactor-lead-finder.onrender.com/healthz` every 5 minutes, 5 AM to 9 PM Utah time; **For the site administrator** says "The site had gone to sleep" when it missed | [Keep the site awake](#keep-the-site-awake) |
| Name the login page's help contact | Render → **Environment** → `LEADGEN_SUPPORT_CONTACT` = a name with a phone or email | [Login](#login-and-the-free-plan) |
| Merge a business saved as two rows | `python -m leadgen merge-sites` (lists, changes nothing), then `--apply` | [The database](#the-database-saved-leads-marks-calls-the-yelp-count) |
| Send the monthly scoring check | **For the site administrator** → **Scoring check** → download, send to the developer | [Monthly](#monthly-the-scoring-check) |

All sections:

- [Emergency switches](#emergency-switches-stop-searches-or-paid-calls)
- [Deploying](#deploying) · [Logs and rollback](#logs-and-rolling-back-a-bad-deploy)
- [Alerts: the webhook](#problems-the-webhook)
- [Settings](#settings-environment-variables) · [Put it online and the database](#put-it-online-render)
- [Backups and restoring](#backups-and-restoring)
- [Login](#login-and-the-free-plan) · [Keep the site awake](#keep-the-site-awake) · [Yelp limits](#yelp-notes) · [Google costs](#cost-notes-google)
- [Owner actions still open](#owner-actions-still-open)

## Owner actions still open

Steps only the owner (Thomas) can take, on GitHub, on Render and with the live data;
the code can't take them, and nothing else is waiting on them. Each takes a few minutes. When
one is done, change its **Open** to **Done (the date)** here and in the section it
links to, and take it off the README's "Still open for the owner" list.

1. **Protect `main`** (GitHub; **Done (2026-10-03)**: ruleset `Protect main`). So a commit with a
   failing check can't land on `main`. Steps: [Deploying → Protect `main`](#deploying)
   (a ruleset that requires the checks `lint`, `test (sqlite)` and `test (postgres)`).
   *Check:* `gh api repos/thomaswright03/Compactor-Lead-List-Generator/branches/main --jq .protected`
   prints `true` (or the repository's **Branches** page shows `main` as protected).
2. **Name a real contact on the login page** (Render; **Open**, checked 2026-10-03).
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
3. **Delete the three old branches** (GitHub; **Open**, checked 2026-10-03):
   `wip-yelp-cap-and-baler-marks`, `runbook-owner-steps-done` and `claude-staging`.
   Nothing on them is missing from `main` and nothing deploys from them; they only make
   whoever reads the repository next wonder whether work is unmerged. Steps, with an
   optional archive tag: [Old branches](#the-size-of-a-clone). *Check:*
   `git ls-remote --heads origin` lists only `refs/heads/main` (plus a branch with an
   open pull request, if any).
4. **Turn on the nightly backup** (GitHub; **Done (2026-10-03)**: first run green). So a copy older
   than Neon's 6-hour history exists. On GitHub open the repository → **Settings** →
   **Secrets and variables** → **Actions** → **New repository secret**, twice:
   `BACKUP_DATABASE_URL` = the Neon connection string (the same as Render's
   `DATABASE_URL`), and `BACKUP_PASSPHRASE` = a long passphrase you keep in your password
   manager (without it the copies can't be read, by anyone). Then **Actions** →
   **Nightly backup** → **Run workflow**. *Check:* the run is green and has an artifact
   `leadgen-backup-<date>`. Details: [Backups and restoring](#backups-and-restoring).
5. **Set up the outside keep-awake pinger** (cron-job.org; **Open**, added 2026-10-03).
   So nobody waits up to a minute for the site to wake up during the working day:
   GitHub ran the keep-awake job only every 3 to 5 hours, and Render's free plan sleeps
   after 15 idle minutes. Steps: [Keep the site awake](#keep-the-site-awake) (a free
   cron-job.org job that opens `/healthz` every 5 minutes from 5 AM to 9 PM Utah time).
   *Check:* the job's **History** on cron-job.org lists a 200 answer every 5 minutes, and
   `curl -s https://compactor-lead-finder.onrender.com/healthz` shows the same `up_since`
   at 7 AM and at 5 PM.
6. **Rehearse a restore of the live data once** (your computer; **Open**, added
   2026-10-03). Download a nightly copy (or make one: `python -m leadgen backup`), then
   restore it into a local SQLite file with no `DATABASE_URL` set (`python -m leadgen
   restore <file> --apply`), run `python -m leadgen web` and compare Leads, Calls and Stats
   with the live site. *Check:* the counts match; write the date in the rehearsals table
   under [Backups and restoring](#backups-and-restoring).

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
within a few seconds as well (no more map requests), and Find leads then says "Covered:
… Filling in stopped because searching was paused, so these weren't searched today: …". A search stopped before it found anything gives the day back
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

**Status: on since 2026-10-03.** The ruleset `Protect main` requires `lint`,
`test (sqlite)` and `test (postgres)`, and blocks deleting `main` and force pushes;
GitHub reports `main` as protected.

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
clear whether the ranking works for AARCO's market. The first run needs about 50
businesses marked Yes or No on the live site.

| Date | Marked Yes / No | Tier A Yes share | Tier C Yes share | Weights changed? |
|---|---|---|---|---|
| 2026-09-30 | Not run: the developer has no access to the live site's marks; the owner's first download is still needed | - | - | No |

## The one-search-a-day rule

Find leads runs once per Utah calendar day, as the owner asked. A search that
came back incomplete (a source failed, or parts of the free map data never came in
even after the automatic retries) saves what it found and uses up the day like a
complete one. A source that couldn't be reached at all (Google, say) is named in the
note beside the result.

Parts of the area the free map servers missed are then filled in in the background
for up to an hour, within that same search (not a second one). Find leads says how
much of the area was covered in **one** status line, for example "Covered so far:
about 6 of the 9 parts of the 30-mile area. Still filling in, in the background until
about 1:36 PM: Kaysville and Centerville. The 418 businesses already found are on the
Leads page, ready to call now; ...". It ends as "Covered: the whole 30-mile area", or
names the towns that didn't come in today (that one is also reported under Recent
problems and to the webhook). The search history's Details say it in one line ("Free
map data: area covered"). No other note repeats it, and internal counts (how many
parts were asked again) are not shown. The words are worked out from the record's
numbers each time it is shown, so searches saved by earlier versions read the same
way and no saved record is rewritten.

Pausing searching stops the filling in too, within a few seconds (the line says
"Stopping the filling in ...", then which towns weren't searched today), and so does
the next day's search starting: a filling in that runs past midnight stays on Find
leads until it ends. A restart or deploy during it cuts it short (the line then says
so). Only a search that failed outright (an unknown place, nothing found, the leads
couldn't be saved) or was cut off by a restart gives the day back.

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
an earlier version allowed once, was never approved by the owner, so it was switched
off; on 2026-10-03 its code was removed as well (it could not run). Bringing it back
would be new work, after the owner's decision is recorded here with the date.

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
"AARCO Compactor Lead Finder: Today's search failed: Couldn't reach the map data
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

**Old branches** (checked 2026-10-03; all three are safe to delete, by the owner):

- `wip-yelp-cap-and-baler-marks` (last commit 20eb261, 2026-09-29): the first draft of
  the 50-a-day Yelp cap, the Yes / No marks and the Postgres database. That work
  reached `main` as commit 54de1e5 ("Save leads and baler marks permanently; cap the
  website at 50 Yelp calls a day") and has been built on ever since.
- `runbook-owner-steps-done` (7692522, 2026-10-03, "Runbook: main is protected and the
  nightly backup is on"): that commit is already part of `main`.
- `claude-staging`: where Claude's review-round fixes are pushed so the three checks run
  before `main` moves up to them; once a round is finished it points at a commit already
  on `main`. Delete it only when no review round is running (Claude's next round
  creates it again with its first push).

To delete them: GitHub → the repository → **Branches** → **All branches** → the bin
icon next to each of the three; or, on a computer with a clone:

```bash
git fetch origin
git merge-base --is-ancestor origin/runbook-owner-steps-done origin/main && echo on main  # prints: on main
git merge-base --is-ancestor origin/claude-staging origin/main && echo on main            # prints: on main
git push origin 20eb26110c8537af04bd3e68420251e18a50696d:refs/tags/archive/wip-yelp-cap-and-baler-marks  # optional: keep a tag
git push origin --delete wip-yelp-cap-and-baler-marks runbook-owner-steps-done claude-staging
git ls-remote --heads origin        # check: lists only refs/heads/main
```

If `claude-staging` doesn't print "on main", a round is still running: leave it and
delete it later. Then mark owner action 3 **Done** under
[Owner actions still open](#owner-actions-still-open) and shorten this paragraph.
`main` is the only branch the site needs.

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
(its id, the lead it joined, when, and the row as it was, with its mark), and
`lead_contacts`, which holds every verified phone and contact name the team saved
on a lead (who saved it and when; a later save adds a row, nothing is overwritten), and
`site_starts`, one row each time the site starts (when, and which version: see
[Keep the site awake](#keep-the-site-awake); not in backups). Without `DATABASE_URL` on Render, searches
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
a name that only adds its town, a named building inside its campus's outline, and a
building's outline named for part of the business beside it, such as "Cancer Hospital
South" beside "Huntsman Cancer Hospital") are one lead, and a later search's part joins the
saved site. After a release that adds such a rule, run the dry run below once: rows
saved before it show up there. Rows saved as separate leads before these rules are left alone by
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
contain all of it. Everything is kept for good, including Yelp's details, although
Yelp's terms allow keeping its data for 24 hours (and Google's for 30 days): the
owner's decision (2026-09-29). The switched-off setting that could drop a source's
details after a set time was removed on 2026-10-03; dropping them would now be new
work, after the owner decides.

**Leads from a search around the wrong place.** Since 2026-10-02 a search only
starts around a place more than 30 miles from AARCO's shop (`SERVICE_AREA_MILES` in
`leadgen/config.py`) after a second, explicit confirmation that names the place and
its distance, and a bare town name ("Murray") means the Utah one. Leads saved by an
earlier mistaken search (e.g. a search for "x" that ran around San Francisco) stay
in the list until the owner decides; searches and the site never remove leads. With
`DATABASE_URL` set (Render > the service > **Shell**):

```bash
python -m leadgen out-of-area                 # list leads more than 60 miles from AARCO (changes nothing)
python -m leadgen out-of-area --miles 100     # another distance
python -m leadgen out-of-area --remove        # take them out of the saved list
python -m leadgen out-of-area --restore       # put every removed lead back
```

`--remove` never touches a lead someone marked Yes / No or logged a call for (it
is listed as kept). Each removed row is kept, as it was, in the additive
`removed_leads` table, so nothing is lost and `--restore` brings it back.

**The Map page and the day's search records.** No table or column was added for the
Map page. A search now also writes, into its day's existing record (the `info` text of
`searches`), the point it ran around (`center`) and which parts of the free map data no
server answered for (`map_areas`), and the background filling in keeps its own list as
areas answer (`fill.boxes`). Records written before 2026-10-03 lack these keys and are
read from their words instead; nothing rewrites them. The page only reads.

**The Map page's outside parts.** The map itself (Leaflet 1.9.4) is part of the site,
in `leadgen/static/vendor/leaflet` with its licence (BSD-2) and a README saying where it
came from and how to update it, so it works without any other site. The street map
under the pins is OpenStreetMap's public tiles (`https://tile.openstreetmap.org`),
loaded by each person's browser and credited in the map's corner ("© OpenStreetMap
contributors", which their licence requires: keep it). No key or account is needed,
and the server never asks for tiles. If the tiles are blocked or OpenStreetMap is down,
the page says "The street map couldn't be loaded" and still draws AARCO's pin, the
area, the search areas and every business on a plain background. OpenStreetMap's tile
policy is for light use like a small team's; if the site ever needs heavy map use,
switch `TILES` and `TILE_CREDIT` at the top of `leadgen/static/map.js` to a paid tile
provider.

## Backups and restoring

Everything the sales team produces lives in the one Postgres database named by
`DATABASE_URL` (Neon): the saved list, every Yes / No mark and who made it, every call
with its Conversation Summary, the corrected phones and contact names, and the search
history. The Excel download is not a backup (it has only the latest call of each lead).
There are three copies, each reaching further back:

| Copy | How far back | Where it is | Who sets it up |
|---|---|---|---|
| Neon's history | The last **6 hours** on Neon's free plan (up to 7 days on Launch, 30 on Scale) | In Neon | Nothing to do; check it under **Settings** → **Postgres** → **History window** |
| Nightly copy | Every night for the last **90 days** | GitHub → **Actions** → **Nightly backup** → a run → **Artifacts** (encrypted) | The owner, once: add two secrets ([Owner actions](#owner-actions-still-open), item 4) |
| On-request copy | Whenever someone makes one (keep one a month for good) | A file on the owner's computer or private drive | Anyone with the code and `DATABASE_URL` |

**What a copy holds.** `python -m leadgen backup` writes every row of every table that
keeps the team's work or the site's records (all of `store.SCHEMA` except the cache of
map, Google and Yelp answers, a running search's checkpoints and the list of the site's
starts) to one file,
`leadgen-backup-<Utah date and time>.json.gz` (gzip-compressed JSON, read in one
transaction, so it is one moment's copy). It prints what it holds ("1,204 saved leads,
88 marked yes, 61 marked no, 240 calls, 34 searches"); keep that line with the file.
The file holds call notes and phone numbers: keep it private and never commit it
(`.gitignore` refuses `leadgen-backup-*`). An existing file is never replaced.

**Make a copy by hand** (before a risky change, `merge-sites --apply` or
`out-of-area --remove`, and once a month to keep): on a computer with the code
(`pip install -r requirements.txt`), with the Neon connection string from Neon →
**Connect** (the same as Render's `DATABASE_URL`):

```bash
DATABASE_URL='postgresql://...' python -m leadgen backup
# Windows (PowerShell): $env:DATABASE_URL='postgresql://...'; python -m leadgen backup
```

**The nightly copy** (`.github/workflows/backup.yml`) runs at about 3 AM Utah time, makes
the same copy, encrypts it with the owner's passphrase (AES-256, gpg; the repository is
public, so it is never stored unencrypted) and keeps it for 90 days as the run's artifact
`leadgen-backup-<date>`. It does nothing until the owner adds the two secrets; until then
each run says "The nightly backup is off". To get a copy: GitHub → **Actions** →
**Nightly backup** → the night's run → **Artifacts** → `leadgen-backup-<date>` (a zip
holding `leadgen-backup-<date>.json.gz.gpg`), then unzip it and decrypt it (GnuPG:
`brew install gnupg` on a Mac, Gpg4win on Windows) with the passphrase:

```bash
gpg --output leadgen-backup-2026-10-03.json.gz --decrypt leadgen-backup-2026-10-03.json.gz.gpg
```

To keep a copy older than 90 days, download one each month and keep it with the
owner's private files.

### Restoring

Pick the first that fits. Each one keeps the current database as it is until the site
is pointed at the restored one, so it can be undone by pointing it back.

**A. A mistake in the last few hours (within Neon's history): a new branch from the
past.** In the Neon console, open the project → **Branches** → **New branch**. Parent
branch: the one the site uses (`main`, marked default). Name: `restore-<date>`. Under
"Select what to include in the new branch" pick **Past data** and a date and time just
before the mistake (Utah time is UTC−6 in summer, UTC−7 in winter). Click **Create**.
Then **Connect**, pick the new branch, and copy its connection string. Check it (step C
below) on your computer first, then in Render open the **compactor-lead-finder**
service → **Environment** → `DATABASE_URL` → paste the new branch's string → **Save
Changes** (the site restarts on it in about a minute). Neon can also restore the branch
in place (**Backup & Restore** → **Restore from history**), keeping the state before
the restore in a branch named `<branch>_old_<time>`; the new branch is the safer first
step because nothing changes until Render is pointed at it.

**B. Older than Neon's history, or the database (or the Neon account) is gone: from a
copy.** Create a new database (Neon → a new project, or a new branch with **Current
data**; the steps are in [The database](#the-database-saved-leads-marks-calls-the-yelp-count))
and copy its connection string. Then, on a computer with the code and the decrypted copy:

```bash
DATABASE_URL='postgresql://<the new database>' python -m leadgen restore leadgen-backup-2026-10-03.json.gz
DATABASE_URL='postgresql://<the new database>' python -m leadgen restore leadgen-backup-2026-10-03.json.gz --apply
```

The first command changes nothing: it lists, table by table, the rows in the copy, those
already there and those it would add. `--apply` adds them, all in one transaction (the
tables are created first if the database is new). Then point Render's `DATABASE_URL` at
the new database as in A.

**Rows deleted by mistake in the live database:** run B against the live database
itself (its own `DATABASE_URL`). A restore only ever adds rows the database doesn't
have (`INSERT ... ON CONFLICT DO NOTHING`): a row that is already there is never
changed, nothing is deleted, and marks or calls made since the copy stay as they are.
Run without `--apply` first and check what it would add. The site never restores
anything by itself.

**C. Check a restore.** `restore --apply` ends with "The database now holds N saved
leads, N marked yes, N marked no, N calls, N searches": compare it with the line the
copy printed when it was made (the restore repeats it on its first line). Then open the
site and compare with what it showed before the problem: **Leads** (the tab counts:
All, Has baler or compactor, No baler or compactor), **Calls** (All called
businesses), **Stats** (saved and checked counts by tier) and the search history on
**Find leads**. To look before pointing the live site at it, run the site on your own
computer against the restored database: `DATABASE_URL='postgresql://...' python -m
leadgen web`, then open http://127.0.0.1:5000.

**Rehearsals** (write each one here):

| Date | What was restored | Result |
|---|---|---|
| 2026-10-03 | By the developer, on a copy with test data: a SQLite database (6 leads, 2 marks, 2 calls, 1 search) backed up, restored into an empty Postgres 16 database, and the site started on it | The same counts on Leads, Calls, Stats and the search history as the original; every row identical (also checked by `tests/test_backup.py`, Postgres → SQLite → Postgres) |
| Open | The live data: a nightly or on-request copy restored into a new Neon branch or a SQLite file on the owner's computer (B, then C) | Owner action 6 under [Owner actions still open](#owner-actions-still-open) |

## Login and the free plan

Always set `APP_PASSWORD` on a public site: every search can spend your API keys.
Also set `LEADGEN_SUPPORT_CONTACT` to the person who hands out the login, with a
phone number or email (Render → the service → **Environment** → **Add Environment
Variable**, then **Save Changes**): the login page's "Need access or forgot the
password?" line names them, and a salesperson locked out first thing in the morning
can call or email straight from it. Without it the line says "Ask the person who
gave you your login, or Wright AI Solutions." The contact details belong in Render
only, never in this repository. **Status (2026-10-03): not set on the live site**
(the owner, Thomas, chooses the contact and sets it); change this line when it is.
Until then every start of the site logs "LEADGEN_SUPPORT_CONTACT is not set". To check
it from anywhere: `curl -s https://compactor-lead-finder.onrender.com/login | grep -oE
'href="(tel|mailto):[^"]+"'` prints the contact's link once it is set.
A login lasts 30 days on a device; changing the username or password logs everyone
out. After 10 wrong passwords from one address, logins from it pause for 15 minutes.
Without a password the page only answers on `localhost` or an IP address; to use
another hostname, list it in `LEADGEN_ALLOWED_HOSTS`.
The free plan sleeps after 15 idle minutes, so the first visit after that waits 20 to
60 seconds, and its disk is wiped on each restart (which is why data lives in the
database). What keeps it awake while the team works, and what the site says when it
had to wake up anyway: [Keep the site awake](#keep-the-site-awake).
To run the production server yourself: `gunicorn wsgi:app --workers 1 --threads 8 --timeout 0`.

## Keep the site awake

Render's free plan stops the site after 15 minutes with no visit; the next visit
starts it again and waits 20 to 60 seconds. While it waits, the Leads page says
"Starting up… The Lead Finder sleeps when nobody has used it for a while, and the first
visit can take up to a minute to wake it. Your leads appear here by themselves." (after
45 seconds it adds "Still starting. If nothing appears within two minutes, reload the
page."), and the list appears by itself. Three things keep that from happening from 6 AM
to 9 PM Utah time:

1. **An outside pinger** (the one that counts; [owner action 5](#owner-actions-still-open)).
   A free [cron-job.org](https://cron-job.org) job opens
   `https://compactor-lead-finder.onrender.com/healthz` every 5 minutes from 5 AM to
   9 PM Utah time (`/healthz` needs no login and holds no data). Steps:
   1. Open https://console.cron-job.org/signup, sign up (free) with the owner's email
      and confirm the email it sends.
   2. Log in at https://console.cron-job.org → **Cronjobs** → **Create cronjob**.
   3. **Title** `Lead Finder keep-awake`; **URL**
      `https://compactor-lead-finder.onrender.com/healthz`; **Enable job** on.
   4. **Execution schedule** → **Custom**: every day of the month, every day of the
      week, every month; **Hours** 5 to 20 (tick 5, 6, 7 … 20); **Minutes** 0, 5, 10 …
      55. **Time zone** (on the **Advanced** tab of the same page): `America/Denver`,
      which follows Utah's summer and winter time.
   5. **Notifications**: tick **execution of the cronjob fails**, so you get an email
      when the site doesn't answer.
   6. **Advanced** → **Timeout**: the largest offered (30 seconds on the free plan). The
      5 AM ping may time out while the site wakes; the one 5 minutes later finds it awake.
   7. **Create** (or **Save**). Wait 10 minutes and open the job's **History**: a line
      every 5 minutes, each **200 OK**.

   UptimeRobot works too (https://uptimerobot.com → **Register** → **New monitor** →
   **HTTP(s)**, the same URL, **Monitoring interval** 5 minutes, your email as the alert
   contact → **Create monitor**), but its free plan can't skip the night: the site then
   runs all month, about 744 hours, which fits Render's 750 free hours a month only while
   it is the only free service on the Render account. Prefer cron-job.org.
2. **The GitHub job, as a backup** (`.github/workflows/keep-awake.yml`). GitHub starts
   scheduled jobs late and skips many (from 2026-09-30 to 2026-10-03 it ran this one
   every 3 to 5 hours instead of every 10 minutes), so it can't be the only pinger. It is
   scheduled every 15 minutes from 6 AM to 9 PM Utah time, and each run that does start
   opens `/healthz` every 5 minutes for the next 50 minutes (one run at a time; a run
   that starts while another is pinging waits for it), retrying a ping the site didn't
   answer. A run turns red, and GitHub emails the owner, only if the site never answered
   during it. GitHub turns scheduled jobs off after 60 days with no commits: re-enable it
   under **Actions** → **Keep the site awake** → **Enable workflow** if so.
3. **The site's own check.** Each start of the site is recorded (the additive
   `site_starts` table: when, and which version). A start between 6:20 AM and 9 PM Utah
   time without a new version (no deploy since the start before) means the site had gone
   to sleep, or Render restarted it, and it says so once a Utah day under **For the site
   administrator** → **Recent problems** (and on the webhook): "The site had gone to sleep and
   started again at … Utah time without a new version, so whoever opened it then waited
   up to a minute. The keep-awake pinger isn't reaching the site every few minutes …".
   When you see it, open the cron-job.org job's **History**. Each start also reaches the
   database and reads the saved list once, in the background, so the first page doesn't
   wait for either.

**Check it is working** (any day, after 6 AM):

- `curl -s https://compactor-lead-finder.onrender.com/healthz` prints `"ok": true` and
  `"up_since"`, the Utah time this start of the site began. The same `up_since` at 7 AM
  and at 5 PM means it never slept in between (a deploy changes it, and `"version"` too).
- `curl -s -o /dev/null -w "%{time_total}\n" https://compactor-lead-finder.onrender.com/healthz`
  prints well under 2 (seconds); 20 or more means it had been asleep.
- **Recent problems** has no "The site had gone to sleep".

Render's paid Starter plan never sleeps and doesn't need any of this.

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
  continuing searches deeper where the last run stopped; the result says so ("Yelp
  reused the answers of all its 9 searches...", and on the site the day the area was
  searched before), so a "0 new" isn't mistaken for a search that didn't look.
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
