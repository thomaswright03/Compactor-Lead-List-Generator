"""Current customers: the businesses AARCO already services, kept off the lead lists.

The company's customer list is private, so the site never receives it. The Customers
page reads the file (CSV or Excel) in the browser (static/customers.js), turns each
customer's phone, street address and name into match keys, and sends only a one-way
fingerprint of each key (SHA-256 of the site's salt and the key). This module makes the
same keys for every saved lead; a lead whose fingerprint is on file is a current
customer and is left out of the Leads, Calls and Map pages and the downloads.

The rules below must stay in step with customers.js (tests/test_customers.py checks
both against the same examples). A key is:
  phone:<10 digits>               the phone number, with its area code
  addr:<street>|<ZIP>             the street line (number and street) and the 5-digit ZIP
  addrcity:<street>|<city>        the street line and the town
  name:<name>|<ZIP>               the business name and ZIP (a name alone would hide every
  namecity:<name>|<city>          store of a chain), or the name and town
Uploading a list replaces the one on file; Clear takes it off, and the leads come back.
"""

import hashlib
import re
import secrets
import time
import unicodedata
from collections.abc import Iterable

from . import store
from .models import Lead

# At most this many fingerprints in one list (5 per customer: 20,000 customers).
MAX_HASHES = 100_000
HASH = re.compile(r"^[0-9a-f]{64}$")
NOT_A_LIST = "That doesn't look like a list made by this page. Reload the page and try again."

# Words that say nothing about which business it is.
NAME_DROP = {"the", "inc", "incorporated", "llc", "co", "corp", "corporation", "company",
             "ltd", "limited"}
# Street words, written the one way (USPS abbreviations).
STREET_WORDS = {
    "street": "st", "road": "rd", "avenue": "ave", "av": "ave", "drive": "dr",
    "boulevard": "blvd", "lane": "ln", "parkway": "pkwy", "circle": "cir", "court": "ct",
    "place": "pl", "highway": "hwy", "terrace": "ter", "north": "n", "south": "s",
    "east": "e", "west": "w", "northeast": "ne", "northwest": "nw", "southeast": "se",
    "southwest": "sw",
}
# A unit inside the building ("Suite 200"): the street line ends before it.
UNIT_WORDS = {"suite", "ste", "unit", "apt", "apartment", "bldg", "building", "floor", "fl",
              "room", "rm"}


def words(text: object) -> list[str]:
    """Lowercase words of letters and digits: accents dropped, & read as "and",
    apostrophes taken out ("Smith's" -> "smiths"), anything else a space."""
    text = str(text or "").lower()
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not 0x300 <= ord(c) <= 0x36F)
    text = text.replace("&", " and ")
    text = re.sub(r"['’]", "", text)
    return re.sub(r"[^a-z0-9]+", " ", text).split()


def phone_key(phone: object) -> str:
    """The 10 digits of a US phone number ("" when it isn't one)."""
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def street_key(address: object) -> str:
    """The street line ("876 w fortune rd"): the part before the first comma, up to any
    suite or unit, street words abbreviated; "" unless it starts with a house number."""
    line = str(address or "").split(",")[0]
    out: list[str] = []
    for word in words(line):
        if word in UNIT_WORDS:
            break
        out.append(STREET_WORDS.get(word, word))
    if len(out) < 2 or not out[0][0].isdigit():
        return ""
    return " ".join(out)


def name_key(name: object) -> str:
    """The business name without "Inc", "LLC", "The" or store numbers ("" when too short)."""
    out = [w for w in words(name) if w not in NAME_DROP and not w.isdigit()]
    text = " ".join(out)
    return text if len(text) >= 3 else ""


def zip_key(zip_code: object) -> str:
    digits = re.sub(r"\D", "", str(zip_code or ""))
    return digits[:5] if len(digits) >= 5 else ""


def city_key(city: object) -> str:
    return " ".join(words(city))


def keys(name: object, phone: object, address: object, city: object, zip_code: object) -> set[str]:
    """The match keys of one business (see the module's notes)."""
    found: set[str] = set()
    number = phone_key(phone)
    if number:
        found.add(f"phone:{number}")
    street, business = street_key(address), name_key(name)
    town, zip5 = city_key(city), zip_key(zip_code)
    for kind, value in (("addr", street), ("name", business)):
        if not value:
            continue
        if zip5:
            found.add(f"{kind}:{value}|{zip5}")
        if town:
            found.add(f"{kind}city:{value}|{town}")
    return found


