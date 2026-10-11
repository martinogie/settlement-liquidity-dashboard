"""Synthetic settlement batches and bank statement lines for a card-payments business.

Each business day every settlement source (card networks, ACH, real-time payments) sends a set of
batches it expects to pay. The money then shows up as lines on bank statements, but not always
cleanly: some lines lose their reference, some arrive late, some are short-paid, a few are split
in two, ACH sometimes nets two batches into one line, and a few never arrive until operations
chases them. Unexpected credits (unidentified cash) also appear.

The true batch behind each bank line is kept in `truth_batches` so the matching engine can be
scored; the engine itself never reads it.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar

START, END = "2026-04-01", "2026-10-09"


@dataclass(frozen=True)
class Source:
    code: str
    name: str
    lag: int            # business days from sale to expected settlement
    share: float        # share of daily volume
    batches: int        # batches per day
    bank: str           # collection bank
    p_no_ref: float     # bank line loses the batch reference
    p_short: float      # short-paid (fees or adjustments deducted)
    p_late: float       # arrives 1-3 business days late
    p_missing: float    # does not arrive until chased (5-12 business days)
    p_split: float      # paid as two lines
    p_net: float        # netted with another batch into one line (per batch)


SOURCES = [
    Source("VI", "Visa", 1, 0.42, 12, "Bank A", 0.02, 0.008, 0.015, 0.002, 0.010, 0.00),
    Source("MC", "Mastercard", 1, 0.30, 10, "Bank A", 0.02, 0.010, 0.020, 0.003, 0.010, 0.00),
    Source("AX", "Amex", 2, 0.10, 3, "Bank B", 0.03, 0.040, 0.030, 0.004, 0.000, 0.00),
    Source("AC", "ACH", 1, 0.12, 6, "Bank B", 0.30, 0.040, 0.030, 0.005, 0.000, 0.12),
    Source("RT", "RTP", 0, 0.06, 4, "Bank A", 0.01, 0.000, 0.005, 0.001, 0.000, 0.00),
]
BY_CODE = {s.code: s for s in SOURCES}
DAILY_VOLUME = 24e6
NET_RATE = 0.985          # settlement net of interchange and scheme fees

# Incidents the dashboard should surface
INCIDENTS = [
    {"source": "AX", "start": "2026-08-17", "days": 3, "delay": 3, "note": "Amex settlement outage"},
    {"source": "AC", "start": "2026-09-14", "days": 1, "delay": 2, "note": "ACH file rejected by bank"},
    {"source": "VI", "start": "2026-09-29", "days": 1, "delay": 1, "note": "Visa settlement held one day"},
]


def business_days(start=START, end=END, pad=30):
    start = pd.Timestamp(start) - pd.Timedelta(days=pad)   # earlier sales settle into the period
    hol = USFederalHolidayCalendar().holidays(start, pd.Timestamp(end) + pd.Timedelta(days=pad))
    days = pd.bdate_range(start, pd.Timestamp(end) + pd.Timedelta(days=pad))
    return days[~days.isin(hol)].as_unit("ns")


def shift(bdays, day, n):
    i = bdays.get_loc(day) + n
    return bdays[min(i, len(bdays) - 1)]


def expected_batches(rng, bdays, end=END):
    rows = []
    sale_days = bdays[(bdays >= pd.Timestamp(START) - pd.Timedelta(days=5)) & (bdays <= pd.Timestamp(end))]
    for day in sale_days:
        level = DAILY_VOLUME * (1.25 if day.dayofweek == 0 else 1.0) * rng.lognormal(0, 0.08)
        for s in SOURCES:
            weights = rng.dirichlet(np.ones(s.batches) * 3)
            for k, w in enumerate(weights, 1):
                exp_day = shift(bdays, day, s.lag)
                rows.append({
                    "batch_id": f"{s.code}{day:%Y%m%d}-{k:02d}", "source": s.code, "bank": s.bank,
                    "sale_date": day, "expected_date": exp_day,
                    "amount": round(level * s.share * w * NET_RATE, 2),
                })
    df = pd.DataFrame(rows)
    # batches due after `end` are kept: they are tomorrow's known payout schedule
    df = df[df["expected_date"] >= pd.Timestamp(START)]
    return df.reset_index(drop=True)


def describe(source, ref):
    s = BY_CODE[source]
    prefix = {"VI": "VISA NET SETTLEMENT", "MC": "MASTERCARD SETTLE", "AX": "AMEX SETTLEMENT",
              "AC": "ACH CREDIT ORIG", "RT": "RTP CREDIT"}[source]
    return f"{prefix} REF {ref}" if ref else f"{prefix} {s.bank.upper()} DEPOSIT"


def bank_lines(rng, batches, bdays, end=END):
    """Turn expected batches into what the bank actually reports."""
    lines, outcome = [], {}
    incident_delay = {}
    for inc in INCIDENTS:
        start = pd.Timestamp(inc["start"])
        days = bdays[bdays >= start][:inc["days"]]
        for d in days:
            incident_delay[(inc["source"], d)] = inc["delay"]

    def add(day, amount, desc, truth, bank):
        lines.append({"value_date": day, "amount": round(amount, 2), "description": desc,
                      "bank": bank, "truth_batches": truth})

    pending_net = {}
    for b in batches.itertuples():
        s = BY_CODE[b.source]
        r = rng.random(6)
        day, amount, kind = b.expected_date, b.amount, "clean"
        if (b.source, b.expected_date) in incident_delay:
            day, kind = shift(bdays, day, incident_delay[(b.source, b.expected_date)]), "incident"
        elif r[0] < s.p_missing:
            day, kind = shift(bdays, day, int(rng.integers(5, 13))), "missing"
        elif r[1] < s.p_late:
            day, kind = shift(bdays, day, int(rng.integers(1, 4))), "late"
        short = 0.0
        if r[2] < s.p_short:
            short = round(amount * rng.uniform(0.004, 0.03), 2)
            kind = kind + "+short" if kind != "clean" else "short"
        ref = None if r[3] < s.p_no_ref else b.batch_id
        paid = amount - short
        outcome[b.batch_id] = {"kind": kind, "paid_date": day, "short": short,
                               "resolved_date": shift(bdays, day, int(rng.integers(3, 16))) if short else day}
        if r[4] < s.p_net:
            key = (b.source, day)
            if key in pending_net:
                other_id, other_paid = pending_net.pop(key)
                add(day, other_paid + paid, describe(b.source, None), [other_id, b.batch_id], s.bank)
                continue
            pending_net[key] = (b.batch_id, paid)
            continue
        if r[5] < s.p_split:
            part = round(paid * rng.uniform(0.3, 0.7), 2)
            add(day, part, describe(b.source, ref), [b.batch_id], s.bank)
            add(day, paid - part, describe(b.source, ref), [b.batch_id], s.bank)
        else:
            add(day, paid, describe(b.source, ref), [b.batch_id], s.bank)
    for (src, day), (bid, paid) in pending_net.items():   # no partner that day: paid alone
        add(day, paid, describe(src, None), [bid], BY_CODE[src].bank)

    # unidentified credits: chargeback reversals, misdirected wires
    for day in bdays[(bdays >= pd.Timestamp(START)) & (bdays <= pd.Timestamp(end))]:
        if rng.random() < 0.15:
            bank = rng.choice(["Bank A", "Bank B"])
            add(day, round(rng.uniform(2e3, 9e4), 2), "INCOMING WIRE ORIG UNKNOWN", [], bank)

    df = pd.DataFrame(lines)
    df = df[df["value_date"] <= pd.Timestamp(end)].sort_values(["value_date", "bank"], kind="stable")
    df.insert(0, "line_id", [f"L{i:06d}" for i in range(1, len(df) + 1)])
    out = pd.DataFrame.from_dict(outcome, orient="index").rename_axis("batch_id").reset_index()
    return df.reset_index(drop=True), out


def generate(seed=11, end=END):
    rng = np.random.default_rng(seed)
    bdays = business_days(end=end)
    batches = expected_batches(rng, bdays, end)
    lines, truth = bank_lines(rng, batches, bdays, end)
    unidentified = lines["truth_batches"].str.len() == 0
    lines["resolved_date"] = pd.NaT
    lines.loc[unidentified, "resolved_date"] = [shift(bdays, d, int(rng.integers(2, 11)))
                                                for d in lines.loc[unidentified, "value_date"]]
    return batches, lines, truth, bdays
