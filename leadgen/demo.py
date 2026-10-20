"""The demo account (sign-up mode): log in as "test" with the password "test" to show the
site, for an ad or a sales call, on a made-up company with made-up leads, Yes / No marks,
calls, contacts and searches around Salt Lake City. Every business, person and phone
number in it is invented (the numbers are 555-01xx, kept for fiction).

ensure() makes it once (the login page calls it the first time someone logs in as
"test"; `python -m leadgen demo` does it by hand) and leaves it alone after. The demo person is a member, not the owner,
so Settings, invites and billing are closed to it, and searching is off for its company
(allowance.refusal), so the made-up list stays as it is.
"""

import json
import logging
import random
import time
import uuid

from . import accounts, allowance, company, config, localtime, saved, store
from .accounts import AccountError
from .models import Lead
from .scoring import score_lead

log = logging.getLogger(__name__)

LOGIN = accounts.DEMO_LOGIN          # typed in the login page's Email box
PASSWORD = "test"
COMPANY_ID = allowance.DEMO_COMPANY
COMPANY = "Summit Compactor Service"
ADDRESS = "Salt Lake City, UT 84104"
CENTER = (40.7469, -111.9387)
OWNER = "Jordan Reyes"
TEAM = ("Jordan Reyes", "Casey Morgan", "Sam Patel")
WORDS = ("compactor", "baler", "cardboard", "recycling")
COMPETITORS = ("Valley Baler Co", "Crossroads Compaction")
DAY = 86400

