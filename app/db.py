"""Lead storage in a local SQLite file (data/leads.db)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from . import config
from .contacts import registrable_domain

LEAD_STATUSES = ("new", "shortlisted", "contacted", "replied", "won", "rejected")
AUDIT_STATUSES = ("pending", "running", "done", "failed", "no_website", "blocked")
HIGH_OPPORTUNITY = 60

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    max_results INTEGER NOT NULL DEFAULT 20,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id INTEGER,
    place_id TEXT UNIQUE,
    domain TEXT,
    name TEXT NOT NULL,
    category TEXT,
    address TEXT,
    website TEXT,
    phone TEXT,
    emails TEXT NOT NULL DEFAULT '[]',
    rating REAL,
    reviews INTEGER,
    maps_url TEXT,
    source TEXT NOT NULL DEFAULT 'google_places',
    status TEXT NOT NULL DEFAULT 'new',
    audit_status TEXT NOT NULL DEFAULT 'pending',
    opportunity_score INTEGER,
    top_issues TEXT NOT NULL DEFAULT '[]',
    services TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    audit TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_leads (
    search_id INTEGER NOT NULL,
    lead_id INTEGER NOT NULL,
    PRIMARY KEY (search_id, lead_id)
);
CREATE INDEX IF NOT EXISTS idx_leads_domain ON leads(domain);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            config.ensure_data_dir()
            _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
            _migrate(_conn)
            _conn.commit()
        return _conn


# Columns added after the first release. Existing lead files gain them on start-up.
ADDED_COLUMNS = {
    "email_subject": "TEXT",
    "email_body": "TEXT",
    "email_source": "TEXT",
    "email_note": "TEXT",
    "email_generated_at": "TEXT",
    "email_edited": "INTEGER NOT NULL DEFAULT 0",
    "report_generated_at": "TEXT",
}


def _migrate(db: sqlite3.Connection) -> None:
    existing = {row["name"] for row in db.execute("PRAGMA table_info(leads)")}
    for column, kind in ADDED_COLUMNS.items():
        if column not in existing:
            db.execute(f"ALTER TABLE leads ADD COLUMN {column} {kind}")


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def recover_interrupted() -> None:
    """After a restart, nothing is running any more. Put half-done work back in a clear state."""
    with _lock:
        db = connect()
        db.execute("UPDATE leads SET audit_status='pending' WHERE audit_status='running'")
        db.execute(
            "UPDATE searches SET status='error', error='The app was closed before this search finished.' "
            "WHERE status IN ('queued', 'finding')"
        )
        db.execute("UPDATE searches SET status='done' WHERE status='auditing'")
        db.commit()


def website_domain(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url if "://" in url else "https://" + url).netloc
    return registrable_domain(host) or None


# ------------------------------------------------------------------ searches

def _search_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "query": row["query"],
        "location": row["location"],
        "max_results": row["max_results"],
        "status": row["status"],
        "found": row["found"],
        "audited": row["audited"],
        "error": row["error"],
        "created_at": row["created_at"],
    }


_SEARCH_SELECT = """
SELECT s.*,
  (SELECT COUNT(*) FROM search_leads sl WHERE sl.search_id = s.id) AS found,
  (SELECT COUNT(*) FROM search_leads sl JOIN leads l ON l.id = sl.lead_id
     WHERE sl.search_id = s.id AND l.audit_status NOT IN ('pending', 'running')) AS audited
