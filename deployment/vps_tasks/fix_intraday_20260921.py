"""One-off: drop 21-Sep intraday rows recorded while Kotak's holdings margin was
counted as cash (cash > 1L; the real cash is ~15k). Derived snapshot only - the
hash-chained ledger (fills/orders) is not touched."""
import csv
p = r"C:\Users\Administrator\Desktop\fyers_data_pipeline_git\deployment\dualmom_live_state\ledger\intraday\2026-09-21.csv"
rows = list(csv.DictReader(open(p, newline="")))
good = [r for r in rows if float(r["cash"]) < 100000]
if rows:
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(good)
print(f"intraday rows: {len(rows)} -> kept {len(good)} (dropped {len(rows) - len(good)})")
