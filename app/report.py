"""Build the client-facing report and homepage concept for one lead.

``build_context`` turns a lead's audit into the data the two templates need:

- ``report.html.j2`` - an A4 report, printed to PDF
- ``concept.html.j2`` - a homepage concept, captured as a picture

Everything in the context is either a fact from the audit, the business's own
public details, or clearly generic sample wording. Nothing about the business
is invented.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from .contacts import registrable_domain

# Which part of the site each finding belongs to, in the order the report shows them.
AREAS: list[tuple[str, str, str]] = [
    ("presence", "Online presence", "Whether customers can find and reach a website for the business at all."),
    ("security", "Security and trust", "Whether browsers treat the site as safe to visit."),
    ("speed", "Speed", "How quickly pages reach a visitor."),
    ("mobile", "Mobile experience", "How the site works on a phone, where most local searches happen."),
    ("design", "Design and technology", "How current the site looks and what it is built on."),
    ("search", "Google visibility", "How well search engines can understand and list the site."),
    ("conversion", "Turning visitors into customers", "How easy it is to enquire, call or book."),
    ("measurement", "Measurement", "Whether the business can see what its website is doing for it."),
    ("upkeep", "Upkeep", "Small signs of whether the site is being looked after."),
]

AREA_OF = {
    "no_website": "presence", "no_website_listed": "presence", "social_only": "presence",
    "site_down": "presence", "site_error": "presence",
    "ssl_error": "security", "no_https": "security",
    "slow_response": "speed", "pagespeed_low": "speed", "many_scripts": "speed", "heavy_html": "speed",
    "no_compression": "speed", "no_lazy_images": "speed",
    "no_viewport": "mobile",
    "old_markup": "design", "old_copyright": "design", "old_wordpress": "design", "old_jquery": "design",
    "site_builder": "design", "js_rendered": "design",
    "noindex": "search", "no_title": "search", "short_title": "search", "no_meta_description": "search",
    "no_h1": "search", "images_no_alt": "search", "no_structured_data": "search", "no_og": "search",
    "no_sitemap": "search",
    "no_contact_form": "conversion", "no_click_to_call": "conversion", "no_booking": "conversion", "no_chat": "conversion",
    "no_analytics": "measurement",
    "broken_links": "upkeep", "no_favicon": "upkeep",
}

# The recommended fix for each finding, addressed to the owner.
FIX = {
    "no_website": "Launch a fast, mobile-first website with your services, opening hours, reviews and an enquiry form.",
    "no_website_listed": "If you have no website yet, launch a simple, fast one. If you do, make sure it is listed wherever customers look you up.",
    "social_only": "Add a website of your own alongside the social page, so you control how you appear and show up in Google.",
    "site_down": "Restore the site, or replace it with a reliable new one, and set up monitoring so you hear about outages first.",
    "site_error": "Repair the page behind your listed address, or point the address at a working page.",
    "ssl_error": "Install a valid security certificate and set it to renew automatically.",
    "no_https": "Move the site to HTTPS with a free certificate and redirect the old address.",
    "slow_response": "Add caching and move to faster hosting, so the first page arrives in under a second.",
    "pagespeed_low": "Compress images, trim unused scripts and add caching to lift the mobile speed score.",
    "many_scripts": "Remove unused plugins and combine the scripts that remain.",
    "heavy_html": "Rebuild the page with clean, lightweight code.",
    "no_compression": "Switch on compression on the server. It is a one-line change.",
    "no_lazy_images": "Load images only as the visitor scrolls to them.",
    "no_viewport": "Rebuild the layout so it adapts to phone, tablet and desktop screens.",
    "old_markup": "Rebuild the site on current web standards.",
    "old_copyright": "Refresh the content and set the footer year to update itself.",
    "old_wordpress": "Update WordPress and its plugins, after taking a full backup.",
    "old_jquery": "Update or remove the outdated script library.",
    "site_builder": "Move to a custom build for more speed, better ranking and a look of your own.",
    "js_rendered": "Serve the page's content directly, so search engines and slower phones can read it.",
    "noindex": "Remove the instruction that hides the page from search engines.",
    "no_title": "Give every page a clear title naming the service and the location.",
    "short_title": "Rewrite the page title to name your main service and location.",
    "no_meta_description": "Write a short, inviting description for each main page.",
    "no_h1": "Add one clear main heading per page that says what you offer.",
    "images_no_alt": "Add a short text description to each image.",
    "no_structured_data": "Add business details (name, address, hours, reviews) in the format Google reads.",
    "no_og": "Add a preview image and summary for when the site is shared.",
    "no_sitemap": "Publish a sitemap and submit it to Google.",
    "no_contact_form": "Add a short enquiry form that sends messages straight to your email or WhatsApp.",
    "no_click_to_call": "Make the phone number tappable on mobile.",
    "no_booking": "Add online booking with automatic reminders.",
    "no_chat": "Add a chat or WhatsApp button, or an AI assistant that answers common questions at any hour.",
    "no_analytics": "Set up visitor tracking and a simple monthly dashboard.",
    "broken_links": "Fix or redirect the broken links.",
    "no_favicon": "Add your logo as the site icon.",
}

# Checks that are always made on a readable site: when not failed, they are reported as passed.
PASS_LABELS = [
    ("no_https", "Secure connection (HTTPS)"),
    ("slow_response", "Server responds quickly"),
    ("no_viewport", "Layout adapts to phone screens"),
    ("no_title", "Page has a title"),
    ("no_meta_description", "Search description is present"),
    ("no_h1", "Clear main heading"),
    ("no_structured_data", "Business details labelled for Google"),
    ("no_og", "Preview shows when the site is shared"),
    ("no_sitemap", "Sitemap is published"),
    ("no_contact_form", "Enquiry form is present"),
    ("no_click_to_call", "Phone number is tappable"),
    ("no_booking", "Online booking is available"),
    ("no_chat", "Chat or instant messaging is available"),
    ("no_analytics", "Visitor tracking is in place"),
    ("no_favicon", "Site icon is set"),
]

# Owner-facing wording for the audit's upgrade ideas.
RECOMMENDATION = {
    "A website, if they truly have none": ("A website of your own", "A fast, mobile-first site with your services, reviews and an enquiry form."),
    "Build their first proper website": ("A website of your own", "A fast, mobile-first site with your services, prices, reviews and an enquiry form, so you no longer rely on a listing you do not control."),
    "Get them back online": ("Back online, reliably", "Restore the site or replace it, with monitoring so outages are caught early."),
    "Modern mobile-first redesign": ("Modern, mobile-first redesign", "A rebuild on current technology that looks right on phones, loads securely and is easy for you to update."),
    "Speed tune-up": ("Speed tune-up", "Caching, lighter images and script clean-up so pages load quickly."),
    "Local search clean-up": ("Local search clean-up", "Titles, descriptions and business details fixed so you show up properly for local Google searches."),
    "Make it easy to enquire": ("Easier enquiries", "A short enquiry form and tap-to-call button that send enquiries straight to your email or WhatsApp."),
    "Online booking system": ("Online booking", "Customers book at any hour, with automatic reminders to cut no-shows."),
    "AI assistant for enquiries": ("AI assistant for enquiries", "A chat assistant that answers common questions and collects contact details around the clock."),
    "Visitor and enquiry dashboard": ("Visitor and enquiry dashboard", "Tracking with a simple monthly dashboard showing visitors, enquiries and which pages bring in work."),
    "Show off their reviews": ("Reviews on show", "Your Google reviews featured on the site, to win trust faster."),
    "Automatic review requests": ("Automatic review requests", "A review request sent after each job, to build up your Google rating."),
}

SERVICE_LABEL = {"website": "Website", "app": "App", "ai": "AI", "data": "Data"}
SEVERITY_LABEL = {"high": "High priority", "medium": "Worth fixing", "low": "Nice to have"}

# Colour sets for the concept. Each is (primary, dark, tint, accent); chosen by business type, else by name.
PALETTES = [
    ("#0F766E", "#134E4A", "#F0FDFA", "#F59E0B"),   # teal
    ("#1D4ED8", "#1E3A8A", "#EFF6FF", "#F97316"),   # blue
    ("#7C3AED", "#4C1D95", "#F5F3FF", "#10B981"),   # violet
    ("#BE123C", "#881337", "#FFF1F2", "#0EA5E9"),   # rose
    ("#B45309", "#78350F", "#FFFBEB", "#0D9488"),   # amber
    ("#15803D", "#14532D", "#F0FDF4", "#EA580C"),   # green
    ("#334155", "#0F172A", "#F8FAFC", "#E11D48"),   # slate
]

# Generic sample wording by business type: (keywords, headline, three sample service names, palette index).
CONCEPT_COPY: list[tuple[tuple[str, ...], str, tuple[str, str, str], int]] = [
    (("dent", "orthodont"), "Dental care that fits around you", ("Check-ups", "Whitening", "Emergency care"), 0),
    (("doctor", "clinic", "medical", "physio", "chiropract", "health"), "Care you can book in a minute", ("Consultations", "Treatments", "Follow-up care"), 1),
    (("vet",), "Kind, expert care for your pet", ("Check-ups", "Vaccinations", "Surgery"), 5),
    (("lawyer", "attorney", "law", "solicitor", "notary"), "Clear legal advice when it matters", ("Consultations", "Contracts", "Disputes"), 6),
    (("account", "tax", "financ"), "Your numbers, handled", ("Bookkeeping", "Tax returns", "Advice"), 6),
    (("estate", "realt", "property"), "Find the right place, faster", ("Buying", "Selling", "Lettings"), 1),
    (("plumb", "electric", "roof", "hvac", "heating", "builder", "construct", "carpent", "paint", "locksmith"), "Reliable work, done properly", ("Repairs", "Installations", "Emergency call-outs"), 4),
    (("clean", "laundry"), "A spotless result, every time", ("Home cleaning", "Office cleaning", "Deep cleans"), 0),
    (("car", "auto", "garage", "mechanic", "tyre", "tire"), "Back on the road, without the wait", ("Servicing", "Repairs", "Diagnostics"), 6),
    (("hair", "salon", "barber", "beauty", "spa", "nail", "cosmetic", "makeup", "massage"), "Look and feel your best", ("Cuts and styling", "Colour", "Treatments"), 3),
    (("gym", "fitness", "yoga", "dance"), "Train your way", ("Memberships", "Classes", "Personal training"), 2),
    (("restaurant", "cafe", "coffee", "bakery", "bar", "pub", "food"), "Good food, a table waiting", ("Menu", "Reservations", "Private events"), 4),
    (("hotel", "guest", "hostel", "motel"), "Your stay, booked direct", ("Rooms", "Dining", "Offers"), 1),
    (("school", "academy", "tutor", "kindergarten", "driving", "college"), "Learning that moves you forward", ("Courses", "Enrolment", "Timetables"), 2),
    (("florist", "flower"), "Fresh flowers, delivered", ("Bouquets", "Weddings", "Same-day delivery"), 3),
    (("photo",), "Moments, beautifully kept", ("Portraits", "Weddings", "Events"), 6),
]


def _site_label(website: str | None) -> str:
    if not website:
        return ""
    host = urlparse(website if "://" in website else "https://" + website).netloc
    return registrable_domain(host) or host


def _pretty_date(iso: str | None = None) -> str:
    try:
        moment = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ") if iso else datetime.now(timezone.utc)
    except (TypeError, ValueError):
        moment = datetime.now(timezone.utc)
    return f"{moment.day} {moment.strftime('%B %Y')}"


def _initials(name: str) -> str:
    words = [re.sub(r"[^\w]", "", w, flags=re.UNICODE) for w in name.split()]
    words = [w for w in words if w and w.lower() not in {"the", "and", "of"}]
    return "".join(w[0] for w in words[:2]).upper() or "•"


def concept_copy(lead: dict[str, Any]) -> tuple[str, list[str], tuple[str, str, str, str]]:
    """Sample headline, sample service names and colours for the concept."""
    haystack = f"{lead.get('category') or ''} {lead.get('name') or ''}".lower()
    for keywords, headline, services, palette_index in CONCEPT_COPY:
        if any(k in haystack for k in keywords):
            return headline, list(services), PALETTES[palette_index]
    digest = hashlib.sha256((lead.get("name") or "").encode("utf-8")).digest()
    return "Everything you need, in one place", ["Our services", "About us", "Get in touch"], PALETTES[digest[0] % len(PALETTES)]


def build_context(lead: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """Everything the report and concept templates need for one lead."""
    audit = lead.get("audit") or {}
    issues = [i for i in (audit.get("issues") or []) if isinstance(i, dict) and i.get("title")]
    ideas = [i for i in (audit.get("ideas") or []) if isinstance(i, dict) and i.get("title")]
    codes = {i.get("code") for i in issues}
    site_read = lead.get("audit_status") == "done"

    # Findings, grouped by area
    by_area: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        code = issue.get("code") or ""
        by_area.setdefault(AREA_OF.get(code, "upkeep"), []).append({
            "severity": issue.get("severity") or "low",
            "severity_label": SEVERITY_LABEL.get(issue.get("severity") or "low", "Nice to have"),
            "title": issue.get("title"),
            "what": issue.get("detail") or "",
            "fix": FIX.get(code, ""),
        })
    passed_by_area: dict[str, list[str]] = {}
    if site_read:
        skip_content_checks = "js_rendered" in codes
        for code, label in PASS_LABELS:
            if code in codes:
                continue
            if skip_content_checks and code in ("no_h1", "no_contact_form", "no_click_to_call", "no_booking"):
                continue
            passed_by_area.setdefault(AREA_OF[code], []).append(label)

    areas = []
    for key, title, about in AREAS:
        findings = by_area.get(key, [])
        passed = passed_by_area.get(key, [])
        if not findings and not passed:
            continue
        worst = min((["high", "medium", "low"].index(f["severity"]) for f in findings), default=3)
        areas.append({
            "key": key,
            "title": title,
            "about": about,
            "status": ["urgent", "needs_work", "minor", "good"][worst],
            "status_label": ["Needs attention", "Could be better", "Minor points", "Looking good"][worst],
            "findings": findings,
            "passed": passed,
        })

    counts = {s: sum(1 for i in issues if i.get("severity") == s) for s in ("high", "medium", "low")}
    if not site_read:
        headline = "There is no working website for customers to find." if codes & {"no_website", "social_only", "site_down", "site_error"} \
            else "We could not find a website listed for the business."
    elif counts["high"]:
        n = counts["high"]
        headline = f"{n} issue{'s' if n != 1 else ''} need{'s' if n == 1 else ''} attention, and each is fixable."
    elif counts["medium"]:
        headline = "The site works, with clear room to win more enquiries."
    elif issues:
        headline = "The site is in good shape, with a few small points to tidy."
    else:
        headline = "The site is in good shape."

    recommendations = []
    seen_titles: set[str] = set()
    for idea in ideas:
        title, detail = RECOMMENDATION.get(idea["title"], (idea["title"], idea.get("detail") or ""))
        if title in seen_titles:
            continue
        seen_titles.add(title)
        recommendations.append({
            "title": title,
            "detail": detail,
            "service": idea.get("service") or "website",
            "service_label": SERVICE_LABEL.get(idea.get("service") or "website", "Website"),
        })

    # Homepage concept: shows the upgrades this report recommends
    headline_text, sample_services, palette = concept_copy(lead)
    rating, reviews = lead.get("rating"), lead.get("reviews")
    has_rating = bool(rating and reviews)
    idea_titles = {i["title"] for i in ideas}
    name = (lead.get("name") or "Your business").strip()
    concept = {
        "business_name": name,
        "initials": _initials(name),
        "category": lead.get("category") or "",
        "phone": lead.get("phone") or "",
        "address": lead.get("address") or "",
        "headline": headline_text,
        "sample_services": sample_services,
        "rating": rating if has_rating and float(rating) >= 4.0 else None,
        "reviews": reviews if has_rating and float(rating) >= 4.0 else None,
        "palette": {"primary": palette[0], "dark": palette[1], "tint": palette[2], "accent": palette[3]},
        "features": {
            # Always part of a modern local-business homepage
            "enquiry_form": True,
            "tap_to_call": bool(lead.get("phone")),
            # Shown when the report recommends them, or when there is no site yet
            "booking": "Online booking system" in idea_titles or not site_read,
            "chat": "AI assistant for enquiries" in idea_titles or not site_read,
            # Only worth featuring when the rating is one to be proud of
            "reviews": has_rating and float(rating) >= 4.0,
        },
        "label": "Concept only. Sample wording and layout, to show the direction.",
    }

    sender_lines = [settings.get("your_name") or "", settings.get("business_name") or ""]
    return {
        "generated_on": _pretty_date(),
        "sender": {
            "name": settings.get("your_name") or "",
            "studio": settings.get("business_name") or "",
            "email": settings.get("email") or "",
            "whatsapp": settings.get("whatsapp") or "",
            "portfolio_url": settings.get("portfolio_url") or "",
            "services": list(settings.get("services") or []),
            "portfolio": [p for p in (settings.get("portfolio") or []) if isinstance(p, dict) and p.get("name")][:4],
            "byline": " · ".join(line for line in sender_lines if line.strip()),
        },
        "lead": {
            "name": name,
            "category": lead.get("category") or "",
            "address": lead.get("address") or "",
            "website": _site_label(lead.get("website")),
            "phone": lead.get("phone") or "",
            "rating": rating if has_rating else None,
            "reviews": reviews if has_rating else None,
        },
        "summary": {
            "site_read": site_read,
            "headline": headline,
            "counts": counts,
            "total_findings": len(issues),
            "checks_passed": sum(len(v) for v in passed_by_area.values()),
            "checked_on": _pretty_date(audit.get("checked_at")),
            "pages_checked": len(audit.get("pages_checked") or []),
            "response_seconds": round(audit["load_ms"] / 1000, 1) if audit.get("load_ms") else None,
            "https": audit.get("https"),
            "platform": audit.get("platform"),
            "pagespeed_mobile": audit.get("pagespeed_mobile"),
        },
        "areas": areas,
        "recommendations": recommendations,
        "concept": concept,
        "method_note": (
            "This review was produced by an automated check of the business's public web pages on the date shown. "
            "It looks only at what any visitor can see. Automated checks can miss context, so please treat each "
            "point as a prompt to look, not a final verdict."
        ),
    }