FROM searches s
"""


def create_search(query: str, location: str, max_results: int) -> dict[str, Any]:
    with _lock:
        db = connect()
        cur = db.execute(
            "INSERT INTO searches (query, location, max_results, status, created_at) VALUES (?, ?, ?, 'queued', ?)",
            (query, location, max_results, now_iso()),
        )
        db.commit()
        return get_search(cur.lastrowid)  # type: ignore[arg-type,return-value]


def get_search(search_id: int) -> dict[str, Any] | None:
    with _lock:
        row = connect().execute(_SEARCH_SELECT + " WHERE s.id = ?", (search_id,)).fetchone()
    return _search_row(row) if row else None


def list_searches(limit: int = 50) -> list[dict[str, Any]]:
    with _lock:
        rows = connect().execute(_SEARCH_SELECT + " ORDER BY s.id DESC LIMIT ?", (limit,)).fetchall()
    return [_search_row(r) for r in rows]


def set_search_status(search_id: int, status: str, error: str | None = None) -> None:
    with _lock:
        db = connect()
        db.execute("UPDATE searches SET status = ?, error = ? WHERE id = ?", (status, error, search_id))
        db.commit()


# --------------------------------------------------------------------- leads

def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _lead_row(row: sqlite3.Row, detail: bool = False) -> dict[str, Any]:
    lead = {
        "id": row["id"],
        "search_id": row["search_id"],
        "name": row["name"],
        "category": row["category"],
        "address": row["address"],
        "website": row["website"],
        "phone": row["phone"],
        "emails": _loads(row["emails"], []),
        "rating": row["rating"],
        "reviews": row["reviews"],
        "maps_url": row["maps_url"],
        "source": row["source"],
        "status": row["status"],
        "audit_status": row["audit_status"],
        "opportunity_score": row["opportunity_score"],
        "top_issues": _loads(row["top_issues"], []),
        "services": _loads(row["services"], []),
        "notes": row["notes"],
        "has_draft": bool(row["email_body"]),
        "has_report": bool(row["report_generated_at"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if detail:
        lead["audit"] = _loads(row["audit"], None)
        lead["email"] = None
        if row["email_body"]:
            lead["email"] = {
                "to": lead["emails"][0] if lead["emails"] else None,
                "subject": row["email_subject"] or "",
                "body": row["email_body"],
                "source": row["email_source"] or "manual",
                "note": row["email_note"],
                "edited": bool(row["email_edited"]),
                "generated_at": row["email_generated_at"],
            }
        lead["report"] = None
        if row["report_generated_at"]:
            lead["report"] = {
                "generated_at": row["report_generated_at"],
                "pdf_url": f"/api/leads/{row['id']}/report.pdf",
                "image_url": f"/api/leads/{row['id']}/concept.png",
            }
    return lead


def get_lead(lead_id: int, detail: bool = True) -> dict[str, Any] | None:
    with _lock:
        row = connect().execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return _lead_row(row, detail) if row else None


def upsert_place(search_id: int, place: dict[str, Any]) -> tuple[int, bool]:
    """Store a business found by a search. Returns (lead id, was it new)."""
    with _lock:
        db = connect()
        existing = None
        if place.get("place_id"):
            existing = db.execute("SELECT id FROM leads WHERE place_id = ?", (place["place_id"],)).fetchone()
        listed_emails = [e for e in (place.get("emails") or []) if isinstance(e, str) and "@" in e]
        if existing:
            lead_id, created = existing["id"], False
            # Refresh the listing's facts; keep the user's status, notes and audit.
            if listed_emails:
                row = db.execute("SELECT emails FROM leads WHERE id = ?", (lead_id,)).fetchone()
                merged = _loads(row["emails"], [])
                merged.extend(e for e in listed_emails if e not in merged)
                db.execute("UPDATE leads SET emails = ? WHERE id = ?", (json.dumps(merged[:8]), lead_id))
            db.execute(
                "UPDATE leads SET name=?, category=?, address=?, rating=?, reviews=?, maps_url=?, "
                "phone=COALESCE(?, phone), website=COALESCE(?, website), updated_at=? WHERE id=?",
                (
                    place["name"], place.get("category"), place.get("address"), place.get("rating"),
                    place.get("reviews"), place.get("maps_url"), place.get("phone"), place.get("website"),
                    now_iso(), lead_id,
                ),
            )
        else:
            now = now_iso()
            cur = db.execute(
                "INSERT INTO leads (search_id, place_id, domain, name, category, address, website, phone, "
                "emails, rating, reviews, maps_url, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    search_id, place.get("place_id"), website_domain(place.get("website")), place["name"],
                    place.get("category"), place.get("address"), place.get("website"), place.get("phone"),
                    json.dumps(listed_emails[:8]), place.get("rating"), place.get("reviews"),
                    place.get("maps_url"), place.get("source") or "google_places", now, now,
                ),
            )
            lead_id, created = cur.lastrowid, True
        db.execute("INSERT OR IGNORE INTO search_leads (search_id, lead_id) VALUES (?, ?)", (search_id, lead_id))
        db.commit()
        return int(lead_id), created  # type: ignore[arg-type]


def create_manual_lead(website: str, name: str | None) -> tuple[dict[str, Any], bool]:
    """Add a site typed in by hand. If that site is already a lead, reuse it."""
    domain = website_domain(website)
    with _lock:
        db = connect()
        row = db.execute("SELECT id FROM leads WHERE domain = ? ORDER BY id LIMIT 1", (domain,)).fetchone() if domain else None
        if row:
            db.execute("UPDATE leads SET audit_status='pending', updated_at=? WHERE id=?", (now_iso(), row["id"]))
            db.commit()
            return get_lead(row["id"], detail=False), False  # type: ignore[return-value]
        now = now_iso()
        cur = db.execute(
            "INSERT INTO leads (domain, name, website, source, created_at, updated_at) VALUES (?, ?, ?, 'manual', ?, ?)",
            (domain, (name or "").strip() or domain or website, website, now, now),
        )
        db.commit()
        return get_lead(cur.lastrowid, detail=False), True  # type: ignore[arg-type,return-value]


def set_audit_status(lead_id: int, status: str) -> None:
    with _lock:
        db = connect()
        db.execute("UPDATE leads SET audit_status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), lead_id))
        db.commit()


def save_audit(lead_id: int, result: dict[str, Any]) -> None:
    """Store an audit result. Emails found on the site are merged with any already known."""
    with _lock:
        db = connect()
        row = db.execute("SELECT emails, phone, name, source FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if row is None:
            return
        emails = _loads(row["emails"], [])
        for email in result.get("emails", []):
            if email not in emails:
                emails.append(email)
        audit = result.get("audit") or {}
        phone = row["phone"]
        if not phone and audit.get("phones_found"):
            phone = audit["phones_found"][0]
        db.execute(
            "UPDATE leads SET audit_status=?, opportunity_score=?, top_issues=?, services=?, emails=?, "
            "phone=?, audit=?, updated_at=? WHERE id=?",
            (
                result["audit_status"], result.get("opportunity_score"),
                json.dumps(result.get("top_issues", [])), json.dumps(result.get("services", [])),
                json.dumps(emails[:8]), phone, json.dumps(audit), now_iso(), lead_id,
            ),
        )
        db.commit()


def update_lead(lead_id: int, status: str | None = None, notes: str | None = None) -> dict[str, Any] | None:
    with _lock:
        db = connect()
        if status is not None:
            db.execute("UPDATE leads SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), lead_id))
        if notes is not None:
            db.execute("UPDATE leads SET notes = ?, updated_at = ? WHERE id = ?", (notes[:5000], now_iso(), lead_id))
        db.commit()
    return get_lead(lead_id)


def save_email(lead_id: int, subject: str, body: str, source: str, note: str | None) -> None:
    """Store a freshly written draft, replacing any earlier one."""
    with _lock:
        db = connect()
        now = now_iso()
        db.execute(
            "UPDATE leads SET email_subject=?, email_body=?, email_source=?, email_note=?, "
            "email_generated_at=?, email_edited=0, updated_at=? WHERE id=?",
            (subject[:300], body[:20000], source, note, now, now, lead_id),
        )
        db.commit()


def edit_email(lead_id: int, subject: str | None, body: str | None) -> None:
    """Save the user's own changes to the draft (creating one if there was none)."""
    with _lock:
        db = connect()
        row = db.execute("SELECT email_subject, email_body, email_source FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if row is None:
            return
        new_subject = row["email_subject"] or "" if subject is None else subject[:300]
        new_body = row["email_body"] or "" if body is None else body[:20000]
        now = now_iso()
        if not row["email_body"]:
            if not new_body.strip():
                # Nothing to keep yet: a subject alone is not a draft.
                db.execute("UPDATE leads SET email_subject=?, updated_at=? WHERE id=?", (new_subject, now, lead_id))
            else:
                db.execute(
                    "UPDATE leads SET email_subject=?, email_body=?, email_source='manual', email_note=NULL, "
                    "email_generated_at=?, email_edited=0, updated_at=? WHERE id=?",
                    (new_subject, new_body, now, now, lead_id),
                )
        elif new_subject != (row["email_subject"] or "") or new_body != row["email_body"]:
            edited = 0 if row["email_source"] == "manual" else 1
            db.execute(
                "UPDATE leads SET email_subject=?, email_body=?, email_edited=?, updated_at=? WHERE id=?",
                (new_subject, new_body, edited, now, lead_id),
            )
        db.commit()


def set_report(lead_id: int, generated: bool) -> None:
    with _lock:
        db = connect()
        now = now_iso()
        db.execute("UPDATE leads SET report_generated_at=?, updated_at=? WHERE id=?", (now if generated else None, now, lead_id))
        db.commit()


def delete_email(lead_id: int) -> None:
    with _lock:
        db = connect()
        db.execute(
            "UPDATE leads SET email_subject=NULL, email_body=NULL, email_source=NULL, email_note=NULL, "
            "email_generated_at=NULL, email_edited=0, updated_at=? WHERE id=?",
            (now_iso(), lead_id),
        )
        db.commit()


def delete_lead(lead_id: int) -> bool:
    with _lock:
        db = connect()
        cur = db.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
        db.execute("DELETE FROM search_leads WHERE lead_id = ?", (lead_id,))
        db.commit()
        return cur.rowcount > 0


def pending_lead_ids(search_id: int | None = None) -> list[int]:
    with _lock:
        db = connect()
        if search_id is None:
            rows = db.execute("SELECT id FROM leads WHERE audit_status = 'pending' ORDER BY id").fetchall()
        else:
            rows = db.execute(
                "SELECT l.id FROM leads l JOIN search_leads sl ON sl.lead_id = l.id "
                "WHERE sl.search_id = ? AND l.audit_status = 'pending' ORDER BY l.id",
                (search_id,),
            ).fetchall()
    return [r["id"] for r in rows]


SORTS = {
    "score": "l.opportunity_score IS NULL, l.opportunity_score DESC, l.reviews DESC, l.id DESC",
    "newest": "l.id DESC",
    "name": "l.name COLLATE NOCASE ASC",
    "rating": "l.rating IS NULL, l.rating DESC, l.reviews DESC",
}


def list_leads(
    search_id: int | None = None,
    status: str | None = None,
    min_score: int | None = None,
    has_email: bool | None = None,
    has_website: bool | None = None,
    q: str | None = None,
    sort: str = "score",
    limit: int = 2000,
    has_draft: bool | None = None,
    detail: bool = False,
) -> list[dict[str, Any]]:
    where: list[str] = []
    args: list[Any] = []
    join = ""
    if search_id is not None:
        join = "JOIN search_leads sl ON sl.lead_id = l.id"
        where.append("sl.search_id = ?")
        args.append(search_id)
    if status:
        where.append("l.status = ?")
        args.append(status)
    if min_score is not None:
        where.append("l.opportunity_score >= ?")
        args.append(min_score)
    if has_email:
        where.append("l.emails != '[]'")
    if has_draft:
        where.append("l.email_body IS NOT NULL AND l.email_body != ''")
    if has_website is True:
        where.append("l.website IS NOT NULL AND l.website != '' AND l.audit_status != 'no_website'")
    elif has_website is False:
        where.append("(l.website IS NULL OR l.website = '' OR l.audit_status = 'no_website')")
    if q:
        like = f"%{q.strip()}%"
        where.append("(l.name LIKE ? OR l.address LIKE ? OR l.website LIKE ? OR l.category LIKE ?)")
        args.extend([like, like, like, like])
    sql = f"SELECT l.* FROM leads l {join}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {SORTS.get(sort, SORTS['score'])} LIMIT ?"
    args.append(limit)
    with _lock:
        rows = connect().execute(sql, args).fetchall()
    return [_lead_row(r, detail) for r in rows]


def stats() -> dict[str, Any]:
    with _lock:
        db = connect()
        row = db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(emails != '[]') AS with_email, "
            "SUM(phone IS NOT NULL AND phone != '') AS with_phone, "
            "SUM(audit_status = 'no_website') AS no_website, "
            "SUM(opportunity_score >= ?) AS high_opportunity FROM leads",
            (HIGH_OPPORTUNITY,),
        ).fetchone()
        by_status = {s: 0 for s in LEAD_STATUSES}
        for r in db.execute("SELECT status, COUNT(*) AS n FROM leads GROUP BY status"):
            by_status[r["status"]] = r["n"]
    return {
        "total": row["total"] or 0,
        "with_email": row["with_email"] or 0,
        "with_phone": row["with_phone"] or 0,
        "no_website": row["no_website"] or 0,
        "high_opportunity": row["high_opportunity"] or 0,
        "by_status": by_status,
    }
