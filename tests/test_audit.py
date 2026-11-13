import asyncio

import httpx

from app.audit import analyze_html, audit_site, normalize_url, score_issues
from app.contacts import decode_cfemail, extract_emails, is_social_or_directory_url, registrable_domain
from bs4 import BeautifulSoup

from conftest import GOOD_HTML, OLD_HTML


def run(transport, url, lead=None):
    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            return await audit_site(client, url, lead)
    return asyncio.run(go())


def codes(result):
    return {i["code"] for i in result["audit"]["issues"]}


def test_normalize_url():
    assert normalize_url("example.com") == "https://example.com"
    assert normalize_url(" http://example.com/x ") == "http://example.com/x"
    assert normalize_url("ftp://example.com") is None
    assert normalize_url("not a site") is None
    assert normalize_url("") is None
    assert normalize_url("localhost") is None


def test_good_site_has_few_issues(transport):
    result = run(transport, "https://good.example/", {"rating": 4.6, "reviews": 212})
    assert result["audit_status"] == "done"
    found = codes(result)
    for unexpected in ("no_viewport", "no_https", "no_meta_description", "no_h1", "no_contact_form",
                       "no_booking", "no_chat", "no_analytics", "no_click_to_call", "old_markup",
                       "old_copyright", "no_sitemap", "no_favicon", "broken_links", "no_structured_data"):
        assert unexpected not in found, unexpected
    assert result["opportunity_score"] <= 15
    assert result["audit"]["https"] is True
    assert "https://good.example/contact" in result["audit"]["pages_checked"]
    assert "info@good.example" in result["emails"]
    assert "frontdesk@good.example" in result["emails"]
    assert "manager@good.example" in result["emails"]  # Cloudflare-protected address decoded
    assert result["audit"]["phones_found"] == ["+17135550142"]
    assert result["audit"]["social"] == {"facebook": "https://www.facebook.com/brightsmile"}
    assert any("212 Google reviews" in idea["detail"] for idea in result["audit"]["ideas"])


def test_old_site_is_a_strong_opportunity(transport):
    result = run(transport, "http://old.example", {"rating": 3.9, "reviews": 8})
    assert result["audit_status"] == "done"
    found = codes(result)
    for expected in ("no_https", "no_viewport", "old_markup", "old_copyright", "old_jquery", "short_title",
                     "no_meta_description", "no_h1", "images_no_alt", "no_contact_form", "no_click_to_call",
                     "no_booking", "no_chat", "no_analytics", "broken_links", "no_favicon", "no_sitemap"):
        assert expected in found, expected
    assert result["opportunity_score"] >= 80
    assert result["emails"] == ["joe@old.example"]
    assert set(result["services"]) == {"website", "app", "ai", "data"}
    assert len(result["top_issues"]) == 3
    severities = [i["severity"] for i in result["audit"]["issues"]]
    assert severities == sorted(severities, key=["high", "medium", "low"].index)
    broken = next(i for i in result["audit"]["issues"] if i["code"] == "broken_links")
    assert "1 of 2" in broken["detail"]


def test_https_only_given_but_site_answers_on_http(transport):
    result = run(transport, "old.example")
    assert result["audit_status"] == "done"
    assert "no_https" in codes(result)
    assert result["audit"]["https"] is False


def test_robots_disallow_is_respected(transport):
    result = run(transport, "https://blocked.example/")
    assert result["audit_status"] == "blocked"
    assert result["opportunity_score"] is None
    assert result["audit"]["issues"] == []


def test_refused_access_is_blocked_not_failed(transport):
    assert run(transport, "https://forbidden.example/")["audit_status"] == "blocked"


def test_no_website(transport):
    result = run(transport, None, {"rating": 4.8, "reviews": 90})
    assert result["audit_status"] == "no_website"
    assert result["opportunity_score"] == 85
    assert result["top_issues"] == ["No website"]
    assert result["services"][0] == "website"


def test_social_page_counts_as_no_website(transport):
    result = run(transport, "https://www.facebook.com/joesplumbing")
    assert result["audit_status"] == "no_website"
    assert "Facebook" in result["top_issues"][0]


def test_unreachable_site(transport):
    result = run(transport, "https://nowhere.example/")
    assert result["audit_status"] == "failed"
    assert result["audit"]["error"]
    assert "site_down" in codes(result)


def test_timeout_site(transport):
    result = run(transport, "https://slow.example/")
    assert result["audit_status"] == "failed"
    assert "too long" in result["audit"]["error"]


def test_broken_certificate_is_reported(transport):
    result = run(transport, "https://badcert.example/")
    assert result["audit_status"] == "done"
    assert "ssl_error" in codes(result)
    assert result["audit"]["https"] is False


def test_javascript_only_page(transport):
    result = run(transport, "https://spa.example/")
    found = codes(result)
    assert "js_rendered" in found
    assert "no_h1" not in found and "no_contact_form" not in found


def test_garbage_input(transport):
    assert run(transport, "not a website")["audit_status"] == "failed"


def test_analyze_html_facts():
    good = analyze_html(GOOD_HTML, "https://good.example/")
    assert good["viewport"] and good["has_booking"] and good["has_chat"] and good["has_analytics"]
    assert good["has_contact_form"] and good["h1_count"] == 1 and good["copyright_year"] == 2026
    assert good["contact_links"] == ["https://good.example/contact"]
    old = analyze_html(OLD_HTML, "http://old.example/")
    assert not old["viewport"] and old["old_tags"] == ["font", "center"]
    assert old["old_jquery"] == "1.8" and old["copyright_year"] == 2015
    assert old["images_missing_alt"] == 5


def test_score_is_capped():
    issues = [{"severity": "high"}] * 10
    assert score_issues(issues) == 100
    assert score_issues([]) == 0


def test_contact_helpers():
    assert registrable_domain("www.shop.example.co.uk") == "example.co.uk"
    assert registrable_domain("blog.example.com") == "example.com"
    assert decode_cfemail("54393114312c35392438317a373b39") == "me@example.com"
    soup = BeautifulSoup('<a href="mailto:Sales@Shop.example,owner@gmail.com">x</a> img@2x.png you@example.com', "html.parser")
    assert extract_emails(soup, "www.shop.example") == ["sales@shop.example", "owner@gmail.com"]
    assert is_social_or_directory_url("https://m.facebook.com/x") == "Facebook"
    assert is_social_or_directory_url("https://joes.example") is None


def test_search_words_map_to_directory_tags():
    from app.osm import build_overpass_query, tags_for
    assert tags_for("Dentists")[0] == [("amenity", "dentist"), ("healthcare", "dentist")]
    assert tags_for("barbers")[0] == [("shop", "hairdresser")]          # not also a bar
    assert tags_for("pet shops")[0] == [("shop", "pet")]
    assert tags_for("petrol stations")[0] == []                          # not a pet shop
    assert tags_for("bakeries") == ([("shop", "bakery")], "bakery")
    assert tags_for("veterinarians")[0] == [("amenity", "veterinary")]
    assert tags_for("real estate agents")[0] == [("office", "estate_agent")]
    # Whatever is typed cannot break out of the query
    hostile = build_overpass_query('x"];node(1);out;//', (1, 2, 3, 4), 10)
    assert '"];' not in hostile.replace('"](1.00000', "").replace('"."](', "") and "node(1)" not in hostile
