# KKJ diamond analysis dashboard

Internal diamond inventory and sales analytics for KKJ: daily ERP pipelines into
PostgreSQL, a tested metric library (stock turn, sell-through, ageing, ...), data
checks, and a Python web dashboard.

This repo replaces two earlier projects, kept locally in `archive/` for reference
(not in git):

- `archive/inventory_project/`: the ERP → PostgreSQL stock pipeline and Power BI
  report. Has its own git history.
- `archive/Stock_ETL/`: the weighted stock-turn prototype (Modules 1–10). Its
  logic is being rebuilt here; several bugs in it are known, so don't reuse its
  outputs.

## Layout

| Path | What |
|---|---|
| `src/kkj/settings.py` | `.env`, paths, database engines |
| `src/kkj/erp/` | ERP login/session (`client.py`) and one module per report (`stock_report.py`) |
| `src/kkj/db/` | database setup and loaders |
| `src/kkj/pipelines/` | runnable pipelines: fetch → clean → validate → load |
| `src/kkj/metrics/`, `checks/`, `dashboard/` | metric library, data checks, web dashboard (to come) |
| `config/stock_payload.json` | stock report filters, captured from the ERP screen |
| `sql/schema.sql` | schema `erp` in database `kkj_erp_stock` (tables, views, grants) |
| `scripts/probes/` | one-off investigation scripts from planning (ERP sales chain, data-quality checks) |
| `tests/` | pytest |
| `data/`, `logs/` | ERP downloads and run logs (not in git) |

## Setup (once)

```
python -m venv --system-site-packages .venv
.venv\Scripts\python -m pip install -e ".[dev]"
copy .env.example .env        (then fill it in)
.venv\Scripts\python -m kkj.db.setup
```

## Stock pipeline

```
.venv\Scripts\python -m kkj.pipelines.stock                     # today, all branches, diamond groups
.venv\Scripts\python -m kkj.pipelines.stock --date 27/10/2026
.venv\Scripts\python -m kkj.pipelines.stock --from-file data\stock\raw\<file>.xlsx
.venv\Scripts\python -m kkj.pipelines.stock --from-file <file> --check-only   # no DB, no ERP
.venv\Scripts\python -m kkj.pipelines.stock --dry-run
```

Nothing is loaded unless every check passes:

- the row count must match `max(sr_no)`, the totals row and the ERP's own record count;
- the sums must match the totals row;
- no barcode may be empty;
- no value may fail to parse.

After loading, the stored rows are read back and compared again. Every run is
recorded in `erp.etl_run_log`. Re-running a day replaces that day's rows.

## Tests

```
.venv\Scripts\python -m pytest
```

`tests/test_stock_report.py` cleans every real export in `data/stock/raw/` and
compares the result, cell for cell, with the original pipeline's output.
