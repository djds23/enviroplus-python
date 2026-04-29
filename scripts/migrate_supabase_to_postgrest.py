#!/usr/bin/env python3
"""
One-time migration: Supabase readings -> PostgREST (PostgreSQL).

Usage:
    python3 scripts/migrate_supabase_to_postgrest.py

Reads credentials from /home/deanrex/.env (same as the main app).
Requires SUPABASE_URL, SUPABASE_KEY, POSTGREST_URL, POSTGREST_JWT.
"""

import json
import logging
import sys
import urllib.error
import urllib.request

sys.path.insert(0, "app")
from config import POSTGREST_URL

# Supabase vars are no longer in config.py — read them directly here
# since this is a one-time migration script.
import os
sys.path.insert(0, "app")
from config import load_env  # reuse the .env loader
load_env()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    level=logging.INFO,
    datefmt="%H:%M:%S",
)

PAGE_SIZE  = 1000
BATCH_SIZE = 500   # rows per PostgREST insert

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sb_get(path: str) -> list:
    req = urllib.request.Request(
        f"{SUPABASE_URL}{path}",
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def pgr_insert(rows: list[dict]) -> None:
    req = urllib.request.Request(
        f"{POSTGREST_URL}/readings",
        data=json.dumps(rows).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30):
        pass  # 201 = success, HTTPError raised otherwise


# ---------------------------------------------------------------------------
# Read from Supabase (paginated)
# ---------------------------------------------------------------------------

def fetch_all_supabase_rows() -> list[dict]:
    rows = []
    offset = 0
    while True:
        logging.info(f"Fetching Supabase rows {offset}–{offset + PAGE_SIZE - 1}...")
        page = sb_get(
            f"/rest/v1/readings"
            f"?select=label,unit,value"
            f"&order=recorded_at.asc"
            f"&offset={offset}&limit={PAGE_SIZE}"
        )
        if not page:
            break
        rows.extend(page)
        logging.info(f"  Got {len(page)} rows (total: {len(rows)})")
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


# ---------------------------------------------------------------------------
# Write to PostgREST (batched)
# ---------------------------------------------------------------------------

def insert_all(rows: list[dict]) -> tuple[int, int]:
    ok = failed = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        try:
            pgr_insert(batch)
            ok += len(batch)
            logging.info(f"  Inserted batch {i // BATCH_SIZE + 1} ({len(batch)} rows)")
        except urllib.error.HTTPError as e:
            failed += len(batch)
            logging.error(f"  Batch failed ({e.code}): {e.read().decode()[:200]}")
        except urllib.error.URLError as e:
            failed += len(batch)
            logging.error(f"  PostgREST unreachable: {e.reason}")
    return ok, failed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    rows = fetch_all_supabase_rows()
    if not rows:
        logging.info("No rows found in Supabase — nothing to migrate.")
        return

    logging.info(f"Migrating {len(rows)} rows to PostgREST in batches of {BATCH_SIZE}...")
    ok, failed = insert_all(rows)
    logging.info(f"Done. {ok} succeeded, {failed} failed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
