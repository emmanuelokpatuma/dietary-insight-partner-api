"""HTTP-level tests through FastAPI's TestClient (needs requirements-dev.txt installed)."""
from __future__ import annotations

import asyncio
import unittest

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("fastapi not installed; pip install -r requirements-dev.txt")

from partner_api.config import Settings
from partner_api.keys import generate_key, hash_key
from partner_api.main import create_app
from partner_api.store import MemoryStore

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def make(env="development"):
    store = MemoryStore()
    asyncio.run(store.insert_partner({"partner_id": "ptn_a", "name": "A", "plan": "pilot", "status": "active"}))
    key = generate_key("test")
    asyncio.run(store.insert_key({"key_id": "key_1", "key_hash": hash_key(key), "partner_id": "ptn_a", "mode": "test",
                                  "scopes": ["nutrition:read", "photos:analyze", "usage:read"], "revoked_at": None}))
    app = create_app(Settings(env=env), store=store)
    return TestClient(app), {"Authorization": f"Bearer {key}"}


class ApiTests(unittest.TestCase):
    def test_health(self):
        c, _ = make()
        self.assertEqual(c.get("/healthz").json(), {"ok": True})

    def test_error_format_and_request_id(self):
        c, _ = make()
        r = c.post("/v1/menu-items/analyze", json={"partner_item_id": "a", "name": "b"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"]["code"], "invalid_key")
        self.assertIn("X-Request-Id", r.headers)
        r = c.get("/v1/nope")
        self.assertEqual(r.json()["error"]["code"], "not_found")

    def test_analyze_and_cache(self):
        c, h = make()
        body = {"partner_item_id": "b1", "name": "Chicken burger", "declared_allergens": ["gluten"]}
        r1 = c.post("/v1/menu-items/analyze", json=body, headers=h)
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertFalse(r1.json()["cached"])
        r2 = c.post("/v1/menu-items/analyze", json=body, headers=h)
        self.assertTrue(r2.json()["cached"])

    def test_validation_error_format(self):
        c, h = make()
        r = c.post("/v1/menu-items/analyze", json={"name": "x"}, headers=h)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_item")
        self.assertIn("partner_item_id", r.json()["error"]["message"])

    def test_photo_upload(self):
        c, h = make()
        r = c.post("/v1/photos/analyze", headers=h, files={"image": ("meal.jpg", JPEG, "image/jpeg")},
                   data={"hint": "my lunch"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["analysis_id"].startswith("pa_"))
        r = c.post("/v1/photos/analyze", headers=h, files={"image": ("x.pdf", b"%PDF-1.7", "application/pdf")})
        self.assertEqual(r.status_code, 415)

    def test_usage(self):
        c, h = make()
        c.post("/v1/menu-items/analyze", json={"partner_item_id": "b1", "name": "Soup"}, headers=h)
        r = c.get("/v1/usage", headers=h)
        self.assertEqual(r.json()["counts"]["analysis"], 1)

    def test_docs_hidden_in_production(self):
        c, _ = make("development")
        self.assertEqual(c.get("/v1/docs").status_code, 200)
        # production requires secrets; supply dummies (MemoryStore + no live engine)
        store = MemoryStore()
        app = create_app(Settings(env="production", mongo_uri="x", gemini_api_key=""), store=store)
        self.assertEqual(TestClient(app).get("/v1/docs").status_code, 404)


if __name__ == "__main__":
    unittest.main()
