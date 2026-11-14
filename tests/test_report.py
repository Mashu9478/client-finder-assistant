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

    def fake(context, out_dir, concept_html=None):
        calls.append({**context, "_concept_html": concept_html})
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.pdf").write_bytes(b"%PDF-1.4 fake report for " + context["lead"]["name"].encode())
        (out_dir / "concept.png").write_bytes(b"\x89PNG fake picture")
        return {"pdf": out_dir / "report.pdf", "png": out_dir / "concept.png", "used_custom_concept": bool(concept_html)}

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
    statuses = [a["status"] for a in context["areas"]]
    assert statuses == sorted(statuses, key=["urgent", "needs_work", "minor", "good"].index)   # worst first, fine last
    assert context["areas"][0]["key"] == "security" and context["areas"][-1]["key"] == "speed"
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
    assert report["designer"] == "builtin" and report["note"] is None and fake_renderer[0]["_concept_html"] is None
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

    def broken(context, out_dir, concept_html=None):
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
    assert "A short PDF review of their website" in CLAUDE_STATE["calls"][-1]["body"]["messages"][0]["content"]


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


# ------------------------------------------------------- a design per business

def test_report_follows_the_business_needs(client):
    client.put("/api/settings", json=SENDER)
    leads = google_leads(client)
    joe = build_context(client.get(f"/api/leads/{leads['Joe\x27s Plumbing']['id']}").json(), SENDER)
    bright = build_context(client.get(f"/api/leads/{leads['Bright Smile Dental']['id']}").json(), SENDER)

    # A weak site gets priorities and a staged plan; a sound one gets neither
    assert [p["title"] for p in joe["priorities"]] == ["No secure connection (HTTPS)", "Not mobile-friendly", "Built with outdated techniques"]
    assert len({p["area"] for p in joe["priorities"]}) == 3
    assert [stage["key"] for stage in joe["plan"]] == ["quick", "next", "project"]
    assert joe["plan"][0]["steps"][0] == {"action": "Move the site to HTTPS with a free certificate and redirect the old address.", "because": "No secure connection (HTTPS)"}
    assert joe["plan"][0]["more"] > 0 and len(joe["plan"][0]["steps"]) == 6
    assert bright["priorities"] == [] and bright["plan"] == [] and len(bright["strengths"]) == 15
    assert "It already gets 2 things right: the server responds quickly and the page has a proper title." in joe["summary"]["intro"]
    assert "security and trust" in joe["summary"]["intro"]
    assert "It already gets 15 things right: the connection is secure and the layout adapts to phones, among others." in bright["summary"]["intro"]
    assert "old.example" in joe["summary"]["intro"] and "good.example" in bright["summary"]["intro"]
    assert "needs fixing now" in joe["summary"]["insight"] and "healthcare provider" in bright["summary"]["insight"]

    # The concept uses the business's own words and colours when its site has them
    concept = bright["concept"]
    assert concept["headline"] == "Family dentist in Houston" and concept["nav"] == ["Treatments", "Our team", "Fees", "Contact"]
    assert concept["services"] == ["General dentistry", "Teeth whitening", "Emergency appointments"] and concept["services_are_samples"] is False
    assert concept["palette"]["primary"] == "#1b6ca8" and bright["theme"]["palette"] == concept["palette"]
    assert "Your own wording" in concept["label"] and concept["uses_own_words"] is True
    # ... and falls back to sample wording and a colour set for its trade when it does not
    assert joe["concept"]["services"] == ["Repairs", "Installations", "Emergency call-outs"] and joe["concept"]["services_are_samples"] is True
    assert joe["concept"]["palette"]["primary"] == "#B45309" and "Sample wording" in joe["concept"]["label"]
    assert 0 <= joe["concept"]["variant"] < 4 and 0 <= joe["theme"]["cover_variant"] < 3

    # A narrative written for the business replaces the standard opening, if it is sensible
    told = build_context(client.get(f"/api/leads/{leads['Joe\x27s Plumbing']['id']}").json(), SENDER,
                         narrative={"intro": "We looked closely at how your site works for someone on a phone.", "insight": "x"})
    assert told["summary"]["intro"].startswith("We looked closely") and "needs fixing now" in told["summary"]["insight"]   # too-short insight ignored


