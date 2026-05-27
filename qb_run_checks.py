#!/usr/bin/env python3
"""
Quickbase Data Quality Check Runner
Executes all read-only data-quality checks defined as YAML specs in checks/.

Usage:
    export QB_REALM=yourcompany.quickbase.com
    export QB_TOKEN=...
    python qb_run_checks.py [--checks-dir checks] [--evidence-dir quickbase-agent-memory/evidence] [--ids id1,id2]

Each check is a YAML file. See `checks/_shared/spec.md` for the format.

Exit codes:
    0  all checks passed
    1  one or more checks failed
    2  configuration error (missing env, invalid YAML, etc.)
"""

import argparse
import importlib.util
import os
import sys
import time
from datetime import date
from pathlib import Path

import requests
import yaml

BASE = "https://api.quickbase.com/v1/"
TABLES_FILE = "tables.yaml"
REQUIRED_FIELDS = {"id", "title"}
# Declarative checks (no executor) additionally require: table, filter
DECLARATIVE_REQUIRED = {"table", "filter"}
# Scripted checks (executor: python) additionally require: script
SCRIPTED_REQUIRED = {"script"}


# ─── HTTP helper ─────────────────────────────────────────────────────

def make_session(token: str, realm: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "QB-Realm-Hostname": realm,
        "Authorization": f"QB-USER-TOKEN {token}",
        "Content-Type": "application/json",
        "User-Agent": "QB-Check-Runner/1.0",
    })
    return s


def post(session: requests.Session, endpoint: str, body: dict) -> dict:
    """POST with retry/backoff for 429."""
    url = BASE + endpoint
    for _ in range(4):
        r = session.post(url, json=body)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 5)))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


# ─── Spec loading ────────────────────────────────────────────────────

def discover_checks(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.yaml")
                  if not p.parts[len(root.parts):][0].startswith("_"))


def load_table_map(path: Path) -> dict:
    """Load table name → DBID mapping from tables.yaml."""
    if not path.is_file():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def resolve_table(spec: dict, table_map: dict, path: Path) -> None:
    """Resolve a table alias to its DBID, if a mapping exists."""
    table = spec.get("table")
    if table and table in table_map:
        spec["table"] = table_map[table]


def load_spec(path: Path, table_map: dict) -> dict:
    with open(path) as f:
        spec = yaml.safe_load(f) or {}
    missing = REQUIRED_FIELDS - set(spec)
    if missing:
        raise ValueError(f"{path}: missing required fields {sorted(missing)}")
    executor = spec.get("executor", "query")
    # Resolve table aliases in params (for scripted checks)
    for key, val in spec.get("params", {}).items():
        if isinstance(val, str) and val in table_map:
            spec["params"][key] = table_map[val]
    if executor == "query":
        resolve_table(spec, table_map, path)
        extra_missing = DECLARATIVE_REQUIRED - set(spec)
    elif executor == "python":
        extra_missing = SCRIPTED_REQUIRED - set(spec)
    else:
        raise ValueError(f"{path}: unknown executor '{executor}' "
                         f"(supported: query, python)")
    if extra_missing:
        raise ValueError(
            f"{path}: executor '{executor}' requires {sorted(extra_missing)}"
        )
    spec.setdefault("select", [3])
    spec.setdefault("expect", {"max_count": 0})
    spec.setdefault("severity", "medium")
    spec.setdefault("limit", 200)
    spec.setdefault("description", "")
    spec.setdefault("tags", [])
    spec.setdefault("params", {})
    spec["executor"] = executor
    spec["__path__"] = str(path)
    return spec


def load_script_module(script_path: str):
    p = Path(script_path)
    if not p.is_file():
        raise FileNotFoundError(f"script not found: {script_path}")
    mod_spec = importlib.util.spec_from_file_location(p.stem, p)
    if mod_spec is None or mod_spec.loader is None:
        raise ImportError(f"cannot load script {script_path}")
    module = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(module)
    if not hasattr(module, "run"):
        raise AttributeError(f"{script_path}: missing run(session, spec) function")
    return module


# ─── Execution ───────────────────────────────────────────────────────

