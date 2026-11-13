import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db  # noqa: E402

GOOD_HTML = """<!doctype html>
<html lang="en"><head>
<title>Bright Smile Dental | Family dentist in Houston, Texas</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Friendly family dentist in Houston offering check-ups, whitening and emergency care.">
<meta property="og:title" content="Bright Smile Dental">
<link rel="icon" href="/icon.png">
<script type="application/ld+json">{"@type": "Dentist"}</script>
<script async src="https://www.googletagmanager.com/gtag/js?id=G-TEST"></script>
<script src="https://widget.intercom.io/widget/abc"></script>
</head><body>
<h1>Family dentist in Houston</h1>
<p>We look after families across Houston with gentle, modern dental care. Our team has
more than twenty years of experience and we welcome new patients every week of the year.
Call us today or book online to arrange your first visit with our friendly dentists.</p>
<a href="tel:+17135550142">Call +1 713-555-0142</a>
<a href="mailto:info@good.example?subject=Hello">Email us</a>
<a href="https://calendly.com/brightsmile">Book an appointment</a>
<a href="/contact">Contact</a> <a href="/services">Services</a>
<a href="https://www.facebook.com/brightsmile">Facebook</a>
<form action="/send"><input name="name"><input type="email" name="email"><textarea name="message"></textarea></form>
<footer>&copy; 2026 Bright Smile Dental</footer>
</body></html>"""

CONTACT_HTML = """<!doctype html><html lang="en"><head><title>Contact Bright Smile Dental Houston</title>
<meta name="viewport" content="width=device-width"></head><body><h1>Contact</h1>
<p>Write to frontdesk@good.example or to our manager.</p>
<a class="__cf_email__" data-cfemail="5439353a3533312614333b3b307a312c3539243831">[email&#160;protected]</a>
</body></html>"""

OLD_HTML = """<html><head><title>Home</title>
<script src="/js/jquery-1.8.3.min.js"></script>
</head><body>
<center><font size="5">Joe's Plumbing</font></center>
<p>Joe has been fixing pipes, boilers and drains in the local area for many years and is
always happy to give a free quote for any plumbing job, large or small, at short notice.
Please telephone the office during working hours and we will get back to you quickly.</p>
<img src="a.jpg"><img src="b.jpg"><img src="c.jpg"><img src="d.jpg"><img src="e.jpg">
<a href="/gone">Old offers</a> <a href="/prices">Prices</a>
<p>Email joe@old.example or sentry@sentry.io or logo@2x.png</p>
<p>Copyright 2015 Joe's Plumbing</p>
</body></html>"""

SPA_HTML = """<!doctype html><html><head><title>Loading application shell</title>
<meta name="viewport" content="width=device-width"><script src="/bundle.js"></script></head>
<body><div id="root"></div></body></html>"""


def site_handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    path = request.url.path
    html = {"content-type": "text/html; charset=utf-8"}

    if host == "places.googleapis.com":
        return places_handler(request)
    if host == "nominatim.openstreetmap.org":
        return nominatim_handler(request)
    if host in ("overpass-api.de", "overpass.private.coffee", "maps.mail.ru"):
        return overpass_handler(request)

    if host == "good.example":
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\nSitemap: https://good.example/sitemap.xml\n", headers={"content-type": "text/plain"})
        if path == "/":
            return httpx.Response(200, text=GOOD_HTML, headers=html)
        if path == "/contact":
            return httpx.Response(200, text=CONTACT_HTML, headers=html)
        if path == "/services":
            return httpx.Response(200, text="<html><body>Services</body></html>", headers=html)
        return httpx.Response(404, text="not found", headers=html)

    if host == "old.example":
        if request.url.scheme == "https":
            raise httpx.ConnectError("connection refused")
        if path == "/":
            return httpx.Response(200, text=OLD_HTML, headers=html)
        if path == "/prices":
            return httpx.Response(200, text="<html><body>Prices</body></html>", headers=html)
        return httpx.Response(404, text="not found", headers=html)

    if host == "blocked.example":
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n", headers={"content-type": "text/plain"})
        return httpx.Response(200, text=GOOD_HTML, headers=html)

    if host == "forbidden.example":
        return httpx.Response(403, text="denied", headers=html)

    if host == "spa.example":
        if path == "/":
            return httpx.Response(200, text=SPA_HTML, headers=html)
        return httpx.Response(404, text="not found", headers=html)

    if host == "badcert.example":
        if request.url.scheme == "https":
            raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired")
        if path == "/":
            return httpx.Response(200, text=GOOD_HTML, headers=html)
        return httpx.Response(404, text="not found", headers=html)

    if host == "slow.example":
        raise httpx.ReadTimeout("timed out")

    raise httpx.ConnectError("name not resolved")


PLACES_STATE = {"calls": [], "mode": "ok"}
OSM_STATE = {"nominatim": [], "overpass": [], "mode": "ok"}

