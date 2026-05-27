#!/usr/bin/env python3
"""
Quickbase Schema Diff
Compare two schema JSON snapshots produced by qb_extract_schema.py and emit
a markdown changelog of what was added, removed, or changed.

Usage:
    python qb_schema_diff.py OLD.json NEW.json [-o changelog.md]

Read-only. Does not call the Quickbase API.
"""

import argparse
import json
import sys
from typing import Any


# ─── Loading and indexing ────────────────────────────────────────────

def load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def index_by_id(items: list, key: str = "id") -> dict:
    return {str(item[key]): item for item in items if key in item}


def index_tables(snapshot: dict) -> dict:
    """Map table_id -> the per-table block from the snapshot."""
    return {t["table_meta"]["id"]: t for t in snapshot.get("tables", [])}


# ─── Field-level diff ────────────────────────────────────────────────

# Top-level field attributes worth tracking
FIELD_ATTRS = ["label", "fieldType", "mode", "required", "unique", "audited"]
# Nested properties.* attributes worth tracking
FIELD_PROPS = ["formula", "choices", "defaultValue", "foreignKey", "primaryKey"]


def field_signature(f: dict) -> dict:
    sig = {a: f.get(a) for a in FIELD_ATTRS}
    props = f.get("properties") or {}
    for p in FIELD_PROPS:
        sig[f"properties.{p}"] = props.get(p)
    return sig


def diff_field(old: dict, new: dict) -> list[tuple[str, Any, Any]]:
    """Return [(attr, old_value, new_value), ...] for any attr that changed."""
    osig = field_signature(old)
    nsig = field_signature(new)
    return [(k, osig[k], nsig[k]) for k in osig if osig[k] != nsig[k]]


def diff_fields(old_fields: list, new_fields: list) -> dict:
    old_idx = index_by_id(old_fields)
    new_idx = index_by_id(new_fields)
    added = [new_idx[fid] for fid in new_idx if fid not in old_idx]
    removed = [old_idx[fid] for fid in old_idx if fid not in new_idx]
    changed = []
    for fid in sorted(set(old_idx) & set(new_idx), key=int):
        deltas = diff_field(old_idx[fid], new_idx[fid])
        if deltas:
            changed.append((new_idx[fid], deltas))
    return {"added": added, "removed": removed, "changed": changed}


# ─── Relationship-level diff ─────────────────────────────────────────

def rel_summary(r: dict) -> dict:
    """Compact, comparable view of a relationship."""
    return {
        "parentTableId": r.get("parentTableId"),
        "childTableId": r.get("childTableId"),
        "foreignKey": (r.get("foreignKeyField") or {}).get("label"),
        "lookupFields": sorted(
            (lf.get("label") or "") for lf in (r.get("lookupFields") or [])
        ),
        "summaryFields": sorted(
            (sf.get("label") or "") for sf in (r.get("summaryFields") or [])
        ),
    }


def diff_relationships(old_rels: list, new_rels: list) -> dict:
    old_idx = index_by_id(old_rels)
    new_idx = index_by_id(new_rels)
    added = [new_idx[rid] for rid in new_idx if rid not in old_idx]
    removed = [old_idx[rid] for rid in old_idx if rid not in new_idx]
    changed = []
    for rid in sorted(set(old_idx) & set(new_idx)):
        os_, ns_ = rel_summary(old_idx[rid]), rel_summary(new_idx[rid])
        if os_ != ns_:
            changed.append((new_idx[rid], os_, ns_))
    return {"added": added, "removed": removed, "changed": changed}


# ─── Report-level diff ───────────────────────────────────────────────

REPORT_ATTRS = ["name", "type", "description"]


def report_signature(r: dict) -> dict:
    sig = {a: r.get(a) for a in REPORT_ATTRS}
    sig["query"] = r.get("query")
    sig["properties"] = r.get("properties")
    return sig


