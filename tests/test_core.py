"""Core tests: run with `python -m unittest discover -s tests` (no web framework or database needed)."""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from partner_api import errors
from partner_api.config import Settings
from partner_api.engine import SandboxEngine
from partner_api.keys import generate_key, hash_key, key_mode, parse_bearer
from partner_api.models import MenuItemIn
from partner_api.nutrition import (EngineResult, build_result, input_hash, normalise_declared,
                                   parse_engine_json, sniff_image_type)
from partner_api.service import PartnerService
from partner_api.store import MemoryStore

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

BURGER = {
    "partner_item_id": "burger-001",
    "name": "Chicken burger",
    "description": "Grilled chicken breast, brioche bun, lettuce, mayo",
    "ingredients": ["chicken breast", "brioche bun", "lettuce", "mayonnaise"],
    "portion": {"amount": 1, "unit": "serving", "grams": 280},
    "official_nutrition": {"kcal": 640, "protein_g": 38, "carbs_g": 52, "fat_g": 29},
    "declared_allergens": ["gluten", "eggs", "milk"],
}


class FakeLiveEngine:
    """Stands in for Gemini; counts calls."""

    def __init__(self, result: EngineResult | None = None, raise_exc: Exception | None = None):
        self.calls = 0
        self.result = result or EngineResult(True, "Burger", [
            {"name": "Chicken", "grams": 150, "kcal": 250, "protein_g": 34, "carbs_g": 0, "fat_g": 6},
            {"name": "Bun", "grams": 80, "kcal": 220, "protein_g": 7, "carbs_g": 40, "fat_g": 4},
        ], ["gluten", "sesame"], "processed", 0.82)
        self.raise_exc = raise_exc

    async def analyze_text(self, item):
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        return self.result

    async def analyze_photo(self, image, mime, hint):
        return await self.analyze_text({})


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 12, 12, 0, 5, tzinfo=timezone.utc)

    def __call__(self):
        return self.t


def run(coro):
    return asyncio.run(coro)


class Fixture:
    def __init__(self, cap=500, engine=None, plan="pilot"):
        self.store = MemoryStore()
        self.clock = Clock()
        self.engine = engine or FakeLiveEngine()
        self.settings = Settings(daily_ai_call_cap=cap)
        self.svc = PartnerService(self.store, self.settings, self.engine, SandboxEngine(), now=self.clock)
        run(self.store.insert_partner({"partner_id": "ptn_a", "name": "A", "plan": plan, "status": "active"}))
        self.live = self.add_key("ptn_a", "live", ["nutrition:read", "photos:analyze", "usage:read"])
        self.test = self.add_key("ptn_a", "test", ["nutrition:read", "photos:analyze", "usage:read"])

    def add_key(self, partner_id, mode, scopes, revoked=False):
        key = generate_key(mode)
        run(self.store.insert_key({"key_id": f"key_{len(self.store.keys)}", "key_hash": hash_key(key),
                                   "partner_id": partner_id, "mode": mode, "scopes": scopes,
                                   "revoked_at": self.clock() if revoked else None}))
        return key

    def ctx(self, key):
        return run(self.svc.authenticate(f"Bearer {key}"))


class KeyTests(unittest.TestCase):
    def test_generate_and_parse(self):
        k = generate_key("live")
        self.assertTrue(k.startswith("dik_live_"))
        self.assertEqual(key_mode(k), "live")
        self.assertEqual(key_mode(generate_key("test")), "test")
        self.assertIsNone(key_mode("dik_live_short"))
        self.assertIsNone(key_mode("sk_live_" + "a" * 32))
        self.assertEqual(len(hash_key(k)), 64)
        self.assertNotEqual(generate_key("live"), generate_key("live"))

    def test_parse_bearer(self):
        self.assertEqual(parse_bearer("Bearer abc"), "abc")
        self.assertEqual(parse_bearer("bearer  abc "), "abc")
        self.assertIsNone(parse_bearer("Basic abc"))
        self.assertIsNone(parse_bearer(None))
        self.assertIsNone(parse_bearer("Bearer "))