# (name, category tag, footprint in square feet, Google reviews, street, town, ZIP)
BUSINESSES = [
    ("Wasatch Fresh Market", "supermarket", 48000, 1830, "2150 S Redwood Rd", "West Valley City", "84119"),
    ("Granite Peak Grocers", "supermarket", 42000, 1210, "6950 S Highland Dr", "Cottonwood Heights", "84121"),
    ("Bonneville Distribution Center", "building=warehouse", 410000, 95, "5200 W 2100 S", "West Valley City",
     "84120"),
    ("Jordan River Wholesale Club", "shop=wholesale", 145000, 4210, "11200 S State St", "Sandy", "84070"),
    ("Saltair Logistics Hub", "building=warehouse", 280000, 60, "1800 S 4800 W", "Salt Lake City", "84104"),
    ("Mountain West Home Center", "shop=department_store", 112000, 2650, "3300 S 700 E", "Salt Lake City",
     "84106"),
    ("Pioneer Valley Medical Center", "amenity=hospital", 390000, 1450, "4000 S 3900 W", "West Valley City",
     "84120"),
    ("Red Butte Foods Plant", "landuse=industrial", 165000, 40, "950 N 2200 W", "Salt Lake City", "84116"),
    ("Big Cottonwood Market", "supermarket", 39000, 980, "4700 S Holladay Blvd", "Holladay", "84117"),
    ("Oquirrh Supply Warehouse", "building=warehouse", 220000, 75, "7400 W 2700 S", "Magna", "84044"),
    ("Riverbend Supercenter", "shop=department_store", 185000, 5120, "5600 S Redwood Rd", "Taylorsville",
     "84123"),
    ("Canyon Rim Hotel & Suites", "tourism=hotel", 160000, 2380, "150 W 500 S", "Salt Lake City", "84101"),
    ("Liberty Park Grocery", "supermarket", 31000, 760, "1100 S 900 E", "Salt Lake City", "84105"),
    ("Great Basin Cold Storage", "building=warehouse", 240000, 35, "2900 S 5600 W", "West Valley City", "84120"),
    ("Sugar House Furniture Outlet", "shop=furniture", 68000, 1320, "2200 S 1300 E", "Salt Lake City",
     "84106"),
    ("Westpointe Packaging", "landuse=industrial", 125000, 25, "2200 N 2200 W", "Salt Lake City", "84116"),
    ("Midvale Fresh Foods", "supermarket", 36000, 890, "7200 S Fort Union Blvd", "Midvale", "84047"),
    ("Draper Commerce Park Warehouse", "building=warehouse", 300000, 50, "13800 S Minuteman Dr", "Draper",
     "84020"),
    ("Kearns Discount Market", "supermarket", 28000, 640, "4800 W 5400 S", "Kearns", "84118"),
    ("Murray Regional Clinic", "amenity=hospital", 210000, 1010, "5100 S Cottonwood St", "Murray", "84107"),
    ("Valley Beverage Bottling", "landuse=industrial", 140000, 30, "3800 S 1900 W", "West Valley City",
     "84119"),
    ("Cottonwood Sports Outfitters", "shop=sports", 54000, 1480, "6900 S 1300 E", "Cottonwood Heights",
     "84121"),
    ("South Towne Home Store", "shop=department_store", 98000, 1890, "10400 S State St", "Sandy", "84070"),
    ("North Salt Lake Freight Depot", "building=warehouse", 260000, 45, "800 W Center St", "North Salt Lake",
     "84054"),
    ("Holladay Hardware & Garden", "shop=hardware", 32000, 520, "4600 S 2300 E", "Holladay", "84117"),
    ("Tri-County Food Bank", "building=warehouse", 90000, 310, "1500 S 3600 W", "Salt Lake City", "84104"),
    ("University Heights Market", "supermarket", 33000, 1100, "1400 E 1300 S", "Salt Lake City", "84105"),
    ("Brighton Electronics Warehouse", "shop=electronics", 45000, 870, "1100 W 7200 S", "Midvale", "84047"),
    ("Salt Flats Printing Co", "landuse=industrial", 75000, 20, "1300 S 3200 W", "Salt Lake City", "84104"),
    ("Taylorsville Family Grocer", "supermarket", 30000, 580, "1800 W 4700 S", "Taylorsville", "84129"),
    ("Westgate Conference Hotel", "tourism=hotel", 190000, 2950, "75 S West Temple", "Salt Lake City", "84101"),
    ("Granger Auto Parts Distribution", "building=warehouse", 130000, 85, "3500 S 3200 W", "West Valley City",
     "84119"),
    ("Fort Union Appliance Center", "shop=appliance", 38000, 410, "1000 E Fort Union Blvd", "Midvale", "84047"),
    ("Mill Creek Natural Foods", "supermarket", 26000, 950, "3900 S 2300 E", "Millcreek", "84124"),
    ("Herriman Supply Yard", "shop=hardware", 41000, 330, "5400 W 13400 S", "Herriman", "84096"),
    ("Riverton Market Place", "supermarket", 40000, 1240, "12600 S 1300 W", "Riverton", "84065"),
    ("Capitol Hill Senior Living", "amenity=nursing_home", 120000, 210, "300 N Main St", "Salt Lake City",
     "84103"),
    ("West Jordan Wholesale Foods", "shop=wholesale", 110000, 1650, "7800 S Redwood Rd", "West Jordan", "84088"),
    ("Airport Cargo Center", "building=warehouse", 350000, 55, "4100 W 500 N", "Salt Lake City", "84116"),
    ("Sandy Outdoor Gear", "shop=outdoor", 47000, 760, "9400 S 700 E", "Sandy", "84070"),
    ("East Bench Grocery", "supermarket", 29000, 690, "2700 E Parleys Way", "Salt Lake City", "84109"),
    ("Magna Recycled Paper Mill", "landuse=industrial", 200000, 15, "8800 W 2100 S", "Magna", "84044"),
    ("Downtown Convention Plaza", "tourism=hotel", 230000, 3400, "100 S 200 W", "Salt Lake City", "84101"),
    ("Valley Fair Department Store", "shop=department_store", 125000, 2240, "3600 S 2700 W", "West Valley City",
     "84119"),
    ("South Jordan Fresh Market", "supermarket", 37000, 870, "10400 S Bangerter Pkwy", "South Jordan", "84095"),
    ("Parleys Distribution Warehouse", "building=warehouse", 180000, 40, "2300 S 3000 E", "Salt Lake City",
     "84109"),
    ("Lakeside Furniture Gallery", "shop=furniture", 52000, 640, "4300 S 900 W", "Salt Lake City", "84123"),
    ("Copperton Industrial Plant", "landuse=industrial", 150000, 10, "8200 W 10200 S", "West Jordan", "84081"),
    ("Millcreek Medical Pavilion", "amenity=hospital", 170000, 780, "3600 S 1300 E", "Millcreek", "84106"),
    ("Rose Park Neighborhood Market", "supermarket", 22000, 410, "900 N Redwood Rd", "Salt Lake City", "84116"),
]
# Roughly where each town is, so a lead sits near its address on the map.
TOWNS = {
    "Salt Lake City": (40.7500, -111.8950), "West Valley City": (40.6900, -111.9900),
    "Cottonwood Heights": (40.6200, -111.8100), "Sandy": (40.5700, -111.8700),
    "Holladay": (40.6680, -111.8250), "Magna": (40.7000, -112.0950), "Taylorsville": (40.6600, -111.9400),
    "Midvale": (40.6100, -111.8950), "Kearns": (40.6560, -111.9950), "Murray": (40.6600, -111.8900),
    "Draper": (40.5250, -111.8650), "North Salt Lake": (40.8480, -111.9100), "Millcreek": (40.6880, -111.8400),
    "Herriman": (40.5100, -112.0300), "Riverton": (40.5200, -111.9400), "West Jordan": (40.6100, -111.9700),
    "South Jordan": (40.5600, -111.9300),
}
# How the team's calls went, with what was said: (outcome, notes).
CALLS = [
    ("Interested", "Spoke with the store manager. Their baler is 12 years old and breaks down monthly. "
                   "Wants a quote on a 60-inch vertical baler."),
    ("Follow Up", "Facilities manager is out until next week. Call back Tuesday morning."),
    ("Interested", "Running two compactors on a lease that ends in March. Open to a service contract."),
    ("Not Interested", "Happy with their current provider for now."),
    ("Follow Up", "Asked us to email pricing for preventive maintenance. Sent it, follow up Friday."),
    ("No Contact", "Left a voicemail with the front desk."),
    ("Interested", "Cardboard volume doubled this year. Wants a site visit to look at the dock."),
    ("Not Qualified", "Waste is handled by the building's landlord."),
    ("Follow Up", "Ops director wants to compare quotes after the holidays."),
    ("Interested", "Compactor door seal is leaking. Booked a service call for Thursday."),
    ("Bad Lead", "Location closed last month."),
    ("No Contact", "No answer after two tries."),
]
CONTACTS = ["Maria Lopez, store manager", "Derek Hall, facilities", "Angela Brooks, operations",
            "Tom Nguyen, maintenance lead", "Priya Shah, purchasing", "Luis Romero, warehouse manager"]


