#!/usr/bin/env python3
"""
One-time migration: Supabase readings -> PocketBase readings.

Note: original recorded_at timestamps are NOT preserved — PocketBase will
set the 'created' field to the time of migration for each row.

Usage:
    python3 scripts/migrate_supabase_to_pocketbase.py

Reads credentials from /home/deanrex/.env (same as the main app).
"""

import json
import logging
import sys
import urllib.error
import urllib.request

sys.path.insert(0, "app")
from config import (
    SUPABASE_URL, SUPABASE_KEY,
    POCKETBASE_URL, POCKETBASE_EMAIL, POCKETBASE_PASSWORD,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    level=logging.INFO,
    datefmt="%H:%M:%S",
)

PAGE_SIZE = 1000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def pb_post(path: str, body: dict, token: str | None) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = token
    req = urllib.request.Request(
        f"{POCKETBASE_URL}{path}", data=data, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        return e.code, {"error": body_text}


def sb_get(path: str) -> dict:
    req = urllib.request.Request(
        f"{SUPABASE_URL}{path}",
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def authenticate() -> str:
    logging.info("Authenticating with PocketBase...")
    status, body = pb_post(
        "/api/collections/users/auth-with-password",
        {"identity": POCKETBASE_EMAIL, "password": POCKETBASE_PASSWORD},
        token=None,
    )
    if status != 200:
        logging.error(f"Auth failed ({status}): {body}")
        sys.exit(1)
    logging.info("Authenticated.")
    return body["token"]


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
            f"?select=label,unit,value,recorded_at"
            f"&order=recorded_at.asc"
            f"&offset={offset}&limit={PAGE_SIZE}"
        )
        if not page:
            break
        rows.extend(page)
        logging.info(f"  Got {len(page)} rows (total so far: {len(rows)})")
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


# ---------------------------------------------------------------------------
# Write to PocketBase
# ---------------------------------------------------------------------------

def insert_rows(rows: list[dict], token: str) -> tuple[int, int]:
    ok = failed = 0
    for i, row in enumerate(rows, 1):
        status, body = pb_post(
            "/api/collections/readings/records",
            {"label": row["label"], "unit": row["unit"], "value": row["value"]},
            token=token,
        )
        if status in (200, 201):
            ok += 1
        else:
            failed += 1
            logging.warning(f"Row {i} failed ({status}): {body.get('error', '')[:120]}")

        if i % 100 == 0:
            logging.info(f"  Progress: {i}/{len(rows)} inserted")

    return ok, failed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    token = authenticate()
    rows  = fetch_all_supabase_rows()

    if not rows:
        logging.info("No rows found in Supabase — nothing to migrate.")
        return

    logging.info(f"Migrating {len(rows)} rows to PocketBase...")
    ok, failed = insert_rows(rows, token)
    logging.info(f"Done. {ok} succeeded, {failed} failed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