def run_check(session: requests.Session, spec: dict) -> dict:
    body = {
        "from": spec["table"],
        "select": spec["select"],
        "where": spec["filter"],
        "options": {"top": spec["limit"]},
    }
    resp = post(session, "records/query", body)
    data = resp.get("data") or []
    fields = {f["id"]: f.get("label", f"FID {f['id']}")
              for f in (resp.get("fields") or [])}
    metadata = resp.get("metadata") or {}
    total = metadata.get("totalRecords", len(data))
    max_count = (spec.get("expect") or {}).get("max_count", 0)
    passed = total <= max_count
    return {
        "spec": spec,
        "passed": passed,
        "total": total,
        "returned": len(data),
        "data": data,
        "fields": fields,
    }


def run_scripted_check(session: requests.Session, spec: dict) -> dict:
    module = load_script_module(spec["script"])
    result = module.run(session, spec) or {}
    for key in ("passed", "total", "body_md"):
        if key not in result:
            raise ValueError(
                f"{spec['script']}: run() result missing required key '{key}'"
            )
    result.setdefault("returned", result["total"])
    result.setdefault("data", [])
    result.setdefault("fields", {})
    result["spec"] = spec
    return result


# ─── Evidence rendering ──────────────────────────────────────────────

def fmt_cell(v) -> str:
    if v is None or v == "":
        return "_(blank)_"
    if isinstance(v, dict) and "value" in v:
        return fmt_cell(v["value"])
    if isinstance(v, str):
        return v.replace("|", "\\|").replace("\n", " ")
    return str(v)


def render_evidence(result: dict, run_at: str) -> str:
    spec = result["spec"]
    out: list[str] = []
    out.append(f"# {spec['title']}\n")
    out.append(f"- **Check ID:** `{spec['id']}`")
    out.append(f"- **Run at:** {run_at}")
    out.append(f"- **Status:** {'PASS' if result['passed'] else 'FAIL'}")
    out.append(f"- **Severity:** {spec.get('severity')}")
    out.append(f"- **Records flagged:** {result['total']}")
    out.append(f"- **Expected at most:** {spec['expect'].get('max_count', 0)}")
    if spec.get("executor", "query") == "python":
        out.append(f"- **Executor:** python (`{spec['script']}`)")
    else:
        out.append(f"- **Table:** `{spec['table']}`")
        out.append(f"- **Filter:** `{spec['filter']}`")
    if spec.get("tags"):
        out.append(f"- **Tags:** {', '.join(spec['tags'])}")
    out.append(f"- **Spec:** `{spec['__path__']}`")
    out.append("")
    if spec.get("description"):
        out.append(spec["description"].rstrip() + "\n")

    if result.get("body_md"):
        out.append(result["body_md"])
    elif result["data"]:
        select = spec["select"]
        headers = [result["fields"].get(fid, f"FID {fid}") for fid in select]
        out.append("## Sample exceptions\n")
        if result["returned"] < result["total"]:
            out.append(
                f"_Showing first {result['returned']} of {result['total']}. "
                f"Increase `limit:` in the spec to see more._\n"
            )
        out.append("| " + " | ".join(headers) + " |")
        out.append("|" + "|".join("---" for _ in headers) + "|")
        for rec in result["data"]:
            row = [fmt_cell(rec.get(str(fid), rec.get(fid))) for fid in select]
            out.append("| " + " | ".join(row) + " |")
    else:
        out.append("_No records flagged._")
    return "\n".join(out) + "\n"


def render_summary(results: list[dict], run_at: str) -> str:
    out: list[str] = []
    out.append(f"# Data Quality Check Summary — {run_at}\n")
    n_pass = sum(1 for r in results if r["passed"])
    n_fail = len(results) - n_pass
    out.append(f"- **Checks run:** {len(results)}")
    out.append(f"- **Passed:** {n_pass}")
    out.append(f"- **Failed:** {n_fail}\n")
    out.append("| Status | Severity | ID | Title | Flagged |")
    out.append("|---|---|---|---|---:|")
    severity_order = {"high": 0, "medium": 1, "low": 2}
    sorted_results = sorted(
        results,
        key=lambda r: (
            r["passed"],
            severity_order.get(r["spec"].get("severity"), 99),
            r["spec"]["id"],
        ),
    )
    for r in sorted_results:
        s = r["spec"]
        status = "PASS" if r["passed"] else "**FAIL**"
        out.append(
            f"| {status} | {s.get('severity')} | `{s['id']}` | "
            f"{s['title']} | {r['total']} |"
        )
    return "\n".join(out) + "\n"