def diff_reports(old_reports: list, new_reports: list) -> dict:
    old_idx = index_by_id(old_reports)
    new_idx = index_by_id(new_reports)
    added = [new_idx[rid] for rid in new_idx if rid not in old_idx]
    removed = [old_idx[rid] for rid in old_idx if rid not in new_idx]
    changed = []
    for rid in sorted(set(old_idx) & set(new_idx)):
        osig = report_signature(old_idx[rid])
        nsig = report_signature(new_idx[rid])
        deltas = [(k, osig[k], nsig[k]) for k in osig if osig[k] != nsig[k]]
        if deltas:
            changed.append((new_idx[rid], deltas))
    return {"added": added, "removed": removed, "changed": changed}


# ─── Top-level diff ──────────────────────────────────────────────────

def diff_app(old_app: dict, new_app: dict) -> list[tuple[str, Any, Any]]:
    attrs = ["name", "description", "dateFormat", "timeZone", "updated"]
    return [(a, old_app.get(a), new_app.get(a))
            for a in attrs if old_app.get(a) != new_app.get(a)]


def diff_snapshots(old: dict, new: dict) -> dict:
    old_tables = index_tables(old)
    new_tables = index_tables(new)

    added_tables = [new_tables[tid]["table_meta"]
                    for tid in new_tables if tid not in old_tables]
    removed_tables = [old_tables[tid]["table_meta"]
                      for tid in old_tables if tid not in new_tables]

    changed_tables = []
    for tid in sorted(set(old_tables) & set(new_tables)):
        ot, nt = old_tables[tid], new_tables[tid]
        f_diff = diff_fields(ot.get("fields") or [], nt.get("fields") or [])
        r_diff = diff_relationships(ot.get("relationships") or [],
                                    nt.get("relationships") or [])
        rep_diff = diff_reports(ot.get("reports") or [],
                                nt.get("reports") or [])
        if any(f_diff[k] for k in f_diff) or \
           any(r_diff[k] for k in r_diff) or \
           any(rep_diff[k] for k in rep_diff):
            changed_tables.append({
                "table_meta": nt["table_meta"],
                "fields": f_diff,
                "relationships": r_diff,
                "reports": rep_diff,
            })

    return {
        "app": diff_app(old.get("app") or {}, new.get("app") or {}),
        "tables_added": added_tables,
        "tables_removed": removed_tables,
        "tables_changed": changed_tables,
    }


# ─── Markdown rendering ──────────────────────────────────────────────

def fmt_value(v: Any) -> str:
    if v is None:
        return "_(none)_"
    if isinstance(v, str):
        if "\n" in v:
            return "\n```\n" + v + "\n```"
        return f"`{v}`"
    return f"`{json.dumps(v, default=str)}`"


