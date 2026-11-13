"""Find businesses for free with OpenStreetMap data. No key, no card.

Two public services are used, both run by volunteers and both asking for
light, identified use:

- Nominatim turns "Houston, Texas" into an area on the map (one request per search).
- Overpass lists the businesses of a given kind inside that area.

OpenStreetMap is community-edited. It lists fewer businesses than Google,
has no ratings, and often lacks a phone number or website even when the
business has one.

Data (c) OpenStreetMap contributors, ODbL. https://www.openstreetmap.org/copyright
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from . import __version__
from .places import PlacesError

# The public servers turn away requests that do not say plainly which program is asking.
USER_AGENT = f"ClientFinderAssistant/{__version__} (+https://github.com/Mashu9478/client-finder-assistant)"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# The main public server first, then an independent mirror if it is busy.
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)

MAX_RESULTS = 60
MAX_AREA_SQ_DEGREES = 6.0     # roughly a large metropolitan area; a state or country is refused
MIN_HALF_SPAN = 0.04          # a street address is widened to a few kilometres around it
BUSINESS_KEYS = ("amenity", "shop", "craft", "office", "healthcare", "tourism", "leisure")
KEY_REGEX = "^(" + "|".join(BUSINESS_KEYS) + ")$"

# What people type -> how OpenStreetMap tags it. Longest phrases are matched first.
CATEGORY_TAGS: dict[str, list[tuple[str, str]]] = {
    # health
    "dentist": [("amenity", "dentist"), ("healthcare", "dentist")],
    "dental": [("amenity", "dentist"), ("healthcare", "dentist")],
    "orthodontist": [("amenity", "dentist"), ("healthcare", "dentist")],
    "doctor": [("amenity", "doctors"), ("healthcare", "doctor")],
    "clinic": [("amenity", "clinic"), ("amenity", "doctors"), ("healthcare", "clinic")],
    "medical": [("amenity", "clinic"), ("amenity", "doctors")],
    "hospital": [("amenity", "hospital")],
    "pharmacy": [("amenity", "pharmacy")],
    "chemist": [("amenity", "pharmacy"), ("shop", "chemist")],
    "vet": [("amenity", "veterinary")],
    "veterinar": [("amenity", "veterinary")],
    "physio": [("healthcare", "physiotherapist")],
    "chiropract": [("healthcare", "chiropractor")],
    "optician": [("shop", "optician"), ("healthcare", "optometrist")],
    "optometrist": [("shop", "optician"), ("healthcare", "optometrist")],
    "psycholog": [("healthcare", "psychotherapist"), ("healthcare", "psychologist")],
    "therapist": [("healthcare", "psychotherapist"), ("healthcare", "physiotherapist")],
    # food and stay
    "restaurant": [("amenity", "restaurant")],
    "cafe": [("amenity", "cafe")],
    "coffee": [("amenity", "cafe"), ("shop", "coffee")],
    "bakery": [("shop", "bakery")],
    "bar": [("amenity", "bar")],
    "pub": [("amenity", "pub")],
    "fast food": [("amenity", "fast_food")],
    "takeaway": [("amenity", "fast_food")],
    "caterer": [("craft", "caterer")],
    "catering": [("craft", "caterer")],
    "hotel": [("tourism", "hotel")],
    "guest house": [("tourism", "guest_house")],
    "guesthouse": [("tourism", "guest_house")],
    "hostel": [("tourism", "hostel")],
    "motel": [("tourism", "motel")],
    # beauty and fitness
    "salon": [("shop", "hairdresser"), ("shop", "beauty")],
    "hairdresser": [("shop", "hairdresser")],
    "hair": [("shop", "hairdresser")],
    "barber": [("shop", "hairdresser")],
    "beauty": [("shop", "beauty")],
    "spa": [("leisure", "spa"), ("shop", "beauty"), ("shop", "massage")],
    "nail": [("shop", "beauty")],
    "massage": [("shop", "massage")],
    "tattoo": [("shop", "tattoo")],
    "gym": [("leisure", "fitness_centre")],
    "fitness": [("leisure", "fitness_centre")],
    "yoga": [("leisure", "fitness_centre")],
    "dance": [("leisure", "dance")],
    # trades
    "plumber": [("craft", "plumber")],
    "plumbing": [("craft", "plumber")],
    "electrician": [("craft", "electrician")],
    "carpenter": [("craft", "carpenter")],
    "painter": [("craft", "painter")],
    "roofer": [("craft", "roofer")],
    "roofing": [("craft", "roofer")],
    "hvac": [("craft", "hvac")],
    "air conditioning": [("craft", "hvac")],
    "heating": [("craft", "hvac"), ("craft", "plumber")],
    "builder": [("craft", "builder"), ("office", "construction_company")],
    "contractor": [("craft", "builder"), ("office", "construction_company")],
    "construction": [("office", "construction_company"), ("craft", "builder")],
    "locksmith": [("craft", "locksmith"), ("shop", "locksmith")],
    "gardener": [("craft", "gardener")],
    "landscap": [("craft", "gardener")],
    "cleaning": [("craft", "cleaning"), ("shop", "dry_cleaning")],
    "cleaner": [("craft", "cleaning"), ("shop", "dry_cleaning")],
    "laundry": [("shop", "laundry"), ("shop", "dry_cleaning")],
    "photographer": [("craft", "photographer"), ("shop", "photo")],
    "photo studio": [("craft", "photographer"), ("shop", "photo")],
    "tailor": [("craft", "tailor"), ("shop", "tailor")],
    "printer": [("craft", "printer"), ("shop", "copyshop")],
    "printing": [("craft", "printer"), ("shop", "copyshop")],
    "mover": [("office", "moving_company")],
    "moving": [("office", "moving_company")],
    "pest control": [("craft", "pest_control")],
    # professional services
    "lawyer": [("office", "lawyer")],
    "attorney": [("office", "lawyer")],
    "law firm": [("office", "lawyer")],
    "solicitor": [("office", "lawyer")],
    "notary": [("office", "notary")],
    "accountant": [("office", "accountant")],
    "accounting": [("office", "accountant")],
    "tax": [("office", "tax_advisor"), ("office", "accountant")],
    "real estate": [("office", "estate_agent")],
    "estate agent": [("office", "estate_agent")],
    "realtor": [("office", "estate_agent")],
    "property": [("office", "estate_agent")],
    "insurance": [("office", "insurance")],
    "architect": [("office", "architect")],
    "consult": [("office", "consulting")],
    "financial": [("office", "financial_advisor"), ("office", "financial")],
    "travel agen": [("shop", "travel_agency"), ("office", "travel_agent")],
    "marketing": [("office", "advertising_agency")],
    "advertising": [("office", "advertising_agency")],
    "recruit": [("office", "employment_agency")],
    "employment agen": [("office", "employment_agency")],
    "coworking": [("amenity", "coworking_space"), ("office", "coworking")],
    # cars
    "car repair": [("shop", "car_repair")],
    "auto repair": [("shop", "car_repair")],
    "mechanic": [("shop", "car_repair")],
    "garage": [("shop", "car_repair")],
    "car dealer": [("shop", "car")],
    "car wash": [("amenity", "car_wash")],
    "car rental": [("amenity", "car_rental")],
    "tyre": [("shop", "tyres")],
    "tire": [("shop", "tyres")],
    "driving school": [("amenity", "driving_school")],
    # education and care
    "school": [("amenity", "school")],
    "college": [("amenity", "college")],
    "kindergarten": [("amenity", "kindergarten")],
    "nursery": [("amenity", "kindergarten"), ("amenity", "childcare")],
    "daycare": [("amenity", "childcare"), ("amenity", "kindergarten")],
    "childcare": [("amenity", "childcare"), ("amenity", "kindergarten")],
    "language school": [("amenity", "language_school")],
    "music school": [("amenity", "music_school")],
    "tutor": [("amenity", "prep_school"), ("office", "tutoring")],
    "academy": [("amenity", "prep_school"), ("amenity", "school"), ("amenity", "college")],
    # shops
    "florist": [("shop", "florist")],
    "jewel": [("shop", "jewelry")],
    "clothes": [("shop", "clothes")],
    "clothing": [("shop", "clothes")],
    "boutique": [("shop", "clothes"), ("shop", "boutique")],
    "shoe": [("shop", "shoes")],
    "furniture": [("shop", "furniture")],
    "electronics": [("shop", "electronics")],
    "computer": [("shop", "computer")],
    "mobile phone": [("shop", "mobile_phone")],
    "phone shop": [("shop", "mobile_phone")],
    "supermarket": [("shop", "supermarket")],
    "grocery": [("shop", "supermarket"), ("shop", "convenience"), ("shop", "greengrocer")],
    "butcher": [("shop", "butcher")],
    "pet": [("shop", "pet")],
    "book": [("shop", "books")],
    "hardware": [("shop", "hardware"), ("shop", "doityourself")],
    "bike": [("shop", "bicycle")],
    "bicycle": [("shop", "bicycle")],
    "toy": [("shop", "toys")],
    "gift": [("shop", "gift")],
    "cosmetic": [("shop", "cosmetics"), ("shop", "beauty")],
    "makeup": [("shop", "cosmetics"), ("shop", "beauty")],
    "make-up": [("shop", "cosmetics"), ("shop", "beauty")],
    "make up": [("shop", "cosmetics"), ("shop", "beauty")],
    "perfume": [("shop", "perfumery")],
    "funeral": [("shop", "funeral_directors")],
    "event": [("amenity", "events_venue")],
    "wedding": [("amenity", "events_venue"), ("shop", "wedding")],
    "bank": [("amenity", "bank")],
}


def _clean(text: str) -> str:
    """Letters, digits, spaces and hyphens only, so the text is safe inside a query."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s\-]", " ", text, flags=re.UNICODE)).strip().lower()


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


