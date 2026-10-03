"""Taking saved leads far outside AARCO's area out of the list, only on request.

A search around the wrong place (a typo, or a same-named town in another state)
used to save its businesses for good. Searches now confirm a far place first
(web/finding.py), but leads saved before that stay. The owner decides what
happens to them: `python -m leadgen out-of-area` lists the saved leads farther
than --miles from AARCO's shop, and only with --remove takes them out of the list.

Nothing is lost: a removed lead's row is kept as it was in the removed_leads
table, and `--restore` puts every removed lead back. A lead someone marked Yes /
No or logged a call for is never removed (it is listed as kept). Searches and the
site never call this; a later search that finds the business again saves it anew.
"""

import json
import time
from collections import Counter
from dataclasses import dataclass

from . import config, store
from .geo import haversine_miles
from .places import tidy_town

# The default distance: twice AARCO's area, so leads from a confirmed search just
# outside the area (and its radius) are not listed.
DEFAULT_MILES = 2 * config.SERVICE_AREA_MILES


@dataclass
class FarLead:
    uid: str
    name: str
    city: str
    state: str
    miles: float
    kept: bool          # marked Yes / No or called: never removed


def _worked_on(db: store.Db) -> set[str]:
    """The uids someone marked or logged a call for."""
    uids = {uid for (uid,) in db.all("SELECT uid FROM marks")}
    uids |= {uid for (uid,) in db.all("SELECT uid FROM mark_changes")}
    uids |= {uid for (uid,) in db.all("SELECT uid FROM calls")}
    return uids


def far_leads(miles: float = DEFAULT_MILES) -> list[FarLead]:
    """The saved leads farther than `miles` from AARCO's shop, farthest first."""
    with store.connect() as db:
        rows = db.all("SELECT uid, lead FROM leads")
        worked = _worked_on(db)
    found = []
    for uid, text in rows:
        lead = json.loads(text)
        if not lead:
            continue
        lat, lon = float(lead["lat"]), float(lead["lon"])
        away = haversine_miles(*config.SERVICE_CENTER, lat, lon)
        if away > miles:
            town = tidy_town(lead.get("city", ""), lead.get("state", ""), lat, lon, lead.get("zip", ""))
            found.append(FarLead(uid, lead.get("name", ""), town,
                                 lead.get("state", ""), round(away, 1), uid in worked))
    return sorted(found, key=lambda f: (-f.miles, f.name.lower()))


def remove(leads: list[FarLead]) -> int:
    """Take these leads out of the saved list (the ones not worked on), keeping each
    row in removed_leads; returns how many were removed."""
    now = time.time()
    removed = 0
    with store.connect() as db:
        worked = _worked_on(db)
        for far in leads:
            if far.kept or far.uid in worked:
                continue
            row = db.one("SELECT uid, lead, parts, ids, first_seen, last_seen FROM leads "
                         "WHERE uid = ?", (far.uid,))
            if row is None:
                continue
            db.run("INSERT INTO removed_leads (uid, at, miles, row) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT (uid) DO UPDATE SET at = excluded.at, miles = excluded.miles, "
                   "row = excluded.row", (far.uid, now, far.miles, json.dumps(list(row))))
            db.run("DELETE FROM leads WHERE uid = ?", (far.uid,))
            removed += 1
    return removed


def restore() -> int:
    """Put every removed lead back in the saved list (a lead a later search saved
    again keeps that newer row); returns how many came back."""
    back = 0
    with store.connect() as db:
        for uid, text in db.all("SELECT uid, row FROM removed_leads"):
            _, lead, parts, ids, first_seen, _ = json.loads(text)
            got = db.one("INSERT INTO leads (uid, lead, parts, ids, first_seen, last_seen) "
                         "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (uid) DO NOTHING RETURNING uid",
                         (uid, lead, parts, ids, first_seen, time.time()))
            db.run("DELETE FROM removed_leads WHERE uid = ?", (uid,))
            back += got is not None
    return back


def summary(leads: list[FarLead]) -> list[str]:
    """Plain lines for the command line: how many, where, and the farthest few."""
    if not leads:
        return []
    places = Counter(", ".join(x for x in (f.city, f.state) if x) or "(no town listed)"
                     for f in leads)
    lines = [f"  {n:>5}  {place}" for place, n in places.most_common(10)]
    lines += ["", "  Farthest:"] + [f"  {f.miles:>7,.0f} mi  {f.name[:50]}" + ("  (kept: marked or called)"
                                                                            if f.kept else "")
                                    for f in leads[:10]]
    return lines
