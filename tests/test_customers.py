"""Current customers: the company's list is read in the browser and never sent; only
one-way fingerprints reach the site, which hides every lead they match (customers.py,
static/customers.js)."""

import io
import json

import pytest
from openpyxl import Workbook
from test_browser import _context, browser, site  # noqa: F401 - fixtures

from leadgen import config, customers, saved, store, web
from leadgen.models import Lead
from leadgen.scoring import score_lead

# (name, phone, address, city, zip) -> the keys both sides must make.
EXAMPLES = [
    (("Smith's Food & Drug #123", "+1 (801) 555-0101", "876 West Fortune Road, Suite 200",
      "Salt Lake City", "84104-1234"),
     {"phone:8015550101", "addr:876 w fortune rd|84104", "addrcity:876 w fortune rd|salt lake city",
      "name:smiths food and drug|84104", "namecity:smiths food and drug|salt lake city"}),
    (("The Café Rio, Inc.", "555-0101", "PO Box", "", ""), set()),
    (("Costco Wholesale", "801.555.0102", "11100 S. Auto Mall Dr.", "Sandy", ""),
     {"phone:8015550102", "addrcity:11100 s auto mall dr|sandy", "namecity:costco wholesale|sandy"}),
    (("ACME LLC", "", "", "", "84101"), {"name:acme|84101"}),
]


def _lead(name, i=0, **kw):
    kw.setdefault("city", "Salt Lake City")
    lead = Lead(name=name, lat=40.72 + i * 0.01, lon=-111.9, source="yelp", source_id=f"c{i}",
                raw_categories=["yelp:grocery"], yelp_reviews=200, **kw)
    score_lead(lead, config.DEFAULT_KEYWORDS)
    return lead


@pytest.mark.parametrize(("fields", "keys"), EXAMPLES)
def test_match_keys(fields, keys):
    assert customers.keys(*fields) == keys


def _fingerprints(rows):
    salt = customers.salt()
    return sorted({customers.fingerprint(salt, k) for row in rows for k in customers.keys(*row)})


def test_a_list_on_file_hides_the_leads_it_matches_everywhere_and_clear_brings_them_back():
    leads = [_lead("Smith's Marketplace", 0, phone="(801) 555-0100", address="100 Main St"),
             _lead("Costco Wholesale", 1, phone="(801) 555-0101", address="101 Main St", zip="84101"),
             _lead("Hampton Inn", 2, phone="(801) 555-0102", address="102 Main Street")]
    saved.save_search(leads, config.DEFAULT_KEYWORDS)
    client = web.create_app().test_client()
    assert client.get("/customers").get_json()["keys"] == 0
    # One customer by phone, one by address (written differently), one that isn't a lead.
    hashes = _fingerprints([("", "801-555-0100", "", "", ""),
                            ("", "", "102 Main Street, Suite 4", "salt lake city", ""),
                            ("Nobody Here", "801-555-0199", "", "", "")])
    answer = client.post("/customers", json={"hashes": hashes, "customers": 3, "by": "Dana"})
    assert answer.status_code == 200
    state = answer.get_json()
    assert (state["customers"], state["hidden"], state["by"]) == (3, 2, "Dana") and state["when"]
    names = [l["name"] for l in client.get("/leads").get_json()["leads"]]
    assert names == ["Costco Wholesale"]
    assert "Smith" not in client.get("/download/saved.csv").get_data(as_text=True)
    # Only fingerprints are kept: nothing of the customers themselves.
    with store.connect() as db:
        kept = [r[0] for r in db.all("SELECT hash FROM customer_keys")]
    assert sorted(kept) == hashes
    # Clear: every lead shows again.
    state = client.post("/customers/clear", json={"by": "Dana"}).get_json()
    assert (state["keys"], state["hidden"]) == (0, 0)
    assert len(client.get("/leads").get_json()["leads"]) == 3


def test_a_new_list_replaces_the_one_on_file_and_bad_lists_are_refused():
    saved.save_search([_lead("Hampton Inn", 0, phone="(801) 555-0102")], config.DEFAULT_KEYWORDS)
    client = web.create_app().test_client()
    first = _fingerprints([("", "801-555-0102", "", "", "")])
    assert client.post("/customers", json={"hashes": first, "customers": 1}).get_json()["hidden"] == 1
    other = _fingerprints([("", "801-555-0177", "", "", "")])
    assert client.post("/customers", json={"hashes": other, "customers": 1}).get_json()["hidden"] == 0
    for body in ({"hashes": ["801-555-0102"], "customers": 1}, {"hashes": [], "customers": 0},
                 {"hashes": "x", "customers": 1}, {"customers": 1}):
        assert client.post("/customers", json=body).status_code == 400
    assert client.post("/customers", json={"hashes": first, "customers": 1},
                       headers={"Origin": "https://elsewhere.example"}).status_code == 403


