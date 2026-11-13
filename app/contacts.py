"""Pull publicly listed contact details out of a web page.

Only what the business itself publishes on its own pages: mailto links,
visible email addresses, tel links and social profile links.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+\-]{0,63}@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,62}\.)+[A-Za-z]{2,24}")

# Addresses that show up in page source but belong to tools, not the business.
JUNK_DOMAINS = {
    "example.com", "example.org", "domain.com", "email.com", "yourdomain.com",
    "sentry.io", "sentry-next.wixpress.com", "wixpress.com", "wix.com",
    "schema.org", "w3.org", "godaddy.com", "squarespace.com", "wordpress.org",
    "wordpress.com", "gravatar.com", "googleapis.com", "google.com",
    "mysite.com", "website.com", "company.com", "sitename.com",
}
JUNK_LOCAL_PARTS = {"you", "your", "name", "email", "user", "username", "example", "yourname", "youremail"}
FILE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js", ".woff", ".woff2", ".ico")
GENERIC_PREFIXES = ("info", "contact", "hello", "sales", "office", "admin", "enquiries", "inquiries", "support", "team", "mail")

SOCIAL_HOSTS = {
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "instagram.com": "instagram",
    "linkedin.com": "linkedin",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "youtube.com": "youtube",
    "tiktok.com": "tiktok",
    "pinterest.com": "pinterest",
}
SHARE_MARKERS = ("sharer", "share.php", "/share", "intent/tweet", "/shareArticle", "pin/create", "/plugins/", "/tr?")


def registrable_domain(host: str) -> str:
    """Rough 'main domain' of a host: good enough to compare a site and an email."""
    host = (host or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # Handle common two-part suffixes such as .co.uk, .com.au, .com.pk
    if parts[-2] in {"co", "com", "org", "net", "gov", "edu", "ac"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def decode_cfemail(encoded: str) -> str:
    """Decode Cloudflare's email obfuscation (data-cfemail attribute)."""
    try:
        key = int(encoded[:2], 16)
        chars = [chr(int(encoded[i:i + 2], 16) ^ key) for i in range(2, len(encoded), 2)]
        return "".join(chars)
    except (ValueError, IndexError):
        return ""


def _valid_email(email: str) -> bool:
    email = email.lower()
    if email.endswith(FILE_SUFFIXES):
        return False
    local, _, domain = email.partition("@")
    if not local or not domain:
        return False
    if local in JUNK_LOCAL_PARTS:
        return False
    if domain in JUNK_DOMAINS or any(domain.endswith("." + junk) for junk in JUNK_DOMAINS):
        return False
    # Long hex strings are tracking ids, not mailboxes.
    if re.fullmatch(r"[0-9a-f]{20,}", local):
        return False
    # "image@2x.png"-style retina filenames
    if re.fullmatch(r"\dx", domain.split(".")[0]):
        return False
    return True


def extract_emails(soup: BeautifulSoup, site_host: str = "") -> list[str]:
    found: list[str] = []

    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if href.lower().startswith("mailto:"):
            address = unquote(href[7:].split("?")[0]).strip()
            for part in re.split(r"[,;]", address):
                match = EMAIL_RE.search(part)
                if match:
                    found.append(match.group(0))

    for tag in soup.find_all(attrs={"data-cfemail": True}):
        decoded = decode_cfemail(tag["data-cfemail"])
        match = EMAIL_RE.search(decoded)
        if match:
            found.append(match.group(0))

    text = soup.get_text(" ", strip=True)
    found.extend(EMAIL_RE.findall(text))

    site_domain = registrable_domain(site_host)
    seen: set[str] = set()
    cleaned: list[str] = []
    for email in found:
        email = email.strip(".").lower()
        if email in seen or not _valid_email(email):
            continue
        seen.add(email)
        cleaned.append(email)

    def rank(email: str) -> tuple[int, int]:
        local, _, domain = email.partition("@")
        own_domain = 0 if site_domain and registrable_domain(domain) == site_domain else 1
        generic = 0 if local.startswith(GENERIC_PREFIXES) else 1
        return (own_domain, generic)

    cleaned.sort(key=rank)
    return cleaned[:5]


def extract_phones(soup: BeautifulSoup) -> list[str]:
    phones: list[str] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if not href.lower().startswith("tel:"):
            continue
        raw = unquote(href[4:]).strip()
        digits = re.sub(r"\D", "", raw)
        if len(digits) < 7 or len(digits) > 15 or digits in seen:
            continue
        seen.add(digits)
        phones.append(re.sub(r"\s+", " ", raw))
    return phones[:3]


def extract_social(soup: BeautifulSoup) -> dict[str, str]:
    social: dict[str, str] = {}
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if not href.lower().startswith(("http://", "https://")):
            continue
        if any(marker in href for marker in SHARE_MARKERS):
            continue
        host = registrable_domain(urlparse(href).netloc)
        network = SOCIAL_HOSTS.get(host)
        if network and network not in social:
            path = urlparse(href).path.strip("/")
            if path:  # a bare facebook.com link is not a profile
                social[network] = href
    return social


def is_social_or_directory_url(url: str) -> str | None:
    """Return the platform name if a 'website' is really a social/listing page."""
    host = registrable_domain(urlparse(url if "://" in url else "https://" + url).netloc)
    platforms = {
        "facebook.com": "Facebook",
        "fb.com": "Facebook",
        "instagram.com": "Instagram",
        "linktr.ee": "Linktree",
        "wa.me": "WhatsApp",
        "whatsapp.com": "WhatsApp",
        "yelp.com": "Yelp",
        "tiktok.com": "TikTok",
        "linkedin.com": "LinkedIn",
        "x.com": "X",
        "twitter.com": "X",
        "youtube.com": "YouTube",
        "business.site": "a Google Business page",
        "tripadvisor.com": "Tripadvisor",
    }
    return platforms.get(host)
