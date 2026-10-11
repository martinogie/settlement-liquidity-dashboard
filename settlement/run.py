"""End to end: generate data, reconcile, simulate liquidity, build daily snapshots, load SQLite,
run the SQL KPIs, and write reports plus the dashboard page.

    python -m settlement.run
"""
import json
import math
import re
import sqlite3
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from .data import END, INCIDENTS, SOURCES, START, generate
from .liquidity import ACCOUNTS, simulate
from .matching import reconcile, score_against_truth
from .metrics import BUCKETS, LIMITS, manual_resolution, snapshots

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"


def build(seed=11):
    batches, lines, truth, bdays = generate(seed)
    links, status, line_status = reconcile(batches, lines, bdays)
    status, line_status = manual_resolution(status, line_status, truth, bdays)
    liq = simulate(batches, lines, bdays, START, END)
    return dict(batches=batches, lines=lines, truth=truth, bdays=bdays, links=links,
                status=status, line_status=line_status, liq=liq)


def load_sqlite(r, path):
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    day = lambda s: s.dt.strftime("%Y-%m-%d")
    b = r["batches"].copy()
    for c in ("sale_date", "expected_date"):
        b[c] = day(b[c])
    b.to_sql("batches", con, index=False)
    ln = r["line_status"][["line_id", "value_date", "bank", "amount", "description", "matched"]].copy()
    ln["value_date"] = day(ln["value_date"])
    ln.to_sql("bank_lines", con, index=False)
    r["links"].to_sql("links", con, index=False)
    s = r["status"][["batch_id", "source", "bank", "expected_date", "amount", "matched", "rule",
                     "received", "received_date", "variance", "manual_date"]].copy()
    for c in ("expected_date", "received_date", "manual_date"):
        s[c] = day(s[c])
    s.to_sql("recon", con, index=False)
    bal = r["liq"].rename(columns={"Bank A": "bank_a", "Bank B": "bank_b", "Bank C": "bank_c"}).copy()
    bal["date"] = day(bal["date"])
    bal.to_sql("balances", con, index=False)
    con.commit()
    return con


def sql_queries(path=ROOT / "sql" / "kpis.sql"):
    text = path.read_text()
    parts = re.split(r"^-- name: (\w+)\s*$", text, flags=re.M)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts), 2)}


def run_sql(con, as_of=END):
    return {name: pd.read_sql_query(q, con, params={"as_of": as_of}) for name, q in sql_queries().items()}


