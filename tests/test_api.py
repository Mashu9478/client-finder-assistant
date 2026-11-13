import json
import time

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import create_app

from conftest import OSM_STATE, PLACES_STATE


@pytest.fixture
def client(transport):
    app = create_app(transport=transport)
    with TestClient(app) as test_client:
        yield test_client


def wait_for(client, path, done, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(path).json()
        if done(data):
            return data
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting on {path}: {data}")


def save_key(client):
    assert client.put("/api/settings", json={"google_api_key": "test-key"}).status_code == 200


def test_settings_never_return_keys(client):
    first = client.get("/api/settings").json()
    assert first["has_google_key"] is False and first["has_claude_key"] is False
    assert len(first["services"]) == 4
    saved = client.put("/api/settings", json={
        "your_name": "  Test User ", "business_name": "Studio", "email": "a@b.example", "whatsapp": "+10000000000",
        "portfolio_url": "https://studio.example", "services": ["Web", "", "AI"],
        "portfolio": [{"name": "Site", "description": "A site", "url": "https://x.example"}, {"junk": 1}],
        "use_pagespeed": True, "google_api_key": "secret-google", "claude_api_key": "secret-claude",
    }).json()
    assert saved["your_name"] == "Test User" and saved["services"] == ["Web", "AI"]
    assert saved["portfolio"] == [{"name": "Site", "description": "A site", "url": "https://x.example"}]
    assert saved["has_google_key"] and saved["has_claude_key"] and saved["use_pagespeed"] is True
    assert "secret" not in json.dumps(saved)
    # An empty key field keeps the saved key
    again = client.put("/api/settings", json={"google_api_key": "", "your_name": "Other"}).json()
    assert again["has_google_key"] is True and again["your_name"] == "Other"
    assert json.loads(config.SETTINGS_PATH.read_text())["google_api_key"] == "secret-google"


def test_free_search_without_a_key(client):
    assert client.get("/api/settings").json()["search_source"] == "openstreetmap"
    # The free search cannot cover the whole world: it needs a place
    resp = client.post("/api/searches", json={"query": "dentists"})
    assert resp.status_code == 400 and "needs a place" in resp.json()["detail"]

    search = client.post("/api/searches", json={"query": "Dentists", "location": "Houston, Texas", "max_results": 10}).json()
    done = wait_for(client, f"/api/searches/{search['id']}", lambda s: s["status"] in ("done", "error"))
    assert done["status"] == "done", done
    assert done["found"] == 3 and done["audited"] == 3  # unnamed, disused and duplicate entries dropped
    assert PLACES_STATE["calls"] == []  # Google was never contacted

    geocode = OSM_STATE["nominatim"][0]
    assert geocode["q"] == "Houston, Texas" and geocode["params"]["format"] == "jsonv2"
    assert "ClientFinderAssistant" in geocode["headers"]["user-agent"]
    overpass = OSM_STATE["overpass"][0]
    assert overpass["host"] == "overpass-api.de"
    assert 'nwr["amenity"="dentist"]["name"](29.52000,-95.91000,30.11000,-95.01000);' in overpass["query"]
    assert "ClientFinderAssistant" in overpass["headers"]["user-agent"]

    leads = client.get("/api/leads", params={"search_id": search["id"], "sort": "newest"}).json()
    by_name = {l["name"]: l for l in leads}
    assert set(by_name) == {"Bright Smile Dental", "Phone Only Dental", "No Contact Dental"}
    bright = by_name["Bright Smile Dental"]
    assert bright["source"] == "openstreetmap" and bright["audit_status"] == "done"
    assert bright["phone"] == "+1 713-555-0142" and bright["category"] == "Dentist"
    assert bright["address"] == "1200 Main St, Houston, 77002"
    assert bright["maps_url"] == "https://www.openstreetmap.org/way/2"
    assert bright["emails"][0] == "hello@good.example" and "info@good.example" in bright["emails"]
    assert bright["rating"] is None
    # A blank website in the free directory is only a hint, so it is scored cautiously
    unlisted = by_name["Phone Only Dental"]
    assert unlisted["audit_status"] == "no_website" and unlisted["opportunity_score"] == 45
    assert unlisted["top_issues"] == ["No website listed (check first)"]
    # The entry with a website came first, the one with nothing last
    assert [l["name"] for l in leads][0] == "No Contact Dental" and bright["id"] < unlisted["id"] < by_name["No Contact Dental"]["id"]

    # With a Google key saved, Google is used instead
    save_key(client)
    assert client.get("/api/settings").json()["search_source"] == "google"
    again = client.post("/api/searches", json={"query": "dentists"}).json()
    wait_for(client, f"/api/searches/{again['id']}", lambda s: s["status"] in ("done", "error"))
    assert len(PLACES_STATE["calls"]) >= 1 and len(OSM_STATE["overpass"]) == 1


@pytest.mark.parametrize("location,mode,phrase", [
    ("Nowhereville", "ok", "Could not find a place"),
    ("Texas", "ok", "too large an area"),
    ("Houston, Texas", "busy", "busy right now"),
    ("Houston, Texas", "empty", "lists no"),
])
def test_free_search_problems_are_explained(client, location, mode, phrase):
    OSM_STATE["mode"] = mode
    search = client.post("/api/searches", json={"query": "dentists", "location": location}).json()
    done = wait_for(client, f"/api/searches/{search['id']}", lambda s: s["status"] in ("done", "error"))
    assert done["status"] == "error" and phrase in done["error"], done


def test_free_search_falls_back_to_mirror_and_widens_an_address(client):
    OSM_STATE["mode"] = "main_busy"
    search = client.post("/api/searches", json={"query": "dentists", "location": "1200 Main St, Houston"}).json()
    done = wait_for(client, f"/api/searches/{search['id']}", lambda s: s["status"] in ("done", "error"))
    assert done["status"] == "done" and done["found"] == 3
    assert [c["host"] for c in OSM_STATE["overpass"]] == ["overpass-api.de", "overpass.private.coffee"]
    assert "(29.71005,-95.40005,29.79005,-95.32005)" in OSM_STATE["overpass"][1]["query"]


def test_full_search_flow(client):
    save_key(client)
    resp = client.post("/api/searches", json={"query": "dentists", "location": "Houston, Texas", "max_results": 40})
    assert resp.status_code == 201
    search = resp.json()
    assert search["status"] in ("queued", "finding", "auditing", "done")
    done = wait_for(client, f"/api/searches/{search['id']}", lambda s: s["status"] in ("done", "error"))
    assert done["status"] == "done", done
    assert done["found"] == 4 and done["audited"] == 4  # closed shop skipped

    first_call = PLACES_STATE["calls"][0]
    assert first_call["body"]["textQuery"] == "dentists in Houston, Texas"
    assert first_call["headers"]["x-goog-api-key"] == "test-key"
    assert "places.websiteUri" in first_call["headers"]["x-goog-fieldmask"]
    assert PLACES_STATE["calls"][1]["body"]["pageToken"] == "page2"

    leads = client.get("/api/leads", params={"search_id": search["id"]}).json()
    by_name = {l["name"]: l for l in leads}
    assert set(by_name) == {"Bright Smile Dental", "Joe's Plumbing", "No Site Barber", "Blocked Dental"}
    assert by_name["Joe's Plumbing"]["audit_status"] == "done"
    assert by_name["Joe's Plumbing"]["opportunity_score"] >= 80
    assert by_name["Joe's Plumbing"]["emails"] == ["joe@old.example"]
    assert by_name["Joe's Plumbing"]["phone"] == "(713) 555-0199"
    assert by_name["No Site Barber"]["audit_status"] == "no_website"
    assert by_name["Blocked Dental"]["audit_status"] == "blocked"
    assert by_name["Bright Smile Dental"]["category"] == "Dentist"
    assert "audit" not in by_name["Bright Smile Dental"]
    # Default sort: best opportunity first, unscored last
    scores = [l["opportunity_score"] for l in leads]
    assert scores[0] == max(s for s in scores if s is not None) and scores[-1] is None

    joe_id = by_name["Joe's Plumbing"]["id"]
    detail = client.get(f"/api/leads/{joe_id}").json()
    assert detail["audit"]["issues"] and detail["audit"]["ideas"] and detail["audit"]["error"] is None
    assert set(detail["audit"]["issues"][0]) == {"code", "severity", "title", "detail", "service"}

    # Filters
    assert {l["name"] for l in client.get("/api/leads", params={"has_email": "true"}).json()} == {"Bright Smile Dental", "Joe's Plumbing"}
    assert [l["name"] for l in client.get("/api/leads", params={"has_website": "false"}).json()] == ["No Site Barber"]
    assert len(client.get("/api/leads", params={"has_website": "true"}).json()) == 3
    assert [l["name"] for l in client.get("/api/leads", params={"q": "plumb"}).json()] == ["Joe's Plumbing"]
    assert len(client.get("/api/leads", params={"min_score": 80}).json()) == 2
    assert [l["name"] for l in client.get("/api/leads", params={"sort": "name"}).json()][0] == "Blocked Dental"
    assert client.get("/api/leads", params={"sort": "bogus"}).status_code == 422
    assert client.get("/api/leads", params={"status": "bogus"}).status_code == 400

    stats = client.get("/api/stats").json()
    assert stats["total"] == 4 and stats["with_email"] == 2 and stats["with_phone"] == 2
    assert stats["no_website"] == 1 and stats["high_opportunity"] == 2 and stats["by_status"]["new"] == 4

    # Pipeline status and notes
    lead_id = by_name["Joe's Plumbing"]["id"]
    patched = client.patch(f"/api/leads/{lead_id}", json={"status": "shortlisted", "notes": "Call Monday"}).json()
    assert patched["status"] == "shortlisted" and patched["notes"] == "Call Monday" and patched["audit"]
    assert client.patch(f"/api/leads/{lead_id}", json={"status": "nonsense"}).status_code == 422
    assert [l["name"] for l in client.get("/api/leads", params={"status": "shortlisted"}).json()] == ["Joe's Plumbing"]

    # CSV
    csv_resp = client.get("/api/leads/export.csv", params={"status": "shortlisted"})
    assert csv_resp.status_code == 200 and "attachment" in csv_resp.headers["content-disposition"]
    lines = csv_resp.text.lstrip("﻿").strip().splitlines()
    assert lines[0].startswith("Business,Category,Opportunity score")
    assert len(lines) == 2 and "Joe's Plumbing" in lines[1] and "Call Monday" in lines[1]

    # Running the same search again does not duplicate leads or lose the user's work
    again = client.post("/api/searches", json={"query": "dentists", "location": "Houston, Texas", "max_results": 40}).json()
    wait_for(client, f"/api/searches/{again['id']}", lambda s: s["status"] == "done")
    assert client.get("/api/stats").json()["total"] == 4
    assert client.get(f"/api/leads/{lead_id}").json()["status"] == "shortlisted"
    assert len(client.get("/api/leads", params={"search_id": again["id"]}).json()) == 4

    # Re-audit and delete
    assert client.post(f"/api/leads/{lead_id}/reaudit").json()["audit_status"] in ("pending", "running", "done")
    wait_for(client, f"/api/leads/{lead_id}", lambda l: l["audit_status"] == "done")
    assert client.delete(f"/api/leads/{lead_id}").status_code == 204
    assert client.get(f"/api/leads/{lead_id}").status_code == 404
    assert client.delete(f"/api/leads/{lead_id}").status_code == 404
    assert client.get("/api/stats").json()["total"] == 3


def test_max_results_is_honoured(client):
    save_key(client)
    search = client.post("/api/searches", json={"query": "dentists", "max_results": 2}).json()
    done = wait_for(client, f"/api/searches/{search['id']}", lambda s: s["status"] in ("done", "error"))
    assert done["found"] == 2
    assert PLACES_STATE["calls"][0]["body"]["textQuery"] == "dentists"
    assert len(PLACES_STATE["calls"]) == 1


@pytest.mark.parametrize("mode,phrase", [("bad_key", "rejected the API key"), ("disabled", "not switched on")])
def test_google_errors_are_explained(client, mode, phrase):
    save_key(client)
    PLACES_STATE["mode"] = mode
    search = client.post("/api/searches", json={"query": "dentists"}).json()
    done = wait_for(client, f"/api/searches/{search['id']}", lambda s: s["status"] in ("done", "error"))
    assert done["status"] == "error" and phrase in done["error"]
    test = client.post("/api/settings/test-google").json()
    assert test["ok"] is False and phrase in test["message"]


def test_test_google_key(client):
    assert client.post("/api/settings/test-google").json()["ok"] is False
    save_key(client)
    assert client.post("/api/settings/test-google").json() == {"ok": True, "message": "Google Places key works."}


def test_manual_lead(client):
    resp = client.post("/api/leads", json={"website": "good.example", "name": "My prospect"})
    assert resp.status_code == 201
    lead = resp.json()
    assert lead["source"] == "manual" and lead["name"] == "My prospect" and lead["website"] == "https://good.example"
    done = wait_for(client, f"/api/leads/{lead['id']}", lambda l: l["audit_status"] not in ("pending", "running"))
    assert done["audit_status"] == "done" and done["phone"] == "+17135550142"
    # Same site again reuses the lead
    again = client.post("/api/leads", json={"website": "https://www.good.example/about"}).json()
    assert again["id"] == lead["id"]
    assert client.post("/api/leads", json={"website": "not a site"}).status_code == 400
    assert client.post("/api/leads", json={}).status_code == 422
    unnamed = client.post("/api/leads", json={"website": "spa.example"}).json()
    assert unnamed["name"] == "spa.example"


def test_validation_and_missing(client):
    resp = client.post("/api/searches", json={"query": "", "max_results": 500})
    assert resp.status_code == 422 and isinstance(resp.json()["detail"], str)
    assert client.get("/api/searches/999").status_code == 404
    assert client.get("/api/leads/999").status_code == 404
    assert client.get("/api/nope").status_code == 404
    assert client.get("/api/searches").json() == []


def test_only_local_requests_are_served(client):
    assert client.get("/api/stats", headers={"host": "evil.example"}).status_code == 403
    assert client.put("/api/settings", json={"your_name": "x"}, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.put("/api/settings", json={"your_name": "x"}, headers={"origin": "http://testserver"}).status_code == 200


def test_csv_formula_text_is_neutralised(client):
    lead = client.post("/api/leads", json={"website": "spa.example", "name": "=HYPERLINK(\"http://x\")"}).json()
    wait_for(client, f"/api/leads/{lead['id']}", lambda l: l["audit_status"] not in ("pending", "running"))
    text = client.get("/api/leads/export.csv").text
    assert "'=HYPERLINK" in text


def test_interrupted_work_recovers_on_restart(transport):
    from app import db
    db.connect()
    search = db.create_search("dentists", "", 20)
    db.set_search_status(search["id"], "finding")
    lead, _ = db.create_manual_lead("https://good.example", None)
    db.set_audit_status(lead["id"], "running")
    db.close()
    with TestClient(create_app(transport=transport)) as client:
        assert client.get(f"/api/searches/{search['id']}").json()["status"] == "error"
        done = wait_for(client, f"/api/leads/{lead['id']}", lambda l: l["audit_status"] == "done")
        assert done["opportunity_score"] is not None