def render(diff: dict, old_meta: dict, new_meta: dict) -> str:
    out: list[str] = []
    out.append("# Schema Diff\n")
    out.append(f"- **Old snapshot extracted at:** {old_meta.get('extracted_at', '?')}")
    out.append(f"- **New snapshot extracted at:** {new_meta.get('extracted_at', '?')}")
    out.append(f"- **App ID:** `{new_meta.get('app_id', old_meta.get('app_id', '?'))}`")
    out.append("")

    # App
    if diff["app"]:
        out.append("## App metadata changes\n")
        for attr, ov, nv in diff["app"]:
            out.append(f"- **{attr}:** {fmt_value(ov)} → {fmt_value(nv)}")
        out.append("")

    # Tables added/removed
    if diff["tables_added"]:
        out.append("## Tables added\n")
        for t in diff["tables_added"]:
            out.append(f"- **{t.get('name', '?')}** (`{t.get('id')}`)")
        out.append("")
    if diff["tables_removed"]:
        out.append("## Tables removed\n")
        for t in diff["tables_removed"]:
            out.append(f"- **{t.get('name', '?')}** (`{t.get('id')}`)")
        out.append("")

    # Tables changed
    if diff["tables_changed"]:
        out.append("## Tables changed\n")
        for tc in diff["tables_changed"]:
            tm = tc["table_meta"]
            out.append(f"### {tm.get('name', '?')} (`{tm.get('id')}`)\n")

            f = tc["fields"]
            if f["added"]:
                out.append(f"**Fields added ({len(f['added'])}):**")
                for fld in f["added"]:
                    out.append(
                        f"- `{fld.get('id')}` {fld.get('label')} "
                        f"({fld.get('fieldType')}{', formula' if fld.get('mode') == 'lookup' or (fld.get('properties') or {}).get('formula') else ''})"
                    )
                out.append("")
            if f["removed"]:
                out.append(f"**Fields removed ({len(f['removed'])}):**")
                for fld in f["removed"]:
                    out.append(f"- `{fld.get('id')}` {fld.get('label')} ({fld.get('fieldType')})")
                out.append("")
            if f["changed"]:
                out.append(f"**Fields changed ({len(f['changed'])}):**")
                for fld, deltas in f["changed"]:
                    out.append(f"- `{fld.get('id')}` {fld.get('label')}")
                    for attr, ov, nv in deltas:
                        out.append(f"  - {attr}: {fmt_value(ov)} → {fmt_value(nv)}")
                out.append("")

            r = tc["relationships"]
            if r["added"] or r["removed"] or r["changed"]:
                out.append("**Relationships:**")
                for rel in r["added"]:
                    out.append(f"- added: parent `{rel.get('parentTableId')}` → child `{rel.get('childTableId')}`")
                for rel in r["removed"]:
                    out.append(f"- removed: parent `{rel.get('parentTableId')}` → child `{rel.get('childTableId')}`")
                for rel, os_, ns_ in r["changed"]:
                    out.append(f"- changed: relationship id `{rel.get('id')}`")
                    for k in os_:
                        if os_[k] != ns_[k]:
                            out.append(f"  - {k}: {fmt_value(os_[k])} → {fmt_value(ns_[k])}")
                out.append("")

            rp = tc["reports"]
            if rp["added"]:
                out.append(f"**Reports added ({len(rp['added'])}):**")
                for r_ in rp["added"]:
                    out.append(f"- `{r_.get('id')}` {r_.get('name')} ({r_.get('type')})")
                out.append("")
            if rp["removed"]:
                out.append(f"**Reports removed ({len(rp['removed'])}):**")
                for r_ in rp["removed"]:
                    out.append(f"- `{r_.get('id')}` {r_.get('name')}")
                out.append("")
            if rp["changed"]:
                out.append(f"**Reports changed ({len(rp['changed'])}):**")
                for r_, deltas in rp["changed"]:
                    out.append(f"- `{r_.get('id')}` {r_.get('name')}")
                    for attr, ov, nv in deltas:
                        out.append(f"  - {attr}: {fmt_value(ov)} → {fmt_value(nv)}")
                out.append("")

    if not (diff["app"] or diff["tables_added"]
            or diff["tables_removed"] or diff["tables_changed"]):
        out.append("_No differences detected._\n")

    return "\n".join(out)


# ─── Main ────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Diff two Quickbase schema JSON snapshots.")
    p.add_argument("old", help="Path to older schema JSON")
    p.add_argument("new", help="Path to newer schema JSON")
    p.add_argument("-o", "--output", help="Markdown output path (default: stdout)")
    p.add_argument("--json", action="store_true",
                   help="Emit raw diff as JSON instead of markdown")
    args = p.parse_args()

    old = load(args.old)
    new = load(args.new)
    diff = diff_snapshots(old, new)

    if args.json:
        body = json.dumps(diff, indent=2, default=str)
    else:
        body = render(diff, old.get("_meta") or {}, new.get("_meta") or {})

    if args.output:
        with open(args.output, "w") as f:
            f.write(body)
        # Brief stderr summary so cron/CI can see something happened
        n_changed = len(diff["tables_changed"])
        n_added = len(diff["tables_added"])
        n_removed = len(diff["tables_removed"])
        print(
            f"Wrote {args.output} — {n_added} table(s) added, "
            f"{n_removed} removed, {n_changed} changed.",
            file=sys.stderr,
        )
    else:
        print(body)


if __name__ == "__main__":
    main()
