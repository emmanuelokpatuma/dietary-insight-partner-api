"""Admin CLI for partners and API keys. Runs against the database in MONGO_URI.

  python -m scripts.manage_partners create-partner --name "Tossed" --plan pilot --contact ops@tossed.example
  python -m scripts.manage_partners create-key --partner-id ptn_… --mode test --scopes nutrition:read photos:analyze usage:read
  python -m scripts.manage_partners list-keys --partner-id ptn_…
  python -m scripts.manage_partners revoke-key --key-id key_…

A new key is printed ONCE. Only its SHA-256 hash is stored.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

from partner_api.config import load_settings
from partner_api.keys import SCOPES, generate_key, hash_key, new_id
from partner_api.store import MongoStore


async def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="manage_partners")
    sub = p.add_subparsers(dest="cmd", required=True)

    cp = sub.add_parser("create-partner")
    cp.add_argument("--name", required=True)
    cp.add_argument("--plan", choices=["pilot", "production"], default="pilot")
    cp.add_argument("--contact", default="")
    cp.add_argument("--rate-limit", type=int, default=None, help="requests/min override")

    ck = sub.add_parser("create-key")
    ck.add_argument("--partner-id", required=True)
    ck.add_argument("--mode", choices=["test", "live"], default="test")
    ck.add_argument("--scopes", nargs="+", default=["nutrition:read", "usage:read"], choices=sorted(SCOPES))
    ck.add_argument("--label", default="")

    lk = sub.add_parser("list-keys")
    lk.add_argument("--partner-id", required=True)

    rk = sub.add_parser("revoke-key")
    rk.add_argument("--key-id", required=True)

    args = p.parse_args(argv)
    s = load_settings()
    if not s.mongo_uri:
        print("Set MONGO_URI first.", file=sys.stderr)
        return 2
    store = MongoStore(s.mongo_uri, s.mongo_db)
    await store.ensure_indexes()
    now = datetime.now(timezone.utc)

    if args.cmd == "create-partner":
        doc = {"partner_id": new_id("ptn"), "name": args.name, "contact": args.contact, "plan": args.plan,
               "status": "active", "created_at": now}
        if args.rate_limit:
            doc["rate_limit_per_min"] = args.rate_limit
        await store.insert_partner(doc)
        print(json.dumps({k: v for k, v in doc.items() if k != "created_at"}, indent=2))

    elif args.cmd == "create-key":
        if not await store.get_partner(args.partner_id):
            print("No such partner.", file=sys.stderr)
            return 1
        key = generate_key(args.mode)
        await store.insert_key({"key_id": new_id("key"), "key_hash": hash_key(key), "partner_id": args.partner_id,
                                "mode": args.mode, "scopes": sorted(set(args.scopes)), "label": args.label,
                                "last4": key[-4:], "created_at": now, "last_used_at": None, "revoked_at": None})
        print("API key (shown once — store it securely and send it to the partner over a secure channel):")
        print(key)

    elif args.cmd == "list-keys":
        for k in await store.list_keys(args.partner_id):
            print(json.dumps(k, default=str))

    elif args.cmd == "revoke-key":
        ok = await store.revoke_key(args.key_id, now)
        print("revoked" if ok else "not found or already revoked")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
