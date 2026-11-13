"""Website audit.

Reads a business's public pages the way any visitor's browser would and
reports weaknesses a web/app/AI/data freelancer could fix. It is passive:
it loads the homepage, at most two contact/about pages and a handful of the
site's own links. It identifies itself, obeys robots.txt, and never tries
logins, forms, hidden paths or anything a visitor could not see.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from . import __version__
from .contacts import (
    extract_emails,
    extract_phones,
    extract_social,
    is_social_or_directory_url,
    registrable_domain,
)

log = logging.getLogger("cfa.audit")

BOT_TOKEN = "ClientFinderAssistant"
USER_AGENT = (
    f"Mozilla/5.0 (compatible; {BOT_TOKEN}/{__version__}; "
    "+https://github.com/Mashu9478/client-finder-assistant)"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    "Accept-Language": "en;q=0.9,*;q=0.5",
}

MAX_HTML_BYTES = 3_000_000
PAGE_TIMEOUT = 20.0
SMALL_TIMEOUT = 8.0
MAX_EXTRA_PAGES = 2
MAX_LINK_CHECKS = 8
SITE_BUDGET_SECONDS = 75.0

PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

SEVERITY_WEIGHT = {"high": 18, "medium": 9, "low": 4}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

BOOKING_VENDORS = (
    "calendly.com", "acuityscheduling", "booksy", "setmore", "squareup.com/appointments",
    "square.site", "opentable", "resy.com", "fresha", "vagaro", "mindbodyonline", "simplybook",
    "zocdoc", "housecallpro", "getjobber", "servicetitan", "bookwhen", "appointy", "youcanbook",
    "tidycal", "cal.com/", "schedulista", "localmed", "nexhealth", "bookeo", "amelia",
)
BOOKING_WORDS = re.compile(
    r"\b(book (now|online|an? (appointment|table|session|call|visit|consultation))|"
    r"schedule (now|online|an? (appointment|visit|call|consultation|service))|"
    r"make an? (appointment|reservation)|request an? appointment|reserve (now|a table|online)|"
    r"online booking|book your)\b",
    re.I,
)
CHAT_SIGNATURES = (
    "intercom", "tawk.to", "crisp.chat", "drift.com", "driftt", "tidio", "zdassets", "zopim",
    "livechatinc", "freshchat", "olark", "wa.me/", "api.whatsapp.com", "m.me/", "chatbase",
    "botpress", "manychat", "hs-scripts.com", "smartsupp", "chatra", "gorgias", "liveagent",
    "podium", "birdeye", "chat-widget", "chatwidget", "leadconnectorhq", "elfsight",
)
ANALYTICS_SIGNATURES = (
    "googletagmanager.com", "google-analytics.com", "gtag(", "ga('create'", "fbq(",
    "connect.facebook.net", "plausible.io", "matomo", "clarity.ms", "hotjar", "segment.com/analytics",
    "mixpanel", "cloudflareinsights", "usefathom", "statcounter", "simpleanalytics", "umami",
    "posthog", "heap-analytics", "hs-analytics",
)
FORM_VENDORS = (
    "wpcf7", "gform_", "wpforms", "typeform", "jotform", "hsforms", "formspree", "docs.google.com/forms",
    "ninja-forms", "formidable", "elementor-form", "fluentform", "cognitoforms", "formstack", "netlify",
    "w-form", "form-block", "wufoo",
)
SITE_BUILDERS = (
    ("wixstatic.com", "Wix"), ("_wixcss", "Wix"), ("squarespace", "Squarespace"),
    ("weebly", "Weebly"), ("wsimg.com", "GoDaddy Website Builder"), ("sites.google.com", "Google Sites"),
    ("multiscreensite", "Duda"), ("dudaone", "Duda"), ("jimdo", "Jimdo"), ("webnode", "Webnode"),
    ("strikingly", "Strikingly"), ("site123", "SITE123"),
)
OTHER_PLATFORMS = (
    ("wp-content/", "WordPress"), ("wp-includes/", "WordPress"), ("cdn.shopify.com", "Shopify"),
    ("webflow", "Webflow"), ("/media/jui/", "Joomla"), ("drupal-settings-json", "Drupal"),
    ("/sites/default/files", "Drupal"), ("framerusercontent", "Framer"), ("cdn.bigcommerce", "BigCommerce"),
    ("ghost.io", "Ghost"), ("hubspot", "HubSpot CMS"),
)
CONTACT_HINT = re.compile(r"(contact|kontakt|contacto|contato|get-in-touch|reach-us|about|uber-uns|a-propos|impressum)", re.I)
SKIP_LINK_SUFFIXES = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip", ".doc", ".docx", ".xls",
    ".xlsx", ".mp4", ".mp3", ".ics", ".vcf", ".xml", ".rss",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_url(raw: str | None) -> str | None:
    """Turn what a person types (or Google returns) into a fetchable URL."""
    if not raw:
        return None
    url = raw.strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        if "://" in url:
            return None
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if "." not in host or " " in host or len(host) > 253:
        return None
    return url


def make_issue(code: str, severity: str, title: str, detail: str, service: str = "website") -> dict[str, str]:
    return {"code": code, "severity": severity, "title": title, "detail": detail, "service": service}


def make_idea(service: str, title: str, detail: str) -> dict[str, str]:
    return {"service": service, "title": title, "detail": detail}


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

class Fetched:
    def __init__(self) -> None:
        self.ok = False
        self.status = 0
        self.url = ""
        self.headers: dict[str, str] = {}
        self.text = ""
        self.size = 0
        self.elapsed_ms = 0
        self.error = ""
        self.error_kind = ""  # timeout | ssl | connect | other


async def fetch_page(client: httpx.AsyncClient, url: str, timeout: float = PAGE_TIMEOUT) -> Fetched:
    """GET a page, capped in size and time."""
    out = Fetched()
    out.url = url
    start = time.perf_counter()
    try:
        async with client.stream("GET", url, headers=HEADERS, timeout=timeout, follow_redirects=True) as resp:
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                chunks.append(chunk)
                if total > MAX_HTML_BYTES:
                    break
            body = b"".join(chunks)
            out.status = resp.status_code
            out.url = str(resp.url)
            out.headers = {k.lower(): v for k, v in resp.headers.items()}
            encoding = resp.charset_encoding or "utf-8"
        try:
            out.text = body.decode(encoding, errors="replace")
        except LookupError:
            out.text = body.decode("utf-8", errors="replace")
        out.size = len(body)
        out.ok = True
    except httpx.TimeoutException:
        out.error, out.error_kind = "The site took too long to respond.", "timeout"
    except httpx.ConnectError as exc:
        message = str(exc).lower()
        if "certificate" in message or "ssl" in message or "tls" in message:
            out.error, out.error_kind = "The site's security certificate is broken or expired.", "ssl"
        else:
            out.error, out.error_kind = "The site could not be reached (it may be offline or the address is wrong).", "connect"
    except httpx.TooManyRedirects:
        out.error, out.error_kind = "The site redirects in a loop.", "other"
    except (httpx.HTTPError, ValueError) as exc:
        out.error, out.error_kind = f"The site could not be read ({type(exc).__name__}).", "other"
    out.elapsed_ms = int((time.perf_counter() - start) * 1000)
    return out


async def load_robots(client: httpx.AsyncClient, base_url: str) -> tuple[RobotFileParser | None, bool, bool]:
    """Returns (parser or None, robots.txt exists, robots.txt lists a sitemap)."""
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        resp = await client.get(robots_url, headers=HEADERS, timeout=SMALL_TIMEOUT, follow_redirects=True)
    except (httpx.HTTPError, ValueError):
        return None, False, False
    content_type = resp.headers.get("content-type", "").lower()
    if resp.status_code != 200 or "html" in content_type:
        return None, False, False
    text = resp.text[:200_000]
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    has_sitemap = bool(re.search(r"^\s*sitemap\s*:", text, re.I | re.M))
    return parser, True, has_sitemap


def allowed(parser: RobotFileParser | None, url: str) -> bool:
    if parser is None:
        return True
    try:
        return parser.can_fetch(BOT_TOKEN, url)
    except Exception:  # robotparser can choke on odd URLs; treat as allowed
        return True


async def link_status(client: httpx.AsyncClient, url: str) -> int:
    """Status code of a link without downloading its body. 0 = could not tell."""
    try:
        async with client.stream("GET", url, headers=HEADERS, timeout=SMALL_TIMEOUT, follow_redirects=True) as resp:
            return resp.status_code
    except (httpx.HTTPError, ValueError):
        return 0


async def pagespeed_score(client: httpx.AsyncClient, url: str, api_key: str) -> int | None:
    """Google's mobile performance score (0-100), or None if unavailable."""
    try:
        resp = await client.get(
            PSI_URL,
            params={"url": url, "strategy": "mobile", "category": "performance", "key": api_key},
            timeout=70.0,
        )
        if resp.status_code != 200:
            log.info("PageSpeed returned %s for %s", resp.status_code, url)
            return None
        score = resp.json()["lighthouseResult"]["categories"]["performance"]["score"]
        return None if score is None else int(round(float(score) * 100))
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Reading one page
# --------------------------------------------------------------------------

