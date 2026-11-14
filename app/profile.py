"""What a business's own homepage says about it: its words and its colours.

The concept picture and report are built from this, so each one reflects the
real business rather than a stock layout. Everything here is read from the
public homepage (and, for colours, its main stylesheet).
"""

from __future__ import annotations

import colorsys
import re
from collections import Counter
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

HEX_RE = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-fA-F])")
RGB_RE = re.compile(r"rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})")

# Menu and heading words that say nothing about the particular business.
GENERIC_LABELS = {
    "home", "menu", "search", "login", "log in", "sign in", "sign up", "register", "cart", "basket", "skip to content",
    "skip to main content", "more", "read more", "learn more", "click here", "next", "previous", "close", "open",
    "privacy policy", "terms", "terms and conditions", "cookie policy", "cookies", "sitemap", "accessibility",
    "facebook", "instagram", "twitter", "linkedin", "youtube", "tiktok", "english", "español", "welcome",
}


def _clean_text(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip(" \t\r\n-–—|•·:>»")
    return text[:limit].rstrip()


def _hex6(value: str) -> str:
    value = value.lower()
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return "#" + value


def _rgb(hex6: str) -> tuple[int, int, int]:
    return int(hex6[1:3], 16), int(hex6[3:5], 16), int(hex6[5:7], 16)


def _to_hex(r: float, g: float, b: float) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c * 255)))) for c in (r, g, b))


def _hls(hex6: str) -> tuple[float, float, float]:
    r, g, b = _rgb(hex6)
    return colorsys.rgb_to_hls(r / 255, g / 255, b / 255)


def colour_counts(css_text: str) -> Counter[str]:
    """How often each colour appears in some CSS or HTML."""
    counts: Counter[str] = Counter()
    for match in HEX_RE.finditer(css_text):
        counts[_hex6(match.group(1))] += 1
    for match in RGB_RE.finditer(css_text):
        r, g, b = (int(v) for v in match.groups())
        if max(r, g, b) <= 255:
            counts["#%02x%02x%02x" % (r, g, b)] += 1
    return counts


def is_brand_colour(hex6: str) -> bool:
    """Colourful enough, and neither nearly white nor nearly black."""
    _hue, light, sat = _hls(hex6)
    return sat >= 0.28 and 0.14 <= light <= 0.72


def pick_brand_colours(counts: Counter[str], preferred: str | None = None) -> list[str]:
    """Up to two distinct brand colours, most used first."""
    ranked = [c for c, _n in counts.most_common(60) if is_brand_colour(c)]
    if preferred and re.fullmatch(r"#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3}", preferred.strip()):
        chosen = _hex6(preferred.strip()[1:])
        if is_brand_colour(chosen):
            ranked = [chosen] + [c for c in ranked if c != chosen]
    picked: list[str] = []
    for colour in ranked:
        hue = _hls(colour)[0]
        # A second colour must be a different hue, not a shade of the first.
        if all(min(abs(hue - _hls(p)[0]), 1 - abs(hue - _hls(p)[0])) > 0.08 for p in picked):
            picked.append(colour)
        if len(picked) == 2:
            break
    return picked


def make_palette(primary: str, accent: str | None = None) -> dict[str, str]:
    """A full colour set from a brand colour: primary, dark, tint and accent, all readable."""
    hue, light, sat = _hls(primary)
    sat = min(sat, 0.85)
    # White text must stay readable on the primary colour.
    light = min(light, 0.42)
    light = max(light, 0.24)
    primary_out = _to_hex(*colorsys.hls_to_rgb(hue, light, sat))
    dark = _to_hex(*colorsys.hls_to_rgb(hue, max(0.12, light * 0.55), min(sat, 0.7)))
    tint = _to_hex(*colorsys.hls_to_rgb(hue, 0.965, min(sat, 0.6)))
    if accent:
        a_hue, a_light, a_sat = _hls(accent)
        accent_out = _to_hex(*colorsys.hls_to_rgb(a_hue, min(max(a_light, 0.42), 0.58), min(max(a_sat, 0.6), 0.9)))
    else:
        # No second brand colour: use a warm or cool complement so buttons stand out.
        accent_out = _to_hex(*colorsys.hls_to_rgb((hue + 0.47) % 1.0, 0.5, 0.78))
    return {"primary": primary_out, "dark": dark, "tint": tint, "accent": accent_out}


