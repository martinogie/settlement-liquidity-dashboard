"""Daily "as of" snapshots for the dashboard: open exceptions, settlement timeliness, liquidity
and the alerts a treasury or payments-operations lead would want each morning."""
import numpy as np
import pandas as pd

from .data import BY_CODE, SOURCES, shift

BUCKETS = [("0-1", 0, 1), ("2-3", 2, 3), ("4-5", 4, 5), ("6+", 6, 10**6)]
LIMITS = {
    "coverage_amber": 3.0, "coverage_red": 2.5,
    "concentration_amber": 0.45,
    "on_time_amber": 0.90, "on_time_red": 0.75,
    "aged_red": 2e6,                     # $ of exceptions older than 5 business days
    "unidentified_amber": 2.5e5,
}


def manual_resolution(status, lines, truth, bdays, seed=5):
    """Simulated operations case log: items the engine could not match are investigated and
    matched by hand 1-5 business days after the cash arrives. Unidentified credits already carry
    the date they were applied."""
    rng = np.random.default_rng(seed)
    paid = truth.set_index("batch_id")["paid_date"]
    status = status.copy()
    status["manual_date"] = pd.NaT
    todo = status.index[~status["matched"] & status["batch_id"].isin(paid.index)]
    status.loc[todo, "manual_date"] = [shift(bdays, paid[b], int(rng.integers(1, 6))) for b in status.loc[todo, "batch_id"]]
    closed = dict(zip(status["batch_id"], status["manual_date"]))
    lines = lines.copy()
    todo = ~lines["matched"] & (lines["truth_batches"].str.len() > 0)
    lines.loc[todo, "resolved_date"] = [max(closed[b] for b in tb) for tb in lines.loc[todo, "truth_batches"]]
    return status, lines


def exceptions_as_of(d, status, lines, claims, pos):
    """Every open item on day d with its type, amount and age in business days."""
    dpos = pos[d]
    s = status
    still_open = (s["matched"] & (s["received_date"] > d)) | \
        (~s["matched"] & (s["manual_date"].isna() | (s["manual_date"] > d)))
    not_recv = s[(s["expected_date"] <= d) & still_open]
    a = pd.DataFrame({"id": not_recv["batch_id"], "type": "Not received", "source": not_recv["source"],
                      "amount": not_recv["amount"], "age": dpos - not_recv["expected_date"].map(pos)})
    short = s[s["matched"] & (s["variance"].abs() > 0.01) & (s["received_date"] <= d)]
    short = short[short["batch_id"].map(claims) > d]
    b = pd.DataFrame({"id": short["batch_id"], "type": "Short-paid", "source": short["source"],
                      "amount": short["variance"], "age": dpos - short["received_date"].map(pos)})
    un = lines[~lines["matched"] & (lines["value_date"] <= d)
               & (lines["resolved_date"].isna() | (lines["resolved_date"] > d))]
    c = pd.DataFrame({"id": un["line_id"], "type": "Unidentified cash", "source": un["source"].fillna("--"),
                      "amount": un["amount"], "age": dpos - un["value_date"].map(pos)})
    return pd.concat([x for x in (a, b, c) if len(x)], ignore_index=True) if any(len(x) for x in (a, b, c)) \
        else pd.DataFrame(columns=["id", "type", "source", "amount", "age"])


def bucket(age):
    for name, lo, hi in BUCKETS:
        if lo <= age <= hi:
            return name


def on_time(status, d, days_back, bdays, source=None):
    window = bdays[(bdays <= d)][-days_back:]
    s = status[status["expected_date"].isin(window)]
    if source:
        s = s[s["source"] == source]
    if not len(s):
        return np.nan
    ok = s["matched"] & (s["received_date"] <= s["expected_date"])
    return float((s.loc[ok, "amount"].sum()) / s["amount"].sum())


