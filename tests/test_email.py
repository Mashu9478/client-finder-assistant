import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.emailer import OPT_OUT, EmailError, check_ready, parse_claude_text, write_with_template
from app.main import create_app

from conftest import CLAUDE_STATE
from test_api import save_key, wait_for

SENDER = {
    "your_name": "Test Person", "business_name": "Studio", "email": "me@studio.example",
    "whatsapp": "+10000000000", "portfolio_url": "https://studio.example",
}


@pytest.fixture
def client(transport):
    with TestClient(create_app(transport=transport)) as test_client:
        yield test_client


def google_leads(client):
    """Run the simulated Google search and return its leads by name."""
    save_key(client)
    search = client.post("/api/searches", json={"query": "dentists", "location": "Houston", "max_results": 40}).json()
    wait_for(client, f"/api/searches/{search['id']}", lambda s: s["status"] in ("done", "error"))
    return {l["name"]: l for l in client.get("/api/leads").json()}


def test_template_email_is_specific_and_complete(client):
    client.put("/api/settings", json=SENDER)
    leads = google_leads(client)
    joe = leads["Joe's Plumbing"]
    assert joe["has_draft"] is False and client.get(f"/api/leads/{joe['id']}").json()["email"] is None
    assert client.get("/api/settings").json()["email_writer"] == "template"

    resp = client.post(f"/api/leads/{joe['id']}/email")
    assert resp.status_code == 200
    email = resp.json()["email"]
    assert email["source"] == "template" and email["note"] is None and email["edited"] is False
    assert email["to"] == "joe@old.example" and email["generated_at"]
    assert "Joe's Plumbing" in email["subject"]
    body = email["body"]
    assert body.startswith("Hello Joe's Plumbing team,")
    assert "I'm Test Person from Studio." in body and "old.example" in body
    assert "Not secure" in body and "phone screens" in body          # the highest-severity findings, in plain words
    assert "<font>" not in body and "well over a decade ago" in body # technical detail reworded for an owner
    assert "easy for you to update" in body and "for them" not in body  # the fix is addressed to the owner
    assert "https://studio.example" in body and "WhatsApp at +10000000000" in body
    assert "Test Person\nStudio\nme@studio.example | WhatsApp: +10000000000" in body
    assert body.rstrip().endswith(OPT_OUT)
    assert not any(word in body.lower() for word in ("$", "price", "discount", "guarantee"))
    assert CLAUDE_STATE["calls"] == []                                # no key, so Claude was never contacted

    # Saved with the lead, shown in the list, filterable, and in the export
    assert client.get(f"/api/leads/{joe['id']}").json()["email"]["body"] == body
    assert [l["name"] for l in client.get("/api/leads", params={"has_draft": "true"}).json()] == ["Joe's Plumbing"]
    csv_text = client.get("/api/leads/export.csv", params={"has_draft": "true"}).text
    assert "Email subject,Email body" in csv_text and "Hello Joe's Plumbing team" in csv_text

    # A site with nothing wrong is told so; no problem is invented for it
    bright = client.post(f"/api/leads/{leads['Bright Smile Dental']['id']}/email").json()["email"]
    assert bright["subject"] == "An idea for the Bright Smile Dental website"
    assert "it is in good shape" in bright["body"] and "costing you enquiries" not in bright["body"]
    assert "Featuring your Google reviews" in bright["body"] and "\n\n\n" not in bright["body"]

    # A business with a good rating gets a truthful compliment using Google's own numbers
    rated = write_with_template({"name": "Rated Co", "website": "https://rated.example", "rating": 4.8, "reviews": 90,
        "audit_status": "done", "audit": {"issues": [{"code": "no_viewport", "severity": "high", "title": "Not mobile-friendly", "detail": "x"}], "ideas": []}}, SENDER)
    assert "90 Google reviews averaging 4.8" in rated["body"] and "noticed something that is probably costing" in rated["body"]