def _labels(elements: list[Any], low: int, high: int, limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for element in elements:
        text = _clean_text(element.get_text(" ", strip=True), high + 1)
        key = text.lower()
        if not (low <= len(text) <= high) or key in seen or key in GENERIC_LABELS:
            continue
        if not re.search(r"[^\W\d_]", text, re.UNICODE):   # needs at least one letter
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def main_stylesheet_url(soup: BeautifulSoup, page_url: str) -> str | None:
    """The first stylesheet served by the site itself (not a font or CDN library)."""
    host = urlparse(page_url).netloc
    for link in soup.find_all("link", rel=True, href=True):
        if "stylesheet" not in [r.lower() for r in link.get("rel", [])]:
            continue
        try:
            absolute = urljoin(page_url, link["href"].strip())
        except ValueError:
            continue
        target = urlparse(absolute)
        if target.scheme in ("http", "https") and target.netloc == host:
            return absolute
    return None


def extract_profile(soup: BeautifulSoup, html: str, url: str) -> dict[str, Any]:
    """The business's own words and colours, from its homepage. Pure function."""
    def meta(name: str) -> str:
        tag = soup.find("meta", attrs={"name": re.compile(rf"^{re.escape(name)}$", re.I)}) or \
            soup.find("meta", attrs={"property": re.compile(rf"^{re.escape(name)}$", re.I)})
        return (tag.get("content") or "").strip() if tag else ""

    title = _clean_text(soup.title.get_text(" ", strip=True) if soup.title else "", 120)
    h1 = soup.find("h1")
    nav_links: list[Any] = []
    for nav in soup.find_all(["nav", "header"])[:3]:
        nav_links.extend(nav.find_all("a"))
    if len(nav_links) < 3:
        nav_links = soup.find_all("a")[:25]

    og_image = meta("og:image")
    if og_image:
        try:
            og_image = urljoin(url, og_image)
        except ValueError:
            og_image = ""
        if urlparse(og_image).scheme not in ("http", "https"):
            og_image = ""

    inline_css = " ".join(style.get_text(" ") for style in soup.find_all("style"))
    inline_css += " " + " ".join(tag.get("style", "") for tag in soup.find_all(style=True)[:400])
    counts = colour_counts(inline_css)

    return {
        "title": title,
        "description": _clean_text(meta("description") or meta("og:description"), 240),
        "h1": _clean_text(h1.get_text(" ", strip=True) if h1 else "", 110),
        "headings": _labels(soup.find_all(["h2", "h3"]), 4, 60, 8),
        "nav": _labels(nav_links, 2, 24, 7),
        "theme_color": meta("theme-color"),
        "og_image": og_image,
        "colour_counts": dict(counts.most_common(40)),
        "stylesheet": main_stylesheet_url(soup, url),
    }


def finish_profile(profile: dict[str, Any], stylesheet_css: str = "") -> dict[str, Any]:
    """Add the stylesheet's colours, choose the brand colours, and drop the working data."""
    counts: Counter[str] = Counter(profile.get("colour_counts") or {})
    if stylesheet_css:
        counts.update(colour_counts(stylesheet_css[:400_000]))
    brand = pick_brand_colours(counts, profile.get("theme_color"))
    return {
        "title": profile.get("title") or "",
        "description": profile.get("description") or "",
        "h1": profile.get("h1") or "",
        "headings": profile.get("headings") or [],
        "nav": profile.get("nav") or [],
        "og_image": profile.get("og_image") or "",
        "brand_colours": brand,
    }
