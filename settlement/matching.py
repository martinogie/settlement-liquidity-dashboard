"""Rule-based reconciliation of expected settlement batches to bank statement lines.

Passes run from most to least certain, and each only sees what earlier passes left unmatched:

1. reference      reference found in the description, amounts agree to the cent
2. reference+var  reference found, amount differs (short-paid): matched, variance raised
3. split          two or more lines carry the same reference and sum to the batch
4. amount+date    no reference; same source and bank, exact amount, value date within
                  -1..+5 business days of expected, and only one candidate either way
5. netted         no reference; one line equals the sum of two or three unmatched batches from
                  the same source expected on or before the value date

Anything left is an exception: an expected batch not received, or unidentified cash.
"""
import itertools
import re

import numpy as np
import pandas as pd

REF = re.compile(r"\b([A-Z]{2}\d{8}-\d{2})\b")
KEYWORD = {"VISA": "VI", "MASTERCARD": "MC", "AMEX": "AX", "ACH": "AC", "RTP": "RT"}
TOL = 0.005


def source_of(description):
    for word, code in KEYWORD.items():
        if description.startswith(word):
            return code
    return None


def reconcile(batches, lines, bdays):
    """Returns (links, batch_status, line_status).

    links: one row per (batch_id, line_id) pair with the rule that made it.
    """
    lines = lines.copy()
    lines["ref"] = lines["description"].str.extract(REF, expand=False)
    lines["source"] = lines["description"].map(source_of)
    pos = pd.Series(np.arange(len(bdays)), index=bdays)
    lines["bpos"] = lines["value_date"].map(pos)
    b = batches.set_index("batch_id")
    b_pos = b["expected_date"].map(pos)

    links = []
    open_batches = set(b.index)
    open_lines = set(lines["line_id"])

    def link(batch_ids, line_ids, rule):
        for bid in batch_ids:
            for lid in line_ids:
                links.append({"batch_id": bid, "line_id": lid, "rule": rule})
        open_batches.difference_update(batch_ids)
        open_lines.difference_update(line_ids)

    # passes 1-3: reference based
    with_ref = lines[lines["ref"].notna() & lines["ref"].isin(b.index)]
    for ref, grp in with_ref.groupby("ref"):
        total = grp["amount"].sum()
        expected = b.at[ref, "amount"]
        if len(grp) == 1:
            rule = "reference" if abs(total - expected) < TOL else "reference+variance"
        else:
            rule = "split" if abs(total - expected) < TOL else "split+variance"
        link([ref], list(grp["line_id"]), rule)

    # pass 4: exact amount within a date window, unique both ways
    cand = lines[lines["line_id"].isin(open_lines) & lines["source"].notna()]
    ob = b.loc[sorted(open_batches)].assign(bpos=b_pos)
    ob["cents"] = (ob["amount"] * 100).round().astype("int64")
    cand = cand.assign(cents=(cand["amount"] * 100).round().astype("int64"))
    pairs = cand.merge(ob.reset_index()[["batch_id", "source", "bank", "cents", "bpos"]],
                       on=["source", "bank", "cents"], suffixes=("", "_exp"))
    pairs = pairs[(pairs["bpos"] - pairs["bpos_exp"]).between(-1, 5)]
    unique = pairs[~pairs["line_id"].duplicated(keep=False) & ~pairs["batch_id"].duplicated(keep=False)]
    for r in unique.itertuples():
        link([r.batch_id], [r.line_id], "amount+date")

    # pass 5: one unreferenced line = sum of 2-3 open batches from the same source
    cand = lines[lines["line_id"].isin(open_lines) & lines["source"].notna() & lines["ref"].isna()]
    ob = b.loc[sorted(open_batches)].assign(bpos=b_pos)
    for r in cand.itertuples():
        pool = ob[(ob["source"] == r.source) & (ob["bank"] == r.bank) & ob.index.isin(open_batches)
                  & (r.bpos - ob["bpos"]).between(0, 5)]
        if len(pool) < 2 or len(pool) > 40:
            continue
        hits = [combo for k in (2, 3) for combo in itertools.combinations(pool.index, k)
                if abs(pool.loc[list(combo), "amount"].sum() - r.amount) < TOL]
        if len(hits) == 1:
            link(list(hits[0]), [r.line_id], "netted")

    links = pd.DataFrame(links, columns=["batch_id", "line_id", "rule"])
    paid = links.merge(lines[["line_id", "amount", "value_date"]], on="line_id")
    # a netted line pays several batches; attribute it in proportion to expected amounts
    paid = paid.merge(batches[["batch_id", "amount"]].rename(columns={"amount": "expected"}), on="batch_id")
    per_line = paid.groupby("line_id")["expected"].transform("sum")
    paid["paid"] = paid["amount"] * paid["expected"] / per_line
    agg = paid.groupby("batch_id").agg(received=("paid", "sum"), received_date=("value_date", "max"),
                                       rule=("rule", "first"))
    status = batches.set_index("batch_id").join(agg)
    status["received"] = status["received"].fillna(0.0)
    status["variance"] = (status["amount"] - status["received"]).where(status["rule"].notna(), 0.0).round(2)
    status["matched"] = status["rule"].notna()
    line_status = lines.set_index("line_id")
    line_status["matched"] = line_status.index.isin(links["line_id"])
    return links, status.reset_index(), line_status.reset_index()


def score_against_truth(links, lines):
    """Precision: share of proposed links that are right. Recall: share of true links found."""
    truth = {(bid, r.line_id) for r in lines.itertuples() for bid in r.truth_batches}
    found = set(zip(links["batch_id"], links["line_id"]))
    tp = len(truth & found)
    return {"precision": tp / max(len(found), 1), "recall": tp / max(len(truth), 1),
            "true_links": len(truth), "proposed_links": len(found)}
