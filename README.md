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

**Emergency stop:** on the site, **Find leads** → **For the site administrator**
(at the bottom, closed until opened, then unlocked with the administrator password) →
**Pause searching** → confirm **Pause searching** (or **Stop using Google** / **Stop
using Yelp**). It works on the next
request, with no restart, and a search already running stops within a few seconds
(also during the free map data step), keeping and saving what it had found. The backup, if the site itself won't load: in Render, the
service → **Environment**, add `LEADGEN_SEARCH_PAUSED` = `1` and **Save Changes**
(`LEADGEN_GOOGLE_OFF` / `LEADGEN_YELP_OFF` stop just one paid source). See the
[runbook](docs/operator-runbook.md#emergency-switches-stop-searches-or-paid-calls).

## What it does

- **Find leads** once a day (Utah calendar day) around Arco's shop or any ZIP or
  city, from Google Places, Yelp (at most 50 calls in any 24 hours, with the reset
  time shown, "today at ..." or "tomorrow at ...") and the free OpenStreetMap data
  (asked in parts, the area around the search's centre first and then outwards, so
  the nearest businesses come first and busy public servers still answer; areas a
  busy server missed are asked again automatically once during the search, which so
  takes about 7 minutes at most, and then **in the background for up to an hour**,
  within the same search: their businesses join the saved list as they arrive, and
  Find leads and the search history say "Still filling in N areas" with the towns
  they hold ("around Kaysville, Centerville and Morgan"), then "Complete" or "N
  areas never answered" (or, when searching was paused, that the areas left were not
  asked again). Every line of that search's history and Details (how many areas
  answered, the towns still missing) follows the filling in, so they all name the same
  missing towns. A search with missing areas saves what it found and still
  uses up the day, as the owner asked; the filling in is not a second search). A
  filling in that runs past midnight stays on Find leads until it ends, and is ended
  first when the next day's search starts, so two map searches never run at once.
  Pausing searching ends it within a few seconds, and Find leads says so.
  The progress bar covers only the steps the search runs (with the free map data
  alone it starts near 0%), its map step moves with the areas that answered, not
  with the clock, and the "N of M areas done" count stays in view until that step
  ends; the search history shows today's search as "Running…" from the moment it starts.
  A search cut off by a server restart (a deploy) keeps what it had found: it is
  saved, the day's search is given back at once, and the history says so
  ("Interrupted by a server restart: the 128 businesses it had found were saved"); Find
  leads says the same (or, when it had found nothing yet, that nothing was saved and
  today's search is still available).
  When the place lookups themselves are down, the page says to try again in a minute
  (nothing spent), not to check the spelling.
  Before anything is spent, the confirmation names the place the search will
  actually run around and how far it is from Arco's shop; a town name on its own
  ("Murray", "Sandy") means the Utah one. A place outside Arco's area (more than 30
  miles from the shop, `SERVICE_AREA_MILES`) needs a second, explicit yes that names
  it again, and the search history shows where each search ran under what was typed.
  The standard words (compactor, baler, waste, recycling) are always searched;
  **Extra search words** (empty to start) are searched as well. Scores always use
  the standard words, so the minimum score, the list and Stats all use the same number.
