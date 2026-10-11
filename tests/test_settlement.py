import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from settlement import run as R
from settlement.data import START, business_days
from settlement.liquidity import COLL_TARGET, PREFUND
from settlement.matching import reconcile, score_against_truth
from settlement.metrics import exceptions_as_of, snapshots

END = "2026-06-30"


class Built:
    r = None

    @classmethod
    def get(cls):
        if cls.r is None:
            import settlement.data as d
            batches, lines, truth, bdays = d.generate(seed=3, end=END)
            from settlement.metrics import manual_resolution
            from settlement.liquidity import simulate
            links, status, line_status = reconcile(batches, lines, bdays)
            status, line_status = manual_resolution(status, line_status, truth, bdays)
            liq = simulate(batches, lines, bdays, START, END)
            cls.r = dict(batches=batches, lines=lines, truth=truth, bdays=bdays, links=links,
                         status=status, line_status=line_status, liq=liq)
        return cls.r


def frame(rows, cols):
    return pd.DataFrame(rows, columns=cols)


class TestMatching(unittest.TestCase):
    def setUp(self):
        self.bdays = business_days("2026-05-01", "2026-05-29")
        d = pd.Timestamp("2026-05-12")
        self.batches = frame([
            ("VI20260511-01", "VI", "Bank A", d, d, 1000.00),
            ("AC20260511-01", "AC", "Bank B", d, d, 300.00),
            ("AC20260511-02", "AC", "Bank B", d, d, 450.25),
            ("MC20260511-01", "MC", "Bank A", d, d, 800.00),
        ], ["batch_id", "source", "bank", "sale_date", "expected_date", "amount"])
        self.lines = frame([
            ("L1", d, 990.00, "VISA NET SETTLEMENT REF VI20260511-01", "Bank A", ["VI20260511-01"]),
            ("L2", d, 750.25, "ACH CREDIT ORIG BANK B DEPOSIT", "Bank B", ["AC20260511-01", "AC20260511-02"]),
            ("L3", d + pd.Timedelta(days=1), 800.00, "MASTERCARD SETTLE BANK A DEPOSIT", "Bank A", ["MC20260511-01"]),
            ("L4", d, 5000.00, "INCOMING WIRE ORIG UNKNOWN", "Bank A", []),
        ], ["line_id", "value_date", "amount", "description", "bank", "truth_batches"])

    def test_each_pass(self):
        links, status, lines = reconcile(self.batches, self.lines, self.bdays)
        rule = dict(zip(links["batch_id"], links["rule"]))
        self.assertEqual(rule["VI20260511-01"], "reference+variance")
        self.assertEqual(rule["AC20260511-01"], "netted")
        self.assertEqual(rule["AC20260511-02"], "netted")
        self.assertEqual(rule["MC20260511-01"], "amount+date")
        self.assertFalse(lines.set_index("line_id").at["L4", "matched"])
        var = status.set_index("batch_id").at["VI20260511-01", "variance"]
        self.assertAlmostEqual(var, 10.00)

    def test_engine_never_proposes_a_wrong_link(self):
        r = Built.get()
        acc = score_against_truth(r["links"], r["lines"])
        self.assertEqual(acc["precision"], 1.0)
        self.assertGreater(acc["recall"], 0.99)

    def test_short_payments_carry_their_variance(self):
        r = Built.get()
        s = r["status"].merge(r["truth"], on="batch_id")
        flagged = s[s["rule"] == "reference+variance"]
        self.assertGreater(len(flagged), 0)
        np.testing.assert_allclose(flagged["variance"], flagged["short"], atol=0.02)


class TestAsOf(unittest.TestCase):
    def test_no_future_items_in_a_snapshot(self):
        r = Built.get()
        pos = pd.Series(np.arange(len(r["bdays"])), index=r["bdays"])
        claims = r["truth"].set_index("batch_id")["resolved_date"]
        d = pd.Timestamp("2026-05-20")
        ex = exceptions_as_of(d, r["status"], r["line_status"], claims, pos)
        self.assertTrue((ex["age"] >= 0).all())


class TestLiquidity(unittest.TestCase):
    def test_policy_holds(self):
        liq = Built.get()["liq"]
        ok = liq["prefund_shortfall"] == 0
        self.assertTrue((liq.loc[ok, "PAYOUT"] >= PREFUND * liq.loc[ok, "payouts_tomorrow"] - 0.01).all())
        self.assertTrue((liq[["A_COLL", "B_COLL"]] <= COLL_TARGET + 0.01).all().all())
        self.assertTrue((liq["RESERVE"] >= liq["restricted"]).all())


class TestSQL(unittest.TestCase):
    def test_sql_matches_python(self):
        r = Built.get()
        with tempfile.TemporaryDirectory() as tmp:
            con = R.load_sqlite(r, Path(tmp) / "t.db")
            res = R.run_sql(con, as_of="2026-05-20")
            snaps = snapshots(r["status"], r["line_status"], r["truth"], r["liq"], r["bdays"])
            snap = next(s for s in snaps if s["date"] == "2026-05-20")
            self.assertAlmostEqual(res["open_exceptions_aging"]["total"].sum(), snap["types"]["Not received"], delta=5)
            sql_cov = res["liquidity_coverage"].iloc[0]["coverage_days"]
            self.assertAlmostEqual(sql_cov, r["liq"]["coverage_days"].iloc[-1], places=2)
            con.close()


if __name__ == "__main__":
    unittest.main()
