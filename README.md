# Stockwise

**Inventory and sales intelligence for KKJ.**


Stockwise turns KKJ's daily ERP stock and sales data into recommendations:
stock transfers, replenishment, markdowns, clearance, purchasing and supplier
reviews, each with its supporting data, approved or dismissed in the app. It is
made of daily ERP pipelines into PostgreSQL, a tested metric library (stock
turn, sell-through, ageing, ...), data checks, and a Python web app. Diamond
products come first; the design extends to gold and silver.

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
  - They come as bill type `SR`, with negative pcs, weights and item_amt, and no
    barcode, label number or taxable amount.
  - A return carries the same `inventory_ref_code` as the sale it reverses.
    That is how `orig_bill_no` / `orig_sale_date` are filled; `item_inv_own_code`
    does not link them.
- **Report scope = the original sale date.** A return appears in the pull
  covering its sale, dated the day it came back, which can be after the range. A
  load replaces the rows whose `orig_sale_date` falls in its range. The line key
  is (`bill_no`, `inventory_ref_code`), with a unique index.
- **Verified:** April–June 2026 match the legacy CSV on all 1,637 sale lines
  (date, pieces; values to within whole-rupee rounding, ₹4 in total).
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

## Web app (Stockwise)

```
.venv\Scripts\python -m pip install -e ".[web]"                       # once
.venv\Scripts\uvicorn kkj.web.app:app --host 0.0.0.0 --port 8600      # http://localhost:8600
```

**Stack:** FastAPI routes (`src/kkj/web/*.py`), Jinja2 templates
(`src/kkj/web/templates`, plain HTML), HTMX (`hx-*` attributes update part of a
page) and Tabler (a dashboard UI kit from a CDN). Charts are Plotly figures
built in Python (`kkj/web/charts.py`). There is no JavaScript build step. The app
connects as `dashboard_app`, which can write only schemas `brain` and `config`
(plus sessions and the sign-in log in `auth`).

### Sign-in

Every page needs a signed-in account. There are no roles: everyone signed in
sees and can do everything, including managing users. The account's display
name is recorded with every approval, note and settings change.

**Settings → Users** lists the accounts, with recent sign-in attempts. From
there you can:
- add a person;
- set a temporary password;
- sign someone out everywhere;
- disable or enable an account, which also unlocks it;
- rename an account.

A password set for someone else is temporary: until they choose their own, every
page sends them to **Account**. Your own password is changed from **Account**
(top right). Account changes appear in the change history, never with
passwords. The same can be done from the command line:

```
.venv\Scripts\python -m kkj.web.users add <username> --name "Full Name"   # asks for the password
.venv\Scripts\python -m kkj.web.users list | reset <username> | disable <username> | enable <username> | log
```

- **Passwords** are hashed with Argon2id; at least 8 characters, no forced
  symbols (NIST SP 800-63B). Changing a password signs out that person's other
  browsers.
- **Sessions** are kept in `auth.session`. The cookie is HttpOnly and
  SameSite=Lax, and the table stores only a SHA-256 of the token. A session
  ends after 12 hours unused (the pages' background polling doesn't count) or
  7 days in all; see `SESSION_*` in `.env.example`. Signing out ends it on the
  server.
- **Lock-out:** 5 wrong passwords in a row lock the account for 15 minutes. The
  message never says whether the username or the password was wrong. Every
  attempt is logged in `auth.login_event`.
- **Protection:** POSTs from another site are refused (Origin check). Pages are
  sent with `no-store`, `X-Frame-Options: DENY` and `nosniff`.
- **HTTPS:** on plain HTTP a password can be read by anyone able to watch the
  office network. Serve over HTTPS (a reverse proxy or uvicorn's
  `--ssl-keyfile/--ssl-certfile`), then set `COOKIE_SECURE=true`.