def test_template_wording_matches_the_situation(client):
    client.put("/api/settings", json=SENDER)
    leads = google_leads(client)
    barber = client.post(f"/api/leads/{leads['No Site Barber']['id']}/email").json()["email"]
    assert barber["to"] is None and barber["subject"] == "A website for No Site Barber"
    assert "you have no website at the moment" in barber["body"]

    # The free directory's blank website is only a hint, so the email must not assert it
    lead = {"name": "Phone Only Dental", "audit_status": "no_website", "source": "openstreetmap", "audit": {
        "issues": [{"code": "no_website_listed", "severity": "medium", "title": "No website listed (check first)", "detail": "x"}],
        "ideas": []}}
    hedged = write_with_template(lead, SENDER)
    assert "could not find a website listed" in hedged["body"] and "If you have one and I missed it" in hedged["body"]
    assert "you have no website" not in hedged["body"]

    down = {"name": "Offline Cafe", "website": "https://offline.example", "audit_status": "failed", "audit": {
        "issues": [{"code": "site_down", "severity": "high", "title": "Website is not loading", "detail": "x"}], "ideas": []}}
    assert "did not load for me" in write_with_template(down, SENDER)["body"]


def test_email_needs_findings_a_finished_check_and_a_name(client):
    leads = google_leads(client)
    joe_id = leads["Joe's Plumbing"]["id"]
    resp = client.post(f"/api/leads/{joe_id}/email")                  # no name saved yet
    assert resp.status_code == 400 and "Add your name in Settings" in resp.json()["detail"]
    client.put("/api/settings", json=SENDER)
    resp = client.post(f"/api/leads/{leads['Blocked Dental']['id']}/email")
    assert resp.status_code == 409 and "no findings" in resp.json()["detail"]
    assert client.post("/api/leads/999/email").status_code == 404
    with pytest.raises(EmailError) as info:
        check_ready({"audit_status": "running", "audit": None}, SENDER)
    assert info.value.status == 409 and "still running" in str(info.value)


def test_claude_writes_the_email_when_a_key_is_saved(client):
    client.put("/api/settings", json={**SENDER, "claude_api_key": "sk-test", "claude_model": "claude-haiku-5-5"})
    settings = client.get("/api/settings").json()
    assert settings["email_writer"] == "claude" and settings["claude_model"] == "claude-haiku-5-5"
    joe_id = google_leads(client)["Joe's Plumbing"]["id"]

    email = client.post(f"/api/leads/{joe_id}/email").json()["email"]
    assert email["source"] == "claude" and email["note"] is None
    assert email["subject"] == "Joe's Plumbing on phones"
    assert email["body"].startswith("Hello Joe's Plumbing team,")
    assert "private reasoning" not in email["body"] and "```" not in email["body"]
    assert email["body"].rstrip().endswith(OPT_OUT)                   # added because Claude left it out

    call = CLAUDE_STATE["calls"][0]
    assert call["headers"]["x-api-key"] == "sk-test" and call["headers"]["anthropic-version"] == "2023-06-01"
    sent = call["body"]
    assert sent["model"] == "claude-haiku-5-5" and sent["max_tokens"] >= 2000
    assert "temperature" not in sent and "top_p" not in sent          # current models reject these
    assert "Never state or hint at prices" in sent["system"]
    data = sent["messages"][0]["content"]
    assert "Joe's Plumbing" in data and "Not mobile-friendly" in data and "old.example" in data
    assert "+10000000000" in data and "sk-test" not in data and "me@studio.example | WhatsApp" in data


@pytest.mark.parametrize("mode,phrase", [
    ("bad_key", "Claude rejected the API key"),
    ("no_credit", "no credit left"),
    ("garbled", "not in the expected form"),
])
def test_claude_failure_falls_back_to_the_template(client, mode, phrase):
    client.put("/api/settings", json={**SENDER, "claude_api_key": "sk-test"})
    joe_id = google_leads(client)["Joe's Plumbing"]["id"]
    CLAUDE_STATE["mode"] = mode
    email = client.post(f"/api/leads/{joe_id}/email").json()["email"]
    assert email["source"] == "template" and phrase in email["note"] and "standard wording" in email["note"]
    assert email["body"].startswith("Hello Joe's Plumbing team,")