- One **saved list**, one row per business, kept for good with its source details.
  The buildings of one site (an apartment complex's numbered buildings, a campus's
  parts, one name spread over a site up to half a mile across) are one lead, and so
  are a listing inside the map outline of a same-named one, the parts of an air base,
  airport or campus up to 1.5 miles apart ("Hill Air Force Base" as the airfield and
  as the base), and a name that only adds its town ("Smith's Distribution Center" /
  "Smith's Layton Distribution"). Neighbours with different names or phone numbers
  (two stores in one strip mall) stay separate.
  Police, fire, impound and trailer yards and parcel lockers are not prospects,
  nor are pumping stations, wells, substations and small (under 5,000 sq ft)
  industrial buildings known only by a map tag, nor self-storage, data centres,
  career centres or a city's maintenance shops. "Harbor Freight" is a tool shop,
  not a freight warehouse, and a furniture shop mapped as a mall is not a venue. A
  map listing named only "Recycling" or "Junkyard" gets its operator, street or
  city added to its name and ranks below named places. A brand counts only when the
  business is that brand (a hotel named after the air base next to it is not the base).
  A name word alone never makes a small shop a plant ("Day Dairy Barn" is not a
  dairy). Parcel delivery stations and depots (mapped as a post depot, or named for a
  carrier's delivery station or home-delivery hub: "FedEx Home Delivery", "Amazon
  Delivery Station") are warehouse / logistics sites, not manufacturing; a carrier's
  shop ("The UPS Store") or lockers are not.
  **Why this score** and the Excel and CSV **Why This Score** column explain each
  lead in a salesperson's words: where its kind of business comes from ("from the map
  listing", "from its name") and, when only the building type or the name suggests
  it, "confirm before calling". The classifier's own rule names stay in the code. When the buildings of one site marked Yes and No are joined
  (`python -m leadgen merge-sites --apply`), the lead keeps Yes and says the marks
  disagreed until someone presses Yes or No on it again.
- Every Yes / No mark and call needs **Your name**: the server refuses one without it.
- **Yes / No** "has a baler or compactor" marks (permanent; a click can be undone
  for 5 minutes), **Just called** notes with six results and a Calls tab for each
  (with a filter box: several words, such as "walmart layton", find the rows that
  hold every one of them, in any order, on Leads and Calls alike; on Calls it also
  looks in what was said on the calls, how they went and who made them, so
  "forklift" or a colleague's name finds the business),
  a **Stats** page, and Excel / CSV downloads (named with the Utah date, in plain words; columns empty for
  every lead in the file are left out and named on the Run Info sheet). Each mark and call records who made
  it (the name set under **Your name** in that browser, asked before the first
  mark or call and not skippable). **Has phone** on Leads shows only businesses
  that can be phoned; under Not checked, among equal scores, those come first.
  Leads and Calls show one page of rows at a time (**Show more** adds the next), with
  every tab's count however long the list grows; a tab, filter or sort that takes
  more than a moment says **Loading…** over the dimmed rows (and, after about five
  seconds, that it is taking longer than usual).
- A business the listing gives no street address or town for shows the town and ZIP
  its map position is near ("No street address · near West Jordan, UT 84088 · map"),
  worked out offline from a small table of Census towns and ZIP areas
  (`leadgen/data/places.json`, see `leadgen/places.py`), so same-named stores (a
  dozen Smith's) can be told apart; the downloads write it as City "near West
  Jordan", ZIP "near 84088". It is shown, never saved: the saved rows are unchanged.
- Town names are shown and downloaded as the town's own name from the same table,
  however the listing typed them: "CLEARFIELD" is Clearfield, "american Fork" American
  Fork, "West Jordan City" West Jordan, "Saratoga Spring" Saratoga Springs, "SLC" Salt
  Lake City, and a short form like "la" the town the business is near that starts with
  it (Layton). A town not in the table keeps its name, in title case when it was all
  capitals or all lower case. So **City A to Z** and the filter treat one town as one
  town. As with "near", only what is shown changes; the saved rows keep the text the
  source gave.
- Keyboard use: after a Yes / No, an undo, a call saved or **Show more**, the focus
  stays on that business (or moves to the next one's Yes when it left the view). A
  click that lands just as the list moves up is not saved, and the page says so.
- A Yes / No that couldn't be saved (offline, say) is said on the row itself, "Yes
  not saved." with the reason and **Try again**, until it is saved or dismissed, as
  well as in the note at the bottom of the screen.
- The Leads page gets one page of rows at a time (100); **Show more** fetches the
  next page, and the tab counts are always exact, however long the list grows.
- Works on phones, tablets and laptops: below 1,100 px wide each lead is a card,
  so the reasons for its score are always in view without scrolling sideways.
- A login from `APP_USERNAME` / `APP_PASSWORD` in the environment, and a separate
  `ADMIN_PASSWORD` that unlocks the administrator's section. The login page's
  "Need access or forgot the password?" line names the contact set in
  `LEADGEN_SUPPORT_CONTACT` (a name with a phone number or email, which become
  tap-to-call and email links); until it is set it says "Ask the person who gave you
  your login, or Wright AI Solutions." Set it in Render's **Environment**, never in
  the repository.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Command line: 30 miles around Arco Compactor (876 Fortune Rd, SLC), saved to output/leads.xlsx
python -m leadgen run

# Web page at http://127.0.0.1:5000
python -m leadgen web

# List saved leads that are one business saved as two or more rows (changes nothing);
# --apply merges each group into its first row, keeping every listing, mark and call
python -m leadgen merge-sites [--apply]

# List saved leads far outside Arco's area (e.g. from a search around the wrong place);
# --remove takes them out of the list (kept aside), --restore puts them back
python -m leadgen out-of-area [--miles 60] [--remove | --restore]

# Copy every saved lead, mark, call and search (with who made each) to a file, and put a
# copy back: restore lists what it would add; --apply adds only the rows the database
# lacks, never changing or removing one (operator runbook: "Backups and restoring")
python -m leadgen backup [--out FILE]
python -m leadgen restore FILE [--apply]
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
Once online, every push to `main` deploys on its own, but only after the GitHub
checks (`lint`, `test (sqlite)`, `test (postgres)`) pass: a commit with a failing
test never reaches the live site (`autoDeployTrigger: checksPass` in `render.yaml`;
see "Deploying" in the runbook, which also has the steps for protecting `main`).

**Protecting `main` (a one-time GitHub setting for the owner).** So that a commit
with a failing check can't even land on `main`, the repository owner turns on a
rule on GitHub; the code can't do it. Open the repository's **Settings** → **Rules**
→ **Rulesets** → **New ruleset** → **New branch ruleset**; name it `Protect main`,
set **Enforcement status** to **Active** and leave the bypass list empty; under
**Target branches** choose **Add target** → **Include default branch**; under
**Branch rules** keep **Restrict deletions** and **Block force pushes**, tick
**Require status checks to pass** and add the checks `lint`, `test (sqlite)` and
`test (postgres)`; then click **Create**. From then on, work is pushed to a branch
first and reaches `main` (by a pull request, or `git push origin <branch>:main`)
only once its checks are green. The
[runbook](docs/operator-runbook.md#deploying) has the same steps in full, the
classic-rule alternative, how to check the rule is on and its current status (not
set yet on 2026-10-02).

**Still open for the owner** (each a few minutes, with exact steps and a check in the
runbook's [Owner actions still open](docs/operator-runbook.md#owner-actions-still-open)):
protect `main` as above; set `LEADGEN_SUPPORT_CONTACT` in Render so the login page
names a real contact (until then each start of the site logs that it is not set);
and delete the finished branch `wip-yelp-cap-and-baler-marks` on GitHub.

---

© Wright AI Solutions.
