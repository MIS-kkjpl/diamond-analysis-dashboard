"""
Save the ERP login for an earlier financial year in .env (the password is typed at a hidden prompt).

Each financial year is its own ERP program: 2025-26 = sProgramKey 54. The current year keeps using
ERP_USERNAME / ERP_PASSWORD.

Run:  .venv\\Scripts\\python scripts\\set_erp_year.py 2025-26
"""
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kkj.settings import ERP_YEARS  # noqa: E402

year = sys.argv[1] if len(sys.argv) > 1 else "2025-26"
if year not in ERP_YEARS:
    sys.exit(f"Unknown year {year}; known: {', '.join(ERP_YEARS)}")
tag = "ERP_" + year.replace("-", "_")
user = input(f"ERP username for {year}: ").strip()
password = getpass.getpass(f"ERP password for {year} (hidden): ")
if not user or not password:
    sys.exit("Nothing saved.")

env = ROOT / ".env"
lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
new = {f"{tag}_USERNAME": user, f"{tag}_PASSWORD": password, f"{tag}_PROGRAM_KEY": ERP_YEARS[year]}
lines = [l for l in lines if l.split("=", 1)[0].strip() not in new]
lines += [f"{k}={v}" for k, v in new.items()]
env.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"Saved the {year} login in .env.")
