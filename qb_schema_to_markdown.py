#!/usr/bin/env python3
"""
Quickbase Schema JSON → Agent Markdown Generator (Merged)

Takes the JSON produced by qb_extract_schema.py and creates an agent-friendly
markdown knowledge base with cross-cutting indexes, runbook-style controls,
and reusable skill procedures.

Usage:
    python qb_schema_to_markdown.py qb_schema_APPID.json --out quickbase-agent-memory

Flags:
    --include-samples       Write sample record shapes (values only, no raw PII).
                            Off by default. Crawl with --redact for safety.
    --include-pages         Write custom code pages to pages/ subfolder.

Recommended workflow:
    1. Run the crawler:  python qb_extract_schema.py APP_ID --samples 0
    2. Run this script:  python qb_schema_to_markdown.py qb_schema_APP_ID.json
    3. Drop pipeline YAML exports into the output folder.
    4. Point your agent / coding assistant at the generated folder.

Generated structure:
    quickbase-agent-memory/
      README.md                       — agent usage guide + safety notes
      app-overview.md                 — app metadata, variables, table index
      fields-index.md                 — all fields across all tables (flat)
      field-usage.md                  — where fields are referenced (from getFieldsUsage)
      formulas-index.md               — formula source code, dependency graph, reverse index
      relationships.md                — parent-child, lookups, summaries
      reports-index.md                — saved report definitions
      data-quality-rules.md           — auto-derived quality check starting points
      tables/
        <table-name>.md               — per-table: fields, formulas, relationships, reports
      controls/
        required-fields.md            — agent runbook: blank required fields
        orphan-records.md             — agent runbook: orphan child records
        formula-mismatch-checks.md    — agent runbook: formula reconciliation
      skills/
        schema-refresh.md             — how to re-crawl and regenerate
        troubleshoot-formula.md       — diagnose formula issues
        inspect-relationship.md       — diagnose lookup/summary issues
        run-data-quality-check.md     — execute quality checks
      pages/                          — custom code pages (optional)
        <page-name>.md
      samples/                        — sample record shapes (optional)
        <table-name>.md

Notes:
    - This script does not call Quickbase directly. It only transforms crawler JSON.
    - It is defensive about response shapes because Quickbase API output varies.
    - It intentionally avoids writing sample record values by default to reduce
      the chance of sensitive data landing in long-lived agent memory.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


# ─── Utilities ───────────────────────────────────────────────────────


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "untitled"


def md_escape(value: Any) -> str:
    """Escape characters that break markdown tables."""
    if value is None:
        return ""
    text = str(value)
    return text.replace("|", r"\|").replace("\n", " ").strip()


def code_block(value: Any, language: str = "") -> str:
    if value is None or value == "":
        return "_None captured._"
    return f"```{language}\n{value}\n```"


def write_md(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def as_list(value: Any) -> list[Any]:
    """Normalize API responses that may be a list, a dict wrapper, or None."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "data", "fields", "reports", "relationships"):
            if isinstance(value.get(key), list):
                return value[key]
        return [value]
    return []