def _meta(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"name": re.compile(rf"^{re.escape(name)}$", re.I)})
    if tag is None:
        tag = soup.find("meta", attrs={"property": re.compile(rf"^{re.escape(name)}$", re.I)})
    return (tag.get("content") or "").strip() if tag else ""


def _looks_like_contact_form(form: Any) -> bool:
    if (form.get("role") or "").lower() == "search":
        return False
    fields = form.find_all(["input", "textarea", "select"])
    visible = [f for f in fields if (f.get("type") or "text").lower() not in {"hidden", "submit", "button", "image", "reset"}]
    if form.find("textarea"):
        return True
    names = " ".join(
        f"{f.get('name', '')} {f.get('id', '')} {f.get('placeholder', '')} {f.get('type', '')}".lower() for f in visible
    )
    has_contact_field = any(word in names for word in ("email", "phone", "tel", "message", "name"))
    return has_contact_field and len(visible) >= 2


def analyze_html(html: str, url: str) -> dict[str, Any]:
    """Facts about one page. Pure function: no network."""
    soup = BeautifulSoup(html, "html.parser")
    lower = html.lower()
    parsed = urlparse(url)
    site_domain = registrable_domain(parsed.netloc)

    # Scripts stay in `soup` for signature checks; visible text comes from a cleaned copy.
    body = soup.body or soup
    text_soup = BeautifulSoup(str(body), "html.parser")
    for tag in text_soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    text = text_soup.get_text(" ", strip=True)
    words = len(text.split())

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    images = soup.find_all("img")
    scripts = soup.find_all("script", src=True)
    stylesheets = [l for l in soup.find_all("link", rel=True) if "stylesheet" in [r.lower() for r in l.get("rel", [])]]
    icons = [l for l in soup.find_all("link", rel=True) if any("icon" in r.lower() for r in l.get("rel", []))]

    # Platform
    generator = _meta(soup, "generator")
    platform = None
    builder = None
    if generator:
        platform = generator[:60]
    for needle, name in SITE_BUILDERS:
        if needle in lower:
            builder = name
            platform = platform or name
            break
    if not platform:
        for needle, name in OTHER_PLATFORMS:
            if needle in lower:
                platform = name
                break
    if generator and not builder:
        for _needle, name in SITE_BUILDERS:
            if name.lower().split()[0] in generator.lower():
                builder = name
                break

    wp_version = None
    match = re.search(r"wordpress\s+(\d+)\.(\d+)", generator, re.I)
    if match:
        wp_version = (int(match.group(1)), int(match.group(2)))

    # Old jQuery
    old_jquery = None
    for script in scripts:
        match = re.search(r"jquery[.\-/]?v?(\d)\.(\d+)(?:\.\d+)?(?:\.min)?\.js", script["src"], re.I)
        if match and int(match.group(1)) < 3:
            old_jquery = f"{match.group(1)}.{match.group(2)}"
            break

    # Copyright year
    this_year = datetime.now(timezone.utc).year
    years = []
    for match in re.finditer(r"(?:©|&copy;|\(c\)|copyright)\s*(?:\d{4}\s*[-–—]\s*)?((?:19|20)\d{2})", text, re.I):
        year = int(match.group(1))
        if 1995 <= year <= this_year + 1:
            years.append(year)
    copyright_year = max(years) if years else None

    # Links
    internal_links: list[str] = []
    contact_links: list[str] = []
    seen_links: set[str] = set()
    has_tel = False
    has_mailto = False
    booking_link = False
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        href_lower = href.lower()
        if href_lower.startswith("tel:"):
            has_tel = True
            continue
        if href_lower.startswith("mailto:"):
            has_mailto = True
            continue
        if href_lower.startswith(("javascript:", "#", "sms:", "data:")) or not href:
            continue
        label = link.get_text(" ", strip=True)
        if BOOKING_WORDS.search(label) or any(v in href_lower for v in BOOKING_VENDORS):
            booking_link = True
        try:
            absolute = urldefrag(urljoin(url, href))[0]
        except ValueError:
            continue
        target = urlparse(absolute)
        if target.scheme not in ("http", "https"):
            continue
        if registrable_domain(target.netloc) != site_domain:
            continue
        if target.path.lower().endswith(SKIP_LINK_SUFFIXES):
            continue
        if absolute in seen_links:
            continue
        seen_links.add(absolute)
        internal_links.append(absolute)
        if CONTACT_HINT.search(target.path) or CONTACT_HINT.search(label):
            contact_links.append(absolute)

    forms = soup.find_all("form")
    has_contact_form = any(_looks_like_contact_form(f) for f in forms) or any(v in lower for v in FORM_VENDORS)

    has_booking = booking_link or any(v in lower for v in BOOKING_VENDORS) or bool(BOOKING_WORDS.search(text))
    has_chat = any(sig in lower for sig in CHAT_SIGNATURES)
    has_analytics = any(sig in lower for sig in ANALYTICS_SIGNATURES)

    old_tags = [name for name in ("font", "center", "marquee", "frameset", "frame", "blink") if soup.find(name)]
    has_flash = ".swf" in lower

    robots_meta = _meta(soup, "robots").lower()
    html_tag = soup.find("html")

    imgs_missing_alt = [img for img in images if img.get("alt") is None]
    lazy_imgs = [
        img for img in images
        if (img.get("loading") or "").lower() == "lazy" or img.get("data-src") or "lazy" in " ".join(img.get("class", [])).lower()
    ]

    return {
        "soup": soup,
        "url": url,
        "title": title,
        "description": _meta(soup, "description"),
        "viewport": bool(_meta(soup, "viewport")),
        "noindex": "noindex" in robots_meta,
        "h1_count": len(soup.find_all("h1")),
        "image_count": len(images),
        "images_missing_alt": len(imgs_missing_alt),
        "lazy_images": len(lazy_imgs),
        "script_count": len(scripts),
        "stylesheet_count": len(stylesheets),
        "has_og": bool(_meta(soup, "og:title") or _meta(soup, "og:image")),
        "has_structured_data": bool(soup.find("script", attrs={"type": re.compile("ld\\+json", re.I)}) or soup.find(attrs={"itemscope": True})),
        "has_favicon_tag": bool(icons),
        "lang": bool(html_tag and html_tag.get("lang")),
        "platform": platform,
        "builder": builder,
        "wp_version": wp_version,
        "old_jquery": old_jquery,
        "copyright_year": copyright_year,
        "internal_links": internal_links,
        "contact_links": contact_links,
        "has_tel": has_tel,
        "has_mailto": has_mailto,
        "has_contact_form": has_contact_form,
        "has_booking": has_booking,
        "has_chat": has_chat,
        "has_analytics": has_analytics,
        "old_tags": old_tags,
        "has_flash": has_flash,
        "words": words,
        "js_rendered": words < 40 and len(scripts) > 0,
    }


