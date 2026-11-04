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

### Stock as on a past date

```
.venv\Scripts\python -m kkj.pipelines.stock --as-on --date 30/06/2026 [--no-load]
```

- Sends the ERP's "as on date" settings: `isLabelAsOnDate=1`,
  `calStkAsOnLabelDate=true`, `flagForItem=false`.
- Loads into `stock_hist` as `erp_asof`, never into `erp`, and only after the
  snapshot checks in `kkj/checks/snapshot.py` pass.
- Verified on 30/06/2026: the labels, pieces, weights, locations and label dates
  were 100% identical to the verified legacy snapshot.
- **Value: use `taxable_amount`.** In this mode `item_amt` leaves out the metal
  for many labels; `taxable_amount` holds the full pre-GST value (30/06/2026:
  ₹239.26 Cr vs ₹238.63 Cr in the legacy file, 0.3% apart; DTP15907 =
  ₹3,39,407.52 as on the ERP screen).

**Value basis everywhere: `taxable_amount` (before GST).** Sales revenue and
stock value (current and as-on) both use it, so stock turn compares like with
like. Legacy files only have `item_amt`, which is within about 0.4% of it.

## Sales pipeline

```
.venv\Scripts\python -m kkj.pipelines.sales --from 01/10/2026 --to 31/10/2026
.venv\Scripts\python -m kkj.pipelines.sales --from 01/04/2026 --to 04/11/2026 --by-month --pause 20
.venv\Scripts\python -m kkj.pipelines.sales --from 30/06/2026 --no-load            # fetch + check only
.venv\Scripts\python -m kkj.pipelines.sales --from-file data\sales\raw\<file>.json.gz
```

- **Fetch:**
  - Calls step A (`getRptKeyAndServerTotalForSalesRpt`), then the JSON details
    pages, and saves the whole response to `data/sales/raw/` before cleaning.
  - The item codes for each product group are looked up live on every run.
- **Checks** (nothing loads unless all pass): row count and sums of pcs,
  item_amt, net_wt and dia_wt must match the ERP's `serverTotal`.
- **Load:** replaces the date range in one transaction, then reads it back and
  checks again.
- **Revenue** is `taxable_amount` (the billed amount before GST). `item_amt` is
  the label value.
- **Returns:**
  - `considerSalesReturn` defaults to `2`, the only value that includes them
    (`0` and `1` leave them out).
  - They come as bill type `SR`, with negative pcs, weights and item_amt.
  - They have no barcode, label number or taxable amount. `item_inv_own_code`
    links them to the item.
  - A return can be dated outside the range it was pulled with, so loads also
    replace rows with the same bill number and inventory id.
- `salesType`: `1` and `2` returned the same rows and `3` returned none, so it
  stays `1`.

## Database schemas (`kkj_erp_stock`)

| Schema | Holds | Notes |
|---|---|---|
| `erp` | `stock_snapshot`: same-day ERP stock, one row per label per day; `etl_run_log` | Production. Only the stock pipeline writes here. |
| `sales` | `sales_line`, `load_log`, view `v_sales_line` | The view uses ERP rows where loaded and legacy CSV rows only before that. |
| `stock_hist` | `snapshot`, `snapshot_status` | Stock on past dates. Every file considered is listed in `snapshot_status`, loaded or quarantined, with its check numbers. |

`python -m kkj.db.setup sales.sql stock_hist.sql` (re)creates only those schemas. Both
can be dropped and reloaded without touching `erp`.

## Legacy import (one-off, no ERP calls)

```
.venv\Scripts\python -m kkj.pipelines.legacy_import sales [--check-only]
.venv\Scripts\python -m kkj.pipelines.legacy_import stock [--check-only]   # after sales
```

- **Sales** (`ALL_Data\SALES2025JUNE_2026JUNEUPDATED.csv`, diamond groups only):
  - Both date formats in the file are read.
  - For May–Dec 2025 the file's barcode column holds the bill number. The
    barcode is rebuilt as item prefix + label number (`barcode_rebuilt` = true),
    and the rule must reproduce at least 98% of the real barcodes before
    anything loads.
- **Stock** (`Stock_all_in_csv\*_Stock.csv`): each file is quarantined if any of
  these is true:
  - labels in it were created after its date;
  - items sold in the 30 days before its date are still in it;
  - it is an identical copy of another file and the checks can't tell which date
    is real.

  `age_days` is recalculated as snapshot date − label date. The file's own value,
  which is the age on the July 2026 export day, is kept in `age_days_reported`.

## Tests

```
.venv\Scripts\python -m pytest
```

`tests/test_stock_report.py` cleans every real export in `data/stock/raw/` and
compares the result, cell for cell, with the original pipeline's output.