class NutritionTests(unittest.TestCase):
    def test_allergen_normalisation(self):
        self.assertEqual(normalise_declared(["Eggs", "wheat", "Tree Nuts", "soy"]), ["egg", "gluten", "nuts", "soya"])
        with self.assertRaises(ValueError):
            normalise_declared(["gluten", "banana"])

    def test_parse_engine_json_sanitises(self):
        r = parse_engine_json('```json\n{"is_food": true, "dish_name": "Soup", "items": ['
                              '{"name": "Soup", "grams": 300, "kcal": -5, "protein_g": "4", "carbs_g": 20, "fat_g": 1e9}], '
                              '"allergens": ["Celery", "unicorn"], "processing_level": "weird", "confidence": 7}\n```')
        self.assertTrue(r.is_food)
        self.assertEqual(r.items[0]["kcal"], 0.0)
        self.assertEqual(r.items[0]["protein_g"], 4.0)
        self.assertEqual(r.items[0]["fat_g"], 1000.0)
        self.assertEqual(r.allergens, ["celery"])
        self.assertEqual(r.processing_level, "processed")
        self.assertEqual(r.confidence, 1.0)
        self.assertFalse(parse_engine_json('{"is_food": false}').is_food)
        with self.assertRaises(ValueError):
            parse_engine_json("not json")

    def test_official_figures_win(self):
        ai = FakeLiveEngine().result
        r = build_result(ai, BURGER["official_nutrition"], ["egg", "gluten", "milk"])
        self.assertEqual(r["source"], "partner_official")
        self.assertFalse(r["is_estimate"])
        self.assertEqual(r["nutrition"]["kcal"], 640)
        self.assertEqual(r["allergens"], ["egg", "gluten", "milk"])
        self.assertEqual(r["possible_allergens"], ["sesame"])  # AI saw sesame; partner didn't declare it
        self.assertTrue(r["needs_review"])

    def test_ai_estimate_sums_items(self):
        ai = FakeLiveEngine().result
        r = build_result(ai, None, None)
        self.assertEqual(r["source"], "ai_estimate")
        self.assertTrue(r["is_estimate"])
        self.assertEqual(r["nutrition"]["kcal"], 470)
        self.assertFalse(r["needs_review"])
        low = build_result(EngineResult(True, "x", [], [], "processed", 0.5), None, None)
        self.assertTrue(low["needs_review"])

    def test_input_hash_stable_and_sensitive(self):
        a = input_hash({"name": "Chicken  Burger", "ingredients": ["Bun", "chicken"]}, "v1")
        b = input_hash({"name": "chicken burger", "ingredients": ["chicken", "bun"]}, "v1")
        c = input_hash({"name": "chicken burger", "ingredients": ["chicken", "bun", "mayo"]}, "v1")
        d = input_hash({"name": "chicken burger", "ingredients": ["chicken", "bun"]}, "v2")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, d)

    def test_sniff(self):
        self.assertEqual(sniff_image_type(JPEG), "image/jpeg")
        self.assertEqual(sniff_image_type(PNG), "image/png")
        self.assertEqual(sniff_image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        self.assertIsNone(sniff_image_type(b"%PDF-1.7"))


class AuthTests(unittest.TestCase):
    def test_valid_live_and_test_keys(self):
        f = Fixture()
        self.assertEqual(f.ctx(f.live).mode, "live")
        self.assertEqual(f.ctx(f.test).mode, "test")

    def assertCode(self, code, coro):
        with self.assertRaises(errors.ApiError) as cm:
            run(coro)
        self.assertEqual(cm.exception.code, code)
        return cm.exception

    def test_rejections(self):
        f = Fixture()
        self.assertCode("invalid_key", f.svc.authenticate(None))
        self.assertCode("invalid_key", f.svc.authenticate("Bearer nonsense"))
        self.assertCode("invalid_key", f.svc.authenticate("Bearer " + generate_key("live")))  # unknown key
        revoked = f.add_key("ptn_a", "live", ["nutrition:read"], revoked=True)
        self.assertCode("invalid_key", f.svc.authenticate(f"Bearer {revoked}"))
        run(f.store.insert_partner({"partner_id": "ptn_off", "name": "Off", "plan": "pilot", "status": "suspended"}))
        off = f.add_key("ptn_off", "live", ["nutrition:read"])
        self.assertCode("invalid_key", f.svc.authenticate(f"Bearer {off}"))

    def test_scope(self):
        f = Fixture()
        narrow = f.add_key("ptn_a", "live", ["nutrition:read"])
        ctx = f.ctx(narrow)
        f.svc.require_scope(ctx, "nutrition:read")
        with self.assertRaises(errors.ApiError) as cm:
            f.svc.require_scope(ctx, "photos:analyze")
        self.assertEqual(cm.exception.status, 403)

    def test_rate_limit_sandbox_60_per_minute(self):
        f = Fixture()
        ctx = f.ctx(f.test)
        for _ in range(60):
            run(f.svc.rate_limit(ctx))
        e = self.assertCode("rate_limited", f.svc.rate_limit(ctx))
        self.assertEqual(e.status, 429)
        self.assertEqual(e.headers["Retry-After"], "55")
        f.clock.t += timedelta(minutes=1)  # new window
        run(f.svc.rate_limit(ctx))
        # live traffic has its own counter
        live = f.ctx(f.live)
        run(f.svc.rate_limit(live))


class MenuItemTests(unittest.TestCase):
    def test_cache_hit_costs_one_ai_call(self):
        f = Fixture()
        ctx = f.ctx(f.live)
        first = run(f.svc.analyze_menu_item(ctx, MenuItemIn(**BURGER)))
        second = run(f.svc.analyze_menu_item(ctx, MenuItemIn(**BURGER)))
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(first["menu_item_id"], second["menu_item_id"])
        self.assertEqual(f.engine.calls, 1)
        self.assertEqual(first["allergens"], ["egg", "gluten", "milk"])
        self.assertEqual(first["review_status"], "pending")
        u = run(f.svc.usage(ctx, None, None))
        self.assertEqual(u["counts"], {"analysis": 1, "analysis_cached": 1, "photo_analysis": 0})

    def test_changed_input_is_new_version(self):
        f = Fixture()
        ctx = f.ctx(f.live)
        a = run(f.svc.analyze_menu_item(ctx, MenuItemIn(**BURGER)))
        b = run(f.svc.analyze_menu_item(ctx, MenuItemIn(**{**BURGER, "description": "Now with bacon"})))
        self.assertNotEqual(a["menu_item_id"], b["menu_item_id"])
        self.assertEqual(f.engine.calls, 2)

    def test_sandbox_never_calls_ai_and_is_isolated(self):
        f = Fixture()
        t = run(f.svc.analyze_menu_item(f.ctx(f.test), MenuItemIn(**BURGER)))
        self.assertEqual(f.engine.calls, 0)
        self.assertFalse(t["cached"])
        live = run(f.svc.analyze_menu_item(f.ctx(f.live), MenuItemIn(**BURGER)))
        self.assertFalse(live["cached"])  # sandbox cache did not leak into live
        self.assertEqual(run(f.svc.usage(f.ctx(f.live), None, None))["counts"]["analysis"], 1)
        self.assertEqual(run(f.svc.usage(f.ctx(f.test), None, None))["counts"]["analysis"], 1)

    def test_partners_do_not_share_cache(self):
        f = Fixture()
        run(f.store.insert_partner({"partner_id": "ptn_b", "name": "B", "plan": "pilot", "status": "active"}))
        kb = f.add_key("ptn_b", "live", ["nutrition:read"])
        run(f.svc.analyze_menu_item(f.ctx(f.live), MenuItemIn(**BURGER)))
        r = run(f.svc.analyze_menu_item(f.ctx(kb), MenuItemIn(**BURGER)))
        self.assertFalse(r["cached"])

    def test_not_food_and_bad_allergen(self):
        f = Fixture()
        with self.assertRaises(errors.ApiError) as cm:
            run(f.svc.analyze_menu_item(f.ctx(f.test), MenuItemIn(partner_item_id="x", name="__not_food__")))
        self.assertEqual(cm.exception.code, "not_food")
        with self.assertRaises(errors.ApiError) as cm:
            run(f.svc.analyze_menu_item(f.ctx(f.test), MenuItemIn(**{**BURGER, "declared_allergens": ["banana"]})))
        self.assertEqual(cm.exception.code, "invalid_item")

    def test_daily_ai_cap(self):
        f = Fixture(cap=2)
        ctx = f.ctx(f.live)
        for i in range(2):
            run(f.svc.analyze_menu_item(ctx, MenuItemIn(partner_item_id=f"i{i}", name=f"Dish {i}")))
        with self.assertRaises(errors.ApiError) as cm:
            run(f.svc.analyze_menu_item(ctx, MenuItemIn(partner_item_id="i3", name="Dish 3")))
        self.assertEqual(cm.exception.code, "capacity_reached")
        # cached items still served when the cap is hit
        self.assertTrue(run(f.svc.analyze_menu_item(ctx, MenuItemIn(partner_item_id="i0", name="Dish 0")))["cached"])
        # the cap resets the next UTC day
        f.clock.t += timedelta(days=1)
        run(f.svc.analyze_menu_item(ctx, MenuItemIn(partner_item_id="i3", name="Dish 3")))

    def test_ai_failure_is_503_and_not_cached(self):
        f = Fixture(engine=FakeLiveEngine(raise_exc=errors.ai_unavailable()))
        with self.assertRaises(errors.ApiError) as cm:
            run(f.svc.analyze_menu_item(f.ctx(f.live), MenuItemIn(**BURGER)))
        self.assertEqual(cm.exception.status, 503)
        self.assertEqual(f.store.menu_items, {})

    def test_validation(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            MenuItemIn(**{**BURGER, "unknown_field": 1})
        with self.assertRaises(ValidationError):
            MenuItemIn(**{**BURGER, "official_nutrition": {"kcal": -1, "protein_g": 1, "carbs_g": 1, "fat_g": 1}})


class PhotoTests(unittest.TestCase):
    def test_photo_ok_and_not_stored(self):
        f = Fixture()
        r = run(f.svc.analyze_photo(f.ctx(f.live), JPEG, "image/jpeg", "  lunch  "))
        self.assertTrue(r["analysis_id"].startswith("pa_"))
        self.assertEqual(r["source"], "ai_estimate")
        self.assertIn("guide only", r["allergen_notice"])
        self.assertEqual(f.store.menu_items, {})
        self.assertEqual(run(f.svc.usage(f.ctx(f.live), None, None))["counts"]["photo_analysis"], 1)

    def test_photo_rejections(self):
        f = Fixture(cap=1000)
        ctx = f.ctx(f.test)
        cases = [
            (b"", "image/jpeg", "invalid_item"),
            (b"%PDF-1.7 not an image", "image/jpeg", "unsupported_image"),
            (JPEG, "application/pdf", "unsupported_image"),
            (b"\xff\xd8\xff" + b"0" * (8 * 1024 * 1024), "image/jpeg", "image_too_large"),
        ]
        for data, mime, code in cases:
            with self.assertRaises(errors.ApiError) as cm:
                run(f.svc.analyze_photo(ctx, data, mime, None))
            self.assertEqual(cm.exception.code, code, (mime, code))
        with self.assertRaises(errors.ApiError) as cm:
            run(f.svc.analyze_photo(ctx, PNG, "image/png", "__not_food__"))
        self.assertEqual(cm.exception.code, "not_food")


class UsageTests(unittest.TestCase):
    def test_range_validation(self):
        f = Fixture()
        ctx = f.ctx(f.live)
        today = f.clock().date()
        with self.assertRaises(errors.ApiError):
            run(f.svc.usage(ctx, today, today - timedelta(days=1)))
        r = run(f.svc.usage(ctx, None, None))
        self.assertEqual((r["from"], r["to"]), ("2026-10-01", "2026-10-12"))

    def test_daily_counters_and_date_range(self):
        f = Fixture()
        ctx = f.ctx(f.test)
        run(f.svc.analyze_menu_item(ctx, MenuItemIn(partner_item_id="a", name="Soup")))
        f.clock.t += timedelta(days=1)
        run(f.svc.analyze_menu_item(ctx, MenuItemIn(partner_item_id="b", name="Salad")))
        run(f.svc.analyze_menu_item(ctx, MenuItemIn(partner_item_id="b", name="Salad")))
        self.assertEqual(len(f.store.usage), 3)  # one row per day per kind, not per request
        day1 = run(f.svc.usage(ctx, f.clock().date() - timedelta(days=1), f.clock().date() - timedelta(days=1)))
        self.assertEqual(day1["counts"], {"analysis": 1, "analysis_cached": 0, "photo_analysis": 0})
        both = run(f.svc.usage(ctx, None, None))
        self.assertEqual(both["counts"], {"analysis": 2, "analysis_cached": 1, "photo_analysis": 0})


class ReviewFixTests(unittest.TestCase):
    def test_cache_hit_echoes_this_requests_partner_item_id(self):
        f = Fixture()
        ctx = f.ctx(f.live)
        first = run(f.svc.analyze_menu_item(ctx, MenuItemIn(**BURGER)))
        other = run(f.svc.analyze_menu_item(ctx, MenuItemIn(**{**BURGER, "partner_item_id": "burger-XL-shop2"})))
        self.assertTrue(other["cached"])
        self.assertEqual(first["partner_item_id"], "burger-001")
        self.assertEqual(other["partner_item_id"], "burger-XL-shop2")
        self.assertEqual(other["menu_item_id"], first["menu_item_id"])  # same content, one analysis

    def test_usage_needs_its_own_scope(self):
        f = Fixture()
        narrow = f.ctx(f.add_key("ptn_a", "live", ["nutrition:read"]))
        with self.assertRaises(errors.ApiError) as cm:
            f.svc.require_scope(narrow, "usage:read")
        self.assertEqual(cm.exception.code, "insufficient_scope")

    def test_last_used_written_at_most_every_5_minutes(self):
        f = Fixture()
        writes = []
        original = f.store.touch_key

        async def counting(key_id, when):
            writes.append(when)
            await original(key_id, when)

        f.store.touch_key = counting
        for _ in range(10):
            f.ctx(f.live)
        self.assertEqual(len(writes), 1)
        f.clock.t += timedelta(minutes=5)
        f.ctx(f.live)
        self.assertEqual(len(writes), 2)


if __name__ == "__main__":
    unittest.main()
