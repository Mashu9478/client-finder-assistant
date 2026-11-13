"""Find businesses with Google's Places API (New) - Text Search.

Docs: https://developers.google.com/maps/documentation/places/web-service/text-search
"""

from __future__ import annotations

from typing import Any

import httpx

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

# Only the fields the app uses. Phone and website put the request in Google's
# higher-priced "Enterprise" tier, so nothing beyond what is needed is asked for.
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.websiteUri",
    "places.nationalPhoneNumber",
    "places.internationalPhoneNumber",
    "places.rating",
    "places.userRatingCount",
    "places.googleMapsUri",
    "places.businessStatus",
    "places.primaryTypeDisplayName",
    "nextPageToken",
])

PAGE_SIZE = 20      # Google's maximum per page
MAX_RESULTS = 60    # Google's maximum per query (three pages)


class PlacesError(Exception):
    """A problem the user can act on, worded for them."""


def _friendly_error(status: int, payload: Any) -> str:
    message = ""
    reason = ""
    if isinstance(payload, dict):
        error = payload.get("error") or {}
        message = str(error.get("message") or "")
        for detail in error.get("details") or []:
            if isinstance(detail, dict) and detail.get("reason"):
                reason = str(detail["reason"])
                break
    lower = message.lower()
    if reason == "API_KEY_INVALID" or "api key not valid" in lower:
        return "Google rejected the API key. Check it was copied in full in Settings."
    if reason == "SERVICE_DISABLED" or "has not been used in project" in lower or "is disabled" in lower:
        return "The Places API (New) is not switched on for this Google project. Enable \"Places API (New)\" in Google Cloud, wait a minute, and try again."
    if reason == "BILLING_DISABLED" or "billing" in lower:
        return "Google needs billing enabled on the project before the Places API will answer."
    if reason in ("API_KEY_SERVICE_BLOCKED", "API_KEY_HTTP_REFERRER_BLOCKED", "API_KEY_IP_ADDRESS_BLOCKED") or "blocked" in lower:
        return "This API key has restrictions that block the Places API. Edit the key in Google Cloud and allow \"Places API (New)\"."
    if status == 429 or reason == "RATE_LIMIT_EXCEEDED" or "quota" in lower:
        return "Google's usage limit for this key has been reached. Try again later or raise the quota in Google Cloud."
    if message:
        return f"Google Places returned an error: {message}"
    return f"Google Places returned an unexpected error (HTTP {status})."


def _parse_place(raw: dict[str, Any]) -> dict[str, Any] | None:
    if raw.get("businessStatus") == "CLOSED_PERMANENTLY":
        return None
    name = ((raw.get("displayName") or {}).get("text") or "").strip()
    if not name:
        return None
    return {
        "place_id": raw.get("id"),
        "name": name,
        "category": ((raw.get("primaryTypeDisplayName") or {}).get("text") or None),
        "address": raw.get("formattedAddress"),
        "website": raw.get("websiteUri"),
        "phone": raw.get("internationalPhoneNumber") or raw.get("nationalPhoneNumber"),
        "rating": raw.get("rating"),
        "reviews": raw.get("userRatingCount"),
        "maps_url": raw.get("googleMapsUri"),
    }


def build_query(query: str, location: str) -> str:
    query = query.strip()
    location = location.strip()
    return f"{query} in {location}" if location else query


async def search_places(
    client: httpx.AsyncClient,
    api_key: str,
    query: str,
    location: str = "",
    max_results: int = 20,
) -> list[dict[str, Any]]:
    """Return up to ``max_results`` businesses matching the query."""
    if not api_key:
        raise PlacesError("No Google Places key is saved. Add it in Settings first.")
    max_results = max(1, min(MAX_RESULTS, max_results))
    text_query = build_query(query, location)
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    places: list[dict[str, Any]] = []
    seen: set[str] = set()
    page_token: str | None = None

    while len(places) < max_results:
        body: dict[str, Any] = {"textQuery": text_query, "pageSize": PAGE_SIZE}
        if page_token:
            body["pageToken"] = page_token
        try:
            resp = await client.post(SEARCH_URL, json=body, headers=headers, timeout=30.0)
        except httpx.TimeoutException as exc:
            raise PlacesError("Google Places took too long to answer. Check your internet connection and try again.") from exc
        except httpx.HTTPError as exc:
            raise PlacesError("Could not reach Google Places. Check your internet connection and try again.") from exc

        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if resp.status_code != 200:
            raise PlacesError(_friendly_error(resp.status_code, payload))
        if not isinstance(payload, dict):
            raise PlacesError("Google Places sent back something unreadable. Try again in a moment.")

        for raw in payload.get("places") or []:
            place = _parse_place(raw)
            if not place:
                continue
            key = place["place_id"] or place["name"]
            if key in seen:
                continue
            seen.add(key)
            places.append(place)
            if len(places) >= max_results:
                break

        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    return places


async def test_key(client: httpx.AsyncClient, api_key: str) -> tuple[bool, str]:
    """One tiny, cheap request to confirm the key works."""
    if not api_key:
        return False, "No Google Places key is saved yet."
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.id",
    }
    try:
        resp = await client.post(SEARCH_URL, json={"textQuery": "coffee in London", "pageSize": 1}, headers=headers, timeout=20.0)
    except httpx.HTTPError:
        return False, "Could not reach Google. Check your internet connection."
    if resp.status_code == 200:
        return True, "Google Places key works."
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    return False, _friendly_error(resp.status_code, payload)