def test_the_salt_is_made_once_and_kept():
    assert customers.salt() == customers.salt()
    customers.clear("")
    assert len(customers.salt()) == 32


# ---------- in the browser ----------

@pytest.fixture
def tab(browser, site):  # noqa: F811
    leads = [_lead("Smith's Marketplace", 0, phone="(801) 555-0100", address="100 Main St", zip="84101"),
             _lead("Costco Wholesale", 1, phone="(801) 555-0101", address="101 Main St", zip="84101"),
             _lead("Hampton Inn", 2, phone="(801) 555-0102", address="102 Main St", zip="84101")]
    saved.save_search(leads, config.DEFAULT_KEYWORDS)
    context = _context(browser, viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors, sent = [], []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("request", lambda r: sent.append(r.post_data or "") if r.method == "POST" else None)
    page.goto(site + "#customers")
    page.wait_for_selector("#page-customers:not([hidden])")
    page.sent = sent
    yield page
    context.close()
    assert not errors


def test_the_page_makes_the_same_keys_and_fingerprints_as_the_site(tab):
    for (name, phone, address, city, zip_code), keys in EXAMPLES:
        made = tab.evaluate("(c) => customerKeys(c)", {"name": name, "phone": phone, "address": address,
                                                        "city": city, "zip": zip_code})
        assert set(made) == keys
    # A full address in one column gives its city and ZIP.
    made = tab.evaluate("customerKeys({address: '876 W. Fortune Rd, Salt Lake City, UT 84104'})")
    assert set(made) == {"addr:876 w fortune rd|84104", "addrcity:876 w fortune rd|salt lake city"}
    salt = customers.salt()
    assert tab.evaluate("([s, k]) => fingerprint(s, k)", [salt, "phone:8015550101"]) == \
        customers.fingerprint(salt, "phone:8015550101")


def _upload(tab, name, data, mime):
    tab.set_input_files("#cust-file", files=[{"name": name, "mimeType": mime, "buffer": data}])


def test_a_csv_hides_its_customers_without_sending_the_list(tab, site):  # noqa: F811
    csv = ("Customer Name,Phone Number,Street Address,City,Zip\n"
           '"Smith\'s Marketplace","801-555-0100","100 Main Street","Salt Lake City",84101\n'
           "Someone Else,(385) 555-0123,9 Elm Ave,Murray,84107\n")
    _upload(tab, "customers.csv", csv.encode(), "text/csv")
    tab.wait_for_selector("#cust-columns:not([hidden])")
    assert "Read 2 rows" in tab.inner_text("#cust-read")
    assert tab.input_value("#cust-col-name") == "0" and tab.input_value("#cust-col-phone") == "1"
    assert tab.input_value("#cust-col-address") == "2" and tab.input_value("#cust-col-zip") == "4"
    tab.click("#cust-send")
    tab.wait_for_function("document.getElementById('cust-summary').textContent.includes('2 customers on file')")
    assert "1 saved lead matches" in tab.inner_text("#cust-hidden")
    body = json.loads(next(s for s in tab.sent if "hashes" in s))
    assert set(body) == {"hashes", "customers", "by"} and body["customers"] == 2
    everything = " ".join(tab.sent).lower()
    for private in ("smith", "555-0100", "8015550100", "main", "someone", "elm", "84107"):
        assert private not in everything
    tab.goto(site + "#leads")
    tab.wait_for_selector("table.leads")
    assert "Smith" not in tab.inner_text("table.leads")
    assert "Costco" in tab.inner_text("table.leads")
    # Clearing brings it back.
    tab.goto(site + "#customers")
    tab.click("#cust-clear")
    tab.click("#cust-clear-go")
    tab.wait_for_function("document.getElementById('cust-summary').textContent.includes('No customer list')")
    tab.goto(site + "#leads")
    tab.wait_for_selector("table.leads")
    assert "Smith" in tab.inner_text("table.leads")


def test_an_excel_file_is_read_in_the_browser(tab):
    book = Workbook()
    sheet = book.active
    sheet.append(["Account", "Phone", "Address"])
    sheet.append(["Hampton Inn", 8015550102, "102 Main St, Salt Lake City, UT 84101"])
    sheet.append(["Nobody", None, "PO Box 4"])
    out = io.BytesIO()
    book.save(out)
    _upload(tab, "list.xlsx", out.getvalue(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    tab.wait_for_selector("#cust-columns:not([hidden])")
    assert "Read 2 rows" in tab.inner_text("#cust-read")
    assert "1 of 2 rows" in tab.inner_text("#cust-usable")
    tab.click("#cust-send")
    tab.wait_for_function("document.getElementById('cust-summary').textContent.includes('1 customer on file')")
    assert "1 saved lead matches" in tab.inner_text("#cust-hidden")