OSM_ELEMENTS = [
    {"type": "node", "id": 1, "lat": 29.7, "lon": -95.4, "tags": {"name": "No Contact Dental", "amenity": "dentist"}},
    {"type": "way", "id": 2, "center": {"lat": 29.7, "lon": -95.4}, "tags": {
        "name": "Bright Smile Dental", "amenity": "dentist", "website": "https://good.example/",
        "contact:phone": "+1 713-555-0142;+1 713-555-0143", "email": "Hello@Good.example",
        "addr:housenumber": "1200", "addr:street": "Main St", "addr:city": "Houston", "addr:postcode": "77002"}},
    {"type": "node", "id": 3, "tags": {"name": "Phone Only Dental", "healthcare": "dentist", "phone": "+1 713-555-0100"}},
    {"type": "node", "id": 4, "tags": {"amenity": "dentist"}},
    {"type": "node", "id": 5, "tags": {"name": "Closed Dental", "disused:amenity": "dentist", "amenity": "dentist"}},
    {"type": "node", "id": 6, "tags": {"name": "Bright Smile Dental", "amenity": "dentist", "website": "https://good.example/"}},
]


def nominatim_handler(request: httpx.Request) -> httpx.Response:
    q = request.url.params.get("q", "")
    OSM_STATE["nominatim"].append({"q": q, "headers": dict(request.headers), "params": dict(request.url.params)})
    if "nowhere" in q.lower():
        return httpx.Response(200, json=[])
    if q.lower() == "texas":
        return httpx.Response(200, json=[{"boundingbox": ["25.8", "36.5", "-106.6", "-93.5"]}])
    if "main st" in q.lower():
        return httpx.Response(200, json=[{"boundingbox": ["29.7500", "29.7501", "-95.3601", "-95.3600"]}])
    return httpx.Response(200, json=[{"osm_type": "relation", "osm_id": 2688911, "display_name": "Houston, Texas",
                                      "boundingbox": ["29.52", "30.11", "-95.91", "-95.01"]}])


def overpass_handler(request: httpx.Request) -> httpx.Response:
    from urllib.parse import parse_qs
    query = parse_qs(request.content.decode())["data"][0]
    OSM_STATE["overpass"].append({"host": request.url.host, "query": query, "headers": dict(request.headers)})
    mode = OSM_STATE["mode"]
    if mode == "busy" or (mode == "main_busy" and request.url.host == "overpass-api.de"):
        return httpx.Response(429, text="rate limited")
    if mode == "refused":
        return httpx.Response(406, text="Not Acceptable")
    if mode == "heavy":
        return httpx.Response(200, json={"elements": [], "remark": "runtime error: Query timed out in \"query\" at line 3 after 46 seconds."})
    if mode == "empty":
        return httpx.Response(200, json={"elements": []})
    return httpx.Response(200, json={"elements": OSM_ELEMENTS})


def places_handler(request: httpx.Request) -> httpx.Response:
    import json
    body = json.loads(request.content)
    PLACES_STATE["calls"].append({"body": body, "headers": dict(request.headers)})
    if PLACES_STATE["mode"] == "bad_key":
        return httpx.Response(400, json={"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "details": [{"reason": "API_KEY_INVALID"}]}})
    if PLACES_STATE["mode"] == "disabled":
        return httpx.Response(403, json={"error": {"code": 403, "message": "Places API (New) has not been used in project 1 before or it is disabled."}})
    if body.get("pageToken") == "page2":
        return httpx.Response(200, json={"places": [
            {"id": "p4", "displayName": {"text": "Closed Shop"}, "businessStatus": "CLOSED_PERMANENTLY"},
            {"id": "p5", "displayName": {"text": "Blocked Dental"}, "websiteUri": "https://blocked.example/"},
        ]})
    return httpx.Response(200, json={"places": [
        {"id": "p1", "displayName": {"text": "Bright Smile Dental"}, "formattedAddress": "1200 Main St, Houston, TX",
         "websiteUri": "https://good.example/", "internationalPhoneNumber": "+1 713-555-0142", "rating": 4.6,
         "userRatingCount": 212, "googleMapsUri": "https://maps.google.com/?cid=1", "businessStatus": "OPERATIONAL",
         "primaryTypeDisplayName": {"text": "Dentist"}},
        {"id": "p2", "displayName": {"text": "Joe's Plumbing"}, "formattedAddress": "5 Side Rd, Houston, TX",
         "websiteUri": "http://old.example", "nationalPhoneNumber": "(713) 555-0199", "rating": 3.9, "userRatingCount": 8},
        {"id": "p3", "displayName": {"text": "No Site Barber"}, "formattedAddress": "9 High St, Houston, TX"},
    ], "nextPageToken": "page2"})


@pytest.fixture
def transport():
    PLACES_STATE["calls"].clear()
    PLACES_STATE["mode"] = "ok"
    OSM_STATE["nominatim"].clear()
    OSM_STATE["overpass"].clear()
    OSM_STATE["mode"] = "ok"
    return httpx.MockTransport(site_handler)


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    db.close()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "leads.db")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield
    db.close()