# --------------------------------------------------------------------------
# Turning facts into issues, ideas and a score
# --------------------------------------------------------------------------

def build_issues(
    home: dict[str, Any],
    pages: list[dict[str, Any]],
    *,
    fetched: Fetched,
    started_https: bool,
    ssl_broken: bool,
    robots_exists: bool,
    has_sitemap: bool,
    broken_links: list[str],
    links_checked: int,
    favicon_ok: bool,
    pagespeed: int | None,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    final_https = urlparse(fetched.url).scheme == "https"
    any_page = lambda key: any(p.get(key) for p in pages)  # noqa: E731
    js_rendered = home["js_rendered"]

    # Security basics a visitor's browser shows
    if ssl_broken:
        issues.append(make_issue(
            "ssl_error", "high", "Security certificate problem",
            "Browsers show a full-page warning before visitors can reach this site, so most people leave.",
        ))
    elif not final_https:
        issues.append(make_issue(
            "no_https", "high", "No secure connection (HTTPS)",
            "Browsers label this site \"Not secure\", which puts visitors off and hurts its Google ranking.",
        ))

    # Speed
    seconds = fetched.elapsed_ms / 1000
    if fetched.elapsed_ms >= 4000:
        issues.append(make_issue(
            "slow_response", "high", f"Slow to load ({seconds:.1f} s)",
            f"The server took {seconds:.1f} seconds just to send the page, before any images or scripts. Many visitors give up after about 3 seconds.",
        ))
    elif fetched.elapsed_ms >= 2000:
        issues.append(make_issue(
            "slow_response", "medium", f"Sluggish to load ({seconds:.1f} s)",
            f"The server took {seconds:.1f} seconds to send the page. Faster hosting or caching would help.",
        ))
    if pagespeed is not None:
        if pagespeed < 50:
            issues.append(make_issue(
                "pagespeed_low", "high", f"Poor mobile speed score ({pagespeed}/100)",
                f"Google's own test scores this site {pagespeed} out of 100 for speed on phones.",
            ))
        elif pagespeed < 75:
            issues.append(make_issue(
                "pagespeed_low", "medium", f"Average mobile speed score ({pagespeed}/100)",
                f"Google's own test scores this site {pagespeed} out of 100 for speed on phones.",
            ))
    if home["script_count"] > 30:
        issues.append(make_issue(
            "many_scripts", "medium", f"Heavy page ({home['script_count']} scripts)",
            f"The homepage loads {home['script_count']} separate script files, which slows it down, especially on phones.",
        ))
    if fetched.size > 500_000:
        issues.append(make_issue(
            "heavy_html", "low", f"Bloated page code ({fetched.size // 1024} KB)",
            "The page's own code is unusually large, a common sign of a page-builder that was never optimised.",
        ))
    if fetched.size > 50_000 and not fetched.headers.get("content-encoding"):
        issues.append(make_issue(
            "no_compression", "low", "Page is sent uncompressed",
            "The server does not compress the page, so every visitor downloads more than they need to.",
        ))
    if home["image_count"] >= 15 and home["lazy_images"] == 0:
        issues.append(make_issue(
            "no_lazy_images", "low", f"All {home['image_count']} images load at once",
            "Images further down the page load immediately instead of when the visitor scrolls to them.",
        ))

    # Mobile
    if not home["viewport"]:
        issues.append(make_issue(
            "no_viewport", "high", "Not mobile-friendly",
            "The page is not set up for phone screens, so mobile visitors see a tiny zoomed-out desktop layout.",
        ))

    # Old technology
    if home["has_flash"] or home["old_tags"]:
        what = "Flash" if home["has_flash"] else ", ".join(f"<{t}>" for t in home["old_tags"][:3])
        issues.append(make_issue(
            "old_markup", "high", "Built with outdated techniques",
            f"The page still uses {what}, which dates the build to well over a decade ago.",
        ))
    this_year = datetime.now(timezone.utc).year
    if home["copyright_year"] and home["copyright_year"] <= this_year - 2:
        issues.append(make_issue(
            "old_copyright", "medium", f"Looks out of date (© {home['copyright_year']})",
            f"The footer still says {home['copyright_year']}, which suggests nobody has updated the site in years.",
        ))
    if home["wp_version"] and home["wp_version"][0] < 6:
        version = f"{home['wp_version'][0]}.{home['wp_version'][1]}"
        issues.append(make_issue(
            "old_wordpress", "medium", f"Outdated WordPress ({version})",
            f"The site announces it runs WordPress {version}, which is several years behind and no longer gets full updates.",
        ))
    if home["old_jquery"]:
        issues.append(make_issue(
            "old_jquery", "low", f"Old scripts (jQuery {home['old_jquery']})",
            "The site relies on a script library version that was retired years ago.",
        ))
    if home["builder"]:
        issues.append(make_issue(
            "site_builder", "low", f"Built on {home['builder']}",
            f"A do-it-yourself {home['builder']} site. A custom build would be faster, rank better and look less like a template.",
        ))

    # Search visibility
    if home["noindex"]:
        issues.append(make_issue(
            "noindex", "high", "Hidden from Google",
            "The homepage tells search engines not to list it, so it will not appear in Google results.",
        ))
    if not home["title"]:
        issues.append(make_issue(
            "no_title", "high", "No page title",
            "The homepage has no title, so Google and browser tabs have nothing meaningful to show.",
        ))
    elif len(home["title"]) < 15:
        issues.append(make_issue(
            "short_title", "low", "Weak page title",
            f"The title is just \"{home['title']}\". It says nothing about the service or the location.",
        ))
    if not home["description"]:
        issues.append(make_issue(
            "no_meta_description", "medium", "No search description",
            "There is no summary for Google to show under the site's name, so the search listing is left to chance.",
        ))
    if not js_rendered:
        if home["h1_count"] == 0:
            issues.append(make_issue(
                "no_h1", "medium", "No main heading",
                "The homepage has no main heading, which search engines use to understand what the page is about.",
            ))
        if home["image_count"] >= 5 and home["images_missing_alt"] / home["image_count"] > 0.4:
            issues.append(make_issue(
                "images_no_alt", "low", "Images have no descriptions",
                f"{home['images_missing_alt']} of {home['image_count']} images have no text description, which hurts Google image search and screen-reader users.",
            ))
    if not home["has_structured_data"]:
        issues.append(make_issue(
            "no_structured_data", "low", "No business details for Google",
            "The site does not label its name, address, hours or reviews in the format Google uses for rich search results.",
        ))
    if not home["has_og"]:
        issues.append(make_issue(
            "no_og", "low", "No preview when shared",
            "Links to this site shared on WhatsApp, Facebook or LinkedIn show no image or summary.",
        ))
    if not has_sitemap:
        issues.append(make_issue(
            "no_sitemap", "low", "No sitemap",
            "There is no sitemap to help search engines find every page.",
        ))
    if js_rendered:
        issues.append(make_issue(
            "js_rendered", "medium", "Content only appears with JavaScript",
            "Without JavaScript the page is almost empty, so search engines and slow phones may see a blank page.",
        ))

    # Turning visitors into customers
    if not js_rendered:
        if not any_page("has_contact_form"):
            issues.append(make_issue(
                "no_contact_form", "medium", "No enquiry form",
                "Visitors cannot send a message from the site; they have to phone or write their own email.",
            ))
        if not any_page("has_tel"):
            issues.append(make_issue(
                "no_click_to_call", "low", "Phone number is not tappable",
                "Mobile visitors cannot tap to call; they have to copy the number by hand.",
            ))
        if not any_page("has_booking"):
            issues.append(make_issue(
                "no_booking", "low", "No online booking",
                "Customers cannot book or reserve online, so the business only takes bookings while someone answers the phone.",
                "app",
            ))
    if not any_page("has_chat"):
        issues.append(make_issue(
            "no_chat", "low", "No chat or instant answers",
            "There is no chat or WhatsApp button, so questions outside office hours go unanswered.",
            "ai",
        ))
    if not any_page("has_analytics"):
        issues.append(make_issue(
            "no_analytics", "medium", "No visitor tracking",
            "The site has no analytics, so the owner cannot see how many people visit or which pages bring enquiries.",
            "data",
        ))

    # Upkeep
    if broken_links:
        severity = "high" if len(broken_links) >= 3 else "medium"
        issues.append(make_issue(
            "broken_links", severity, f"{len(broken_links)} broken link{'s' if len(broken_links) != 1 else ''}",
            f"{len(broken_links)} of {links_checked} links checked lead to a \"page not found\" error, for example {broken_links[0]}",
        ))
    if not favicon_ok:
        issues.append(make_issue(
            "no_favicon", "low", "No site icon",
            "The browser tab shows a blank icon instead of the business's logo.",
        ))

    issues.sort(key=lambda i: SEVERITY_ORDER[i["severity"]])
    return issues


def build_ideas(issues: list[dict[str, str]], lead: dict[str, Any] | None = None) -> list[dict[str, str]]:
    codes = {i["code"] for i in issues}
    ideas: list[dict[str, str]] = []
    lead = lead or {}

    if "no_website" in codes or "social_only" in codes:
        ideas.append(make_idea("website", "Build their first proper website", "A fast, mobile-first site with services, prices, reviews and an enquiry form, so they stop relying on a listing they do not control."))
    if codes & {"site_down", "site_error"}:
        ideas.append(make_idea("website", "Get them back online", "Their listed website is not loading. Offer to restore it or replace it with a reliable new site."))
    if codes & {"no_viewport", "old_markup", "old_copyright", "site_builder", "old_wordpress", "ssl_error", "no_https"}:
        ideas.append(make_idea("website", "Modern mobile-first redesign", "Rebuild the site on current technology so it looks right on phones, loads securely and is easy for them to update."))
    if codes & {"slow_response", "pagespeed_low", "many_scripts", "heavy_html", "no_compression", "no_lazy_images"}:
        ideas.append(make_idea("website", "Speed tune-up", "Caching, image optimisation and script clean-up to get pages loading in under two seconds."))
    seo_codes = codes & {"no_title", "short_title", "no_meta_description", "no_h1", "no_structured_data", "no_sitemap", "noindex", "images_no_alt", "no_og"}
    if len(seo_codes) >= 2:
        ideas.append(make_idea("website", "Local search clean-up", "Fix titles, descriptions and business details so the site shows up properly for local Google searches."))
    if "no_contact_form" in codes or "no_click_to_call" in codes:
        ideas.append(make_idea("website", "Make it easy to enquire", "Add a short enquiry form and tap-to-call button that send leads straight to their email or WhatsApp."))
    if "no_booking" in codes:
        ideas.append(make_idea("app", "Online booking system", "Let customers book and pay a deposit online at any hour, with automatic reminders to cut no-shows."))
    if "no_chat" in codes:
        ideas.append(make_idea("ai", "AI assistant for enquiries", "A chat assistant trained on their services and prices that answers questions and collects contact details around the clock."))
    if "no_analytics" in codes:
        ideas.append(make_idea("data", "Visitor and enquiry dashboard", "Set up tracking and a simple monthly dashboard showing visitors, enquiries and which pages bring in work."))

    rating = lead.get("rating")
    reviews = lead.get("reviews") or 0
    if rating and rating >= 4.3 and reviews >= 30:
        ideas.append(make_idea("website", "Show off their reviews", f"They have {reviews} Google reviews averaging {rating}. Feature them on the site to win trust faster."))
    elif reviews < 20 and "no_website" not in codes:
        ideas.append(make_idea("ai", "Automatic review requests", "Send customers a review request after each job to build up their Google rating."))
    return ideas


def score_issues(issues: list[dict[str, str]]) -> int:
    total = sum(SEVERITY_WEIGHT[i["severity"]] for i in issues)
    return max(0, min(100, total))


def summarise(issues: list[dict[str, str]], ideas: list[dict[str, str]]) -> tuple[list[str], list[str]]:
    top = [i["title"] for i in issues[:3]]
    services: list[str] = []
    for item in issues + ideas:
        if item["service"] not in services:
            services.append(item["service"])
    return top, services


# --------------------------------------------------------------------------
# Results for leads without a readable site
# --------------------------------------------------------------------------

def result_no_website(lead: dict[str, Any] | None = None, platform: str | None = None) -> dict[str, Any]:
    if platform:
        issues = [make_issue(
            "social_only", "high", f"Only has {platform}, no website",
            f"The business sends people to {platform} instead of its own website, so it does not control how it appears or show up well in Google.",
        )]
        score = 80
    else:
        issues = [make_issue(
            "no_website", "high", "No website",
            "Google lists no website for this business, so anyone searching finds competitors instead.",
        )]
        score = 85
    ideas = build_ideas(issues, lead)
    top, services = summarise(issues, ideas)
    return {
        "audit_status": "no_website",
        "opportunity_score": score,
        "top_issues": top,
        "services": services,
        "emails": [],
        "audit": {
            "checked_at": now_iso(), "final_url": None, "pages_checked": [], "load_ms": None,
            "page_kb": None, "https": None, "platform": platform, "pagespeed_mobile": None,
            "error": None, "issues": issues, "ideas": ideas, "social": {}, "phones_found": [],
        },
    }


def result_unreadable(status: str, message: str, issues: list[dict[str, str]] | None = None) -> dict[str, Any]:
    issues = issues or []
    ideas = build_ideas(issues) if issues else []
    top, services = summarise(issues, ideas)
    return {
        "audit_status": status,
        "opportunity_score": score_issues(issues) if issues else None,
        "top_issues": top,
        "services": services,
        "emails": [],
        "audit": {
            "checked_at": now_iso(), "final_url": None, "pages_checked": [], "load_ms": None,
            "page_kb": None, "https": None, "platform": None, "pagespeed_mobile": None,
            "error": message, "issues": issues, "ideas": ideas, "social": {}, "phones_found": [],
        },
    }


# --------------------------------------------------------------------------
# The audit itself
# --------------------------------------------------------------------------

async def _audit(client: httpx.AsyncClient, raw_url: str, lead: dict[str, Any] | None, pagespeed_key: str) -> dict[str, Any]:
    url = normalize_url(raw_url)
    if not url:
        return result_unreadable("failed", "That does not look like a website address.")

    platform = is_social_or_directory_url(url)
    if platform:
        return result_no_website(lead, platform)

    started_https = url.lower().startswith("https://")
    robots, robots_exists, has_sitemap = await load_robots(client, url)
    if not allowed(robots, url):
        return result_unreadable("blocked", "This site asks automated tools not to read it, so it was left alone.")

    ssl_broken = False
    home_fetch = await fetch_page(client, url)
    if not home_fetch.ok and home_fetch.error_kind == "ssl":
        # A broken certificate is itself a finding. See whether plain http still serves the site.
        ssl_broken = True
        http_url = "http://" + url.split("://", 1)[1]
        retry = await fetch_page(client, http_url)
        if retry.ok:
            home_fetch = retry
        else:
            issue = make_issue(
                "ssl_error", "high", "Security certificate problem",
                "Browsers show a full-page warning before visitors can reach this site, so most people leave.",
            )
            return result_unreadable("failed", home_fetch.error, [issue])
    elif not home_fetch.ok and home_fetch.error_kind == "connect" and started_https:
        # Some old sites only answer on http.
        retry = await fetch_page(client, "http://" + url.split("://", 1)[1])
        if retry.ok:
            home_fetch = retry

    if not home_fetch.ok:
        issues = []
        if home_fetch.error_kind in ("timeout", "connect"):
            issues.append(make_issue(
                "site_down", "high", "Website is not loading",
                "The address Google lists for this business did not load. Customers who click it get an error.",
            ))
        return result_unreadable("failed", home_fetch.error, issues)

    if home_fetch.status in (401, 403, 429) or home_fetch.status == 503 and "cf-ray" in home_fetch.headers:
        return result_unreadable("blocked", "The site refused automated access, so it could not be checked.")
    if home_fetch.status >= 400:
        issue = make_issue(
            "site_error", "high", f"Website shows an error ({home_fetch.status})",
            f"The address Google lists for this business returns error {home_fetch.status} instead of a page.",
        )
        return result_unreadable("failed", f"The site returned error {home_fetch.status}.", [issue])
    content_type = home_fetch.headers.get("content-type", "").lower()
    if content_type and "html" not in content_type:
        return result_unreadable("failed", "The address does not lead to a web page.")

    # robots.txt on the final host may differ from the one we started with
    if registrable_domain(urlparse(home_fetch.url).netloc) != registrable_domain(urlparse(url).netloc):
        robots, robots_exists, has_sitemap = await load_robots(client, home_fetch.url)
        if not allowed(robots, home_fetch.url):
            return result_unreadable("blocked", "This site asks automated tools not to read it, so it was left alone.")
        landed_on = is_social_or_directory_url(home_fetch.url)
        if landed_on:
            return result_no_website(lead, landed_on)

    home = analyze_html(home_fetch.text, home_fetch.url)
    pages = [home]
    pages_checked = [home_fetch.url]
    site_host = urlparse(home_fetch.url).netloc

    # Up to two contact/about pages, for contact details and forms
    extra_urls = [u for u in home["contact_links"] if u.rstrip("/") != home_fetch.url.rstrip("/") and allowed(robots, u)]
    extra_urls = extra_urls[:MAX_EXTRA_PAGES]
    if extra_urls:
        extra_fetches = await asyncio.gather(*(fetch_page(client, u, timeout=12.0) for u in extra_urls))
        for fetched in extra_fetches:
            if fetched.ok and fetched.status == 200 and "html" in fetched.headers.get("content-type", "html").lower():
                pages.append(analyze_html(fetched.text, fetched.url))
                pages_checked.append(fetched.url)

    # A small sample of the site's own links, to spot dead pages
    already = {p.rstrip("/") for p in pages_checked} | {u.rstrip("/") for u in extra_urls}
    sample = [u for u in home["internal_links"] if u.rstrip("/") not in already and allowed(robots, u)][:MAX_LINK_CHECKS]
    origin = f"{urlparse(home_fetch.url).scheme}://{site_host}"
    side_checks: list[Any] = [link_status(client, u) for u in sample]
    need_favicon = not home["has_favicon_tag"]
    need_sitemap = not has_sitemap
    if need_favicon:
        side_checks.append(link_status(client, origin + "/favicon.ico"))
    if need_sitemap:
        side_checks.append(link_status(client, origin + "/sitemap.xml"))
    use_psi = bool(pagespeed_key)
    if use_psi:
        side_checks.append(pagespeed_score(client, home_fetch.url, pagespeed_key))
    results = await asyncio.gather(*side_checks) if side_checks else []

    link_results = results[:len(sample)]
    rest = list(results[len(sample):])
    favicon_ok = True
    if need_favicon:
        favicon_ok = rest.pop(0) == 200
    if need_sitemap:
        has_sitemap = rest.pop(0) == 200
    pagespeed = rest.pop(0) if use_psi else None
    broken = [u for u, status in zip(sample, link_results) if status in (404, 410)]

    issues = build_issues(
        home, pages,
        fetched=home_fetch, started_https=started_https, ssl_broken=ssl_broken,
        robots_exists=robots_exists, has_sitemap=has_sitemap,
        broken_links=broken, links_checked=len(sample), favicon_ok=favicon_ok, pagespeed=pagespeed,
    )
    ideas = build_ideas(issues, lead)
    top, services = summarise(issues, ideas)

    emails: list[str] = []
    phones: list[str] = []
    social: dict[str, str] = {}
    for page in pages:
        for email in extract_emails(page["soup"], site_host):
            if email not in emails:
                emails.append(email)
        for phone in extract_phones(page["soup"]):
            if re.sub(r"\D", "", phone) not in {re.sub(r"\D", "", p) for p in phones}:
                phones.append(phone)
        for network, link in extract_social(page["soup"]).items():
            social.setdefault(network, link)

    return {
        "audit_status": "done",
        "opportunity_score": score_issues(issues),
        "top_issues": top,
        "services": services,
        "emails": emails[:5],
        "audit": {
            "checked_at": now_iso(),
            "final_url": home_fetch.url,
            "pages_checked": pages_checked,
            "load_ms": home_fetch.elapsed_ms,
            "page_kb": max(1, home_fetch.size // 1024),
            "https": urlparse(home_fetch.url).scheme == "https" and not ssl_broken,
            "platform": home["platform"],
            "pagespeed_mobile": pagespeed,
            "error": None,
            "issues": issues,
            "ideas": ideas,
            "social": social,
            "phones_found": phones[:3],
        },
    }


async def audit_site(
    client: httpx.AsyncClient,
    raw_url: str | None,
    lead: dict[str, Any] | None = None,
    pagespeed_key: str = "",
) -> dict[str, Any]:
    """Audit one website. Never raises: problems come back as a 'failed' result."""
    if not raw_url or not raw_url.strip():
        return result_no_website(lead)
    budget = SITE_BUDGET_SECONDS + (75.0 if pagespeed_key else 0.0)
    try:
        return await asyncio.wait_for(_audit(client, raw_url, lead, pagespeed_key), timeout=budget)
    except asyncio.TimeoutError:
        return result_unreadable("failed", "Checking this site took too long, so it was skipped.")
    except Exception as exc:  # one odd site must not stop the whole search
        log.exception("Audit crashed for %s", raw_url)
        return result_unreadable("failed", f"Something unexpected went wrong while reading this site ({type(exc).__name__}).")