FILLER_WORDS = {
    "business", "shop", "store", "company", "service", "firm", "place", "local", "best", "near", "me", "the", "in",
}

# Keywords that are word beginnings ("veterinar" covers veterinary and veterinarian).
STEMS = {
    "veterinar", "physio", "chiropract", "psycholog", "landscap", "consult", "jewel",
    "recruit", "travel agen", "employment agen",
}


def tags_for(query: str) -> tuple[list[tuple[str, str]], str]:
    """Return (OpenStreetMap tag filters, the cleaned singular phrase) for what was typed."""
    cleaned = _clean(query)
    # Words that describe any business add nothing: "makeup businesses" means "makeup".
    words = [w for w in cleaned.split() if _singular(w) not in FILLER_WORDS] or cleaned.split()
    if words:
        words[-1] = _singular(words[-1])
    phrase = " ".join(words)
    filters: list[tuple[str, str]] = []
    remaining = f"{cleaned} | {phrase}"
    for keyword in sorted(CATEGORY_TAGS, key=len, reverse=True):
        if keyword in STEMS:
            pattern = rf"(?<![a-z]){re.escape(keyword)}[a-z]*"
        else:
            pattern = rf"(?<![a-z]){re.escape(keyword)}(?:s|es)?(?![a-z])"
        if not re.search(pattern, remaining):
            continue
        # Blank the matched words so a shorter keyword cannot match inside them.
        remaining = re.sub(pattern, " ", remaining)
        for tag in CATEGORY_TAGS[keyword]:
            if tag not in filters:
                filters.append(tag)
        if len(filters) >= 5:
            break
    return filters, phrase