def lead_keys(lead: Lead) -> set[str]:
    """A saved lead's keys: its listing's phone and the team's verified one, its address,
    its name and the names of the listings merged into it."""
    found = keys(lead.name, lead.phone, lead.address, lead.city, lead.zip)
    number = phone_key(lead.verified_phone)
    if number:
        found.add(f"phone:{number}")
    for other in lead.alt_names:
        found |= {k for k in keys(other, "", "", lead.city, lead.zip) if k.startswith("name")}
    return found


def fingerprint(salt: str, key: str) -> str:
    """What the browser sends for a key: hex SHA-256 of the salt, a newline and the key."""
    return hashlib.sha256(f"{salt}\n{key}".encode()).hexdigest()


def salt() -> str:
    """The site's salt (made once, kept for good, so stored fingerprints keep matching)."""
    with store.connect() as db:
        row = db.one("SELECT salt FROM customer_list WHERE id = 'current'")
        if row:
            return str(row[0])
        made = secrets.token_hex(16)
        db.run("INSERT INTO customer_list (id, salt, at, by_name, customers) VALUES ('current', ?, NULL, '', 0) "
               "ON CONFLICT (id) DO NOTHING", (made,))
        row = db.one("SELECT salt FROM customer_list WHERE id = 'current'")
        return str(row[0]) if row else made


def on_file() -> tuple[str, set[str]]:
    """The salt and every stored fingerprint (an empty set when no list is on file)."""
    with store.connect() as db:
        row = db.one("SELECT salt FROM customer_list WHERE id = 'current'")
        if not row:
            return "", set()
        return str(row[0]), {str(r[0]) for r in db.all("SELECT hash FROM customer_keys")}


def is_customer(lead: Lead, salt_text: str, hashes: set[str]) -> bool:
    return any(fingerprint(salt_text, k) in hashes for k in lead_keys(lead))


def leave_out(leads: list[Lead]) -> list[Lead]:
    """leads without the current customers (as they are when no list is on file)."""
    salt_text, hashes = on_file()
    if not hashes:
        return leads
    return [l for l in leads if not is_customer(l, salt_text, hashes)]


def info() -> dict[str, object]:
    """The list on file, for the page: how many customers, when, by whom (no fingerprints)."""
    with store.connect() as db:
        row = db.one("SELECT at, by_name, customers FROM customer_list WHERE id = 'current'")
        count = db.one("SELECT COUNT(*) FROM customer_keys")
    at, by, customers = row if row else (None, "", 0)
    return {"at": at, "by": by or "", "customers": int(customers or 0), "keys": int(count[0] if count else 0)}


def replace(hashes: Iterable[str], customers: int, by: str) -> int:
    """Put this list on file in place of the one before; returns how many fingerprints.
    Raises ValueError for anything that isn't a list of fingerprints."""
    kept = sorted({str(h).lower() for h in hashes})
    if not kept:
        raise ValueError("No customers could be read from that file.")
    if len(kept) > MAX_HASHES:
        raise ValueError(f"That list is too long: at most {MAX_HASHES // 5:,} customers at a time.")
    if not all(HASH.match(h) for h in kept):
        raise ValueError(NOT_A_LIST)
    salt()                                            # the row exists
    now = time.time()
    with store.connect() as db, db.transaction():
        db.run("DELETE FROM customer_keys")
        for start in range(0, len(kept), 1000):
            chunk = kept[start:start + 1000]
            db.run("INSERT INTO customer_keys (hash) VALUES " + ", ".join("(?)" for _ in chunk), chunk)
        db.run("UPDATE customer_list SET at = ?, by_name = ?, customers = ? WHERE id = 'current'",
               (now, store.person(by), max(0, int(customers))))
    return len(kept)


def clear(by: str) -> None:
    """Take the list off file: every lead it hid shows again. The salt stays."""
    salt()
    with store.connect() as db, db.transaction():
        db.run("DELETE FROM customer_keys")
        db.run("UPDATE customer_list SET at = ?, by_name = ?, customers = 0 WHERE id = 'current'",
               (time.time(), store.person(by)))