def _leads() -> list[Lead]:
    rng = random.Random(7)
    leads = []
    for i, (name, tag, sqft, reviews, street, town, zip_code) in enumerate(BUSINESSES):
        lat = TOWNS[town][0] + rng.uniform(-0.025, 0.025)
        lon = TOWNS[town][1] + rng.uniform(-0.03, 0.03)
        lead = Lead(name=name, lat=round(lat, 5), lon=round(lon, 5), source="google", source_id=f"demo-{i}",
                    address=f"{street}, {town}, UT {zip_code}", city=town, state="UT", zip=zip_code,
                    phone=f"(801) 555-01{i:02d}",
                    raw_categories=[tag], rating_count=reviews, footprint_sqft=sqft,
                    business_status="OPERATIONAL")
        leads.append(score_lead(lead, [*config.DEFAULT_KEYWORDS, *WORDS]))
    return leads


def _user_exists() -> bool:
    return accounts.user_by_email(LOGIN) is not None


def _make_account() -> None:
    now = time.time()
    with accounts._base() as db, db.transaction():
        if db.one("SELECT id FROM companies WHERE id = ?", (COMPANY_ID,)) is None:
            db.run(f"INSERT INTO companies ({accounts._COMPANY_COLUMNS}) VALUES ({', '.join('?' * 15)})",
                   (COMPANY_ID, COMPANY, accounts.short_name(COMPANY), ADDRESS, CENTER[0], CENTER[1], 30.0,
                    json.dumps(list(WORDS)), json.dumps(list(COMPETITORS)), now - 45 * DAY, "team", "active",
                    None, None, None))
        accounts._insert_user(db, LOGIN, PASSWORD, OWNER, COMPANY_ID, "member", verified=True)


