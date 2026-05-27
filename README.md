# Quickbase App Schema Extraction Pipeline

Two-step pipeline that crawls a Quickbase app via REST API and generates
an agent-ready markdown knowledge base for Claude Code or any LLM agent.

## Prerequisites

```bash
pip install requests
```

You need a Quickbase **user token** with admin-level access to the target app.
Admin access ensures formula text is included in field responses — lower
permission levels may silently omit `properties.formula`.

Create or manage tokens at: **My Preferences → Manage User Tokens** in Quickbase.

## Quick Start

```bash
# 1. Set credentials (one-time per session)
export QB_REALM=yourcompany.quickbase.com
export QB_TOKEN=your_user_token

# 2. Crawl the app schema (safe — read-only, no sample data)
python qb_extract_schema.py APP_ID --samples 0

# 3. Generate agent memory
python qb_schema_to_markdown.py qb_schema_APP_ID.json --out quickbase-agent-memory/

# 4. Drop pipeline YAML exports into the same folder
cp ~/Downloads/*.yaml quickbase-agent-memory/

# 5. Point your agent at the folder
```

## Step 1: Extract Schema (`qb_extract_schema.py`)

Crawls the Quickbase REST API and writes a single JSON file with all
app metadata, tables, fields, field usage, relationships, reports, and
optionally sample records.

### Usage

```bash
python qb_extract_schema.py APP_ID [options]
```

### Options

| Flag | Default | Description |
|---|---|---|
| `APP_ID` | *(required)* | Quickbase app ID (e.g., `bqr4tnc5x`) |
| `--samples N` | `5` | Sample records per table. **Use `0` for sensitive apps.** |
| `--redact` | off | Mask SSNs, emails, phones, case numbers in samples |
| `--no-pages` | off | Skip the custom pages endpoint |
| `--no-webhooks` | off | Skip the webhooks endpoint |
| `--output FILE` | `qb_schema_APP_ID.json` | Output file path |

### Examples

```bash
# Safest — no sample data, skip optional endpoints
python qb_extract_schema.py bqr4tnc5x --samples 0 --no-pages --no-webhooks

# With redacted samples
python qb_extract_schema.py bqr4tnc5x --samples 5 --redact

# Full extraction with custom output path
python qb_extract_schema.py bqr4tnc5x --samples 10 --output ./exports/my_app.json
```

### API Endpoints Used

All calls are **read-only** except `POST /v1/records/query` (which reads records, not writes).

| Endpoint | Purpose |
|---|---|
| `GET /v1/apps/{appId}` | App metadata |
| `GET /v1/apps/{appId}/tables` | Auto-discover all tables |
| `GET /v1/tables/{tableId}?appId=...` | Full table detail |
| `GET /v1/fields?tableId=...` | Field definitions, formulas, permissions |
| `GET /v1/fields/usage?tableId=...` | Where fields are used (reports, forms, etc.) |
| `GET /v1/tables/{tableId}/relationships` | Parent-child, lookups, summaries |
| `GET /v1/reports?tableId=...` | Saved reports |
| `GET /v1/webhooks?tableId=...` | Webhooks (optional) |
| `GET /v1/pages?appId=...` | Custom code pages (optional) |
| `POST /v1/records/query` | Sample records (optional) |

Rate limits (HTTP 429) are handled automatically with retry + backoff.

## Step 2: Generate Agent Memory (`qb_schema_to_markdown.py`)

Transforms the JSON export into structured markdown files organized for
agent consumption.

### Usage

```bash
python qb_schema_to_markdown.py SCHEMA_JSON [options]
```

### Options

| Flag | Default | Description |
|---|---|---|
| `SCHEMA_JSON` | *(required)* | Path to JSON from Step 1 |
| `--out DIR` | `quickbase-agent-memory/` | Output directory |
| `--include-samples` | off | Write sample record shapes to `samples/` |
| `--include-pages` | off | Write custom code pages to `pages/` |

### Examples

```bash
# Minimal (safest)
python qb_schema_to_markdown.py qb_schema_bqr4tnc5x.json

# With code pages and sample shapes
python qb_schema_to_markdown.py qb_schema_bqr4tnc5x.json \
  --include-pages --include-samples \
  --out ./my-app-agent-memory/
```

### Generated Structure

```
quickbase-agent-memory/
├── README.md                          Agent usage guide + safety notes
├── app-overview.md                    App metadata, variables, table index
├── fields-index.md                    All fields across all tables (flat)
├── field-usage.md                     Where fields are referenced (getFieldsUsage)
├── formulas-index.md                  Formula source, dependency graph, reverse index
├── relationships.md                   Parent-child, lookups, summaries
├── reports-index.md                   Saved report definitions
├── data-quality-rules.md              Auto-derived quality check starting points
├── tables/
│   ├── accounts.md                    Per-table: fields, formulas, relationships
│   ├── payments.md
│   └── ...
├── controls/
│   ├── required-fields.md             Runbook: find blank required fields
│   ├── orphan-records.md              Runbook: find orphan child records
│   └── formula-mismatch-checks.md     Runbook: reconcile formula outputs
├── skills/
│   ├── schema-refresh.md              How to re-crawl and regenerate
│   ├── troubleshoot-formula.md        Diagnose formula issues
│   ├── inspect-relationship.md        Diagnose lookup/summary issues
│   └── run-data-quality-check.md      Execute quality checks
├── pages/                             (optional) Custom code pages
│   └── my-custom-page.md
└── samples/                           (optional) Sample record shapes
    └── accounts.md
```

## Adding Pipeline YAMLs

Quickbase pipelines can be exported as YAML files from the Pipelines UI.
Drop them into the agent memory folder alongside the markdown:

```bash
cp ~/Downloads/pipeline-*.yaml quickbase-agent-memory/
```

These capture trigger → condition → action logic that isn't available
through the REST API.

## Refreshing After Schema Changes

```bash
# Re-crawl
python qb_extract_schema.py APP_ID --samples 0

# Regenerate (overwrites existing markdown)
python qb_schema_to_markdown.py qb_schema_APP_ID.json --out quickbase-agent-memory/
```

See `skills/schema-refresh.md` in the generated output for the full procedure.

## Security Notes

- **Use `--samples 0`** for apps with debtor, customer, or financial data
  unless you specifically need sample shapes for the agent.
- **Use `--redact`** if you do include samples — it masks SSNs, emails,
  phone numbers, and bankruptcy case numbers.
- **Use an admin token** to ensure formulas are captured, but **do not
  commit the token** to version control.
- **Review generated markdown** before sharing with agents or committing
  to a repository. The `data-quality-rules.md` and `formulas-index.md`
  files are schema-derived and safe, but sample data files may contain
  sensitive values.
