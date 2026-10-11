"""Daily cash positions by bank account, with the treasury sweep and prefunding policy.

Merchants are paid on schedule whether or not the network has settled, so a late settlement
must be covered from the company's own cash. Each evening treasury:

1. sweeps collection accounts down to $1M into the payout account,
2. makes sure the payout account holds 105% of tomorrow's payouts, topping up from operating
   (kept above $8M) and then from the unrestricted part of the reserve,
3. moves payout-account cash above 125% of tomorrow's need back to operating,
4. on Fridays, invests operating cash above $35M into the reserve (money-market) account.
"""
import numpy as np
import pandas as pd

ACCOUNTS = [  # code, label, bank
    ("A_COLL", "Collections", "Bank A"),
    ("B_COLL", "Collections", "Bank B"),
    ("PAYOUT", "Merchant payouts", "Bank B"),
    ("OPER", "Operating", "Bank A"),
    ("RESERVE", "Reserve (money market)", "Bank C"),
]
OPENING = {"A_COLL": 1e6, "B_COLL": 1e6, "PAYOUT": 26e6, "OPER": 32e6, "RESERVE": 45e6}
PAYOUT_RATE = 0.988          # merchants receive settlement less our margin
COLL_TARGET, OPER_MIN, OPER_CAP = 1e6, 8e6, 35e6
PREFUND, PREFUND_CAP = 1.05, 1.25
RESTRICTED = [("2026-04-01", 25e6), ("2026-07-01", 40e6)]   # scheme reserve requirement steps up in Q3


def restricted_on(day):
    amt = 0.0
    for start, value in RESTRICTED:
        if day >= pd.Timestamp(start):
            amt = value
    return amt


def simulate(batches, lines, bdays, start, end):
    days = bdays[(bdays >= pd.Timestamp(start)) & (bdays <= pd.Timestamp(end))]
    payouts = (batches.groupby("expected_date")["amount"].sum() * PAYOUT_RATE).reindex(bdays, fill_value=0.0)
    receipts = lines.groupby(["value_date", "bank"])["amount"].sum()
    bal = dict(OPENING)
    rows = []
    for i, d in enumerate(days):
        tomorrow = bdays[bdays.get_loc(d) + 1]
        bal["A_COLL"] += receipts.get((d, "Bank A"), 0.0)
        bal["B_COLL"] += receipts.get((d, "Bank B"), 0.0)
        bal["PAYOUT"] -= payouts[d]
        opex = 0.12e6 + (1.3e6 if d.dayofweek == 4 and (d - days[0]).days // 7 % 2 == 0 else 0.0)
        bal["OPER"] -= opex
        if d.day <= 3 and d.month != days[max(i - 1, 0)].month:
            bal["RESERVE"] += bal["RESERVE"] * 0.042 / 12
        intraday_low = bal["PAYOUT"]

        for acct in ("A_COLL", "B_COLL"):
            sweep = max(bal[acct] - COLL_TARGET, 0.0)
            bal[acct] -= sweep
            bal["PAYOUT"] += sweep
        need = PREFUND * payouts[tomorrow]
        topup_oper = topup_res = shortfall = 0.0
        if bal["PAYOUT"] < need:
            topup_oper = min(need - bal["PAYOUT"], max(bal["OPER"] - OPER_MIN, 0.0))
            bal["OPER"] -= topup_oper
            bal["PAYOUT"] += topup_oper
        if bal["PAYOUT"] < need:
            free = max(bal["RESERVE"] - restricted_on(d), 0.0)
            topup_res = min(need - bal["PAYOUT"], free)
            bal["RESERVE"] -= topup_res
            bal["PAYOUT"] += topup_res
        if bal["PAYOUT"] < need:
            shortfall = need - bal["PAYOUT"]
        excess = bal["PAYOUT"] - PREFUND_CAP * payouts[tomorrow]
        if excess > 0:
            bal["PAYOUT"] -= excess
            bal["OPER"] += excess
        if d.dayofweek == 4 and bal["OPER"] > OPER_CAP:
            invest = bal["OPER"] - OPER_CAP
            bal["OPER"] -= invest
            bal["RESERVE"] += invest

        rows.append({"date": d, **{k: round(v, 2) for k, v in bal.items()},
                     "payouts_today": payouts[d], "payouts_tomorrow": payouts[tomorrow],
                     "receipts_today": receipts.get((d, "Bank A"), 0.0) + receipts.get((d, "Bank B"), 0.0),
                     "payout_intraday_low": intraday_low, "topup_from_operating": topup_oper,
                     "topup_from_reserve": topup_res, "prefund_shortfall": shortfall,
                     "restricted": restricted_on(d)})
    df = pd.DataFrame(rows)
    df["total_cash"] = df[[a for a, _, _ in ACCOUNTS]].sum(axis=1)
    df["available"] = df["total_cash"] - df["restricted"]
    avg20 = payouts.where(payouts.index >= pd.Timestamp(start)).rolling(20, min_periods=1).mean().reindex(df["date"]).values
    df["coverage_days"] = df["available"] / avg20     # days of payouts covered with no settlements
    banks = {"Bank A": ["A_COLL", "OPER"], "Bank B": ["B_COLL", "PAYOUT"], "Bank C": ["RESERVE"]}
    for bank, accts in banks.items():
        df[bank] = df[accts].sum(axis=1)
    df["top_bank_share"] = df[list(banks)].max(axis=1) / df["total_cash"]
    df["top_bank"] = df[list(banks)].idxmax(axis=1)
    df["prefund_headroom"] = df["PAYOUT"] - PREFUND * df["payouts_tomorrow"]
    return df
