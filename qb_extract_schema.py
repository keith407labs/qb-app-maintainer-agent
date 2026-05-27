#!/usr/bin/env python3
"""
Quickbase App Schema Extractor (Step 1 of 2)
Dumps full app metadata, tables, fields, field usage, relationships,
reports, and sample records into a single JSON file.

Step 2: Run qb_schema_to_markdown.py to generate agent-ready markdown.

Usage:
    export QB_REALM=yourcompany.quickbase.com
    export QB_TOKEN=your_user_token
    python qb_extract_schema.py APP_ID [--samples 5] [--redact] [--output schema.json]

    --samples 0     Skip sample records entirely (safest for sensitive data)
    --redact        Mask SSNs, emails, phone numbers, and names in sample records
    --no-pages      Skip the pages endpoint
    --no-webhooks   Skip the webhooks endpoint

No table IDs needed — the script auto-discovers all tables from the app.

API endpoints used:
    GET  /v1/apps/{appId}                  — app metadata
    GET  /v1/tables?appId={appId}          — discover all tables
    GET  /v1/tables/{tableId}?appId=...    — full table detail
    GET  /v1/fields?tableId=...            — field definitions
    GET  /v1/fields/usage?tableId=...      — field usage in reports/forms/etc.
    GET  /v1/tables/{tableId}/relationships — parent-child, lookups, summaries
    GET  /v1/reports?tableId=...           — saved reports
    GET  /v1/webhooks?tableId=...          — webhooks (optional)
    GET  /v1/pages?appId=...              — custom code pages (optional)
    POST /v1/records/query                 — sample records (optional)
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests

BASE = "https://api.quickbase.com/v1/"


def make_headers(token: str, realm: str) -> dict:
    return {
        "QB-Realm-Hostname": realm,
        "Authorization": f"QB-USER-TOKEN {token}",
        "Content-Type": "application/json",
        "User-Agent": "QB-Schema-Crawler/1.0",
    }


def api_get(session: requests.Session, endpoint: str, params: dict | None = None) -> dict | list:
    """GET with retry/backoff for rate limits."""
    url = urljoin(BASE, endpoint)
    for attempt in range(4):
        r = session.get(url, params=params or {})
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 5))
            print(f"    ⏳ Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def api_post(session: requests.Session, endpoint: str, body: dict) -> dict:
    """POST with retry/backoff for rate limits."""
    url = urljoin(BASE, endpoint)
    for attempt in range(4):
        r = session.post(url, json=body)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 5))
            print(f"    ⏳ Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


# ─── Redaction ───────────────────────────────────────────────────────

SSN_RE = re.compile(r"\b\d{3}-?\d{2}-?\d{4}\b")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
# Matches "Case 12-34567" style bankruptcy case numbers
CASE_NUM_RE = re.compile(r"\b\d{2}-\d{5}\b")


def redact_value(val: str) -> str:
    """Mask PII patterns in a string value."""
    val = SSN_RE.sub("***-**-****", val)
    val = EMAIL_RE.sub("***@***.***", val)
    val = PHONE_RE.sub("(***) ***-****", val)
    val = CASE_NUM_RE.sub("**-*****", val)
    return val


def redact_record(record: dict) -> dict:
    """Walk a sample record and redact string values."""
    redacted = {}
    for fid, cell in record.items():
        if isinstance(cell, dict) and "value" in cell:
            v = cell["value"]
            if isinstance(v, str):
                cell = {**cell, "value": redact_value(v)}
        redacted[fid] = cell
    return redacted


# ─── Extraction functions ────────────────────────────────────────────

def extract_app(session, app_id):
    """GET /v1/apps/{appId}"""
    print(f"📦 App metadata for {app_id}...")
    return api_get(session, f"apps/{app_id}")


def extract_tables(session, app_id):
    """GET /v1/tables?appId={appId} — discover all tables."""
    print("📋 Discovering tables...")
    return api_get(session, "tables", params={"appId": app_id})


def extract_table_detail(session, table_id, app_id):
    """GET /v1/tables/{tableId}?appId={appId}
    Fuller table metadata than what getAppTables returns.
    """
    return api_get(session, f"tables/{table_id}", params={"appId": app_id})


def extract_fields(session, table_id):
    """GET /v1/fields?tableId={tableId}&includeFieldPerms=true
    https://developer.quickbase.com/operation/getFields
    """
    return api_get(session, "fields", params={
        "tableId": table_id,
        "includeFieldPerms": "true",
    })


def extract_fields_usage(session, table_id):
    """GET /v1/fields/usage?tableId={tableId}
    https://developer.quickbase.com/operation/getFieldsUsage
    Paginated via skip/top.
    """
    all_usage = []
    skip = 0
    page_size = 100
    while True:
        page = api_get(session, "fields/usage", params={
            "tableId": table_id,
            "skip": skip,
            "top": page_size,
        })
        if not page:
            break
        all_usage.extend(page)
        if len(page) < page_size:
            break
        skip += page_size
    return all_usage


def extract_relationships(session, table_id):
    """GET /v1/tables/{tableId}/relationships
    https://developer.quickbase.com/operation/getRelationships
    """
    return api_get(session, f"tables/{table_id}/relationships")


def extract_reports(session, table_id):
    """GET /v1/reports?tableId={tableId}"""
    return api_get(session, "reports", params={"tableId": table_id})


def extract_webhooks(session, table_id):
    """GET /v1/webhooks?tableId={tableId}"""
    try:
        return api_get(session, "webhooks", params={"tableId": table_id})
    except requests.HTTPError:
        return []


def extract_pages(session, app_id):
    """GET /v1/pages?appId={appId}"""
    print("📄 Custom pages...")
    try:
        return api_get(session, "pages", params={"appId": app_id})
    except requests.HTTPError:
        print("   ⚠️  Pages endpoint unavailable (may need XML API)")
        return []


def extract_sample_records(session, table_id, field_ids, n, redact=False):
    """POST /v1/records/query — grab N sample records."""
    if not field_ids or n <= 0:
        return []
    body = {
        "from": table_id,
        "select": field_ids[:50],
        "options": {"top": n},
    }
    try:
        resp = api_post(session, "records/query", body)
        data = resp.get("data", [])
        if redact:
            data = [redact_record(r) for r in data]
        return data
    except requests.HTTPError:
        return []


# ─── Main ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extract full Quickbase app schema to JSON (Step 1 of 2)"
    )
    parser.add_argument("app_id", help="Quickbase App ID (e.g., bqr4tnc5x)")
    parser.add_argument(
        "--samples", type=int, default=5,
        help="Sample records per table (0 to skip entirely)"
    )
    parser.add_argument(
        "--redact", action="store_true",
        help="Mask SSNs, emails, phones, case numbers in sample records"
    )
    parser.add_argument(
        "--no-pages", action="store_true",
        help="Skip the pages endpoint"
    )
    parser.add_argument(
        "--no-webhooks", action="store_true",
        help="Skip the webhooks endpoint"
    )
    parser.add_argument(
        "--output", "-o",
        help="Output file path (default: qb_schema_{app_id}.json)"
    )
    args = parser.parse_args()

    realm = os.environ.get("QB_REALM")
    token = os.environ.get("QB_TOKEN")
    if not realm or not token:
        print("❌ Set QB_REALM and QB_TOKEN environment variables.", file=sys.stderr)
        print("   export QB_REALM=yourcompany.quickbase.com", file=sys.stderr)
        print("   export QB_TOKEN=your_user_token", file=sys.stderr)
        sys.exit(1)

    session = requests.Session()
    session.headers.update(make_headers(token, realm))

    # ── App-level data ──
    app_meta = extract_app(session, args.app_id)
    tables_raw = extract_tables(session, args.app_id)
    pages = [] if args.no_pages else extract_pages(session, args.app_id)

    # ── Per-table data ──
    table_details = []
    total_fields = 0

    for t in tables_raw:
        tid = t["id"]
        tname = t.get("name", tid)
        print(f"  🔍 Table: {tname} ({tid})")

        # Full table detail (may have more than getAppTables summary)
        table_detail = extract_table_detail(session, tid, args.app_id)

        # Fields — full definitions
        fields = extract_fields(session, tid)
        n_fields = len(fields) if isinstance(fields, list) else 0
        total_fields += n_fields
        print(f"       {n_fields} fields")

        # Field usage
        fields_usage = extract_fields_usage(session, tid)
        if fields_usage:
            print(f"       {len(fields_usage)} field usage records")

        # Relationships
        relationships = extract_relationships(session, tid)

        # Reports
        reports = extract_reports(session, tid)

        # Webhooks (optional)
        webhooks = [] if args.no_webhooks else extract_webhooks(session, tid)

        # Sample records (optional, with redaction)
        field_ids = []
        if isinstance(fields, list):
            field_ids = [f["id"] for f in fields if f.get("id")]
        samples = extract_sample_records(
            session, tid, field_ids, args.samples, redact=args.redact
        )
        if samples:
            label = "sample records (redacted)" if args.redact else "sample records"
            print(f"       {len(samples)} {label}")

        table_details.append({
            "table_meta": t,
            "table_detail": table_detail,
            "fields": fields,
            "fields_usage": fields_usage,
            "relationships": relationships,
            "reports": reports,
            "webhooks": webhooks,
            "sample_records": samples,
        })

    # ── Assemble output ──
    output = {
        "_meta": {
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "app_id": args.app_id,
            "realm": realm,
            "sample_size": args.samples,
            "redacted": args.redact,
            "instructions": (
                "This JSON contains the full schema of a Quickbase app. "
                "Run qb_schema_to_markdown.py on this file to generate "
                "agent-ready markdown docs. Place those alongside any "
                "pipeline YAML exports for a complete reconstruction kit."
            ),
        },
        "app": app_meta,
        "pages": pages,
        "tables": table_details,
    }

    out_path = args.output or f"qb_schema_{args.app_id}.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n✅ Schema written to {out_path}")
    print(f"   {len(table_details)} tables, {total_fields} total fields")
    if args.redact:
        print(f"   🔒 Sample records were redacted")
    print(f"\n💡 Next: python qb_schema_to_markdown.py {out_path} -o ./agent_memory/")


if __name__ == "__main__":
    main()
