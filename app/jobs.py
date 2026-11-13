"""Background work: running searches and auditing websites."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from . import config, db
from .audit import audit_site
from .places import PlacesError, search_places

log = logging.getLogger("cfa.jobs")

SITES_AT_ONCE = 4


class Jobs:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self.client: httpx.AsyncClient | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._auditing: set[int] = set()
        self._sem: asyncio.Semaphore | None = None

    async def start(self) -> None:
        limits = httpx.Limits(max_connections=40, max_keepalive_connections=10)
        self.client = httpx.AsyncClient(transport=self._transport, limits=limits, max_redirects=8)
        self._sem = asyncio.Semaphore(SITES_AT_ONCE)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    def spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait_idle(self) -> None:
        """Wait for everything queued so far to finish (used by tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ------------------------------------------------------------- audits

    async def audit_lead(self, lead_id: int) -> None:
        if lead_id in self._auditing or self.client is None or self._sem is None:
            return
        self._auditing.add(lead_id)
        try:
            async with self._sem:
                lead = db.get_lead(lead_id, detail=False)
                if lead is None:
                    return
                db.set_audit_status(lead_id, "running")
                pagespeed_key = config.get_secret("google_api_key") if config.get_setting("use_pagespeed") else ""
                result = await audit_site(self.client, lead.get("website"), lead, pagespeed_key)
                db.save_audit(lead_id, result)
        except asyncio.CancelledError:
            # App is closing: leave the lead ready to be picked up next time.
            db.set_audit_status(lead_id, "pending")
            raise
        finally:
            self._auditing.discard(lead_id)

    def queue_audit(self, lead_id: int) -> None:
        self.spawn(self.audit_lead(lead_id))

    def resume_pending(self) -> None:
        for lead_id in db.pending_lead_ids():
            self.queue_audit(lead_id)

    # ----------------------------------------------------------- searches

    async def run_search(self, search_id: int) -> None:
        search = db.get_search(search_id)
        if search is None or self.client is None:
            return
        try:
            db.set_search_status(search_id, "finding")
            places = await search_places(
                self.client,
                config.get_secret("google_api_key"),
                search["query"],
                search["location"],
                search["max_results"],
            )
            for place in places:
                db.upsert_place(search_id, place)
            db.set_search_status(search_id, "auditing")
            pending = db.pending_lead_ids(search_id)
            await asyncio.gather(*(self.audit_lead(lead_id) for lead_id in pending))
            db.set_search_status(search_id, "done")
        except PlacesError as exc:
            db.set_search_status(search_id, "error", str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep the app alive whatever one search does
            log.exception("Search %s failed", search_id)
            db.set_search_status(search_id, "error", f"Something unexpected went wrong ({type(exc).__name__}). Try the search again.")

    def queue_search(self, search_id: int) -> None:
        self.spawn(self.run_search(search_id))
