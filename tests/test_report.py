import os
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app import config, render
from app.audit import result_no_website
from app.main import create_app
from app.render import RenderError
from app.report import AREA_OF, FIX, build_context

from conftest import CLAUDE_STATE
from test_api import save_key, wait_for
from test_email import SENDER, google_leads

CHROMIUM = os.environ.get("CFA_BROWSER") or "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"
HAS_BROWSER = os.path.isfile(CHROMIUM) or bool(render.find_browser())
HAS_TEMPLATES = (render.TEMPLATE_DIR / "report.html.j2").is_file() and (render.TEMPLATE_DIR / "concept.html.j2").is_file()


@pytest.fixture
def client(transport):
    with TestClient(create_app(transport=transport)) as test_client:
        yield test_client


@pytest.fixture
def fake_renderer(monkeypatch):
    """Stand in for the browser: write small files and remember what was asked for."""
    calls = []

    def fake(context, out_dir):
        calls.append(context)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.pdf").write_bytes(b"%PDF-1.4 fake report for " + context["lead"]["name"].encode())
        (out_dir / "concept.png").write_bytes(b"\x89PNG fake picture")
        return {"pdf": out_dir / "report.pdf", "png": out_dir / "concept.png"}

    monkeypatch.setattr(main_module, "make_report_files", fake)
    return calls


def test_every_finding_has_an_area_and_a_fix():
    assert set(AREA_OF) == set(FIX)


def test_report_context_for_a_weak_site(client):
    client.put("/api/settings", json=SENDER)
    joe = client.get(f"/api/leads/{google_leads(client)['Joe\x27s Plumbing']['id']}").json()
    context = build_context(joe, client.get("/api/settings").json())

    assert context["lead"]["name"] == "Joe's Plumbing" and context["lead"]["website"] == "old.example"
    assert context["sender"]["byline"] == "Test Person · Studio"
    summary = context["summary"]
    assert summary["site_read"] is True and summary["counts"]["high"] >= 3
    assert summary["total_findings"] == sum(summary["counts"].values())
    assert "score" not in " ".join(summary)                               # no invented grade
    areas = {a["key"]: a for a in context["areas"]}
    assert [a["key"] for a in context["areas"]] == [k for k in ("presence", "security", "speed", "mobile", "design", "search", "conversion", "measurement", "upkeep") if k in areas]
    assert areas["security"]["status"] == "urgent" and areas["security"]["findings"][0]["title"] == "No secure connection (HTTPS)"
    assert areas["security"]["findings"][0]["fix"].startswith("Move the site to HTTPS")
    assert areas["mobile"]["findings"][0]["severity_label"] == "High priority"
    assert areas["speed"]["status"] == "good" and areas["speed"]["passed"] == ["Server responds quickly"] and not areas["speed"]["findings"]
    assert all(f["fix"] for a in context["areas"] for f in a["findings"])
    assert not any("<font>" in f["what"] for a in context["areas"] for f in a["findings"])   # no markup jargon for the owner
    titles = [r["title"] for r in context["recommendations"]]
    assert "Modern, mobile-first redesign" in titles and "Online booking" in titles
    assert all("their" not in r["detail"] and "them" not in r["detail"].split() for r in context["recommendations"])
    concept = context["concept"]
    assert concept["business_name"] == "Joe's Plumbing" and concept["initials"] == "JP"
    assert concept["headline"] == "Reliable work, done properly"          # generic sample wording for a plumber
    assert concept["rating"] is None and concept["features"]["reviews"] is False   # a 3.9 rating is not put on show
    assert concept["features"]["booking"] is True and concept["features"]["tap_to_call"] is True
    assert "Concept only" in concept["label"]


def test_report_context_without_a_website():
    found = result_no_website({"rating": 4.8, "reviews": 90})
    lead = {"name": "No Site Barber", "category": "Barber shop", "phone": "+1 713-555-0111", "rating": 4.8, "reviews": 90,
            "audit_status": "no_website", "audit": found["audit"]}
    context = build_context(lead, SENDER)
    assert context["summary"]["site_read"] is False and "no working website" in context["summary"]["headline"]
    assert [a["key"] for a in context["areas"]] == ["presence"] and context["areas"][0]["passed"] == []
    assert context["concept"]["rating"] == 4.8 and context["concept"]["features"]["reviews"] is True

    hedged = result_no_website({"source": "openstreetmap"})
    context = build_context({"name": "Unlisted Co", "audit_status": "no_website", "source": "openstreetmap", "audit": hedged["audit"]}, SENDER)
    assert context["summary"]["headline"] == "We could not find a website listed for the business."
    finding = context["areas"][0]["findings"][0]                           # worded for the owner, not the freelancer
    assert finding["title"] == "No website found" and "search for them" not in finding["what"] and "please disregard" in finding["what"]
    assert context["concept"]["features"]["tap_to_call"] is False and context["lead"]["phone"] == ""


