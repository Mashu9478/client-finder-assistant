# Local API contract (stage 1)

The app is a local server on `http://127.0.0.1:8765`. The UI in `app/static/`
talks to it with `fetch`. All bodies are JSON. Errors come back as
`{"detail": "human-readable message"}` with a 4xx/5xx status.

Times are ISO 8601 UTC strings (`2026-11-13T17:20:00Z`).

## Objects

### Lead (list form)

```json
{
  "id": 12,
  "search_id": 3,
  "name": "Bright Smile Dental",
  "category": "Dentist",
  "address": "1200 Main St, Houston, TX 77002, USA",
  "website": "https://brightsmile.example",
  "phone": "+1 713-555-0142",
  "emails": ["info@brightsmile.example"],
  "rating": 4.6,
  "reviews": 212,
  "maps_url": "https://maps.google.com/?cid=123",
  "source": "google_places",
  "status": "new",
  "audit_status": "done",
  "opportunity_score": 72,
  "top_issues": ["Not mobile-friendly", "Slow to load (4.2 s)", "No online booking"],
  "services": ["website", "ai"],
  "notes": "",
  "created_at": "2026-11-13T17:20:00Z",
  "updated_at": "2026-11-13T17:21:10Z"
}
```

- `website`, `phone`, `rating`, `reviews`, `maps_url`, `category`, `address`,
  `search_id`, `opportunity_score` can be `null`.
- `source`: `google_places` | `openstreetmap` | `manual`.
- `status` (the user's pipeline): `new` | `shortlisted` | `contacted` |
  `replied` | `won` | `rejected`.
- `audit_status`: `pending` | `running` | `done` | `failed` | `no_website` |
  `blocked` (the site's robots.txt asks bots to stay out, so it was not read).
- `opportunity_score`: 0–100, higher means more to sell. `null` until audited.
  A business with no website scores high (`no_website`). For `openstreetmap`
  leads a blank website is only a hint, so those score a cautious 45.
- `services`: which of the user's services fit — any of `website`, `app`,
  `ai`, `data`.

### Lead (detail form)

Everything above plus:

```json
{
  "audit": {
    "checked_at": "2026-11-13T17:21:10Z",
    "final_url": "https://www.brightsmile.example/",
    "pages_checked": ["https://www.brightsmile.example/", "https://www.brightsmile.example/contact"],
    "load_ms": 4210,
    "page_kb": 1830,
    "https": true,
    "platform": "WordPress 5.2",
    "pagespeed_mobile": 34,
    "error": null,
    "issues": [
      {
        "code": "no_viewport",
        "severity": "high",
        "title": "Not mobile-friendly",
        "detail": "The page has no viewport tag, so phones show the desktop layout zoomed out.",
        "service": "website"
      }
    ],
    "ideas": [
      {
        "service": "ai",
        "title": "Add an AI chat assistant",
        "detail": "Answer common questions and capture enquiries outside office hours."
      }
    ],
    "social": {"facebook": "https://facebook.com/brightsmile"},
    "phones_found": ["+1 713-555-0142"]
  }
}
```

- `audit` is `null` until an audit has run.
- `severity`: `high` | `medium` | `low`.
- `audit.error` is a sentence when the site could not be read; `issues` may
  then be short or empty.
- `load_ms`, `page_kb`, `platform`, `pagespeed_mobile` can be `null`.

### Search

```json
{
  "id": 3,
  "query": "dentists",
  "location": "Houston, Texas",
  "max_results": 20,
  "status": "auditing",
  "found": 18,
  "audited": 7,
  "error": null,
  "created_at": "2026-11-13T17:20:00Z"
}
```

- `status`: `queued` | `finding` | `auditing` | `done` | `error`.
- `error` is a sentence when `status` is `error` (for example a missing or
  rejected Google key).

### Settings

```json
{
  "your_name": "",
  "business_name": "",
  "email": "",
  "whatsapp": "",
  "portfolio_url": "",
  "services": ["AI & automation", "Website development & redesign", "App development", "Data analysis"],
  "portfolio": [{"name": "Project", "description": "What it is", "url": "https://…"}],
  "use_pagespeed": false,
  "has_google_key": false,
  "has_claude_key": false,
  "search_source": "openstreetmap"
}
```

`search_source` is read-only: `google` when a Google key is saved, otherwise
`openstreetmap` (the free directory, which needs a `location` for every search).

API keys are write-only: the server never returns them, only `has_*_key`.

## Endpoints

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/api/settings` | — | Settings |
| PUT | `/api/settings` | any Settings fields, plus optional `google_api_key`, `claude_api_key` (omitted or `""` = keep the saved key) | Settings |
| POST | `/api/settings/test-google` | — | `{"ok": true, "message": "Google Places key works."}` (also `ok: false` with the reason; always HTTP 200) |
| GET | `/api/stats` | — | `{"total": 40, "with_email": 22, "with_phone": 35, "no_website": 6, "high_opportunity": 14, "by_status": {"new": 30, "shortlisted": 4, "contacted": 3, "replied": 2, "won": 1, "rejected": 0}}` |
| POST | `/api/searches` | `{"query": "dentists", "location": "Houston, Texas", "max_results": 20}` (`location` optional, `max_results` 1–60, default 20) | Search (HTTP 201). Runs in the background. Uses Google when a key is saved, otherwise the free OpenStreetMap directory; 400 if neither a key nor a `location` is given. |
| GET | `/api/searches` | — | `[Search]`, newest first |
| GET | `/api/searches/{id}` | — | Search |
| GET | `/api/leads` | query: `search_id`, `status`, `min_score`, `has_email` (`true`), `has_website` (`true`/`false`), `q` (text in name/address/website), `sort` (`score` default, `newest`, `name`, `rating`) | `[Lead list form]` |
| POST | `/api/leads` | `{"website": "example.com", "name": "optional"}` — add a site by hand and audit it | Lead list form (HTTP 201), `audit_status` `pending` |
| GET | `/api/leads/{id}` | — | Lead detail form |
| PATCH | `/api/leads/{id}` | `{"status": "shortlisted", "notes": "…"}` (either or both) | Lead detail form |
| POST | `/api/leads/{id}/reaudit` | — | Lead list form with `audit_status` `pending` |
| DELETE | `/api/leads/{id}` | — | HTTP 204 |
| GET | `/api/leads/export.csv` | same query as `GET /api/leads` | CSV file download |

## Polling

There are no websockets. While any search has a status other than `done` or
`error`, or any visible lead has `audit_status` `pending` or `running`, the UI
re-fetches every ~2 seconds.
