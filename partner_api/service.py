"""Business logic for the partner API, independent of the web framework.

The FastAPI routes are thin wrappers around PartnerService, so everything here is testable
without HTTP.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from . import errors
from .config import Settings
from .engine import Engine
from .keys import hash_key, key_mode, parse_bearer
from .models import MenuItemIn
from .nutrition import (ALLOWED_IMAGE_TYPES, build_result, input_hash, normalise_declared,
                        sniff_image_type)
from .store import DuplicateError, Store

log = logging.getLogger("partner_api")

# Requests per minute, per partner, per mode.
SANDBOX_RPM = 60
PLAN_RPM = {"pilot": 300, "production": 1200}
TOUCH_INTERVAL = timedelta(minutes=5)
USAGE_KINDS = ("analysis", "analysis_cached", "photo_analysis")

ALLERGEN_NOTICE = ("Allergen information is a guide only, not a guarantee. "
                   "Always show it alongside the business's legal allergen information.")


@dataclass
class Ctx:
    partner: dict
    key: dict
    mode: str  # "live" | "test"

    @property
    def partner_id(self) -> str:
        return self.partner["partner_id"]

    @property
    def partner_ns(self) -> str:
        # Test keys get their own cache namespace so sandbox data never mixes with live data.
        return self.partner_id if self.mode == "live" else f"{self.partner_id}:test"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PartnerService:
    def __init__(self, store: Store, settings: Settings, live_engine: Engine | None, sandbox_engine: Engine,
                 now: Callable[[], datetime] = _utcnow):
        self.store = store
        self.settings = settings
        self.live_engine = live_engine
        self.sandbox_engine = sandbox_engine
        self.now = now

    # --- auth & limits ------------------------------------------------------

    async def authenticate(self, authorization: str | None) -> Ctx:
        token = parse_bearer(authorization)
        if not token:
            raise errors.invalid_key("Send 'Authorization: Bearer dik_live_…' or 'dik_test_…'.")
        mode = key_mode(token)
        if not mode:
            raise errors.invalid_key()
        key = await self.store.get_key(hash_key(token))
        if not key or key.get("revoked_at") or key.get("mode") != mode:
            raise errors.invalid_key()
        partner = await self.store.get_partner(key["partner_id"])
        if not partner or partner.get("status") != "active":
            raise errors.invalid_key("This partner account is not active.")
        # Record last use at most every few minutes, not on every request (saves a DB write per call).
        now = self.now()
        last = key.get("last_used_at")
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last is None or now - last >= TOUCH_INTERVAL:
            try:
                await self.store.touch_key(key["key_id"], now)
            except Exception:  # best effort, never fail a request over it
                log.warning("touch_key failed", exc_info=True)
        return Ctx(partner=partner, key=key, mode=mode)

    @staticmethod
    def require_scope(ctx: Ctx, scope: str) -> None:
        if scope not in (ctx.key.get("scopes") or []):
            raise errors.insufficient_scope(scope)

    async def rate_limit(self, ctx: Ctx) -> None:
        if ctx.mode == "test":
            limit = SANDBOX_RPM
        else:
            limit = int(ctx.partner.get("rate_limit_per_min") or PLAN_RPM.get(ctx.partner.get("plan"), 300))
        now = self.now()
        window = int(now.timestamp() // 60)
        n = await self.store.incr_counter(f"rl:{ctx.partner_id}:{ctx.mode}:{window}", now + timedelta(minutes=2))
        if n > limit:
            raise errors.rate_limited(60 - int(now.timestamp() % 60))

    async def _engine_for(self, ctx: Ctx) -> Engine:
        if ctx.mode == "test":
            return self.sandbox_engine
        if self.live_engine is None:
            raise errors.ai_unavailable("Live analysis is not configured on this server.")
        # Global daily cap on live AI calls: a cost guard while running on free credits.
        today = self.now().date()
        tomorrow = datetime(today.year, today.month, today.day, tzinfo=timezone.utc) + timedelta(days=1)
        n = await self.store.incr_counter(f"ai:{today.isoformat()}", tomorrow + timedelta(days=1))
        if n > self.settings.daily_ai_call_cap:
            raise errors.capacity_reached()
        return self.live_engine

    async def _meter(self, ctx: Ctx, endpoint: str, kind: str) -> None:
        # One counter per partner/mode/day/kind, incremented in place: a single small upsert per
        # request, and usage reports read a few rows instead of aggregating every event.
        await self.store.incr_usage(ctx.partner_id, ctx.mode, self.now().date(), kind)

    # --- endpoints ------------------------------------------------------------

    async def analyze_menu_item(self, ctx: Ctx, item: MenuItemIn) -> dict:
        payload = item.model_dump(exclude_none=True)
        declared = None
        if item.declared_allergens is not None:
            try:
                declared = normalise_declared(item.declared_allergens)
            except ValueError as e:
                raise errors.invalid_item(str(e)) from e
            payload["declared_allergens"] = declared

        h = input_hash(payload, self.settings.analysis_version)
        cached = await self.store.get_menu_item(ctx.partner_ns, h)
        if cached:
            await self._meter(ctx, "menu-items/analyze", "analysis_cached")
            return self._public_item(cached, item.partner_item_id, cached=True)

        engine = await self._engine_for(ctx)
        ai = await engine.analyze_text({k: v for k, v in payload.items() if k != "partner_item_id"})
        await self._meter(ctx, "menu-items/analyze", "analysis")  # a miss costs an AI call, food or not
        if not ai.is_food:
            raise errors.not_food()

        official = payload.get("official_nutrition")
        doc = {
            "menu_item_id": "mi_" + h[:20],
            "partner_ns": ctx.partner_ns,
            "partner_item_id": item.partner_item_id,
            "input_hash": h,
            "name": item.name,
            **build_result(ai, official, declared),
            "review_status": "pending",
            "analysis_version": self.settings.analysis_version,
            "created_at": self.now(),
        }
        try:
            await self.store.insert_menu_item(doc)
        except DuplicateError:  # a concurrent request analysed the same item first; return theirs
            existing = await self.store.get_menu_item(ctx.partner_ns, h)
            if existing:
                return self._public_item(existing, item.partner_item_id, cached=True)
        return self._public_item(doc, item.partner_item_id, cached=False)

    async def analyze_photo(self, ctx: Ctx, data: bytes, declared_type: str | None, hint: str | None) -> dict:
        if not data:
            raise errors.invalid_item("The 'image' file is empty.")
        if len(data) > self.settings.max_photo_bytes:
            raise errors.ApiError(413, "image_too_large",
                                  f"Images must be {self.settings.max_photo_bytes // (1024 * 1024)} MB or smaller.")
        sniffed = sniff_image_type(data)
        if sniffed is None or (declared_type or "").lower() not in ALLOWED_IMAGE_TYPES:
            raise errors.ApiError(415, "unsupported_image", "Send a JPEG, PNG, WebP or HEIC image.")
        if hint is not None:
            hint = hint.strip()[:200] or None

        engine = await self._engine_for(ctx)
        ai = await engine.analyze_photo(data, sniffed, hint)
        await self._meter(ctx, "photos/analyze", "photo_analysis")
        if not ai.is_food:
            raise errors.not_food()

        # The photo is not stored: it is analysed in memory and discarded (data minimisation).
        r = build_result(ai, None, None)
        return {
            "analysis_id": "pa_" + secrets.token_hex(10),
            "dish_name": ai.dish_name,
            "source": r["source"],
            "is_estimate": r["is_estimate"],
            "confidence": r["confidence"],
            "nutrition": r["nutrition"],
            "items": r["items"],
            "allergens": r["allergens"],
            "allergen_notice": ALLERGEN_NOTICE,
            "processing_level": r["processing_level"],
            "analysis_version": self.settings.analysis_version,
        }

    async def usage(self, ctx: Ctx, start: date | None, end: date | None) -> dict:
        """Usage between two UTC dates, inclusive. Defaults to the current month to date."""
        today = self.now().date()
        start = start or today.replace(day=1)
        end = end or today
        if end < start or (end - start).days > 400:
            raise errors.invalid_item("'to' must be on or after 'from', and the range at most 400 days.")
        counts = await self.store.usage_summary(ctx.partner_id, ctx.mode, start, end)
        return {
            "partner_id": ctx.partner_id,
            "mode": ctx.mode,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "counts": {k: counts.get(k, 0) for k in USAGE_KINDS},
        }

    # --- helpers --------------------------------------------------------------

    @staticmethod
    def _public_item(doc: dict, partner_item_id: str, cached: bool) -> dict:
        # The cache is keyed on the item's content, so two partner IDs with identical content share
        # one analysis. Always echo the ID from *this* request, never the one first stored.
        return {
            "menu_item_id": doc["menu_item_id"],
            "partner_item_id": partner_item_id,
            "source": doc["source"],
            "is_estimate": doc["is_estimate"],
            "confidence": doc["confidence"],
            "nutrition": doc["nutrition"],
            "items": doc["items"],
            "allergens": doc["allergens"],
            "possible_allergens": doc.get("possible_allergens", []),
            "allergen_notice": ALLERGEN_NOTICE,
            "processing_level": doc["processing_level"],
            "review_status": doc["review_status"],
            "needs_review": doc.get("needs_review", False),
            "cached": cached,
            "analysis_version": doc["analysis_version"],
        }