def get_nested(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = d
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


# ─── Field accessors (defensive against varying API shapes) ──────────


def field_id(field: dict[str, Any]) -> Any:
    return field.get("id") or field.get("fid") or field.get("fieldId")


def field_name(field: dict[str, Any]) -> str:
    return str(
        field.get("label") or field.get("name") or field.get("fieldName")
        or f"FID {field.get('id', '')}"
    ).strip()


def field_type(field: dict[str, Any]) -> str:
    return str(field.get("fieldType") or field.get("type") or field.get("mode") or "")


def field_formula(field: dict[str, Any]) -> str:
    return (
        get_nested(field, "properties", "formula", default=None)
        or field.get("formula")
        or get_nested(field, "properties", "formulaText", default=None)
        or ""
    )


def field_required(field: dict[str, Any]) -> Any:
    return (
        get_nested(field, "properties", "required", default=None)
        or field.get("required")
        or get_nested(field, "permissions", "required", default=None)
    )


def field_unique(field: dict[str, Any]) -> Any:
    return (
        get_nested(field, "properties", "unique", default=None)
        or field.get("unique")
    )


def field_detail_str(field: dict[str, Any]) -> str:
    """Summarize key field properties beyond type (choices, FK, lookup, summary)."""
    props = field.get("properties", {})
    extras = []

    choices = props.get("choices")
    if choices:
        if len(choices) <= 6:
            extras.append(f"choices: {choices}")
        else:
            extras.append(f"{len(choices)} choices")
    if props.get("foreignKey"):
        extras.append("FK")
    if props.get("masterTableTag"):
        extras.append(f"lookup from {props['masterTableTag']}")
    if props.get("summaryFunction"):
        extras.append(f"summary: {props['summaryFunction']}")
    if props.get("snapFieldId"):
        extras.append(f"snap of FID {props['snapFieldId']}")
    if props.get("defaultValue") not in (None, ""):
        extras.append(f"default: {props['defaultValue']}")

    return "; ".join(extras) if extras else ""


# ─── Table accessors ────────────────────────────────────────────────


def table_name(table_blob: dict[str, Any]) -> str:
    meta = table_blob.get("table_meta") or table_blob.get("table_detail") or table_blob
    return str(meta.get("name") or meta.get("label") or meta.get("id") or "Unnamed Table")


def table_id(table_blob: dict[str, Any]) -> str:
    meta = table_blob.get("table_meta") or table_blob.get("table_detail") or table_blob
    return str(meta.get("id") or meta.get("tableId") or meta.get("dbid") or "")


def table_description(table_blob: dict[str, Any]) -> str:
    td = table_blob.get("table_detail") or table_blob.get("table_meta") or {}
    return str(td.get("description") or "")


def relationship_name(rel: dict[str, Any]) -> str:
    candidates = [
        rel.get("name"),
        rel.get("label"),
        get_nested(rel, "parentTable", "name"),
        get_nested(rel, "childTable", "name"),
        rel.get("id"),
    ]
    return str(next((x for x in candidates if x), "Relationship"))


def collect_tables(schema: dict[str, Any]) -> list[dict[str, Any]]:
    return as_list(schema.get("tables"))


# ─── Renderers ───────────────────────────────────────────────────────


def render_readme(schema: dict[str, Any]) -> str:
    meta = schema.get("_meta", {})
    app = schema.get("app", {}) or {}
    app_name = app.get("name") or app.get("appName") or meta.get("app_id") or "Quickbase App"
    return f"""# Quickbase Agent Memory — {md_escape(app_name)}

Generated from a Quickbase schema JSON export.

## App

- **Name:** {md_escape(app_name)}
- **App ID:** `{md_escape(meta.get('app_id'))}`
- **Realm:** `{md_escape(meta.get('realm'))}`
- **Extracted at:** {md_escape(meta.get('extracted_at'))}
- **Sample size in source JSON:** {md_escape(meta.get('sample_size'))}

## How to use this folder

Use these markdown files as read-only operating context for an agent that helps
maintain, troubleshoot, or rebuild the Quickbase application.

Recommended agent instructions:

1. Read `app-overview.md` first to understand the table structure.
2. Use `fields-index.md` or `field-usage.md` to find where a field lives and how it is used.
3. Use `relationships.md` before diagnosing lookup or summary-field issues.
4. Use `formulas-index.md` before changing or troubleshooting formulas —
   check the **Reverse Dependency Index** before renaming or deleting any field.
5. Use `data-quality-rules.md` and files in `controls/` before proposing quality checks.
6. Use files in `skills/` for step-by-step agent procedures.
7. Never make write/update/delete API calls unless explicitly authorized by a human.

## Important safety note

Avoid storing live debtor, customer, SSN, address, case, payment, or
account-level values in long-lived agent memory unless they are masked
or intentionally approved.
"""


def render_app_overview(schema: dict[str, Any], tables: list[dict[str, Any]]) -> str:
    meta = schema.get("_meta", {})
    app = schema.get("app", {}) or {}
    lines = [
        "# App Overview",
        "",
        f"- **App name:** {md_escape(app.get('name') or app.get('appName'))}",
        f"- **App ID:** `{md_escape(meta.get('app_id') or app.get('id'))}`",
        f"- **Realm:** `{md_escape(meta.get('realm'))}`",
        f"- **Extracted at:** {md_escape(meta.get('extracted_at'))}",
        "",
    ]

    # App variables (if present)
    variables = app.get("variables") or []
    if variables:
        lines += ["## App Variables", ""]
        for var in variables:
            if isinstance(var, dict):
                lines.append(f"- `{var.get('name', '?')}` = `{var.get('value', '')}`")
            else:
                lines.append(f"- `{var}`")
        lines.append("")

    lines += [
        "## Tables",
        "",
        "| Table | DBID | Description | Fields | Relationships | Reports |",
        "|---|---|---|---:|---:|---:|",
    ]
    for t in tables:
        desc = table_description(t)
        desc_short = (desc[:60] + "…") if len(desc) > 60 else desc
        lines.append(
            f"| {md_escape(table_name(t))} | `{md_escape(table_id(t))}` | "
            f"{md_escape(desc_short)} | "
            f"{len(as_list(t.get('fields')))} | "
            f"{len(as_list(t.get('relationships')))} | "
            f"{len(as_list(t.get('reports')))} |"
        )
    return "\n".join(lines)


def render_table_file(table: dict[str, Any]) -> str:
    tname = table_name(table)
    tid = table_id(table)
    desc = table_description(table)
    fields = as_list(table.get("fields"))
    relationships = as_list(table.get("relationships"))
    reports = as_list(table.get("reports"))
    webhooks = as_list(table.get("webhooks"))

    lines = [
        f"# Table: {tname}",
        "",
        f"- **DBID:** `{tid}`",
    ]
    if desc:
        lines.append(f"- **Description:** {md_escape(desc)}")
    lines += [
        f"- **Fields:** {len(fields)}",
        f"- **Relationships captured:** {len(relationships)}",
        f"- **Reports captured:** {len(reports)}",
        f"- **Webhooks captured:** {len(webhooks)}",
        "",
        "## Fields",
        "",
        "| FID | Field Name | Type | Req | Uniq | Formula? | Details |",
        "|---:|---|---|---|---|---|---|",
    ]

    for f in sorted(fields, key=lambda x: (field_id(x) is None, field_id(x) or 0)):
        has_formula = "Yes" if field_formula(f) else ""
        req = "✓" if field_required(f) else ""
        uniq = "✓" if field_unique(f) else ""
        detail = field_detail_str(f)
        if len(detail) > 80:
            detail = detail[:77] + "…"
        lines.append(
            f"| {md_escape(field_id(f))} | {md_escape(field_name(f))} | "
            f"{md_escape(field_type(f))} | {req} | {uniq} | {has_formula} | "
            f"{md_escape(detail)} |"
        )

    # Formula fields with full source
    formula_fields = [f for f in fields if field_formula(f)]
    if formula_fields:
        lines.extend(["", "## Formula Fields", ""])
        for f in formula_fields:
            lines.extend([
                f"### FID {field_id(f)} — {field_name(f)}",
                "",
                f"- **Type:** {md_escape(field_type(f))}",
                "",
                code_block(field_formula(f), "quickbase"),
                "",
            ])

    # Relationships inline
    if relationships:
        lines.extend(["", "## Relationships", ""])
        for rel in relationships:
            lines.extend([
                f"### {relationship_name(rel)}",
                "",
                code_block(json.dumps(rel, indent=2, default=str), "json"),
                "",
            ])

    # Reports inline
    if reports:
        lines.extend([
            "", "## Reports", "",
            "| Report ID | Name | Type | Description |",
            "|---|---|---|---|",
        ])
        for report in reports:
            lines.append(
                f"| {md_escape(report.get('id'))} | {md_escape(report.get('name'))} | "
                f"{md_escape(report.get('type'))} | {md_escape(report.get('description'))} |"
            )

    return "\n".join(lines)


def render_fields_index(tables: list[dict[str, Any]]) -> str:
    lines = [
        "# Fields Index",
        "",
        "All fields across all tables in one flat view.",
        "",
        "| Table | DBID | FID | Field Name | Type | Req | Uniq | Formula? | Details |",
        "|---|---|---:|---|---|---|---|---|---|",
    ]
    for t in tables:
        for f in as_list(t.get("fields")):
            req = "✓" if field_required(f) else ""
            uniq = "✓" if field_unique(f) else ""
            has_formula = "Yes" if field_formula(f) else ""
            detail = field_detail_str(f)
            if len(detail) > 60:
                detail = detail[:57] + "…"
            lines.append(
                f"| {md_escape(table_name(t))} | `{md_escape(table_id(t))}` | "
                f"{md_escape(field_id(f))} | {md_escape(field_name(f))} | "
                f"{md_escape(field_type(f))} | {req} | {uniq} | {has_formula} | "
                f"{md_escape(detail)} |"
            )
    return "\n".join(lines)


def render_field_usage(tables: list[dict[str, Any]]) -> str:
    """Render field usage data from getFieldsUsage API (where each field
    appears in reports, forms, relationships, notifications, etc.)."""
    lines = [
        "# Field Usage",
        "",
        "Where each field is referenced across reports, forms, relationships,",
        "default reports, notifications, and reminders.",
        "",
        "Source: `GET /v1/fields/usage?tableId=...`",
        "",
    ]
    any_usage = False

    for t in tables:
        usage_list = as_list(t.get("fields_usage"))
        if not usage_list:
            continue

        any_usage = True
        lines.extend([f"## {table_name(t)} (`{table_id(t)}`)", ""])

        for u in usage_list:
            # Usage records have a "field" key and a "usage" key
            f_info = u.get("field", {}) if isinstance(u, dict) else {}
            fid = f_info.get("id") or u.get("fieldId") or "?"
            fname = f_info.get("name") or f_info.get("label") or ""

            usages = u.get("usage", {}) if isinstance(u, dict) else {}
            if not isinstance(usages, dict):
                continue

            parts = []
            for context, items in usages.items():
                if items:
                    count = len(items) if isinstance(items, list) else 1
                    parts.append(f"{context}: {count}")

            if parts:
                lines.append(f"- **FID {fid}** `{md_escape(fname)}` — {', '.join(parts)}")

        lines.append("")

    if not any_usage:
        lines.append(
            "_No field usage data found. Run the crawler with a user token that has "
            "access to the getFieldsUsage endpoint._"
        )

    return "\n".join(lines)


def parse_formula_deps(formula: str) -> list[str]:
    """Extract [FieldName] references from a Quickbase formula string.

    Returns deduplicated list preserving first-occurrence order.
    Handles nested brackets, underscored system fields like [_DBID_...],
    and ignores string literals inside double quotes.
    """
    if not formula:
        return []
    # Strip quoted strings so we don't pick up field refs inside literals
    cleaned = re.sub(r'"[^"]*"', '', formula)
    refs = re.findall(r'\[([^\[\]]+)\]', cleaned)
    seen: set[str] = set()
    deduped: list[str] = []
    for r in refs:
        key = r.strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(key)
    return deduped


# Patterns worth flagging for an agent maintaining formulas
_FORMULA_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("null-handling", "`Nz()` — null-to-zero/default coercion", re.compile(r'\bNz\s*\(', re.I)),
    ("conditional", "`If()` — branching logic", re.compile(r'\bIf\s*\(', re.I)),
    ("case", "`Case()` — multi-branch logic", re.compile(r'\bCase\s*\(', re.I)),
    ("date-math", "Date arithmetic (`Today()`, `ToDate()`, `Months()`, etc.)", re.compile(r'\b(Today|ToDate|Months|Weeks|Days|Hours|Minutes|Seconds|DateAdd|DateDiff)\s*\(', re.I)),
    ("text-coerce", "Type coercion (`ToText()`, `ToNumber()`, `ToDate()`)", re.compile(r'\b(ToText|ToNumber|ToDate)\s*\(', re.I)),
    ("cross-table", "Cross-table query (`GetRecord()`, `GetRecords()`, `GetFieldValues()`)", re.compile(r'\b(GetRecord|GetRecords|GetFieldValues)\s*\(', re.I)),
    ("aggregation", "Aggregation (`Size()`, `SumValues()`, `AvgValues()`)", re.compile(r'\b(Size|SumValues|AvgValues|MinValues|MaxValues)\s*\(', re.I)),
    ("string-ops", "String operations (`Contains()`, `Left()`, `Right()`, `SearchAndReplace()`)", re.compile(r'\b(Contains|Left|Right|Mid|Trim|SearchAndReplace|Upper|Lower)\s*\(', re.I)),
    ("url-ops", "URL/navigation (`URLRoot()`, `Rurl()`, `URLEncode()`)", re.compile(r'\b(URLRoot|Rurl|URLEncode)\s*\(', re.I)),
    ("user-ops", "User context (`User()`, `UserRoles()`)", re.compile(r'\b(User|UserRoles)\s*\(', re.I)),
]


