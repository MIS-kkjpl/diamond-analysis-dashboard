"""
Why did labels leave stock without a retail sale? Look up the voucher history of a stratified sample
of the audit's unexplained exits (latest data/audit/*.xlsx, sheet "L3 left, unexplained") and report
the voucher that took each one out.

One small ERP call per label, --pause seconds apart. Raw responses saved to data/audit/label_status/.

Run:  .venv\\Scripts\\python scripts\\probes\\label_history_sample.py [--n 200] [--pause 3]
"""
import argparse
import glob
import json
import time

import numpy as np
import pandas as pd
from sqlalchemy import text

from kkj import settings
from kkj.erp import label_status
from kkj.erp.client import login, new_session
from kkj.metrics.core import location_type

ap = argparse.ArgumentParser()
ap.add_argument("--n", type=int, default=200)
ap.add_argument("--pause", type=float, default=3)
ap.add_argument("--since", help="only exits on or after this date (YYYY-MM-DD); the ERP login sees the current FY only")
args = ap.parse_args()

out_dir = settings.PROJECT_DIR / "data" / "audit" / "label_status"
out_dir.mkdir(parents=True, exist_ok=True)
audit = sorted(glob.glob(str(settings.PROJECT_DIR / "data" / "audit" / "audit_*.xlsx")))[-1]
gone = pd.read_excel(audit, "L3 left, unexplained")
gone["last_seen"], gone["missing_from"] = pd.to_datetime(gone["last_seen"]), pd.to_datetime(gone["missing_from"])
with settings.etl_engine().connect() as c:
    kinds = pd.read_sql(text("SELECT erp_branch, kind FROM config.branch"), c).set_index("erp_branch")["kind"]
gone["kind"] = gone["branch"].map(kinds).fillna("other")
gone["where"] = location_type(gone["location"])
gone = gone.drop_duplicates("barcode", keep="last")
if args.since:
    gone = gone[gone["missing_from"] >= pd.Timestamp(args.since)]

# stratified: every (branch kind, location type) group gets a share of the sample, at least 5 where possible
groups = gone.groupby(["kind", "where"])
share = (groups.size() / len(gone) * args.n).clip(lower=5).round().astype(int)
sample = pd.concat([g.sample(min(len(g), share[k]), random_state=1) for k, g in groups])
print(f"{len(sample)} labels from {audit}")

erp = settings.erp_login_settings()
session = new_session(erp)
login(session, erp)
rows = []
for i, r in enumerate(sample.itertuples()):
    path = out_dir / f"{r.barcode}.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        if i:
            time.sleep(args.pause)
        data = label_status.fetch(session, r.barcode, erp["role_code"])
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    h = label_status.history(data)
    # the voucher that took it out: an Out with pieces after it was last seen in stock
    window = h[(h["docDate"] >= r.last_seen) & (h["docDate"] <= r.missing_from + pd.Timedelta(days=1))] if len(h) else h
    outs = window[(window["transactionType"] == "Out") & (window["piecesOut"].fillna(0) > 0)] if len(h) else h
    later = h[h["docDate"] > r.missing_from] if len(h) else h
    rows.append({"barcode": r.barcode, "kind": r.kind, "where": r.where, "location": r.location,
                 "last_seen": r.last_seen.date(), "missing_from": r.missing_from.date(),
                 "out_voucher": outs["voucherMaster"].iloc[-1] if len(outs) else ("(no history)" if h.empty else "(no Out voucher)"),
                 "out_location": outs["locationName"].iloc[-1] if len(outs) else "",
                 "out_doc": outs["docNo"].iloc[-1] if len(outs) else "",
                 "vouchers_in_window": " > ".join(map(str, window["voucherMaster"])) if len(h) else "",
                 "came_back_later": bool(len(later) and (later["transactionType"] == "In").any()),
                 "last_voucher": h["voucherMaster"].iloc[-1] if len(h) else "",
                 "last_location": h["locationName"].iloc[-1] if len(h) else ""})
    if (i + 1) % 25 == 0:
        print(f"  {i + 1}/{len(sample)}")

res = pd.DataFrame(rows)
res.to_csv(out_dir.parent / f"label_history_sample{'_since_' + args.since if args.since else ''}.csv", index=False)
pd.set_option("display.width", 220)
print("\nVoucher that took the label out, by where it was:")
print(pd.crosstab(res["out_voucher"], [res["kind"], res["where"]], margins=True).to_string())
print("\nCame back into stock later:", int(res["came_back_later"].sum()), "of", len(res))
print("\nMost common last vouchers:", res["last_voucher"].value_counts().head(10).to_dict())
