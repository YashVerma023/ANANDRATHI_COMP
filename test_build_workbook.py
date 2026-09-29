"""Self-check for cycle bounding + POS/INT classification. Run: python test_build_workbook.py"""

import pandas as pd

from build_workbook import (assign_cycles, avg_daily, backfill_allocation, classify,
                            cycle_mtm, normalise, sum_daily)


def rows(*specs):
    return pd.DataFrame(
        [{"Date": d, "Index": i, "DTE": t, "User ID": u, "Algo": 1} for d, i, t, u in specs]
    )


def types(df):
    c = classify(assign_cycles(df))
    return dict(zip(zip(c["Cycle"], c["User ID"]), c["Running Type"]))


def main() -> None:
    # Full NF cycle (Fri 4DTE -> Mon 1DTE -> Tue 0DTE) + full SX cycle (Wed -> Thu).
    t = types(
        rows(
            ("28-Aug-26", "NF", "4DTE", "A"),
            ("31-Aug-26", "NF", "1DTE", "A"),
            ("01-Sep-26", "NF", "0DTE", "A"),
            ("01-Sep-26", "NF", "0DTE", "B"),   # 0DTE only -> INT
            ("31-Aug-26", "NF", "1DTE", "C"),
            ("01-Sep-26", "NF", "0DTE", "C"),   # no 4DTE -> still POS
            ("02-Sep-26", "SX", "1DTE", "A"),
            ("03-Sep-26", "SX", "0DTE", "A"),
            ("03-Sep-26", "SX", "0DTE", "B"),
        )
    )
    assert t[("NF_2026-09-01", "A")] == "POS", t
    assert t[("NF_2026-09-01", "B")] == "INT", t
    assert t[("NF_2026-09-01", "C")] == "POS", t
    assert t[("SX_2026-09-03", "A")] == "POS", t
    assert t[("SX_2026-09-03", "B")] == "INT", t

    # Edge: cycle where only 0DTE ran (Wed holiday) -> everyone INT.
    t = types(rows(("10-Sep-26", "SX", "0DTE", "A"), ("10-Sep-26", "SX", "0DTE", "B")))
    assert set(t.values()) == {"INT"}, t

    # Two consecutive cycles must not merge: A is POS in cycle 1, INT in cycle 2.
    t = types(
        rows(
            ("31-Aug-26", "NF", "1DTE", "A"),
            ("01-Sep-26", "NF", "0DTE", "A"),
            ("08-Sep-26", "NF", "0DTE", "A"),
        )
    )
    assert t[("NF_2026-09-01", "A")] == "POS", t
    assert t[("NF_2026-09-08", "A")] == "INT", t

    # Backfill: 4DTE/1DTE allocation is overwritten by the cycle's 0DTE value,
    # per account, and does not leak across accounts or cycles.
    d = pd.DataFrame(
        [
            # A: NF cycle, all three days, allocations disagree -> all become 3 Cr
            ("28-Aug-26", "NF", "4DTE", "A", 10_000_000, 50_000),
            ("31-Aug-26", "NF", "1DTE", "A", 20_000_000, 50_000),
            ("01-Sep-26", "NF", "0DTE", "A", 30_000_000, 50_000),
            # B: different account, same cycle, must be untouched by A's value
            ("31-Aug-26", "NF", "1DTE", "B", 10_000_000, 10_000),
            ("01-Sep-26", "NF", "0DTE", "B", 10_000_000, 10_000),
            # A again, next cycle -> its own 0DTE value, not the previous cycle's
            ("08-Sep-26", "NF", "1DTE", "A", 10_000_000, 10_000),
            ("08-Sep-26", "NF", "0DTE", "A", 50_000_000, 10_000),
        ],
        columns=["Date", "Index", "DTE", "User ID", "Allocation", "MTM"],
    ).assign(Algo=1, Alias="x", **{"MTM %": 0.0})
    b = backfill_allocation(assign_cycles(d))

    def col(date, user, field):
        m = b[(b["Date"] == date) & (b["User ID"] == user)]
        return sorted(m[field].tolist())

    assert col("28-Aug-26", "A", "Allocation") == [30_000_000], b
    assert col("31-Aug-26", "A", "Allocation") == [30_000_000], b
    assert col("31-Aug-26", "B", "Allocation") == [10_000_000], b
    assert col("08-Sep-26", "A", "Allocation") == [50_000_000] * 2, b
    # MTM % recomputed against the new allocation
    assert col("28-Aug-26", "A", "MTM %") == [round(50_000 / 30_000_000 * 100, 2)], b

    # Account-cycle with no 0DTE row is dropped entirely; the rest survives.
    d2 = pd.DataFrame(
        [
            ("31-Aug-26", "NF", "1DTE", "C", 10_000_000, 1.0),  # no 0DTE -> dropped
            ("31-Aug-26", "NF", "1DTE", "D", 10_000_000, 1.0),
            ("01-Sep-26", "NF", "0DTE", "D", 20_000_000, 1.0),
        ],
        columns=["Date", "Index", "DTE", "User ID", "Allocation", "MTM"],
    ).assign(Algo=1, Alias="x", **{"MTM %": 0.0})
    b2 = backfill_allocation(assign_cycles(d2))
    assert set(b2["User ID"]) == {"D"}, b2
    assert b2["Allocation"].tolist() == [20_000_000] * 2, b2

    # Normalisation + cycle MTM%: a 2 Cr account must weigh the same as a 1 Cr one,
    # and a 3-day POS account the same as a 1-day INT account.
    d3 = pd.DataFrame(
        [
            # POS account P, 2 Cr, +1% each of 3 days -> 20L, 20L, 20L
            ("28-Aug-26", "NF", "4DTE", "P", 20_000_000, 200_000, "POS"),
            ("31-Aug-26", "NF", "1DTE", "P", 20_000_000, 200_000, "POS"),
            ("01-Sep-26", "NF", "0DTE", "P", 20_000_000, 200_000, "POS"),
            # INT account I, 1 Cr, +1% on its single day
            ("01-Sep-26", "NF", "0DTE", "I", 10_000_000, 100_000, "INT"),
        ],
        columns=["Date", "Index", "DTE", "User ID", "Allocation", "MTM", "Running Type"],
    ).assign(Algo=1, Cycle="NF_2026-09-01", Month="SEP-26", _month_sort="2026-09")
    c = cycle_mtm(normalise(d3)).set_index("Running Type")
    # P: 3 days x 1% on a normalised 1 Cr -> 3% for the cycle
    assert c.loc["POS", "Cycle MTM %"] == 3.0, c
    assert c.loc["POS", "Total_Allocation"] == 10_000_000, c
    # I: single day, 1%
    assert c.loc["INT", "Cycle MTM %"] == 1.0, c

    # Two 1 Cr accounts at +1% must still give 1%, not 2% (avg, not total).
    d4 = pd.DataFrame(
        [
            ("01-Sep-26", "NF", "0DTE", "X", 10_000_000, 100_000, "INT"),
            ("01-Sep-26", "NF", "0DTE", "Y", 50_000_000, 500_000, "INT"),
        ],
        columns=["Date", "Index", "DTE", "User ID", "Allocation", "MTM", "Running Type"],
    ).assign(Algo=1, Cycle="NF_2026-09-01", Month="SEP-26", _month_sort="2026-09")
    assert cycle_mtm(normalise(d4))["Cycle MTM %"].tolist() == [1.0], "size-weighting leaked"

    # A user counts its full allocation once per cycle however many days it ran,
    # and avg_daily divides by the MEAN of the cycle allocations.
    d5 = pd.DataFrame(
        [
            # Cycle 1: user P alone, 3 days, 1L/day -> alloc 1 Cr, MTM 3L -> 3%
            ("28-Aug-26", "NF", "4DTE", "P", 10_000_000, 100_000, "POS", "NF_2026-09-01"),
            ("31-Aug-26", "NF", "1DTE", "P", 10_000_000, 100_000, "POS", "NF_2026-09-01"),
            ("01-Sep-26", "NF", "0DTE", "P", 10_000_000, 100_000, "POS", "NF_2026-09-01"),
            # Cycle 2: P runs 2 days, Q only 1 -> alloc 2 Cr (Q still counts 1 Cr
            # despite its single day), MTM 2L -> 1%
            ("07-Sep-26", "NF", "1DTE", "P", 10_000_000, 50_000, "POS", "NF_2026-09-08"),
            ("08-Sep-26", "NF", "0DTE", "P", 10_000_000, 50_000, "POS", "NF_2026-09-08"),
            ("08-Sep-26", "NF", "0DTE", "Q", 10_000_000, 100_000, "POS", "NF_2026-09-08"),
        ],
        columns=["Date", "Index", "DTE", "User ID", "Allocation", "MTM", "Running Type", "Cycle"],
    ).assign(Algo=1, Month="SEP-26", _month_sort="2026-09")
    c5 = cycle_mtm(normalise(d5)).set_index("Cycle")
    assert c5.loc["NF_2026-09-08", "Total_Allocation"] == 20_000_000, c5
    # 5L total MTM / mean(1 Cr, 2 Cr) = 3.3333%
    got = avg_daily(normalise(d5), ["Algo"]).loc[1, "POS"]
    assert round(got, 4) == 3.3333, got
    # sum_daily adds the cycle ratios instead: 3% + 1% = 4%
    assert round(sum_daily(normalise(d5), ["Algo"]).loc[1, "POS"], 4) == 4.0

    print("all checks passed")


if __name__ == "__main__":
    main()