def test_editing_rewriting_and_discarding_a_draft(client):
    client.put("/api/settings", json=SENDER)
    joe_id = google_leads(client)["Joe's Plumbing"]["id"]
    original = client.post(f"/api/leads/{joe_id}/email").json()["email"]

    same = client.patch(f"/api/leads/{joe_id}", json={"email_subject": original["subject"], "email_body": original["body"]}).json()["email"]
    assert same["edited"] is False                                    # saving unchanged text is not an edit
    edited = client.patch(f"/api/leads/{joe_id}", json={"email_body": "My own words.\nSecond line."}).json()["email"]
    assert edited["edited"] is True and edited["body"] == "My own words.\nSecond line." and edited["subject"] == original["subject"]
    assert edited["source"] == "template"
    retitled = client.patch(f"/api/leads/{joe_id}", json={"email_subject": "New subject", "status": "contacted"}).json()
    assert retitled["email"]["subject"] == "New subject" and retitled["email"]["body"].startswith("My own words")
    assert retitled["status"] == "contacted"

    rewritten = client.post(f"/api/leads/{joe_id}/email").json()["email"]
    assert rewritten["edited"] is False and rewritten["body"] == original["body"]

    assert client.delete(f"/api/leads/{joe_id}/email").status_code == 204
    after = client.get(f"/api/leads/{joe_id}").json()
    assert after["email"] is None and after["has_draft"] is False
    assert client.delete("/api/leads/999/email").status_code == 404

    # Typing an email with no generated draft creates one marked as the user's own
    manual = client.patch(f"/api/leads/{joe_id}", json={"email_subject": "Hi", "email_body": "Typed by hand."}).json()["email"]
    assert manual["source"] == "manual" and manual["edited"] is False
    assert client.patch(f"/api/leads/{joe_id}", json={"email_body": "x" * 20001}).status_code == 422


def test_parse_claude_text():
    assert parse_claude_text("Subject: \"Hello\"\n\n" + "Body text. " * 6) == {"subject": "Hello", "body": ("Body text. " * 6).strip()}
    with pytest.raises(EmailError):
        parse_claude_text("No subject line here, just a long body of text that goes on and on.")
    with pytest.raises(EmailError):
        parse_claude_text("Subject: Only a subject")


def test_existing_lead_file_gains_the_email_columns(transport):
    """Someone who installed the first version keeps their leads when they update."""
    old = sqlite3.connect(config.DB_PATH)
    old.executescript("""
        CREATE TABLE searches (id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT NOT NULL, location TEXT NOT NULL DEFAULT '',
            max_results INTEGER NOT NULL DEFAULT 20, status TEXT NOT NULL DEFAULT 'queued', error TEXT, created_at TEXT NOT NULL);
        CREATE TABLE leads (id INTEGER PRIMARY KEY AUTOINCREMENT, search_id INTEGER, place_id TEXT UNIQUE, domain TEXT,
            name TEXT NOT NULL, category TEXT, address TEXT, website TEXT, phone TEXT, emails TEXT NOT NULL DEFAULT '[]',
            rating REAL, reviews INTEGER, maps_url TEXT, source TEXT NOT NULL DEFAULT 'google_places',
            status TEXT NOT NULL DEFAULT 'new', audit_status TEXT NOT NULL DEFAULT 'pending', opportunity_score INTEGER,
            top_issues TEXT NOT NULL DEFAULT '[]', services TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '',
            audit TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE search_leads (search_id INTEGER NOT NULL, lead_id INTEGER NOT NULL, PRIMARY KEY (search_id, lead_id));
        INSERT INTO leads (name, status, audit_status, notes, created_at, updated_at)
            VALUES ('Kept Lead', 'shortlisted', 'no_website', 'my note', '2026-11-13T00:00:00Z', '2026-11-13T00:00:00Z');
    """)
    old.commit(); old.close()
    with TestClient(create_app(transport=transport)) as client:
        lead = client.get("/api/leads/1").json()
        assert lead["name"] == "Kept Lead" and lead["status"] == "shortlisted" and lead["notes"] == "my note"
        assert lead["has_draft"] is False and lead["email"] is None
        client.patch("/api/leads/1", json={"email_subject": "s", "email_body": "b"})
        assert client.get("/api/leads/1").json()["email"]["body"] == "b"