def detect_formula_patterns(formula: str) -> list[str]:
    """Return human-readable labels for notable patterns found in a formula."""
    return [label for _, label, pat in _FORMULA_PATTERNS if pat.search(formula)]


def render_formulas_index(tables: list[dict[str, Any]]) -> str:
    """Render a comprehensive formulas reference with:
    - Per-formula: source code, dependencies, pattern flags
    - Reverse dependency index: which formulas reference a given field
    - Summary statistics
    """
    # ── Collect all formulas ──
    all_formulas: list[dict[str, Any]] = []   # enriched formula records
    reverse_deps: dict[str, list[str]] = defaultdict(list)  # dep_name → [formula labels]
    field_name_by_table: dict[str, dict[Any, str]] = {}  # tid → {fid: name}

    for t in tables:
        tid = table_id(t)
        tname = table_name(t)
        fields = as_list(t.get("fields"))

        # Build name lookup for this table (used to resolve deps to FIDs)
        name_map: dict[Any, str] = {}
        fid_by_name: dict[str, Any] = {}
        for f in fields:
            fid = field_id(f)
            fname = field_name(f)
            if fid is not None:
                name_map[fid] = fname
                fid_by_name[fname] = fid
        field_name_by_table[tid] = name_map

        for f in fields:
            formula = field_formula(f)
            if not formula:
                continue

            fid = field_id(f)
            fname = field_name(f)
            deps = parse_formula_deps(formula)
            patterns = detect_formula_patterns(formula)
            label = f"{tname} → FID {fid} `{fname}`"

            # Build reverse index
            for dep in deps:
                reverse_deps[dep].append(label)

            # Resolve deps to FIDs where possible
            deps_with_fids = []
            for dep in deps:
                resolved_fid = fid_by_name.get(dep)
                if resolved_fid is not None:
                    deps_with_fids.append(f"`[{dep}]` (FID {resolved_fid})")
                else:
                    deps_with_fids.append(f"`[{dep}]`")

            all_formulas.append({
                "table_name": tname,
                "table_id": tid,
                "field_id": fid,
                "field_name": fname,
                "field_type": field_type(f),
                "formula": formula,
                "deps": deps,
                "deps_display": deps_with_fids,
                "patterns": patterns,
            })

    # ── Render ──
    lines = [
        "# Formulas Index",
        "",
        "All formula fields across the app with source code, dependencies, and pattern flags.",
        "",
    ]

    if not all_formulas:
        lines.append("No formula fields were found in the schema export.")
        return "\n".join(lines)

    # Summary stats
    tables_with_formulas = len(set(f["table_name"] for f in all_formulas))
    pattern_counts: dict[str, int] = defaultdict(int)
    for f in all_formulas:
        for p in f["patterns"]:
            pattern_counts[p] += 1

    lines.extend([
        "## Summary",
        "",
        f"- **Total formula fields:** {len(all_formulas)}",
        f"- **Tables with formulas:** {tables_with_formulas}",
        f"- **Unique dependencies referenced:** {len(reverse_deps)}",
        "",
    ])

    if pattern_counts:
        lines.append("**Patterns detected across all formulas:**")
        lines.append("")
        for pattern, count in sorted(pattern_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {pattern} — {count} formula(s)")
        lines.append("")

    lines.extend([
        "---",
        "",
    ])

    # ── Per-table formula details ──
    current_table = None
    for f in all_formulas:
        if f["table_name"] != current_table:
            current_table = f["table_name"]
            lines.extend([f"## {current_table}", ""])

        lines.extend([
            f"### FID {f['field_id']} — {f['field_name']}",
            "",
            f"- **Table:** {md_escape(f['table_name'])}",
            f"- **DBID:** `{md_escape(f['table_id'])}`",
            f"- **Type:** {md_escape(f['field_type'])}",
        ])

        if f["deps_display"]:
            lines.append(f"- **Depends on:** {', '.join(f['deps_display'])}")
        else:
            lines.append("- **Depends on:** _none detected_")

        if f["patterns"]:
            lines.append(f"- **Patterns:** {'; '.join(f['patterns'])}")

        lines.extend([
            "",
            code_block(f["formula"], "quickbase"),
            "",
        ])

    # ── Reverse dependency index ──
    lines.extend([
        "---",
        "",
        "## Reverse Dependency Index",
        "",
        "Which formulas reference a given field. Use this for impact analysis",
        "before renaming, deleting, or changing the type of a field.",
        "",
    ])

    for dep_name in sorted(reverse_deps.keys()):
        consumers = reverse_deps[dep_name]
        lines.append(f"### `[{dep_name}]`")
        lines.append("")
        lines.append(f"Referenced by {len(consumers)} formula(s):")
        lines.append("")
        for consumer in consumers:
            lines.append(f"- {consumer}")
        lines.append("")

    return "\n".join(lines)


def render_relationships(tables: list[dict[str, Any]]) -> str:
    lines = ["# Relationships", ""]
    found = False
    for t in tables:
        rels = as_list(t.get("relationships"))
        if not rels:
            continue
        found = True
        lines.extend([f"## {table_name(t)}", ""])
        for rel in rels:
            lines.extend([
                f"### {relationship_name(rel)}",
                "",
                code_block(json.dumps(rel, indent=2, default=str), "json"),
                "",
            ])
    if not found:
        lines.append("No relationships were found in the schema export.")
    return "\n".join(lines)


def render_reports_index(tables: list[dict[str, Any]]) -> str:
    lines = [
        "# Reports Index",
        "",
        "| Table | DBID | Report ID | Report Name | Type | Description |",
        "|---|---|---|---|---|---|",
    ]
    found = False
    for t in tables:
        for report in as_list(t.get("reports")):
            found = True
            lines.append(
                f"| {md_escape(table_name(t))} | `{md_escape(table_id(t))}` | "
                f"{md_escape(report.get('id'))} | {md_escape(report.get('name'))} | "
                f"{md_escape(report.get('type'))} | {md_escape(report.get('description'))} |"
            )
    if not found:
        lines.append("| _No reports found_ |  |  |  |  |  |")
    return "\n".join(lines)


def render_data_quality_rules(tables: list[dict[str, Any]]) -> str:
    required_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    formula_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for t in tables:
        for f in as_list(t.get("fields")):
            if field_required(f):
                required_by_table[table_name(t)].append(f)
            if field_formula(f):
                formula_by_table[table_name(t)].append(f)

    lines = [
        "# Data Quality Rules",
        "",
        "Use this file as a starting point for building repeatable Quickbase data-quality checks.",
        "",
        "## Suggested Control Categories",
        "",
        "1. Required business fields that are blank even if Quickbase does not technically require them.",
        "2. Orphan child records where a reference field is blank or invalid.",
        "3. Formula mismatch checks where rounded numeric outputs disagree.",
        "4. Duplicate detection using debtor, case, account, or external IDs.",
        "5. Stale records that have not moved status after an expected time window.",
        "6. Relationship summary checks where totals should reconcile to child records.",
        "",
        "## Required Fields Captured from Schema",
        "",
    ]

    if not required_by_table:
        lines.append("No required fields were detected from the exported schema.")
    else:
        for tname, fields in required_by_table.items():
            lines.extend([f"### {tname}", "", "| FID | Field Name | Type |", "|---:|---|---|"])
            for f in fields:
                lines.append(
                    f"| {md_escape(field_id(f))} | {md_escape(field_name(f))} | "
                    f"{md_escape(field_type(f))} |"
                )
            lines.append("")

    lines.extend([
        "## Formula Fields Worth Reviewing",
        "",
        "Formula fields often encode business logic. Review these when troubleshooting data-quality issues.",
        "",
    ])

    if not formula_by_table:
        lines.append("No formula fields were detected from the exported schema.")
    else:
        for tname, fields in formula_by_table.items():
            lines.extend([f"### {tname}", "", "| FID | Field Name | Type |", "|---:|---|---|"])
            for f in fields:
                lines.append(
                    f"| {md_escape(field_id(f))} | {md_escape(field_name(f))} | "
                    f"{md_escape(field_type(f))} |"
                )
            lines.append("")

    return "\n".join(lines)


def render_pages(schema: dict[str, Any]) -> dict[str, str]:
    """Returns dict of {filename: content} for each custom code page."""
    pages = as_list(schema.get("pages"))
    files: dict[str, str] = {}

    if not pages:
        return {}

    for p in pages:
        pname = p.get("name") or p.get("pageName") or f"page-{p.get('id', 'unknown')}"
        pid = p.get("id") or "?"
        body = p.get("body") or p.get("content") or ""

        lines = [
            f"# Page: {pname}",
            "",
            f"- **Page ID:** `{pid}`",
            "",
        ]

        if body:
            # Truncate very long pages to keep memory manageable
            if len(body) > 10_000:
                body = body[:10_000] + "\n\n... [truncated — see source JSON for full content]"
            lines.append(code_block(body, "html"))
        else:
            lines.append("_No page body captured._")

        filename = slugify(pname) + ".md"
        files[filename] = "\n".join(lines)

    return files


def render_samples(tables: list[dict[str, Any]]) -> dict[str, str]:
    """Returns dict of {filename: content} for tables that have sample records."""
    files: dict[str, str] = {}

    for t in tables:
        samples = as_list(t.get("sample_records"))
        if not samples:
            continue

        tname = table_name(t)
        lines = [
            f"# Sample Records: {tname}",
            "",
            f"- **Table:** {tname} (`{table_id(t)}`)",
            f"- **Records shown:** {len(samples)}",
            "",
            "⚠️ **Check for sensitive data before committing this file.**",
            "",
            "These records show the data shape (which fields are populated, typical",
            "value formats, null patterns). If the crawler was run with `--redact`,",
            "PII patterns have been masked.",
            "",
        ]

        # Show at most 5 for readability
        for i, rec in enumerate(samples[:5], 1):
            lines.extend([
                f"### Record {i}",
                "",
                code_block(json.dumps(rec, indent=2, default=str), "json"),
                "",
            ])

        if len(samples) > 5:
            lines.append(f"_… {len(samples) - 5} additional records in source JSON._")

        filename = slugify(tname) + ".md"
        files[filename] = "\n".join(lines)

    return files


# ─── Controls (agent runbooks) ───────────────────────────────────────


def render_controls() -> dict[str, str]:
    return {
        "required-fields.md": """# Control: Required Fields

Purpose: identify records where critical fields are blank.

## Agent procedure

1. Read the relevant table file in `tables/`.
2. Identify required Quickbase fields and business-required fields.
3. Build a read-only `POST /v1/records/query` request.
4. Filter for records where required fields are blank.
5. Return record IDs and field names only unless a human authorizes showing sensitive values.

## Example query pattern

```json
{
  "from": "TABLE_DBID",
  "select": [3, 6, 7],
  "where": "{FIELD_ID.EX.''}",
  "options": {"top": 100}
}
```
""",
        "orphan-records.md": """# Control: Orphan Records

Purpose: identify child records whose parent/reference field is blank or invalid.

## Agent procedure

1. Read `relationships.md`.
2. Identify the child table and reference field.
3. Query for records where the reference field is blank.
4. If needed, query questionable reference values and compare against parent records.
5. Report counts first, then record IDs.

## Example blank-reference query

```json
{
  "from": "CHILD_TABLE_DBID",
  "select": [3, REFERENCE_FIELD_ID],
  "where": "{REFERENCE_FIELD_ID.EX.''}",
  "options": {"top": 100}
}
```
""",
        "formula-mismatch-checks.md": """# Control: Formula Mismatch Checks

Purpose: find records where calculated or reconciled values disagree.

## Agent procedure

1. Read `formulas-index.md`.
2. Identify the formula field and source fields.
3. Pull a small sample of records using `POST /v1/records/query`.
4. Recompute expected values outside Quickbase when practical.
5. Compare rounded values to avoid false positives from floating-point precision.
6. Report record IDs, expected value, actual value, and difference.

## Recommended numeric comparison

Use both signed and absolute differences:

- Signed difference: shows which value is higher.
- Absolute difference: helps threshold/filter materiality.
""",
    }


# ─── Skills (agent procedures) ──────────────────────────────────────


def render_skills() -> dict[str, str]:
    return {
        "schema-refresh.md": """# Skill: Schema Refresh

Purpose: keep agent memory aligned with the current Quickbase application schema.

## Steps

1. Run the Quickbase schema crawler.
2. Prefer `--samples 0` unless live sample data is explicitly approved.
3. Run `qb_schema_to_markdown.py` against the JSON export.
4. Review changed markdown files before giving the agent access.
5. Record the refresh date in `README.md` or version control.

## Recommended command

```bash
python qb_extract_schema.py APP_ID --samples 0 --output qb_schema_APP_ID.json
python qb_schema_to_markdown.py qb_schema_APP_ID.json --out quickbase-agent-memory
```
""",
        "troubleshoot-formula.md": """# Skill: Troubleshoot Formula

Purpose: diagnose Quickbase formula-field issues.

## Inputs

- Table name or DBID
- Field name or FID
- Description of observed issue
- Example Record ID# when available

## Steps

1. Read the relevant table markdown file in `tables/`.
2. Find the formula in `formulas-index.md` — note its **Depends on** list.
3. Check the **Reverse Dependency Index** at the bottom of `formulas-index.md`
   to see if other formulas depend on the field in question (cascading impact).
4. Look at the **Patterns** flags — `Nz()`, cross-table queries, type coercion,
   and date math are the most common sources of bugs.
5. Pull relevant records with read-only query access.
6. Compare source values to formula output.
7. Check for common Quickbase issues:
   - `Nz()` masking nulls that should surface as errors
   - Text fields checked with unsupported null logic
   - Rounding or currency precision differences
   - Empty values treated as zero
   - Type conversion problems (ToText/ToNumber/ToDate)
   - Relationship summary fields not refreshed as expected
   - Cross-table query formulas (`GetRecords`/`GetFieldValues`) hitting
     performance limits or returning unexpected null on no-match

## Output

Return likely root cause, affected fields, dependency chain, example records,
and a proposed formula fix.
""",
        "inspect-relationship.md": """# Skill: Inspect Relationship

Purpose: diagnose lookup, summary, and parent-child relationship issues.

## Steps

1. Read `relationships.md`.
2. Identify parent table, child table, and reference field.
3. Check whether the reference field is populated on child records.
4. Check whether lookup/summary fields exist and point to the expected parent fields.
5. Query sample child records and parent records by Record ID#.
6. Report missing links, unexpected parent references, and summary mismatches.
""",
        "run-data-quality-check.md": """# Skill: Run Data Quality Check

Purpose: execute repeatable read-only checks against Quickbase data.

## Steps

1. Identify the control file in `controls/`.
2. Confirm the target table and field IDs from `tables/` or `fields-index.md`.
3. Build a read-only `POST /v1/records/query` request.
4. Return counts first.
5. Return record IDs and non-sensitive fields.
6. Avoid exposing live PII unless explicitly approved.

## Output format

```md
## Check Result

- Control:
- Table:
- Query:
- Records checked:
- Exceptions found:

| Record ID# | Issue | Notes |
|---:|---|---|
```
""",
    }


# ─── Main ────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert Quickbase schema JSON into agent markdown files."
    )
    parser.add_argument("schema_json", help="Path to JSON produced by the Quickbase schema crawler")
    parser.add_argument("--out", "-o", default="quickbase-agent-memory", help="Output folder")
    parser.add_argument(
        "--include-samples", action="store_true",
        help="Write sample record shapes to samples/ subfolder (off by default for PII safety).",
    )
    parser.add_argument(
        "--include-pages", action="store_true",
        help="Write custom code pages to pages/ subfolder.",
    )
    args = parser.parse_args()

    schema_path = Path(args.schema_json)
    out_dir = Path(args.out)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    tables = collect_tables(schema)

    file_count = 0

    def emit(path: Path, content: str) -> None:
        nonlocal file_count
        write_md(path, content)
        file_count += 1
        print(f"  📝 {path.relative_to(out_dir)}")

    print(f"📂 Generating agent memory in {out_dir}/\n")

    # ── Top-level docs ──
    emit(out_dir / "README.md", render_readme(schema))
    emit(out_dir / "app-overview.md", render_app_overview(schema, tables))
    emit(out_dir / "fields-index.md", render_fields_index(tables))
    emit(out_dir / "field-usage.md", render_field_usage(tables))
    emit(out_dir / "formulas-index.md", render_formulas_index(tables))
    emit(out_dir / "relationships.md", render_relationships(tables))
    emit(out_dir / "reports-index.md", render_reports_index(tables))
    emit(out_dir / "data-quality-rules.md", render_data_quality_rules(tables))

    # ── Per-table files ──
    for table in tables:
        filename = slugify(table_name(table)) + ".md"
        emit(out_dir / "tables" / filename, render_table_file(table))

    # ── Controls ──
    for filename, content in render_controls().items():
        emit(out_dir / "controls" / filename, content)

    # ── Skills ──
    for filename, content in render_skills().items():
        emit(out_dir / "skills" / filename, content)

    # ── Pages (optional) ──
    if args.include_pages:
        page_files = render_pages(schema)
        if page_files:
            for filename, content in page_files.items():
                emit(out_dir / "pages" / filename, content)
        else:
            print("  ⚠️  No custom pages found in schema JSON")

    # ── Samples (optional) ──
    if args.include_samples:
        sample_files = render_samples(tables)
        if sample_files:
            for filename, content in sample_files.items():
                emit(out_dir / "samples" / filename, content)
        else:
            print("  ⚠️  No sample records found in schema JSON (run crawler with --samples N)")

    print(f"\n✅ Done! {file_count} files in {out_dir}/")
    print(f"   Tables: {len(tables)}")
    print(f"\nNext steps:")
    print(f"   1. Review generated markdown for sensitive content.")
    print(f"   2. Drop pipeline YAML exports into {out_dir}/")
    print(f"   3. Commit to a private repo or point your agent at the folder.")
    print(f"   4. Re-run after schema changes (see skills/schema-refresh.md).")


if __name__ == "__main__":
    main()