def snapshots(status, lines, truth, liq, bdays):
    pos = pd.Series(np.arange(len(bdays)), index=bdays)
    claims = truth.set_index("batch_id")["resolved_date"]
    out = []
    for row in liq.itertuples():
        d = row.date
        ex = exceptions_as_of(d, status, lines, claims, pos)
        ex["bucket"] = ex["age"].map(bucket)
        recent = bdays[bdays <= d][-20:]
        today_lines = lines[lines["value_date"].isin(recent)]
        due = status[status["expected_date"] == d]
        sources = []
        for s in SOURCES:
            e = ex[ex["source"] == s.code]
            sources.append({
                "code": s.code, "name": s.name,
                "due": round(due.loc[due["source"] == s.code, "amount"].sum()),
                "on_time_5d": on_time(status, d, 5, bdays, s.code),
                "open": round(e["amount"].sum()),
                "aging": [round(e.loc[e["bucket"] == b, "amount"].sum()) for b, _, _ in BUCKETS],
            })
        aged = ex.loc[ex["age"] > 5, "amount"].sum()
        unidentified = ex.loc[ex["type"] == "Unidentified cash", "amount"].sum()
        alerts = []

        def alert(level, text):
            alerts.append({"level": level, "text": text})

        if row.prefund_shortfall > 0:
            alert("red", f"Payout account short ${row.prefund_shortfall / 1e6:.1f}M for tomorrow's payouts")
        if row.coverage_days < LIMITS["coverage_red"]:
            alert("red", f"Liquidity covers {row.coverage_days:.2f} days of payouts (limit {LIMITS['coverage_red']})")
        elif row.coverage_days < LIMITS["coverage_amber"]:
            alert("amber", f"Liquidity covers {row.coverage_days:.1f} days of payouts (target {LIMITS['coverage_amber']:.0f})")
        if row.topup_from_reserve > 0:
            alert("amber", f"Drew ${row.topup_from_reserve / 1e6:.1f}M from reserve to prefund payouts")
        if row.topup_from_operating > 2e6:
            alert("amber", f"Moved ${row.topup_from_operating / 1e6:.1f}M from operating to prefund payouts")
        if row.top_bank_share > LIMITS["concentration_amber"]:
            alert("amber", f"{row.top_bank} holds {row.top_bank_share:.0%} of cash (limit {LIMITS['concentration_amber']:.0%})")
        for s in sources:
            r = s["on_time_5d"]
            if r == r and r < LIMITS["on_time_red"]:
                alert("red", f"{s['name']} settled {r:.0%} on time over 5 days")
            elif r == r and r < LIMITS["on_time_amber"]:
                alert("amber", f"{s['name']} settled {r:.0%} on time over 5 days")
        if aged > LIMITS["aged_red"]:
            alert("red", f"${aged / 1e6:.1f}M of exceptions older than 5 business days")
        elif aged > 0:
            alert("amber", f"${aged / 1e3:,.0f}K of exceptions older than 5 business days")
        if unidentified > LIMITS["unidentified_amber"]:
            alert("amber", f"${unidentified / 1e3:,.0f}K of unidentified cash to apply")

        top = ex.sort_values(["age", "amount"], ascending=False).head(8)
        out.append({
            "date": d.strftime("%Y-%m-%d"),
            "kpi": {
                "auto_match_20d": float(today_lines["matched"].mean()) if len(today_lines) else None,
                "lines_20d": int(len(today_lines)),
                "on_time_all_5d": on_time(status, d, 5, bdays),
                "open_amount": round(ex["amount"].sum()), "open_count": int(len(ex)),
                "aged_amount": round(aged), "unidentified": round(unidentified),
                "receivable": round(ex.loc[ex["type"] == "Not received", "amount"].sum()),
            },
            "liquidity": {k: round(getattr(row, k)) for k in
                          ["A_COLL", "B_COLL", "PAYOUT", "OPER", "RESERVE", "total_cash", "available", "restricted",
                           "payouts_tomorrow", "prefund_headroom", "topup_from_operating", "topup_from_reserve"]}
                         | {"coverage_days": round(row.coverage_days, 2), "top_bank_share": round(row.top_bank_share, 3),
                            "top_bank": row.top_bank},
            "sources": sources,
            "types": {t: round(ex.loc[ex["type"] == t, "amount"].sum()) for t in ["Not received", "Short-paid", "Unidentified cash"]},
            "top": [{"id": r.id, "type": r.type, "source": BY_CODE[r.source].name if r.source in BY_CODE else "Unknown",
                     "amount": round(r.amount), "age": int(r.age)} for r in top.itertuples()],
            "alerts": sorted(alerts, key=lambda a: a["level"] != "red"),
        })
    return out