| Section | Page | What it does |
|---|---|---|
| Actions | **Recommendations** (`/recommendations`) | The live inbox: filter, approve, dismiss, snooze, mark complete, add a note. Each recommendation has its own page with supporting data, the pieces and history. Exports: transfer slips, markdown lists, the purchasing list. |
| Analytics | **Overview** | Headline numbers (stock, FY net sales, turn, ageing), monthly sales, stock over time |
| | **Inventory health** | Value by branch and age, group × age, oldest stock, stock not on a counter |
| | **Sales** | By month and group, branch, items, days to sell, salespeople (named from Settings), returns |
| | **Stock turn** | Weighted stock turn per brick against the ideal turn: increase, reduce, keep, phase out (see below) |
| | **Insights** | Sell rate by age, price-adjusted supplier performance, stock vs sales by price band and item, exhibitions vs showrooms, clearance candidates; each with a recommended action |
| Admin | **Data quality** | Recommendation refreshes, stock runs, sales loads, past-date snapshot verdicts |
| | **Settings** | Business rules by section, salesperson and branch lists (rows save as you edit), change history |

All filters are plain URL parameters, so a filtered view can be shared as a link.

**Refreshing recommendations:** `python -m kkj.brain.run` re-runs every rule on
the latest data. A recommendation keeps its identity across refreshes:
- it reopens if its issue comes back;
- it is resolved automatically when the issue clears;
- once approved, it is completed when the issue clears.

It runs after the overnight load and from the **Refresh recommendations** button.

The earlier Streamlit dashboard (`src/kkj/dashboard`) is superseded by the web
app and no longer run.

## Weighted stock turn (`kkj/metrics/brick_turn.py`)

The Stock_ETL requirement, rebuilt on the clean data. Every rule below is a business
rule in **Settings → Stock turn**, not a constant.

- **Brick:** Purity – Product (`item_name`) – Variety (`category`) – Type (`sub_category`) –
  Weight range. For diamond jewellery the weight range is the total diamond weight in
  carats (default edges 0.1 / 0.25 / 0.5 / 1 / 2 / 5 ct), the usual planning measure; gross
  weight in grams is the alternative for gold. The data has no size field.
- **Windows:** the last 3, 6 and 12 months, with stock read once a fortnight (6 / 13 / 26
  fortnights; the latest snapshot in each).
  - A window is judged only if at least 60% of its fortnights have stock data. Missing
    or quarantined snapshots don't count against a brick.
  - A window counts if the brick was in stock (pcs > 0) in at least 60% of those
    fortnights.
- **Turn** = net pieces sold in the window, annualised ÷ average pieces held. Out-of-stock
  fortnights count as 0 in the average.
- **Blend:**
  - all three windows count → 60/30/10;
  - 6 and 12 months → 75/25;
  - 3 and 6 months (a new product) → 75/25;
  - 3 months only → "too new".
- **Verdict:** compared with the ideal turn (default 1.0× a year):
  - within ±10% → keep;
  - below → reduce;
  - above → increase.

  Ideal stock = blended yearly sales ÷ ideal turn.
- **Exception:** when a brick has too little availability and is out of range, it is
  held for 3 months if its neighbouring weight ranges are both within range. Otherwise
  it is phased out, or, if it sells whenever it is in stock, marked "sells when stocked".
- **Fixed from Stock_ETL:**
  - bricks with stock but no sales are kept (turn 0);
  - bricks that sold with no stock are shown;
  - zero-stock fortnights count in the average;
  - new launches are shown, not dropped.
- **Runs:** with every recommendation refresh.
  - It saves the company and per-showroom results to `brain.brick_turn` (one set per
    stock date).
  - It raises **Stock levels** recommendations: Increase / Reduce / Phase out at company
    level, with the split by branch as evidence.
  - It needs the fortnightly history, which the past-date stock pulls provide.

## Tests

```
.venv\Scripts\python -m pytest
```

`tests/test_stock_report.py` cleans every real export in `data/stock/raw/` and
compares the result, cell for cell, with the original pipeline's output.