def test_report_endpoints(client, fake_renderer):
    client.put("/api/settings", json=SENDER)
    leads = google_leads(client)
    joe_id = leads["Joe's Plumbing"]["id"]
    assert leads["Joe's Plumbing"]["has_report"] is False
    assert client.get(f"/api/leads/{joe_id}/report.pdf").status_code == 404
    assert client.get(f"/api/leads/{joe_id}/concept.png").status_code == 404

    created = client.post(f"/api/leads/{joe_id}/report")
    assert created.status_code == 200
    report = created.json()["report"]
    assert report["pdf_url"] == f"/api/leads/{joe_id}/report.pdf" and report["image_url"] == f"/api/leads/{joe_id}/concept.png"
    assert report["generated_at"] and fake_renderer[0]["lead"]["name"] == "Joe's Plumbing"
    assert [l["name"] for l in client.get("/api/leads").json() if l["has_report"]] == ["Joe's Plumbing"]

    pdf = client.get(report["pdf_url"])
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
    from urllib.parse import unquote
    assert pdf.headers["content-disposition"].startswith("attachment")
    assert "Joe's Plumbing - website review.pdf" in unquote(pdf.headers["content-disposition"])
    assert client.get(report["pdf_url"], params={"inline": "true"}).headers["content-disposition"].startswith("inline")
    png = client.get(report["image_url"])
    assert png.status_code == 200 and png.headers["content-type"] == "image/png" and png.headers["content-disposition"].startswith("inline")
    assert client.get(report["image_url"], params={"download": "true"}).headers["content-disposition"].startswith("attachment")
    assert (config.report_dir(joe_id) / "report.pdf").is_file()

    # A site with nothing wrong still gets a report (it says so); an unreadable one does not
    assert client.post(f"/api/leads/{leads['Bright Smile Dental']['id']}/report").status_code == 200
    blocked = client.post(f"/api/leads/{leads['Blocked Dental']['id']}/report")
    assert blocked.status_code == 409 and "could not be read" in blocked.json()["detail"]
    assert client.post("/api/leads/999/report").status_code == 404

    # Once a report exists, the email says it is attached
    email = client.post(f"/api/leads/{joe_id}/email").json()["email"]
    assert "I have attached a short review" in email["body"]
    barber = client.post(f"/api/leads/{leads['No Site Barber']['id']}/email").json()["email"]
    assert "attached" not in barber["body"]

    assert client.delete(f"/api/leads/{joe_id}/report").status_code == 204
    after = client.get(f"/api/leads/{joe_id}").json()
    assert after["report"] is None and after["has_report"] is False and not config.report_dir(joe_id).exists()
    assert client.get(f"/api/leads/{joe_id}/report.pdf").status_code == 404

    # Deleting a lead removes its files too
    bright_id = leads["Bright Smile Dental"]["id"]
    assert config.report_dir(bright_id).exists()
    client.delete(f"/api/leads/{bright_id}")
    assert not config.report_dir(bright_id).exists()


def test_report_needs_a_name_and_explains_render_failures(client, monkeypatch):
    leads = google_leads(client)
    joe_id = leads["Joe's Plumbing"]["id"]
    resp = client.post(f"/api/leads/{joe_id}/report")
    assert resp.status_code == 400 and "Add your name in Settings" in resp.json()["detail"]
    client.put("/api/settings", json=SENDER)

    def broken(context, out_dir):
        raise RenderError("Making the PDF needs Microsoft Edge or Google Chrome. Install one of them, then try again.")

    monkeypatch.setattr(main_module, "make_report_files", broken)
    resp = client.post(f"/api/leads/{joe_id}/report")
    assert resp.status_code == 500 and "Microsoft Edge or Google Chrome" in resp.json()["detail"]
    assert client.get(f"/api/leads/{joe_id}").json()["report"] is None


def test_claude_is_told_about_the_attachments(client, fake_renderer):
    client.put("/api/settings", json={**SENDER, "claude_api_key": "sk-test"})
    joe_id = google_leads(client)["Joe's Plumbing"]["id"]
    client.post(f"/api/leads/{joe_id}/email")
    assert '"attachments": []' in CLAUDE_STATE["calls"][0]["body"]["messages"][0]["content"]
    client.post(f"/api/leads/{joe_id}/report")
    client.post(f"/api/leads/{joe_id}/email")
    assert "A short PDF review of their website" in CLAUDE_STATE["calls"][1]["body"]["messages"][0]["content"]


def test_file_names_are_safe_for_windows():
    assert main_module._file_name('A/B: "C" <D>|E?*.') == "A B C D E"
    assert main_module._file_name("") == "Business" and len(main_module._file_name("x" * 300)) == 80


@pytest.mark.skipif(not (HAS_BROWSER and HAS_TEMPLATES), reason="needs a Chromium-family browser and the report templates")
def test_real_pdf_and_picture(tmp_path, monkeypatch):
    if os.path.isfile(CHROMIUM):
        monkeypatch.setenv("CFA_BROWSER", CHROMIUM)
    found = result_no_website({"rating": 4.8, "reviews": 90})
    lead = {"name": "Ó Briain <b>Barbers</b> & Sons", "category": "Barber shop", "address": "9 High St, Dublin", "phone": "+353 1 555 0111",
            "rating": 4.8, "reviews": 90, "audit_status": "no_website", "audit": found["audit"]}
    context = build_context(lead, {**SENDER, "portfolio": [{"name": "Site", "description": "A site", "url": "https://x.example"}], "services": ["Web"]})

    report_html, concept_html = render.render_templates(context)
    for html in (report_html, concept_html):
        assert "&lt;b&gt;Barbers&lt;/b&gt;" in html and "<b>Barbers</b>" not in html   # the name is shown, never run
        assert "http://" not in html.replace("http://www.w3.org", "") and "<script" not in html.lower()
    assert "Ó Briain" in report_html and "Test Person" in report_html

    files = render.make_report_files(context, tmp_path / "out")
    pdf = files["pdf"].read_bytes()
    assert pdf.startswith(b"%PDF") and len(pdf) > 5000
    png = files["png"].read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", png[16:24]) == (1440, 1800)