def _fill() -> None:
    """The company's made-up data: leads first seen over the last six weeks, marks, calls,
    contacts and a search a few days a week."""
    rng = random.Random(11)
    now = time.time()
    leads = _leads()
    saved.save_search(leads, [*config.DEFAULT_KEYWORDS, *WORDS])
    with store.connect() as db, db.transaction():
        uids = [r[0] for r in db.all("SELECT uid FROM leads ORDER BY uid")]
        rng.shuffle(uids)
        for uid in uids:
            seen = now - rng.uniform(1, 42) * DAY
            db.run("UPDATE leads SET first_seen = ?, last_seen = ? WHERE uid = ?", (seen, max(seen, now - DAY), uid))
        # Marks: about two thirds checked, most Yes.
        for n, uid in enumerate(uids[:34]):
            value = "no" if n % 5 == 4 else "yes"
            at = now - rng.uniform(0.2, 30) * DAY
            change = uuid.uuid4().hex
            db.run("INSERT INTO marks (uid, value, updated_at) VALUES (?, ?, ?)", (uid, value, at))
            db.run("INSERT INTO mark_changes (id, uid, value, previous, at, undone_at) VALUES (?, ?, ?, '', ?, NULL)",
                   (change, uid, value, at))
            store.record_maker(db, change, TEAM[n % len(TEAM)])
        # Calls on the Yes leads: some called twice.
        for n, uid in enumerate(uids[:26]):
            if n % 5 == 4:
                continue
            first = now - rng.uniform(2, 25) * DAY
            outcome, notes = CALLS[n % len(CALLS)]
            times = [first, first + rng.uniform(1, 6) * DAY] if n % 4 == 1 else [first]
            for k, at in enumerate(times):
                at = min(at, now - 3600)
                said = (outcome, notes) if k == len(times) - 1 else ("No Contact", "Left a voicemail.")
                call = uuid.uuid4().hex
                db.run("INSERT INTO calls (id, uid, at, outcome, notes) VALUES (?, ?, ?, ?, ?)", (call, uid, at, *said))
                store.record_maker(db, call, TEAM[(n + k) % len(TEAM)])
            if n % 3 == 0:
                person = CONTACTS[n % len(CONTACTS)]
                db.run("INSERT INTO lead_contacts (id, uid, phone, contact, by_name, at) VALUES (?, ?, ?, ?, ?, ?)",
                       (uuid.uuid4().hex, uid, f"(801) 555-01{50 + n:02d}", person, TEAM[n % len(TEAM)],
                        first + 600))
        # The search history: a search most weekdays over the last six weeks.
        for back in range(42, 0, -1):
            at = now - back * DAY
            day = localtime.utah(at)
            if day.weekday() >= 5 or back % 3 == 0:
                continue
            found = rng.randint(180, 420)
            info = {"location": ADDRESS, "radius": 30.0, "keywords": ", ".join(WORDS), "source": "all",
                    "place": f"{COMPANY}, {ADDRESS}", "miles": 0.0, "center": list(CENTER),
                    "leads": found, "new": rng.randint(0, 14), "found_near": ADDRESS, "warnings": []}
            db.run("INSERT INTO searches (day, at, info) VALUES (?, ?, ?) ON CONFLICT (day) DO NOTHING",
                   (day.strftime("%Y-%m-%d"), at, json.dumps(info)))


def ensure() -> bool:
    """Make the demo account and its data when it isn't there yet. True when it was made."""
    if _user_exists():
        return False
    try:
        _make_account()
    except AccountError:
        return False
    found = accounts.get_company(COMPANY_ID)
    assert found is not None
    token = company.use(found)
    try:
        with store.for_company(COMPANY_ID):
            if saved.count() == 0:
                _fill()
    finally:
        company.reset(token)
    log.info("Made the demo account (log in as %r)", LOGIN)
    return True


def ensure_quietly() -> None:
    """ensure(), for the login page: a failure is logged, never raised."""
    try:
        ensure()
    except Exception:
        log.exception("Making the demo account failed")
