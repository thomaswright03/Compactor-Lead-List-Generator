"""The Terms of use and Privacy policy: open without logging in, linked from every page,
and saying how the current customer import keeps the list private."""

from leadgen import web


def _client():
    return web.create_app(password="secret", username="Matt").test_client()


def test_terms_and_privacy_open_without_logging_in_and_cover_the_customer_import():
    client = _client()
    privacy = client.get("/privacy")
    assert privacy.status_code == 200
    text = privacy.get_data(as_text=True)
    assert "Privacy policy" in text and "never uploaded" in text and "fingerprint" in text
    assert "Clear the customer list" in text
    terms = client.get("/terms")
    assert terms.status_code == 200
    assert "Current customer import" in terms.get_data(as_text=True)


def test_every_page_links_to_them():
    client = _client()
    login = client.get("/login").get_data(as_text=True)
    assert 'href="/terms"' in login and 'href="/privacy"' in login
    missing = web.create_app().test_client().get("/no-such-page", headers={"Accept": "text/html"})
    missing = missing.get_data(as_text=True)
    assert 'href="/privacy"' in missing
