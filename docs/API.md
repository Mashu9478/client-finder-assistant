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
  "has_draft": false,
  "created_at": "2026-11-13T17:20:00Z",
  "updated_at": "2026-11-13T17:21:10Z"
}
```

- `has_draft`: an outreach email has been written and saved for this lead.
- `has_report`: a PDF report and homepage concept picture have been created
  for this lead (list form also carries `"has_report": false`).

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

The detail form also carries the saved outreach email, or `null`:

```json
{
  "email": {
    "to": "info@brightsmile.example",
    "subject": "Three quick fixes for Bright Smile Dental's website",
    "body": "Hello Bright Smile Dental team,\n\n…",
    "source": "claude",
    "note": null,
    "edited": false,
    "generated_at": "2026-11-13T20:40:00Z"
  }
}
```

- `to`: the lead's first email address, or `null` when none was found.
- `source`: `claude` (written by the Claude API), `template` (built-in
  wording, used when no Claude key is saved or Claude could not be reached),
  or `manual` (typed by the user with no generated draft).
- `note`: a sentence to show as a warning, for example why Claude was not
  used. Usually `null`.
- `edited`: the user has changed the text since it was generated.
- `body` is plain text with `\n` line breaks.

The detail form also carries the report, or `null`:

```json
{
  "report": {
    "generated_at": "2026-11-13T20:45:00Z",
    "pdf_url": "/api/leads/12/report.pdf",
    "image_url": "/api/leads/12/concept.png"
  }
}
```

- `pdf_url` downloads the PDF (add `?inline=true` to open it in a tab).
- `image_url` shows the concept picture (add `?download=true` to download
  it). Both are regenerated in place, so add a cache-busting query such as
  `&v=<generated_at>` when showing the image.

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
  "claude_model": "claude-sonnet-5-5",
  "has_google_key": false,
  "has_claude_key": false,
  "search_source": "openstreetmap",
  "email_writer": "template"
}
```

`claude_model` is writable: `claude-sonnet-5-5` (best quality, default) or
`claude-haiku-5-5` (fastest and cheapest). `email_writer` is read-only:
`claude` when a Claude key is saved, otherwise `template`.

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
| GET | `/api/leads` | query: `search_id`, `status`, `min_score`, `has_email` (`true`), `has_draft` (`true`), `has_website` (`true`/`false`), `q` (text in name/address/website), `sort` (`score` default, `newest`, `name`, `rating`) | `[Lead list form]` |
| POST | `/api/leads` | `{"website": "example.com", "name": "optional"}` — add a site by hand and audit it | Lead list form (HTTP 201), `audit_status` `pending` |
| GET | `/api/leads/{id}` | — | Lead detail form |
| PATCH | `/api/leads/{id}` | any of `status`, `notes`, `email_subject`, `email_body` | Lead detail form. Sending `email_subject` / `email_body` saves the user's edits to the draft (and creates a `manual` draft if there was none). |
| POST | `/api/leads/{id}/email` | — | Lead detail form with `email` filled in. Writes (or rewrites) the outreach email from the audit's issues and ideas and saves it. Can take up to a minute. 409 while the website check is still running or when there are no findings to write about; 400 when the user's name is missing from Settings. |
| DELETE | `/api/leads/{id}/email` | — | HTTP 204. Discards the saved draft. |
| POST | `/api/leads/{id}/report` | — | Lead detail form with `report` filled in. Creates (or recreates) the PDF report and the homepage concept picture. Takes 5–30 seconds. 409 while the website check is running or when the site could not be read; 400 when the user's name is missing from Settings; 500 with a `detail` sentence if the PDF could not be made (for example no Edge or Chrome installed). |
| GET | `/api/leads/{id}/report.pdf` | `inline` (`true`) | The PDF file. 404 if none exists. |
| GET | `/api/leads/{id}/concept.png` | `download` (`true`) | The concept picture (1440×1800 PNG). 404 if none exists. |
| DELETE | `/api/leads/{id}/report` | — | HTTP 204. Removes the report and picture. |
| POST | `/api/leads/{id}/reaudit` | — | Lead list form with `audit_status` `pending` |
| DELETE | `/api/leads/{id}` | — | HTTP 204 |
| GET | `/api/leads/export.csv` | same query as `GET /api/leads` | CSV file download |

## Polling

There are no websockets. While any search has a status other than `done` or
`error`, or any visible lead has `audit_status` `pending` or `running`, the UI
re-fetches every ~2 seconds.