def test_business_types_are_matched_by_whole_words():
    from app.report import category_insight, concept_copy
    assert concept_copy({"category": "Barber shop", "name": "No Site Barber"})[0] == "Look and feel your best"      # not a "bar"
    assert concept_copy({"category": "Bar", "name": "The Anchor"})[0] == "Good food, a table waiting"
    assert concept_copy({"category": "Car repair", "name": "Coachbuilders Ltd"})[0] == "Back on the road, without the wait"   # type beats name
    assert concept_copy({"category": "Veterinary", "name": "Paws"})[0] == "Kind, expert care for your pet"
    assert "first impression" in category_insight({"category": "", "name": "Acme Widgets"})


def test_each_new_report_gets_a_different_builtin_design(client, fake_renderer):
    client.put("/api/settings", json=SENDER)
    joe_id = google_leads(client)["Joe's Plumbing"]["id"]
    for _ in range(3):
        assert client.post(f"/api/leads/{joe_id}/report").status_code == 200
    variants = [call["concept"]["variant"] for call in fake_renderer]
    assert len(set(variants)) == 3 and all(0 <= v < 4 for v in variants)
    assert CLAUDE_STATE["calls"] == []                                   # no key saved, so Claude is not involved


def test_the_business_own_photo_is_used_when_it_publishes_one(client, fake_renderer):
    client.put("/api/settings", json=SENDER)
    leads = google_leads(client)
    client.post(f"/api/leads/{leads['Bright Smile Dental']['id']}/report")
    client.post(f"/api/leads/{leads['Joe\x27s Plumbing']['id']}/report")
    assert fake_renderer[0]["concept"]["hero_image"].startswith("data:image/jpeg;base64,/9j/")
    assert fake_renderer[1]["concept"]["hero_image"] is None


def test_claude_designs_the_concept_and_writes_the_opening(client, fake_renderer):
    client.put("/api/settings", json={**SENDER, "claude_api_key": "sk-test", "claude_model": "claude-haiku-5-5"})
    bright_id = google_leads(client)["Bright Smile Dental"]["id"]
    report = client.post(f"/api/leads/{bright_id}/report").json()["report"]
    assert report["designer"] == "claude" and report["note"] is None

    call = fake_renderer[0]
    assert call["summary"]["intro"].startswith("We spent some time with your website")      # Claude's opening
    page = call["_concept_html"]
    assert page.lstrip().lower().startswith("<!doctype html>")
    assert "Bright Smile Dental" in page and "A homepage designed for this business alone" in page
    # Everything that could run or reach outside has been removed
    lowered = page.lower()
    for forbidden in ("<script", "onload", "onclick", "<iframe", "<link", "http-equiv=\"refresh\"", "@import", "evil.example", "formaction", "steal()"):
        assert forbidden not in lowered, forbidden
    assert "default-src 'none'" in page and "http-equiv=\"Content-Security-Policy\"" in page
    assert "HERO_IMAGE_SRC" not in page and page.count("data:image/jpeg;base64,/9j/") == 2  # the photo, in the img and in the CSS
    assert "url(data:image/png;base64,AAAA)" in page and 'href="#offers"' in page          # harmless parts kept
    assert 'id="cfa-concept-label"' in page and "Concept only" in page                     # always labelled
    assert "width:1440px!important" in page and "height:1800px!important" in page

    design_call = next(c for c in CLAUDE_STATE["calls"] if "senior web designer" in c["body"]["system"])
    assert design_call["body"]["model"] == "claude-haiku-5-5" and "temperature" not in design_call["body"]
    brief = design_call["body"]["messages"][0]["content"]
    for expected in ("Bright Smile Dental", "Family dentist in Houston", "General dentistry", "#1b6ca8", "art_direction", '"hero_photo_available": true'):
        assert expected in brief, expected
    assert "sk-test" not in brief and "data:image" not in brief and "me@studio.example" not in brief
    narrative_call = next(c for c in CLAUDE_STATE["calls"] if "opening of a short website review" in c["body"]["system"])
    assert "Secure connection (HTTPS)" in narrative_call["body"]["messages"][0]["content"]

    # A second report asks for a different art direction
    client.post(f"/api/leads/{bright_id}/report")
    briefs = [c["body"]["messages"][0]["content"] for c in CLAUDE_STATE["calls"] if "senior web designer" in c["body"]["system"]]
    direction = lambda text: text.split('"art_direction": "')[1].split('"')[0]
    assert direction(briefs[0]) != direction(briefs[1])