# ─── Main ────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="Run Quickbase data-quality checks.")
    p.add_argument("--checks-dir", default="checks",
                   help="Directory containing YAML check specs")
    p.add_argument("--evidence-dir",
                   default="quickbase-agent-memory/evidence",
                   help="Directory to write evidence markdown into")
    p.add_argument("--ids",
                   help="Comma-separated check IDs to run (default: all)")
    p.add_argument("--dry-run", action="store_true",
                   help="Validate specs without calling the QB API")
    args = p.parse_args()

    realm = os.environ.get("QB_REALM")
    token = os.environ.get("QB_TOKEN")
    if not args.dry_run and (not realm or not token):
        print("ERROR: set QB_REALM and QB_TOKEN", file=sys.stderr)
        return 2

    checks_dir = Path(args.checks_dir)
    if not checks_dir.is_dir():
        print(f"ERROR: checks directory not found: {checks_dir}", file=sys.stderr)
        return 2

    table_map = load_table_map(Path(TABLES_FILE))

    paths = discover_checks(checks_dir)
    specs: list[dict] = []
    for path in paths:
        try:
            specs.append(load_spec(path, table_map))
        except Exception as e:
            print(f"ERROR loading {path}: {e}", file=sys.stderr)
            return 2

    if args.ids:
        wanted = {s.strip() for s in args.ids.split(",")}
        specs = [s for s in specs if s["id"] in wanted]
        missing = wanted - {s["id"] for s in specs}
        if missing:
            print(f"ERROR: unknown check ids: {sorted(missing)}", file=sys.stderr)
            return 2

    if args.dry_run:
        for s in specs:
            if s.get("executor") == "python":
                try:
                    load_script_module(s["script"])
                except Exception as e:
                    print(f"FAIL {s['id']}  {s['__path__']}  ({e})", file=sys.stderr)
                    return 2
            print(f"OK  {s['id']}  {s['__path__']}")
        return 0

    session = make_session(token, realm)
    today = date.today().isoformat()
    out_dir = Path(args.evidence_dir) / today
    out_dir.mkdir(parents=True, exist_ok=True)

    run_at = time.strftime("%Y-%m-%dT%H:%M:%S%z") or today
    results: list[dict] = []
    for spec in specs:
        try:
            if spec.get("executor") == "python":
                result = run_scripted_check(session, spec)
            else:
                result = run_check(session, spec)
        except requests.HTTPError as e:
            print(f"FAIL {spec['id']} — HTTP {e.response.status_code}",
                  file=sys.stderr)
            results.append({
                "spec": spec, "passed": False, "total": -1,
                "returned": 0, "data": [], "fields": {},
                "body_md": f"_HTTP error: {e}_",
                "error": str(e),
            })
            continue
        except Exception as e:
            print(f"FAIL {spec['id']} — {type(e).__name__}: {e}",
                  file=sys.stderr)
            results.append({
                "spec": spec, "passed": False, "total": -1,
                "returned": 0, "data": [], "fields": {},
                "body_md": f"_Error: {e}_",
                "error": str(e),
            })
            continue
        results.append(result)
        marker = "PASS" if result["passed"] else "FAIL"
        print(f"{marker} {spec['id']}  flagged={result['total']}  "
              f"severity={spec.get('severity')}")

        evidence = render_evidence(result, run_at)
        (out_dir / f"{spec['id']}.md").write_text(evidence)

    summary = render_summary(results, run_at)
    (out_dir / "SUMMARY.md").write_text(summary)
    print(f"\nEvidence written to {out_dir}")

    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
