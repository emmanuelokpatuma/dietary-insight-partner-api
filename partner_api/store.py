"""Storage. MongoStore for production (same MongoDB as the consumer app, new collections only);
MemoryStore for tests and local sandbox runs.

Collections: partners, partner_keys, menu_items, usage_daily, rate_counters.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol

USAGE_RETENTION = timedelta(days=400)


def _usage_id(partner_id: str, mode: str, day: date, kind: str) -> str:
    return f"{partner_id}:{mode}:{day.isoformat()}:{kind}"


class DuplicateError(Exception):
    pass


class Store(Protocol):
    async def ensure_indexes(self) -> None: ...
    async def get_key(self, key_hash: str) -> dict | None: ...
    async def touch_key(self, key_id: str, when: datetime) -> None: ...
    async def get_partner(self, partner_id: str) -> dict | None: ...
    async def insert_partner(self, doc: dict) -> None: ...
    async def insert_key(self, doc: dict) -> None: ...
    async def revoke_key(self, key_id: str, when: datetime) -> bool: ...
    async def list_keys(self, partner_id: str) -> list[dict]: ...
    async def get_menu_item(self, partner_ns: str, input_hash: str) -> dict | None: ...
    async def insert_menu_item(self, doc: dict) -> None: ...  # raises DuplicateError
    async def incr_counter(self, key: str, expires_at: datetime) -> int: ...
    async def incr_usage(self, partner_id: str, mode: str, day: date, kind: str) -> None: ...
    async def usage_summary(self, partner_id: str, mode: str, start: date, end: date) -> dict[str, int]: ...


class MemoryStore:
    def __init__(self):
        self.partners: dict[str, dict] = {}
        self.keys: dict[str, dict] = {}          # by key_hash
        self.menu_items: dict[tuple, dict] = {}  # by (partner_ns, input_hash)
        self.counters: dict[str, int] = {}
        self.usage: dict[str, dict] = {}  # usage_daily rows by _id

    async def ensure_indexes(self) -> None:
        return None

    async def get_key(self, key_hash):
        return self.keys.get(key_hash)

    async def touch_key(self, key_id, when):
        for k in self.keys.values():
            if k["key_id"] == key_id:
                k["last_used_at"] = when

    async def get_partner(self, partner_id):
        return self.partners.get(partner_id)

    async def insert_partner(self, doc):
        if doc["partner_id"] in self.partners:
            raise DuplicateError(doc["partner_id"])
        self.partners[doc["partner_id"]] = dict(doc)

    async def insert_key(self, doc):
        self.keys[doc["key_hash"]] = dict(doc)

    async def revoke_key(self, key_id, when):
        for k in self.keys.values():
            if k["key_id"] == key_id and not k.get("revoked_at"):
                k["revoked_at"] = when
                return True
        return False

    async def list_keys(self, partner_id):
        return [{k: v for k, v in d.items() if k != "key_hash"} for d in self.keys.values() if d["partner_id"] == partner_id]

    async def get_menu_item(self, partner_ns, input_hash):
        return self.menu_items.get((partner_ns, input_hash))

    async def insert_menu_item(self, doc):
        k = (doc["partner_ns"], doc["input_hash"])
        if k in self.menu_items:
            raise DuplicateError(str(k))
        self.menu_items[k] = dict(doc)

    async def incr_counter(self, key, expires_at):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def incr_usage(self, partner_id, mode, day, kind):
        _id = _usage_id(partner_id, mode, day, kind)
        row = self.usage.setdefault(_id, {"partner_id": partner_id, "mode": mode, "day": day.isoformat(),
                                          "kind": kind, "n": 0})
        row["n"] += 1

    async def usage_summary(self, partner_id, mode, start, end):
        out: dict[str, int] = {}
        for r in self.usage.values():
            if r["partner_id"] == partner_id and r["mode"] == mode and start.isoformat() <= r["day"] <= end.isoformat():
                out[r["kind"]] = out.get(r["kind"], 0) + r["n"]
        return out


class MongoStore:
    def __init__(self, uri: str, db_name: str):
        from motor.motor_asyncio import AsyncIOMotorClient

        self._client = AsyncIOMotorClient(uri, tz_aware=True, appname="dietary-insight-partner-api")
        self.db = self._client[db_name]

    async def ensure_indexes(self):
        from pymongo import ASCENDING

        db = self.db
        await db.partners.create_index("partner_id", unique=True)
        await db.partner_keys.create_index("key_hash", unique=True)
        await db.partner_keys.create_index("key_id", unique=True)
        await db.partner_keys.create_index("partner_id")
        await db.menu_items.create_index([("partner_ns", ASCENDING), ("input_hash", ASCENDING)], unique=True)
        await db.menu_items.create_index([("partner_ns", ASCENDING), ("partner_item_id", ASCENDING)])
        await db.usage_daily.create_index([("partner_id", ASCENDING), ("mode", ASCENDING), ("day", ASCENDING)])
        await db.usage_daily.create_index("expires_at", expireAfterSeconds=0)
        await db.rate_counters.create_index("expires_at", expireAfterSeconds=0)

    async def get_key(self, key_hash):
        return await self.db.partner_keys.find_one({"key_hash": key_hash}, {"_id": 0})

    async def touch_key(self, key_id, when):
        await self.db.partner_keys.update_one({"key_id": key_id}, {"$set": {"last_used_at": when}})

    async def get_partner(self, partner_id):
        return await self.db.partners.find_one({"partner_id": partner_id}, {"_id": 0})

    async def insert_partner(self, doc):
        from pymongo.errors import DuplicateKeyError

        try:
            await self.db.partners.insert_one(dict(doc))
        except DuplicateKeyError as e:
            raise DuplicateError(str(e)) from e

    async def insert_key(self, doc):
        await self.db.partner_keys.insert_one(dict(doc))

    async def revoke_key(self, key_id, when):
        r = await self.db.partner_keys.update_one({"key_id": key_id, "revoked_at": None}, {"$set": {"revoked_at": when}})
        return r.modified_count == 1

    async def list_keys(self, partner_id):
        cur = self.db.partner_keys.find({"partner_id": partner_id}, {"_id": 0, "key_hash": 0})
        return await cur.to_list(length=500)

    async def get_menu_item(self, partner_ns, input_hash):
        return await self.db.menu_items.find_one({"partner_ns": partner_ns, "input_hash": input_hash}, {"_id": 0})

    async def insert_menu_item(self, doc):
        from pymongo.errors import DuplicateKeyError

        try:
            await self.db.menu_items.insert_one(dict(doc))
        except DuplicateKeyError as e:
            raise DuplicateError(str(e)) from e

    async def incr_counter(self, key, expires_at):
        from pymongo import ReturnDocument

        doc = await self.db.rate_counters.find_one_and_update(
            {"_id": key},
            {"$inc": {"n": 1}, "$setOnInsert": {"expires_at": expires_at}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(doc["n"])

    async def incr_usage(self, partner_id, mode, day, kind):
        expires = datetime.combine(day, time.min, tzinfo=timezone.utc) + USAGE_RETENTION
        await self.db.usage_daily.update_one(
            {"_id": _usage_id(partner_id, mode, day, kind)},
            {"$inc": {"n": 1},
             "$setOnInsert": {"partner_id": partner_id, "mode": mode, "day": day.isoformat(), "kind": kind,
                              "expires_at": expires}},
            upsert=True,
        )

    async def usage_summary(self, partner_id, mode, start, end):
        pipeline = [
            {"$match": {"partner_id": partner_id, "mode": mode,
                        "day": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
            {"$group": {"_id": "$kind", "n": {"$sum": "$n"}}},
        ]
        return {row["_id"]: int(row["n"]) async for row in self.db.usage_daily.aggregate(pipeline)}
