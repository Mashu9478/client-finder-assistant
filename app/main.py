"""The local web server behind the app window."""

from __future__ import annotations

import csv
import io
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
        if not config.get_secret("google_api_key"):
            raise HTTPException(400, "Add your Google Places key in Settings before searching.")
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
    ) -> dict[str, Any]:
        if status and status not in db.LEAD_STATUSES:
            raise HTTPException(400, "Unknown status filter.")
        return {
            "search_id": search_id, "status": status or None, "min_score": min_score,
            "has_email": has_email, "has_website": has_website, "q": (q or "").strip() or None, "sort": sort,
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
    ) -> Response:
        leads = db.list_leads(**lead_filters(search_id, status, min_score, has_email, has_website, q, sort))
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "Business", "Category", "Opportunity score", "Top issues", "Services that fit", "Phone", "Emails",
            "Website", "Address", "Rating", "Reviews", "Status", "Notes", "Google Maps", "Added",
        ])
        for lead in leads:
            writer.writerow([_csv_safe(v) for v in [
                lead["name"], lead["category"] or "", lead["opportunity_score"] if lead["opportunity_score"] is not None else "",
                "; ".join(lead["top_issues"]), ", ".join(lead["services"]), lead["phone"] or "",
                "; ".join(lead["emails"]), lead["website"] or "", lead["address"] or "",
                lead["rating"] if lead["rating"] is not None else "", lead["reviews"] if lead["reviews"] is not None else "",
                lead["status"], lead["notes"], lead["maps_url"] or "", lead["created_at"],
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
    ) -> list[dict[str, Any]]:
        return db.list_leads(**lead_filters(search_id, status, min_score, has_email, has_website, q, sort))

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
        lead = db.update_lead(lead_id, status=body.status, notes=body.notes)
        return lead  # type: ignore[return-value]

    @app.post("/api/leads/{lead_id}/reaudit")
    async def reaudit(lead_id: int) -> dict[str, Any]:
        if db.get_lead(lead_id, detail=False) is None:
            raise HTTPException(404, "That lead no longer exists.")
        db.set_audit_status(lead_id, "pending")
        jobs.queue_audit(lead_id)
        return db.get_lead(lead_id, detail=False)  # type: ignore[return-value]

    @app.delete("/api/leads/{lead_id}", status_code=204)
    async def delete_lead(lead_id: int) -> Response:
        if not db.delete_lead(lead_id):
            raise HTTPException(404, "That lead no longer exists.")
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


def _csv_safe(value: Any) -> Any:
    """Stop spreadsheet apps treating scraped text as a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        # Phone numbers legitimately start with "+": keep those readable.
        if value[:1] == "+" and value[1:].replace(" ", "").replace("-", "").replace("(", "").replace(")", "").isdigit():
            return value
        return "'" + value
    return value


app = create_app()