def md_table(df):
    head = "| " + " | ".join(df.columns) + " |\n|" + "---|" * len(df.columns)
    rows = ["| " + " | ".join("" if pd.isna(v) else str(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join([head] + rows)


def clean(o):
    if isinstance(o, float):
        return None if math.isnan(o) else round(o, 4)
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    return o


def plot(r, snaps, path):
    liq = r["liq"]
    fig, ax = plt.subplots(1, 3, figsize=(17, 4.6))
    banks = ["Bank A", "Bank B", "Bank C"]
    ax[0].stackplot(liq["date"], *[liq[b] / 1e6 for b in banks], labels=banks,
                    colors=["#2a78d6", "#eb6834", "#1baf7a"])
    ax[0].plot(liq["date"], liq["restricted"] / 1e6, color="#0b0b0b", lw=1.4, ls="--", label="Restricted reserve")
    ax[0].set_title("Cash by bank, $M")
    ax[0].legend(frameon=False, fontsize=8, loc="upper left", ncol=4)
    ax[0].set_ylim(0, liq["total_cash"].max() / 1e6 * 1.18)
    ax[1].plot(liq["date"], liq["coverage_days"], color="#1F2A36", lw=1.6)
    ax[1].axhline(LIMITS["coverage_amber"], color="#C0892B", ls="--", lw=1, label="Target 3.0")
    ax[1].axhline(LIMITS["coverage_red"], color="#B4372E", ls="--", lw=1, label="Limit 2.5")
    ax[1].set_title("Liquidity coverage, days of payouts")
    ax[1].legend(frameon=False, fontsize=8)
    dates = pd.to_datetime([s["date"] for s in snaps])
    ax[2].bar(dates, [s["types"]["Not received"] / 1e6 for s in snaps], color="#2a78d6", label="Not received", width=1)
    ax[2].bar(dates, [s["types"]["Short-paid"] / 1e6 for s in snaps], bottom=[s["types"]["Not received"] / 1e6 for s in snaps],
              color="#eb6834", label="Short-paid", width=1)
    ax[2].set_title("Open settlement exceptions, $M")
    top = ax[2].get_ylim()[1]
    for k, inc in enumerate(INCIDENTS):
        ax[2].axvline(pd.Timestamp(inc["start"]), color="#898781", lw=0.8, ls=":")
        ax[2].text(pd.Timestamp(inc["start"]), top * (0.97 - 0.07 * k), " " + inc["note"], fontsize=7.5,
                   ha="right", va="top", color="#52514e")
    ax[2].legend(frameon=False, fontsize=8, loc="upper left")
    for a in ax:
        a.spines[["top", "right"]].set_visible(False)
        a.tick_params(axis="x", labelrotation=30, labelsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main():
    t0 = time.time()
    REPORTS.mkdir(exist_ok=True)
    r = build()
    snaps = clean(snapshots(r["status"], r["line_status"], r["truth"], r["liq"], r["bdays"]))
    st = r["status"][r["status"]["expected_date"] <= pd.Timestamp(END)]
    rules = st["rule"].fillna((st["manual_date"] <= pd.Timestamp(END)).map({True: "manual", False: "open"})).value_counts()
    acc = score_against_truth(r["links"], r["lines"])
    auto = float(st["matched"].mean())
    lines_auto = float(r["line_status"]["matched"].mean())
    red_days = [s["date"] for s in snaps if any(a["level"] == "red" for a in s["alerts"])]
    metrics = {
        "period": [START, END], "business_days": len(snaps), "batches": int(len(st)),
        "settled_usd": float(st["amount"].sum()), "bank_lines": int(len(r["lines"])),
        "auto_match_batches": auto, "auto_match_lines": lines_auto,
        "rules": {k: int(v) for k, v in rules.items()}, "engine_accuracy": acc,
        "red_alert_days": red_days, "incidents": INCIDENTS,
        "runtime_seconds": round(time.time() - t0, 1),
    }
    (REPORTS / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str))

    con = load_sqlite(r, REPORTS / "settlement.db")
    results = run_sql(con)
    md = [f"# SQL KPI results (as of {END})", "", "Queries: [sql/kpis.sql](../sql/kpis.sql). Run `python -m settlement.run` to rebuild `reports/settlement.db`.", ""]
    for name, df in results.items():
        md += [f"## {name}", "", md_table(df), ""]
    (REPORTS / "SQL_RESULTS.md").write_text("\n".join(md))
    plot(r, snaps, REPORTS / "results.png")

    page = {
        "snapshots": snaps,
        "buckets": [b for b, _, _ in BUCKETS],
        "accounts": [{"code": c, "label": l, "bank": b} for c, l, b in ACCOUNTS],
        "limits": LIMITS,
        "engine": {"rules": metrics["rules"], "precision": acc["precision"], "recall": acc["recall"],
                   "auto_match_batches": auto, "auto_match_lines": lines_auto, "batches": metrics["batches"],
                   "settled_usd": metrics["settled_usd"], "lines": metrics["bank_lines"]},
        "incidents": INCIDENTS,
    }
    html = (ROOT / "web" / "template.html").read_text().replace("/*DATA*/null", json.dumps(page, separators=(",", ":")))
    (ROOT / "index.html").write_text(html)
    print(json.dumps({k: metrics[k] for k in ("batches", "auto_match_batches", "auto_match_lines", "rules", "engine_accuracy")}, indent=1))
    print(f"red alert days: {len(red_days)}; runtime {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