def build_overpass_query(query: str, bbox: tuple[float, float, float, float], limit: int) -> str:
    south, west, north, east = bbox
    box = f"({south:.5f},{west:.5f},{north:.5f},{east:.5f})"
    filters, phrase = tags_for(query)
    lines: list[str] = []
    for key, value in filters:
        lines.append(f'  nwr["{key}"="{value}"]["name"]{box};')
    if not filters and phrase:
        # Unknown kind of business. Try it as a tag value ("locksmith" -> craft=locksmith) ...
        value = re.sub(r"[\s\-]+", "_", phrase)
        for key in BUSINESS_KEYS:
            lines.append(f'  nwr["{key}"="{value}"]["name"]{box};')
        # ... and as words in a business's name.
        if len(phrase) >= 4:
            for key in ("shop", "amenity", "office", "craft"):
                lines.append(f'  nwr["{key}"]["name"~"{phrase}",i]{box};')
    body = "\n".join(lines)
    return f"[out:json][timeout:45];\n(\n{body}\n);\nout tags center {limit};"


async def find_area(client: httpx.AsyncClient, location: str) -> tuple[float, float, float, float]:
    """Turn a place name into (south, west, north, east)."""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en"}
    try:
        resp = await client.get(
            NOMINATIM_URL,
            params={"q": location, "format": "jsonv2", "limit": 1},
            headers=headers,
            timeout=25.0,
        )
    except httpx.HTTPError as exc:
        raise PlacesError("Could not reach the free map service. Check your internet connection and try again.") from exc
    if resp.status_code == 429:
        raise PlacesError("The free map service is busy. Wait a minute and try again.")
    if resp.status_code != 200:
        raise PlacesError(f"The free map service returned an error (HTTP {resp.status_code}). Try again in a minute.")
    try:
        results = resp.json()
    except ValueError:
        results = None
    if not isinstance(results, list) or not results:
        raise PlacesError(f"Could not find a place called \"{location}\". Try a city name with its country, like \"Leeds, UK\".")
    try:
        south, north, west, east = (float(v) for v in results[0]["boundingbox"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PlacesError(f"Could not work out where \"{location}\" is. Try a city name with its country.") from exc

    if (north - south) * (east - west) > MAX_AREA_SQ_DEGREES:
        raise PlacesError(f"\"{location}\" is too large an area for the free search. Name a city, town or district instead.")
    # A single address or building: widen to its neighbourhood.
    mid_lat, mid_lon = (south + north) / 2, (west + east) / 2
    if north - south < 2 * MIN_HALF_SPAN:
        south, north = mid_lat - MIN_HALF_SPAN, mid_lat + MIN_HALF_SPAN
    if east - west < 2 * MIN_HALF_SPAN:
        west, east = mid_lon - MIN_HALF_SPAN, mid_lon + MIN_HALF_SPAN
    return south, west, north, east


def _first(tags: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = tags.get(key)
        if isinstance(value, str) and value.strip():
            # OpenStreetMap separates several values with ";"
            return value.split(";")[0].strip()
    return None


def _address(tags: dict[str, Any]) -> str | None:
    street = " ".join(p for p in (tags.get("addr:housenumber"), tags.get("addr:street")) if p)
    parts = [street, tags.get("addr:suburb"), tags.get("addr:city"), tags.get("addr:postcode"), tags.get("addr:country")]
    text = ", ".join(str(p).strip() for p in parts if p and str(p).strip())
    return text or None


def _category(tags: dict[str, Any]) -> str | None:
    for key in BUSINESS_KEYS:
        value = tags.get(key)
        if isinstance(value, str) and value and value != "yes":
            return value.replace("_", " ").capitalize()
    return None


def parse_element(element: dict[str, Any]) -> dict[str, Any] | None:
    tags = element.get("tags") or {}
    name = (tags.get("name") or "").strip()
    if not name:
        return None
    if tags.get("shop") == "vacant" or any(str(k).startswith(("disused:", "abandoned:", "was:")) for k in tags):
        return None
    osm_type, osm_id = element.get("type"), element.get("id")
    if osm_type not in ("node", "way", "relation") or osm_id is None:
        return None
    emails = []
    for key in ("email", "contact:email"):
        for value in str(tags.get(key) or "").split(";"):
            value = value.strip().lower()
            if "@" in value and value not in emails:
                emails.append(value)
    return {
        "place_id": f"osm:{osm_type}/{osm_id}",
        "name": name[:200],
        "category": _category(tags),
        "address": _address(tags),
        "website": _first(tags, "website", "contact:website", "url"),
        "phone": _first(tags, "phone", "contact:phone", "contact:mobile", "mobile"),
        "emails": emails[:3],
        "rating": None,
        "reviews": None,
        "maps_url": f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
        "source": "openstreetmap",
    }


async def _run_overpass(client: httpx.AsyncClient, query: str) -> dict[str, Any]:
    """Try each public server in turn. Raises PlacesError naming what each one answered."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    problems: list[str] = []
    too_heavy = False
    for url in OVERPASS_URLS:
        host = url.split("/")[2]
        try:
            resp = await client.post(url, data={"data": query}, headers=headers, timeout=75.0)
        except httpx.TimeoutException:
            problems.append(f"{host}: no answer in time")
            continue
        except httpx.HTTPError as exc:
            problems.append(f"{host}: could not connect ({type(exc).__name__})")
            continue
        if resp.status_code == 400:
            raise PlacesError("The free search could not understand that kind of business. Try a simpler word, like \"dentist\" or \"plumber\".")
        if resp.status_code != 200:
            problems.append(f"{host}: HTTP {resp.status_code}")
            continue
        try:
            payload = resp.json()
        except ValueError:
            problems.append(f"{host}: unreadable answer")
            continue
        if not isinstance(payload, dict):
            problems.append(f"{host}: unreadable answer")
            continue
        remark = str(payload.get("remark") or "")
        if remark and not payload.get("elements"):
            # The server gave up part-way (usually out of time or memory).
            too_heavy = True
            problems.append(f"{host}: {remark[:80]}")
            continue
        return payload

    details = "; ".join(problems)
    if too_heavy:
        raise PlacesError(f"That search was too heavy for the free directory. Try a smaller place, such as one district of the city. (Details: {details})")
    raise PlacesError(f"The free business directory is busy or could not be reached. Wait a minute and try again. (Details: {details})")


async def search_osm(
    client: httpx.AsyncClient,
    query: str,
    location: str,
    max_results: int = 20,
) -> list[dict[str, Any]]:
    """Return up to ``max_results`` businesses of a kind in a place, from OpenStreetMap."""
    if not location.strip():
        raise PlacesError("The free search needs a place. Add a city or district in the \"Where?\" box.")
    if not _clean(query):
        raise PlacesError("Type what kind of business to look for.")
    max_results = max(1, min(MAX_RESULTS, max_results))

    bbox = await find_area(client, location.strip())
    # Ask for extra so the ones with a website or phone can be put first.
    payload = await _run_overpass(client, build_overpass_query(query, bbox, max(max_results * 4, 80)))

    places: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for element in payload.get("elements") or []:
        if not isinstance(element, dict):
            continue
        place = parse_element(element)
        if not place:
            continue
        key = (place["name"].lower(), (place["website"] or place["address"] or "").lower())
        if key in seen:
            continue
        seen.add(key)
        places.append(place)

    if not places:
        raise PlacesError(
            f"The free directory lists no \"{query.strip()}\" in \"{location.strip()}\". "
            "Try a simpler word (dentist, plumber, restaurant) or a bigger nearby city."
        )
    # Most useful first: a website to audit, then some way to contact them.
    places.sort(key=lambda p: (0 if p["website"] else 1, 0 if (p["phone"] or p["emails"]) else 1))
    return places[:max_results]
