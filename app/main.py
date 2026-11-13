"""The local web server behind the app window."""

from __future__ import annotations

import asyncio
import csv
import io
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, config, db
from .audit import normalize_url
from .emailer import EmailError, write_email
from .render import RenderError, make_report_files
from .report import build_context
from .jobs import Jobs
from .places import test_key

STATIC_DIR = Path(__file__).resolve().parent / "static"

LeadStatus = Literal["new", "shortlisted", "contacted", "replied", "won", "rejected"]
SortKey = Literal["score", "newest", "name", "rating"]


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    location: str = Field(default="", max_length=200)
    max_results: int = Field(default=20, ge=1, le=60)


class ManualLeadIn(BaseModel):
    website: str = Field(min_length=1, max_length=500)
    name: Optional[str] = Field(default=None, max_length=200)


class LeadPatch(BaseModel):
    status: Optional[LeadStatus] = None
    notes: Optional[str] = Field(default=None, max_length=5000)
    email_subject: Optional[str] = Field(default=None, max_length=300)
    email_body: Optional[str] = Field(default=None, max_length=20000)


class NoCacheStatic(StaticFiles):
    """Serve UI files so an updated app is picked up without clearing the browser cache."""

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    jobs = Jobs(transport=transport)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        config.ensure_data_dir()
        db.connect()
        db.recover_interrupted()
        await jobs.start()
        jobs.resume_pending()
        yield
        await jobs.stop()
        db.close()

    report_lock = asyncio.Lock()  # one browser render at a time

    app = FastAPI(title="Client Finder Assistant", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.jobs = jobs

    allowed_hosts = {f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}", "127.0.0.1", "localhost", "testserver"}

    @app.middleware("http")
    async def local_only(request: Request, call_next: Any) -> Response:
        """Only this computer's own app window may talk to the server.

        Stops another website open in the browser from reading leads or
        changing settings through the local address.
        """
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            return JSONResponse({"detail": "This app only answers on this computer."}, status_code=403)
        origin = request.headers.get("origin")
        if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
            origin_host = origin.split("://", 1)[-1]
            if origin_host not in allowed_hosts:
                return JSONResponse({"detail": "Request blocked: it did not come from the app window."}, status_code=403)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = []
        for err in exc.errors():
            field = ".".join(str(p) for p in err.get("loc", []) if p not in ("body", "query", "path"))
            problems.append(f"{field}: {err.get('msg', 'invalid')}" if field else str(err.get("msg", "invalid")))
        detail = "Please check what was entered. " + "; ".join(problems[:3])
        return JSONResponse({"detail": detail}, status_code=422)

    @app.get("/api/version")
    async def version() -> dict[str, str]:
        return {"version": __version__}

    # ---------------------------------------------------------- settings

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return config.public_settings()

    @app.put("/api/settings")
    async def put_settings(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Settings could not be read.")
        if not isinstance(body, dict):
            raise HTTPException(400, "Settings could not be read.")
        return config.update_settings(body)

    @app.post("/api/settings/test-google")
    async def test_google() -> dict[str, Any]:
        ok, message = await test_key(jobs.client, config.get_secret("google_api_key"))  # type: ignore[arg-type]
        return {"ok": ok, "message": message}

    # ------------------------------------------------------------- stats

    @app.get("/api/stats")
    async def get_stats() -> dict[str, Any]:
        return db.stats()

    # ---------------------------------------------------------- searches

    @app.post("/api/searches", status_code=201)
    async def create_search(body: SearchIn) -> dict[str, Any]:
        query = body.query.strip()
        if not query:
            raise HTTPException(400, "Type what kind of business to look for.")
        if not config.get_secret("google_api_key") and not body.location.strip():
            raise HTTPException(400, "The free search needs a place. Type a city or district in the \"Where?\" box.")
        search = db.create_search(query, body.location.strip(), body.max_results)
        jobs.queue_search(search["id"])
        return search

    @app.get("/api/searches")
    async def list_searches() -> list[dict[str, Any]]:
        return db.list_searches()

    @app.get("/api/searches/{search_id}")
    async def get_search(search_id: int) -> dict[str, Any]:
        search = db.get_search(search_id)
        if search is None:
            raise HTTPException(404, "That search no longer exists.")
        return search

    # ------------------------------------------------------------- leads

    def lead_filters(
        search_id: Optional[int],
        status: Optional[str],
        min_score: Optional[int],
        has_email: Optional[bool],
        has_website: Optional[bool],
        q: Optional[str],
        sort: str,
        has_draft: Optional[bool] = None,
    ) -> dict[str, Any]:
        if status and status not in db.LEAD_STATUSES:
            raise HTTPException(400, "Unknown status filter.")
        return {
            "search_id": search_id, "status": status or None, "min_score": min_score,
            "has_email": has_email, "has_website": has_website, "q": (q or "").strip() or None, "sort": sort,
            "has_draft": has_draft,
        }

    @app.get("/api/leads/export.csv")
    async def export_csv(
        search_id: Optional[int] = None,
        status: Optional[str] = None,
        min_score: Optional[int] = Query(default=None, ge=0, le=100),
        has_email: Optional[bool] = None,
        has_website: Optional[bool] = None,
        q: Optional[str] = None,
        sort: SortKey = "score",
        has_draft: Optional[bool] = None,
    ) -> Response:
        leads = db.list_leads(**lead_filters(search_id, status, min_score, has_email, has_website, q, sort, has_draft), detail=True)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "Business", "Category", "Opportunity score", "Top issues", "Services that fit", "Phone", "Emails",
            "Website", "Address", "Rating", "Reviews", "Status", "Notes", "Map link", "Added",
            "Email subject", "Email body",
        ])
        for lead in leads:
            writer.writerow([_csv_safe(v) for v in [
                lead["name"], lead["category"] or "", lead["opportunity_score"] if lead["opportunity_score"] is not None else "",
                "; ".join(lead["top_issues"]), ", ".join(lead["services"]), lead["phone"] or "",
                "; ".join(lead["emails"]), lead["website"] or "", lead["address"] or "",
                lead["rating"] if lead["rating"] is not None else "", lead["reviews"] if lead["reviews"] is not None else "",
                lead["status"], lead["notes"], lead["maps_url"] or "", lead["created_at"],
                (lead.get("email") or {}).get("subject", ""), (lead.get("email") or {}).get("body", ""),
            ]])
        # BOM so Excel opens accented names correctly
        content = "﻿" + buffer.getvalue()
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="leads.csv"'},
        )

    @app.get("/api/leads")
    async def list_leads(
        search_id: Optional[int] = None,
        status: Optional[str] = None,
        min_score: Optional[int] = Query(default=None, ge=0, le=100),
        has_email: Optional[bool] = None,
        has_website: Optional[bool] = None,
        q: Optional[str] = None,
        sort: SortKey = "score",
        has_draft: Optional[bool] = None,
    ) -> list[dict[str, Any]]:
        return db.list_leads(**lead_filters(search_id, status, min_score, has_email, has_website, q, sort, has_draft))

    @app.post("/api/leads", status_code=201)
    async def add_lead(body: ManualLeadIn) -> dict[str, Any]:
        url = normalize_url(body.website)
        if not url:
            raise HTTPException(400, "That does not look like a website address. Try something like example.com.")
        lead, _created = db.create_manual_lead(url, body.name)
        jobs.queue_audit(lead["id"])
        return lead

    @app.get("/api/leads/{lead_id}")
    async def get_lead(lead_id: int) -> dict[str, Any]:
        lead = db.get_lead(lead_id)
        if lead is None:
            raise HTTPException(404, "That lead no longer exists.")
        return lead

    @app.patch("/api/leads/{lead_id}")
    async def patch_lead(lead_id: int, body: LeadPatch) -> dict[str, Any]:
        if db.get_lead(lead_id, detail=False) is None:
            raise HTTPException(404, "That lead no longer exists.")
        if body.email_subject is not None or body.email_body is not None:
            db.edit_email(lead_id, body.email_subject, body.email_body)
        lead = db.update_lead(lead_id, status=body.status, notes=body.notes)
        return lead  # type: ignore[return-value]

    @app.post("/api/leads/{lead_id}/email")
    async def write_lead_email(lead_id: int) -> dict[str, Any]:
        """Write (or rewrite) the outreach email for one lead and save it with the lead."""
        lead = db.get_lead(lead_id)
        if lead is None:
            raise HTTPException(404, "That lead no longer exists.")
        settings = config.public_settings()
        try:
            draft = await write_email(
                jobs.client,  # type: ignore[arg-type]
                lead,
                settings,
                api_key=config.get_secret("claude_api_key"),
                model=settings["claude_model"],
            )
        except EmailError as exc:
            raise HTTPException(getattr(exc, "status", 502), str(exc))
        db.save_email(lead_id, draft["subject"], draft["body"], draft["source"], draft["note"])
        return db.get_lead(lead_id)  # type: ignore[return-value]

    @app.delete("/api/leads/{lead_id}/email", status_code=204)
    async def discard_lead_email(lead_id: int) -> Response:
        if db.get_lead(lead_id, detail=False) is None:
            raise HTTPException(404, "That lead no longer exists.")
        db.delete_email(lead_id)
        return Response(status_code=204)

    @app.post("/api/leads/{lead_id}/reaudit")
    async def reaudit(lead_id: int) -> dict[str, Any]:
        if db.get_lead(lead_id, detail=False) is None:
            raise HTTPException(404, "That lead no longer exists.")
        db.set_audit_status(lead_id, "pending")
        jobs.queue_audit(lead_id)
        return db.get_lead(lead_id, detail=False)  # type: ignore[return-value]

    # ------------------------------------------------- report and concept

    def report_file(lead_id: int, name: str) -> Any:
        lead = db.get_lead(lead_id, detail=False)
        if lead is None:
            raise HTTPException(404, "That lead no longer exists.")
        path = config.report_dir(lead_id) / name
        if not lead["has_report"] or not path.is_file():
            raise HTTPException(404, "No report has been created for this lead yet.")
        return lead, path

    @app.post("/api/leads/{lead_id}/report")
    async def create_report(lead_id: int) -> dict[str, Any]:
        """Create (or recreate) the PDF report and homepage concept picture for one lead."""
        lead = db.get_lead(lead_id)
        if lead is None:
            raise HTTPException(404, "That lead no longer exists.")
        if lead["audit_status"] in ("pending", "running"):
            raise HTTPException(409, "The website check for this business is still running. Wait for it to finish, then create the report.")
        audit = lead.get("audit") or {}
        if lead["audit_status"] in ("failed", "blocked") and not audit.get("issues"):
            raise HTTPException(409, "This business's website could not be read, so there is nothing to report. Open the site yourself, or press Re-audit.")
        if lead["audit_status"] != "done" and not audit.get("issues"):
            raise HTTPException(409, "There are no findings for this business yet. Press Re-audit first.")
        settings = config.public_settings()
        if not (settings.get("your_name") or "").strip():
            raise HTTPException(400, "Add your name in Settings first, so the report can say who prepared it.")
        context = build_context(lead, settings)
        async with report_lock:
            try:
                await asyncio.to_thread(make_report_files, context, config.report_dir(lead_id))
            except RenderError as exc:
                raise HTTPException(500, str(exc))
        db.set_report(lead_id, True)
        return db.get_lead(lead_id)  # type: ignore[return-value]

    @app.get("/api/leads/{lead_id}/report.pdf")
    async def get_report_pdf(lead_id: int, inline: bool = False) -> Response:
        lead, path = report_file(lead_id, "report.pdf")
        filename = f"{_file_name(lead['name'])} - website review.pdf"
        return FileResponse(
            path, media_type="application/pdf", filename=filename,
            content_disposition_type="inline" if inline else "attachment",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/leads/{lead_id}/concept.png")
    async def get_concept_png(lead_id: int, download: bool = False) -> Response:
        lead, path = report_file(lead_id, "concept.png")
        filename = f"{_file_name(lead['name'])} - homepage concept.png"
        return FileResponse(
            path, media_type="image/png", filename=filename,
            content_disposition_type="attachment" if download else "inline",
            headers={"Cache-Control": "no-store"},
        )

    @app.delete("/api/leads/{lead_id}/report", status_code=204)
    async def delete_report(lead_id: int) -> Response:
        if db.get_lead(lead_id, detail=False) is None:
            raise HTTPException(404, "That lead no longer exists.")
        shutil.rmtree(config.report_dir(lead_id), ignore_errors=True)
        db.set_report(lead_id, False)
        return Response(status_code=204)

    @app.delete("/api/leads/{lead_id}", status_code=204)
    async def delete_lead(lead_id: int) -> Response:
        if not db.delete_lead(lead_id):
            raise HTTPException(404, "That lead no longer exists.")
        shutil.rmtree(config.report_dir(lead_id), ignore_errors=True)
        return Response(status_code=204)

    @app.get("/api/{rest:path}", include_in_schema=False)
    async def api_not_found(rest: str) -> JSONResponse:
        return JSONResponse({"detail": "The app asked for something that does not exist."}, status_code=404)

    # ---------------------------------------------------------------- UI

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        index_file = STATIC_DIR / "index.html"
        if not index_file.exists():
            return PlainTextResponse("The app's screen files are missing. Download the app again.", status_code=500)
        return FileResponse(index_file, headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        icon = STATIC_DIR / "favicon.svg"
        if icon.exists():
            return FileResponse(icon, media_type="image/svg+xml")
        return Response(status_code=204)

    if STATIC_DIR.exists():
        app.mount("/static", NoCacheStatic(directory=STATIC_DIR), name="static")

    return app


def _file_name(name: str) -> str:
    """A business name made safe to use as a Windows file name."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:80] or "Business"


def _csv_safe(value: Any) -> Any:
    """Stop spreadsheet apps treating scraped text as a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        # Phone numbers legitimately start with "+": keep those readable.
        if value[:1] == "+" and value[1:].replace(" ", "").replace("-", "").replace("(", "").replace(")", "").isdigit():
            return value
        return "'" + value
    return value


app = create_app()