@pytest.mark.parametrize("mode,phrase", [
    ("bad_key", "Claude rejected the API key"),
    ("design_offtopic", "did not describe this business"),
    ("design_truncated", "cut short"),
])
def test_a_failed_claude_design_falls_back_to_a_builtin_one(client, fake_renderer, mode, phrase):
    client.put("/api/settings", json={**SENDER, "claude_api_key": "sk-test"})
    joe_id = google_leads(client)["Joe's Plumbing"]["id"]
    CLAUDE_STATE["mode"] = mode
    report = client.post(f"/api/leads/{joe_id}/report").json()["report"]
    assert report["designer"] == "builtin" and phrase in report["note"] and "built-in designs" in report["note"]
    assert fake_renderer[0]["_concept_html"] is None


def test_sanitizer_rejects_pages_that_are_not_usable():
    from app.designer import DesignError, sanitize_concept
    with pytest.raises(DesignError):
        sanitize_concept("I cannot help with that.", "Acme", None, "Concept only.")
    with pytest.raises(DesignError):
        sanitize_concept("<html><body>Acme " + "word " * 80 + "</body></html>", "Acme", None, "Concept only.")   # no styling
    page = sanitize_concept("<html><head><style>p{color:red}</style></head><body><p>Acme " + "word " * 80 + '</p><img src="HERO_IMAGE_SRC"></body></html>', "Acme", None, "Concept only.")
    assert "HERO_IMAGE_SRC" not in page and "data:image/gif;base64" in page      # no photo: a blank pixel, never a broken link


@pytest.mark.skipif(not (HAS_BROWSER and HAS_TEMPLATES), reason="needs a Chromium-family browser and the report templates")
def test_real_render_of_a_claude_design(tmp_path, monkeypatch):
    from app.designer import sanitize_concept
    from conftest import CLAUDE_DESIGN
    if os.path.isfile(CHROMIUM):
        monkeypatch.setenv("CFA_BROWSER", CHROMIUM)
    found = result_no_website({"rating": 4.8, "reviews": 90})
    lead = {"name": "Harbour Barbers", "category": "Barber shop", "phone": "+353 1 555 0111", "rating": 4.8, "reviews": 90,
            "audit_status": "no_website", "audit": found["audit"]}
    context = build_context(lead, SENDER)
    page = sanitize_concept(CLAUDE_DESIGN.replace("__NAME__", "Harbour Barbers"), "Harbour Barbers", None, context["concept"]["label"])
    files = render.make_report_files(context, tmp_path / "out", page)
    assert files["used_custom_concept"] is True
    png = files["png"].read_bytes()
    assert struct.unpack(">II", png[16:24]) == (1440, 1800) and files["pdf"].read_bytes().startswith(b"%PDF")
    # A page the browser cannot photograph falls back to the built-in layout instead of failing
    files = render.make_report_files(context, tmp_path / "out2", "")
    assert files["used_custom_concept"] is False and files["png"].is_file()
